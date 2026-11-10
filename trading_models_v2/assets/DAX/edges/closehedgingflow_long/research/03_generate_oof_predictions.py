"""
03_generate_oof_predictions.py

V2 Step 03: OOF raw-probability generation for the current edge package.

Blueprint boundary
------------------
This script generates out-of-fold raw_probability predictions using the selected
features from Step 01 and the best XGBoost parameters from Step 02.

It does NOT fit probability calibrators and does NOT select the deployable live
threshold. Calibration and calibrated-threshold selection belong to Step 04.

Recommended run mode
--------------------
Run this locally from VSCode/PowerShell after copying Step 01 and Step 02
artifacts back into the repo:

    python ./assets/<ASSET>/edges/<FAMILY>_<SIDE>/research/03_generate_oof_predictions.py

Expected local inputs:

    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/research_dataset/<edge_stem>_mldp_execution_resolution.csv
    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/feature_surgery/<edge_stem>_selected_features.json
    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/model/<edge_stem>_meta_model_best_params.json
    assets/<ASSET>/edges/<FAMILY>_<SIDE>/contracts/feature_schema.json
    assets/<ASSET>/bars/standard_quote_tick/<asset>_standard_quote_tick_bars.csv

Expected local outputs:

    assets/<ASSET>/edges/<FAMILY>_<SIDE>/artifacts/oof/

Flat Colab fallback
-------------------
Flat Colab mode is still supported, but Step 03 is intended to run locally.
Flat mode activates only when all flat input files are present in /content.
In flat mode also upload <asset>_standard_quote_tick_bars.csv.

MLDP target migration (Task 2C)
--------------------------------
Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md and
docs/mldp/TASK2B_POPULATION_RULE_DECISION.md, this script now reads Task 2A's
execution-resolution CSV directly and generates OOF raw probabilities against
quote_executable_meta_label, restricted to mldp_target_eligible==True rows.
Because the resolution CSV already carries Task 2A's exact-execution columns
(entry_price/pt_price/sl_price/exit_price/execution_status/quote_executable_r,
etc.) for every row, this script's OOF output carries them forward too --
Step 03A (03a_exact_oof_executable_economics.py), which used to re-derive an
independent bid/ask execution replay, is RETIRED as redundant with Task 2A.
The pipeline now goes 03 -> 03b directly. See
docs/mldp/A8_STEP03_ONWARD_MIGRATION.md.

V1 logic preserved from Step 7
------------------------------
- CPCV split generation
- manual t1 overlap purge
- manual embargo
- AFML Ch.4 sample weight = fixed class balancing * normalized absolute concurrency-adjusted log-return attribution
- average uniqueness is computed separately on the standard_quote_tick bar clock
- train-fold-only F1 threshold diagnostics
- OOF raw probability aggregation
- global raw-probability diagnostics
- calibration bins for raw probability diagnostics only
- by-barrier / by-year / by-regime reports where columns exist

V2 changes
----------
- uses exported V2 side-specialized model rows only
- uses selected features from Step 01
- uses best params from Step 02
- verifies selected features against feature_schema.json
- verifies side/barrier contract inferred from edge folder or flat filenames
- output probability column is raw_probability
- final live threshold is deferred to Step 04 after sigmoid calibration
"""

from __future__ import annotations

# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1

# QUANTARA_AFML_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import hashlib
import json
import sys
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
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.tree import DecisionTreeClassifier
from sklearn.utils.class_weight import compute_sample_weight

try:
    from skfolio.model_selection import CombinatorialPurgedCV
except Exception as exc:  # pragma: no cover
    CombinatorialPurgedCV = None
    SKFOLIO_IMPORT_ERROR = exc
else:
    SKFOLIO_IMPORT_ERROR = None

warnings.filterwarnings("ignore")

# Windows terminals frequently default stdout to a legacy codepage (e.g.
# cp1252) that cannot encode the checkmark/cross-mark characters used in
# this script's status prints -- reconfigure to UTF-8 so a successful run's
# own final banner print cannot itself crash the process after all real
# work (manifest/CSV writes) is already done.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
        "edge_stem": edge_stem,
    }


def _single_file(pattern: str, description: str) -> Path | None:
    matches = sorted(CWD.glob(pattern))
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise ValueError(f"Flat mode found multiple {description} files for pattern {pattern!r}: {matches}")
    return matches[0]


RESOLUTION_SUFFIX = "_mldp_execution_resolution.csv"


def _resolve_paths() -> tuple[str, Path, Path, Path, Path, Path, Path, dict[str, Any]]:
    flat_resolution = _single_file(f"*{RESOLUTION_SUFFIX}", "MLDP executable-target resolution")
    flat_selected_features = _single_file("*_selected_features.json", "selected-feature")
    flat_best_params = _single_file("*_meta_model_best_params.json", "best-params")
    flat_feature_schema = CWD / "feature_schema.json"
    if (
        _looks_like_colab_root(CWD)
        and flat_resolution is not None
        and flat_selected_features is not None
        and flat_best_params is not None
        and flat_feature_schema.exists()
    ):
        rows_stem = flat_resolution.name.removesuffix(RESOLUTION_SUFFIX)
        features_stem = flat_selected_features.name.removesuffix("_selected_features.json")
        params_stem = flat_best_params.name.removesuffix("_meta_model_best_params.json")
        if len({rows_stem, features_stem, params_stem}) != 1:
            raise ValueError(
                "Flat mode edge stem mismatch: "
                f"resolution={rows_stem!r}, selected features={features_stem!r}, best params={params_stem!r}"
            )
        context = _context_from_edge_stem(rows_stem)
        return (
            "flat_colab",
            CWD,
            CWD,
            flat_resolution,
            flat_selected_features,
            flat_best_params,
            flat_feature_schema,
            context,
        )

    edge_root = THIS_FILE.parents[1]
    v2_root = THIS_FILE.parents[5]
    context = _context_from_edge_root(edge_root)
    edge_stem = context["edge_stem"]
    resolution_path = edge_root / "artifacts" / "research_dataset" / f"{edge_stem}{RESOLUTION_SUFFIX}"
    selected_features_path = edge_root / "artifacts" / "feature_surgery" / f"{edge_stem}_selected_features.json"
    best_params_path = edge_root / "artifacts" / "model" / f"{edge_stem}_meta_model_best_params.json"
    feature_schema_path = edge_root / "contracts" / "feature_schema.json"
    return "repo_local", v2_root, edge_root, resolution_path, selected_features_path, best_params_path, feature_schema_path, context


RUN_MODE, V2_ROOT, EDGE_ROOT, RESOLUTION_CSV_PATH, SELECTED_FEATURES_PATH, BEST_PARAMS_PATH, FEATURE_SCHEMA_PATH, EDGE_CONTEXT = _resolve_paths()
ASSET = EDGE_CONTEXT["asset"]
FAMILY = EDGE_CONTEXT["family"]
SIDE = EDGE_CONTEXT["side"]
SIDE_VALUE = EDGE_CONTEXT["side_value"]
EDGE_ID = EDGE_CONTEXT["edge_id"]
EDGE_STEM = EDGE_CONTEXT["edge_stem"]

OUTPUT_DIR = CWD if RUN_MODE == "flat_colab" else EDGE_ROOT / "artifacts" / "oof"

OOF_PREDICTIONS_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_predictions.csv"
OOF_FOLD_REPORT_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_fold_report.csv"
OOF_FEATURE_IMPORTANCE_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_feature_importance.csv"
OOF_GLOBAL_METRICS_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_global_metrics.json"
OOF_RAW_CALIBRATION_BINS_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_raw_probability_calibration_bins.csv"
OOF_BY_YEAR_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_by_year.csv"
OOF_BY_REGIME_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_by_regime.csv"
OOF_BY_BARRIER_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_oof_by_barrier.csv"
MANIFEST_OUTPUT = OUTPUT_DIR / "oof_predictions_manifest.json"

# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md, the frozen
# training/OOF-evaluation target is quote_executable_meta_label, restricted
# below to mldp_target_eligible==True rows (Task 2B's population rule). The
# old gross label (meta_label/realized_r, now duplicated under canonical
# names gross_meta_label/gross_r/gross_t1 in Task 2A's resolution CSV) is
# retained ONLY for an internal data-integrity check -- it is never used as
# the training/OOF target.
TARGET_COL = "quote_executable_meta_label"
ELIGIBILITY_COL = "mldp_target_eligible"
GROSS_TARGET_COL = "gross_meta_label"
GROSS_REALIZED_R_COL = "gross_r"
GROSS_LABEL_CONTRACT_VERSION = "quantara_v3_gross_economic_meta_label_v1"
GROSS_TARGET_DEFINITION = "int(gross_r > 0)"
STEP02_TRAINING_MANIFEST_PATH = (
    CWD / "model_training_manifest.json"
    if RUN_MODE == "flat_colab"
    else EDGE_ROOT / "artifacts" / "model" / "model_training_manifest.json"
)
STEP01_FEATURE_SURGERY_MANIFEST_PATH = (
    CWD / "feature_surgery_manifest.json"
    if RUN_MODE == "flat_colab"
    else EDGE_ROOT / "artifacts" / "feature_surgery" / "feature_surgery_manifest.json"
)
# Deliberate simplification (see docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md):
# gross_t1 is used consistently for both AFML sample-weight computation (full
# population) and purge/embargo (eligible subset) rather than a two-t1 scheme.
T1_COL = "gross_t1"
# AFML Ch.4 walkthrough 2026-10-23: sample weights + sequential bootstrap end each event at the training label's own
# t1 (Task 2A label_t1), snapped to the bar clock; purge/embargo keep T1_COL until the Ch.7 review.
LABEL_T1_COL = "label_t1"
# AFML Snippet 4.11 time-decay parameter c (1.0 = no decay). Chosen in the Ch.9 hyper-parameter review, not here.
TIME_DECAY_C = 1.0
EVENT_VOL_COL = "Event_Vol"
TIME_COL = "Datetime"
RAW_PROB_COL = "raw_probability"

# Kept in sync with shared/labels/leakage_columns.py by
# tests/test_leakage_columns_sync.py -- this script runs standalone on
# Google Colab (flat mode) and cannot import shared/ at runtime, so it
# carries its own inlined copy instead of importing the canonical module.
LEAKAGE_COLS = {
    TARGET_COL,
    ELIGIBILITY_COL,
    GROSS_TARGET_COL,
    GROSS_REALIZED_R_COL,
    T1_COL,
    "t1",
    "barrier_hit",
    "Bar_Start",
    "entry_price",
    "pt_price",
    "sl_price",
    "realized_r",
    "meta_label",
    "pt_hit_label",
    "executed_meta_label",
    "resolution_source",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "approved_signal",
    # Task 2A/2B executable-target resolution columns:
    "source_event_row_number",
    "trgt",
    "geometry_hash",
    "execution_status",
    "execution_contract_version",
    "execution_geometry_hash",
    "signal_mid",
    "vertical_deadline",
    "entry_time",
    "entry_bid",
    "entry_ask",
    "entry_spread",
    "entry_spread_bps",
    "entry_lag_ms",
    "entry_drift_bps",
    "stop_distance_price",
    "take_profit_distance_price",
    "exit_time",
    "exit_price",
    "exit_reason",
    "executed_r",
    "hold_seconds",
    "entry_age_seconds",
    "max_signal_age_seconds",
    "quote_executable_r",
    "quote_executable_t1",
    "gross_to_quote_label_flip",
    "mldp_training_authority",
}

N_FOLDS = 10  # AFML Ch.12 walkthrough (D052): N = phi + 1 with k = 2 -> 9 CPCV paths, 45 splits (s.12.4.3 p.165)
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
F1_THRESHOLD_GRID = np.round(np.arange(0.30, 0.81, 0.01), 2)
RANDOM_STATE = 42

# Pilot validation value, not yet AFML's own illustrative 1000 (Snippet 6.2) --
# see 02_train_meta_model_optuna.py's printed rho_bar diagnostic for a
# data-driven read on whether raising this would meaningfully help. Must
# match Step02's N_ESTIMATORS_BAGGING so OOF predictions come from the same
# kind of ensemble as the deployed final model.
N_ESTIMATORS_BAGGING = 1000  # AFML Snippet 6.2 (owner decision 2026-10-24, Ch.6 walkthrough)


class OOFGenerationError(RuntimeError):
    """Raised when Step 03 must fail closed."""


# =============================================================================
# AFML Ch.6 (Ensemble Methods, p.93-102) + Ch.4 SS4.5 (Bagging Classifiers and
# Uniqueness, p.61-66) sequential-bootstrap bagging ensemble.
# =============================================================================
# Verbatim from shared/models/sequential_bootstrap.py and
# shared/models/sequential_bootstrap_bagging.py. Inlined rather than imported
# because this script runs standalone on Google Colab and cannot import the
# rest of `shared/` at runtime -- see docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's
# "Operational note" for the same constraint applied to purge/embargo.
# Each CPCV fold's model here must be the SAME kind of estimator as Step02's
# deployed final model (both bagged, same n_estimators) -- this is the fix
# for the OOF-vs-final-model raw-probability divergence found empirically on
# US500/volatilitycarry_long and BTCUSD/absorption_long (~0.15 mean shift):
# OOF predictions must come from a structurally identical ensemble to what
# is actually deployed, or calibration built on OOF is calibrating the wrong
# model.
# QUANTARA_AFML_CH6_CH4_5_SEQUENTIAL_BOOTSTRAP_BAGGING_V1


class SequentialBootstrapError(RuntimeError):
    """Raised when a sequential bootstrap draw cannot be constructed safely."""


def _sb_validate_positions(start_pos: np.ndarray, end_pos: np.ndarray, n_bars: int) -> None:
    if len(start_pos) != len(end_pos):
        raise SequentialBootstrapError("start_pos and end_pos length mismatch.")
    if len(start_pos) == 0:
        raise SequentialBootstrapError("No events supplied.")
    if (end_pos < start_pos).any():
        raise SequentialBootstrapError("At least one event has end_pos < start_pos.")
    if (start_pos < 0).any() or (end_pos >= n_bars).any():
        raise SequentialBootstrapError("Event bar positions fall outside [0, n_bars).")


def average_uniqueness_if_added(start_pos: np.ndarray, end_pos: np.ndarray, concurrency: np.ndarray) -> np.ndarray:
    inv = 1.0 / (concurrency + 1.0)
    prefix = np.concatenate(([0.0], np.cumsum(inv)))
    lengths = (end_pos - start_pos + 1).astype("float64")
    sums = prefix[end_pos + 1] - prefix[start_pos]
    return sums / lengths


def sequential_bootstrap(
    start_pos: np.ndarray,
    end_pos: np.ndarray,
    n_bars: int,
    *,
    sample_length: int | None = None,
    random_state: np.random.Generator | int | None = None,
    block_size: int | None = None,
) -> np.ndarray:
    """AFML Snippet 4.5 seqBootstrap, exact algorithm, sqrt-decomposition
    vectorized (see shared/models/sequential_bootstrap.py for the full
    derivation and correctness proof against AFML's own SS4.5.3 worked
    numerical example -- verified bit-for-bit against a brute-force
    reference)."""
    start_pos = np.asarray(start_pos, dtype="int64")
    end_pos = np.asarray(end_pos, dtype="int64")
    _sb_validate_positions(start_pos, end_pos, n_bars)
    n_events = len(start_pos)
    if sample_length is None:
        sample_length = n_events
    if sample_length <= 0:
        raise SequentialBootstrapError("sample_length must be > 0.")
    if block_size is None:
        block_size = max(1, int(np.sqrt(n_bars)))

    rng = random_state if isinstance(random_state, np.random.Generator) else np.random.default_rng(random_state)

    n_blocks = (n_bars + block_size - 1) // block_size
    concurrency = np.zeros(n_bars, dtype="float64")
    inv = np.ones(n_bars, dtype="float64")
    local_prefix = np.empty(n_bars, dtype="float64")
    block_sum = np.empty(n_blocks, dtype="float64")
    block_start = np.arange(n_blocks, dtype="int64") * block_size
    block_end = np.minimum(block_start + block_size, n_bars)
    for b in range(n_blocks):
        lo, hi = block_start[b], block_end[b]
        local_prefix[lo:hi] = np.cumsum(inv[lo:hi])
        block_sum[b] = local_prefix[hi - 1]
    block_prefix = np.concatenate(([0.0], np.cumsum(block_sum)))

    event_block_end = end_pos // block_size
    start_minus_1 = start_pos - 1
    has_prefix = start_minus_1 >= 0
    safe_start_minus_1 = np.where(has_prefix, start_minus_1, 0)
    event_block_start_minus_1 = safe_start_minus_1 // block_size
    lengths = (end_pos - start_pos + 1).astype("float64")

    def _full_prefix(pos: np.ndarray, blocks: np.ndarray) -> np.ndarray:
        return block_prefix[blocks] + local_prefix[pos]

    phi = np.empty(sample_length, dtype="int64")
    for draw in range(sample_length):
        end_sum = _full_prefix(end_pos, event_block_end)
        start_sum = np.where(has_prefix, _full_prefix(safe_start_minus_1, event_block_start_minus_1), 0.0)
        avg_u = (end_sum - start_sum) / lengths
        total = float(avg_u.sum())
        if not np.isfinite(total) or total <= 0.0:
            raise SequentialBootstrapError("Average uniqueness is degenerate; cannot form a draw probability.")
        cs = np.cumsum(avg_u)
        r = rng.random() * total
        choice = int(np.searchsorted(cs, r, side="right"))
        choice = min(choice, n_events - 1)
        phi[draw] = choice

        lo, hi = int(start_pos[choice]), int(end_pos[choice]) + 1
        concurrency[lo:hi] += 1.0
        np.add(concurrency[lo:hi], 1.0, out=inv[lo:hi])
        np.reciprocal(inv[lo:hi], out=inv[lo:hi])

        b_lo, b_hi = lo // block_size, (hi - 1) // block_size
        for b in range(b_lo, b_hi + 1):
            blo, bhi = int(block_start[b]), int(block_end[b])
            np.cumsum(inv[blo:bhi], out=local_prefix[blo:bhi])
            block_sum[b] = local_prefix[bhi - 1]
        block_prefix[1:] = np.cumsum(block_sum)
    return phi


class SequentialBootstrapBaggingClassifier:
    """AFML Ch.6 bagging ensemble of decision trees, resampled via AFML Ch.4
    SS4.5's sequential bootstrap instead of a plain (IID) bootstrap. See
    shared/models/sequential_bootstrap_bagging.py for full documentation."""

    def __init__(self, tree_params: dict[str, Any], *, n_estimators: int, random_state: int | None = None,
                 max_samples: str = "avgU") -> None:
        if n_estimators <= 0:
            raise SequentialBootstrapError("n_estimators must be > 0.")
        self.tree_params = dict(tree_params)
        self.n_estimators = int(n_estimators)
        self.random_state = random_state
        if max_samples not in ("avgU", "n"):
            raise SequentialBootstrapError("max_samples must be 'avgU' or 'n'.")
        self.max_samples = max_samples
        self.estimators_: list[DecisionTreeClassifier] = []
        self.classes_: np.ndarray | None = None

    def fit(self, x_data: Any, y: Any, *, sample_weight: np.ndarray | None = None,
            event_start_pos: np.ndarray, event_end_pos: np.ndarray, n_bars: int) -> "SequentialBootstrapBaggingClassifier":
        x_arr = np.asarray(x_data)
        y_arr = np.asarray(y)
        n_rows = len(y_arr)
        if x_arr.shape[0] != n_rows:
            raise SequentialBootstrapError("x_data/y row-count mismatch.")
        if len(event_start_pos) != n_rows or len(event_end_pos) != n_rows:
            raise SequentialBootstrapError("event_start_pos/event_end_pos must have one entry per training row.")
        weights = np.ones(n_rows, dtype="float64") if sample_weight is None else np.asarray(sample_weight, dtype="float64")
        if len(weights) != n_rows:
            raise SequentialBootstrapError("sample_weight row-count mismatch.")

        # AFML s.6.3.3 (p.97): with overlapping labels each estimator samples about average-uniqueness x n observations,
        # "but not more" (Snippet 4.5 sLength); avgU is that of THIS training set (concurrency among its own events).
        start_arr = np.asarray(event_start_pos, dtype="int64")
        end_arr = np.asarray(event_end_pos, dtype="int64")
        diff = np.zeros(int(n_bars) + 1, dtype="float64")
        np.add.at(diff, start_arr, 1.0)
        np.add.at(diff, end_arr + 1, -1.0)
        concurrency = np.cumsum(diff[:-1])
        inv = np.where(concurrency > 0, 1.0 / np.maximum(concurrency, 1.0), 0.0)
        prefix = np.concatenate(([0.0], np.cumsum(inv)))
        self.avg_uniqueness_ = float(np.mean((prefix[end_arr + 1] - prefix[start_arr]) / (end_arr - start_arr + 1)))
        self.sample_length_ = (max(1, int(round(self.avg_uniqueness_ * n_rows))) if self.max_samples == "avgU" else n_rows)
        rng = np.random.default_rng(self.random_state)
        self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(event_start_pos, event_end_pos, n_bars, sample_length=self.sample_length_, random_state=rng)
            # AFML s.6.4 (p.98): every tree draws its own random feature subsets. One shared random_state made all trees
            # draw the same subsets (fixed 2026-10-24); per-tree seeds come from the seeded ensemble rng, as in sklearn.
            tree = DecisionTreeClassifier(**{**self.tree_params, "random_state": int(rng.integers(0, 2**31 - 1))})
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)

        self.classes_ = self.estimators_[0].classes_
        for tree in self.estimators_[1:]:
            if not np.array_equal(tree.classes_, self.classes_):
                raise SequentialBootstrapError(
                    "A bagged tree saw a different class set than the ensemble; "
                    "some bootstrap draw is missing a class entirely (check class balance/n_estimators)."
                )
        self.feature_importances_ = np.mean([tree.feature_importances_ for tree in self.estimators_], axis=0)
        return self

    def predict_proba(self, x_data: Any) -> np.ndarray:
        if not self.estimators_:
            raise SequentialBootstrapError("Ensemble is not fit yet.")
        x_arr = np.asarray(x_data)
        proba_sum = np.zeros((x_arr.shape[0], len(self.classes_)), dtype="float64")
        for tree in self.estimators_:
            proba_sum += tree.predict_proba(x_arr)
        return proba_sum / len(self.estimators_)

    def predict(self, x_data: Any) -> np.ndarray:
        proba = self.predict_proba(x_data)
        return self.classes_[np.argmax(proba, axis=1)]

    def mean_pairwise_tree_correlation(self, x_data: Any) -> float:
        """AFML's rho_bar (SS6.3.1, p.95). See shared/models/sequential_bootstrap_bagging.py."""
        if len(self.estimators_) < 2:
            raise SequentialBootstrapError("Need at least 2 estimators to compute pairwise correlation.")
        x_arr = np.asarray(x_data)
        pos_col = int(np.where(self.classes_ == 1)[0][0]) if 1 in self.classes_ else 1
        preds = np.stack([tree.predict_proba(x_arr)[:, pos_col] for tree in self.estimators_], axis=0)
        corr = np.corrcoef(preds)
        n = corr.shape[0]
        off_diag = corr[~np.eye(n, dtype=bool)]
        return float(np.mean(off_diag))

# Note: this script never joblib.dumps a SequentialBootstrapBaggingClassifier
# instance (per-fold OOF models are used in-process only, never persisted),
# so it does not need the portable-payload treatment 02_train_meta_model_
# optuna.py's final model requires -- see that script's
# bagging_to_portable_payload docstring if a future change here ever does
# need to persist a fitted ensemble.


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


def load_json(path: Path) -> Any:
    if not path.exists():
        raise OOFGenerationError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(V2_ROOT.resolve())).replace("\\", "/")
    except Exception:
        return str(path.resolve()).replace("\\", "/")



def _phase3a_sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()




def _afml_weight_table_sha256(table: pd.DataFrame) -> str:
    """Stable digest of event timestamps + canonical AFML financial fields.

    Numerical values are rounded only for the provenance digest so harmless
    last-bit platform differences do not break lineage. Training values are
    never rounded or altered.
    """
    required = [AFML_AVERAGE_UNIQUENESS_COL, AFML_RETURN_ATTRIBUTION_WEIGHT_COL]
    missing = [c for c in required if c not in table.columns]
    if missing:
        raise OOFGenerationError(f"Cannot hash AFML table; missing columns: {missing}")
    work = table.sort_index()
    h = hashlib.sha256()
    for ts, u, w in zip(
        pd.DatetimeIndex(work.index),
        work[AFML_AVERAGE_UNIQUENESS_COL].to_numpy(dtype="float64"),
        work[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].to_numpy(dtype="float64"),
    ):
        h.update(f"{int(pd.Timestamp(ts).value)}|{float(u):.12f}|{float(w):.12f}\n".encode("utf-8"))
    return h.hexdigest()


def _afml_provenance_snapshot() -> dict[str, Any]:
    bars_path = _resolve_standard_quote_tick_bars_path()
    table = _get_afml_weight_table()
    return {
        "contract_version": AFML_SAMPLE_WEIGHT_CONTRACT_VERSION,
        "standard_quote_tick_bars": display_path(bars_path),
        "standard_quote_tick_bars_sha256": _phase3a_sha256_file(bars_path),
        "event_weight_table_sha256": _afml_weight_table_sha256(table),
        "event_count": int(len(table)),
        "average_uniqueness": {
            "mean": float(table[AFML_AVERAGE_UNIQUENESS_COL].mean()),
            "min": float(table[AFML_AVERAGE_UNIQUENESS_COL].min()),
            "median": float(table[AFML_AVERAGE_UNIQUENESS_COL].median()),
            "max": float(table[AFML_AVERAGE_UNIQUENESS_COL].max()),
        },
        "return_attribution_weight": {
            "mean": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].mean()),
            "min": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].min()),
            "median": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].median()),
            "max": float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].max()),
        },
        "weighting_note": (
            "AFML financial observation weight is normalized absolute "
            "concurrency-adjusted log-return attribution on the "
            "standard_quote_tick bar clock; class balancing is separate."
        ),
    }


def _validate_afml_provenance_numerically(
    declared: dict[str, Any], current: dict[str, Any], *, prior_step_label: str
) -> None:
    """Compare a prior step's declared AFML weight-table summary stats
    against a freshly recomputed table using a numerical tolerance rather
    than exact hash equality.

    WHY NOT AN EXACT HASH: the underlying computation (cumulative sums over
    tens of thousands of bars) can legitimately differ at the ~1e-12 level
    across different execution environments (e.g. a prior step run on
    Google Colab vs. this step run locally) even when the input files and
    algorithm are provably identical -- confirmed by cross-checking the
    Colab-inlined and shared implementations against each other on
    identical local data (zero difference) while the exact-hash comparison
    against a genuinely Colab-computed table still failed. An exact hash is
    the wrong tool for verifying floating-point results computed in a
    different environment; this tolerance still catches real errors (wrong
    file, wrong algorithm, wrong column) since those produce differences
    far larger than floating-point noise.
    """
    if declared.get("event_count") != current.get("event_count"):
        raise OOFGenerationError(
            f"{prior_step_label} AFML provenance mismatch for event_count: "
            f"declared={declared.get('event_count')}, actual={current.get('event_count')}."
        )
    for field in ("average_uniqueness", "return_attribution_weight"):
        declared_stats = declared.get(field, {})
        current_stats = current.get(field, {})
        for stat in ("mean", "min", "median", "max"):
            declared_value = float(declared_stats.get(stat, float("nan")))
            current_value = float(current_stats.get(stat, float("nan")))
            if not np.isclose(declared_value, current_value, rtol=1e-6, atol=1e-9):
                raise OOFGenerationError(
                    f"{prior_step_label} AFML provenance mismatch for {field}.{stat}: "
                    f"declared={declared_value!r}, actual={current_value!r}."
                )


def _phase3a_validate_gross_target(df: pd.DataFrame) -> None:
    """Data-integrity check on the GROSS layer only (gross_meta_label must
    equal int(gross_r > 0) for every row). This never touches the
    executable-target OOF label; it exists purely to catch a corrupted or
    stale resolution CSV before Step 03 proceeds."""
    if GROSS_REALIZED_R_COL not in df.columns:
        raise OOFGenerationError(
            f"{EDGE_ID} resolution rows missing {GROSS_REALIZED_R_COL!r}; "
            "Step 03 requires Task 2A's executable-target resolution CSV."
        )
    realized = pd.to_numeric(df[GROSS_REALIZED_R_COL], errors="coerce")
    target = pd.to_numeric(df[GROSS_TARGET_COL], errors="coerce")
    if realized.isna().any() or target.isna().any():
        raise OOFGenerationError(
            f"{EDGE_ID}: gross target provenance contains non-numeric/NaN values."
        )
    expected = (realized > 0.0).astype(int)
    actual = target.astype(int)
    if not actual.equals(expected):
        mismatches = int((actual != expected).sum())
        raise OOFGenerationError(
            f"{EDGE_ID}: {GROSS_TARGET_COL} violates {GROSS_LABEL_CONTRACT_VERSION}; "
            f"expected {GROSS_TARGET_DEFINITION}; mismatches={mismatches}."
        )


def _validate_executable_target_eligibility(df: pd.DataFrame) -> None:
    """Task 2B/A2 consistency check: mldp_target_eligible must agree exactly
    with whether quote_executable_meta_label is populated."""
    if ELIGIBILITY_COL not in df.columns:
        raise OOFGenerationError(
            f"{EDGE_ID} resolution rows missing {ELIGIBILITY_COL!r}; "
            "Step 03 requires Task 2A's executable-target resolution CSV."
        )
    eligible = df[ELIGIBILITY_COL].astype(bool)
    target_present = df[TARGET_COL].notna()
    if not eligible.equals(target_present):
        mismatches = int((eligible != target_present).sum())
        raise OOFGenerationError(
            f"{EDGE_ID}: {ELIGIBILITY_COL} disagrees with {TARGET_COL} "
            f"presence for {mismatches} row(s); Task 2A resolution CSV is "
            "internally inconsistent."
        )


def _phase3a_validate_step02_training_manifest() -> dict[str, Any]:
    if not STEP02_TRAINING_MANIFEST_PATH.exists():
        raise OOFGenerationError(
            f"Missing Step-02 training manifest required by Phase 3A: "
            f"{STEP02_TRAINING_MANIFEST_PATH}"
        )
    payload = load_json(STEP02_TRAINING_MANIFEST_PATH)
    if payload.get("edge_id") != EDGE_ID:
        raise OOFGenerationError("Step-02 training manifest edge_id mismatch.")
    if payload.get("gross_label_contract_version") != GROSS_LABEL_CONTRACT_VERSION:
        raise OOFGenerationError("Step-02 gross label contract is stale.")
    if payload.get("gross_target_column") != GROSS_TARGET_COL:
        raise OOFGenerationError(f"Step-02 gross_target_column must be {GROSS_TARGET_COL!r}.")
    if payload.get("gross_target_definition") != GROSS_TARGET_DEFINITION:
        raise OOFGenerationError("Step-02 gross target definition mismatch.")
    if payload.get("training_target_column") != TARGET_COL:
        raise OOFGenerationError(f"Step-02 training_target_column must be {TARGET_COL!r}.")
    if payload.get("training_eligibility_column") != ELIGIBILITY_COL:
        raise OOFGenerationError(f"Step-02 training_eligibility_column must be {ELIGIBILITY_COL!r}.")
    declared_step01 = str(payload.get("step01_feature_surgery_manifest_sha256", ""))
    actual_step01 = _phase3a_sha256_file(STEP01_FEATURE_SURGERY_MANIFEST_PATH)
    if not declared_step01 or declared_step01 != actual_step01:
        raise OOFGenerationError(
            "Step-02 was not built from the current Step-01 feature-surgery manifest. "
            "Rerun Step 02 before Step 03."
        )
    checks = {
        "model_rows_sha256": RESOLUTION_CSV_PATH,
        "selected_features_sha256": SELECTED_FEATURES_PATH,
        "feature_schema_sha256": FEATURE_SCHEMA_PATH,
        "best_params_sha256": BEST_PARAMS_PATH,
    }
    for key, path in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(path)
        if not declared or declared != actual:
            raise OOFGenerationError(
                f"Step-02 provenance mismatch for {key}; rerun Step 02 before Step 03."
            )

    afml = payload.get("afml_provenance")
    if not isinstance(afml, dict):
        raise OOFGenerationError(
            "Step-02 manifest has no AFML provenance block. Rerun patched Step 02 before Step 03."
        )
    if afml.get("contract_version") != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
        raise OOFGenerationError("Step-02 AFML sample-weight contract is stale.")

    current_afml = _afml_provenance_snapshot()
    if str(afml.get("standard_quote_tick_bars_sha256", "")) != current_afml["standard_quote_tick_bars_sha256"]:
        raise OOFGenerationError("Step-02 AFML provenance mismatch for standard_quote_tick_bars_sha256.")
    _validate_afml_provenance_numerically(afml, current_afml, prior_step_label="Step-02")
    return payload


def preflight_check() -> None:
    required_paths = {
        "model_rows": RESOLUTION_CSV_PATH,
        "selected_features": SELECTED_FEATURES_PATH,
        "best_params": BEST_PARAMS_PATH,
        "feature_schema": FEATURE_SCHEMA_PATH,
        "step02_training_manifest": STEP02_TRAINING_MANIFEST_PATH,
        "step01_feature_surgery_manifest": STEP01_FEATURE_SURGERY_MANIFEST_PATH,
    }
    missing = {name: path for name, path in required_paths.items() if not path.exists()}
    if missing:
        lines = ["Step 03 preflight failed. Missing required input(s):"]
        for name, path in missing.items():
            lines.append(f"  - {name}: {path}")
        if RUN_MODE == "repo_local":
            lines.extend(
                [
                    "",
                    "Copy Step 01 outputs into:",
                    f"  assets/{ASSET}/edges/{FAMILY}_{SIDE}/artifacts/feature_surgery/",
                    "Copy Step 02 outputs into:",
                    f"  assets/{ASSET}/edges/{FAMILY}_{SIDE}/artifacts/model/",
                    "Keep the research dataset in:",
                    f"  assets/{ASSET}/edges/{FAMILY}_{SIDE}/artifacts/research_dataset/",
                ]
            )
        raise OOFGenerationError("\n".join(lines))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _event_bar_positions(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, int]:
    """Bar-clock positions of each row's [Datetime, gross_t1] span, needed by
    sequential_bootstrap. See 02_train_meta_model_optuna.py's identical
    helper for the full explanation."""
    bars_path = _resolve_standard_quote_tick_bars_path()
    bars = pd.read_csv(bars_path, usecols=[TIME_COL])
    bars[TIME_COL] = to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce")
    if bars[TIME_COL].isna().any():
        raise OOFGenerationError("standard_quote_tick bars contain invalid Datetime values.")
    if bars[TIME_COL].duplicated().any():
        raise OOFGenerationError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[TIME_COL].is_monotonic_increasing:
        raise OOFGenerationError("standard_quote_tick bars are not chronologically sorted.")
    bar_index = pd.DatetimeIndex(bars[TIME_COL])

    starts = pd.DatetimeIndex(to_datetime_iso(df.index, utc=True, errors="coerce"))
    ends = _label_end_times(df)
    if starts.isna().any() or ends.isna().any():
        raise OOFGenerationError("Rows contain invalid Datetime/gross_t1 for bar-position mapping.")

    start_pos = bar_index.get_indexer(starts)
    end_pos = _snap_to_bar_clock(ends, bar_index)
    if (start_pos < 0).any() or (end_pos < 0).any():
        raise OOFGenerationError("Some event start/end timestamps are absent from the standard_quote_tick bar clock.")
    if (end_pos < start_pos).any():
        raise OOFGenerationError("At least one event maps to an end bar before its start bar.")
    return start_pos.astype("int64"), end_pos.astype("int64"), len(bar_index)


def make_bagged_model(best_params: dict[str, Any], df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series,
                       sample_weight: np.ndarray, *, random_state: int | None = RANDOM_STATE) -> SequentialBootstrapBaggingClassifier:
    """AFML Ch.6 bagging ensemble, sequential-bootstrap resampled (AFML Ch.4
    SS4.5) -- identical construction to Step02's train_final_model, applied
    per CPCV fold here so OOF predictions come from the same kind of
    estimator as the deployed final model. tree_params come from Step02's
    best_params.json (Optuna-tuned DecisionTreeClassifier hyperparameters)."""
    start_pos, end_pos, n_bars = _event_bar_positions(df)
    tree_params = {k: v for k, v in best_params.items() if k != "class_weight_mode"}
    tree_params = tree_params | {"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced", "random_state": random_state}
    model = SequentialBootstrapBaggingClassifier(tree_params=tree_params, n_estimators=N_ESTIMATORS_BAGGING, random_state=random_state)
    model.fit(x_data, y, sample_weight=sample_weight, event_start_pos=start_pos, event_end_pos=end_pos, n_bars=n_bars)
    return model


def load_allowed_schema_features() -> set[str]:
    schema = load_json(FEATURE_SCHEMA_PATH)
    allowed = set()
    for row in schema.get("features", []):
        if (
            row.get("live_available") is True
            and row.get("allowed_for_model_input") is True
            and row.get("uses_future_information") is False
        ):
            allowed.add(str(row["feature_name"]))
    if not allowed:
        raise OOFGenerationError("No live-safe allowed model-input features found in feature_schema.json")
    return allowed


def load_selected_features() -> list[str]:
    features = load_json(SELECTED_FEATURES_PATH)
    if not isinstance(features, list) or not features:
        raise OOFGenerationError("Selected features JSON must be a non-empty list.")
    features = [str(f) for f in features]
    duplicates = sorted({f for f in features if features.count(f) > 1})
    if duplicates:
        raise OOFGenerationError(f"Duplicate selected features found: {duplicates}")
    bad = [f for f in features if f in LEAKAGE_COLS]
    if bad:
        raise OOFGenerationError(f"Selected features contain forbidden leakage columns: {bad}")
    allowed = load_allowed_schema_features()
    not_allowed = [f for f in features if f not in allowed]
    if not_allowed:
        raise OOFGenerationError(f"Selected features not allowed by feature_schema.json: {not_allowed}")
    return features


def _validate_edge_contract(df: pd.DataFrame) -> None:
    if "edge_id" in df.columns and not (df["edge_id"] == EDGE_ID).all():
        bad = df.loc[df["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise OOFGenerationError(f"{EDGE_ID} model rows contain wrong edge_id values: {bad}")
    if "side" in df.columns and not (pd.to_numeric(df["side"], errors="coerce") == SIDE_VALUE).all():
        bad = df.loc[pd.to_numeric(df["side"], errors="coerce") != SIDE_VALUE, "side"].unique().tolist()
        raise OOFGenerationError(f"{EDGE_ID} model rows contain wrong side values: {bad}; expected {SIDE_VALUE}")
    if "pt_price" in df.columns and "entry_price" in df.columns:
        pt = pd.to_numeric(df["pt_price"], errors="coerce")
        entry = pd.to_numeric(df["entry_price"], errors="coerce")
        # Task 2A's resolution CSV legitimately carries NaN entry/pt/sl prices
        # for events that never executed (e.g. execution_status ==
        # "stale_entry_quote"): NaN here means "no execution happened," not a
        # geometry violation, so only rows where both prices are defined are
        # checked -- see docs/mldp/TASK2B_POPULATION_RULE_DECISION.md.
        mask = pt.notna() & entry.notna()
        if SIDE_VALUE == 1 and not (pt[mask] > entry[mask]).all():
            raise OOFGenerationError(f"{EDGE_ID} model rows must have pt_price above entry_price for long edges.")
        if SIDE_VALUE == -1 and not (pt[mask] < entry[mask]).all():
            raise OOFGenerationError(f"{EDGE_ID} model rows must have pt_price below entry_price for short edges.")
    if "sl_price" in df.columns and "entry_price" in df.columns:
        sl = pd.to_numeric(df["sl_price"], errors="coerce")
        entry = pd.to_numeric(df["entry_price"], errors="coerce")
        mask = sl.notna() & entry.notna()
        if SIDE_VALUE == 1 and not (sl[mask] < entry[mask]).all():
            raise OOFGenerationError(f"{EDGE_ID} model rows must have sl_price below entry_price for long edges.")
        if SIDE_VALUE == -1 and not (sl[mask] > entry[mask]).all():
            raise OOFGenerationError(f"{EDGE_ID} model rows must have sl_price above entry_price for short edges.")


_LAST_ELIGIBILITY_FILTER_COUNTS: dict[str, int] = {}


def load_dataset(selected_features: list[str]) -> pd.DataFrame:
    """Load Task 2A's executable-target resolution CSV, validate it on the
    FULL (unfiltered) gross population, then restrict to
    mldp_target_eligible==True rows -- see
    docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md and
    docs/mldp/TASK2B_POPULATION_RULE_DECISION.md.

    Note: this filtering happens on the in-memory frame returned here; the
    separate AFML sample-weight table (_get_afml_weight_table) re-reads the
    same file directly and is deliberately NOT filtered, since concurrency/
    uniqueness must be computed over every gross event.
    """
    if not RESOLUTION_CSV_PATH.exists():
        raise OOFGenerationError(f"Missing Task 2A executable-target resolution CSV: {RESOLUTION_CSV_PATH}")
    df = pd.read_csv(RESOLUTION_CSV_PATH)
    _validate_edge_contract(df)
    required = [TIME_COL, TARGET_COL, ELIGIBILITY_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, EVENT_VOL_COL, T1_COL, *selected_features]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise OOFGenerationError(f"Resolution rows missing required columns: {missing}")

    df[TIME_COL] = to_datetime_iso(df[TIME_COL], utc=True, errors="coerce")
    df[T1_COL] = to_datetime_iso(df[T1_COL], utc=True, errors="coerce")
    df[GROSS_TARGET_COL] = pd.to_numeric(df[GROSS_TARGET_COL], errors="coerce")
    df[GROSS_REALIZED_R_COL] = pd.to_numeric(df[GROSS_REALIZED_R_COL], errors="coerce")
    df[EVENT_VOL_COL] = pd.to_numeric(df[EVENT_VOL_COL], errors="coerce")
    df[ELIGIBILITY_COL] = df[ELIGIBILITY_COL].astype(bool)
    # TARGET_COL (quote_executable_meta_label) is legitimately NaN for every
    # ineligible row -- coerce without dropping NaNs here; eligibility
    # filtering below is what removes them, not a blanket dropna.
    df[TARGET_COL] = pd.to_numeric(df[TARGET_COL], errors="coerce")
    for col in selected_features:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Both integrity checks run on the FULL gross population, before any
    # eligibility filtering, since they validate columns that must be
    # correct for every row regardless of executable-target eligibility.
    _phase3a_validate_gross_target(df)
    _validate_executable_target_eligibility(df)

    before = len(df)
    df = df.dropna(subset=[TIME_COL, T1_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, EVENT_VOL_COL, *selected_features]).copy()
    after = len(df)
    if after == 0:
        raise OOFGenerationError("All rows dropped after gross-column/selected-feature NaN filtering.")

    before_eligibility = after
    df = df.loc[df[ELIGIBILITY_COL]].copy()
    after_eligibility = len(df)
    global _LAST_ELIGIBILITY_FILTER_COUNTS
    _LAST_ELIGIBILITY_FILTER_COUNTS = {
        "rows_before_eligibility_filter": int(before_eligibility),
        "rows_after_eligibility_filter": int(after_eligibility),
    }
    print(
        f"Task 2B eligibility filter: {after_eligibility:,}/{before_eligibility:,} "
        f"rows retained (mldp_target_eligible==True); "
        f"{before_eligibility - after_eligibility:,} excluded (TRUE_NO_FILL / "
        "RIGHT_CENSORED_END_OF_HISTORY per docs/mldp/TASK2B_POPULATION_RULE_DECISION.md).",
        flush=True,
    )
    if df.empty:
        raise OOFGenerationError("No mldp_target_eligible==True rows remain; cannot generate OOF predictions.")
    if df[TARGET_COL].isna().any():
        raise OOFGenerationError(
            f"{TARGET_COL} contains NaN among eligible rows; Task 2A resolution CSV is inconsistent."
        )
    df[TARGET_COL] = df[TARGET_COL].astype(int)

    df = df.sort_values(TIME_COL).set_index(TIME_COL)
    if df[TARGET_COL].nunique() < 2:
        raise OOFGenerationError(f"{TARGET_COL} has fewer than 2 classes among eligible rows.")
    print(f"Rows before selected-feature NaN filter: {before:,}", flush=True)
    print(f"Rows after selected-feature NaN filter : {after:,}", flush=True)
    return df


AFML_SAMPLE_WEIGHT_CONTRACT_VERSION = "quantara_afml_ch4_bar_clock_return_attribution_v2_label_t1"
AFML_AVERAGE_UNIQUENESS_COL = "average_uniqueness"
AFML_RETURN_ATTRIBUTION_WEIGHT_COL = "return_attribution_weight"
_AFML_WEIGHT_CACHE = None


def _resolve_standard_quote_tick_bars_path() -> Path:
    if RUN_MODE == "flat_colab":
        path = CWD / f"{ASSET.lower()}_standard_quote_tick_bars.csv"
    else:
        path = (
            V2_ROOT
            / "assets"
            / ASSET
            / "bars"
            / "standard_quote_tick"
            / f"{ASSET.lower()}_standard_quote_tick_bars.csv"
        )
    if not path.exists():
        mode_hint = (
            f"Flat Colab requires uploading {ASSET.lower()}_standard_quote_tick_bars.csv into /content."
            if RUN_MODE == "flat_colab"
            else "Forge/restore the asset standard_quote_tick bars before running this research step."
        )
        raise OOFGenerationError(f"Missing standard_quote_tick bars: {path}. {mode_hint}")
    return path


# AFML Ch.4 walkthrough 2026-10-23: event end = the training label's own t1 (Task 2A label_t1, fallback gross_t1),
# snapped to the bar that contains it (first bar whose Datetime >= the tick time). Local copy of
# shared/models/afml_sample_weights.label_end_times / snap_to_bar_clock so flat_colab mode stays self-contained.
def _label_end_times(frame: pd.DataFrame) -> pd.DatetimeIndex:
    fallback = to_datetime_iso(frame[T1_COL], utc=True, errors="coerce")
    if LABEL_T1_COL in frame.columns:
        fallback = to_datetime_iso(frame[LABEL_T1_COL], utc=True, errors="coerce").fillna(fallback)
    return pd.DatetimeIndex(fallback)


def _snap_to_bar_clock(times: pd.DatetimeIndex, bar_index: pd.DatetimeIndex) -> np.ndarray:
    pos = np.searchsorted(bar_index.as_unit("ns").asi8, pd.DatetimeIndex(times).as_unit("ns").asi8, side="left")
    if (pos >= len(bar_index)).any():
        raise OOFGenerationError(f"{int((pos >= len(bar_index)).sum())} event end time(s) fall after the last bar.")
    return pos.astype("int64")


def _time_decay_factors(index: pd.Index, uniqueness: np.ndarray) -> np.ndarray:
    """AFML Snippet 4.11 (p.70) on cumulative uniqueness; TIME_DECAY_C = 1.0 returns all ones."""
    if TIME_DECAY_C == 1.0:
        return np.ones(len(uniqueness), dtype="float64")
    tw = pd.Series(np.asarray(uniqueness, dtype="float64"), index=pd.DatetimeIndex(index))
    cum = tw.sort_index().cumsum()
    total = float(cum.iloc[-1])
    slope = (1.0 - TIME_DECAY_C) / total if TIME_DECAY_C >= 0 else 1.0 / ((TIME_DECAY_C + 1.0) * total)
    decay = (1.0 - slope * total + slope * cum).clip(lower=0.0)
    return decay.reindex(tw.index).to_numpy(dtype="float64")


def _compute_afml_event_weight_table_local(model_rows_path: Path, bars_path: Path) -> pd.DataFrame:
    try:
        events = pd.read_csv(model_rows_path, usecols=lambda c: c in (TIME_COL, T1_COL, LABEL_T1_COL))
    except Exception as exc:
        raise OOFGenerationError(f"Could not read Datetime/t1 from model rows {model_rows_path}: {exc}") from exc
    try:
        bars = pd.read_csv(bars_path, usecols=[TIME_COL, "Close"])
    except Exception as exc:
        raise OOFGenerationError(f"Could not read Datetime/Close from standard_quote_tick bars {bars_path}: {exc}") from exc

    events[TIME_COL] = to_datetime_iso(events[TIME_COL], utc=True, errors="coerce")
    events[T1_COL] = to_datetime_iso(events[T1_COL], utc=True, errors="coerce")
    if events[[TIME_COL, T1_COL]].isna().any().any():
        bad = int(events[[TIME_COL, T1_COL]].isna().any(axis=1).sum())
        raise OOFGenerationError(f"Model rows contain {bad} event(s) with invalid t0/t1.")
    if events[TIME_COL].duplicated().any():
        raise OOFGenerationError("Model rows contain duplicate event start timestamps.")
    events = events.sort_values(TIME_COL).reset_index(drop=True)

    bars[TIME_COL] = to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce")
    bars["Close"] = pd.to_numeric(bars["Close"], errors="coerce")
    if bars[[TIME_COL, "Close"]].isna().any().any():
        bad = int(bars[[TIME_COL, "Close"]].isna().any(axis=1).sum())
        raise OOFGenerationError(f"standard_quote_tick bars contain {bad} invalid Datetime/Close row(s).")
    if bars[TIME_COL].duplicated().any():
        raise OOFGenerationError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[TIME_COL].is_monotonic_increasing:
        raise OOFGenerationError("standard_quote_tick bars are not chronologically sorted; refusing to silently reorder them.")

    starts = pd.DatetimeIndex(events[TIME_COL])
    bar_index = pd.DatetimeIndex(bars[TIME_COL])
    ends = bar_index[_snap_to_bar_clock(_label_end_times(events), bar_index)]
    close = bars["Close"].to_numpy(dtype="float64")

    if len(starts) == 0:
        raise OOFGenerationError("No labelled events were supplied for AFML weighting.")
    if (ends.asi8 < starts.asi8).any():
        raise OOFGenerationError("At least one event has t1 earlier than t0.")
    if not np.isfinite(close).all() or (close <= 0.0).any():
        raise OOFGenerationError("Underlying Close must be finite and strictly positive for log returns.")

    start_pos = bar_index.get_indexer(starts)
    end_pos = bar_index.get_indexer(ends)
    if (start_pos < 0).any():
        missing = starts[start_pos < 0][:5].astype(str).tolist()
        raise OOFGenerationError(f"Event start timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < 0).any():
        missing = ends[end_pos < 0][:5].astype(str).tolist()
        raise OOFGenerationError(f"Event t1 timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < start_pos).any():
        raise OOFGenerationError("At least one event maps to an end bar before its start bar.")

    diff = np.zeros(len(bar_index) + 1, dtype="float64")
    np.add.at(diff, start_pos, 1.0)
    np.add.at(diff, end_pos + 1, -1.0)
    concurrency = np.cumsum(diff[:-1])
    active = concurrency > 0.0
    if not active.any():
        raise OOFGenerationError("No active event bars found after AFML concurrency construction.")

    inv = np.zeros_like(concurrency)
    inv[active] = 1.0 / concurrency[active]
    inv_prefix = np.concatenate(([0.0], np.cumsum(inv)))
    lengths = (end_pos - start_pos + 1).astype("float64")
    uniqueness = (inv_prefix[end_pos + 1] - inv_prefix[start_pos]) / lengths
    if not np.isfinite(uniqueness).all() or (uniqueness <= 0.0).any() or (uniqueness > 1.0 + 1e-12).any():
        raise OOFGenerationError("AFML average uniqueness is invalid/outside (0,1].")

    log_close = np.log(close)
    log_ret = np.empty_like(log_close)
    log_ret[0] = np.nan
    log_ret[1:] = np.diff(log_close)
    attributed = np.zeros_like(log_ret)
    valid = active & np.isfinite(log_ret)
    attributed[valid] = log_ret[valid] / concurrency[valid]
    prefix = np.concatenate(([0.0], np.cumsum(attributed)))
    raw_weight = np.abs(prefix[end_pos + 1] - prefix[start_pos])
    total = float(raw_weight.sum())
    if not np.isfinite(raw_weight).all() or not np.isfinite(total) or total <= 0.0:
        raise OOFGenerationError("AFML return-attribution weights are non-finite or sum to zero.")
    normalized = raw_weight * (float(len(raw_weight)) / total)
    if not np.isclose(float(normalized.mean()), 1.0, rtol=1e-10, atol=1e-12):
        raise OOFGenerationError(f"AFML normalized weight mean is not 1: {float(normalized.mean())}")

    return pd.DataFrame(
        {
            AFML_AVERAGE_UNIQUENESS_COL: uniqueness.astype("float64"),
            AFML_RETURN_ATTRIBUTION_WEIGHT_COL: normalized.astype("float64"),
        },
        index=starts,
    )


def _get_afml_weight_table() -> pd.DataFrame:
    global _AFML_WEIGHT_CACHE
    if _AFML_WEIGHT_CACHE is not None:
        return _AFML_WEIGHT_CACHE

    bars_path = _resolve_standard_quote_tick_bars_path()
    if RUN_MODE == "flat_colab":
        table = _compute_afml_event_weight_table_local(RESOLUTION_CSV_PATH, bars_path)
    else:
        root_str = str(V2_ROOT)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            from shared.models.afml_sample_weights import (
                AFML_SAMPLE_WEIGHT_CONTRACT_VERSION as shared_contract,
                build_afml_event_weight_table,
            )
        except Exception as exc:
            raise OOFGenerationError(f"Could not import shared AFML sample-weight engine: {exc}") from exc
        if shared_contract != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
            raise OOFGenerationError(
                f"AFML sample-weight contract mismatch: script={AFML_SAMPLE_WEIGHT_CONTRACT_VERSION}, "
                f"shared={shared_contract}"
            )
        try:
            table = build_afml_event_weight_table(
                RESOLUTION_CSV_PATH,
                bars_path,
                time_col=TIME_COL,
                t1_col=T1_COL,
                label_t1_col=LABEL_T1_COL,
                close_col="Close",
            )
        except Exception as exc:
            raise OOFGenerationError(f"Shared AFML sample-weight construction failed: {exc}") from exc

    required = {AFML_AVERAGE_UNIQUENESS_COL, AFML_RETURN_ATTRIBUTION_WEIGHT_COL}
    missing = sorted(required.difference(table.columns))
    if missing:
        raise OOFGenerationError(f"AFML weight table missing required columns: {missing}")
    if table.index.has_duplicates:
        raise OOFGenerationError("AFML weight table has duplicate event timestamps.")
    if not np.isfinite(table[list(required)].to_numpy(dtype="float64")).all():
        raise OOFGenerationError("AFML weight table contains NaN/inf values.")

    _AFML_WEIGHT_CACHE = table.sort_index()
    print(
        f"AFML Ch.4 weights | source={display_path(bars_path)} | events={len(table):,} | "
        f"mean_uniqueness={float(table[AFML_AVERAGE_UNIQUENESS_COL].mean()):.6f} | "
        f"mean_return_weight={float(table[AFML_RETURN_ATTRIBUTION_WEIGHT_COL].mean()):.6f}",
        flush=True,
    )
    return _AFML_WEIGHT_CACHE


def _afml_values_for_index(index: pd.Index, column: str) -> np.ndarray:
    table = _get_afml_weight_table()
    idx = pd.DatetimeIndex(to_datetime_iso(index, utc=True, errors="coerce"))
    if idx.isna().any():
        raise OOFGenerationError("Training/event index contains NaT; cannot align AFML weights.")
    missing = idx.difference(table.index)
    if len(missing):
        examples = missing[:5].astype(str).tolist()
        raise OOFGenerationError(f"{len(missing)} model event timestamp(s) are absent from AFML weight table; examples={examples}")
    values = table.loc[idx, column].to_numpy(dtype="float64")
    if not np.isfinite(values).all():
        raise OOFGenerationError(f"AFML column {column} contains NaN/inf after alignment.")
    return values


def compute_uniqueness(df: pd.DataFrame) -> np.ndarray:
    """Return AFML average uniqueness computed on the underlying price-bar clock."""
    if len(df) == 0:
        return np.array([], dtype=float)
    return _afml_values_for_index(df.index, AFML_AVERAGE_UNIQUENESS_COL)


def _afml_return_weights(df: pd.DataFrame) -> np.ndarray:
    if len(df) == 0:
        return np.array([], dtype=float)
    return _afml_values_for_index(df.index, AFML_RETURN_ATTRIBUTION_WEIGHT_COL)


def _weighted_binary_log_loss(y_true: pd.Series, probability: np.ndarray, sample_weight: np.ndarray) -> float:
    y_arr = np.asarray(y_true, dtype="float64")
    p = np.clip(np.asarray(probability, dtype="float64"), 1e-15, 1.0 - 1e-15)
    w = np.asarray(sample_weight, dtype="float64")
    if len(y_arr) != len(p) or len(p) != len(w):
        raise OOFGenerationError("Weighted log-loss inputs have mismatched lengths.")
    if not np.isfinite(w).all() or (w < 0.0).any() or float(w.sum()) <= 0.0:
        raise OOFGenerationError("Weighted log-loss sample weights are invalid.")
    loss = -(y_arr * np.log(p) + (1.0 - y_arr) * np.log(1.0 - p))
    return float(np.average(loss, weights=w))



def compute_class_weights(y: pd.Series, class_weight_mode: str) -> np.ndarray:
    """Class imbalance treatment is separate from AFML financial observation weights."""
    if class_weight_mode == "none":
        return np.ones(len(y), dtype=float)
    if class_weight_mode != "balanced":
        raise OOFGenerationError(
            f"Unsupported class_weight_mode={class_weight_mode!r} after AFML Ch.4 patch; "
            "rerun Step 02 so best params use the fixed 'balanced' mode."
        )
    return compute_sample_weight(class_weight="balanced", y=y)



def make_train_weights(df: pd.DataFrame, y: pd.Series, train_idx: np.ndarray, class_weight_mode: str) -> np.ndarray:
    """Fit weight = canonical AFML return-attribution weight x time decay; class balance is the estimator's."""
    train_idx = np.asarray(train_idx, dtype=int)
    y_train = y.iloc[train_idx]
    train_df = df.iloc[train_idx]
    # AFML s.4.8 (p.71-72): class balance is applied ONCE, by the estimator's class_weight='balanced' (recomputed on
    # every sequential-bootstrap draw = 'balanced_subsample'). Multiplying compute_sample_weight('balanced') in here
    # as well squared the balancing (fixed 2026-10-23, Ch.4 walkthrough); compute_class_weights only validates mode.
    compute_class_weights(y_train, class_weight_mode=class_weight_mode)
    financial_w = _afml_return_weights(train_df)
    final_w = np.asarray(financial_w, dtype="float64") * _time_decay_factors(train_df.index, compute_uniqueness(train_df))
    mean_w = float(np.mean(final_w)) if len(final_w) else 0.0
    if not np.isfinite(final_w).all() or not np.isfinite(mean_w) or mean_w <= 0.0:
        raise OOFGenerationError("Training sample weights are invalid/non-positive after AFML construction.")
    return final_w / mean_w



def flatten_cv_indices(idx: Any) -> np.ndarray:
    if isinstance(idx, np.ndarray):
        return np.unique(idx.astype(int))
    if hasattr(idx, "__iter__"):
        parts = []
        for part in idx:
            parts.append(np.atleast_1d(part).astype(int))
        return np.unique(np.concatenate(parts)) if parts else np.array([], dtype=int)
    return np.array([int(idx)])


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


def get_positive_class_proba(model: Any, x_data: pd.DataFrame) -> np.ndarray:
    proba = model.predict_proba(x_data)
    if proba.shape[1] == 1:
        cls = int(model.classes_[0])
        return np.ones(len(x_data), dtype=float) if cls == 1 else np.zeros(len(x_data), dtype=float)
    class_to_col = {int(cls): i for i, cls in enumerate(model.classes_)}
    if 1 not in class_to_col:
        return np.zeros(len(x_data), dtype=float)
    return proba[:, class_to_col[1]]


def find_best_f1_threshold(y_true: pd.Series, proba: np.ndarray) -> tuple[float, float]:
    y_arr = np.asarray(y_true).astype(int)
    best_thr = 0.50
    best_f1 = -np.inf
    for thr in F1_THRESHOLD_GRID:
        pred = (proba >= thr).astype(int)
        score = f1_score(y_arr, pred, zero_division=0)
        if score > best_f1:
            best_f1 = float(score)
            best_thr = float(thr)
    return best_thr, best_f1


def classification_metrics_at_threshold(y_true: pd.Series, proba: np.ndarray, threshold: float) -> dict[str, float]:
    pred = (np.asarray(proba) >= threshold).astype(int)
    y_arr = np.asarray(y_true).astype(int)
    return {
        "precision": float(precision_score(y_arr, pred, zero_division=0)),
        "recall": float(recall_score(y_arr, pred, zero_division=0)),
        "f1": float(f1_score(y_arr, pred, zero_division=0)),
        "accuracy": float(accuracy_score(y_arr, pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y_arr, pred)),
        "predicted_positive_rate": float(np.mean(pred)),
    }


# QUANTARA_AFML_CH12_CPCV_PATHS_V1
# AFML s.12.4.2 step 5 (pp.165-166), owner decision D052 (2026-10-26): keep every CPCV backtest path.
CPCV_PATH_COLUMN_TEMPLATE = "cpcv_path_{:02d}_raw_probability"


def assign_cpcv_path_predictions(path_probs: np.ndarray, path_ids: np.ndarray, split_index: int,
                                 test_parts: list, test_idx: np.ndarray, preds: np.ndarray) -> None:
    """Write one split's test forecasts into its paths: test group j of split s belongs to path path_ids[s, j]."""
    by_row = dict(zip(np.asarray(test_idx, dtype=int).tolist(), np.asarray(preds, dtype=float).tolist()))
    for j, part in enumerate(test_parts):
        rows = np.atleast_1d(np.asarray(part, dtype=int))
        path_probs[int(path_ids[split_index, j]), rows] = [by_row[int(r)] for r in rows]
# END_QUANTARA_AFML_CH12_CPCV_PATHS_V1


def generate_oof_predictions(df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series, best_params: dict[str, Any]):
    if CombinatorialPurgedCV is None:
        raise OOFGenerationError(
            "skfolio.model_selection.CombinatorialPurgedCV is required. "
            f"Original import error: {SKFOLIO_IMPORT_ERROR}"
        )
    class_weight_mode = best_params.get("class_weight_mode", "balanced")
    cv = CombinatorialPurgedCV(
        n_folds=N_FOLDS,
        n_test_folds=N_TEST_FOLDS,
        purged_size=PURGED_SIZE,
        embargo_size=EMBARGO_SIZE,
    )
    prob_sum = np.zeros(len(df), dtype=float)
    prob_count = np.zeros(len(df), dtype=int)
    path_ids = np.asarray(cv.get_path_ids())
    path_probs = np.full((int(cv.n_test_paths), len(df)), np.nan, dtype=float)
    fold_rows = []
    fi_rows = []

    print("Generating CPCV OOF raw probabilities...", flush=True)
    for fold_num, (train_idx, test_idx) in enumerate(cv.split(x_data), start=1):
        test_parts = list(test_idx) if isinstance(test_idx, (list, tuple)) else [test_idx]
        raw_train_rows = len(flatten_cv_indices(train_idx))
        raw_test_rows = len(flatten_cv_indices(test_idx))
        train_idx = flatten_cv_indices(train_idx)
        test_idx = flatten_cv_indices(test_idx)
        train_idx = purge_train_overlaps(train_idx, test_idx, df.index, pd.Series(_label_end_times(df), index=df.index))
        train_idx = apply_embargo(train_idx, test_idx, len(df), embargo_size_for(len(df)))
        if len(train_idx) == 0 or len(test_idx) == 0:
            print(f"Fold {fold_num:02d}: skipped after purge/embargo", flush=True)
            continue
        x_train, x_test = x_data.iloc[train_idx], x_data.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
        if y_train.nunique() < 2:
            print(f"Fold {fold_num:02d}: skipped because train has one class", flush=True)
            continue
        weights = make_train_weights(df=df, y=y, train_idx=train_idx, class_weight_mode=class_weight_mode)
        model = make_bagged_model(best_params, df.iloc[train_idx], x_train, y_train, weights)
        train_prob = get_positive_class_proba(model, x_train)
        train_thr, train_f1 = find_best_f1_threshold(y_train, train_prob)
        preds = get_positive_class_proba(model, x_test)
        fold_clf = classification_metrics_at_threshold(y_test, preds, train_thr)
        try:
            fold_log_loss = float(log_loss(y_test, preds, labels=[0, 1]))
        except Exception:
            fold_log_loss = np.nan
        prob_sum[test_idx] += preds
        prob_count[test_idx] += 1
        assign_cpcv_path_predictions(path_probs, path_ids, fold_num - 1, test_parts, test_idx, preds)
        fold_rows.append(
            {
                "fold": int(fold_num),
                "raw_train_rows": int(raw_train_rows),
                "raw_test_rows": int(raw_test_rows),
                "train_size_after_purge_embargo": int(len(train_idx)),
                "test_size": int(len(test_idx)),
                "train_selected_f1_threshold": float(train_thr),
                "train_f1_at_selected_threshold": float(train_f1),
                "test_f1_at_train_threshold": fold_clf["f1"],
                "test_precision_at_train_threshold": fold_clf["precision"],
                "test_recall_at_train_threshold": fold_clf["recall"],
                "test_accuracy_at_train_threshold": fold_clf["accuracy"],
                "test_balanced_accuracy_at_train_threshold": fold_clf["balanced_accuracy"],
                "test_predicted_positive_rate": fold_clf["predicted_positive_rate"],
                "log_loss": fold_log_loss,
            }
        )
        fi_rows.append(pd.DataFrame({"feature": x_data.columns, "importance": model.feature_importances_, "fold": fold_num}))
        print(
            f"Fold {fold_num:02d}: train={len(train_idx):,} test={len(test_idx):,} "
            f"thr={train_thr:.2f} train_f1={train_f1:.4f} test_f1={fold_clf['f1']:.4f} "
            f"precision={fold_clf['precision']:.4f} recall={fold_clf['recall']:.4f} "
            f"logloss={fold_log_loss:.5f}",
            flush=True,
        )

    raw_probability = np.where(prob_count > 0, prob_sum / np.maximum(prob_count, 1), np.nan)
    oof_df = df.copy()
    oof_df[RAW_PROB_COL] = raw_probability
    oof_df["oof_pred_count"] = prob_count
    for path_no in range(path_probs.shape[0]):  # AFML s.12.4.2 step 5: every path kept (D052)
        oof_df[CPCV_PATH_COLUMN_TEMPLATE.format(path_no)] = path_probs[path_no]
    fold_df = pd.DataFrame(fold_rows)
    fi_df = pd.concat(fi_rows, ignore_index=True) if fi_rows else pd.DataFrame(columns=["feature", "importance", "fold"])
    return oof_df, fold_df, fi_df


def compute_global_oof_metrics(oof_df: pd.DataFrame) -> dict[str, Any]:
    valid = oof_df.dropna(subset=[RAW_PROB_COL]).copy()
    if valid.empty:
        raise OOFGenerationError("No rows with OOF predictions.")
    y_true = valid[TARGET_COL].values.astype(int)
    y_prob = valid[RAW_PROB_COL].values.astype(float)
    metrics = {
        "rows_with_oof_prediction": int(len(valid)),
        "total_rows": int(len(oof_df)),
        "coverage_rate": float(len(valid) / len(oof_df)),
        "base_positive_rate": float(valid[TARGET_COL].mean()),
        "brier_score": float(brier_score_loss(y_true, y_prob)),
    }
    for name, fn in [
        ("log_loss", lambda: log_loss(y_true, y_prob, labels=[0, 1])),
        ("roc_auc", lambda: roc_auc_score(y_true, y_prob)),
        ("pr_auc", lambda: average_precision_score(y_true, y_prob)),
    ]:
        try:
            metrics[name] = float(fn())
        except Exception:
            metrics[name] = np.nan
    best_thr, best_f1 = find_best_f1_threshold(valid[TARGET_COL], y_prob)
    clf = classification_metrics_at_threshold(valid[TARGET_COL], y_prob, best_thr)
    metrics.update(
        {
            "global_best_f1_threshold_diagnostic": float(best_thr),
            "global_best_f1_diagnostic": float(best_f1),
            "global_precision_at_best_f1_threshold": clf["precision"],
            "global_recall_at_best_f1_threshold": clf["recall"],
            "global_accuracy_at_best_f1_threshold": clf["accuracy"],
            "global_balanced_accuracy_at_best_f1_threshold": clf["balanced_accuracy"],
            "note": "Raw-probability diagnostics only. Deployable threshold must use calibrated_probability in Step 04.",
        }
    )
    return metrics


def make_calibration_bins(oof_df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    valid = oof_df.dropna(subset=[RAW_PROB_COL]).copy()
    valid["prob_bin"] = pd.qcut(valid[RAW_PROB_COL], q=n_bins, duplicates="drop")
    return valid.groupby("prob_bin", observed=False).agg(
        count=(TARGET_COL, "size"),
        avg_raw_probability=(RAW_PROB_COL, "mean"),
        actual_positive_rate=(TARGET_COL, "mean"),
    ).reset_index()


def safe_group_report(oof_df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    valid = oof_df.dropna(subset=[RAW_PROB_COL]).copy()
    if group_col not in valid.columns:
        return pd.DataFrame()
    rows = []
    for group, part in valid.groupby(group_col, observed=False):
        y_true = part[TARGET_COL].values.astype(int)
        y_prob = part[RAW_PROB_COL].values.astype(float)
        row = {
            group_col: group,
            "rows": int(len(part)),
            "positive_rate": float(part[TARGET_COL].mean()),
            "avg_raw_probability": float(part[RAW_PROB_COL].mean()),
            "brier_score": float(brier_score_loss(y_true, y_prob)) if len(part) else np.nan,
        }
        try:
            row["roc_auc"] = float(roc_auc_score(y_true, y_prob)) if len(np.unique(y_true)) == 2 else np.nan
        except Exception:
            row["roc_auc"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def write_outputs(oof_df: pd.DataFrame, fold_df: pd.DataFrame, fi_df: pd.DataFrame, metrics: dict[str, Any], selected_features: list[str], best_params: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_oof = oof_df.reset_index().rename(columns={oof_df.index.name or "index": TIME_COL})
    out_oof.to_csv(OOF_PREDICTIONS_OUTPUT, index=False)
    fold_df.to_csv(OOF_FOLD_REPORT_OUTPUT, index=False)
    if not fi_df.empty:
        fi_df.to_csv(OOF_FEATURE_IMPORTANCE_OUTPUT, index=False)
    save_json(OOF_GLOBAL_METRICS_OUTPUT, metrics)
    make_calibration_bins(oof_df).to_csv(OOF_RAW_CALIBRATION_BINS_OUTPUT, index=False)

    by_year = oof_df.dropna(subset=[RAW_PROB_COL]).copy()
    by_year["year"] = by_year.index.year
    safe_group_report(by_year, "year").to_csv(OOF_BY_YEAR_OUTPUT, index=False)
    safe_group_report(oof_df, "Regime_State").to_csv(OOF_BY_REGIME_OUTPUT, index=False)
    safe_group_report(oof_df, "barrier_hit").to_csv(OOF_BY_BARRIER_OUTPUT, index=False)

    afml_provenance = _afml_provenance_snapshot()

    manifest = {
        "created_at": now_utc(),
        "edge_id": EDGE_ID,
        "run_mode": RUN_MODE,
        "script": display_path(THIS_FILE),
        "blueprint_stage": "oof_raw_probability_generation_before_sigmoid_calibration",
        "feature_engineering_performed_here": False,
        "input_model_rows": display_path(RESOLUTION_CSV_PATH),
        "input_selected_features": display_path(SELECTED_FEATURES_PATH),
        "input_best_params": display_path(BEST_PARAMS_PATH),
        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),
        "afml_provenance": afml_provenance,
        "selected_feature_count": int(len(selected_features)),
        "selected_features": selected_features,
        "best_params": best_params,
        "raw_probability_column": RAW_PROB_COL,
        "side_specialization": {"edge_side": SIDE, "side_value": SIDE_VALUE, "model_learns_direction": False},
        "deployable": False,
        "deployability_blockers": [
            "sigmoid calibration not yet complete",
            "calibrated threshold sweep not yet complete",
            "runtime.py::score not yet wired to model/calibrator artifacts",
            "deployability replay audit not yet passed",
        ],
        "oof_metrics": metrics,
        "cpcv": {
            "n_folds": N_FOLDS,
            "n_test_folds": N_TEST_FOLDS,
            "n_paths": int(sum(1 for c in oof_df.columns if str(c).startswith("cpcv_path_"))),
            "path_columns": sorted(c for c in oof_df.columns if str(c).startswith("cpcv_path_")),
            "path_policy": "every CPCV path kept (AFML s.12.4.2 step 5, D052); raw_probability = average of paths, diagnostic",
            "purged_size": PURGED_SIZE,
            "embargo_size": EMBARGO_SIZE,
            "manual_embargo_size": MANUAL_EMBARGO_SIZE,
            "embargo_rule": EMBARGO_RULE,
            "purge_t1": PURGE_T1_RULE,
            "mode": "manual_t1_overlap_purge_after_cpcv_split_generation",
        },
        "outputs": {
            "oof_predictions": display_path(OOF_PREDICTIONS_OUTPUT),
            "fold_report": display_path(OOF_FOLD_REPORT_OUTPUT),
            "feature_importance": display_path(OOF_FEATURE_IMPORTANCE_OUTPUT),
            "global_metrics": display_path(OOF_GLOBAL_METRICS_OUTPUT),
            "raw_probability_calibration_bins": display_path(OOF_RAW_CALIBRATION_BINS_OUTPUT),
            "by_year": display_path(OOF_BY_YEAR_OUTPUT),
            "by_regime": display_path(OOF_BY_REGIME_OUTPUT),
            "by_barrier": display_path(OOF_BY_BARRIER_OUTPUT),
            "manifest": display_path(MANIFEST_OUTPUT),
        },
        "next_ordered_step": "03b_oof_threshold_sweep.py",
        "notes": [
            "This script ports the raw OOF probability generation part of V1 Step 7 into V2.",
            "It consumes Task 2A's execution-resolution CSV, Step 01 selected features, and Step 02 best params only.",
            "It does not calibrate probability.",
            "It does not select deployable live threshold.",
            "Step 03A is retired (redundant with Task 2A); this script's OOF output already carries "
            "Task 2A's execution-provenance columns forward for Step 04 to consume directly.",
            "Step 04 must fit sigmoid calibrator and run calibrated_probability threshold sweep.",
        ],
    }
    manifest.update({
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": GROSS_TARGET_COL,
        "gross_realized_r_column": GROSS_REALIZED_R_COL,
        "gross_target_definition": GROSS_TARGET_DEFINITION,
        "training_target_column": TARGET_COL,
        "training_eligibility_column": ELIGIBILITY_COL,
        "training_eligibility_filter": _LAST_ELIGIBILITY_FILTER_COUNTS,
        "step01_feature_surgery_manifest_sha256": _phase3a_sha256_file(STEP01_FEATURE_SURGERY_MANIFEST_PATH),
        "step02_training_manifest_sha256": _phase3a_sha256_file(STEP02_TRAINING_MANIFEST_PATH),
        "model_rows_sha256": _phase3a_sha256_file(RESOLUTION_CSV_PATH),
        "selected_features_sha256": _phase3a_sha256_file(SELECTED_FEATURES_PATH),
        "best_params_sha256": _phase3a_sha256_file(BEST_PARAMS_PATH),
        "feature_schema_sha256": _phase3a_sha256_file(FEATURE_SCHEMA_PATH),
        "oof_predictions_sha256": _phase3a_sha256_file(OOF_PREDICTIONS_OUTPUT),
        "afml_sample_weight_contract_version": afml_provenance["contract_version"],
        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],
        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],
        "phase3a_provenance_status": "PASS",
    })
    save_json(MANIFEST_OUTPUT, manifest)


def main() -> None:
    print("=" * 80, flush=True)
    print(f"V2 STEP 03: OOF RAW-PROBABILITY GENERATION — {EDGE_ID}", flush=True)
    print("=" * 80, flush=True)
    print(f"Run mode: {RUN_MODE}", flush=True)
    print(f"Working directory: {CWD}", flush=True)
    print(f"Model rows: {RESOLUTION_CSV_PATH}", flush=True)
    print(f"Selected features: {SELECTED_FEATURES_PATH}", flush=True)
    print(f"Best params: {BEST_PARAMS_PATH}", flush=True)
    print(f"Feature schema: {FEATURE_SCHEMA_PATH}", flush=True)
    print(f"Output directory: {OUTPUT_DIR}", flush=True)
    preflight_check()
    _phase3a_validate_step02_training_manifest()
    print("Preflight + Step-02 semantic provenance: PASS", flush=True)

    selected_features = load_selected_features()
    best_params = load_json(BEST_PARAMS_PATH)
    df = load_dataset(selected_features)
    x_data = df[selected_features].copy()
    y = df[TARGET_COL].copy()
    print(f"Date range: {df.index.min()} -> {df.index.max()}", flush=True)
    print(f"Rows used: {len(df):,}", flush=True)
    print(f"Selected feature count: {len(selected_features):,}", flush=True)
    print(f"Target mean: {y.mean():.6f}", flush=True)
    print(f"CPCV: C({N_FOLDS}, {N_TEST_FOLDS}) splits; every backtest path kept separately (AFML s.12.4, D052)", flush=True)
    print("Raw_probability is diagnostic/intermediate only. Step 04 handles sigmoid calibration and threshold sweep.", flush=True)

    oof_df, fold_df, fi_df = generate_oof_predictions(df, x_data, y, best_params)
    metrics = compute_global_oof_metrics(oof_df)
    write_outputs(oof_df, fold_df, fi_df, metrics, selected_features, best_params)

    print("\n✅ V2 OOF raw-probability generation completed.", flush=True)
    print(f"Rows with OOF prediction: {metrics['rows_with_oof_prediction']:,} / {metrics['total_rows']:,}", flush=True)
    print(f"Coverage rate: {metrics['coverage_rate']:.4f}", flush=True)
    print(f"Raw log loss: {metrics.get('log_loss', np.nan):.6f}", flush=True)
    print(f"Raw Brier: {metrics.get('brier_score', np.nan):.6f}", flush=True)
    print(f"Raw ROC AUC: {metrics.get('roc_auc', np.nan):.6f}", flush=True)
    print(f"Raw PR AUC: {metrics.get('pr_auc', np.nan):.6f}", flush=True)
    print(f"Diagnostic global F1 threshold: {metrics['global_best_f1_threshold_diagnostic']}", flush=True)
    print(f"Saved OOF predictions: {OOF_PREDICTIONS_OUTPUT}", flush=True)
    print(f"Saved manifest: {MANIFEST_OUTPUT}", flush=True)
    print("Next ordered step: 03b_oof_threshold_sweep.py", flush=True)

    if RUN_MODE == "flat_colab":
        print("\nFlat Colab output files to download:", flush=True)
        for path in [
            OOF_PREDICTIONS_OUTPUT,
            OOF_FOLD_REPORT_OUTPUT,
            OOF_FEATURE_IMPORTANCE_OUTPUT,
            OOF_GLOBAL_METRICS_OUTPUT,
            OOF_RAW_CALIBRATION_BINS_OUTPUT,
            OOF_BY_YEAR_OUTPUT,
            OOF_BY_REGIME_OUTPUT,
            OOF_BY_BARRIER_OUTPUT,
            MANIFEST_OUTPUT,
        ]:
            print(f"  - {path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except OOFGenerationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
