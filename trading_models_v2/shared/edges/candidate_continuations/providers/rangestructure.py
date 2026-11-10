from __future__ import annotations

"""Exact incremental rangestructure_long candidate continuation for Phase D."""

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


# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_
# LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS). JPN and XAUUSD share an identical
# EVENT_VOL_LOOKBACK_HOURS=8.0 (and every other constant), so they still
# resolve to one shared exact signature, same as before the relabel.
RANGESTRUCTURE_LONG_SIGNATURE_JPN_XAUUSD = "7c138a5bc783ce176da23683fde23d2f00c5c7d06741562d40ce29ca5e80debc"
RANGESTRUCTURE_LONG_SIGNATURE = RANGESTRUCTURE_LONG_SIGNATURE_JPN_XAUUSD

PROVIDER_SPEC = {
    "provider_id": "phase_d4_rangestructure_candidate",
    "provider_version": "1",
    "supported_signatures": {
        RANGESTRUCTURE_LONG_SIGNATURE_JPN_XAUUSD: {"side_specialization": "long"},
    },
}

OPTIONAL_CONTEXT = (
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
    "Range_Structure_Event_Type",
    "Structure_Bias_TStat",
    "Range_Position",
    "Range_High",
    "Range_Low",
    "Range_Width",
    "Range_Compression_Ratio",
    "Structure_Bias_Mean",
    "Structure_Bias_Std",
    "event_known_after_close",
    "uses_future_information",
    "Open",
    "High",
    "Low",
    "Close",
    "Bar_Return",
    "Dist_To_Range_High",
    "Dist_To_Range_Low",
    "Range_Width_Median",
    "Body_Direction",
    "Body_Share",
    "Close_Location",
    *OPTIONAL_CONTEXT,
    "range_lookback",
    "extreme_zone_threshold",
    "structure_bias_lookback",
    "structure_bias_tstat_threshold",
    "max_mid_range_position",
    "min_gap_bars",
    "event_vol_lookback_hours",
    "event_vol_ewm_span",
]


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    close_history_ns: list[int] = field(default_factory=list)
    high_history: list[float] = field(default_factory=list)
    low_history: list[float] = field(default_factory=list)
    range_width_history: list[float] = field(default_factory=list)
    bar_return_history: list[float] = field(default_factory=list)
    last_close: float = math.nan
    last_selected_event_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class RangeStructureCandidateContinuation:
    def __init__(
        self,
        *,
        identity,
        eps: float,
        range_lookback: int,
        extreme_zone_threshold: float,
        structure_bias_lookback: int,
        structure_bias_tstat_threshold: float,
        max_mid_range_position: float,
        min_obs: int,
        event_vol_lookback_hours: float,
        event_vol_ewm_span: int,
        min_vol: float,
        min_gap_bars: int,
    ) -> None:
        if eps <= 0 or min_vol <= 0:
            raise CandidateContinuationError("EPS/MIN_VOL must be positive")
        if min_gap_bars < 0:
            raise CandidateContinuationError("MIN_GAP_BARS must be >= 0")
        for value in (
            range_lookback,
            structure_bias_lookback,
            min_obs,
            event_vol_ewm_span,
        ):
            if int(value) <= 0:
                raise CandidateContinuationError(
                    "range-structure lookbacks/spans must be positive"
                )
        if event_vol_lookback_hours <= 0:
            raise CandidateContinuationError(
                "range-structure lookbacks/spans must be positive"
            )
        if min_obs > min(range_lookback, structure_bias_lookback):
            raise CandidateContinuationError(
                "MIN_OBS exceeds a required rolling lookback"
            )

        self.identity = identity
        self.eps = float(eps)
        self.range_lookback = int(range_lookback)
        self.extreme_zone_threshold = float(extreme_zone_threshold)
        self.structure_bias_lookback = int(structure_bias_lookback)
        self.structure_bias_tstat_threshold = float(
            structure_bias_tstat_threshold
        )
        self.max_mid_range_position = float(max_mid_range_position)
        self.min_obs = int(min_obs)
        self.event_vol_lookback_hours = float(event_vol_lookback_hours)
        self.event_vol_lookback_ns = int(
            round(self.event_vol_lookback_hours * 3600.0 * 1e9)
        )
        self.event_vol_ewm_span = int(event_vol_ewm_span)
        self.min_vol = float(min_vol)
        self.min_gap_bars = int(min_gap_bars)
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
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise CandidateContinuationError(f"invalid {name}: {value!r}")
        return float(x)

    @staticmethod
    def _optional(value: Any) -> float:
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        return math.nan if pd.isna(x) else float(x)

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
        missing = [name for name in required if name not in features.columns]
        if missing:
            raise CandidateContinuationError(
                f"features missing range-structure columns: {missing}"
            )

        frame = features.copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        frame["Bar_Start"] = to_datetime_iso(
            frame["Bar_Start"], utc=True, errors="coerce"
        )
        for name in ["Open", "High", "Low", "Close", *OPTIONAL_CONTEXT]:
            if name in frame.columns:
                frame[name] = pd.to_numeric(frame[name], errors="coerce")
        frame = frame.dropna(subset=required)
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise CandidateContinuationError(
                "features empty after range-structure normalization"
            )
        return frame

    @staticmethod
    def _finite_window_stats(
        values: list[float],
        *,
        min_obs: int,
    ) -> tuple[float, float]:
        array = np.asarray(values, dtype="float64")
        finite = array[np.isfinite(array)]
        if finite.size < min_obs:
            return math.nan, math.nan
        series = pd.Series(finite, dtype="float64")
        return float(series.mean()), float(series.std())

    def _event_vol(self, close: float, timestamp: pd.Timestamp) -> float:
        # Calendar-time backward lookup -- see absorption.py's identical
        # port (verified 2026-09-25 via synthetic parity test against
        # shared/labels/calendar_time_barriers.py::calendar_time_event_vol,
        # max abs diff = 0.0).
        history = self._state.close_history
        history_ns = self._state.close_history_ns
        ts_ns = int(timestamp.value)
        target_ns = ts_ns - self.event_vol_lookback_ns
        pos = bisect.bisect_left(history_ns, target_ns) - 1
        if pos >= 0:
            lag = float(history[pos])
            with np.errstate(divide="ignore", invalid="ignore"):
                ret = float(np.log((close + self.eps) / (lag + self.eps)))
        else:
            ret = math.nan

        variance = float(_ewm_var_update(self._state.ewm_var, ret))
        event_vol = (
            max(math.sqrt(variance), self.min_vol)
            if np.isfinite(variance) and variance >= 0
            else math.nan
        )
        history.append(float(close))
        history_ns.append(ts_ns)
        # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
        # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
        # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
        cutoff_ns = ts_ns - self.event_vol_lookback_ns
        drop = bisect.bisect_left(history_ns, cutoff_ns) - 1
        if drop > 0:
            del history[:drop]
            del history_ns[:drop]
        return event_vol

    def _prior_range(self) -> tuple[float, float]:
        state = self._state
        if (
            len(state.high_history) < self.min_obs
            or len(state.low_history) < self.min_obs
        ):
            return math.nan, math.nan
        return (
            float(max(state.high_history[-self.range_lookback:])),
            float(min(state.low_history[-self.range_lookback:])),
        )

    def _prior_range_width_median(self) -> float:
        values = self._state.range_width_history[-self.range_lookback:]
        finite = np.asarray(values, dtype="float64")
        finite = finite[np.isfinite(finite)]
        if finite.size < self.min_obs:
            return math.nan
        return float(pd.Series(finite, dtype="float64").median())

    def _prior_bias(self) -> tuple[float, float]:
        values = self._state.bar_return_history[
            -self.structure_bias_lookback:
        ]
        return self._finite_window_stats(values, min_obs=self.min_obs)

    def _append_bounded(
        self,
        target: list[float],
        value: float,
        limit: int,
    ) -> None:
        target.append(float(value))
        if len(target) > limit:
            del target[: len(target) - limit]

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

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and timestamp <= state.last_timestamp
        ):
            raise CandidateContinuationError(
                "update_one requires strictly increasing Datetime"
            )

        source_index = int(state.processed_rows)

        event_vol = self._event_vol(close, timestamp)

        if source_index == 0 or not np.isfinite(state.last_close):
            bar_return = math.nan
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                bar_return = float(
                    np.log((close + self.eps) / (state.last_close + self.eps))
                )

        range_high, range_low = self._prior_range()
        if np.isfinite(range_high) and np.isfinite(range_low):
            range_width = max(range_high - range_low, self.eps)
            range_position = float(
                np.clip(
                    (close - range_low) / (range_width + self.eps),
                    -1.0,
                    2.0,
                )
            )
            dist_high = float(
                (range_high - close) / (range_width + self.eps)
            )
            dist_low = float(
                (close - range_low) / (range_width + self.eps)
            )
        else:
            range_width = math.nan
            range_position = math.nan
            dist_high = math.nan
            dist_low = math.nan

        range_width_median = self._prior_range_width_median()
        range_compression_ratio = (
            float(range_width / (range_width_median + self.eps))
            if np.isfinite(range_width) and np.isfinite(range_width_median)
            else math.nan
        )

        bias_mean, bias_std = self._prior_bias()
        if np.isfinite(bias_mean) and np.isfinite(bias_std):
            structure_tstat = float(
                bias_mean
                / (
                    (bias_std + self.eps)
                    / math.sqrt(float(self.structure_bias_lookback))
                )
            )
        else:
            structure_tstat = math.nan

        bar_range = max(high - low, self.eps)
        body = close - open_
        body_direction = float(np.sign(body))
        body_share = float(abs(body) / (bar_range + self.eps))
        close_location = float((close - low) / (bar_range + self.eps))

        event_type = 0
        if np.isfinite(range_position) and np.isfinite(structure_tstat):
            near_low = range_position <= self.extreme_zone_threshold
            near_high = range_position >= (
                1.0 - self.extreme_zone_threshold
            )
            away_from_mid = (
                range_position <= self.max_mid_range_position
                or range_position >= (1.0 - self.max_mid_range_position)
            )
            bias_ok = (
                abs(structure_tstat)
                >= self.structure_bias_tstat_threshold
            )
            if near_low and away_from_mid and bias_ok:
                event_type = -1
            if near_high and away_from_mid and bias_ok:
                event_type = 1

        raw_side = (
            int(np.sign(structure_tstat))
            if np.isfinite(structure_tstat)
            else 0
        )

        emitted = None
        if (
            event_type != 0
            and raw_side != 0
            and source_index - state.last_selected_event_index
            >= self.min_gap_bars
        ):
            # Raw +/- events consume the shared spacing before long-only primary.
            state.last_selected_event_index = source_index

            if np.isfinite(event_vol):
                emitted = {
                    "Datetime": timestamp,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": raw_side,
                    "Event_Vol": float(event_vol),
                    "event_id": (
                        f"{self.identity.edge_id}|{timestamp.isoformat()}|"
                        "range_structure"
                    ),
                    "candidate_event_generator": (
                        self.identity.candidate_event_generator
                    ),
                    "Range_Structure_Event_Type": int(event_type),
                    "Structure_Bias_TStat": float(structure_tstat),
                    "Range_Position": float(range_position),
                    "Range_High": float(range_high),
                    "Range_Low": float(range_low),
                    "Range_Width": float(range_width),
                    "Range_Compression_Ratio": float(
                        range_compression_ratio
                    ),
                    "Structure_Bias_Mean": float(bias_mean),
                    "Structure_Bias_Std": float(bias_std),
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": float(open_),
                    "High": float(high),
                    "Low": float(low),
                    "Close": float(close),
                    "Bar_Return": float(bar_return),
                    "Dist_To_Range_High": float(dist_high),
                    "Dist_To_Range_Low": float(dist_low),
                    "Range_Width_Median": float(range_width_median),
                    "Body_Direction": body_direction,
                    "Body_Share": body_share,
                    "Close_Location": close_location,
                }

                for name in OPTIONAL_CONTEXT:
                    if name in row:
                        emitted[name] = self._optional(row[name])

                emitted.update(
                    {
                        "range_lookback": self.range_lookback,
                        "extreme_zone_threshold": (
                            self.extreme_zone_threshold
                        ),
                        "structure_bias_lookback": (
                            self.structure_bias_lookback
                        ),
                        "structure_bias_tstat_threshold": (
                            self.structure_bias_tstat_threshold
                        ),
                        "max_mid_range_position": (
                            self.max_mid_range_position
                        ),
                        "min_gap_bars": self.min_gap_bars,
                        "event_vol_lookback_hours": self.event_vol_lookback_hours,
                        "event_vol_ewm_span": self.event_vol_ewm_span,
                    }
                )

        self._append_bounded(
            state.high_history, high, self.range_lookback
        )
        self._append_bounded(
            state.low_history, low, self.range_lookback
        )
        self._append_bounded(
            state.range_width_history,
            range_width,
            self.range_lookback,
        )
        self._append_bounded(
            state.bar_return_history,
            bar_return,
            self.structure_bias_lookback,
        )

        state.last_close = float(close)
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
            "range_width_history_rows": len(state.range_width_history),
            "bar_return_history_rows": len(state.bar_return_history),
            "close_history_rows": len(state.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"range-structure D.1 constant missing: {name}"
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
            f"range-structure provider does not support {signature}"
        )

    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError(
            "D.1/provider candidate signature mismatch"
        )

    constants = candidate.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise CandidateContinuationError("D.1 semantic_constants missing")

    identity = extract_candidate_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )

    return RangeStructureCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        range_lookback=int(_constant(constants, "RANGE_LOOKBACK")),
        extreme_zone_threshold=float(
            _constant(constants, "EXTREME_ZONE_THRESHOLD")
        ),
        structure_bias_lookback=int(
            _constant(constants, "STRUCTURE_BIAS_LOOKBACK")
        ),
        structure_bias_tstat_threshold=float(
            _constant(constants, "STRUCTURE_BIAS_TSTAT_THRESHOLD")
        ),
        max_mid_range_position=float(
            _constant(constants, "MAX_MID_RANGE_POSITION")
        ),
        min_obs=int(_constant(constants, "MIN_OBS")),
        event_vol_lookback_hours=float(
            _constant(constants, "EVENT_VOL_LOOKBACK_HOURS")
        ),
        event_vol_ewm_span=int(
            _constant(constants, "EVENT_VOL_EWM_SPAN")
        ),
        min_vol=float(_constant(constants, "MIN_VOL")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
    )
