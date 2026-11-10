from __future__ import annotations
"""Exact trendpullbackcontinuation_short primary continuation with FFD/return context."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional,Union
import numpy as np,pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[4]))
    from shared.io.timeparse import to_datetime_iso
from ..base import PrimarySideContinuationError,extract_primary_source_identity
TRENDPULLBACK_SHORT_PRIMARY_SIGNATURE="1738111228b22745e8c43a56091f2c225bc3c23796d36aa1f535209dfef48bf3"
PROVIDER_SPEC={"provider_id":"phase_d4_trendpullback_short_primary","provider_version":"1","supported_signatures":{TRENDPULLBACK_SHORT_PRIMARY_SIGNATURE:{"side_value":-1}}}
@dataclass
class _State:
 processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None;close_history:list[float]=field(default_factory=list);ffd_history:list[float]=field(default_factory=list);current_context:dict[str,Any]=field(default_factory=dict)
class TrendPullbackShortPrimarySideContinuation:
 def __init__(self,*,identity,return_lookback=5):
  self.identity=identity;self.return_lookback=int(return_lookback);self._state=_State()
 def reset(self):self._state=_State()
 @staticmethod
 def _ts(v):
  x=to_datetime_iso(v,utc=True,errors="coerce")
  if pd.isna(x):raise PrimarySideContinuationError("invalid timestamp")
  return pd.Timestamp(x)
 @staticmethod
 def _num(v,n):
  x=pd.to_numeric(pd.Series([v]),errors="coerce").iat[0]
  if pd.isna(x) or not np.isfinite(float(x)):raise PrimarySideContinuationError(f"invalid {n}")
  return float(x)
 def bootstrap(self,f):
  self.reset()
  for r in f.sort_values("Datetime").to_dict("records"):self._advance(r,False)
  return self.state_summary()
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise PrimarySideContinuationError("non-increasing timestamp")
  close=self._num(r["Close"],"Close")
  ffd=self._num(r["Close_FFD"],"Close_FFD") if "Close_FFD" in r and pd.notna(r["Close_FFD"]) else math.nan
  f5=float(np.log((ffd+1e-12)/(s.ffd_history[-5]+1e-12))) if np.isfinite(ffd) and len(s.ffd_history)>=5 else math.nan
  f20=float(np.log((ffd+1e-12)/(s.ffd_history[-20]+1e-12))) if np.isfinite(ffd) and len(s.ffd_history)>=20 else math.nan
  ret=float(np.log((close+1e-12)/(s.close_history[-self.return_lookback]+1e-12))) if len(s.close_history)>=self.return_lookback else math.nan
  s.current_context={"Close_FFD":ffd,"ffd_return_5":f5,"ffd_return_20":f20,"ffd_short_context":int(np.isfinite(f5) and f5<0),
  f"TrendPullback_Return_{self.return_lookback}":ret,"pullback_failure_return_context":int(np.isfinite(ret) and ret<=0),"primary_side":-1}
  s.close_history.append(close);s.ffd_history.append(ffd)
  if len(s.close_history)>self.return_lookback:del s.close_history[:len(s.close_history)-self.return_lookback]
  if len(s.ffd_history)>20:del s.ffd_history[:len(s.ffd_history)-20]
  s.last_timestamp=ts;s.processed_rows+=1
 def update_one(self,row):
  r=row.to_dict() if isinstance(row,pd.Series) else dict(row);self._advance(r,True);return {"Datetime":self._state.last_timestamp,**self._state.current_context}
 def filter_event(self,event):
  if event is None:return None
  e=dict(event)
  if self._ts(e["Datetime"])!=self._state.last_timestamp:raise PrimarySideContinuationError("timestamp mismatch")
  if str(e["edge_id"])!=self.identity.edge_id or int(e["side"])!=-1:raise PrimarySideContinuationError("edge/side mismatch")
  for k,v in {"trend_direction":"downside","pullback_direction":"upward","failure_direction":"downside"}.items():
   if e.get(k)!=v:raise PrimarySideContinuationError(f"{k} mismatch")
  if bool(e.get("uses_future_information")) or not bool(e.get("event_known_after_close")):raise PrimarySideContinuationError("causality flag invalid")
  if int(e.get("trend_pullback_sequence_ok",0))!=1:return None
  rc=self._num(e["reentry_close"],"reentry_close");ph=self._num(e["pullback_high"],"pullback_high");br=self._num(e["bearish_reentry_return_atr"],"bearish_reentry_return_atr");loc=self._num(e["rejection_close_location"],"rejection_close_location")
  if not (rc<=ph and br>=0 and loc<=1):return None
  out=dict(e);out["bearish_reentry_confirmed"]=1
  for k,v in self._state.current_context.items():out[k]=v
  out["primary_side"]=-1;out["primary_side_filter"]=1
  return out
 def state_summary(self):
  s=self._state;return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":s.processed_rows,"close_history_rows":len(s.close_history),"ffd_history_rows":len(s.ffd_history),"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported primary signature")
 c=(d1_edge.get("primary_side") or {}).get("semantic_constants") or {}
 return TrendPullbackShortPrimarySideContinuation(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),return_lookback=int(c.get("RETURN_LOOKBACK",5)))
