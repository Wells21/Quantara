from __future__ import annotations

"""Exact reversal_short primary-side continuation for Phase D.4."""

from typing import Any, Mapping, Optional, Union

import pandas as pd

from ..base import (
    PrimarySideContinuationError,
    extract_primary_source_identity,
)
from .reversal import (
    ReversalLongPrimarySideContinuation,
    _State,
)


REVERSAL_SHORT_PRIMARY_SIGNATURE = (
    "cdd2d8ebf5e5c03c377d78cefe1ee2f4111248d5584fc2c8e5cc261b306f17b1"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_reversal_short_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        REVERSAL_SHORT_PRIMARY_SIGNATURE: {"side_value": -1},
    },
}


class ReversalShortPrimarySideContinuation(
    ReversalLongPrimarySideContinuation
):
    """Reuse the certified event-row rejection context with short polarity."""

    def __init__(
        self,
        *,
        identity,
        deviation_side: int,
        eps: float,
        min_wick_to_body: float,
        min_wick_share_of_range: float,
        use_regime_filter: bool,
        use_entropy_filter: bool,
        use_vol_filter: bool,
        max_regime_entropy: float,
        min_event_norm_range: float,
    ) -> None:
        if int(identity.side_value) != -1 or int(deviation_side) != 1:
            raise PrimarySideContinuationError(
                "reversal_short side identity changed"
            )
        self.identity = identity
        self.deviation_side = 1
        self.eps = float(eps)
        self.min_wick_to_body = float(min_wick_to_body)
        self.min_wick_share_of_range = float(min_wick_share_of_range)
        self.use_regime_filter = bool(use_regime_filter)
        self.use_entropy_filter = bool(use_entropy_filter)
        self.use_vol_filter = bool(use_vol_filter)
        self.max_regime_entropy = float(max_regime_entropy)
        self.min_event_norm_range = float(min_event_norm_range)
        self._state = _State()

    def update_one(
        self,
        row: Union[Mapping[str, Any], pd.Series],
    ) -> dict[str, Any]:
        out = dict(super().update_one(row))
        out["primary_side"] = -1
        return out

    def filter_event(
        self,
        event: Optional[Mapping[str, Any]],
    ) -> Optional[dict[str, Any]]:
        # Parent logic computes the exact candidate-event-row rejection context
        # and maintains Prev_* state.  self.deviation_side=+1 selects the source
        # short specialization.  Only final side materialization differs.
        out = super().filter_event(event)
        if out is None:
            return None
        out = dict(out)
        out["primary_side_rule"] = (
            "Deviation_Side=+1 -> side=-1 short"
        )
        out["primary_side"] = -1
        out["primary_side_filter"] = 1
        out["side"] = -1
        return out

    def state_summary(self) -> dict[str, Any]:
        out = dict(super().state_summary())
        out["provider_id"] = PROVIDER_SPEC["provider_id"]
        out["provider_version"] = PROVIDER_SPEC["provider_version"]
        return out


def _constant(
    constants: Mapping[str, Any],
    name: str,
    default: Any = None,
) -> Any:
    return constants[name] if name in constants else default


def create_provider(
    *,
    primary_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> ReversalShortPrimarySideContinuation:
    signature = str(primary_logic_signature or "").strip()
    if signature != REVERSAL_SHORT_PRIMARY_SIGNATURE:
        raise PrimarySideContinuationError(
            f"reversal_short primary provider does not support {signature}"
        )

    primary = d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError(
            "D.1/provider primary signature mismatch"
        )
    constants = primary.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise PrimarySideContinuationError(
            "D.1 primary semantic_constants missing"
        )

    identity = extract_primary_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )

    return ReversalShortPrimarySideContinuation(
        identity=identity,
        deviation_side=int(_constant(constants, "DEVIATION_SIDE", 1)),
        eps=float(_constant(constants, "EPS", 1e-12)),
        min_wick_to_body=float(
            _constant(constants, "MIN_WICK_TO_BODY", 0.80)
        ),
        min_wick_share_of_range=float(
            _constant(constants, "MIN_WICK_SHARE_OF_RANGE", 0.25)
        ),
        use_regime_filter=bool(
            _constant(constants, "USE_REGIME_FILTER", False)
        ),
        use_entropy_filter=bool(
            _constant(constants, "USE_ENTROPY_FILTER", False)
        ),
        use_vol_filter=bool(
            _constant(constants, "USE_VOL_FILTER", False)
        ),
        max_regime_entropy=float(
            _constant(constants, "MAX_REGIME_ENTROPY", 1.25)
        ),
        min_event_norm_range=float(
            _constant(constants, "MIN_EVENT_NORM_RANGE", 0.30)
        ),
    )
