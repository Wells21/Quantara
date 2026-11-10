from __future__ import annotations
"""Exact incremental breakdown-retest-failure V3 state machine."""
import bisect
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_LOOKBACK_BARS -> EVENT_VOL_LOOKBACK_HOURS).
BREAKDOWN_RETEST_SHORT_SIGNATURE="01444a79279dd73bc48cb81ecc84ec2b6370acd0c0eddf675534d34bf19bdfc1"
PROVIDER_SPEC={"provider_id":"phase_d4_breakdownretestfailure_short_candidate","provider_version":"1","supported_signatures":{BREAKDOWN_RETEST_SHORT_SIGNATURE:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);close_hist_ns:list[int]=field(default_factory=list);low_hist:list[float]=field(default_factory=list);atr_hist:list[float]=field(default_factory=list);prev_break_state:dict[int,int]=field(default_factory=dict);active:list[dict]=field(default_factory=list);last_event_index:int=-(10**9);sequence:int=0;processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class BreakdownRetestFailureShortCandidateContinuation:
 def __init__(self,*,identity,eps,support_lookbacks,retest_min_bars,retest_max_bars,retest_tolerance_atr,failure_confirmation_max_bars,decisive_reclaim_atr,local_duplicate_gap_bars,local_duplicate_distance_atr,min_event_gap_bars,event_vol_lookback_hours,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.looks=tuple(int(x) for x in support_lookbacks);self.rmin=int(retest_min_bars);self.rmax=int(retest_max_bars);self.tol=float(retest_tolerance_atr);self.fmax=int(failure_confirmation_max_bars);self.reclaim=float(decisive_reclaim_atr);self.dupgap=int(local_duplicate_gap_bars);self.dupdist=float(local_duplicate_distance_atr);self.egap=int(min_event_gap_bars);self.evlook_hours=float(event_vol_lookback_hours);self.evlook_ns=int(round(self.evlook_hours*3600.0*1e9));self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self._state=self._new()
 def _new(self):return _S(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.evspan),prev_break_state={x:0 for x in self.looks})
 def reset(self):self._state=self._new()
 @staticmethod
 def _ts(v):
  x=to_datetime_iso(v,utc=True,errors="coerce")
  if pd.isna(x):raise CandidateContinuationError("invalid ts")
  return pd.Timestamp(x)
 @staticmethod
 def _num(v,n):
  x=pd.to_numeric(pd.Series([v]),errors="coerce").iat[0]
  if pd.isna(x) or not np.isfinite(float(x)):raise CandidateContinuationError(f"invalid {n}")
  return float(x)
 def _norm(self,f):
  req=["Datetime","Bar_Start","Open","High","Low","Close","ATR_14"];miss=[c for c in req if c not in f]
  if miss:raise CandidateContinuationError(f"missing {miss}")
  o=f.copy();o["Datetime"]=to_datetime_iso(o["Datetime"],utc=True,errors="coerce");o["Bar_Start"]=to_datetime_iso(o["Bar_Start"],utc=True,errors="coerce")
  for c in ["Open","High","Low","Close","ATR_14",*OPTIONAL]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  o=o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True);return o[(o.Open>0)&(o.High>0)&(o.Low>0)&(o.Close>0)&(o.ATR_14>0)].copy()
 def _eventvol(self,c,ts):
  # Calendar-time backward lookup -- see absorption.py's identical port
  # (verified 2026-09-25 via synthetic parity test, max abs diff = 0.0).
  s=self._state;ts_ns=int(ts.value);target_ns=ts_ns-self.evlook_ns;pos=bisect.bisect_left(s.close_hist_ns,target_ns)-1
  ret=float(np.log((c+self.eps)/(s.close_hist[pos]+self.eps))) if pos>=0 else math.nan;var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 def _duplicate(self,look,support,shifted):
  for q in self._state.active:
   if q["lookback"]!=look:continue
   if self._state.processed_rows-q["index"]<=self.dupgap and abs(support-q["support"])/(shifted+self.eps)<=self.dupdist:return True
  return False
 @staticmethod
 def _primary(confirmed,fc):
  return max(confirmed,key=lambda q:(q["lookback"],-abs(q["support"]-fc),q["index"]))
 def _merge(self,base,confirmed,shifted):
  i=self._state.processed_rows;fc=base["Close"];p=self._primary(confirmed,fc);rt=p["retest"];supports=[q["support"] for q in confirmed];looks=sorted(set(q["lookback"] for q in confirmed));ages=[i-q["index"] for q in confirmed];support=p["support"]
  out=dict(base);out.update({"Breakdown_Retest_Failure_Event":1,"Breakdown_Confirmed":1,"Retest_Touched":1,"Failed_Reclaim":1,
  "Breakdown_Bars_Ago":i-p["index"],"Retest_Bars_After_Breakdown":rt["index"]-p["index"],"Failure_Bars_After_Retest":i-rt["index"],
  "Broken_Support_Level":support,"Primary_Support_Lookback":p["lookback"],"Support_Confluence_Count":len(confirmed),"Support_Horizon_Count":len(looks),"Support_Lookback_Min":min(looks),"Support_Lookback_Max":max(looks),"Multiple_Supports_Failed":int(len(confirmed)>1),
  "Confirmed_Support_20":int(20 in looks),"Confirmed_Support_50":int(50 in looks),"Confirmed_Support_100":int(100 in looks),"Confirmed_Support_Level_Min":min(supports),"Confirmed_Support_Level_Max":max(supports),"Support_Level_Spread_ATR":(max(supports)-min(supports))/(shifted+self.eps),
  "Earliest_Breakdown_Bars_Ago":max(ages),"Latest_Breakdown_Bars_Ago":min(ages),"Breakdown_Close":p["close"],"Breakdown_Low":p["low"],"Breakdown_Shifted_ATR_14":p["atr"],"Breakdown_Depth_ATR":p["depth_atr"],"Breakdown_Log_Return_1":p["log_return_1"],"Breakdown_Body_Direction":p["body_direction"],"Breakdown_Body_Share":p["body_share"],"Breakdown_Close_Location":p["close_location"],"Breakdown_Sequence_Number":p["sequence_number"],
  "Shifted_ATR_14":shifted,"Breakdown_Datetime":p["datetime"],"Retest_Datetime":rt["datetime"],"Failure_Datetime":base["Datetime"],"Retest_Threshold_Price":rt["threshold"],"Retest_Decisive_Reclaim_Price":rt["decisive_reclaim_price"],"Retest_High":rt["high"],"Retest_Low":rt["low"],"Retest_Close":rt["close"],"Retest_Distance_ATR":rt["distance_atr"],"Retest_Penetration_ATR":rt["penetration_atr"],"Retest_Reclaim_Depth_ATR":rt["reclaim_depth_atr"],"Retest_Closed_Above_Support":int(rt["close"]>=support),
  "Failure_Decisive_Reclaim_Price":support+self.reclaim*shifted,"Failure_High":base["High"],"Failure_Low":base["Low"],"Failure_Close":fc,"Failure_Shifted_ATR_14":shifted,"Failed_Reclaim_Distance_ATR":(support-fc)/(shifted+self.eps)})
  return out
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close");atr=self._num(r["ATR_14"],"ATR_14")
  shifted=s.atr_hist[-1] if s.atr_hist else math.nan;prevclose=s.close_hist[-1] if s.close_hist else math.nan;logret=float(np.log((c+self.eps)/(prevclose+self.eps))) if np.isfinite(prevclose) else math.nan;rng=max(h-l,self.eps);body=c-o
  supports={look:(min(s.low_hist[-look:]) if len(s.low_hist)>=look else math.nan) for look in self.looks};states={look:int(np.isfinite(supports[look]) and c<supports[look]) for look in self.looks};crosses={look:int(states[look]==1 and s.prev_break_state.get(look,0)==0) for look in self.looks};ev=self._eventvol(c,ts)
  base={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,"Event_Vol":ev,"Range_ATR":rng/(shifted+self.eps) if np.isfinite(shifted) else math.nan,"Body_Direction":float(np.sign(body)),"Body_Share":abs(body)/(rng+self.eps),"Close_Location":(c-l)/(rng+self.eps),"Bearish_Response":int(c<o)}
  for k in OPTIONAL:
   if k in r:base[k]=float(r[k]) if pd.notna(r[k]) else math.nan
  survivors=[];confirmed=[]
  for q in s.active:
   support=q["support"];age=s.processed_rows-q["index"]
   if q["retest"] is None:
    if age>self.rmax:continue
    if age<self.rmin or not np.isfinite(shifted):survivors.append(q);continue
    threshold=support-self.tol*shifted
    if h<threshold:survivors.append(q);continue
    decisive=support+self.reclaim*shifted
    if c>=decisive:continue
    q["retest"]={"index":s.processed_rows,"datetime":ts,"threshold":threshold,"decisive_reclaim_price":decisive,"high":h,"low":l,"close":c,"distance_atr":(support-h)/(shifted+self.eps),"penetration_atr":(h-support)/(shifted+self.eps),"reclaim_depth_atr":max((c-support)/(shifted+self.eps),0.0)}
    if c<support:confirmed.append(q)
    else:survivors.append(q)
   else:
    fage=s.processed_rows-q["retest"]["index"]
    if fage>self.fmax:continue
    if not np.isfinite(shifted):survivors.append(q);continue
    if c>=support+self.reclaim*shifted:continue
    if c<support:confirmed.append(q)
    else:survivors.append(q)
  s.active=survivors;em=None
  if confirmed and s.processed_rows-s.last_event_index>=self.egap:
   merged=self._merge(base,confirmed,shifted)
   if np.isfinite(ev):
    em=merged;em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|multilevel_failed_retest","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False,
    "support_lookback_count":len(self.looks),"support_lookback_min":min(self.looks),"support_lookback_max":max(self.looks),"retest_min_bars":self.rmin,"retest_max_bars":self.rmax,"retest_tolerance_atr":self.tol,"failure_confirmation_max_bars":self.fmax,"decisive_reclaim_atr":self.reclaim,"local_duplicate_gap_bars":self.dupgap,"local_duplicate_distance_atr":self.dupdist,"breakdown_rearm_gap_bars":self.dupgap,"breakdown_rearm_distance_atr":self.dupdist,"min_gap_bars":self.egap,"event_vol_lookback_hours":self.evlook_hours,"event_vol_ewm_span":self.evspan})
   s.last_event_index=s.processed_rows
  # arm after processing existing setups
  if np.isfinite(shifted):
   for look in self.looks:
    if not crosses[look]:continue
    support=supports[look]
    if not np.isfinite(support) or support<=0 or self._duplicate(look,support,shifted):continue
    s.sequence+=1;s.active.append({"index":s.processed_rows,"datetime":ts,"support":support,"lookback":look,"close":c,"low":l,"atr":shifted,"depth_atr":(support-c)/(shifted+self.eps),"log_return_1":logret,"body_direction":float(np.sign(body)),"body_share":abs(body)/(rng+self.eps),"close_location":(c-l)/(rng+self.eps),"sequence_number":s.sequence,"retest":None})
  ts_ns=int(ts.value);s.close_hist.append(c);s.close_hist_ns.append(ts_ns);s.low_hist.append(l);s.atr_hist.append(atr)
  # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
  # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
  # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
  cutoff_ns=ts_ns-self.evlook_ns;drop=bisect.bisect_left(s.close_hist_ns,cutoff_ns)-1
  if drop>0:del s.close_hist[:drop];del s.close_hist_ns[:drop]
  if len(s.low_hist)>max(self.looks):del s.low_hist[:len(s.low_hist)-max(self.looks)]
  if len(s.atr_hist)>2:del s.atr_hist[:-2]
  s.prev_break_state=states;s.processed_rows+=1;s.last_timestamp=ts;return em
 def bootstrap(self,f):
  x=self._norm(f);self.reset()
  for r in x.to_dict("records"):self._advance(r,False)
  return self.state_summary()
 def update_one(self,r):return self._advance(r.to_dict() if isinstance(r,pd.Series) else dict(r),True)
 def replay(self,f):
  x=self._norm(f);self.reset();rows=[]
  for r in x.to_dict("records"):
   e=self._advance(r,False)
   if e is not None:rows.append(e)
  return pd.DataFrame(rows)
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"active_setups":len(self._state.active),"low_history_rows":len(self._state.low_hist),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 looks=c.get("SUPPORT_LOOKBACKS",(20,50,100))
 return BreakdownRetestFailureShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),support_lookbacks=tuple(looks),retest_min_bars=int(_c(c,"RETEST_MIN_BARS",1)),retest_max_bars=int(_c(c,"RETEST_MAX_BARS",20)),retest_tolerance_atr=float(_c(c,"RETEST_TOLERANCE_ATR",.25)),failure_confirmation_max_bars=int(_c(c,"FAILURE_CONFIRMATION_MAX_BARS",3)),decisive_reclaim_atr=float(_c(c,"DECISIVE_RECLAIM_ATR",.5)),local_duplicate_gap_bars=int(_c(c,"LOCAL_DUPLICATE_GAP_BARS",5)),local_duplicate_distance_atr=float(_c(c,"LOCAL_DUPLICATE_DISTANCE_ATR",.1)),min_event_gap_bars=int(_c(c,"MIN_EVENT_GAP_BARS",1)),event_vol_lookback_hours=float(_c(c,"EVENT_VOL_LOOKBACK_HOURS",8.0)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
