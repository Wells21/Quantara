"""Fast config test for clustered feature importance: tree early stopping (AFML s.6.4 p.98) on an edge (diagnostic).

Owner-approved test 2026-10-24: MLAM Snippet 6.8 grows trees fully (min_weight_fraction_leaf=0); AFML p.98 offers early
stopping, "min_weight_fraction_leaf to a sufficiently large value (e.g., 5%)". Compares min_weight_fraction_leaf 0 vs
0.05, raw vs MLAM p.85 residual features, on the approved method (VI clusters from the edge's
feature_importance_audit.json, rule B, 1,000 trees, 10-fold purged + embargoed CV, sequential bootstrap avgU x n).

Speed: clusters reused from the audit (seeded, identical); each fold's sequential-bootstrap draws are computed once and
shared by every configuration; folds run in parallel worker processes.

Run from trading_models_v2:
    python ./shared/research/feature_importance_config_test.py --edge GBPJPY/dailyrangecompletionfade_long --workers 4
Writes assets/<A>/edges/<edge>/artifacts/feature_importance_audit/config_test_min_weight_fraction_leaf.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

N_TREES = 1000
N_SPLITS = 10
MWFL = (0.0, 0.05)
Z = 1.645


def _job(args: dict) -> dict:
    """One fold (or the full fit): shared bootstrap draws, every config fit on them, MDA scores per cluster."""
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier

    from shared.features import clustered_feature_importance as cfi

    train, test = args["train"], args["test"]
    start, end, n_bars = args["start"], args["end"], args["n_bars"]
    draws, seeds = cfi._draws(start[train], end[train], n_bars, N_TREES, args["seed"])
    draws = [train[d] for d in draws]
    y, w = args["y"], args["w"]
    rng = np.random.default_rng(args["seed"] + 7)
    out = {}
    for name, x in (("raw", args["x"]), ("residualized", args["xr"])):
        for mw in MWFL:
            params = {**cfi.TREE_PARAMS, "min_weight_fraction_leaf": mw}
            trees = [DecisionTreeClassifier(**params, random_state=s).fit(x[d], y[d], sample_weight=w[d])
                     for d, s in zip(draws, seeds)]
            key = f"{name}|mwfl={mw}"
            if test is None:  # full fit -> per-tree importances for clustered MDI
                out[key] = {"importances": np.array([t.feature_importances_ for t in trees])}
                continue

            def prob(a):
                p = np.zeros(len(a))
                for t in trees:
                    p += t.predict_proba(a)[:, list(t.classes_).index(1)] if 1 in t.classes_ else 0.0
                return p / len(trees)

            x1 = x[test]
            s0 = cfi._neg_log_loss(y[test], prob(x1), w[test])
            s1 = {}
            for g, cols in args["groups"].items():
                x1_ = x1.copy()
                for c in cols:
                    x1_[:, c] = x1_[rng.permutation(len(x1_)), c]
                s1[g] = cfi._neg_log_loss(y[test], prob(x1_), w[test])
            out[key] = {"scr0": s0, "scr1": s1}
    return out


def run(edge_id: str, workers: int, seed: int = 0) -> dict:
    import importlib.util
    import io
    from contextlib import redirect_stdout

    from shared.features import clustered_feature_importance as cfi
    from shared.models.afml_sample_weights import label_end_times, snap_to_bar_clock

    t_start = time.time()
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
    audit = json.loads((root / "artifacts/feature_importance_audit/feature_importance_audit.json").read_text(encoding="utf-8"))
    clusters = {int(k.split("_")[1]): v for k, v in audit["variants"]["vi"]["clusters"].items()}
    spec = importlib.util.spec_from_file_location("s01", root / "research/01_feature_surgery_clustered_mda.py")
    m = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
        df = m.load_model_rows()
        _fx, cands, _ = m.choose_candidate_features(df, m.load_feature_schema_allowed_features())
        df = df.dropna(subset=cands)
        w = np.asarray(m._afml_return_weights(df), dtype="float64")
        bars = pd.read_csv(m._resolve_standard_quote_tick_bars_path(), usecols=["Datetime"])
    sys.argv = argv
    w = w / w.mean()
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars["Datetime"], utc=True))
    start = bar_index.get_indexer(pd.DatetimeIndex(df.index))
    ends = label_end_times(df.reset_index())
    end = snap_to_bar_clock(ends, bar_index)
    x = df[cands].astype("float64")
    xr = cfi.residualize(x, clusters)
    y = df[m.TARGET_COL].astype(int).to_numpy()
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = pd.DatetimeIndex(ends).values.astype("datetime64[ns]")
    splits = cfi.purged_kfold_splits(t0, t1, N_SPLITS, int(m.embargo_size_for(len(df))))
    col = {c: i for i, c in enumerate(cands)}
    groups = {f"C_{i}": [col[c] for c in mm] for i, mm in clusters.items()}
    base = dict(x=x.to_numpy(), xr=xr.to_numpy(), y=y, w=w, start=start, end=end, n_bars=len(bar_index), groups=groups)
    jobs = [dict(base, train=tr, test=te, seed=seed + k) for k, (tr, te) in enumerate(splits)]
    jobs.append(dict(base, train=np.arange(len(df)), test=None, seed=seed + 99))
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_job, jobs))
    folds, full = results[:-1], results[-1]

    report = {"edge_id": edge_id, "rows": int(len(df)), "features": len(cands), "clusters": {f"C_{i}": len(mm) for i, mm in clusters.items()},
              "method": "VI clusters (feature_importance_audit.json), rule B z=1.645, 1,000 trees, 10-fold purged+embargoed",
              "configs": {}}
    for key in folds[0]:
        s0 = pd.Series([f[key]["scr0"] for f in folds])
        s1 = pd.DataFrame([f[key]["scr1"] for f in folds])
        imp = (-s1).add(s0, axis=0) / (-s1)
        mda = pd.concat({"mean": imp.mean(), "std": imp.std() * imp.shape[0] ** -0.5}, axis=1)
        imps = pd.DataFrame(full[key]["importances"], columns=cands).replace(0, np.nan)
        mdi = pd.DataFrame({f"C_{i}": imps[mm].sum(axis=1) for i, mm in clusters.items()})
        mdi = pd.concat({"mean": mdi.mean(), "std": mdi.std() * mdi.shape[0] ** -0.5}, axis=1)
        mdi = mdi / mdi["mean"].sum()
        kept = [c for c in mda.index if mda.loc[c, "mean"] - Z * mda.loc[c, "std"] > 0]
        report["configs"][key] = {
            "oos_neg_log_loss_weighted": round(float(s0.mean()), 5),
            "clustered_mda": {c: {"mean": round(float(mda.loc[c, "mean"]), 5), "se": round(float(mda.loc[c, "std"]), 5),
                                  "t": round(float(mda.loc[c, "mean"] / mda.loc[c, "std"]) if mda.loc[c, "std"] > 0 else 0.0, 2)}
                              for c in mda.index},
            "clustered_mdi": {c: round(float(mdi.loc[c, "mean"]), 4) for c in mdi.index},
            "rule_B_kept_clusters": kept,
            "rule_B_kept_features": int(sum(len(clusters[int(c.split('_')[1])]) for c in kept)),
        }
    report["seconds"] = round(time.time() - t_start, 1)
    dest = root / "artifacts/feature_importance_audit/config_test_min_weight_fraction_leaf.json"
    dest.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", required=True)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    r = run(args.edge, args.workers)
    for key, c in r["configs"].items():
        print(f"{r['edge_id']} | {key:24s} | OOS wNLL {c['oos_neg_log_loss_weighted']:+.4f} | rule B kept {c['rule_B_kept_features']:3d} "
              f"| t-stats {[v['t'] for v in c['clustered_mda'].values()]}", flush=True)
    print(f"{r['edge_id']} | {r['seconds']} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
