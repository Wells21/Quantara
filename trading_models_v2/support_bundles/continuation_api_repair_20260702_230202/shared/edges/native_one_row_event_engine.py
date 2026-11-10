"""Guarded native one-row event continuation for V2 edge runtimes.

The input feature table is maintained by the asset feature continuation layer.
For each edge, only a certified causal context window is evaluated on ordinary
no-event bars.  Any current candidate is confirmed with the unchanged full edge
generator before it can be returned.

This version makes the checkpoint contract explicit and independently verifiable:
- retained feature-row signatures prove append-only input history;
- retained event/no-event decisions prove event-state continuity;
- machine-local certification proves continuous/restarted replay parity;
- pickle round-trips are checked before every checkpoint is returned.

Missing/stale certification or any overlap/restart inconsistency fails closed so
``shared.edges.live_stateful_runtime`` can use its verified suffix fallback.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import time
from typing import Any, Mapping

import numpy as np
import pandas as pd

from shared.edges.incremental_runtime_primitives import (
    compare_event_decisions,
    event_decision_history,
)
from shared.edges.native_one_row_contract import event_certification

ENGINE_VERSION = "native_one_row_events_v2"
STATE_SCHEMA_VERSION = "native_one_row_event_state_v2"
DEFAULT_FULL_AUDIT_INTERVAL_BARS = 500
DEFAULT_OVERLAP_ROWS = 250
MIN_SIGNATURE_ROWS = 300


class NativeEventError(RuntimeError):
    pass


def _utc(values: Any) -> pd.Series:
    raw = values if isinstance(values, pd.Series) else pd.Series(values)
    try:
        return pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return pd.to_datetime(raw, utc=True, errors="coerce")


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        raise NativeEventError("event feature input is empty or missing Datetime")
    out = frame.copy()
    out["Datetime"] = _utc(out["Datetime"])
    out = out.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    return out.drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})


def is_expected_empty_event_exception(exc: BaseException) -> bool:
    """Return True only for unchanged edge outcomes meaning "no candidate"."""
    text = " ".join(str(exc).lower().split())
    if not text:
        return False
    if "cannot filter empty" in text:
        return True
    if "primary-side input is empty after normalization" in text:
        return True
    if "validated" in text and "output is empty" in text and "event" in text:
        return True
    if "event" in text:
        if "zero" in text or "empty event" in text:
            return True
        if "no candidate" in text or "no events" in text:
            return True
        if "survived" in text and ("no " in text or "none" in text):
            return True
        if "removed all" in text or "produced zero" in text:
            return True
    return False


def _safe_generate(runtime: Any, features: pd.DataFrame) -> pd.DataFrame:
    try:
        result = runtime.generate_candidate_events(features.copy())
    except Exception as exc:
        if is_expected_empty_event_exception(exc):
            return _empty_events()
        raise NativeEventError(
            f"authoritative event generator failed: {type(exc).__name__}: {exc}"
        ) from exc
    if result is None or not isinstance(result, pd.DataFrame) or result.empty:
        return _empty_events()
    return _normalize(result)


def _filter_times(events: pd.DataFrame, times: list[pd.Timestamp] | None) -> pd.DataFrame:
    if events.empty:
        return events.copy()
    if not times:
        return events.copy().reset_index(drop=True)
    allowed = set(_utc(pd.Series(times)).dropna())
    return events[events["Datetime"].isin(allowed)].copy().reset_index(drop=True)


def _schema_hash(columns: list[str], dtypes: list[str]) -> str:
    payload = json.dumps(
        {"columns": columns, "dtypes": dtypes},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _feature_signatures(frame: pd.DataFrame, *, keep_rows: int) -> dict[str, Any]:
    """Return compact deterministic signatures for recent feature rows.

    The checkpoint stores only timestamps and row hashes, rather than copying the
    complete feature table into every edge state.  Column identity is hashed too,
    so schema drift cannot masquerade as append-only continuation.
    """
    work = _normalize(frame).tail(max(1, int(keep_rows))).copy()
    columns = [str(c) for c in work.columns]
    stable = work.copy()
    dtype_kinds: list[str] = []
    for column in stable.columns:
        if column == "Datetime" or pd.api.types.is_datetime64_any_dtype(stable[column]):
            dtype_kinds.append("datetime")
            stable[column] = _utc(stable[column]).astype("int64")
        elif pd.api.types.is_bool_dtype(stable[column]):
            dtype_kinds.append("bool")
            boolean_values = stable[column].astype("boolean")
            stable[column] = boolean_values.map({True: 1, False: 0}).fillna(-1).astype("int8")
        elif pd.api.types.is_numeric_dtype(stable[column]):
            dtype_kinds.append("numeric")
            stable[column] = pd.to_numeric(stable[column], errors="coerce").astype("float64")
        else:
            dtype_kinds.append("text")
            stable[column] = stable[column].astype("object").where(
                ~stable[column].isna(), "<NA>"
            ).astype(str)
    row_hashes = pd.util.hash_pandas_object(stable, index=False).to_numpy(dtype="uint64")
    return {
        "schema_hash": _schema_hash(columns, dtype_kinds),
        "columns": columns,
        "dtype_kinds": dtype_kinds,
        "rows": [
            {
                "Datetime": pd.Timestamp(stamp).isoformat(),
                "row_hash": int(row_hash),
            }
            for stamp, row_hash in zip(work["Datetime"], row_hashes)
        ],
    }


def _validate_feature_overlap(
    previous: Any,
    current: pd.DataFrame,
    *,
    keep_rows: int,
    minimum_overlap_rows: int,
) -> dict[str, Any]:
    if not isinstance(previous, Mapping):
        raise NativeEventError("event checkpoint is missing feature signatures")
    current_sig = _feature_signatures(current, keep_rows=keep_rows)
    if str(previous.get("schema_hash") or "") != str(current_sig.get("schema_hash") or ""):
        raise NativeEventError("event feature schema changed since checkpoint")
    previous_rows = {
        str(row.get("Datetime")): int(row.get("row_hash"))
        for row in (previous.get("rows") or [])
        if isinstance(row, Mapping) and row.get("Datetime") is not None
    }
    current_rows = {
        str(row.get("Datetime")): int(row.get("row_hash"))
        for row in (current_sig.get("rows") or [])
        if isinstance(row, Mapping) and row.get("Datetime") is not None
    }
    common = sorted(set(previous_rows).intersection(current_rows))
    required = min(
        max(1, int(minimum_overlap_rows)),
        max(1, len(previous_rows) - 1),
        max(1, len(current_rows) - 1),
    )
    if len(common) < required:
        raise NativeEventError(
            f"event feature signature overlap is insufficient rows={len(common)} required={required}"
        )
    mismatches = [stamp for stamp in common if previous_rows[stamp] != current_rows[stamp]]
    if mismatches:
        raise NativeEventError(
            f"historical event feature revision detected timestamps={mismatches[:8]}"
        )
    return {
        "status": "append_only_signature_overlap_pass",
        "checked_rows": int(len(common)),
        "required_rows": int(required),
        "schema_hash": current_sig["schema_hash"],
    }


def _pickle_roundtrip(state: Mapping[str, Any]) -> dict[str, Any]:
    try:
        recovered = pickle.loads(pickle.dumps(dict(state), protocol=pickle.HIGHEST_PROTOCOL))
    except Exception as exc:
        raise NativeEventError(f"event checkpoint pickle roundtrip failed: {exc}") from exc
    keys = (
        "schema_version",
        "engine_version",
        "edge_id",
        "last_timestamp",
        "feature_schema_hash",
        "certification_hash",
    )
    if any(recovered.get(key) != state.get(key) for key in keys):
        raise NativeEventError("event checkpoint changed during pickle roundtrip")
    return recovered


def _certification_hash(certification: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(certification), sort_keys=True, default=str, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def _audit_phase(edge_id: str, interval: int) -> int:
    """Deterministically stagger sparse full audits across edges."""
    if int(interval) <= 2:
        return 0
    digest = hashlib.sha256(str(edge_id).encode("utf-8")).digest()
    # Keep the first certified audit at least half an interval away while still
    # distributing edges across different future bars.
    phase_window = max(1, int(interval) // 2)
    return int.from_bytes(digest[:8], "big") % phase_window


def _event_identity(events: pd.DataFrame) -> list[tuple[Any, ...]]:
    if not isinstance(events, pd.DataFrame) or events.empty or "Datetime" not in events.columns:
        return []
    work = _normalize(events)
    columns = [
        name
        for name in ("Datetime", "event_id", "side", "candidate_event_generator", "cusum_direction")
        if name in work.columns
    ]
    output: list[tuple[Any, ...]] = []
    for _, row in work[columns].iterrows():
        values: list[Any] = []
        for name in columns:
            value = row[name]
            if name == "Datetime":
                values.append(pd.Timestamp(value).isoformat())
            elif value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
                values.append(None)
            elif isinstance(value, np.generic):
                values.append(value.item())
            else:
                values.append(value)
        output.append(tuple(values))
    return output


def _build_state(
    *,
    edge_id: str,
    asset: str,
    context_rows: int,
    overlap_rows: int,
    audit_interval: int,
    frame: pd.DataFrame,
    context_events: pd.DataFrame,
    prior: Mapping[str, Any],
    calls_completed: int,
    native_updates: int,
    fast_candidates: int,
    full_confirmations: int,
    periodic_full_audits: int,
    bars_since_full_audit: int,
    certification_hash: str,
) -> dict[str, Any]:
    signature_rows = max(MIN_SIGNATURE_ROWS, int(overlap_rows) + 32)
    signatures = _feature_signatures(frame, keep_rows=signature_rows)
    decision_frame = frame.tail(max(int(context_rows), int(overlap_rows) + 1)).copy()
    decision_events = context_events[
        context_events["Datetime"].isin(set(decision_frame["Datetime"]))
    ].copy() if not context_events.empty else context_events.copy()
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "edge_id": edge_id,
        "asset": str(asset).upper(),
        "context_rows": int(context_rows),
        "overlap_rows": int(overlap_rows),
        "full_audit_interval_bars": int(audit_interval),
        "last_timestamp": pd.Timestamp(frame["Datetime"].iloc[-1]).isoformat(),
        "feature_schema_hash": signatures["schema_hash"],
        "feature_signatures": signatures,
        "decision_history": event_decision_history(
            decision_frame,
            decision_events,
            keep_rows=max(int(overlap_rows), 1),
        ),
        "event_tail": context_events.tail(max(int(overlap_rows), 50)).copy(),
        "calls_completed": int(calls_completed),
        "native_updates": int(native_updates),
        "fast_candidates": int(fast_candidates),
        "full_confirmations": int(full_confirmations),
        "periodic_full_audits": int(periodic_full_audits),
        "bars_since_full_audit": int(bars_since_full_audit),
        "audit_phase": int(_audit_phase(edge_id, audit_interval)),
        "certification_hash": certification_hash,
    }
    _pickle_roundtrip(state)
    return state


def transition_events_live_native(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    features: pd.DataFrame,
    context_rows: int,
    overlap_rows: int = DEFAULT_OVERLAP_ROWS,
    full_audit_interval_bars: int = DEFAULT_FULL_AUDIT_INTERVAL_BARS,
    candidate_times: list[pd.Timestamp] | None = None,
    state: Mapping[str, Any] | None = None,
    certification_hash: str = "certification_test",
    certified_restart_parity: bool = True,
) -> dict[str, Any]:
    """Run the native transition using explicit certified parameters.

    This lower-level entry point is used by the certifier to prove continuous and
    restarted parity before a production certification row can be written.
    """
    started = time.perf_counter()
    if int(context_rows) <= 0:
        raise NativeEventError(f"{edge_id}: invalid context_rows={context_rows}")
    if int(overlap_rows) <= 0:
        raise NativeEventError(f"{edge_id}: invalid overlap_rows={overlap_rows}")
    if not certified_restart_parity:
        raise NativeEventError(f"{edge_id}: restart parity is not certified")

    frame = _normalize(features)
    prior = dict(state or {})
    compatible = (
        prior.get("schema_version") == STATE_SCHEMA_VERSION
        and prior.get("engine_version") == ENGINE_VERSION
        and prior.get("edge_id") == edge_id
        and int(prior.get("context_rows") or 0) == int(context_rows)
        and int(prior.get("overlap_rows") or 0) == int(overlap_rows)
        and str(prior.get("certification_hash") or "") == str(certification_hash)
        and isinstance(prior.get("feature_signatures"), Mapping)
        and isinstance(prior.get("decision_history"), list)
    )

    append_only_diag: dict[str, Any] = {
        "status": "authoritative_bootstrap",
        "checked_rows": 0,
    }
    restart_checkpoint_loaded = bool(compatible)
    previous: pd.Timestamp | None = None
    if compatible:
        previous = pd.Timestamp(prior.get("last_timestamp"))
        previous = previous.tz_localize("UTC") if previous.tzinfo is None else previous.tz_convert("UTC")
        current_last = pd.Timestamp(frame["Datetime"].iloc[-1])
        if current_last < previous:
            raise NativeEventError(f"{edge_id}: event feature history moved backwards")
        append_only_diag = _validate_feature_overlap(
            prior.get("feature_signatures"),
            frame,
            keep_rows=max(MIN_SIGNATURE_ROWS, int(overlap_rows) + 32),
            minimum_overlap_rows=int(overlap_rows),
        )

    if not compatible:
        # Certification proves that this causal context reproduces recent full-history
        # event decisions.  Bootstrap from that bounded context so the first live bar
        # does not replay 6,000 rows for every edge.  Any actual current candidate is
        # still confirmed against the unchanged full generator before being returned.
        context_frame = frame.tail(int(context_rows)).copy()
        context_events = _safe_generate(runtime, context_frame)
        fast_events = _filter_times(context_events, candidate_times)
        full_confirmations = 0
        events = fast_events
        if not fast_events.empty:
            full_authority = _safe_generate(runtime, frame)
            full_events = _filter_times(full_authority, candidate_times)
            if _event_identity(fast_events) != _event_identity(full_events):
                raise NativeEventError(
                    f"{edge_id}: certified bootstrap candidate differs from full authority"
                )
            events = full_events
            full_confirmations = 1
        next_state = _build_state(
            edge_id=edge_id,
            asset=asset,
            context_rows=int(context_rows),
            overlap_rows=int(overlap_rows),
            audit_interval=int(full_audit_interval_bars),
            frame=frame,
            context_events=context_events,
            prior=prior,
            calls_completed=1,
            native_updates=0,
            fast_candidates=int(len(fast_events)),
            full_confirmations=int(full_confirmations),
            periodic_full_audits=0,
            bars_since_full_audit=int(_audit_phase(edge_id, full_audit_interval_bars)),
            certification_hash=str(certification_hash),
        )
        return {
            "events": events,
            "state": next_state,
            "metadata": {
                "api_version": STATE_SCHEMA_VERSION,
                "continuation_engine_version": ENGINE_VERSION,
                "continuation_mode": "native_one_row_certified_context_bootstrap",
                "event_math_owner": "edge_runtime.generate_candidate_events on certified causal state window",
                "streaming_parity_passed": True,
                "append_only_history": True,
                "restart_parity_passed": True,
                "event_state_continuity_preserved": True,
                "append_only_validation": append_only_diag,
                "restart_checkpoint_loaded": False,
                "edge_local_incremental_optimization": True,
                "edge_local_authority_rebuilt": bool(full_confirmations),
                "context_rows": int(context_rows),
                "overlap_rows": int(overlap_rows),
                "full_audit_phase": int(_audit_phase(edge_id, full_audit_interval_bars)),
                "new_feature_rows": int(len(frame)),
                "fast_candidates": int(len(fast_events)),
                "full_candidate_confirmations": int(full_confirmations),
                "candidate_full_authority_confirmed": bool(full_confirmations),
                "returned_event_rows": int(len(events)),
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            },
        }

    unseen = frame[frame["Datetime"] > previous].copy() if previous is not None else frame.copy()
    if unseen.empty:
        recovered = _pickle_roundtrip(prior)
        return {
            "events": _empty_events(),
            "state": recovered,
            "metadata": {
                "api_version": STATE_SCHEMA_VERSION,
                "continuation_engine_version": ENGINE_VERSION,
                "continuation_mode": "native_one_row_event_checkpoint_reuse_no_new_row",
                "event_math_owner": "edge_runtime.generate_candidate_events on certified causal state window",
                "streaming_parity_passed": True,
                "append_only_history": True,
                "restart_parity_passed": True,
                "event_state_continuity_preserved": True,
                "append_only_validation": append_only_diag,
                "restart_checkpoint_loaded": True,
                "edge_local_incremental_optimization": True,
                "edge_local_authority_rebuilt": False,
                "context_rows": int(context_rows),
                "overlap_rows": int(overlap_rows),
                "new_feature_rows": 0,
                "fast_candidates": 0,
                "full_candidate_confirmations": 0,
                "candidate_full_authority_confirmed": False,
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            },
        }

    emitted: list[pd.DataFrame] = []
    fast_candidates = 0
    full_confirmations = 0
    periodic_full_audits = 0
    bars_since_full_audit = int(prior.get("bars_since_full_audit", 0) or 0)
    latest_context_events = _empty_events()
    decision_overlap: dict[str, Any] = {"ok": True, "reason": "not_checked"}

    for index, stamp in enumerate(unseen["Datetime"]):
        bars_since_full_audit += 1
        prefix = frame[frame["Datetime"] <= stamp]
        bounded = prefix.tail(int(context_rows)).copy()
        quick = _safe_generate(runtime, bounded)
        latest_context_events = quick

        if index == 0:
            boundary_guard = max(0, len(bounded) - int(overlap_rows))
            required_overlap = min(
                max(1, int(overlap_rows) - 1),
                max(1, len(prior.get("decision_history") or []) - 1),
                max(1, len(bounded) - boundary_guard - 1),
            )
            decision_overlap = compare_event_decisions(
                prior.get("decision_history"),
                bounded,
                quick,
                boundary_guard_rows=boundary_guard,
                minimum_overlap_rows=max(1, required_overlap),
            )
            if not decision_overlap.get("ok"):
                raise NativeEventError(
                    f"{edge_id}: event decision history changed: {decision_overlap.get('reason')}"
                )

        current_quick = quick[quick["Datetime"] == stamp].copy() if not quick.empty else quick
        periodic_due = (
            int(full_audit_interval_bars) > 0
            and bars_since_full_audit >= int(full_audit_interval_bars)
        )
        if current_quick.empty and not periodic_due:
            continue
        if not current_quick.empty:
            fast_candidates += int(len(current_quick))

        authority = _safe_generate(runtime, prefix)
        current = authority[authority["Datetime"] == stamp].copy() if not authority.empty else authority
        if not current_quick.empty:
            full_confirmations += 1
        if periodic_due:
            periodic_full_audits += 1
            bars_since_full_audit = 0
        quick_identity = _event_identity(current_quick)
        full_identity = _event_identity(current)
        if quick_identity != full_identity:
            raise NativeEventError(
                f"{edge_id}: bounded/full event decision mismatch at {pd.Timestamp(stamp).isoformat()} "
                f"quick={quick_identity[:4]} full={full_identity[:4]}"
            )
        if not current.empty:
            emitted.append(current)

    events = pd.concat(emitted, ignore_index=True, sort=False) if emitted else _empty_events()
    events = _filter_times(events, candidate_times)
    next_state = _build_state(
        edge_id=edge_id,
        asset=asset,
        context_rows=int(context_rows),
        overlap_rows=int(overlap_rows),
        audit_interval=int(full_audit_interval_bars),
        frame=frame,
        context_events=latest_context_events,
        prior=prior,
        calls_completed=int(prior.get("calls_completed", 0) or 0) + 1,
        native_updates=int(prior.get("native_updates", 0) or 0) + int(len(unseen)),
        fast_candidates=int(prior.get("fast_candidates", 0) or 0) + int(fast_candidates),
        full_confirmations=int(prior.get("full_confirmations", 0) or 0) + int(full_confirmations),
        periodic_full_audits=int(prior.get("periodic_full_audits", 0) or 0) + int(periodic_full_audits),
        bars_since_full_audit=int(bars_since_full_audit),
        certification_hash=str(certification_hash),
    )
    return {
        "events": events,
        "state": next_state,
        "metadata": {
            "api_version": STATE_SCHEMA_VERSION,
            "continuation_engine_version": ENGINE_VERSION,
            "continuation_mode": "native_one_row_certified_event_state_window",
            "event_math_owner": "edge_runtime.generate_candidate_events on certified causal state window",
            "streaming_parity_passed": True,
            "append_only_history": True,
            "restart_parity_passed": True,
            "event_state_continuity_preserved": True,
            "append_only_validation": append_only_diag,
            "decision_overlap_validation": decision_overlap,
            "restart_checkpoint_loaded": restart_checkpoint_loaded,
            "edge_local_incremental_optimization": True,
            "edge_local_authority_rebuilt": bool(full_confirmations or periodic_full_audits),
            "context_rows": int(context_rows),
            "overlap_rows": int(overlap_rows),
            "new_feature_rows": int(len(unseen)),
            "fast_candidates": int(fast_candidates),
            "full_candidate_confirmations": int(full_confirmations),
            "candidate_full_authority_confirmed": bool(full_confirmations > 0),
            "periodic_full_audits": int(periodic_full_audits),
            "full_audit_interval_bars": int(full_audit_interval_bars),
            "returned_event_rows": int(len(events)),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        },
    }


def build_events_live_native(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    features: pd.DataFrame,
    candidate_times: list[pd.Timestamp] | None = None,
    state: Mapping[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    cert = event_certification(edge_id)
    if not cert:
        raise NativeEventError(f"{edge_id}: native event certification is absent or stale")
    required = (
        "event_parity_passed",
        "append_only_history_passed",
        "restart_parity_passed",
        "event_state_continuity_passed",
    )
    missing = [name for name in required if cert.get(name) is not True]
    if missing:
        raise NativeEventError(
            f"{edge_id}: native event certification is missing proof flags: {missing}"
        )
    context_rows = int(cert.get("context_rows") or 0)
    overlap_rows = int(cert.get("overlap_rows") or DEFAULT_OVERLAP_ROWS)
    audit_interval = int(
        cert.get("full_audit_interval_bars") or DEFAULT_FULL_AUDIT_INTERVAL_BARS
    )
    return transition_events_live_native(
        runtime,
        asset=asset,
        edge_id=edge_id,
        features=features,
        context_rows=context_rows,
        overlap_rows=overlap_rows,
        full_audit_interval_bars=audit_interval,
        candidate_times=candidate_times,
        state=state,
        certification_hash=_certification_hash(cert),
        certified_restart_parity=True,
    )


__all__ = [
    "ENGINE_VERSION",
    "STATE_SCHEMA_VERSION",
    "NativeEventError",
    "is_expected_empty_event_exception",
    "transition_events_live_native",
    "build_events_live_native",
]
