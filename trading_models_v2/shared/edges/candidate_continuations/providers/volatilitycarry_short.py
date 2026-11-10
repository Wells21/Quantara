from __future__ import annotations

from typing import Any, Mapping

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .volatilitycarry import VolatilityCarryCandidateContinuation

VOLATILITYCARRY_SHORT_SIGNATURE = "e109f9aad77744159b1fd3451d2aa6242736eff7d9e583fa48b6d2d2cd5d2859"

PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilitycarry_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        VOLATILITYCARRY_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}

def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"volatilitycarry_short D.1 constant missing: {name}"
        )
    return constants[name]

def create_provider(*, candidate_logic_signature: str, d1_edge: Mapping[str, Any], v2_root):
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(
            f"volatilitycarry_short provider does not support {signature}"
        )
    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider candidate signature mismatch")
    constants = candidate.get("semantic_constants") or {}
    identity = extract_candidate_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return VolatilityCarryCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        vol_lookback=int(_constant(constants, "VOL_LOOKBACK")),
        low_vol_quantile=float(_constant(constants, "LOW_VOL_QUANTILE")),
        drift_lookback=int(_constant(constants, "DRIFT_LOOKBACK")),
        drift_tstat_threshold=float(_constant(constants, "DRIFT_TSTAT_THRESHOLD")),
        max_activity_ratio=float(_constant(constants, "MAX_ACTIVITY_RATIO")),
        min_obs=int(_constant(constants, "MIN_OBS")),
        event_vol_lookback=int(_constant(constants, "EVENT_VOL_LOOKBACK")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
