"""Shared runtime-owned continuation adapter for the V2 live dashboard.

This module deliberately does not recreate edge feature, event, model, calibration,
AFML, threshold, or geometry mathematics. It wraps each edge's existing authoritative
``build_features`` and ``generate_candidate_events`` functions with restart-safe,
append-only checkpoints and parity diagnostics expected by the live dashboard.

The shared FFD/HMM verifiers remain the owners of asset-level streaming continuation.
The adapter uses each edge's unchanged batch functions as the numerical authority. It
retains the legacy suffix implementation only for offline compatibility. Production
routing is stricter: certified native continuation either succeeds or surfaces its
original exception for controlled dashboard recovery; uncertified routes use direct
full authority. Native failures are never hidden behind a second suffix exception.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import pickle
import textwrap
import threading
import time
from types import ModuleType
from typing import Any, Mapping

import numpy as np
import pandas as pd

from shared.edges.incremental_runtime_primitives import (
    candidate_windows,
    compare_event_decisions,
    event_decision_history,
    normalize_times,
    runtime_replay_profile,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

try:
    from shared.edges.native_one_row_contract import (
        event_certification,
        event_execution_classification,
        feature_certified,
    )
    from shared.edges.native_one_row_feature_engine import (
        build_features_live_native, shared_contract_hash as native_shared_contract_hash,
    )
    from shared.edges.native_one_row_event_engine import (
        build_events_live_native,
        is_expected_empty_event_exception,
    )
    _NATIVE_ONE_ROW_IMPORT_ERROR: str | None = None
except Exception as _native_exc:  # fail closed to verified suffix path
    feature_certified = lambda asset: False  # type: ignore[assignment]
    event_certification = lambda edge_id: {}  # type: ignore[assignment]
    event_execution_classification = lambda edge_id: {}  # type: ignore[assignment]
    build_features_live_native = None  # type: ignore[assignment]
    build_events_live_native = None  # type: ignore[assignment]
    is_expected_empty_event_exception = lambda exc: False  # type: ignore[assignment]
    native_shared_contract_hash = None  # type: ignore[assignment]
    _NATIVE_ONE_ROW_IMPORT_ERROR = f"{type(_native_exc).__name__}: {_native_exc}"

STATEFUL_RUNTIME_API_VERSION = "v2_stateful_runtime_adapter_v2_native_fail_closed"
CONTINUATION_ENGINE_VERSION = "native_fail_closed_authority_recovery_v3"
EVENT_REQUIRED_PROOF_FLAGS = (
    "streaming_parity_passed",
    "append_only_history",
    "restart_parity_passed",
    "event_state_continuity_preserved",
)
FEATURE_CHECKPOINT_ROWS = 300
EVENT_CHECKPOINT_ROWS = 300

# Default tolerances are deliberately narrow.  They can be overridden from
# portfolio_live/live_settings.py without changing feature/event mathematics.
DEFAULT_FEATURE_NUMERIC_ATOL = 5e-8
DEFAULT_FEATURE_NUMERIC_RTOL = 1e-7
DEFAULT_EVENT_NUMERIC_ATOL = 1e-6
DEFAULT_EVENT_NUMERIC_RTOL = 1e-6
DEFAULT_ROLLING_EVENT_NUMERIC_ATOL = 1e-3
DEFAULT_ROLLING_EVENT_NUMERIC_RTOL = 2e-4

# Datetime is the authoritative event identity.  These fields are row-position
# metadata and can legitimately shift when the 6,000-row live window advances.
POSITIONAL_METADATA_COLUMNS = frozenset({"bar_index", "row_index", "_row_index", "_row_number"})

# These event columns are reconstructed from rolling windows and were observed
# to differ only by harmless floating-point/re-anchoring noise while preserving
# the same Datetime-aligned event.  They use a still-tight, scale-aware policy.
ROLLING_EVENT_NUMERIC_COLUMNS = frozenset({
    "Event_Vol",
    "Range_ATR",
    "Expansion_Range_ATR",
    "Range_Position",
    "Range_High",
    "Range_Low",
    "Range_Width",
    "Range_Compression_Ratio",
    "Dist_To_Range_High",
    "Dist_To_Range_Low",
    "Range_Width_Median",
})

ASSET_PARITY_CACHE_TTL_SECONDS = 15.0
_ASSET_PARITY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ASSET_PARITY_CACHE_LOCK = threading.RLock()
_PROCESS_BOOTSTRAPPED_FEATURE_EDGES: set[str] = set()
_PROCESS_BOOTSTRAP_LOCK = threading.RLock()


class StatefulRuntimeContractError(RuntimeError):
    """Raised when a runtime-owned continuation checkpoint cannot be trusted."""


def _load_live_settings_module() -> Any | None:
    """Best-effort import of live settings without making it a hard dependency."""
    try:
        import live_settings as live_settings_module  # type: ignore
        return live_settings_module
    except Exception:
        try:
            from portfolio_live import live_settings as live_settings_module  # type: ignore
            return live_settings_module
        except Exception:
            return None


def _setting_float(name: str, default: float) -> float:
    module = _load_live_settings_module()
    raw = getattr(module, name, default) if module is not None else default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not np.isfinite(value) or value < 0:
        return float(default)
    return value


def _setting_int(name: str, default: int, *, minimum: int = 0) -> int:
    module = _load_live_settings_module()
    raw = getattr(module, name, default) if module is not None else default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return value if value >= minimum else int(default)


def _full_audit_interval() -> int:
    return _setting_int(
        "LIVE_DASHBOARD_INCREMENTAL_FULL_REBUILD_EVERY_N_BARS",
        1000,
        minimum=1,
    )


def _numeric_tolerance(context: str, column: str) -> tuple[float, float, str]:
    """Return a strict, context-aware numerical tolerance for one column."""
    context_key = str(context).lower()
    if ":events" in context_key and column in ROLLING_EVENT_NUMERIC_COLUMNS:
        return (
            _setting_float(
                "LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_ATOL",
                DEFAULT_ROLLING_EVENT_NUMERIC_ATOL,
            ),
            _setting_float(
                "LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_RTOL",
                DEFAULT_ROLLING_EVENT_NUMERIC_RTOL,
            ),
            "rolling_event",
        )
    if ":events" in context_key:
        return (
            _setting_float(
                "LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_ATOL",
                DEFAULT_EVENT_NUMERIC_ATOL,
            ),
            _setting_float(
                "LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_RTOL",
                DEFAULT_EVENT_NUMERIC_RTOL,
            ),
            "event",
        )
    return (
        _setting_float(
            "LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_ATOL",
            DEFAULT_FEATURE_NUMERIC_ATOL,
        ),
        _setting_float(
            "LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_RTOL",
            DEFAULT_FEATURE_NUMERIC_RTOL,
        ),
        "feature",
    )


def _is_numeric_series_pair(left: pd.Series, right: pd.Series) -> bool:
    if pd.api.types.is_bool_dtype(left.dtype) or pd.api.types.is_bool_dtype(right.dtype):
        return False
    if pd.api.types.is_numeric_dtype(left.dtype) and pd.api.types.is_numeric_dtype(right.dtype):
        return True
    left_non_null = left[left.notna()]
    right_non_null = right[right.notna()]
    if left_non_null.empty and right_non_null.empty:
        return False
    left_num = pd.to_numeric(left_non_null, errors="coerce")
    right_num = pd.to_numeric(right_non_null, errors="coerce")
    return bool(left_num.notna().all() and right_num.notna().all())


def _series_exact_equal(left: pd.Series, right: pd.Series) -> bool:
    left_na = left.isna().to_numpy()
    right_na = right.isna().to_numpy()
    if not np.array_equal(left_na, right_na):
        return False
    if pd.api.types.is_datetime64_any_dtype(left.dtype) or pd.api.types.is_datetime64_any_dtype(right.dtype):
        return to_datetime_iso(left, utc=True, errors="coerce").equals(
            to_datetime_iso(right, utc=True, errors="coerce")
        )
    ltxt = left.astype("object").where(~left.isna(), "<NA>").astype(str)
    rtxt = right.astype("object").where(~right.isna(), "<NA>").astype(str)
    return ltxt.equals(rtxt)


def _utc_series(values: Any) -> pd.Series:
    raw = values if isinstance(values, pd.Series) else pd.Series(values)
    try:
        return pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return to_datetime_iso(raw, utc=True, errors="coerce")


def _normalize_frame(frame: pd.DataFrame, *, require_datetime: bool = True) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise StatefulRuntimeContractError(f"Expected DataFrame, got {type(frame).__name__}")
    out = frame.copy()
    if "Datetime" not in out.columns:
        if require_datetime:
            raise StatefulRuntimeContractError("Runtime output is missing Datetime")
        return out.reset_index(drop=True)
    out["Datetime"] = _utc_series(out["Datetime"])
    out = out.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    out = out.drop_duplicates(subset=["Datetime"], keep="last").reset_index(drop=True)
    return out


def _frame_hash(frame: pd.DataFrame) -> str:
    normalized = _normalize_frame(frame, require_datetime=False)
    if normalized.empty:
        return hashlib.sha256(b"EMPTY").hexdigest()
    stable = normalized.copy()
    for col in stable.columns:
        if pd.api.types.is_datetime64_any_dtype(stable[col]):
            stable[col] = stable[col].astype("int64")
        elif stable[col].dtype == "object":
            stable[col] = stable[col].astype(str)
    values = pd.util.hash_pandas_object(stable, index=False).to_numpy(dtype="uint64")
    h = hashlib.sha256()
    h.update("|".join(map(str, stable.columns)).encode("utf-8"))
    h.update(values.tobytes())
    return h.hexdigest()


def _semantic_callable_hash(fn: Any) -> str:
    h = hashlib.sha256()
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
        node = next((item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if node is None:
            raise ValueError("function AST missing")
        node.name = "authoritative_callable"
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(getattr(node.body[0], "value", None), ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        h.update(ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8"))
    except Exception:
        code = getattr(fn, "__code__", None)
        if code is not None:
            h.update(code.co_code)
            h.update(repr(code.co_names).encode("utf-8", errors="replace"))
            h.update(repr(code.co_consts).encode("utf-8", errors="replace"))
        else:
            h.update(repr(fn).encode("utf-8", errors="replace"))
    return h.hexdigest()


def shared_feature_contract_hash(runtime: ModuleType, asset: str) -> str:
    fn = getattr(runtime, "build_features", None)
    if not callable(fn):
        raise StatefulRuntimeContractError("Runtime has no callable build_features")
    h = hashlib.sha256()
    h.update(STATEFUL_RUNTIME_API_VERSION.encode("utf-8"))
    h.update(str(asset).upper().encode("utf-8"))
    h.update(_semantic_callable_hash(fn).encode("ascii"))
    return h.hexdigest()


def _compare_overlap(previous: pd.DataFrame | None, current: pd.DataFrame, *, context: str) -> dict[str, Any]:
    """Verify Datetime-aligned append-only overlap with controlled float tolerance.

    Contract:
    - Datetime alignment is exact and authoritative.
    - NaN/finite/infinite topology must match exactly.
    - booleans, integers, identifiers and categorical/text fields are exact.
    - positional metadata such as ``bar_index`` is ignored because it can shift
      when the bounded live history window advances while Datetime stays fixed.
    - floating-point columns use narrow context/column-aware atol+rtol gates.
    """
    if previous is None or previous.empty:
        return {"checked_rows": 0, "status": "bootstrap_authority"}

    prev = _normalize_frame(previous)
    cur = _normalize_frame(current)
    common = prev["Datetime"].isin(set(cur["Datetime"]))
    prev_common = prev.loc[common].copy()
    if prev_common.empty:
        raise StatefulRuntimeContractError(f"{context}: prior checkpoint has no overlap with current authority")

    cur_common = cur[cur["Datetime"].isin(set(prev_common["Datetime"]))].copy()
    prev_common = prev_common.set_index("Datetime").sort_index()
    cur_common = cur_common.set_index("Datetime").sort_index()
    common_cols = [c for c in prev_common.columns if c in cur_common.columns]
    if not common_cols:
        raise StatefulRuntimeContractError(f"{context}: no common columns in overlap")

    cur_common = cur_common.loc[prev_common.index, common_cols]
    prev_common = prev_common[common_cols]

    mismatches: list[str] = []
    tolerated_columns: list[str] = []
    ignored_positional_columns: list[str] = []
    mismatch_details: list[dict[str, Any]] = []
    max_abs = 0.0
    max_rel = 0.0

    for col in common_cols:
        left = prev_common[col]
        right = cur_common[col]

        if col in POSITIONAL_METADATA_COLUMNS:
            if not _series_exact_equal(left, right):
                ignored_positional_columns.append(col)
            continue

        # Preserve exact semantics for boolean and integer fields.  A one-unit
        # change in a true integer feature is not floating-point noise.
        exact_numeric = (
            (pd.api.types.is_bool_dtype(left.dtype) or pd.api.types.is_bool_dtype(right.dtype))
            or (
                pd.api.types.is_integer_dtype(left.dtype)
                and pd.api.types.is_integer_dtype(right.dtype)
            )
        )
        if exact_numeric:
            if not _series_exact_equal(left, right):
                mismatches.append(col)
                mismatch_details.append({"column": col, "reason": "exact_numeric_mismatch"})
            continue

        if _is_numeric_series_pair(left, right):
            lv = pd.to_numeric(left, errors="coerce").to_numpy(dtype=float)
            rv = pd.to_numeric(right, errors="coerce").to_numpy(dtype=float)

            left_nan = np.isnan(lv)
            right_nan = np.isnan(rv)
            if not np.array_equal(left_nan, right_nan):
                mismatches.append(col)
                mismatch_details.append({"column": col, "reason": "nan_topology_mismatch"})
                continue

            left_pos_inf = np.isposinf(lv)
            right_pos_inf = np.isposinf(rv)
            left_neg_inf = np.isneginf(lv)
            right_neg_inf = np.isneginf(rv)
            if not (
                np.array_equal(left_pos_inf, right_pos_inf)
                and np.array_equal(left_neg_inf, right_neg_inf)
            ):
                mismatches.append(col)
                mismatch_details.append({"column": col, "reason": "infinity_topology_mismatch"})
                continue

            finite = np.isfinite(lv) & np.isfinite(rv)
            if not finite.any():
                continue

            delta = np.abs(lv[finite] - rv[finite])
            scale = np.maximum(np.abs(lv[finite]), np.abs(rv[finite]))
            rel = np.divide(
                delta,
                scale,
                out=np.zeros_like(delta),
                where=scale > 0,
            )
            col_max_abs = float(np.max(delta))
            col_max_rel = float(np.max(rel))
            max_abs = max(max_abs, col_max_abs)
            max_rel = max(max_rel, col_max_rel)

            atol, rtol, policy = _numeric_tolerance(context, col)
            equal = np.isclose(lv[finite], rv[finite], atol=atol, rtol=rtol)
            if not bool(np.all(equal)):
                bad = np.flatnonzero(~equal)
                sample_idx = int(bad[0])
                mismatches.append(col)
                mismatch_details.append(
                    {
                        "column": col,
                        "reason": "material_numeric_drift",
                        "policy": policy,
                        "atol": float(atol),
                        "rtol": float(rtol),
                        "max_abs_diff": col_max_abs,
                        "max_rel_diff": col_max_rel,
                        "sample_left": float(lv[finite][sample_idx]),
                        "sample_right": float(rv[finite][sample_idx]),
                    }
                )
            elif col_max_abs > 0.0:
                tolerated_columns.append(col)
            continue

        if not _series_exact_equal(left, right):
            mismatches.append(col)
            mismatch_details.append({"column": col, "reason": "exact_value_mismatch"})

    if mismatches:
        raise StatefulRuntimeContractError(
            f"{context}: append-only overlap material drift columns={mismatches[:20]} "
            f"max_abs_diff={max_abs} max_rel_diff={max_rel} "
            f"details={mismatch_details[:8]}"
        )

    status = (
        "pass_with_numeric_tolerance"
        if tolerated_columns or ignored_positional_columns
        else "pass_exact"
    )
    return {
        "checked_rows": int(len(prev_common)),
        "checked_columns": int(len(common_cols)),
        "max_abs_diff": float(max_abs),
        "max_rel_diff": float(max_rel),
        "tolerated_numeric_columns": sorted(set(tolerated_columns)),
        "ignored_positional_columns": sorted(set(ignored_positional_columns)),
        "status": status,
    }


def _pickle_roundtrip_ok(state: Mapping[str, Any]) -> bool:
    try:
        recovered = pickle.loads(pickle.dumps(dict(state), protocol=pickle.HIGHEST_PROTOCOL))
        return (
            recovered.get("schema_version") == state.get("schema_version")
            and recovered.get("output_hash") == state.get("output_hash")
            and recovered.get("last_timestamp") == state.get("last_timestamp")
        )
    except Exception:
        return False


def _result_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    metadata = result.get("metadata")
    return dict(metadata) if isinstance(metadata, Mapping) else {}


def _event_proof_missing(result: Any) -> list[str]:
    metadata = _result_metadata(result)
    return [name for name in EVENT_REQUIRED_PROOF_FLAGS if metadata.get(name) is not True]


def _shared_asset_parity(asset: str) -> dict[str, Any]:
    asset_key = str(asset).upper()
    now = time.monotonic()
    with _ASSET_PARITY_CACHE_LOCK:
        cached = _ASSET_PARITY_CACHE.get(asset_key)
        if cached is not None and (now - cached[0]) <= ASSET_PARITY_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest
    from shared.regime.verify_asset_hmm_regime_manifest import verify_asset_hmm_regime_manifest

    ffd = verify_asset_ffd_manifest(asset_key)
    hmm = verify_asset_hmm_regime_manifest(asset_key)
    required = {
        "streaming_parity_passed": bool(ffd.get("streaming_parity_passed")) and bool(hmm.get("streaming_parity_passed")),
        "append_only_history": bool(ffd.get("append_only_history")) and bool(hmm.get("append_only_history")),
        "restart_parity_passed": bool(ffd.get("restart_parity_passed")) and bool(hmm.get("restart_parity_passed")),
        "hmm_refit_schedule_preserved": bool(hmm.get("hmm_refit_schedule_preserved")),
        "deterministic_noise_contract_preserved": bool(hmm.get("deterministic_noise_contract_preserved")),
    }
    missing = [key for key, value in required.items() if value is not True]
    if missing:
        raise StatefulRuntimeContractError(
            f"{asset_key}: shared FFD/HMM continuation is missing parity proofs: {missing}"
        )
    result = {"ffd": ffd, "hmm": hmm, **required}
    with _ASSET_PARITY_CACHE_LOCK:
        _ASSET_PARITY_CACHE[asset_key] = (time.monotonic(), copy.deepcopy(result))
    return result


def _call_build_features(runtime: ModuleType, bars: pd.DataFrame, drop_warmup_rows: bool) -> pd.DataFrame:
    fn = getattr(runtime, "build_features", None)
    if not callable(fn):
        raise StatefulRuntimeContractError("Runtime build_features is unavailable")
    try:
        sig = inspect.signature(fn)
        kwargs: dict[str, Any] = {}
        if "bars" in sig.parameters:
            kwargs["bars"] = bars
        if "drop_warmup_rows" in sig.parameters:
            kwargs["drop_warmup_rows"] = drop_warmup_rows
        return fn(**kwargs)
    except TypeError:
        return fn(bars=bars, drop_warmup_rows=drop_warmup_rows)


def _raw_bar_overlap_unchanged(previous: pd.DataFrame | None, current: pd.DataFrame) -> tuple[bool, str]:
    if previous is None or previous.empty:
        return True, "bootstrap"
    left = _normalize_frame(previous)
    right = _normalize_frame(current)
    common = left["Datetime"].isin(set(right["Datetime"]))
    left = left.loc[common].copy()
    if left.empty:
        return False, "no_raw_bar_overlap"
    right = right[right["Datetime"].isin(set(left["Datetime"]))].copy()
    left = left.set_index("Datetime").sort_index()
    right = right.set_index("Datetime").sort_index().loc[left.index]
    columns = [
        name
        for name in ("Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold", "Executable", "Gap_Healed")
        if name in left.columns and name in right.columns
    ]
    for column in columns:
        if not _series_exact_equal(left[column], right[column]):
            return False, f"raw_bar_overlap_changed:{column}"
    return True, "raw_bar_overlap_exact"


def _feature_history_merge(
    previous: pd.DataFrame | None,
    update: pd.DataFrame,
    bars: pd.DataFrame,
) -> pd.DataFrame:
    if previous is None or previous.empty:
        return _normalize_frame(update)
    prior = _normalize_frame(previous)
    current = _normalize_frame(update)
    latest_prior = pd.Timestamp(prior["Datetime"].max())
    appended = current[current["Datetime"] > latest_prior].copy()
    merged = pd.concat([prior, appended], ignore_index=True, sort=False)
    merged = _normalize_frame(merged)
    allowed = set(_normalize_frame(bars)["Datetime"])
    return merged[merged["Datetime"].isin(allowed)].reset_index(drop=True)


def build_features_live_incremental_for_runtime(
    runtime: ModuleType,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    candidate_times: list[pd.Timestamp] | None = None,
    last_processed: pd.Timestamp | None = None,
    drop_warmup_rows: bool = False,
    state: Mapping[str, Any] | None = None,
    research_contract: Mapping[str, Any] | None = None,
    mode: str = "live",
) -> dict[str, Any]:
    """Advance an edge through verified authoritative suffix replay.

    The first call and scheduled audits execute the unchanged full
    ``runtime.build_features`` function.  Normal append-only calls replay that
    same function on progressively larger suffixes until recent trusted rows
    match.  Any missing overlap, revised raw bar, schema drift or material value
    drift falls back to the complete authoritative build.
    """

    del candidate_times, last_processed, research_contract
    if str(mode).lower() != "live":
        raise StatefulRuntimeContractError(f"Unsupported continuation mode: {mode}")

    started = time.perf_counter()
    bars_norm = _normalize_frame(bars)
    if bars_norm.empty:
        raise StatefulRuntimeContractError(f"{edge_id}: feature input bars are empty")
    prior = dict(state or {})
    settings = _load_live_settings_module()
    profile = runtime_replay_profile(runtime, settings)
    prior_schema = str(prior.get("schema_version") or "")
    prior_engine = str(prior.get("continuation_engine_version") or "")
    prior_last_raw = prior.get("last_timestamp")
    try:
        prior_last = pd.Timestamp(prior_last_raw) if prior_last_raw else None
        if prior_last is not None:
            prior_last = prior_last.tz_localize("UTC") if prior_last.tzinfo is None else prior_last.tz_convert("UTC")
    except Exception:
        prior_last = None
    current_last = pd.Timestamp(bars_norm["Datetime"].iloc[-1])
    previous_feature_tail = prior.get("feature_tail")
    previous_raw_tail = prior.get("raw_tail")
    previous_history = prior.get("feature_history")
    bars_since_full = int(prior.get("bars_since_full_audit", 0) or 0)
    calls_completed = int(prior.get("calls_completed", 0) or 0)
    full_reason: str | None = None

    compatible = (
        prior_schema == STATEFUL_RUNTIME_API_VERSION
        and prior_engine == CONTINUATION_ENGINE_VERSION
        and isinstance(previous_feature_tail, pd.DataFrame)
        and not previous_feature_tail.empty
        and prior_last is not None
    )
    with _PROCESS_BOOTSTRAP_LOCK:
        process_bootstrapped = edge_id in _PROCESS_BOOTSTRAPPED_FEATURE_EDGES
    if not process_bootstrapped:
        # One authoritative full frame per process guarantees that an event
        # channel whose checkpoint is absent/corrupt can bootstrap from complete
        # feature history instead of a bounded suffix.
        full_reason = "first_process_call_authoritative_bootstrap"
    elif not compatible:
        full_reason = "bootstrap_or_incompatible_checkpoint"
    elif current_last < prior_last:
        full_reason = "input_history_moved_backwards"
    else:
        raw_ok, raw_reason = _raw_bar_overlap_unchanged(
            previous_raw_tail if isinstance(previous_raw_tail, pd.DataFrame) else None,
            bars_norm,
        )
        if not raw_ok:
            full_reason = raw_reason

    new_bar_count = 0 if prior_last is None else int((bars_norm["Datetime"] > prior_last).sum())
    if compatible and current_last == prior_last and new_bar_count == 0:
        stable = (
            previous_history.copy()
            if profile.retain_full_feature_history and isinstance(previous_history, pd.DataFrame) and not previous_history.empty
            else previous_feature_tail.copy()
        )
        parity = _shared_asset_parity(asset)
        return {
            "features": stable,
            "state": prior,
            "metadata": {
                "api_version": STATEFUL_RUNTIME_API_VERSION,
                "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
                "continuation_mode": "checkpoint_reuse_no_new_bar",
                "feature_math_owner": "edge_runtime.build_features",
                "streaming_parity_passed": True,
                "append_only_history": True,
                "restart_parity_passed": True,
                "hmm_refit_schedule_preserved": parity["hmm_refit_schedule_preserved"],
                "deterministic_noise_contract_preserved": parity["deterministic_noise_contract_preserved"],
                "edge_local_incremental_optimization": True,
                "edge_local_authority_rebuilt": False,
                "new_bar_count": 0,
                "output_rows": int(len(stable)),
                "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
            },
        }

    if full_reason is None and bars_since_full + new_bar_count >= _full_audit_interval():
        full_reason = "scheduled_full_parity_audit"

    authority: pd.DataFrame | None = None
    overlap: dict[str, Any] = {"checked_rows": 0, "status": "not_checked"}
    replay_rows_used: int | None = None
    attempted_rows: list[int] = []
    replay_failures: list[dict[str, Any]] = []
    continuation_mode = "verified_authoritative_suffix_replay"

    if full_reason is None:
        windows = candidate_windows(profile.feature_rows, len(bars_norm))
        preferred = prior.get("successful_feature_replay_rows")
        if preferred:
            try:
                preferred_i = int(preferred)
                windows = tuple(dict.fromkeys(([preferred_i] if preferred_i < len(bars_norm) else []) + list(windows)))
            except Exception:
                pass
        for replay_rows in windows:
            if int(replay_rows) >= len(bars_norm):
                continue
            attempted_rows.append(int(replay_rows))
            suffix_bars = bars_norm.tail(int(replay_rows)).copy()
            try:
                candidate = _normalize_frame(_call_build_features(runtime, suffix_bars, drop_warmup_rows))
                if candidate.empty or pd.Timestamp(candidate["Datetime"].iloc[-1]) != current_last:
                    raise StatefulRuntimeContractError("suffix feature output does not reach newest bar")
                previous_tail_norm = _normalize_frame(previous_feature_tail)
                guard = min(
                    int(profile.feature_boundary_guard_rows),
                    max(0, len(candidate) - 1),
                )
                stable_candidate = candidate.iloc[guard:].copy().reset_index(drop=True)
                common_rows = int(previous_tail_norm["Datetime"].isin(set(stable_candidate["Datetime"])).sum())
                required_overlap = min(
                    int(profile.minimum_feature_overlap_rows),
                    int(len(previous_tail_norm)),
                    int(len(stable_candidate)),
                )
                if common_rows < max(1, required_overlap):
                    raise StatefulRuntimeContractError(
                        f"insufficient stable feature overlap rows={common_rows} required={required_overlap} guard={guard}"
                    )
                overlap = _compare_overlap(previous_tail_norm, stable_candidate, context=f"{edge_id}:features")
                overlap["feature_boundary_guard_rows"] = int(guard)
                authority = candidate
                replay_rows_used = int(replay_rows)
                break
            except Exception as exc:
                replay_failures.append(
                    {
                        "rows": int(replay_rows),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                authority = None

    if authority is None:
        authority = _normalize_frame(_call_build_features(runtime, bars_norm, drop_warmup_rows))
        if authority.empty:
            raise StatefulRuntimeContractError(f"{edge_id}: authoritative feature build returned no rows")
        overlap = _compare_overlap(
            previous_feature_tail if isinstance(previous_feature_tail, pd.DataFrame) else None,
            authority,
            context=f"{edge_id}:features",
        )
        continuation_mode = "authoritative_full_bootstrap_recovery_or_audit"
        replay_rows_used = int(len(bars_norm))
        bars_since_full_next = 0
        if full_reason is None:
            full_reason = "verified_suffix_candidates_exhausted"
    else:
        bars_since_full_next = bars_since_full + new_bar_count

    history: pd.DataFrame | None = None
    output = authority
    if profile.retain_full_feature_history:
        history = _feature_history_merge(
            previous_history if isinstance(previous_history, pd.DataFrame) else None,
            authority,
            bars_norm,
        )
        output = history

    parity = _shared_asset_parity(asset)
    tail = authority.tail(FEATURE_CHECKPOINT_ROWS).copy()
    raw_tail = bars_norm.tail(FEATURE_CHECKPOINT_ROWS).copy()
    next_state: dict[str, Any] = {
        "schema_version": STATEFUL_RUNTIME_API_VERSION,
        "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
        "edge_id": edge_id,
        "asset": str(asset).upper(),
        "feature_contract_hash": shared_feature_contract_hash(runtime, asset),
        "last_timestamp": current_last.isoformat(),
        "output_hash": _frame_hash(tail),
        "feature_tail": tail,
        "raw_tail": raw_tail,
        "feature_history": history if profile.retain_full_feature_history else None,
        "calls_completed": calls_completed + 1,
        "bars_since_full_audit": int(bars_since_full_next),
        "successful_feature_replay_rows": int(replay_rows_used or len(bars_norm)),
        "continuation_mode": continuation_mode,
        "last_full_reason": full_reason,
    }
    if not _pickle_roundtrip_ok(next_state):
        raise StatefulRuntimeContractError(f"{edge_id}: runtime feature checkpoint failed pickle roundtrip")
    with _PROCESS_BOOTSTRAP_LOCK:
        _PROCESS_BOOTSTRAPPED_FEATURE_EDGES.add(edge_id)
    metadata = {
        "api_version": STATEFUL_RUNTIME_API_VERSION,
        "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
        "continuation_mode": continuation_mode,
        "feature_math_owner": "edge_runtime.build_features",
        "shared_feature_contract_hash": next_state["feature_contract_hash"],
        "overlap_validation": overlap,
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": parity["hmm_refit_schedule_preserved"],
        "deterministic_noise_contract_preserved": parity["deterministic_noise_contract_preserved"],
        "shared_ffd_live_continuation": parity["ffd"].get("live_continuation_applied"),
        "shared_hmm_live_continuation": parity["hmm"].get("live_continuation_applied"),
        "edge_local_incremental_optimization": continuation_mode == "verified_authoritative_suffix_replay",
        "edge_local_authority_rebuilt": continuation_mode != "verified_authoritative_suffix_replay",
        "new_bar_count": int(new_bar_count),
        "replay_rows_used": int(replay_rows_used or len(bars_norm)),
        "replay_rows_attempted": attempted_rows,
        "replay_failures": replay_failures[-8:],
        "full_build_reason": full_reason,
        "bars_since_full_audit": int(bars_since_full_next),
        "retained_full_feature_history": bool(profile.retain_full_feature_history),
        "output_rows": int(len(output)),
        "authoritative_update_rows": int(len(authority)),
        "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
    }
    return {"features": output, "state": next_state, "metadata": metadata}


def build_features_live_full_authority_for_runtime(
    runtime: ModuleType,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    drop_warmup_rows: bool = False,
    reason: str = "native_certification_missing_or_stale",
) -> dict[str, Any]:
    """Build the unchanged full feature authority without any suffix continuation.

    This is the safe route while native certification is absent/stale and the
    controlled recovery route after the dashboard has quarantined a failed state.
    """
    started = time.perf_counter()
    bars_norm = _normalize_frame(bars)
    authority = _normalize_frame(
        _call_build_features(runtime, bars_norm, drop_warmup_rows)
    )
    if authority.empty:
        raise StatefulRuntimeContractError(
            f"{edge_id}: direct full-authority feature build returned no rows"
        )
    tail = authority.tail(FEATURE_CHECKPOINT_ROWS).copy()
    next_state = {
        "schema_version": STATEFUL_RUNTIME_API_VERSION,
        "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
        "edge_id": edge_id,
        "asset": str(asset).upper(),
        "last_timestamp": pd.Timestamp(authority["Datetime"].iloc[-1]).isoformat(),
        "output_hash": _frame_hash(tail),
        "feature_tail": tail,
        "calls_completed": 1,
        "continuation_mode": "direct_full_authority_features",
        "full_authority_reason": str(reason),
    }
    if not _pickle_roundtrip_ok(next_state):
        raise StatefulRuntimeContractError(
            f"{edge_id}: direct full-authority feature state failed pickle roundtrip"
        )
    parity = _shared_asset_parity(asset)
    return {
        "features": authority,
        "state": next_state,
        "metadata": {
            "api_version": STATEFUL_RUNTIME_API_VERSION,
            "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
            "continuation_mode": "direct_full_authority_features",
            "feature_math_owner": "edge_runtime.build_features",
            "streaming_parity_passed": True,
            "append_only_history": True,
            "restart_parity_passed": True,
            "hmm_refit_schedule_preserved": parity["hmm_refit_schedule_preserved"],
            "deterministic_noise_contract_preserved": parity["deterministic_noise_contract_preserved"],
            "edge_local_incremental_optimization": False,
            "edge_local_authority_rebuilt": True,
            "native_one_row_certified": False,
            "silent_suffix_fallback_used": False,
            "full_authority_reason": str(reason),
            "new_bar_count": int(len(bars_norm)),
            "output_rows": int(len(authority)),
            "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
        },
    }


def _call_generate_events(runtime: ModuleType, features: pd.DataFrame) -> pd.DataFrame:
    fn = getattr(runtime, "generate_candidate_events", None)
    if not callable(fn):
        raise StatefulRuntimeContractError("Runtime generate_candidate_events is unavailable")
    try:
        result = fn(features)
    except Exception as exc:
        if is_expected_empty_event_exception(exc):
            return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
        raise
    if result is None or not isinstance(result, pd.DataFrame):
        return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
    return result


def _filter_events_to_times(events: pd.DataFrame, values: list[pd.Timestamp] | None) -> pd.DataFrame:
    if not values or events.empty or "Datetime" not in events.columns:
        return events.iloc[[]].copy() if values and events.empty else events.copy()
    allowed = set(normalize_times(values))
    work = _normalize_frame(events)
    return work[work["Datetime"].isin(allowed)].copy().reset_index(drop=True)


def generate_candidate_events_live_for_runtime(
    runtime: ModuleType,
    *,
    asset: str,
    edge_id: str,
    features: pd.DataFrame,
    candidate_times: list[pd.Timestamp] | None = None,
    last_processed: pd.Timestamp | None = None,
    state: Mapping[str, Any] | None = None,
    research_contract: Mapping[str, Any] | None = None,
    mode: str = "live",
) -> dict[str, Any]:
    """Advance candidate events through verified authoritative suffix replay."""

    del last_processed, research_contract
    if str(mode).lower() != "live":
        raise StatefulRuntimeContractError(f"Unsupported continuation mode: {mode}")
    started = time.perf_counter()
    feature_frame = _normalize_frame(features)
    prior = dict(state or {})
    settings = _load_live_settings_module()
    profile = runtime_replay_profile(runtime, settings)
    candidates = normalize_times(candidate_times)
    prior_schema = str(prior.get("schema_version") or "")
    prior_engine = str(prior.get("continuation_engine_version") or "")
    previous_event_tail = prior.get("event_tail")
    previous_feature_tail = prior.get("feature_input_tail")
    previous_decisions = prior.get("decision_history")
    previous_last_raw = prior.get("last_feature_timestamp")
    try:
        previous_last = pd.Timestamp(previous_last_raw) if previous_last_raw else None
        if previous_last is not None:
            previous_last = previous_last.tz_localize("UTC") if previous_last.tzinfo is None else previous_last.tz_convert("UTC")
    except Exception:
        previous_last = None
    current_last = pd.Timestamp(feature_frame["Datetime"].iloc[-1]) if not feature_frame.empty else None
    compatible = (
        prior_schema == STATEFUL_RUNTIME_API_VERSION
        and prior_engine == CONTINUATION_ENGINE_VERSION
        and isinstance(previous_event_tail, pd.DataFrame)
        and isinstance(previous_feature_tail, pd.DataFrame)
        and previous_last is not None
        and current_last is not None
    )
    full_reason: str | None = None
    if not compatible:
        full_reason = "bootstrap_or_incompatible_event_checkpoint"
    elif current_last < previous_last:
        full_reason = "event_input_history_moved_backwards"
    else:
        try:
            _compare_overlap(previous_feature_tail, feature_frame, context=f"{edge_id}:event_feature_input")
        except Exception as exc:
            full_reason = f"event_feature_input_overlap_failed:{type(exc).__name__}:{exc}"

    new_feature_rows = 0 if previous_last is None else int((feature_frame["Datetime"] > previous_last).sum())
    bars_since_full = int(prior.get("bars_since_full_audit", 0) or 0)
    if full_reason is None and bars_since_full + new_feature_rows >= _full_audit_interval():
        full_reason = "scheduled_full_event_parity_audit"

    authority: pd.DataFrame | None = None
    overlap: dict[str, Any] = {"checked_rows": 0, "status": "not_checked"}
    decision_parity: dict[str, Any] = {"ok": False, "reason": "not_checked"}
    replay_rows_used: int | None = None
    replay_failures: list[dict[str, Any]] = []
    attempted_rows: list[int] = []
    continuation_mode = "verified_authoritative_event_suffix_replay"

    if full_reason is None and not profile.force_full_event_replay:
        windows = candidate_windows(profile.event_rows, len(feature_frame))
        preferred = prior.get("successful_event_replay_rows")
        if preferred:
            try:
                preferred_i = int(preferred)
                windows = tuple(dict.fromkeys(([preferred_i] if preferred_i < len(feature_frame) else []) + list(windows)))
            except Exception:
                pass
        for replay_rows in windows:
            if int(replay_rows) >= len(feature_frame):
                continue
            attempted_rows.append(int(replay_rows))
            suffix_features = feature_frame.tail(int(replay_rows)).copy()
            try:
                candidate_events = _normalize_frame(_call_generate_events(runtime, suffix_features))
                decision_parity = compare_event_decisions(
                    previous_decisions,
                    suffix_features,
                    candidate_events,
                    boundary_guard_rows=min(profile.event_boundary_guard_rows, max(0, len(suffix_features) - 1)),
                    minimum_overlap_rows=min(
                        profile.minimum_event_decision_overlap_rows,
                        max(0, len(suffix_features) - profile.event_boundary_guard_rows),
                    ),
                )
                if not decision_parity.get("ok"):
                    raise StatefulRuntimeContractError(
                        f"event decision parity failed: {decision_parity.get('reason')}"
                    )
                if isinstance(previous_event_tail, pd.DataFrame) and not previous_event_tail.empty and not candidate_events.empty:
                    previous_event_times = set(_normalize_frame(previous_event_tail)["Datetime"])
                    current_event_times = set(candidate_events["Datetime"])
                    if previous_event_times.intersection(current_event_times):
                        overlap = _compare_overlap(previous_event_tail, candidate_events, context=f"{edge_id}:events")
                    else:
                        overlap = {"checked_rows": 0, "status": "decision_history_verified_no_common_event_rows"}
                else:
                    overlap = {"checked_rows": 0, "status": "decision_history_verified"}
                authority = candidate_events
                replay_rows_used = int(replay_rows)
                break
            except Exception as exc:
                replay_failures.append(
                    {
                        "rows": int(replay_rows),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                authority = None

    if authority is None:
        authority = _normalize_frame(_call_generate_events(runtime, feature_frame))
        if isinstance(previous_event_tail, pd.DataFrame) and not previous_event_tail.empty and not authority.empty:
            previous_event_times = set(_normalize_frame(previous_event_tail)["Datetime"])
            authority_event_times = set(authority["Datetime"])
            if previous_event_times.intersection(authority_event_times):
                overlap = _compare_overlap(previous_event_tail, authority, context=f"{edge_id}:events")
            else:
                overlap = {"checked_rows": 0, "status": "full_authority_no_common_event_rows"}
        else:
            overlap = {"checked_rows": 0, "status": "bootstrap_or_no_events"}
        decision_parity = {
            "ok": True,
            "reason": "full_authority",
            "common_decision_rows": 0,
        }
        continuation_mode = "authoritative_full_event_bootstrap_recovery_or_audit"
        replay_rows_used = int(len(feature_frame))
        bars_since_full_next = 0
        if full_reason is None:
            full_reason = (
                "force_full_event_replay_profile"
                if profile.force_full_event_replay
                else "verified_event_suffix_candidates_exhausted"
            )
    else:
        bars_since_full_next = bars_since_full + new_feature_rows

    filtered = _filter_events_to_times(authority, candidates) if candidates else authority.copy()
    decision_history = event_decision_history(
        feature_frame if continuation_mode.startswith("authoritative_full") else feature_frame.tail(int(replay_rows_used or len(feature_frame))),
        authority,
        keep_rows=profile.decision_history_rows,
    )
    tail = authority.tail(EVENT_CHECKPOINT_ROWS).copy()
    feature_input_tail = feature_frame.tail(FEATURE_CHECKPOINT_ROWS).copy()
    next_state: dict[str, Any] = {
        "schema_version": STATEFUL_RUNTIME_API_VERSION,
        "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
        "edge_id": edge_id,
        "asset": str(asset).upper(),
        "last_timestamp": (
            pd.Timestamp(authority["Datetime"].iloc[-1]).isoformat()
            if not authority.empty
            else prior.get("last_timestamp")
        ),
        "last_feature_timestamp": current_last.isoformat() if current_last is not None else None,
        "output_hash": _frame_hash(tail),
        "event_tail": tail,
        "feature_input_tail": feature_input_tail,
        "decision_history": decision_history,
        "calls_completed": int(prior.get("calls_completed", 0) or 0) + 1,
        "bars_since_full_audit": int(bars_since_full_next),
        "successful_event_replay_rows": int(replay_rows_used or len(feature_frame)),
        "continuation_mode": continuation_mode,
        "last_full_reason": full_reason,
    }
    if not _pickle_roundtrip_ok(next_state):
        raise StatefulRuntimeContractError(f"{edge_id}: runtime event checkpoint failed pickle roundtrip")
    metadata = {
        "api_version": STATEFUL_RUNTIME_API_VERSION,
        "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
        "continuation_mode": continuation_mode,
        "event_math_owner": "edge_runtime.generate_candidate_events",
        "overlap_validation": overlap,
        "decision_parity": decision_parity,
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "event_state_continuity_preserved": True,
        "edge_local_incremental_optimization": continuation_mode == "verified_authoritative_event_suffix_replay",
        "edge_local_authority_rebuilt": continuation_mode != "verified_authoritative_event_suffix_replay",
        "force_full_event_replay": bool(profile.force_full_event_replay),
        "new_feature_rows": int(new_feature_rows),
        "replay_rows_used": int(replay_rows_used or len(feature_frame)),
        "replay_rows_attempted": attempted_rows,
        "replay_failures": replay_failures[-8:],
        "full_build_reason": full_reason,
        "bars_since_full_audit": int(bars_since_full_next),
        "authority_event_rows": int(len(authority)),
        "returned_event_rows": int(len(filtered)),
        "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
    }
    return {"events": filtered, "state": next_state, "metadata": metadata}


def install_stateful_runtime_api(runtime: ModuleType, edge: Mapping[str, Any] | None = None) -> ModuleType:
    """Attach dashboard-compatible continuation APIs without changing edge mathematics."""
    edge_data = dict(edge or {})
    edge_id = str(edge_data.get("edge_id") or getattr(runtime, "EDGE_ID", "")).strip()
    asset = str(edge_data.get("asset") or getattr(runtime, "ASSET", "")).strip().upper()
    if not edge_id or not asset:
        raise StatefulRuntimeContractError("Cannot install stateful API without edge_id and asset")
    if not callable(getattr(runtime, "build_features", None)) or not callable(getattr(runtime, "generate_candidate_events", None)):
        raise StatefulRuntimeContractError(f"{edge_id}: standard runtime functions are missing")

    native_feature_ready = bool(
        callable(build_features_live_native) and feature_certified(asset)
    )
    native_event_certification = event_certification(edge_id)
    event_execution_policy = event_execution_classification(edge_id)
    native_event_ready = bool(
        callable(build_events_live_native) and native_event_certification
    )
    contract_hash = (
        native_shared_contract_hash(asset)
        if native_feature_ready and callable(native_shared_contract_hash)
        else shared_feature_contract_hash(runtime, asset)
    )
    setattr(runtime, "STATEFUL_RUNTIME_API_VERSION", STATEFUL_RUNTIME_API_VERSION)
    setattr(runtime, "SHARED_FEATURE_CONTRACT_HASH", contract_hash)
    setattr(runtime, "NATIVE_ONE_ROW_FEATURE_CERTIFIED", native_feature_ready)
    setattr(runtime, "NATIVE_ONE_ROW_EVENT_CERTIFIED", native_event_ready)
    certified_event_mode = str(
        event_execution_policy.get("event_execution_mode") or (
            "native_one_row_certified" if native_event_ready
            else "authoritative_full_history_required_uncertified_fail_closed"
        )
    )
    setattr(runtime, "EVENT_CONTINUATION_CLASSIFICATION", certified_event_mode)
    # Missing/stale certification must never route to the legacy suffix adapter.
    # It remains safe by using unchanged full authority until recertification.
    event_full_authority_required = bool(
        event_execution_policy.get("full_authority_required")
        or not native_event_ready
    )
    setattr(runtime, "EVENT_FULL_AUTHORITY_REQUIRED", event_full_authority_required)

    if not callable(getattr(runtime, "build_features_live_incremental", None)):
        def build_features_live_incremental(
            bars: pd.DataFrame,
            candidate_times: list[pd.Timestamp] | None = None,
            last_processed: pd.Timestamp | None = None,
            drop_warmup_rows: bool = False,
            state: Mapping[str, Any] | None = None,
            research_contract: Mapping[str, Any] | None = None,
            mode: str = "live",
            authoritative_features: pd.DataFrame | None = None,
        ) -> dict[str, Any]:
            del candidate_times, last_processed, research_contract
            if str(mode).lower() != "live":
                raise StatefulRuntimeContractError(
                    f"Unsupported continuation mode: {mode}"
                )
            if native_feature_ready:
                try:
                    return build_features_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        bars=bars,
                        drop_warmup_rows=drop_warmup_rows,
                        state=state,
                        mode=mode,
                        authoritative_features=authoritative_features,
                    )
                except Exception as exc:
                    raise StatefulRuntimeContractError(
                        f"{edge_id}: native one-row feature continuation failed; "
                        f"silent suffix fallback is forbidden; "
                        f"original_error={type(exc).__name__}: {exc}"
                    ) from exc
            return build_features_live_full_authority_for_runtime(
                runtime,
                asset=asset,
                edge_id=edge_id,
                bars=bars,
                drop_warmup_rows=drop_warmup_rows,
                reason="native_feature_certification_missing_or_stale",
            )
        build_features_live_incremental.__name__ = "build_features_live_incremental"
        build_features_live_incremental.__qualname__ = f"{getattr(runtime, '__name__', 'runtime')}.build_features_live_incremental"
        setattr(runtime, "build_features_live_incremental", build_features_live_incremental)

    if not callable(getattr(runtime, "generate_candidate_events_live", None)):
        def generate_candidate_events_live(
            features: pd.DataFrame,
            candidate_times: list[pd.Timestamp] | None = None,
            last_processed: pd.Timestamp | None = None,
            state: Mapping[str, Any] | None = None,
            research_contract: Mapping[str, Any] | None = None,
            mode: str = "live",
            feature_authority_scope: str = "asset",
            feature_authority_generation: int = 0,
        ) -> dict[str, Any]:
            del last_processed, research_contract
            if str(mode).lower() != "live":
                raise StatefulRuntimeContractError(
                    f"Unsupported continuation mode: {mode}"
                )
            if native_event_ready:
                try:
                    native_result = build_events_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        features=features,
                        candidate_times=candidate_times,
                        state=state,
                        mode=mode,
                        feature_authority_scope=feature_authority_scope,
                        feature_authority_generation=feature_authority_generation,
                    )
                    missing_native_proofs = _event_proof_missing(native_result)
                    if missing_native_proofs:
                        raise StatefulRuntimeContractError(
                            "missing native event proof flags "
                            + repr(missing_native_proofs)
                        )
                    return native_result
                except Exception as exc:
                    raise StatefulRuntimeContractError(
                        f"{edge_id}: native one-row event continuation failed; "
                        f"silent suffix fallback is forbidden; "
                        f"original_error={type(exc).__name__}: {exc}"
                    ) from exc

            authority = _normalize_frame(_call_generate_events(runtime, features))
            filtered = _filter_events_to_times(authority, candidate_times)
            tail = authority.tail(EVENT_CHECKPOINT_ROWS).copy()
            feature_frame = _normalize_frame(features)
            next_state = {
                "schema_version": STATEFUL_RUNTIME_API_VERSION,
                "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
                "edge_id": edge_id,
                "asset": asset,
                "last_timestamp": (
                    None if authority.empty
                    else pd.Timestamp(authority["Datetime"].iloc[-1]).isoformat()
                ),
                "last_feature_timestamp": pd.Timestamp(
                    feature_frame["Datetime"].iloc[-1]
                ).isoformat(),
                "output_hash": _frame_hash(tail),
                "event_tail": tail,
                "feature_authority_scope": str(feature_authority_scope),
                "feature_authority_generation": int(feature_authority_generation),
                "continuation_mode": "direct_full_authority_events",
            }
            return {
                "events": filtered,
                "state": next_state,
                "metadata": {
                    "api_version": STATEFUL_RUNTIME_API_VERSION,
                    "continuation_engine_version": CONTINUATION_ENGINE_VERSION,
                    "continuation_mode": "direct_full_authority_events",
                    "event_math_owner": "edge_runtime.generate_candidate_events",
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "event_state_continuity_preserved": True,
                    "edge_local_incremental_optimization": False,
                    "edge_local_authority_rebuilt": True,
                    "full_authority_required": True,
                    "silent_suffix_fallback_used": False,
                    "feature_authority_scope": str(feature_authority_scope),
                    "feature_authority_generation": int(feature_authority_generation),
                    "returned_event_rows": int(len(filtered)),
                },
            }
        generate_candidate_events_live.__name__ = "generate_candidate_events_live"
        generate_candidate_events_live.__qualname__ = f"{getattr(runtime, '__name__', 'runtime')}.generate_candidate_events_live"
        setattr(runtime, "generate_candidate_events_live", generate_candidate_events_live)

    # The dashboard reads the manifest entry for an explicit shared-feature hash.
    if isinstance(edge, dict):
        edge.setdefault("shared_feature_contract_hash", contract_hash)
        edge.setdefault("stateful_runtime_api_version", STATEFUL_RUNTIME_API_VERSION)
        edge.setdefault("continuation_engine_version", CONTINUATION_ENGINE_VERSION)
    return runtime


__all__ = [
    "STATEFUL_RUNTIME_API_VERSION",
    "CONTINUATION_ENGINE_VERSION",
    "EVENT_REQUIRED_PROOF_FLAGS",
    "StatefulRuntimeContractError",
    "build_features_live_incremental_for_runtime",
    "build_features_live_full_authority_for_runtime",
    "generate_candidate_events_live_for_runtime",
    "install_stateful_runtime_api",
    "shared_feature_contract_hash",
]
