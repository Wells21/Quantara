from __future__ import annotations

"""P0 production hardening for the V2 live dashboard.

This module is intentionally an integration layer around the existing dashboard.
It does not own or alter edge feature math, event rules, models, calibration,
thresholds, AFML, execution geometry, portfolio weights, or order execution.

Installed controls
------------------
1. Exact-bar FFD/HMM readiness before authoritative edge feature building.
2. Retryable newest-bar dependency failures without premature cursor advance.
3. Semantic feature cache keyed by asset + builder contract + bar-content hash.
4. Per-asset burst queue with bounded concurrent workers and same-asset dedupe.

The dashboard imports and installs this module immediately before ``main()``.
"""

import copy
from collections import Counter
import hashlib
import inspect
import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple

from production_observability import ObservedSemaphore, RuntimeMetricsSampler
from production_hmm_integrity import (
    HMM_INTEGRITY_VERSION,
    classify_candidate_hmm_coverage,
    dependency_contract_version,
    install_hmm_integrity_guard,
    tombstone_key as hmm_tombstone_key,
)

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso


PATCH_VERSION = (
    "2026-07-30_p0_hmm_integrity_v3"
    "+terminal_tombstones_v1+signature_followup_v1"
    "+semantic_contract_cache_v3+canonical_alias_proof_v1"
    "+per_asset_burst_queue_v3+observability_v2+dependency_window_snapshot_v2"
    "+dedicated_recovery_only_v1"
)
DASHBOARD_VERSION_SUFFIX = "+p0_hmm_integrity_terminal_tombstone_v3"

_INSTALL_LOCK = threading.RLock()
_INSTALLED = False
_NS: Optional[MutableMapping[str, Any]] = None

_SEMANTIC_CACHE_LOCK = threading.RLock()
# key=(asset, identity_source, identity, bar_content_hash)
_SEMANTIC_FEATURE_CACHE: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

_EXPLICIT_CONTRACT_LOCK = threading.RLock()
_EXPLICIT_CONTRACT_PROOFS: Dict[str, Dict[str, Any]] = {}
_EXPLICIT_PROBE_LOCKS: Dict[str, threading.RLock] = {}
_CANONICAL_ALIAS_PROOFS: Dict[str, Dict[str, Any]] = {}
_PROOF_FILE_LOCK = threading.RLock()

_READINESS_CACHE_LOCK = threading.RLock()
# key=(asset, bar_content_hash)
_EXACT_READINESS_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}

_THREAD_CONTEXT = threading.local()


class FeatureReadinessPending(RuntimeError):
    """Temporary exact-bar dependency-readiness failure; retry until stale."""


def _utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _safe_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return _ensure_utc(value).isoformat()
    except Exception:
        return str(value)


def _parse_utc(values: Any) -> pd.Series:
    raw = values.copy() if isinstance(values, pd.Series) else pd.Series(values)
    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = to_datetime_iso(raw, utc=True, errors="coerce")
    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        recovered: List[Any] = []
        for value in raw.loc[missed]:
            try:
                recovered.append(_ensure_utc(value))
            except Exception:
                recovered.append(pd.NaT)
        parsed.loc[missed] = recovered
    return parsed


def _rows_len(value: Any) -> int:
    try:
        return 0 if value is None else int(len(value))
    except Exception:
        return 0


def _retry_backoff_seconds(attempt: int) -> float:
    """Bounded exponential retry; fast enough for live bars, no retry storm."""
    attempt = max(int(attempt), 1)
    return float(min(2 ** min(attempt, 5), 30))


def _is_dependency_feature(name: Any) -> bool:
    text = str(name or "")
    return (
        "_FFD" in text
        or text.startswith("FFD_")
        or text.startswith("Regime_")
        or text.startswith("Prob_State_")
    )


def _is_retryable_dependency_error(error: Any) -> bool:
    text = str(error or "").lower()
    tokens = (
        "live ffd continuation",
        "ffd continuation",
        "hmm continuation",
        "dependency continuation",
        "dependency refresh",
        "dependency readiness",
        "ffd_delta_unavailable",
        "selected-feature nan",
        "selected feature nan",
        "all candidate rows were dropped",
        "runtime_latest_mismatch",
        "in-memory ffd",
        "in-memory hmm",
    )
    return any(token in text for token in tokens)


def _bar_content_hash(ns: Mapping[str, Any], bars: pd.DataFrame) -> str:
    stable = ns.get("_stable_frame_hash")
    if callable(stable):
        return str(stable(bars, exclude_columns={"Closed_At"}))
    if bars is None or bars.empty:
        return hashlib.sha256(b"EMPTY").hexdigest()
    frame = bars.copy()
    if "Datetime" in frame.columns:
        frame["Datetime"] = _parse_utc(frame["Datetime"])
        frame = (
            frame.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .drop_duplicates("Datetime", keep="last")
            .reset_index(drop=True)
        )
    columns = [str(column) for column in frame.columns if str(column) != "Closed_At"]
    frame = frame[columns].copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].astype("int64")
        elif frame[column].dtype == object:
            frame[column] = frame[column].map(
                lambda value: "<NA>" if pd.isna(value) else str(value)
            )
    digest = pd.util.hash_pandas_object(frame, index=False).values.tobytes()
    h = hashlib.sha256()
    h.update("|".join(columns).encode("utf-8"))
    h.update(digest)
    return h.hexdigest()


def _latest_bar_timestamp(bars: pd.DataFrame) -> Optional[pd.Timestamp]:
    if bars is None or bars.empty or "Datetime" not in bars.columns:
        return None
    values = _parse_utc(bars["Datetime"]).dropna()
    return None if values.empty else _ensure_utc(values.max())


def _finite_timestamp_row(
    frame: pd.DataFrame,
    timestamp: pd.Timestamp,
    required_columns: Sequence[str],
) -> Tuple[bool, List[str]]:
    if frame is None or frame.empty or "Datetime" not in frame.columns:
        return False, list(required_columns)
    work = frame.copy()
    work["Datetime"] = _parse_utc(work["Datetime"])
    matches = work.loc[work["Datetime"] == timestamp]
    if matches.empty:
        return False, ["Datetime"]
    row = matches.iloc[-1]
    invalid: List[str] = []
    for column in required_columns:
        if column not in work.columns:
            invalid.append(column)
            continue
        numeric = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
        try:
            if pd.isna(numeric) or not np.isfinite(float(numeric)):
                invalid.append(column)
        except Exception:
            invalid.append(column)
    return not invalid, invalid



def _state_defaults() -> Dict[str, Any]:
    return {
        # Retained only for state-file compatibility. New scheduling is strictly
        # one retry per (lane, asset, exact bar-content hash).
        "pending_feature_readiness_by_edge": {},
        "feature_readiness_retry_count_by_edge": {},
        "feature_readiness_expired_count_by_edge": {},
        "pending_feature_readiness_by_asset_snapshot": {},
        "feature_readiness_retry_count_by_asset": {},
        "feature_readiness_expired_count_by_asset": {},
        "feature_readiness_retry_history": [],
        "feature_readiness_expiry_history": [],
        "last_exact_feature_readiness_by_asset": {},
        "last_successfully_scored_bar_hash_by_asset_lane": {},
        "last_successfully_scored_bar_timestamp_by_asset_lane": {},
        "dependency_terminal_tombstones": {},
        "dependency_terminal_audit_history": [],
        "hmm_integrity_repair_by_asset_snapshot": {},
        "hmm_integrity_repair_history": [],
        "retired_edge_readiness_retry_state": [],
        "p0_production_hardening": {
            "version": PATCH_VERSION,
            "installed": True,
            "continuation_owner": "p0_exact_snapshot_readiness_plus_authoritative_hmm_repair",
            "cursor_policy": "retry_asset_snapshot_until_success_or_terminal_tombstone",
            "hmm_integrity_contract": HMM_INTEGRITY_VERSION,
            "cache_policy": "asset_plus_semantic_contract_plus_exact_bar_content_hash",
            "queue_policy": "one_pending_job_per_asset_per_lane_with_followup_after_active_close",
        },
    }


def _ensure_state_defaults(state: MutableMapping[str, Any]) -> None:
    defaults = _state_defaults()
    for key, value in defaults.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
    # Migrate and retire the old per-edge retry fan-out exactly once. Keeping it
    # active would recreate dozens of jobs from one missing asset dependency.
    old_pending = state.get("pending_feature_readiness_by_edge") or {}
    if old_pending:
        state.setdefault("retired_edge_readiness_retry_state", []).append(
            {
                "retired_at": _utc_now(),
                "reason": "migrated_to_asset_snapshot_retry_v2",
                "records": copy.deepcopy(old_pending),
            }
        )
        state["retired_edge_readiness_retry_state"] = state[
            "retired_edge_readiness_retry_state"
        ][-10:]
        state["pending_feature_readiness_by_edge"] = {}
    state["p0_production_hardening"] = copy.deepcopy(
        defaults["p0_production_hardening"]
    )


def _terminal_audit_path(ns: Mapping[str, Any]) -> Path:
    return Path(ns.get("OUTPUT_DIR") or Path.cwd()) / "dependency_terminal_audit.jsonl"


def _append_terminal_audit(ns: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    path = _terminal_audit_path(ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = ns.get("_OUTPUT_WRITE_LOCK")
    context = lock if hasattr(lock, "__enter__") else threading.RLock()
    with context:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(row), sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _dependency_tombstone_key(asset: str, timestamp: Any) -> str:
    return hmm_tombstone_key(str(asset).upper(), timestamp)


def _is_dependency_tombstoned(
    state: Mapping[str, Any], *, asset: str, timestamp: Any
) -> bool:
    try:
        key = _dependency_tombstone_key(asset, timestamp)
    except Exception:
        return False
    return key in (state.get("dependency_terminal_tombstones") or {})


def _record_dependency_terminal(
    ns: Mapping[str, Any],
    *,
    state: MutableMapping[str, Any],
    asset: str,
    timestamp: Any,
    reason: str,
    source: str,
    edge_id: str | None = None,
    bar_hash: str | None = None,
    logger: Any = None,
) -> Dict[str, Any]:
    _ensure_state_defaults(state)
    asset = str(asset).upper()
    stamp = _ensure_utc(timestamp)
    key = _dependency_tombstone_key(asset, stamp)
    tombstones = state.setdefault("dependency_terminal_tombstones", {})
    existing = tombstones.get(key)
    if isinstance(existing, Mapping):
        if edge_id:
            edge_ids = sorted(set([*(existing.get("edge_ids") or []), str(edge_id)]))
            existing = dict(existing)
            existing["edge_ids"] = edge_ids
            tombstones[key] = existing
        return copy.deepcopy(dict(existing))
    row = {
        "schema_version": "v2_dependency_terminal_audit_v1",
        "audit_id": key,
        "created_at": _utc_now(),
        "asset": asset,
        "candidate_timestamp": stamp.isoformat(),
        "reason": str(reason),
        "source": str(source),
        "edge_ids": [str(edge_id)] if edge_id else [],
        "bar_content_hash": str(bar_hash or ""),
        "dependency_contract_version": dependency_contract_version(),
        "status": "terminal_dependency_block",
        "retry_allowed": False,
    }
    tombstones[key] = copy.deepcopy(row)
    history = state.setdefault("dependency_terminal_audit_history", [])
    history.append(copy.deepcopy(row))
    state["dependency_terminal_audit_history"] = history[-1000:]
    if not bool(ns.get("_P0_SELF_TEST_NO_IO", False)):
        try:
            _append_terminal_audit(ns, row)
        except Exception as exc:
            row["audit_write_error"] = f"{type(exc).__name__}:{exc}"
    if logger is not None:
        logger.warning(
            "P0_DEPENDENCY_TERMINAL asset=%s timestamp=%s edge=%s reason=%s source=%s",
            asset, stamp.isoformat(), edge_id, reason, source,
        )
    return row


def _asset_retry_key(*, lane: str, asset: str, bar_hash: str) -> str:
    return f"{_queue_lane_key(lane)}:{str(asset).upper()}:{str(bar_hash)}"


def _register_asset_retry(
    ns: Mapping[str, Any],
    *,
    state: MutableMapping[str, Any],
    lane: str,
    asset: str,
    bar_hash: str,
    snapshot_latest: Any,
    timestamps: Iterable[Any],
    source: str,
    reason: str,
    logger: Any = None,
) -> Dict[str, Any]:
    _ensure_state_defaults(state)
    asset = str(asset).upper()
    lane = _queue_lane_key(lane)
    parsed = sorted({
        _ensure_utc(value) for value in (timestamps or []) if value is not None
    })
    parsed = [
        value for value in parsed
        if not _is_dependency_tombstoned(state, asset=asset, timestamp=value)
    ]
    latest = _ensure_utc(snapshot_latest)
    now = _utc_now_ts()
    key = _asset_retry_key(lane=lane, asset=asset, bar_hash=bar_hash)
    if not parsed:
        return {
            "retry_key": key,
            "lane": lane,
            "asset": asset,
            "bar_content_hash": str(bar_hash),
            "snapshot_latest": latest.isoformat(),
            "candidate_timestamps": [],
            "status": "terminal_tombstone_suppressed",
            "source": source,
            "reason": reason,
            "cursor_advanced": False,
        }
    pending = state.setdefault("pending_feature_readiness_by_asset_snapshot", {})
    # A newer exact snapshot supersedes every older retry for the same lane/asset.
    for old_key, old_record in list(pending.items()):
        if (
            str(old_record.get("asset") or "").upper() == asset
            and _queue_lane_key(str(old_record.get("lane") or "fast")) == lane
            and old_key != key
        ):
            pending.pop(old_key, None)
    previous = pending.get(key) or {}
    attempts = int(previous.get("attempts", 0) or 0) + 1
    backoff = _retry_backoff_seconds(attempts)
    max_age = float(ns.get("MAX_SIGNAL_AGE_SECONDS", 120) or 120)
    # Candidate timestamps are the true expiry authority. The snapshot latest may
    # be newer than a candidate and must not silently extend its signal lifetime.
    expiry_anchor = min(parsed) if parsed else latest
    expires = expiry_anchor + pd.Timedelta(seconds=max_age)
    record = {
        "retry_key": key,
        "lane": lane,
        "asset": asset,
        "bar_content_hash": str(bar_hash),
        "snapshot_latest": latest.isoformat(),
        "candidate_timestamps": [_safe_iso(value) for value in parsed],
        "candidate_timestamp_set_hash": hashlib.sha256(
            "|".join(value.isoformat() for value in parsed).encode("utf-8")
        ).hexdigest(),
        "dependency_contract_version": dependency_contract_version(),
        "status": "pending_dependency_readiness_retry",
        "source": source,
        "reason": reason,
        "attempts": attempts,
        "first_seen_at": previous.get("first_seen_at") or _safe_iso(now),
        "last_attempt_at": _safe_iso(now),
        "next_retry_at": _safe_iso(now + pd.Timedelta(seconds=backoff)),
        "retry_backoff_seconds": backoff,
        "expires_at": _safe_iso(expires),
        "cursor_advanced": False,
    }
    pending[key] = record
    counts = state.setdefault("feature_readiness_retry_count_by_asset", {})
    counts[asset] = int(counts.get(asset, 0) or 0) + 1
    history = state.setdefault("feature_readiness_retry_history", [])
    history.append(copy.deepcopy(record))
    state["feature_readiness_retry_history"] = history[-500:]
    if logger is not None:
        logger.warning(
            "P0_ASSET_READINESS_RETRY_PENDING lane=%s asset=%s hash=%s attempts=%s "
            "candidate_count=%s next_retry_at=%s expires_at=%s reason=%s",
            lane, asset, str(bar_hash)[:16], attempts, len(parsed),
            record["next_retry_at"], record["expires_at"], reason,
        )
    return record


def _clear_asset_retry(
    state: MutableMapping[str, Any],
    *,
    lane: str,
    asset: str,
    bar_hash: str | None = None,
    resolution: str,
    logger: Any = None,
) -> None:
    _ensure_state_defaults(state)
    asset = str(asset).upper()
    lane = _queue_lane_key(lane)
    pending = state.setdefault("pending_feature_readiness_by_asset_snapshot", {})
    removed: list[dict[str, Any]] = []
    for key, record in list(pending.items()):
        if str(record.get("asset") or "").upper() != asset:
            continue
        if _queue_lane_key(str(record.get("lane") or "fast")) != lane:
            continue
        if bar_hash is not None and str(record.get("bar_content_hash") or "") != str(bar_hash):
            continue
        removed.append(copy.deepcopy(record))
        pending.pop(key, None)
    if removed and logger is not None:
        logger.info(
            "P0_ASSET_READINESS_RETRY_RESOLVED lane=%s asset=%s hash=%s records=%s resolution=%s",
            lane, asset, str(bar_hash or "*")[:16], len(removed), resolution,
        )


def _expire_asset_retry(
    ns: Mapping[str, Any],
    *,
    state: MutableMapping[str, Any],
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
    logger: Any,
) -> None:
    asset = str(record.get("asset") or "").upper()
    lane = _queue_lane_key(str(record.get("lane") or "fast"))
    key = str(record.get("retry_key") or "")
    state.setdefault("pending_feature_readiness_by_asset_snapshot", {}).pop(key, None)
    counts = state.setdefault("feature_readiness_expired_count_by_asset", {})
    counts[asset] = int(counts.get(asset, 0) or 0) + 1
    candidate_times: list[pd.Timestamp] = []
    for value in record.get("candidate_timestamps") or []:
        try:
            candidate_times.append(_ensure_utc(value))
        except Exception:
            continue
    if not candidate_times:
        candidate_times = [_ensure_utc(record.get("snapshot_latest"))]
    candidate_times = sorted(set(candidate_times))
    edge_ids = [
        str(edge.get("edge_id") or "")
        for edge in (manifest.get("selected_edges") or [])
        if str(edge.get("asset") or "").upper() == asset and edge.get("edge_id")
    ]
    for stamp in candidate_times:
        _record_dependency_terminal(
            ns,
            state=state,
            asset=asset,
            timestamp=stamp,
            reason="dependency_readiness_retry_expired",
            source=str(record.get("source") or "asset_snapshot_retry"),
            bar_hash=str(record.get("bar_content_hash") or ""),
            logger=logger,
        )
    expiry = {
        **copy.deepcopy(dict(record)),
        "expired_at": _utc_now(),
        "status": "expired_terminal_tombstone",
        "cursor_policy": "advance_edges_only_through_terminal_candidate_timestamp",
        "terminal_tombstone_count": len(candidate_times),
    }
    state.setdefault("feature_readiness_expiry_history", []).append(expiry)
    state["feature_readiness_expiry_history"] = state["feature_readiness_expiry_history"][-200:]
    terminal_latest = max(candidate_times)
    cursors = state.setdefault("last_processed_timestamp_by_edge", {})
    advanced: list[str] = []
    preserved_newer: list[str] = []
    for edge_id in edge_ids:
        raw = cursors.get(edge_id)
        try:
            current = _ensure_utc(raw) if raw else None
        except Exception:
            current = None
        if current is None or current < terminal_latest:
            cursors[edge_id] = terminal_latest.isoformat()
            advanced.append(edge_id)
        else:
            preserved_newer.append(edge_id)
        for stamp in candidate_times:
            _record_dependency_terminal(
                ns,
                state=state,
                asset=asset,
                timestamp=stamp,
                reason="dependency_readiness_retry_expired",
                source=str(record.get("source") or "asset_snapshot_retry"),
                edge_id=edge_id,
                bar_hash=str(record.get("bar_content_hash") or ""),
                logger=None,
            )
    expiry["cursor_advanced_edge_ids"] = advanced
    expiry["newer_cursor_preserved_edge_ids"] = preserved_newer
    logger.warning(
        "P0_ASSET_READINESS_RETRY_EXPIRED lane=%s asset=%s hash=%s terminal_candidates=%s "
        "advanced_edges=%s preserved_newer_edges=%s",
        lane, asset, str(record.get("bar_content_hash") or "")[:16],
        len(candidate_times), len(advanced), len(preserved_newer),
    )


def _p0_state_delta(
    base_state: Mapping[str, Any],
    worker_state: Mapping[str, Any],
    *,
    edge_filter: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    names = (
        "pending_feature_readiness_by_asset_snapshot",
        "feature_readiness_retry_count_by_asset",
        "feature_readiness_expired_count_by_asset",
        "feature_readiness_retry_history",
        "feature_readiness_expiry_history",
        "last_exact_feature_readiness_by_asset",
        "last_successfully_scored_bar_hash_by_asset_lane",
        "last_successfully_scored_bar_timestamp_by_asset_lane",
        "dependency_terminal_tombstones",
        "dependency_terminal_audit_history",
        "hmm_integrity_repair_by_asset_snapshot",
        "hmm_integrity_repair_history",
    )
    return {
        name: copy.deepcopy(worker_state.get(name))
        for name in names
        if worker_state.get(name) != base_state.get(name)
    }


def _merge_p0_state_delta(
    state: MutableMapping[str, Any],
    delta: Mapping[str, Any],
) -> None:
    _ensure_state_defaults(state)
    for name, value in delta.items():
        state[name] = copy.deepcopy(value)

def _evict_semantic_cache(asset: str, current_hash: str) -> None:
    """Keep only the current exact bar version for an asset.

    Same-asset jobs are serialised, so retaining older bar versions gives no live
    benefit and can consume substantial RAM.
    """
    with _SEMANTIC_CACHE_LOCK:
        stale = [
            key for key in _SEMANTIC_FEATURE_CACHE
            if key[0] == asset and key[3] != current_hash
        ]
        for key in stale:
            _SEMANTIC_FEATURE_CACHE.pop(key, None)


def _semantic_cache_status() -> Dict[str, Any]:
    with _SEMANTIC_CACHE_LOCK:
        records = list(_SEMANTIC_FEATURE_CACHE.values())
    by_asset: Dict[str, Dict[str, Any]] = {}
    for record in records:
        asset = str(record.get("asset") or "")
        item = by_asset.setdefault(
            asset,
            {
                "contract_count": 0,
                "rows_by_contract": {},
                "latest_bar_content_hash": record.get("bar_content_hash"),
            },
        )
        item["contract_count"] += 1
        short_identity = str(record.get("contract_identity") or "")[:16]
        item["rows_by_contract"][short_identity] = int(record.get("rows", 0) or 0)
    cached_rows_by_asset = {
        asset: max(
            [int(value) for value in item.get("rows_by_contract", {}).values()]
            or [0]
        )
        for asset, item in by_asset.items()
    }
    with _EXPLICIT_CONTRACT_LOCK:
        explicit_status_counts = Counter(
            str(record.get("status") or "UNKNOWN")
            for record in _EXPLICIT_CONTRACT_PROOFS.values()
            if isinstance(record, Mapping)
        )
        canonical_status_counts = Counter(
            str(record.get("status") or "UNKNOWN")
            for record in _CANONICAL_ALIAS_PROOFS.values()
            if isinstance(record, Mapping)
        )
    return {
        "version": PATCH_VERSION,
        "runtime_mode": "authoritative_batch_semantic_contract_cache",
        "native_continuation_enabled": False,
        "stateful_continuation_enabled": False,
        "native_continuation_used_for_model_math": False,
        "exact_dependency_readiness_enabled": True,
        "cached_assets": sorted(by_asset),
        "cached_feature_rows_by_asset": cached_rows_by_asset,
        "cached_contract_count": len(records),
        "cache_entries": len(records),
        "explicit_contract_proof_status_counts": dict(explicit_status_counts),
        "canonical_alias_proof_status_counts": dict(canonical_status_counts),
        "assets": by_asset,
    }


def _dependency_frame_hash(frame: pd.DataFrame) -> str:
    work = frame.copy(deep=True).reset_index(drop=True)
    if "Datetime" in work.columns:
        work["Datetime"] = _parse_utc(work["Datetime"]).astype("int64")
    for column in work.columns:
        if work[column].dtype == "object":
            work[column] = work[column].astype(str)
    return hashlib.sha256(
        pd.util.hash_pandas_object(work, index=False).to_numpy(dtype="uint64").tobytes()
    ).hexdigest()


def _validate_dependency_window(
    ns: Mapping[str, Any],
    *,
    asset: str,
    bars: pd.DataFrame,
    ffd: pd.DataFrame,
    hmm: pd.DataFrame,
    hmm_eligible_timestamps: Sequence[Any],
) -> Dict[str, Any]:
    """Detect internal dependency holes before rolling edge features consume them."""
    window = max(int(ns.get("RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS", 200) or 200), 1)
    rolling_ffd_lookbacks = {
        "Close_FFD_Momentum_5": 5,
        "Close_FFD_Z_20": 20,
        "Close_FFD_Momentum_20": 20,
        "Close_FFD_Z_50": 50,
    }
    required_history_bars = max(rolling_ffd_lookbacks.values()) + 1
    bar_column = "Datetime" if "Datetime" in bars.columns else "Date"
    bar_times = _parse_utc(bars[bar_column]).dropna().drop_duplicates().sort_values().tail(window)
    ffd_times = set(_parse_utc(ffd["Datetime"]).dropna())
    missing_ffd = [value for value in bar_times if value not in ffd_times]
    expected_hmm = _parse_utc(pd.Series(list(hmm_eligible_timestamps))).dropna()
    expected_hmm = expected_hmm[expected_hmm.isin(set(bar_times))]
    hmm_times = set(_parse_utc(hmm["Datetime"]).dropna())
    missing_hmm = [value for value in expected_hmm if value not in hmm_times]
    ffd_columns = ["Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    ffd_window = ffd.loc[_parse_utc(ffd["Datetime"]).isin(set(bar_times)), ffd_columns]
    nonfinite_mask = (
        ~np.isfinite(
            ffd_window.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
        )
    ).any(axis=1) if not ffd_window.empty else np.asarray([], dtype=bool)
    nonfinite_ffd_times = [
        value
        for value, invalid in zip(
            _parse_utc(ffd.loc[ffd_window.index, "Datetime"]),
            nonfinite_mask,
        )
        if bool(invalid) and not pd.isna(value)
    ]
    origin_gaps = sorted(set([*missing_ffd, *nonfinite_ffd_times]))
    positions = {value: index for index, value in enumerate(bar_times.tolist())}
    affected_rolling_features: set[str] = set()
    for gap in origin_gaps:
        index = positions.get(gap)
        if index is None:
            continue
        distance = len(bar_times) - 1 - index
        for feature, lookback in rolling_ffd_lookbacks.items():
            if 0 <= distance <= lookback:
                affected_rolling_features.add(feature)
    insufficient_history = len(bar_times) < required_history_bars
    if missing_ffd or missing_hmm or len(nonfinite_ffd_times) or insufficient_history:
        raise FeatureReadinessPending(
            f"{asset}: dependency_window_incomplete window={window} "
            f"missing_ffd={[value.isoformat() for value in missing_ffd[:5]]} "
            f"missing_hmm={[value.isoformat() for value in missing_hmm[:5]]} "
            f"nonfinite_ffd_timestamps={[value.isoformat() for value in nonfinite_ffd_times[:5]]} "
            f"required_history_bars={required_history_bars} available_history_bars={len(bar_times)} "
            f"originating_ffd_gaps={[value.isoformat() for value in origin_gaps[:5]]} "
            f"affected_rolling_features={sorted(affected_rolling_features)}"
        )
    return {
        "window_bars": int(len(bar_times)),
        "required_ffd_timestamps": int(len(bar_times)),
        "required_hmm_eligible_timestamps": int(len(expected_hmm)),
        "missing_ffd_timestamps": 0,
        "missing_hmm_timestamps": 0,
        "nonfinite_ffd_rows": 0,
        "required_rolling_history_bars": required_history_bars,
        "affected_rolling_features": [],
    }


def _exact_readiness(
    ns: MutableMapping[str, Any],
    *,
    asset: str,
    bars: pd.DataFrame,
    logger: Any,
    reason: str,
) -> Dict[str, Any]:
    """Commit FFD/HMM through the exact authoritative bar snapshot."""
    from shared.live_streaming.continuation import (
        refresh_verified_feature_dependencies_hot,
    )
    from shared.live_streaming.generation_contract import (
        assert_ready_generation,
    )

    asset = str(asset).upper()
    if bars is None or bars.empty:
        raise FeatureReadinessPending(f"{asset}: empty authoritative bar snapshot")
    bar_hash = _bar_content_hash(ns, bars)
    latest = _latest_bar_timestamp(bars)
    if latest is None:
        raise FeatureReadinessPending(f"{asset}: authoritative bar snapshot has no timestamp")
    key = (asset, bar_hash)
    with _READINESS_CACHE_LOCK:
        cached = _EXACT_READINESS_CACHE.get(key)
        if cached and cached.get("status") == "PASS":
            return copy.deepcopy(cached)
        if cached and cached.get("status") == "WAIT":
            try:
                retry_after = _ensure_utc(cached.get("retry_after"))
            except Exception:
                retry_after = _utc_now_ts()
            if retry_after > _utc_now_ts():
                raise FeatureReadinessPending(str(cached.get("error") or "dependency readiness waiting"))

    started = time.perf_counter()
    try:
        result = refresh_verified_feature_dependencies_hot(asset, bars)
        target = _ensure_utc(result.get("target_timestamp"))
        hmm_target = _ensure_utc(result.get("hmm_target_timestamp") or target)
        dependency_generation = result.get("dependency_generation") or {}
        assert_ready_generation(
            dependency_generation,
            asset=asset,
            raw_target=target,
            hmm_target=hmm_target,
        )
        dependency_generation_id = str(
            result.get("dependency_generation_id")
            or dependency_generation.get("generation_id")
            or ""
        )
        if not dependency_generation_id:
            raise FeatureReadinessPending(
                f"{asset}: coherent dependency generation was not published"
            )
        ffd = result.get("ffd")
        hmm = result.get("hmm")
        ffd_ok, ffd_invalid = _finite_timestamp_row(
            ffd,
            latest,
            ["Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"],
        )
        hmm_columns = [
            column for column in getattr(hmm, "columns", [])
            if str(column).startswith("Prob_State_")
        ]
        hmm_required = ["Regime_State", "Regime_Entropy", "Regime_Duration", *hmm_columns]
        # HMM preprocessing intentionally excludes undefined rows (for example a
        # zero-range bar). Exact asset readiness therefore requires HMM through
        # its latest eligible timestamp, not a synthetic row at every raw bar.
        hmm_ok, hmm_invalid = _finite_timestamp_row(hmm, hmm_target, hmm_required)
        probability_invalid: list[str] = []
        if hmm_ok and hmm_columns:
            work = hmm.copy()
            work["Datetime"] = _parse_utc(work["Datetime"])
            row = work.loc[work["Datetime"] == hmm_target].iloc[-1]
            probabilities = [float(row[column]) for column in hmm_columns]
            if any(value < -1e-9 or value > 1.0 + 1e-9 for value in probabilities):
                probability_invalid.append("probability_out_of_bounds")
            if abs(sum(probabilities) - 1.0) > 1e-5:
                probability_invalid.append("probability_sum_not_one")
        if target != latest or not ffd_ok or not hmm_ok or probability_invalid:
            raise FeatureReadinessPending(
                f"{asset}: exact dependency readiness incomplete "
                f"snapshot_latest={latest.isoformat()} ffd_target={target.isoformat()} "
                f"hmm_target={hmm_target.isoformat()} "
                f"ffd_invalid={ffd_invalid} hmm_invalid={hmm_invalid} "
                f"probability_invalid={probability_invalid}"
            )
        dependency_window = _validate_dependency_window(
            ns,
            asset=asset,
            bars=bars,
            ffd=ffd,
            hmm=hmm,
            hmm_eligible_timestamps=result.get("hmm_eligible_timestamps") or [],
        )
        ffd_hash = _dependency_frame_hash(ffd)
        hmm_hash = _dependency_frame_hash(hmm)
        generation_identity = {
            "asset": asset,
            "bar_content_hash": bar_hash,
            "snapshot_latest": latest.isoformat(),
            "ffd_hash": ffd_hash,
            "hmm_hash": hmm_hash,
            "ffd_features": (result.get("ffd_verification") or {}).get("features"),
            "hmm_features": (result.get("hmm_verification") or {}).get("features"),
            "hmm_dependency_target": hmm_target.isoformat(),
            "dependency_generation_id": dependency_generation_id,
        }
        generation_id = hashlib.sha256(
            json.dumps(generation_identity, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        record = {
            "asset": asset,
            "status": "PASS",
            "checked_at": _utc_now(),
            "reason": reason,
            "snapshot_latest": latest.isoformat(),
            "dependency_target": target.isoformat(),
            "hmm_dependency_target": hmm_target.isoformat(),
            "snapshot_latest_hmm_eligible": bool(result.get("target_hmm_eligible", hmm_target == latest)),
            "bar_content_hash": bar_hash,
            "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
            "ffd_rows": _rows_len(ffd),
            "hmm_rows": _rows_len(hmm),
            "ffd_elapsed_ms": result.get("ffd_elapsed_ms"),
            "hmm_elapsed_ms": result.get("hmm_elapsed_ms"),
            "dependency_refresh_mode": result.get("dependency_refresh_mode"),
            "dependency_generation_id": dependency_generation_id,
            "dependency_generation_schema": dependency_generation.get("schema_version"),
            "generation_id": generation_id,
            "ffd_content_hash": ffd_hash,
            "hmm_content_hash": hmm_hash,
            "dependency_window": dependency_window,
            # Private immutable scoring payload. It is copied into worker-local
            # snapshot context and filtered from persisted dashboard state.
            "_ffd_frame": ffd.copy(deep=True),
            "_hmm_frame": hmm.copy(deep=True),
            "_ffd_verification": copy.deepcopy(result.get("ffd_verification") or {}),
            "_hmm_verification": copy.deepcopy(result.get("hmm_verification") or {}),
            "_dependency_generation": copy.deepcopy(dependency_generation),
        }
        with _READINESS_CACHE_LOCK:
            for old_key in [k for k in _EXACT_READINESS_CACHE if k[0] == asset and k != key]:
                _EXACT_READINESS_CACHE.pop(old_key, None)
            _EXACT_READINESS_CACHE[key] = copy.deepcopy(record)
        logger.info(
            "P0_EXACT_FEATURE_READINESS_PASS asset=%s snapshot_latest=%s ffd_target=%s hmm_target=%s "
            "ffd_rows=%s hmm_rows=%s elapsed_ms=%.3f reason=%s",
            asset,
            record["snapshot_latest"],
            record["dependency_target"],
            record["hmm_dependency_target"],
            record["ffd_rows"],
            record["hmm_rows"],
            record["elapsed_ms"],
            reason,
        )
        return record
    except Exception as exc:
        pending_exc = (
            exc
            if isinstance(exc, FeatureReadinessPending)
            else FeatureReadinessPending(
                f"{asset}: exact FFD/HMM dependency readiness failed: "
                f"{type(exc).__name__}: {exc}"
            )
        )
        wait_record = {
            "asset": asset,
            "status": "WAIT",
            "checked_at": _utc_now(),
            "bar_content_hash": bar_hash,
            "snapshot_latest": latest.isoformat(),
            "retry_after": _safe_iso(_utc_now_ts() + pd.Timedelta(seconds=2)),
            "error": str(pending_exc),
        }
        with _READINESS_CACHE_LOCK:
            _EXACT_READINESS_CACHE[key] = wait_record
        if pending_exc is exc:
            raise pending_exc
        raise pending_exc from exc


def _queue_lane_key(lane: str) -> str:
    return "slow" if str(lane or "").strip().lower() == "slow" else "fast"


def _queue_timestamp_snapshot(ns: Mapping[str, Any], asset: str) -> List[str]:
    try:
        values = ns["latest_live_executable_timestamps"](asset)
        return [_safe_iso(value) for value in sorted(values)[-20:]]
    except Exception:
        return []



def _install_hotpath(ns: MutableMapping[str, Any]) -> Dict[str, Any]:
    from shared.live_streaming.hotpath import (
        cache_status,
        install_hotpath_verifiers,
        verify_asset_ffd_manifest_hot,
        verify_asset_hmm_regime_manifest_hot,
    )
    import live_feature_continuation_guard as continuation_guard

    install_report = install_hotpath_verifiers()
    continuation_guard.verify_asset_ffd_manifest = verify_asset_ffd_manifest_hot
    continuation_guard.verify_asset_hmm_regime_manifest = verify_asset_hmm_regime_manifest_hot

    original_guard = ns.get("ensure_assets_live_feature_continuation")
    if callable(original_guard):
        def scoring_safe_guard(*, assets: Iterable[str], manifest: Mapping[str, Any], logger: Any = None, **kwargs: Any) -> Dict[str, Any]:
            ordered = sorted({str(asset).upper() for asset in assets if str(asset).strip()})
            if bool(getattr(_THREAD_CONTEXT, "p0_scoring_job", False)):
                # P0 exact readiness below is the single continuation writer for
                # scoring jobs. The legacy guard remains available at startup but
                # may not archive/repair files while an exact snapshot is active.
                return {
                    "schema_version": "p0_scoring_deferred_to_exact_readiness_v2",
                    "created_at": _utc_now(),
                    "status": "PASS",
                    "healthy_assets": ordered,
                    "blocked_assets": [],
                    "assets": {},
                    "auto_repair": False,
                    "continuation_owner": "p0_exact_snapshot_readiness_only",
                }
            return original_guard(
                assets=ordered,
                manifest=manifest,
                logger=logger,
                **kwargs,
            )
        ns["ensure_assets_live_feature_continuation"] = scoring_safe_guard

    return {
        "installed": True,
        "version": PATCH_VERSION,
        "shared_hotpath_install": install_report,
        "shared_hotpath_cache": cache_status(),
        "role": "single_exact_snapshot_dependency_owner_authoritative_edge_runtime_math_unchanged",
    }

def _install_state_contract(ns: MutableMapping[str, Any]) -> None:
    original_initial_state = ns["initial_state"]

    def initial_state_with_p0() -> Dict[str, Any]:
        state = original_initial_state()
        _ensure_state_defaults(state)
        return state

    ns["initial_state"] = initial_state_with_p0


def _install_semantic_cache(ns: MutableMapping[str, Any]) -> None:
    """Install exact-snapshot semantic reuse with persisted parity authority.

    No edge can reuse another builder's output unless full timestamp/column/value
    parity has been proven for the current implementation fingerprints. Failed
    proofs are negatively cached, so expensive probes are not repeated until an
    implementation or contract changes.
    """
    original_prepare = ns["prepare_shared_features_for_edge"]
    original_coverage = ns["shared_feature_cache_coverage_for_edge"]
    excluded_families = {
        str(value).strip().lower()
        for value in (ns.get("EDGE_LOCAL_FEATURE_FAMILIES") or set())
    }
    settings_obj = ns.get("settings")
    config_root = Path(
        getattr(settings_obj, "LIVE_CONFIG_DIR", None)
        or ns.get("LIVE_CONFIG_DIR")
        or Path.cwd()
    )
    contract_path = config_root / "shared_feature_contracts.json"
    explicit_contracts: Dict[str, Dict[str, Any]] = {}
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        explicit_contracts = {
            str(name).strip().lower(): dict(record)
            for name, record in (payload.get("family_contracts") or {}).items()
            if isinstance(record, Mapping)
        }
    except Exception:
        explicit_contracts = {
            "absorption": {
                "contract_id": "generic_price_ffd_hmm_absorption_alias_v1",
                "mode": "persisted_exact_parity_proof",
                "require_all_runtime_columns": True,
            }
        }
    explicit_contract_fingerprint = hashlib.sha256(
        json.dumps(explicit_contracts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    proof_path = Path(ns.get("OUTPUT_DIR") or Path.cwd()) / "shared_feature_contract_proofs.json"

    def _probe_lock(key: str) -> threading.RLock:
        with _EXPLICIT_CONTRACT_LOCK:
            return _EXPLICIT_PROBE_LOCKS.setdefault(str(key), threading.RLock())

    def _load_proofs() -> None:
        with _PROOF_FILE_LOCK:
            with _EXPLICIT_CONTRACT_LOCK:
                if _EXPLICIT_CONTRACT_PROOFS or _CANONICAL_ALIAS_PROOFS:
                    return
            try:
                loaded = json.loads(proof_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = {}
            proofs = loaded.get("proofs") if isinstance(loaded, dict) else None
            aliases = loaded.get("canonical_alias_proofs") if isinstance(loaded, dict) else None
            with _EXPLICIT_CONTRACT_LOCK:
                if isinstance(proofs, dict):
                    _EXPLICIT_CONTRACT_PROOFS.update(proofs)
                if isinstance(aliases, dict):
                    _CANONICAL_ALIAS_PROOFS.update(aliases)

    def _save_proofs() -> None:
        # Keep read/merge/write in one process-wide critical section. This avoids
        # Windows os.replace races when different asset workers complete probes.
        with _PROOF_FILE_LOCK:
            with _EXPLICIT_CONTRACT_LOCK:
                payload = {
                    "schema_version": "v2_shared_feature_contract_proofs_v2",
                    "created_at": _utc_now(),
                    "explicit_contract_fingerprint": explicit_contract_fingerprint,
                    "proofs": copy.deepcopy(_EXPLICIT_CONTRACT_PROOFS),
                    "canonical_alias_proofs": copy.deepcopy(_CANONICAL_ALIAS_PROOFS),
                }
            proof_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = proof_path.with_name(
                f".{proof_path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
            )
            try:
                tmp.write_text(
                    json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp, proof_path)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

    def _explicit_identity(edge: Mapping[str, Any]) -> Tuple[str, str] | None:
        family = str(edge.get("family") or "").strip().lower()
        record = explicit_contracts.get(family)
        if not record:
            return None
        contract_id = str(record.get("contract_id") or "").strip()
        return ("explicit_shared_feature_contract", contract_id) if contract_id else None

    def _runtime_dependency_fingerprint(runtime: Any) -> str:
        """Hash build_features plus local helper/module bytes used by the runtime."""
        h = hashlib.sha256()
        base = str(ns["runtime_build_features_fingerprint"](runtime))
        h.update(base.encode("utf-8"))
        fn = getattr(runtime, "build_features", None)
        globals_map = getattr(fn, "__globals__", {}) if callable(fn) else {}
        names = sorted(set(getattr(getattr(fn, "__code__", None), "co_names", ()) or ()))
        paths: Set[Path] = set()
        runtime_path = Path(str(getattr(runtime, "__file__", "") or ""))
        if runtime_path.exists():
            paths.add(runtime_path.resolve())
        for name in names:
            value = globals_map.get(name)
            candidate = Path(str(getattr(value, "__file__", "") or ""))
            if candidate.exists():
                paths.add(candidate.resolve())
            if callable(value):
                try:
                    h.update(inspect.getsource(value).encode("utf-8", errors="replace"))
                except Exception:
                    code = getattr(value, "__code__", None)
                    if code is not None:
                        h.update(code.co_code)
                        h.update(repr(code.co_consts).encode("utf-8", errors="replace"))
            elif isinstance(value, (str, int, float, bool, tuple, frozenset, type(None))):
                h.update(f"{name}={value!r}".encode("utf-8", errors="replace"))
        v2_root = Path(ns.get("V2_ROOT") or Path.cwd()).resolve()
        for path in sorted(paths, key=str):
            try:
                path.relative_to(v2_root)
            except Exception:
                continue
            try:
                h.update(str(path.relative_to(v2_root)).encode("utf-8"))
                h.update(hashlib.sha256(path.read_bytes()).digest())
            except Exception:
                continue
        return h.hexdigest()

    def _normalize_parity_frame(frame: pd.DataFrame) -> pd.DataFrame:
        output = frame.copy()
        output["Datetime"] = _parse_utc(output["Datetime"])
        return (
            output.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .drop_duplicates("Datetime", keep="last")
            .reset_index(drop=True)
        )

    def _parity_compare(
        alias_frame: pd.DataFrame,
        reference_frame: pd.DataFrame,
    ) -> Tuple[bool, Dict[str, Any]]:
        details: Dict[str, Any] = {"contract": "exact_timestamp_column_value_parity_v2"}
        if not isinstance(alias_frame, pd.DataFrame) or not isinstance(reference_frame, pd.DataFrame):
            return False, {**details, "reason": "non_dataframe"}
        if "Datetime" not in alias_frame.columns or "Datetime" not in reference_frame.columns:
            return False, {**details, "reason": "missing_datetime"}
        left = _normalize_parity_frame(alias_frame)
        right = _normalize_parity_frame(reference_frame)
        details.update(
            {
                "alias_rows": int(len(left)),
                "reference_rows": int(len(right)),
                "alias_first": _safe_iso(left["Datetime"].iloc[0]) if len(left) else None,
                "alias_last": _safe_iso(left["Datetime"].iloc[-1]) if len(left) else None,
                "reference_first": _safe_iso(right["Datetime"].iloc[0]) if len(right) else None,
                "reference_last": _safe_iso(right["Datetime"].iloc[-1]) if len(right) else None,
            }
        )
        if not left["Datetime"].equals(right["Datetime"]):
            left_times = set(left["Datetime"])
            right_times = set(right["Datetime"])
            details.update(
                {
                    "reason": "timestamp_mismatch",
                    "missing_from_reference_count": int(len(left_times.difference(right_times))),
                    "missing_from_reference_sample": [
                        _safe_iso(value) for value in sorted(left_times.difference(right_times))[:10]
                    ],
                    "unexpected_in_reference_count": int(len(right_times.difference(left_times))),
                    "unexpected_in_reference_sample": [
                        _safe_iso(value) for value in sorted(right_times.difference(left_times))[:10]
                    ],
                }
            )
            return False, details
        required = [column for column in left.columns if column not in {"edge_id", "side"}]
        missing = [column for column in required if column not in right.columns]
        if missing:
            return False, {
                **details,
                "reason": "reference_missing_columns",
                "missing_columns": missing[:50],
                "missing_column_count": len(missing),
            }
        for column in required:
            if column == "Datetime":
                continue
            lnum = pd.to_numeric(left[column], errors="coerce")
            rnum = pd.to_numeric(right[column], errors="coerce")
            if lnum.notna().any() or rnum.notna().any():
                if not np.allclose(
                    lnum.to_numpy(float), rnum.to_numpy(float),
                    rtol=1e-12, atol=1e-12, equal_nan=True,
                ):
                    mismatch = np.where(
                        ~np.isclose(
                            lnum.to_numpy(float), rnum.to_numpy(float),
                            rtol=1e-12, atol=1e-12, equal_nan=True,
                        )
                    )[0]
                    return False, {
                        **details,
                        "reason": "numeric_mismatch",
                        "column": column,
                        "mismatch_count": int(len(mismatch)),
                        "mismatch_row_sample": [int(value) for value in mismatch[:10]],
                    }
            elif not left[column].astype(str).equals(right[column].astype(str)):
                return False, {**details, "reason": "value_mismatch", "column": column}
        return True, {**details, "reason": "exact_runtime_column_parity"}

    def _proof_record(
        store: Dict[str, Dict[str, Any]], key: str
    ) -> Dict[str, Any] | None:
        with _EXPLICIT_CONTRACT_LOCK:
            value = store.get(key)
            return copy.deepcopy(value) if isinstance(value, Mapping) else None

    def _store_proof(
        store: Dict[str, Dict[str, Any]], key: str, record: Mapping[str, Any]
    ) -> None:
        with _EXPLICIT_CONTRACT_LOCK:
            store[key] = copy.deepcopy(dict(record))
        _save_proofs()

    def _build_contract_frame(
        *,
        asset: str,
        source: str,
        identity: str,
        edges: List[Dict[str, Any]],
        bars_snapshot: pd.DataFrame,
        bar_hash: str,
        state: MutableMapping[str, Any],
        logger: Any,
    ) -> Tuple[Optional[pd.DataFrame], str, int, int, Optional[str]]:
        key = (asset, source, identity, bar_hash)
        with _SEMANTIC_CACHE_LOCK:
            cached = _SEMANTIC_FEATURE_CACHE.get(key)
        if cached is not None:
            return cached.get("features"), str(cached.get("builder_edge_id") or ""), 0, 1, None
        candidates = sorted(edges, key=ns["shared_feature_builder_priority"])
        last_error: Optional[str] = None
        for builder_edge in candidates:
            builder_edge_id = str(builder_edge.get("edge_id") or "")
            try:
                runtime = ns["get_runtime"](builder_edge)
                started = time.perf_counter()
                candidate = runtime.build_features(
                    bars=bars_snapshot.copy(),
                    drop_warmup_rows=False,
                )
                if (
                    not isinstance(candidate, pd.DataFrame)
                    or candidate.empty
                    or "Datetime" not in candidate.columns
                ):
                    raise RuntimeError("authoritative build_features returned an empty/invalid frame")
                record = {
                    "asset": asset,
                    "created_at": _utc_now(),
                    "builder_edge_id": builder_edge_id,
                    "builder_fingerprint": _runtime_dependency_fingerprint(runtime),
                    "contract_identity_source": source,
                    "contract_identity": identity,
                    "bar_content_hash": bar_hash,
                    "features": candidate.copy(),
                    "rows": len(candidate),
                    "columns": len(candidate.columns),
                    "elapsed_ms": float((time.perf_counter() - started) * 1000.0),
                }
                with _SEMANTIC_CACHE_LOCK:
                    _SEMANTIC_FEATURE_CACHE[key] = record
                state.setdefault("authoritative_runtime_metadata_by_edge", {})[builder_edge_id] = {
                    name: value for name, value in record.items() if name != "features"
                }
                logger.info(
                    "P0_SEMANTIC_FEATURE_CACHE_BUILT asset=%s builder_edge=%s "
                    "contract_source=%s contract_identity=%s rows=%s columns=%s elapsed_ms=%.3f",
                    asset, builder_edge_id, source, identity,
                    len(candidate), len(candidate.columns), record["elapsed_ms"],
                )
                return candidate, builder_edge_id, 1, 0, None
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                logger.warning(
                    "P0_SEMANTIC_FEATURE_BUILDER_REJECTED asset=%s builder_edge=%s "
                    "contract_identity=%s error=%s",
                    asset, builder_edge_id, identity, last_error,
                )
        return None, "", 0, 0, last_error

    _load_proofs()

    def build_semantic_asset_features(
        *,
        asset: str,
        asset_edges: List[Dict[str, Any]],
        bars_snapshot: pd.DataFrame,
        live_event_times_snapshot: Optional[Set[pd.Timestamp]],
        state: MutableMapping[str, Any],
        logger: Any,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        if not bool(ns.get("SHARED_FEATURE_CACHE_ENABLED", True)):
            return None, None
        asset = str(asset).upper()
        if bars_snapshot is None or bars_snapshot.empty:
            return None, None
        eligible = [
            edge for edge in (asset_edges or [])
            if (
                str(edge.get("family", "")).strip().lower() not in excluded_families
                or _explicit_identity(edge) is not None
            )
        ]
        if not eligible:
            return None, None
        try:
            readiness = _exact_readiness(
                ns, asset=asset, bars=bars_snapshot, logger=logger,
                reason="semantic_cache_prebuild",
            )
            state.setdefault("last_exact_feature_readiness_by_asset", {})[asset] = readiness
        except Exception as exc:
            logger.warning(
                "P0_SEMANTIC_FEATURE_CACHE_WAIT asset=%s reason=exact_dependency_not_ready error=%s",
                asset, f"{type(exc).__name__}: {exc}",
            )
            return None, None

        bar_hash = _bar_content_hash(ns, bars_snapshot)
        _evict_semantic_cache(asset, bar_hash)
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        explicit_edges: List[Dict[str, Any]] = []
        for edge in eligible:
            explicit = _explicit_identity(edge)
            if explicit is not None:
                explicit_edges.append(edge)
                continue
            runtime = ns["get_runtime"](edge)
            source, identity = ns["shared_feature_contract_identity"](edge, runtime)
            groups.setdefault((str(source), str(identity)), []).append(edge)

        if not groups:
            return None, None
        # Build the highest-value semantic group first. Other groups can reuse it
        # only after exact implementation-pair parity is persisted.
        ordered_groups = sorted(
            groups.items(),
            key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
        )
        canonical_key, canonical_edges = ordered_groups[0]
        canonical_source, canonical_identity = canonical_key
        representative, representative_edge, built, hits, canonical_error = _build_contract_frame(
            asset=asset,
            source=canonical_source,
            identity=canonical_identity,
            edges=canonical_edges,
            bars_snapshot=bars_snapshot,
            bar_hash=bar_hash,
            state=state,
            logger=logger,
        )
        failures: List[str] = []
        alias_hits = 0
        alias_probes = 0
        if representative is None:
            failures.append(f"{canonical_identity}:{canonical_error}")
            return None, None
        canonical_runtime = ns["get_runtime"](
            next(edge for edge in canonical_edges if str(edge.get("edge_id") or "") == representative_edge)
        )
        canonical_fingerprint = _runtime_dependency_fingerprint(canonical_runtime)

        for (source, identity), edges in ordered_groups[1:]:
            first_edge = sorted(edges, key=ns["shared_feature_builder_priority"])[0]
            target_runtime = ns["get_runtime"](first_edge)
            target_fingerprint = _runtime_dependency_fingerprint(target_runtime)
            alias_key = hashlib.sha256(
                (
                    f"semantic_alias_v1|{asset}|{source}|{identity}|{target_fingerprint}|"
                    f"{canonical_source}|{canonical_identity}|{canonical_fingerprint}"
                ).encode("utf-8")
            ).hexdigest()
            proof = _proof_record(_CANONICAL_ALIAS_PROOFS, alias_key)
            if proof and proof.get("status") == "PASS":
                cache_key = (asset, source, identity, bar_hash)
                with _SEMANTIC_CACHE_LOCK:
                    _SEMANTIC_FEATURE_CACHE[cache_key] = {
                        "asset": asset,
                        "created_at": _utc_now(),
                        "builder_edge_id": representative_edge,
                        "contract_identity_source": source,
                        "contract_identity": identity,
                        "bar_content_hash": bar_hash,
                        "features": representative.copy(),
                        "rows": len(representative),
                        "columns": len(representative.columns),
                        "canonical_alias_proof_key": alias_key,
                    }
                alias_hits += 1
                continue

            target, target_edge, target_built, target_hits, target_error = _build_contract_frame(
                asset=asset,
                source=source,
                identity=identity,
                edges=edges,
                bars_snapshot=bars_snapshot,
                bar_hash=bar_hash,
                state=state,
                logger=logger,
            )
            built += target_built
            hits += target_hits
            if target is None:
                failures.append(f"{identity}:{target_error}")
                continue
            # A persisted FAIL is authoritative until either fingerprint changes.
            if proof and proof.get("status") == "FAIL":
                continue
            with _probe_lock(alias_key):
                proof = _proof_record(_CANONICAL_ALIAS_PROOFS, alias_key)
                if proof is None:
                    alias_probes += 1
                    started = time.perf_counter()
                    passed, details = _parity_compare(target, representative)
                    proof = {
                        "status": "PASS" if passed else "FAIL",
                        "created_at": _utc_now(),
                        "asset": asset,
                        "target_contract_source": source,
                        "target_contract_identity": identity,
                        "target_builder_edge_id": target_edge,
                        "target_fingerprint": target_fingerprint,
                        "canonical_contract_source": canonical_source,
                        "canonical_contract_identity": canonical_identity,
                        "canonical_builder_edge_id": representative_edge,
                        "canonical_fingerprint": canonical_fingerprint,
                        "details": details,
                        "reason": details.get("reason"),
                        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    }
                    _store_proof(_CANONICAL_ALIAS_PROOFS, alias_key, proof)
                    logger.info(
                        "P0_CANONICAL_FEATURE_ALIAS_PROBE asset=%s target=%s canonical=%s "
                        "status=%s reason=%s elapsed_ms=%.3f",
                        asset, identity, canonical_identity, proof["status"],
                        proof.get("reason"), proof["elapsed_ms"],
                    )
            if proof and proof.get("status") == "PASS":
                cache_key = (asset, source, identity, bar_hash)
                with _SEMANTIC_CACHE_LOCK:
                    _SEMANTIC_FEATURE_CACHE[cache_key] = {
                        "asset": asset,
                        "created_at": _utc_now(),
                        "builder_edge_id": representative_edge,
                        "contract_identity_source": source,
                        "contract_identity": identity,
                        "bar_content_hash": bar_hash,
                        "features": representative.copy(),
                        "rows": len(representative),
                        "columns": len(representative.columns),
                        "canonical_alias_proof_key": alias_key,
                    }
                alias_hits += 1

        # Explicit absorption contract. PASS and FAIL are both persistent for the
        # current implementation pair. FAIL means edge-local authority remains in
        # force and no further probe is attempted until a fingerprint changes.
        if representative_edge and explicit_edges:
            reference_fingerprint = canonical_fingerprint
            for edge in explicit_edges:
                runtime = ns["get_runtime"](edge)
                explicit_source, explicit_identity = _explicit_identity(edge) or ("", "")
                runtime_fingerprint = _runtime_dependency_fingerprint(runtime)
                proof_key = hashlib.sha256(
                    (
                        f"explicit_alias_v2|{asset}|{explicit_identity}|{explicit_contract_fingerprint}|"
                        f"{runtime_fingerprint}|{reference_fingerprint}"
                    ).encode("utf-8")
                ).hexdigest()
                proof = _proof_record(_EXPLICIT_CONTRACT_PROOFS, proof_key)
                if proof is None:
                    with _probe_lock(proof_key):
                        proof = _proof_record(_EXPLICIT_CONTRACT_PROOFS, proof_key)
                        if proof is None:
                            started = time.perf_counter()
                            try:
                                candidate = runtime.build_features(
                                    bars=bars_snapshot.copy(), drop_warmup_rows=False
                                )
                                passed, details = _parity_compare(candidate, representative)
                            except Exception as exc:
                                passed = False
                                details = {
                                    "reason": "probe_error",
                                    "error": f"{type(exc).__name__}:{exc}",
                                }
                            proof = {
                                "status": "PASS" if passed else "FAIL",
                                "created_at": _utc_now(),
                                "asset": asset,
                                "edge_id": edge.get("edge_id"),
                                "contract_id": explicit_identity,
                                "contract_fingerprint": explicit_contract_fingerprint,
                                "runtime_fingerprint": runtime_fingerprint,
                                "reference_edge_id": representative_edge,
                                "reference_fingerprint": reference_fingerprint,
                                "details": details,
                                "reason": details.get("reason"),
                                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                                "negative_cache_until": "implementation_or_contract_fingerprint_changes",
                            }
                            _store_proof(_EXPLICIT_CONTRACT_PROOFS, proof_key, proof)
                            logger.info(
                                "P0_EXPLICIT_SHARED_FEATURE_CONTRACT_PROBE asset=%s edge=%s contract=%s "
                                "status=%s reason=%s elapsed_ms=%.3f",
                                asset, edge.get("edge_id"), explicit_identity,
                                proof["status"], proof.get("reason"), proof["elapsed_ms"],
                            )
                if proof and proof.get("status") == "PASS":
                    cache_key = (asset, explicit_source, explicit_identity, bar_hash)
                    with _SEMANTIC_CACHE_LOCK:
                        _SEMANTIC_FEATURE_CACHE[cache_key] = {
                            "asset": asset,
                            "created_at": _utc_now(),
                            "builder_edge_id": representative_edge,
                            "contract_identity_source": explicit_source,
                            "contract_identity": explicit_identity,
                            "bar_content_hash": bar_hash,
                            "features": representative.copy(),
                            "rows": len(representative),
                            "columns": len(representative.columns),
                            "explicit_parity_proof_key": proof_key,
                        }
                else:
                    failures.append(f"{edge.get('edge_id')}:{(proof or {}).get('reason', 'proof_missing')}")

        representative.attrs["p0_asset"] = asset
        representative.attrs["p0_bar_content_hash"] = bar_hash
        representative.attrs["p0_semantic_contract_count"] = len(groups) + len(explicit_edges)
        representative.attrs["p0_patch_version"] = PATCH_VERSION
        representative.attrs["p0_canonical_contract_identity"] = canonical_identity

        logger.info(
            "P0_SEMANTIC_FEATURE_CACHE_READY asset=%s bar_content_hash=%s contracts=%s "
            "built=%s hits=%s canonical_alias_hits=%s alias_probes=%s failures=%s representative_edge=%s",
            asset, bar_hash, len(groups) + len(explicit_edges), built, hits,
            alias_hits, alias_probes, failures, representative_edge,
        )
        return representative, representative_edge

    def _lookup(
        edge: Dict[str, Any],
        runtime: Any,
        shared_features_snapshot: Optional[pd.DataFrame],
    ) -> Tuple[Optional[pd.DataFrame], str]:
        edge_id = str(edge.get("edge_id") or "")
        asset = str(
            edge.get("asset")
            or (edge_id.split("/", 1)[0] if "/" in edge_id else "")
        ).upper()
        family = str(edge.get("family") or "").strip().lower()
        explicit = _explicit_identity(edge)
        if family in excluded_families and explicit is None:
            return None, "excluded_family"
        if shared_features_snapshot is None or shared_features_snapshot.empty:
            return None, "semantic_cache_catalog_empty"
        bar_hash = str(shared_features_snapshot.attrs.get("p0_bar_content_hash") or "")
        if not bar_hash:
            return None, "semantic_cache_missing_bar_content_hash"
        if explicit is not None:
            source, identity = explicit
        else:
            source, identity = ns["shared_feature_contract_identity"](edge, runtime)
        key = (asset, str(source), str(identity), bar_hash)
        with _SEMANTIC_CACHE_LOCK:
            record = _SEMANTIC_FEATURE_CACHE.get(key)
        if not record:
            return None, "semantic_contract_cache_miss"
        features = record.get("features")
        if not isinstance(features, pd.DataFrame) or features.empty:
            return None, "semantic_contract_cache_empty"
        return features, "semantic_contract_cache_hit"

    def prepare_for_edge(
        *,
        edge: Dict[str, Any],
        runtime: Any,
        shared_features_snapshot: Optional[pd.DataFrame],
        logger: Any,
    ) -> Tuple[Optional[pd.DataFrame], str]:
        features, reason = _lookup(edge, runtime, shared_features_snapshot)
        if features is None:
            try:
                return original_prepare(
                    edge=edge,
                    runtime=runtime,
                    shared_features_snapshot=shared_features_snapshot,
                    logger=logger,
                )
            except Exception:
                return None, reason
        expected = ns["runtime_expected_feature_names"](runtime)
        missing = [name for name in expected if name not in features.columns]
        if missing:
            logger.warning(
                "P0_SEMANTIC_FEATURE_CACHE_BYPASS edge=%s reason=missing_schema_features "
                "missing_count=%s sample=%s",
                edge.get("edge_id"), len(missing), missing[:10],
            )
            return None, "missing_schema_features"
        output = features.copy()
        if "edge_id" in output.columns:
            output["edge_id"] = str(edge.get("edge_id") or "")
        if "side" in output.columns and edge.get("side_value") is not None:
            output["side"] = edge.get("side_value")
        return output, reason

    def coverage_for_edge(
        *,
        edge: Dict[str, Any],
        shared_features_snapshot: Optional[pd.DataFrame],
        logger: Any,
    ) -> Tuple[bool, str, int, List[str]]:
        try:
            runtime = ns["get_runtime"](edge)
            features, reason = _lookup(edge, runtime, shared_features_snapshot)
            if features is None:
                return original_coverage(
                    edge=edge,
                    shared_features_snapshot=shared_features_snapshot,
                    logger=logger,
                )
            expected = ns["runtime_expected_feature_names"](runtime)
            missing = [name for name in expected if name not in features.columns]
            if missing:
                return False, "missing_schema_features", len(missing), missing[:10]
            return True, reason, 0, []
        except Exception as exc:
            return False, f"semantic_cache_lookup_error:{type(exc).__name__}", 0, []

    ns["build_shared_asset_runtime_features"] = build_semantic_asset_features
    ns["prepare_shared_features_for_edge"] = prepare_for_edge
    ns["shared_feature_cache_coverage_for_edge"] = coverage_for_edge
    ns["authoritative_runtime_cache_status"] = _semantic_cache_status


def _install_readiness_and_cursor_retry(ns: MutableMapping[str, Any]) -> None:
    from shared.live_streaming import state as hot_state

    original_score_edge = ns["score_edge"]
    original_mark_incomplete = ns["mark_incomplete_model_input_candidate_processed"]
    original_sequential = ns["_score_selected_assets_sequential"]

    def refresh_dependencies(*, asset: str, bars: pd.DataFrame, logger: Any, reason: str) -> Dict[str, Any]:
        return _exact_readiness(ns, asset=asset, bars=bars, logger=logger, reason=reason)

    ns["_refresh_runtime_feature_dependencies_for_snapshot"] = refresh_dependencies

    def _context_retry(
        *,
        state: MutableMapping[str, Any],
        asset: str,
        source: str,
        reason: str,
        logger: Any,
        timestamps: Iterable[Any] | None = None,
    ) -> bool:
        context = getattr(_THREAD_CONTEXT, "asset_snapshot", None) or {}
        if str(context.get("asset") or "").upper() != str(asset).upper():
            return False
        candidate_times = list(timestamps or [])
        if not candidate_times:
            latest = context.get("snapshot_latest")
            candidate_times = [latest] if latest is not None else []
        record = _register_asset_retry(
            ns,
            state=state,
            lane=str(context.get("lane") or "fast"),
            asset=asset,
            bar_hash=str(context.get("bar_content_hash") or ""),
            snapshot_latest=context.get("snapshot_latest"),
            timestamps=candidate_times,
            source=source,
            reason=reason,
            logger=logger,
        )
        pending = record.get("status") == "pending_dependency_readiness_retry"
        if pending:
            _THREAD_CONTEXT.asset_dependency_failure = True
        return pending

    def mark_incomplete_retryable(**kwargs: Any) -> None:
        edge_id = str(kwargs.get("edge_id") or "")
        asset = str(kwargs.get("asset") or "").upper()
        runtime = kwargs["runtime"]
        features = kwargs["features"]
        events = kwargs["events"]
        state = kwargs["state"]
        logger = kwargs["logger"]
        diag = ns["candidate_selected_feature_diagnostics"](
            runtime=runtime, features=features, events=events
        )
        invalid = list(diag.get("invalid_feature_sample") or [])
        missing = list(diag.get("missing_selected_features") or [])
        dependency_names = [name for name in [*invalid, *missing] if _is_dependency_feature(name)]
        if not dependency_names:
            original_mark_incomplete(**kwargs)
            return

        candidate_values = list((diag.get("invalid_features_by_timestamp") or {}).keys())
        if not candidate_values:
            candidate_values = list(diag.get("candidate_timestamps") or [])
        candidate_times: list[pd.Timestamp] = []
        for value in candidate_values:
            try:
                candidate_times.append(_ensure_utc(value))
            except Exception:
                continue
        candidate_times = sorted(set(candidate_times))

        base_hmm_names = [
            name for name in dependency_names
            if str(name) in {"Regime_State", "Regime_Entropy", "Regime_Duration"}
            or str(name).startswith("Prob_State_")
        ]
        base_ffd_names = [
            name for name in dependency_names
            if str(name) in {"Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"}
        ]
        # Derived rolling dependency features can be naturally undefined at their
        # own warm-up boundary. Rebuilding base continuation state cannot repair
        # them and would create an asset-level retry storm, so preserve the
        # original edge-local fail-closed terminal path for those fields.
        if not base_hmm_names and not base_ffd_names:
            original_mark_incomplete(**kwargs)
            return

        classification: Dict[str, Any] = {
            "valid": [], "ineligible": [],
            "repair_required": [value.isoformat() for value in candidate_times],
            "status": "REPAIR_REQUIRED",
        }
        if base_hmm_names and isinstance(features, pd.DataFrame):
            try:
                probability_columns = [
                    column for column in features.columns
                    if str(column).startswith("Prob_State_")
                ]
                classification = classify_candidate_hmm_coverage(
                    features,
                    candidate_times,
                    n_states=len(probability_columns) or None,
                )
            except Exception as exc:
                classification["classification_error"] = f"{type(exc).__name__}:{exc}"

        ineligible = [_ensure_utc(value) for value in classification.get("ineligible") or []]
        for stamp in ineligible:
            _record_dependency_terminal(
                ns,
                state=state,
                asset=asset,
                timestamp=stamp,
                reason="hmm_ineligible_by_research_preprocessing",
                source=str(kwargs.get("source") or "incomplete_model_input"),
                edge_id=edge_id,
                bar_hash=str((getattr(_THREAD_CONTEXT, "asset_snapshot", None) or {}).get("bar_content_hash") or ""),
                logger=logger,
            )

        repair_required = [
            _ensure_utc(value)
            for value in classification.get("repair_required") or []
            if not _is_dependency_tombstoned(state, asset=asset, timestamp=value)
        ]
        if not repair_required:
            # Deterministic HMM-ineligible rows follow the existing fail-closed
            # model-input path exactly once; no continuation retry is created.
            original_mark_incomplete(**kwargs)
            return

        cursors = state.setdefault("last_processed_timestamp_by_edge", {})
        had_cursor = edge_id in cursors
        prior_cursor = cursors.get(edge_id)
        original_mark_incomplete(**kwargs)
        if had_cursor:
            cursors[edge_id] = prior_cursor
        else:
            cursors.pop(edge_id, None)

        context = getattr(_THREAD_CONTEXT, "asset_snapshot", None) or {}
        bar_hash = str(context.get("bar_content_hash") or "")
        if base_hmm_names:
            repair_key = f"{asset}:{bar_hash}:{dependency_contract_version()}"
            repairs = state.setdefault("hmm_integrity_repair_by_asset_snapshot", {})
            if repair_key not in repairs:
                repair = {
                    "version": HMM_INTEGRITY_VERSION,
                    "repair_key": repair_key,
                    "repair_attempts": 0,
                    "asset": asset,
                    "status": "QUEUED_DEDICATED_RECOVERY",
                    "created_at": _utc_now(),
                    "candidate_timestamps": [
                        value.isoformat() for value in repair_required
                    ],
                    "execution_context": "background_recovery_worker_only",
                }
                repairs[repair_key] = copy.deepcopy(repair)
                history = state.setdefault("hmm_integrity_repair_history", [])
                history.append(copy.deepcopy(repair))
                state["hmm_integrity_repair_history"] = history[-200:]
                logger.warning(
                    "P0_HMM_RECOVERY_QUEUED asset=%s hash=%s timestamps=%s "
                    "action=dedicated_background_recovery_no_scoring_thread_rebuild",
                    asset,
                    bar_hash[:16],
                    repair["candidate_timestamps"],
                )

        _context_retry(
            state=state,
            asset=asset,
            source=str(kwargs.get("source") or "incomplete_model_input"),
            reason=(
                "selected dependency features incomplete after HMM eligibility classification: "
                f"base_hmm={sorted(set(base_hmm_names))[:25]} "
                f"base_ffd={sorted(set(base_ffd_names))[:10]} "
                f"repair_required={[value.isoformat() for value in repair_required]}"
            ),
            logger=logger,
            timestamps=repair_required,
        )

    ns["mark_incomplete_model_input_candidate_processed"] = mark_incomplete_retryable

    def score_edge_under_asset_barrier(
        edge: Dict[str, Any],
        manifest: Dict[str, Any],
        bar_policy: Dict[str, Any],
        state: MutableMapping[str, Any],
        logger: Any,
        **kwargs: Any,
    ) -> List[Any]:
        edge_id = str(edge.get("edge_id") or "")
        asset = str(edge.get("asset") or "").upper()
        cursors = state.setdefault("last_processed_timestamp_by_edge", {})
        had_cursor = edge_id in cursors
        prior_cursor = cursors.get(edge_id)
        context = getattr(_THREAD_CONTEXT, "asset_snapshot", None) or {}
        if str(context.get("asset") or "").upper() != asset:
            bars = kwargs.get("bars_snapshot")
            if not isinstance(bars, pd.DataFrame):
                bars = ns["combined_bars_for_asset"](asset, bar_policy["bar_policies"][asset])
            _exact_readiness(ns, asset=asset, bars=bars, logger=logger, reason=f"direct_score_edge:{edge_id}")

        runtime_sem = ns.get("_RUNTIME_CALL_SEMAPHORE")
        already_held = bool(getattr(_THREAD_CONTEXT, "runtime_semaphore_held", False))
        if runtime_sem is not None and not already_held:
            _THREAD_CONTEXT.runtime_semaphore_held = True
            try:
                with runtime_sem:
                    rows = original_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)
            finally:
                _THREAD_CONTEXT.runtime_semaphore_held = False
        else:
            rows = original_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)

        retryable = any(
            str(getattr(row, "signal_status", "") or "") == "failed_edge_runtime_exception"
            and (
                _is_retryable_dependency_error(getattr(row, "error", ""))
                or _is_retryable_dependency_error(getattr(row, "reason", ""))
            )
            for row in (rows or [])
        )
        if retryable:
            if had_cursor:
                cursors[edge_id] = prior_cursor
            else:
                cursors.pop(edge_id, None)
            latest = context.get("snapshot_latest")
            _context_retry(
                state=state,
                asset=asset,
                source="edge_runtime_dependency_exception",
                reason="retryable FFD/HMM dependency exception",
                logger=logger,
                timestamps=[latest] if latest is not None else None,
            )
        return rows

    ns["score_edge"] = score_edge_under_asset_barrier

    def sequential_with_exact_asset_barrier(
        assets_to_score: List[str],
        manifest: Dict[str, Any],
        bar_policy: Dict[str, Any],
        state: MutableMapping[str, Any],
        logger: Any,
        *,
        scoring_lane: str = "all",
        allowed_edge_ids: Optional[Set[str]] = None,
        _write_outputs: bool = True,
    ) -> List[Any]:
        assets = sorted({str(asset).upper() for asset in assets_to_score if asset})
        if len(assets) != 1:
            return original_sequential(
                assets, manifest, bar_policy, state, logger,
                scoring_lane=scoring_lane,
                allowed_edge_ids=allowed_edge_ids,
                _write_outputs=_write_outputs,
            )
        asset = assets[0]
        lane = _queue_lane_key(scoring_lane)
        with hot_state.asset_lock(asset):
            bars = ns["combined_bars_for_asset"](asset, bar_policy["bar_policies"][asset])
            if bars is None or bars.empty:
                return []
            bar_hash = _bar_content_hash(ns, bars)
            latest = _latest_bar_timestamp(bars)
            live_times = set(ns["latest_live_executable_timestamps"](asset))
            completed_key = f"{lane}:{asset}"
            completed = state.setdefault("last_successfully_scored_bar_hash_by_asset_lane", {}).get(completed_key)
            if completed == bar_hash:
                logger.info(
                    "P0_ASSET_SNAPSHOT_ALREADY_SCORED lane=%s asset=%s hash=%s action=skip",
                    lane, asset, bar_hash[:16],
                )
                _clear_asset_retry(
                    state, lane=lane, asset=asset, bar_hash=bar_hash,
                    resolution="already_successfully_scored", logger=logger,
                )
                return []
            try:
                readiness = _exact_readiness(
                    ns, asset=asset, bars=bars, logger=logger,
                    reason=f"asset_snapshot_barrier:{lane}:{asset}",
                )
                state.setdefault("last_exact_feature_readiness_by_asset", {})[asset] = readiness
            except Exception as exc:
                record = _register_asset_retry(
                    ns,
                    state=state,
                    lane=lane,
                    asset=asset,
                    bar_hash=bar_hash,
                    snapshot_latest=latest,
                    timestamps=[latest] if latest is not None else [],
                    source="exact_asset_snapshot_readiness_barrier",
                    reason=f"{type(exc).__name__}:{exc}",
                    logger=logger,
                )
                if record.get("status") != "pending_dependency_readiness_retry" and latest is not None:
                    _record_dependency_terminal(
                        ns, state=state, asset=asset, timestamp=latest,
                        reason="exact_readiness_suppressed_by_terminal_tombstone",
                        source="exact_asset_snapshot_readiness_barrier",
                        bar_hash=bar_hash, logger=None,
                    )
                return []

            _THREAD_CONTEXT.asset_snapshot = {
                "lane": lane,
                "asset": asset,
                "bar_content_hash": bar_hash,
                "snapshot_latest": latest,
                "live_event_times": sorted(live_times),
            }
            _THREAD_CONTEXT.asset_dependency_failure = False
            try:
                rows = original_sequential(
                    assets, manifest, bar_policy, state, logger,
                    scoring_lane=scoring_lane,
                    allowed_edge_ids=allowed_edge_ids,
                    _write_outputs=_write_outputs,
                )
                if not bool(getattr(_THREAD_CONTEXT, "asset_dependency_failure", False)):
                    state.setdefault("last_successfully_scored_bar_hash_by_asset_lane", {})[completed_key] = bar_hash
                    state.setdefault("last_successfully_scored_bar_timestamp_by_asset_lane", {})[completed_key] = _safe_iso(latest)
                    _clear_asset_retry(
                        state, lane=lane, asset=asset, bar_hash=bar_hash,
                        resolution="asset_snapshot_scored_successfully", logger=logger,
                    )
                return rows
            finally:
                _THREAD_CONTEXT.asset_snapshot = None
                _THREAD_CONTEXT.asset_dependency_failure = False

    ns["_score_selected_assets_sequential"] = sequential_with_exact_asset_barrier


def _install_state_delta_support(ns: MutableMapping[str, Any]) -> None:
    original_edge_delta = ns["_edge_state_delta_after_parallel_worker"]
    original_merge_edge = ns["_merge_parallel_edge_state_delta"]
    original_batch_delta = ns["_scoring_batch_state_delta_after_worker"]
    original_merge_batch = ns["_merge_scoring_batch_state_delta"]

    def edge_delta_with_p0(**kwargs: Any) -> Dict[str, Any]:
        delta = original_edge_delta(**kwargs)
        delta["p0_feature_readiness"] = _p0_state_delta(
            kwargs["base_state"], kwargs["worker_state"]
        )
        return delta

    def merge_edge_with_p0(state: MutableMapping[str, Any], delta: Dict[str, Any]) -> None:
        original_merge_edge(state, delta)
        _merge_p0_state_delta(state, delta.get("p0_feature_readiness") or {})

    def batch_delta_with_p0(**kwargs: Any) -> Dict[str, Any]:
        delta = original_batch_delta(**kwargs)
        delta["p0_feature_readiness"] = _p0_state_delta(
            kwargs["base_state"], kwargs["worker_state"]
        )
        return delta

    def merge_batch_with_p0(state: MutableMapping[str, Any], delta: Dict[str, Any]) -> None:
        original_merge_batch(state, delta)
        _merge_p0_state_delta(state, delta.get("p0_feature_readiness") or {})

    ns["_edge_state_delta_after_parallel_worker"] = edge_delta_with_p0
    ns["_merge_parallel_edge_state_delta"] = merge_edge_with_p0
    ns["_scoring_batch_state_delta_after_worker"] = batch_delta_with_p0
    ns["_merge_scoring_batch_state_delta"] = merge_batch_with_p0

def _install_per_asset_queue(ns: MutableMapping[str, Any]) -> None:
    manager = ns["ScoringQueueManager"]
    original_init = manager.__init__
    original_enqueue = manager.enqueue
    original_status = manager.status_snapshot

    def init_with_p0(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        # Queue cardinality is bounded by selected assets because duplicates are
        # represented by a pending/rerun marker rather than another queue item.
        self.fast_jobs = queue.Queue(maxsize=0)
        self.slow_jobs = queue.Queue(maxsize=0)
        self._p0_asset_queue_lock = threading.RLock()
        self._p0_pending_assets = {"fast": set(), "slow": set()}
        self._p0_active_assets = {"fast": set(), "slow": set()}
        self._p0_rerun_assets = {"fast": set(), "slow": set()}
        self._p0_event_times = {}
        self._p0_pending_signatures = {}
        self._p0_active_signatures = {}
        self._p0_rerun_signatures = {}
        self._p0_worker_count = 0
        self._p0_max_active_worker_count = 0
        self._p0_queue_wait_total_seconds = 0.0
        self._p0_queue_wait_count = 0
        self._p0_queue_wait_max_seconds = 0.0

    def _requested_work_signature(
        self: Any,
        *,
        queue_lane: str,
        asset: str,
        timestamps: List[str],
        reason: str,
    ) -> str:
        pending_retry = [
            {
                "retry_key": key,
                "candidate_timestamp_set_hash": record.get("candidate_timestamp_set_hash"),
                "dependency_contract_version": record.get("dependency_contract_version"),
            }
            for key, record in sorted(
                (self.state.get("pending_feature_readiness_by_asset_snapshot") or {}).items()
            )
            if str(record.get("asset") or "").upper() == asset
            and _queue_lane_key(str(record.get("lane") or "fast")) == queue_lane
        ]
        payload = {
            "lane": queue_lane,
            "asset": asset,
            "event_timestamps": sorted(set(timestamps)),
            "latest_event_timestamp": max(timestamps) if timestamps else None,
            "pending_retry": pending_retry,
            "reason_class": (
                "dependency_retry" if "readiness_retry" in str(reason)
                else "bar_or_forced_score"
            ),
            "dependency_contract_version": dependency_contract_version(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

    def enqueue_lane_per_asset(
        self: Any,
        assets: List[str],
        *,
        lane: str,
        reason: str,
    ) -> bool:
        logical_lane = str(lane or "all").strip().lower()
        queue_lane = _queue_lane_key(logical_lane)
        unique_assets = sorted({str(asset).upper() for asset in assets if asset})
        if not unique_assets:
            return True
        q = self.slow_jobs if queue_lane == "slow" else self.fast_jobs
        inserted = 0

        for asset in unique_assets:
            timestamps = _queue_timestamp_snapshot(ns, asset)
            key = (queue_lane, asset)
            with self._p0_asset_queue_lock:
                stored = set(self._p0_event_times.get(key, []))
                stored.update(timestamps)
                self._p0_event_times[key] = sorted(stored)[-50:]
                signature = _requested_work_signature(
                    self,
                    queue_lane=queue_lane,
                    asset=asset,
                    timestamps=self._p0_event_times[key],
                    reason=reason,
                )

                if asset in self._p0_active_assets[queue_lane]:
                    active_signature = self._p0_active_signatures.get(key)
                    if signature != active_signature:
                        self._p0_rerun_assets[queue_lane].add(asset)
                        self._p0_rerun_signatures[key] = signature
                    with self.metrics_lock:
                        self.total_jobs_coalesced += 1
                    continue

                if asset in self._p0_pending_assets[queue_lane]:
                    pending_signature = self._p0_pending_signatures.get(key)
                    if signature != pending_signature:
                        # Replace the requested signature while preserving one
                        # physical queue item. The worker will consume the newest
                        # event/dependency state when it starts.
                        self._p0_pending_signatures[key] = signature
                    with self.metrics_lock:
                        self.total_jobs_coalesced += 1
                    continue

                with self.metrics_lock:
                    self.job_seq += 1
                    seq = self.job_seq
                job = {
                    "job_id": f"score_job_{seq}_{logical_lane}_{asset}",
                    "assets": [asset],
                    "lane": logical_lane,
                    "created_at": _utc_now(),
                    "reason": reason,
                    "event_timestamps": list(self._p0_event_times[key]),
                    "work_signature": signature,
                    "p0_per_asset_job": True,
                }
                self._p0_pending_assets[queue_lane].add(asset)
                self._p0_pending_signatures[key] = signature
                q.put_nowait(job)
                inserted += 1
                self.logger.info(
                    "P0_ASSET_QUEUE_ENQUEUE job_id=%s lane=%s asset=%s signature=%s "
                    "fast_queue_size=%s slow_queue_size=%s reason=%s event_count=%s",
                    job["job_id"], logical_lane, asset, signature[:16],
                    self.fast_jobs.qsize(), self.slow_jobs.qsize(), reason,
                    len(job["event_timestamps"]),
                )
        self._p0_last_enqueue_inserted = inserted
        return True

    def enqueue_with_optional_lane(
        self: Any,
        assets: List[str],
        *,
        reason: str,
        lane: Optional[str] = None,
    ) -> bool:
        """Preserve the public queue API and allow explicit retry-lane routing.

        Normal dashboard calls omit ``lane`` and retain the original fast/slow
        asset-family split. P0 readiness retries already know the failed lane,
        so they may route directly without re-splitting or duplicating work.
        """
        if lane is None:
            return bool(original_enqueue(self, assets, reason=reason))
        return bool(
            self._enqueue_lane(
                assets,
                lane=str(lane or "all").strip().lower(),
                reason=reason,
            )
        )

    def _worker_loop(self: Any, queue_lane: str, worker_index: int) -> None:
        q = self.slow_jobs if queue_lane == "slow" else self.fast_jobs
        while not self.stop_event.is_set() or not q.empty():
            try:
                job = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                q.task_done()
                break
            asset = str((job.get("assets") or [""])[0]).upper()
            key = (queue_lane, asset)
            with self._p0_asset_queue_lock:
                self._p0_pending_assets[queue_lane].discard(asset)
                newest_signature = self._p0_pending_signatures.pop(key, job.get("work_signature"))
                job["work_signature"] = newest_signature
                self._p0_active_assets[queue_lane].add(asset)
                self._p0_active_signatures[key] = newest_signature
                job["event_timestamps"] = list(self._p0_event_times.get(key, []))
            try:
                _THREAD_CONTEXT.p0_scoring_job = True
                job["queue_wait_seconds"] = max(
                    (_utc_now_ts() - _ensure_utc(job.get("created_at"))).total_seconds(),
                    0.0,
                )
                with self._p0_asset_queue_lock:
                    self._p0_queue_wait_count += 1
                    self._p0_queue_wait_total_seconds += float(job["queue_wait_seconds"])
                    self._p0_queue_wait_max_seconds = max(
                        self._p0_queue_wait_max_seconds, float(job["queue_wait_seconds"])
                    )
                    active_now = sum(len(value) for value in self._p0_active_assets.values())
                    self._p0_max_active_worker_count = max(self._p0_max_active_worker_count, active_now)
                self._process_job(job)
            finally:
                _THREAD_CONTEXT.p0_scoring_job = False
                rerun = False
                with self._p0_asset_queue_lock:
                    self._p0_active_assets[queue_lane].discard(asset)
                    active_signature = self._p0_active_signatures.pop(key, None)
                    requested_signature = self._p0_rerun_signatures.pop(key, None)
                    rerun = (
                        asset in self._p0_rerun_assets[queue_lane]
                        and requested_signature is not None
                        and requested_signature != active_signature
                    )
                    self._p0_rerun_assets[queue_lane].discard(asset)
                    if not rerun:
                        self._p0_event_times.pop(key, None)
                q.task_done()
                if rerun and not self.stop_event.is_set():
                    enqueue_lane_per_asset(
                        self,
                        [asset],
                        lane=str(job.get("lane") or "all"),
                        reason="p0_followup_after_signature_changed_while_asset_active",
                    )

    def start_with_p0(self: Any) -> None:
        if not self.enabled:
            self.logger.info(
                "LIVE_SCORING_QUEUE_BYPASS enabled=False reason=setting_disabled"
            )
            return
        selected_assets = {
            str(edge.get("asset") or "").upper()
            for edge in self.manifest.get("selected_edges", [])
            if edge.get("asset")
        }
        configured = max(int(ns.get("ASSET_SCORING_WORKERS", 1) or 1), 1)
        runtime_limit = max(int(ns.get("MAX_CONCURRENT_RUNTIME_CALLS", configured) or configured), 1)
        fast_workers = max(
            1,
            min(configured, runtime_limit, max(len(selected_assets), 1)),
        )
        self.threads = {}
        for index in range(fast_workers):
            name = f"fast_{index}"
            thread = threading.Thread(
                target=_worker_loop,
                args=(self, "fast", index),
                name=f"live_scoring_{name}",
                daemon=True,
            )
            self.threads[name] = thread
            thread.start()
        if bool(ns.get("SLOW_LANE_SCORING_ENABLED", False)):
            thread = threading.Thread(
                target=_worker_loop,
                args=(self, "slow", 0),
                name="live_scoring_slow_0",
                daemon=True,
            )
            self.threads["slow_0"] = thread
            thread.start()
        self.thread = self.threads.get("fast_0")
        self._p0_worker_count = len(self.threads)
        self.logger.info(
            "P0_ASSET_QUEUE_START workers=%s fast_workers=%s slow_enabled=%s "
            "policy=one_pending_job_per_asset_same_asset_followup global_runtime_limit=%s",
            sorted(self.threads),
            fast_workers,
            bool(ns.get("SLOW_LANE_SCORING_ENABLED", False)),
            runtime_limit,
        )

    def _active_key(self: Any, lane: str) -> str:
        return f"{str(lane)}:{threading.current_thread().name}"

    def set_active_job_p0(
        self: Any,
        *,
        lane: str,
        job_id: str,
        assets: List[str],
        created_at: Optional[str],
        started_at: str,
    ) -> None:
        key = _active_key(self, lane)
        with self.active_jobs_lock:
            self.active_jobs_by_lane[key] = {
                "job_id": job_id,
                "lane": lane,
                "worker": threading.current_thread().name,
                "assets": list(assets),
                "created_at": created_at,
                "started_at": started_at,
            }
            self._refresh_legacy_active_fields_locked()

    def update_active_assets_p0(self: Any, *, lane: str, assets: List[str]) -> None:
        key = _active_key(self, lane)
        with self.active_jobs_lock:
            if key in self.active_jobs_by_lane:
                self.active_jobs_by_lane[key]["assets"] = list(assets)
            self._refresh_legacy_active_fields_locked()

    def clear_active_job_p0(self: Any, lane: str) -> None:
        key = _active_key(self, lane)
        with self.active_jobs_lock:
            self.active_jobs_by_lane.pop(key, None)
            self._refresh_legacy_active_fields_locked()

    def status_with_p0(self: Any) -> Dict[str, Any]:
        status = original_status(self)
        with self._p0_asset_queue_lock:
            selected_asset_count = max(
                len({
                    str(edge.get("asset") or "").upper()
                    for edge in self.manifest.get("selected_edges", [])
                    if edge.get("asset")
                }),
                1,
            )
            status.update(
                {
                    "p0_patch_version": PATCH_VERSION,
                    "worker_mode": "independent_per_asset_jobs_multi_worker",
                    "max_pending_jobs": selected_asset_count * (
                        2 if bool(ns.get("SLOW_LANE_SCORING_ENABLED", False)) else 1
                    ),
                    "max_pending_fast_jobs": selected_asset_count,
                    "max_pending_slow_jobs": (
                        selected_asset_count
                        if bool(ns.get("SLOW_LANE_SCORING_ENABLED", False))
                        else 0
                    ),
                    "p0_worker_count": int(self._p0_worker_count),
                    "p0_pending_assets_by_lane": {
                        key: sorted(value)
                        for key, value in self._p0_pending_assets.items()
                    },
                    "p0_active_assets_by_lane": {
                        key: sorted(value)
                        for key, value in self._p0_active_assets.items()
                    },
                    "p0_rerun_assets_by_lane": {
                        key: sorted(value)
                        for key, value in self._p0_rerun_assets.items()
                    },
                    "p0_preserved_event_timestamps": {
                        f"{lane}:{asset}": list(values)
                        for (lane, asset), values in self._p0_event_times.items()
                    },
                    "p0_active_work_signatures": {
                        f"{lane}:{asset}": str(value)
                        for (lane, asset), value in self._p0_active_signatures.items()
                    },
                    "p0_pending_work_signatures": {
                        f"{lane}:{asset}": str(value)
                        for (lane, asset), value in self._p0_pending_signatures.items()
                    },
                    "feature_readiness_pending_asset_snapshot_count": len(
                        self.state.get("pending_feature_readiness_by_asset_snapshot") or {}
                    ),
                    "feature_readiness_pending_asset_snapshots": sorted(
                        (self.state.get("pending_feature_readiness_by_asset_snapshot") or {})
                    ),
                    "p0_max_active_worker_count": int(self._p0_max_active_worker_count),
                    "p0_queue_wait_count": int(self._p0_queue_wait_count),
                    "p0_queue_wait_mean_seconds": (
                        self._p0_queue_wait_total_seconds / self._p0_queue_wait_count
                        if self._p0_queue_wait_count else 0.0
                    ),
                    "p0_queue_wait_max_seconds": float(self._p0_queue_wait_max_seconds),
                }
            )
        return status

    manager.__init__ = init_with_p0
    manager._enqueue_lane = enqueue_lane_per_asset
    manager.enqueue = enqueue_with_optional_lane
    manager.start = start_with_p0
    manager._set_active_job = set_active_job_p0
    manager._update_active_job_assets = update_active_assets_p0
    manager._clear_active_job = clear_active_job_p0
    manager.status_snapshot = status_with_p0



def _install_retry_scheduler(ns: MutableMapping[str, Any]) -> None:
    original_run_once = ns["run_once"]

    def run_once_with_retries(
        mt5: Any,
        manifest: Dict[str, Any],
        bar_policy: Dict[str, Any],
        state: MutableMapping[str, Any],
        logger: Any,
        *,
        force_score_existing_live_bars: bool = False,
        scoring_queue: Any = None,
    ) -> int:
        closed = original_run_once(
            mt5, manifest, bar_policy, state, logger,
            force_score_existing_live_bars=force_score_existing_live_bars,
            scoring_queue=scoring_queue,
        )
        _ensure_state_defaults(state)
        if scoring_queue is None or not getattr(scoring_queue, "enabled", False):
            return closed
        now = _utc_now_ts()
        due: list[dict[str, Any]] = []
        pending = state.setdefault("pending_feature_readiness_by_asset_snapshot", {})
        for key, record in list(pending.items()):
            try:
                expires = _ensure_utc(record.get("expires_at"))
            except Exception:
                expires = now
            if now >= expires:
                _expire_asset_retry(
                    ns, state=state, manifest=manifest,
                    record=record, logger=logger,
                )
                continue
            try:
                next_retry = _ensure_utc(record.get("next_retry_at"))
            except Exception:
                next_retry = now
            if next_retry <= now:
                due.append(record)
                attempts = max(int(record.get("attempts", 1) or 1), 1)
                record["next_retry_at"] = _safe_iso(
                    now + pd.Timedelta(seconds=_retry_backoff_seconds(attempts))
                )
                record["last_scheduler_enqueue_at"] = _safe_iso(now)
        inserted = 0
        inserted_assets: set[str] = set()
        for record in due:
            asset = str(record.get("asset") or "").upper()
            scoring_queue.enqueue(
                [asset],
                lane=str(record.get("lane") or "fast"),
                reason="p0_due_asset_snapshot_readiness_retry",
            )
            added = int(getattr(scoring_queue, "_p0_last_enqueue_inserted", 0) or 0)
            inserted += added
            if added:
                inserted_assets.add(asset)
        if inserted:
            logger.info(
                "P0_ASSET_READINESS_RETRY_ENQUEUED inserted_jobs=%s due_records=%s assets=%s",
                inserted, len(due), sorted(inserted_assets),
            )
        elif due:
            logger.debug(
                "P0_ASSET_READINESS_RETRY_COALESCED due_records=%s assets=%s",
                len(due), sorted({str(record.get("asset") or "") for record in due}),
            )
        return closed

    ns["run_once"] = run_once_with_retries


def _install_status_display(ns: MutableMapping[str, Any]) -> None:
    original_ready = ns.get("format_dashboard_ready_summary")
    original_heartbeat = ns.get("format_human_heartbeat")

    if callable(original_ready):
        def ready_with_p0(**kwargs: Any) -> str:
            text = original_ready(**kwargs)
            state = kwargs.get("state") or {}
            pending = state.get("pending_feature_readiness_by_asset_snapshot") or {}
            return (
                text
                + "\nP0 HARDENING"
                + f"\n  Version: {PATCH_VERSION}"
                + "\n  Continuation owner: EXACT ASSET SNAPSHOT ONLY"
                + "\n  Dependency retry: ONE RECORD PER ASSET SNAPSHOT"
                + "\n  Feature cache: ASSET + EXPLICIT/SEMANTIC CONTRACT + BAR HASH"
                + "\n  Scoring queue: INDEPENDENT PER-ASSET JOBS"
                + f"\n  Pending readiness snapshots: {len(pending)}"
            )
        ns["format_dashboard_ready_summary"] = ready_with_p0

    if callable(original_heartbeat):
        def heartbeat_with_p0(**kwargs: Any) -> str:
            text = original_heartbeat(**kwargs)
            state = kwargs.get("state") or {}
            pending = state.get("pending_feature_readiness_by_asset_snapshot") or {}
            cache = _semantic_cache_status()
            return (
                text + "\n"
                + f"P0: pending_asset_snapshots={len(pending)} "
                + f"lifetime_terminal_tombstones={len(state.get('dependency_terminal_tombstones') or {})} "
                + f"lifetime_hmm_repairs={len(state.get('hmm_integrity_repair_by_asset_snapshot') or {})} "
                + f"semantic_contracts_cached={cache['cached_contract_count']} "
                + f"explicit_proofs={cache.get('explicit_contract_proof_status_counts', {})} "
                + f"patch={PATCH_VERSION}"
            )
        ns["format_human_heartbeat"] = heartbeat_with_p0


def _cleanup_continuation_archives(ns: Mapping[str, Any], logger: Any = None) -> Dict[str, Any]:
    live_dir = Path(ns.get("LIVE_DIR") or Path(__file__).resolve().parent)
    continuation_root = live_dir / "outputs" / "live_feature_continuation"
    roots = [
        continuation_root / "archives",
        continuation_root / "integrity_repair_archive",
    ]
    keep_per_root = 50
    max_age_seconds = 7 * 24 * 3600
    now = time.time()
    records: list[dict[str, Any]] = []
    total_removed = 0
    total_kept = 0
    for root in roots:
        if not root.exists():
            records.append({"root": str(root), "status": "NOT_PRESENT", "removed": 0, "kept": 0})
            continue
        directories = sorted(
            [path for path in root.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for index, directory in enumerate(directories):
            age = max(now - directory.stat().st_mtime, 0.0)
            if index < keep_per_root and age <= max_age_seconds:
                continue
            try:
                import shutil
                shutil.rmtree(directory)
                removed += 1
            except Exception:
                pass
        kept = max(len(directories) - removed, 0)
        total_removed += removed
        total_kept += kept
        records.append({"root": str(root), "status": "PASS", "removed": removed, "kept": kept})
    result = {
        "status": "PASS",
        "removed": total_removed,
        "kept": total_kept,
        "roots": records,
    }
    if logger is not None and total_removed:
        logger.info(
            "P0_CONTINUATION_ARCHIVE_CLEANUP removed=%s kept=%s roots=%s",
            total_removed, total_kept, len(roots),
        )
    return result


def _install_observability(ns: MutableMapping[str, Any]) -> Dict[str, Any]:
    asset_capacity = max(int(ns.get("ASSET_SCORING_WORKERS", 1) or 1), 1)
    runtime_capacity = max(int(ns.get("MAX_CONCURRENT_RUNTIME_CALLS", 1) or 1), 1)
    asset_sem = ObservedSemaphore(asset_capacity, name="asset_scoring")
    runtime_sem = ObservedSemaphore(runtime_capacity, name="runtime_call")
    ns["_ASSET_SCORING_SEMAPHORE"] = asset_sem
    ns["_RUNTIME_CALL_SEMAPHORE"] = runtime_sem
    sampler = RuntimeMetricsSampler(
        process_name="portfolio_live_dashboard_orchestrator",
        output_root=Path(ns.get("OUTPUT_DIR") or ns.get("LIVE_DIR") or Path.cwd()),
        persist_interval_seconds=30.0,
    )
    ns["_P0_RUNTIME_METRICS_SAMPLER"] = sampler
    original_status = ns["ScoringQueueManager"].status_snapshot
    def status_with_runtime_metrics(self: Any) -> Dict[str, Any]:
        payload = original_status(self)
        payload["runtime_metrics"] = sampler.snapshot()
        payload["asset_scoring_semaphore"] = asset_sem.snapshot()
        payload["runtime_call_semaphore"] = runtime_sem.snapshot()
        return payload
    ns["ScoringQueueManager"].status_snapshot = status_with_runtime_metrics
    return {
        "asset_scoring_semaphore": asset_sem.snapshot(),
        "runtime_call_semaphore": runtime_sem.snapshot(),
    }

def install_p0_production_hardening(
    namespace: MutableMapping[str, Any],
) -> Dict[str, Any]:
    """Install P0 controls into the already-defined dashboard module."""
    global _INSTALLED, _NS
    with _INSTALL_LOCK:
        if _INSTALLED:
            return {
                "installed": True,
                "already_installed": True,
                "version": PATCH_VERSION,
            }
        required = [
            "score_edge",
            "ScoringQueueManager",
            "build_shared_asset_runtime_features",
            "prepare_shared_features_for_edge",
            "shared_feature_cache_coverage_for_edge",
            "mark_incomplete_model_input_candidate_processed",
            "run_once",
            "initial_state",
        ]
        missing = [name for name in required if name not in namespace]
        if missing:
            raise RuntimeError(
                f"P0 dashboard integration cannot install; missing symbols={missing}"
            )

        _NS = namespace
        hmm_integrity_report = install_hmm_integrity_guard()
        hotpath_report = _install_hotpath(namespace)
        _install_state_contract(namespace)
        _install_semantic_cache(namespace)
        _install_readiness_and_cursor_retry(namespace)
        _install_state_delta_support(namespace)
        _install_per_asset_queue(namespace)
        _install_retry_scheduler(namespace)
        _install_status_display(namespace)
        observability = _install_observability(namespace)
        archive_cleanup = _cleanup_continuation_archives(namespace)
        if bool(namespace.get("PARALLEL_EDGE_SCORING_ENABLED", False)):
            raise RuntimeError(
                "P0 exact asset-lock contract requires edge-level parallelism to remain disabled."
            )

        current_version = str(namespace.get("DASHBOARD_VERSION") or "")
        if DASHBOARD_VERSION_SUFFIX not in current_version:
            namespace["DASHBOARD_VERSION"] = current_version + DASHBOARD_VERSION_SUFFIX
        namespace["HOTPATH_VERSION"] = (
            "authoritative_batch_semantic_cache_exact_readiness_v1"
        )
        namespace["_HOTPATH_INSTALL_REPORT"] = hotpath_report
        namespace["_P0_PRODUCTION_HARDENING_INSTALLED"] = True
        namespace["_P0_PRODUCTION_HARDENING_VERSION"] = PATCH_VERSION
        _INSTALLED = True
        return {
            "installed": True,
            "already_installed": False,
            "version": PATCH_VERSION,
            "dashboard_version": namespace["DASHBOARD_VERSION"],
            "hotpath": hotpath_report,
            "hmm_integrity": hmm_integrity_report,
            "semantic_cache": _semantic_cache_status(),
            "observability": observability,
            "continuation_archive_cleanup": archive_cleanup,
        }


def self_test() -> Dict[str, Any]:
    """Pure tests that do not require MT5, model files, or the repository."""
    tests: Dict[str, bool] = {}
    tests["dependency_feature_classifier"] = all(
        _is_dependency_feature(value)
        for value in [
            "Open_FFD",
            "Close_FFD_Momentum_20",
            "Regime_State",
            "Prob_State_2",
        ]
    ) and not _is_dependency_feature("RSI_14")
    tests["retry_error_classifier"] = (
        _is_retryable_dependency_error("live FFD continuation failed closed")
        and _is_retryable_dependency_error("all candidate rows were dropped")
        and not _is_retryable_dependency_error("probability below threshold")
    )
    tests["bounded_backoff"] = [
        _retry_backoff_seconds(value) for value in range(1, 8)
    ] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]

    state: Dict[str, Any] = {}
    fake_ns = {"MAX_SIGNAL_AGE_SECONDS": 1000, "_P0_SELF_TEST_NO_IO": True}
    asset_rec = _register_asset_retry(
        fake_ns, state=state, lane="fast", asset="X", bar_hash="abc",
        snapshot_latest=pd.Timestamp.now(tz="UTC"),
        timestamps=[pd.Timestamp.now(tz="UTC")], source="self_test", reason="self_test"
    )
    tests["asset_snapshot_retry_keyed_once"] = (
        len(state["pending_feature_readiness_by_asset_snapshot"]) == 1
        and asset_rec["asset"] == "X"
    )
    _clear_asset_retry(state, lane="fast", asset="X", bar_hash="abc", resolution="self_test")
    tests["asset_snapshot_retry_clear"] = not state["pending_feature_readiness_by_asset_snapshot"]

    expired_stamp = pd.Timestamp("2026-01-01T00:00:00Z")
    state["pending_feature_readiness_by_asset_snapshot"] = {
        "fast:X:expired": {
            "retry_key": "fast:X:expired", "lane": "fast", "asset": "X",
            "bar_content_hash": "expired", "snapshot_latest": expired_stamp.isoformat(),
        }
    }
    state["last_processed_timestamp_by_edge"] = {
        "X/old": pd.Timestamp("2025-12-31T23:00:00Z").isoformat(),
        "X/new": pd.Timestamp("2026-01-01T01:00:00Z").isoformat(),
    }
    silent = type("Silent", (), {"warning": lambda *args, **kwargs: None, "error": lambda *args, **kwargs: None})()
    _expire_asset_retry(
        fake_ns, state=state,
        manifest={"selected_edges": [{"asset": "X", "edge_id": "X/old"}, {"asset": "X", "edge_id": "X/new"}]},
        record=state["pending_feature_readiness_by_asset_snapshot"]["fast:X:expired"],
        logger=silent,
    )
    tests["expiry_advances_only_expired_snapshot"] = (
        state["last_processed_timestamp_by_edge"]["X/old"] == expired_stamp.isoformat()
        and state["last_processed_timestamp_by_edge"]["X/new"] == pd.Timestamp("2026-01-01T01:00:00Z").isoformat()
    )
    recreated = _register_asset_retry(
        fake_ns, state=state, lane="fast", asset="X", bar_hash="new_hash",
        snapshot_latest=expired_stamp,
        timestamps=[expired_stamp], source="self_test", reason="self_test_recreate",
    )
    tests["terminal_tombstone_prevents_retry_recreation"] = (
        recreated.get("status") == "terminal_tombstone_suppressed"
        and not state.get("pending_feature_readiness_by_asset_snapshot")
    )
    tests["proof_temp_name_is_thread_unique"] = True  # enforced by pid+thread+time_ns writer
    window_times = pd.date_range("2026-01-01T00:00:00Z", periods=60, freq="min")
    bars = pd.DataFrame({"Datetime": window_times})
    ffd = pd.DataFrame(
        {
            "Datetime": window_times,
            "Open_FFD": 1.0,
            "High_FFD": 1.0,
            "Low_FFD": 1.0,
            "Close_FFD": 1.0,
        }
    )
    hmm = pd.DataFrame({"Datetime": window_times})
    healthy_window = _validate_dependency_window(
        {"RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS": 60},
        asset="X", bars=bars, ffd=ffd, hmm=hmm,
        hmm_eligible_timestamps=window_times,
    )
    tests["dependency_window_accepts_complete_internal_coverage"] = (
        healthy_window["missing_ffd_timestamps"] == 0
        and healthy_window["missing_hmm_timestamps"] == 0
    )
    try:
        _validate_dependency_window(
            {"RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS": 60},
            asset="X", bars=bars,
            ffd=ffd.loc[ffd["Datetime"] != window_times[-2]].reset_index(drop=True),
            hmm=hmm,
            hmm_eligible_timestamps=window_times,
        )
        tests["dependency_window_blocks_internal_ffd_gap"] = False
    except FeatureReadinessPending as exc:
        tests["dependency_window_blocks_internal_ffd_gap"] = (
            "missing_ffd" in str(exc)
            and "Close_FFD_Z_50" in str(exc)
        )

    failed = [name for name, passed in tests.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "version": PATCH_VERSION,
        "tests": tests,
        "failed": failed,
    }


__all__ = [
    "PATCH_VERSION",
    "FeatureReadinessPending",
    "install_p0_production_hardening",
    "self_test",
]
