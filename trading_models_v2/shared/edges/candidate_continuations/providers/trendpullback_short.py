from __future__ import annotations
"""Exact incremental trendpullbackcontinuation_short candidate continuation."""
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional,Union
import numpy as np,pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
TRENDPULLBACK_SHORT_SIGNATURE="2dd24363edc120f97fab8102eeb1dbe93cad2d47aa9b14aa8dc4673f29d59c6d"
PROVIDER_SPEC={"provider_id":"phase_d4_trendpullback_short_candidate","provider_version":"1","supported_signatures":{TRENDPULLBACK_SHORT_SIGNATURE:{"side_specialization":"short"}}}
@dataclass
class _State:
 ewm_var:dict[str,Any]
 close_history:list[float]=field(default_factory=list)
 high_history:list[float]=field(default_factory=list)
 low_history:list[float]=field(default_factory=list)
 tr_history:list[float]=field(default_factory=list)
 processed_rows:int=0
 last_event_index:int=-(10**9)
 last_timestamp:Optional[pd.Timestamp]=None
 last_close:float=math.nan
class TrendPullbackShortCandidateContinuation:
 def __init__(self,*,identity,eps,trend_lookback_bars,fast_trend_ma_bars,slow_trend_ma_bars,pullback_lookback_bars,event_vol_lookback_bars,event_vol_ewm_span,atr_lookback_bars,min_event_vol,min_pullback_atr,max_pullback_atr,max_rejection_close_location,min_bearish_return_atr,max_chase_distance_atr,min_gap_bars):
  self.identity=identity;self.eps=float(eps);self.trend_lookback_bars=int(trend_lookback_bars);self.fast_trend_ma_bars=int(fast_trend_ma_bars);self.slow_trend_ma_bars=int(slow_trend_ma_bars);self.pullback_lookback_bars=int(pullback_lookback_bars);self.event_vol_lookback_bars=int(event_vol_lookback_bars);self.event_vol_ewm_span=int(event_vol_ewm_span);self.atr_lookback_bars=int(atr_lookback_bars);self.min_event_vol=float(min_event_vol);self.min_pullback_atr=float(min_pullback_atr);self.max_pullback_atr=float(max_pullback_atr);self.max_rejection_close_location=float(max_rejection_close_location);self.min_bearish_return_atr=float(min_bearish_return_atr);self.max_chase_distance_atr=float(max_chase_distance_atr);self.min_gap_bars=int(min_gap_bars);self._state=self._new_state()
 def _new_state(self):return _State(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.event_vol_ewm_span))
 def reset(self):self._state=self._new_state()
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
 @staticmethod
 def _slope(values):
  y=np.asarray(values,dtype="float64")
  if len(y)<3 or not np.isfinite(y).all():return math.nan
  x=np.arange(len(y),dtype="float64");x=x-x.mean();y=y-y.mean();den=float(np.sum(x*x));return math.nan if den<=1e-12 else float(np.sum(x*y)/den)
 def _normalize(self,f):
  req=["Datetime","Bar_Start","Open","High","Low","Close"];miss=[c for c in req if c not in f]
  if miss:raise CandidateContinuationError(f"missing {miss}")
  o=f.copy();o["Datetime"]=to_datetime_iso(o["Datetime"],utc=True,errors="coerce");o["Bar_Start"]=to_datetime_iso(o["Bar_Start"],utc=True,errors="coerce")
  for c in ["Open","High","Low","Close","ATR_14"]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  return o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
 def _event_vol(self,close):
  h=self._state.close_history
  ret=float(np.log((close+self.eps)/(h[-self.event_vol_lookback_bars]+self.eps))) if len(h)>=self.event_vol_lookback_bars else math.nan
  var=float(_ewm_var_update(self._state.ewm_var,ret));ev=max(math.sqrt(var),self.min_event_vol) if np.isfinite(var) and var>=0 else math.nan
  return ev
 def _advance(self,row,enforce):
  s=self._state;ts=self._ts(row["Datetime"]);bs=self._ts(row["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing timestamp")
  open_=self._num(row["Open"],"Open");high=self._num(row["High"],"High");low=self._num(row["Low"],"Low");close=self._num(row["Close"],"Close")
  prior_close=s.last_close
  # source ATR_14 is used directly when supplied; otherwise rolling true-range mean.
  if "ATR_14" in row and pd.notna(row["ATR_14"]):atr=max(self._num(row["ATR_14"],"ATR_14"),self.eps)
  else:
   tr=max(high-low,abs(high-prior_close) if np.isfinite(prior_close) else high-low,abs(low-prior_close) if np.isfinite(prior_close) else high-low)
   s.tr_history.append(tr)
   if len(s.tr_history)>self.atr_lookback_bars:del s.tr_history[:len(s.tr_history)-self.atr_lookback_bars]
   atr=float(np.mean(s.tr_history)) if len(s.tr_history)>=self.atr_lookback_bars else math.nan
  ev=self._event_vol(close)
  prior_closes=s.close_history
  fast=float(np.mean(prior_closes[-self.fast_trend_ma_bars:])) if len(prior_closes)>=self.fast_trend_ma_bars else math.nan
  slow=float(np.mean(prior_closes[-self.slow_trend_ma_bars:])) if len(prior_closes)>=self.slow_trend_ma_bars else math.nan
  logs=[math.log(x+self.eps) for x in prior_closes[-self.trend_lookback_bars:]]
  slope=self._slope(logs) if len(logs)>=self.trend_lookback_bars else math.nan
  ph=max(s.high_history[-self.pullback_lookback_bars:]) if s.high_history else math.nan
  pl=min(s.low_history[-self.pullback_lookback_bars:]) if s.low_history else math.nan
  # np.nanargmax returns earliest maximum; preserve its global bar index.
  if s.high_history:
   win=s.high_history[-self.pullback_lookback_bars:];local=int(np.nanargmax(np.asarray(win,dtype=float)));start=s.processed_rows-len(win);ph_idx=start+local
  else:ph_idx=-1
  emitted=None
  vals=[atr,ev,fast,slow,slope,prior_close,ph,pl]
  if all(np.isfinite(x) for x in vals) and atr>self.eps and ev>0:
   bearish=fast<=slow and prior_close<=slow and slope<=0
   dist=(ph-pl)/(atr+self.eps);bars_since=s.processed_rows-ph_idx
   rng=max(high-low,self.eps);loc=(close-low)/(rng+self.eps)
   down=(prior_close-close)/(atr+self.eps);chase=(ph-close)/(atr+self.eps)
   below=close<=fast;bear_candle=(close-open_)<0;gap=s.processed_rows-s.last_event_index>=self.min_gap_bars
   if bearish and self.min_pullback_atr<=dist<=self.max_pullback_atr and loc<=self.max_rejection_close_location and down>=self.min_bearish_return_atr and 0<=chase<=self.max_chase_distance_atr and below and bear_candle and gap:
    emitted={"Datetime":ts,"Bar_Start":bs,"edge_id":self.identity.edge_id,"side":-1,"Event_Vol":float(ev),
    "event_id":f"{self.identity.edge_id}|{ts.isoformat()}|trend_pullback_continuation","candidate_event_generator":self.identity.candidate_event_generator,
    "trend_direction":"downside","pullback_direction":"upward","failure_direction":"downside","event_known_after_close":True,"uses_future_information":False,
    "trend_lookback_bars":self.trend_lookback_bars,"fast_trend_ma_bars":self.fast_trend_ma_bars,"slow_trend_ma_bars":self.slow_trend_ma_bars,"pullback_lookback_bars":self.pullback_lookback_bars,
    "trend_ma_fast":fast,"trend_ma_slow":slow,"trend_slope":slope,"trend_distance_atr":(slow-close)/(atr+self.eps),
    "pullback_high":ph,"pullback_low":pl,"pullback_distance_atr":dist,"pullback_high_index":ph_idx,"reentry_bar_index":s.processed_rows,
    "bars_since_pullback_high":bars_since,"reentry_close":close,"reentry_open":open_,"reentry_high":high,"reentry_low":low,
    "rejection_close_location":loc,"bearish_reentry_return_atr":down,"chase_distance_atr":chase,"close_below_fast_ma":int(below),"bearish_candle":int(bear_candle),"trend_pullback_sequence_ok":1,"Close":close}
    s.last_event_index=s.processed_rows
  s.close_history.append(close)
  keep=max(self.slow_trend_ma_bars,self.trend_lookback_bars,self.event_vol_lookback_bars)
  if len(s.close_history)>keep:del s.close_history[:len(s.close_history)-keep]
  s.high_history.append(high);s.low_history.append(low)
  if len(s.high_history)>self.pullback_lookback_bars:del s.high_history[:len(s.high_history)-self.pullback_lookback_bars]
  if len(s.low_history)>self.pullback_lookback_bars:del s.low_history[:len(s.low_history)-self.pullback_lookback_bars]
  s.last_close=close;s.last_timestamp=ts;s.processed_rows+=1;return emitted
 def bootstrap(self,f):
  x=self._normalize(f);self.reset()
  for r in x.to_dict("records"):self._advance(r,False)
  return self.state_summary()
 def update_one(self,row):return self._advance(row.to_dict() if isinstance(row,pd.Series) else dict(row),True)
 def replay(self,f):
  x=self._normalize(f);self.reset();rows=[]
  for r in x.to_dict("records"):
   ev=self._advance(r,False)
   if ev is not None:rows.append(ev)
  return pd.DataFrame(rows)
 def state_summary(self):
  s=self._state;return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":s.processed_rows,
  "close_history_rows":len(s.close_history),"high_history_rows":len(s.high_history),"low_history_rows":len(s.low_history),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported signature")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return TrendPullbackShortCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),trend_lookback_bars=int(_c(c,"TREND_LOOKBACK_BARS",50)),fast_trend_ma_bars=int(_c(c,"FAST_TREND_MA_BARS",20)),slow_trend_ma_bars=int(_c(c,"SLOW_TREND_MA_BARS",50)),pullback_lookback_bars=int(_c(c,"PULLBACK_LOOKBACK_BARS",8)),event_vol_lookback_bars=int(_c(c,"EVENT_VOL_LOOKBACK_BARS",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),atr_lookback_bars=int(_c(c,"ATR_LOOKBACK_BARS",14)),min_event_vol=float(_c(c,"MIN_EVENT_VOL",1e-8)),min_pullback_atr=float(_c(c,"MIN_PULLBACK_ATR",.15)),max_pullback_atr=float(_c(c,"MAX_PULLBACK_ATR",3)),max_rejection_close_location=float(_c(c,"MAX_REJECTION_CLOSE_LOCATION",.55)),min_bearish_return_atr=float(_c(c,"MIN_BEARISH_RETURN_ATR",0)),max_chase_distance_atr=float(_c(c,"MAX_CHASE_DISTANCE_ATR",2.5)),min_gap_bars=int(_c(c,"MIN_GAP_BARS",1)))
