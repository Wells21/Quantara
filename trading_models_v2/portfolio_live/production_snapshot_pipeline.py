from __future__ import annotations

"""Asset-scoped dependency coordination for the live dashboard.

This module installs after :mod:`production_readiness_p0` and replaces its
retry/rebuild control plane.  It deliberately leaves edge feature math, event
generation, model scoring, calibration, AFML, thresholds, and execution plans
unchanged.

The production lifecycle is one-way:

    closed executable bar -> exact FFD/HMM continuation -> READY snapshot
    -> fast edge consumers -> edge-local consumers

Only the coordinator advances dependencies.  Edge consumers are read-only.
Full-history repair runs on a separate recovery worker while the asset is
quarantined; it can never run from an edge-scoring call.
"""

import copy
from collections import defaultdict, deque
import hashlib
import inspect
import json
import queue
import threading
import time
from typing import Any, Iterable, Mapping, MutableMapping

import pandas as pd

import production_readiness_p0 as p0
from shared.live_streaming import state as hot_state
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)


PIPELINE_VERSION = "2026-08-15_asset_snapshot_coordinator_v8_phase_b_dependency_cache_hydration"
DASHBOARD_VERSION_SUFFIX = "+asset_snapshot_coordinator_v8_phase_b_dependency_cache_hydration"

_INSTALL_LOCK = threading.RLock()
_INSTALLED = False
_THREAD_CONTEXT = threading.local()
_LATENCY_LOCK = threading.RLock()
_LATENCIES: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=512))


def _utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _utc_now() -> str:
    return _utc_now_ts().isoformat()


def _ensure_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return _ensure_utc(value).isoformat()
    except Exception:
        return str(value)


class RecoveryShutdownRequested(RuntimeError):
    """Control-flow cancellation; it is not an operational recovery failure."""


def _startup_failure_summary(record: Mapping[str, Any]) -> str:
    values = (
        record.get("errors")
        or (record.get("ffd") or {}).get("failures")
        or (record.get("hmm") or {}).get("failures")
        or [record.get("status") or "startup_continuation_unhealthy"]
    )
    return " | ".join(str(value) for value in values)[:2000]


def _reconcile_startup_continuation_health(
    state: MutableMapping[str, Any],
    selected_assets: Iterable[str],
) -> list[str]:
    """Make startup health the recovery queue authority, not stale state alone."""
    selected = {str(asset).upper() for asset in selected_assets if str(asset)}
    health = state.get("live_feature_continuation_health_by_asset") or {}
    quarantines = state.setdefault("asset_snapshot_quarantine_by_asset", {})
    healthy_statuses = {"PASS", "REPAIRED_PASS", "NOT_REQUIRED"}
    for asset in sorted(selected):
        record = health.get(asset)
        if not isinstance(record, Mapping):
            continue
        if str(record.get("status") or "") in healthy_statuses:
            quarantines.pop(asset, None)
            continue
        prior = quarantines.get(asset) or {}
        quarantines[asset] = {
            **prior,
            "status": "QUARANTINED_STARTUP_CONTINUATION_FAILED",
            "asset": asset,
            "created_at": _utc_now(),
            "snapshot_latest": record.get("live_bar_latest"),
            "reason": _startup_failure_summary(record),
            "reason_class": "FEATURE_RECOVERY_FAILED",
            "source": "startup_continuation_health",
            "failure_count": max(int(prior.get("failure_count", 0) or 0), 1),
            "next_recovery_at": _utc_now(),
        }
    return sorted(
        asset for asset in quarantines if str(asset).upper() in selected
    )


def _persisted_live_bar_latest(asset: str) -> pd.Timestamp | None:
    """Return the latest compacted live bar, excluding in-memory journals."""
    try:
        frame = hot_state.normalized_live_bars_disk(str(asset).upper())
    except Exception:
        return None
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        return None
    values = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce").dropna()
    return _ensure_utc(values.max()) if not values.empty else None


def _wait_for_persisted_snapshot(
    asset: str,
    target: Any,
    *,
    stop_event: Any,
    timeout_seconds: float,
    poll_seconds: float = 0.25,
    _latest_reader: Any = None,
    _monotonic: Any = None,
    _wait: Any = None,
) -> dict[str, Any]:
    """Wait until the journaled recovery target is visible to file-based repair.

    Snapshot scoring consumes a synchronously journaled in-memory bar, whereas
    the legacy full-rebuild guard reads the compacted live-bar CSV. Recovery
    must not run that guard against the older CSV snapshot.
    """
    target_stamp = _ensure_utc(target)
    latest_reader = _latest_reader or _persisted_live_bar_latest
    monotonic = _monotonic or time.monotonic
    wait = _wait or stop_event.wait
    started = monotonic()
    deadline = started + max(float(timeout_seconds), 0.0)
    polls = 0
    latest: pd.Timestamp | None = None
    while True:
        polls += 1
        latest = latest_reader(str(asset).upper())
        if latest is not None:
            latest = _ensure_utc(latest)
            if latest >= target_stamp:
                return {
                    "target": target_stamp.isoformat(),
                    "persisted_latest": latest.isoformat(),
                    "polls": polls,
                    "waited_seconds": max(monotonic() - started, 0.0),
                }
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"{str(asset).upper()}: journaled snapshot was not compacted before recovery "
                f"target={target_stamp.isoformat()} persisted_latest={_safe_iso(latest)} "
                f"timeout_seconds={float(timeout_seconds):.3f}"
            )
        if wait(min(max(float(poll_seconds), 0.01), remaining)):
            raise RecoveryShutdownRequested(
                f"{str(asset).upper()}: shutdown requested while waiting for journal compaction"
            )


def _record_latency(stage: str, elapsed_ms: float) -> None:
    value = max(float(elapsed_ms), 0.0)
    with _LATENCY_LOCK:
        _LATENCIES[str(stage)].append(value)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * quantile)), 0), len(ordered) - 1)
    return float(ordered[index])


def latency_snapshot() -> dict[str, Any]:
    with _LATENCY_LOCK:
        copied = {stage: list(values) for stage, values in _LATENCIES.items()}
    return {
        stage: {
            "samples": len(values),
            "latest_ms": float(values[-1]) if values else 0.0,
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
            "max_ms": max(values) if values else 0.0,
        }
        for stage, values in sorted(copied.items())
    }


def _closure_value(function: Any, name: str) -> Any:
    try:
        values = inspect.getclosurevars(function).nonlocals
    except Exception as exc:  # pragma: no cover - install-time contract failure
        raise RuntimeError(f"Cannot inspect P0 wrapper {function!r}: {exc}") from exc
    if name not in values:
        raise RuntimeError(
            f"P0 wrapper {getattr(function, '__name__', function)!r} is missing closure {name!r}"
        )
    return values[name]


def _state_defaults() -> dict[str, Any]:
    return {
        "snapshot_pipeline_version": PIPELINE_VERSION,
        "last_ready_asset_snapshot_by_asset": {},
        "last_terminal_asset_snapshot_by_asset": {},
        "asset_snapshot_quarantine_by_asset": {},
        "asset_snapshot_recovery_history": [],
        "asset_snapshot_dependency_failure_history": [],
        "snapshot_latency_budget_breaches": [],
        "retired_feature_readiness_retries_v4": [],
    }


def _ensure_state(state: MutableMapping[str, Any]) -> None:
    for key, value in _state_defaults().items():
        state.setdefault(key, copy.deepcopy(value))
    state["snapshot_pipeline_version"] = PIPELINE_VERSION
    pending = state.get("pending_feature_readiness_by_asset_snapshot") or {}
    if pending:
        retired = state.setdefault("retired_feature_readiness_retries_v4", [])
        retired.append(
            {
                "retired_at": _utc_now(),
                "reason": "replaced_by_single_flight_snapshot_coordinator",
                "records": copy.deepcopy(pending),
            }
        )
        state["retired_feature_readiness_retries_v4"] = retired[-20:]
        state["pending_feature_readiness_by_asset_snapshot"] = {}


def _is_dependency_error(value: Any) -> bool:
    text = str(value or "").lower()
    return p0._is_retryable_dependency_error(text) or any(
        token in text
        for token in (
            "checkpoint_schema",
            "state_or_output_missing",
            "cold_cache_not_initialized",
            "parity not proven",
            "coverage mismatch",
            "dependency target",
            "exact ffd/hmm",
            "phase_c4_feature_contract",
            "phase_c4_feature_state",
        )
    )


def _feature_reason_class(value: Any, *, recovery: bool = False) -> str:
    text = str(value or "").lower()
    if "phase_c4_feature_contract" in text or "phase_c4_feature_state" in text:
        return "PHASE_C4_FEATURE_CONTRACT"
    if any(
        token in text
        for token in (
            "generation mismatch",
            "generation_id",
            "output/checkpoint generation mismatch",
            "feature_generation_mismatch",
        )
    ):
        return "FEATURE_GENERATION_MISMATCH"
    return "FEATURE_RECOVERY_FAILED" if recovery else "FEATURE_DEPENDENCY_NOT_READY"


def _snapshot_signature(asset: str, event_times: Iterable[Any]) -> str:
    normalized = {
        stamp
        for value in event_times
        if value is not None and (stamp := _safe_iso(value)) is not None
    }
    payload = {
        "asset": str(asset).upper(),
        "event_times": sorted(normalized),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _state_work_signature(
    state: Mapping[str, Any],
    *,
    asset: str,
    recovery_epoch: int = 0,
) -> str:
    continuity = (state.get("asset_continuity") or {}).get(asset) or {}
    payload = {
        "asset": str(asset).upper(),
        "latest_live": str(continuity.get("latest_live") or ""),
        "closed_bar_count": int(
            (state.get("closed_bar_counts") or {}).get(asset, 0) or 0
        ),
        "recovery_epoch": int(recovery_epoch),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _install_state_contract(ns: MutableMapping[str, Any]) -> None:
    original = ns["initial_state"]

    def initial_state_with_snapshot_pipeline() -> dict[str, Any]:
        state = original()
        _ensure_state(state)
        return state

    ns["initial_state"] = initial_state_with_snapshot_pipeline


def _install_snapshot_readers(
    ns: MutableMapping[str, Any],
    *,
    original_combined_bars: Any,
    original_live_times: Any,
    original_exact_readiness: Any,
) -> None:
    def combined_bars_for_snapshot(asset: str, policy: dict[str, Any], *args: Any, **kwargs: Any):
        context = getattr(_THREAD_CONTEXT, "snapshot", None) or {}
        if str(context.get("asset") or "").upper() == str(asset).upper():
            bars = context.get("bars")
            if isinstance(bars, pd.DataFrame):
                return bars.copy(deep=True)
        return original_combined_bars(asset, policy, *args, **kwargs)

    def live_times_for_snapshot(asset: str, *args: Any, **kwargs: Any):
        context = getattr(_THREAD_CONTEXT, "snapshot", None) or {}
        if str(context.get("asset") or "").upper() == str(asset).upper():
            return set(context.get("live_event_times") or set())
        return original_live_times(asset, *args, **kwargs)

    def exact_readiness_for_snapshot(
        namespace: MutableMapping[str, Any],
        *,
        asset: str,
        bars: pd.DataFrame,
        logger: Any,
        reason: str,
    ) -> dict[str, Any]:
        context = getattr(_THREAD_CONTEXT, "snapshot", None) or {}
        if (
            str(context.get("asset") or "").upper() == str(asset).upper()
            and context.get("status") == "READY"
        ):
            return copy.deepcopy(context["readiness"])
        return original_exact_readiness(
            namespace, asset=asset, bars=bars, logger=logger, reason=reason
        )

    ns["combined_bars_for_asset"] = combined_bars_for_snapshot
    ns["latest_live_executable_timestamps"] = live_times_for_snapshot
    p0._exact_readiness = exact_readiness_for_snapshot


def _install_feature_timing(ns: MutableMapping[str, Any]) -> None:
    original = ns["build_shared_asset_runtime_features"]

    def timed_build(*args: Any, **kwargs: Any):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            _record_latency(
                "semantic_feature_contract_cache",
                (time.perf_counter() - started) * 1000.0,
            )

    ns["build_shared_asset_runtime_features"] = timed_build


def _install_read_only_edge_consumers(
    ns: MutableMapping[str, Any],
    *,
    base_score_edge: Any,
    base_mark_incomplete: Any,
    base_sequential: Any,
    original_exact_readiness: Any,
    original_combined_bars: Any,
    original_live_times: Any,
) -> None:
    def mark_incomplete_read_only(**kwargs: Any) -> None:
        edge_id = str(kwargs.get("edge_id") or "")
        asset = str(kwargs.get("asset") or "").upper()
        state = kwargs["state"]
        runtime = kwargs["runtime"]
        features = kwargs["features"]
        events = kwargs["events"]
        logger = kwargs["logger"]
        diagnostics = ns["candidate_selected_feature_diagnostics"](
            runtime=runtime, features=features, events=events
        )
        invalid = list(diagnostics.get("invalid_feature_sample") or [])
        missing = list(diagnostics.get("missing_selected_features") or [])
        dependency_names = [
            name for name in [*invalid, *missing] if p0._is_dependency_feature(name)
        ]
        base_names = [
            name
            for name in dependency_names
            if str(name) in {
                "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD",
                "Regime_State", "Regime_Entropy", "Regime_Duration",
            }
            or str(name).startswith("Prob_State_")
        ]
        if not base_names:
            base_mark_incomplete(**kwargs)
            return

        candidate_values = list((diagnostics.get("invalid_features_by_timestamp") or {}).keys())
        if not candidate_values:
            candidate_values = list(diagnostics.get("candidate_timestamps") or [])
        candidate_times: list[pd.Timestamp] = []
        for value in candidate_values:
            try:
                candidate_times.append(_ensure_utc(value))
            except Exception:
                continue

        hmm_names = [
            name for name in base_names
            if str(name).startswith("Prob_State_") or str(name).startswith("Regime_")
        ]
        classification: dict[str, Any] = {"ineligible": [], "repair_required": candidate_times}
        if hmm_names and isinstance(features, pd.DataFrame):
            try:
                probability_columns = [
                    column for column in features.columns
                    if str(column).startswith("Prob_State_")
                ]
                classification = p0.classify_candidate_hmm_coverage(
                    features,
                    candidate_times,
                    n_states=len(probability_columns) or None,
                )
            except Exception:
                pass

        ineligible = []
        for value in classification.get("ineligible") or []:
            try:
                ineligible.append(_ensure_utc(value))
            except Exception:
                continue
        repair_required = []
        for value in classification.get("repair_required") or []:
            try:
                repair_required.append(_ensure_utc(value))
            except Exception:
                continue

        if ineligible and not repair_required:
            for stamp in ineligible:
                p0._record_dependency_terminal(
                    ns,
                    state=state,
                    asset=asset,
                    timestamp=stamp,
                    reason="hmm_ineligible_by_research_preprocessing",
                    source=str(kwargs.get("source") or "incomplete_model_input"),
                    edge_id=edge_id,
                    bar_hash=str((getattr(_THREAD_CONTEXT, "snapshot", None) or {}).get("bar_content_hash") or ""),
                    logger=logger,
                )
            base_mark_incomplete(**kwargs)
            return

        cursors = state.setdefault("last_processed_timestamp_by_edge", {})
        had_cursor = edge_id in cursors
        prior_cursor = cursors.get(edge_id)
        base_mark_incomplete(**kwargs)
        if had_cursor:
            cursors[edge_id] = prior_cursor
        else:
            cursors.pop(edge_id, None)

        context = getattr(_THREAD_CONTEXT, "snapshot", None)
        failure = {
            "created_at": _utc_now(),
            "asset": asset,
            "edge_id": edge_id,
            "bar_content_hash": str((context or {}).get("bar_content_hash") or ""),
            "base_dependency_features": sorted(set(str(value) for value in base_names)),
            "candidate_timestamps": [_safe_iso(value) for value in repair_required or candidate_times],
            "source": str(kwargs.get("source") or "incomplete_model_input"),
            "action": "asset_quarantine_and_dedicated_recovery",
        }
        if isinstance(context, dict):
            context.setdefault("dependency_failures", []).append(failure)
        logger.error(
            "SNAPSHOT_EDGE_DEPENDENCY_INCOMPLETE asset=%s edge=%s hash=%s features=%s action=quarantine_no_edge_repair",
            asset,
            edge_id,
            failure["bar_content_hash"][:16],
            failure["base_dependency_features"],
        )

    ns["mark_incomplete_model_input_candidate_processed"] = mark_incomplete_read_only

    def score_edge_read_only(
        edge: dict[str, Any],
        manifest: dict[str, Any],
        bar_policy: dict[str, Any],
        state: MutableMapping[str, Any],
        logger: Any,
        **kwargs: Any,
    ) -> list[Any]:
        edge_id = str(edge.get("edge_id") or "")
        asset = str(edge.get("asset") or "").upper()
        context = getattr(_THREAD_CONTEXT, "snapshot", None) or {}
        if str(context.get("asset") or "").upper() != asset:
            bars = kwargs.get("bars_snapshot")
            if not isinstance(bars, pd.DataFrame):
                bars = original_combined_bars(asset, bar_policy["bar_policies"][asset])
            original_exact_readiness(
                ns, asset=asset, bars=bars, logger=logger,
                reason=f"direct_score_edge_readiness:{edge_id}",
            )

        expected_dependency_generation = str(
            context.get("dependency_generation_id") or ""
        )
        if expected_dependency_generation:
            current_dependency_generation = (
                hot_state.current_dependency_generation(asset) or {}
            )
            actual_dependency_generation = str(
                current_dependency_generation.get("generation_id") or ""
            )
            if actual_dependency_generation != expected_dependency_generation:
                raise RuntimeError(
                    f"{asset}: feature_generation_mismatch "
                    f"snapshot_dependency_generation={expected_dependency_generation} "
                    f"published_dependency_generation={actual_dependency_generation}"
                )

        cursors = state.setdefault("last_processed_timestamp_by_edge", {})
        had_cursor = edge_id in cursors
        prior_cursor = cursors.get(edge_id)
        started = time.perf_counter()
        runtime_sem = ns.get("_RUNTIME_CALL_SEMAPHORE")
        already_held = bool(getattr(_THREAD_CONTEXT, "runtime_semaphore_held", False))
        if runtime_sem is not None and not already_held:
            _THREAD_CONTEXT.runtime_semaphore_held = True
            try:
                with runtime_sem:
                    rows = base_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)
            finally:
                _THREAD_CONTEXT.runtime_semaphore_held = False
        else:
            rows = base_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)
        _record_latency("edge_runtime_total", (time.perf_counter() - started) * 1000.0)

        dependency_failures = [
            row for row in (rows or [])
            if str(getattr(row, "signal_status", "") or "") == "failed_edge_runtime_exception"
            and _is_dependency_error(
                f"{getattr(row, 'error', '')} {getattr(row, 'reason', '')}"
            )
        ]
        if dependency_failures:
            if had_cursor:
                cursors[edge_id] = prior_cursor
            else:
                cursors.pop(edge_id, None)
            current = getattr(_THREAD_CONTEXT, "snapshot", None)
            if isinstance(current, dict):
                current.setdefault("dependency_failures", []).append(
                    {
                        "created_at": _utc_now(),
                        "asset": asset,
                        "edge_id": edge_id,
                        "bar_content_hash": str(current.get("bar_content_hash") or ""),
                        "source": "edge_runtime_dependency_exception",
                        "action": "asset_quarantine_and_dedicated_recovery",
                    }
                )
        return rows

    ns["score_edge"] = score_edge_read_only

    def sequential_ready_only(
        assets_to_score: list[str],
        manifest: dict[str, Any],
        bar_policy: dict[str, Any],
        state: MutableMapping[str, Any],
        logger: Any,
        *,
        scoring_lane: str = "all",
        allowed_edge_ids: set[str] | None = None,
        _write_outputs: bool = True,
    ) -> list[Any]:
        assets = sorted({str(asset).upper() for asset in assets_to_score if asset})
        context = getattr(_THREAD_CONTEXT, "snapshot", None) or {}
        if (
            len(assets) == 1
            and str(context.get("asset") or "").upper() == assets[0]
            and context.get("status") == "READY"
        ):
            return base_sequential(
                assets,
                manifest,
                bar_policy,
                state,
                logger,
                scoring_lane=scoring_lane,
                allowed_edge_ids=allowed_edge_ids,
                _write_outputs=_write_outputs,
            )

        rows: list[Any] = []
        for asset in assets:
            with hot_state.asset_lock(asset):
                bars = original_combined_bars(asset, bar_policy["bar_policies"][asset])
                live_times = set(original_live_times(asset))
                if bars is None or bars.empty or not live_times:
                    continue
                readiness = original_exact_readiness(
                    ns,
                    asset=asset,
                    bars=bars,
                    logger=logger,
                    reason=f"synchronous_asset_snapshot:{asset}",
                )
                public_readiness = {
                    key: copy.deepcopy(value)
                    for key, value in readiness.items()
                    if not str(key).startswith("_")
                }
                feature_snapshot = {
                    "asset": asset,
                    "generation_id": readiness["generation_id"],
                    "dependency_generation_id": readiness["dependency_generation_id"],
                    "dependency_generation": copy.deepcopy(
                        readiness.get("_dependency_generation") or {}
                    ),
                    "bar_content_hash": readiness["bar_content_hash"],
                    "snapshot_latest": readiness["snapshot_latest"],
                    "ffd": readiness["_ffd_frame"].copy(deep=True),
                    "hmm": readiness["_hmm_frame"].copy(deep=True),
                    "ffd_verification": copy.deepcopy(readiness["_ffd_verification"]),
                    "hmm_verification": copy.deepcopy(readiness["_hmm_verification"]),
                    "ffd_content_hash": readiness["ffd_content_hash"],
                    "hmm_content_hash": readiness["hmm_content_hash"],
                }
                context = {
                    "status": "READY",
                    "asset": asset,
                    "bars": bars.copy(deep=True),
                    "bar_content_hash": str(readiness.get("bar_content_hash") or p0._bar_content_hash(ns, bars)),
                    "snapshot_latest": p0._latest_bar_timestamp(bars),
                    "live_event_times": set(live_times),
                    "readiness": public_readiness,
                    "feature_generation_id": readiness["generation_id"],
                    "dependency_generation_id": readiness["dependency_generation_id"],
                    "dependency_failures": [],
                }
                with hot_state.feature_snapshot_scope(feature_snapshot):
                    _THREAD_CONTEXT.snapshot = context
                    p0._THREAD_CONTEXT.asset_snapshot = context
                    try:
                        rows.extend(
                            base_sequential(
                                [asset], manifest, bar_policy, state, logger,
                                scoring_lane=scoring_lane,
                                allowed_edge_ids=allowed_edge_ids,
                                _write_outputs=_write_outputs,
                            )
                        )
                    finally:
                        p0._THREAD_CONTEXT.asset_snapshot = None
                        _THREAD_CONTEXT.snapshot = None
        return rows

    ns["_score_selected_assets_sequential"] = sequential_ready_only


def _install_manager(
    ns: MutableMapping[str, Any],
    *,
    base_sequential: Any,
    original_combined_bars: Any,
    original_live_times: Any,
    original_exact_readiness: Any,
) -> None:
    manager = ns["ScoringQueueManager"]
    previous_init = manager.__init__
    previous_status = manager.status_snapshot
    settings = ns.get("settings")

    # Snapshot coordination must not invent a second signal-validity policy.
    # It consumes the dashboard's already-established live max signal age.
    max_snapshot_age = max(
        float(ns.get("MAX_SIGNAL_AGE_SECONDS", 120.0) or 120.0),
        1.0,
    )
    recovery_cooldown = max(
        float(getattr(settings, "LIVE_DASHBOARD_RECOVERY_COOLDOWN_SECONDS", 300.0)),
        10.0,
    )
    recovery_persistence_wait = max(
        float(ns.get("LIVE_BAR_COMPACT_MAX_AGE_SECONDS", 60.0)) * 2.0 + 5.0,
        15.0,
    )
    slow_families = {
        str(value).strip().lower()
        for value in (
            set(ns.get("SLOW_LANE_EDGE_FAMILIES") or set())
            | set(ns.get("EDGE_LOCAL_FEATURE_FAMILIES") or set())
        )
        if str(value).strip()
    }

    def init_snapshot_manager(self: Any, *args: Any, **kwargs: Any) -> None:
        previous_init(self, *args, **kwargs)
        _ensure_state(self.state)
        self.snapshot_jobs: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=0)
        self.recovery_jobs: queue.Queue[str | None] = queue.Queue(maxsize=0)
        self.snapshot_lock = threading.RLock()
        self.snapshot_pending: set[str] = set()
        self.snapshot_active: set[str] = set()
        self.snapshot_rerun: set[str] = set()
        self.snapshot_pending_signature: dict[str, str] = {}
        self.snapshot_active_signature: dict[str, str] = {}
        self.snapshot_rerun_signature: dict[str, str] = {}
        self.recovery_pending: set[str] = set()
        self.recovery_active: set[str] = set()
        self.recovery_pending_keys: set[str] = set()
        self.recovery_active_keys: set[str] = set()
        self.recovery_key_by_asset: dict[str, str] = {}
        self.recovery_last_attempt_at: dict[str, str] = {}
        self.snapshot_worker_count = 0
        self.snapshot_workers: list[threading.Thread] = []
        self.recovery_worker: threading.Thread | None = None
        self.recovery_worker_count = 0
        self.recovery_workers: list[threading.Thread] = []
        self.snapshot_jobs_superseded = 0
        self.snapshot_jobs_terminal_stale = 0
        self.snapshot_recovery_started = 0
        self.snapshot_recovery_completed = 0
        self.snapshot_recovery_failed = 0
        self.snapshot_recovery_cancelled = 0
        self.feature_generation_mismatches = 0

    def _refresh_active_fields(self: Any) -> None:
        with self.active_jobs_lock:
            active = copy.deepcopy(self.active_jobs_by_lane)
            preferred = next(iter(active.values()), {})
            self.active_job_id = preferred.get("job_id")
            self.active_job_lane = preferred.get("lane")
            self.active_job_assets = list(preferred.get("assets") or [])
            self.active_job_created_at = preferred.get("created_at")
            self.active_job_started_at = preferred.get("started_at")

    def enqueue_snapshot(
        self: Any,
        assets: list[str],
        *,
        reason: str,
        lane: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        if self.stop_event.is_set() or bool(
            getattr(ns.get("_DASHBOARD_SHUTDOWN_EVENT"), "is_set", lambda: False)()
        ):
            return False
        inserted = 0
        for asset in sorted({str(value).upper() for value in assets if value}):
            force_after_recovery = "recovery_completed" in str(reason).lower()
            with self.state_lock:
                signature = _state_work_signature(
                    self.state,
                    asset=asset,
                    recovery_epoch=(
                        int(self.snapshot_recovery_completed)
                        if force_after_recovery else 0
                    ),
                )
            with self.snapshot_lock:
                if asset in self.snapshot_active:
                    if force_after_recovery or signature != self.snapshot_active_signature.get(asset):
                        self.snapshot_rerun.add(asset)
                        self.snapshot_rerun_signature[asset] = signature
                    self.total_jobs_coalesced += 1
                    continue
                if asset in self.snapshot_pending:
                    if force_after_recovery or signature != self.snapshot_pending_signature.get(asset):
                        self.snapshot_pending_signature[asset] = signature
                        self.snapshot_jobs_superseded += 1
                    self.total_jobs_coalesced += 1
                    continue
                self.job_seq += 1
                job = {
                    "job_id": f"snapshot_{self.job_seq}_{asset}",
                    "asset": asset,
                    "created_at": _utc_now(),
                    "reason": reason,
                    "work_signature": signature,
                }
                self.snapshot_pending.add(asset)
                self.snapshot_pending_signature[asset] = signature
                self.snapshot_jobs.put_nowait(job)
                inserted += 1
        if inserted:
            self.logger.info(
                "ASSET_SNAPSHOT_ENQUEUED assets=%s inserted=%s pending=%s reason=%s",
                sorted({str(value).upper() for value in assets if value}),
                inserted,
                self.snapshot_jobs.qsize(),
                reason,
            )
        return True

    def _record_quarantine(
        self: Any,
        *,
        asset: str,
        bar_hash: str,
        latest: Any,
        reason: str,
        source: str,
    ) -> None:
        reason_class = _feature_reason_class(reason)
        if reason_class == "FEATURE_GENERATION_MISMATCH":
            self.feature_generation_mismatches += 1
        with self.state_lock:
            _ensure_state(self.state)
            prior = (self.state.get("asset_snapshot_quarantine_by_asset") or {}).get(asset) or {}
            attempts = int(prior.get("failure_count", 0) or 0) + 1
            self.state.setdefault("asset_snapshot_quarantine_by_asset", {})[asset] = {
                "status": "QUARANTINED",
                "asset": asset,
                "created_at": _utc_now(),
                "bar_content_hash": bar_hash,
                "snapshot_latest": _safe_iso(latest),
                "reason": reason,
                "reason_class": reason_class,
                "source": source,
                "failure_count": attempts,
                "next_recovery_at": _safe_iso(_utc_now_ts() + pd.Timedelta(seconds=recovery_cooldown)),
            }

    def _schedule_recovery(self: Any, asset: str, *, force: bool = False) -> bool:
        asset = str(asset).upper()
        now = _utc_now_ts()
        with self.state_lock:
            quarantine = copy.deepcopy(
                (self.state.get("asset_snapshot_quarantine_by_asset") or {}).get(asset)
                or {}
            )
        target_payload = {
            "asset": asset,
            "snapshot_latest": str(quarantine.get("snapshot_latest") or ""),
            "bar_content_hash": str(quarantine.get("bar_content_hash") or ""),
        }
        target_key = hashlib.sha256(
            json.dumps(
                target_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.snapshot_lock:
            active_key = self.recovery_key_by_asset.get(asset)
            if asset in self.recovery_active:
                if target_key != active_key:
                    self.logger.info(
                        "ASSET_SNAPSHOT_RECOVERY_COALESCED asset=%s "
                        "active_key=%s newer_target_key=%s "
                        "action=hydrate_then_replay_latest_snapshot",
                        asset,
                        str(active_key or "")[:16],
                        target_key[:16],
                    )
                return False
            if asset in self.recovery_pending:
                old_key = self.recovery_key_by_asset.get(asset)
                if old_key and old_key != target_key:
                    self.recovery_pending_keys.discard(old_key)
                    self.recovery_pending_keys.add(target_key)
                    self.recovery_key_by_asset[asset] = target_key
                    self.logger.info(
                        "ASSET_SNAPSHOT_RECOVERY_TARGET_SUPERSEDED asset=%s "
                        "old_key=%s new_key=%s",
                        asset,
                        old_key[:16],
                        target_key[:16],
                    )
                return False
            previous = self.recovery_last_attempt_at.get(asset)
            if previous and not force:
                try:
                    if (now - _ensure_utc(previous)).total_seconds() < recovery_cooldown:
                        return False
                except Exception:
                    pass
            if target_key in self.recovery_pending_keys or target_key in self.recovery_active_keys:
                return False
            self.recovery_pending.add(asset)
            self.recovery_pending_keys.add(target_key)
            self.recovery_key_by_asset[asset] = target_key
            self.recovery_jobs.put_nowait(asset)
        self.logger.warning(
            "ASSET_SNAPSHOT_RECOVERY_QUEUED asset=%s target_key=%s "
            "policy=single_worker_dedup_by_asset_target_generation",
            asset,
            target_key[:16],
        )
        return True

    def _merge_lane(
        self: Any,
        *,
        asset: str,
        edge_ids: set[str],
        lane_name: str,
        context: dict[str, Any],
    ) -> tuple[list[Any], float]:
        if not edge_ids:
            return [], 0.0
        with self.state_lock:
            base_state = copy.deepcopy(self.state)
        worker_state = copy.deepcopy(base_state)
        started = time.perf_counter()
        rows = base_sequential(
            [asset],
            self.manifest,
            self.bar_policy,
            worker_state,
            self.logger,
            scoring_lane="all",
            allowed_edge_ids=edge_ids,
            _write_outputs=False,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _record_latency(f"{lane_name}_edge_fanout", elapsed_ms)
        delta = ns["_scoring_batch_state_delta_after_worker"](
            assets=[asset],
            base_state=base_state,
            worker_state=worker_state,
            rows=rows,
        )
        with self.state_lock:
            ns["_merge_scoring_batch_state_delta"](self.state, delta)
        return rows, elapsed_ms

    def _process_snapshot(self: Any, job: dict[str, Any]) -> None:
        asset = str(job.get("asset") or "").upper()
        queue_wait_ms = max(
            (_utc_now_ts() - _ensure_utc(job.get("created_at"))).total_seconds() * 1000.0,
            0.0,
        )
        _record_latency("snapshot_queue_wait", queue_wait_ms)
        started = time.perf_counter()
        with self.active_jobs_lock:
            self.active_jobs_by_lane[f"snapshot:{threading.current_thread().name}"] = {
                "job_id": job.get("job_id"),
                "lane": "asset_snapshot",
                "assets": [asset],
                "created_at": job.get("created_at"),
                "started_at": _utc_now(),
            }
        _refresh_active_fields(self)
        self.total_jobs_started += 1

        def finish_without_scoring(status: str, **extra: Any) -> None:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            summary = {
                "job_id": job.get("job_id"),
                "status": status,
                "asset": asset,
                "queue_wait_ms": queue_wait_ms,
                "elapsed_ms": elapsed_ms,
                "rows": 0,
                "approved_signal_count": 0,
                "finished_at": _utc_now(),
                **extra,
            }
            with self.state_lock:
                self.last_job_summary = summary
                self.state["last_async_scoring_job"] = copy.deepcopy(summary)
            self.total_jobs_completed += 1

        try:
            with hot_state.asset_lock(asset):
                bars = original_combined_bars(asset, self.bar_policy["bar_policies"][asset])
                live_times = set(original_live_times(asset))
                if bars is None or bars.empty or not live_times:
                    self.logger.info(
                        "ASSET_SNAPSHOT_SKIP asset=%s reason=no_bars_or_executable_live_timestamps",
                        asset,
                    )
                    finish_without_scoring("skipped_no_executable_bars")
                    return
                bar_hash = p0._bar_content_hash(ns, bars)
                latest = p0._latest_bar_timestamp(bars)
                newest_live = max(live_times)
                age_seconds = max((_utc_now_ts() - _ensure_utc(newest_live)).total_seconds(), 0.0)
                if age_seconds > max_snapshot_age:
                    self.snapshot_jobs_terminal_stale += 1
                    with self.state_lock:
                        self.state.setdefault("last_terminal_asset_snapshot_by_asset", {})[asset] = {
                            "status": "TERMINAL_STALE",
                            "asset": asset,
                            "created_at": _utc_now(),
                            "bar_content_hash": bar_hash,
                            "snapshot_latest": _safe_iso(latest),
                            "newest_executable_live_timestamp": _safe_iso(newest_live),
                            "age_seconds": age_seconds,
                            "max_age_seconds": max_snapshot_age,
                        }
                    self.logger.warning(
                        "ASSET_SNAPSHOT_TERMINAL_STALE asset=%s hash=%s age_seconds=%.3f ceiling_seconds=%.3f",
                        asset, bar_hash[:16], age_seconds, max_snapshot_age,
                    )
                    finish_without_scoring(
                        "terminal_stale",
                        bar_content_hash=bar_hash,
                        snapshot_latest=_safe_iso(latest),
                        age_seconds=age_seconds,
                        max_age_seconds=max_snapshot_age,
                    )
                    return

                with self.state_lock:
                    quarantine = copy.deepcopy(
                        (self.state.get("asset_snapshot_quarantine_by_asset") or {}).get(asset)
                        or {}
                    )
                if quarantine:
                    next_recovery = quarantine.get("next_recovery_at")
                    recovery_due = True
                    if next_recovery:
                        try:
                            recovery_due = _utc_now_ts() >= _ensure_utc(next_recovery)
                        except Exception:
                            recovery_due = True
                    if recovery_due:
                        _schedule_recovery(self, asset, force=True)
                    self.logger.warning(
                        "ASSET_SNAPSHOT_QUARANTINED asset=%s hash=%s status=%s recovery_due=%s action=no_scoring",
                        asset,
                        bar_hash[:16],
                        quarantine.get("status"),
                        recovery_due,
                    )
                    finish_without_scoring(
                        "quarantined_no_scoring",
                        bar_content_hash=bar_hash,
                        snapshot_latest=_safe_iso(latest),
                        quarantine_status=quarantine.get("status"),
                        recovery_due=recovery_due,
                    )
                    return

                readiness_started = time.perf_counter()
                try:
                    readiness = original_exact_readiness(
                        ns,
                        asset=asset,
                        bars=bars,
                        logger=self.logger,
                        reason=f"snapshot_coordinator:{asset}",
                    )
                except Exception as exc:
                    reason = f"{type(exc).__name__}:{exc}"
                    _record_quarantine(
                        self,
                        asset=asset,
                        bar_hash=bar_hash,
                        latest=latest,
                        reason=reason,
                        source="snapshot_readiness",
                    )
                    _schedule_recovery(self, asset)
                    self.logger.error(
                        "ASSET_SNAPSHOT_READINESS_FAILED asset=%s hash=%s error=%s action=quarantine_and_dedicated_recovery",
                        asset, bar_hash[:16], reason,
                    )
                    finish_without_scoring(
                        "readiness_failed_quarantined",
                        bar_content_hash=bar_hash,
                        snapshot_latest=_safe_iso(latest),
                        error=reason,
                    )
                    return
                readiness_ms = (time.perf_counter() - readiness_started) * 1000.0
                _record_latency("ffd_hmm_readiness", readiness_ms)
                if readiness.get("ffd_elapsed_ms") is not None:
                    _record_latency("ffd_continuation", float(readiness["ffd_elapsed_ms"]))
                if readiness.get("hmm_elapsed_ms") is not None:
                    _record_latency("hmm_continuation", float(readiness["hmm_elapsed_ms"]))

                public_readiness = {
                    key: copy.deepcopy(value)
                    for key, value in readiness.items()
                    if not str(key).startswith("_")
                }
                feature_snapshot = {
                    "asset": asset,
                    "generation_id": readiness["generation_id"],
                    "dependency_generation_id": readiness["dependency_generation_id"],
                    "dependency_generation": copy.deepcopy(
                        readiness.get("_dependency_generation") or {}
                    ),
                    "bar_content_hash": bar_hash,
                    "snapshot_latest": _safe_iso(latest),
                    "ffd": readiness["_ffd_frame"].copy(deep=True),
                    "hmm": readiness["_hmm_frame"].copy(deep=True),
                    "ffd_verification": copy.deepcopy(readiness["_ffd_verification"]),
                    "hmm_verification": copy.deepcopy(readiness["_hmm_verification"]),
                    "ffd_content_hash": readiness["ffd_content_hash"],
                    "hmm_content_hash": readiness["hmm_content_hash"],
                }
                context = {
                    "status": "READY",
                    "asset": asset,
                    "bars": bars.copy(deep=True),
                    "bar_content_hash": bar_hash,
                    "feature_generation_id": readiness["generation_id"],
                    "dependency_generation_id": readiness["dependency_generation_id"],
                    "snapshot_latest": latest,
                    "live_event_times": set(live_times),
                    "readiness": public_readiness,
                    "ready_at": _utc_now(),
                    "dependency_failures": [],
                }
                with self.state_lock:
                    self.state.setdefault("last_ready_asset_snapshot_by_asset", {})[asset] = {
                        key: copy.deepcopy(value)
                        for key, value in context.items()
                        if key not in {"bars", "live_event_times", "dependency_failures"}
                    }
                    self.state.setdefault("asset_snapshot_quarantine_by_asset", {}).pop(asset, None)

                edges = [
                    edge for edge in self.manifest.get("selected_edges", [])
                    if str(edge.get("asset") or "").upper() == asset
                ]
                slow_ids = {
                    str(edge.get("edge_id"))
                    for edge in edges
                    if str(ns["edge_family_name"](edge)).lower() in slow_families
                }
                all_ids = {str(edge.get("edge_id")) for edge in edges}
                fast_ids = all_ids.difference(slow_ids)

                snapshot_scope = hot_state.feature_snapshot_scope(feature_snapshot)
                snapshot_scope.__enter__()
                _THREAD_CONTEXT.snapshot = context
                p0._THREAD_CONTEXT.asset_snapshot = context
                p0._THREAD_CONTEXT.p0_scoring_job = True
                try:
                    fast_rows, fast_ms = _merge_lane(
                        self,
                        asset=asset,
                        edge_ids=fast_ids,
                        lane_name="fast",
                        context=context,
                    )
                    elapsed_from_live = max(
                        (_utc_now_ts() - _ensure_utc(newest_live)).total_seconds(), 0.0
                    )
                    slow_rows: list[Any] = []
                    slow_ms = 0.0
                    if slow_ids and elapsed_from_live <= max_snapshot_age:
                        slow_rows, slow_ms = _merge_lane(
                            self,
                            asset=asset,
                            edge_ids=slow_ids,
                            lane_name="edge_local",
                            context=context,
                        )
                    elif slow_ids:
                        self.logger.warning(
                            "ASSET_SNAPSHOT_EDGE_LOCAL_SKIP asset=%s hash=%s reason=latency_budget_exhausted age_seconds=%.3f ceiling_seconds=%.3f",
                            asset, bar_hash[:16], elapsed_from_live, max_snapshot_age,
                        )

                    dependency_failures = list(context.get("dependency_failures") or [])
                    if dependency_failures:
                        with self.state_lock:
                            history = self.state.setdefault(
                                "asset_snapshot_dependency_failure_history", []
                            )
                            history.extend(copy.deepcopy(dependency_failures))
                            self.state["asset_snapshot_dependency_failure_history"] = history[-500:]
                        _record_quarantine(
                            self,
                            asset=asset,
                            bar_hash=bar_hash,
                            latest=latest,
                            reason="edge_consumer_reported_base_dependency_failure",
                            source="read_only_edge_consumer",
                        )
                        _schedule_recovery(self, asset)

                    total_ms = (time.perf_counter() - started) * 1000.0
                    decision_latency_ms = max(
                        (_utc_now_ts() - _ensure_utc(newest_live)).total_seconds() * 1000.0,
                        0.0,
                    )
                    _record_latency("snapshot_processing_total", total_ms)
                    _record_latency("bar_close_to_all_decisions", decision_latency_ms)
                    approved = sum(
                        1
                        for row in [*fast_rows, *slow_rows]
                        if bool(getattr(row, "approved_signal", False))
                    )
                    with self.state_lock:
                        self.last_job_summary = {
                            "job_id": job.get("job_id"),
                            "status": "done_with_dependency_quarantine" if dependency_failures else "done",
                            "asset": asset,
                            "bar_content_hash": bar_hash,
                            "snapshot_latest": _safe_iso(latest),
                            "queue_wait_ms": queue_wait_ms,
                            "readiness_ms": readiness_ms,
                            "fast_scoring_ms": fast_ms,
                            "edge_local_scoring_ms": slow_ms,
                            "elapsed_ms": total_ms,
                            "bar_close_to_all_decisions_ms": decision_latency_ms,
                            "rows": len(fast_rows) + len(slow_rows),
                            "approved_signal_count": approved,
                            "dependency_failure_count": len(dependency_failures),
                            "finished_at": _utc_now(),
                        }
                        self.state["last_async_scoring_job"] = copy.deepcopy(self.last_job_summary)
                        ns["write_runtime_outputs"](
                            [*fast_rows, *slow_rows],
                            self.state,
                            self.manifest,
                            self.bar_policy,
                        )
                    self.total_jobs_completed += 1
                finally:
                    p0._THREAD_CONTEXT.p0_scoring_job = False
                    p0._THREAD_CONTEXT.asset_snapshot = None
                    _THREAD_CONTEXT.snapshot = None
                    snapshot_scope.__exit__(None, None, None)
        except Exception as exc:
            self.total_jobs_failed += 1
            self.last_job_summary = {
                "job_id": job.get("job_id"),
                "status": "failed",
                "asset": asset,
                "error": f"{type(exc).__name__}:{exc}",
                "finished_at": _utc_now(),
            }
            self.logger.exception(
                "ASSET_SNAPSHOT_JOB_FAILED asset=%s job_id=%s error=%s",
                asset, job.get("job_id"), exc,
            )
            if _is_dependency_error(exc):
                _record_quarantine(
                    self,
                    asset=asset,
                    bar_hash="",
                    latest=None,
                    reason=f"{type(exc).__name__}:{exc}",
                    source="snapshot_job_exception",
                )
                _schedule_recovery(self, asset)
        finally:
            with self.active_jobs_lock:
                self.active_jobs_by_lane.pop(
                    f"snapshot:{threading.current_thread().name}", None
                )
            _refresh_active_fields(self)

    def _snapshot_worker(self: Any) -> None:
        while not self.stop_event.is_set() or not self.snapshot_jobs.empty():
            try:
                job = self.snapshot_jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                self.snapshot_jobs.task_done()
                break
            asset = str(job.get("asset") or "").upper()
            with self.snapshot_lock:
                self.snapshot_pending.discard(asset)
                signature = self.snapshot_pending_signature.pop(
                    asset, str(job.get("work_signature") or "")
                )
                job["work_signature"] = signature
                self.snapshot_active.add(asset)
                self.snapshot_active_signature[asset] = signature
            try:
                _process_snapshot(self, job)
            finally:
                rerun = False
                with self.snapshot_lock:
                    active_signature = self.snapshot_active_signature.pop(asset, None)
                    self.snapshot_active.discard(asset)
                    requested = self.snapshot_rerun_signature.pop(asset, None)
                    rerun = (
                        asset in self.snapshot_rerun
                        and requested is not None
                        and requested != active_signature
                    )
                    self.snapshot_rerun.discard(asset)
                self.snapshot_jobs.task_done()
                if rerun and not self.stop_event.is_set():
                    enqueue_snapshot(
                        self,
                        [asset],
                        reason="newer_bar_superseded_active_snapshot",
                    )

    def _recovery_loop(self: Any) -> None:
        while not self.stop_event.is_set() or not self.recovery_jobs.empty():
            try:
                asset = self.recovery_jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if asset is None:
                self.recovery_jobs.task_done()
                break
            asset = str(asset).upper()
            with self.snapshot_lock:
                recovery_key = str(self.recovery_key_by_asset.get(asset) or "")
                self.recovery_pending.discard(asset)
                if recovery_key:
                    self.recovery_pending_keys.discard(recovery_key)
                    self.recovery_active_keys.add(recovery_key)
                self.recovery_active.add(asset)
                self.recovery_last_attempt_at[asset] = _utc_now()
            self.snapshot_recovery_started += 1
            started = time.perf_counter()
            record: dict[str, Any]
            try:
                p0._THREAD_CONTEXT.p0_scoring_job = False
                with self.state_lock:
                    quarantine = copy.deepcopy(
                        (self.state.get("asset_snapshot_quarantine_by_asset") or {}).get(asset)
                        or {}
                    )
                snapshot_latest = quarantine.get("snapshot_latest")
                persistence: dict[str, Any] | None = None
                if snapshot_latest:
                    with self.state_lock:
                        ns["compact_pending_live_bars"](
                            asset,
                            self.state,
                            self.logger,
                            force=True,
                        )
                    persistence = _wait_for_persisted_snapshot(
                        asset,
                        snapshot_latest,
                        stop_event=self.stop_event,
                        timeout_seconds=recovery_persistence_wait,
                    )
                    self.logger.info(
                        "ASSET_SNAPSHOT_RECOVERY_PERSISTENCE_READY asset=%s target=%s "
                        "persisted_latest=%s waited_seconds=%.3f polls=%s",
                        asset,
                        persistence["target"],
                        persistence["persisted_latest"],
                        persistence["waited_seconds"],
                        persistence["polls"],
                    )
                # Recovery and normal snapshot continuation share the exact
                # same per-asset mutation owner. Recovery is not successful until
                # persistent authority has also been hydrated into the hot caches
                # and published as one coherent FFD+HMM dependency generation.
                with hot_state.asset_lock(asset):
                    result = ns["ensure_assets_live_feature_continuation"](
                        assets=[asset],
                        manifest=self.manifest,
                        logger=self.logger,
                        auto_repair=True,
                        fail_closed=False,
                        use_cache=False,
                    )
                    healthy = asset in set(result.get("healthy_assets") or [])
                    if not healthy:
                        raise RuntimeError(
                            "dedicated recovery did not return asset healthy: "
                            + json.dumps(
                                result.get("assets", {}).get(asset, {}),
                                default=str,
                            )[:2000]
                        )
                    from shared.live_streaming.continuation import (
                        hydrate_verified_feature_dependencies_from_authority,
                    )
                    hydration = hydrate_verified_feature_dependencies_from_authority(
                        asset,
                        source="dedicated_recovery_cache_hydration",
                    )
                if not bool(hydration.get("hot_cache_hydrated")):
                    raise RuntimeError(
                        f"{asset}: dedicated recovery did not hydrate hot caches"
                    )
                published_generation = hot_state.current_dependency_generation(asset)
                if (
                    not isinstance(published_generation, Mapping)
                    or str(published_generation.get("generation_id") or "")
                    != str(hydration.get("dependency_generation_id") or "")
                ):
                    raise RuntimeError(
                        f"{asset}: dedicated recovery generation publication mismatch"
                    )
                with p0._READINESS_CACHE_LOCK:
                    for key in [value for value in p0._EXACT_READINESS_CACHE if value[0] == asset]:
                        p0._EXACT_READINESS_CACHE.pop(key, None)
                p0._evict_semantic_cache(asset, current_hash="")

                # PHASE_C4_RECOVERY_RESET_V1
                # If the certified incremental feature router is active, a full
                # dependency recovery invalidates its compact feature state and
                # consumer ring before the latest snapshot is replayed.  When C.4
                # is staged but inactive this hook does not exist and Phase A/B
                # behavior is unchanged.
                phase_c4_reset = ns.get("_PHASE_C4_RECOVERY_RESET")
                if callable(phase_c4_reset):
                    reset_report = phase_c4_reset(
                        asset,
                        reason="dedicated_dependency_recovery_completed",
                    )
                    self.logger.warning(
                        "PHASE_C4_RECOVERY_STATE_RESET asset=%s report=%s "
                        "action=authoritative_feature_bootstrap_on_latest_snapshot",
                        asset,
                        json.dumps(reset_report, sort_keys=True, default=str),
                    )

                record = {
                    "status": "RECOVERED",
                    "asset": asset,
                    "created_at": _utc_now(),
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "persistence_barrier": persistence,
                    "continuation": result,
                    "hot_cache_hydration": {
                        "status": hydration.get("status"),
                        "target_timestamp": hydration.get("target_timestamp"),
                        "hmm_target_timestamp": hydration.get("hmm_target_timestamp"),
                        "dependency_generation_id": hydration.get("dependency_generation_id"),
                        "hot_cache_hydrated": hydration.get("hot_cache_hydrated"),
                    },
                    "recovery_target_key": recovery_key,
                }
                self.snapshot_recovery_completed += 1
                with self.state_lock:
                    self.state.setdefault("asset_snapshot_quarantine_by_asset", {}).pop(asset, None)
                enqueue_snapshot(
                    self,
                    [asset],
                    reason="dedicated_dependency_recovery_completed",
                )
                self.logger.warning(
                    "ASSET_SNAPSHOT_RECOVERY_COMPLETED asset=%s elapsed_ms=%.3f "
                    "dependency_generation_id=%s action=enqueue_latest_snapshot",
                    asset,
                    record["elapsed_ms"],
                    str(
                        (record.get("hot_cache_hydration") or {}).get(
                            "dependency_generation_id"
                        )
                        or ""
                    )[:16],
                )
            except RecoveryShutdownRequested as exc:
                self.snapshot_recovery_cancelled += 1
                record = {
                    "status": "RECOVERY_CANCELLED_SHUTDOWN",
                    "reason_class": "SHUTDOWN",
                    "asset": asset,
                    "created_at": _utc_now(),
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "error": f"{type(exc).__name__}:{exc}",
                }
                self.logger.info(
                    "ASSET_SNAPSHOT_RECOVERY_CANCELLED asset=%s reason=shutdown",
                    asset,
                )
            except Exception as exc:
                self.snapshot_recovery_failed += 1
                record = {
                    "status": "RECOVERY_FAILED",
                    "reason_class": _feature_reason_class(exc, recovery=True),
                    "asset": asset,
                    "created_at": _utc_now(),
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "error": f"{type(exc).__name__}:{exc}",
                    "next_recovery_at": _safe_iso(
                        _utc_now_ts() + pd.Timedelta(seconds=recovery_cooldown)
                    ),
                }
                self.logger.exception(
                    "ASSET_SNAPSHOT_RECOVERY_FAILED asset=%s error=%s cooldown_seconds=%.3f",
                    asset, exc, recovery_cooldown,
                )
            finally:
                with self.state_lock:
                    history = self.state.setdefault("asset_snapshot_recovery_history", [])
                    history.append(copy.deepcopy(record))
                    self.state["asset_snapshot_recovery_history"] = history[-200:]
                    if record.get("status") not in {
                        "RECOVERED", "RECOVERY_CANCELLED_SHUTDOWN"
                    }:
                        quarantine = self.state.setdefault(
                            "asset_snapshot_quarantine_by_asset", {}
                        ).setdefault(asset, {})
                        quarantine.update(record)
                        quarantine["status"] = "QUARANTINED_RECOVERY_FAILED"
                with self.snapshot_lock:
                    self.recovery_active.discard(asset)
                    active_key = self.recovery_key_by_asset.pop(asset, None)
                    if active_key:
                        self.recovery_active_keys.discard(active_key)
                self.recovery_jobs.task_done()

    def start_snapshot_manager(self: Any) -> None:
        if not self.enabled:
            self.logger.info(
                "ASSET_SNAPSHOT_COORDINATOR_BYPASS enabled=False reason=setting_disabled"
            )
            return
        selected_assets = {
            str(edge.get("asset") or "").upper()
            for edge in self.manifest.get("selected_edges", [])
            if edge.get("asset")
        }
        configured = max(int(ns.get("ASSET_SCORING_WORKERS", 1) or 1), 1)
        runtime_limit = max(int(ns.get("MAX_CONCURRENT_RUNTIME_CALLS", 1) or 1), 1)
        count = max(1, min(configured, runtime_limit, max(len(selected_assets), 1)))

        with self.state_lock:
            startup_recovery_assets = _reconcile_startup_continuation_health(
                self.state,
                selected_assets,
            )

        # PHASE_B_STARTUP_DEPENDENCY_CACHE_HYDRATION_V1
        #
        # Full FFD/HMM authority scans are allowed here, before normal snapshot
        # workers start. Every healthy selected asset must have warm FFD/HMM
        # frames, warm HMM static context, and a warm HMM eligibility/input ring.
        # A hydration failure is quarantined and routed to the same dedicated
        # recovery worker used by Phase A.
        from shared.live_streaming.continuation import (
            hydrate_verified_feature_dependencies_from_authority,
        )
        startup_hydrated_assets: list[str] = []
        startup_hydration_failures: dict[str, str] = {}
        recovery_assets = set(startup_recovery_assets)
        with self.state_lock:
            health_snapshot = copy.deepcopy(
                self.state.get("live_feature_continuation_health_by_asset") or {}
            )

        for asset in sorted(selected_assets.difference(recovery_assets)):
            health_record = health_snapshot.get(asset) or {}
            if str(health_record.get("status") or "") not in {
                "PASS",
                "REPAIRED_PASS",
                "NOT_REQUIRED",
            }:
                recovery_assets.add(asset)
                continue
            try:
                hydration = hydrate_verified_feature_dependencies_from_authority(
                    asset,
                    source="startup_phase_b_dependency_cache_hydration",
                )
                if not bool(hydration.get("hot_cache_hydrated")):
                    raise RuntimeError(
                        f"{asset}: startup dependency cache hydration incomplete"
                    )
                if int(hydration.get("hmm_input_ring_rows", 0) or 0) <= 0:
                    raise RuntimeError(
                        f"{asset}: startup HMM input ring was not hydrated"
                    )
                startup_hydrated_assets.append(asset)
                self.logger.info(
                    "PHASE_B_STARTUP_DEPENDENCY_CACHE_READY asset=%s "
                    "generation=%s hmm_input_ring_rows=%s",
                    asset,
                    str(hydration.get("dependency_generation_id") or "")[:16],
                    int(hydration.get("hmm_input_ring_rows", 0) or 0),
                )
            except Exception as exc:
                reason = f"{type(exc).__name__}:{exc}"
                startup_hydration_failures[asset] = reason
                recovery_assets.add(asset)
                _record_quarantine(
                    self,
                    asset=asset,
                    bar_hash="",
                    latest=health_record.get("live_bar_latest"),
                    reason=reason,
                    source="startup_phase_b_dependency_cache_hydration",
                )
                self.logger.exception(
                    "PHASE_B_STARTUP_DEPENDENCY_CACHE_FAILED asset=%s "
                    "error=%s action=quarantine_and_dedicated_recovery",
                    asset,
                    exc,
                )

        startup_recovery_assets = sorted(recovery_assets)
        self.startup_phase_b_hydrated_assets = startup_hydrated_assets
        self.startup_phase_b_hydration_failures = startup_hydration_failures

        # Only now may normal snapshot workers begin consuming dependency state.
        self.snapshot_worker_count = count
        self.snapshot_workers = []
        self.threads = {}
        for index in range(count):
            thread = threading.Thread(
                target=_snapshot_worker,
                args=(self,),
                name=f"asset_snapshot_{index}",
                daemon=True,
            )
            self.snapshot_workers.append(thread)
            self.threads[f"snapshot_{index}"] = thread
            thread.start()

        # Preserve Phase A's single heavyweight recovery owner.
        recovery_count = 1
        self.recovery_worker_count = recovery_count
        self.recovery_workers = []
        for index in range(recovery_count):
            recovery_worker = threading.Thread(
                target=_recovery_loop,
                args=(self,),
                name=f"asset_dependency_recovery_{index}",
                daemon=True,
            )
            self.recovery_workers.append(recovery_worker)
            self.threads[f"dependency_recovery_{index}"] = recovery_worker
            recovery_worker.start()
        self.recovery_worker = self.recovery_workers[0]

        for asset in startup_recovery_assets:
            _schedule_recovery(self, asset, force=True)
        self.thread = self.snapshot_workers[0]
        self.logger.info(
            "ASSET_SNAPSHOT_COORDINATOR_STARTED version=%s workers=%s recovery_workers=%s "
            "max_snapshot_age_seconds=%.3f startup_recovery_assets=%s "
            "startup_phase_b_hydrated_assets=%s "
            "policy=single_flight_per_asset_latest_snapshot",
            PIPELINE_VERSION,
            count,
            recovery_count,
            max_snapshot_age,
            startup_recovery_assets,
            startup_hydrated_assets,
        )

    def stop_snapshot_manager(self: Any, *, wait: bool = True) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        for _ in self.snapshot_workers:
            self.snapshot_jobs.put_nowait(None)
        for _ in self.recovery_workers:
            self.recovery_jobs.put_nowait(None)
        if wait:
            timeout = max(
                float(getattr(settings, "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_JOIN_TIMEOUT_SECONDS", 180.0)),
                1.0,
            )
            for thread in [*self.snapshot_workers, *self.recovery_workers]:
                if thread is not None:
                    thread.join(timeout=timeout)
                    if thread.is_alive():
                        self.logger.error(
                            "ASSET_SNAPSHOT_COORDINATOR_STOP_TIMEOUT worker=%s timeout_seconds=%.3f",
                            thread.name, timeout,
                        )

    def status_snapshot_manager(self: Any) -> dict[str, Any]:
        try:
            status = previous_status(self)
        except Exception:
            status = {}
        with self.state_lock:
            quarantines = copy.deepcopy(
                self.state.get("asset_snapshot_quarantine_by_asset") or {}
            )
            recovery_history = list(
                self.state.get("asset_snapshot_recovery_history") or []
            )
        last_recovery_failure = next(
            (
                record for record in reversed(recovery_history)
                if str(record.get("status")) == "RECOVERY_FAILED"
            ),
            None,
        )
        with self.snapshot_lock:
            status.update(
                {
                    "snapshot_pipeline_version": PIPELINE_VERSION,
                    "worker_mode": "asset_snapshot_coordinator_with_dedicated_recovery",
                    "pending_queue_size": self.snapshot_jobs.qsize(),
                    "active_worker_count": len(self.snapshot_active),
                    "snapshot_worker_count": self.snapshot_worker_count,
                    "recovery_worker_count": self.recovery_worker_count,
                    "startup_phase_b_hydrated_assets": list(
                        getattr(self, "startup_phase_b_hydrated_assets", [])
                    ),
                    "startup_phase_b_hydration_failures": copy.deepcopy(
                        getattr(self, "startup_phase_b_hydration_failures", {})
                    ),
                    "pending_assets": sorted(self.snapshot_pending),
                    "active_assets": sorted(self.snapshot_active),
                    "rerun_assets": sorted(self.snapshot_rerun),
                    "recovery_pending_assets": sorted(self.recovery_pending),
                    "recovery_active_assets": sorted(self.recovery_active),
                    "recovery_queue_size": self.recovery_jobs.qsize(),
                    "snapshot_jobs_superseded": self.snapshot_jobs_superseded,
                    "snapshot_jobs_terminal_stale": self.snapshot_jobs_terminal_stale,
                    "snapshot_recovery_started": self.snapshot_recovery_started,
                    "snapshot_recovery_completed": self.snapshot_recovery_completed,
                    "snapshot_recovery_failed": self.snapshot_recovery_failed,
                    "snapshot_recovery_cancelled": self.snapshot_recovery_cancelled,
                    "feature_generation_mismatches": self.feature_generation_mismatches,
                    "quarantine_count": len(quarantines),
                    "quarantined_assets": sorted(quarantines),
                    "quarantine_by_asset": quarantines,
                    "last_recovery_failure": last_recovery_failure,
                    "max_snapshot_age_seconds": max_snapshot_age,
                    "stage_latency": latency_snapshot(),
                }
            )
        return status

    manager.__init__ = init_snapshot_manager
    manager.enqueue = enqueue_snapshot
    manager.start = start_snapshot_manager
    manager.stop = stop_snapshot_manager
    manager.status_snapshot = status_snapshot_manager


def install_snapshot_pipeline(namespace: MutableMapping[str, Any]) -> dict[str, Any]:
    """Install the single-flight snapshot coordinator into the dashboard."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return {
                "installed": True,
                "already_installed": True,
                "version": PIPELINE_VERSION,
            }

        required = {
            "ScoringQueueManager",
            "score_edge",
            "mark_incomplete_model_input_candidate_processed",
            "_score_selected_assets_sequential",
            "combined_bars_for_asset",
            "latest_live_executable_timestamps",
            "build_shared_asset_runtime_features",
            "initial_state",
            "ensure_assets_live_feature_continuation",
        }
        missing = sorted(required.difference(namespace))
        if missing:
            raise RuntimeError(
                f"Snapshot pipeline cannot install; missing dashboard symbols={missing}"
            )
        if not bool(namespace.get("_P0_PRODUCTION_HARDENING_INSTALLED")):
            raise RuntimeError("Snapshot pipeline requires production_readiness_p0 first")

        original_combined_bars = namespace["combined_bars_for_asset"]
        original_live_times = namespace["latest_live_executable_timestamps"]
        original_exact_readiness = p0._exact_readiness
        base_score_edge = _closure_value(namespace["score_edge"], "original_score_edge")
        base_mark_incomplete = _closure_value(
            namespace["mark_incomplete_model_input_candidate_processed"],
            "original_mark_incomplete",
        )
        base_sequential = _closure_value(
            namespace["_score_selected_assets_sequential"],
            "original_sequential",
        )

        _install_state_contract(namespace)
        _install_snapshot_readers(
            namespace,
            original_combined_bars=original_combined_bars,
            original_live_times=original_live_times,
            original_exact_readiness=original_exact_readiness,
        )
        _install_feature_timing(namespace)
        _install_read_only_edge_consumers(
            namespace,
            base_score_edge=base_score_edge,
            base_mark_incomplete=base_mark_incomplete,
            base_sequential=base_sequential,
            original_exact_readiness=original_exact_readiness,
            original_combined_bars=original_combined_bars,
            original_live_times=original_live_times,
        )
        _install_manager(
            namespace,
            base_sequential=base_sequential,
            original_combined_bars=original_combined_bars,
            original_live_times=original_live_times,
            original_exact_readiness=original_exact_readiness,
        )

        current_version = str(namespace.get("DASHBOARD_VERSION") or "")
        if DASHBOARD_VERSION_SUFFIX not in current_version:
            namespace["DASHBOARD_VERSION"] = current_version + DASHBOARD_VERSION_SUFFIX
        namespace["HOTPATH_VERSION"] = "incremental_asset_snapshot_coordinator_v1"
        namespace["_SNAPSHOT_PIPELINE_INSTALLED"] = True
        namespace["_SNAPSHOT_PIPELINE_VERSION"] = PIPELINE_VERSION
        _INSTALLED = True
        return {
            "installed": True,
            "already_installed": False,
            "version": PIPELINE_VERSION,
            "dashboard_version": namespace["DASHBOARD_VERSION"],
            "normal_dependency_path": "incremental_single_flight_per_asset",
            "full_rebuild_path": "dedicated_recovery_worker_only",
            "edge_consumers": "read_only",
        }


def self_test() -> dict[str, Any]:
    sample_state = {
        "asset_continuity": {"X": {"latest_live": "2026-01-02T00:00:00+00:00"}},
        "closed_bar_counts": {"X": 7},
    }
    initial_work = _state_work_signature(sample_state, asset="X")
    changed_bar_state = copy.deepcopy(sample_state)
    changed_bar_state["closed_bar_counts"]["X"] = 8
    persisted_values = iter(
        [
            pd.Timestamp("2026-01-01T23:59:00Z"),
            pd.Timestamp("2026-01-02T00:00:00Z"),
        ]
    )
    fake_clock = [0.0]

    def fake_wait(seconds: float) -> bool:
        fake_clock[0] += float(seconds)
        return False

    persistence = _wait_for_persisted_snapshot(
        "X",
        "2026-01-02T00:00:00Z",
        stop_event=threading.Event(),
        timeout_seconds=1.0,
        poll_seconds=0.1,
        _latest_reader=lambda asset: next(persisted_values),
        _monotonic=lambda: fake_clock[0],
        _wait=fake_wait,
    )
    shutdown_cancelled = False
    try:
        _wait_for_persisted_snapshot(
            "X",
            "2026-01-02T00:00:00Z",
            stop_event=threading.Event(),
            timeout_seconds=1.0,
            _latest_reader=lambda asset: pd.Timestamp("2026-01-01T23:59:00Z"),
            _monotonic=lambda: 0.0,
            _wait=lambda seconds: True,
        )
    except RecoveryShutdownRequested:
        shutdown_cancelled = True

    startup_state: dict[str, Any] = {
        "asset_snapshot_quarantine_by_asset": {
            "Y": {"status": "QUARANTINED_RECOVERY_FAILED"},
            "Z": {"status": "QUARANTINED_RECOVERY_FAILED"},
        },
        "live_feature_continuation_health_by_asset": {
            "X": {
                "status": "FAIL",
                "live_bar_latest": "2026-01-02T00:00:00+00:00",
                "errors": ["staged recovery required"],
            },
            "Y": {"status": "PASS"},
        },
    }
    startup_recovery_assets = _reconcile_startup_continuation_health(
        startup_state,
        {"X", "Y"},
    )
    source_frame = pd.DataFrame(
        {"Datetime": [pd.Timestamp("2026-01-02T00:00:00Z")], "Close_FFD": [1.0]}
    )
    child_generation: list[str | None] = []
    with hot_state.feature_snapshot_scope(
        {"asset": "X", "generation_id": "generation-a", "ffd": source_frame}
    ):
        source_frame.loc[0, "Close_FFD"] = 999.0
        bound = hot_state.current_feature_snapshot("X")
        immutable_snapshot = bool(
            bound is not None
            and bound["generation_id"] == "generation-a"
            and float(bound["ffd"].loc[0, "Close_FFD"]) == 1.0
            and hot_state.current_feature_snapshot("Y") is None
        )
        child = threading.Thread(
            target=lambda: child_generation.append(
                (hot_state.current_feature_snapshot("X") or {}).get("generation_id")
            )
        )
        child.start()
        child.join(timeout=2.0)
    tests = {
        "signature_is_order_independent": (
            _snapshot_signature("X", ["2026-01-02", "2026-01-01"])
            == _snapshot_signature("X", ["2026-01-01", "2026-01-02"])
        ),
        "dependency_classifier": (
            _is_dependency_error("checkpoint_schema_mismatch")
            and _is_dependency_error("in-memory HMM dependency continuation failed closed")
            and not _is_dependency_error("probability below threshold")
        ),
        "latency_quantiles": _percentile([1.0, 2.0, 3.0], 0.95) == 3.0,
        "state_contract_has_quarantine": "asset_snapshot_quarantine_by_asset" in _state_defaults(),
        "work_signature_changes_on_new_bar": (
            initial_work
            != _state_work_signature(changed_bar_state, asset="X")
        ),
        "work_signature_forces_post_recovery_rerun": (
            initial_work
            != _state_work_signature(sample_state, asset="X", recovery_epoch=1)
        ),
        "recovery_waits_for_compacted_snapshot": (
            persistence["target"] == "2026-01-02T00:00:00+00:00"
            and persistence["persisted_latest"] == "2026-01-02T00:00:00+00:00"
            and persistence["polls"] == 2
            and persistence["waited_seconds"] == 0.1
        ),
        "immutable_worker_local_feature_generation": immutable_snapshot,
        "feature_generation_propagates_to_edge_threads": child_generation == ["generation-a"],
        "startup_health_seeds_every_blocked_asset": (
            startup_recovery_assets == ["X"]
            and startup_state["asset_snapshot_quarantine_by_asset"]["X"]["source"]
            == "startup_continuation_health"
        ),
        "startup_health_clears_stale_healthy_quarantine": (
            "Y" not in startup_state["asset_snapshot_quarantine_by_asset"]
            and "Z" in startup_state["asset_snapshot_quarantine_by_asset"]
        ),
        "shutdown_wait_is_cancellation_not_failure": shutdown_cancelled,
    }
    failed = [name for name, passed in tests.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "version": PIPELINE_VERSION,
        "tests": tests,
        "failed": failed,
    }


__all__ = [
    "PIPELINE_VERSION",
    "install_snapshot_pipeline",
    "latency_snapshot",
    "self_test",
]
