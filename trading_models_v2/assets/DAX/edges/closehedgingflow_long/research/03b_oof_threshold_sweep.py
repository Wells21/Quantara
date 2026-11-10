"""
03b_oof_threshold_sweep.py

V2 Step 03B: raw-OOF threshold sweep diagnostics for the current edge package.

Blueprint boundary
------------------
This script ports the useful V1 7B threshold-sweep metrics into V2, but only as
a raw-OOF research diagnostic.

It does NOT select the final deployable threshold. The Blueprint-required final
threshold must be selected after Step 04 sigmoid calibration using
calibrated_probability.

Why the script name is side-generic
-----------------------------------
In V1, one package compared long-only vs short-only because the OOF file could
contain both sides.

In V2, every edge package is already side-specialized:

    assets/<ASSET>/edges/<FAMILY>_<SIDE>/

So this script intentionally does not say `long_only` or `short_only` in the
filename. The edge folder itself defines the side.

Recommended run mode
--------------------
Run locally from trading_models_v2 after Step 03 creates OOF predictions:

    python ./assets/<ASSET>/edges/<FAMILY>_<SIDE>/research/03b_oof_threshold_sweep.py

Required input:

    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/oof/<edge_stem>_oof_predictions.csv

Expected outputs:

    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/oof/<edge_stem>_oof_raw_threshold_sweep.csv
    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/oof/<edge_stem>_oof_raw_best_summary.csv
    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/oof/<edge_stem>_oof_raw_threshold_sweep_manifest.json

V1 logic preserved from 7B
--------------------------
- threshold sweep grid
- trade count
- win rate
- average win/loss R
- expectancy R
- payoff ratio
- profit factor
- cumulative R
- max drawdown R
- Sharpe/Sortino/Calmar-style R diagnostics
- classification metrics at each threshold
- selected-trade precision/recall/F1
- best threshold selected by trading metrics for diagnostics

V2 portable edge contract checks
--------------------------------
- side is inferred from the edge package and must match if side column exists
- if PT/SL columns exist, barrier direction must match the inferred edge side
- uses raw_probability from Step 03
- uses realized_r from V2 model rows
- final deployable threshold is deferred to Step 04 calibrated_probability sweep
"""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[5]))
    from shared.io.timeparse import to_datetime_iso
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    f1_score,
    precision_score,
    recall_score,
)

warnings.filterwarnings("ignore")

THIS_FILE = Path(__file__).resolve()
CWD = Path.cwd().resolve()


def _looks_like_colab_root(path: Path) -> bool:
    return str(path).replace("\\", "/").rstrip("/") == "/content"


def _side_value_from_name(side: str) -> int:
    if side == "long":
        return 1
    if side == "short":
        return -1
    raise ValueError(f"Unsupported edge side: {side!r}. Expected 'long' or 'short'.")


def _parse_edge_segment(edge_segment: str) -> tuple[str, str]:
    family, sep, side = edge_segment.rpartition("_")
    if not sep or not family or side not in {"long", "short"}:
        raise ValueError(
            f"Cannot parse edge segment {edge_segment!r}. Expected '<family>_long' or '<family>_short'."
        )
    return family, side


def _context_from_edge_root(edge_root: Path) -> dict[str, Any]:
    asset = edge_root.parent.parent.name
    family, side = _parse_edge_segment(edge_root.name)
    edge_stem = f"{asset.lower()}_{family}_{side}"
    return {
        "asset": asset,
        "family": family,
        "side": side,
        "side_value": _side_value_from_name(side),
        "edge_id": f"{asset}/{family}_{side}",
        "edge_segment": f"{family}_{side}",
        "edge_stem": edge_stem,
    }


def _context_from_edge_stem(edge_stem: str) -> dict[str, Any]:
    parts = edge_stem.split("_")
    if len(parts) < 3:
        raise ValueError(f"Cannot parse edge stem {edge_stem!r}. Expected '<asset>_<family>_<side>'.")
    asset = parts[0].upper()
    side = parts[-1]
    family = "_".join(parts[1:-1])
    if side not in {"long", "short"} or not family:
        raise ValueError(f"Cannot parse edge stem {edge_stem!r}. Expected '<asset>_<family>_<side>'.")
    return {
        "asset": asset,
        "family": family,
        "side": side,
        "side_value": _side_value_from_name(side),
        "edge_id": f"{asset}/{family}_{side}",
        "edge_segment": f"{family}_{side}",
        "edge_stem": edge_stem,
    }


def _single_file(pattern: str, description: str) -> Path | None:
    matches = sorted(CWD.glob(pattern))
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise ValueError(f"Flat mode found multiple {description} files for pattern {pattern!r}: {matches}")
    return matches[0]


def _resolve_paths() -> tuple[str, Path, Path, Path, dict[str, Any]]:
    flat_oof = _single_file("*_oof_predictions.csv", "OOF prediction")
    if _looks_like_colab_root(CWD) and flat_oof is not None:
        edge_stem = flat_oof.name.removesuffix("_oof_predictions.csv")
        context = _context_from_edge_stem(edge_stem)
        return "flat_colab", CWD, CWD, flat_oof, context

    edge_root = THIS_FILE.parents[1]
    v2_root = THIS_FILE.parents[5]
    context = _context_from_edge_root(edge_root)
    oof_path = edge_root / "artifacts" / "oof" / f"{context['edge_stem']}_oof_predictions.csv"
    return "repo_local", v2_root, edge_root, oof_path, context


RUN_MODE, V2_ROOT, EDGE_ROOT, INPUT_OOF_CSV, EDGE_CONTEXT = _resolve_paths()
ASSET = EDGE_CONTEXT["asset"]
FAMILY = EDGE_CONTEXT["family"]
SIDE = EDGE_CONTEXT["side"]
SIDE_VALUE = EDGE_CONTEXT["side_value"]
EDGE_ID = EDGE_CONTEXT["edge_id"]
EDGE_SIDE_SEGMENT = EDGE_CONTEXT["edge_segment"]
EDGE_STEM = EDGE_CONTEXT["edge_stem"]

OUTPUT_DIR = CWD if RUN_MODE == "flat_colab" else EDGE_ROOT / "artifacts" / "oof"

OUTPUT_SWEEP_CSV = OUTPUT_DIR / f"{EDGE_STEM}_oof_raw_threshold_sweep.csv"
OUTPUT_BEST_SUMMARY_CSV = OUTPUT_DIR / f"{EDGE_STEM}_oof_raw_best_summary.csv"
OUTPUT_MANIFEST_JSON = OUTPUT_DIR / f"{EDGE_STEM}_oof_raw_threshold_sweep_manifest.json"

TIME_COL = "Datetime"
# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md: Step 03's OOF
# CSV now carries Task 2A's executable-target columns forward (it reads the
# resolution CSV and copies every column into its OOF output), so the
# threshold sweep evaluates against the executable target/economics, not the
# old gross meta_label/realized_r.
TARGET_COL = "quote_executable_meta_label"
RAW_PROB_COL = "raw_probability"
REALIZED_R_COL = "quote_executable_r"
OPTIONAL_SIDE_COL = "side"
ENTRY_PRICE_COL = "entry_price"
PT_PRICE_COL = "pt_price"
SL_PRICE_COL = "sl_price"

THRESHOLDS = [round(x, 2) for x in np.arange(0.10, 0.96, 0.01)]
MIN_TRADES_FOR_SELECTION = 30


class ThresholdSweepError(RuntimeError):
    """Raised when Step 03B must fail closed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return str(obj)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=json_default) + "\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(V2_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path.resolve()).replace("\\", "/")


def preflight_check() -> None:
    if not INPUT_OOF_CSV.exists():
        raise ThresholdSweepError(
            "Step 03B preflight failed. Missing Step 03 OOF predictions:\n"
            f"  - {INPUT_OOF_CSV}\n\n"
            "Run Step 03 first:\n"
            f"  python .\\assets\\{ASSET}\\edges\\{FAMILY}_{SIDE}\\research\\03_generate_oof_predictions.py"
        )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _validate_side_and_barriers(df: pd.DataFrame) -> None:
    if OPTIONAL_SIDE_COL in df.columns:
        df[OPTIONAL_SIDE_COL] = pd.to_numeric(df[OPTIONAL_SIDE_COL], errors="coerce")
        non_matching = df[df[OPTIONAL_SIDE_COL].notna() & (df[OPTIONAL_SIDE_COL].astype(int) != SIDE_VALUE)]
        if len(non_matching) > 0:
            raise ThresholdSweepError(
                f"OOF file contains {len(non_matching)} rows whose side column does not match edge side "
                f"{SIDE!r} ({SIDE_VALUE}). V2 edge OOF files must be side-specialized."
            )

    price_cols = {ENTRY_PRICE_COL, PT_PRICE_COL, SL_PRICE_COL}
    if price_cols.issubset(df.columns):
        for col in price_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        valid_price_rows = df.dropna(subset=list(price_cols)).copy()
        if valid_price_rows.empty:
            return
        if SIDE_VALUE == 1:
            if not (valid_price_rows[PT_PRICE_COL] > valid_price_rows[ENTRY_PRICE_COL]).all():
                raise ThresholdSweepError(f"{EDGE_ID} OOF rows must have pt_price above entry_price for long edges.")
            if not (valid_price_rows[SL_PRICE_COL] < valid_price_rows[ENTRY_PRICE_COL]).all():
                raise ThresholdSweepError(f"{EDGE_ID} OOF rows must have sl_price below entry_price for long edges.")
        elif SIDE_VALUE == -1:
            if not (valid_price_rows[PT_PRICE_COL] < valid_price_rows[ENTRY_PRICE_COL]).all():
                raise ThresholdSweepError(f"{EDGE_ID} OOF rows must have pt_price below entry_price for short edges.")
            if not (valid_price_rows[SL_PRICE_COL] > valid_price_rows[ENTRY_PRICE_COL]).all():
                raise ThresholdSweepError(f"{EDGE_ID} OOF rows must have sl_price above entry_price for short edges.")
        else:
            raise ThresholdSweepError(f"Unsupported SIDE_VALUE={SIDE_VALUE!r} for {EDGE_ID}.")


def load_oof(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if TIME_COL not in df.columns:
        first_col = df.columns[0]
        df[first_col] = to_datetime_iso(df[first_col], utc=True, errors="coerce")
        df = df.rename(columns={first_col: TIME_COL})
    df[TIME_COL] = to_datetime_iso(df[TIME_COL], utc=True, errors="coerce")
    required = [TIME_COL, RAW_PROB_COL, TARGET_COL, REALIZED_R_COL]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ThresholdSweepError(f"OOF predictions missing required columns: {missing}")

    df[RAW_PROB_COL] = pd.to_numeric(df[RAW_PROB_COL], errors="coerce")
    df[TARGET_COL] = pd.to_numeric(df[TARGET_COL], errors="coerce")
    df[REALIZED_R_COL] = pd.to_numeric(df[REALIZED_R_COL], errors="coerce")
    df = df.dropna(subset=[TIME_COL, RAW_PROB_COL, TARGET_COL, REALIZED_R_COL]).copy()
    if df.empty:
        raise ThresholdSweepError("OOF predictions are empty after required-column filtering.")
    df[TARGET_COL] = df[TARGET_COL].astype(int)

    _validate_side_and_barriers(df)

    return df.sort_values(TIME_COL).set_index(TIME_COL)


def safe_div(a: float, b: float) -> float:
    return float(a / b) if b not in [0, 0.0] else np.nan


def max_drawdown_from_equity(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity - running_max
    return float(drawdown.min()) if len(drawdown) else 0.0


def classification_metrics_at_threshold(df_valid: pd.DataFrame, threshold: float) -> dict[str, float]:
    y_true = df_valid[TARGET_COL].values.astype(int)
    y_pred = (df_valid[RAW_PROB_COL].values >= threshold).astype(int)
    return {
        "clf_precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "clf_recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "clf_f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "clf_accuracy": float(accuracy_score(y_true, y_pred)),
        "clf_balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
        "predicted_positive_rate": float(np.mean(y_pred)),
    }


def compute_trade_metrics(df_sel: pd.DataFrame) -> dict[str, float | int]:
    if len(df_sel) == 0:
        return {}
    r = df_sel[REALIZED_R_COL].dropna().values.astype(float)
    if len(r) == 0:
        return {}

    wins = r[r > 0]
    losses = r[r <= 0]
    avg_win = float(np.mean(wins)) if len(wins) else np.nan
    avg_loss = float(np.mean(losses)) if len(losses) else np.nan
    gross_profit = float(np.sum(wins)) if len(wins) else 0.0
    gross_loss = float(np.abs(np.sum(losses))) if len(losses) else 0.0

    equity = pd.Series(r).cumsum()
    cumulative_r = float(equity.iloc[-1]) if len(equity) else 0.0
    max_dd_r = max_drawdown_from_equity(equity)

    mean_r = float(np.mean(r))
    std_r = float(np.std(r, ddof=1)) if len(r) > 1 else np.nan
    downside_std = float(np.std(r[r < 0], ddof=1)) if np.sum(r < 0) > 1 else np.nan

    y_true = df_sel[TARGET_COL].values.astype(int)
    y_pred = np.ones(len(y_true), dtype=int)

    return {
        "trade_count": int(len(r)),
        "win_rate": float(np.mean(r > 0)),
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "expectancy_r": mean_r,
        "payoff_ratio": safe_div(avg_win, abs(avg_loss)) if not np.isnan(avg_loss) and avg_loss != 0 else np.nan,
        "profit_factor": safe_div(gross_profit, gross_loss),
        "gross_profit_r": gross_profit,
        "gross_loss_r": gross_loss,
        "cumulative_r": cumulative_r,
        "max_drawdown_r": max_dd_r,
        "sharpe_r": safe_div(mean_r, std_r),
        "sortino_r": safe_div(mean_r, downside_std),
        "calmar_r": safe_div(cumulative_r, abs(max_dd_r)) if max_dd_r < 0 else np.nan,
        "selected_precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "selected_recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "selected_f1": float(f1_score(y_true, y_pred, zero_division=0)),
    }


def threshold_sweep(df: pd.DataFrame, thresholds: list[float]) -> pd.DataFrame:
    valid = df.dropna(subset=[RAW_PROB_COL, REALIZED_R_COL]).copy()
    rows = []
    for threshold in thresholds:
        selected = valid[valid[RAW_PROB_COL] >= threshold].copy()
        if len(selected) == 0:
            continue
        trade_metrics = compute_trade_metrics(selected)
        if not trade_metrics:
            continue
        clf_metrics = classification_metrics_at_threshold(valid, threshold)
        row = {
            "edge_id": EDGE_ID,
            "segment": EDGE_SIDE_SEGMENT,
            "threshold": float(threshold),
            "candidate_count": int(len(valid)),
            "take_rate": float(len(selected) / len(valid)),
        }
        row.update(trade_metrics)
        row.update(clf_metrics)
        rows.append(row)
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("threshold").reset_index(drop=True)


def choose_best_threshold(sweep_df: pd.DataFrame) -> pd.Series:
    if sweep_df.empty:
        raise ThresholdSweepError("Threshold sweep is empty.")
    eligible = sweep_df[sweep_df["trade_count"] >= MIN_TRADES_FOR_SELECTION].copy()
    if eligible.empty:
        eligible = sweep_df.copy()
    return eligible.sort_values(
        ["expectancy_r", "profit_factor", "trade_count"],
        ascending=[False, False, False],
    ).iloc[0]


def main() -> None:
    print("=" * 80, flush=True)
    print(f"V2 STEP 03B: EDGE RAW-OOF THRESHOLD SWEEP DIAGNOSTICS — {EDGE_ID}", flush=True)
    print("=" * 80, flush=True)
    print(f"Edge: {EDGE_ID}", flush=True)
    print(f"Run mode: {RUN_MODE}", flush=True)
    print(f"OOF input: {INPUT_OOF_CSV}", flush=True)
    print(f"Output directory: {OUTPUT_DIR}", flush=True)
    preflight_check()
    print("Preflight: PASS", flush=True)

    df = load_oof(INPUT_OOF_CSV)
    print(f"Valid edge OOF rows: {len(df):,}", flush=True)
    print(f"Date range: {df.index.min()} -> {df.index.max()}", flush=True)
    print(f"Threshold grid: {THRESHOLDS[0]:.2f} -> {THRESHOLDS[-1]:.2f} step 0.01", flush=True)
    print("Using raw_probability only for diagnostics. Deployable threshold waits for Step 04 calibrated_probability.", flush=True)

    sweep = threshold_sweep(df, THRESHOLDS)
    if sweep.empty:
        raise ThresholdSweepError("Threshold sweep returned no rows.")
    best = choose_best_threshold(sweep)
    best_summary = pd.DataFrame([best])

    sweep.to_csv(OUTPUT_SWEEP_CSV, index=False)
    best_summary.to_csv(OUTPUT_BEST_SUMMARY_CSV, index=False)

    manifest = {
        "created_at": now_utc(),
        "edge_id": EDGE_ID,
        "run_mode": RUN_MODE,
        "script": display_path(THIS_FILE),
        "blueprint_stage": "raw_oof_edge_threshold_sweep_diagnostics_before_sigmoid_calibration",
        "feature_engineering_performed_here": False,
        "input_oof_csv": display_path(INPUT_OOF_CSV),
        "probability_column": RAW_PROB_COL,
        "r_multiple_column": REALIZED_R_COL,
        "segment": EDGE_SIDE_SEGMENT,
        "side_specialization": {"edge_side": SIDE, "side_value": SIDE_VALUE},
        "rows_valid": int(len(df)),
        "thresholds": THRESHOLDS,
        "min_trades_for_selection": MIN_TRADES_FOR_SELECTION,
        "best_raw_diagnostic": best.to_dict(),
        "deployable": False,
        "deployability_blockers": [
            "sigmoid calibration not yet complete",
            "calibrated threshold sweep not yet complete",
            "raw_probability threshold is diagnostic only",
            "runtime.py::score not yet wired to model/calibrator artifacts",
            "deployability replay audit not yet passed",
        ],
        "outputs": {
            "threshold_sweep": display_path(OUTPUT_SWEEP_CSV),
            "best_summary": display_path(OUTPUT_BEST_SUMMARY_CSV),
            "manifest": display_path(OUTPUT_MANIFEST_JSON),
        },
        "next_ordered_step": "04_fit_sigmoid_calibrator_and_thresholds.py",
        "notes": [
            "This ports the useful V1 7B threshold-sweep metrics into V2 edge-specialized form.",
            "The edge folder or flat filename defines the side, so this script is portable across side-specialized edges.",
            "The selected raw threshold is diagnostic and must not be used as the deployable live threshold.",
            "Final deployable threshold must be selected after sigmoid calibration using calibrated_probability.",
        ],
    }
    save_json(OUTPUT_MANIFEST_JSON, manifest)

    print("\nBest raw diagnostic threshold for this edge:", flush=True)
    print(best.to_string(), flush=True)
    print("\nSaved outputs:", flush=True)
    print(f"  - {OUTPUT_SWEEP_CSV}", flush=True)
    print(f"  - {OUTPUT_BEST_SUMMARY_CSV}", flush=True)
    print(f"  - {OUTPUT_MANIFEST_JSON}", flush=True)
    print("Next ordered step: 04_fit_sigmoid_calibrator_and_thresholds.py", flush=True)


if __name__ == "__main__":
    try:
        main()
    except ThresholdSweepError as exc:
        raise SystemExit(f"❌ {exc}") from exc
