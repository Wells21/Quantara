from __future__ import annotations

"""Exact incremental volatilityregime_short candidate continuation for Phase D.4."""

from typing import Any, Mapping

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .volatilityregime import VolatilityRegimeCandidateContinuation


VOLATILITYREGIME_SHORT_SIGNATURE = (
    "6dae724563ca12ed930d2141330bd36f65009b638a7cf336d8f2f7abcd668d12"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilityregime_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        VOLATILITYREGIME_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}


class VolatilityRegimeShortCandidateContinuation(
    VolatilityRegimeCandidateContinuation
):
    def state_summary(self) -> dict[str, Any]:
        out = dict(super().state_summary())
        out["provider_id"] = PROVIDER_SPEC["provider_id"]
        out["provider_version"] = PROVIDER_SPEC["provider_version"]
        return out


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"volatilityregime_short D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> VolatilityRegimeShortCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature != VOLATILITYREGIME_SHORT_SIGNATURE:
        raise CandidateContinuationError(
            f"volatilityregime_short provider does not support {signature}"
        )

    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider signature mismatch")
    constants = candidate.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise CandidateContinuationError("D.1 semantic_constants missing")

    identity = extract_candidate_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    if int(identity.side_value) != -1:
        raise CandidateContinuationError(
            "volatilityregime_short source side identity changed"
        )

    return VolatilityRegimeShortCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        low_vol_quantile=float(_constant(constants, "LOW_VOL_QUANTILE")),
        expansion_multiplier=float(
            _constant(constants, "EXPANSION_MULTIPLIER")
        ),
        vol_lookback=int(_constant(constants, "VOL_LOOKBACK")),
        recent_low_vol_lookback=int(
            _constant(constants, "RECENT_LOW_VOL_LOOKBACK")
        ),
        event_vol_lookback=int(_constant(constants, "EVENT_VOL_LOOKBACK")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
