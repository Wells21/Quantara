from __future__ import annotations

"""Exact incremental reversal_long candidate continuation for Phase D."""

import bisect
from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd

from shared.edges.native_one_row_feature_engine import _ewm_var_state, _ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError, extract_candidate_source_identity

# 2026-09-25: regenerated after the calendar-time relabel rewrote
# candidate_events.py's volatility lookback from a bar-count constant
# (VOL_LOOKBACK) to a real-hours constant (EVENT_VOL_LOOKBACK_HOURS).
REVERSAL_LONG_SIGNATURE = "ac7ddc24607a4be811a27b0ac1af4f6a17ea07c3470c9f92daada66a587bb847"
PROVIDER_SPEC = {
    "provider_id": "phase_d4_reversal_long_candidate",
    "provider_version": "1",
    "supported_signatures": {REVERSAL_LONG_SIGNATURE: {"side_specialization": "long"}},
}

OPTIONAL_CONTEXT = (
    "Regime_State", "Regime_Entropy", "Regime_Duration",
    "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3",
)

@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    close_history_ns: list[int] = field(default_factory=list)
    deviation_history: list[float] = field(default_factory=list)
    kalman_mean: float = math.nan
    kalman_cov: float = 1.0
    kalman_initialized: bool = False
    last_selected_event_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None

class ReversalLongCandidateContinuation:
    PROCESS_VARIANCE = 1e-5
    MEASUREMENT_VARIANCE = 1e-3

    def __init__(
        self, *, identity, eps: float, vol_lookback_hours: float, vol_ewm_span: int,
        min_vol: float, dev_window: int, dev_z_threshold: float, min_gap_bars: int
    ) -> None:
        if eps <= 0 or min_vol <= 0 or vol_lookback_hours <= 0 or vol_ewm_span <= 0:
            raise CandidateContinuationError("invalid reversal volatility configuration")
        if dev_window <= 1 or dev_z_threshold <= 0 or min_gap_bars < 0:
            raise CandidateContinuationError("invalid reversal deviation configuration")
        self.identity = identity
        self.eps = float(eps)
        self.vol_lookback_hours = float(vol_lookback_hours)
        self.vol_lookback_ns = int(round(self.vol_lookback_hours * 3600.0 * 1e9))
        self.vol_ewm_span = int(vol_ewm_span)
        self.min_vol = float(min_vol)
        self.dev_window = int(dev_window)
        self.dev_z_threshold = float(dev_z_threshold)
        self.min_gap_bars = int(min_gap_bars)
        self._state = self._new_state()

    def _new_state(self) -> _State:
        return _State(
            ewm_var=_ewm_var_state(np.asarray([], dtype="float64"), self.vol_ewm_span)
        )

    def reset(self) -> None:
        self._state = self._new_state()

    @staticmethod
    def _ts(value: Any) -> pd.Timestamp:
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise CandidateContinuationError(f"invalid timestamp: {value!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _num(value: Any, name: str) -> float:
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise CandidateContinuationError(f"invalid {name}: {value!r}")
        return float(x)

    @staticmethod
    def _optional(value: Any) -> float:
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        return math.nan if pd.isna(x) else float(x)

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        required = ["Datetime","Bar_Start","Open","High","Low","Close"]
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        missing = [c for c in required if c not in features.columns]
        if missing:
            raise CandidateContinuationError(f"missing reversal columns: {missing}")
        out = features.copy()
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
        for c in ["Open","High","Low","Close",*OPTIONAL_CONTEXT]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=required).sort_values("Datetime").reset_index(drop=True)
        if out.empty:
            raise CandidateContinuationError("features empty after reversal normalization")
        return out

    def _event_vol(self, close: float, timestamp: pd.Timestamp) -> float:
        # Calendar-time backward lookup -- see absorption.py's identical
        # port (verified 2026-09-25 via a 4000-bar synthetic parity test
        # against shared/labels/calendar_time_barriers.py's
        # calendar_time_event_vol, max abs diff = 0.0).
        h = self._state.close_history
        h_ts = self._state.close_history_ns
        ts_ns = int(timestamp.value)
        target_ns = ts_ns - self.vol_lookback_ns
        pos = bisect.bisect_left(h_ts, target_ns) - 1
        if pos >= 0:
            lag = float(h[pos])
            with np.errstate(divide="ignore", invalid="ignore"):
                ret = float(np.log((close + self.eps)/(lag + self.eps)))
        else:
            ret = math.nan
        var = float(_ewm_var_update(self._state.ewm_var, ret))
        event_vol = max(math.sqrt(var), self.min_vol) if np.isfinite(var) and var >= 0 else math.nan
        h.append(float(close))
        h_ts.append(ts_ns)
        # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
        # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
        # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
        cutoff_ns = ts_ns - self.vol_lookback_ns
        drop = bisect.bisect_left(h_ts, cutoff_ns) - 1
        if drop > 0:
            del h[:drop]
            del h_ts[:drop]
        return event_vol

    def _kalman(self, close: float) -> float:
        s = self._state
        if not s.kalman_initialized:
            s.kalman_mean = float(close)
            s.kalman_cov = 1.0
            s.kalman_initialized = True
            return s.kalman_mean
        prior_mean = s.kalman_mean
        prior_cov = s.kalman_cov + self.PROCESS_VARIANCE
        gain = prior_cov / (prior_cov + self.MEASUREMENT_VARIANCE)
        s.kalman_mean = prior_mean + gain * (float(close) - prior_mean)
        s.kalman_cov = (1.0 - gain) * prior_cov
        return float(s.kalman_mean)

    def _advance(self, row: Mapping[str, Any], *, enforce_append_only: bool):
        s = self._state
        ts = self._ts(row["Datetime"])
        bar_start = self._ts(row["Bar_Start"])
        open_ = self._num(row["Open"], "Open")
        high = self._num(row["High"], "High")
        low = self._num(row["Low"], "Low")
        close = self._num(row["Close"], "Close")
        if enforce_append_only and s.last_timestamp is not None and ts <= s.last_timestamp:
            raise CandidateContinuationError("update_one requires increasing Datetime")

        i = s.processed_rows
        event_vol = self._event_vol(close, ts)
        kalman = self._kalman(close)
        dist = float((close - kalman) / (kalman + self.eps))

        # Source rolling mean/std INCLUDE the current Dist_Kalman_Local.
        s.deviation_history.append(dist)
        if len(s.deviation_history) > self.dev_window:
            del s.deviation_history[:len(s.deviation_history)-self.dev_window]
        if len(s.deviation_history) >= self.dev_window:
            series = pd.Series(s.deviation_history[-self.dev_window:], dtype="float64")
            mean = float(series.mean())
            std = max(float(series.std()), self.min_vol)
            z = float((dist - mean)/(std + self.eps))
        else:
            z = math.nan

        side = 0
        if np.isfinite(z):
            if z >= self.dev_z_threshold:
                side = 1
            elif z <= -self.dev_z_threshold:
                side = -1

        emitted = None
        if side != 0 and i - s.last_selected_event_index >= self.min_gap_bars:
            # Both overbought and oversold events consume the V1 spacing.
            s.last_selected_event_index = i
            if side == -1 and np.isfinite(event_vol):
                emitted = {
                    "Datetime": ts,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": 1,
                    "Event_Vol": float(event_vol),
                    "event_id": f"{self.identity.edge_id}|{ts.isoformat()}|oversold_kalman_deviation",
                    "candidate_event_generator": self.identity.candidate_event_generator,
                    "Deviation_Side": -1,
                    "Deviation_Z": float(z),
                    "Kalman_Close_Local": float(kalman),
                    "Dist_Kalman_Local": float(dist),
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": open_, "High": high, "Low": low, "Close": close,
                }
                for c in OPTIONAL_CONTEXT:
                    if c in row:
                        emitted[c] = self._optional(row[c])
                emitted["dev_window"] = self.dev_window
                emitted["dev_z_threshold"] = self.dev_z_threshold
                emitted["min_gap_bars"] = self.min_gap_bars
                emitted["vol_lookback_hours"] = self.vol_lookback_hours
                emitted["vol_ewm_span"] = self.vol_ewm_span

        s.processed_rows += 1
        s.last_timestamp = ts
        return emitted

    def bootstrap(self, features: pd.DataFrame):
        frame = self._normalize(features)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(self, row: Union[Mapping[str, Any], pd.Series]):
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features: pd.DataFrame) -> pd.DataFrame:
        frame = self._normalize(features)
        self.reset()
        rows = []
        for row in frame.to_dict(orient="records"):
            ev = self._advance(row, enforce_append_only=False)
            if ev is not None:
                rows.append(ev)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    def state_summary(self):
        s = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "processed_rows": s.processed_rows,
            "deviation_history_rows": len(s.deviation_history),
            "close_history_rows": len(s.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }

def _c(constants: Mapping[str, Any], name: str):
    if name not in constants:
        raise CandidateContinuationError(f"reversal D.1 constant missing: {name}")
    return constants[name]

def create_provider(*, candidate_logic_signature: str, d1_edge: Mapping[str, Any], v2_root):
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(f"unsupported reversal signature: {signature}")
    candidate = d1_edge.get("candidate") or {}
    constants = candidate.get("semantic_constants") or {}
    identity = extract_candidate_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return ReversalLongCandidateContinuation(
        identity=identity,
        eps=float(_c(constants, "EPS")),
        vol_lookback_hours=float(_c(constants, "EVENT_VOL_LOOKBACK_HOURS")),
        vol_ewm_span=int(_c(constants, "VOL_EWM_SPAN")),
        min_vol=float(_c(constants, "MIN_VOL")),
        dev_window=int(_c(constants, "DEV_WINDOW")),
        dev_z_threshold=float(_c(constants, "DEV_Z_THRESHOLD")),
        min_gap_bars=int(_c(constants, "MIN_GAP_BARS")),
    )
