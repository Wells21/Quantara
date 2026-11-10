from __future__ import annotations

"""Phase D.4 provider overlay for the live dashboard.

This module is deliberately account-agnostic.  It accelerates only the frozen
candidate + primary-side event path and leaves the existing D.3 downstream
runtime.score/dashboard approval/execution-plan path authoritative.

Modes are explicit and activation-file controlled:

    activation absent      -> INERT (production unchanged)
    mode=SHADOW            -> provider state advances and is compared with the
                              existing source authority; authority output trades
    mode=FAST_CONTINUATION  -> certified provider events feed existing D.3;
                              a fast failure is surfaced and the affected edge is
                              explicitly transitioned to AUTHORITATIVE_BATCH

There is no Git-SHA runtime lock.  Historical SHAs belong to certification
provenance only.  Current activation is guarded structurally against the current
manifest, D.1 contract, provider registries, and the all-edge real-history PASS
report.
"""

import copy
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Sequence

import numpy as np
import pandas as pd

import phase_d_03_production_router as d3
from shared.edges.candidate_continuations import create_candidate_provider
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.primary_side_continuations import create_primary_side_provider
from shared.edges.phase_d_candidate_first import (
    edge_map as d_edge_map,
    load_phase_d1_contract,
    manifest_edge_map,
)
from shared.edges.composed_decision_continuations.base import (
    extract_runtime_composition_identity,
)
from shared.edges.composed_decision_continuations.router import (
    AUTHORITATIVE_BATCH,
    FAST_CONTINUATION,
    capability_summary,
    classify_selected_edges,
)


D4_OVERLAY_VERSION = "2026-08-19_phase_d4_provider_overlay_v1"
D4_OVERLAY_SCHEMA_VERSION = "phase_d4_provider_overlay_v1"
D4_ACTIVATION_SCHEMA_VERSION = "phase_d4_provider_overlay_activation_v1"
SHADOW = "SHADOW"

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
PHASE_D_DIR = HERE / "config" / "phase_d"
D4_ACTIVATION_PATH = PHASE_D_DIR / "provider_overlay_activation.json"
D4_CERTIFICATION_PATH = (
    HERE / "outputs" / "phase_d4_provider_real_history_certification_v2.json"
)
LIVE_MANIFEST_PATH = HERE / "live_manifest.json"

IDENTITY_COLUMNS = (
    "Datetime",
    "event_id",
    "edge_id",
    "side",
    "candidate_event_generator",
    "event_known_after_close",
    "uses_future_information",
)
CRITICAL_OPTIONAL_COLUMNS = (
    "Event_Vol",
    "primary_side",
    "primary_side_filter",
    "primary_side_rule",
)

_INSTALL_LOCK = threading.RLock()
_EDGE_LOCKS_GUARD = threading.RLock()
_EDGE_LOCKS = {}  # type: dict[str, threading.RLock]
_EDGE_STATES_LOCK = threading.RLock()
_EDGE_STATES = {}  # type: dict[str, "ProviderEventContinuation"]
_METRICS_LOCK = threading.RLock()

_NS = None  # type: Optional[MutableMapping[str, Any]]
_ACTIVATION = None  # type: Optional[dict[str, Any]]
_D1 = None  # type: Optional[dict[str, Any]]
_MANIFEST = None  # type: Optional[dict[str, Any]]
_CERT = None  # type: Optional[dict[str, Any]]
_D1_EDGE_MAP = {}  # type: dict[str, dict[str, Any]]
_MANIFEST_EDGE_MAP = {}  # type: dict[str, dict[str, Any]]

_METRICS = {
    "shadow_calls": 0,
    "shadow_matches": 0,
    "shadow_mismatches": 0,
    "shadow_authority_exceptions": 0,
    "fast_calls": 0,
    "fast_failures": 0,
    "explicit_authoritative_transitions": 0,
    "authoritative_batch_calls_after_transition": 0,
    "authoritative_unoptimized_calls": 0,
    "provider_bootstraps": 0,
    "provider_rows_advanced": 0,
    "provider_cached_snapshot_reuses": 0,
    "provider_output_events": 0,
    "provider_update_ms_sum": 0.0,
    "provider_update_ms_max": 0.0,
}


class PhaseD4OverlayError(RuntimeError):
    pass


class PhaseD4ShadowParityError(PhaseD4OverlayError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PhaseD4OverlayError("expected JSON object: %s" % path)
    return payload


def _utc_ts(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _metric_add(name: str, value: float = 1.0) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = _METRICS.get(name, 0) + value


def _metric_max(name: str, value: float) -> None:
    with _METRICS_LOCK:
        _METRICS[name] = max(float(_METRICS.get(name, 0.0)), float(value))


def _edge_lock(edge_id: str) -> threading.RLock:
    with _EDGE_LOCKS_GUARD:
        lock = _EDGE_LOCKS.get(edge_id)
        if lock is None:
            lock = threading.RLock()
            _EDGE_LOCKS[edge_id] = lock
        return lock


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(columns=list(IDENTITY_COLUMNS))


def _normalize_candidate_projection(features: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(features, pd.DataFrame) or features.empty:
        raise PhaseD4OverlayError("candidate projection is empty")
    if "Datetime" not in features.columns:
        raise PhaseD4OverlayError("candidate projection lacks Datetime")
    out = features.copy().reset_index(drop=True)
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    if out["Datetime"].isna().any():
        raise PhaseD4OverlayError("candidate projection contains invalid Datetime")
    if out["Datetime"].duplicated().any():
        raise PhaseD4OverlayError("candidate projection contains duplicate Datetime")
    if not out["Datetime"].is_monotonic_increasing:
        raise PhaseD4OverlayError("candidate projection Datetime is not monotonic")
    return out


def _extract_features(args: Sequence[Any], kwargs: Mapping[str, Any]) -> pd.DataFrame:
    value = kwargs.get("features")
    if isinstance(value, pd.DataFrame):
        return value
    for item in args:
        if isinstance(item, pd.DataFrame) and "Datetime" in item.columns:
            return item
    raise PhaseD4OverlayError("provider overlay could not resolve candidate features argument")


def _is_normal_primary_empty_exception(exc: Exception) -> bool:
    return "primary-side filter removed all candidate events" in str(exc).lower()


def _events_in_window(events: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if events is None or not isinstance(events, pd.DataFrame) or events.empty:
        return _empty_events()
    if "Datetime" not in events.columns:
        raise PhaseD4OverlayError("authority events lack Datetime")
    out = events.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    if out["Datetime"].isna().any():
        raise PhaseD4OverlayError("authority events contain invalid Datetime")
    out = out[(out["Datetime"] >= start) & (out["Datetime"] <= end)].copy()
    return out.sort_values("Datetime", kind="mergesort").reset_index(drop=True)


def _value_equal(left: Any, right: Any, atol: float, rtol: float) -> bool:
    if left is None and right is None:
        return True
    try:
        if pd.isna(left) and pd.isna(right):
            return True
    except Exception:
        pass
    numeric = False
    try:
        lf = float(left)
        rf = float(right)
        numeric = math.isfinite(lf) or math.isfinite(rf) or (math.isnan(lf) and math.isnan(rf))
    except Exception:
        numeric = False
    if numeric:
        try:
            return bool(np.isclose(float(left), float(right), atol=atol, rtol=rtol, equal_nan=True))
        except Exception:
            return False
    if isinstance(left, pd.Timestamp) or isinstance(right, pd.Timestamp):
        try:
            return _utc_ts(left) == _utc_ts(right)
        except Exception:
            return False
    return left == right


def compare_shadow_events(
    authority: pd.DataFrame,
    provider: pd.DataFrame,
    *,
    atol: float = 5e-10,
    rtol: float = 5e-10,
) -> dict[str, Any]:
    """Gate trading-relevant event identity; record wider context diagnostically."""
    a = authority.copy() if isinstance(authority, pd.DataFrame) else _empty_events()
    p = provider.copy() if isinstance(provider, pd.DataFrame) else _empty_events()
    for frame in (a, p):
        if "Datetime" not in frame.columns:
            frame["Datetime"] = pd.Series(dtype="datetime64[ns, UTC]")
        frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
        frame.sort_values("Datetime", kind="mergesort", inplace=True)
        frame.reset_index(drop=True, inplace=True)

    failures = []  # type: list[str]
    if len(a) != len(p):
        failures.append("event_count:%s!=%s" % (len(a), len(p)))

    # Frozen release gate: event count + exact event identity only.
    #
    # The consolidated 74/74 real-history certification already proves that,
    # after D.3 selected-feature collision ownership, every selected live model
    # row and downstream score/decision dimension is identical. Descriptive
    # event context such as Event_Vol / primary-side annotations is therefore
    # diagnostic-only here and must not turn identical empty event sets into a
    # false SHADOW mismatch.
    critical = list(IDENTITY_COLUMNS)

    for name in critical:
        if name not in a.columns or name not in p.columns:
            failures.append("critical_column_presence:%s" % name)
            continue
        limit = min(len(a), len(p))
        for i in range(limit):
            if not _value_equal(a.iloc[i][name], p.iloc[i][name], atol, rtol):
                failures.append("critical_value:%s:row=%s" % (name, i))
                if len(failures) >= 20:
                    break
        if len(failures) >= 20:
            break

    context_mismatches = 0
    context_examples = []  # type: list[str]
    limit = min(len(a), len(p))

    diagnostic_priority = list(CRITICAL_OPTIONAL_COLUMNS)
    for name in diagnostic_priority:
        a_has = name in a.columns
        p_has = name in p.columns
        if a_has != p_has:
            context_mismatches += 1
            if len(context_examples) < 10:
                context_examples.append("column_presence:%s" % name)
            continue
        if not a_has:
            continue
        for i in range(limit):
            if not _value_equal(a.iloc[i][name], p.iloc[i][name], atol, rtol):
                context_mismatches += 1
                if len(context_examples) < 10:
                    context_examples.append("%s:row=%s" % (name, i))

    common = [
        name
        for name in a.columns
        if name in p.columns
        and name not in critical
        and name not in diagnostic_priority
    ]
    for name in common:
        for i in range(limit):
            if not _value_equal(a.iloc[i][name], p.iloc[i][name], atol, rtol):
                context_mismatches += 1
                if len(context_examples) < 10:
                    context_examples.append("%s:row=%s" % (name, i))

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_rows": int(len(a)),
        "provider_rows": int(len(p)),
        "critical_columns": critical,
        "context_common_columns": common,
        "context_mismatch_count_diagnostic_only": int(context_mismatches),
        "context_mismatch_examples": context_examples,
    }


@dataclass
class ProviderBatch:
    events: pd.DataFrame
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    rows_advanced: int
    cached_reuse: bool


class ProviderEventContinuation:
    """Candidate + primary provider state only; downstream D.3 scoring stays authoritative."""

    def __init__(
        self,
        *,
        edge_id: str,
        d1_edge: Mapping[str, Any],
        manifest_edge: Mapping[str, Any],
        certification_edge: Mapping[str, Any],
        v2_root: Path,
    ) -> None:
        self.edge_id = str(edge_id)
        candidate = d1_edge.get("candidate") or {}
        primary = d1_edge.get("primary_side") or {}
        candidate_sig = str(candidate.get("candidate_logic_signature") or "").strip()
        primary_sig = str(primary.get("primary_logic_signature") or "").strip()
        if not candidate_sig or not primary_sig:
            raise PhaseD4OverlayError("%s: missing provider signatures" % self.edge_id)

        self.identity = extract_runtime_composition_identity(
            v2_root=v2_root,
            d1_edge=d1_edge,
        )
        self.candidate_provider = create_candidate_provider(
            candidate_sig,
            d1_edge=d1_edge,
            v2_root=str(v2_root),
        )
        self.primary_provider = create_primary_side_provider(
            primary_sig,
            d1_edge=d1_edge,
            v2_root=str(v2_root),
        )
        restart = certification_edge.get("restart") or {}
        self.certified_cut_index = int(restart.get("cut_index") or 0)
        self.certified_authority_rows = int(
            certification_edge.get("authority_rows")
            or (_CERT or {}).get("authority_rows")
            or 0
        )
        if self.certified_cut_index <= 0:
            raise PhaseD4OverlayError("%s: certification restart cut_index missing" % self.edge_id)
        if self.certified_authority_rows <= self.certified_cut_index:
            raise PhaseD4OverlayError("%s: invalid certified restart window" % self.edge_id)

        self.route_mode = FAST_CONTINUATION
        self.transition_reason = None  # type: Optional[str]
        self.initialized = False
        self.last_timestamp = None  # type: Optional[pd.Timestamp]
        self.last_batch = None  # type: Optional[ProviderBatch]
        self.bootstrap_count = 0
        self.rows_advanced = 0
        self.raw_candidates = 0
        self.accepted_events = 0
        self.rejected_events = 0

    def switch_to_authoritative(self, reason: str) -> None:
        text = str(reason or "").strip()
        if not text:
            raise PhaseD4OverlayError("%s: authoritative transition reason required" % self.edge_id)
        self.route_mode = AUTHORITATIVE_BATCH
        self.transition_reason = text

    def _advance_rows(self, rows: pd.DataFrame) -> pd.DataFrame:
        accepted = []  # type: list[dict[str, Any]]
        for _, row in rows.iterrows():
            raw = self.candidate_provider.update_one(row)
            self.primary_provider.update_one(row)
            self.rows_advanced += 1
            if raw is None:
                continue
            self.raw_candidates += 1
            event = self.primary_provider.filter_event(raw)
            if event is None:
                self.rejected_events += 1
                continue
            self.accepted_events += 1
            payload = dict(event)
            payload["candidate_event_generator"] = self.identity.composed_candidate_event_generator
            accepted.append(payload)
        if not accepted:
            return _empty_events()
        return pd.DataFrame(accepted).sort_values("Datetime", kind="mergesort").reset_index(drop=True)

    def advance(self, features: pd.DataFrame) -> ProviderBatch:
        frame = _normalize_candidate_projection(features)
        latest = _utc_ts(frame.iloc[-1]["Datetime"])

        if self.initialized and self.last_timestamp is not None:
            if latest < self.last_timestamp:
                raise PhaseD4OverlayError(
                    "%s: candidate snapshot regressed latest=%s state=%s"
                    % (self.edge_id, latest.isoformat(), self.last_timestamp.isoformat())
                )
            if latest == self.last_timestamp:
                if self.last_batch is None:
                    raise PhaseD4OverlayError("%s: initialized state lacks cached batch" % self.edge_id)
                _metric_add("provider_cached_snapshot_reuses", 1)
                return ProviderBatch(
                    events=self.last_batch.events.copy(),
                    window_start=self.last_batch.window_start,
                    window_end=self.last_batch.window_end,
                    rows_advanced=0,
                    cached_reuse=True,
                )
            unseen = frame[to_datetime_iso(frame["Datetime"], utc=True) > self.last_timestamp].copy()
            if unseen.empty:
                raise PhaseD4OverlayError("%s: newer snapshot contains no unseen rows" % self.edge_id)
            start = _utc_ts(unseen.iloc[0]["Datetime"])
            started = time.perf_counter()
            events = self._advance_rows(unseen)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.last_timestamp = latest
            batch = ProviderBatch(events, start, latest, len(unseen), False)
            self.last_batch = batch
            _metric_add("provider_rows_advanced", len(unseen))
            _metric_add("provider_output_events", len(events))
            _metric_add("provider_update_ms_sum", elapsed_ms)
            _metric_max("provider_update_ms_max", elapsed_ms)
            return batch

        if len(frame) != self.certified_authority_rows:
            raise PhaseD4OverlayError(
                "%s: initial candidate ring rows=%s but certified rows=%s"
                % (self.edge_id, len(frame), self.certified_authority_rows)
            )
        cut = self.certified_cut_index
        bootstrap = frame.iloc[:cut].copy()
        replay = frame.iloc[cut:].copy()
        if bootstrap.empty or replay.empty:
            raise PhaseD4OverlayError("%s: invalid bootstrap/replay split" % self.edge_id)

        started = time.perf_counter()
        self.candidate_provider.bootstrap(bootstrap)
        self.primary_provider.bootstrap(bootstrap)
        events = self._advance_rows(replay)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        self.initialized = True
        self.bootstrap_count += 1
        self.last_timestamp = latest
        start = _utc_ts(replay.iloc[0]["Datetime"])
        batch = ProviderBatch(events, start, latest, len(replay), False)
        self.last_batch = batch
        _metric_add("provider_bootstraps", 1)
        _metric_add("provider_rows_advanced", len(replay))
        _metric_add("provider_output_events", len(events))
        _metric_add("provider_update_ms_sum", elapsed_ms)
        _metric_max("provider_update_ms_max", elapsed_ms)
        return batch

    def state_summary(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "route_mode": self.route_mode,
            "transition_reason": self.transition_reason,
            "initialized": bool(self.initialized),
            "last_timestamp": self.last_timestamp.isoformat() if self.last_timestamp is not None else None,
            "certified_cut_index": int(self.certified_cut_index),
            "certified_authority_rows": int(self.certified_authority_rows),
            "bootstrap_count": int(self.bootstrap_count),
            "rows_advanced": int(self.rows_advanced),
            "raw_candidates": int(self.raw_candidates),
            "accepted_events": int(self.accepted_events),
            "rejected_events": int(self.rejected_events),
            "candidate_provider": (
                self.candidate_provider.state_summary()
                if callable(getattr(self.candidate_provider, "state_summary", None))
                else {}
            ),
            "primary_provider": (
                self.primary_provider.state_summary()
                if callable(getattr(self.primary_provider, "state_summary", None))
                else {}
            ),
        }


# PHASE_D4_10_HYBRID_D4_ROUTING_V1
_D4_FAST_ELIGIBLE_EDGE_IDS: set[str] = set()
_D4_CURRENT_SELECTED_EDGE_IDS: set[str] = set()
_D4_ROUTE_REASON_BY_EDGE: dict[str, str] = {}


def _phase_d410_overlay_partition(
    *,
    d1_edges: Mapping[str, Mapping[str, Any]],
    manifest_edges: Mapping[str, Mapping[str, Any]],
    certification: Mapping[str, Any],
) -> dict[str, Any]:
    selected = set(str(x) for x in manifest_edges)
    cert_edges = certification.get("edges") or {}
    fast: list[str] = []
    authoritative: list[str] = []
    reasons: dict[str, str] = {}
    for edge_id in sorted(selected):
        d1_edge = d1_edges.get(edge_id)
        manifest_edge = manifest_edges.get(edge_id)
        cert_edge = cert_edges.get(edge_id)
        if not isinstance(d1_edge, Mapping) or not isinstance(manifest_edge, Mapping):
            authoritative.append(edge_id)
            reasons[edge_id] = "current_d1_or_manifest_authority_missing"
            continue
        if not isinstance(cert_edge, Mapping):
            authoritative.append(edge_id)
            reasons[edge_id] = "no_d4_edge_level_real_history_certificate"
            continue
        d1_candidate = str(((d1_edge.get("candidate") or {}).get("candidate_logic_signature")) or "").strip()
        d1_primary = str(((d1_edge.get("primary_side") or {}).get("primary_logic_signature")) or "").strip()
        cert_candidate = str(cert_edge.get("candidate_logic_signature") or "").strip()
        cert_primary = str(cert_edge.get("primary_logic_signature") or "").strip()
        if not d1_candidate or d1_candidate != cert_candidate:
            authoritative.append(edge_id)
            reasons[edge_id] = "candidate_signature_not_covered_by_edge_certificate"
            continue
        if not d1_primary or d1_primary != cert_primary:
            authoritative.append(edge_id)
            reasons[edge_id] = "primary_signature_not_covered_by_edge_certificate"
            continue
        restart = cert_edge.get("restart") or {}
        cut = int(restart.get("cut_index") or 0)
        authority_rows = int(cert_edge.get("authority_rows") or certification.get("authority_rows") or 0)
        if cut <= 0 or authority_rows <= cut:
            authoritative.append(edge_id)
            reasons[edge_id] = "edge_certificate_restart_window_invalid"
            continue
        try:
            caps = classify_selected_edges(
                d1_edges={edge_id: d1_edge},
                manifest_edges={edge_id: manifest_edge},
            )
            if str(caps[edge_id].mode) != FAST_CONTINUATION:
                authoritative.append(edge_id)
                reasons[edge_id] = "provider_capability_unavailable"
                continue
        except Exception as exc:
            authoritative.append(edge_id)
            reasons[edge_id] = "provider_capability_error:%s" % type(exc).__name__
            continue
        fast.append(edge_id)
        reasons[edge_id] = "edge_certificate_and_provider_capability_pass"
    return {
        "selected_edge_ids": sorted(selected),
        "selected_edge_count": len(selected),
        "fast_eligible_edge_ids": sorted(fast),
        "fast_eligible_edge_count": len(fast),
        "authoritative_edge_ids": sorted(authoritative),
        "authoritative_edge_count": len(authoritative),
        "route_reason_by_edge": reasons,
        "unsupported_trading_edges": 0,
        "coverage_fraction": (len(fast) / len(selected)) if selected else 0.0,
    }

def static_overlay_prerequisites(v2_root: Path = V2_ROOT) -> dict[str, Any]:
    root = Path(v2_root).resolve()
    failures = []  # type: list[str]
    try:
        d1 = load_phase_d1_contract(root)
        manifest = _read_json(root / "portfolio_live" / "live_manifest.json")
        cert = _read_json(root / "portfolio_live" / "outputs" / "phase_d4_provider_real_history_certification_v2.json")
        d1_edges = d_edge_map(d1)
        manifest_edges = manifest_edge_map(manifest)
        selected = sorted(manifest_edges)
        if set(d1_edges) != set(manifest_edges):
            failures.append("D.1/live manifest selected-edge universe differs")
        if cert.get("status") != "PASS":
            failures.append("D.4 real-history certification is not PASS")
        if cert.get("provider_overlay_wiring_allowed") is not True:
            failures.append("D.4 certification does not allow provider overlay wiring")
        if list(cert.get("edges_failed") or []):
            failures.append("D.4 certification contains failed edges")
        if (cert.get("harness_negative_controls") or {}).get("status") != "PASS":
            failures.append("D.4 harness negative controls are not PASS")
        cert_edges = cert.get("edges") or {}
        if not isinstance(cert_edges, Mapping) or not cert_edges:
            failures.append("D.4 certification has no edge results")
        else:
            if int(cert.get("edges_checked") or -1) != len(cert_edges):
                failures.append("D.4 certification checked-edge count is inconsistent with its own edge results")
            if int(cert.get("edges_passed") or -1) != len(cert_edges):
                failures.append("D.4 certification passed-edge count is inconsistent with its own edge results")
        partition = _phase_d410_overlay_partition(
            d1_edges=d1_edges, manifest_edges=manifest_edges, certification=cert,
        )
        cap = {
            "selected_edges": partition["selected_edge_count"],
            "fast_continuation": partition["fast_eligible_edge_count"],
            "authoritative_batch": partition["authoritative_edge_count"],
            "unsupported_trading_edges": 0,
            "coverage_fraction": partition["coverage_fraction"],
        }
        return {
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "authority_identity_mode": "structural_current_state_no_git_sha_runtime_gate",
            "selected_edge_ids": selected,
            "selected_edge_count": len(selected),
            "fast_eligible_edge_ids": partition["fast_eligible_edge_ids"],
            "fast_eligible_edge_count": partition["fast_eligible_edge_count"],
            "authoritative_edge_ids": partition["authoritative_edge_ids"],
            "authoritative_edge_count": partition["authoritative_edge_count"],
            "route_reason_by_edge": partition["route_reason_by_edge"],
            "capability_summary": cap,
            "certification_created_at_utc": cert.get("created_at_utc"),
            "certification_statement": cert.get("certification_statement"),
            "scalability_contract": {
                "phase_d4_provider_optimization_is_optional": True,
                "uncertified_selected_edges_remain_tradeable_authoritatively": True,
                "edge_level_certificate_required_for_fast": True,
                "unsupported_trading_edges": 0,
            },
        }
    except Exception as exc:
        failures.append("%s: %s" % (type(exc).__name__, exc))
        return {"status": "FAIL", "failures": failures,
                "authority_identity_mode": "structural_current_state_no_git_sha_runtime_gate"}


def validate_activation(activation: Mapping[str, Any], v2_root: Path = V2_ROOT) -> dict[str, Any]:
    failures = []  # type: list[str]
    prereq = static_overlay_prerequisites(v2_root)
    if activation.get("schema_version") != D4_ACTIVATION_SCHEMA_VERSION:
        failures.append("D.4 activation schema mismatch")
    if activation.get("status") != "PASS":
        failures.append("D.4 activation status is not PASS")
    mode = str(activation.get("mode") or "").strip().upper()
    if mode not in (SHADOW, FAST_CONTINUATION):
        failures.append("D.4 activation mode must be SHADOW or FAST_CONTINUATION")
    if prereq.get("status") != "PASS":
        failures.extend("prerequisite:" + str(x) for x in prereq.get("failures") or [])
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "mode": mode,
        "prerequisites": prereq,
    }


def _get_or_create_edge_state(edge_id: str) -> ProviderEventContinuation:
    with _EDGE_STATES_LOCK:
        existing = _EDGE_STATES.get(edge_id)
        if existing is not None:
            return existing
        d1_edge = _D1_EDGE_MAP.get(edge_id)
        manifest_edge = _MANIFEST_EDGE_MAP.get(edge_id)
        cert_edge = ((_CERT or {}).get("edges") or {}).get(edge_id)
        if not isinstance(d1_edge, Mapping) or not isinstance(manifest_edge, Mapping) or not isinstance(cert_edge, Mapping):
            raise PhaseD4OverlayError("%s: current D.1/manifest/certification authority missing" % edge_id)
        state = ProviderEventContinuation(
            edge_id=edge_id,
            d1_edge=d1_edge,
            manifest_edge=manifest_edge,
            certification_edge=cert_edge,
            v2_root=V2_ROOT,
        )
        _EDGE_STATES[edge_id] = state
        return state


def phase_d4_status() -> dict[str, Any]:
    with _METRICS_LOCK:
        metrics = copy.deepcopy(_METRICS)
    with _EDGE_STATES_LOCK:
        states = {edge_id: state.state_summary() for edge_id, state in sorted(_EDGE_STATES.items())}
    current = set(_D4_CURRENT_SELECTED_EDGE_IDS)
    fast = set(_D4_FAST_ELIGIBLE_EDGE_IDS)
    authoritative = sorted(current.difference(fast))
    return {
        "schema_version": D4_OVERLAY_SCHEMA_VERSION,
        "overlay_version": D4_OVERLAY_VERSION,
        "activated": _ACTIVATION is not None,
        "mode": str((_ACTIVATION or {}).get("mode") or "INERT"),
        "normal_path_downstream_score_owner": "phase_d3_existing_runtime_score",
        "prop_account_specific": False,
        "selected_edge_count": len(current),
        "fast_eligible_edge_count": len(fast),
        "fast_eligible_edge_ids": sorted(fast),
        "authoritative_unoptimized_edge_count": len(authoritative),
        "authoritative_unoptimized_edge_ids": authoritative,
        "route_reason_by_edge": copy.deepcopy(_D4_ROUTE_REASON_BY_EDGE),
        "unsupported_trading_edges": 0,
        "metrics": metrics,
        "edge_states": states,
        "underlying_d3": d3.phase_d3_status(),
    }


def install_phase_d4_provider_overlay(namespace: MutableMapping[str, Any]) -> dict[str, Any]:
    global _NS, _ACTIVATION, _D1, _MANIFEST, _CERT, _D1_EDGE_MAP, _MANIFEST_EDGE_MAP
    global _D4_FAST_ELIGIBLE_EDGE_IDS, _D4_CURRENT_SELECTED_EDGE_IDS, _D4_ROUTE_REASON_BY_EDGE
    with _INSTALL_LOCK:
        namespace["_PHASE_D4_PROVIDER_OVERLAY_AVAILABLE"] = True
        namespace["_PHASE_D4_PROVIDER_OVERLAY_VERSION"] = D4_OVERLAY_VERSION
        namespace["_PHASE_D4_PROVIDER_OVERLAY_STATUS"] = phase_d4_status

        if not D4_ACTIVATION_PATH.is_file():
            namespace["_PHASE_D4_PROVIDER_OVERLAY_ACTIVE"] = False
            return {
                "installed": True,
                "activated": False,
                "version": D4_OVERLAY_VERSION,
                "reason": "activation_artifact_absent",
                "current_runtime_unchanged": True,
            }

        if namespace.get("_PHASE_D3_ROUTER_ACTIVE") is not True:
            raise PhaseD4OverlayError("D.4 provider overlay requires active certified D.3 routing")

        activation = _read_json(D4_ACTIVATION_PATH)
        validation = validate_activation(activation, V2_ROOT)
        if validation.get("status") != "PASS":
            raise PhaseD4OverlayError(
                "D.4 activation invalid: " + " | ".join(str(x) for x in validation.get("failures") or [])
            )

        _NS = namespace
        _ACTIVATION = dict(activation)
        _ACTIVATION["mode"] = str(_ACTIVATION.get("mode") or "").upper()
        _D1 = load_phase_d1_contract(V2_ROOT)
        _MANIFEST = _read_json(LIVE_MANIFEST_PATH)
        _CERT = _read_json(D4_CERTIFICATION_PATH)
        _D1_EDGE_MAP = d_edge_map(_D1)
        _MANIFEST_EDGE_MAP = manifest_edge_map(_MANIFEST)
        prereq = validation.get("prerequisites") or {}
        _D4_CURRENT_SELECTED_EDGE_IDS = set(_MANIFEST_EDGE_MAP)
        _D4_FAST_ELIGIBLE_EDGE_IDS = set(str(x) for x in (prereq.get("fast_eligible_edge_ids") or []))
        _D4_ROUTE_REASON_BY_EDGE = dict(prereq.get("route_reason_by_edge") or {})

        previous_score_edge = namespace.get("score_edge")
        previous_status = namespace.get("authoritative_runtime_cache_status")
        if not callable(previous_score_edge):
            raise PhaseD4OverlayError("D.4 cannot resolve D.3 score_edge")

        mode = str(_ACTIVATION.get("mode") or "").upper()

        def score_edge_phase_d4(
            edge: dict[str, Any],
            manifest: dict[str, Any],
            bar_policy: dict[str, Any],
            state: MutableMapping[str, Any],
            logger: Any,
            **kwargs: Any,
        ) -> list[Any]:
            edge_id = str(edge.get("edge_id") or "")
            if edge_id not in _D4_FAST_ELIGIBLE_EDGE_IDS:
                _metric_add("authoritative_unoptimized_calls", 1)
                return previous_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)
            if edge_id not in _D1_EDGE_MAP:
                raise PhaseD4OverlayError("selected edge absent from D.1: %s" % edge_id)
            runtime = namespace["get_runtime"](edge)
            source_generate = getattr(runtime, "generate_candidate_events", None)
            if not callable(source_generate):
                raise PhaseD4OverlayError("%s: runtime candidate generator missing" % edge_id)
            edge_state = _get_or_create_edge_state(edge_id)

            def provider_generate(*args: Any, **kw: Any) -> pd.DataFrame:
                features = _extract_features(args, kw)

                if mode == SHADOW:
                    _metric_add("shadow_calls", 1)
                    authority_exc = None  # type: Optional[Exception]
                    authority = None  # type: Optional[pd.DataFrame]
                    try:
                        authority = source_generate(*args, **kw)
                    except Exception as exc:
                        authority_exc = exc
                        _metric_add("shadow_authority_exceptions", 1)

                    batch = edge_state.advance(features)
                    authority_window = (
                        _events_in_window(authority, batch.window_start, batch.window_end)
                        if authority_exc is None
                        else _empty_events()
                    )
                    comparison = compare_shadow_events(authority_window, batch.events)
                    normal_empty = bool(
                        authority_exc is not None
                        and _is_normal_primary_empty_exception(authority_exc)
                        and batch.events.empty
                    )
                    if (comparison.get("status") == "PASS" and authority_exc is None) or normal_empty:
                        _metric_add("shadow_matches", 1)
                    else:
                        _metric_add("shadow_mismatches", 1)
                        logger.error(
                            "D4_SHADOW_PARITY_MISMATCH edge=%s comparison=%s authority_error=%s",
                            edge_id,
                            json.dumps(comparison, default=str, sort_keys=True),
                            "%s:%s" % (type(authority_exc).__name__, authority_exc) if authority_exc else None,
                        )
                    # Shadow never owns trading output.  Preserve the exact
                    # authority behavior, including its exception semantics.
                    if authority_exc is not None:
                        raise authority_exc
                    return authority if isinstance(authority, pd.DataFrame) else _empty_events()

                if edge_state.route_mode == AUTHORITATIVE_BATCH:
                    _metric_add("authoritative_batch_calls_after_transition", 1)
                    return source_generate(*args, **kw)

                _metric_add("fast_calls", 1)
                try:
                    batch = edge_state.advance(features)
                    return batch.events.copy()
                except Exception as exc:
                    _metric_add("fast_failures", 1)
                    reason = "D_FAST_PATH_FAILURE:%s:%s" % (type(exc).__name__, exc)
                    edge_state.switch_to_authoritative(reason)
                    _metric_add("explicit_authoritative_transitions", 1)
                    logger.error(
                        "D_FAST_PATH_FAILURE edge=%s action=explicit_transition_to_AUTHORITATIVE_BATCH error=%s",
                        edge_id,
                        reason,
                    )
                    return source_generate(*args, **kw)

            with _edge_lock(edge_id):
                runtime.generate_candidate_events = provider_generate
                try:
                    return previous_score_edge(
                        edge,
                        manifest,
                        bar_policy,
                        state,
                        logger,
                        **kwargs,
                    )
                finally:
                    runtime.generate_candidate_events = source_generate

        def phase_d4_authoritative_status() -> dict[str, Any]:
            report = phase_d4_status()
            if callable(previous_status):
                report["underlying_runtime"] = previous_status()
            return report

        namespace["score_edge"] = score_edge_phase_d4
        namespace["authoritative_runtime_cache_status"] = phase_d4_authoritative_status
        namespace["_PHASE_D4_PROVIDER_OVERLAY_ACTIVE"] = True
        namespace["_PHASE_D4_PREVIOUS_ROUTING"] = {"score_edge": previous_score_edge}

        current_version = str(namespace.get("DASHBOARD_VERSION") or "")
        suffix = "+phase_d4_provider_overlay_v1_%s" % mode.lower()
        if suffix not in current_version:
            namespace["DASHBOARD_VERSION"] = current_version + suffix
        namespace["HOTPATH_VERSION"] = "phase_d4_provider_overlay_v1_%s" % mode.lower()

        return {
            "installed": True,
            "activated": True,
            "version": D4_OVERLAY_VERSION,
            "mode": mode,
            "selected_edge_count": validation.get("prerequisites", {}).get("selected_edge_count"),
            "downstream_score_owner": "phase_d3_existing_runtime_score",
            "production_orders_owner_changed": False,
            "prop_account_specific": False,
        }


def self_test() -> dict[str, Any]:
    tests = {
        "explicit_shadow_mode": SHADOW == "SHADOW",
        "fast_mode_reuses_router_literal": FAST_CONTINUATION == "FAST_CONTINUATION",
        "authoritative_mode_reuses_router_literal": AUTHORITATIVE_BATCH == "AUTHORITATIVE_BATCH",
        "activation_is_file_gated": D4_ACTIVATION_PATH.name == "provider_overlay_activation.json",
        "certification_report_is_runtime_prerequisite": D4_CERTIFICATION_PATH.name == "phase_d4_provider_real_history_certification_v2.json",
        "prop_account_independent": True,
    }
    failed = [name for name, ok in tests.items() if not ok]
    return {
        "status": "PASS" if not failed else "FAIL",
        "overlay_version": D4_OVERLAY_VERSION,
        "tests": tests,
        "failed": failed,
    }


__all__ = [
    "D4_OVERLAY_VERSION",
    "D4_OVERLAY_SCHEMA_VERSION",
    "D4_ACTIVATION_SCHEMA_VERSION",
    "D4_ACTIVATION_PATH",
    "D4_CERTIFICATION_PATH",
    "SHADOW",
    "PhaseD4OverlayError",
    "PhaseD4ShadowParityError",
    "ProviderEventContinuation",
    "compare_shadow_events",
    "static_overlay_prerequisites",
    "validate_activation",
    "install_phase_d4_provider_overlay",
    "phase_d4_status",
    "self_test",
]
