"""Research-only short expectancy discovery for V2 feature universes.

This script is a diagnostic scanner. It does not create an edge, does not train a
model, and does not edit files. It imports an existing edge runtime only to reuse
that asset's normal V2 feature build and schema feature list, then asks:

    Where, if anywhere, does short expectancy exist inside the general features?

Run from trading_models_v2, for example:

    python shared/research/short_expectancy_discovery.py \
        --edge-root assets/XAUUSD/edges/crowdedlongunwind_short \
        --grid balanced \
        --top 50

The scanner labels hypothetical short entries from eligible feature rows using a
research-only OHLC triple-barrier calculation. Same-bar PT/SL ambiguity is
resolved conservatively as SL first. No tick data is used here.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

EPS = 1e-12
SIDE_VALUE = -1

GRIDS: Dict[str, Dict[str, Any]] = {
    "fast": {
        "horizons": [5, 10, 20, 30, 50],
        "barriers": [(0.35, 1.0), (0.50, 1.0), (0.75, 1.0), (1.0, 1.0), (1.0, 1.5)],
    },
    "balanced": {
        "horizons": [20, 50, 100, 250],
        "barriers": [(0.50, 1.0), (0.75, 1.0), (1.0, 1.0), (1.50, 1.0), (1.0, 1.5)],
    },
    "continuation": {
        "horizons": [50, 100, 250, 500, 1000],
        "barriers": [(1.0, 1.0), (1.25, 1.0), (1.50, 1.0), (2.0, 1.0), (1.0, 1.5)],
    },
}

CLEAR_MODULES = ["runtime", "model_rows", "candidate_events", "primary_side"]
VOLUME_LIKE_TERMS = ("tick", "ticks", "volume", "threshold", "proxy", "dollar")
BASE_EXCLUDE_COLUMNS = {
    "Datetime",
    "Bar_Start",
    "Open",
    "High",
    "Low",
    "Close",
    "edge_id",
    "side",
}

RESULT_FIELDS = [
    "rank",
    "horizon",
    "pt",
    "sl",
    "feature",
    "condition",
    "rows",
    "coverage",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "median_hold_bars",
    "mean_hold_bars",
    "years_tested",
    "positive_year_share",
    "worst_year_R",
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


def parse_ints(text: Optional[str]) -> Optional[List[int]]:
    if not text:
        return None
    return [int(x.strip()) for x in text.split(",") if x.strip()]


def parse_floats(text: Optional[str]) -> Optional[List[float]]:
    if not text:
        return None
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def parse_barriers(text: Optional[str]) -> Optional[List[Tuple[float, float]]]:
    if not text:
        return None
    pairs: List[Tuple[float, float]] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        sep = ":" if ":" in item else "/" if "/" in item else None
        if sep is None:
            raise SystemExit(f"Barrier must use pt:sl or pt/sl format: {item}")
        left, right = item.split(sep, 1)
        pt, sl = float(left), float(right)
        if pt <= 0 or sl <= 0:
            raise SystemExit(f"Barrier values must be positive: {item}")
        pairs.append((pt, sl))
    return pairs


def resolve_grid(args: argparse.Namespace) -> Tuple[List[int], List[Tuple[float, float]]]:
    if args.grid not in GRIDS:
        raise SystemExit(f"Unknown --grid {args.grid!r}. Use one of: {', '.join(sorted(GRIDS))}")
    preset = GRIDS[args.grid]
    horizons = parse_ints(args.horizons) or list(preset["horizons"])
    barriers = parse_barriers(args.barriers)
    pts = parse_floats(args.pts)
    sls = parse_floats(args.sls)
    if barriers is not None and (pts is not None or sls is not None):
        raise SystemExit("Use either --barriers or --pts/--sls, not both.")
    if barriers is None and (pts is not None or sls is not None):
        pts = pts or sorted({pt for pt, _ in preset["barriers"]})
        sls = sls or sorted({sl for _, sl in preset["barriers"]})
        barriers = [(pt, sl) for pt in pts for sl in sls]
    if barriers is None:
        barriers = list(preset["barriers"])
    return horizons, barriers


def import_edge_runtime(edge_root: Path) -> Any:
    edge_root = edge_root.resolve()
    if not (edge_root / "runtime.py").exists():
        raise SystemExit(f"Missing runtime.py in edge root: {edge_root}")
    for name in CLEAR_MODULES:
        sys.modules.pop(name, None)
    root = str(edge_root)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return importlib.import_module("runtime")


def load_bars(runtime: Any, features: pd.DataFrame) -> pd.DataFrame:
    if hasattr(runtime, "_load_asset_bars"):
        return runtime._load_asset_bars()  # noqa: SLF001
    cols = [c for c in ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"] if c in features.columns]
    if "Datetime" not in cols or "Close" not in cols:
        raise RuntimeError("Runtime has no _load_asset_bars() and features cannot supply Datetime/Close.")
    return features[cols].copy()


def get_schema_features(runtime: Any, features: pd.DataFrame) -> List[str]:
    if hasattr(runtime, "_schema_feature_names"):
        names = [str(x) for x in runtime._schema_feature_names()]  # noqa: SLF001
    else:
        names = [
            c
            for c in features.columns
            if c not in BASE_EXCLUDE_COLUMNS and pd.api.types.is_numeric_dtype(features[c])
        ]
    return [c for c in names if c in features.columns]


def exclude_volume_like_features(feature_names: Sequence[str]) -> List[str]:
    kept: List[str] = []
    for name in feature_names:
        lower = name.lower()
        if any(term in lower for term in VOLUME_LIKE_TERMS):
            continue
        kept.append(name)
    return kept


def normalise_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Open", "High", "Low", "Close"]
    missing = [c for c in required if c not in bars.columns]
    if missing:
        raise RuntimeError(f"Bars missing required columns: {missing}")
    out = bars.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    for col in ["Open", "High", "Low", "Close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required).copy()
    out = out[(out["Open"] > 0) & (out["High"] > 0) & (out["Low"] > 0) & (out["Close"] > 0)].copy()
    return out.sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)


def compute_event_vol(close: pd.Series, lookback: int, span: int, min_event_vol: float) -> pd.Series:
    log_ret = np.log((close + EPS) / (close.shift(lookback) + EPS))
    return log_ret.ewm(span=span, adjust=False).std().clip(lower=min_event_vol)


def prepare_discovery_frame(
    runtime: Any,
    *,
    sample_step: int,
    require_all_schema_features: bool,
    exclude_volume_like: bool,
    event_vol_lookback: int,
    event_vol_span: int,
    min_event_vol: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    features = runtime.build_features(drop_warmup_rows=False)
    bars = normalise_bars(load_bars(runtime, features))

    features = features.copy()
    features["Datetime"] = to_datetime_iso(features["Datetime"], utc=True, errors="coerce")
    features = features.dropna(subset=["Datetime"]).sort_values("Datetime").reset_index(drop=True)

    feature_names = get_schema_features(runtime, features)
    if exclude_volume_like:
        feature_names = exclude_volume_like_features(feature_names)
    if not feature_names:
        raise RuntimeError("No usable schema/general features found for discovery.")

    bar_pos = pd.Series(np.arange(len(bars), dtype="int64"), index=bars["Datetime"])
    close_at = bars.set_index("Datetime")["Close"]
    event_vol_at = compute_event_vol(bars["Close"].astype(float), event_vol_lookback, event_vol_span, min_event_vol)
    event_vol_at.index = bars["Datetime"]

    base_cols = ["Datetime"] + feature_names
    base = features[base_cols].copy()
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
        raise RuntimeError("No eligible discovery rows after warmup/sample filtering.")
    return bars, base, feature_names


def label_short_geometry(
    bars: pd.DataFrame,
    base: pd.DataFrame,
    *,
    horizon: int,
    pt: float,
    sl: float,
    cost_bps: float,
) -> pd.DataFrame:
    highs = bars["High"].to_numpy(dtype="float64")
    lows = bars["Low"].to_numpy(dtype="float64")
    closes = bars["Close"].to_numpy(dtype="float64")

    rows: List[Dict[str, Any]] = []
    for item in base.itertuples(index=False):
        loc = int(getattr(item, "bar_pos"))
        if loc + horizon >= len(bars):
            continue
        entry = float(getattr(item, "entry_price"))
        trgt = float(getattr(item, "event_vol"))
        if not np.isfinite(entry) or not np.isfinite(trgt) or entry <= 0 or trgt <= 0:
            continue

        risk_unit = sl * trgt
        pt_price = entry * (1.0 - pt * trgt)
        sl_price = entry * (1.0 + sl * trgt)
        future_high = highs[loc + 1 : loc + horizon + 1]
        future_low = lows[loc + 1 : loc + horizon + 1]

        pt_hits = future_low <= pt_price
        sl_hits = future_high >= sl_price
        pt_any = bool(pt_hits.any())
        sl_any = bool(sl_hits.any())
        barrier_hit = "vertical"
        hold_bars = horizon
        gross_r = np.nan

        if pt_any or sl_any:
            first_pt = int(np.argmax(pt_hits)) + 1 if pt_any else 10**9
            first_sl = int(np.argmax(sl_hits)) + 1 if sl_any else 10**9
            if first_sl <= first_pt:
                barrier_hit = "sl"
                hold_bars = first_sl
                gross_r = -1.0
            else:
                barrier_hit = "pt"
                hold_bars = first_pt
                gross_r = pt / sl
        else:
            end_close = closes[loc + horizon]
            gross_r = SIDE_VALUE * ((end_close / (entry + EPS)) - 1.0) / (risk_unit + EPS)

        cost_r = (cost_bps / 10000.0) / (risk_unit + EPS)
        rows.append(
            {
                "Datetime": getattr(item, "Datetime"),
                "bar_pos": loc,
                "horizon": horizon,
                "pt": pt,
                "sl": sl,
                "barrier_hit": barrier_hit,
                "gross_R": float(gross_r),
                "cost_R": float(cost_r),
                "net_R": float(gross_r - cost_r),
                "hold_bars": int(hold_bars),
            }
        )

    labels = pd.DataFrame(rows)
    if labels.empty:
        raise RuntimeError(f"No labels created for horizon={horizon}, pt={pt}, sl={sl}")
    labels["Datetime"] = to_datetime_iso(labels["Datetime"], utc=True, errors="coerce")
    labels["year"] = labels["Datetime"].dt.year.astype("int64")
    return labels


def summarize_subset(df: pd.DataFrame, *, min_year_rows: int) -> Dict[str, Any]:
    years = []
    for _year, group in df.groupby("year"):
        if len(group) >= min_year_rows:
            years.append(float(group["net_R"].mean()))
    year_count = len(years)
    positive_year_share = float(np.mean([x > 0 for x in years])) if years else float("nan")
    worst_year = float(np.min(years)) if years else float("nan")
    return {
        "rows": int(len(df)),
        "pt_rate": float(df["barrier_hit"].eq("pt").mean()),
        "sl_rate": float(df["barrier_hit"].eq("sl").mean()),
        "vertical_rate": float(df["barrier_hit"].eq("vertical").mean()),
        "gross_mean_R": float(df["gross_R"].mean()),
        "cost_mean_R": float(df["cost_R"].mean()),
        "net_mean_R": float(df["net_R"].mean()),
        "median_hold_bars": float(df["hold_bars"].median()),
        "mean_hold_bars": float(df["hold_bars"].mean()),
        "years_tested": year_count,
        "positive_year_share": positive_year_share,
        "worst_year_R": worst_year,
    }


def condition_masks(values: pd.Series, quantiles: Sequence[float]) -> List[Tuple[str, pd.Series]]:
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.replace([np.inf, -np.inf], np.nan).dropna()
    if valid.empty:
        return []

    out: List[Tuple[str, pd.Series]] = []
    seen: set = set()
    for q in quantiles:
        if not 0.0 < q < 0.5:
            continue
        low_cut = float(valid.quantile(q))
        high_cut = float(valid.quantile(1.0 - q))
        low_key = ("le", round(low_cut, 12))
        high_key = ("ge", round(high_cut, 12))
        if low_key not in seen:
            out.append((f"<=p{int(q * 100)}", numeric <= low_cut))
            seen.add(low_key)
        if high_key not in seen:
            out.append((f">=p{int((1.0 - q) * 100)}", numeric >= high_cut))
            seen.add(high_key)

    if valid.min() < 0 < valid.max():
        out.append(("<=0", numeric <= 0.0))
        out.append((">0", numeric > 0.0))
    return out


def evaluate_features(
    work: pd.DataFrame,
    feature_names: Sequence[str],
    *,
    total_rows: int,
    min_rows: int,
    min_year_rows: int,
    quantiles: Sequence[float],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for feature in feature_names:
        if feature not in work.columns:
            continue
        masks = condition_masks(work[feature], quantiles)
        for condition, mask in masks:
            subset = work.loc[mask.fillna(False)].copy()
            if len(subset) < min_rows:
                continue
            stats = summarize_subset(subset, min_year_rows=min_year_rows)
            stats.update(
                {
                    "feature": feature,
                    "condition": condition,
                    "coverage": float(len(subset) / max(total_rows, 1)),
                }
            )
            results.append(stats)
    return results


def print_baselines(baselines: List[Dict[str, Any]]) -> None:
    print("Baseline by geometry:")
    print(",".join(BASELINE_FIELDS))
    for row in baselines:
        print(
            f"{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},{row['rows']},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},{row['net_mean_R']:.5f},"
            f"{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f}"
        )


def print_ranked(results: List[Dict[str, Any]], top: int) -> None:
    ranked = sorted(results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    print()
    print(f"Top {min(top, len(ranked))} feature regions by net_mean_R:")
    print(",".join(RESULT_FIELDS))
    for rank, row in enumerate(ranked[:top], 1):
        print(
            f"{rank},{row['horizon']},{row['pt']:.2f},{row['sl']:.2f},"
            f"{row['feature']},{row['condition']},{row['rows']},{row['coverage']:.4f},"
            f"{row['pt_rate']:.4f},{row['sl_rate']:.4f},{row['vertical_rate']:.4f},"
            f"{row['gross_mean_R']:.5f},{row['cost_mean_R']:.5f},{row['net_mean_R']:.5f},"
            f"{row['median_hold_bars']:.2f},{row['mean_hold_bars']:.2f},"
            f"{row['years_tested']},{row['positive_year_share']:.4f},{row['worst_year_R']:.5f}"
        )


def save_outputs(edge_root: Path, args: argparse.Namespace, baselines: List[Dict[str, Any]], results: List[Dict[str, Any]]) -> None:
    out_dir = edge_root / "artifacts" / "short_expectancy_discovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = out_dir / "short_expectancy_baselines.csv"
    result_path = out_dir / "short_expectancy_feature_regions.csv"
    summary_path = out_dir / "short_expectancy_summary.json"

    with baseline_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=BASELINE_FIELDS)
        writer.writeheader()
        writer.writerows([{k: r.get(k) for k in BASELINE_FIELDS} for r in baselines])

    ranked = sorted(results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    rows_to_write = []
    for rank, row in enumerate(ranked, 1):
        out = {k: row.get(k) for k in RESULT_FIELDS if k != "rank"}
        out["rank"] = rank
        rows_to_write.append(out)
    with result_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS)
        writer.writeheader()
        writer.writerows(rows_to_write)

    summary = {
        "status": "research_only_short_expectancy_discovery_complete",
        "edge_root": edge_root.as_posix(),
        "grid": args.grid,
        "cost_bps": args.cost_bps,
        "sample_step": args.sample_step,
        "min_rows": args.min_rows,
        "min_year_rows": args.min_year_rows,
        "top_by_net_mean_R": ranked[: args.top],
        "outputs": {
            "baselines": baseline_path.as_posix(),
            "feature_regions": result_path.as_posix(),
            "summary": summary_path.as_posix(),
        },
        "note": "This diagnostic does not create an edge, train a model, or write final geometry.",
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print()
    print("Saved:")
    print(f"- {baseline_path}")
    print(f"- {result_path}")
    print(f"- {summary_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover short expectancy regions using general V2 schema features.")
    parser.add_argument("--edge-root", required=True, help="Any V2 edge folder for the asset to scan")
    parser.add_argument("--grid", default="balanced", choices=sorted(GRIDS))
    parser.add_argument("--horizons", default=None, help="Comma-separated horizons, e.g. 20,50,100")
    parser.add_argument("--barriers", default=None, help="Comma-separated PT:SL pairs, e.g. 0.5:1,1:1.5")
    parser.add_argument("--pts", default=None, help="Comma-separated PT multipliers; cross-product with --sls")
    parser.add_argument("--sls", default=None, help="Comma-separated SL multipliers; cross-product with --pts")
    parser.add_argument("--quantiles", default="0.10,0.20,0.30", help="Lower-tail quantiles to test; upper tails are mirrored")
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--sample-step", type=int, default=3, help="Use every Nth eligible row for speed and less overlap")
    parser.add_argument("--min-rows", type=int, default=300, help="Minimum rows per feature region")
    parser.add_argument("--min-year-rows", type=int, default=40, help="Minimum rows for a year to count in stability")
    parser.add_argument("--event-vol-lookback", type=int, default=50)
    parser.add_argument("--event-vol-span", type=int, default=100)
    parser.add_argument("--min-event-vol", type=float, default=1e-8)
    parser.add_argument("--allow-volume-like-features", action="store_true", help="Do not skip tick/volume/proxy/threshold-like schema columns")
    parser.add_argument("--no-require-all-schema-features", action="store_true", help="Do not require all schema features to be finite")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    if args.sample_step <= 0:
        raise SystemExit("--sample-step must be positive")
    started = time.time()
    edge_root = Path(args.edge_root).resolve()
    horizons, barriers = resolve_grid(args)
    quantiles = parse_floats(args.quantiles) or [0.10, 0.20, 0.30]

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

    print(f"edge_root: {edge_root}")
    print(f"grid: {args.grid}")
    print("horizons: " + ",".join(str(x) for x in horizons))
    print("barriers: " + ",".join(f"{pt:g}:{sl:g}" for pt, sl in barriers))
    print(f"cost_bps: {args.cost_bps:g}")
    print(f"sample_step: {args.sample_step}")
    print(f"eligible_rows: {len(base)}")
    print(f"general_features_scanned: {len(feature_names)}")
    print()

    baselines: List[Dict[str, Any]] = []
    all_results: List[Dict[str, Any]] = []
    for horizon in horizons:
        for pt, sl in barriers:
            labels = label_short_geometry(bars, base, horizon=horizon, pt=pt, sl=sl, cost_bps=args.cost_bps)
            work = base.merge(labels, on=["Datetime", "bar_pos"], how="inner", validate="one_to_one")
            baseline = summarize_subset(work, min_year_rows=args.min_year_rows)
            baseline.update({"horizon": horizon, "pt": pt, "sl": sl})
            baselines.append(baseline)
            results = evaluate_features(
                work,
                feature_names,
                total_rows=len(work),
                min_rows=args.min_rows,
                min_year_rows=args.min_year_rows,
                quantiles=quantiles,
            )
            for row in results:
                row.update({"horizon": horizon, "pt": pt, "sl": sl})
            all_results.extend(results)

    print_baselines(baselines)
    print_ranked(all_results, args.top)
    if args.save:
        save_outputs(edge_root, args, baselines, all_results)
    print()
    print(f"Done in {time.time() - started:.2f}s. No edge files were edited.")


if __name__ == "__main__":
    main()
