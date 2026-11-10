from __future__ import annotations
from dataclasses import dataclass
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
TRENDPULLBACK_LONG_PRIMARY_SIGNATURE="55bcde0deb35157b3bfdad83fac6f2660249a613ab099cb543d7e1ff543dbf3e"
PROVIDER_SPEC={"provider_id":"phase_d4_trendpullback_long_primary","provider_version":"1","supported_signatures":{TRENDPULLBACK_LONG_PRIMARY_SIGNATURE:{"side_value":1}}}
@dataclass
class _State: processed_rows:int=0; last_timestamp:Optional[pd.Timestamp]=None
class TrendPullbackLongPrimarySideContinuation:
 def __init__(self,*,identity,constants): self.identity=identity;self.c=constants;self._state=_State()
 @staticmethod
 def _ts(v):
  x=to_datetime_iso(v,utc=True,errors="coerce")
  if pd.isna(x):raise PrimarySideContinuationError("invalid timestamp")
  return pd.Timestamp(x)
 def reset(self):self._state=_State()
 def bootstrap(self,f):
  self.reset()
  for v in to_datetime_iso(f["Datetime"],utc=True,errors="coerce").dropna().sort_values():self._state.last_timestamp=pd.Timestamp(v);self._state.processed_rows+=1
  return self.state_summary()
 def update_one(self,row):
  r=row.to_dict() if isinstance(row,pd.Series) else dict(row);ts=self._ts(r["Datetime"])
  if self._state.last_timestamp is not None and ts<=self._state.last_timestamp:raise PrimarySideContinuationError("non-increasing timestamp")
  self._state.last_timestamp=ts;self._state.processed_rows+=1;return {"Datetime":ts,"primary_side":1}
 def filter_event(self,event):
  if event is None:return None
  e=dict(event)
  if self._ts(e["Datetime"])!=self._state.last_timestamp:raise PrimarySideContinuationError("timestamp mismatch")
  if str(e["edge_id"])!=self.identity.edge_id:raise PrimarySideContinuationError("edge mismatch")
  def f(n):return float(pd.to_numeric(pd.Series([e[n]]),errors="coerce").iat[0])
  pb=f("Pullback_Return");z=f("Pullback_Return_Z");depth=f("Pullback_Depth_ATR");ch20=f("Channel_Position_20_Context");ch50=f("Channel_Position_50_Context");cl=f("Current_Close_Location");lw=f("Lower_Wick_Share_Context")
  c=self.c
  ok=(f("Trend_Pullback_Continuation_Event")==1 and f("Positive_Drift_State")==1
      and (pb<=float(c.get("MAX_PULLBACK_RETURN",0)) or z<=float(c.get("MAX_PULLBACK_RETURN_Z",-0.2)))
      and ch20<=float(c.get("MAX_CHANNEL_POSITION_20",.7)) and ch50>=float(c.get("MIN_CHANNEL_POSITION_50",.3))
      and depth>=float(c.get("MIN_PULLBACK_DEPTH_ATR",.05))
      and (cl>=float(c.get("MIN_CLOSE_LOCATION",.4)) or lw>=float(c.get("MIN_LOWER_WICK_SHARE",.05)))
      and f("Pullback_Reclaim")==1)
  if not ok:return None
  e["side"]=1;e["primary_side"]=1;e["primary_side_filter"]=1;e["primary_side_rule"]="positive medium-term drift + short-term pullback/reclaim -> side=+1 long";return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported primary signature")
 p=d1_edge.get("primary_side") or {};return TrendPullbackLongPrimarySideContinuation(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),constants=p.get("semantic_constants") or {})
