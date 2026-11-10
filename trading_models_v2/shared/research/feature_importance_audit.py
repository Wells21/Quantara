"""MLAM Ch.6 clustered feature importance + AFML Ch.8 diagnostics on an edge's research dataset (read-only audit).

For one edge (Step 01's own data loading, candidate features, AFML Ch.4 return weights, label t1):
  * clustering variants (shared/features/clustered_feature_importance.py):
      current      = denoised Pearson correlation, sqrt((1-rho)/2), no detone, no PCA reduction (old Step 01, for reference)
      corr_signed  = MLAM full recipe: denoise + detone + sqrt((1-rho)/2) + PCA-reduced observation matrix
      corr_abs     = same with sqrt(1-|rho|) (MLAM p.39)
      vi           = normalised variation of information (MLAM Snippet 3.3), PCA-reduced
    each also with MLAM p.85 residual features (refit on residualised X)
  * clustered MDA (10-fold purged + embargoed, weighted neg log loss), clustered MDI (full fit), MLAM Snippet 6.8 estimator
  * selection rules A (MDA > 0), B (MDA - 1.645 se > 0), C (clustered MDI > cluster share under the null)
  * per-feature MDA, MDI, SFI (AFML Snippets 8.2-8.4) and the PCA weighted-Kendall check (AFML Snippets 8.5-8.6)
Writes assets/<A>/edges/<edge>/artifacts/feature_importance_audit/feature_importance_audit.json.

Run from trading_models_v2 (venv312), one process per edge in parallel:
    python ./shared/research/feature_importance_audit.py --edge XAUUSD/rangestructure_long
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.features import clustered_feature_importance as cfi  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.models.afml_sample_weights import label_end_times, snap_to_bar_clock  # noqa: E402

N_SPLITS = 10
N_TREES = 1000
N_TREES_SFI = 1000  # AFML Snippet 8.8: same 1,000-tree classifier for SFI


def _load_step01(root: Path):
    spec = importlib.util.spec_from_file_location(f"step01_{root.name}", root / "research" / "01_feature_surgery_clustered_mda.py")
    m = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(m)
    finally:
        sys.argv = argv
    return m


def run(edge_id: str, seed: int = 0) -> dict:
    t_start = time.time()
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
    m = _load_step01(root)
    with redirect_stdout(io.StringIO()):
        df = m.load_model_rows()
        fixed, cands, _ = m.choose_candidate_features(df, m.load_feature_schema_allowed_features())
        df = df.dropna(subset=cands)
        w = m._afml_return_weights(df)
        bars = pd.read_csv(m._resolve_standard_quote_tick_bars_path(), usecols=["Datetime"])
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars["Datetime"], utc=True))
    start = bar_index.get_indexer(pd.DatetimeIndex(df.index))
    if (start < 0).any():
        return {"edge_id": edge_id, "status": "STALE_BAR_CLOCK", "rows_not_on_bar_clock": int((start < 0).sum())}
    ends = label_end_times(df.reset_index())
    end = snap_to_bar_clock(ends, bar_index)
    x = df[cands].astype("float64")
    y = df[m.TARGET_COL].astype(int).to_numpy()
    w = np.asarray(w, dtype="float64") / np.mean(w)
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = pd.DatetimeIndex(ends).values.astype("datetime64[ns]")
    splits = cfi.purged_kfold_splits(t0, t1, N_SPLITS, int(m.embargo_size_for(len(df))))
    out: dict = {"edge_id": edge_id, "status": "OK", "version": cfi.CFI_VERSION, "rows": int(len(df)),
                 "features": len(cands), "fixed_features": fixed, "n_splits": N_SPLITS, "n_trees": N_TREES,
                 "estimator": "MLAM Snippet 6.8 / AFML 8.8 bagged entropy trees, max_features=1, balanced, "
                              "sequential bootstrap avgU x n"}

    # ---- fits on the raw features (shared by every clustering variant and the per-feature MDA)
    bags, fold_draws = cfi.fit_fold_bags(x, y, w, splits, start, end, len(bar_index), N_TREES, seed)
    d, s = cfi._draws(start, end, len(bar_index), N_TREES, seed + 99)
    full = cfi._fit_bag(x.to_numpy(), y, w, d, s)
    per_mda, base_score = cfi.mda(x, y, w, splits, bags, {c: [c] for c in cands}, seed)
    per_mdi = cfi.mdi([full], cands)
    out["oos_neg_log_loss_weighted"] = round(base_score, 5)
    out["per_feature"] = {"mda": per_mda.round(6).to_dict(orient="index"), "mdi": per_mdi.round(6).to_dict(orient="index")}
    out["timing_s"] = {"fits": round(time.time() - t_start, 1)}

    # ---- clustering variants
    variants = {
        "current": dict(method="corr_signed", denoise=True, detone=False, reduce=False),
        "corr_signed": dict(method="corr_signed"),
        "corr_abs": dict(method="corr_abs"),
        "vi": dict(method="vi"),
    }
    out["variants"] = {}
    for vname, kw in variants.items():
        obs, diag = cfi.observation_matrix(x, kw.pop("method"), **kw)
        clusters, silh = cfi.onc_top(obs, max_k=None, n_init=10, seed=seed)
        groups = {f"C_{i}": mm for i, mm in clusters.items()}
        cmda, _ = cfi.mda(x, y, w, splits, bags, groups, seed)
        cmdi = cfi.mdi([full], cands, clusters)
        keep = cfi.select(cmda, cmdi, clusters, len(cands))
        rec = {"diag": diag, "summary": cfi.cluster_summary(clusters, silh),
               "clusters": {f"C_{i}": mm for i, mm in clusters.items()},
               "clustered_mda": cmda.round(6).to_dict(orient="index"), "clustered_mdi": cmdi.round(6).to_dict(orient="index"),
               "kept_features": {k: len(v) for k, v in keep.items()}, "kept": keep}
        # MLAM p.85 residual features: refit on residualised X, same clusters
        xr = cfi.residualize(x, clusters)
        rbags, _ = cfi.fit_fold_bags(xr, y, w, splits, start, end, len(bar_index), N_TREES, seed)
        rd, rs = cfi._draws(start, end, len(bar_index), N_TREES, seed + 199)
        rfull = cfi._fit_bag(xr.to_numpy(), y, w, rd, rs)
        rmda, rbase = cfi.mda(xr, y, w, splits, rbags, groups, seed)
        rmdi = cfi.mdi([rfull], cands, clusters)
        rkeep = cfi.select(rmda, rmdi, clusters, len(cands))
        rec["residualized"] = {"oos_neg_log_loss_weighted": round(rbase, 5),
                               "clustered_mda": rmda.round(6).to_dict(orient="index"),
                               "clustered_mdi": rmdi.round(6).to_dict(orient="index"),
                               "kept_features": {k: len(v) for k, v in rkeep.items()}, "kept": rkeep}
        out["variants"][vname] = rec
        out["timing_s"][vname] = round(time.time() - t_start, 1)

    # ---- AFML diagnostics
    out["per_feature"]["sfi"] = cfi.sfi(x, y, w, splits, [(dr[:N_TREES_SFI], se[:N_TREES_SFI]) for dr, se in fold_draws]).round(6).to_dict(orient="index")
    out["pca_kendall"] = cfi.pca_kendall(x, y, w, start, end, len(bar_index), N_TREES, seed)
    out["timing_s"]["total"] = round(time.time() - t_start, 1)
    out["written_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dest = root / "artifacts" / "feature_importance_audit" / "feature_importance_audit.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    global N_TREES, N_TREES_SFI
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", required=True)
    ap.add_argument("--trees", type=int, default=N_TREES, help="smoke tests only; the audit uses 1,000 (Snippet 6.8)")
    args = ap.parse_args(argv)
    if args.trees != N_TREES:
        N_TREES = N_TREES_SFI = args.trees
    r = run(args.edge)
    if r["status"] != "OK":
        print(f"{args.edge}: {r['status']}")
        return 0
    for v, rec in r["variants"].items():
        print(f"{args.edge} | {v:11s} | clusters {rec['summary']['clusters']:3d} sizes {rec['summary']['sizes'][:8]} "
              f"| kept A/B/C {rec['kept_features']} | residualized kept {rec['residualized']['kept_features']}", flush=True)
    print(f"{args.edge} | PCA weighted Kendall tau {r['pca_kendall']} | total {r['timing_s']['total']} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
