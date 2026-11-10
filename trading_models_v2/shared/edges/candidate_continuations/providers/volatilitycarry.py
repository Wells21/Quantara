from __future__ import annotations

"""Exact incremental volatilitycarry_long candidate continuation for Phase D."""

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
# LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS). Each edge now carries a distinct
# exact hours value baked into semantic_constants (JPN=8.0, US500=2.837,
# XAUUSD=6.0), so each has its own distinct signature -- the old JPN/
# XAUUSD-shared grouping no longer holds. NASDAQ/volatilitycarry_long is
# retired: it failed Gate D after the relabel and is not in the current
# live manifest.
VOLATILITYCARRY_LONG_SIGNATURE_JPN = "2b94a51f62efa9ab8f5e702a33f6ccc25230779acb97eafeb363e57a45ec0386"
VOLATILITYCARRY_LONG_SIGNATURE_US500 = "fb9336a88f4160cf34fe6112c937cfd02e71e2bfe5ddd15c95f41f445f802efe"
VOLATILITYCARRY_LONG_SIGNATURE_XAUUSD = "a19c7b9f9ef53f2a247be6049c8ff6cb4d88bce43ca1b00e18ec61354b8bda39"
VOLATILITYCARRY_LONG_SIGNATURE = VOLATILITYCARRY_LONG_SIGNATURE_JPN

PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilitycarry_candidate",
    "provider_version": "1",
    "supported_signatures": {
        VOLATILITYCARRY_LONG_SIGNATURE_JPN: {"side_specialization": "long"},
        VOLATILITYCARRY_LONG_SIGNATURE_US500: {"side_specialization": "long"},
        VOLATILITYCARRY_LONG_SIGNATURE_XAUUSD: {"side_specialization": "long"},
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
    "Volatility_Carry_Event_Type",
    "Drift_TStat",
    "Volatility_Ratio",
    "Is_Calm_Vol",
    "Bar_Volatility_Bps",
    "Low_Vol_Threshold",
    "Vol_Median",
    "Drift_Mean",
    "Drift_Std",
    "Bar_Return",
    "Body_Direction",
    "Body_Share",
    "Close_Location",
    "event_known_after_close",
    "uses_future_information",
    "Open",
    "High",
    "Low",
    "Close",
    *OPTIONAL_CONTEXT,
    "vol_lookback",
    "low_vol_quantile",
    "drift_lookback",
    "drift_tstat_threshold",
    "max_activity_ratio",
    "min_gap_bars",
    "event_vol_lookback_hours",
    "event_vol_ewm_span",
]


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    close_history_ns: list[int] = field(default_factory=list)
    volatility_history: list[float] = field(default_factory=list)
    bar_return_history: list[float] = field(default_factory=list)
    last_close: float = math.nan
    last_selected_event_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class VolatilityCarryCandidateContinuation:
    def __init__(
        self,
        *,
        identity,
        eps: float,
        min_gap_bars: int,
        vol_lookback: int,
        low_vol_quantile: float,
        drift_lookback: int,
        drift_tstat_threshold: float,
        max_activity_ratio: float,
        min_obs: int,
        event_vol_lookback_hours: float,
        event_vol_ewm_span: int,
        min_vol: float,
    ) -> None:
        if eps <= 0 or min_vol <= 0:
            raise CandidateContinuationError("EPS/MIN_VOL must be positive")
        if min_gap_bars < 0:
            raise CandidateContinuationError("MIN_GAP_BARS must be >= 0")
        for value in (
            vol_lookback,
            drift_lookback,
            min_obs,
            event_vol_ewm_span,
        ):
            if int(value) <= 0:
                raise CandidateContinuationError(
                    "volatility-carry lookbacks/spans must be positive"
                )
        if event_vol_lookback_hours <= 0:
            raise CandidateContinuationError(
                "volatility-carry lookbacks/spans must be positive"
            )
        if min_obs > min(vol_lookback, drift_lookback):
            raise CandidateContinuationError(
                "MIN_OBS exceeds required rolling lookback"
            )
        if not (0.0 <= low_vol_quantile <= 1.0):
            raise CandidateContinuationError(
                "LOW_VOL_QUANTILE must be in [0,1]"
            )

        self.identity = identity
        self.eps = float(eps)
        self.min_gap_bars = int(min_gap_bars)
        self.vol_lookback = int(vol_lookback)
        self.low_vol_quantile = float(low_vol_quantile)
        self.drift_lookback = int(drift_lookback)
        self.drift_tstat_threshold = float(drift_tstat_threshold)
        self.max_activity_ratio = float(max_activity_ratio)
        self.min_obs = int(min_obs)
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
                f"features missing volatility-carry columns: {missing}"
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
        frame = frame.dropna(subset=required)
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise CandidateContinuationError(
                "features empty after volatility-carry normalization"
            )
        return frame

    @staticmethod
    def _finite_values(values: list[float]) -> np.ndarray:
        array = np.asarray(values, dtype="float64")
        return array[np.isfinite(array)]

    def _prior_volatility_context(
        self,
        current: float,
    ) -> tuple[float, float, float, bool]:
        history = self._state.volatility_history[-self.vol_lookback:]
        finite = self._finite_values(history)

        if finite.size >= self.min_obs:
            series = pd.Series(finite, dtype="float64")
            low_threshold = float(
                series.quantile(self.low_vol_quantile)
            )
            median = float(series.median())
            ratio = float(current / (median + self.eps))
            is_calm = bool(current <= low_threshold)
        else:
            low_threshold = math.nan
            median = math.nan
            ratio = math.nan
            is_calm = False

        self._state.volatility_history.append(float(current))
        if len(self._state.volatility_history) > self.vol_lookback:
            del self._state.volatility_history[
                : len(self._state.volatility_history) - self.vol_lookback
            ]

        return low_threshold, median, ratio, is_calm

    def _prior_drift(self) -> tuple[float, float, float]:
        history = self._state.bar_return_history[-self.drift_lookback:]
        finite = self._finite_values(history)

        if finite.size < self.min_obs:
            return math.nan, math.nan, math.nan

        series = pd.Series(finite, dtype="float64")
        mean = float(series.mean())
        std = float(series.std())
        tstat = float(
            mean
            / (
                (std + self.eps)
                / math.sqrt(float(self.drift_lookback))
            )
        )
        return mean, std, tstat

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
                ret = float(
                    np.log((close + self.eps) / (lag + self.eps))
                )
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
        bar_vol = self._number(
            row["Bar_Volatility_Bps"],
            "Bar_Volatility_Bps",
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

        if source_index == 0 or not np.isfinite(state.last_close):
            bar_return = math.nan
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                bar_return = float(
                    np.log(
                        (close + self.eps)
                        / (state.last_close + self.eps)
                    )
                )

        low_threshold, vol_median, vol_ratio, is_calm = (
            self._prior_volatility_context(bar_vol)
        )
        drift_mean, drift_std, drift_tstat = self._prior_drift()

        bar_range = max(high - low, self.eps)
        body = close - open_
        body_direction = float(np.sign(body))
        body_share = float(abs(body) / (bar_range + self.eps))
        close_location = float((close - low) / (bar_range + self.eps))

        event_vol = self._event_vol(close, timestamp)

        event_type = 0
        if (
            is_calm
            and np.isfinite(vol_ratio)
            and vol_ratio <= self.max_activity_ratio
            and np.isfinite(drift_tstat)
        ):
            if drift_tstat >= self.drift_tstat_threshold:
                event_type = 1
            elif drift_tstat <= -self.drift_tstat_threshold:
                event_type = -1

        emitted = None
        if (
            event_type != 0
            and source_index - state.last_selected_event_index
            >= self.min_gap_bars
        ):
            # Both +/- raw carry events consume spacing before long-only primary.
            state.last_selected_event_index = source_index

            # The source extraction also occurs before the later Event_Vol
            # dropna, so the spacing remains consumed if Event_Vol is not finite.
            if np.isfinite(event_vol):
                emitted = {
                    "Datetime": timestamp,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": int(event_type),
                    "Event_Vol": float(event_vol),
                    "event_id": (
                        f"{self.identity.edge_id}|{timestamp.isoformat()}|"
                        "volatility_carry"
                    ),
                    "candidate_event_generator": (
                        self.identity.candidate_event_generator
                    ),
                    "Volatility_Carry_Event_Type": int(event_type),
                    "Drift_TStat": float(drift_tstat),
                    "Volatility_Ratio": float(vol_ratio),
                    "Is_Calm_Vol": bool(is_calm),
                    "Bar_Volatility_Bps": float(bar_vol),
                    "Low_Vol_Threshold": float(low_threshold),
                    "Vol_Median": float(vol_median),
                    "Drift_Mean": float(drift_mean),
                    "Drift_Std": float(drift_std),
                    "Bar_Return": float(bar_return),
                    "Body_Direction": body_direction,
                    "Body_Share": body_share,
                    "Close_Location": close_location,
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": float(open_),
                    "High": float(high),
                    "Low": float(low),
                    "Close": float(close),
                }

                for name in OPTIONAL_CONTEXT:
                    if name in row:
                        emitted[name] = self._optional(row[name])

                emitted.update(
                    {
                        "vol_lookback": self.vol_lookback,
                        "low_vol_quantile": self.low_vol_quantile,
                        "drift_lookback": self.drift_lookback,
                        "drift_tstat_threshold": (
                            self.drift_tstat_threshold
                        ),
                        "max_activity_ratio": self.max_activity_ratio,
                        "min_gap_bars": self.min_gap_bars,
                        "event_vol_lookback_hours": self.event_vol_lookback_hours,
                        "event_vol_ewm_span": self.event_vol_ewm_span,
                    }
                )

        state.bar_return_history.append(float(bar_return))
        if len(state.bar_return_history) > self.drift_lookback:
            del state.bar_return_history[
                : len(state.bar_return_history) - self.drift_lookback
            ]

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
            "volatility_history_rows": len(state.volatility_history),
            "bar_return_history_rows": len(state.bar_return_history),
            "close_history_rows": len(state.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], name: str) -> Any:
    if name not in constants:
        raise CandidateContinuationError(
            f"volatility-carry D.1 constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> VolatilityCarryCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(
            f"volatility-carry provider does not support {signature}"
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

    return VolatilityCarryCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        vol_lookback=int(_constant(constants, "VOL_LOOKBACK")),
        low_vol_quantile=float(
            _constant(constants, "LOW_VOL_QUANTILE")
        ),
        drift_lookback=int(_constant(constants, "DRIFT_LOOKBACK")),
        drift_tstat_threshold=float(
            _constant(constants, "DRIFT_TSTAT_THRESHOLD")
        ),
        max_activity_ratio=float(
            _constant(constants, "MAX_ACTIVITY_RATIO")
        ),
        min_obs=int(_constant(constants, "MIN_OBS")),
        event_vol_lookback_hours=float(
            _constant(constants, "EVENT_VOL_LOOKBACK_HOURS")
        ),
        event_vol_ewm_span=int(
            _constant(constants, "EVENT_VOL_EWM_SPAN")
        ),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
