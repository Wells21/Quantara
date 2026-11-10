# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
"""Generic SOTD Step 04 sigmoid calibration and calibrated threshold sweep.

This shared module replaces copied edge-local Step 04 implementations.

SOTD probability path:
    raw_probability -> sigmoid calibrated_probability -> payoff-aware AFML bet size -> calibrated-threshold decision

Supported run modes
-------------------
1. Repo-local mode:
   Reads/writes canonical edge artifacts under assets/<ASSET>/edges/<edge>/.

2. Flat Colab mode:
   If current working directory is /content and contains <edge_stem>_oof_predictions.csv,
   reads/writes flat files in /content. This preserves the existing Colab workflow for
   heavy research steps while keeping the local repo verifiers as the source of truth.
"""

from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import hashlib
import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

try:
    from skfolio.model_selection import CombinatorialPurgedCV
except Exception as exc:  # pragma: no cover
    CombinatorialPurgedCV = None
    SKFOLIO_IMPORT_ERROR = exc
else:
    SKFOLIO_IMPORT_ERROR = None

from shared.calibration.sigmoid_afml import (  # noqa: E402
    AFML_ECONOMIC_STRENGTH_COL,
    AFML_EFFECTIVE_PROBABILITY_COL,
    AFML_SIZING_METHOD,
    AFML_SIZING_METHOD_COL,
    BREAK_EVEN_PROBABILITY_COL,
    REWARD_RISK_RATIO_COL,
    PAYOFF_PROFILE_VERSION,
    EXECUTED_TARGET_CONTRACT_VERSION,
    break_even_probability_from_reward_risk,
    empirical_payoff_profile,
    payoff_profile_afml_components,
    validate_payoff_profile,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402
from shared.models.cpcv_paths import path_columns, stacked_path_fit_inputs, stacked_path_fit_weights  # noqa: E402
from shared.edges.edge_yaml_contract import validate_edge_yaml_file  # noqa: E402
from shared.edges.trade_geometry import load_trade_geometry  # noqa: E402
from shared.validation.v3_semantic_contract import (  # noqa: E402
    GROSS_LABEL_CONTRACT_VERSION,
    GROSS_TARGET_DEFINITION,
    attach_payoff_context_columns,
    context_from_mapping,
    validate_gross_label_frame,
    validate_step03_generation,
)

warnings.filterwarnings("ignore")

TIME_COL = "Datetime"
# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md: Step 03's OOF
# CSV now carries Task 2A's executable-target columns forward directly (it
# reads the resolution CSV and copies every column into its OOF output), so
# Step 04 calibrates/thresholds against the executable target/economics
# straight from that file -- no separate Step 03A execution replay/merge.
GROSS_TARGET_COL = "gross_meta_label"
GROSS_REALIZED_R_COL = "gross_r"
TARGET_COL = "quote_executable_meta_label"
ELIGIBILITY_COL = "mldp_target_eligible"
T1_COL = "gross_t1"
RAW_PROB_COL = "raw_probability"
RAW_PROB_WORK_COL = "oof_prob_raw"
CAL_PROB_COL = "calibrated_probability"
# QUANTARA_V3_LIVE_CONSISTENT_CALIBRATION_ARTIFACT (2026-09-15): CAL_PROB_COL
# above is the CPCV-fold-AVERAGED research diagnostic (cpcv_calibrate_edge) --
# an average of several different per-fold sigmoid fits (empirically shown to
# vary wildly fold-to-fold, e.g. coef ranging 0.03-0.48 across 10 folds on one
# edge), NOT what live scoring produces. LIVE_CAL_PROB_COL below is raw_
# probability passed through the SAME single sigmoid live scoring actually
# uses (fit_final_live_sigmoid's probability_calibrator_sigmoid_side_<side>.
# json). Use LIVE_CAL_PROB_COL for anything meant to reflect what a live
# signal's probability would actually be; CAL_PROB_COL is retained only for
# the calibration-quality bucket/ECE diagnostics it was designed for.
LIVE_CAL_PROB_COL = "live_calibrated_probability"
AFML_BET_COL = "afml_bet_size"
ECONOMIC_R_COL = "quote_executable_r"
EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"
OPTIONAL_SIDE_COL = "side"

N_FOLDS = 5
N_TEST_FOLDS = 2
PURGED_SIZE = 0
EMBARGO_SIZE = 0
MANUAL_EMBARGO_SIZE = 100  # minimum embargo (observations)
# AFML Ch.7 walkthrough 2026-10-24: embargo h ~ .01T (AFML s.7.4.2 p.107) -> max(100, ceil(1% of rows)); purge on the label's own t1.
EMBARGO_PCT = 0.01
EMBARGO_RULE = "max(100, ceil(0.01 x rows)) observations after each contiguous test block (AFML s.7.4.2 p.107)"
PURGE_T1_RULE = "label_t1 (Task 2A training-label end), fallback gross_t1"


def embargo_size_for(n_rows: int) -> int:
    return max(MANUAL_EMBARGO_SIZE, int(np.ceil(EMBARGO_PCT * int(n_rows))))
MIN_TOTAL_ROWS_PER_SIDE = 250
MIN_TRAIN_ROWS_TO_FIT = 200
MIN_CLASS_COUNT_TO_FIT = 20
PROBABILITY_CLIP = 1e-6
THRESHOLD_STEP = 0.01
THRESHOLD_MIN_BOUND = 0.01
THRESHOLD_MAX_BOUND = 0.99
THRESHOLD_DECIMALS = 2
N_CALIBRATION_BINS = 10
MIN_TRADES_FOR_SELECTION = 30
EPS = 1e-12
PROBABILITY_FLOW = "raw_probability -> sigmoid calibrated_probability -> payoff-aware AFML bet size -> calibrated-threshold decision"


class CalibrationError(RuntimeError):
    """Raised when Step 04 must fail closed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_default(obj: Any) -> Any:
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
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


def _looks_like_colab_root(path: Path) -> bool:
    return str(path).replace("\\", "/").rstrip("/") == "/content"


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def safe_div(a: float, b: float) -> float:
    try:
        if b is None or pd.isna(b) or abs(float(b)) < EPS:
            return np.nan
        return float(a) / float(b)
    except Exception:
        return np.nan


def load_payoff_geometry(layout: EdgeLayout) -> dict[str, Any]:
    """Load and validate the edge's finalized, pre-trade payoff geometry."""

    return load_trade_geometry(
        layout.edge_root / "edge.yaml",
        expected_edge_id=layout.edge_id,
        expected_side_name=layout.side_name,
    )


def clip_probability(p: Any, eps: float = PROBABILITY_CLIP) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), eps, 1.0 - eps)


def logit(p: Any, eps: float = PROBABILITY_CLIP) -> np.ndarray:
    p = clip_probability(p, eps)
    return np.log(p / (1.0 - p))


def sigmoid(x: Any) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    return 1.0 / (1.0 + np.exp(-np.clip(x, -60.0, 60.0)))



class Step04Paths:
    def __init__(self, layout: EdgeLayout, run_mode: str, root: Path, edge_root: Path):
        self.layout = layout
        self.run_mode = run_mode
        self.root = root
        self.edge_root = edge_root
        oof_dir = root if run_mode == "flat_colab" else edge_root / "artifacts" / "oof"
        execution_dir = root if run_mode == "flat_colab" else edge_root / "artifacts" / "execution"
        calibration_dir = root if run_mode == "flat_colab" else edge_root / "artifacts" / "calibration"
        thresholds_dir = root if run_mode == "flat_colab" else edge_root / "artifacts" / "thresholds"
        self.oof_dir = oof_dir
        self.execution_dir = execution_dir
        self.calibration_dir = calibration_dir
        self.thresholds_dir = thresholds_dir
        self.input_oof_csv = oof_dir / f"{layout.edge_stem}_oof_predictions.csv"
        self.input_raw_sweep_csv = oof_dir / f"{layout.edge_stem}_oof_raw_threshold_sweep.csv"
        self.sigmoid_calibrator_json = calibration_dir / f"probability_calibrator_sigmoid_side_{layout.side_name}.json"
        self.sigmoid_calibrator_pkl = calibration_dir / f"{layout.edge_stem}_sigmoid_calibrator.pkl"
        self.calibrated_oof_output = calibration_dir / f"{layout.edge_stem}_calibrated_oof_predictions.csv"
        self.calibration_report_output = calibration_dir / f"{layout.edge_stem}_calibration_report.json"
        self.calibration_bins_output = calibration_dir / f"{layout.edge_stem}_calibration_bins.csv"
        self.calibration_fold_report_output = calibration_dir / f"{layout.edge_stem}_calibration_fold_report.csv"
        self.calibration_manifest_output = calibration_dir / f"{layout.edge_stem}_calibration_manifest.json"
        self.threshold_sweep_output = thresholds_dir / f"{layout.edge_stem}_calibrated_threshold_sweep.csv"
        self.selected_threshold_output = thresholds_dir / f"{layout.edge_stem}_selected_threshold.json"
        self.threshold_manifest_output = thresholds_dir / f"{layout.edge_stem}_threshold_manifest.json"


def resolve_step04_paths(edge_root: str | Path) -> Step04Paths:
    layout = get_edge_layout_from_edge_root(edge_root)
    cwd = Path.cwd().resolve()
    if _looks_like_colab_root(cwd) and (cwd / f"{layout.edge_stem}_oof_predictions.csv").exists():
        return Step04Paths(layout=layout, run_mode="flat_colab", root=cwd, edge_root=layout.edge_root)
    return Step04Paths(layout=layout, run_mode="repo_local", root=layout.v2_root, edge_root=layout.edge_root)


def preflight_check(paths: Step04Paths) -> None:
    if paths.run_mode == "repo_local":
        validate_edge_yaml_file(paths.layout.edge_root)
    missing = []
    for label, path in [
        ("OOF predictions", paths.input_oof_csv),
        ("raw OOF threshold sweep", paths.input_raw_sweep_csv),
    ]:
        if not path.exists():
            missing.append((label, path))
    if missing:
        lines = ["Step 04 preflight failed. Missing required input(s):"]
        for label, path in missing:
            lines.append(f"  - {label}: {path}")
        lines.extend([
            "",
            "Run/copy Step 03 and Step 03B outputs first. Step 03A is retired "
            "(redundant with Task 2A's resolution CSV, which Step 03's own OOF "
            "output already carries forward).",
            f"Expected OOF predictions: {paths.input_oof_csv}",
            f"Expected raw threshold sweep: {paths.input_raw_sweep_csv}",
        ])
        raise CalibrationError("\n".join(lines))
    if CombinatorialPurgedCV is None:
        raise CalibrationError(
            "skfolio.model_selection.CombinatorialPurgedCV is required for V1-style CPCV calibration. "
            f"Original import error: {SKFOLIO_IMPORT_ERROR}"
        )
    paths.calibration_dir.mkdir(parents=True, exist_ok=True)
    paths.thresholds_dir.mkdir(parents=True, exist_ok=True)


def load_oof(paths: Step04Paths) -> pd.DataFrame:
    """Load Step 03's OOF predictions CSV. Under the MLDP executable-target
    architecture, Step 03 already reads Task 2A's resolution CSV and copies
    every column (including entry_price/pt_price/sl_price/exit_price/
    execution_status/quote_executable_r/quote_executable_meta_label/
    mldp_target_eligible) into its OOF output, and already restricts itself
    to mldp_target_eligible==True rows. So this function no longer needs a
    separate Step 03A execution-replay CSV/manifest merge -- Step 03A is
    retired as redundant with Task 2A (see
    docs/mldp/A8_STEP03_ONWARD_MIGRATION.md)."""
    df = pd.read_csv(paths.input_oof_csv)
    if TIME_COL not in df.columns:
        first_col = df.columns[0]
        df[first_col] = to_datetime_iso(df[first_col], utc=True, errors="coerce")
        df = df.rename(columns={first_col: TIME_COL})
    required = [
        TIME_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, TARGET_COL,
        ELIGIBILITY_COL, RAW_PROB_COL, ECONOMIC_R_COL, T1_COL,
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise CalibrationError(f"OOF predictions missing required columns: {missing}")

    df[TIME_COL] = to_datetime_iso(df[TIME_COL], utc=True, errors="coerce")
    df[T1_COL] = to_datetime_iso(df[T1_COL], utc=True, errors="coerce")
    df[GROSS_TARGET_COL] = pd.to_numeric(df[GROSS_TARGET_COL], errors="coerce")
    df[GROSS_REALIZED_R_COL] = pd.to_numeric(df[GROSS_REALIZED_R_COL], errors="coerce")
    df[RAW_PROB_COL] = pd.to_numeric(df[RAW_PROB_COL], errors="coerce")
    df[ECONOMIC_R_COL] = pd.to_numeric(df[ECONOMIC_R_COL], errors="coerce")
    df[ELIGIBILITY_COL] = df[ELIGIBILITY_COL].astype(bool)
    df = df.dropna(subset=[TIME_COL, T1_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, RAW_PROB_COL]).copy()
    if df.empty:
        raise CalibrationError("OOF predictions are empty after required-column filtering.")
    df[GROSS_TARGET_COL] = df[GROSS_TARGET_COL].astype(int)
    validate_gross_label_frame(df, context=f"{paths.layout.edge_id} Step04 gross OOF")
    df[RAW_PROB_WORK_COL] = clip_probability(df[RAW_PROB_COL].astype(float).to_numpy())

    if OPTIONAL_SIDE_COL in df.columns:
        df[OPTIONAL_SIDE_COL] = pd.to_numeric(df[OPTIONAL_SIDE_COL], errors="coerce")
        non_matching = df[df[OPTIONAL_SIDE_COL].notna() & (df[OPTIONAL_SIDE_COL].astype(int) != paths.layout.side_value)]
        if len(non_matching) > 0:
            raise CalibrationError(
                f"OOF file contains {len(non_matching)} rows whose side column does not match edge side '{paths.layout.side_name}'."
            )

    # Step 03 already restricted its OOF output to mldp_target_eligible==True
    # rows (Task 2B's population rule) -- every remaining row here already
    # has a real executed outcome. Fail closed if that invariant somehow
    # doesn't hold rather than silently including a non-executed row.
    if not df[ELIGIBILITY_COL].all():
        ineligible = int((~df[ELIGIBILITY_COL]).sum())
        raise CalibrationError(
            f"OOF predictions contain {ineligible} ineligible (non-executed) row(s); rerun Step 03."
        )
    if df[TARGET_COL].isna().any() or df[ECONOMIC_R_COL].isna().any():
        raise CalibrationError(f"{TARGET_COL}/{ECONOMIC_R_COL} contain NaN among eligible OOF rows.")
    # 2026-10-17 label/economics split (deviation register D016): when Task 2A wrote label_r, the target is the
    # Dukascopy MID-path label (int(label_r > 0)) while ECONOMIC_R_COL is the research firm's net R used for the
    # threshold economics below -- the two legitimately differ, so check the label against its own source.
    label_source_col = "label_r" if "label_r" in df.columns else ECONOMIC_R_COL
    label_source = pd.to_numeric(df[label_source_col], errors="coerce")
    if label_source.isna().any():
        raise CalibrationError(f"{label_source_col} contains NaN among eligible OOF rows.")
    expected_target = (label_source > 0.0).astype(int)
    if not df[TARGET_COL].astype(int).equals(expected_target):
        raise CalibrationError(f"{TARGET_COL} != int({label_source_col} > 0) for at least one row.")
    df[TARGET_COL] = df[TARGET_COL].astype(int)
    if df[TARGET_COL].nunique() < 2:
        raise CalibrationError("Executable outcome labels have fewer than two classes; cannot fit sigmoid calibrator.")

    return df.sort_values(TIME_COL).set_index(TIME_COL)

def flatten_cv_indices(idx: Any) -> np.ndarray:
    if isinstance(idx, np.ndarray):
        return np.unique(idx.astype(int))
    if hasattr(idx, "__iter__"):
        parts = []
        for part in idx:
            arr = np.atleast_1d(part).astype(int)
            parts.append(arr)
        return np.unique(np.concatenate(parts)) if parts else np.array([], dtype=int)
    return np.array([int(idx)])


def _purge_t1(frame: pd.DataFrame) -> pd.Series:
    """Training-label end per OOF row: label_t1 (Task 2A), fallback gross_t1 (AFML Ch.7 walkthrough 2026-10-24)."""
    fallback = to_datetime_iso(frame[T1_COL], utc=True, errors="coerce")
    if "label_t1" in frame.columns:
        fallback = to_datetime_iso(frame["label_t1"], utc=True, errors="coerce").fillna(fallback)
    return pd.Series(fallback.to_numpy(), index=frame.index)


def purge_train_overlaps(train_idx: np.ndarray, test_idx: np.ndarray, index: pd.Index, t1: pd.Series) -> np.ndarray:
    train_idx = np.asarray(train_idx, dtype=int)
    test_idx = np.asarray(test_idx, dtype=int)
    if len(train_idx) == 0 or len(test_idx) == 0:
        return train_idx
    train_starts = index[train_idx].values.astype("datetime64[ns]")
    train_ends = t1.iloc[train_idx].values.astype("datetime64[ns]")
    test_starts = index[test_idx].values.astype("datetime64[ns]")
    test_ends = t1.iloc[test_idx].values.astype("datetime64[ns]")
    keep_mask = np.ones(len(train_idx), dtype=bool)
    for i in range(len(train_idx)):
        overlaps = (train_starts[i] <= test_ends) & (train_ends[i] >= test_starts)
        if overlaps.any():
            keep_mask[i] = False
    return train_idx[keep_mask]


def apply_embargo(train_idx: np.ndarray, test_idx: np.ndarray, n_samples: int, embargo_size: int) -> np.ndarray:
    train_idx = np.asarray(train_idx, dtype=int)
    test_idx = np.sort(np.unique(np.asarray(test_idx, dtype=int)))
    if len(train_idx) == 0 or len(test_idx) == 0 or embargo_size <= 0:
        return train_idx
    keep_mask = np.ones(len(train_idx), dtype=bool)
    block_ends = []
    for i in range(1, len(test_idx)):
        if test_idx[i] != test_idx[i - 1] + 1:
            block_ends.append(test_idx[i - 1])
    block_ends.append(test_idx[-1])
    for block_end in block_ends:
        embargo_start = block_end + 1
        embargo_end = min(embargo_start + embargo_size, n_samples)
        keep_mask &= (train_idx < embargo_start) | (train_idx >= embargo_end)
    return train_idx[keep_mask]


def can_fit_calibrator(y: np.ndarray, min_rows: int = MIN_TRAIN_ROWS_TO_FIT) -> tuple[bool, str]:
    y = np.asarray(y, dtype=int)
    if len(y) < min_rows:
        return False, f"too_few_rows:{len(y)}"
    counts = pd.Series(y).value_counts().to_dict()
    if len(counts) < 2:
        return False, f"one_class:{counts}"
    if min(counts.values()) < MIN_CLASS_COUNT_TO_FIT:
        return False, f"minority_class_too_small:{counts}"
    return True, "ok"


def fit_sigmoid_params(raw_prob: Any, y: Any, sample_weight: Any = None) -> dict[str, Any]:
    """Platt sigmoid on logit(raw). sample_weight: AFML return-attribution weights (D082, owner 2026-11-04: the live
    calibrator is fitted with the same weights the model is trained and Gate D v4 scored with -- AFML s.4.6, Snippet
    7.4, s.9.4); None = unweighted (the per-fold diagnostic calibration)."""
    ok, reason = can_fit_calibrator(np.asarray(y, dtype=int))
    if not ok:
        return {"method": "identity", "fallback_reason": reason, "probability_clip": PROBABILITY_CLIP, "output_column": CAL_PROB_COL}
    x = logit(raw_prob).reshape(-1, 1)
    y_arr = np.asarray(y, dtype=int)
    model = LogisticRegression(solver="lbfgs", C=1.0, max_iter=1000)
    model.fit(x, y_arr, sample_weight=None if sample_weight is None else np.asarray(sample_weight, dtype=float))
    return {
        "method": "sigmoid_platt_logit",
        "coef": float(model.coef_[0, 0]),
        "intercept": float(model.intercept_[0]),
        "probability_clip": PROBABILITY_CLIP,
        "output_column": CAL_PROB_COL,
    }


def apply_sigmoid_params(raw_prob: Any, params: Mapping[str, Any]) -> np.ndarray:
    if params.get("method") == "identity":
        return clip_probability(raw_prob)
    return sigmoid(float(params["intercept"]) + float(params["coef"]) * logit(raw_prob))


def cpcv_calibrate_edge(oof: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = oof.sort_index().copy()
    n = len(work)
    raw = work[RAW_PROB_WORK_COL].to_numpy(dtype=float)
    y = work[TARGET_COL].to_numpy(dtype=int)

    if n < MIN_TOTAL_ROWS_PER_SIDE:
        out = work.copy()
        out[CAL_PROB_COL] = raw
        out["calibration_pred_count_sigmoid"] = 0
        out["calibration_status"] = f"raw_fallback_too_few_rows:{n}"
        return out, pd.DataFrame([{
            "fold": 0,
            "train_size": 0,
            "test_size": n,
            "method": "sigmoid",
            "status": f"raw_fallback_too_few_rows:{n}",
            "fallback_reason": f"too_few_rows:{n}",
        }])

    sig_sum = np.zeros(n, dtype=float)
    sig_count = np.zeros(n, dtype=int)
    fold_rows = []

    cv = CombinatorialPurgedCV(
        n_folds=N_FOLDS,
        n_test_folds=N_TEST_FOLDS,
        purged_size=PURGED_SIZE,
        embargo_size=EMBARGO_SIZE,
    )
    dummy_x = pd.DataFrame({"raw_probability": raw}, index=work.index)

    print("Generating V1-style CPCV second-level sigmoid calibrated probabilities...", flush=True)
    for fold_num, (train_idx, test_idx) in enumerate(cv.split(dummy_x), start=1):
        train_idx = flatten_cv_indices(train_idx)
        test_idx = flatten_cv_indices(test_idx)
        raw_train_size = len(train_idx)
        raw_test_size = len(test_idx)
        train_idx = purge_train_overlaps(train_idx=train_idx, test_idx=test_idx, index=work.index, t1=_purge_t1(work))
        train_idx = apply_embargo(train_idx=train_idx, test_idx=test_idx, n_samples=n, embargo_size=embargo_size_for(n))

        if len(train_idx) == 0 or len(test_idx) == 0:
            fold_rows.append({
                "fold": fold_num,
                "raw_train_size": raw_train_size,
                "raw_test_size": raw_test_size,
                "train_size": int(len(train_idx)),
                "test_size": int(len(test_idx)),
                "method": "sigmoid",
                "status": "skipped_empty_train_or_test",
                "fallback_reason": "",
            })
            print(f"Fold {fold_num:02d}: skipped after purge/embargo", flush=True)
            continue

        params = fit_sigmoid_params(raw[train_idx], y[train_idx])
        pred = apply_sigmoid_params(raw[test_idx], params)
        sig_sum[test_idx] += pred
        sig_count[test_idx] += 1
        fold_rows.append({
            "fold": fold_num,
            "raw_train_size": raw_train_size,
            "raw_test_size": raw_test_size,
            "train_size": int(len(train_idx)),
            "test_size": int(len(test_idx)),
            "method": "sigmoid",
            "status": params.get("method", "unknown"),
            "fallback_reason": params.get("fallback_reason", ""),
            "coef": params.get("coef", np.nan),
            "intercept": params.get("intercept", np.nan),
        })
        print(
            f"Fold {fold_num:02d}: train={len(train_idx):,} test={len(test_idx):,} "
            f"status={params.get('method')} pred_count={int(sig_count[test_idx].sum())}",
            flush=True,
        )

    out = work.copy()
    out[CAL_PROB_COL] = np.where(sig_count > 0, sig_sum / np.maximum(sig_count, 1), raw)
    out[CAL_PROB_COL] = clip_probability(out[CAL_PROB_COL].to_numpy(dtype=float))
    out["calibration_pred_count_sigmoid"] = sig_count
    out["calibration_status"] = np.where(sig_count > 0, "cpcv_calibrated_sigmoid", "raw_fallback_no_cpcv_test_prediction")
    return out, pd.DataFrame(fold_rows)


def expected_calibration_error(y: Any, p: Any, n_bins: int = N_CALIBRATION_BINS) -> float:
    y_arr = np.asarray(y, dtype=float)
    p_arr = clip_probability(p)
    if len(y_arr) == 0:
        return np.nan
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bins[:-1], bins[1:]):
        if hi >= 1.0:
            mask = (p_arr >= lo) & (p_arr <= hi)
        else:
            mask = (p_arr >= lo) & (p_arr < hi)
        if not mask.any():
            continue
        ece += float(mask.mean()) * abs(float(y_arr[mask].mean()) - float(p_arr[mask].mean()))
    return float(ece)


def auc_safe(y: Any, p: Any) -> float:
    try:
        y_arr = np.asarray(y, dtype=int)
        if len(np.unique(y_arr)) < 2:
            return np.nan
        return float(roc_auc_score(y_arr, p))
    except Exception:
        return np.nan


def brier_safe(y: Any, p: Any) -> float:
    try:
        return float(brier_score_loss(np.asarray(y, dtype=int), clip_probability(p)))
    except Exception:
        return np.nan


def log_loss_safe(y: Any, p: Any) -> float:
    try:
        return float(log_loss(np.asarray(y, dtype=int), clip_probability(p), labels=[0, 1]))
    except Exception:
        return np.nan


def probability_metrics(y: Any, p: Any, prefix: str) -> dict[str, Any]:
    y_arr = np.asarray(y, dtype=int)
    p_arr = clip_probability(p)
    return {
        f"{prefix}_rows": int(len(y_arr)),
        f"{prefix}_actual_rate": float(np.mean(y_arr)) if len(y_arr) else np.nan,
        f"{prefix}_prob_mean": float(np.mean(p_arr)) if len(y_arr) else np.nan,
        f"{prefix}_brier": brier_safe(y_arr, p_arr),
        f"{prefix}_log_loss": log_loss_safe(y_arr, p_arr),
        f"{prefix}_ece": expected_calibration_error(y_arr, p_arr),
        f"{prefix}_auc": auc_safe(y_arr, p_arr),
    }


def max_drawdown_from_r(r_values: Any) -> float:
    r = np.asarray(r_values, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return 0.0
    equity = pd.Series(r).cumsum()
    dd = equity - equity.cummax()
    return float(dd.min())


def profit_factor_from_r(r_values: Any) -> float:
    r = np.asarray(r_values, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return np.nan
    gp = float(r[r > 0].sum()) if np.any(r > 0) else 0.0
    gl = float(abs(r[r <= 0].sum())) if np.any(r <= 0) else 0.0
    return safe_div(gp, gl)


def trade_metrics_from_selection(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or ECONOMIC_R_COL not in df.columns:
        return {}
    r = pd.to_numeric(df[ECONOMIC_R_COL], errors="coerce").dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return {}
    wins = r[r > 0]
    losses = r[r <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(abs(losses.sum())) if len(losses) else 0.0
    cumulative_r = float(r.sum())
    max_dd = max_drawdown_from_r(r)
    return {
        "trade_count": int(len(r)),
        "win_rate": float(np.mean(r > 0)),
        "expectancy_r": float(np.mean(r)),
        "profit_factor": safe_div(gp, gl),
        "gross_profit_r": gp,
        "gross_loss_r": gl,
        "cumulative_r": cumulative_r,
        "max_drawdown_r": max_dd,
        "calmar_r": safe_div(cumulative_r, abs(max_dd)) if max_dd < 0 else np.nan,
    }


def _round_threshold(value: float) -> float:
    return round(float(value), THRESHOLD_DECIMALS)


def _floor_to_threshold_step(value: float) -> float:
    return _round_threshold(math.floor(float(value) / THRESHOLD_STEP) * THRESHOLD_STEP)


def _ceil_to_threshold_step(value: float) -> float:
    return _round_threshold(math.ceil(float(value) / THRESHOLD_STEP) * THRESHOLD_STEP)


def build_dynamic_calibrated_threshold_grid(valid: pd.DataFrame) -> tuple[list[float], dict[str, Any]]:
    """Build an edge-specific calibrated threshold grid from observed calibrated probabilities.

    This replaces the previous fixed 0.50->0.95 sweep floor. The deployable
    probability space remains calibrated_probability; only the search range is
    adapted to the actual OOF calibrated probability distribution so allocator
    candidate profiles are not artificially restricted.
    """

    policy = {
        "threshold_sweep_policy": "dynamic_observed_calibrated_probability_range",
        "probability_column": CAL_PROB_COL,
        "threshold_step": THRESHOLD_STEP,
        "threshold_min_bound": THRESHOLD_MIN_BOUND,
        "threshold_max_bound": THRESHOLD_MAX_BOUND,
        "threshold_decimals": THRESHOLD_DECIMALS,
        "valid_probability_rows": 0,
        "calibrated_probability_min": np.nan,
        "calibrated_probability_max": np.nan,
        "threshold_start": None,
        "threshold_end": None,
        "threshold_count": 0,
    }

    if valid.empty or CAL_PROB_COL not in valid.columns:
        return [], policy

    probs = pd.to_numeric(valid[CAL_PROB_COL], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    probs = probs[(probs >= 0.0) & (probs <= 1.0)]
    if probs.empty:
        return [], policy

    prob_min = float(probs.min())
    prob_max = float(probs.max())
    start = max(THRESHOLD_MIN_BOUND, _floor_to_threshold_step(prob_min))
    end = min(THRESHOLD_MAX_BOUND, _ceil_to_threshold_step(prob_max))
    start = _round_threshold(start)
    end = _round_threshold(end)

    if start > end:
        start = end

    steps = int(round((end - start) / THRESHOLD_STEP))
    thresholds = [_round_threshold(start + i * THRESHOLD_STEP) for i in range(steps + 1)]
    thresholds = sorted(set(thr for thr in thresholds if THRESHOLD_MIN_BOUND <= thr <= THRESHOLD_MAX_BOUND))

    policy.update({
        "valid_probability_rows": int(len(probs)),
        "calibrated_probability_min": prob_min,
        "calibrated_probability_max": prob_max,
        "threshold_start": float(thresholds[0]) if thresholds else None,
        "threshold_end": float(thresholds[-1]) if thresholds else None,
        "threshold_count": int(len(thresholds)),
    })
    return thresholds, policy


def _payoff_profile_columns(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "payoff_profile_version": profile.get("payoff_profile_version"),
        "payoff_profile_valid": bool(profile.get("payoff_profile_valid", False)),
        "payoff_profile_invalid_reason": profile.get("payoff_profile_invalid_reason", ""),
        "payoff_trade_count": profile.get("payoff_trade_count"),
        "payoff_win_count": profile.get("payoff_win_count"),
        "payoff_loss_count": profile.get("payoff_loss_count"),
        "payoff_zero_count": profile.get("payoff_zero_count"),
        "payoff_zero_rate": profile.get("payoff_zero_rate"),
        "min_wins_required": profile.get("min_wins_required"),
        "min_losses_required": profile.get("min_losses_required"),
        "empirical_avg_win_r": profile.get("empirical_avg_win_r"),
        "empirical_avg_loss_r": profile.get("empirical_avg_loss_r"),
        "empirical_reward_risk_ratio": profile.get("empirical_reward_risk_ratio"),
        "empirical_break_even_probability": profile.get("empirical_break_even_probability"),
    }


def _profile_from_sweep_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = {key: row.get(key) for key in [
        "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count", "payoff_zero_rate",
        "min_wins_required", "min_losses_required", "empirical_avg_win_r", "empirical_avg_loss_r",
        "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]}
    return validate_payoff_profile(profile)


def build_threshold_sweep(df: pd.DataFrame) -> pd.DataFrame:
    sweep_columns = [
        "threshold", "calibration_method", "probability_column", "take_rate", "precision",
        "avg_pred_prob", "afml_sizing_method", "avg_afml_bet_size", "positive_afml_trade_count",
        "positive_afml_trade_rate", "avg_positive_afml_bet_size", "trade_count", "win_rate",
        "expectancy_r", "profit_factor", "gross_profit_r", "gross_loss_r", "cumulative_r",
        "max_drawdown_r", "calmar_r", "payoff_profile_version", "payoff_profile_valid",
        "payoff_profile_invalid_reason", "payoff_trade_count", "payoff_win_count", "payoff_loss_count",
        "payoff_zero_count", "payoff_zero_rate", "min_wins_required", "min_losses_required", "empirical_avg_win_r",
        "empirical_avg_loss_r", "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]
    rows = []
    valid = df.dropna(subset=[CAL_PROB_COL, ECONOMIC_R_COL, TARGET_COL]).copy()
    thresholds, threshold_policy = build_dynamic_calibrated_threshold_grid(valid)
    if valid.empty or not thresholds:
        out = pd.DataFrame(columns=sweep_columns)
        out.attrs["threshold_sweep_policy"] = threshold_policy
        return out
    for thr in thresholds:
        sel = valid[valid[CAL_PROB_COL] >= thr].copy()
        if sel.empty:
            continue
        metrics = trade_metrics_from_selection(sel)
        if not metrics:
            continue
        profile = empirical_payoff_profile(sel[ECONOMIC_R_COL].to_numpy(dtype=float))
        afml = np.full(len(sel), np.nan, dtype=float)
        if profile.get("payoff_profile_valid") is True:
            components = payoff_profile_afml_components(sel[CAL_PROB_COL].to_numpy(dtype=float), profile)
            afml = components[AFML_BET_COL]
        positive = np.isfinite(afml) & (afml > 0.0)
        row = {
            "threshold": float(thr),
            "calibration_method": "sigmoid",
            "probability_column": CAL_PROB_COL,
            "take_rate": float(len(sel) / len(valid)),
            "precision": float(sel[TARGET_COL].mean()),
            "avg_pred_prob": float(sel[CAL_PROB_COL].mean()),
            "afml_sizing_method": AFML_SIZING_METHOD,
            "avg_afml_bet_size": float(np.nanmean(afml)) if np.isfinite(afml).any() else np.nan,
            "positive_afml_trade_count": int(positive.sum()),
            "positive_afml_trade_rate": float(positive.mean()),
            "avg_positive_afml_bet_size": float(np.nanmean(afml[positive])) if positive.any() else 0.0,
            **metrics,
            **_payoff_profile_columns(profile),
        }
        rows.append(row)
    out = pd.DataFrame(rows, columns=sweep_columns).sort_values("threshold").reset_index(drop=True) if rows else pd.DataFrame(columns=sweep_columns)
    out.attrs["threshold_sweep_policy"] = threshold_policy
    return out

def build_bucket_report(df: pd.DataFrame, prob_col: str, method: str, edge_id: str) -> list[dict[str, Any]]:
    rows = []
    valid = df.dropna(subset=[prob_col, TARGET_COL]).copy()
    if valid.empty:
        return rows
    p = valid[prob_col].to_numpy(dtype=float)
    bins = np.linspace(0.0, 1.0, N_CALIBRATION_BINS + 1)
    for i, (lo, hi) in enumerate(zip(bins[:-1], bins[1:]), start=1):
        if hi >= 1.0:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        g = valid.loc[mask]
        if g.empty:
            continue
        r = pd.to_numeric(g[ECONOMIC_R_COL], errors="coerce").dropna().to_numpy(dtype=float)
        rows.append({
            "edge_id": edge_id,
            "calibration_method": method,
            "probability_column": prob_col,
            "bin_id": i,
            "bin_low": float(lo),
            "bin_high": float(hi),
            "rows": int(len(g)),
            "prob_mean": float(g[prob_col].mean()),
            "actual_rate": float(g[TARGET_COL].mean()),
            "calibration_error": float(abs(g[TARGET_COL].mean() - g[prob_col].mean())),
            "expectancy_r": float(np.mean(r)) if len(r) else np.nan,
            "win_rate_r_positive": float(np.mean(r > 0)) if len(r) else np.nan,
            "profit_factor_r": profit_factor_from_r(r),
            "cumulative_r": float(np.sum(r)) if len(r) else 0.0,
        })
    return rows


def choose_selected_threshold(sweep: pd.DataFrame) -> dict[str, Any]:
    if sweep.empty:
        return {
            "selection_status": "no_calibrated_threshold_rows",
            "reason": "No valid calibrated threshold rows were produced from the dynamic calibrated_probability sweep.",
            "selected_threshold": None,
        }
    eligible = sweep[(sweep["trade_count"] >= MIN_TRADES_FOR_SELECTION) & (sweep["payoff_profile_valid"] == True)].copy()
    if eligible.empty:
        eligible = sweep[sweep["payoff_profile_valid"] == True].copy()
    if eligible.empty:
        return {"selection_status": "no_valid_empirical_payoff_profile", "reason": "No threshold has enough exact executable wins and losses for a valid empirical payoff profile.", "selected_threshold": None}
    row = eligible.sort_values(
        ["expectancy_r", "profit_factor", "trade_count"],
        ascending=[False, False, False],
    ).iloc[0]

    expectancy = float(row["expectancy_r"])
    pf = float(row["profit_factor"])
    cumulative = float(row["cumulative_r"])
    if not (
        expectancy > 0.0
        and cumulative > 0.0
        and (np.isinf(pf) or pf > 1.0)
    ):
        return {
            "selection_status": "no_positive_executable_threshold_region",
            "reason": (
                "The best calibrated threshold is not positive under exact "
                "Step 03A Bid/Ask executed_r economics."
            ),
            "selected_threshold": None,
            "best_executable_expectancy_r": expectancy,
            "best_executable_profit_factor": pf,
            "best_executable_cumulative_r": cumulative,
        }

    out = row.to_dict()
    out["selection_status"] = "selected_from_calibrated_probability_exact_executable_sweep"
    out["selected_threshold"] = float(row["threshold"])
    out["threshold_role"] = (
        "edge-default diagnostic/admissibility threshold; allocator 04C owns "
        "final representative/live threshold selection"
    )
    return out

def afml_return_weights(oof: pd.DataFrame, layout: EdgeLayout) -> np.ndarray:
    """AFML Ch.4 return-attribution weight (Snippet 4.10, class balance excluded) per OOF row, mean 1 (D082). Built
    by the shared engine Steps 01-03 use, from Task 2A's resolution CSV on the standard_quote_tick bar clock."""
    from shared.models.afml_sample_weights import build_afml_event_weight_table
    bars = layout.v2_root / "assets" / layout.asset / "bars" / "standard_quote_tick" / f"{layout.asset.lower()}_standard_quote_tick_bars.csv"
    if not layout.resolution_csv.exists() or not bars.exists():
        raise CalibrationError(f"{layout.edge_id}: D082 weighted live calibrator needs {layout.resolution_csv} and {bars}")
    table = build_afml_event_weight_table(layout.resolution_csv, bars, time_col=TIME_COL, t1_col=T1_COL,
                                          label_t1_col="label_t1", close_col="Close")
    idx = pd.DatetimeIndex(to_datetime_iso(oof.index, utc=True))
    w = table["return_attribution_weight"].reindex(idx).to_numpy(dtype="float64")
    if not np.isfinite(w).all() or (w <= 0).any():
        raise CalibrationError(f"{layout.edge_id}: {int((~np.isfinite(w)).sum())} OOF rows have no valid AFML weight")
    return w / w.mean()


def fit_final_live_sigmoid(oof: pd.DataFrame, layout: EdgeLayout, paths: Step04Paths) -> dict[str, Any]:
    # AFML Ch.12 walkthrough (owner decision D052, 2026-10-26): the live calibrator is fitted on ALL CPCV backtest
    # paths stacked (every path is a full out-of-sample forecast of every row, AFML s.12.4.2), not on their average.
    # Fail closed if Step 03 did not write the paths (an old Step 03 output must be regenerated).
    if not path_columns(oof.columns):
        raise CalibrationError(
            f"{layout.edge_id}: Step 03 output has no cpcv_path_XX_raw_probability columns. D052 requires the CPCV "
            "paths; re-run Step 03 (docs/tools/patch_afml_ch12_step03_cpcv_paths.py) before Step 04."
        )
    raw_paths, y, path_info = stacked_path_fit_inputs(oof, TARGET_COL)
    raw = clip_probability(raw_paths)
    # D082 (owner 2026-11-04): fitted with the AFML Ch.4 return-attribution weights (Snippet 4.10), the weights the model
    # is trained with and Gate D v4 D2 scores with (AFML s.4.6 pp.68-69, Snippet 7.4, s.9.4 pp.134-135).
    event_w = afml_return_weights(oof, layout)
    params = fit_sigmoid_params(raw, y, sample_weight=stacked_path_fit_weights(oof, TARGET_COL, event_w))
    params["sample_weight"] = "AFML Ch.4 return attribution (Snippet 4.10), mean 1 over the OOF rows (D082)"
    params["calibration_input"] = "cpcv_paths_stacked (AFML s.12.4.2 step 5, D052)"
    params["cpcv_paths"] = {k: v for k, v in path_info.items() if k != "path_columns"}
    params.update({
        "artifact_type": "probability_calibrator",
        "calibration_method": "sigmoid",
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "side_label": layout.side_name,
        "side_value": layout.side_value,
        "rows_fit": int(len(oof)),
        "rows_fit_stacked_paths": int(len(y)),
        "positive_rate_fit": float(np.mean(y)) if len(y) else np.nan,
        "start_time_fit": oof.index.min().isoformat(),
        "end_time_fit": oof.index.max().isoformat(),
        "input_column": RAW_PROB_COL,
        "output_column": CAL_PROB_COL,
        "transform": "calibrated_prob = sigmoid(intercept + coef * logit(raw_probability))",
        "target_column": TARGET_COL,
        "target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "target_definition": f"{TARGET_COL} = int({ECONOMIC_R_COL} > 0)",
        "calibration_purge_end_column": T1_COL,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_definition": GROSS_TARGET_DEFINITION,
        "afml_payoff_embedded_in_calibrator": False,
    })
    save_json(paths.sigmoid_calibrator_json, params)
    joblib.dump(params, paths.sigmoid_calibrator_pkl)
    return params

def run_step04(edge_root: str | Path) -> dict[str, Any]:
    paths = resolve_step04_paths(edge_root)
    layout = paths.layout
    print("=" * 88, flush=True)
    print(f"V3 STEP 04: EXECUTABLE-OUTCOME SIGMOID CALIBRATION + EMPIRICAL PAYOFF AFML — {layout.edge_id}", flush=True)
    print("=" * 88, flush=True)
    preflight_check(paths)
    geometry = load_payoff_geometry(layout)
    upstream_generation = validate_step03_generation(layout)
    static_rr = float(geometry["reward_risk_ratio"])
    static_pbe = float(break_even_probability_from_reward_risk(static_rr))
    print(f"Geometry diagnostic only: R:R={static_rr:.6g} static_pBE={static_pbe:.6f}", flush=True)

    oof = load_oof(paths)
    print(f"Rows loaded: {len(oof):,}", flush=True)
    print(f"Executable positive rate: {oof[TARGET_COL].mean():.6f}", flush=True)
    print(f"Gross positive rate: {oof[GROSS_TARGET_COL].mean():.6f}", flush=True)
    print(f"Calibration purge horizon: exact {T1_COL}", flush=True)

    calibrated, fold_report = cpcv_calibrate_edge(oof)
    calibrated[CAL_PROB_COL] = clip_probability(calibrated[CAL_PROB_COL].to_numpy(dtype=float))

    y = calibrated[TARGET_COL].to_numpy(dtype=int)
    raw = calibrated[RAW_PROB_WORK_COL].to_numpy(dtype=float)
    cal = calibrated[CAL_PROB_COL].to_numpy(dtype=float)

    bucket_rows = []
    bucket_rows.extend(build_bucket_report(calibrated, RAW_PROB_WORK_COL, "raw", layout.edge_id))
    bucket_rows.extend(build_bucket_report(calibrated, CAL_PROB_COL, "sigmoid", layout.edge_id))

    sweep = attach_payoff_context_columns(
        build_threshold_sweep(calibrated),
        edge_id=layout.edge_id,
        geometry_hash=str(geometry["geometry_hash"]),
    )
    threshold_sweep_policy = sweep.attrs.get("threshold_sweep_policy", {})
    selected = choose_selected_threshold(sweep)
    selected_profile = None
    if selected.get("selected_threshold") is not None:
        selected_profile = _profile_from_sweep_mapping(selected)
        selected["payoff_profile"] = selected_profile
        components = payoff_profile_afml_components(calibrated[CAL_PROB_COL].to_numpy(dtype=float), selected_profile)
        calibrated[REWARD_RISK_RATIO_COL] = components[REWARD_RISK_RATIO_COL]
        calibrated[BREAK_EVEN_PROBABILITY_COL] = components[BREAK_EVEN_PROBABILITY_COL]
        calibrated[AFML_ECONOMIC_STRENGTH_COL] = components[AFML_ECONOMIC_STRENGTH_COL]
        calibrated[AFML_EFFECTIVE_PROBABILITY_COL] = components[AFML_EFFECTIVE_PROBABILITY_COL]
        calibrated[AFML_BET_COL] = components[AFML_BET_COL]
        calibrated[AFML_SIZING_METHOD_COL] = AFML_SIZING_METHOD
        sweep["selected_threshold"] = sweep["threshold"] == float(selected["selected_threshold"])
    else:
        calibrated[REWARD_RISK_RATIO_COL] = np.nan
        calibrated[BREAK_EVEN_PROBABILITY_COL] = np.nan
        calibrated[AFML_ECONOMIC_STRENGTH_COL] = 0.0
        calibrated[AFML_EFFECTIVE_PROBABILITY_COL] = 0.5
        calibrated[AFML_BET_COL] = 0.0
        calibrated[AFML_SIZING_METHOD_COL] = AFML_SIZING_METHOD
        sweep["selected_threshold"] = False

    live_sigmoid_params = fit_final_live_sigmoid(oof, layout, paths)

    # Write the actual live-consistent calibrated probability alongside the
    # CPCV research diagnostic, so it exists as an inspectable artifact
    # instead of only ever being recomputed transiently downstream (see
    # LIVE_CAL_PROB_COL definition above for why this is not the same value
    # as CAL_PROB_COL).
    calibrated[LIVE_CAL_PROB_COL] = apply_sigmoid_params(raw, live_sigmoid_params)

    summary = {
        "edge_id": layout.edge_id,
        "rows": int(len(calibrated)),
        "executed_positive_rate": float(np.mean(y)) if len(y) else np.nan,
        "gross_positive_rate": float(calibrated[GROSS_TARGET_COL].mean()),
        "gross_to_executed_label_flip_count": int((calibrated[GROSS_TARGET_COL].astype(int) != calibrated[TARGET_COL].astype(int)).sum()),
        "raw_prob_mean": float(np.mean(raw)) if len(raw) else np.nan,
        "sigmoid_prob_mean": float(np.mean(cal)) if len(cal) else np.nan,
        "static_geometry_reward_risk_ratio_diagnostic": static_rr,
        "static_geometry_break_even_probability_diagnostic": static_pbe,
        "afml_sizing_method": AFML_SIZING_METHOD,
        "selected_payoff_profile": selected_profile,
        "sigmoid_afml_size_mean": float(calibrated[AFML_BET_COL].mean()),
        **probability_metrics(y, raw, "raw"),
        **probability_metrics(y, cal, "sigmoid_cpcv"),
        **probability_metrics(y, calibrated[LIVE_CAL_PROB_COL].to_numpy(dtype=float), "sigmoid_live"),
    }
    summary["sigmoid_brier_improvement_vs_raw"] = summary["raw_brier"] - summary["sigmoid_cpcv_brier"]
    summary["sigmoid_ece_improvement_vs_raw"] = summary["raw_ece"] - summary["sigmoid_cpcv_ece"]
    summary["recommended_calibration_method"] = "sigmoid"
    summary["live_calibrated_probability_column"] = LIVE_CAL_PROB_COL
    summary["live_vs_cpcv_note"] = (
        "sigmoid_live_* metrics reflect LIVE_CAL_PROB_COL (raw_probability through the single "
        "live calibrator) -- this is what live scoring and the portfolio_allocator threshold "
        "grid actually use. sigmoid_cpcv_* metrics reflect CAL_PROB_COL, the CPCV-fold-averaged "
        "research diagnostic, retained for calibration-quality bucket/ECE checks only."
    )

    calibrated.reset_index().to_csv(paths.calibrated_oof_output, index=False)
    fold_report.to_csv(paths.calibration_fold_report_output, index=False)
    pd.DataFrame(bucket_rows).to_csv(paths.calibration_bins_output, index=False)
    sweep.to_csv(paths.threshold_sweep_output, index=False)

    oof_sha = hashlib.sha256(paths.input_oof_csv.read_bytes()).hexdigest()
    selected_payload = {
        "edge_id": layout.edge_id,
        "calibration_method": "sigmoid",
        "calibration_target_column": TARGET_COL,
        "calibration_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "threshold_probability_column": CAL_PROB_COL,
        "threshold_sweep_policy": threshold_sweep_policy,
        "afml_sizing": {
            "method": AFML_SIZING_METHOD,
            "payoff_profile_version": PAYOFF_PROFILE_VERSION,
            "static_geometry_break_even_used_for_sizing": False,
            "geometry_hash": str(geometry["geometry_hash"]),
        },
        "payoff_profile": selected_profile,
        "payoff_profile_provenance": {
            "oof_predictions_sha256": oof_sha,
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "geometry_hash": str(geometry["geometry_hash"]),
            "threshold": selected.get("selected_threshold"),
            "probability_column": CAL_PROB_COL,
        },
        "selected": selected,
        "deployable": False,
        "note": "Edge-default threshold/profile only. Allocator 04C owns final representative threshold; allocator 04B recomputes threshold-specific AFML from each sweep row payoff profile.",
    }
    if selected_profile is not None:
        payoff_context = context_from_mapping(selected)
        selected_payload.update({
            "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "payoff_context_version": payoff_context["payoff_context_version"],
            "payoff_context_id": payoff_context["payoff_context_id"],
            "payoff_context": payoff_context,
            "upstream_generation": upstream_generation,
        })
    else:
        selected_payload.update({
            "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "payoff_context_version": None,
            "payoff_context_id": None,
            "payoff_context": None,
            "upstream_generation": upstream_generation,
        })
    save_json(paths.selected_threshold_output, selected_payload)

    report = {
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "run_mode": paths.run_mode,
        "script": display_path(_THIS_FILE, paths.root),
        "blueprint_stage": "v3_executable_outcome_sigmoid_and_empirical_exact_payoff_afml",
        "probability_flow": PROBABILITY_FLOW,
        "calibration_target_column": TARGET_COL,
        "calibration_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "calibration_purge_end_column": T1_COL,
        "economic_return_column": ECONOMIC_R_COL,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "rows_loaded": int(len(oof)),
        "rows_cpcv_calibrated": int(len(calibrated)),
        "summary": summary,
        "trade_geometry": geometry,
        "static_geometry_payoff_role": "diagnostic_and_execution_geometry_only_not_afml_break_even",
        "afml_sizing": selected_payload["afml_sizing"],
        "selected_payoff_profile": selected_profile,
        "threshold_sweep_policy": threshold_sweep_policy,
        "live_sigmoid_calibrator": live_sigmoid_params,
        "selected_threshold": selected_payload,
        "cpcv": {
            "n_folds": N_FOLDS, "n_test_folds": N_TEST_FOLDS, "purged_size": PURGED_SIZE,
            "embargo_size": EMBARGO_SIZE, "manual_embargo_size": MANUAL_EMBARGO_SIZE,
            "embargo_rule": EMBARGO_RULE, "purge_t1": PURGE_T1_RULE,
            "mode": "manual_t1_overlap_purge_after_cpcv_split_generation",
        },
        "outputs": {
            "sigmoid_calibrator_json": display_path(paths.sigmoid_calibrator_json, paths.root),
            "sigmoid_calibrator_pkl": display_path(paths.sigmoid_calibrator_pkl, paths.root),
            "calibrated_oof_predictions": display_path(paths.calibrated_oof_output, paths.root),
            "calibration_report": display_path(paths.calibration_report_output, paths.root),
            "calibration_bins": display_path(paths.calibration_bins_output, paths.root),
            "calibration_fold_report": display_path(paths.calibration_fold_report_output, paths.root),
            "threshold_sweep": display_path(paths.threshold_sweep_output, paths.root),
            "selected_threshold": display_path(paths.selected_threshold_output, paths.root),
            "threshold_manifest": display_path(paths.threshold_manifest_output, paths.root),
        },
        "deployable": False,
        "deployability_blockers": [
            "runtime scoring readiness not yet verified after this calibration run",
            "probability/calibration/AFML replay audit not yet passed after this calibration run",
            "edge_manifest not yet exported from current artifacts",
        ],
        "next_ordered_step": "05_training_artifact_verifier.py",
        "notes": [
            f"Sigmoid calibration target is exact executable profitability: {TARGET_COL}=int({ECONOMIC_R_COL}>0).",
            "Step 03A is retired; Step 03's OOF output already carries Task 2A's executable-target columns.",
            f"CPCV calibration overlap purge uses {T1_COL}, consistent with Step 01/02/03's purge/embargo.",
            "Every threshold sweep row freezes its own empirical exact-executable mean win/loss, RR and break-even probability.",
            "AFML is centered on empirical executable break-even. Static PT:SL break-even is diagnostic only.",
            "Allocator 04B must recompute AFML from the payoff profile belonging to each candidate threshold.",
        ],
    }
    report.update({
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_definition": GROSS_TARGET_DEFINITION,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "payoff_context_version": selected_payload.get("payoff_context_version"),
        "payoff_context_id": selected_payload.get("payoff_context_id"),
        "upstream_generation": upstream_generation,
    })
    save_json(paths.calibration_report_output, report)
    save_json(paths.calibration_manifest_output, report)
    save_json(paths.threshold_manifest_output, report)

    print("\nPASS: V3 executable-outcome sigmoid calibration + empirical payoff AFML completed.", flush=True)
    print(f"Raw Brier: {summary['raw_brier']:.6f} | Sigmoid CPCV Brier: {summary['sigmoid_cpcv_brier']:.6f}", flush=True)
    print("Selected calibrated threshold payload:", flush=True)
    print(json.dumps(selected_payload, indent=2, default=json_default), flush=True)
    print("Next ordered step: 05_training_artifact_verifier.py", flush=True)
    return report

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic SOTD Step 04 sigmoid calibration + calibrated threshold sweep.")
    parser.add_argument("--edge-root", required=True, help="Path to assets/<ASSET>/edges/<family>_<side>.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    run_step04(args.edge_root)


if __name__ == "__main__":
    try:
        main()
    except CalibrationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
