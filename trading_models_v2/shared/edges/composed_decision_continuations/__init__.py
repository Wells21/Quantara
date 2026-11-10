"""Phase D.4 inert composed decision continuation infrastructure."""

from .base import (
    ComposedDecisionContinuationError,
    RuntimeCompositionIdentity,
    extract_runtime_composition_identity,
)
from .resident import ResidentEdgeScorer
from .router import (
    AUTHORITATIVE_BATCH,
    FAST_CONTINUATION,
    EdgeCapability,
    PhaseDEdgeRoute,
    PhaseDFastPathFailure,
    PhaseDRouterError,
    capability_summary,
    classify_edge_capability,
    classify_selected_edges,
    create_edge_route,
)
from .runtime import (
    ComposedDecisionContinuation,
    build_current_live_model_row,
    create_composed_decision_continuation,
)

__all__ = [
    "AUTHORITATIVE_BATCH",
    "FAST_CONTINUATION",
    "ComposedDecisionContinuation",
    "EdgeCapability",
    "PhaseDEdgeRoute",
    "PhaseDFastPathFailure",
    "PhaseDRouterError",
    "ComposedDecisionContinuationError",
    "ResidentEdgeScorer",
    "RuntimeCompositionIdentity",
    "build_current_live_model_row",
    "capability_summary",
    "classify_edge_capability",
    "classify_selected_edges",
    "create_composed_decision_continuation",
    "create_edge_route",
    "extract_runtime_composition_identity",
]
