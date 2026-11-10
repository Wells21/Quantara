from __future__ import annotations
from dataclasses import dataclass
from typing import Any,Mapping,Optional
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[4]))
    from shared.io.timeparse import to_datetime_iso
from ..base import PrimarySideContinuationError,extract_primary_source_identity
DAILY_RANGE_FADE_LONG_PRIMARY_SIGNATURE="d7cc2f95c030577f9c7850b2f4b0d1af9a550a6540ac0f538d171e9080737b02"
PROVIDER_SPEC={"provider_id":"phase_d4_dailyrangecompletionfade_long_primary","provider_version":"1","supported_signatures":{DAILY_RANGE_FADE_LONG_PRIMARY_SIGNATURE:{"side_value":1}}}
@dataclass
class _S:processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class DailyPrimary:
 def __init__(self,*,identity,constants):self.identity=identity;self.c=constants;self._state=_S()
 def reset(self):self._state=_S()
 def bootstrap(self,f):
  self.reset()
  for v in to_datetime_iso(f["Datetime"],utc=True,errors="coerce").dropna().sort_values():self._state.last_timestamp=pd.Timestamp(v);self._state.processed_rows+=1
  return self.state_summary()
 def update_one(self,r):
  p=r.to_dict() if isinstance(r,pd.Series) else dict(r);ts=pd.Timestamp(to_datetime_iso(p["Datetime"],utc=True))
  if self._state.last_timestamp is not None and ts<=self._state.last_timestamp:raise PrimarySideContinuationError("non-increasing")
  self._state.last_timestamp=ts;self._state.processed_rows+=1;return {"Datetime":ts,"primary_side":1}
 def filter_event(self,e):
  if e is None:return None
  e=dict(e);ts=pd.Timestamp(to_datetime_iso(e["Datetime"],utc=True))
  if ts!=self._state.last_timestamp or str(e["edge_id"])!=self.identity.edge_id:raise PrimarySideContinuationError("identity")
  n=lambda k:float(pd.to_numeric(pd.Series([e[k]]),errors="coerce").iat[0]);c=self.c
  ok=(n("Daily_Range_Fade_Event")==1 and n("Bars_Into_Day")>=float(c.get("MIN_DAY_PROGRESS_BARS",20)) and n("Daily_Range_Completion")>=float(c.get("MIN_DAILY_RANGE_COMPLETION",.7)) and n("Downside_Extension_ATR")>=float(c.get("MIN_DOWNSIDE_EXTENSION_ATR",.5)) and n("Day_Position")<=float(c.get("MAX_DAY_POSITION",.35)) and n("Close")<n("Day_Open") and n("Event_Vol")>0)
  if not ok:return None
  e["primary_side_rule"]="downside daily range completion/exhaustion -> side=+1 long";e["primary_side"]=1;e["primary_side_filter"]=1;e["side"]=1;return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported")
 p=d1_edge.get("primary_side") or {};return DailyPrimary(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),constants=p.get("semantic_constants") or {})
