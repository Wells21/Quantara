from __future__ import annotations
"""Exact incremental dailyrangecompletionfade_long provider."""
from collections import defaultdict,deque
from dataclasses import dataclass,field
import math
from typing import Any,Mapping,Optional
import numpy as np,pandas as pd
import bisect
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity
# 2026-09-25: regenerated after the calendar-time relabel (EVENT_VOL_LOOKBACK -> EVENT_VOL_LOOKBACK_HOURS).
# XAUUSD/dailyrangecompletionfade_long is retired: it failed Step04's
# no-positive-executable-threshold-region check after the relabel and is
# not in the current live manifest.
# 2026-10-18: v5.2 re-selection changed only the D1 constant EVENT_VOL_LOOKBACK_HOURS (7.462 -> 6.0); logic identical,
# constants are read from D1, so the current signature is added (pre-v5.2 signature kept as legacy).
DAILY_RANGE_FADE_LONG_SIGNATURE_GBPJPY_PRE_V52="b9742d55ff1bef7f43dca872a98db5ca01e589c41e63f12332d5e7592acda714"
DAILY_RANGE_FADE_LONG_SIGNATURE_GBPJPY="ce9242f633ccf9847950771b508734250ed1de1616f5115e86aeb1486e0284dc"
DAILY_RANGE_FADE_LONG_SIGNATURE=DAILY_RANGE_FADE_LONG_SIGNATURE_GBPJPY
PROVIDER_SPEC={"provider_id":"phase_d4_dailyrangecompletionfade_long_candidate","provider_version":"1","supported_signatures":{DAILY_RANGE_FADE_LONG_SIGNATURE_GBPJPY:{"side_specialization":"long"},DAILY_RANGE_FADE_LONG_SIGNATURE_GBPJPY_PRE_V52:{"side_specialization":"long"}}}
OPTIONAL=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")
@dataclass
class _S:
 ewm_var:dict[str,Any];close_hist:list[float]=field(default_factory=list);close_hist_ns:list[int]=field(default_factory=list);completed_ranges:deque=field(default_factory=deque);current_date:Optional[str]=None;day_open:float=math.nan;day_high:float=-math.inf;day_low:float=math.inf;bars_into_day:int=0;maturity_queue:deque=field(default_factory=deque);bias_history:dict[str,deque]=field(default_factory=dict);last_event_index:int=-(10**9);processed_rows:int=0;last_timestamp:Optional[pd.Timestamp]=None
class DailyRangeFadeLongCandidateContinuation:
 def __init__(self,*,identity,eps,expected_daily_range_lookback,min_expected_range_days,fade_forward_horizon,fade_lookback,min_fade_obs,min_day_progress_bars,min_daily_range_completion,min_downside_extension_atr,max_day_position,min_gap_bars,event_vol_lookback_hours,event_vol_ewm_span,min_vol):
  self.identity=identity;self.eps=float(eps);self.look=int(expected_daily_range_lookback);self.mindays=int(min_expected_range_days);self.horizon=int(fade_forward_horizon);self.fadelook=int(fade_lookback);self.minobs=int(min_fade_obs);self.minprogress=int(min_day_progress_bars);self.mincompletion=float(min_daily_range_completion);self.minext=float(min_downside_extension_atr);self.maxpos=float(max_day_position);self.gap=int(min_gap_bars);self.evlook_hours=float(event_vol_lookback_hours);self.evlook_ns=int(round(self.evlook_hours*3600.0*1e9));self.evspan=int(event_vol_ewm_span);self.minvol=float(min_vol);self._state=self._new()
 def _new(self):
  return _S(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.evspan),completed_ranges=deque(maxlen=self.look),maturity_queue=deque(maxlen=self.horizon),bias_history={})
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
 @staticmethod
 def _bp(x):
  if not np.isfinite(x):return "progress_unknown"
  if x<40:return "progress_early"
  if x<120:return "progress_mid"
  return "progress_late"
 @staticmethod
 def _bc(x):
  if not np.isfinite(x):return "completion_unknown"
  if x<.75:return "completion_low"
  if x<1.10:return "completion_normal"
  return "completion_overfilled"
 @staticmethod
 def _be(x):
  if not np.isfinite(x):return "downside_unknown"
  if x<.75:return "downside_mild"
  if x<1.5:return "downside_large"
  return "downside_extreme"
 @staticmethod
 def _bpos(x):
  if not np.isfinite(x):return "position_unknown"
  if x<=.20:return "position_near_low"
  if x<=.40:return "position_lower"
  return "position_not_low"
 def _norm(self,f):
  req=["Datetime","Bar_Start","Open","High","Low","Close","ATR_14"];miss=[c for c in req if c not in f]
  if miss:raise CandidateContinuationError(f"missing {miss}")
  o=f.copy();o["Datetime"]=to_datetime_iso(o["Datetime"],utc=True,errors="coerce");o["Bar_Start"]=to_datetime_iso(o["Bar_Start"],utc=True,errors="coerce")
  for c in ["Open","High","Low","Close","ATR_14","NATR","Range_Bps",*OPTIONAL]:
   if c in o:o[c]=pd.to_numeric(o[c],errors="coerce")
  return o.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
 def _eventvol(self,c,ts):
  # Calendar-time backward lookup -- see absorption.py's identical port
  # (verified 2026-09-25 via synthetic parity test, max abs diff = 0.0).
  s=self._state;ts_ns=int(ts.value);target_ns=ts_ns-self.evlook_ns;pos=bisect.bisect_left(s.close_hist_ns,target_ns)-1
  ret=float(np.log((c+self.eps)/(s.close_hist[pos]+self.eps))) if pos>=0 else math.nan
  var=float(_ewm_var_update(s.ewm_var,ret));return max(math.sqrt(var),self.minvol) if np.isfinite(var) and var>=0 else math.nan
 def _history(self,key):
  s=self._state
  if key not in s.bias_history:s.bias_history[key]=deque(maxlen=self.fadelook)
  return s.bias_history[key]
 def _advance(self,r,enforce):
  s=self._state;ts=self._ts(r["Datetime"]);bs=self._ts(r["Bar_Start"])
  if enforce and s.last_timestamp is not None and ts<=s.last_timestamp:raise CandidateContinuationError("non-increasing")
  o=self._num(r["Open"],"Open");h=self._num(r["High"],"High");l=self._num(r["Low"],"Low");c=self._num(r["Close"],"Close");atr=max(self._num(r["ATR_14"],"ATR_14"),self.eps);date=ts.strftime("%Y-%m-%d")
  if s.current_date!=date:
   if s.current_date is not None and np.isfinite(s.day_high) and np.isfinite(s.day_low):s.completed_ranges.append(max(s.day_high-s.day_low,self.eps))
   s.current_date=date;s.day_open=o;s.day_high=h;s.day_low=l;s.bars_into_day=1
  else:
   s.day_high=max(s.day_high,h);s.day_low=min(s.day_low,l);s.bars_into_day+=1
  expected=float(np.mean(s.completed_ranges)) if len(s.completed_ranges)>=self.mindays else math.nan
  dayrange=max(s.day_high-s.day_low,self.eps);completion=dayrange/(expected+self.eps) if np.isfinite(expected) else math.nan;down=max(s.day_open-c,0.0);downatr=down/(atr+self.eps);downexp=down/(expected+self.eps) if np.isfinite(expected) else math.nan;pos=(c-s.day_low)/(dayrange+self.eps)
  pb=self._bp(s.bars_into_day);cb=self._bc(completion);eb=self._be(downatr);posb=self._bpos(pos);key="|".join([pb,cb,eb,posb])
  # Mature i-horizon before querying current key, exactly like source.
  if len(s.maturity_queue)>=self.horizon:
   start_close,start_key=s.maturity_queue[0]
   outcome=float(np.log((c+self.eps)/(start_close+self.eps)))
   if np.isfinite(outcome):self._history(start_key).append(outcome)
  hist=self._history(key);count=len(hist);mean=std=tstat=math.nan
  if count>=self.minobs:
   arr=np.asarray(hist,dtype=float);mean=float(np.nanmean(arr));std=float(np.nanstd(arr,ddof=1)) if count>1 else math.nan
   if np.isfinite(std) and std>self.eps:tstat=mean/((std+self.eps)/math.sqrt(float(count)))
  ev=self._eventvol(c,ts);flag=int(np.isfinite(expected) and np.isfinite(ev) and s.bars_into_day>=self.minprogress and completion>=self.mincompletion and downatr>=self.minext and pos<=self.maxpos and c<s.day_open);em=None
  if flag and s.processed_rows-s.last_event_index>=self.gap:
   s.last_event_index=s.processed_rows
   em={"Datetime":ts,"Bar_Start":bs,"Open":o,"High":h,"Low":l,"Close":c,"Event_Vol":ev,"Daily_Date":date,"Day_Open":s.day_open,"Day_High_So_Far":s.day_high,"Day_Low_So_Far":s.day_low,"Day_Range_So_Far":dayrange,"Expected_Daily_Range":expected,"Daily_Range_Completion":completion,"Downside_Extension_From_Open":down,"Downside_Extension_ATR":downatr,"Downside_Extension_Expected_Range":downexp,"Day_Position":pos,"Bars_Into_Day":s.bars_into_day,"Day_Progress_Bucket":pb,"Range_Completion_Bucket":cb,"Downside_Extension_Bucket":eb,"Day_Position_Bucket":posb,"Daily_Fade_State_Key":key,"Daily_Fade_Sample_Count":count,"Daily_Fade_Bias_Mean":mean,"Daily_Fade_Bias_Std":std,"Daily_Fade_Bias_TStat":tstat,"Daily_Fade_Bias_Forward_Horizon":self.horizon,"Daily_Fade_Lookback":self.fadelook,"Daily_Fade_Bias_Available":int(count>=self.minobs),"Daily_Range_Fade_Event":1}
   for k in OPTIONAL:
    if k in r:em[k]=float(r[k]) if pd.notna(r[k]) else math.nan
   em.update({"edge_id":self.identity.edge_id,"side":1,"event_id":f"{self.identity.edge_id}|{ts.isoformat()}|daily_range_completion_fade","candidate_event_generator":self.identity.candidate_event_generator,"event_known_after_close":True,"uses_future_information":False,"expected_daily_range_lookback":self.look,"min_expected_range_days":self.mindays,"fade_forward_horizon":self.horizon,"fade_lookback":self.fadelook,"min_fade_obs":self.minobs,"min_day_progress_bars":self.minprogress,"min_daily_range_completion":self.mincompletion,"min_downside_extension_atr":self.minext,"max_day_position":self.maxpos,"min_gap_bars":self.gap,"event_vol_lookback_hours":self.evlook_hours,"event_vol_ewm_span":self.evspan})
  s.maturity_queue.append((c,key));s.close_hist.append(c);s.close_hist_ns.append(int(ts.value))
  # 2026-10-18 parity fix: keep every bar at/after (t - L) PLUS the single latest bar before it -- the reference
  # calendar_time_event_vol looks back to the latest bar strictly before (t' - L) at ANY age, so the old 2L cutoff
  # lost that bar after session/weekend gaps longer than 2L. Bounded and exact for all t' >= t.
  cutoff_ns=int(ts.value)-self.evlook_ns;drop=bisect.bisect_left(s.close_hist_ns,cutoff_ns)-1
  if drop>0:del s.close_hist[:drop];del s.close_hist_ns[:drop]
  s.processed_rows+=1;s.last_timestamp=ts;return em
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
 def state_summary(self):return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,"processed_rows":self._state.processed_rows,"completed_daily_ranges":len(self._state.completed_ranges),"fade_state_count":len(self._state.bias_history),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}
def _c(c,n,d):return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
 sig=str(candidate_logic_signature or "").strip()
 if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError("unsupported")
 c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {};id=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
 return DailyRangeFadeLongCandidateContinuation(identity=id,eps=float(_c(c,"EPS",1e-12)),expected_daily_range_lookback=int(_c(c,"EXPECTED_DAILY_RANGE_LOOKBACK",20)),min_expected_range_days=int(_c(c,"MIN_EXPECTED_RANGE_DAYS",10)),fade_forward_horizon=int(_c(c,"FADE_FORWARD_HORIZON",50)),fade_lookback=int(_c(c,"FADE_LOOKBACK",500)),min_fade_obs=int(_c(c,"MIN_FADE_OBS",30)),min_day_progress_bars=int(_c(c,"MIN_DAY_PROGRESS_BARS",20)),min_daily_range_completion=float(_c(c,"MIN_DAILY_RANGE_COMPLETION",.70)),min_downside_extension_atr=float(_c(c,"MIN_DOWNSIDE_EXTENSION_ATR",.50)),max_day_position=float(_c(c,"MAX_DAY_POSITION",.35)),min_gap_bars=int(_c(c,"MIN_GAP_BARS",3)),event_vol_lookback_hours=float(_c(c,"EVENT_VOL_LOOKBACK_HOURS",7.462)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_vol=float(_c(c,"MIN_VOL",1e-8)))
