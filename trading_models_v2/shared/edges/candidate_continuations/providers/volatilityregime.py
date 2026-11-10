from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd

from shared.edges.native_one_row_feature_engine import _ewm_var_state, _ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError, extract_candidate_source_identity

# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_
# LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS).
VOLATILITYREGIME_LONG_SIGNATURE = "4a136b76c91e9a1915692dc7c9e2791c09b3f2d316023382026c7c6274854aca"
PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilityregime_candidate",
    "provider_version": "1",
    "supported_signatures": {VOLATILITYREGIME_LONG_SIGNATURE: {}},
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
    volatility_history: list[float] = field(default_factory=list)
    last_selected_event_index: int = -(10**9)
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None

class VolatilityRegimeCandidateContinuation:
    def __init__(
        self, *, identity, eps, min_gap_bars, low_vol_quantile,
        expansion_multiplier, vol_lookback, recent_low_vol_lookback,
        event_vol_lookback_hours, event_vol_ewm_span, min_vol
    ):
        if eps <= 0 or min_vol <= 0 or min_gap_bars < 0:
            raise CandidateContinuationError("invalid volatility-regime configuration")
        if not (0 <= low_vol_quantile <= 1) or expansion_multiplier <= 0:
            raise CandidateContinuationError("invalid volatility-regime thresholds")
        for value in (
            vol_lookback, recent_low_vol_lookback, event_vol_ewm_span,
        ):
            if int(value) <= 0:
                raise CandidateContinuationError("lookbacks/spans must be positive")
        if event_vol_lookback_hours <= 0:
            raise CandidateContinuationError("lookbacks/spans must be positive")
        self.identity = identity
        self.eps = float(eps)
        self.min_gap_bars = int(min_gap_bars)
        self.low_vol_quantile = float(low_vol_quantile)
        self.expansion_multiplier = float(expansion_multiplier)
        self.vol_lookback = int(vol_lookback)
        self.recent_low_vol_lookback = int(recent_low_vol_lookback)
        self.event_vol_lookback_hours = float(event_vol_lookback_hours)
        self.event_vol_lookback_ns = int(
            round(self.event_vol_lookback_hours * 3600.0 * 1e9)
        )
        self.event_vol_ewm_span = int(event_vol_ewm_span)
        self.min_vol = float(min_vol)
        self._state = self._new_state()

    def _new_state(self):
        return _State(
            ewm_var=_ewm_var_state(
                np.asarray([], dtype="float64"),
                self.event_vol_ewm_span,
            )
        )

    def reset(self):
        self._state = self._new_state()

    @staticmethod
    def _ts(value):
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise CandidateContinuationError(f"invalid timestamp: {value!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _num(value, name):
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise CandidateContinuationError(f"invalid {name}: {value!r}")
        return float(x)

    @staticmethod
    def _optional(value):
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        return math.nan if pd.isna(x) else float(x)

    def _normalize(self, features):
        required = [
            "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
            "Bar_Volatility_Bps", "ATR_14", "NATR",
        ]
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        missing = [c for c in required if c not in features.columns]
        if missing:
            raise CandidateContinuationError(f"missing columns: {missing}")
        out = features.copy()
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
        for c in ["Open","High","Low","Close","Bar_Volatility_Bps","ATR_14","NATR",*OPTIONAL_CONTEXT]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=required).sort_values("Datetime").reset_index(drop=True)
        if out.empty:
            raise CandidateContinuationError("features empty after normalization")
        return out

    def _vol_context(self, current):
        h = self._state.volatility_history
        if len(h) >= self.vol_lookback:
            s = pd.Series(h[-self.vol_lookback:], dtype="float64")
            med = float(s.median())
            q = float(s.quantile(self.low_vol_quantile))
        else:
            med = q = math.nan
        if len(h) >= self.recent_low_vol_lookback:
            recent = h[-self.recent_low_vol_lookback:]
            mean = float(math.fsum(recent) / self.recent_low_vol_lookback)
        else:
            mean = math.nan
        ratio = float(current / (mean + self.eps)) if np.isfinite(mean) else math.nan
        h.append(float(current))
        keep = max(self.vol_lookback, self.recent_low_vol_lookback)
        if len(h) > keep:
            del h[:len(h)-keep]
        return med, q, mean, ratio

    def _event_vol(self, close, timestamp):
        # Calendar-time backward lookup -- see absorption.py's identical
        # port (verified 2026-09-25 via synthetic parity test against
        # shared/labels/calendar_time_barriers.py::calendar_time_event_vol,
        # max abs diff = 0.0).
        h = self._state.close_history
        h_ts = self._state.close_history_ns
        ts_ns = int(timestamp.value)
        target_ns = ts_ns - self.event_vol_lookback_ns
        pos = bisect.bisect_left(h_ts, target_ns) - 1
        if pos >= 0:
            lag = float(h[pos])
            with np.errstate(divide="ignore", invalid="ignore"):
                ret = float(np.log((close + self.eps) / (lag + self.eps)))
        else:
            ret = math.nan
        var = float(_ewm_var_update(self._state.ewm_var, ret))
        event_vol = (
            max(math.sqrt(var), self.min_vol)
            if np.isfinite(var) and var >= 0 else math.nan
        )
        h.append(float(close))
        h_ts.append(ts_ns)
        # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
        # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
        # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
        cutoff_ns = ts_ns - self.event_vol_lookback_ns
        drop = bisect.bisect_left(h_ts, cutoff_ns) - 1
        if drop > 0:
            del h[:drop]
            del h_ts[:drop]
        return event_vol

    def _advance(self, row, *, enforce_append_only):
        s = self._state
        ts = self._ts(row["Datetime"])
        bar_start = self._ts(row["Bar_Start"])
        open_ = self._num(row["Open"], "Open")
        high = self._num(row["High"], "High")
        low = self._num(row["Low"], "Low")
        close = self._num(row["Close"], "Close")
        bar_vol = self._num(row["Bar_Volatility_Bps"], "Bar_Volatility_Bps")
        atr = self._num(row["ATR_14"], "ATR_14")
        if enforce_append_only and s.last_timestamp is not None and ts <= s.last_timestamp:
            raise CandidateContinuationError("update_one requires increasing Datetime")

        i = s.processed_rows
        med, q, recent, ratio = self._vol_context(bar_vol)
        compressed = int(
            np.isfinite(recent) and np.isfinite(q)
            and recent <= q * self.expansion_multiplier
        )
        expansion = int(np.isfinite(ratio) and ratio >= self.expansion_multiplier)
        flag = int(compressed == 1 and expansion == 1)

        rng = max(high - low, self.eps)
        body = close - open_
        body_direction = float(np.sign(body))
        body_share = float(abs(body) / (rng + self.eps))
        close_location = float((close - low) / (rng + self.eps))
        expansion_range_atr = float(rng / (atr + self.eps))
        event_vol = self._event_vol(close, ts)

        emitted = None
        if flag == 1 and i - s.last_selected_event_index >= self.min_gap_bars:
            s.last_selected_event_index = i
            if np.isfinite(event_vol) and np.isfinite(ratio):
                emitted = {
                    "Datetime": ts,
                    "Bar_Start": bar_start,
                    "edge_id": self.identity.edge_id,
                    "side": 0,
                    "Event_Vol": float(event_vol),
                    "event_id": f"{self.identity.edge_id}|{ts.isoformat()}|volatility_regime",
                    "candidate_event_generator": self.identity.candidate_event_generator,
                    "Vol_Regime_Event": 1,
                    "Was_Compressed": 1,
                    "Vol_Expansion": 1,
                    "Current_Vol_Ratio": float(ratio),
                    "Bar_Volatility_Bps": float(bar_vol),
                    "Vol_Median_50": float(med),
                    "Vol_Q55_50": float(q),
                    "Recent_Vol_Mean": float(recent),
                    "Expansion_Range_ATR": expansion_range_atr,
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
                emitted["low_vol_quantile"] = self.low_vol_quantile
                emitted["expansion_multiplier"] = self.expansion_multiplier
                emitted["vol_lookback"] = self.vol_lookback
                emitted["recent_low_vol_lookback"] = self.recent_low_vol_lookback
                emitted["min_gap_bars"] = self.min_gap_bars
                emitted["event_vol_lookback_hours"] = self.event_vol_lookback_hours
                emitted["event_vol_ewm_span"] = self.event_vol_ewm_span

        s.processed_rows += 1
        s.last_timestamp = ts
        return emitted

    def bootstrap(self, features):
        frame = self._normalize(features)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(self, row: Union[Mapping[str, Any], pd.Series]):
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features):
        frame = self._normalize(features)
        self.reset()
        rows = []
        for row in frame.to_dict(orient="records"):
            event = self._advance(row, enforce_append_only=False)
            if event is not None:
                rows.append(event)
        return pd.DataFrame(rows)

    def state_summary(self):
        s = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "processed_rows": s.processed_rows,
            "last_timestamp": s.last_timestamp.isoformat() if s.last_timestamp is not None else None,
            "volatility_history_rows": len(s.volatility_history),
            "close_history_rows": len(s.close_history),
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }

def _constant(constants, name):
    if name not in constants:
        raise CandidateContinuationError(f"missing D.1 constant: {name}")
    return constants[name]

def create_provider(*, candidate_logic_signature, d1_edge, v2_root):
    signature = str(candidate_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(f"unsupported signature: {signature}")
    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider signature mismatch")
    constants = candidate.get("semantic_constants") or {}
    identity = extract_candidate_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return VolatilityRegimeCandidateContinuation(
        identity=identity,
        eps=float(_constant(constants, "EPS")),
        min_gap_bars=int(_constant(constants, "MIN_GAP_BARS")),
        low_vol_quantile=float(_constant(constants, "LOW_VOL_QUANTILE")),
        expansion_multiplier=float(_constant(constants, "EXPANSION_MULTIPLIER")),
        vol_lookback=int(_constant(constants, "VOL_LOOKBACK")),
        recent_low_vol_lookback=int(_constant(constants, "RECENT_LOW_VOL_LOOKBACK")),
        event_vol_lookback_hours=float(_constant(constants, "EVENT_VOL_LOOKBACK_HOURS")),
        event_vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_vol=float(_constant(constants, "MIN_VOL")),
    )
