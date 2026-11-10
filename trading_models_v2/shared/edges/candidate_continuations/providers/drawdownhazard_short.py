from __future__ import annotations
"""Exact online drawdownhazardcontinuation_short provider with delayed causal resolution."""
from collections import defaultdict
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
import bisect
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS).
# 2026-10-18: v5.2 re-selection changed only the D1 constant EVENT_VOL_LOOKBACK_HOURS (8.519 -> 8.0); logic identical,
# constants are read from D1, so the current signature is added (pre-v5.2 signature kept as legacy).
DRAWDOWNHAZARD_SHORT_SIGNATURE_PRE_V52="11011cecdd833d8bcbd375a95631d22915ddfd06b393ff67ec6d7f6a16075cb0"
DRAWDOWNHAZARD_SHORT_SIGNATURE="f0a04a7e00e55e4b2d83546f09e585776c8bbec91f95ab84e11f320ba22d261b"
PROVIDER_SPEC={"provider_id":"phase_d4_drawdownhazardcontinuation_short_candidate","provider_version":"1","supported_signatures":{DRAWDOWNHAZARD_SHORT_SIGNATURE:{"side_specialization":"short"},DRAWDOWNHAZARD_SHORT_SIGNATURE_PRE_V52:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_ev:list[float]=field(default_factory=list);close_ev_ns:list[int]=field(default_factory=list);path:list[tuple]=field(default_factory=list);severity_hist:list[float]=field(default_factory=list);new_trough_flags:list[int]=field(default_factory=list);ret20:list[float]=field(default_factory=list);episode_peak_index:int=-1;episode:Optional[dict]=None;last_observation:int=-(10**9);observations:list[dict]=field(default_factory=list);pending_updates:list[tuple]=field(default_factory=list);primary:dict=field(default_factory=dict);secondary:dict=field(default_factory=dict);tertiary:dict=field(default_factory=dict);global_counter:list[float]=field(default_factory=lambda:[0.,0.,0.]);previous_active:bool=False;emitted_in_state:bool=False;last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class DrawdownHazardShortCandidateContinuation:
 def __init__(self,*,identity,eps,primary_peak_window,secondary_peak_windows,severity_baseline_window,hazard_resolution_max_bars,hazard_observation_gap_bars,new_trough_buffer_vol,recovery_success_fraction,recovery_attempt_fraction,recovery_failure_fraction,min_drawdown_severity_percentile,max_recovery_fraction,min_hazard_probability,min_hazard_cell_observations,min_event_gap_bars,hazard_prior_alpha,hazard_prior_beta,event_vol_lookback_hours,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.pw=int(primary_peak_window);self.second=tuple(int(x) for x in secondary_peak_windows);self.sevbase=int(severity_baseline_window);self.resmax=int(hazard_resolution_max_bars);self.obsgap=int(hazard_observation_gap_bars);self.buffer=float(new_trough_buffer_vol);self.recsuccess=float(recovery_success_fraction);self.recattempt=float(recovery_attempt_fraction);self.recfail=float(recovery_failure_fraction);self.minsev=float(min_drawdown_severity_percentile);self.maxrec=float(max_recovery_fraction);self.minp=float(min_hazard_probability);self.mincount=int(min_hazard_cell_observations);self.egap=int(min_event_gap_bars);self.alpha=float(hazard_prior_alpha);self.beta=float(hazard_prior_beta);self.evlook_hours=float(event_vol_lookback_hours);self.evlook_ns=int(round(self.evlook_hours*3600.0*1e9));self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self.maxpeak=max((self.pw,*self.second));self._state=self._new()
 def _new(self):
  return _S(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.evspan),primary=defaultdict(lambda:[0.,0.,0.]),secondary=defaultdict(lambda:[0.,0.,0.]),tertiary=defaultdict(lambda:[0.,0.,0.]))
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
  req=["Datetime","Bar_Start","Open","High","Low","Close"];miss=[c for c in req if c not in f]
  if miss:raise CandidateContinuationError(f"missing {miss}")
  o=f.copy();o["Datetime"]=to_datetime_iso(o["Datetime"],utc=True,errors="coerce");o["Bar_Start"]=to_datetime_iso(o["Bar_Start"],utc=True,errors="coerce")
  for c in ["Open","High","Low","Close","ATR_14",*OPTIONAL]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  o=o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True);return o[(o.Open>0)&(o.High>0)&(o.Low>0)&(o.Close>0)].copy()
 def _eventvol(self,c,ts):
  # Calendar-time backward lookup -- see absorption.py's identical port
  # (verified 2026-09-25 via synthetic parity test, max abs diff = 0.0).
  s=self._state;ts_ns=int(ts.value);target_ns=ts_ns-self.evlook_ns;pos=bisect.bisect_left(s.close_ev_ns,target_ns)-1
  ret=float(np.log((c+self.eps)/(s.close_ev[pos]+self.eps))) if pos>=0 else math.nan;var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 @staticmethod
 def _latest_peak(records,w):
  subset=records[-w:];mx=max(x[1] for x in subset);idx=max(x[0] for x in subset if x[1]==mx);return float(mx),int(idx)
 def _new_episode(self,peak,idx):return {"peak":float(peak),"peak_index":int(idx),"trough":float(peak),"trough_index":int(idx),"new_trough_count":0,"failed_recovery_count":0,"last_failed_index":-1,"maximum_recovery":0.0,"attempt_active":False,"can_arm_attempt":True,"negative_count":0}
 def _advance_episode(self,state,price,index,negative):
  if negative:state["negative_count"]+=1
  peak=float(state["peak"]);tb=float(state["trough"]);den=peak-tb;rb=(price-tb)/(den+self.eps) if den>self.eps else 0.0
  if state["attempt_active"]:
   if rb>=self.recsuccess:state["attempt_active"]=False;state["can_arm_attempt"]=False
   elif rb<=self.recfail:state["failed_recovery_count"]+=1;state["last_failed_index"]=int(index);state["attempt_active"]=False;state["can_arm_attempt"]=False
  elif rb<self.recattempt*.75:state["can_arm_attempt"]=True
  if state["can_arm_attempt"] and self.recattempt<=rb<self.recsuccess:state["attempt_active"]=True
  new=bool(price<tb-self.eps)
  if new:
   state["trough"]=float(price);state["trough_index"]=int(index);state["new_trough_count"]+=1;state["maximum_recovery"]=0.;state["attempt_active"]=False;state["can_arm_attempt"]=True;rec=0.
  else:
   den=peak-float(state["trough"]);rec=(price-float(state["trough"]))/(den+self.eps) if den>self.eps else 1.;state["maximum_recovery"]=max(float(state["maximum_recovery"]),float(rec))
  return float(np.clip(rec,0,2)),new
 @staticmethod
 def _bin_sev(v):
  if not np.isfinite(v):return -1
  return 0 if v<.5 else 1 if v<.75 else 2 if v<.9 else 3
 @staticmethod
 def _bin_age(v):
  if not np.isfinite(v):return -1
  return 0 if v<=10 else 1 if v<=30 else 2 if v<=80 else 3
 @staticmethod
 def _bin_rec(v):
  if not np.isfinite(v):return -1
  return 0 if v<=.1 else 1 if v<=.25 else 2 if v<=.5 else 3
 @staticmethod
 def _update_counter(counter,key,outcome,lag):
  if key not in counter:counter[key]=[0.,0.,0.]
  row=counter[key];row[0]+=float(outcome);row[1]+=1.;row[2]+=float(lag)
 def _apply_pending(self):
  s=self._state
  for sb,ab,rb,out,lag in s.pending_updates:
   self._update_counter(s.primary,(sb,ab,rb),out,lag);self._update_counter(s.secondary,(sb,rb),out,lag);self._update_counter(s.tertiary,sb,out,lag);s.global_counter[0]+=out;s.global_counter[1]+=1;s.global_counter[2]+=lag
  s.pending_updates=[]
 def _resolve_existing(self,c,i):
  s=self._state;remain=[]
  for ob in s.observations:
   age=i-ob["start"]
   if age<=0:remain.append(ob);continue
   cont=ob["trough"]*(1-self.buffer*ob["event_vol"]);rec=ob["trough"]+self.recsuccess*(ob["peak"]-ob["trough"]);resolved=None
   if c<=cont:resolved=1
   elif c>=rec:resolved=0
   if resolved is not None:s.pending_updates.append((ob["sb"],ob["ab"],ob["rb"],resolved,age))
   elif age<self.resmax:remain.append(ob)
  s.observations=remain
 def _advance(self,r,enforce):
  s=self._state;i=s.processed_rows;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close")
  prev=s.path[-1][1] if s.path else math.nan;ret=float(np.log((c+self.eps)/(prev+self.eps))) if np.isfinite(prev) else math.nan;negative=bool(np.isfinite(ret) and ret<0);ev=self._eventvol(c,ts)
  current_records=s.path+[(i,c,ret)]
  peak50,idx50=self._latest_peak(current_records,50) if len(current_records)>=1 else (c,i);peak100,idx100=self._latest_peak(current_records,self.pw);peak250,idx250=self._latest_peak(current_records,250)
  depth50=c/(peak50+self.eps)-1;depth100=c/(peak100+self.eps)-1;depth250=c/(peak250+self.eps)-1;severity=max(0.,-depth100);drawactive=bool(severity>self.eps)
  if idx100!=s.episode_peak_index:
   ep=self._new_episode(peak100,idx100);rec=1.;new=False
   for idx,price,rr in current_records:
    if idx>idx100:rec,new=self._advance_episode(ep,price,idx,bool(np.isfinite(rr) and rr<0))
   s.episode=ep;s.episode_peak_index=idx100
  else:
   ep=s.episode;rec,new=self._advance_episode(ep,c,i,negative)
  ep=s.episode;age=float(i-idx100);trough=float(ep["trough"]);bars_trough=float(i-int(ep["trough_index"]));maxrec=float(ep["maximum_recovery"])
  pct=float(np.mean(np.asarray(s.severity_hist[-self.sevbase:])<=severity)) if len(s.severity_hist)>=self.sevbase else math.nan
  sev3=severity-s.severity_hist[-3] if len(s.severity_hist)>=3 else math.nan;sev5=severity-s.severity_hist[-5] if len(s.severity_hist)>=5 else math.nan;sev10=severity-s.severity_hist[-10] if len(s.severity_hist)>=10 else math.nan
  accel=sev3-sev10 if np.isfinite(sev3) and np.isfinite(sev10) else math.nan
  flags=s.new_trough_flags+[int(new)];new20=float(sum(flags[-20:]))
  retvals=s.ret20+[ret];last20=retvals[-20:];valid20=len(last20)>=20 and np.isfinite(last20).all()
  negfrac=float(np.mean(np.asarray(last20)<0)) if valid20 else math.nan;cumneg=float(np.sum(np.minimum(last20,0))) if valid20 else math.nan;semivar=float(np.mean(np.square(np.minimum(last20,0)))) if valid20 else math.nan;down=float(np.sum(np.abs(np.minimum(last20,0)))) if valid20 else math.nan;up=float(np.sum(np.maximum(last20,0))) if valid20 else math.nan;downup=down/(up+self.eps) if valid20 else math.nan
  atr=self._num(r["ATR_14"],"ATR_14") if "ATR_14" in r and pd.notna(r["ATR_14"]) else (c*ev if np.isfinite(ev) else math.nan);disttr=(c-trough)/(atr+self.eps) if np.isfinite(atr) else math.nan
  # Only outcomes resolved strictly before current may enter the hazard table.
  self._apply_pending();self._resolve_existing(c,i)
  sb,ab,rb=self._bin_sev(pct),self._bin_age(age),self._bin_rec(rec)
  candidates=[(0,s.primary[(sb,ab,rb)]),(1,s.secondary[(sb,rb)]),(2,s.tertiary[sb]),(3,s.global_counter)] if min(sb,ab,rb)>=0 else []
  level=4;selected=s.global_counter
  for lev,row in candidates:
   if row[1]>=self.mincount:level=lev;selected=row;break
  successes,count,lags=selected;p=(successes+self.alpha)/(count+self.alpha+self.beta);reslag=lags/count if count>0 else math.nan;global_count=s.global_counter[1]
  active=int(drawactive and np.isfinite(pct) and pct>=self.minsev and rec<=self.maxrec and p>=self.minp and count>=self.mincount)
  # observation creation consumes gap even if resolution will later be unavailable
  if idx100!=getattr(s,"observation_peak_index",-999999):
   s.observation_peak_index=idx100;s.last_observation=-(10**9)
  if drawactive and np.isfinite(pct) and rec<self.recsuccess and i-s.last_observation>=self.obsgap and min(sb,ab,rb)>=0:
   s.last_observation=i
   if np.isfinite(ev) and ev>0 and peak100>trough:s.observations.append({"start":i,"peak":peak100,"trough":trough,"event_vol":ev,"sb":sb,"ab":ab,"rb":rb})
  if active and not s.previous_active:s.emitted_in_state=False
  if not active:s.emitted_in_state=False
  em=None
  if active and negative and i-s.last_event_index>=self.egap:
   entry=int(not s.emitted_in_state);s.emitted_in_state=True;s.last_event_index=i;rng=max(h-l,self.eps)
   em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,
   "Event_Vol":ev,"Drawdown_Hazard_Event":1,"Drawdown_Hazard_Active_State":active,"Drawdown_Hazard_State_Entry":entry,"Drawdown_Hazard_Renewed_Impulse":1-entry,"Negative_Event_Bar":1,"Log_Return_1":ret,"Drawdown_Active":int(drawactive),
   "Drawdown_Peak_50":peak50,"Drawdown_Peak_100":peak100,"Drawdown_Peak_250":peak250,"Drawdown_Depth_50":depth50,"Drawdown_Depth_100":depth100,"Drawdown_Depth_250":depth250,"Drawdown_Severity_100":severity,"Drawdown_Severity_Percentile_100_Over_1000":pct,
   "Bars_Since_Peak_100":age,"Time_Under_Water_100":age if drawactive else 0.,"Drawdown_Trough":trough,"Bars_Since_Trough":bars_trough,"Distance_From_Trough_ATR":disttr,"Recovery_Fraction":rec,"Maximum_Recovery_Fraction":maxrec,
   "Drawdown_Velocity_3":sev3,"Drawdown_Velocity_5":sev5,"Drawdown_Velocity_10":sev10,"Drawdown_Acceleration_3_10":accel,"New_Trough_Bar":int(new),"New_Trough_Count_20":new20,"New_Trough_Count_Since_Peak":int(ep["new_trough_count"]),"Trough_Renewal_Rate":int(ep["new_trough_count"])/(age+1.),
   "Failed_Recovery_Count":int(ep["failed_recovery_count"]),"Bars_Since_Last_Failed_Recovery":float(i-ep["last_failed_index"]) if ep["last_failed_index"]>=0 else math.nan,"Negative_Return_Fraction_20":negfrac,"Negative_Return_Fraction_Since_Peak":float(ep["negative_count"])/max(1,i-idx100),"Cumulative_Return_Since_Peak":float(np.log((c+self.eps)/(peak100+self.eps))),"Cumulative_Negative_Return_20":cumneg,"Downside_Semivariance_20":semivar,"Downside_Upmove_Ratio_20":downup,
   "Drawdown_Hazard_Probability":p,"Drawdown_Hazard_Cell_Count":count,"Drawdown_Hazard_Backoff_Level":float(level),"Drawdown_Hazard_Resolution_Lag":reslag,"Drawdown_Hazard_Global_Count":global_count,
   "Body_Direction":float(np.sign(c-o)),"Body_Share":abs(c-o)/(rng+self.eps),"Close_Location":(c-l)/(rng+self.eps)}
   for k in OPTIONAL:
    if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
   em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|drawdown_hazard_impulse","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False,
   "primary_peak_window":self.pw,"severity_baseline_window":self.sevbase,"hazard_resolution_max_bars":self.resmax,"hazard_observation_gap_bars":self.obsgap,"new_trough_buffer_vol":self.buffer,"recovery_success_fraction":self.recsuccess,"min_drawdown_severity_percentile":self.minsev,"max_recovery_fraction":self.maxrec,"min_hazard_probability":self.minp,"min_hazard_cell_observations":self.mincount,"min_gap_bars":self.egap})
  s.path.append((i,c,ret))
  if len(s.path)>self.maxpeak:del s.path[:len(s.path)-self.maxpeak]
  s.close_ev.append(c);s.close_ev_ns.append(int(ts.value))
  # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
  # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
  # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
  cutoff_ns=int(ts.value)-self.evlook_ns;drop=bisect.bisect_left(s.close_ev_ns,cutoff_ns)-1
  if drop>0:del s.close_ev[:drop];del s.close_ev_ns[:drop]
  s.severity_hist.append(severity)
  if len(s.severity_hist)>self.sevbase:del s.severity_hist[:len(s.severity_hist)-self.sevbase]
  s.new_trough_flags.append(int(new))
  if len(s.new_trough_flags)>20:del s.new_trough_flags[:-20]
  s.ret20.append(ret)
  if len(s.ret20)>20:del s.ret20[:-20]
  s.previous_active=bool(active);s.processed_rows+=1;s.last_timestamp=ts;return em
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
 def state_summary(self):
  s=self._state;return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":s.processed_rows,"path_rows":len(s.path),"severity_rows":len(s.severity_hist),"unresolved_hazard_observations":len(s.observations),"hazard_global_count":s.global_counter[1],"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return DrawdownHazardShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),primary_peak_window=int(_c(c,"PRIMARY_PEAK_WINDOW",100)),secondary_peak_windows=tuple(_c(c,"SECONDARY_PEAK_WINDOWS",(50,250))),severity_baseline_window=int(_c(c,"SEVERITY_BASELINE_WINDOW",1000)),hazard_resolution_max_bars=int(_c(c,"HAZARD_RESOLUTION_MAX_BARS",250)),hazard_observation_gap_bars=int(_c(c,"HAZARD_OBSERVATION_GAP_BARS",3)),new_trough_buffer_vol=float(_c(c,"NEW_TROUGH_BUFFER_VOL",.25)),recovery_success_fraction=float(_c(c,"RECOVERY_SUCCESS_FRACTION",.35)),recovery_attempt_fraction=float(_c(c,"RECOVERY_ATTEMPT_FRACTION",.15)),recovery_failure_fraction=float(_c(c,"RECOVERY_FAILURE_FRACTION",.05)),min_drawdown_severity_percentile=float(_c(c,"MIN_DRAWDOWN_SEVERITY_PERCENTILE",.5)),max_recovery_fraction=float(_c(c,"MAX_RECOVERY_FRACTION",.4)),min_hazard_probability=float(_c(c,"MIN_HAZARD_PROBABILITY",.52)),min_hazard_cell_observations=int(_c(c,"MIN_HAZARD_CELL_OBSERVATIONS",30)),min_event_gap_bars=int(_c(c,"MIN_EVENT_GAP_BARS",3)),hazard_prior_alpha=float(_c(c,"HAZARD_PRIOR_ALPHA",1)),hazard_prior_beta=float(_c(c,"HAZARD_PRIOR_BETA",1)),event_vol_lookback_hours=float(_c(c,"EVENT_VOL_LOOKBACK_HOURS",8.0)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
