"""Owner-requested test 2026-10-24: does repeating the MDA shuffle within each fold stabilise rule-B decisions?

MLAM Snippet 6.5 (p.87) shuffles each cluster once per fold. Here, with the Step 01 setup (VI clusters, Snippet 6.8 trees
mwfl 0.05, 1,000 trees, 10-fold purged + embargoed, sequential bootstrap), the trees of each fold are fitted ONCE and the
clustered MDA is recomputed for K shuffles per fold (K = 1 book, 5, 10; scr1 averaged over the K shuffles) under 10
different random permutation seeds. For each cluster and K: mean/sd of the MDA t-stat across seeds and the share of
seeds in which rule B keeps it; per K: share of seeds giving the modal selected set.

Run from trading_models_v2:
    python ./shared/research/shuffle_stability_test.py --edge XAUUSD/rangestructure_long --workers 2
Writes assets/<A>/edges/<edge>/artifacts/feature_importance_audit/shuffle_stability_test.json.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
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
K_VALUES = (1, 5, 10)
N_SEEDS = 10
Z = 1.645


def _job(a: dict) -> dict:
    import numpy as np
    from sklearn.tree import DecisionTreeClassifier

    from shared.features import clustered_feature_importance as cfi

    train, test = a["train"], a["test"]
    draws, seeds = cfi._draws(a["start"][train], a["end"][train], a["n_bars"], N_TREES, a["seed"])
    draws = [train[d] for d in draws]
    x, y, w = a["x"], a["y"], a["w"]
    trees = [DecisionTreeClassifier(**cfi.TREE_PARAMS, random_state=s).fit(x[d], y[d], sample_weight=w[d]) for d, s in zip(draws, seeds)]

    def prob(arr):
        p = np.zeros(len(arr))
        for t in trees:
            p += t.predict_proba(arr)[:, list(t.classes_).index(1)] if 1 in t.classes_ else 0.0
        return p / len(trees)

    x1, y1, w1 = x[test], y[test], w[test]
    s0 = cfi._neg_log_loss(y1, prob(x1), w1)
    # scores[seed][g] = list of K_max shuffled scores; K-averages are prefixes of the same shuffle sequence
    kmax = max(K_VALUES)
    scores = {}
    for sd in range(N_SEEDS):
        rng = np.random.default_rng(1000 * (a["fold"] + 1) + sd)
        scores[sd] = {}
        for g, cols in a["groups"].items():
            vals = []
            for _ in range(kmax):
                x1_ = x1.copy()
                for c in cols:
                    x1_[:, c] = x1_[rng.permutation(len(x1_)), c]
                vals.append(cfi._neg_log_loss(y1, prob(x1_), w1))
            scores[sd][g] = vals
    return {"scr0": s0, "scores": scores}


def run(edge_id: str, workers: int, seed: int = 0) -> dict:
    import importlib.util

    from shared.features import clustered_feature_importance as cfi
    from shared.models.afml_sample_weights import label_end_times, snap_to_bar_clock

    t_start = time.time()
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
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
    x = df[cands].astype("float64")
    obs, _diag = cfi.observation_matrix(x, cfi.STEP01_METHOD)
    clusters, _silh = cfi.onc_top(obs, max_k=None, n_init=10, seed=seed)
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars["Datetime"], utc=True))
    start = bar_index.get_indexer(pd.DatetimeIndex(df.index))
    ends = label_end_times(df.reset_index())
    end = snap_to_bar_clock(ends, bar_index)
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = pd.DatetimeIndex(ends).values.astype("datetime64[ns]")
    splits = cfi.purged_kfold_splits(t0, t1, N_SPLITS, int(m.embargo_size_for(len(df))))
    col = {c: i for i, c in enumerate(cands)}
    groups = {f"C_{i}": [col[c] for c in mm] for i, mm in clusters.items()}
    base = dict(x=x.to_numpy(), y=df[m.TARGET_COL].astype(int).to_numpy(), w=w, start=start, end=end,
                n_bars=len(bar_index), groups=groups)
    jobs = [dict(base, train=tr, test=te, seed=seed + k, fold=k) for k, (tr, te) in enumerate(splits)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_job, jobs))

    s0 = pd.Series([r["scr0"] for r in res])
    out = {"edge_id": edge_id, "clusters": {g: len(clusters[int(g.split('_')[1])]) for g in groups}, "k_values": list(K_VALUES),
           "n_seeds": N_SEEDS, "results": {}}
    for k in K_VALUES:
        per_cluster = {g: [] for g in groups}
        selections = []
        for sd in range(N_SEEDS):
            s1 = pd.DataFrame([{g: float(np.mean(r["scores"][sd][g][:k])) for g in groups} for r in res])
            imp = (-s1).add(s0, axis=0) / (-s1)
            mean, se = imp.mean(), imp.std() / np.sqrt(len(imp))
            kept = []
            for g in groups:
                t = float(mean[g] / se[g]) if se[g] > 0 else 0.0
                per_cluster[g].append((t, bool(mean[g] - Z * se[g] > 0)))
                if mean[g] - Z * se[g] > 0:
                    kept.append(g)
            selections.append(tuple(sorted(kept)))
        modal, count = Counter(selections).most_common(1)[0]
        out["results"][f"K={k}"] = {
            "clusters": {g: {"t_mean": round(float(np.mean([v[0] for v in vals])), 2),
                             "t_sd": round(float(np.std([v[0] for v in vals])), 2),
                             "kept_share": round(float(np.mean([v[1] for v in vals])), 2)} for g, vals in per_cluster.items()},
            "modal_selection": list(modal), "modal_selection_share": round(count / N_SEEDS, 2),
        }
    out["seconds"] = round(time.time() - t_start, 1)
    dest = root / "artifacts" / "feature_importance_audit" / "shuffle_stability_test.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", required=True)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    r = run(a.edge, a.workers)
    for k, rec in r["results"].items():
        cl = "; ".join(f"{g}(n{r['clusters'][g]}) t {v['t_mean']}±{v['t_sd']} kept {v['kept_share']:.0%}" for g, v in rec["clusters"].items())
        print(f"{r['edge_id']} | {k:5s} | same selection in {rec['modal_selection_share']:.0%} of seeds | {cl}", flush=True)
    print(f"{r['edge_id']} | {r['seconds']} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
