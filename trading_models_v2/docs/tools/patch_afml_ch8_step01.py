"""AFML Ch.8 / MLAM Ch.6 walkthrough patch (owner decisions 2026-10-24) for every edge's Step 01.

Replaces Step 01's iterative XGBoost clustered-MDA surgery (Marcenko-Pastur denoised Spearman correlation, ONC capped at
30 clusters, up to 50 drop-one-cluster refits) with the approved method in shared/features/clustered_feature_importance.py:
  * MLAM ONC on the normalised variation-of-information observation matrix, PCA-reduced per MLAM p.55
  * clustered MDA (OOS, AFML-weighted neg log loss, 10-fold purged + embargoed on label_t1) and clustered MDI
  * estimator MLAM Snippet 6.8 / AFML 8.8 bagged entropy trees, max_features=1, balanced, min_weight_fraction_leaf 0.05
    (D041), 1,000 trees, sequential bootstrap round(avgU x n) (D039), raw features (D042), no SFI (D043)
  * single pass, rule B: keep clusters with MDA mean - 1.645 se > 0 (MLAM p.79 / p.89); fail closed if none is kept
  * folds fit in parallel worker processes (--workers, default 4)
Step 01 now imports the shared module, so flat-Colab mode is retired for Step 01 (owner decision 6).
Manifest keys Step 02 / the lock step validate (edge_id, label contract, provenance sha256s, AFML provenance) are unchanged.
Idempotent; every anchor must occur exactly once or the file is left untouched.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch8_step01.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "def run_mlam_clustered_feature_importance("

NEW_FUNC = '''def run_mlam_clustered_feature_importance(df: pd.DataFrame, fixed_features: list[str], candidate_features: list[str]):
    """AFML Ch.8 / MLAM Ch.6 (walkthrough 2026-10-24): one pass of MLAM clustered feature importance via
    shared/features/clustered_feature_importance.select_features_clustered (VI clusters, clustered MDA + MDI, rule B,
    MLAM Snippet 6.8 trees with min_weight_fraction_leaf 0.05, 1,000 trees, purged + embargoed 10-fold, sequential
    bootstrap). Returns the same tuple shape write_outputs expects."""
    import os

    root_str = str(V2_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    from shared.features.clustered_feature_importance import select_features_clustered

    bars = pd.read_csv(_resolve_standard_quote_tick_bars_path(), usecols=[TIME_COL])
    bar_index = pd.DatetimeIndex(pd.to_datetime(bars[TIME_COL], utc=True, errors="coerce"))
    start = bar_index.get_indexer(pd.DatetimeIndex(df.index))
    if (start < 0).any():
        raise FeatureSurgeryError(
            f"{int((start < 0).sum())} model rows are not on the current bar clock; re-export the research dataset first."
        )
    ends = _label_end_times(df)
    end = _snap_to_bar_clock(ends, bar_index)
    weights = _afml_return_weights(df)
    weights = np.asarray(weights, dtype="float64") / float(np.mean(weights))
    workers = int(os.environ.get("QUANTARA_STEP01_WORKERS", "4"))
    result = select_features_clustered(
        df[candidate_features].astype("float64"),
        df[TARGET_COL].astype(int).to_numpy(),
        weights,
        df.index.values.astype("datetime64[ns]"),
        pd.DatetimeIndex(ends).values.astype("datetime64[ns]"),
        start,
        end,
        len(bar_index),
        embargo_size_for(len(df)),
        workers=workers,
    )
    if not result["selected_features"]:
        raise FeatureSurgeryError(
            "No feature cluster has clustered MDA significantly above zero (rule B): the features carry no detectable "
            "out-of-sample information for this edge (AFML s.8.3.2 point 4, p.116). The edge fails Step 01."
        )
    cluster_df = pd.DataFrame(result["clusters"]).sort_values("mda_mean", ascending=False).reset_index(drop=True)
    cluster_df = cluster_df.rename(columns={"mda_mean": "avg_mda", "mda_se": "std_mda"})
    selected = list(fixed_features) + list(result["selected_features"])
    summary = {k: v for k, v in result.items() if k not in ("clusters", "selected_features")}
    corr_df = df[candidate_features].astype("float64").corr()
    global _LAST_DENOISE_ONC_DIAGNOSTICS
    _LAST_DENOISE_ONC_DIAGNOSTICS = {**result["observation_matrix"], **result["cluster_summary"]}
    print(cluster_df[["cluster_id", "n_features", "avg_mda", "std_mda", "mda_t", "mdi_mean", "kept_rule_B"]].to_string(index=False), flush=True)
    print(f"OOS weighted neg log loss {result['oos_neg_log_loss_weighted']:.5f} | OOS AUC {result['oos_auc']:.4f} | "
          f"kept {len(result['selected_features'])}/{len(candidate_features)} candidate features", flush=True)
    return selected, cluster_df.assign(iteration=1), cluster_df, {}, corr_df, summary


def main() -> None:'''

PAIRS = [
    ("def main() -> None:", NEW_FUNC),
    ('''    selected_features, iteration_report_df, final_report_df, final_clusters, final_corr_df, final_score_summary = iterative_feature_surgery(
        df=df,
        fixed_features=fixed_features,
        initial_candidates=candidate_features,
    )''',
     '''    if RUN_MODE == "flat_colab":
        raise FeatureSurgeryError("Flat-Colab mode is retired for Step 01 (AFML Ch.8 walkthrough 2026-10-24); run in repo mode.")
    selected_features, iteration_report_df, final_report_df, final_clusters, final_corr_df, final_score_summary = run_mlam_clustered_feature_importance(
        df=df,
        fixed_features=fixed_features,
        candidate_features=candidate_features,
    )'''),
    ('''        "scoring": {
            "primary_metric": "f1",
            "mda_formula": "weighted_oos_binary_log_loss_after_permutation - weighted_oos_binary_log_loss_base",
            "mda_scoring_weight": "canonical_afml_return_attribution_weight",
            "mda_interpretation": "positive means permutation worsened AFML-weighted OOS probability loss",
            "threshold_selection": "best F1 threshold selected on train fold only, applied to test fold",
            "threshold_grid": F1_THRESHOLD_GRID.tolist(),
        },''',
     '''        "scoring": {
            "primary_metric": "neg_log_loss (AFML-weighted, OOS)",
            "mda_formula": "(scr0 - scr1) / -scr1, scr = weighted OOS neg log loss; each cluster feature shuffled independently (MLAM Snippet 6.5)",
            "mda_scoring_weight": "canonical_afml_return_attribution_weight",
            "selection_rule": "B: keep a cluster if clustered MDA mean - 1.645 x standard error > 0 (MLAM p.79, p.89)",
        },'''),
    ('''        "clustering_method": "mlam_marcenko_pastur_denoising_plus_onc",
        "onc_max_clusters_cap": ONC_MAX_CLUSTERS_CAP,
        "max_iterations": MAX_ITERATIONS,
        "min_candidate_features": MIN_CANDIDATE_FEATURES,
        "xgb_params": {**XGB_PARAMS, **get_xgb_device_kwargs()},''',
     '''        "clustering_method": "mlam_onc_on_normalised_variation_of_information_pca_reduced_p55",
        "feature_importance_method": "mlam_clustered_mda_and_mdi_single_pass",
        "feature_importance_module": "shared/features/clustered_feature_importance.py",
        "estimator": final_score_summary.get("estimator"),
        "cv": final_score_summary.get("cv"),
        "oos_neg_log_loss_weighted": final_score_summary.get("oos_neg_log_loss_weighted"),
        "oos_auc": final_score_summary.get("oos_auc"),
        "deviations": ["D039 sequential bootstrap + purged CV", "D040 seeded k-means", "D041 min_weight_fraction_leaf 0.05", "D045 5 MDA shuffles per fold",
                       "D042 no residual features", "D043 no SFI"],'''),
    ('''            "Negative-MDA clusters are removed one at a time and clustering is recomputed each iteration.",''',
     '''            "AFML Ch.8 / MLAM Ch.6 (2026-10-24): one pass of clustered MDA/MDI; clusters failing rule B are discarded (MLAM p.89).",'''),
    ('''            "Flat Colab mode writes outputs directly to the current working directory for easy download.",''',
     '''            "Flat-Colab mode is retired for Step 01 (it imports shared/features/clustered_feature_importance.py).",'''),
]


def patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    for old, new in PAIRS:
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"anchor found {n} times: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return "patched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", nargs="*")
    args = ap.parse_args(argv)
    roots = ([V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges] if args.edges
             else sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir()))
    failed = 0
    for root in roots:
        path = root / "research" / "01_feature_surgery_clustered_mda.py"
        try:
            print(f"{patch(path):16s} {path.relative_to(V2)}")
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"FAILED           {path.relative_to(V2)}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
