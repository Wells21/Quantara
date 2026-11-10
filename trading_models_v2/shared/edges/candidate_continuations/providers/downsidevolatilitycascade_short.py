from __future__ import annotations
"""Exact incremental downsidevolatilitycascade_short provider."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
DOWNSIDEVOLCASCADE_SHORT_SIGNATURE="4de2eadb9063d3bea67631824d348274d5deb848f2906218b0393e5ac9c1e4d5"
PROVIDER_SPEC={"provider_id":"phase_d4_downsidevolatilitycascade_short_candidate","provider_version":"1","supported_signatures":{DOWNSIDEVOLCASCADE_SHORT_SIGNATURE:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);ret_hist:list[float]=field(default_factory=list);atr_hist:list[float]=field(default_factory=list);prev_active:int=0;last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class DownsideVolatilityCascadeShortCandidateContinuation:
 def __init__(self,*,identity,eps,semivariance_window,baseline_window,baseline_exclude_recent_bars,downside_share_threshold,vol_acceleration_threshold,min_event_gap_bars,event_vol_lookback_bars,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.semi=int(semivariance_window);self.base=int(baseline_window);self.exclude=int(baseline_exclude_recent_bars);self.share=float(downside_share_threshold);self.accel=float(vol_acceleration_threshold);self.gap=int(min_event_gap_bars);self.evlook=int(event_vol_lookback_bars);self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self._state=self._new()
 def _new(self):return _S(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.evspan))
 def reset(self):self._state=self._new()
 @staticmethod
 def _ts(v):
  x=to_datetime_iso(v,utc=True,errors="coerce")
  if pd.isna(x):raise CandidateContinuationError("invalid timestamp")
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
  return o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
 def _eventvol(self,c):
  s=self._state;ret=float(np.log((c+self.eps)/(s.close_hist[-self.evlook]+self.eps))) if len(s.close_hist)>=self.evlook else math.nan
  var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 @staticmethod
 def _consecutive(vals):
  n=0
  for x in vals[::-1]:
   if np.isfinite(x) and x<0:n+=1
   else:break
  return float(n)
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close");atr=self._num(r["ATR_14"],"ATR_14")
  prevc=s.close_hist[-1] if s.close_hist else math.nan;ret=float(np.log((c+self.eps)/(prevc+self.eps))) if np.isfinite(prevc) else math.nan
  current_hist=s.ret_hist+[ret]
  def stats(w):
   vals=current_hist[-w:]
   if len(vals)<w or not np.isfinite(vals).all():return (math.nan,)*8
   neg=np.asarray([min(x,0.0) for x in vals]);pos=np.asarray([max(x,0.0) for x in vals]);ds=float(np.sum(neg**2));us=float(np.sum(pos**2));rv=ds+us
   return ds,us,rv,ds/(rv+self.eps),ds/(us+self.eps),rv/w,float(np.sum(np.asarray(vals)<0)),float(np.sum(vals))
  st5=stats(5);st10=stats(10);st20=stats(20)
  # squared.shift(20).rolling(100).mean -> returns from i-120 .. i-21.
  baseline_vals=current_hist[-(self.base+self.exclude):-self.exclude] if len(current_hist)>=self.base+self.exclude else []
  baseline=float(np.mean(np.square(baseline_vals))) if len(baseline_vals)>=self.base else math.nan
  accel5=st5[5]/(baseline+self.eps) if np.isfinite(st5[5]) and np.isfinite(baseline) else math.nan
  accel10=st10[5]/(baseline+self.eps) if np.isfinite(st10[5]) and np.isfinite(baseline) else math.nan
  accel20=st20[5]/(baseline+self.eps) if np.isfinite(st20[5]) and np.isfinite(baseline) else math.nan
  vals20=current_hist[-20:];consec=self._consecutive(vals20)
  largest_neg=min(vals20) if len(vals20)>=20 else math.nan;largest_pos=max(vals20) if len(vals20)>=20 else math.nan
  negsq=max(min(ret,0.0),-1e99)**2 if np.isfinite(ret) else math.nan
  largest_share=(max(min(x,0.0)**2 for x in vals20)/(st20[0]+self.eps)) if len(vals20)>=20 else math.nan
  current_share=negsq/(st20[0]+self.eps) if np.isfinite(negsq) and np.isfinite(st20[0]) else math.nan
  shifted_atr=s.atr_hist[-1] if s.atr_hist else math.nan;rng=max(h-l,self.eps);rangeatr=rng/(shifted_atr+self.eps) if np.isfinite(shifted_atr) else math.nan
  ev=self._eventvol(c);dominance=bool(np.isfinite(st20[3]) and st20[3]>=self.share);accelerating=bool(np.isfinite(accel20) and accel20>=self.accel);active=int(dominance and accelerating);entry=int(active==1 and s.prev_active==0);negative=int(np.isfinite(ret) and ret<0)
  em=None
  if active and negative and s.processed_rows-s.last_event_index>=self.gap:
   s.last_event_index=s.processed_rows
   if np.isfinite(ev):
    em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,
    "Event_Vol":ev,"Downside_Volatility_Cascade_Event":1,"Cascade_State_Entry":entry,"Cascade_Renewed_Impulse":1-entry,"Cascade_Active_State":active,"Log_Return_1":ret,"Negative_Event_Bar":negative,
    "Downside_Semivariance_5":st5[0],"Upside_Semivariance_5":st5[1],"Realized_Variance_5":st5[2],
    "Downside_Semivariance_10":st10[0],"Upside_Semivariance_10":st10[1],"Realized_Variance_10":st10[2],
    "Downside_Semivariance_20":st20[0],"Upside_Semivariance_20":st20[1],"Realized_Variance_20":st20[2],
    "Downside_Variance_Share_5":st5[3],"Downside_Variance_Share_10":st10[3],"Downside_Variance_Share_20":st20[3],
    "Bad_Vol_Ratio_5":st5[4],"Bad_Vol_Ratio_10":st10[4],"Bad_Vol_Ratio_20":st20[4],
    "RV_Per_Bar_5":st5[5],"RV_Per_Bar_10":st10[5],"RV_Per_Bar_20":st20[5],"Baseline_RV_Per_Bar_100":baseline,
    "Vol_Acceleration_5_100":accel5,"Vol_Acceleration_10_100":accel10,"Vol_Acceleration_20_100":accel20,
    "Negative_Return_Count_5":st5[6],"Negative_Return_Count_10":st10[6],"Negative_Return_Count_20":st20[6],"Negative_Return_Fraction_20":st20[6]/20 if np.isfinite(st20[6]) else math.nan,
    "Consecutive_Negative_Returns":consec,"Largest_Negative_Return_20":largest_neg,"Largest_Positive_Return_20":largest_pos,
    "Cumulative_Return_5":st5[7],"Cumulative_Return_10":st10[7],"Cumulative_Return_20":st20[7],
    "Largest_Negative_Variance_Share_20":largest_share,"Current_Return_Share_Of_Downside_Variance":current_share,
    "Range_ATR":rangeatr,"Body_Direction":float(np.sign(c-o)),"Body_Share":abs(c-o)/(rng+self.eps),"Close_Location":(c-l)/(rng+self.eps)}
    for k in OPTIONAL:
     if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
    em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|cascade_impulse","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False})
  s.close_hist.append(c);s.ret_hist.append(ret);s.atr_hist.append(atr)
  if len(s.close_hist)>self.evlook:del s.close_hist[:len(s.close_hist)-self.evlook]
  keep=self.base+self.exclude
  if len(s.ret_hist)>keep:del s.ret_hist[:len(s.ret_hist)-keep]
  if len(s.atr_hist)>2:del s.atr_hist[:-2]
  s.prev_active=active;s.processed_rows+=1;s.last_timestamp=ts;return em
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
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"return_history_rows":len(self._state.ret_hist),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return DownsideVolatilityCascadeShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),semivariance_window=int(_c(c,"SEMIVARIANCE_WINDOW",20)),baseline_window=int(_c(c,"BASELINE_WINDOW",100)),baseline_exclude_recent_bars=int(_c(c,"BASELINE_EXCLUDE_RECENT_BARS",20)),downside_share_threshold=float(_c(c,"DOWNSIDE_SHARE_THRESHOLD",.6)),vol_acceleration_threshold=float(_c(c,"VOL_ACCELERATION_THRESHOLD",1.1)),min_event_gap_bars=int(_c(c,"MIN_EVENT_GAP_BARS",3)),event_vol_lookback_bars=int(_c(c,"EVENT_VOL_LOOKBACK_BARS",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
