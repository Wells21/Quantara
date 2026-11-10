from __future__ import annotations

"""Exact incremental reversal_short candidate continuation for Phase D.4."""

from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..base import CandidateContinuationError, extract_candidate_source_identity
from .reversal import (
    OPTIONAL_CONTEXT,
    ReversalLongCandidateContinuation,
)


REVERSAL_SHORT_SIGNATURE = (
    "e750853609baa539411e2c5cca940c3547afb60d9cec111b6d0061de3e41d927"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_reversal_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        REVERSAL_SHORT_SIGNATURE: {"side_specialization": "short"},
    },
}


class ReversalShortCandidateContinuation(ReversalLongCandidateContinuation):
    """Same causal Kalman/deviation state, opposite source specialization."""

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any] | None:
        state = self._state
        ts = self._ts(row["Datetime"])
        bar_start = self._ts(row["Bar_Start"])
        open_ = self._num(row["Open"], "Open")
        high = self._num(row["High"], "High")
        low = self._num(row["Low"], "Low")
        close = self._num(row["Close"], "Close")

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and ts <= state.last_timestamp
        ):
            raise CandidateContinuationError(
                "update_one requires increasing Datetime"
            )

        source_index = int(state.processed_rows)
        event_vol = self._event_vol(close)
        kalman = self._kalman(close)
        dist = float((close - kalman) / (kalman + self.eps))

        # Match the authoritative source: current Dist_Kalman_Local participates
        # in the current rolling mean/std.
        state.deviation_history.append(dist)
        if len(state.deviation_history) > self.dev_window:
            del state.deviation_history[
                : len(state.deviation_history) - self.dev_window
            ]

        if len(state.deviation_history) >= self.dev_window:
            series = pd.Series(
                state.deviation_history[-self.dev_window:],
                dtype="float64",
            )
            mean = float(series.mean())
            std = max(float(series.std()), self.min_vol)
            z_value = float((dist - mean) / (std + self.eps))
        else:
            z_value = np.nan

        deviation_side = 0
        if np.isfinite(z_value):
            if z_value >= self.dev_z_threshold:
                deviation_side = 1
            elif z_value <= -self.dev_z_threshold:
                deviation_side = -1

        emitted = None
        if (
            deviation_side != 0
            and source_index - state.last_selected_event_index
            >= self.min_gap_bars
        ):
            # Authoritative V1 spacing is consumed across both overbought and
            # oversold deviations before the short-side specialization.
            state.last_selected_event_index = source_index

            if deviation_side == 1 and np.isfinite(event_vol):
                emitted = {
                    "Datetime": ts,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": -1,
                    "Event_Vol": float(event_vol),
                    "event_id": (
                        f"{self.identity.edge_id}|{ts.isoformat()}|"
                        "overbought_kalman_deviation"
                    ),
                    "candidate_event_generator": (
                        self.identity.candidate_event_generator
                    ),
                    "Deviation_Side": 1,
                    "Deviation_Z": float(z_value),
                    "Kalman_Close_Local": float(kalman),
                    "Dist_Kalman_Local": float(dist),
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": open_,
                    "High": high,
                    "Low": low,
                    "Close": close,
                }

                for name in OPTIONAL_CONTEXT:
                    if name in row:
                        emitted[name] = self._optional(row[name])

                emitted["dev_window"] = self.dev_window
                emitted["dev_z_threshold"] = self.dev_z_threshold
                emitted["min_gap_bars"] = self.min_gap_bars
                emitted["vol_lookback"] = self.vol_lookback
                emitted["vol_ewm_span"] = self.vol_ewm_span

        state.processed_rows += 1
        state.last_timestamp = ts
        return emitted

    def state_summary(self) -> dict[str, Any]:
        out = dict(super().state_summary())
        out["provider_id"] = PROVIDER_SPEC["provider_id"]
        out["provider_version"] = PROVIDER_SPEC["provider_version"]
        return out


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"reversal_short D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> ReversalShortCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature != REVERSAL_SHORT_SIGNATURE:
        raise CandidateContinuationError(
            f"reversal_short provider does not support {signature}"
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
            "reversal_short source side identity changed"
        )

    return ReversalShortCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        vol_lookback=int(_constant(constants, "VOL_LOOKBACK")),
        vol_ewm_span=int(_constant(constants, "VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
        dev_window=int(_constant(constants, "DEV_WINDOW")),
        dev_z_threshold=float(_constant(constants, "DEV_Z_THRESHOLD")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
    )
