from __future__ import annotations

"""Exact incremental absorption_short candidate continuation for Phase D.4."""

from typing import Any, Mapping

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .absorption import AbsorptionCandidateContinuation


ABSORPTION_SHORT_SIGNATURE = (
    "040f38689962bfcacc7e6175b84d5a1a6b555599351cb1b733819debf94e6294"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_absorption_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        ABSORPTION_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}


class AbsorptionShortCandidateContinuation(AbsorptionCandidateContinuation):
    """Reuse the certified absorption state machine with the source short polarity."""

    def __init__(self, **kwargs: Any) -> None:
        # The existing engine contains the exact two-sided absorption detection and
        # spacing state.  Its constructor is long-specialized, so initialize that
        # engine and then select the +1 source event type before any rows are seen.
        super().__init__(absorption_event_type=-1, **kwargs)
        self.absorption_event_type = 1

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any] | None:
        event = super()._advance(
            row,
            enforce_append_only=enforce_append_only,
        )
        if event is None:
            return None

        # Parent detection already emitted only when the internally computed
        # event_type equals self.absorption_event_type (+1).  Rewrite only the
        # long-specialized materialization fields; all detection/state/context
        # values are untouched.
        out = dict(event)
        out["side"] = -1
        out["Absorption_Event_Type"] = 1
        out["event_id"] = (
            f"{self.identity.edge_id}|"
            f"{out['Datetime'].isoformat()}|upside_failed_displacement"
        )
        return out

    def state_summary(self) -> dict[str, Any]:
        out = dict(super().state_summary())
        out["provider_id"] = PROVIDER_SPEC["provider_id"]
        out["provider_version"] = PROVIDER_SPEC["provider_version"]
        return out


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"absorption_short D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> AbsorptionShortCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature != ABSORPTION_SHORT_SIGNATURE:
        raise CandidateContinuationError(
            f"absorption_short provider does not support {signature}"
        )

    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider signature mismatch")

    constants = candidate.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise CandidateContinuationError("D.1 semantic_constants missing")

    if int(_constant(constants, "ABSORPTION_EVENT_TYPE")) != 1:
        raise CandidateContinuationError(
            "absorption_short source ABSORPTION_EVENT_TYPE changed"
        )

    identity = extract_candidate_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    if int(identity.side_value) != -1:
        raise CandidateContinuationError(
            "absorption_short source side identity changed"
        )

    return AbsorptionShortCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        wick_share_threshold=float(_constant(constants, "WICK_SHARE_THRESHOLD")),
        close_fail_threshold=float(_constant(constants, "CLOSE_FAIL_THRESHOLD")),
        min_range_atr=float(_constant(constants, "MIN_RANGE_ATR")),
        max_body_share=float(_constant(constants, "MAX_BODY_SHARE")),
        min_wick_atr=float(_constant(constants, "MIN_WICK_ATR")),
        exclude_liquidity_sweeps=bool(
            _constant(constants, "EXCLUDE_LIQUIDITY_SWEEPS")
        ),
        liquidity_lookback=int(_constant(constants, "LIQUIDITY_LOOKBACK")),
        liquidity_sweep_atr_threshold=float(
            _constant(constants, "LIQUIDITY_SWEEP_ATR_THRESHOLD")
        ),
        event_vol_lookback=int(_constant(constants, "EVENT_VOL_LOOKBACK")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
