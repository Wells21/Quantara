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
BREAKDOWN_RETEST_SHORT_PRIMARY_SIGNATURE="48145dddb64c9b59a8c2c87c4eea992d4c8a1de9e725cdf0d6a9e33d35f449f6"
PROVIDER_SPEC={"provider_id":"phase_d4_breakdownretestfailure_short_primary","provider_version":"1","supported_signatures":{BREAKDOWN_RETEST_SHORT_PRIMARY_SIGNATURE:{"side_value":-1}}}
@dataclass
class _S:processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class Primary:
 def __init__(self,*,identity,rmin=1,rmax=20,fmax=3):self.identity=identity;self.rmin=rmin;self.rmax=rmax;self.fmax=fmax;self._state=_S()
 def reset(self):self._state=_S()
 def bootstrap(self,f):
  self.reset()
  for v in to_datetime_iso(f["Datetime"],utc=True,errors="coerce").dropna().sort_values():self._state.last_timestamp=pd.Timestamp(v);self._state.processed_rows+=1
  return self.state_summary()
 def update_one(self,r):
  p=r.to_dict() if isinstance(r,pd.Series) else dict(r);ts=pd.Timestamp(to_datetime_iso(p["Datetime"],utc=True))
  if self._state.last_timestamp is not None and ts<=self._state.last_timestamp:raise PrimarySideContinuationError("non-increasing")
  self._state.last_timestamp=ts;self._state.processed_rows+=1;return {"Datetime":ts,"primary_side":-1}
 def filter_event(self,e):
  if e is None:return None
  e=dict(e);ts=pd.Timestamp(to_datetime_iso(e["Datetime"],utc=True))
  if ts!=self._state.last_timestamp or str(e["edge_id"])!=self.identity.edge_id:raise PrimarySideContinuationError("identity")
  n=lambda k:float(pd.to_numeric(pd.Series([e[k]]),errors="coerce").iat[0]);ra=n("Retest_Bars_After_Breakdown");fa=n("Failure_Bars_After_Retest");ba=n("Breakdown_Bars_Ago")
  ok=(n("Breakdown_Retest_Failure_Event")==1 and n("Breakdown_Confirmed")==1 and n("Retest_Touched")==1 and n("Failed_Reclaim")==1 and self.rmin<=ra<=self.rmax and 0<=fa<=self.fmax and ba==ra+fa and n("Broken_Support_Level")>0 and n("Shifted_ATR_14")>0 and n("Retest_High")>=n("Retest_Threshold_Price") and n("Retest_Close")<n("Retest_Decisive_Reclaim_Price") and n("Failure_Close")<n("Broken_Support_Level"))
  if not ok:return None
  e["primary_side_rule"]="stored broken support retested within bars 1-20 and failure close below that level within 0-3 bars maps to side=-1 short";e["primary_side"]=-1;e["primary_side_filter"]=1;e["side"]=-1;return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported")
 c=(d1_edge.get("primary_side") or {}).get("semantic_constants") or {};return Primary(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),rmin=int(c.get("RETEST_MIN_BARS",1)),rmax=int(c.get("RETEST_MAX_BARS",20)),fmax=int(c.get("FAILURE_CONFIRMATION_MAX_BARS",3)))
