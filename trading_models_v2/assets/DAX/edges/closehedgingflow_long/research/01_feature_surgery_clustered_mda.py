"""
01_feature_surgery_clustered_mda.py

V2 Step 01: V1-style iterative clustered feature surgery for the current edge package.

Blueprint boundary
------------------
This script is allowed to SELECT features from the exported V2 model rows.
It is NOT allowed to engineer new model-input features.

Two supported run modes
-----------------------
1) Repo mode, from trading_models_v2:

    python ./assets/<ASSET>/edges/<FAMILY>_<SIDE>/research/01_feature_surgery_clustered_mda.py

2) Flat Colab mode, from /content:

    Upload these four files directly into /content:
        01_feature_surgery_clustered_mda.py
        <edge_stem>_mldp_execution_resolution.csv
        feature_schema.json
        <asset>_standard_quote_tick_bars.csv

    Then run:
        !python /content/01_feature_surgery_clustered_mda.py

    Output files will be written directly into /content for easy download.

This ports the useful V1 Step 6C logic while enforcing V2 contracts:
    - feature_schema.json is the model-input authority
    - no Colab/manual feature engineering
    - manual t1 overlap purge
    - manual embargo
    - AFML Ch.4 sample weight = class_weight * normalized absolute concurrency-adjusted log-return attribution,
      computed over every gross event (including ones later excluded as
      target-ineligible) so retained events' concurrency is not understated --
      see docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md
    - average uniqueness is computed separately on the standard_quote_tick bar clock
    - train-fold-only F1 threshold selection
    - clustered MDA removes one negative-MDA cluster at a time

MLDP target migration (Task 2C, per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md)
----------------------------------------------------------------------------------------------
This step now consumes Task 2A's executable-target resolution CSV (a strict
superset of the old model-rows CSV: every original feature column, plus
gross_r/gross_meta_label/gross_t1 and quote_executable_r/quote_executable_t1/
quote_executable_meta_label/mldp_target_eligible). Clustered feature
importance is computed against quote_executable_meta_label (not the gross
label), restricted to mldp_target_eligible==True rows only (Task 2B's frozen
population rule -- see docs/mldp/TASK2B_POPULATION_RULE_DECISION.md).
Feature clustering itself uses Marcenko-Pastur denoising + ONC
(docs/mldp/A4_DENOISE_ONC_UTILITY.md) instead of raw Spearman correlation with
a fixed Ward-linkage distance threshold.

Side-specialized note
---------------------
Each V2 edge has a fixed side derived from the edge folder or flat filename. The
model should learn whether to accept a side-specialized candidate, not trade
direction. Therefore `side` is treated as metadata unless feature_schema.json
explicitly requires otherwise; this Step 01 does not force it as a fixed model
feature.
"""

from __future__ import annotations

# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1

# QUANTARA_AFML_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

import hashlib
import json
import math
import os
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
import scipy.cluster.hierarchy as sch
import scipy.stats as stats
import xgboost as xgb
from scipy.optimize import minimize
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans
from sklearn.metrics import f1_score, precision_score, recall_score, silhouette_samples
from sklearn.neighbors import KernelDensity
from sklearn.utils.class_weight import compute_sample_weight

try:
    from skfolio.model_selection import CombinatorialPurgedCV
except Exception as exc:  # pragma: no cover
    CombinatorialPurgedCV = None
    SKFOLIO_IMPORT_ERROR = exc
else:
    SKFOLIO_IMPORT_ERROR = None

warnings.filterwarnings("ignore")

RNG = np.random.default_rng(42)

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


def _resolve_paths() -> tuple[str, Path, Path, Path, Path, dict[str, Any]]:
    flat_resolution = _single_file(f"*{RESOLUTION_SUFFIX}", "MLDP executable-target resolution")
    flat_feature_schema = CWD / "feature_schema.json"
    if _looks_like_colab_root(CWD) and flat_resolution is not None and flat_feature_schema.exists():
        edge_stem = flat_resolution.name.removesuffix(RESOLUTION_SUFFIX)
        context = _context_from_edge_stem(edge_stem)
        return "flat_colab", CWD, CWD, flat_resolution, flat_feature_schema, context

    edge_root = THIS_FILE.parents[1]
    v2_root = THIS_FILE.parents[5]
    context = _context_from_edge_root(edge_root)
    resolution_path = edge_root / "artifacts" / "research_dataset" / f"{context['edge_stem']}{RESOLUTION_SUFFIX}"
    feature_schema_path = edge_root / "contracts" / "feature_schema.json"
    return "repo", v2_root, edge_root, resolution_path, feature_schema_path, context


RUN_MODE, V2_ROOT, EDGE_ROOT, RESOLUTION_CSV_PATH, FEATURE_SCHEMA_PATH, EDGE_CONTEXT = _resolve_paths()
ASSET = EDGE_CONTEXT["asset"]
FAMILY = EDGE_CONTEXT["family"]
SIDE = EDGE_CONTEXT["side"]
SIDE_VALUE = EDGE_CONTEXT["side_value"]
EDGE_ID = EDGE_CONTEXT["edge_id"]
EDGE_STEM = EDGE_CONTEXT["edge_stem"]

OUTPUT_DIR = CWD if RUN_MODE == "flat_colab" else EDGE_ROOT / "artifacts" / "feature_surgery"

OUTPUT_ITERATION_REPORT_CSV = OUTPUT_DIR / f"{EDGE_STEM}_iterative_cfi_report.csv"
OUTPUT_FINAL_CLUSTER_REPORT_CSV = OUTPUT_DIR / f"{EDGE_STEM}_final_cluster_report.csv"
OUTPUT_CORRELATION_MATRIX_CSV = OUTPUT_DIR / f"{EDGE_STEM}_feature_correlation_matrix.csv"
OUTPUT_SELECTED_FEATURES_JSON = OUTPUT_DIR / f"{EDGE_STEM}_selected_features.json"
OUTPUT_MANIFEST_JSON = OUTPUT_DIR / "feature_surgery_manifest.json"

# --- MLDP target migration (Task 2C) ---
# Per docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md, the frozen
# training/importance target is quote_executable_meta_label, restricted to
# mldp_target_eligible==True rows (Task 2B's population rule). The old gross
# label (meta_label/realized_r, now duplicated under canonical names
# gross_meta_label/gross_r/gross_t1 in Task 2A's resolution CSV) is retained
# ONLY for an internal data-integrity check -- it is never used as the
# training/importance target or as a model feature.
TARGET_COL = "quote_executable_meta_label"
ELIGIBILITY_COL = "mldp_target_eligible"
GROSS_TARGET_COL = "gross_meta_label"
GROSS_REALIZED_R_COL = "gross_r"
GROSS_LABEL_CONTRACT_VERSION = "quantara_v3_gross_economic_meta_label_v1"
GROSS_TARGET_DEFINITION = "int(gross_r > 0)"
# Purge/embargo and AFML sample weights both use gross_t1 (the frozen event
# geometry's information-window end, known at label time), not
# quote_executable_t1 (only defined for eligible rows). This is a deliberate
# simplification, not an oversight: gross_t1 and quote_executable_t1 bound
# essentially the same event-resolution window (same frozen barrier
# geometry, resolved via bar-level touch vs. exact tick-level replay), and
# using one consistent, always-populated column for both the full-population
# weight computation and the eligible-subset purge avoids a two-t1 scheme
# that would be easy to get subtly wrong for a marginal precision gain.
T1_COL = "gross_t1"
# AFML Ch.4 walkthrough 2026-10-23: sample weights + sequential bootstrap end each event at the training label's own
# t1 (Task 2A label_t1), snapped to the bar clock; purge/embargo keep T1_COL until the Ch.7 review.
LABEL_T1_COL = "label_t1"
# AFML Snippet 4.11 time-decay parameter c (1.0 = no decay). Chosen in the Ch.9 hyper-parameter review, not here.
TIME_DECAY_C = 1.0
EVENT_VOL_COL = "Event_Vol"
TIME_COL = "Datetime"

# V2 edges are side-specialized. Do not force constant side into the model.
OPTIONAL_FIXED_FEATURES: list[str] = []

# V1 leakage/metadata columns plus V2 probability/risk outputs, PLUS every
# outcome/execution column Task 2A's resolution CSV adds beyond the plain
# model-rows file. None of these are currently whitelisted in
# feature_schema.json (verified before this migration), so
# choose_candidate_features() would already exclude them via schema
# filtering alone -- this set is deliberate defense-in-depth, not the only
# safeguard.
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

NON_FEATURE_COLS = {
    TIME_COL,
}

# V1 deliberately removed raw price levels and absolute price-unit columns.
# V2 still lets feature_schema.json be the authority first; this exclusion is a
# V1-compatible surgery policy applied only after schema filtering.
DROP_RAW_PRICE_COLS = {
    "Open",
    "High",
    "Low",
    "Close",
    "Kalman_Close",
    "EMA_10",
    "EMA_20",
    "EMA_50",
    "EMA_100",
    "EMA_200",
    "BBL_20_2",
    "BBM_20_2",
    "BBU_20_2",
    "DCL_20",
    "DCM_20",
    "DCU_20",
    "Range",
    "Body",
    "Upper_Wick",
    "Lower_Wick",
    "Body_Size",
    "ATR_14",
    "ATR_21",
    "ATR_50",
    "ATR_100",
    "OBV",
    "Proxy_Value",
    "Ticks",
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

# Clustering / surgery. (Ward-linkage fixed-threshold clustering was replaced
# by MLDP A4's Marcenko-Pastur denoising + ONC -- see build_feature_clusters.)
MAX_ITERATIONS = 50
MIN_CANDIDATE_FEATURES = 5
F1_THRESHOLD_GRID = np.round(np.arange(0.30, 0.81, 0.01), 2)

# V1 XGBoost CFI/MDA model.
XGB_PARAMS = {
    "n_estimators": 500,
    "max_depth": 3,
    "learning_rate": 0.03,
    "min_child_weight": 25,
    "subsample": 0.75,
    "colsample_bytree": 0.75,
    "gamma": 0.5,
    "reg_alpha": 0.10,
    "reg_lambda": 10.0,
    "objective": "binary:logistic",
    "eval_metric": "aucpr",
    "random_state": 42,
    "n_jobs": -1,
}


class FeatureSurgeryError(RuntimeError):
    """Raised when feature surgery must fail closed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, (pd.Timestamp,)):
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


def get_xgb_device_kwargs() -> dict[str, Any]:
    """V1-compatible XGBoost GPU helper."""
    try:
        major = int(xgb.__version__.split(".")[0])
    except Exception:
        major = 2
    if major >= 2:
        return {"tree_method": "hist", "device": "cuda"}
    return {"tree_method": "gpu_hist"}


def make_model(params: dict[str, Any]) -> xgb.XGBClassifier:
    return xgb.XGBClassifier(**params, **get_xgb_device_kwargs())


def fit_model_safely(model: xgb.XGBClassifier, x_train: pd.DataFrame, y_train: pd.Series, sample_weight: np.ndarray) -> xgb.XGBClassifier:
    """Fit on GPU when available, retry on CPU if the local XGBoost/GPU stack fails."""
    try:
        model.fit(x_train, y_train, sample_weight=sample_weight)
        return model
    except Exception as exc:
        print(f"GPU/accelerated XGBoost fit failed; retrying on CPU. Reason: {exc}", flush=True)
        params = model.get_params()
        params.pop("device", None)
        params["tree_method"] = "hist"
        cpu_model = xgb.XGBClassifier(**params)
        cpu_model.fit(x_train, y_train, sample_weight=sample_weight)
        return cpu_model



def _phase3a1_sha256_file(path: Path) -> str:
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
        raise FeatureSurgeryError(f"Cannot hash AFML table; missing columns: {missing}")
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
        "standard_quote_tick_bars_sha256": _phase3a1_sha256_file(bars_path),
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


def _phase3a1_validate_gross_target(df: pd.DataFrame) -> None:
    """Data-integrity check on the GROSS layer only (gross_meta_label must
    equal int(gross_r > 0) for every row). This never touches the
    executable-target training label; it exists purely to catch a corrupted
    or stale resolution CSV before Step 01 proceeds."""
    if GROSS_REALIZED_R_COL not in df.columns:
        raise FeatureSurgeryError(
            f"{EDGE_ID} resolution rows missing {GROSS_REALIZED_R_COL!r}; "
            "Step 01 requires Task 2A's executable-target resolution CSV."
        )
    realized = pd.to_numeric(df[GROSS_REALIZED_R_COL], errors="coerce")
    target = pd.to_numeric(df[GROSS_TARGET_COL], errors="coerce")
    if realized.isna().any() or target.isna().any():
        raise FeatureSurgeryError(
            f"{EDGE_ID}: Step-01 gross target provenance contains non-numeric/NaN values."
        )
    expected = (realized > 0.0).astype(int)
    actual = target.astype(int)
    if not actual.equals(expected):
        mismatches = int((actual != expected).sum())
        raise FeatureSurgeryError(
            f"{EDGE_ID}: {GROSS_TARGET_COL} violates {GROSS_LABEL_CONTRACT_VERSION}; "
            f"expected {GROSS_TARGET_DEFINITION}; mismatches={mismatches}."
        )


def _validate_executable_target_eligibility(df: pd.DataFrame) -> None:
    """Task 2B/A2 consistency check: mldp_target_eligible must agree exactly
    with whether quote_executable_meta_label is populated. Any disagreement
    means Task 2A's own invariant was violated upstream -- fail closed rather
    than silently trusting one column over the other."""
    if ELIGIBILITY_COL not in df.columns:
        raise FeatureSurgeryError(
            f"{EDGE_ID} resolution rows missing {ELIGIBILITY_COL!r}; "
            "Step 01 requires Task 2A's executable-target resolution CSV."
        )
    eligible = df[ELIGIBILITY_COL].astype(bool)
    target_present = df[TARGET_COL].notna()
    if not eligible.equals(target_present):
        mismatches = int((eligible != target_present).sum())
        raise FeatureSurgeryError(
            f"{EDGE_ID}: {ELIGIBILITY_COL} disagrees with {TARGET_COL} "
            f"presence for {mismatches} row(s); Task 2A resolution CSV is "
            "internally inconsistent."
        )


def load_feature_schema_allowed_features() -> list[str]:
    if not FEATURE_SCHEMA_PATH.exists():
        raise FeatureSurgeryError(f"Missing feature schema: {FEATURE_SCHEMA_PATH}")
    schema = json.loads(FEATURE_SCHEMA_PATH.read_text(encoding="utf-8"))
    features = []
    for row in schema.get("features", []):
        if (
            row.get("live_available") is True
            and row.get("allowed_for_model_input") is True
            and row.get("uses_future_information") is False
        ):
            features.append(str(row["feature_name"]))
    if not features:
        raise FeatureSurgeryError("feature_schema.json has no allowed model-input features.")
    return features


def _validate_edge_contract(df: pd.DataFrame) -> None:
    if "edge_id" in df.columns and not (df["edge_id"] == EDGE_ID).all():
        bad = df.loc[df["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise FeatureSurgeryError(f"{EDGE_ID} model rows contain wrong edge_id values: {bad}")
    if "side" in df.columns and not (pd.to_numeric(df["side"], errors="coerce") == SIDE_VALUE).all():
        bad = df.loc[pd.to_numeric(df["side"], errors="coerce") != SIDE_VALUE, "side"].unique().tolist()
        raise FeatureSurgeryError(f"{EDGE_ID} model rows contain wrong side values: {bad}; expected {SIDE_VALUE}")
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
            raise FeatureSurgeryError(f"{EDGE_ID} model rows must have pt_price above entry_price for long edges.")
        if SIDE_VALUE == -1 and not (pt[mask] < entry[mask]).all():
            raise FeatureSurgeryError(f"{EDGE_ID} model rows must have pt_price below entry_price for short edges.")
    if "sl_price" in df.columns and "entry_price" in df.columns:
        sl = pd.to_numeric(df["sl_price"], errors="coerce")
        entry = pd.to_numeric(df["entry_price"], errors="coerce")
        mask = sl.notna() & entry.notna()
        if SIDE_VALUE == 1 and not (sl[mask] < entry[mask]).all():
            raise FeatureSurgeryError(f"{EDGE_ID} model rows must have sl_price below entry_price for long edges.")
        if SIDE_VALUE == -1 and not (sl[mask] > entry[mask]).all():
            raise FeatureSurgeryError(f"{EDGE_ID} model rows must have sl_price above entry_price for short edges.")


_LAST_ELIGIBILITY_FILTER_COUNTS: dict[str, int] = {}


def load_model_rows() -> pd.DataFrame:
    """Load Task 2A's executable-target resolution CSV, validate it on the
    FULL (unfiltered) gross population, then restrict to
    mldp_target_eligible==True rows for training/importance purposes only.

    Note: this filtering happens on the in-memory frame returned here; the
    separate AFML sample-weight table (_get_afml_weight_table) re-reads the
    same file directly and is deliberately NOT filtered, since concurrency/
    uniqueness must be computed over every gross event -- see
    docs/mldp/A2_TARGET_CALIBRATION_ARCHITECTURE_DECISION.md's "sample-weight
    scope" section. Retained rows' already-computed weights are looked up
    from that full-population table by timestamp, never recomputed on the
    filtered subset.
    """
    if not RESOLUTION_CSV_PATH.exists():
        raise FeatureSurgeryError(f"Missing Task 2A executable-target resolution CSV: {RESOLUTION_CSV_PATH}")
    df = pd.read_csv(RESOLUTION_CSV_PATH)
    _validate_edge_contract(df)
    required = [TIME_COL, TARGET_COL, ELIGIBILITY_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, EVENT_VOL_COL, T1_COL]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise FeatureSurgeryError(f"Resolution rows missing required columns: {missing}")

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

    # Both integrity checks run on the FULL gross population, before any
    # eligibility filtering, since they validate columns that must be
    # correct for every row regardless of executable-target eligibility.
    _phase3a1_validate_gross_target(df)
    _validate_executable_target_eligibility(df)

    df = df.dropna(subset=[TIME_COL, T1_COL, GROSS_TARGET_COL, GROSS_REALIZED_R_COL, EVENT_VOL_COL]).copy()
    if df.empty:
        raise FeatureSurgeryError("Resolution rows are empty after gross-column normalization.")

    before_eligibility = len(df)
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
        raise FeatureSurgeryError("No mldp_target_eligible==True rows remain; cannot run feature surgery.")
    if df[TARGET_COL].isna().any():
        raise FeatureSurgeryError(
            f"{TARGET_COL} contains NaN among eligible rows; Task 2A resolution CSV is inconsistent."
        )
    df[TARGET_COL] = df[TARGET_COL].astype(int)

    df = df.sort_values(TIME_COL).set_index(TIME_COL)
    if df[TARGET_COL].nunique() < 2:
        raise FeatureSurgeryError(f"{TARGET_COL} has fewer than 2 classes among eligible rows. Cannot run feature surgery.")
    return df


def choose_candidate_features(df: pd.DataFrame, schema_features: list[str]) -> tuple[list[str], list[str], dict[str, Any]]:
    present_allowed = [f for f in schema_features if f in df.columns]
    missing_from_rows = [f for f in schema_features if f not in df.columns]

    fixed = []
    for col in OPTIONAL_FIXED_FEATURES:
        if col in present_allowed and col not in LEAKAGE_COLS:
            fixed.append(col)

    candidates = []
    exclusion_reasons = {}
    for col in present_allowed:
        if col in fixed:
            continue
        reason = None
        if col in LEAKAGE_COLS:
            reason = "leakage_or_label_metadata"
        elif col in NON_FEATURE_COLS:
            reason = "timestamp_or_non_feature"
        elif col in DROP_RAW_PRICE_COLS:
            reason = "v1_raw_price_or_absolute_level_exclusion"
        elif col == "side":
            reason = "side_specialized_constant_metadata"
        elif not pd.api.types.is_numeric_dtype(df[col]):
            reason = "non_numeric"
        else:
            series = pd.to_numeric(df[col], errors="coerce")
            if series.notna().sum() == 0:
                reason = "all_nan_after_numeric_cast"
            elif series.nunique(dropna=True) <= 1:
                reason = "constant_or_single_value"

        if reason is not None:
            exclusion_reasons[col] = reason
            continue
        candidates.append(col)

    if not candidates:
        raise FeatureSurgeryError("No candidate features left after schema and V1-compatible exclusions.")

    diagnostics = {
        "schema_feature_count": len(schema_features),
        "present_allowed_feature_count": len(present_allowed),
        "missing_schema_features_from_model_rows": missing_from_rows,
        "fixed_features": fixed,
        "candidate_feature_count": len(candidates),
        "excluded_feature_count": len(exclusion_reasons),
        "exclusion_reasons": exclusion_reasons,
    }
    return fixed, candidates, diagnostics


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
        raise FeatureSurgeryError(f"Missing standard_quote_tick bars: {path}. {mode_hint}")
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
        raise FeatureSurgeryError(f"{int((pos >= len(bar_index)).sum())} event end time(s) fall after the last bar.")
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
        raise FeatureSurgeryError(f"Could not read Datetime/t1 from model rows {model_rows_path}: {exc}") from exc
    try:
        bars = pd.read_csv(bars_path, usecols=[TIME_COL, "Close"])
    except Exception as exc:
        raise FeatureSurgeryError(f"Could not read Datetime/Close from standard_quote_tick bars {bars_path}: {exc}") from exc

    events[TIME_COL] = to_datetime_iso(events[TIME_COL], utc=True, errors="coerce")
    events[T1_COL] = to_datetime_iso(events[T1_COL], utc=True, errors="coerce")
    if events[[TIME_COL, T1_COL]].isna().any().any():
        bad = int(events[[TIME_COL, T1_COL]].isna().any(axis=1).sum())
        raise FeatureSurgeryError(f"Model rows contain {bad} event(s) with invalid t0/t1.")
    if events[TIME_COL].duplicated().any():
        raise FeatureSurgeryError("Model rows contain duplicate event start timestamps.")
    events = events.sort_values(TIME_COL).reset_index(drop=True)

    bars[TIME_COL] = to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce")
    bars["Close"] = pd.to_numeric(bars["Close"], errors="coerce")
    if bars[[TIME_COL, "Close"]].isna().any().any():
        bad = int(bars[[TIME_COL, "Close"]].isna().any(axis=1).sum())
        raise FeatureSurgeryError(f"standard_quote_tick bars contain {bad} invalid Datetime/Close row(s).")
    if bars[TIME_COL].duplicated().any():
        raise FeatureSurgeryError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[TIME_COL].is_monotonic_increasing:
        raise FeatureSurgeryError("standard_quote_tick bars are not chronologically sorted; refusing to silently reorder them.")

    starts = pd.DatetimeIndex(events[TIME_COL])
    bar_index = pd.DatetimeIndex(bars[TIME_COL])
    ends = bar_index[_snap_to_bar_clock(_label_end_times(events), bar_index)]
    close = bars["Close"].to_numpy(dtype="float64")

    if len(starts) == 0:
        raise FeatureSurgeryError("No labelled events were supplied for AFML weighting.")
    if (ends.asi8 < starts.asi8).any():
        raise FeatureSurgeryError("At least one event has t1 earlier than t0.")
    if not np.isfinite(close).all() or (close <= 0.0).any():
        raise FeatureSurgeryError("Underlying Close must be finite and strictly positive for log returns.")

    start_pos = bar_index.get_indexer(starts)
    end_pos = bar_index.get_indexer(ends)
    if (start_pos < 0).any():
        missing = starts[start_pos < 0][:5].astype(str).tolist()
        raise FeatureSurgeryError(f"Event start timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < 0).any():
        missing = ends[end_pos < 0][:5].astype(str).tolist()
        raise FeatureSurgeryError(f"Event t1 timestamp(s) absent from standard_quote_tick bar clock; examples={missing}")
    if (end_pos < start_pos).any():
        raise FeatureSurgeryError("At least one event maps to an end bar before its start bar.")

    diff = np.zeros(len(bar_index) + 1, dtype="float64")
    np.add.at(diff, start_pos, 1.0)
    np.add.at(diff, end_pos + 1, -1.0)
    concurrency = np.cumsum(diff[:-1])
    active = concurrency > 0.0
    if not active.any():
        raise FeatureSurgeryError("No active event bars found after AFML concurrency construction.")

    inv = np.zeros_like(concurrency)
    inv[active] = 1.0 / concurrency[active]
    inv_prefix = np.concatenate(([0.0], np.cumsum(inv)))
    lengths = (end_pos - start_pos + 1).astype("float64")
    uniqueness = (inv_prefix[end_pos + 1] - inv_prefix[start_pos]) / lengths
    if not np.isfinite(uniqueness).all() or (uniqueness <= 0.0).any() or (uniqueness > 1.0 + 1e-12).any():
        raise FeatureSurgeryError("AFML average uniqueness is invalid/outside (0,1].")

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
        raise FeatureSurgeryError("AFML return-attribution weights are non-finite or sum to zero.")
    normalized = raw_weight * (float(len(raw_weight)) / total)
    if not np.isclose(float(normalized.mean()), 1.0, rtol=1e-10, atol=1e-12):
        raise FeatureSurgeryError(f"AFML normalized weight mean is not 1: {float(normalized.mean())}")

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
            raise FeatureSurgeryError(f"Could not import shared AFML sample-weight engine: {exc}") from exc
        if shared_contract != AFML_SAMPLE_WEIGHT_CONTRACT_VERSION:
            raise FeatureSurgeryError(
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
            raise FeatureSurgeryError(f"Shared AFML sample-weight construction failed: {exc}") from exc

    required = {AFML_AVERAGE_UNIQUENESS_COL, AFML_RETURN_ATTRIBUTION_WEIGHT_COL}
    missing = sorted(required.difference(table.columns))
    if missing:
        raise FeatureSurgeryError(f"AFML weight table missing required columns: {missing}")
    if table.index.has_duplicates:
        raise FeatureSurgeryError("AFML weight table has duplicate event timestamps.")
    if not np.isfinite(table[list(required)].to_numpy(dtype="float64")).all():
        raise FeatureSurgeryError("AFML weight table contains NaN/inf values.")

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
        raise FeatureSurgeryError("Training/event index contains NaT; cannot align AFML weights.")
    missing = idx.difference(table.index)
    if len(missing):
        examples = missing[:5].astype(str).tolist()
        raise FeatureSurgeryError(f"{len(missing)} model event timestamp(s) are absent from AFML weight table; examples={examples}")
    values = table.loc[idx, column].to_numpy(dtype="float64")
    if not np.isfinite(values).all():
        raise FeatureSurgeryError(f"AFML column {column} contains NaN/inf after alignment.")
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
        raise FeatureSurgeryError("Weighted log-loss inputs have mismatched lengths.")
    if not np.isfinite(w).all() or (w < 0.0).any() or float(w.sum()) <= 0.0:
        raise FeatureSurgeryError("Weighted log-loss sample weights are invalid.")
    loss = -(y_arr * np.log(p) + (1.0 - y_arr) * np.log(1.0 - p))
    return float(np.average(loss, weights=w))



def make_train_weights(df: pd.DataFrame, y: pd.Series, train_idx: np.ndarray) -> np.ndarray:
    """Quantara fit weight = fixed class balancing × canonical AFML return weight."""
    train_idx = np.asarray(train_idx, dtype=int)
    y_train = y.iloc[train_idx]
    train_df = df.iloc[train_idx]
    class_w = compute_sample_weight(class_weight="balanced", y=y_train)
    financial_w = _afml_return_weights(train_df)
    final_w = np.asarray(class_w, dtype="float64") * financial_w
    mean_w = float(np.mean(final_w)) if len(final_w) else 0.0
    if not np.isfinite(final_w).all() or not np.isfinite(mean_w) or mean_w <= 0.0:
        raise FeatureSurgeryError("Step 01 training sample weights are invalid/non-positive.")
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


# =============================================================================
# MLDP A4: Marcenko-Pastur denoising + ONC clustering (Colab-portable inline)
# =============================================================================
# Verbatim from shared/colab_portable/denoise_onc_snippet.py (see
# docs/mldp/A4_DENOISE_ONC_UTILITY.md). Inlined rather than imported because
# this script runs standalone on Google Colab and cannot import the rest of
# `shared/` at runtime -- see docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md's
# "Operational note" for the same constraint applied to purge/embargo.
# tests/test_step01_colab_portable_snippet_sync.py asserts this block stays
# byte-for-byte identical to the canonical file.
# --- BEGIN denoise_onc_snippet.py verbatim ---


class DenoiseONCError(RuntimeError):
    """Raised on invalid inputs or a failed fit in this module."""


# ---------------------------------------------------------------------------
# Correlation/covariance utilities (MLAM p.29, standard rescaling helpers)
# ---------------------------------------------------------------------------


def cov_to_corr(cov: np.ndarray) -> np.ndarray:
    """Derive the correlation matrix from a covariance matrix."""
    std = np.sqrt(np.diag(cov))
    corr = cov / np.outer(std, std)
    corr[corr < -1] = -1
    corr[corr > 1] = 1
    return corr


def corr_to_cov(corr: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Recover a covariance matrix from a correlation matrix and std vector."""
    return corr * np.outer(std, std)


def get_pca(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Eigendecomposition of a Hermitian matrix, sorted by descending
    eigenvalue. Returns (eVal, eVec) where eVal is an N x N diagonal matrix.
    """
    eigenvalues, eigenvectors = np.linalg.eigh(matrix)
    indices = eigenvalues.argsort()[::-1]
    eigenvalues, eigenvectors = eigenvalues[indices], eigenvectors[:, indices]
    eigenvalues = np.diagflat(eigenvalues)
    return eigenvalues, eigenvectors


# ---------------------------------------------------------------------------
# Marcenko-Pastur theorem (MLAM Section 2.2, book p.24-26)
# ---------------------------------------------------------------------------


def mp_pdf(var: float, q: float, pts: int) -> pd.Series:
    """Theoretical Marcenko-Pastur probability density function.

    q = T/N (observations / variables). Eigenvalues outside [eMin, eMax] are
    consistent with signal, not randomness.
    """
    if q <= 0:
        raise DenoiseONCError("q (T/N) must be > 0.")
    e_min = var * (1 - (1.0 / q) ** 0.5) ** 2
    e_max = var * (1 + (1.0 / q) ** 0.5) ** 2
    e_val = np.linspace(e_min, e_max, pts)
    pdf = q / (2 * np.pi * var * e_val) * ((e_max - e_val) * (e_val - e_min)) ** 0.5
    pdf = np.nan_to_num(pdf, nan=0.0)
    return pd.Series(pdf, index=e_val)


def fit_kde(
    observations: np.ndarray,
    bwidth: float = 0.25,
    kernel: str = "gaussian",
    x: np.ndarray | None = None,
) -> pd.Series:
    """Fit a kernel density estimate to a series of observations (typically
    empirical eigenvalues), evaluated at `x` if supplied."""
    obs = np.asarray(observations).reshape(-1, 1)
    kde = KernelDensity(kernel=kernel, bandwidth=bwidth).fit(obs)
    if x is None:
        x = np.unique(obs).reshape(-1, 1)
    else:
        x = np.asarray(x).reshape(-1, 1)
    log_prob = kde.score_samples(x)
    return pd.Series(np.exp(log_prob), index=x.flatten())


def _err_pdfs(var: float, eigenvalues: np.ndarray, q: float, bwidth: float, pts: int = 1000) -> float:
    theoretical = mp_pdf(var, q, pts)
    empirical = fit_kde(eigenvalues, bwidth, x=theoretical.index.values)
    return float(np.sum((empirical - theoretical) ** 2))


def find_max_eval(eigenvalues: np.ndarray, q: float, bwidth: float) -> tuple[float, float]:
    """Fit the Marcenko-Pastur distribution's free parameter (var, the
    fraction of variance attributable to noise) by minimizing the SSE
    between the theoretical MP PDF and a KDE of the empirical eigenvalue
    distribution (MLAM p.27-28). Returns (eMax, var)."""
    result = minimize(
        lambda v: _err_pdfs(v[0], eigenvalues, q, bwidth),
        x0=np.array([0.5]),
        bounds=((1e-5, 1 - 1e-5),),
    )
    var = float(result.x[0]) if result.success else 1.0
    e_max = var * (1 + (1.0 / q) ** 0.5) ** 2
    return e_max, var


# ---------------------------------------------------------------------------
# Denoising (MLAM Section 2.5, book p.28-31)
# ---------------------------------------------------------------------------


def denoised_corr_constant_residual_eigenvalue(
    eigenvalues: np.ndarray, eigenvectors: np.ndarray, n_facts: int
) -> np.ndarray:
    """Constant residual eigenvalue method (MLAM p.29) -- the book's
    preferred/canonical denoising method. Replaces every eigenvalue below
    the fitted noise cutoff with their common average, preserving the
    matrix's trace, then rescales the diagonal back to 1s."""
    eigenvalues_ = np.diag(eigenvalues).copy()
    if n_facts < len(eigenvalues_):
        eigenvalues_[n_facts:] = eigenvalues_[n_facts:].sum() / float(len(eigenvalues_) - n_facts)
    eigenvalues_ = np.diag(eigenvalues_)
    corr = eigenvectors @ eigenvalues_ @ eigenvectors.T
    return cov_to_corr(corr)


def denoised_corr_targeted_shrinkage(
    eigenvalues: np.ndarray, eigenvectors: np.ndarray, n_facts: int, alpha: float = 0.0
) -> np.ndarray:
    """Targeted shrinkage method (MLAM p.29-31) -- an alternative denoising
    method that shrinks only the noise eigenvector block toward its
    diagonal, controlled by alpha (alpha=0 gives total shrinkage of the
    noise block to its diagonal). Presented as an alternative to, not
    superior to, the constant-residual-eigenvalue method above."""
    eigenvalues_l = eigenvalues[:n_facts, :n_facts]
    eigenvectors_l = eigenvectors[:, :n_facts]
    eigenvalues_r = eigenvalues[n_facts:, n_facts:]
    eigenvectors_r = eigenvectors[:, n_facts:]
    corr_left = eigenvectors_l @ eigenvalues_l @ eigenvectors_l.T
    corr_right = eigenvectors_r @ eigenvalues_r @ eigenvectors_r.T
    corr = corr_left + alpha * corr_right + (1 - alpha) * np.diag(np.diag(corr_right))
    return corr


def detone_corr(
    corr: np.ndarray, eigenvalues: np.ndarray, eigenvectors: np.ndarray, market_components: int = 1
) -> np.ndarray:
    """Remove the dominant "market" component(s) before clustering (MLAM
    p.30-32). The result is singular by construction and must not be used
    directly for mean-variance optimization -- map optimal weights back via
    the surviving (non-market) eigenvectors instead."""
    market_eigenvalues = eigenvalues[:market_components, :market_components]
    market_eigenvectors = eigenvectors[:, :market_components]
    corr_market = market_eigenvectors @ market_eigenvalues @ market_eigenvectors.T
    corr_detoned = corr - corr_market
    return cov_to_corr(corr_detoned)


def denoise_correlation(
    corr: np.ndarray,
    n_obs: int,
    *,
    bwidth: float = 0.01,
    detone: bool = False,
    market_components: int = 1,
    method: str = "constant_residual_eigenvalue",
    alpha: float = 0.0,
) -> dict[str, Any]:
    """Top-level orchestration: fit the Marcenko-Pastur noise cutoff on an
    empirical correlation matrix, denoise it, and optionally detone it.

    Parameters
    ----------
    corr : N x N empirical correlation matrix.
    n_obs : T, the number of observations the correlation matrix was
        estimated from (q = T/N feeds the Marcenko-Pastur fit).
    method : "constant_residual_eigenvalue" (canonical/recommended) or
        "targeted_shrinkage" (alternative).

    Returns a dict with the denoised (and, if requested, detoned)
    correlation matrix plus fit diagnostics (n_facts, fitted noise variance,
    condition numbers before/after) so callers can audit the fit rather than
    trust it blindly.
    """
    corr = np.asarray(corr, dtype="float64")
    n = corr.shape[0]
    if corr.shape != (n, n):
        raise DenoiseONCError("corr must be a square matrix.")
    if n_obs <= n:
        raise DenoiseONCError(
            f"n_obs (T={n_obs}) must exceed the number of variables (N={n}) "
            "for the Marcenko-Pastur fit to be meaningful (q=T/N must be > 1)."
        )
    q = float(n_obs) / float(n)

    eigenvalues, eigenvectors = get_pca(corr)
    raw_eigenvalues = np.diag(eigenvalues)
    condition_number_before = float(raw_eigenvalues.max() / max(raw_eigenvalues.min(), 1e-16))

    e_max, fitted_var = find_max_eval(raw_eigenvalues, q, bwidth)
    # Snippet 2.4: nFacts = eigenvalues above lambda+, no floor (D076); 0 gives the identity matrix.
    n_facts = int((raw_eigenvalues > e_max).sum())

    if method == "constant_residual_eigenvalue":
        denoised = denoised_corr_constant_residual_eigenvalue(eigenvalues, eigenvectors, n_facts)
    elif method == "targeted_shrinkage":
        denoised = denoised_corr_targeted_shrinkage(eigenvalues, eigenvectors, n_facts, alpha)
    else:
        raise DenoiseONCError(f"Unknown denoising method: {method!r}")

    denoised_eigenvalues_diag, denoised_eigenvectors = get_pca(denoised)
    denoised_eigenvalues = np.diag(denoised_eigenvalues_diag)
    condition_number_after = float(
        denoised_eigenvalues.max() / max(denoised_eigenvalues.min(), 1e-16)
    )

    result: dict[str, Any] = {
        "denoised_corr": denoised,
        "q": q,
        "n_facts": n_facts,
        "fitted_noise_variance": fitted_var,
        "e_max": e_max,
        "condition_number_before": condition_number_before,
        "condition_number_after": condition_number_after,
        "detoned": False,
    }

    if detone and n_facts == 0:
        result["detone_skipped"] = "no eigenvalue above lambda+: no market component to remove"
    elif detone:
        if market_components > n_facts:
            raise DenoiseONCError(
                "market_components cannot exceed the number of signal "
                f"factors found (n_facts={n_facts}). Detoning all detected "
                "signal (market_components == n_facts) is valid -- it "
                "leaves a fully singular, noise-only matrix by design."
            )
        detoned = detone_corr(
            denoised, denoised_eigenvalues_diag, denoised_eigenvectors, market_components
        )
        result["detoned_corr"] = detoned
        result["detoned"] = True

    return result


# ---------------------------------------------------------------------------
# Optimal Number of Clusters -- ONC (MLAM Section 4.4, book p.54-59)
# ---------------------------------------------------------------------------


def _cluster_quality(silhouette: np.ndarray) -> float:
    std = silhouette.std()
    if std <= 0 or not np.isfinite(std):
        return float("-inf")
    return float(silhouette.mean() / std)


def cluster_kmeans_base(
    corr0: pd.DataFrame,
    max_num_clusters: int = 10,
    n_init: int = 10,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    """ONC base clustering (MLAM p.55-57): a double loop over random k-means
    initializations and candidate cluster counts k, scoring each attempt by
    the silhouette-based quality metric q = mean(silhouette)/std(silhouette)
    and keeping only the single best-scoring (init, k) combination."""
    corr0 = corr0.copy()
    x = ((1 - corr0.fillna(0)) / 2.0) ** 0.5
    max_num_clusters = min(max_num_clusters, corr0.shape[1] - 1)
    if max_num_clusters < 2:
        raise DenoiseONCError("Need at least 3 items to form 2+ clusters.")

    best_stat = float("-inf")
    best_silh: pd.Series | None = None
    best_kmeans: KMeans | None = None
    rng = np.random.default_rng(random_state)

    for _ in range(n_init):
        for k in range(2, max_num_clusters + 1):
            seed = int(rng.integers(0, 2**31 - 1))
            kmeans_ = KMeans(n_clusters=k, n_init=1, random_state=seed).fit(x)
            silh_ = silhouette_samples(x, kmeans_.labels_)
            stat = _cluster_quality(silh_)
            if stat > best_stat:
                best_stat = stat
                best_silh = pd.Series(silh_, index=x.index)
                best_kmeans = kmeans_

    if best_kmeans is None or best_silh is None:
        raise DenoiseONCError("ONC base clustering failed to find any valid clustering.")

    new_idx = [corr0.index[i] for i in np.argsort(best_kmeans.labels_)]
    corr1 = corr0.loc[new_idx, new_idx]
    clstrs = {
        i: corr0.columns[np.where(best_kmeans.labels_ == i)[0]].tolist()
        for i in np.unique(best_kmeans.labels_)
    }
    return corr1, clstrs, best_silh


def _make_new_outputs(
    corr0: pd.DataFrame,
    clstrs: dict[int, list[Any]],
    clstrs2: dict[int, list[Any]],
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    clstrs_new: dict[int, list[Any]] = {}
    for i in clstrs.keys():
        clstrs_new[len(clstrs_new)] = list(clstrs[i])
    for i in clstrs2.keys():
        clstrs_new[len(clstrs_new)] = list(clstrs2[i])
    new_idx = [j for members in clstrs_new.values() for j in members]
    corr_new = corr0.loc[new_idx, new_idx]
    x = ((1 - corr0.fillna(0)) / 2.0) ** 0.5
    labels = pd.Series(index=corr0.index, dtype="int64")
    for cluster_id, members in clstrs_new.items():
        for member in members:
            labels.loc[member] = cluster_id
    silh_new = pd.Series(
        silhouette_samples(x, labels.loc[x.index].to_numpy()), index=x.index
    )
    return corr_new, clstrs_new, silh_new


def cluster_kmeans_top(
    corr0: pd.DataFrame,
    max_num_clusters: int | None = None,
    n_init: int = 10,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    """Full ONC algorithm (MLAM p.55-59): base clustering, then recursive
    re-clustering of only the below-average-quality clusters, accepted only
    if it genuinely improves overall quality (else the base clustering is
    returned unchanged)."""
    if max_num_clusters is None:
        max_num_clusters = corr0.shape[1] - 1
    corr1, clstrs, silh = cluster_kmeans_base(
        corr0,
        max_num_clusters=min(max_num_clusters, corr0.shape[1] - 1),
        n_init=n_init,
        random_state=random_state,
    )
    cluster_t_stats = {
        i: _cluster_quality(silh.loc[members].to_numpy()) for i, members in clstrs.items()
    }
    t_stat_mean = float(np.mean(list(cluster_t_stats.values())))
    redo_clusters = [i for i, stat in cluster_t_stats.items() if stat < t_stat_mean]

    if len(redo_clusters) <= 1:
        return corr1, clstrs, silh

    keys_redo = [j for i in redo_clusters for j in clstrs[i]]
    corr_tmp = corr0.loc[keys_redo, keys_redo]
    redo_t_stat_mean = float(np.mean([cluster_t_stats[i] for i in redo_clusters]))
    corr2, clstrs2, _silh2 = cluster_kmeans_top(
        corr_tmp,
        max_num_clusters=min(max_num_clusters, corr_tmp.shape[1] - 1),
        n_init=n_init,
        random_state=random_state,
    )
    kept_clusters = {i: members for i, members in clstrs.items() if i not in redo_clusters}
    corr_new, clstrs_new, silh_new = _make_new_outputs(corr0, kept_clusters, clstrs2)
    new_t_stat_mean = float(
        np.mean(
            [
                _cluster_quality(silh_new.loc[members].to_numpy())
                for members in clstrs_new.values()
            ]
        )
    )
    if new_t_stat_mean <= redo_t_stat_mean:
        return corr1, clstrs, silh
    return corr_new, clstrs_new, silh_new


# --- END denoise_onc_snippet.py verbatim ---

# ONC search cap: testing every possible k up to N-1 candidate features is
# not computationally practical for ~170 candidate features on Colab. MLAM
# does not mandate an exhaustive k search; NCO's own convention (MLAM Ch.7)
# is N/2 as a practical cap since singleton clusters can't be the target of
# further reclustering either way. Kept explicit and named so it is an
# acknowledged practical constraint, not a silent shortcut.
ONC_MAX_CLUSTERS_CAP = 30
# Marcenko-Pastur KDE bandwidth for the feature-correlation denoising fit.
DENOISE_BWIDTH = 0.02


def build_feature_clusters(x: pd.DataFrame) -> tuple[dict[int, list[str]], pd.DataFrame]:
    """MLDP A4: Marcenko-Pastur denoising + ONC, replacing the prior raw
    Spearman correlation + fixed-threshold Ward-linkage clustering. See
    docs/mldp/A4_DENOISE_ONC_UTILITY.md."""
    if x.shape[1] == 1:
        corr_df = pd.DataFrame([[1.0]], index=x.columns, columns=x.columns)
        return {1: list(x.columns)}, corr_df

    corr_matrix, _ = stats.spearmanr(x)
    corr_matrix = np.asarray(corr_matrix)
    if corr_matrix.ndim != 2:
        raise FeatureSurgeryError("Correlation matrix is not 2D; clustering failed.")
    corr_matrix = np.nan_to_num(np.clip(corr_matrix, -1.0, 1.0))
    np.fill_diagonal(corr_matrix, 1.0)
    corr_matrix = (corr_matrix + corr_matrix.T) / 2.0

    n_obs, n_features = x.shape
    if n_obs > n_features:
        denoise_result = denoise_correlation(corr_matrix, n_obs=n_obs, bwidth=DENOISE_BWIDTH)
        denoised_matrix = denoise_result["denoised_corr"]
        denoising_applied = True
    else:
        # Not enough observations relative to candidate features for a
        # meaningful Marcenko-Pastur fit (q=T/N would be <=1) -- fall back to
        # the raw correlation matrix rather than crash or fabricate a fit.
        denoised_matrix = corr_matrix
        denoising_applied = False
        denoise_result = {"n_facts": None, "fitted_noise_variance": None, "q": None}

    corr_df = pd.DataFrame(denoised_matrix, index=x.columns, columns=x.columns)
    max_k = min(ONC_MAX_CLUSTERS_CAP, n_features - 1)
    _reordered, onc_clusters, _silh = cluster_kmeans_top(
        corr_df, max_num_clusters=max_k, n_init=10, random_state=42
    )
    clusters: dict[int, list[str]] = {int(cid): [str(f) for f in feats] for cid, feats in onc_clusters.items()}

    global _LAST_DENOISE_ONC_DIAGNOSTICS
    _LAST_DENOISE_ONC_DIAGNOSTICS = {
        "denoising_applied": denoising_applied,
        "n_facts": denoise_result["n_facts"],
        "fitted_noise_variance": denoise_result["fitted_noise_variance"],
        "q_t_over_n": denoise_result["q"],
        "onc_cluster_count": len(clusters),
        "onc_max_clusters_cap": max_k,
    }
    return clusters, corr_df


_LAST_DENOISE_ONC_DIAGNOSTICS: dict[str, Any] = {}


def get_positive_class_proba(model: xgb.XGBClassifier, x: pd.DataFrame) -> np.ndarray:
    proba = model.predict_proba(x)
    if proba.shape[1] == 1:
        cls = int(model.classes_[0])
        return np.ones(len(x), dtype=float) if cls == 1 else np.zeros(len(x), dtype=float)
    class_to_col = {int(cls): i for i, cls in enumerate(model.classes_)}
    if 1 not in class_to_col:
        return np.zeros(len(x), dtype=float)
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


def run_clustered_mda(df: pd.DataFrame, fixed_features: list[str], cluster_features: list[str]) -> tuple[pd.DataFrame, dict[int, list[str]], pd.DataFrame, dict[str, Any]]:
    """Clustered MDA with AFML-weighted OOS probability-loss deterioration.

    Step 01 retains the existing F1/precision/recall diagnostics and output keys.
    The *importance* calculation itself uses financial observation weights on the
    held-out fold and measures the increase in binary log loss after permutation,
    which is the AFML MDA probability-scoring construction.
    """
    if CombinatorialPurgedCV is None:
        raise FeatureSurgeryError(
            "skfolio.model_selection.CombinatorialPurgedCV is required. "
            f"Original import error: {SKFOLIO_IMPORT_ERROR}"
        )

    x_cluster = df[cluster_features].copy()
    y = df[TARGET_COL].copy()
    clusters, corr_df = build_feature_clusters(x_cluster)

    cv = CombinatorialPurgedCV(
        n_folds=N_FOLDS,
        n_test_folds=N_TEST_FOLDS,
        purged_size=PURGED_SIZE,
        embargo_size=EMBARGO_SIZE,
    )

    mda_scores = {cid: [] for cid in clusters.keys()}
    base_f1_scores = []
    base_precision_scores = []
    base_recall_scores = []
    fold_thresholds = []
    fold_count = 0

    for split_num, (train_idx, test_idx) in enumerate(cv.split(x_cluster), start=1):
        train_idx = flatten_cv_indices(train_idx)
        test_idx = flatten_cv_indices(test_idx)
        train_idx = purge_train_overlaps(train_idx, test_idx, df.index, pd.Series(_label_end_times(df), index=df.index))
        train_idx = apply_embargo(train_idx, test_idx, len(df), embargo_size_for(len(df)))

        if len(train_idx) == 0 or len(test_idx) == 0:
            continue

        if fixed_features:
            x_train = pd.concat([df[fixed_features].iloc[train_idx], x_cluster.iloc[train_idx]], axis=1)
            x_test = pd.concat([df[fixed_features].iloc[test_idx], x_cluster.iloc[test_idx]], axis=1)
        else:
            x_train = x_cluster.iloc[train_idx].copy()
            x_test = x_cluster.iloc[test_idx].copy()

        y_train = y.iloc[train_idx]
        y_test = y.iloc[test_idx]
        if y_train.nunique() < 2:
            continue

        weights = make_train_weights(df, y, train_idx)
        model = fit_model_safely(make_model(XGB_PARAMS), x_train, y_train, weights)

        # Preserve Quantara's existing train-fold-only F1 diagnostics.
        train_proba = get_positive_class_proba(model, x_train)
        best_thr, _ = find_best_f1_threshold(y_train, train_proba)
        fold_thresholds.append(best_thr)

        base_proba = get_positive_class_proba(model, x_test)
        base_metrics = score_at_threshold(y_test, base_proba, best_thr)
        base_f1_scores.append(base_metrics["f1"])
        base_precision_scores.append(base_metrics["precision"])
        base_recall_scores.append(base_metrics["recall"])
        fold_count += 1

        # MDA scoring uses canonical AFML financial observation weights on OOS rows.
        test_financial_w = _afml_return_weights(df.iloc[test_idx])
        base_loss = _weighted_binary_log_loss(y_test, base_proba, test_financial_w)

        for cid, feats in clusters.items():
            x_shuffled = x_test.copy()
            perm = RNG.permutation(len(x_shuffled))
            for feat in feats:
                x_shuffled.loc[:, feat] = x_shuffled[feat].iloc[perm].values
            shuffled_proba = get_positive_class_proba(model, x_shuffled)
            shuffled_loss = _weighted_binary_log_loss(y_test, shuffled_proba, test_financial_w)
            # Positive => permutation worsened OOS loss => informative cluster.
            mda_scores[cid].append(shuffled_loss - base_loss)

    cluster_rows = []
    for cid, feats in clusters.items():
        values = mda_scores[cid]
        cluster_rows.append({
            "cluster_id": int(cid),
            "n_features": int(len(feats)),
            "avg_mda": float(np.mean(values)) if values else np.nan,
            "std_mda": float(np.std(values, ddof=1)) if len(values) > 1 else np.nan,
            "features": feats,
        })

    report_df = pd.DataFrame(cluster_rows).sort_values("avg_mda", ascending=False).reset_index(drop=True)
    score_summary = {
        "mean_base_f1": float(np.mean(base_f1_scores)) if base_f1_scores else np.nan,
        "mean_base_precision": float(np.mean(base_precision_scores)) if base_precision_scores else np.nan,
        "mean_base_recall": float(np.mean(base_recall_scores)) if base_recall_scores else np.nan,
        "mean_train_selected_threshold": float(np.mean(fold_thresholds)) if fold_thresholds else np.nan,
        "fold_count_used": int(fold_count),
    }
    return report_df, clusters, corr_df, score_summary



def iterative_feature_surgery(df: pd.DataFrame, fixed_features: list[str], initial_candidates: list[str]):
    current_features = list(initial_candidates)
    iteration_rows = []
    final_report_df = pd.DataFrame()
    final_clusters: dict[int, list[str]] = {}
    final_corr_df = pd.DataFrame()
    final_score_summary: dict[str, Any] = {}

    for iteration in range(1, MAX_ITERATIONS + 1):
        if len(current_features) < MIN_CANDIDATE_FEATURES:
            print(f"Stopping: candidate feature count below minimum ({MIN_CANDIDATE_FEATURES}).", flush=True)
            break

        print("\n" + "-" * 72, flush=True)
        print(f"ITERATION {iteration}", flush=True)
        print(f"Current candidate feature count: {len(current_features):,}", flush=True)

        report_df, clusters, corr_df, score_summary = run_clustered_mda(
            df=df,
            fixed_features=fixed_features,
            cluster_features=current_features,
        )
        final_report_df = report_df.copy()
        final_clusters = clusters
        final_corr_df = corr_df.copy()
        final_score_summary = dict(score_summary)

        print(report_df[["cluster_id", "n_features", "avg_mda", "std_mda"]].to_string(index=False), flush=True)
        print(
            f"Base F1={score_summary.get('mean_base_f1', np.nan):.6f} | "
            f"Precision={score_summary.get('mean_base_precision', np.nan):.6f} | "
            f"Recall={score_summary.get('mean_base_recall', np.nan):.6f} | "
            f"Mean train-selected threshold={score_summary.get('mean_train_selected_threshold', np.nan):.4f} | "
            f"Folds={score_summary.get('fold_count_used', 0)}",
            flush=True,
        )

        for _, row in report_df.iterrows():
            iteration_rows.append({
                "iteration": iteration,
                "cluster_id": int(row["cluster_id"]),
                "n_features": int(row["n_features"]),
                "avg_mda": float(row["avg_mda"]) if pd.notna(row["avg_mda"]) else np.nan,
                "std_mda": float(row["std_mda"]) if pd.notna(row["std_mda"]) else np.nan,
                "features": row["features"],
                "base_f1": score_summary.get("mean_base_f1", np.nan),
                "base_precision": score_summary.get("mean_base_precision", np.nan),
                "base_recall": score_summary.get("mean_base_recall", np.nan),
                "mean_train_selected_threshold": score_summary.get("mean_train_selected_threshold", np.nan),
                "fold_count_used": score_summary.get("fold_count_used", 0),
            })

        negative_clusters = report_df[report_df["avg_mda"] < 0].copy()
        if negative_clusters.empty:
            print("Stopping: no negative MDA clusters remain.", flush=True)
            break

        worst_row = negative_clusters.sort_values("avg_mda", ascending=True).iloc[0]
        worst_cluster_id = int(worst_row["cluster_id"])
        worst_score = float(worst_row["avg_mda"])
        worst_features = clusters[worst_cluster_id]

        print(f"Removing worst cluster {worst_cluster_id} | avg_mda={worst_score:.6f}", flush=True)
        print(f"Features removed: {worst_features}", flush=True)
        current_features = [f for f in current_features if f not in worst_features]

        if len(current_features) == 0:
            print("Stopping: no candidate features remain after removal.", flush=True)
            break

    selected_features = list(fixed_features) + list(current_features)
    iteration_report_df = pd.DataFrame(iteration_rows)
    return selected_features, iteration_report_df, final_report_df, final_clusters, final_corr_df, final_score_summary


def write_outputs(
    selected_features: list[str],
    iteration_report_df: pd.DataFrame,
    final_report_df: pd.DataFrame,
    final_clusters: dict[int, list[str]],
    final_corr_df: pd.DataFrame,
    final_score_summary: dict[str, Any],
    diagnostics: dict[str, Any],
    rows_used: int,
    date_start: Any,
    date_end: Any,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not iteration_report_df.empty:
        out = iteration_report_df.copy()
        out["features"] = out["features"].apply(lambda x: ", ".join(x) if isinstance(x, list) else str(x))
        out.to_csv(OUTPUT_ITERATION_REPORT_CSV, index=False)

    if not final_report_df.empty:
        out = final_report_df.copy()
        out["features"] = out["features"].apply(lambda x: ", ".join(x) if isinstance(x, list) else str(x))
        out.to_csv(OUTPUT_FINAL_CLUSTER_REPORT_CSV, index=False)

    if not final_corr_df.empty:
        final_corr_df.to_csv(OUTPUT_CORRELATION_MATRIX_CSV)

    # V1-compatible selected-features artifact: pure list.
    save_json(OUTPUT_SELECTED_FEATURES_JSON, selected_features)

    afml_provenance = _afml_provenance_snapshot()

    manifest = {
        "created_at": now_utc(),
        "edge_id": EDGE_ID,
        "run_mode": RUN_MODE,
        "script": display_path(THIS_FILE),
        "blueprint_stage": "feature_surgery_before_model_training",
        "feature_engineering_performed_here": False,
        "input_model_rows": display_path(RESOLUTION_CSV_PATH),
        "input_feature_schema": display_path(FEATURE_SCHEMA_PATH),
        "afml_provenance": afml_provenance,
        "rows_used": int(rows_used),
        "date_start": str(date_start),
        "date_end": str(date_end),
        "fixed_features": diagnostics["fixed_features"],
        "initial_candidate_feature_count": diagnostics["candidate_feature_count"],
        "selected_feature_count": int(len(selected_features)),
        "selected_features": selected_features,
        "final_score_summary": final_score_summary,
        "feature_schema_diagnostics": diagnostics,
        "side_specialization": {
            "edge_side": SIDE,
            "side_value": SIDE_VALUE,
            "side_column_forced_as_model_input": False,
        },
        "scoring": {
            "primary_metric": "neg_log_loss (AFML-weighted, OOS)",
            "mda_formula": "(scr0 - scr1) / -scr1, scr = weighted OOS neg log loss; each cluster feature shuffled independently (MLAM Snippet 6.5)",
            "mda_scoring_weight": "canonical_afml_return_attribution_weight",
            "selection_rule": "B: keep a cluster if clustered MDA mean - 1.645 x standard error > 0 (MLAM p.79, p.89)",
        },
        "cpcv": {
            "n_folds": N_FOLDS,
            "n_test_folds": N_TEST_FOLDS,
            "purged_size": PURGED_SIZE,
            "embargo_size": EMBARGO_SIZE,
            "manual_embargo_size": MANUAL_EMBARGO_SIZE,
            "embargo_rule": EMBARGO_RULE,
            "purge_t1": PURGE_T1_RULE,
            "mode": "manual_t1_overlap_purge_after_cpcv_split_generation",
        },
        "clustering_method": "mlam_onc_on_normalised_variation_of_information_pca_reduced_p55",
        "feature_importance_method": "mlam_clustered_mda_and_mdi_single_pass",
        "feature_importance_module": "shared/features/clustered_feature_importance.py",
        "estimator": final_score_summary.get("estimator"),
        "cv": final_score_summary.get("cv"),
        "oos_neg_log_loss_weighted": final_score_summary.get("oos_neg_log_loss_weighted"),
        "oos_auc": final_score_summary.get("oos_auc"),
        "deviations": ["D039 sequential bootstrap + purged CV", "D040 seeded k-means", "D041 min_weight_fraction_leaf 0.05", "D045 5 MDA shuffles per fold",
                       "D042 no residual features", "D043 no SFI"],
        "outputs": {
            "selected_features": display_path(OUTPUT_SELECTED_FEATURES_JSON),
            "iteration_report": display_path(OUTPUT_ITERATION_REPORT_CSV),
            "final_cluster_report": display_path(OUTPUT_FINAL_CLUSTER_REPORT_CSV),
            "correlation_matrix": display_path(OUTPUT_CORRELATION_MATRIX_CSV),
            "manifest": display_path(OUTPUT_MANIFEST_JSON),
        },
        "next_ordered_step": "02_train_meta_model_optuna.py",
        "notes": [
            "This script ports V1 Step 6C into V2.",
            "It consumes exported V2 side-specialized model rows only.",
            "It uses feature_schema.json as the model-input authority.",
            "It does not create new features.",
            "Edge side is fixed by the edge package, so side is treated as constant metadata, not a forced model input.",
            "AFML Ch.8 / MLAM Ch.6 (2026-10-24): one pass of clustered MDA/MDI; clusters failing rule B are discarded (MLAM p.89).",
            "The selected features should be consumed by the next V2 training step, not edited manually.",
            "Flat-Colab mode is retired for Step 01 (it imports shared/features/clustered_feature_importance.py).",
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
        "denoise_onc_diagnostics": _LAST_DENOISE_ONC_DIAGNOSTICS,
        "model_rows_sha256": _phase3a1_sha256_file(RESOLUTION_CSV_PATH),
        "feature_schema_sha256": _phase3a1_sha256_file(FEATURE_SCHEMA_PATH),
        "selected_features_sha256": _phase3a1_sha256_file(OUTPUT_SELECTED_FEATURES_JSON),
        "afml_sample_weight_contract_version": afml_provenance["contract_version"],
        "standard_quote_tick_bars_sha256": afml_provenance["standard_quote_tick_bars_sha256"],
        "afml_event_weight_table_sha256": afml_provenance["event_weight_table_sha256"],
        "phase3a_step01_provenance_status": "PASS",
    })
    save_json(OUTPUT_MANIFEST_JSON, manifest)


def run_mlam_clustered_feature_importance(df: pd.DataFrame, fixed_features: list[str], candidate_features: list[str]):
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
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars[TIME_COL], utc=True, errors="coerce"))
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


def main() -> None:
    print("=" * 80, flush=True)
    print(f"V2 STEP 01: ITERATIVE CLUSTERED MDA FEATURE SURGERY — {EDGE_ID}", flush=True)
    print("=" * 80, flush=True)
    print(f"Run mode: {RUN_MODE}", flush=True)
    print(f"Working directory: {CWD}", flush=True)
    print(f"Model rows: {RESOLUTION_CSV_PATH}", flush=True)
    print(f"Feature schema: {FEATURE_SCHEMA_PATH}", flush=True)
    print(f"Output directory: {OUTPUT_DIR}", flush=True)

    schema_features = load_feature_schema_allowed_features()
    df = load_model_rows()

    for col in schema_features:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    fixed_features, candidate_features, diagnostics = choose_candidate_features(df, schema_features)
    usable_cols = fixed_features + candidate_features
    before = len(df)
    df = df.dropna(subset=usable_cols).copy()
    after = len(df)
    if after == 0:
        raise FeatureSurgeryError("All rows dropped after selected candidate/fixed feature NaN filtering.")

    print(f"Rows before feature NaN filter: {before:,}", flush=True)
    print(f"Rows after feature NaN filter : {after:,}", flush=True)
    print(f"Date range: {df.index.min()} -> {df.index.max()}", flush=True)
    print(f"Schema allowed features: {len(schema_features):,}", flush=True)
    print(f"Fixed features: {fixed_features}", flush=True)
    print(f"Candidate features for clustering: {len(candidate_features):,}", flush=True)

    if RUN_MODE == "flat_colab":
        raise FeatureSurgeryError("Flat-Colab mode is retired for Step 01 (AFML Ch.8 walkthrough 2026-10-24); run in repo mode.")
    selected_features, iteration_report_df, final_report_df, final_clusters, final_corr_df, final_score_summary = run_mlam_clustered_feature_importance(
        df=df,
        fixed_features=fixed_features,
        candidate_features=candidate_features,
    )

    write_outputs(
        selected_features=selected_features,
        iteration_report_df=iteration_report_df,
        final_report_df=final_report_df,
        final_clusters=final_clusters,
        final_corr_df=final_corr_df,
        final_score_summary=final_score_summary,
        diagnostics=diagnostics,
        rows_used=len(df),
        date_start=df.index.min(),
        date_end=df.index.max(),
    )

    print("\n✅ V2 clustered MDA feature surgery completed.", flush=True)
    print(f"Selected features: {len(selected_features):,}", flush=True)
    print(f"Saved selected features: {OUTPUT_SELECTED_FEATURES_JSON}", flush=True)
    print(f"Saved manifest: {OUTPUT_MANIFEST_JSON}", flush=True)
    print("Next ordered step: 02_train_meta_model_optuna.py", flush=True)

    if RUN_MODE == "flat_colab":
        print("\nFlat Colab output files to download:", flush=True)
        for path in [
            OUTPUT_SELECTED_FEATURES_JSON,
            OUTPUT_MANIFEST_JSON,
            OUTPUT_ITERATION_REPORT_CSV,
            OUTPUT_FINAL_CLUSTER_REPORT_CSV,
            OUTPUT_CORRELATION_MATRIX_CSV,
        ]:
            print(f"  - {path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except FeatureSurgeryError as exc:
        raise SystemExit(f"❌ {exc}") from exc
