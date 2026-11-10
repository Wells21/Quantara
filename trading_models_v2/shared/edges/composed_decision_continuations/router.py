from __future__ import annotations

"""Explicit scalable Phase D capability routing.

FAST_CONTINUATION is an accelerator. AUTHORITATIVE_BATCH is a valid compatibility
mode owned by the pre-existing dashboard/orchestrator path. Fast failures are
surfaced; this module never performs hidden fast->batch fallback.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from shared.edges.candidate_continuations import discover_candidate_providers
from shared.edges.primary_side_continuations import discover_primary_side_providers

from .runtime import create_composed_decision_continuation


FAST_CONTINUATION = "FAST_CONTINUATION"
AUTHORITATIVE_BATCH = "AUTHORITATIVE_BATCH"


class PhaseDRouterError(RuntimeError):
    pass


class PhaseDFastPathFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class EdgeCapability:
    edge_id: str
    mode: str
    reason: str
    candidate_logic_signature: str
    primary_logic_signature: str
    candidate_provider_available: bool
    primary_provider_available: bool


def classify_edge_capability(
    *,
    d1_edge: Mapping[str, Any],
    manifest_edge: Mapping[str, Any],
    candidate_registry: Optional[Mapping[str, Any]] = None,
    primary_registry: Optional[Mapping[str, Any]] = None,
) -> EdgeCapability:
    edge_id = str(d1_edge.get("edge_id") or "").strip()
    manifest_edge_id = str(manifest_edge.get("edge_id") or "").strip()
    if not edge_id or edge_id != manifest_edge_id:
        raise PhaseDRouterError(
            f"D.1/manifest edge identity mismatch: {edge_id!r} vs {manifest_edge_id!r}"
        )

    candidate_sig = str(
        ((d1_edge.get("candidate") or {}).get("candidate_logic_signature")) or ""
    ).strip()
    primary_sig = str(
        ((d1_edge.get("primary_side") or {}).get("primary_logic_signature")) or ""
    ).strip()

    c_registry = (
        discover_candidate_providers()
        if candidate_registry is None else candidate_registry
    )
    p_registry = (
        discover_primary_side_providers()
        if primary_registry is None else primary_registry
    )
    c_ok = bool(candidate_sig) and candidate_sig in c_registry
    p_ok = bool(primary_sig) and primary_sig in p_registry

    if c_ok and p_ok:
        mode = FAST_CONTINUATION
        reason = "candidate_and_primary_continuations_available"
    else:
        mode = AUTHORITATIVE_BATCH
        missing: list[str] = []
        if not candidate_sig:
            missing.append("candidate_signature_missing")
        elif not c_ok:
            missing.append("candidate_provider_unavailable")
        if not primary_sig:
            missing.append("primary_signature_missing")
        elif not p_ok:
            missing.append("primary_provider_unavailable")
        reason = ",".join(missing) or "fast_path_not_eligible"

    return EdgeCapability(
        edge_id=edge_id,
        mode=mode,
        reason=reason,
        candidate_logic_signature=candidate_sig,
        primary_logic_signature=primary_sig,
        candidate_provider_available=bool(c_ok),
        primary_provider_available=bool(p_ok),
    )


class PhaseDEdgeRoute:
    def __init__(self, *, capability: EdgeCapability, fast_engine: Any = None):
        self.capability = capability
        self.edge_id = capability.edge_id
        self.mode = capability.mode
        self.fast_engine = fast_engine
        self.transition_reason: Optional[str] = None
        if self.mode == FAST_CONTINUATION and fast_engine is None:
            raise PhaseDRouterError(
                f"{self.edge_id}: FAST_CONTINUATION requires a fast engine"
            )
        if self.mode == AUTHORITATIVE_BATCH and fast_engine is not None:
            raise PhaseDRouterError(
                f"{self.edge_id}: AUTHORITATIVE_BATCH must not own a fast engine"
            )

    def fast_update_one(self, **kwargs: Any) -> Any:
        if self.mode != FAST_CONTINUATION or self.fast_engine is None:
            raise PhaseDRouterError(
                f"{self.edge_id}: fast_update_one called while mode={self.mode}"
            )
        try:
            return self.fast_engine.update_one(**kwargs)
        except Exception as exc:
            raise PhaseDFastPathFailure(
                f"{self.edge_id}: D_FAST_PATH_FAILURE: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def switch_to_authoritative(self, *, reason: str) -> None:
        text = str(reason or "").strip()
        if not text:
            raise PhaseDRouterError(
                f"{self.edge_id}: authoritative transition reason is required"
            )
        self.mode = AUTHORITATIVE_BATCH
        self.fast_engine = None
        self.transition_reason = text

    def state_summary(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "mode": self.mode,
            "initial_mode": self.capability.mode,
            "capability_reason": self.capability.reason,
            "transition_reason": self.transition_reason,
            "candidate_logic_signature": self.capability.candidate_logic_signature,
            "primary_logic_signature": self.capability.primary_logic_signature,
            "candidate_provider_available": self.capability.candidate_provider_available,
            "primary_provider_available": self.capability.primary_provider_available,
            "hidden_authoritative_fallback": False,
        }


def create_edge_route(
    *,
    d1_edge: Mapping[str, Any],
    manifest_edge: Mapping[str, Any],
    v2_root: Union[str, Path],
    candidate_registry: Optional[Mapping[str, Any]] = None,
    primary_registry: Optional[Mapping[str, Any]] = None,
) -> PhaseDEdgeRoute:
    capability = classify_edge_capability(
        d1_edge=d1_edge,
        manifest_edge=manifest_edge,
        candidate_registry=candidate_registry,
        primary_registry=primary_registry,
    )
    if capability.mode == AUTHORITATIVE_BATCH:
        return PhaseDEdgeRoute(capability=capability)

    try:
        engine = create_composed_decision_continuation(
            d1_edge=d1_edge,
            manifest_edge=manifest_edge,
            v2_root=v2_root,
        )
    except Exception as exc:
        raise PhaseDRouterError(
            f"{capability.edge_id}: advertised fast path could not initialize: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return PhaseDEdgeRoute(capability=capability, fast_engine=engine)


def classify_selected_edges(
    *,
    d1_edges: Mapping[str, Mapping[str, Any]],
    manifest_edges: Mapping[str, Mapping[str, Any]],
    candidate_registry: Optional[Mapping[str, Any]] = None,
    primary_registry: Optional[Mapping[str, Any]] = None,
) -> dict[str, EdgeCapability]:
    c_registry = (
        discover_candidate_providers()
        if candidate_registry is None else candidate_registry
    )
    p_registry = (
        discover_primary_side_providers()
        if primary_registry is None else primary_registry
    )
    out: dict[str, EdgeCapability] = {}
    for edge_id, manifest_edge in manifest_edges.items():
        if edge_id not in d1_edges:
            raise PhaseDRouterError(
                f"Selected manifest edge missing from D.1: {edge_id}"
            )
        out[edge_id] = classify_edge_capability(
            d1_edge=d1_edges[edge_id],
            manifest_edge=manifest_edge,
            candidate_registry=c_registry,
            primary_registry=p_registry,
        )
    return out


def capability_summary(capabilities: Mapping[str, EdgeCapability]) -> dict[str, Any]:
    total = len(capabilities)
    fast = sum(1 for row in capabilities.values() if row.mode == FAST_CONTINUATION)
    batch = sum(
        1 for row in capabilities.values() if row.mode == AUTHORITATIVE_BATCH
    )
    return {
        "selected_edges": total,
        "fast_continuation": fast,
        "authoritative_batch": batch,
        "unsupported_trading_edges": 0,
        "coverage_fraction": (fast / total) if total else 0.0,
    }
