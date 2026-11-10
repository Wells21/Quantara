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
SIGNENTROPY_SHORT_PRIMARY_SIGNATURE="ebc50575170a14be4908647b0bd9cf5225aa1d1fd5c9b9dddaf11a0da4c841ff"
PROVIDER_SPEC={"provider_id":"phase_d4_signentropycollapse_short_primary","provider_version":"1","supported_signatures":{SIGNENTROPY_SHORT_PRIMARY_SIGNATURE:{"side_value":-1}}}
@dataclass
class _S:processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class EntropyPrimary:
 def __init__(self,*,identity,neg=.5,pnn=.52,adv=0,ent=.65):self.identity=identity;self.neg=neg;self.pnn=pnn;self.adv=adv;self.ent=ent;self._state=_S()
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
  n=lambda k:float(pd.to_numeric(pd.Series([e[k]]),errors="coerce").iat[0]);entry=n("Entropy_State_Entry");renew=n("Entropy_Renewed_Impulse")
  ok=(n("Sign_Entropy_Collapse_Event")==1 and n("Entropy_Active_State")==1 and n("Negative_Event_Bar")==1 and n("Log_Return_1")<0 and n("Negative_Sign_Fraction_20")>=self.neg and n("Entropy_Percentile_20_Over_500")<=self.ent and n("P_Neg_After_Neg_50")>=self.pnn and n("Downside_Transition_Advantage_50")>=self.adv and n("Event_Vol")>0 and entry+renew==1)
  if not ok:return None
  e["primary_side_rule"]="negative completed-bar impulses inside a low-entropy, downside-dominated and conditionally persistent sign state map to side=-1 short; both initial state entries and spaced renewed impulses are valid";e["primary_side"]=-1;e["primary_side_filter"]=1;e["side"]=-1;return e
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_ffd_disk_reads":False,"normal_path_history_disk_reads":False,"normal_path_full_primary_fallback":False}
def create_provider(*,primary_logic_signature,d1_edge,v2_root):
 sig=str(primary_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise PrimarySideContinuationError("unsupported")
 c=(d1_edge.get("primary_side") or {}).get("semantic_constants") or {};return EntropyPrimary(identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge),neg=float(c.get("NEGATIVE_FRACTION_THRESHOLD",.5)),pnn=float(c.get("NEG_AFTER_NEG_THRESHOLD",.52)),adv=float(c.get("TRANSITION_ADVANTAGE_THRESHOLD",0)),ent=float(c.get("ENTROPY_PERCENTILE_THRESHOLD",.65)))
