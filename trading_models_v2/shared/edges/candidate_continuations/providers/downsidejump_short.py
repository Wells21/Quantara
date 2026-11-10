from __future__ import annotations
"""Exact incremental downsidejumpcontinuation_short provider."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
DOWNSIDEJUMP_SHORT_SIGNATURE="43c646985c34b739e3f1d26b2c65c9fce9ff94ea2b824e75fc31277af042c9bd"
PROVIDER_SPEC={"provider_id":"phase_d4_downsidejump_short_candidate","provider_version":"1","supported_signatures":{DOWNSIDEJUMP_SHORT_SIGNATURE:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);ret_hist:list[float]=field(default_factory=list);bipower_hist:list[float]=field(default_factory=list);high_hist:list[float]=field(default_factory=list);low_hist:list[float]=field(default_factory=list);atr_hist:list[float]=field(default_factory=list);cross_hist:list[int]=field(default_factory=list);prev_jump_state:int=0;last_cross_index:int=-(10**9);last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class DownsideJumpShortCandidateContinuation:
 def __init__(self,*,identity,eps,bipower_variation_window_bars,jump_score_threshold,min_range_atr_context,max_close_location_context,semivariance_window_bars,recent_jump_window_bars,structural_range_window_bars,min_gap_bars,event_vol_lookback_bars,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.bw=int(bipower_variation_window_bars);self.threshold=float(jump_score_threshold);self.minrange=float(min_range_atr_context);self.maxloc=float(max_close_location_context);self.semi=int(semivariance_window_bars);self.recent=int(recent_jump_window_bars);self.struct=int(structural_range_window_bars);self.gap=int(min_gap_bars);self.evlook=int(event_vol_lookback_bars);self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self._state=self._new()
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
  o=o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True);return o[(o.Open>0)&(o.High>0)&(o.Low>0)&(o.Close>0)&(o.ATR_14>0)].copy()
 def _eventvol(self,c):
  s=self._state;ret=float(np.log((c+self.eps)/(s.close_hist[-self.evlook]+self.eps))) if len(s.close_hist)>=self.evlook else math.nan
  var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close");atr=self._num(r["ATR_14"],"ATR_14")
  prev_close=s.close_hist[-1] if s.close_hist else math.nan;ret=float(np.log((c+self.eps)/(prev_close+self.eps))) if np.isfinite(prev_close) else math.nan;absjump=abs(ret) if np.isfinite(ret) else math.nan
  prev_ret=s.ret_hist[-1] if s.ret_hist else math.nan;bp=abs(ret)*abs(prev_ret) if np.isfinite(ret) and np.isfinite(prev_ret) else math.nan
  bvals=[x for x in s.bipower_hist[-self.bw:] if np.isfinite(x)]
  bpv=(math.pi/2)*float(np.mean(bvals)) if len(bvals)>=self.bw else math.nan;rob=math.sqrt(max(bpv,self.eps)) if np.isfinite(bpv) else math.nan;jump=ret/(rob+self.eps) if np.isfinite(ret) and np.isfinite(rob) else math.nan
  state=int(np.isfinite(jump) and jump<=self.threshold);prevstate=s.prev_jump_state;cross=int(state==1 and prevstate==0)
  shifted_atr=s.atr_hist[-1] if s.atr_hist else math.nan;rng=max(h-l,self.eps);rangeatr=rng/(shifted_atr+self.eps) if np.isfinite(shifted_atr) else math.nan;loc=(c-l)/(rng+self.eps)
  prev20=s.ret_hist[-self.semi:];neg=sum(min(x,0.0)**2 for x in prev20 if np.isfinite(x)) if len(prev20)>=self.semi else math.nan;pos=sum(max(x,0.0)**2 for x in prev20 if np.isfinite(x)) if len(prev20)>=self.semi else math.nan;bad=neg/(pos+self.eps) if np.isfinite(neg) else math.nan
  recent_count=float(sum(s.cross_hist[-self.recent:])) if s.cross_hist else 0.0
  bars_since=float(s.processed_rows-s.last_cross_index-1) if s.last_cross_index>-(10**8) else math.nan
  prior_low=min(s.low_hist[-self.struct:]) if len(s.low_hist)>=self.struct else math.nan;prior_high=max(s.high_hist[-self.struct:]) if len(s.high_hist)>=self.struct else math.nan
  structloc=(c-prior_low)/(prior_high-prior_low+self.eps) if np.isfinite(prior_low) and np.isfinite(prior_high) else math.nan
  ev=self._eventvol(c);em=None
  if cross and s.processed_rows-s.last_event_index>=self.gap:
   s.last_event_index=s.processed_rows
   if all(np.isfinite(x) for x in [ev,ret,bpv,rob,jump]):
    # match source keep-order: OHLC, then EVENT_CONTEXT_COLUMNS, then metadata.
    em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,
    "Event_Vol":ev,"Log_Return_1":ret,"Absolute_Jump_Size":absjump,"Bipower_Product":bp,"Bipower_Variation_50":bpv,"Robust_Continuous_Volatility_50":rob,"Jump_Score":jump,
    "Downside_Jump_State":state,"Previous_Downside_Jump_State":prevstate,"First_Downside_Jump_Cross":cross,"Downside_Jump_Event":1,"Shifted_ATR_14":shifted_atr,
    "Range_ATR":rangeatr,"Meaningful_Range_Context":int(np.isfinite(rangeatr) and rangeatr>=self.minrange),"Body_Direction":float(np.sign(c-o)),"Body_Share":abs(c-o)/(rng+self.eps),"Close_Location":loc,"Close_Near_Low_Context":int(loc<=self.maxloc),
    "Downside_Semivariance_20":neg,"Upside_Semivariance_20":pos,"Bad_Volatility_Ratio":bad,"Recent_Downside_Jump_Count":recent_count,"Bars_Since_Previous_Downside_Jump":bars_since,"Pre_Jump_Volatility":rob,
    "Prior_Structural_Low_50":prior_low,"Prior_Structural_High_50":prior_high,"Structural_Range_Location":structloc}
    for k in OPTIONAL:
     if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
    em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|downside_jump_cross","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False,
    "bipower_variation_window_bars":self.bw,"jump_score_threshold":self.threshold,"minimum_range_atr_context":self.minrange,"maximum_close_location_context":self.maxloc,"semivariance_window_bars":self.semi,"recent_jump_window_bars":self.recent,"structural_range_window_bars":self.struct,"min_gap_bars":self.gap,"event_vol_lookback_bars":self.evlook,"event_vol_ewm_span":self.evspan})
  s.close_hist.append(c);s.ret_hist.append(ret);s.bipower_hist.append(bp);s.high_hist.append(h);s.low_hist.append(l);s.atr_hist.append(atr);s.cross_hist.append(cross)
  for arr,lim in [(s.close_hist,self.evlook),(s.ret_hist,max(self.semi,2)),(s.bipower_hist,self.bw),(s.high_hist,self.struct),(s.low_hist,self.struct),(s.atr_hist,2),(s.cross_hist,self.recent)]:
   if len(arr)>lim:del arr[:len(arr)-lim]
  if cross:s.last_cross_index=s.processed_rows
  s.prev_jump_state=state;s.processed_rows+=1;s.last_timestamp=ts;return em
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
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return DownsideJumpShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),bipower_variation_window_bars=int(_c(c,"BIPOWER_VARIATION_WINDOW_BARS",50)),jump_score_threshold=float(_c(c,"JUMP_SCORE_THRESHOLD",-1.75)),min_range_atr_context=float(_c(c,"MIN_RANGE_ATR_CONTEXT",1.2)),max_close_location_context=float(_c(c,"MAX_CLOSE_LOCATION_CONTEXT",.4)),semivariance_window_bars=int(_c(c,"SEMIVARIANCE_WINDOW_BARS",20)),recent_jump_window_bars=int(_c(c,"RECENT_JUMP_WINDOW_BARS",50)),structural_range_window_bars=int(_c(c,"STRUCTURAL_RANGE_WINDOW_BARS",50)),min_gap_bars=int(_c(c,"MIN_GAP_BARS",1)),event_vol_lookback_bars=int(_c(c,"EVENT_VOL_LOOKBACK_BARS",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
