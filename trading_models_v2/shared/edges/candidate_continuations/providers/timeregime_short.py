from __future__ import annotations

"""Exact incremental timeregime_short candidate continuation for Phase D.4."""

from typing import Any, Mapping

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .timeregime import TimeRegimeCandidateContinuation


TIMEREGIME_SHORT_SIGNATURE = (
    "bc20f97fb481e22d7c3b92d1821a0762218d78c29200a8de3aa3a5393628dda2"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_timeregime_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        TIMEREGIME_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}


class TimeRegimeShortCandidateContinuation(TimeRegimeCandidateContinuation):
    def state_summary(self) -> dict[str, Any]:
        out = dict(super().state_summary())
        out["provider_id"] = PROVIDER_SPEC["provider_id"]
        out["provider_version"] = PROVIDER_SPEC["provider_version"]
        return out


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"timeregime_short D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> TimeRegimeShortCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature != TIMEREGIME_SHORT_SIGNATURE:
        raise CandidateContinuationError(
            f"timeregime_short provider does not support {signature}"
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
            "timeregime_short source side identity changed"
        )

    return TimeRegimeShortCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        bias_window=int(_constant(constants, "BIAS_WINDOW")),
        time_tstat_threshold=float(
            _constant(constants, "TIME_TSTAT_THRESHOLD")
        ),
        volatility_ratio_threshold=float(
            _constant(constants, "VOLATILITY_RATIO_THRESHOLD")
        ),
        min_obs=int(_constant(constants, "MIN_OBS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        event_vol_lookback=int(_constant(constants, "EVENT_VOL_LOOKBACK")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
