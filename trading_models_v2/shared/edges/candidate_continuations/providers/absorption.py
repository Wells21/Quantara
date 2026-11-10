from __future__ import annotations

"""Exact incremental absorption candidate continuation for Phase D."""

import bisect
from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd

from shared.edges.native_one_row_feature_engine import (
    _ewm_var_state,
    _ewm_var_update,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

from ..base import (
    CandidateContinuationError,
    extract_candidate_source_identity,
)


# 2026-09-25: signatures regenerated after the calendar-time relabel
# rewrote candidate_events.py's Event_Vol lookback from a bar-count
# constant (EVENT_VOL_LOOKBACK) to a real-hours constant
# (EVENT_VOL_LOOKBACK_HOURS) -- see calendar-time port below. The old
# US500/absorption_long signature is retired: that edge did not clear
# Step 02's threshold-neighborhood stability gate in the post-relabel
# allocator run and is not in the current live manifest.
ABSORPTION_LONG_SIGNATURE_DAX = "9079736d8a92d0aa95f2d4c8d34148d073c38b099e767b907363e6a84b3d04dd"
ABSORPTION_LONG_SIGNATURE = ABSORPTION_LONG_SIGNATURE_DAX

PROVIDER_SPEC = {
    "provider_id": "phase_d4_absorption_candidate",
    "provider_version": "1",
    "supported_signatures": {
        ABSORPTION_LONG_SIGNATURE_DAX: {"side_specialization": "long"},
    },
}

OPTIONAL_CONTEXT = (
    "NATR",
    "Regime_State",
    "Regime_Entropy",
    "Regime_Duration",
    "Prob_State_0",
    "Prob_State_1",
    "Prob_State_2",
    "Prob_State_3",
)

OUTPUT_COLUMNS = [
    "Datetime",
    "Bar_Start",
    "edge_id",
    "side",
    "Event_Vol",
    "event_id",
    "candidate_event_generator",
    "Absorption_Event_Type",
    "event_known_after_close",
    "uses_future_information",
    "Open",
    "High",
    "Low",
    "Close",
    "Body_Direction",
    "Body_Size",
    "Body_Share",
    "Close_Location",
    "Upper_Wick",
    "Lower_Wick",
    "Upper_Wick_Share",
    "Lower_Wick_Share",
    "Wick_Imbalance",
    "Range_ATR",
    "Upper_Wick_ATR",
    "Lower_Wick_ATR",
    "Bull_Response_ATR",
    "Bear_Response_ATR",
    "Clean_Liquidity_High_Sweep",
    "Clean_Liquidity_Low_Sweep",
    "Sweep_High_ATR_Liq",
    "Sweep_Low_ATR_Liq",
    *OPTIONAL_CONTEXT,
    "min_gap_bars",
    "wick_share_threshold",
    "close_fail_threshold",
    "min_range_atr",
    "max_body_share",
    "min_wick_atr",
    "exclude_liquidity_sweeps",
    "liquidity_lookback",
    "liquidity_sweep_atr_threshold",
]


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    close_history_ns: list[int] = field(default_factory=list)
    high_history: list[float] = field(default_factory=list)
    low_history: list[float] = field(default_factory=list)
    last_selected_absorption_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class AbsorptionCandidateContinuation:
    def __init__(
        self,
        *,
        identity,
        eps: float,
        absorption_event_type: int,
        min_gap_bars: int,
        wick_share_threshold: float,
        close_fail_threshold: float,
        min_range_atr: float,
        max_body_share: float,
        min_wick_atr: float,
        exclude_liquidity_sweeps: bool,
        liquidity_lookback: int,
        liquidity_sweep_atr_threshold: float,
        event_vol_lookback_hours: float,
        event_vol_ewm_span: int,
        min_vol: float,
    ) -> None:
        if int(absorption_event_type) != -1:
            raise CandidateContinuationError(
                "current absorption_long signature must emit event type -1"
            )
        if eps <= 0 or min_vol <= 0:
            raise CandidateContinuationError("EPS/MIN_VOL must be positive")
        if min_gap_bars < 0:
            raise CandidateContinuationError("MIN_GAP_BARS must be >= 0")
        if liquidity_lookback <= 0 or event_vol_lookback_hours <= 0:
            raise CandidateContinuationError("lookbacks must be positive")
        if event_vol_ewm_span <= 0:
            raise CandidateContinuationError("EWM span must be positive")
        if liquidity_sweep_atr_threshold < 0:
            raise CandidateContinuationError(
                "liquidity sweep ATR threshold must be >= 0"
            )

        self.identity = identity
        self.eps = float(eps)
        self.absorption_event_type = -1
        self.min_gap_bars = int(min_gap_bars)
        self.wick_share_threshold = float(wick_share_threshold)
        self.close_fail_threshold = float(close_fail_threshold)
        self.min_range_atr = float(min_range_atr)
        self.max_body_share = float(max_body_share)
        self.min_wick_atr = float(min_wick_atr)
        self.exclude_liquidity_sweeps = bool(exclude_liquidity_sweeps)
        self.liquidity_lookback = int(liquidity_lookback)
        self.liquidity_sweep_atr_threshold = float(
            liquidity_sweep_atr_threshold
        )
        self.event_vol_lookback_hours = float(event_vol_lookback_hours)
        self.event_vol_lookback_ns = int(
            round(self.event_vol_lookback_hours * 3600.0 * 1e9)
        )
        self.event_vol_ewm_span = int(event_vol_ewm_span)
        self.min_vol = float(min_vol)
        self._state = self._new_state()

    def _new_state(self) -> _State:
        return _State(
            ewm_var=_ewm_var_state(
                np.asarray([], dtype="float64"),
                self.event_vol_ewm_span,
            )
        )

    def reset(self) -> None:
        self._state = self._new_state()

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise CandidateContinuationError(f"invalid timestamp: {value!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _number(value: Any, name: str) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(result) or not np.isfinite(float(result)):
            raise CandidateContinuationError(f"invalid {name}: {value!r}")
        return float(result)

    @staticmethod
    def _optional_number(value: Any) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        return math.nan if pd.isna(result) else float(result)

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        required = [
            "Datetime", "Bar_Start", "Open", "High", "Low", "Close", "ATR_14"
        ]
        missing = [name for name in required if name not in features.columns]
        if missing:
            raise CandidateContinuationError(
                f"features missing absorption columns: {missing}"
            )

        frame = features.copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        frame["Bar_Start"] = to_datetime_iso(
            frame["Bar_Start"], utc=True, errors="coerce"
        )
        for name in [
            "Open", "High", "Low", "Close", "ATR_14", *OPTIONAL_CONTEXT
        ]:
            if name in frame.columns:
                frame[name] = pd.to_numeric(frame[name], errors="coerce")

        frame = frame.dropna(subset=required).copy()
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise CandidateContinuationError(
                "features empty after absorption normalization"
            )
        return frame

    def _prior_liquidity(self) -> tuple[float, float]:
        state = self._state
        if (
            len(state.high_history) < self.liquidity_lookback
            or len(state.low_history) < self.liquidity_lookback
        ):
            return math.nan, math.nan
        return (
            float(max(state.high_history[-self.liquidity_lookback:])),
            float(min(state.low_history[-self.liquidity_lookback:])),
        )

    def _append_liquidity(self, high: float, low: float) -> None:
        state = self._state
        state.high_history.append(float(high))
        state.low_history.append(float(low))
        if len(state.high_history) > self.liquidity_lookback:
            del state.high_history[
                : len(state.high_history) - self.liquidity_lookback
            ]
        if len(state.low_history) > self.liquidity_lookback:
            del state.low_history[
                : len(state.low_history) - self.liquidity_lookback
            ]

    def _event_vol(self, close: float, timestamp: pd.Timestamp) -> float:
        # Calendar-time backward lookup, mirroring shared/labels/
        # calendar_time_barriers.py::calendar_time_event_vol exactly:
        # target = timestamp - hours; find the last history entry STRICTLY
        # before target (AFML Snippet 3.1 convention: searchsorted(side=
        # "left") - 1, i.e. dt_ns[pos] < target_ns). bisect_left on a plain
        # ascending list gives the same insertion point as np.searchsorted
        # (side="left").
        history_ts = self._state.close_history_ns
        history_close = self._state.close_history
        timestamp_ns = int(timestamp.value)
        target_ns = timestamp_ns - self.event_vol_lookback_ns
        pos = bisect.bisect_left(history_ts, target_ns) - 1
        if pos >= 0:
            lag = float(history_close[pos])
            with np.errstate(divide="ignore", invalid="ignore"):
                long_return = float(
                    np.log((close + self.eps) / (lag + self.eps))
                )
        else:
            long_return = math.nan

        variance = float(_ewm_var_update(self._state.ewm_var, long_return))
        event_vol = (
            max(math.sqrt(variance), self.min_vol)
            if np.isfinite(variance) and variance >= 0
            else math.nan
        )

        history_close.append(float(close))
        history_ts.append(timestamp_ns)
        # Trim history older than the lookback window plus a safety margin
        # (2x) rather than pruning right at the boundary -- keeps the
        # bisect lookup correct even if a future call's target_ns lands
        # slightly earlier due to bar-duration variance, without needing
        # unbounded memory growth.
        # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
        # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
        # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
        cutoff_ns = timestamp_ns - self.event_vol_lookback_ns
        drop = bisect.bisect_left(history_ts, cutoff_ns) - 1
        if drop > 0:
            del history_close[:drop]
            del history_ts[:drop]
        return event_vol

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any] | None:
        state = self._state
        timestamp = self._timestamp(row["Datetime"])
        bar_start = self._timestamp(row["Bar_Start"])
        open_ = self._number(row["Open"], "Open")
        high = self._number(row["High"], "High")
        low = self._number(row["Low"], "Low")
        close = self._number(row["Close"], "Close")
        atr = self._number(row["ATR_14"], "ATR_14")

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and timestamp <= state.last_timestamp
        ):
            raise CandidateContinuationError(
                "update_one requires strictly increasing Datetime"
            )

        source_index = int(state.processed_rows)
        prev_high, prev_low = self._prior_liquidity()

        bar_range = max(high - low, self.eps)
        body = close - open_
        body_abs = abs(body)
        upper_wick = max(high - max(open_, close), 0.0)
        lower_wick = max(min(open_, close) - low, 0.0)

        body_direction = float(np.sign(body))
        body_share = float(body_abs / (bar_range + self.eps))
        close_location = float((close - low) / (bar_range + self.eps))
        upper_wick_share = float(upper_wick / (bar_range + self.eps))
        lower_wick_share = float(lower_wick / (bar_range + self.eps))
        wick_imbalance = float(
            (upper_wick - lower_wick) / (bar_range + self.eps)
        )
        range_atr = float(bar_range / (atr + self.eps))
        upper_wick_atr = float(upper_wick / (atr + self.eps))
        lower_wick_atr = float(lower_wick / (atr + self.eps))
        bull_response_atr = float((close - open_) / (atr + self.eps))
        bear_response_atr = float((open_ - close) / (atr + self.eps))

        if np.isfinite(prev_high):
            sweep_high_atr = float((high - prev_high) / (atr + self.eps))
            clean_high = float(
                sweep_high_atr >= self.liquidity_sweep_atr_threshold
                and close < prev_high
            )
        else:
            sweep_high_atr = math.nan
            clean_high = 0.0

        if np.isfinite(prev_low):
            sweep_low_atr = float((prev_low - low) / (atr + self.eps))
            clean_low = float(
                sweep_low_atr >= self.liquidity_sweep_atr_threshold
                and close > prev_low
            )
        else:
            sweep_low_atr = math.nan
            clean_low = 0.0

        upside = (
            upper_wick_share >= self.wick_share_threshold
            and upper_wick_atr >= self.min_wick_atr
            and close_location <= self.close_fail_threshold
            and range_atr >= self.min_range_atr
            and body_share <= self.max_body_share
        )
        downside = (
            lower_wick_share >= self.wick_share_threshold
            and lower_wick_atr >= self.min_wick_atr
            and close_location >= (1.0 - self.close_fail_threshold)
            and range_atr >= self.min_range_atr
            and body_share <= self.max_body_share
        )

        if self.exclude_liquidity_sweeps:
            upside = upside and not bool(clean_high)
            downside = downside and not bool(clean_low)

        # Source assignment order is +1 first, then -1. If both ever hold,
        # downside (-1) wins.
        event_type = 0
        if upside:
            event_type = 1
        if downside:
            event_type = -1

        event_vol = self._event_vol(close, timestamp)

        emitted = None
        if (
            event_type != 0
            and source_index - state.last_selected_absorption_index
            >= self.min_gap_bars
        ):
            # Critical parity rule: BOTH event directions consume spacing.
            state.last_selected_absorption_index = source_index

            if (
                event_type == self.absorption_event_type
                and np.isfinite(event_vol)
            ):
                emitted = {
                    "Datetime": timestamp,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": 1,
                    "Event_Vol": float(event_vol),
                    "event_id": (
                        f"{self.identity.edge_id}|{timestamp.isoformat()}|"
                        "downside_failed_displacement"
                    ),
                    "candidate_event_generator": (
                        self.identity.candidate_event_generator
                    ),
                    "Absorption_Event_Type": -1,
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": float(open_),
                    "High": float(high),
                    "Low": float(low),
                    "Close": float(close),
                    "Body_Direction": body_direction,
                    "Body_Size": float(body_abs),
                    "Body_Share": body_share,
                    "Close_Location": close_location,
                    "Upper_Wick": float(upper_wick),
                    "Lower_Wick": float(lower_wick),
                    "Upper_Wick_Share": upper_wick_share,
                    "Lower_Wick_Share": lower_wick_share,
                    "Wick_Imbalance": wick_imbalance,
                    "Range_ATR": range_atr,
                    "Upper_Wick_ATR": upper_wick_atr,
                    "Lower_Wick_ATR": lower_wick_atr,
                    "Bull_Response_ATR": bull_response_atr,
                    "Bear_Response_ATR": bear_response_atr,
                    "Clean_Liquidity_High_Sweep": clean_high,
                    "Clean_Liquidity_Low_Sweep": clean_low,
                    "Sweep_High_ATR_Liq": sweep_high_atr,
                    "Sweep_Low_ATR_Liq": sweep_low_atr,
                }

                for name in OPTIONAL_CONTEXT:
                    if name in row:
                        emitted[name] = self._optional_number(row[name])

                emitted.update(
                    {
                        "min_gap_bars": self.min_gap_bars,
                        "wick_share_threshold": self.wick_share_threshold,
                        "close_fail_threshold": self.close_fail_threshold,
                        "min_range_atr": self.min_range_atr,
                        "max_body_share": self.max_body_share,
                        "min_wick_atr": self.min_wick_atr,
                        "exclude_liquidity_sweeps": (
                            self.exclude_liquidity_sweeps
                        ),
                        "liquidity_lookback": self.liquidity_lookback,
                        "liquidity_sweep_atr_threshold": (
                            self.liquidity_sweep_atr_threshold
                        ),
                    }
                )

        self._append_liquidity(high, low)
        state.processed_rows += 1
        state.last_timestamp = timestamp
        return emitted

    def bootstrap(self, features: pd.DataFrame) -> dict[str, Any]:
        frame = self._normalize(features)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(
        self,
        row: Union[Mapping[str, Any], pd.Series],
    ) -> dict[str, Any] | None:
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = self._normalize(features)
        self.reset()
        rows: list[dict[str, Any]] = []
        for row in frame.to_dict(orient="records"):
            event = self._advance(row, enforce_append_only=False)
            if event is not None:
                rows.append(event)
        if not rows:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
        columns = [name for name in OUTPUT_COLUMNS if name in rows[0]]
        return pd.DataFrame(rows, columns=columns)

    def state_summary(self) -> dict[str, Any]:
        state = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "processed_rows": int(state.processed_rows),
            "last_timestamp": (
                state.last_timestamp.isoformat()
                if state.last_timestamp is not None
                else None
            ),
            "high_history_rows": len(state.high_history),
            "low_history_rows": len(state.low_history),
            "close_history_rows": len(state.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"absorption D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> AbsorptionCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(
            f"absorption provider does not support {signature}"
        )

    candidate_record = d1_edge.get("candidate") or {}
    if (
        str(candidate_record.get("candidate_logic_signature") or "")
        != signature
    ):
        raise CandidateContinuationError("D.1/provider signature mismatch")

    constants = candidate_record.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise CandidateContinuationError("D.1 semantic_constants missing")

    identity = extract_candidate_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )

    return AbsorptionCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        absorption_event_type=int(
            _constant(constants, "ABSORPTION_EVENT_TYPE")
        ),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        wick_share_threshold=float(
            _constant(constants, "WICK_SHARE_THRESHOLD")
        ),
        close_fail_threshold=float(
            _constant(constants, "CLOSE_FAIL_THRESHOLD")
        ),
        min_range_atr=float(_constant(constants, "MIN_RANGE_ATR")),
        max_body_share=float(_constant(constants, "MAX_BODY_SHARE")),
        min_wick_atr=float(_constant(constants, "MIN_WICK_ATR")),
        exclude_liquidity_sweeps=bool(
            _constant(constants, "EXCLUDE_LIQUIDITY_SWEEPS")
        ),
        liquidity_lookback=int(
            _constant(constants, "LIQUIDITY_LOOKBACK")
        ),
        liquidity_sweep_atr_threshold=float(
            _constant(constants, "LIQUIDITY_SWEEP_ATR_THRESHOLD")
        ),
        event_vol_lookback_hours=float(
            _constant(constants, "EVENT_VOL_LOOKBACK_HOURS")
        ),
        event_vol_ewm_span=int(
            _constant(constants, "EVENT_VOL_EWM_SPAN")
        ),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
