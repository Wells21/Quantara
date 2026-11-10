from __future__ import annotations
"""Exact incremental signentropycollapse_short provider."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
SIGNENTROPY_SHORT_SIGNATURE="cb4d40942c9921d1cc1cf56c61b405198dd465983db700af6a23e85fbaec049e"
PROVIDER_SPEC={"provider_id":"phase_d4_signentropycollapse_short_candidate","provider_version":"1","supported_signatures":{SIGNENTROPY_SHORT_SIGNATURE:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);neg_hist:list[float]=field(default_factory=list);ret_hist:list[float]=field(default_factory=list);entropy20_hist:list[float]=field(default_factory=list);previous_active:bool=False;emitted_in_episode:bool=False;last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class SignEntropyCollapseShortCandidateContinuation:
 def __init__(self,*,identity,eps,sign_window_short,sign_window_primary,sign_window_transition,entropy_baseline_window,negative_fraction_threshold,neg_after_neg_threshold,transition_advantage_threshold,entropy_percentile_threshold,min_event_gap_bars,beta_prior_alpha,beta_prior_beta,event_vol_lookback,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.w10=int(sign_window_short);self.w20=int(sign_window_primary);self.w50=int(sign_window_transition);self.base=int(entropy_baseline_window);self.negthr=float(negative_fraction_threshold);self.pthr=float(neg_after_neg_threshold);self.advthr=float(transition_advantage_threshold);self.entthr=float(entropy_percentile_threshold);self.gap=int(min_event_gap_bars);self.a=float(beta_prior_alpha);self.b=float(beta_prior_beta);self.evlook=int(event_vol_lookback);self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self._state=self._new()
 def _new(self):return _S(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.evspan))
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
  for c in ["Open","High","Low","Close",*OPTIONAL]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  return o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
 def _entropy(self,p):
  if not np.isfinite(p):return math.nan
  p=float(np.clip(p,self.eps,1-self.eps));return float(-(p*np.log2(p)+(1-p)*np.log2(1-p)))
 def _bayes(self,succ,trials):return (succ+self.a)/(trials+self.a+self.b)
 def _window(self,neg,w):
  if len(neg)<w:return {}
  x=np.asarray(neg[-w:],dtype=float);frac=float(np.mean(x));return {"frac":frac,"entropy":self._entropy(frac)}
 def _markov(self,neg,w):
  # Source authority computes rolling sums over w transition rows:
  # previous_negative[t] -> negative[t]. Therefore the current bar needs
  # w+1 sign observations, not w signs / w-1 transitions.
  if len(neg)<w+1:return {}
  x=np.asarray(neg[-(w+1):],dtype=float)
  prev=x[:-1];cur=x[1:];prevneg=prev;prevpos=1-prev
  nprevneg=float(np.sum(prevneg));nprevpos=float(np.sum(prevpos));nn=float(np.sum(prevneg*cur));np_=float(np.sum(prevpos*cur))
  pnn=self._bayes(nn,nprevneg);pnp=self._bayes(np_,nprevpos);uncond=float(np.mean(cur));adv=pnn-uncond
  weight=nprevneg/(nprevneg+nprevpos+self.eps);cond=weight*self._entropy(pnn)+(1-weight)*self._entropy(pnp)
  return {"pnn":pnn,"pnp":pnp,"adv":adv,"cond":cond}
 @staticmethod
 def _autocorr(vals,lag):
  x=np.asarray(vals,dtype=float)
  if len(x)<=lag:return math.nan
  a=x[:-lag];b=x[lag:]
  if np.std(a)<=1e-12 or np.std(b)<=1e-12:return 0.0
  return float(np.corrcoef(a,b)[0,1])
 @staticmethod
 def _current_run(vals):
  n=0
  for x in vals[::-1]:
   if x==1:n+=1
   else:break
  return float(n)
 @staticmethod
 def _max_run(vals):
  best=cur=0
  for x in vals:
   if x==1:cur+=1;best=max(best,cur)
   else:cur=0
  return float(best)
 def _eventvol(self,c):
  s=self._state;ret=float(np.log((c+self.eps)/(s.close_hist[-self.evlook]+self.eps))) if len(s.close_hist)>=self.evlook else math.nan
  var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close")
  prev=s.close_hist[-1] if s.close_hist else math.nan;ret=float(np.log((c+self.eps)/(prev+self.eps))) if np.isfinite(prev) else math.nan;neg=1.0 if np.isfinite(ret) and ret<0 else 0.0;current=s.neg_hist+[neg]
  w10=self._window(current,self.w10);w20=self._window(current,self.w20);w50=self._window(current,self.w50);m20=self._markov(current,self.w20);m50=self._markov(current,self.w50)
  # Source authority rolls 50 rows of:
  # negative.shift(1) * negative.shift(2) -> current negative.
  # Exact current-bar evaluation therefore needs 52 sign observations.
  p2=math.nan
  if len(current)>=self.w50+2:
   x=np.asarray(current[-(self.w50+2):],float);prev2=x[:-2]*x[1:-1];succ=float(np.sum(prev2*x[2:]));trials=float(np.sum(prev2));p2=self._bayes(succ,trials)
  entropy20=w20.get("entropy",math.nan)
  baseline=[x for x in s.entropy20_hist[-self.base:] if np.isfinite(x)]
  if len(baseline)>=self.base and np.isfinite(entropy20):
   med=float(np.median(baseline));mean=float(np.mean(baseline));std=float(pd.Series(baseline).std());ratio=entropy20/(med+self.eps);z=(entropy20-mean)/(std+self.eps);pct=float(np.mean(np.asarray(baseline)<=entropy20))
  else:med=mean=std=ratio=z=pct=math.nan
  signed=[1-2*x for x in current]
  run=self._current_run(current[-50:]);maxrun=self._max_run(current[-20:]) if len(current)>=20 else math.nan
  ac120=self._autocorr(signed[-20:],1) if len(signed)>=20 else math.nan;ac150=self._autocorr(signed[-50:],1) if len(signed)>=50 else math.nan;ac250=self._autocorr(signed[-50:],2) if len(signed)>=50 else math.nan
  ev=self._eventvol(c);lowent=bool(np.isfinite(pct) and pct<=self.entthr);negdom=bool(w20 and w20["frac"]>=self.negthr);pers=bool(m50 and m50["pnn"]>=self.pthr);advok=bool(m50 and m50["adv"]>=self.advthr);active=int(lowent and negdom and pers and advok)
  if active and not s.previous_active:s.emitted_in_episode=False
  if not active:s.emitted_in_episode=False
  em=None
  if active and neg==1 and s.processed_rows-s.last_event_index>=self.gap:
   entry=int(not s.emitted_in_episode);s.emitted_in_episode=True;s.last_event_index=s.processed_rows
   if np.isfinite(ev):
    rng=max(h-l,self.eps)
    em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,
    "Event_Vol":ev,"Sign_Entropy_Collapse_Event":1,"Entropy_Active_State":active,"Entropy_State_Entry":entry,"Entropy_Renewed_Impulse":1-entry,"Log_Return_1":ret,"Negative_Event_Bar":1,
    "Negative_Sign_Fraction_10":w10.get("frac",math.nan),"Negative_Sign_Fraction_20":w20.get("frac",math.nan),"Negative_Sign_Fraction_50":w50.get("frac",math.nan),
    "Sign_Entropy_10":w10.get("entropy",math.nan),"Sign_Entropy_20":entropy20,"Sign_Entropy_50":w50.get("entropy",math.nan),
    "Entropy_Percentile_20_Over_500":pct,"Entropy_Ratio_20_To_Median_500":ratio,"Entropy_ZScore_20_Over_500":z,
    "P_Neg_After_Neg_20":m20.get("pnn",math.nan),"P_Neg_After_Neg_50":m50.get("pnn",math.nan),"P_Neg_After_Pos_20":m20.get("pnp",math.nan),"P_Neg_After_Pos_50":m50.get("pnp",math.nan),"P_Neg_After_Two_Neg_50":p2,
    "Downside_Transition_Advantage_20":m20.get("adv",math.nan),"Downside_Transition_Advantage_50":m50.get("adv",math.nan),"Conditional_Sign_Entropy_20":m20.get("cond",math.nan),"Conditional_Sign_Entropy_50":m50.get("cond",math.nan),
    "Current_Negative_Run":run,"Maximum_Negative_Run_20":maxrun,"Sign_Autocorrelation_Lag1_20":ac120,"Sign_Autocorrelation_Lag1_50":ac150,"Sign_Autocorrelation_Lag2_50":ac250,
    "Body_Direction":float(np.sign(c-o)),"Body_Share":abs(c-o)/(rng+self.eps),"Close_Location":(c-l)/(rng+self.eps)}
    for k in OPTIONAL:
     if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
    em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|entropy_impulse","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False})
  s.close_hist.append(c);s.neg_hist.append(neg);s.ret_hist.append(ret);s.entropy20_hist.append(entropy20)
  for arr,lim in [(s.close_hist,self.evlook),(s.neg_hist,max(self.base+self.w20,self.w50)+5),(s.ret_hist,50),(s.entropy20_hist,self.base)]:
   if len(arr)>lim:del arr[:len(arr)-lim]
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
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"sign_history_rows":len(self._state.neg_hist),"entropy_history_rows":len(self._state.entropy20_hist),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return SignEntropyCollapseShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),sign_window_short=int(_c(c,"SIGN_WINDOW_SHORT",10)),sign_window_primary=int(_c(c,"SIGN_WINDOW_PRIMARY",20)),sign_window_transition=int(_c(c,"SIGN_WINDOW_TRANSITION",50)),entropy_baseline_window=int(_c(c,"ENTROPY_BASELINE_WINDOW",500)),negative_fraction_threshold=float(_c(c,"NEGATIVE_FRACTION_THRESHOLD",.5)),neg_after_neg_threshold=float(_c(c,"NEG_AFTER_NEG_THRESHOLD",.52)),transition_advantage_threshold=float(_c(c,"TRANSITION_ADVANTAGE_THRESHOLD",0)),entropy_percentile_threshold=float(_c(c,"ENTROPY_PERCENTILE_THRESHOLD",.65)),min_event_gap_bars=int(_c(c,"MIN_EVENT_GAP_BARS",3)),beta_prior_alpha=float(_c(c,"BETA_PRIOR_ALPHA",1)),beta_prior_beta=float(_c(c,"BETA_PRIOR_BETA",1)),event_vol_lookback=int(_c(c,"EVENT_VOL_LOOKBACK",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
