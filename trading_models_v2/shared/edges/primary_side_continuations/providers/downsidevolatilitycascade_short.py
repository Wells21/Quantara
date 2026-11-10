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
DOWNSIDEVOLCASCADE_SHORT_PRIMARY_SIGNATURE="4c4dd99717e521b86bf227b51e61b36bafbd2974c9a53acd7dc8fe907d4486fc"
PROVIDER_SPEC={"provider_id":"phase_d4_downsidevolatilitycascade_short_primary","provider_version":"1","supported_signatures":{DOWNSIDEVOLCASCADE_SHORT_PRIMARY_SIGNATURE:{"side_value":-1}}}
@dataclass
class _S:processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class CascadePrimary:
 def __init__(self,*,identity,share=.6,accel=1.1):self.identity=identity;self.share=float(share);self.accel=float(accel);self._state=_S()
 def reset(self):self._state=_S()
 @staticmethod
 def _ts(v):return pd.Timestamp(to_datetime_iso(v,utc=True))
 def bootstrap(self,f):
  self.reset()
  for v in to_datetime_iso(f["Datetime"],utc=True,errors="coerce").dropna().sort_values():self._state.last_timestamp=pd.Timestamp(v);self._state.processed_rows+=1
  return self.state_summary()
 def update_one(self,r):
  p=r.to_dict() if isinstance(r,pd.Series) else dict(r);ts=self._ts(p["Datetime"])
  if self._state.last_timestamp is not None and ts<=self._state.last_timestamp:raise PrimarySideContinuationError("non-increasing")
  self._state.last_timestamp=ts;self._state.processed_rows+=1;return {"Datetime":ts,"primary_side":-1}
 def filter_event(self,e):
  if e is None:return None
  e=dict(e)
  if self._ts(e["Datetime"])!=self._state.last_timestamp or str(e["edge_id"])!=self.identity.edge_id:raise PrimarySideContinuationError("identity mismatch")
  n=lambda k:float(pd.to_numeric(pd.Series([e[k]]),errors="coerce").iat[0])
  entry=n("Cascade_State_Entry");renew=n("Cascade_Renewed_Impulse")
  ok=(n("Downside_Volatility_Cascade_Event")==1 and n("Cascade_Active_State")==1 and n("Negative_Event_Bar")==1 and n("Log_Return_1")<0 and n("Downside_Variance_Share_20")>=self.share and n("Vol_Acceleration_20_100")>=self.accel and n("Event_Vol")>0 and entry+renew==1)
  if not ok:return None
  e["primary_side_rule"]="negative completed-bar impulses inside downside variance share >=0.60 and non-overlapping volatility acceleration >=1.10 map to side=-1 short; both initial state entries and spaced renewed impulses are valid";e["primary_side"]=-1;e["primary_side_filter"]=1;e["side"]=-1;return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported")
 c=(d1_edge.get("primary_side") or {}).get("semantic_constants") or {};return CascadePrimary(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),share=float(c.get("DOWNSIDE_SHARE_THRESHOLD",.6)),accel=float(c.get("VOL_ACCELERATION_THRESHOLD",1.1)))
