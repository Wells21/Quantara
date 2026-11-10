from __future__ import annotations
"""Exact incremental compressionbreakoutcontinuation_short provider."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
COMPRESSION_BREAKOUT_SHORT_SIGNATURE="30a55563dcc69c33655643cbe13944ddfc98ce4a00bf948180594f447d705444"
PROVIDER_SPEC={"provider_id":"phase_d4_compressionbreakout_short_candidate","provider_version":"1","supported_signatures":{COMPRESSION_BREAKOUT_SHORT_SIGNATURE:{"side_specialization":"short"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);high_hist:list[float]=field(default_factory=list);low_hist:list[float]=field(default_factory=list);atr_hist:list[float]=field(default_factory=list);range_width_hist:list[float]=field(default_factory=list);compression_ratio_hist:list[float]=field(default_factory=list);last_compression_index:int=-(10**9);last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None;prev_support:float=math.nan;prev_close:float=math.nan
class CompressionBreakoutShortCandidateContinuation:
 def __init__(self,*,identity,eps,compression_window_bars,compression_baseline_window_bars,compression_threshold_lookback_bars,compression_quantile,compression_recency_bars,structural_support_lookback_bars,breakdown_buffer_atr,max_breakdown_close_location,min_gap_bars,event_vol_lookback_bars,event_vol_ewm_span,min_event_vol):
  self.identity=identity;self.eps=float(eps);self.cw=int(compression_window_bars);self.bw=int(compression_baseline_window_bars);self.tw=int(compression_threshold_lookback_bars);self.q=float(compression_quantile);self.rec=int(compression_recency_bars);self.sw=int(structural_support_lookback_bars);self.buffer=float(breakdown_buffer_atr);self.maxloc=float(max_breakdown_close_location);self.gap=int(min_gap_bars);self.evlook=int(event_vol_lookback_bars);self.evspan=int(event_vol_ewm_span);self.minvol=float(min_event_vol);self._state=self._new()
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
  if pd.isna(x) or not np.isfinite(float(x)) or float(x)<=0 and n in {"Open","High","Low","Close","ATR_14"}:raise CandidateContinuationError(f"invalid {n}")
  return float(x)
 def _norm(self,f):
  req=["Datetime","Bar_Start","Open","High","Low","Close","ATR_14"];miss=[c for c in req if c not in f]
  if miss:raise CandidateContinuationError(f"missing {miss}")
  o=f.copy();o["Datetime"]=to_datetime_iso(o["Datetime"],utc=True,errors="coerce");o["Bar_Start"]=to_datetime_iso(o["Bar_Start"],utc=True,errors="coerce")
  for c in ["Open","High","Low","Close","ATR_14",*OPTIONAL]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  return o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
 def _eventvol(self,close):
  s=self._state;ret=float(np.log((close+self.eps)/(s.close_hist[-self.evlook]+self.eps))) if len(s.close_hist)>=self.evlook else math.nan
  var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close");atr=self._num(r["ATR_14"],"ATR_14")
  prior_high=max(s.high_hist[-self.cw:]) if len(s.high_hist)>=self.cw else math.nan;prior_low=min(s.low_hist[-self.cw:]) if len(s.low_hist)>=self.cw else math.nan
  width=max(prior_high-prior_low,self.eps) if np.isfinite(prior_high) and np.isfinite(prior_low) else math.nan
  finite=[x for x in s.range_width_hist[-self.bw:] if np.isfinite(x)]
  baseline=float(pd.Series(finite).median()) if len(finite)>=self.bw else math.nan
  ratio=width/(baseline+self.eps) if np.isfinite(width) and np.isfinite(baseline) else math.nan
  vals=[x for x in s.compression_ratio_hist[-self.tw:] if np.isfinite(x)]
  threshold=float(pd.Series(vals).quantile(self.q)) if len(vals)>=self.tw else math.nan
  compressed=int(np.isfinite(ratio) and np.isfinite(threshold) and ratio<=threshold)
  if compressed:s.last_compression_index=s.processed_rows
  bars_since=float(s.processed_rows-s.last_compression_index) if s.last_compression_index>-(10**8) else math.nan
  recent=int(np.isfinite(bars_since) and 0<=bars_since<=self.rec-1)
  support=min(s.low_hist[-self.sw:]) if len(s.low_hist)>=self.sw else math.nan
  shifted_atr=s.atr_hist[-1] if s.atr_hist else math.nan
  prev_support=s.prev_support;prev_close=s.prev_close
  level=support-self.buffer*shifted_atr if np.isfinite(support) and np.isfinite(shifted_atr) else math.nan
  downside=int(np.isfinite(support) and c<support);prior=int(np.isfinite(prev_support) and np.isfinite(prev_close) and prev_close<prev_support);first=int(downside==1 and prior==0);buffered=int(np.isfinite(level) and c<level)
  rng=max(h-l,self.eps);body=c-o;loc=(c-l)/(rng+self.eps);depth=(support-c)/(shifted_atr+self.eps) if np.isfinite(support) and np.isfinite(shifted_atr) else math.nan
  retatr=(prev_close-c)/(shifted_atr+self.eps) if np.isfinite(prev_close) and np.isfinite(shifted_atr) else math.nan;rangeatr=rng/(shifted_atr+self.eps) if np.isfinite(shifted_atr) else math.nan
  ev=self._eventvol(c);flag=int(recent==1 and first==1);em=None
  if flag and s.processed_rows-s.last_event_index>=self.gap:
   s.last_event_index=s.processed_rows
   if all(np.isfinite(x) for x in [ev,support,depth,loc]):
    em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,"Event_Vol":ev,"Prior_Range_High":prior_high,"Prior_Range_Low":prior_low,"Prior_Range_Width":width,"Range_Width_Baseline":baseline,"Compression_Ratio":ratio,"Compression_Threshold":threshold,"Was_Compressed":compressed,"Compression_Seen_Recently":recent,"Bars_Since_Compression":bars_since,"Structural_Support":support,"Previous_Structural_Support":prev_support,"Shifted_ATR_14":shifted_atr,"Breakdown_Level":level,"Previous_Close":prev_close,"Downside_Structural_Break":downside,"Prior_Downside_Break":prior,"First_Downside_Cross":first,"Buffered_Downside_Break":buffered,"Breakdown_Depth_ATR":depth,"Breakdown_Return_ATR":retatr,"Breakdown_Range_ATR":rangeatr,"Body_Direction":float(np.sign(body)),"Body_Share":abs(body)/(rng+self.eps),"Close_Location":loc,"Compression_Breakout_Event":1,"Breakout_Direction":-1}
    for k in OPTIONAL:
     if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
    em.update({"edge_id":self.identity.edge_id,"side":0,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|compression_downside_break","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False,
    "compression_window_bars":self.cw,"compression_baseline_window_bars":self.bw,"compression_threshold_lookback_bars":self.tw,"compression_quantile":self.q,"compression_recency_bars":self.rec,"structural_support_lookback_bars":self.sw,"breakdown_buffer_atr":self.buffer,"max_breakdown_close_location":self.maxloc,"min_gap_bars":self.gap,"event_vol_lookback_bars":self.evlook,"event_vol_ewm_span":self.evspan})
  s.high_hist.append(h);s.low_hist.append(l);s.atr_hist.append(atr);s.close_hist.append(c)
  for arr,lim in [(s.high_hist,max(self.cw,self.sw)),(s.low_hist,max(self.cw,self.sw)),(s.atr_hist,2),(s.close_hist,self.evlook)]:
   if len(arr)>lim:del arr[:len(arr)-lim]
  s.range_width_hist.append(width)
  if len(s.range_width_hist)>self.bw:del s.range_width_hist[:len(s.range_width_hist)-self.bw]
  s.compression_ratio_hist.append(ratio)
  if len(s.compression_ratio_hist)>self.tw:del s.compression_ratio_hist[:len(s.compression_ratio_hist)-self.tw]
  s.prev_support=support;s.prev_close=c;s.last_timestamp=ts;s.processed_rows+=1
  return em
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
  s=self._state;return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":s.processed_rows,"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported signature")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return CompressionBreakoutShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),compression_window_bars=int(_c(c,"COMPRESSION_WINDOW_BARS",20)),compression_baseline_window_bars=int(_c(c,"COMPRESSION_BASELINE_WINDOW_BARS",100)),compression_threshold_lookback_bars=int(_c(c,"COMPRESSION_THRESHOLD_LOOKBACK_BARS",200)),compression_quantile=float(_c(c,"COMPRESSION_QUANTILE",.5)),compression_recency_bars=int(_c(c,"COMPRESSION_RECENCY_BARS",50)),structural_support_lookback_bars=int(_c(c,"STRUCTURAL_SUPPORT_LOOKBACK_BARS",20)),breakdown_buffer_atr=float(_c(c,"BREAKDOWN_BUFFER_ATR",.05)),max_breakdown_close_location=float(_c(c,"MAX_BREAKDOWN_CLOSE_LOCATION",.4)),min_gap_bars=int(_c(c,"MIN_GAP_BARS",2)),event_vol_lookback_bars=int(_c(c,"EVENT_VOL_LOOKBACK_BARS",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)))
