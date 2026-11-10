"""Owner-requested test 2026-10-24: does adding an edge's own thesis (event) fields to its meta-model help?

Paired comparison on the SAME purged + embargoed 10-fold splits and the SAME sequential-bootstrap draws:
  base   = the 157 generic Step 01 candidate features
  thesis = base + the edge's own event fields (numeric fields of its candidate-events file that are not generic
           features, not label metadata, not constant, and stationary by ADF p < 0.05 (AFML Ch.5), so raw price levels
           are excluded)
Estimator: approved Step 01 importance model (MLAM Snippet 6.8 trees, max_features=1, balanced, early stopping
min_weight_fraction_leaf 0.05 per D041, 1,000 trees, avgU x n sequential bootstrap per D039).
Reports per edge: OOS weighted neg log loss and AUC for both sets, the paired fold-level difference (mean, se, t), and the
clustered MDA of the thesis group (all thesis fields shuffled independently) with rule B, plus per-thesis-field MDA.
This is a research trial (it informs a design decision) and is logged as such.

Run from trading_models_v2:
    python ./shared/research/thesis_feature_test.py --edge XAUUSD/rangestructure_long --workers 2
Writes assets/<A>/edges/<edge>/artifacts/feature_importance_audit/thesis_feature_test.json.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import sys
import time
import warnings
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
Z = 1.645
META_FIELDS = {"side", "Event_Vol", "event_known_after_close", "uses_future_information", "primary_side",
               "primary_side_filter", "raw_side_from_structure_bias"}


def _job(a: dict) -> dict:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sklearn.tree import DecisionTreeClassifier

    from shared.features import clustered_feature_importance as cfi

    train, test = a["train"], a["test"]
    draws, seeds = cfi._draws(a["start"][train], a["end"][train], a["n_bars"], N_TREES, a["seed"])
    draws = [train[d] for d in draws]
    y, w = a["y"], a["w"]
    rng = np.random.default_rng(a["seed"] + 7)
    out = {}
    for name, x in (("base", a["x_base"]), ("thesis", a["x_thesis"])):
        trees = [DecisionTreeClassifier(**cfi.TREE_PARAMS, random_state=s).fit(x[d], y[d], sample_weight=w[d])
                 for d, s in zip(draws, seeds)]

        def prob(arr):
            p = np.zeros(len(arr))
            for t in trees:
                p += t.predict_proba(arr)[:, list(t.classes_).index(1)] if 1 in t.classes_ else 0.0
            return p / len(trees)

        x1 = x[test]
        p1 = prob(x1)
        rec = {"scr0": cfi._neg_log_loss(y[test], p1, w[test]), "prob": p1}
        if name == "thesis":
            groups = {"THESIS": a["thesis_cols"], **{f"T:{n}": [c] for n, c in zip(a["thesis_names"], a["thesis_cols"])}}
            rec["scr1"] = {}
            for g, cols in groups.items():
                x1_ = x1.copy()
                for c in cols:
                    x1_[:, c] = x1_[rng.permutation(len(x1_)), c]
                rec["scr1"][g] = cfi._neg_log_loss(y[test], prob(x1_), w[test])
        out[name] = rec
    return out


def thesis_fields(root: Path, df: pd.DataFrame, generic: list[str]) -> tuple[list[str], dict[str, str]]:
    from statsmodels.tsa.stattools import adfuller

    ev_path = glob.glob(str(root / "artifacts" / "research_dataset" / "*_candidate_events.csv"))[0]
    ev = pd.read_csv(ev_path)
    ev["Datetime"] = to_datetime_iso(ev["Datetime"], utc=True)
    ev = ev.set_index("Datetime")
    reasons, keep = {}, []
    for c in ev.columns:
        if c in generic or c in META_FIELDS or not pd.api.types.is_numeric_dtype(ev[c]):
            continue
        s = ev[c].reindex(df.index)
        if s.notna().mean() < 0.95:
            reasons[c] = "missing_on_model_rows"
            continue
        if s.nunique(dropna=True) <= 1:
            reasons[c] = "constant_setting_or_flag"
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p = adfuller(s.ffill().bfill().to_numpy(dtype="float64"))[1]
        if p >= 0.05:
            reasons[c] = f"non_stationary_adf_p={p:.3f}"
            continue
        keep.append(c)
    return keep, reasons


def run(edge_id: str, workers: int, seed: int = 0) -> dict:
    import importlib.util

    from shared.features import clustered_feature_importance as cfi
    from shared.models.afml_sample_weights import label_end_times, snap_to_bar_clock

    t0_wall = time.time()
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
    spec = importlib.util.spec_from_file_location("s01", root / "research/01_feature_surgery_clustered_mda.py")
    m = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
        df = m.load_model_rows()
        _fx, cands, _ = m.choose_candidate_features(df, m.load_feature_schema_allowed_features())
        bars = pd.read_csv(m._resolve_standard_quote_tick_bars_path(), usecols=["Datetime"])
    sys.argv = argv
    thesis, excluded = thesis_fields(root, df, cands)
    ev = pd.read_csv(glob.glob(str(root / "artifacts" / "research_dataset" / "*_candidate_events.csv"))[0])
    ev["Datetime"] = to_datetime_iso(ev["Datetime"], utc=True)
    ev = ev.set_index("Datetime")
    for c in thesis:
        df[f"THESIS__{c}"] = ev[c].reindex(df.index)
    tcols = [f"THESIS__{c}" for c in thesis]
    df = df.dropna(subset=cands + tcols)
    with redirect_stdout(io.StringIO()):
        w = np.asarray(m._afml_return_weights(df), dtype="float64")
    w = w / w.mean()
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars["Datetime"], utc=True))
    start = bar_index.get_indexer(pd.DatetimeIndex(df.index))
    ends = label_end_times(df.reset_index())
    end = snap_to_bar_clock(ends, bar_index)
    y = df[m.TARGET_COL].astype(int).to_numpy()
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = pd.DatetimeIndex(ends).values.astype("datetime64[ns]")
    splits = cfi.purged_kfold_splits(t0, t1, N_SPLITS, int(m.embargo_size_for(len(df))))
    x_base = df[cands].to_numpy(dtype="float64")
    x_thesis = df[cands + tcols].to_numpy(dtype="float64")
    base = dict(x_base=x_base, x_thesis=x_thesis, y=y, w=w, start=start, end=end, n_bars=len(bar_index),
                thesis_cols=list(range(len(cands), len(cands) + len(tcols))), thesis_names=thesis)
    jobs = [dict(base, train=tr, test=te, seed=seed + k) for k, (tr, te) in enumerate(splits)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_job, jobs))

    from sklearn.metrics import roc_auc_score

    oos = {}
    for nm in ("base", "thesis"):
        p = np.full(len(y), np.nan)
        for (tr, te), r in zip(splits, res):
            p[te] = r[nm]["prob"]
        oos[nm] = {"neg_log_loss_weighted": round(float(np.mean([r[nm]["scr0"] for r in res])), 5),
                   "auc": round(float(roc_auc_score(y, p)), 4)}
    diff = pd.Series([r["thesis"]["scr0"] - r["base"]["scr0"] for r in res])
    s0 = pd.Series([r["thesis"]["scr0"] for r in res])
    s1 = pd.DataFrame([r["thesis"]["scr1"] for r in res])
    imp = (-s1).add(s0, axis=0) / (-s1)
    mda = pd.concat({"mean": imp.mean(), "se": imp.std() * imp.shape[0] ** -0.5}, axis=1)
    mda["t"] = mda["mean"] / mda["se"].replace(0, np.nan)
    per_field = mda.drop("THESIS").sort_values("mean", ascending=False)
    out = {
        "edge_id": edge_id, "rows": int(len(df)), "generic_features": len(cands), "thesis_fields": thesis,
        "thesis_fields_excluded": excluded, "estimator": "Snippet 6.8 trees, mwfl 0.05 (D041), 1,000 trees, purged 10-fold",
        "oos": oos,
        "paired_fold_difference_thesis_minus_base": {"mean": round(float(diff.mean()), 5),
                                                     "se": round(float(diff.std() / np.sqrt(len(diff))), 5),
                                                     "t": round(float(diff.mean() / (diff.std() / np.sqrt(len(diff)))), 2),
                                                     "folds_better": int((diff > 0).sum()), "folds": int(len(diff))},
        "thesis_group_clustered_mda": {k: round(float(v), 5) for k, v in mda.loc["THESIS"].items()},
        "thesis_group_rule_B_kept": bool(mda.loc["THESIS", "mean"] - Z * mda.loc["THESIS", "se"] > 0),
        "per_thesis_field_mda": {k.replace("T:", ""): {kk: round(float(vv), 5) for kk, vv in row.items()}
                                 for k, row in per_field.iterrows()},
        "seconds": round(time.time() - t0_wall, 1),
    }
    dest = root / "artifacts" / "feature_importance_audit" / "thesis_feature_test.json"
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", required=True)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    r = run(a.edge, a.workers)
    d = r["paired_fold_difference_thesis_minus_base"]
    print(f"{r['edge_id']} | thesis fields {len(r['thesis_fields'])} | OOS wNLL base {r['oos']['base']['neg_log_loss_weighted']:+.4f} "
          f"thesis {r['oos']['thesis']['neg_log_loss_weighted']:+.4f} | AUC base {r['oos']['base']['auc']} thesis {r['oos']['thesis']['auc']} "
          f"| paired diff {d['mean']:+.4f} (t {d['t']:+.2f}, {d['folds_better']}/{d['folds']} folds better) "
          f"| thesis group MDA {r['thesis_group_clustered_mda']} kept={r['thesis_group_rule_B_kept']} | {r['seconds']} s", flush=True)
    print("top thesis fields:", list(r["per_thesis_field_mda"].items())[:5], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
