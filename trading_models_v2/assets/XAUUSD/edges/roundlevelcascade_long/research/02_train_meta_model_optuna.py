"""
02_train_meta_model_optuna.py

V2 Step 02: V1-style final meta-model training with Optuna for the current edge package.

Blueprint boundary
------------------
This script trains the final raw-probability meta-model using selected features
from Step 01. It does NOT calibrate probability and does NOT select the final
live trading threshold.

Two supported run modes
-----------------------
1) Repo mode, from trading_models_v2:

    python ./assets/<ASSET>/edges/<FAMILY>_<SIDE>/research/02_train_meta_model_optuna.py

2) Flat Colab mode, from /content:

    Upload these files directly into /content:
        02_train_meta_model_optuna.py
        <edge_stem>_mldp_execution_resolution.csv
        <edge_stem>_selected_features.json
        feature_surgery_manifest.json
        feature_schema.json
        <asset>_standard_quote_tick_bars.csv

    Then run:
        !python /content/02_train_meta_model_optuna.py

    Output files will be written directly into /content for easy download.

MLDP target migration (Task 2C)
--------------------------------
Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md and
docs/mldp/TASK2B_POPULATION_RULE_DECISION.md, the frozen training target is
quote_executable_meta_label restricted to mldp_target_eligible==True rows
(execution resolved before training, no post-hoc retargeting). This script
now consumes Task 2A's execution-resolution CSV directly instead of the
plain model-rows CSV. Per docs/mldp/A6_TRIAL_LEDGER_LOGGING.md, this script
also writes one research-trial ledger record per Optuna study (not one per
internal trial) to a local JSONL file that travels back with the rest of
this station's downloaded artifacts for later merge into the canonical
ledger via shared.research.trial_ledger.merge_trial_records.

V1 logic preserved
------------------
- selected features from Step 01
- Optuna tuning (searches DecisionTreeClassifier hyperparameters; see
  AFML migration note below for why the model itself changed)
- CPCV split generation
- manual t1 overlap purge
- manual embargo
- AFML Ch.4 sample weight = fixed class balancing * normalized absolute concurrency-adjusted log-return attribution
- average uniqueness is computed separately on the standard_quote_tick bar clock
- train-fold-only F1 threshold selection for CPCV objective
- final model trained on all selected-feature rows

AFML Ch.6/Ch.4 SS4.5 migration (bagging, not boosting)
-------------------------------------------------------
Per AFML SS6.6 (p.100-101): "It could be argued that in financial
applications bagging is generally preferable to boosting... because of the
low signal-to-noise ratio." The single-XGBoost-model-refit-on-100%-of-data
architecture this script used previously was found to diverge sharply from
its own OOF estimate (measured: ~0.15 mean raw-probability shift on real
edges) -- an in-sample-vs-OOF gap the book's whole CV discipline exists to
prevent. This script now follows AFML Snippet 9.1's clfHyperFit two-stage
pattern exactly: Optuna tunes a single DecisionTreeClassifier's
hyperparameters via the SAME purged CPCV used below (stage 1), then the
validated hyperparameters are used to build a bagging ensemble of that tree
via AFML Ch.4 SS4.5's sequential bootstrap -- "third and better [best]"
solution to bagging under overlapping financial labels -- for the deployed
final model (stage 2, AFML Snippet 6.2's clf1 configuration). See
shared/models/sequential_bootstrap.py and
shared/models/sequential_bootstrap_bagging.py for the canonical reference
implementation this section is inlined from (Colab cannot import shared/).

V2 additions
------------
- flat Colab mode
- feature_schema.json validation
- explicit trial/fold console progress so long Colab runs do not look frozen
- model remains non-deployable until OOF, sigmoid calibration, calibrated
  threshold sweep, runtime score wiring, and replay audit pass
- portable side/barrier contract checks inferred from edge folder or flat filenames
"""

from __future__ import annotations

# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1

# QUANTARA_AFML_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import hashlib
import json
import os
import sys
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import optuna
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[5]))
    from shared.io.timeparse import to_datetime_iso
from sklearn.metrics import f1_score, precision_score, recall_score
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
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
optuna.logging.set_verbosity(optuna.logging.WARNING)

# =============================================================================
# CONFIG
# =============================================================================

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
        "study_name": f"{edge_stem}_meta_model_tree_bagging_f1_optuna",
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
        "study_name": f"{edge_stem}_meta_model_tree_bagging_f1_optuna",
    }


def _single_file(pattern: str, description: str) -> Path | None:
    matches = sorted(CWD.glob(pattern))
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise ValueError(f"Flat mode found multiple {description} files for pattern {pattern!r}: {matches}")
    return matches[0]


RESOLUTION_SUFFIX = "_mldp_execution_resolution.csv"


def _resolve_paths() -> tuple[str, Path, Path, Path, Path, Path, dict[str, Any]]:
    flat_resolution = _single_file(f"*{RESOLUTION_SUFFIX}", "MLDP executable-target resolution")
    flat_selected_features = _single_file("*_selected_features.json", "selected-feature")
    flat_feature_schema = CWD / "feature_schema.json"
    if _looks_like_colab_root(CWD) and flat_resolution is not None and flat_selected_features is not None and flat_feature_schema.exists():
        rows_stem = flat_resolution.name.removesuffix(RESOLUTION_SUFFIX)
        features_stem = flat_selected_features.name.removesuffix("_selected_features.json")
        if rows_stem != features_stem:
            raise ValueError(
                f"Flat mode edge stem mismatch: resolution stem={rows_stem!r}, selected features stem={features_stem!r}"
            )
        context = _context_from_edge_stem(rows_stem)
        return "flat_colab", CWD, CWD, flat_resolution, flat_selected_features, flat_feature_schema, context

    edge_root = THIS_FILE.parents[1]
    v2_root = THIS_FILE.parents[5]
    context = _context_from_edge_root(edge_root)
    edge_stem = context["edge_stem"]
    resolution_path = edge_root / "artifacts" / "research_dataset" / f"{edge_stem}{RESOLUTION_SUFFIX}"
    selected_features_path = edge_root / "artifacts" / "feature_surgery" / f"{edge_stem}_selected_features.json"
    feature_schema_path = edge_root / "contracts" / "feature_schema.json"
    return "repo", v2_root, edge_root, resolution_path, selected_features_path, feature_schema_path, context


RUN_MODE, V2_ROOT, EDGE_ROOT, RESOLUTION_CSV_PATH, SELECTED_FEATURES_PATH, FEATURE_SCHEMA_PATH, EDGE_CONTEXT = _resolve_paths()
ASSET = EDGE_CONTEXT["asset"]
FAMILY = EDGE_CONTEXT["family"]
SIDE = EDGE_CONTEXT["side"]
SIDE_VALUE = EDGE_CONTEXT["side_value"]
EDGE_ID = EDGE_CONTEXT["edge_id"]
EDGE_STEM = EDGE_CONTEXT["edge_stem"]
STUDY_NAME = EDGE_CONTEXT["study_name"]

OUTPUT_DIR = CWD if RUN_MODE == "flat_colab" else EDGE_ROOT / "artifacts" / "model"

MODEL_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_meta_model.pkl"
BEST_PARAMS_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_meta_model_best_params.json"
FEATURE_IMPORTANCE_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_meta_model_feature_importance.csv"
OPTUNA_TRIALS_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_meta_model_optuna_trials.csv"
MANIFEST_OUTPUT = OUTPUT_DIR / "model_training_manifest.json"
TRIAL_LEDGER_OUTPUT = OUTPUT_DIR / f"{EDGE_STEM}_research_trial_ledger.jsonl"

# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md, the frozen
# training target is quote_executable_meta_label, restricted below to
# mldp_target_eligible==True rows (Task 2B's population rule). The old gross
# label (meta_label/realized_r, now duplicated under canonical names
# gross_meta_label/gross_r/gross_t1 in Task 2A's resolution CSV) is retained
# ONLY for an internal data-integrity check -- it is never used as the
# training target.
TARGET_COL = "quote_executable_meta_label"
ELIGIBILITY_COL = "mldp_target_eligible"
GROSS_TARGET_COL = "gross_meta_label"
GROSS_REALIZED_R_COL = "gross_r"
GROSS_LABEL_CONTRACT_VERSION = "quantara_v3_gross_economic_meta_label_v1"
GROSS_TARGET_DEFINITION = "int(gross_r > 0)"
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

# CPCV split generation only. Actual t1 overlap purge is manual below.
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

# Optuna. Keep V1 default as 30. In Colab, edit N_TRIALS lower if debugging.
N_TRIALS = 30
OPTUNA_TIMEOUT = None
PRINT_FOLD_PROGRESS = True
PRINT_PARAMS_PER_TRIAL = True

F1_THRESHOLD_GRID = np.round(np.arange(0.30, 0.81, 0.01), 2)
RANDOM_STATE = 42

# Pilot validation value, not yet AFML's own illustrative 1000 (Snippet 6.2) --
# see the printed rho_bar diagnostic at the end of this script for a
# data-driven read on whether raising this would meaningfully help, per
# AFML's own variance formula V[bagged] = sigma_bar^2*(rho_bar+(1-rho_bar)/N).
N_ESTIMATORS_BAGGING = 1000  # AFML Snippet 6.2 (owner decision 2026-10-24, Ch.6 walkthrough)
# Optuna search space for the Snippet 6.2 base tree (one dict drives the objective AND the trial-ledger record).
# AFML Ch.9 walkthrough 2026-10-24: tuning CV = Snippet 9.1 purged k-fold; score = F1 at the classifier's own decision
# (Snippet 9.1/9.3 scoring='f1' for meta-labeling, p.130); randomized search (s.9.3); max_depth range widened to 1.
TUNING_N_SPLITS = 5
SCORING_DECISION_THRESHOLD = 0.5
OPTUNA_SEARCH_SPACE = {
    "max_depth": (1, 8),
    "min_samples_leaf": (5, 200),
    "min_samples_split": (10, 400),
    "min_weight_fraction_leaf": (0.0, 0.05),
    "ccp_alpha": (1e-6, 1e-2),
}


class MetaModelTrainingError(RuntimeError):
    """Raised when Step 02 must fail closed."""


# =============================================================================
# AFML Ch.6 (Ensemble Methods, p.93-102) + Ch.4 SS4.5 (Bagging Classifiers and
# Uniqueness, p.61-66) sequential-bootstrap bagging ensemble.
# =============================================================================
# Verbatim from shared/models/sequential_bootstrap.py and
# shared/models/sequential_bootstrap_bagging.py. Inlined rather than imported
# because this script runs standalone on Google Colab and cannot import the
# rest of `shared/` at runtime -- see docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's
# "Operational note" for the same constraint applied to purge/embargo.
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
    """AFML u_t,j = 1_t,j / (1 + sum_{k in phi} 1_t,k) (p.63), averaged over j's
    lifespan, for every candidate j simultaneously, given the CURRENT sample's
    per-bar concurrency."""
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

    def mean_pairwise_tree_correlation(self, x_data: Any) -> tuple[float, int]:
        """AFML's rho_bar (SS6.3.1, p.95). Returns (rho_bar, n_degenerate_
        trees_excluded) -- a heavily regularized tree can legitimately end up
        a single-leaf stump (constant prediction, undefined/NaN correlation),
        observed on real data; excluded from the average rather than
        silently propagating NaN. See shared/models/sequential_bootstrap_bagging.py."""
        if len(self.estimators_) < 2:
            raise SequentialBootstrapError("Need at least 2 estimators to compute pairwise correlation.")
        x_arr = np.asarray(x_data)
        pos_col = int(np.where(self.classes_ == 1)[0][0]) if 1 in self.classes_ else 1
        preds = np.stack([tree.predict_proba(x_arr)[:, pos_col] for tree in self.estimators_], axis=0)
        non_degenerate = preds.std(axis=1) > 0.0
        n_excluded = int((~non_degenerate).sum())
        preds = preds[non_degenerate]
        if preds.shape[0] < 2:
            raise SequentialBootstrapError(
                f"Only {preds.shape[0]} of {len(self.estimators_)} bagged trees have non-constant "
                "predictions; cannot compute a pairwise correlation."
            )
        corr = np.corrcoef(preds)
        n = corr.shape[0]
        off_diag = corr[~np.eye(n, dtype=bool)]
        return float(np.mean(off_diag)), n_excluded


PORTABLE_PAYLOAD_MARKER_KEY = "quantara_sequential_bootstrap_bagging_portable_v1"


def bagging_to_portable_payload(clf: SequentialBootstrapBaggingClassifier) -> dict[str, Any]:
    """Convert a fitted ensemble to a plain dict before joblib.dump, instead
    of pickling the SequentialBootstrapBaggingClassifier instance directly.

    CRITICAL: this script runs standalone on Colab and defines its own
    inline copy of this class (cannot import shared/ at runtime). If you
    joblib.dump the class instance directly, pickle records the class as
    __main__.SequentialBootstrapBaggingClassifier (this script's own run
    context); no other process (Step05-09, shared/scoring/edge_scorer.py,
    live scoring) can resolve that at load time --
    AttributeError: Can't get attribute 'SequentialBootstrapBaggingClassifier'
    on <module '__main__'>. Overriding __module__ on the inline class does
    NOT fix this either -- pickle's save_global does a strict identity
    check (the inline class object is never `is` the canonical shared/
    class object) and raises PicklingError; confirmed by direct test before
    settling on this approach. The actual fix: persist only the ensemble's
    real, portable contents (a list of genuine sklearn DecisionTreeClassifier
    instances, which pickle identically everywhere) and reconstruct a real
    instance of the canonical shared/models/sequential_bootstrap_bagging.py
    class at load time -- shared/scoring/edge_scorer.py's load_model()
    already does this reconstruction, since it already imports that module.
    """
    return {
        PORTABLE_PAYLOAD_MARKER_KEY: True,
        "tree_params": clf.tree_params,
        "n_estimators": clf.n_estimators,
        "random_state": clf.random_state,
        "estimators_": clf.estimators_,
        "classes_": clf.classes_,
        "feature_importances_": getattr(clf, "feature_importances_", None),
    }


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


def load_json(path: Path) -> Any:
    if not path.exists():
        raise MetaModelTrainingError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def make_tree(params: dict[str, Any]) -> DecisionTreeClassifier:
    """AFML Snippet 6.2's clf1 base estimator (criterion='entropy',
    class_weight='balanced'; max_features='auto' is 'sqrt' in current sklearn,
    an exact renaming, not a deviation). This is the estimator Optuna tunes
    via purged CPCV (AFML Ch.9 Snippet 9.1's stage 1) -- a single tree per
    fold for search speed. The bagging ensemble (stage 2) is only built once,
    for the final deployed model, in train_final_model below."""
    tree_params = {k: v for k, v in params.items() if k != "class_weight_mode"}
    return DecisionTreeClassifier(
        criterion="entropy",
        max_features="sqrt",
        class_weight="balanced",
        random_state=RANDOM_STATE,
        **tree_params,
    )


def _event_bar_positions(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, int]:
    """Bar-clock positions of each row's [Datetime, gross_t1] span, needed by
    sequential_bootstrap. Reuses the same standard_quote_tick bar clock and
    mapping already used for AFML sample weights above (_compute_afml_event_
    weight_table_local), so this never re-reads/re-parses the bars file."""
    bars_path = _resolve_standard_quote_tick_bars_path()
    bars = pd.read_csv(bars_path, usecols=[TIME_COL])
    bars[TIME_COL] = to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce")
    if bars[TIME_COL].isna().any():
        raise MetaModelTrainingError("standard_quote_tick bars contain invalid Datetime values.")
    if bars[TIME_COL].duplicated().any():
        raise MetaModelTrainingError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[TIME_COL].is_monotonic_increasing:
        raise MetaModelTrainingError("standard_quote_tick bars are not chronologically sorted.")
    bar_index = pd.DatetimeIndex(bars[TIME_COL])

    starts = pd.DatetimeIndex(to_datetime_iso(df.index, utc=True, errors="coerce"))
    ends = _label_end_times(df)
    if starts.isna().any() or ends.isna().any():
        raise MetaModelTrainingError("Rows contain invalid Datetime/gross_t1 for bar-position mapping.")

    start_pos = bar_index.get_indexer(starts)
    end_pos = _snap_to_bar_clock(ends, bar_index)
    if (start_pos < 0).any() or (end_pos < 0).any():
        raise MetaModelTrainingError("Some event start/end timestamps are absent from the standard_quote_tick bar clock.")
    if (end_pos < start_pos).any():
        raise MetaModelTrainingError("At least one event maps to an end bar before its start bar.")
    return start_pos.astype("int64"), end_pos.astype("int64"), len(bar_index)


def make_bagged_model(tree_params: dict[str, Any], df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series,
                       sample_weight: np.ndarray, *, random_state: int | None = RANDOM_STATE) -> SequentialBootstrapBaggingClassifier:
    """AFML Ch.9 Snippet 9.1's stage 2: 'fit validated model on the entirety
    of the data' via bagging (AFML Ch.6), sequential-bootstrap resampled
    (AFML Ch.4 SS4.5's 'third and better [best]' solution to bagging under
    overlapping financial labels)."""
    start_pos, end_pos, n_bars = _event_bar_positions(df)
    model = SequentialBootstrapBaggingClassifier(
        tree_params={k: v for k, v in tree_params.items() if k not in ("class_weight_mode",)}
        | {"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced", "random_state": random_state},
        n_estimators=N_ESTIMATORS_BAGGING,
        random_state=random_state,
    )
    model.fit(x_data, y, sample_weight=sample_weight, event_start_pos=start_pos, event_end_pos=end_pos, n_bars=n_bars)
    return model



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
        raise MetaModelTrainingError(f"Cannot hash AFML table; missing columns: {missing}")
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
    Google Colab vs. this step run in a separate Colab session or locally)
    even when the input files and algorithm are provably identical --
    confirmed by cross-checking the Colab-inlined and shared
    implementations against each other on identical local data (zero
    difference) while an exact-hash comparison against a genuinely
    separately-computed table still failed. An exact hash is the wrong
    tool for verifying floating-point results computed in a different
    environment; this tolerance still catches real errors (wrong file,
    wrong algorithm, wrong column) since those produce differences far
    larger than floating-point noise.
    """
    if declared.get("event_count") != current.get("event_count"):
        raise MetaModelTrainingError(
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
                raise MetaModelTrainingError(
                    f"{prior_step_label} AFML provenance mismatch for {field}.{stat}: "
                    f"declared={declared_value!r}, actual={current_value!r}."
                )


def _phase3a_validate_gross_target(df: pd.DataFrame) -> None:
    """Data-integrity check on the GROSS layer only (gross_meta_label must
    equal int(gross_r > 0) for every row). This never touches the
    executable-target training label; it exists purely to catch a corrupted
    or stale resolution CSV before Step 02 proceeds."""
    if GROSS_REALIZED_R_COL not in df.columns:
        raise MetaModelTrainingError(
            f"{EDGE_ID} resolution rows missing {GROSS_REALIZED_R_COL!r}; "
            "Step 02 requires Task 2A's executable-target resolution CSV."
        )
    realized = pd.to_numeric(df[GROSS_REALIZED_R_COL], errors="coerce")
    target = pd.to_numeric(df[GROSS_TARGET_COL], errors="coerce")
    if realized.isna().any() or target.isna().any():
        raise MetaModelTrainingError(
            f"{EDGE_ID}: gross target provenance contains non-numeric/NaN values."
        )
    expected = (realized > 0.0).astype(int)
    actual = target.astype(int)
    if not actual.equals(expected):
        mismatches = int((actual != expected).sum())
        raise MetaModelTrainingError(
            f"{EDGE_ID}: {GROSS_TARGET_COL} violates {GROSS_LABEL_CONTRACT_VERSION}; "
            f"expected {GROSS_TARGET_DEFINITION}; mismatches={mismatches}."
        )


def _validate_executable_target_eligibility(df: pd.DataFrame) -> None:
    """Task 2B/A2 consistency check: mldp_target_eligible must agree exactly
    with whether quote_executable_meta_label is populated. Any disagreement
    means Task 2A's own invariant was violated upstream -- fail closed rather
    than silently trusting one column over the other."""
    if ELIGIBILITY_COL not in df.columns:
        raise MetaModelTrainingError(
            f"{EDGE_ID} resolution rows missing {ELIGIBILITY_COL!r}; "
            "Step 02 requires Task 2A's executable-target resolution CSV."
        )
    eligible = df[ELIGIBILITY_COL].astype(bool)
    target_present = df[TARGET_COL].notna()
    if not eligible.equals(target_present):
        mismatches = int((eligible != target_present).sum())
        raise MetaModelTrainingError(
            f"{EDGE_ID}: {ELIGIBILITY_COL} disagrees with {TARGET_COL} "
            f"presence for {mismatches} row(s); Task 2A resolution CSV is "
            "internally inconsistent."
        )



def _phase3a1_validate_step01_feature_surgery_manifest() -> dict[str, Any]:
    path = STEP01_FEATURE_SURGERY_MANIFEST_PATH
    if not path.exists():
        raise MetaModelTrainingError(
            f"Missing Step-01 feature-surgery manifest required by Phase 3A.1: {path}. "
            "Run Step 01 and copy feature_surgery_manifest.json with the selected-features artifact."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MetaModelTrainingError(f"Could not read Step-01 manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MetaModelTrainingError("Step-01 feature-surgery manifest must be a JSON object.")
    expected_pairs = {
        "edge_id": EDGE_ID,
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": GROSS_TARGET_COL,
        "gross_target_definition": GROSS_TARGET_DEFINITION,
        "training_target_column": TARGET_COL,
        "training_eligibility_column": ELIGIBILITY_COL,
        "phase3a_step01_provenance_status": "PASS",
    }
    for key, expected in expected_pairs.items():
        if payload.get(key) != expected:
            raise MetaModelTrainingError(
                f"Step-01 provenance mismatch for {key}: expected {expected!r}, got {payload.get(key)!r}."
            )
    checks = {
        "model_rows_sha256": RESOLUTION_CSV_PATH,
        "feature_schema_sha256": FEATURE_SCHEMA_PATH,
        "selected_features_sha256": SELECTED_FEATURES_PATH,
    }
    for key, artifact in checks.items():
        declared = str(payload.get(key, ""))
        actual = _phase3a_sha256_file(artifact)
        if not declared or declared != actual:
            raise MetaModelTrainingError(
                f"Step-01 provenance mismatch for {key}; rerun Step 01 before Step 02."
            )

    afml = payload.get("afml_provenance")
    if not isinstance(afml, dict):
        raise MetaModelTrainingError(
            "Step-01 manifest has no AFML provenance block. Rerun patched Step 01 "
            "or use the guarded manifest backfill before Step 02."
        )
    if afml.get("contract_version") != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
        raise MetaModelTrainingError(
            "Step-01 AFML sample-weight contract mismatch; rerun Step 01 before Step 02."
        )

    current_afml = _afml_provenance_snapshot()
    if str(afml.get("standard_quote_tick_bars_sha256", "")) != current_afml["standard_quote_tick_bars_sha256"]:
        raise MetaModelTrainingError(
            "Step-01 AFML provenance mismatch for standard_quote_tick_bars_sha256. "
            "The bar CSV in this run is not the exact bar file used by Step 01."
        )
    _validate_afml_provenance_numerically(afml, current_afml, prior_step_label="Step-01")
    return payload


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
        raise MetaModelTrainingError("No live-safe allowed model-input features found in feature_schema.json")
    return allowed


def load_selected_features() -> list[str]:
    features = load_json(SELECTED_FEATURES_PATH)
    if not isinstance(features, list) or not features:
        raise MetaModelTrainingError("Selected features JSON must be a non-empty list.")
    features = [str(f) for f in features]
    duplicates = sorted({f for f in features if features.count(f) > 1})
    if duplicates:
        raise MetaModelTrainingError(f"Duplicate selected features found: {duplicates}")
    bad = [f for f in features if f in LEAKAGE_COLS]
    if bad:
        raise MetaModelTrainingError(f"Selected features contain forbidden leakage columns: {bad}")
    allowed = load_allowed_schema_features()
    not_allowed = [f for f in features if f not in allowed]
    if not_allowed:
        raise MetaModelTrainingError(f"Selected features not allowed by feature_schema.json: {not_allowed}")
    return features


def _validate_edge_contract(df: pd.DataFrame) -> None:
    if "edge_id" in df.columns and not (df["edge_id"] == EDGE_ID).all():
        bad = df.loc[df["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise MetaModelTrainingError(f"{EDGE_ID} model rows contain wrong edge_id values: {bad}")
    if "side" in df.columns and not (pd.to_numeric(df["side"], errors="coerce") == SIDE_VALUE).all():
        bad = df.loc[pd.to_numeric(df["side"], errors="coerce") != SIDE_VALUE, "side"].unique().tolist()
        raise MetaModelTrainingError(f"{EDGE_ID} model rows contain wrong side values: {bad}; expected {SIDE_VALUE}")
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
            raise MetaModelTrainingError(f"{EDGE_ID} model rows must have pt_price above entry_price for long edges.")
        if SIDE_VALUE == -1 and not (pt[mask] < entry[mask]).all():
            raise MetaModelTrainingError(f"{EDGE_ID} model rows must have pt_price below entry_price for short edges.")
    if "sl_price" in df.columns and "entry_price" in df.columns:
        sl = pd.to_numeric(df["sl_price"], errors="coerce")
        entry = pd.to_numeric(df["entry_price"], errors="coerce")
        mask = sl.notna() & entry.notna()
        if SIDE_VALUE == 1 and not (sl[mask] < entry[mask]).all():
            raise MetaModelTrainingError(f"{EDGE_ID} model rows must have sl_price below entry_price for long edges.")
        if SIDE_VALUE == -1 and not (sl[mask] > entry[mask]).all():
            raise MetaModelTrainingError(f"{EDGE_ID} model rows must have sl_price above entry_price for short edges.")


_LAST_ELIGIBILITY_FILTER_COUNTS: dict[str, int] = {}


def load_dataset(selected_features: list[str]) -> pd.DataFrame:
    """Load Task 2A's executable-target resolution CSV, validate it on the
    FULL (unfiltered) gross population, then restrict to
    mldp_target_eligible==True rows for training -- see
    docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md and
    docs/mldp/TASK2B_POPULATION_RULE_DECISION.md.

    Note: this filtering happens on the in-memory frame returned here; the
    separate AFML sample-weight table (_get_afml_weight_table) re-reads the
    same file directly and is deliberately NOT filtered, since concurrency/
    uniqueness must be computed over every gross event -- see A2's "sample-
    weight scope" section. Retained rows' already-computed weights are
    looked up from that full-population table by timestamp, never
    recomputed on the filtered subset.
    """
    if not RESOLUTION_CSV_PATH.exists():
        raise MetaModelTrainingError(f"Missing Task 2A executable-target resolution CSV: {RESOLUTION_CSV_PATH}")
    df = pd.read_csv(RESOLUTION_CSV_PATH)
    _validate_edge_contract(df)
    required = [TIME_COL, TARGET_COL, ELIGIBILITY_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, EVENT_VOL_COL, T1_COL, *selected_features]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise MetaModelTrainingError(f"Resolution rows missing required columns: {missing}")

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
        raise MetaModelTrainingError("All rows dropped after gross-column/selected-feature NaN filtering.")

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
        raise MetaModelTrainingError("No mldp_target_eligible==True rows remain; cannot train.")
    if df[TARGET_COL].isna().any():
        raise MetaModelTrainingError(
            f"{TARGET_COL} contains NaN among eligible rows; Task 2A resolution CSV is inconsistent."
        )
    df[TARGET_COL] = df[TARGET_COL].astype(int)

    df = df.sort_values(TIME_COL).set_index(TIME_COL)
    if df[TARGET_COL].nunique() < 2:
        raise MetaModelTrainingError(f"{TARGET_COL} has fewer than 2 classes among eligible rows.")

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
        raise MetaModelTrainingError(f"Missing standard_quote_tick bars: {path}. {mode_hint}")
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
        raise MetaModelTrainingError(f"{int((pos >= len(bar_index)).sum())} event end time(s) fall after the last bar.")
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
        raise MetaModelTrainingError(f"Could not read Datetime/t1 from model rows {model_rows_path}: {exc}") from exc
    try:
        bars = pd.read_csv(bars_path, usecols=[TIME_COL, "Close"])
    except Exception as exc:
        raise MetaModelTrainingError(f"Could not read Datetime/Close from standard_quote_tick bars {bars_path}: {exc}") from exc

    events[TIME_COL] = to_datetime_iso(events[TIME_COL], utc=True, errors="coerce")
    events[T1_COL] = to_datetime_iso(events[T1_COL], utc=True, errors="coerce")
    if events[[TIME_COL, T1_COL]].isna().any().any():
        bad = int(events[[TIME_COL, T1_COL]].isna().any(axis=1).sum())
        raise MetaModelTrainingError(f"Model rows contain {bad} event(s) with invalid t0/t1.")
    if events[TIME_COL].duplicated().any():
        raise MetaModelTrainingError("Model rows contain duplicate event start timestamps.")
    events = events.sort_values(TIME_COL).reset_index(drop=True)

    bars[TIME_COL] = to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce")
    bars["Close"] = pd.to_numeric(bars["Close"], errors="coerce")
    if bars[[TIME_COL, "Close"]].isna().any().any():
        bad = int(bars[[TIME_COL, "Close"]].isna().any(axis=1).sum())
        raise MetaModelTrainingError(f"standard_quote_tick bars contain {bad} invalid Datetime/Close row(s).")
    if bars[TIME_COL].duplicated().any():
        raise MetaModelTrainingError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[TIME_COL].is_monotonic_increasing:
        raise MetaModelTrainingError("standard_quote_tick bars are not chronologically sorted; refusing to silently reorder them.")

    starts = pd.DatetimeIndex(events[TIME_COL])
    bar_index = pd.DatetimeIndex(bars[TIME_COL])
    ends = bar_index[_snap_to_bar_clock(_label_end_times(events), bar_index)]
    close = bars["Close"].to_numpy(dtype="float64")

    if len(starts) == 0:
        raise MetaModelTrainingError("No labelled events were supplied for AFML weighting.")
    if (ends.asi8 < starts.asi8).any():
        raise MetaModelTrainingError("At least one event has t1 earlier than t0.")
    if not np.isfinite(close).all() or (close <= 0.0).any():
        raise MetaModelTrainingError("Underlying Close must be finite and strictly positive for log returns.")

    start_pos = bar_index.get_indexer(starts)
    end_pos = bar_index.get_indexer(ends)
    if (start_pos < 0).any():
        missing = starts[start_pos < 0][:5].astype(str).tolist()
        raise MetaModelTrainingError(f"Event start timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < 0).any():
        missing = ends[end_pos < 0][:5].astype(str).tolist()
        raise MetaModelTrainingError(f"Event t1 timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < start_pos).any():
        raise MetaModelTrainingError("At least one event maps to an end bar before its start bar.")

    diff = np.zeros(len(bar_index) + 1, dtype="float64")
    np.add.at(diff, start_pos, 1.0)
    np.add.at(diff, end_pos + 1, -1.0)
    concurrency = np.cumsum(diff[:-1])
    active = concurrency > 0.0
    if not active.any():
        raise MetaModelTrainingError("No active event bars found after AFML concurrency construction.")

    inv = np.zeros_like(concurrency)
    inv[active] = 1.0 / concurrency[active]
    inv_prefix = np.concatenate(([0.0], np.cumsum(inv)))
    lengths = (end_pos - start_pos + 1).astype("float64")
    uniqueness = (inv_prefix[end_pos + 1] - inv_prefix[start_pos]) / lengths
    if not np.isfinite(uniqueness).all() or (uniqueness <= 0.0).any() or (uniqueness > 1.0 + 1e-12).any():
        raise MetaModelTrainingError("AFML average uniqueness is invalid/outside (0,1].")

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
        raise MetaModelTrainingError("AFML return-attribution weights are non-finite or sum to zero.")
    normalized = raw_weight * (float(len(raw_weight)) / total)
    if not np.isclose(float(normalized.mean()), 1.0, rtol=1e-10, atol=1e-12):
        raise MetaModelTrainingError(f"AFML normalized weight mean is not 1: {float(normalized.mean())}")

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
            raise MetaModelTrainingError(f"Could not import shared AFML sample-weight engine: {exc}") from exc
        if shared_contract != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
            raise MetaModelTrainingError(
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
            raise MetaModelTrainingError(f"Shared AFML sample-weight construction failed: {exc}") from exc

    required = {AFML_AVERAGE_UNIQUENESS_COL, AFML_RETURN_ATTRIBUTION_WEIGHT_COL}
    missing = sorted(required.difference(table.columns))
    if missing:
        raise MetaModelTrainingError(f"AFML weight table missing required columns: {missing}")
    if table.index.has_duplicates:
        raise MetaModelTrainingError("AFML weight table has duplicate event timestamps.")
    if not np.isfinite(table[list(required)].to_numpy(dtype="float64")).all():
        raise MetaModelTrainingError("AFML weight table contains NaN/inf values.")

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
        raise MetaModelTrainingError("Training/event index contains NaT; cannot align AFML weights.")
    missing = idx.difference(table.index)
    if len(missing):
        examples = missing[:5].astype(str).tolist()
        raise MetaModelTrainingError(f"{len(missing)} model event timestamp(s) are absent from AFML weight table; examples={examples}")
    values = table.loc[idx, column].to_numpy(dtype="float64")
    if not np.isfinite(values).all():
        raise MetaModelTrainingError(f"AFML column {column} contains NaN/inf after alignment.")
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
        raise MetaModelTrainingError("Weighted log-loss inputs have mismatched lengths.")
    if not np.isfinite(w).all() or (w < 0.0).any() or float(w.sum()) <= 0.0:
        raise MetaModelTrainingError("Weighted log-loss sample weights are invalid.")
    loss = -(y_arr * np.log(p) + (1.0 - y_arr) * np.log(1.0 - p))
    return float(np.average(loss, weights=w))



def compute_class_weights(y: pd.Series, class_weight_mode: str) -> np.ndarray:
    """Class imbalance treatment is separate from AFML financial observation weights."""
    if class_weight_mode == "none":
        return np.ones(len(y), dtype=float)
    if class_weight_mode != "balanced":
        raise MetaModelTrainingError(
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
        raise MetaModelTrainingError("Training sample weights are invalid/non-positive after AFML construction.")
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


def score_at_threshold(y_true: pd.Series, proba: np.ndarray, threshold: float) -> dict[str, float]:
    pred = (proba >= threshold).astype(int)
    return {
        "f1": float(f1_score(y_true, pred, zero_division=0)),
        "precision": float(precision_score(y_true, pred, zero_division=0)),
        "recall": float(recall_score(y_true, pred, zero_division=0)),
    }


def evaluate_params_cpcv(
    df: pd.DataFrame,
    x_data: pd.DataFrame,
    y: pd.Series,
    params: dict[str, Any],
    *,
    trial_number: int | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    class_weight_mode = params.get("class_weight_mode", "balanced")
    f1_scores = []
    fold_rows = []
    label = f"Trial {trial_number}" if trial_number is not None else "Trial"

    # AFML Snippet 9.1 (p.130): inner_cv = PurgedKFold -- contiguous test folds; purge on the label t1 and embargo below.
    n_rows = len(df)
    for split_num, test_idx in enumerate(np.array_split(np.arange(n_rows), TUNING_N_SPLITS), start=1):
        train_idx = np.setdiff1d(np.arange(n_rows), test_idx)
        raw_train_rows = len(train_idx)
        raw_test_rows = len(test_idx)
        train_idx = purge_train_overlaps(train_idx, test_idx, df.index, pd.Series(_label_end_times(df), index=df.index))
        train_idx = apply_embargo(train_idx, test_idx, len(df), embargo_size_for(len(df)))

        if len(train_idx) == 0 or len(test_idx) == 0:
            print(
                f"{label} | fold {split_num:02d}: skipped after purge/embargo "
                f"raw_train={raw_train_rows:,} raw_test={raw_test_rows:,}",
                flush=True,
            )
            continue

        x_train = x_data.iloc[train_idx]
        x_test = x_data.iloc[test_idx]
        y_train = y.iloc[train_idx]
        y_test = y.iloc[test_idx]
        if y_train.nunique() < 2:
            print(f"{label} | fold {split_num:02d}: skipped because train fold has one class", flush=True)
            continue

        weights = make_train_weights(df=df, y=y, train_idx=train_idx, class_weight_mode=class_weight_mode)
        model = make_tree(params)
        model.fit(x_train, y_train, sample_weight=weights)
        # AFML Snippet 9.1/9.3: scoring='f1' at the classifier's own decision (predict = p > 0.5), no tuned threshold.
        train_proba = get_positive_class_proba(model, x_train)
        best_thr = SCORING_DECISION_THRESHOLD
        train_f1 = float(f1_score(np.asarray(y_train).astype(int), (train_proba > best_thr).astype(int), zero_division=0))
        test_proba = get_positive_class_proba(model, x_test)
        test_pred = (test_proba > best_thr).astype(int)
        metrics = {
            "f1": float(f1_score(y_test, test_pred, zero_division=0)),
            "precision": float(precision_score(y_test, test_pred, zero_division=0)),
            "recall": float(recall_score(y_test, test_pred, zero_division=0)),
        }
        always_positive_f1 = float(
            f1_score(
                np.asarray(y_test).astype(int),
                np.ones(len(y_test), dtype=int),
                zero_division=0,
            )
        )
        f1_scores.append(metrics["f1"])
        row = {
            "split_num": split_num,
            "raw_train_rows": int(raw_train_rows),
            "raw_test_rows": int(raw_test_rows),
            "train_rows_after_purge_embargo": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
            "best_train_threshold": float(best_thr),
            "train_f1_at_threshold": float(train_f1),
            "test_f1": metrics["f1"],
            "test_precision": metrics["precision"],
            "test_recall": metrics["recall"],
            "always_positive_test_f1": always_positive_f1,
            "model_minus_always_positive_f1": float(metrics["f1"] - always_positive_f1),
        }
        fold_rows.append(row)
        if PRINT_FOLD_PROGRESS:
            running_mean = float(np.mean(f1_scores))
            print(
                f"{label} | fold {split_num:02d}: "
                f"train={len(train_idx):,} test={len(test_idx):,} "
                f"thr={best_thr:.2f} train_f1={train_f1:.4f} "
                f"test_f1={metrics['f1']:.4f} "
                f"precision={metrics['precision']:.4f} recall={metrics['recall']:.4f} "
                f"running_mean_f1={running_mean:.4f}",
                flush=True,
            )

    if not f1_scores:
        return -np.inf, fold_rows
    return float(np.mean(f1_scores)), fold_rows


def optuna_objective(trial: optuna.Trial, df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series) -> float:
    # DecisionTreeClassifier hyperparameters (AFML Ch.6 Snippet 6.2's clf1
    # base estimator). criterion='entropy', max_features='sqrt', and
    # class_weight='balanced' are fixed per the book's exact configuration
    # (see make_tree), not tuned. These four control the single tree's own
    # overfitting risk, which then propagates into the bagged ensemble.
    params = {
        "max_depth": trial.suggest_int("max_depth", *OPTUNA_SEARCH_SPACE["max_depth"]),
        "min_samples_leaf": trial.suggest_int("min_samples_leaf", *OPTUNA_SEARCH_SPACE["min_samples_leaf"], log=True),
        "min_samples_split": trial.suggest_int("min_samples_split", *OPTUNA_SEARCH_SPACE["min_samples_split"], log=True),
        "min_weight_fraction_leaf": trial.suggest_float("min_weight_fraction_leaf", *OPTUNA_SEARCH_SPACE["min_weight_fraction_leaf"]),
        "ccp_alpha": trial.suggest_float("ccp_alpha", *OPTUNA_SEARCH_SPACE["ccp_alpha"], log=True),
        "class_weight_mode": trial.suggest_categorical(
            "class_weight_mode",
            ["balanced"],
        ),
    }
    print("\n" + "-" * 80, flush=True)
    print(f"START TRIAL {trial.number + 1}/{N_TRIALS}", flush=True)
    if PRINT_PARAMS_PER_TRIAL:
        print(f"Trial {trial.number + 1} params: {params}", flush=True)
    score, fold_rows = evaluate_params_cpcv(
        df=df,
        x_data=x_data,
        y=y,
        params=params,
        trial_number=trial.number + 1,
    )
    trial.set_user_attr("fold_rows", fold_rows)
    print(f"END TRIAL {trial.number + 1}/{N_TRIALS} | mean CPCV F1={score:.6f}", flush=True)
    return score


def optuna_progress_callback(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
    best = study.best_trial
    print(
        f"OPTUNA PROGRESS | completed={len(study.trials)}/{N_TRIALS} | "
        f"latest_trial={trial.number + 1} value={trial.value} | "
        f"best_trial={best.number + 1} best_f1={study.best_value:.6f}",
        flush=True,
    )


def train_final_model(df: pd.DataFrame, x_data: pd.DataFrame, y: pd.Series, best_params: dict[str, Any]) -> SequentialBootstrapBaggingClassifier:
    train_idx = np.arange(len(df))
    weights = make_train_weights(
        df=df,
        y=y,
        train_idx=train_idx,
        class_weight_mode=best_params.get("class_weight_mode", "balanced"),
    )
    return make_bagged_model(best_params, df, x_data, y, weights)


def get_feature_importance(model: Any, feature_names: list[str]) -> pd.DataFrame:
    importances = model.feature_importances_
    return pd.DataFrame({"feature": feature_names, "importance": importances}).sort_values(
        "importance", ascending=False
    ).reset_index(drop=True)


def evaluate_final_training_threshold(model: Any, x_data: pd.DataFrame, y: pd.Series) -> dict[str, Any]:
    proba = get_positive_class_proba(model, x_data)
    best_thr, best_f1 = find_best_f1_threshold(y, proba)
    metrics = score_at_threshold(y, proba, best_thr)
    return {
        "diagnostic_only": True,
        "best_train_f1_threshold": float(best_thr),
        "train_f1_at_best_threshold": float(best_f1),
        "train_precision_at_best_threshold": metrics["precision"],
        "train_recall_at_best_threshold": metrics["recall"],
        "note": "Diagnostic only. Final deployable threshold must come from calibrated OOF threshold sweep later.",
    }


# =============================================================================
# MLDP A6: research-trial ledger writer (Colab-portable inline)
# =============================================================================
# Verbatim from shared/colab_portable/trial_ledger_snippet.py (see
# docs/mldp/A6_TRIAL_LEDGER_LOGGING.md). Inlined rather than imported because
# this script runs standalone on Google Colab and cannot import the rest of
# `shared/` at runtime -- see docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's
# "Operational note" for the same constraint applied to purge/embargo.
# tests/test_step02_colab_portable_snippet_sync.py asserts this block stays
# byte-for-byte identical to the canonical file.
# --- BEGIN trial_ledger_snippet.py verbatim ---


REQUIRED_FIELDS: tuple[str, ...] = (
    "trial_id",
    "trial_type",
    "hypothesis_id",
    "parameters",
    "data_window",
    "code_commit",
    "metric_name",
    "result_artifacts",
    "outcome_seen",
    "selection_influenced",
    "historical_backfill",
    "trial_count_quality",
    "created_at_utc",
)

VALID_TRIAL_COUNT_QUALITY = ("exact", "reconstructed", "lower_bound", "unknown")


class TrialLedgerError(RuntimeError):
    """Fail-closed error for the research-trial ledger."""


def new_trial_id() -> str:
    """A fresh, collision-safe id for one ledger row.

    Deliberately NOT a content hash of the trial's parameters: a genuinely
    repeated trial (same hyperparameters re-run, e.g. after a data refresh)
    is still a distinct trial event in an append-only ledger, not a
    duplicate to be merged away -- only two records sharing this exact id
    are treated as the same event (see merge de-duplication in
    shared/research/trial_ledger.py).
    """
    return str(uuid.uuid4())


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_trial_record(
    *,
    trial_type: str,
    hypothesis_id: str,
    parameters: dict[str, Any],
    data_window: dict[str, Any],
    code_commit: str,
    metric_name: str,
    result_artifacts: list[str],
    outcome_seen: bool,
    selection_influenced: bool,
    historical_backfill: bool,
    trial_count_quality: str,
    parent_trial_id: str | None = None,
    notes: str | None = None,
    trial_id: str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Construct one ledger row, matching
    config/research_trial_ledger.schema.json exactly. Raises
    TrialLedgerError immediately if any required field is missing or the
    wrong type -- this function never silently drops or guesses a field.
    """
    if trial_count_quality not in VALID_TRIAL_COUNT_QUALITY:
        raise TrialLedgerError(
            f"trial_count_quality must be one of {VALID_TRIAL_COUNT_QUALITY}, "
            f"got {trial_count_quality!r}"
        )
    record: dict[str, Any] = {
        "trial_id": trial_id or new_trial_id(),
        "trial_type": trial_type,
        "hypothesis_id": hypothesis_id,
        "parent_trial_id": parent_trial_id,
        "parameters": parameters,
        "data_window": data_window,
        "code_commit": code_commit,
        "metric_name": metric_name,
        "result_artifacts": list(result_artifacts),
        "outcome_seen": bool(outcome_seen),
        "selection_influenced": bool(selection_influenced),
        "historical_backfill": bool(historical_backfill),
        "trial_count_quality": trial_count_quality,
        "created_at_utc": created_at_utc or now_utc_iso(),
    }
    if notes is not None:
        record["notes"] = notes
    validate_trial_record(record)
    return record


def validate_trial_record(record: dict[str, Any]) -> None:
    """Lightweight, dependency-free validation mirroring the schema's
    required-fields/basic-type constraints. Raises TrialLedgerError listing
    every problem found, not just the first."""
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in record:
            problems.append(f"missing required field: {field}")

    def _check_type(field: str, expected: type | tuple[type, ...]) -> None:
        if field in record and not isinstance(record[field], expected):
            problems.append(
                f"{field} must be {expected}, got {type(record[field]).__name__}"
            )

    _check_type("trial_id", str)
    _check_type("trial_type", str)
    _check_type("hypothesis_id", str)
    _check_type("parameters", dict)
    _check_type("data_window", dict)
    _check_type("code_commit", str)
    _check_type("metric_name", str)
    _check_type("result_artifacts", list)
    _check_type("outcome_seen", bool)
    _check_type("selection_influenced", bool)
    _check_type("historical_backfill", bool)
    _check_type("created_at_utc", str)

    if isinstance(record.get("trial_id"), str) and len(record["trial_id"]) < 1:
        problems.append("trial_id must not be empty")
    if isinstance(record.get("result_artifacts"), list) and not all(
        isinstance(item, str) for item in record["result_artifacts"]
    ):
        problems.append("result_artifacts must be a list of strings")
    if record.get("trial_count_quality") not in VALID_TRIAL_COUNT_QUALITY:
        problems.append(
            f"trial_count_quality must be one of {VALID_TRIAL_COUNT_QUALITY}"
        )
    if "parent_trial_id" in record and record["parent_trial_id"] is not None:
        if not isinstance(record["parent_trial_id"], str):
            problems.append("parent_trial_id must be a string or null")

    if problems:
        raise TrialLedgerError(
            f"Invalid trial record ({len(problems)} problem(s)): " + "; ".join(problems)
        )


def _acquire_lock(lock_path: Path, timeout_seconds: float) -> int:
    """Simple cross-platform advisory lock via exclusive file creation
    (O_CREAT|O_EXCL). Guards against two concurrent writers (e.g. a local
    consolidation run and a manual append) interleaving partial lines."""
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TrialLedgerError(f"Timed out waiting for ledger lock: {lock_path}")
            time.sleep(0.05)


def append_trial_record(
    ledger_path: str | Path,
    record: dict[str, Any],
    *,
    lock_timeout_seconds: float = 5.0,
) -> None:
    """Validate and append one record as a single JSON line. Append-only,
    single-writer-at-a-time (via an exclusive lock file) and fsync'd before
    the lock is released: no record already written is ever rewritten or
    removed by this function, and a crash mid-write cannot silently corrupt
    or lose a prior line."""
    validate_trial_record(record)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = _acquire_lock(lock_path, lock_timeout_seconds)
    try:
        os.close(fd)
        fd = -1
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def read_trial_records(ledger_path: str | Path) -> list[dict[str, Any]]:
    """Read every record. Raises if the same trial_id appears twice -- the
    ledger is meant to be append-only with a fresh id per trial
    (see new_trial_id's docstring on why content-identical trials are still
    distinct events, not duplicates to silently collapse)."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    records = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TrialLedgerError(
                f"{path}:{line_number}: invalid JSON line: {exc}"
            ) from exc
        trial_id = record.get("trial_id")
        if trial_id in seen_ids:
            raise TrialLedgerError(
                f"{path}:{line_number}: duplicate trial_id {trial_id!r} -- "
                "the ledger must never contain the same trial_id twice."
            )
        seen_ids.add(trial_id)
        records.append(record)
    return records


# --- END trial_ledger_snippet.py verbatim ---


def _git_commit_hash() -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(V2_ROOT), stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
    except Exception:
        return "unknown"


def log_optuna_study_trial(
    study: optuna.Study,
    df: pd.DataFrame,
) -> None:
    """MLDP A6: log ONE trial record per Optuna study (not one per internal
    Optuna trial). result_artifacts points at the full per-trial Optuna
    results CSV, from which the exact internal trial count can always be
    reconstructed -- see docs/mldp/A6_TRIAL_LEDGER_LOGGING.md."""
    record = build_trial_record(
        trial_type="optuna_hyperparameter_search",
        hypothesis_id=EDGE_ID,
        parameters={
            "n_trials": N_TRIALS,
            # AFML Ch.6 walkthrough 2026-10-24: the real decision-tree space (was a stale XGBoost space, D035)
            "search_space": {**{k: list(v) for k, v in OPTUNA_SEARCH_SPACE.items()},
                             "class_weight_mode": ["balanced"], "estimator": "DecisionTreeClassifier (entropy, max_features=sqrt)",
                             "sampler": "random (AFML s.9.3)", "cv": f"purged {TUNING_N_SPLITS}-fold", "score": "f1 at p > 0.5"},
        },
        data_window={"start": str(df.index.min()), "end": str(df.index.max())},
        code_commit=_git_commit_hash(),
        metric_name="mean_cpcv_f1",
        result_artifacts=[display_path(OPTUNA_TRIALS_OUTPUT), display_path(MANIFEST_OUTPUT)],
        outcome_seen=True,
        selection_influenced=True,
        historical_backfill=False,
        trial_count_quality="exact",
        notes=(
            f"study_name={STUDY_NAME}; best_value={study.best_value:.6f}; "
            f"best_trial_number={study.best_trial.number}"
        ),
    )
    append_trial_record(TRIAL_LEDGER_OUTPUT, record)
    print(f"Logged 1 research-trial ledger record to: {TRIAL_LEDGER_OUTPUT}", flush=True)


def main() -> None:
    print("=" * 80, flush=True)
    print(f"V2 STEP 02: FINAL META-MODEL TRAINING WITH OPTUNA — {EDGE_ID}", flush=True)
    print("=" * 80, flush=True)
    print(f"Run mode: {RUN_MODE}", flush=True)
    print(f"Working directory: {CWD}", flush=True)
    print(f"Model rows: {RESOLUTION_CSV_PATH}", flush=True)
    print(f"Selected features: {SELECTED_FEATURES_PATH}", flush=True)
    print(f"Feature schema: {FEATURE_SCHEMA_PATH}", flush=True)
    print(f"Output directory: {OUTPUT_DIR}", flush=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _phase3a1_validate_step01_feature_surgery_manifest()
    print("Step-01 semantic provenance: PASS", flush=True)
    selected_features = load_selected_features()
    df = load_dataset(selected_features)
    x_data = df[selected_features].copy()
    y = df[TARGET_COL].copy()

    print(f"Date range: {df.index.min()} -> {df.index.max()}", flush=True)
    print(f"Rows used: {len(df):,}", flush=True)
    print(f"Selected feature count: {len(selected_features):,}", flush=True)
    print(f"Target mean: {y.mean():.6f}", flush=True)
    print(f"Optuna trials: {N_TRIALS}", flush=True)
    print(f"Tuning CV: purged + embargoed {TUNING_N_SPLITS}-fold (AFML Snippet 9.1); score F1 at p > 0.5; random search", flush=True)
    print(f"Manual t1 overlap purge: ON | manual embargo size: {MANUAL_EMBARGO_SIZE}", flush=True)
    print("F1 thresholds are train-fold-only diagnostics; final live threshold comes later after sigmoid calibration.", flush=True)

    print("\nRunning Optuna tuning with MLDP-style CPCV F1 objective...", flush=True)
    # Seeded sampler: without this, re-running this script (e.g. to regenerate
    # an artifact after an unrelated code fix, as happened this session)
    # finds a DIFFERENT best_params.json each time, silently breaking the
    # provenance chain against any already-generated Step03/04 output that
    # was built from the previous run's best_params. RANDOM_STATE is already
    # the project's fixed seed for every other stochastic component here.
    study = optuna.create_study(
        direction="maximize", study_name=STUDY_NAME,
        sampler=optuna.samplers.RandomSampler(seed=RANDOM_STATE),  # AFML s.9.3 randomized search (Ch.9 2026-10-24)
    )
    study.optimize(
        lambda trial: optuna_objective(trial, df=df, x_data=x_data, y=y),
        n_trials=N_TRIALS,
        timeout=OPTUNA_TIMEOUT,
        show_progress_bar=False,
        callbacks=[optuna_progress_callback],
    )

    best_params = study.best_trial.params
    best_score = float(study.best_value)
    best_fold_rows = study.best_trial.user_attrs.get("fold_rows", [])
    print(f"\nBest CPCV F1: {best_score:.6f}", flush=True)
    print("Best parameters:", flush=True)
    print(best_params, flush=True)

    trials_df = study.trials_dataframe()
    trials_df.to_csv(OPTUNA_TRIALS_OUTPUT, index=False)

    print(f"\nTraining final AFML Ch.6 sequential-bootstrap-bagged model "
          f"(n_estimators={N_ESTIMATORS_BAGGING}) on all selected-feature rows...", flush=True)
    final_model = train_final_model(df=df, x_data=x_data, y=y, best_params=best_params)
    joblib.dump(bagging_to_portable_payload(final_model), MODEL_OUTPUT)

    rho_bar, rho_bar_excluded = final_model.mean_pairwise_tree_correlation(x_data)
    # AFML SS6.3.1, p.95: V[bagged] = sigma_bar^2*(rho_bar+(1-rho_bar)/N). Report
    # the realized variance ratio at N=100 (this run) vs N=1000 (AFML's own
    # Snippet 6.2 illustrative value) so the n_estimators decision is made from
    # this edge's actual data, not copied from the book's example.
    def _variance_ratio(rho: float, n_from: int, n_to: int) -> float:
        return (rho + (1.0 - rho) / n_to) / (rho + (1.0 - rho) / n_from)
    variance_at_1000_vs_100 = _variance_ratio(rho_bar, N_ESTIMATORS_BAGGING, 1000)
    print(f"AFML rho_bar (mean pairwise bagged-tree correlation): {rho_bar:.4f} "
          f"({rho_bar_excluded} of {N_ESTIMATORS_BAGGING} trees excluded as degenerate single-leaf stumps)", flush=True)
    print(f"Projected bagged-variance at N=1000 relative to N={N_ESTIMATORS_BAGGING}: "
          f"{variance_at_1000_vs_100:.4f}x (1.0 = no further benefit, per AFML SS6.3.1's own formula)", flush=True)

    feature_importance = get_feature_importance(final_model, selected_features)
    feature_importance.to_csv(FEATURE_IMPORTANCE_OUTPUT, index=False)
    save_json(BEST_PARAMS_OUTPUT, best_params)
    final_train_diag = evaluate_final_training_threshold(final_model, x_data, y)
    afml_provenance = _afml_provenance_snapshot()
    baseline_values = [float(r["always_positive_test_f1"]) for r in best_fold_rows if "always_positive_test_f1" in r]
    model_minus_baseline_values = [float(r["model_minus_always_positive_f1"]) for r in best_fold_rows if "model_minus_always_positive_f1" in r]

    manifest = {
        "created_at": now_utc(),
        "edge_id": EDGE_ID,
        "run_mode": RUN_MODE,
        "script": display_path(THIS_FILE),
        "blueprint_stage": "raw_probability_model_training_before_oof_and_calibration",
        "feature_engineering_performed_here": False,
        "input_model_rows": display_path(RESOLUTION_CSV_PATH),
        "input_selected_features": display_path(SELECTED_FEATURES_PATH),
        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),
        "afml_provenance": afml_provenance,
        "rows_used": int(len(df)),
        "date_start": str(df.index.min()),
        "date_end": str(df.index.max()),
        "target": TARGET_COL,
        "target_mean": float(y.mean()),
        "selected_feature_count": int(len(selected_features)),
        "selected_features": selected_features,
        "side_specialization": {
            "edge_side": SIDE,
            "side_value": SIDE_VALUE,
            "model_learns_direction": False,
        },
        "model_type": "SequentialBootstrapBaggingClassifier",
        "model_base_estimator": "DecisionTreeClassifier",
        "model_afml_reference": "AFML Ch.6 (Ensemble Methods, p.93-102) bagging, resampled via Ch.4 SS4.5 (p.61-66) sequential bootstrap",
        "model_n_estimators_bagging": int(N_ESTIMATORS_BAGGING),
        "model_bootstrap_max_samples": final_model.max_samples,
        "model_training_avg_uniqueness": float(final_model.avg_uniqueness_),
        "model_bootstrap_sample_length": int(final_model.sample_length_),
        "model_tree_seeds": "per-tree, drawn from the seeded ensemble rng",
        "model_rho_bar": float(rho_bar),
        "model_rho_bar_degenerate_trees_excluded": int(rho_bar_excluded),
        "model_projected_variance_ratio_at_1000_vs_current_n": float(variance_at_1000_vs_100),
        "model_output_type": "raw_probability_model",
        "deployable": False,
        "deployability_blockers": [
            "OOF raw probability generation not yet complete",
            "sigmoid calibration not yet complete",
            "calibrated threshold sweep not yet complete",
            "runtime.py::score not yet wired to model/calibrator artifacts",
            "deployability replay audit not yet passed",
        ],
        "scoring": {
            "primary_metric": "f1",
            "optuna_direction": "maximize",
            "best_cpcv_f1": best_score,
            "threshold_selection": "none: F1 at the classifier's own decision p > 0.5 (AFML Snippet 9.1 scoring='f1', p.130)",
            "search": "randomized (Optuna RandomSampler, seeded; AFML s.9.3)",
            "note": "These F1 thresholds are model-selection diagnostics only. Final live threshold must use calibrated_probability later.",
            "always_positive_cpcv_f1_baseline": {
                "diagnostic_only": True,
                "mean_test_f1": float(np.mean(baseline_values)) if baseline_values else None,
                "mean_model_minus_baseline_f1": float(np.mean(model_minus_baseline_values)) if model_minus_baseline_values else None,
                "note": "Baseline is never used by Optuna or threshold selection.",
            },
        },
        "cpcv": {
            "n_folds": N_FOLDS,
            "n_test_folds": N_TEST_FOLDS,
            "purged_size": PURGED_SIZE,
            "embargo_size": EMBARGO_SIZE,
            "manual_embargo_size": MANUAL_EMBARGO_SIZE,
            "embargo_rule": EMBARGO_RULE,
            "purge_t1": PURGE_T1_RULE,
            "mode": "purged_kfold_label_t1_with_embargo (AFML Snippet 9.1); CPCV fields above are not used for tuning",
            "tuning_n_splits": TUNING_N_SPLITS,
            "best_trial_fold_rows": best_fold_rows,
        },
        "optuna": {
            "n_trials": N_TRIALS,
            "timeout": OPTUNA_TIMEOUT,
            "best_trial_number": int(study.best_trial.number),
            "best_params": best_params,
            "best_value": best_score,
        },
        "final_training_diagnostics": final_train_diag,
        "outputs": {
            "model": display_path(MODEL_OUTPUT),
            "best_params": display_path(BEST_PARAMS_OUTPUT),
            "feature_importance": display_path(FEATURE_IMPORTANCE_OUTPUT),
            "optuna_trials": display_path(OPTUNA_TRIALS_OUTPUT),
            "manifest": display_path(MANIFEST_OUTPUT),
        },
        "next_ordered_step": "03_generate_oof_predictions.py",
        "notes": [
            "This script ports V1 Step 6D into V2.",
            "It consumes exported V2 side-specialized model rows and Step 01 selected features only.",
            "It does not engineer new features.",
            "It trains a raw-probability model only.",
            "Final deployable threshold is not selected here.",
            "The next step must generate OOF raw probabilities for calibration and threshold evaluation.",
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
        "model_rows_sha256": _phase3a_sha256_file(RESOLUTION_CSV_PATH),
        "selected_features_sha256": _phase3a_sha256_file(SELECTED_FEATURES_PATH),
        "feature_schema_sha256": _phase3a_sha256_file(FEATURE_SCHEMA_PATH),
        "model_artifact_sha256": _phase3a_sha256_file(MODEL_OUTPUT),
        "best_params_sha256": _phase3a_sha256_file(BEST_PARAMS_OUTPUT),
        "afml_sample_weight_contract_version": afml_provenance["contract_version"],
        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],
        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],
        "phase3a_provenance_status": "PASS",
    })
    save_json(MANIFEST_OUTPUT, manifest)
    log_optuna_study_trial(study, df)

    print("\n✅ V2 Optuna meta-model training completed.", flush=True)
    print(f"Rows used: {len(df):,}", flush=True)
    print(f"Selected features: {len(selected_features):,}", flush=True)
    print(f"Best CPCV F1: {best_score:.6f}", flush=True)
    print(f"Final diagnostic train threshold: {final_train_diag['best_train_f1_threshold']}", flush=True)
    print(f"Saved model: {MODEL_OUTPUT}", flush=True)
    print(f"Saved manifest: {MANIFEST_OUTPUT}", flush=True)
    print("Next ordered step: 03_generate_oof_predictions.py", flush=True)

    if RUN_MODE == "flat_colab":
        print("\nFlat Colab output files to download:", flush=True)
        for path in [
            MODEL_OUTPUT,
            BEST_PARAMS_OUTPUT,
            FEATURE_IMPORTANCE_OUTPUT,
            OPTUNA_TRIALS_OUTPUT,
            MANIFEST_OUTPUT,
            TRIAL_LEDGER_OUTPUT,
        ]:
            print(f"  - {path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except MetaModelTrainingError as exc:
        raise SystemExit(f"❌ {exc}") from exc
