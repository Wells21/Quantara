from __future__ import annotations
from dataclasses import dataclass
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[4]))
    from shared.io.timeparse import to_datetime_iso
from ..base import PrimarySideContinuationError,extract_primary_source_identity
COMPRESSION_BREAKOUT_SHORT_PRIMARY_SIGNATURE="0e927dda10e6f2e774684d34cf04d8fd4c5a2fe5cda0f64786aa05d5697357b4"
PROVIDER_SPEC={"provider_id":"phase_d4_compressionbreakout_short_primary","provider_version":"1","supported_signatures":{COMPRESSION_BREAKOUT_SHORT_PRIMARY_SIGNATURE:{"side_value":-1}}}
@dataclass
class _S:processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class CompressionBreakoutShortPrimarySideContinuation:
 def __init__(self,*,identity):self.identity=identity;self._state=_S()
 def reset(self):self._state=_S()
 @staticmethod
 def _ts(v):
  x=to_datetime_iso(v,utc=True,errors="coerce")
  if pd.isna(x):raise PrimarySideContinuationError("invalid timestamp")
  return pd.Timestamp(x)
 def bootstrap(self,f):
  self.reset()
  for v in to_datetime_iso(f["Datetime"],utc=True,errors="coerce").dropna().sort_values():self._state.last_timestamp=pd.Timestamp(v);self._state.processed_rows+=1
  return self.state_summary()
 def update_one(self,r):
  p=r.to_dict() if isinstance(r,pd.Series) else dict(r);ts=self._ts(p["Datetime"])
  if self._state.last_timestamp is not None and ts<=self._state.last_timestamp:raise PrimarySideContinuationError("non-increasing")
  self._state.last_timestamp=ts;self._state.processed_rows+=1;return {"Datetime":ts,"primary_side":-1}
 def filter_event(self,event):
  if event is None:return None
  e=dict(event)
  if self._ts(e["Datetime"])!=self._state.last_timestamp:raise PrimarySideContinuationError("timestamp mismatch")
  if str(e["edge_id"])!=self.identity.edge_id:raise PrimarySideContinuationError("edge mismatch")
  def n(k):return float(pd.to_numeric(pd.Series([e[k]]),errors="coerce").iat[0])
  ok=(int(n("Compression_Breakout_Event"))==1 and int(n("Breakout_Direction"))==-1 and int(n("Compression_Seen_Recently"))==1 and int(n("First_Downside_Cross"))==1 and int(n("Downside_Structural_Break"))==1)
  if "Close" in e:ok=ok and n("Close")<n("Structural_Support")
  if not ok:return None
  e["primary_side_rule"]="recent past-only compression followed by the first close below shifted structural support maps to side=-1 short";e["primary_side"]=-1;e["primary_side_filter"]=1;e["side"]=-1;return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported primary")
 return CompressionBreakoutShortPrimarySideContinuation(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge))
