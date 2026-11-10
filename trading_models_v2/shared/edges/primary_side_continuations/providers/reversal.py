from __future__ import annotations

"""Exact reversal_long primary-side continuation including event-row rejection context."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[4]))
    from shared.io.timeparse import to_datetime_iso

from ..base import PrimarySideContinuationError, extract_primary_source_identity

REVERSAL_LONG_PRIMARY_SIGNATURE = "a558a107b8cc2fefdb1cbde4855bb0762131dcdd529c944d0be77df7e181c6f3"
PROVIDER_SPEC = {
    "provider_id": "phase_d4_reversal_long_primary_side",
    "provider_version": "1",
    "supported_signatures": {REVERSAL_LONG_PRIMARY_SIGNATURE: {"side_value": 1}},
}

@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None
    prev_upper_wick_to_body: float = np.nan
    prev_lower_wick_to_body: float = np.nan
    prev_upper_wick_share: float = np.nan
    prev_lower_wick_share: float = np.nan

class ReversalLongPrimarySideContinuation:
    def __init__(self, *, identity, deviation_side: int, eps: float,
                 min_wick_to_body: float, min_wick_share_of_range: float,
                 use_regime_filter: bool, use_entropy_filter: bool, use_vol_filter: bool,
                 max_regime_entropy: float, min_event_norm_range: float) -> None:
        if int(identity.side_value) != 1 or int(deviation_side) != -1:
            raise PrimarySideContinuationError("reversal_long side identity changed")
        self.identity = identity
        self.deviation_side = -1
        self.eps = float(eps)
        self.min_wick_to_body = float(min_wick_to_body)
        self.min_wick_share_of_range = float(min_wick_share_of_range)
        self.use_regime_filter = bool(use_regime_filter)
        self.use_entropy_filter = bool(use_entropy_filter)
        self.use_vol_filter = bool(use_vol_filter)
        self.max_regime_entropy = float(max_regime_entropy)
        self.min_event_norm_range = float(min_event_norm_range)
        self._state = _State()

    def reset(self): self._state = _State()

    @staticmethod
    def _ts(v):
        ts = to_datetime_iso(v, utc=True, errors="coerce")
        if pd.isna(ts): raise PrimarySideContinuationError(f"invalid Datetime: {v!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _num(v, name):
        x = pd.to_numeric(pd.Series([v]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise PrimarySideContinuationError(f"invalid {name}: {v!r}")
        return float(x)

    def bootstrap(self, features: pd.DataFrame):
        if not isinstance(features,pd.DataFrame) or features.empty or "Datetime" not in features:
            raise PrimarySideContinuationError("bootstrap requires Datetime")
        self.reset()
        for v in to_datetime_iso(features["Datetime"], utc=True, errors="coerce").dropna().sort_values():
            self._state.last_timestamp = pd.Timestamp(v)
            self._state.processed_rows += 1
        return self.state_summary()

    def update_one(self, row: Union[Mapping[str,Any],pd.Series]):
        payload = row.to_dict() if isinstance(row,pd.Series) else dict(row)
        ts = self._ts(payload["Datetime"])
        if self._state.last_timestamp is not None and ts <= self._state.last_timestamp:
            raise PrimarySideContinuationError("update_one requires increasing Datetime")
        self._state.last_timestamp=ts
        self._state.processed_rows += 1
        return {"Datetime":ts,"primary_side":1}

    def filter_event(self, event: Optional[Mapping[str,Any]]):
        if event is None: return None
        e=dict(event)
        if self._state.last_timestamp is None or self._ts(e["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError("event/current timestamp mismatch")
        if str(e["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError("edge_id mismatch")
        deviation_side=int(self._num(e["Deviation_Side"],"Deviation_Side"))
        self._num(e["Deviation_Z"],"Deviation_Z")
        event_vol=self._num(e["Event_Vol"],"Event_Vol")
        if deviation_side != self.deviation_side:
            return None

        open_=self._num(e["Open"],"Open"); high=self._num(e["High"],"High")
        low=self._num(e["Low"],"Low"); close=self._num(e["Close"],"Close")
        body=abs(close-open_)
        upper=max(high-max(close,open_),0.0)
        lower=max(min(close,open_)-low,0.0)
        rng=max(high-low,self.eps)
        upper_to_body=upper/(body+self.eps); lower_to_body=lower/(body+self.eps)
        upper_share=upper/(rng+self.eps); lower_share=lower/(rng+self.eps)

        out=dict(e)
        out.update({
            "Body":body,"Upper_Wick":upper,"Lower_Wick":lower,"Bar_Range":rng,
            "Upper_Wick_to_Body":upper_to_body,"Lower_Wick_to_Body":lower_to_body,
            "Upper_Wick_Share":upper_share,"Lower_Wick_Share":lower_share,
            "Prev_Upper_Wick_to_Body":self._state.prev_upper_wick_to_body,
            "Prev_Lower_Wick_to_Body":self._state.prev_lower_wick_to_body,
            "Prev_Upper_Wick_Share":self._state.prev_upper_wick_share,
            "Prev_Lower_Wick_Share":self._state.prev_lower_wick_share,
        })
        event_norm_range=rng/(close*max(event_vol,1e-8)+self.eps)
        out["Event_Norm_Range"]=event_norm_range
        long_now=(lower_to_body>=self.min_wick_to_body and lower_share>=self.min_wick_share_of_range)
        long_prev=(np.isfinite(self._state.prev_lower_wick_to_body)
                   and self._state.prev_lower_wick_to_body>=self.min_wick_to_body
                   and np.isfinite(self._state.prev_lower_wick_share)
                   and self._state.prev_lower_wick_share>=self.min_wick_share_of_range)
        short_now=(upper_to_body>=self.min_wick_to_body and upper_share>=self.min_wick_share_of_range)
        short_prev=(np.isfinite(self._state.prev_upper_wick_to_body)
                    and self._state.prev_upper_wick_to_body>=self.min_wick_to_body
                    and np.isfinite(self._state.prev_upper_wick_share)
                    and self._state.prev_upper_wick_share>=self.min_wick_share_of_range)
        out["long_reject"]=int(long_now or long_prev)
        out["short_reject"]=int(short_now or short_prev)
        out["regime_ok"]=1
        if self.use_regime_filter and "Regime_State" in out:
            out["regime_ok"]=int(int(out["Regime_State"]) != 3)
        out["entropy_ok"]=1
        if self.use_entropy_filter and "Regime_Entropy" in out:
            out["entropy_ok"]=int(float(out["Regime_Entropy"])<=self.max_regime_entropy)
        out["vol_ok"]=1
        if self.use_vol_filter:
            out["vol_ok"]=int(event_norm_range>=self.min_event_norm_range)

        # Source .shift(1) is across candidate-event rows, not bars.
        self._state.prev_upper_wick_to_body=upper_to_body
        self._state.prev_lower_wick_to_body=lower_to_body
        self._state.prev_upper_wick_share=upper_share
        self._state.prev_lower_wick_share=lower_share

        out["primary_side_rule"]="Deviation_Side=-1 -> side=+1 long"
        out["primary_side"]=1; out["primary_side_filter"]=1; out["side"]=1
        return out

    def state_summary(self):
        return {
            "provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1",
            "edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,
            "normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,
            "normal_path_full_primary_fallback":False,
        }

def _c(constants,name,default=None):
    return constants[name] if name in constants else default

def create_provider(*, primary_logic_signature: str, d1_edge: Mapping[str,Any], v2_root):
    sig=str(primary_logic_signature or "").strip()
    if sig not in PROVIDER_SPEC["supported_signatures"]:
        raise PrimarySideContinuationError(f"unsupported reversal primary signature: {sig}")
    primary=d1_edge.get("primary_side") or {}
    constants=primary.get("semantic_constants") or {}
    identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge)
    return ReversalLongPrimarySideContinuation(
        identity=identity,
        deviation_side=int(_c(constants,"DEVIATION_SIDE",-1)),
        eps=float(_c(constants,"EPS",1e-12)),
        min_wick_to_body=float(_c(constants,"MIN_WICK_TO_BODY",0.80)),
        min_wick_share_of_range=float(_c(constants,"MIN_WICK_SHARE_OF_RANGE",0.25)),
        use_regime_filter=bool(_c(constants,"USE_REGIME_FILTER",False)),
        use_entropy_filter=bool(_c(constants,"USE_ENTROPY_FILTER",False)),
        use_vol_filter=bool(_c(constants,"USE_VOL_FILTER",False)),
        max_regime_entropy=float(_c(constants,"MAX_REGIME_ENTROPY",1.25)),
        min_event_norm_range=float(_c(constants,"MIN_EVENT_NORM_RANGE",0.30)),
    )
