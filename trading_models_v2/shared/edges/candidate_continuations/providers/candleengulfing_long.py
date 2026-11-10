from __future__ import annotations
"""Exact incremental candleengulfing_long candidate continuation.

Mirrors assets/US500/edges/candleengulfing_long/candidate_events.py
(generate_v1_candleengulfing_long_candidate_events) function-for-function.
"""
from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd

from shared.edges.native_one_row_feature_engine import _ewm_var_state, _ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError, extract_candidate_source_identity

CANDLEENGULFING_LONG_SIGNATURE = "c728096991789ebfacac96f369ed7db48d7062f94dd80bf8849b058369853c55"
PROVIDER_SPEC = {
    "provider_id": "phase_d4_candleengulfing_long_candidate",
    "provider_version": "1",
    "supported_signatures": {
        CANDLEENGULFING_LONG_SIGNATURE: {"side_specialization": "long"},
    },
}
OPTIONAL_CONTEXT = (
    "Regime_State", "Regime_Entropy", "Regime_Duration",
    "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3",
)


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    low_history: list[float] = field(default_factory=list)
    prev_body_high: float = math.nan
    prev_body_low: float = math.nan
    prev_body: float = math.nan
    prev_bar_range: float = math.nan
    prev_is_black: bool = False
    processed_rows: int = 0
    last_selected_event_index: int = -(10 ** 9)
    last_timestamp: Optional[pd.Timestamp] = None


class CandleEngulfingLongCandidateContinuation:
    def __init__(
        self, *, identity, eps, deviation_side, vol_lookback, vol_ewm_span, min_vol,
        local_low_lookback, min_engulf_ratio, min_body_frac_of_range, min_gap_bars,
    ):
        self.identity = identity
        self.eps = float(eps)
        self.deviation_side = int(deviation_side)
        self.vol_lookback = int(vol_lookback)
        self.vol_ewm_span = int(vol_ewm_span)
        self.min_vol = float(min_vol)
        self.local_low_lookback = int(local_low_lookback)
        self.min_engulf_ratio = float(min_engulf_ratio)
        self.min_body_frac_of_range = float(min_body_frac_of_range)
        self.min_gap_bars = int(min_gap_bars)
        self._state = self._new_state()

    def _new_state(self) -> _State:
        return _State(ewm_var=_ewm_var_state(np.asarray([], dtype="float64"), self.vol_ewm_span))

    def reset(self):
        self._state = self._new_state()

    @staticmethod
    def _ts(v):
        x = to_datetime_iso(v, utc=True, errors="coerce")
        if pd.isna(x):
            raise CandidateContinuationError(f"invalid timestamp {v!r}")
        return pd.Timestamp(x)

    @staticmethod
    def _num(v, name):
        x = pd.to_numeric(pd.Series([v]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise CandidateContinuationError(f"invalid {name}: {v!r}")
        return float(x)

    @staticmethod
    def _opt(v):
        x = pd.to_numeric(pd.Series([v]), errors="coerce").iat[0]
        return math.nan if pd.isna(x) else float(x)

    def _normalize(self, f):
        req = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
        miss = [c for c in req if c not in f.columns]
        if miss:
            raise CandidateContinuationError(f"missing candleengulfing_long columns: {miss}")
        out = f.copy()
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
        for c in ["Open", "High", "Low", "Close", *OPTIONAL_CONTEXT]:
            if c in out:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)

    def _event_vol(self, close: float) -> float:
        h = self._state.close_history
        ret = (
            float(np.log((close + self.eps) / (h[-self.vol_lookback] + self.eps)))
            if len(h) >= self.vol_lookback
            else math.nan
        )
        var = float(_ewm_var_update(self._state.ewm_var, ret))
        ev = max(math.sqrt(var), self.min_vol) if np.isfinite(var) and var >= 0 else math.nan
        h.append(close)
        if len(h) > self.vol_lookback:
            del h[: len(h) - self.vol_lookback]
        return ev

    def _advance(self, row, enforce):
        s = self._state
        ts = self._ts(row["Datetime"])
        bs = self._ts(row["Bar_Start"])
        if enforce and s.last_timestamp is not None and ts <= s.last_timestamp:
            raise CandidateContinuationError("update_one requires increasing Datetime")
        o = self._num(row["Open"], "Open")
        h = self._num(row["High"], "High")
        l = self._num(row["Low"], "Low")
        c = self._num(row["Close"], "Close")

        bar_range = max(h - l, self.eps)
        body_high = max(o, c)
        body_low = min(o, c)
        body = abs(c - o)
        is_black = c < o
        is_white = c > o

        ev = self._event_vol(c)

        # is_local_low: uses low_history as built up through the PRIOR bar
        # only (mirrors source's `low.rolling(N, min_periods=N).min().shift(1)`
        # -- a window ending at, and including, the prior bar -- current
        # bar's own low is never part of this check).
        prior_low = s.low_history[-1] if s.low_history else math.nan
        if len(s.low_history) == self.local_low_lookback:
            is_local_low = prior_low <= min(s.low_history) + self.eps
        else:
            is_local_low = False
        s.low_history.append(l)
        if len(s.low_history) > self.local_low_lookback:
            del s.low_history[: len(s.low_history) - self.local_low_lookback]

        # Prior-bar body geometry (single-bar carry, matches source's .shift(1)).
        prior_body_high = s.prev_body_high
        prior_body_low = s.prev_body_low
        prior_body = s.prev_body
        prior_bar_range = s.prev_bar_range
        prior_is_black = s.prev_is_black  # source: prior_is_black.fillna(False)

        engulfing_current_body_frac = body / bar_range
        engulfing_prior_body_frac = (
            prior_body / max(prior_bar_range, self.eps)
            if np.isfinite(prior_body) and np.isfinite(prior_bar_range)
            else math.nan
        )
        engulfing_body_ratio = (
            body / max(prior_body, self.eps) if np.isfinite(prior_body) else math.nan
        )

        engulfs_body = (body_low <= prior_body_low) and (body_high >= prior_body_high)
        strong_enough = np.isfinite(engulfing_body_ratio) and engulfing_body_ratio >= self.min_engulf_ratio
        is_bullish_engulfing = (
            is_white
            and prior_is_black
            and engulfs_body
            and strong_enough
            and engulfing_current_body_frac >= self.min_body_frac_of_range
            and is_local_low
        )

        deviation_side = self.deviation_side if is_bullish_engulfing else 0
        deviation_z = engulfing_body_ratio

        emitted = None
        flag = deviation_side == self.deviation_side
        if flag and s.processed_rows - s.last_selected_event_index >= self.min_gap_bars:
            # The batch source's _extract_v1_spaced_engulfing_events() selects
            # this bar for spacing purposes (consuming the min_gap_bars slot)
            # BEFORE generate_v1_...()'s final
            # dropna(subset=[...,"Event_Vol","Deviation_Z",...]) drops the row
            # from the returned events -- so the gap slot must still advance
            # here even when Event_Vol is still NaN (VOL_LOOKBACK warmup,
            # which can genuinely outlast the engulfing-geometry warmup since
            # the pattern only needs ~local_low_lookback+2 bars to fire), but
            # the event itself must not be emitted in that case. (Deviation_Z
            # cannot be NaN here: strong_enough already requires it finite.)
            s.last_selected_event_index = s.processed_rows
            if np.isfinite(ev) and np.isfinite(deviation_z):
                # Column order matches generate_v1_candleengulfing_long_candidate_events()'s
                # exact final ordered_cols construction (required columns first,
                # in that function's fixed order, then the remaining columns in
                # the order they were first assigned in the source).
                emitted = {
                    "Datetime": ts, "Bar_Start": bs,
                    "edge_id": self.identity.edge_id,
                    "side": 1,
                    "Event_Vol": float(ev),
                    "event_id": f"{self.identity.edge_id}|{ts.isoformat()}|nison_bullish_engulfing",
                    "candidate_event_generator": self.identity.candidate_event_generator,
                    "Deviation_Side": int(deviation_side),
                    "Deviation_Z": float(deviation_z),
                    "event_known_after_close": True,
                    "uses_future_information": False,
                    "Open": o, "High": h, "Low": l, "Close": c,
                    "Engulfing_Body_Ratio": float(engulfing_body_ratio),
                    "Engulfing_Prior_Body_Frac": float(engulfing_prior_body_frac),
                    "Engulfing_Current_Body_Frac": float(engulfing_current_body_frac),
                }
                for c_name in OPTIONAL_CONTEXT:
                    if c_name in row:
                        emitted[c_name] = self._opt(row[c_name])
                emitted.update({
                    "min_gap_bars": self.min_gap_bars,
                    "vol_lookback": self.vol_lookback,
                    "vol_ewm_span": self.vol_ewm_span,
                    "local_low_lookback": self.local_low_lookback,
                    "min_engulf_ratio": self.min_engulf_ratio,
                })

        s.prev_body_high = body_high
        s.prev_body_low = body_low
        s.prev_body = body
        s.prev_bar_range = bar_range
        s.prev_is_black = bool(is_black)
        s.processed_rows += 1
        s.last_timestamp = ts
        return emitted

    def bootstrap(self, f):
        x = self._normalize(f)
        self.reset()
        for r in x.to_dict("records"):
            self._advance(r, False)
        return self.state_summary()

    def update_one(self, row):
        return self._advance(row.to_dict() if isinstance(row, pd.Series) else dict(row), True)

    def replay(self, f):
        x = self._normalize(f)
        self.reset()
        rows = []
        for r in x.to_dict("records"):
            e = self._advance(r, False)
            if e is not None:
                rows.append(e)
        return pd.DataFrame(rows)

    def state_summary(self):
        s = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"], "provider_version": "1",
            "edge_id": self.identity.edge_id, "processed_rows": s.processed_rows,
            "close_history_rows": len(s.close_history), "low_history_rows": len(s.low_history),
            "normal_path_history_disk_reads": False, "normal_path_full_candidate_fallback": False,
        }


def _c(constants, name, default=None):
    return constants[name] if name in constants else default


def create_provider(*, candidate_logic_signature: str, d1_edge: Mapping[str, Any], v2_root):
    sig = str(candidate_logic_signature or "").strip()
    if sig not in PROVIDER_SPEC["supported_signatures"]:
        raise CandidateContinuationError(f"unsupported candleengulfing_long signature: {sig}")
    constants = (d1_edge.get("candidate") or {}).get("semantic_constants") or {}
    identity = extract_candidate_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return CandleEngulfingLongCandidateContinuation(
        identity=identity,
        eps=float(_c(constants, "EPS", 1e-12)),
        deviation_side=int(_c(constants, "DEVIATION_SIDE", -1)),
        vol_lookback=int(_c(constants, "VOL_LOOKBACK", 20)),
        vol_ewm_span=int(_c(constants, "VOL_EWM_SPAN", 50)),
        min_vol=float(_c(constants, "MIN_VOL", 1e-8)),
        local_low_lookback=int(_c(constants, "LOCAL_LOW_LOOKBACK", 5)),
        min_engulf_ratio=float(_c(constants, "MIN_ENGULF_RATIO", 1.0)),
        min_body_frac_of_range=float(_c(constants, "MIN_BODY_FRAC_OF_RANGE", 0.02)),
        min_gap_bars=int(_c(constants, "MIN_GAP_BARS", 3)),
    )
