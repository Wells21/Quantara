from __future__ import annotations

"""Exact online drawupreversalhazard_short continuation for Phase D.4.

The authoritative edge is causal but stateful: hazard observations only enter the
hazard table after their outcome has resolved.  This provider preserves that
ordering with bounded in-memory state and never calls the batch candidate source
on the normal one-row path.
"""

from collections import defaultdict
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


DRAWUP_REVERSAL_HAZARD_SHORT_SIGNATURE = (
    "6c9467947b229988c48835fef6c456f80ba3a1b1b416601d547395a1787e0db0"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_drawupreversalhazard_short_candidate",
    "provider_version": "1",
    "supported_signatures": {
        DRAWUP_REVERSAL_HAZARD_SHORT_SIGNATURE: {
            "side_specialization": "short"
        },
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
    event_close_history: list[float] = field(default_factory=list)
    path: list[tuple[int, float, float]] = field(default_factory=list)
    severity_history: list[float] = field(default_factory=list)
    concentration_history: list[float] = field(default_factory=list)
    return_history: list[float] = field(default_factory=list)
    positive_flags: list[int] = field(default_factory=list)
    new_high_flags: list[int] = field(default_factory=list)
    positive_jump_flags: list[int] = field(default_factory=list)

    episode_trough_index: int = -(10**9)
    episode: Optional[dict[str, Any]] = None

    observations: list[dict[str, Any]] = field(default_factory=list)
    pending_updates: list[tuple[int, int, int, int, int, int]] = field(
        default_factory=list
    )
    primary: dict[Any, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
    )
    secondary: dict[Any, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
    )
    tertiary: dict[Any, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
    )
    quaternary: dict[Any, list[float]] = field(
        default_factory=lambda: defaultdict(lambda: [0.0, 0.0, 0.0])
    )
    global_counter: list[float] = field(
        default_factory=lambda: [0.0, 0.0, 0.0]
    )

    observation_trough_marker: int = -(10**9)
    last_observation_index: int = -(10**9)

    previous_active: bool = False
    emitted_in_state: bool = False
    last_event_index: int = -(10**9)

    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class DrawupReversalHazardShortCandidateContinuation:
    def __init__(
        self,
        *,
        identity,
        primary_trough_window: int,
        secondary_trough_windows: tuple[int, ...],
        severity_baseline_window: int,
        concentration_window: int,
        concentration_baseline_window: int,
        hazard_resolution_max_bars: int,
        hazard_observation_gap_bars: int,
        new_high_buffer_vol: float,
        reversal_buffer_vol: float,
        min_drawup_severity_percentile: float,
        min_positive_tail_concentration_percentile: float,
        min_pullback_fraction: float,
        max_pullback_fraction: float,
        min_reversal_hazard_probability: float,
        min_hazard_cell_observations: int,
        min_event_gap_bars: int,
        hazard_prior_alpha: float,
        hazard_prior_beta: float,
        event_vol_lookback: int,
        event_vol_ewm_span: int,
        min_event_vol: float,
        eps: float,
    ) -> None:
        if int(identity.side_value) != -1:
            raise CandidateContinuationError(
                "drawupreversalhazard_short source side changed"
            )
        if primary_trough_window <= 0:
            raise CandidateContinuationError(
                "PRIMARY_TROUGH_WINDOW must be positive"
            )
        if not secondary_trough_windows or any(
            int(value) <= 0 for value in secondary_trough_windows
        ):
            raise CandidateContinuationError(
                "SECONDARY_TROUGH_WINDOWS invalid"
            )
        if severity_baseline_window <= 0:
            raise CandidateContinuationError(
                "SEVERITY_BASELINE_WINDOW must be positive"
            )
        if concentration_window <= 1 or concentration_baseline_window <= 0:
            raise CandidateContinuationError(
                "concentration windows are invalid"
            )
        if hazard_resolution_max_bars <= 0:
            raise CandidateContinuationError(
                "HAZARD_RESOLUTION_MAX_BARS must be positive"
            )
        if hazard_observation_gap_bars < 0 or min_event_gap_bars < 0:
            raise CandidateContinuationError(
                "hazard/event gaps must be non-negative"
            )
        if event_vol_lookback <= 0 or event_vol_ewm_span <= 0:
            raise CandidateContinuationError(
                "event-vol configuration invalid"
            )
        if min_event_vol <= 0 or eps <= 0:
            raise CandidateContinuationError(
                "MIN_EVENT_VOL/EPS must be positive"
            )
        if min_hazard_cell_observations <= 0:
            raise CandidateContinuationError(
                "MIN_HAZARD_CELL_OBSERVATIONS must be positive"
            )

        self.identity = identity
        self.primary_trough_window = int(primary_trough_window)
        self.secondary_trough_windows = tuple(
            int(value) for value in secondary_trough_windows
        )
        self.severity_baseline_window = int(severity_baseline_window)
        self.concentration_window = int(concentration_window)
        self.concentration_baseline_window = int(
            concentration_baseline_window
        )
        self.hazard_resolution_max_bars = int(
            hazard_resolution_max_bars
        )
        self.hazard_observation_gap_bars = int(
            hazard_observation_gap_bars
        )
        self.new_high_buffer_vol = float(new_high_buffer_vol)
        self.reversal_buffer_vol = float(reversal_buffer_vol)
        self.min_drawup_severity_percentile = float(
            min_drawup_severity_percentile
        )
        self.min_positive_tail_concentration_percentile = float(
            min_positive_tail_concentration_percentile
        )
        self.min_pullback_fraction = float(min_pullback_fraction)
        self.max_pullback_fraction = float(max_pullback_fraction)
        self.min_reversal_hazard_probability = float(
            min_reversal_hazard_probability
        )
        self.min_hazard_cell_observations = int(
            min_hazard_cell_observations
        )
        self.min_event_gap_bars = int(min_event_gap_bars)
        self.hazard_prior_alpha = float(hazard_prior_alpha)
        self.hazard_prior_beta = float(hazard_prior_beta)
        self.event_vol_lookback = int(event_vol_lookback)
        self.event_vol_ewm_span = int(event_vol_ewm_span)
        self.min_event_vol = float(min_event_vol)
        self.eps = float(eps)

        self.max_trough_window = max(
            self.primary_trough_window,
            *self.secondary_trough_windows,
        )
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
            raise CandidateContinuationError(
                f"invalid timestamp: {value!r}"
            )
        return pd.Timestamp(ts)

    @staticmethod
    def _number(value: Any, name: str) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(result) or not np.isfinite(float(result)):
            raise CandidateContinuationError(
                f"invalid {name}: {value!r}"
            )
        return float(result)

    @staticmethod
    def _optional_number(value: Any) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        return math.nan if pd.isna(result) else float(result)

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
        ]
        missing = [name for name in required if name not in features.columns]
        if missing:
            raise CandidateContinuationError(
                f"drawup hazard features missing columns: {missing}"
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
            "ATR_14",
            *OPTIONAL_CONTEXT,
        ]:
            if name in frame.columns:
                frame[name] = pd.to_numeric(frame[name], errors="coerce")

        frame = (
            frame.dropna(subset=required)
            .sort_values("Datetime")
            .reset_index(drop=True)
        )
        frame = frame[
            (frame["Open"] > 0)
            & (frame["High"] > 0)
            & (frame["Low"] > 0)
            & (frame["Close"] > 0)
        ].copy()
        if frame.empty:
            raise CandidateContinuationError(
                "drawup hazard features empty after normalization"
            )
        return frame

    @staticmethod
    def _latest_trough(
        records: list[tuple[int, float, float]],
        window: int,
    ) -> tuple[float, int]:
        subset = records[-int(window):]
        if not subset:
            raise CandidateContinuationError(
                "latest trough requested without path state"
            )
        minimum = min(float(row[1]) for row in subset)
        # Source deque pops >=, therefore equal minima resolve to the latest
        # occurrence in the active window.
        index = max(
            int(row[0])
            for row in subset
            if float(row[1]) == minimum
        )
        return float(minimum), int(index)

    @staticmethod
    def _positive_tail_concentration(values: list[float], eps: float) -> float:
        arr = np.asarray(values, dtype="float64")
        positive = arr[np.isfinite(arr) & (arr > 0)]
        if positive.size == 0:
            return math.nan
        ordered = np.sort(positive)
        return float(
            ordered[-min(3, ordered.size):].sum()
            / (ordered.sum() + eps)
        )

    @staticmethod
    def _largest_positive(values: list[float]) -> float:
        arr = np.asarray(values, dtype="float64")
        positive = arr[np.isfinite(arr) & (arr > 0)]
        return math.nan if positive.size == 0 else float(positive.max())

    @staticmethod
    def _new_episode(trough: float, trough_index: int) -> dict[str, Any]:
        return {
            "trough": float(trough),
            "trough_index": int(trough_index),
            "peak": float(trough),
            "peak_index": int(trough_index),
            "new_high_count": 0,
            "maximum_pullback": 0.0,
            "positive_count": 0,
        }

    def _advance_episode(
        self,
        episode: dict[str, Any],
        price: float,
        index: int,
        positive_return: bool,
    ) -> tuple[float, bool]:
        if positive_return:
            episode["positive_count"] += 1

        new_high = bool(
            float(price) > float(episode["peak"]) + self.eps
        )
        if new_high:
            episode["peak"] = float(price)
            episode["peak_index"] = int(index)
            episode["new_high_count"] += 1
            pullback = 0.0
        else:
            denominator = (
                float(episode["peak"]) - float(episode["trough"])
            )
            pullback = (
                (float(episode["peak"]) - float(price))
                / (denominator + self.eps)
                if denominator > self.eps
                else 0.0
            )
            episode["maximum_pullback"] = max(
                float(episode["maximum_pullback"]),
                float(pullback),
            )
        return float(np.clip(pullback, 0.0, 2.0)), new_high

    @staticmethod
    def _severity_bin(value: float) -> int:
        if not np.isfinite(value):
            return -1
        if value < 0.60:
            return 0
        if value < 0.75:
            return 1
        if value < 0.90:
            return 2
        return 3

    @staticmethod
    def _age_bin(value: float) -> int:
        if not np.isfinite(value):
            return -1
        if value <= 10:
            return 0
        if value <= 30:
            return 1
        if value <= 80:
            return 2
        return 3

    @staticmethod
    def _concentration_bin(value: float) -> int:
        if not np.isfinite(value):
            return -1
        if value < 0.50:
            return 0
        if value < 0.70:
            return 1
        if value < 0.85:
            return 2
        return 3

    @staticmethod
    def _pullback_bin(value: float) -> int:
        if not np.isfinite(value):
            return -1
        if value <= 0.10:
            return 0
        if value <= 0.25:
            return 1
        if value <= 0.50:
            return 2
        return 3

    @staticmethod
    def _update_counter(
        counter: dict[Any, list[float]],
        key: Any,
        outcome: int,
        lag: int,
    ) -> None:
        row = counter[key]
        row[0] += float(outcome)
        row[1] += 1.0
        row[2] += float(lag)

    def _apply_pending(self) -> None:
        state = self._state
        for s_bin, a_bin, c_bin, p_bin, outcome, lag in state.pending_updates:
            self._update_counter(
                state.primary,
                (s_bin, a_bin, c_bin, p_bin),
                outcome,
                lag,
            )
            self._update_counter(
                state.secondary,
                (s_bin, c_bin, p_bin),
                outcome,
                lag,
            )
            self._update_counter(
                state.tertiary,
                (s_bin, p_bin),
                outcome,
                lag,
            )
            self._update_counter(
                state.quaternary,
                s_bin,
                outcome,
                lag,
            )
            state.global_counter[0] += float(outcome)
            state.global_counter[1] += 1.0
            state.global_counter[2] += float(lag)
        state.pending_updates = []

    def _resolve_existing_observations(
        self,
        close: float,
        index: int,
    ) -> None:
        state = self._state
        remaining: list[dict[str, Any]] = []

        for observation in state.observations:
            age = int(index - int(observation["start_index"]))
            if age <= 0:
                remaining.append(observation)
                continue
            if age > self.hazard_resolution_max_bars:
                continue

            snapshot_close = float(observation["snapshot_close"])
            peak = float(observation["peak"])
            event_vol = float(observation["event_vol"])

            reversal_price = snapshot_close * (
                1.0 - self.reversal_buffer_vol * event_vol
            )
            continuation_price = peak * (
                1.0 + self.new_high_buffer_vol * event_vol
            )

            outcome: Optional[int] = None
            # Match source tie order: reversal is checked before continuation.
            if close <= reversal_price:
                outcome = 1
            elif close >= continuation_price:
                outcome = 0

            if outcome is not None:
                state.pending_updates.append(
                    (
                        int(observation["severity_bin"]),
                        int(observation["age_bin"]),
                        int(observation["concentration_bin"]),
                        int(observation["pullback_bin"]),
                        int(outcome),
                        int(age),
                    )
                )
            elif age < self.hazard_resolution_max_bars:
                remaining.append(observation)

        state.observations = remaining

    def _event_vol(self, close: float) -> float:
        state = self._state
        history = state.event_close_history
        if len(history) >= self.event_vol_lookback:
            lag = float(history[-self.event_vol_lookback])
            with np.errstate(divide="ignore", invalid="ignore"):
                event_return = float(
                    np.log((close + self.eps) / (lag + self.eps))
                )
        else:
            event_return = math.nan

        variance = float(_ewm_var_update(state.ewm_var, event_return))
        if np.isfinite(variance) and variance >= 0.0:
            event_vol = max(
                math.sqrt(variance),
                self.min_event_vol,
            )
        else:
            event_vol = math.nan

        history.append(float(close))
        if len(history) > self.event_vol_lookback:
            del history[
                : len(history) - self.event_vol_lookback
            ]
        return float(event_vol)

    def _hazard_probability(
        self,
        severity_percentile: float,
        age: float,
        concentration_percentile: float,
        pullback: float,
    ) -> tuple[float, float, float, float, tuple[int, int, int, int]]:
        state = self._state
        s_bin = self._severity_bin(severity_percentile)
        a_bin = self._age_bin(age)
        c_bin = self._concentration_bin(concentration_percentile)
        p_bin = self._pullback_bin(pullback)

        if min(s_bin, a_bin, c_bin, p_bin) < 0:
            return (
                math.nan,
                0.0,
                5.0,
                math.nan,
                (s_bin, a_bin, c_bin, p_bin),
            )

        candidates = [
            (0, state.primary[(s_bin, a_bin, c_bin, p_bin)]),
            (1, state.secondary[(s_bin, c_bin, p_bin)]),
            (2, state.tertiary[(s_bin, p_bin)]),
            (3, state.quaternary[s_bin]),
            (4, state.global_counter),
        ]

        selected_level = 5
        selected = state.global_counter
        for level, row in candidates:
            if float(row[1]) >= self.min_hazard_cell_observations:
                selected_level = int(level)
                selected = row
                break

        successes, count, lag_sum = (
            float(selected[0]),
            float(selected[1]),
            float(selected[2]),
        )
        probability = (
            successes + self.hazard_prior_alpha
        ) / (
            count
            + self.hazard_prior_alpha
            + self.hazard_prior_beta
        )
        resolution_lag = (
            lag_sum / count if count > 0.0 else math.nan
        )
        return (
            float(probability),
            float(count),
            float(selected_level),
            float(resolution_lag),
            (s_bin, a_bin, c_bin, p_bin),
        )

    def _append_bounded_histories(
        self,
        *,
        current_records: list[tuple[int, float, float]],
        severity: float,
        concentration: float,
        return_value: float,
        positive_flag: int,
        new_high_flag: int,
        positive_jump_flag: int,
    ) -> None:
        state = self._state

        state.path = current_records[-self.max_trough_window:]

        state.severity_history.append(float(severity))
        if len(state.severity_history) > self.severity_baseline_window:
            del state.severity_history[
                : len(state.severity_history)
                - self.severity_baseline_window
            ]

        state.concentration_history.append(float(concentration))
        if (
            len(state.concentration_history)
            > self.concentration_baseline_window
        ):
            del state.concentration_history[
                : len(state.concentration_history)
                - self.concentration_baseline_window
            ]

        state.return_history.append(float(return_value))
        if len(state.return_history) > 100:
            del state.return_history[
                : len(state.return_history) - 100
            ]

        state.positive_flags.append(int(positive_flag))
        if len(state.positive_flags) > self.concentration_window:
            del state.positive_flags[
                : len(state.positive_flags)
                - self.concentration_window
            ]

        state.new_high_flags.append(int(new_high_flag))
        if len(state.new_high_flags) > 20:
            del state.new_high_flags[
                : len(state.new_high_flags) - 20
            ]

        state.positive_jump_flags.append(int(positive_jump_flag))
        if len(state.positive_jump_flags) > 20:
            del state.positive_jump_flags[
                : len(state.positive_jump_flags) - 20
            ]

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any] | None:
        state = self._state
        index = int(state.processed_rows)

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

        previous_close = (
            float(state.path[-1][1])
            if state.path
            else math.nan
        )
        if np.isfinite(previous_close):
            with np.errstate(divide="ignore", invalid="ignore"):
                log_return = float(
                    np.log(
                        (close + self.eps)
                        / (previous_close + self.eps)
                    )
                )
        else:
            log_return = math.nan
        negative_event_bar = bool(
            np.isfinite(log_return) and log_return < 0.0
        )
        positive_event_bar = bool(
            np.isfinite(log_return) and log_return > 0.0
        )

        event_vol = self._event_vol(close)

        current_records = state.path + [
            (index, float(close), float(log_return))
        ]
        trough50, _ = self._latest_trough(current_records, 50)
        trough100, trough_index100 = self._latest_trough(
            current_records,
            self.primary_trough_window,
        )
        trough250, _ = self._latest_trough(current_records, 250)

        depth50 = float(close / (trough50 + self.eps) - 1.0)
        depth100 = float(close / (trough100 + self.eps) - 1.0)
        depth250 = float(close / (trough250 + self.eps) - 1.0)
        severity = float(max(0.0, depth100))
        drawup_active = bool(severity > self.eps)

        # Source rebuilds episode state whenever the current rolling-100 trough
        # identity changes.
        if int(trough_index100) != int(state.episode_trough_index):
            episode = self._new_episode(trough100, trough_index100)
            pullback = 0.0
            made_high = False
            for record_index, price, record_return in current_records:
                if int(record_index) <= int(trough_index100):
                    continue
                pullback, made_high = self._advance_episode(
                    episode,
                    float(price),
                    int(record_index),
                    bool(
                        np.isfinite(record_return)
                        and record_return > 0.0
                    ),
                )
            state.episode = episode
            state.episode_trough_index = int(trough_index100)
        else:
            if state.episode is None:
                raise CandidateContinuationError(
                    "drawup episode state missing"
                )
            episode = state.episode
            pullback, made_high = self._advance_episode(
                episode,
                close,
                index,
                positive_event_bar,
            )

        if state.episode is None:
            raise CandidateContinuationError(
                "drawup episode failed to initialize"
            )
        episode = state.episode

        drawup_peak = float(episode["peak"])
        peak_index = int(episode["peak_index"])
        bars_since_trough = float(index - trough_index100)
        bars_since_peak = float(index - peak_index)
        pullback_from_peak = float(
            max(
                0.0,
                (drawup_peak - close)
                / (drawup_peak + self.eps),
            )
        )
        maximum_pullback = float(episode["maximum_pullback"])
        new_high_count = int(episode["new_high_count"])
        high_renewal_rate = float(
            new_high_count / (bars_since_trough + 1.0)
        )
        positive_fraction_since_trough = float(
            int(episode["positive_count"])
            / max(1, int(index - trough_index100))
        )

        # Rolling severity percentile: current severity against the prior 1000
        # observations, exactly matching rolling(window=1001).apply().
        if len(state.severity_history) >= self.severity_baseline_window:
            previous = np.asarray(
                state.severity_history[
                    -self.severity_baseline_window:
                ],
                dtype="float64",
            )
            severity_percentile = float(
                np.mean(previous <= severity)
            )
        else:
            severity_percentile = math.nan

        def lagged_severity(lag: int) -> float:
            if len(state.severity_history) >= int(lag):
                return float(state.severity_history[-int(lag)])
            return math.nan

        severity_3 = lagged_severity(3)
        severity_5 = lagged_severity(5)
        severity_10 = lagged_severity(10)
        velocity3 = (
            float(severity - severity_3)
            if np.isfinite(severity_3)
            else math.nan
        )
        velocity5 = (
            float(severity - severity_5)
            if np.isfinite(severity_5)
            else math.nan
        )
        velocity10 = (
            float(severity - severity_10)
            if np.isfinite(severity_10)
            else math.nan
        )
        acceleration = (
            float(velocity3 - velocity10)
            if np.isfinite(velocity3) and np.isfinite(velocity10)
            else math.nan
        )

        # Current rolling return windows.
        return20 = (
            state.return_history + [float(log_return)]
        )[-self.concentration_window:]
        return50 = (
            state.return_history + [float(log_return)]
        )[-50:]

        positive_flag = int(positive_event_bar)
        positive_flags20 = (
            state.positive_flags + [positive_flag]
        )[-self.concentration_window:]
        positive_return_fraction20 = (
            float(np.mean(positive_flags20))
            if len(positive_flags20) >= self.concentration_window
            else math.nan
        )

        returns20_finite = (
            len(return20) >= self.concentration_window
            and np.isfinite(
                np.asarray(return20, dtype="float64")
            ).all()
        )
        if returns20_finite:
            arr20 = np.asarray(return20, dtype="float64")
            positive20 = np.clip(arr20, 0.0, None)
            negative20 = np.abs(np.clip(arr20, None, 0.0))
            cumulative_positive20 = float(positive20.sum())
            concentration20 = self._positive_tail_concentration(
                return20,
                self.eps,
            )
            largest_positive20 = self._largest_positive(return20)
            realized_skew20 = float(
                pd.Series(arr20, dtype="float64").skew()
            )
            upside_semivariance20 = float(
                np.mean(np.square(positive20))
            )
            upside_downmove_ratio20 = float(
                positive20.sum()
                / (negative20.sum() + self.eps)
            )
        else:
            cumulative_positive20 = math.nan
            concentration20 = math.nan
            largest_positive20 = math.nan
            realized_skew20 = math.nan
            upside_semivariance20 = math.nan
            upside_downmove_ratio20 = math.nan

        if (
            len(return50) >= 50
            and np.isfinite(
                np.asarray(return50, dtype="float64")
            ).all()
        ):
            realized_skew50 = float(
                pd.Series(
                    np.asarray(return50, dtype="float64")
                ).skew()
            )
        else:
            realized_skew50 = math.nan

        # rolling(501, min_periods=501).apply(current-percentile)
        if (
            np.isfinite(concentration20)
            and len(state.concentration_history)
            >= self.concentration_baseline_window
        ):
            concentration_previous = np.asarray(
                state.concentration_history[
                    -self.concentration_baseline_window:
                ],
                dtype="float64",
            )
            if np.isfinite(concentration_previous).all():
                concentration_percentile = float(
                    np.mean(
                        concentration_previous
                        <= concentration20
                    )
                )
            else:
                concentration_percentile = math.nan
        else:
            concentration_percentile = math.nan

        cumulative_return_since_trough = float(
            np.log(
                (close + self.eps)
                / (trough100 + self.eps)
            )
        )

        # Current positive-jump flag uses the prior rolling-100 return mean/std.
        previous_returns100 = np.asarray(
            state.return_history[-100:],
            dtype="float64",
        )
        valid_previous = previous_returns100[
            np.isfinite(previous_returns100)
        ]
        if valid_previous.size >= 30:
            jump_mean = float(valid_previous.mean())
            jump_std = float(
                pd.Series(
                    valid_previous,
                    dtype="float64",
                ).std()
            )
        else:
            jump_mean = math.nan
            jump_std = math.nan

        positive_jump_flag = int(
            np.isfinite(log_return)
            and np.isfinite(jump_mean)
            and np.isfinite(jump_std)
            and log_return > jump_mean + 2.0 * jump_std
        )
        jump_flags20 = (
            state.positive_jump_flags + [positive_jump_flag]
        )[-20:]
        positive_jump_count20 = (
            float(sum(jump_flags20))
            if len(jump_flags20) >= 20
            else math.nan
        )

        new_high_flags20 = (
            state.new_high_flags + [int(made_high)]
        )[-20:]
        new_high_count20 = float(sum(new_high_flags20))

        if "ATR_14" in row:
            atr_value = self._optional_number(row.get("ATR_14"))
        else:
            atr_value = (
                close * event_vol
                if np.isfinite(event_vol)
                else math.nan
            )
        distance_from_peak_atr = (
            float(
                (drawup_peak - close)
                / (atr_value + self.eps)
            )
            if np.isfinite(atr_value)
            else math.nan
        )

        bar_range = max(high - low, self.eps)
        body = close - open_
        body_direction = float(np.sign(body))
        body_share = float(abs(body) / (bar_range + self.eps))
        close_location = float(
            (close - low) / (bar_range + self.eps)
        )

        # Outcomes resolved on the previous bar become available now.  Outcomes
        # resolved by the current close are queued for the next bar.
        self._apply_pending()

        (
            hazard_probability,
            hazard_cell_count,
            hazard_backoff_level,
            hazard_resolution_lag,
            bins,
        ) = self._hazard_probability(
            severity_percentile,
            bars_since_trough,
            concentration_percentile,
            pullback,
        )
        hazard_global_count = float(state.global_counter[1])

        active_state = int(
            drawup_active
            and np.isfinite(severity_percentile)
            and severity_percentile
            >= self.min_drawup_severity_percentile
            and np.isfinite(concentration_percentile)
            and concentration_percentile
            >= self.min_positive_tail_concentration_percentile
            and np.isfinite(pullback)
            and pullback >= self.min_pullback_fraction
            and pullback <= self.max_pullback_fraction
            and np.isfinite(hazard_probability)
            and hazard_probability
            >= self.min_reversal_hazard_probability
            and hazard_cell_count
            >= self.min_hazard_cell_observations
        )

        # Current close may resolve previously-open observations, but those
        # outcomes remain pending until the next bar.
        self._resolve_existing_observations(close, index)

        # Observation spacing resets when the rolling-100 trough identity changes.
        if int(trough_index100) != int(state.observation_trough_marker):
            state.observation_trough_marker = int(trough_index100)
            state.last_observation_index = -(10**9)

        s_bin, a_bin, c_bin, p_bin = bins
        observation_eligible = (
            drawup_active
            and np.isfinite(severity_percentile)
            and np.isfinite(concentration_percentile)
            and np.isfinite(pullback)
            and pullback <= self.max_pullback_fraction
            and index - state.last_observation_index
            >= self.hazard_observation_gap_bars
            and min(s_bin, a_bin, c_bin, p_bin) >= 0
        )
        if observation_eligible:
            # Source consumes the observation gap before knowing whether the
            # future outcome will ever resolve.
            state.last_observation_index = index
            if (
                np.isfinite(event_vol)
                and event_vol > 0.0
                and close > 0.0
                and drawup_peak > 0.0
            ):
                state.observations.append(
                    {
                        "start_index": index,
                        "snapshot_close": float(close),
                        "peak": float(drawup_peak),
                        "event_vol": float(event_vol),
                        "severity_bin": int(s_bin),
                        "age_bin": int(a_bin),
                        "concentration_bin": int(c_bin),
                        "pullback_bin": int(p_bin),
                    }
                )

        # Source event extraction state.
        if active_state == 1 and not state.previous_active:
            state.emitted_in_state = False
        if active_state != 1:
            state.previous_active = False
            state.emitted_in_state = False
        else:
            state.previous_active = True

        emitted = None
        if (
            active_state == 1
            and negative_event_bar
            and index - state.last_event_index
            >= self.min_event_gap_bars
        ):
            state_entry = int(not state.emitted_in_state)
            state.emitted_in_state = True
            state.last_event_index = index

            # At a mature active hazard state these fields are finite by
            # construction; retain an explicit fail-closed check.
            required_numeric = [
                event_vol,
                log_return,
                severity_percentile,
                concentration_percentile,
                pullback,
                hazard_probability,
                hazard_cell_count,
            ]
            if not all(np.isfinite(value) for value in required_numeric):
                emitted = None
            else:
                emitted = {
                    "Datetime": timestamp,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": 0,
                    "Event_Vol": float(event_vol),
                    "event_id": (
                        f"{self.identity.edge_id}|{timestamp.isoformat()}|"
                        "drawup_reversal_hazard_impulse"
                    ),
                    "candidate_event_generator": (
                        self.identity.candidate_event_generator
                    ),
                    "Drawup_Reversal_Hazard_Event": 1,
                    "Drawup_Reversal_Hazard_Active_State": 1,
                    "Drawup_Reversal_Hazard_State_Entry": int(state_entry),
                    "Drawup_Reversal_Hazard_Renewed_Impulse": (
                        1 - int(state_entry)
                    ),
                    "Negative_Event_Bar": 1,
                    "Log_Return_1": float(log_return),
                    "Drawup_Active": 1,
                    "Drawup_Trough_50": float(trough50),
                    "Drawup_Trough_100": float(trough100),
                    "Drawup_Trough_250": float(trough250),
                    "Drawup_Depth_50": float(depth50),
                    "Drawup_Depth_100": float(depth100),
                    "Drawup_Depth_250": float(depth250),
                    "Drawup_Severity_100": float(severity),
                    "Drawup_Severity_Percentile_100_Over_1000": float(
                        severity_percentile
                    ),
                    "Bars_Since_Trough_100": float(bars_since_trough),
                    "Drawup_Peak": float(drawup_peak),
                    "Bars_Since_Drawup_Peak": float(bars_since_peak),
                    "Pullback_From_Peak": float(pullback_from_peak),
                    "Pullback_Fraction_Of_Drawup": float(pullback),
                    "Maximum_Pullback_Fraction": float(maximum_pullback),
                    "New_High_Bar": int(made_high),
                    "New_High_Count_20": float(new_high_count20),
                    "New_High_Count_Since_Trough": int(new_high_count),
                    "High_Renewal_Rate": float(high_renewal_rate),
                    "Positive_Return_Fraction_20": float(
                        positive_return_fraction20
                    ),
                    "Positive_Return_Fraction_Since_Trough": float(
                        positive_fraction_since_trough
                    ),
                    "Cumulative_Return_Since_Trough": float(
                        cumulative_return_since_trough
                    ),
                    "Cumulative_Positive_Return_20": float(
                        cumulative_positive20
                    ),
                    "Positive_Tail_Concentration_20": float(
                        concentration20
                    ),
                    "Positive_Tail_Concentration_Percentile_20_Over_500": float(
                        concentration_percentile
                    ),
                    "Largest_Positive_Return_20": float(
                        largest_positive20
                    ),
                    "Top3_Positive_Return_Contribution_20": float(
                        concentration20
                    ),
                    "Realized_Skewness_20": float(realized_skew20),
                    "Realized_Skewness_50": float(realized_skew50),
                    "Positive_Jump_Count_20": float(
                        positive_jump_count20
                    ),
                    "Upside_Semivariance_20": float(
                        upside_semivariance20
                    ),
                    "Upside_Downmove_Ratio_20": float(
                        upside_downmove_ratio20
                    ),
                    "Drawup_Velocity_3": float(velocity3),
                    "Drawup_Velocity_5": float(velocity5),
                    "Drawup_Velocity_10": float(velocity10),
                    "Drawup_Acceleration_3_10": float(acceleration),
                    "Distance_From_Peak_ATR": float(
                        distance_from_peak_atr
                    ),
                    "Drawup_Reversal_Hazard_Probability": float(
                        hazard_probability
                    ),
                    "Drawup_Reversal_Hazard_Cell_Count": float(
                        hazard_cell_count
                    ),
                    "Drawup_Reversal_Hazard_Backoff_Level": float(
                        hazard_backoff_level
                    ),
                    "Drawup_Reversal_Hazard_Resolution_Lag": float(
                        hazard_resolution_lag
                    ),
                    "Drawup_Reversal_Hazard_Global_Count": float(
                        hazard_global_count
                    ),
                    "Body_Direction": float(body_direction),
                    "Body_Share": float(body_share),
                    "Close_Location": float(close_location),
                    "Open": float(open_),
                    "High": float(high),
                    "Low": float(low),
                    "Close": float(close),
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "primary_trough_window": self.primary_trough_window,
                    "severity_baseline_window": (
                        self.severity_baseline_window
                    ),
                    "concentration_window": self.concentration_window,
                    "concentration_baseline_window": (
                        self.concentration_baseline_window
                    ),
                    "hazard_resolution_max_bars": (
                        self.hazard_resolution_max_bars
                    ),
                    "hazard_observation_gap_bars": (
                        self.hazard_observation_gap_bars
                    ),
                    "new_high_buffer_vol": self.new_high_buffer_vol,
                    "reversal_buffer_vol": self.reversal_buffer_vol,
                    "min_drawup_severity_percentile": (
                        self.min_drawup_severity_percentile
                    ),
                    "min_positive_tail_concentration_percentile": (
                        self.min_positive_tail_concentration_percentile
                    ),
                    "min_pullback_fraction": self.min_pullback_fraction,
                    "max_pullback_fraction": self.max_pullback_fraction,
                    "min_reversal_hazard_probability": (
                        self.min_reversal_hazard_probability
                    ),
                    "min_hazard_cell_observations": (
                        self.min_hazard_cell_observations
                    ),
                    "min_gap_bars": self.min_event_gap_bars,
                }
                for name in OPTIONAL_CONTEXT:
                    if name in row:
                        emitted[name] = self._optional_number(row[name])

        self._append_bounded_histories(
            current_records=current_records,
            severity=severity,
            concentration=concentration20,
            return_value=log_return,
            positive_flag=positive_flag,
            new_high_flag=int(made_high),
            positive_jump_flag=positive_jump_flag,
        )

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
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = self._normalize(features)
        self.reset()
        rows: list[dict[str, Any]] = []
        for row in frame.to_dict(orient="records"):
            event = self._advance(row, enforce_append_only=False)
            if event is not None:
                rows.append(event)
        return pd.DataFrame(rows)

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
            "path_rows": len(state.path),
            "severity_history_rows": len(state.severity_history),
            "concentration_history_rows": len(
                state.concentration_history
            ),
            "return_history_rows": len(state.return_history),
            "open_hazard_observations": len(state.observations),
            "pending_hazard_updates": len(state.pending_updates),
            "global_hazard_observations": int(
                state.global_counter[1]
            ),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"drawup hazard D.1 semantic constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> DrawupReversalHazardShortCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature != DRAWUP_REVERSAL_HAZARD_SHORT_SIGNATURE:
        raise CandidateContinuationError(
            f"drawup hazard provider does not support {signature}"
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

    # The authoritative reachable source hard-codes 50 and 250 in
    # _build_path_features().  SECONDARY_TROUGH_WINDOWS is descriptive only and
    # is not referenced by the candidate entry/reachable functions, so D.1 does
    # not include it in semantic_constants.  These literals are nevertheless
    # protected by this exact candidate_logic_signature because the reachable
    # function source is part of the D.1 signature hash.
    secondary = (50, 250)

    return DrawupReversalHazardShortCandidateContinuation(
        identity=identity,
        primary_trough_window=int(
            _constant(constants, "PRIMARY_TROUGH_WINDOW")
        ),
        secondary_trough_windows=tuple(int(x) for x in secondary),
        severity_baseline_window=int(
            _constant(constants, "SEVERITY_BASELINE_WINDOW")
        ),
        concentration_window=int(
            _constant(constants, "CONCENTRATION_WINDOW")
        ),
        concentration_baseline_window=int(
            _constant(constants, "CONCENTRATION_BASELINE_WINDOW")
        ),
        hazard_resolution_max_bars=int(
            _constant(constants, "HAZARD_RESOLUTION_MAX_BARS")
        ),
        hazard_observation_gap_bars=int(
            _constant(constants, "HAZARD_OBSERVATION_GAP_BARS")
        ),
        new_high_buffer_vol=float(
            _constant(constants, "NEW_HIGH_BUFFER_VOL")
        ),
        reversal_buffer_vol=float(
            _constant(constants, "REVERSAL_BUFFER_VOL")
        ),
        min_drawup_severity_percentile=float(
            _constant(constants, "MIN_DRAWUP_SEVERITY_PERCENTILE")
        ),
        min_positive_tail_concentration_percentile=float(
            _constant(
                constants,
                "MIN_POSITIVE_TAIL_CONCENTRATION_PERCENTILE",
            )
        ),
        min_pullback_fraction=float(
            _constant(constants, "MIN_PULLBACK_FRACTION")
        ),
        max_pullback_fraction=float(
            _constant(constants, "MAX_PULLBACK_FRACTION")
        ),
        min_reversal_hazard_probability=float(
            _constant(constants, "MIN_REVERSAL_HAZARD_PROBABILITY")
        ),
        min_hazard_cell_observations=int(
            _constant(constants, "MIN_HAZARD_CELL_OBSERVATIONS")
        ),
        min_event_gap_bars=int(
            _constant(constants, "MIN_EVENT_GAP_BARS")
        ),
        hazard_prior_alpha=float(
            _constant(constants, "HAZARD_PRIOR_ALPHA")
        ),
        hazard_prior_beta=float(
            _constant(constants, "HAZARD_PRIOR_BETA")
        ),
        event_vol_lookback=int(
            _constant(constants, "EVENT_VOL_LOOKBACK")
        ),
        event_vol_ewm_span=int(
            _constant(constants, "EVENT_VOL_EWM_SPAN")
        ),
        min_event_vol=float(
            _constant(constants, "MIN_EVENT_VOL")
        ),
        eps=float(_constant(constants, "EPS")),
    )
