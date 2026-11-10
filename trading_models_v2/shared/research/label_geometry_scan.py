"""Research-only label geometry scanner for V2 edge packages.

This script imports an edge runtime as a black box, builds that edge's normal
features and candidate events, scans PT/SL/horizon combinations in memory, and
prints results. It does not edit any edge files and does not choose a final
geometry automatically.

Run from trading_models_v2:
    python shared/research/label_geometry_scan.py --edge-root assets/XAUUSD/edges/crowdedlongunwind_short --grid unwind
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

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

GRIDS: dict[str, dict[str, Any]] = {
    "unwind": {
        "horizons": [5, 10, 15, 20, 25, 30, 40, 50],
        "barriers": [(0.35, 1.0), (0.50, 1.0), (0.60, 1.0), (0.75, 1.0), (1.0, 1.5)],
    },
    "fast_reaction": {
        "horizons": [3, 5, 8, 10, 15, 20],
        "barriers": [(0.25, 1.0), (0.35, 1.0), (0.50, 1.0), (0.75, 1.0)],
    },
    "continuation": {
        "horizons": [50, 100, 250, 500, 1000],
        "barriers": [(1.0, 1.0), (1.25, 1.0), (1.50, 1.0), (2.0, 1.0), (1.0, 1.5)],
    },
    "breakout": {
        "horizons": [20, 50, 100, 250, 500],
        "barriers": [(0.75, 0.75), (1.0, 0.75), (1.0, 1.0), (1.25, 1.0), (1.50, 1.0)],
    },
    "balanced": {
        "horizons": [20, 50, 100, 250, 500, 1000],
        "barriers": [(0.50, 0.50), (0.50, 1.0), (0.75, 1.0), (1.0, 1.0), (1.50, 1.0), (2.0, 1.0), (1.0, 1.5)],
    },
}

CLEAR_MODULES = ["runtime", "model_rows", "candidate_events", "primary_side"]
OUTPUT_FIELDS = [
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


def parse_ints(text: str | None) -> list[int] | None:
    if not text:
        return None
    return [int(x.strip()) for x in text.split(",") if x.strip()]


def parse_floats(text: str | None) -> list[float] | None:
    if not text:
        return None
    return [float(x.strip()) for x in text.split(",") if x.strip()]


def parse_barriers(text: str | None) -> list[tuple[float, float]] | None:
    if not text:
        return None
    pairs: list[tuple[float, float]] = []
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


def resolve_grid(args: argparse.Namespace) -> tuple[list[int], list[tuple[float, float]]]:
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


def import_edge(edge_root: Path) -> tuple[Any, Any]:
    edge_root = edge_root.resolve()
    if not (edge_root / "runtime.py").exists():
        raise SystemExit(f"Missing runtime.py in edge root: {edge_root}")
    if not (edge_root / "model_rows.py").exists():
        raise SystemExit(f"Missing model_rows.py in edge root: {edge_root}")
    for name in CLEAR_MODULES:
        sys.modules.pop(name, None)
    root = str(edge_root)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    runtime = importlib.import_module("runtime")
    model_rows = importlib.import_module("model_rows")
    return runtime, model_rows


def load_bars(runtime: Any, features: pd.DataFrame) -> pd.DataFrame:
    if hasattr(runtime, "_load_asset_bars"):
        return runtime._load_asset_bars()  # noqa: SLF001
    cols = [c for c in ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"] if c in features.columns]
    if "Datetime" not in cols or "Close" not in cols:
        raise RuntimeError("Runtime has no _load_asset_bars() and features cannot supply Datetime/Close.")
    return features[cols].copy()


def set_geometry(model_rows: Any, horizon: int, pt: float, sl: float) -> None:
    # Process-local only. This does not write to disk.
    if hasattr(model_rows, "VERTICAL_HORIZON"):
        model_rows.VERTICAL_HORIZON = int(horizon)
    if hasattr(model_rows, "PT_MULTIPLIER"):
        model_rows.PT_MULTIPLIER = float(pt)
    if hasattr(model_rows, "SL_MULTIPLIER"):
        model_rows.SL_MULTIPLIER = float(sl)


def bar_maps(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    b = bars.copy()
    b["Datetime"] = to_datetime_iso(b["Datetime"], utc=True, errors="coerce")
    b["Close"] = pd.to_numeric(b["Close"], errors="coerce")
    b = b.dropna(subset=["Datetime", "Close"]).sort_values("Datetime").drop_duplicates("Datetime")
    close_at = b.set_index("Datetime")["Close"]
    number_at = pd.Series(np.arange(len(b), dtype="int64"), index=b["Datetime"])
    return close_at, number_at


def summarize_rows(rows: pd.DataFrame, pt: float, sl: float, close_at: pd.Series, number_at: pd.Series, cost_bps: float) -> dict[str, float | int]:
    r = rows.copy()
    r["Datetime"] = to_datetime_iso(r["Datetime"], utc=True, errors="coerce")
    r["t1"] = to_datetime_iso(r["t1"], utc=True, errors="coerce")
    r["end_close"] = r["t1"].map(close_at)
    trgt = pd.to_numeric(r["Event_Vol"], errors="coerce").astype(float)
    entry = pd.to_numeric(r["entry_price"], errors="coerce").astype(float)
    end_close = pd.to_numeric(r["end_close"], errors="coerce").astype(float)
    side = pd.to_numeric(r["side"], errors="coerce").astype(float)
    risk_unit = (sl * trgt).clip(lower=EPS)
    vertical_r = side * ((end_close / (entry + EPS)) - 1.0) / (risk_unit + EPS)
    gross_r = np.select(
        [r["barrier_hit"].eq("pt"), r["barrier_hit"].eq("sl")],
        [pt / sl, -1.0],
        default=vertical_r,
    )
    cost_r = (cost_bps / 10000.0) / (risk_unit + EPS)
    valid = np.isfinite(gross_r) & np.isfinite(cost_r)
    hold = pd.to_numeric(r["t1"].map(number_at) - r["Datetime"].map(number_at), errors="coerce")
    return {
        "rows": int(len(r)),
        "pt_rate": float(r["barrier_hit"].eq("pt").mean()),
        "sl_rate": float(r["barrier_hit"].eq("sl").mean()),
        "vertical_rate": float(r["barrier_hit"].eq("vertical").mean()),
        "gross_mean_R": float(np.nanmean(gross_r[valid])) if valid.any() else float("nan"),
        "cost_mean_R": float(np.nanmean(cost_r[valid])) if valid.any() else float("nan"),
        "net_mean_R": float(np.nanmean((gross_r - cost_r)[valid])) if valid.any() else float("nan"),
        "median_hold_bars": float(np.nanmedian(hold)) if hold.notna().any() else float("nan"),
        "mean_hold_bars": float(np.nanmean(hold)) if hold.notna().any() else float("nan"),
    }


def run_scan(runtime: Any, model_rows: Any, horizons: list[int], barriers: list[tuple[float, float]], cost_bps: float) -> list[dict[str, Any]]:
    features = runtime.build_features(drop_warmup_rows=False)
    events = runtime.generate_candidate_events(features)
    bars = load_bars(runtime, features)
    close_at, number_at = bar_maps(bars)
    print(f"events: {len(events)}")
    print()
    results: list[dict[str, Any]] = []
    for horizon in horizons:
        for pt, sl in barriers:
            set_geometry(model_rows, horizon, pt, sl)
            try:
                rows, _manifest = runtime.build_model_rows(features=features, events=events, bars=bars, vertical_horizon=horizon)
                result: dict[str, Any] = {"horizon": horizon, "pt": pt, "sl": sl}
                result.update(summarize_rows(rows, pt, sl, close_at, number_at, cost_bps))
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                print(f"{horizon},{pt:.2f},{sl:.2f},FAILED,{exc}")
    return results


def print_results(results: list[dict[str, Any]], top: int) -> None:
    print(",".join(OUTPUT_FIELDS))
    for r in results:
        print(
            f"{r['horizon']},{r['pt']:.2f},{r['sl']:.2f},{r['rows']},"
            f"{r['pt_rate']:.4f},{r['sl_rate']:.4f},{r['vertical_rate']:.4f},"
            f"{r['gross_mean_R']:.5f},{r['cost_mean_R']:.5f},{r['net_mean_R']:.5f},"
            f"{r['median_hold_bars']:.2f},{r['mean_hold_bars']:.2f}"
        )
    if top > 0 and results:
        ranked = sorted(results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
        print()
        print(f"Top {top} by net_mean_R:")
        print("rank,horizon,pt,sl,rows,net_mean_R,gross_mean_R,vertical_rate,median_hold_bars")
        for i, r in enumerate(ranked[:top], 1):
            print(f"{i},{r['horizon']},{r['pt']:.2f},{r['sl']:.2f},{r['rows']},{r['net_mean_R']:.5f},{r['gross_mean_R']:.5f},{r['vertical_rate']:.4f},{r['median_hold_bars']:.2f}")


def save_results(edge_root: Path, args: argparse.Namespace, results: list[dict[str, Any]]) -> None:
    out_dir = edge_root / "artifacts" / "label_geometry_scan"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "label_geometry_grid.csv"
    json_path = out_dir / "label_geometry_summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(results)
    ranked = sorted(results, key=lambda x: (np.nan_to_num(x["net_mean_R"], nan=-1e9), x["rows"]), reverse=True)
    summary = {
        "status": "research_only_label_geometry_scan_complete",
        "edge_root": edge_root.as_posix(),
        "grid": args.grid,
        "cost_bps": args.cost_bps,
        "top_by_net_mean_R": ranked[: args.top],
        "outputs": {"csv": csv_path.as_posix(), "summary": json_path.as_posix()},
        "note": "This script does not select or write final geometry.",
    }
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print()
    print("Saved:")
    print(f"- {csv_path}")
    print(f"- {json_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only PT/SL/horizon scan for any V2 edge runtime.")
    parser.add_argument("--edge-root", required=True)
    parser.add_argument("--grid", default="balanced", choices=sorted(GRIDS))
    parser.add_argument("--horizons", default=None, help="Comma-separated horizons, e.g. 5,10,20,50")
    parser.add_argument("--barriers", default=None, help="Comma-separated pt:sl pairs, e.g. 0.5:1,1:1.5")
    parser.add_argument("--pts", default=None, help="Comma-separated PT multipliers; cross-product with --sls")
    parser.add_argument("--sls", default=None, help="Comma-separated SL multipliers; cross-product with --pts")
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()

    started = time.time()
    edge_root = Path(args.edge_root).resolve()
    horizons, barriers = resolve_grid(args)
    runtime, model_rows = import_edge(edge_root)

    print(f"edge_root: {edge_root}")
    print(f"grid: {args.grid}")
    print("horizons: " + ",".join(str(x) for x in horizons))
    print("barriers: " + ",".join(f"{pt:g}:{sl:g}" for pt, sl in barriers))
    print(f"cost_bps: {args.cost_bps:g}")
    print()

    results = run_scan(runtime, model_rows, horizons, barriers, args.cost_bps)
    print_results(results, args.top)
    if args.save:
        save_results(edge_root, args, results)
    print()
    print(f"Done in {time.time() - started:.2f}s. No edge files were edited.")


if __name__ == "__main__":
    main()
