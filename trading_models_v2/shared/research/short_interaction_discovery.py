"""Research-only two-condition short expectancy interaction discovery.

This diagnostic builds on short_expectancy_discovery.py. It does not create an
edge, does not train a model, and does not edit files. It imports an existing V2
edge runtime only to reuse the asset's normal general feature schema and feature
builder.

Purpose
-------
After a single-feature scan finds a possible short habitat, this script tests
whether that habitat becomes stronger, weaker, or duplicated when combined with a
second general-feature condition.

Example from trading_models_v2:

    python shared/research/short_interaction_discovery.py \
        --edge-root assets/XAUUSD/edges/crowdedlongunwind_short \
        --horizons 100,250,500 \
        --barriers 1:1,1.5:1,1:1.5 \
        --seed-features NATR_50,NATR_21,NATR,Channel_Width_100,Channel_Width_200 \
        --seed-upper-quantiles 0.90,0.95 \
        --partner-quantiles 0.05,0.10,0.20 \
        --sample-step 10 \
        --top 100

Important
---------
Quantile thresholds in this script are full-sample discovery thresholds. They are
for research discovery only. Any real edge built from a discovered interaction
must convert the condition into live-safe rolling percentiles or shifted z-scores.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from short_expectancy_discovery import (  # noqa: E402
    GRIDS,
    condition_masks,
    import_edge_runtime,
    label_short_geometry,
    parse_floats,
    prepare_discovery_frame,
    resolve_grid,
    summarize_subset,
)

DEFAULT_SEED_FEATURES = [
    "NATR_50",
    "NATR_21",
    "NATR",
    "Channel_Width_100",
    "Channel_Width_200",
    "Wavelet_Noise_50",
    "Upside_Vol_100",
    "Upside_Vol_50",
    "Downside_Vol_100",
    "Downside_Vol_50",
    "Downside_Vol_20",
    "Dist_200_High",
]

BASELINE_FIELDS = [
    "horizon",
    "pt",
    "sl",
    "rows",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "median_hold_bars",
    "mean_hold_bars",
]

SEED_FIELDS = [
    "horizon",
    "pt",
    "sl",
    "seed_feature",
    "seed_condition",
    "rows",
    "coverage",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "net_mean_R",
    "lift_vs_baseline_R",
    "years_tested",
    "positive_year_share",
    "worst_year_R",
]

INTERACTION_FIELDS = [
    "rank",
    "horizon",
    "pt",
    "sl",
    "seed_feature",
    "seed_condition",
    "partner_feature",
    "partner_condition",
    "rows",
    "coverage",
    "seed_coverage",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "lift_vs_baseline_R",
    "lift_vs_seed_R",
    "median_hold_bars",
    "mean_hold_bars",
    "years_tested",
    "positive_year_share",
    "worst_year_R",
]


def parse_csv_list(text: Optional[str]) -> Optional[List[str]]:
    if not text:
        return None
    return [item.strip() for item in text.split(",") if item.strip()]


def seed_condition_masks(
    values: pd.Series,
    *,
    upper_quantiles: Sequence[float],
    lower_quantiles: Sequence[float],
) -> List[Tuple[str, pd.Series]]:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.replace([np.inf, -np.inf], np.nan).dropna()
    if valid.empty:
        return []

    masks: List[Tuple[str, pd.Series]] = []
    seen: set[Tuple[str, float]] = set()
    for q in upper_quantiles:
        if not 0.0 < q < 1.0:
            continue
        cut = float(valid.quantile(q))
        key = ("ge", round(cut, 12))
        if key not in seen:
            masks.append((f">=p{int(round(q * 100))}", numeric >= cut))
            seen.add(key)
    for q in lower_quantiles:
        if not 0.0 < q < 1.0:
            continue
        cut = float(valid.quantile(q))
        key = ("le", round(cut, 12))
        if key not in seen:
            masks.append((f"<=p{int(round(q * 100))}", numeric <= cut))
            seen.add(key)
    return masks


def summarize_baselines_and_labels(
    bars: pd.DataFrame,
    base: pd.DataFrame,
    *,
    horizon: int,
    pt: float,
    sl: float,
    cost_bps: float,
    min_year_rows: int,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    labels = label_short_geometry(bars, base, horizon=horizon, pt=pt, sl=sl, cost_bps=cost_bps)
    work = base.merge(labels, on=["Datetime", "bar_pos"], how="inner", validate="one_to_one")
    baseline = summarize_subset(work, min_year_rows=min_year_rows)
    baseline.update({"horizon": horizon, "pt": pt, "sl": sl})
    return work, baseline


def evaluate_interactions_for_geometry(
    work: pd.DataFrame,
    feature_names: Sequence[str],
    seed_features: Sequence[str],
    *,
    horizon: int,
    pt: float,
    sl: float,
    baseline_net_r: float,
    seed_upper_quantiles: Sequence[float],
    seed_lower_quantiles: Sequence[float],
    partner_quantiles: Sequence[float],
    min_rows: int,
    min_year_rows: int,
    max_seed_coverage: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    total_rows = max(len(work), 1)
    seed_summaries: List[Dict[str, Any]] = []
    interactions: List[Dict[str, Any]] = []

    usable_seed_features = [feature for feature in seed_features if feature in work.columns]
    for seed_feature in usable_seed_features:
        seed_masks = seed_condition_masks(
            work[seed_feature],
            upper_quantiles=seed_upper_quantiles,
            lower_quantiles=seed_lower_quantiles,
        )
        for seed_condition, raw_seed_mask in seed_masks:
            seed_mask = raw_seed_mask.fillna(False)
            seed_rows = int(seed_mask.sum())
            if seed_rows < min_rows:
                continue
            seed_coverage = float(seed_rows / total_rows)
            if max_seed_coverage > 0 and seed_coverage > max_seed_coverage:
                continue

            seed_subset = work.loc[seed_mask].copy()
            seed_stats = summarize_subset(seed_subset, min_year_rows=min_year_rows)
            seed_net_r = float(seed_stats["net_mean_R"])
            seed_summary = {
                "horizon": horizon,
                "pt": pt,
                "sl": sl,
                "seed_feature": seed_feature,
                "seed_condition": seed_condition,
                "coverage": seed_coverage,
                "lift_vs_baseline_R": seed_net_r - baseline_net_r,
            }
            seed_summary.update(seed_stats)
            seed_summaries.append(seed_summary)

            for partner_feature in feature_names:
                if partner_feature == seed_feature or partner_feature not in work.columns:
                    continue
                for partner_condition, raw_partner_mask in condition_masks(work[partner_feature], partner_quantiles):
                    combined_mask = seed_mask & raw_partner_mask.fillna(False)
                    rows = int(combined_mask.sum())
                    if rows < min_rows:
                        continue
                    subset = work.loc[combined_mask].copy()
                    stats = summarize_subset(subset, min_year_rows=min_year_rows)
                    net_r = float(stats["net_mean_R"])
                    item = {
                        "horizon": horizon,
                        "pt": pt,
                        "sl": sl,
                        "seed_feature": seed_feature,
                        "seed_condition": seed_condition,
                        "partner_feature": partner_feature,
                        "partner_condition": partner_condition,
                        "coverage": float(rows / total_rows),
                        "seed_coverage": seed_coverage,
                        "lift_vs_baseline_R": net_r - baseline_net_r,
                        "lift_vs_seed_R": net_r - seed_net_r,
                    }
                    item.update(stats)
                    interactions.append(item)

    return seed_summaries, interactions


def print_baselines(baselines: Sequence[Dict[str, Any]]) -> None:
    print("Baseline by geometry:")
    print(",".join(BASELINE_FIELDS))
    for row in baselines:
        print(
            f"{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},{row['rows']},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},{row['net_mean_R']:.5f},"
            f"{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f}"
        )


def print_seed_summaries(seed_summaries: Sequence[Dict[str, Any]], top: int) -> None:
    ranked = sorted(seed_summaries, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    print()
    print(f"Top {min(top, len(ranked))} seed regions by net_mean_R:")
    print(",".join(SEED_FIELDS))
    for row in ranked[:top]:
        print(
            f"{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},"
            f"{row['seed_feature']},{row['seed_condition']},{row['rows']},{row['coverage']:.4f},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['net_mean_R']:.5f},{row['lift_vs_baseline_R']:.5f},"
            f"{row['years_tested']},{row['positive_year_share']:.4f},{row['worst_year_R']:.5f}"
        )


def print_interactions(interactions: Sequence[Dict[str, Any]], top: int) -> None:
    ranked = sorted(
        interactions,
        key=lambda x: (
            np.nan_to_num(x["net_mean_R"], nan=-1e9),
            np.nan_to_num(x["lift_vs_seed_R"], nan=-1e9),
            x["rows"],
        ),
        reverse=True,
    )
    print()
    print(f"Top {min(top, len(ranked))} two-condition regions by net_mean_R:")
    print(",".join(INTERACTION_FIELDS))
    for rank, row in enumerate(ranked[:top], 1):
        print(
            f"{rank},{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},"
            f"{row['seed_feature']},{row['seed_condition']},"
            f"{row['partner_feature']},{row['partner_condition']},"
            f"{row['rows']},{row['coverage']:.4f},{row['seed_coverage']:.4f},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},{row['net_mean_R']:.5f},"
            f"{row['lift_vs_baseline_R']:.5f},{row['lift_vs_seed_R']:.5f},"
            f"{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f},"
            f"{row['years_tested']},{row['positive_year_share']:.4f},{row['worst_year_R']:.5f}"
        )


def save_outputs(
    edge_root: Path,
    args: argparse.Namespace,
    baselines: Sequence[Dict[str, Any]],
    seed_summaries: Sequence[Dict[str, Any]],
    interactions: Sequence[Dict[str, Any]],
) -> None:
    out_dir = edge_root / "artifacts" / "short_interaction_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = out_dir / "interaction_baselines.csv"
    seed_path = out_dir / "interaction_seed_regions.csv"
    interaction_path = out_dir / "interaction_two_condition_regions.csv"
    summary_path = out_dir / "short_interaction_summary.json"

    with baseline_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=BASELINE_FIELDS)
        writer.writeheader()
        writer.writerows([{k: row.get(k) for k in BASELINE_FIELDS} for row in baselines])

    seed_ranked = sorted(seed_summaries, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    with seed_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SEED_FIELDS)
        writer.writeheader()
        writer.writerows([{k: row.get(k) for k in SEED_FIELDS} for row in seed_ranked])

    interaction_ranked = sorted(
        interactions,
        key=lambda x: (
            np.nan_to_num(x["net_mean_R"], nan=-1e9),
            np.nan_to_num(x["lift_vs_seed_R"], nan=-1e9),
            x["rows"],
        ),
        reverse=True,
    )
    rows_to_write: List[Dict[str, Any]] = []
    for rank, row in enumerate(interaction_ranked, 1):
        out = {k: row.get(k) for k in INTERACTION_FIELDS if k != "rank"}
        out["rank"] = rank
        rows_to_write.append(out)
    with interaction_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=INTERACTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows_to_write)

    summary = {
        "status": "research_only_short_interaction_discovery_complete",
        "edge_root": edge_root.as_posix(),
        "grid": args.grid,
        "cost_bps": args.cost_bps,
        "sample_step": args.sample_step,
        "min_rows": args.min_rows,
        "min_year_rows": args.min_year_rows,
        "seed_features": parse_csv_list(args.seed_features) or DEFAULT_SEED_FEATURES,
        "top_seed_regions": seed_ranked[: args.top],
        "top_interactions": interaction_ranked[: args.top],
        "outputs": {
            "baselines": baseline_path.as_posix(),
            "seed_regions": seed_path.as_posix(),
            "two_condition_regions": interaction_path.as_posix(),
            "summary": summary_path.as_posix(),
        },
        "note": "This diagnostic does not create an edge, train a model, or write final geometry.",
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print()
    print("Saved:")
    print(f"- {baseline_path}")
    print(f"- {seed_path}")
    print(f"- {interaction_path}")
    print(f"- {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover two-condition short expectancy regions using general V2 schema features.")
    parser.add_argument("--edge-root", required=True, help="Any V2 edge folder for the asset to scan")
    parser.add_argument("--grid", default="balanced", choices=sorted(GRIDS))
    parser.add_argument("--horizons", default=None, help="Comma-separated horizons, e.g. 100,250,500")
    parser.add_argument("--barriers", default=None, help="Comma-separated PT:SL pairs, e.g. 1:1,1.5:1")
    parser.add_argument("--pts", default=None, help="Comma-separated PT multipliers; cross-product with --sls")
    parser.add_argument("--sls", default=None, help="Comma-separated SL multipliers; cross-product with --pts")
    parser.add_argument("--seed-features", default=",".join(DEFAULT_SEED_FEATURES))
    parser.add_argument("--seed-upper-quantiles", default="0.90,0.95", help="Absolute upper quantiles for seed features")
    parser.add_argument("--seed-lower-quantiles", default="", help="Optional absolute lower quantiles for seed features")
    parser.add_argument("--partner-quantiles", default="0.05,0.10,0.20", help="Lower quantiles; upper tails are mirrored")
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--sample-step", type=int, default=10, help="Use every Nth eligible row for speed and less overlap")
    parser.add_argument("--min-rows", type=int, default=250, help="Minimum rows per interaction region")
    parser.add_argument("--min-year-rows", type=int, default=30, help="Minimum rows for a year to count in stability")
    parser.add_argument("--max-seed-coverage", type=float, default=0.30, help="Skip overly broad seed masks above this coverage; 0 disables")
    parser.add_argument("--event-vol-lookback", type=int, default=50)
    parser.add_argument("--event-vol-span", type=int, default=100)
    parser.add_argument("--min-event-vol", type=float, default=1e-8)
    parser.add_argument("--allow-volume-like-features", action="store_true", help="Do not skip tick/volume/proxy/threshold-like schema columns")
    parser.add_argument("--no-require-all-schema-features", action="store_true", help="Do not require all schema features to be finite")
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    if args.sample_step <= 0:
        raise SystemExit("--sample-step must be positive")
    started = time.time()
    edge_root = Path(args.edge_root).resolve()
    horizons, barriers = resolve_grid(args)
    seed_features = parse_csv_list(args.seed_features) or DEFAULT_SEED_FEATURES
    seed_upper_quantiles = parse_floats(args.seed_upper_quantiles) or []
    seed_lower_quantiles = parse_floats(args.seed_lower_quantiles) or []
    partner_quantiles = parse_floats(args.partner_quantiles) or [0.05, 0.10, 0.20]

    runtime = import_edge_runtime(edge_root)
    bars, base, feature_names = prepare_discovery_frame(
        runtime,
        sample_step=args.sample_step,
        require_all_schema_features=not args.no_require_all_schema_features,
        exclude_volume_like=not args.allow_volume_like_features,
        event_vol_lookback=args.event_vol_lookback,
        event_vol_span=args.event_vol_span,
        min_event_vol=args.min_event_vol,
    )
    seed_features = [feature for feature in seed_features if feature in feature_names]
    if not seed_features:
        raise SystemExit("None of the requested --seed-features are present in the general feature schema.")

    print(f"edge_root: {edge_root}")
    print(f"grid: {args.grid}")
    print("horizons: " + ",".join(str(x) for x in horizons))
    print("barriers: " + ",".join(f"{pt:g}:{sl:g}" for pt, sl in barriers))
    print(f"cost_bps: {args.cost_bps:g}")
    print(f"sample_step: {args.sample_step}")
    print(f"eligible_rows: {len(base)}")
    print(f"general_features_scanned: {len(feature_names)}")
    print("seed_features: " + ",".join(seed_features))
    print("seed_upper_quantiles: " + ",".join(f"{q:g}" for q in seed_upper_quantiles))
    if seed_lower_quantiles:
        print("seed_lower_quantiles: " + ",".join(f"{q:g}" for q in seed_lower_quantiles))
    print("partner_quantiles: " + ",".join(f"{q:g}" for q in partner_quantiles))
    print()

    baselines: List[Dict[str, Any]] = []
    all_seed_summaries: List[Dict[str, Any]] = []
    all_interactions: List[Dict[str, Any]] = []

    for horizon in horizons:
        for pt, sl in barriers:
            work, baseline = summarize_baselines_and_labels(
                bars,
                base,
                horizon=horizon,
                pt=pt,
                sl=sl,
                cost_bps=args.cost_bps,
                min_year_rows=args.min_year_rows,
            )
            baselines.append(baseline)
            seed_summaries, interactions = evaluate_interactions_for_geometry(
                work,
                feature_names,
                seed_features,
                horizon=horizon,
                pt=pt,
                sl=sl,
                baseline_net_r=float(baseline["net_mean_R"]),
                seed_upper_quantiles=seed_upper_quantiles,
                seed_lower_quantiles=seed_lower_quantiles,
                partner_quantiles=partner_quantiles,
                min_rows=args.min_rows,
                min_year_rows=args.min_year_rows,
                max_seed_coverage=args.max_seed_coverage,
            )
            all_seed_summaries.extend(seed_summaries)
            all_interactions.extend(interactions)

    print_baselines(baselines)
    print_seed_summaries(all_seed_summaries, args.top)
    print_interactions(all_interactions, args.top)
    if args.save:
        save_outputs(edge_root, args, baselines, all_seed_summaries, all_interactions)
    print()
    print(f"Done in {time.time() - started:.2f}s. No edge files were edited.")


if __name__ == "__main__":
    main()
