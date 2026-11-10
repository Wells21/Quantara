"""Phase C.2 compact exact-feature continuation candidate.

This module is intentionally NOT wired into production routing.  It provides a
single-closed-bar feature-state transition that is certified offline before any
future Phase C routing change.

Architecture rules
------------------
* Phase A/B remain the sole owners of FFD/HMM continuation and generation.
* This feature consumer never reads dependency CSVs and never refreshes them.
* One update consumes exactly one closed bar plus one immutable READY dependency
  generation supplied by the caller.
* HMM-ineligible raw bars preserve the authoritative left-join behavior: regime
  columns are NaN; no synthetic or fill-forward regime is allowed.
* State contains only bounded compute context and recursive state.  It does not
  retain/pickle a 6,000-row wide feature history on every bar.
* C.1's frozen manifest-driven feature contract determines the required output
  universe.  No asset list or edge count is hardcoded here.

The low-level mathematical transition currently reuses the retired native
one-row kernel only as a mathematical primitive.  Its old dependency-refresh,
per-edge routing, feature-history, and certification wrappers are NOT used.
C.2 certification fingerprints both modules and proves the current manifest-selected
batch authority before any later production activation.  The current edge count is
never encoded into the engine.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import pickle
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from shared.edges import native_one_row_feature_engine as _legacy_math
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

ENGINE_VERSION = "phase_c2_compact_incremental_features_v1_3_bounded_batch_authorities"
STATE_SCHEMA_VERSION = "phase_c2_compact_feature_state_v1_3"
DEFAULT_RAW_CONTEXT_ROWS = 260
DEFAULT_FEATURE_CONTEXT_ROWS = 512
FFD_COLUMNS = tuple(_legacy_math.FFD_COLUMNS)
REGIME_COLUMNS = tuple(_legacy_math.REGIME_COLUMNS)
ROLLING_BATCH_AUTHORITY_ROWS = 6000
SHIFTED_FFD_Z_WINDOWS = (20, 50)


class PhaseCIncrementalFeatureError(RuntimeError):
    """Raised whenever the uncertified Phase C.2 candidate cannot be trusted."""


def _utc_timestamp(value: Any, *, field: str) -> pd.Timestamp:
    try:
        ts = pd.Timestamp(value)
    except Exception as exc:
        raise PhaseCIncrementalFeatureError(f"{field} is not a timestamp: {value!r}") from exc
    if pd.isna(ts):
        raise PhaseCIncrementalFeatureError(f"{field} is NaT")
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def implementation_fingerprint() -> dict[str, str]:
    here = Path(__file__).resolve()
    legacy = Path(_legacy_math.__file__).resolve()
    return {
        "engine_version": ENGINE_VERSION,
        "phase_c2_engine_sha256": _file_sha256(here),
        "native_math_kernel_sha256": _file_sha256(legacy),
    }


def load_phase_c1_contract(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PhaseCIncrementalFeatureError(
            f"Could not read Phase C.1 contract {target}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PhaseCIncrementalFeatureError("Phase C.1 contract must be a JSON object")
    if payload.get("status") != "PASS":
        raise PhaseCIncrementalFeatureError(
            f"Phase C.1 contract status must be PASS, got {payload.get('status')!r}"
        )
    if payload.get("runtime_routing_enabled") is not False:
        raise PhaseCIncrementalFeatureError(
            "Phase C.1 contract unexpectedly enables runtime routing"
        )
    if not str(payload.get("contract_sha256") or ""):
        raise PhaseCIncrementalFeatureError("Phase C.1 contract has no contract_sha256")
    return payload


def asset_contract(contract: Mapping[str, Any], asset: str) -> dict[str, Any]:
    key = str(asset).upper()
    row = (contract.get("assets") or {}).get(key)
    if not isinstance(row, Mapping):
        raise PhaseCIncrementalFeatureError(
            f"{key}: asset is absent from the frozen Phase C.1 contract"
        )
    return dict(row)


def required_asset_features(contract: Mapping[str, Any], asset: str) -> list[str]:
    row = asset_contract(contract, asset)
    values = [str(x) for x in row.get("topological_feature_order") or []]
    declared = int(row.get("declared_feature_name_count") or 0)
    if not values or len(values) != declared or len(values) != len(set(values)):
        raise PhaseCIncrementalFeatureError(
            f"{str(asset).upper()}: invalid C.1 topological feature order "
            f"declared={declared} actual={len(values)}"
        )
    return values


def addon_asset_features(contract: Mapping[str, Any], asset: str) -> list[str]:
    row = asset_contract(contract, asset)
    return [str(x) for x in row.get("addon_candidate_features") or []]


def _normalize_authority(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        raise PhaseCIncrementalFeatureError("authoritative feature frame is empty/invalid")
    out = frame.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    out = (
        out.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )
    if out.empty:
        raise PhaseCIncrementalFeatureError("authoritative feature frame is empty after normalization")
    return out


def _coerce_bar(bar: Mapping[str, Any] | pd.Series) -> dict[str, Any]:
    row = dict(bar)
    required = ("Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold")
    missing = [name for name in required if name not in row]
    if missing:
        raise PhaseCIncrementalFeatureError(f"closed bar missing required fields: {missing}")
    row["Datetime"] = _utc_timestamp(row["Datetime"], field="bar.Datetime")
    row["Bar_Start"] = _utc_timestamp(row["Bar_Start"], field="bar.Bar_Start")
    for name in ("Open", "High", "Low", "Close", "Ticks", "Threshold"):
        try:
            row[name] = float(row[name])
        except Exception as exc:
            raise PhaseCIncrementalFeatureError(f"bar.{name} is not numeric") from exc
    if not all(np.isfinite(row[name]) for name in ("Open", "High", "Low", "Close")):
        raise PhaseCIncrementalFeatureError("closed bar OHLC must be finite")
    return row


def _finite_mapping(values: Mapping[str, Any], columns: tuple[str, ...], *, label: str) -> dict[str, float]:
    output: dict[str, float] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for name in columns:
        if name not in values:
            missing.append(name)
            continue
        try:
            value = float(values[name])
        except Exception:
            invalid.append(name)
            continue
        if not np.isfinite(value):
            invalid.append(name)
        output[name] = value
    if missing or invalid:
        raise PhaseCIncrementalFeatureError(
            f"{label} dependency values invalid missing={missing} nonfinite={invalid}"
        )
    return output


def _generation_value(generation: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in generation and generation.get(name) is not None:
            return generation.get(name)
    return None


def validate_dependency_generation(
    *,
    asset: str,
    bar_timestamp: Any,
    generation: Mapping[str, Any],
    ffd_values: Mapping[str, Any],
    hmm_values: Mapping[str, Any] | None,
) -> tuple[str, dict[str, float], dict[str, float], bool, pd.Timestamp]:
    """Validate an immutable Phase A/B dependency generation for one feature row."""
    if not isinstance(generation, Mapping):
        raise PhaseCIncrementalFeatureError("dependency generation must be a mapping")
    asset_key = str(asset).upper()
    gen_asset = str(generation.get("asset") or asset_key).upper()
    if gen_asset != asset_key:
        raise PhaseCIncrementalFeatureError(
            f"dependency generation asset mismatch expected={asset_key} got={gen_asset}"
        )
    status = str(generation.get("status") or "READY").upper()
    if status not in {"READY", "PASS"}:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: dependency generation is not READY: {status}"
        )
    generation_id = str(
        _generation_value(generation, "generation_id", "dependency_generation_id") or ""
    ).strip()
    if not generation_id:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: dependency generation id is blank"
        )
    bar_ts = _utc_timestamp(bar_timestamp, field="bar_timestamp")
    raw_target_raw = _generation_value(
        generation, "raw_target", "target_timestamp", "dependency_target", "snapshot_latest"
    )
    if raw_target_raw is None:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: dependency generation has no raw target timestamp"
        )
    raw_target = _utc_timestamp(raw_target_raw, field="generation.raw_target")
    if raw_target != bar_ts:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: dependency raw target mismatch bar={bar_ts.isoformat()} "
            f"target={raw_target.isoformat()}"
        )
    eligible = bool(
        _generation_value(
            generation,
            "target_hmm_eligible",
            "snapshot_latest_hmm_eligible",
            "hmm_eligible",
        )
    )
    hmm_target_raw = _generation_value(
        generation, "hmm_target", "hmm_target_timestamp", "hmm_dependency_target"
    )
    if hmm_target_raw is None:
        hmm_target_raw = bar_ts if eligible else generation.get("previous_hmm_target")
    if hmm_target_raw is None:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: HMM-ineligible generation must still identify latest eligible HMM target"
        )
    hmm_target = _utc_timestamp(hmm_target_raw, field="generation.hmm_target")
    if eligible and hmm_target != bar_ts:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: HMM-eligible target must equal the raw bar target"
        )
    if not eligible and hmm_target >= bar_ts:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: HMM-ineligible latest eligible target must precede raw bar target"
        )
    ffd = _finite_mapping(ffd_values, FFD_COLUMNS, label="FFD")
    if eligible:
        if hmm_values is None:
            raise PhaseCIncrementalFeatureError(
                f"{asset_key}: HMM-eligible feature update has no HMM values"
            )
        hmm = _finite_mapping(hmm_values, REGIME_COLUMNS, label="HMM")
        probs = [hmm[name] for name in REGIME_COLUMNS if name.startswith("Prob_State_")]
        if probs:
            if any(value < -1e-12 or value > 1.0 + 1e-12 for value in probs):
                raise PhaseCIncrementalFeatureError("HMM probability is outside [0, 1]")
            if abs(sum(probs) - 1.0) > 1e-5:
                raise PhaseCIncrementalFeatureError("HMM probabilities do not sum to 1")
    else:
        # Exact authoritative regime integration is a timestamp left merge.  A
        # raw timestamp without an eligible HMM row therefore remains NaN.
        hmm = {name: math.nan for name in REGIME_COLUMNS}
    return generation_id, ffd, hmm, eligible, hmm_target



def _bootstrap_stc_authority(
    close_values: np.ndarray, *, authority_window_rows: int = 6000
) -> dict[str, Any]:
    """Seed STC from the exact installed batch authority on a bounded close ring.

    STC is the one current C.1 feature whose third-party implementation has
    whole-input zero-range semantics plus cumulative 8-decimal Numba rounding.
    Reimplementing that recursion risks a one-last-decimal drift.  C.2 therefore
    keeps only the close inputs needed by the production-sized authority window
    and calls the same installed ``pandas_ta_classic`` implementation for STC.
    This is O(window) for ONE feature, not a 6,000-row rebuild of the 163/177
    feature table, and it preserves exact semantics before any future optimization.
    """
    values = np.asarray(close_values, dtype="float64")
    capacity = int(authority_window_rows)
    if capacity <= 0:
        raise PhaseCIncrementalFeatureError("STC authority window must be positive")
    if values.size < 26:
        raise PhaseCIncrementalFeatureError("STC authority bootstrap requires at least 26 closes")
    ring = values[-capacity:]
    frame = ta.stc(pd.Series(ring, dtype="float64"))
    if frame is None or frame.empty or frame.shape[1] < 1:
        raise PhaseCIncrementalFeatureError("pandas_ta_classic STC authority unavailable")
    value = pd.to_numeric(frame.iloc[:, 0], errors="coerce").iloc[-1]
    if not np.isfinite(float(value)):
        raise PhaseCIncrementalFeatureError("STC authority latest value is non-finite")
    return {
        "mode": "bounded_same_library_rolling_authority",
        "authority_window_rows": capacity,
        "close_ring": [float(x) for x in ring],
        "last_value": float(value),
    }


def _update_stc_authority(
    stc_state: Mapping[str, Any], close_value: float
) -> tuple[float, dict[str, Any]]:
    """Return exact latest STC by recomputing only STC on its bounded close ring."""
    st = dict(stc_state or {})
    capacity = int(st.get("authority_window_rows") or 0)
    ring = list(st.get("close_ring") or [])
    if capacity <= 0 or not ring or len(ring) > capacity:
        raise PhaseCIncrementalFeatureError("STC bounded authority state is invalid")
    ring = (ring + [float(close_value)])[-capacity:]
    if len(ring) < 26:
        raise PhaseCIncrementalFeatureError("STC bounded authority ring is too short")
    frame = ta.stc(pd.Series(np.asarray(ring, dtype="float64")))
    if frame is None or frame.empty or frame.shape[1] < 1:
        raise PhaseCIncrementalFeatureError("pandas_ta_classic STC authority unavailable during update")
    value = pd.to_numeric(frame.iloc[:, 0], errors="coerce").iloc[-1]
    if not np.isfinite(float(value)):
        raise PhaseCIncrementalFeatureError("STC authority latest update is non-finite")
    next_state = {
        "mode": "bounded_same_library_rolling_authority",
        "authority_window_rows": capacity,
        "close_ring": [float(x) for x in ring],
        "last_value": float(value),
    }
    return float(value), next_state


def _shifted_zscore_latest_same_pandas(
    values: list[float] | np.ndarray,
    window: int,
) -> float:
    """Match the batch runtime's pandas rolling/shift z-score at the newest row.

    The authoritative edge expansion implements:
        (x - x.rolling(window).mean().shift(1))
        / (x.rolling(window).std().shift(1) + EPS)

    Although mathematically equivalent to direct mean/std over the prior ``window``
    values, pandas' rolling accumulator can differ by several e-9 on large FFD
    levels.  The rolling 6,000-bar input horizon is therefore part of the exact
    live numerical contract for these two features.
    """
    series = pd.Series(np.asarray(values, dtype="float64"))
    if len(series) < int(window) + 1:
        return math.nan
    mean = series.rolling(int(window)).mean().shift(1)
    std = series.rolling(int(window)).std().shift(1)
    value = (series - mean) / (std + _legacy_math.EPS)
    latest = pd.to_numeric(value, errors="coerce").iloc[-1]
    return float(latest) if np.isfinite(float(latest)) else math.nan


def _bootstrap_ffd_z_authority(
    close_ffd_values: np.ndarray,
    *,
    authority_window_rows: int = ROLLING_BATCH_AUTHORITY_ROWS,
) -> dict[str, Any]:
    """Seed exact shifted FFD z-scores from a bounded production-sized ring."""
    values = np.asarray(close_ffd_values, dtype="float64")
    capacity = int(authority_window_rows)
    if capacity <= max(SHIFTED_FFD_Z_WINDOWS):
        raise PhaseCIncrementalFeatureError(
            "FFD z-score authority window is too short"
        )
    if values.size < max(SHIFTED_FFD_Z_WINDOWS) + 1:
        raise PhaseCIncrementalFeatureError(
            "FFD z-score bootstrap has insufficient Close_FFD history"
        )
    ring = values[-capacity:]
    latest = {
        f"Close_FFD_Z_{window}": _shifted_zscore_latest_same_pandas(ring, window)
        for window in SHIFTED_FFD_Z_WINDOWS
    }
    return {
        "mode": "bounded_same_pandas_rolling_shift_authority",
        "authority_window_rows": capacity,
        "close_ffd_ring": [float(x) for x in ring],
        "last_values": latest,
    }


def _update_ffd_z_authority(
    state: Mapping[str, Any],
    close_ffd_value: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Advance the exact bounded pandas FFD z-score authority by one row."""
    st = dict(state or {})
    capacity = int(st.get("authority_window_rows") or 0)
    ring = list(st.get("close_ffd_ring") or [])
    if capacity <= max(SHIFTED_FFD_Z_WINDOWS) or not ring or len(ring) > capacity:
        raise PhaseCIncrementalFeatureError(
            "FFD z-score bounded authority state is invalid"
        )
    ring = (ring + [float(close_ffd_value)])[-capacity:]
    latest = {
        f"Close_FFD_Z_{window}": _shifted_zscore_latest_same_pandas(ring, window)
        for window in SHIFTED_FFD_Z_WINDOWS
    }
    next_state = {
        "mode": "bounded_same_pandas_rolling_shift_authority",
        "authority_window_rows": capacity,
        "close_ffd_ring": [float(x) for x in ring],
        "last_values": latest,
    }
    return latest, next_state


def bootstrap_compact_state(
    *,
    asset: str,
    bars: pd.DataFrame,
    authoritative_features: pd.DataFrame,
    phase_c1_contract: Mapping[str, Any],
    raw_context_rows: int = DEFAULT_RAW_CONTEXT_ROWS,
    feature_context_rows: int = DEFAULT_FEATURE_CONTEXT_ROWS,
) -> dict[str, Any]:
    """Bootstrap compact compute state from the unchanged batch authority."""
    started = time.perf_counter()
    asset_key = str(asset).upper()
    required_features = required_asset_features(phase_c1_contract, asset_key)
    addons = addon_asset_features(phase_c1_contract, asset_key)
    if raw_context_rows < 220:
        raise PhaseCIncrementalFeatureError("raw_context_rows must be >= 220")
    if feature_context_rows < 220:
        raise PhaseCIncrementalFeatureError("feature_context_rows must be >= 220")

    bars_norm = _legacy_math.normalize_bars(bars)
    authority = _normalize_authority(authoritative_features)
    bar_times = set(to_datetime_iso(bars_norm["Datetime"], utc=True))
    authority = authority[authority["Datetime"].isin(bar_times)].copy()
    authority = authority.sort_values("Datetime", kind="mergesort").reset_index(drop=True)
    if authority.empty or authority["Datetime"].iloc[-1] != bars_norm["Datetime"].iloc[-1]:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: authoritative bootstrap is not aligned through latest bar"
        )
    # The canonical batch representative is intentionally allowed to expose only
    # the shared core.  C.1 addon features are derived into the private bootstrap
    # superset and are certified separately against their authoritative addon
    # runtime (currently absorption).  Requiring one representative to expose
    # every addon would defeat C.1's shared-core/addon architecture.
    internal = authority.copy()
    if addons and any(name not in internal.columns for name in addons):
        internal = _legacy_math._absorption_columns(internal)
    internal = _legacy_math._ensure_canonical_shared_columns(internal)
    internal_missing = [name for name in required_features if name not in internal.columns]
    if internal_missing:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: internal bootstrap cannot produce C.1 features: {internal_missing[:30]}"
        )

    recursive = _legacy_math._bootstrap_recursive(bars_norm, internal)
    # STC remains exact by delegating that ONE feature to the same installed
    # rolling-6,000 authority used by the batch runtime.  All other C.1 core
    # features continue through compact one-row state transitions.
    rolling_capacity = min(ROLLING_BATCH_AUTHORITY_ROWS, int(len(bars_norm)))
    recursive["phase_c2_stc_authority"] = _bootstrap_stc_authority(
        bars_norm["Close"].to_numpy(dtype="float64"),
        authority_window_rows=rolling_capacity,
    )
    if "Close_FFD" not in authority.columns:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: canonical bootstrap authority is missing Close_FFD"
        )
    recursive["phase_c2_ffd_z_authority"] = _bootstrap_ffd_z_authority(
        pd.to_numeric(authority["Close_FFD"], errors="coerce").to_numpy(dtype="float64"),
        authority_window_rows=rolling_capacity,
    )
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "asset": asset_key,
        "phase_c1_contract_sha256": str(phase_c1_contract.get("contract_sha256") or ""),
        "math_kernel_version": str(getattr(_legacy_math, "ENGINE_VERSION", "UNKNOWN")),
        "last_timestamp": pd.Timestamp(bars_norm["Datetime"].iloc[-1]).isoformat(),
        "processed_rows": int(len(bars_norm)),
        "raw_context_rows": int(raw_context_rows),
        "feature_context_rows": int(feature_context_rows),
        "raw_tail": bars_norm.tail(raw_context_rows).reset_index(drop=True),
        "feature_tail": internal.tail(feature_context_rows).reset_index(drop=True),
        "recursive": recursive,
        "required_features": list(required_features),
        "addon_features": list(addons),
        "last_dependency_generation_id": None,
        "last_hmm_target": None,
        "bootstrap_elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "updates": 0,
    }
    _validate_compact_state(state, phase_c1_contract=phase_c1_contract)
    return state


def _validate_compact_state(
    state: Mapping[str, Any], *, phase_c1_contract: Mapping[str, Any]
) -> None:
    if not isinstance(state, Mapping):
        raise PhaseCIncrementalFeatureError("feature state must be a mapping")
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise PhaseCIncrementalFeatureError("feature state schema version mismatch")
    if state.get("engine_version") != ENGINE_VERSION:
        raise PhaseCIncrementalFeatureError("feature engine version mismatch")
    contract_hash = str(phase_c1_contract.get("contract_sha256") or "")
    if str(state.get("phase_c1_contract_sha256") or "") != contract_hash:
        raise PhaseCIncrementalFeatureError(
            "feature state C.1 contract fingerprint mismatch; regenerate/rebootstrap"
        )
    asset_key = str(state.get("asset") or "").upper()
    required = required_asset_features(phase_c1_contract, asset_key)
    if list(state.get("required_features") or []) != required:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: feature-state output contract differs from frozen C.1 contract"
        )
    raw_tail = state.get("raw_tail")
    feature_tail = state.get("feature_tail")
    recursive = state.get("recursive")
    if not isinstance(raw_tail, pd.DataFrame) or raw_tail.empty:
        raise PhaseCIncrementalFeatureError("feature state raw_tail is empty/invalid")
    if not isinstance(feature_tail, pd.DataFrame) or feature_tail.empty:
        raise PhaseCIncrementalFeatureError("feature state feature_tail is empty/invalid")
    if not isinstance(recursive, dict):
        raise PhaseCIncrementalFeatureError("feature state recursive payload is invalid")
    stc_state = recursive.get("phase_c2_stc_authority")
    if not isinstance(stc_state, Mapping):
        raise PhaseCIncrementalFeatureError("feature state Phase C.2 STC authority payload is missing/invalid")
    stc_capacity = int(stc_state.get("authority_window_rows") or 0)
    stc_ring = list(stc_state.get("close_ring") or [])
    if stc_capacity <= 0 or not stc_ring or len(stc_ring) > stc_capacity:
        raise PhaseCIncrementalFeatureError("feature state STC bounded ring is invalid")
    ffd_z_state = recursive.get("phase_c2_ffd_z_authority")
    if not isinstance(ffd_z_state, Mapping):
        raise PhaseCIncrementalFeatureError(
            "feature state Phase C.2 FFD z-score authority payload is missing/invalid"
        )
    ffd_z_capacity = int(ffd_z_state.get("authority_window_rows") or 0)
    ffd_z_ring = list(ffd_z_state.get("close_ffd_ring") or [])
    if (
        ffd_z_capacity <= max(SHIFTED_FFD_Z_WINDOWS)
        or not ffd_z_ring
        or len(ffd_z_ring) > ffd_z_capacity
    ):
        raise PhaseCIncrementalFeatureError(
            "feature state FFD z-score bounded ring is invalid"
        )
    if len(raw_tail) > int(state.get("raw_context_rows") or 0):
        raise PhaseCIncrementalFeatureError("raw feature context exceeds configured bound")
    if len(feature_tail) > int(state.get("feature_context_rows") or 0):
        raise PhaseCIncrementalFeatureError("feature context exceeds configured bound")


def update_one(
    *,
    state: Mapping[str, Any],
    bar: Mapping[str, Any] | pd.Series,
    dependency_generation: Mapping[str, Any],
    ffd_values: Mapping[str, Any],
    hmm_values: Mapping[str, Any] | None,
    phase_c1_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Produce exactly one new canonical feature row from one closed bar."""
    started = time.perf_counter()
    _validate_compact_state(state, phase_c1_contract=phase_c1_contract)
    prior = dict(state)
    asset_key = str(prior["asset"]).upper()
    bar_row = _coerce_bar(bar)
    bar_ts = bar_row["Datetime"]
    previous = _utc_timestamp(prior["last_timestamp"], field="state.last_timestamp")
    if bar_ts <= previous:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: update_one requires a strictly new bar "
            f"previous={previous.isoformat()} new={bar_ts.isoformat()}"
        )
    generation_id, ffd, hmm, hmm_eligible, hmm_target = validate_dependency_generation(
        asset=asset_key,
        bar_timestamp=bar_ts,
        generation=dependency_generation,
        ffd_values=ffd_values,
        hmm_values=hmm_values,
    )

    raw_tail = prior["raw_tail"]
    feature_tail = prior["feature_tail"]
    recursive = prior["recursive"]
    exact_stc, exact_stc_state = _update_stc_authority(
        recursive.get("phase_c2_stc_authority") or {},
        bar_row["Close"],
    )
    exact_ffd_z, exact_ffd_z_state = _update_ffd_z_authority(
        recursive.get("phase_c2_ffd_z_authority") or {},
        ffd["Close_FFD"],
    )
    row, next_recursive = _legacy_math._compute_one(
        bar=bar_row,
        raw_tail=raw_tail,
        feature_tail=feature_tail,
        recursive=recursive,
        ffd_values=ffd,
        hmm_values=hmm,
        edge_id=f"{asset_key}/__phase_c_canonical__",
        side_value=0,
        asset=asset_key,
    )
    # STC is continued by the exact Phase C.2 state transition above; never use
    # the retired kernel's historically approximate STC state.
    row["STC"] = exact_stc
    for name, value in exact_ffd_z.items():
        row[name] = value
    next_recursive["phase_c2_stc_authority"] = exact_stc_state
    next_recursive["phase_c2_ffd_z_authority"] = exact_ffd_z_state
    required = list(prior["required_features"])
    missing = [name for name in required if name not in row]
    if missing:
        raise PhaseCIncrementalFeatureError(
            f"{asset_key}: one-row kernel cannot produce current C.1 feature universe: {missing[:30]}"
        )

    projected: dict[str, Any] = {
        "Datetime": bar_ts,
        "Bar_Start": bar_row["Bar_Start"],
        "Open": bar_row["Open"],
        "High": bar_row["High"],
        "Low": bar_row["Low"],
        "Close": bar_row["Close"],
        "Ticks": bar_row["Ticks"],
        "Threshold": bar_row["Threshold"],
    }
    for name in required:
        projected[name] = row.get(name)
    # Normal live feature consumers get C.1 columns only; private aliases such as
    # Event_Vol remain in feature_tail for event/addon continuation and C.3.

    next_raw = _legacy_math._append_frame(
        raw_tail, bar_row, int(prior["raw_context_rows"])
    )
    next_feature = _legacy_math._append_frame(
        feature_tail, row, int(prior["feature_context_rows"])
    )
    next_state = {
        **prior,
        "last_timestamp": bar_ts.isoformat(),
        "processed_rows": int(prior.get("processed_rows", 0)) + 1,
        "raw_tail": next_raw,
        "feature_tail": next_feature,
        "recursive": next_recursive,
        "last_dependency_generation_id": generation_id,
        "last_hmm_target": hmm_target.isoformat(),
        "updates": int(prior.get("updates", 0)) + 1,
    }
    _validate_compact_state(next_state, phase_c1_contract=phase_c1_contract)
    metadata = {
        "engine_version": ENGINE_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "asset": asset_key,
        "bar_timestamp": bar_ts.isoformat(),
        "dependency_generation_id": generation_id,
        "hmm_eligible": bool(hmm_eligible),
        "hmm_target": hmm_target.isoformat(),
        "raw_context_rows": int(len(next_raw)),
        "feature_context_rows": int(len(next_feature)),
        "required_feature_count": int(len(required)),
        "addon_feature_count": int(len(prior.get("addon_features") or [])),
        "runtime_routing_enabled": False,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }
    return projected, next_state, metadata


def serialize_compact_state(
    state: Mapping[str, Any], *, phase_c1_contract: Mapping[str, Any]
) -> bytes:
    _validate_compact_state(state, phase_c1_contract=phase_c1_contract)
    return pickle.dumps(dict(state), protocol=pickle.HIGHEST_PROTOCOL)


def deserialize_compact_state(
    payload: bytes, *, phase_c1_contract: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        state = pickle.loads(payload)
    except Exception as exc:
        raise PhaseCIncrementalFeatureError(
            f"Could not deserialize compact feature state: {type(exc).__name__}: {exc}"
        ) from exc
    _validate_compact_state(state, phase_c1_contract=phase_c1_contract)
    return dict(state)


def state_observability(state: Mapping[str, Any]) -> dict[str, Any]:
    raw = state.get("raw_tail")
    features = state.get("feature_tail")
    recursive = state.get("recursive") if isinstance(state.get("recursive"), Mapping) else {}
    stc = (
        recursive.get("phase_c2_stc_authority")
        if isinstance(recursive.get("phase_c2_stc_authority"), Mapping)
        else {}
    )
    ffd_z = (
        recursive.get("phase_c2_ffd_z_authority")
        if isinstance(recursive.get("phase_c2_ffd_z_authority"), Mapping)
        else {}
    )
    return {
        "engine_version": state.get("engine_version"),
        "schema_version": state.get("schema_version"),
        "asset": state.get("asset"),
        "last_timestamp": state.get("last_timestamp"),
        "processed_rows": int(state.get("processed_rows") or 0),
        "updates": int(state.get("updates") or 0),
        "raw_context_rows": int(len(raw)) if isinstance(raw, pd.DataFrame) else 0,
        "raw_context_capacity": int(state.get("raw_context_rows") or 0),
        "feature_context_rows": int(len(features)) if isinstance(features, pd.DataFrame) else 0,
        "feature_context_capacity": int(state.get("feature_context_rows") or 0),
        "stc_authority_mode": stc.get("mode"),
        "stc_close_rows": len(stc.get("close_ring") or []),
        "stc_close_capacity": int(stc.get("authority_window_rows") or 0),
        "ffd_z_authority_mode": ffd_z.get("mode"),
        "ffd_z_close_rows": len(ffd_z.get("close_ffd_ring") or []),
        "ffd_z_close_capacity": int(ffd_z.get("authority_window_rows") or 0),
        "required_feature_count": len(state.get("required_features") or []),
        "addon_feature_count": len(state.get("addon_features") or []),
        "has_unbounded_feature_history": "feature_history" in state,
        "last_dependency_generation_id": state.get("last_dependency_generation_id"),
    }


__all__ = [
    "ENGINE_VERSION",
    "STATE_SCHEMA_VERSION",
    "DEFAULT_RAW_CONTEXT_ROWS",
    "DEFAULT_FEATURE_CONTEXT_ROWS",
    "FFD_COLUMNS",
    "REGIME_COLUMNS",
    "PhaseCIncrementalFeatureError",
    "implementation_fingerprint",
    "load_phase_c1_contract",
    "asset_contract",
    "required_asset_features",
    "addon_asset_features",
    "validate_dependency_generation",
    "_bootstrap_stc_authority",
    "_update_stc_authority",
    "_shifted_zscore_latest_same_pandas",
    "_bootstrap_ffd_z_authority",
    "_update_ffd_z_authority",
    "bootstrap_compact_state",
    "update_one",
    "serialize_compact_state",
    "deserialize_compact_state",
    "state_observability",
]
