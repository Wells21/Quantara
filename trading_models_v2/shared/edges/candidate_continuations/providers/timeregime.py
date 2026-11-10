from __future__ import annotations

"""Exact incremental time-regime candidate continuation for Phase D."""

import bisect
from dataclasses import dataclass, field
import math
from pathlib import Path
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
    CandidateSourceIdentity,
    extract_candidate_source_identity,
)


# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_
# LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS).
TIMEREGIME_LONG_SIGNATURE = (
    "8a4ff1263ea14fd93d70281888137cd4500f6591bd46312948c53c3a7515d680"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_timeregime_candidate",
    "provider_version": "1",
    "supported_signatures": {
        TIMEREGIME_LONG_SIGNATURE: {"side_specialization": "long"},
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


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    close_history_ns: list[int] = field(default_factory=list)
    volatility_history: list[float] = field(default_factory=list)
    hour_return_history: dict[int, list[float]] = field(default_factory=dict)
    last_close: float = math.nan
    last_selected_event_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class TimeRegimeCandidateContinuation:
    def __init__(
        self,
        *,
        identity: CandidateSourceIdentity,
        eps: float,
        bias_window: int,
        time_tstat_threshold: float,
        volatility_ratio_threshold: float,
        min_obs: int,
        min_gap_bars: int,
        event_vol_lookback_hours: float,
        event_vol_ewm_span: int,
        min_vol: float,
    ) -> None:
        if eps <= 0 or bias_window <= 1 or min_obs <= 1:
            raise CandidateContinuationError("invalid time-regime window configuration")
        if min_obs > bias_window:
            raise CandidateContinuationError("MIN_OBS cannot exceed BIAS_WINDOW")
        if min_gap_bars < 0 or event_vol_lookback_hours <= 0 or event_vol_ewm_span <= 0:
            raise CandidateContinuationError("invalid time-regime state configuration")
        if min_vol <= 0:
            raise CandidateContinuationError("MIN_VOL must be > 0")

        self.identity = identity
        self.eps = float(eps)
        self.bias_window = int(bias_window)
        self.time_tstat_threshold = float(time_tstat_threshold)
        self.volatility_ratio_threshold = float(volatility_ratio_threshold)
        self.min_obs = int(min_obs)
        self.min_gap_bars = int(min_gap_bars)
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

    @staticmethod
    def _time_bucket(hour: int) -> str:
        if 0 <= int(hour) < 7:
            return "asia"
        if 7 <= int(hour) < 12:
            return "london"
        if 12 <= int(hour) < 17:
            return "ny_overlap"
        if 17 <= int(hour) < 22:
            return "late_ny"
        return "rollover"

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        required = [
            "Datetime",
            "Bar_Start",
            "Open",
            "High",
            "Low",
            "Close",
            "Bar_Volatility_Bps",
        ]
        missing = [name for name in required if name not in features.columns]
        if missing:
            raise CandidateContinuationError(
                f"features missing time-regime columns: {missing}"
            )

        frame = features.copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        frame["Bar_Start"] = to_datetime_iso(
            frame["Bar_Start"], utc=True, errors="coerce"
        )
        for name in [
            "Open",
            "High",
            "Low",
            "Close",
            "Bar_Volatility_Bps",
            *OPTIONAL_CONTEXT,
        ]:
            if name in frame.columns:
                frame[name] = pd.to_numeric(frame[name], errors="coerce")

        frame = frame.dropna(subset=required).copy()
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise CandidateContinuationError(
                "features empty after time-regime normalization"
            )
        return frame

    def _hour_stats(self, hour: int) -> tuple[float, float]:
        history = self._state.hour_return_history.get(int(hour), [])
        if len(history) == 0:
            return math.nan, math.nan
        series = pd.Series(history, dtype="float64")
        if int(series.notna().sum()) < self.min_obs:
            return math.nan, math.nan
        return float(series.mean()), float(series.std())

    def _append_hour_return(self, hour: int, value: float) -> None:
        history = self._state.hour_return_history.setdefault(int(hour), [])
        history.append(float(value))
        if len(history) > self.bias_window:
            del history[: len(history) - self.bias_window]

    def _volatility_ratio(self, current: float) -> float:
        history = self._state.volatility_history
        if len(history) < 100:
            ratio = math.nan
        else:
            denominator = float(pd.Series(history[-100:], dtype="float64").median())
            ratio = float(current / (denominator + self.eps))

        history.append(float(current))
        if len(history) > 100:
            del history[: len(history) - 100]
        return ratio

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
                long_return = float(
                    np.log((close + self.eps) / (lag + self.eps))
                )
        else:
            long_return = math.nan

        variance = float(_ewm_var_update(self._state.ewm_var, long_return))
        if not np.isfinite(variance) or variance < 0.0:
            event_vol = math.nan
        else:
            event_vol = max(math.sqrt(variance), self.min_vol)

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

    def _event(
        self,
        *,
        timestamp: pd.Timestamp,
        bar_start: pd.Timestamp,
        open_: float,
        high: float,
        low: float,
        close: float,
        event_vol: float,
        event_type: int,
        tstat: float,
        volatility_ratio: float,
        hour: int,
        day_of_week: int,
        time_bucket: str,
        bias_mean: float,
        bias_std: float,
        bar_return: float,
        body_direction: float,
        body_share: float,
        close_location: float,
        optional: Mapping[str, Any],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "Datetime": timestamp,
            "Bar_Start": bar_start,
            "edge_id": self.identity.edge_id,
            "side": int(event_type),
            "Event_Vol": float(event_vol),
            "event_id": (
                f"{self.identity.edge_id}|{timestamp.isoformat()}|time_regime"
            ),
            "candidate_event_generator": self.identity.candidate_event_generator,
            "Time_Regime_Event_Type": int(event_type),
            "Time_Bias_TStat": float(tstat),
            "Volatility_Ratio": float(volatility_ratio),
            "Hour_UTC": int(hour),
            "Day_Of_Week": int(day_of_week),
            "Time_Bucket": str(time_bucket),
            "Time_Bias_Mean": float(bias_mean),
            "Time_Bias_Std": float(bias_std),
            "Bar_Return": float(bar_return),
            "Body_Direction": float(body_direction),
            "Body_Share": float(body_share),
            "Close_Location": float(close_location),
            "event_known_after_close": True,
            "uses_future_information": False,
            "Open": float(open_),
            "High": float(high),
            "Low": float(low),
            "Close": float(close),
        }
        for name in OPTIONAL_CONTEXT:
            if name in optional:
                out[name] = self._optional_number(optional[name])

        out["bias_window"] = int(self.bias_window)
        out["time_tstat_threshold"] = float(self.time_tstat_threshold)
        out["volatility_ratio_threshold"] = float(
            self.volatility_ratio_threshold
        )
        out["min_gap_bars"] = int(self.min_gap_bars)
        out["event_vol_lookback_hours"] = self.event_vol_lookback_hours
        out["event_vol_ewm_span"] = int(self.event_vol_ewm_span)
        return out

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
        bar_volatility = self._number(
            row["Bar_Volatility_Bps"], "Bar_Volatility_Bps"
        )

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and timestamp <= state.last_timestamp
        ):
            raise CandidateContinuationError(
                "update_one requires strictly increasing Datetime"
            )

        source_index = int(state.processed_rows)
        hour = int(timestamp.hour)
        day_of_week = int(timestamp.dayofweek)
        bucket = self._time_bucket(hour)

        if source_index == 0 or not np.isfinite(state.last_close):
            bar_return = math.nan
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                bar_return = float(
                    np.log((close + self.eps) / (state.last_close + self.eps))
                )

        bias_mean, bias_std = self._hour_stats(hour)
        if np.isfinite(bias_mean) and np.isfinite(bias_std):
            tstat = float(
                bias_mean
                / ((bias_std + self.eps) / math.sqrt(float(self.bias_window)))
            )
        else:
            tstat = math.nan

        volatility_ratio = self._volatility_ratio(bar_volatility)

        bar_range = max(float(high - low), self.eps)
        body = float(close - open_)
        body_direction = float(np.sign(body))
        body_share = float(abs(body) / (bar_range + self.eps))
        close_location = float((close - low) / (bar_range + self.eps))

        event_vol = self._event_vol(close, timestamp)

        event_type = 0
        if (
            np.isfinite(tstat)
            and np.isfinite(volatility_ratio)
            and bucket != "rollover"
        ):
            if (
                tstat >= self.time_tstat_threshold
                and volatility_ratio >= self.volatility_ratio_threshold
            ):
                event_type = 1
            elif (
                tstat <= -self.time_tstat_threshold
                and volatility_ratio >= self.volatility_ratio_threshold
            ):
                event_type = -1

        emitted = None
        if (
            event_type != 0
            and source_index - state.last_selected_event_index
            >= self.min_gap_bars
        ):
            # Match source ordering: spacing is consumed before the later
            # Event_Vol/dropna validation of the materialized event table.
            state.last_selected_event_index = source_index
            if np.isfinite(event_vol):
                emitted = self._event(
                    timestamp=timestamp,
                    bar_start=bar_start,
                    open_=open_,
                    high=high,
                    low=low,
                    close=close,
                    event_vol=event_vol,
                    event_type=event_type,
                    tstat=tstat,
                    volatility_ratio=volatility_ratio,
                    hour=hour,
                    day_of_week=day_of_week,
                    time_bucket=bucket,
                    bias_mean=bias_mean,
                    bias_std=bias_std,
                    bar_return=bar_return,
                    body_direction=body_direction,
                    body_share=body_share,
                    close_location=close_location,
                    optional=row,
                )

        self._append_hour_return(hour, bar_return)
        state.last_close = float(close)
        state.last_timestamp = timestamp
        state.processed_rows += 1
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
        required = (
            "Datetime",
            "Bar_Start",
            "Open",
            "High",
            "Low",
            "Close",
            "Bar_Volatility_Bps",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise CandidateContinuationError(
                f"update_one missing fields: {missing}"
            )
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = self._normalize(features)
        self.reset()
        events: list[dict[str, Any]] = []
        for row in frame.to_dict(orient="records"):
            event = self._advance(row, enforce_append_only=False)
            if event is not None:
                events.append(event)
        return pd.DataFrame(events)

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
            "hour_bucket_count": len(state.hour_return_history),
            "max_hour_history_rows": max(
                [len(values) for values in state.hour_return_history.values()]
                or [0]
            ),
            "volatility_history_rows": len(state.volatility_history),
            "close_history_rows": len(state.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"time-regime D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root: Union[str, Path],
) -> TimeRegimeCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(
            f"time-regime provider does not support {signature}"
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
    if int(identity.side_value) != 1:
        raise CandidateContinuationError(
            "current time-regime signature is long-specialized"
        )

    return TimeRegimeCandidateContinuation(
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
        event_vol_lookback_hours=float(
            _constant(constants, "EVENT_VOL_LOOKBACK_HOURS")
        ),
        event_vol_ewm_span=int(
            _constant(constants, "EVENT_VOL_EWM_SPAN")
        ),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
