"""Research-only regime-state short expectancy discovery.

This diagnostic is MLDP-oriented: it looks for broad enough short habitats where a
high-recall primary side model could emit a useful opportunity set for later
meta-labeling.

It differs from short_expectancy_discovery.py in one important way: regime/state
columns are treated as categorical labels, not ordered numeric features. A regime
state of 3 is not assumed to be "greater" than a regime state of 1.

The script does not create an edge, does not train a model, and does not edit
files. It imports an existing V2 edge runtime only to reuse the asset's normal
feature builder, schema, and verified bar source.

Example from trading_models_v2:

    python shared/research/short_regime_state_discovery.py \
        --edge-root assets/XAUUSD/edges/crowdedlongunwind_short \
        --horizons 50,100,250,500 \
        --barriers 1:1,1.5:1,1:1.5 \
        --sample-step 5 \
        --min-rows 750 \
        --mldp-min-rows 1000 \
        --top 100

Important: any quantile-style trigger thresholds printed here are full-sample
research thresholds. A real edge must convert them to live-safe rolling
percentiles, shifted z-scores, or frozen training-period thresholds.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

from short_expectancy_discovery import (  # noqa: E402
    GRIDS,
    compute_event_vol,
    condition_masks,
    exclude_volume_like_features,
    get_schema_features,
    import_edge_runtime,
    label_short_geometry,
    load_bars,
    normalise_bars,
    parse_barriers,
    parse_floats,
    parse_ints,
    resolve_grid,
    summarize_subset,
)

EPS = 1e-12
REGIME_NAME_HINTS = ("regime", "state", "hmm", "cluster")
FORBIDDEN_NAME_TERMS = (
    "meta_label",
    "barrier",
    "probability",
    "calibrated",
    "approved",
    "afml",
    "realized",
    "entry_price",
    "pt_price",
    "sl_price",
)

DEFAULT_TRIGGER_FEATURES = [
    "Log_Return_1",
    "Log_Return_3",
    "Log_Return_5",
    "Log_Return_10",
    "Return_Z_20",
    "Trend_Slope_20",
    "Trend_Slope_50",
    "Dist_EMA_20",
    "Dist_EMA_50",
    "EMA_10_20_Gap",
    "EMA_20_50_Gap",
    "RSI_14",
    "RSI_21",
    "RSI_50",
    "Channel_Position_20",
    "Channel_Position_50",
    "NATR",
    "NATR_21",
    "NATR_50",
    "Vol_Regime_14_50",
    "Vol_Regime_21_100",
    "Channel_Width_20",
    "Channel_Width_50",
    "Channel_Width_100",
    "Channel_Width_200",
    "Wavelet_Noise_20",
    "Wavelet_Noise_50",
    "Downside_Vol_20",
    "Downside_Vol_50",
    "Upside_Vol_50",
    "Upside_Vol_100",
    "Dist_200_High",
    "BBP_20_2",
    "BBB_20_2",
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

STATE_FIELDS = [
    "horizon",
    "pt",
    "sl",
    "regime_col",
    "state",
    "rows",
    "coverage",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "lift_vs_baseline_R",
    "median_hold_bars",
    "mean_hold_bars",
    "years_tested",
    "positive_year_share",
    "worst_year_R",
    "mldp_pool_ok",
]

TRIGGER_FIELDS = [
    "rank",
    "horizon",
    "pt",
    "sl",
    "regime_col",
    "state",
    "trigger",
    "rows",
    "coverage",
    "state_rows",
    "state_coverage",
    "trigger_state_coverage",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "lift_vs_baseline_R",
    "lift_vs_state_R",
    "median_hold_bars",
    "mean_hold_bars",
    "years_tested",
    "positive_year_share",
    "worst_year_R",
    "mldp_pool_ok",
]


def parse_csv_list(text: Optional[str]) -> Optional[List[str]]:
    if not text:
        return None
    return [item.strip() for item in text.split(",") if item.strip()]


def safe_col_name(name: str) -> str:
    return str(name).strip()


def is_forbidden_name(name: str) -> bool:
    lower = name.lower()
    return any(term in lower for term in FORBIDDEN_NAME_TERMS)


def format_state_value(value: Any) -> str:
    if isinstance(value, (np.integer, int)):
        return str(int(value))
    if isinstance(value, (np.floating, float)):
        if np.isfinite(value) and abs(float(value) - round(float(value))) < 1e-9:
            return str(int(round(float(value))))
        return f"{float(value):.8g}"
    return str(value)


def is_integer_like_series(series: pd.Series) -> bool:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return False
    values = numeric.to_numpy(dtype="float64")
    return bool(np.all(np.isfinite(values)) and np.all(np.abs(values - np.round(values)) < 1e-9))


def auto_detect_regime_columns(
    features: pd.DataFrame,
    *,
    explicit_cols: Optional[Sequence[str]],
    max_states: int,
    min_non_null: int,
) -> List[str]:
    if explicit_cols:
        missing = [col for col in explicit_cols if col not in features.columns]
        if missing:
            raise SystemExit(f"Requested --regime-cols not found in feature table: {missing}")
        return list(dict.fromkeys(explicit_cols))

    detected: List[str] = []
    for col in features.columns:
        name = safe_col_name(col)
        lower = name.lower()
        if is_forbidden_name(name):
            continue
        if not any(hint in lower for hint in REGIME_NAME_HINTS):
            continue
        series = features[col].dropna()
        if len(series) < min_non_null:
            continue
        nunique = int(series.nunique(dropna=True))
        if nunique < 2 or nunique > max_states:
            continue
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            detected.append(name)
            continue
        if is_integer_like_series(series):
            detected.append(name)
            continue
    return list(dict.fromkeys(detected))


def prepare_regime_frame(
    runtime: Any,
    *,
    sample_step: int,
    require_all_schema_features: bool,
    exclude_volume_like: bool,
    event_vol_lookback: int,
    event_vol_span: int,
    min_event_vol: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, List[str]]:
    features = runtime.build_features(drop_warmup_rows=False)
    bars = normalise_bars(load_bars(runtime, features))

    features = features.copy()
    features["Datetime"] = to_datetime_iso(features["Datetime"], utc=True, errors="coerce")
    features = features.dropna(subset=["Datetime"]).sort_values("Datetime").reset_index(drop=True)

    feature_names = get_schema_features(runtime, features)
    if exclude_volume_like:
        feature_names = exclude_volume_like_features(feature_names)
    if not feature_names:
        raise RuntimeError("No usable general schema features found.")

    bar_pos = pd.Series(np.arange(len(bars), dtype="int64"), index=bars["Datetime"])
    close_at = bars.set_index("Datetime")["Close"]
    event_vol_at = compute_event_vol(bars["Close"].astype(float), event_vol_lookback, event_vol_span, min_event_vol)
    event_vol_at.index = bars["Datetime"]

    base = features.copy()
    base["bar_pos"] = base["Datetime"].map(bar_pos)
    base["entry_price"] = base["Datetime"].map(close_at)
    base["event_vol"] = base["Datetime"].map(event_vol_at)
    base = base.dropna(subset=["bar_pos", "entry_price", "event_vol"]).copy()
    base["bar_pos"] = base["bar_pos"].astype("int64")
    base = base[(base["event_vol"] > 0) & np.isfinite(base["event_vol"])].copy()

    if require_all_schema_features:
        for col in feature_names:
            base[col] = pd.to_numeric(base[col], errors="coerce")
        base = base.replace([np.inf, -np.inf], np.nan).dropna(subset=feature_names).copy()

    if sample_step > 1:
        base = base.iloc[::sample_step].copy()
    base = base.reset_index(drop=True)
    if base.empty:
        raise RuntimeError("No eligible rows after warmup/sample filtering.")
    return bars, features, base, feature_names


def threshold_mask(series: pd.Series, condition: str, value: float) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if condition == "<=":
        return numeric <= value
    if condition == ">=":
        return numeric >= value
    raise ValueError(f"Unsupported condition: {condition}")


def build_domain_trigger_masks(work: pd.DataFrame) -> List[Tuple[str, pd.Series]]:
    masks: List[Tuple[str, pd.Series]] = [("all_rows", pd.Series(True, index=work.index))]

    le_zero_cols = [
        "Log_Return_1",
        "Log_Return_3",
        "Log_Return_5",
        "Log_Return_10",
        "Return_Z_20",
        "Trend_Slope_20",
        "Trend_Slope_50",
        "Dist_EMA_20",
        "Dist_EMA_50",
        "EMA_10_20_Gap",
        "EMA_20_50_Gap",
    ]
    for col in le_zero_cols:
        if col in work.columns:
            masks.append((f"{col}<=0", threshold_mask(work[col], "<=", 0.0)))

    le_level_cols = [("RSI_14", 50.0), ("RSI_21", 50.0), ("RSI_50", 50.0)]
    for col, level in le_level_cols:
        if col in work.columns:
            masks.append((f"{col}<={level:g}", threshold_mask(work[col], "<=", level)))

    channel_cols = ["Channel_Position_20", "Channel_Position_50"]
    for col in channel_cols:
        if col in work.columns:
            masks.append((f"{col}<=0.50", threshold_mask(work[col], "<=", 0.50)))

    if "Close_Location_Recalc" in work.columns:
        masks.append(("Close_Location_Recalc<=0.50", threshold_mask(work["Close_Location_Recalc"], "<=", 0.50)))

    if "Close_FFD" in work.columns:
        close_ffd = pd.to_numeric(work["Close_FFD"], errors="coerce")
        ffd_return_5 = np.log((close_ffd + EPS) / (close_ffd.shift(5) + EPS))
        ffd_return_20 = np.log((close_ffd + EPS) / (close_ffd.shift(20) + EPS))
        masks.append(("Close_FFD_return_5<=0", ffd_return_5 <= 0.0))
        masks.append(("Close_FFD_return_20<=0", ffd_return_20 <= 0.0))

    if "Vol_Regime_14_50" in work.columns:
        masks.append(("Vol_Regime_14_50>=1", threshold_mask(work["Vol_Regime_14_50"], ">=", 1.0)))
    if "Vol_Regime_21_100" in work.columns:
        masks.append(("Vol_Regime_21_100>=1", threshold_mask(work["Vol_Regime_21_100"], ">=", 1.0)))

    return masks


def build_quantile_trigger_masks(
    work: pd.DataFrame,
    trigger_features: Sequence[str],
    trigger_quantiles: Sequence[float],
) -> List[Tuple[str, pd.Series]]:
    masks: List[Tuple[str, pd.Series]] = []
    for feature in trigger_features:
        if feature not in work.columns:
            continue
        for condition, mask in condition_masks(work[feature], trigger_quantiles):
            masks.append((f"{feature}{condition}", mask))
    return masks


def dedupe_masks(items: Sequence[Tuple[str, pd.Series]]) -> List[Tuple[str, pd.Series]]:
    seen: set[str] = set()
    out: List[Tuple[str, pd.Series]] = []
    for name, mask in items:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, mask))
    return out


def regime_state_masks(
    work: pd.DataFrame,
    regime_cols: Sequence[str],
    *,
    max_states: int,
) -> List[Tuple[str, str, pd.Series]]:
    out: List[Tuple[str, str, pd.Series]] = []
    for col in regime_cols:
        if col not in work.columns:
            continue
        series = work[col]
        values = series.dropna().unique().tolist()
        if len(values) > max_states:
            continue
        values = sorted(values, key=lambda x: format_state_value(x))
        for value in values:
            state_name = format_state_value(value)
            out.append((col, state_name, series == value))
    return out


def evaluate_states_and_triggers(
    work: pd.DataFrame,
    regime_cols: Sequence[str],
    trigger_masks: Sequence[Tuple[str, pd.Series]],
    *,
    horizon: int,
    pt: float,
    sl: float,
    baseline_net_r: float,
    min_state_rows: int,
    min_rows: int,
    min_year_rows: int,
    max_states: int,
    mldp_min_rows: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    total_rows = max(len(work), 1)
    state_results: List[Dict[str, Any]] = []
    trigger_results: List[Dict[str, Any]] = []

    for regime_col, state_name, raw_state_mask in regime_state_masks(work, regime_cols, max_states=max_states):
        state_mask = raw_state_mask.fillna(False)
        state_rows = int(state_mask.sum())
        if state_rows < min_state_rows:
            continue

        state_subset = work.loc[state_mask].copy()
        state_stats = summarize_subset(state_subset, min_year_rows=min_year_rows)
        state_net_r = float(state_stats["net_mean_R"])
        state_item = {
            "horizon": horizon,
            "pt": pt,
            "sl": sl,
            "regime_col": regime_col,
            "state": state_name,
            "coverage": float(state_rows / total_rows),
            "lift_vs_baseline_R": state_net_r - baseline_net_r,
            "mldp_pool_ok": bool(state_rows >= mldp_min_rows),
        }
        state_item.update(state_stats)
        state_results.append(state_item)

        for trigger_name, raw_trigger_mask in trigger_masks:
            combined = state_mask & raw_trigger_mask.fillna(False)
            rows = int(combined.sum())
            if rows < min_rows:
                continue
            subset = work.loc[combined].copy()
            stats = summarize_subset(subset, min_year_rows=min_year_rows)
            net_r = float(stats["net_mean_R"])
            item = {
                "horizon": horizon,
                "pt": pt,
                "sl": sl,
                "regime_col": regime_col,
                "state": state_name,
                "trigger": trigger_name,
                "coverage": float(rows / total_rows),
                "state_rows": state_rows,
                "state_coverage": float(state_rows / total_rows),
                "trigger_state_coverage": float(rows / max(state_rows, 1)),
                "lift_vs_baseline_R": net_r - baseline_net_r,
                "lift_vs_state_R": net_r - state_net_r,
                "mldp_pool_ok": bool(rows >= mldp_min_rows),
            }
            item.update(stats)
            trigger_results.append(item)
    return state_results, trigger_results


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


def print_state_results(state_results: Sequence[Dict[str, Any]], top: int) -> None:
    ranked = sorted(state_results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    print()
    print(f"Top {min(top, len(ranked))} regime-state-only regions by net_mean_R:")
    print(",".join(STATE_FIELDS))
    for row in ranked[:top]:
        print(
            f"{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},"
            f"{row['regime_col']},{row['state']},{row['rows']},{row['coverage']:.4f},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},{row['net_mean_R']:.5f},"
            f"{row['lift_vs_baseline_R']:.5f},{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f},"
            f"{row['years_tested']},{row['positive_year_share']:.4f},{row['worst_year_R']:.5f},"
            f"{int(bool(row['mldp_pool_ok']))}"
        )


def print_trigger_results(trigger_results: Sequence[Dict[str, Any]], top: int, *, high_recall_only: bool = False) -> None:
    rows = [r for r in trigger_results if bool(r["mldp_pool_ok"])] if high_recall_only else list(trigger_results)
    ranked = sorted(
        rows,
        key=lambda x: (
            np.nan_to_num(x["net_mean_R"], nan=-1e9),
            np.nan_to_num(x["lift_vs_state_R"], nan=-1e9),
            x["rows"],
        ),
        reverse=True,
    )
    title = "Top high-recall regime-state + trigger regions by net_mean_R" if high_recall_only else "Top regime-state + trigger regions by net_mean_R"
    print()
    print(f"{title}: {min(top, len(ranked))}")
    print(",".join(TRIGGER_FIELDS))
    for rank, row in enumerate(ranked[:top], 1):
        print(
            f"{rank},{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},"
            f"{row['regime_col']},{row['state']},{row['trigger']},"
            f"{row['rows']},{row['coverage']:.4f},{row['state_rows']},{row['state_coverage']:.4f},"
            f"{row['trigger_state_coverage']:.4f},{row['pt_rate']:.4f},{row['sl_rate']:.4f},"
            f"{row['vertical_rate']:.4f},{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},"
            f"{row['net_mean_R']:.5f},{row['lift_vs_baseline_R']:.5f},{row['lift_vs_state_R']:.5f},"
            f"{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f},"
            f"{row['years_tested']},{row['positive_year_share']:.4f},{row['worst_year_R']:.5f},"
            f"{int(bool(row['mldp_pool_ok']))}"
        )


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fields: Sequence[str], *, ranked: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output_rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, 1):
        out = {field: row.get(field) for field in fields if field != "rank"}
        if "rank" in fields:
            out["rank"] = idx if ranked else row.get("rank")
        output_rows.append(out)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(output_rows)


def save_outputs(
    edge_root: Path,
    args: argparse.Namespace,
    baselines: Sequence[Dict[str, Any]],
    state_results: Sequence[Dict[str, Any]],
    trigger_results: Sequence[Dict[str, Any]],
    regime_cols: Sequence[str],
) -> None:
    out_dir = edge_root / "artifacts" / "short_regime_state_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = out_dir / "regime_state_baselines.csv"
    state_path = out_dir / "regime_state_only_regions.csv"
    trigger_path = out_dir / "regime_state_trigger_regions.csv"
    high_recall_path = out_dir / "regime_state_trigger_regions_high_recall.csv"
    summary_path = out_dir / "short_regime_state_summary.json"

    state_ranked = sorted(state_results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    trigger_ranked = sorted(
        trigger_results,
        key=lambda x: (
            np.nan_to_num(x["net_mean_R"], nan=-1e9),
            np.nan_to_num(x["lift_vs_state_R"], nan=-1e9),
            x["rows"],
        ),
        reverse=True,
    )
    high_recall_ranked = [row for row in trigger_ranked if bool(row["mldp_pool_ok"])]

    write_csv(baseline_path, baselines, BASELINE_FIELDS)
    write_csv(state_path, state_ranked, STATE_FIELDS)
    write_csv(trigger_path, trigger_ranked, TRIGGER_FIELDS, ranked=True)
    write_csv(high_recall_path, high_recall_ranked, TRIGGER_FIELDS, ranked=True)

    summary = {
        "status": "research_only_short_regime_state_discovery_complete",
        "edge_root": edge_root.as_posix(),
        "grid": args.grid,
        "cost_bps": args.cost_bps,
        "sample_step": args.sample_step,
        "min_rows": args.min_rows,
        "min_state_rows": args.min_state_rows,
        "mldp_min_rows": args.mldp_min_rows,
        "regime_columns": list(regime_cols),
        "top_state_only": state_ranked[: args.top],
        "top_state_trigger": trigger_ranked[: args.top],
        "top_high_recall_state_trigger": high_recall_ranked[: args.top],
        "outputs": {
            "baselines": baseline_path.as_posix(),
            "state_only": state_path.as_posix(),
            "state_triggers": trigger_path.as_posix(),
            "high_recall_state_triggers": high_recall_path.as_posix(),
            "summary": summary_path.as_posix(),
        },
        "note": "This diagnostic does not create an edge, train a model, or write final geometry.",
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print()
    print("Saved:")
    print(f"- {baseline_path}")
    print(f"- {state_path}")
    print(f"- {trigger_path}")
    print(f"- {high_recall_path}")
    print(f"- {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover MLDP-suitable short expectancy by categorical regime state.")
    parser.add_argument("--edge-root", required=True, help="Any V2 edge folder for the asset to scan")
    parser.add_argument("--grid", default="balanced", choices=sorted(GRIDS))
    parser.add_argument("--horizons", default=None, help="Comma-separated horizons, e.g. 50,100,250")
    parser.add_argument("--barriers", default=None, help="Comma-separated PT:SL pairs, e.g. 1:1,1.5:1")
    parser.add_argument("--pts", default=None, help="Comma-separated PT multipliers; cross-product with --sls")
    parser.add_argument("--sls", default=None, help="Comma-separated SL multipliers; cross-product with --pts")
    parser.add_argument("--regime-cols", default=None, help="Comma-separated categorical regime/state columns. If omitted, auto-detect.")
    parser.add_argument("--max-states", type=int, default=12, help="Maximum unique states for a categorical regime column")
    parser.add_argument("--min-regime-non-null", type=int, default=500)
    parser.add_argument("--trigger-features", default=",".join(DEFAULT_TRIGGER_FEATURES))
    parser.add_argument("--trigger-quantiles", default="0.20,0.30", help="Lower quantiles; upper tails are mirrored")
    parser.add_argument("--no-domain-triggers", action="store_true", help="Disable hand-built broad bearish/volatility triggers")
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--sample-step", type=int, default=5, help="Use every Nth eligible row for speed and reduced overlap")
    parser.add_argument("--min-state-rows", type=int, default=750, help="Minimum rows for a state-only region")
    parser.add_argument("--min-rows", type=int, default=750, help="Minimum rows for a state+trigger region")
    parser.add_argument("--mldp-min-rows", type=int, default=1000, help="Rows required to flag a high-recall MLDP-suitable pool")
    parser.add_argument("--min-year-rows", type=int, default=40, help="Minimum rows for a year to count in stability")
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
    if args.max_states < 2:
        raise SystemExit("--max-states must be at least 2")

    started = time.time()
    edge_root = Path(args.edge_root).resolve()
    horizons, barriers = resolve_grid(args)
    explicit_regime_cols = parse_csv_list(args.regime_cols)
    trigger_features = parse_csv_list(args.trigger_features) or DEFAULT_TRIGGER_FEATURES
    trigger_quantiles = parse_floats(args.trigger_quantiles) or [0.20, 0.30]

    runtime = import_edge_runtime(edge_root)
    bars, full_features, base, feature_names = prepare_regime_frame(
        runtime,
        sample_step=args.sample_step,
        require_all_schema_features=not args.no_require_all_schema_features,
        exclude_volume_like=not args.allow_volume_like_features,
        event_vol_lookback=args.event_vol_lookback,
        event_vol_span=args.event_vol_span,
        min_event_vol=args.min_event_vol,
    )

    regime_cols = auto_detect_regime_columns(
        full_features,
        explicit_cols=explicit_regime_cols,
        max_states=args.max_states,
        min_non_null=args.min_regime_non_null,
    )
    if not regime_cols:
        candidate_names = [c for c in full_features.columns if any(h in c.lower() for h in REGIME_NAME_HINTS)]
        raise SystemExit(
            "No categorical regime/state columns detected. "
            "Pass --regime-cols explicitly if you know the state column name. "
            f"Columns containing regime/state hints: {candidate_names[:50]}"
        )

    if not args.allow_volume_like_features:
        trigger_features = exclude_volume_like_features(trigger_features)
    trigger_features = [feature for feature in trigger_features if feature in base.columns and not is_forbidden_name(feature)]

    print(f"edge_root: {edge_root}")
    print(f"grid: {args.grid}")
    print("horizons: " + ",".join(str(x) for x in horizons))
    print("barriers: " + ",".join(f"{pt:g}:{sl:g}" for pt, sl in barriers))
    print(f"cost_bps: {args.cost_bps:g}")
    print(f"sample_step: {args.sample_step}")
    print(f"eligible_rows: {len(base)}")
    print(f"general_features_available: {len(feature_names)}")
    print("regime_cols: " + ",".join(regime_cols))
    print("trigger_features: " + ",".join(trigger_features))
    print("trigger_quantiles: " + ",".join(f"{q:g}" for q in trigger_quantiles))
    print(f"mldp_min_rows: {args.mldp_min_rows}")
    print()

    baselines: List[Dict[str, Any]] = []
    all_state_results: List[Dict[str, Any]] = []
    all_trigger_results: List[Dict[str, Any]] = []

    for horizon in horizons:
        for pt, sl in barriers:
            labels = label_short_geometry(bars, base, horizon=horizon, pt=pt, sl=sl, cost_bps=args.cost_bps)
            work = base.merge(labels, on=["Datetime", "bar_pos"], how="inner", validate="one_to_one")
            baseline = summarize_subset(work, min_year_rows=args.min_year_rows)
            baseline.update({"horizon": horizon, "pt": pt, "sl": sl})
            baselines.append(baseline)

            masks: List[Tuple[str, pd.Series]] = []
            if not args.no_domain_triggers:
                masks.extend(build_domain_trigger_masks(work))
            masks.extend(build_quantile_trigger_masks(work, trigger_features, trigger_quantiles))
            masks = dedupe_masks(masks)

            state_results, trigger_results = evaluate_states_and_triggers(
                work,
                regime_cols,
                masks,
                horizon=horizon,
                pt=pt,
                sl=sl,
                baseline_net_r=float(baseline["net_mean_R"]),
                min_state_rows=args.min_state_rows,
                min_rows=args.min_rows,
                min_year_rows=args.min_year_rows,
                max_states=args.max_states,
                mldp_min_rows=args.mldp_min_rows,
            )
            all_state_results.extend(state_results)
            all_trigger_results.extend(trigger_results)

    print_baselines(baselines)
    print_state_results(all_state_results, args.top)
    print_trigger_results(all_trigger_results, args.top, high_recall_only=False)
    print_trigger_results(all_trigger_results, args.top, high_recall_only=True)

    if args.save:
        save_outputs(edge_root, args, baselines, all_state_results, all_trigger_results, regime_cols)

    print()
    print(f"Done in {time.time() - started:.2f}s. No edge files were edited.")


if __name__ == "__main__":
    main()
