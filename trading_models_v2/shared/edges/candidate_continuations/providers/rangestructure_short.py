from __future__ import annotations

from typing import Any, Mapping

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .rangestructure import RangeStructureCandidateContinuation

RANGESTRUCTURE_SHORT_SIGNATURE = "e340966b1c57f412413da423b75c0d9b5f7259117c3d9724ff75033bb240cd1f"

PROVIDER_SPEC = {
    "provider_id": "phase_d4_rangestructure_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        RANGESTRUCTURE_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}

def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"range-structure-short D.1 constant missing: {name}"
        )
    return constants[name]

def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> RangeStructureCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(
            f"range-structure-short provider does not support {signature}"
        )
    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider candidate signature mismatch")
    constants = candidate.get("semantic_constants") or {}
    identity = extract_candidate_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return RangeStructureCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        range_lookback=int(_constant(constants, "RANGE_LOOKBACK")),
        extreme_zone_threshold=float(_constant(constants, "EXTREME_ZONE_THRESHOLD")),
        structure_bias_lookback=int(_constant(constants, "STRUCTURE_BIAS_LOOKBACK")),
        structure_bias_tstat_threshold=float(_constant(constants, "STRUCTURE_BIAS_TSTAT_THRESHOLD")),
        max_mid_range_position=float(_constant(constants, "MAX_MID_RANGE_POSITION")),
        min_obs=int(_constant(constants, "MIN_OBS")),
        event_vol_lookback=int(_constant(constants, "EVENT_VOL_LOOKBACK")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
    )
