from __future__ import annotations
"""Exact incremental trendpullbackcontinuation_long candidate continuation."""
from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Optional, Union
import numpy as np, pandas as pd
from shared.edges.native_one_row_feature_engine import _ewm_var_state,_ewm_var_update
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import CandidateContinuationError,extract_candidate_source_identity

TRENDPULLBACK_LONG_SIGNATURE="e5a4f046789a078c6162f03bacc6ad5451ee0299ecc10217f23d2061bc7252c4"
PROVIDER_SPEC={"provider_id":"phase_d4_trendpullback_long_candidate","provider_version":"1",
"supported_signatures":{TRENDPULLBACK_LONG_SIGNATURE:{"side_specialization":"long"}}}
OPTIONAL_CONTEXT=("Regime_State","Regime_Entropy","Regime_Duration","Prob_State_0","Prob_State_1","Prob_State_2","Prob_State_3")

@dataclass
class _State:
    ewm_var:dict[str,Any]
    close_history:list[float]=field(default_factory=list)
    pullback_history:list[float]=field(default_factory=list)
    high_history:list[float]=field(default_factory=list)
    ffd_history:list[float]=field(default_factory=list)
    processed_rows:int=0
    last_selected_event_index:int=-(10**9)
    last_timestamp:Optional[pd.Timestamp]=None

class TrendPullbackLongCandidateContinuation:
    def __init__(self,*,identity,eps,trend_lookback,pullback_lookback,pullback_z_lookback,
                 min_trend_slope_50,min_ema_20_50_gap,max_pullback_return_z,max_pullback_return,
                 max_channel_position_20,min_channel_position_50,min_close_location,
                 min_lower_wick_share,min_pullback_depth_atr,min_gap_bars,
                 event_vol_lookback,event_vol_ewm_span,min_vol):
        self.identity=identity; self.eps=float(eps); self.trend_lookback=int(trend_lookback)
        self.pullback_lookback=int(pullback_lookback); self.pullback_z_lookback=int(pullback_z_lookback)
        self.min_trend_slope_50=float(min_trend_slope_50); self.min_ema_20_50_gap=float(min_ema_20_50_gap)
        self.max_pullback_return_z=float(max_pullback_return_z); self.max_pullback_return=float(max_pullback_return)
        self.max_channel_position_20=float(max_channel_position_20); self.min_channel_position_50=float(min_channel_position_50)
        self.min_close_location=float(min_close_location); self.min_lower_wick_share=float(min_lower_wick_share)
        self.min_pullback_depth_atr=float(min_pullback_depth_atr); self.min_gap_bars=int(min_gap_bars)
        self.event_vol_lookback=int(event_vol_lookback); self.event_vol_ewm_span=int(event_vol_ewm_span); self.min_vol=float(min_vol)
        self._state=self._new_state()
    def _new_state(self): return _State(ewm_var=_ewm_var_state(np.asarray([],dtype="float64"),self.event_vol_ewm_span))
    def reset(self): self._state=self._new_state()
    @staticmethod
    def _ts(v):
        x=to_datetime_iso(v,utc=True,errors="coerce")
        if pd.isna(x): raise CandidateContinuationError(f"invalid timestamp {v!r}")
        return pd.Timestamp(x)
    @staticmethod
    def _num(v,n):
        x=pd.to_numeric(pd.Series([v]),errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)): raise CandidateContinuationError(f"invalid {n}: {v!r}")
        return float(x)
    @staticmethod
    def _opt(v):
        x=pd.to_numeric(pd.Series([v]),errors="coerce").iat[0]; return math.nan if pd.isna(x) else float(x)
    @staticmethod
    def _bucket_pb(x):
        if not np.isfinite(x): return "pullback_unknown"
        if x<=-1: return "pullback_deep"
        if x<=-.5: return "pullback_medium"
        if x<=-.2: return "pullback_light"
        return "pullback_flat"
    @staticmethod
    def _bucket_channel(x):
        if not np.isfinite(x): return "channel_unknown"
        if x<=.30:return "channel_low"
        if x<=.55:return "channel_mid"
        if x<=.70:return "channel_upper_mid"
        return "channel_high"
    @staticmethod
    def _bucket_reclaim(cl,lw):
        if not np.isfinite(cl) or not np.isfinite(lw):return "reclaim_unknown"
        if cl>=.60 or lw>=.20:return "reclaim_strong"
        if cl>=.40 or lw>=.05:return "reclaim_mild"
        return "reclaim_weak"
    def _normalize(self,f):
        req=["Datetime","Bar_Start","Open","High","Low","Close","ATR_14","Log_Return_5","Trend_Slope_50","EMA_20_50_Gap","Channel_Position_20","Channel_Position_50","Close_Location_Recalc","Lower_Wick_Share"]
        miss=[c for c in req if c not in f.columns]
        if miss: raise CandidateContinuationError(f"missing trend-pullback columns: {miss}")
        out=f.copy(); out["Datetime"]=to_datetime_iso(out["Datetime"],utc=True,errors="coerce"); out["Bar_Start"]=to_datetime_iso(out["Bar_Start"],utc=True,errors="coerce")
        for c in ["Open","High","Low","Close","ATR_14","Log_Return_5","Trend_Slope_50","EMA_20_50_Gap","Channel_Position_20","Channel_Position_50","Close_Location_Recalc","Lower_Wick_Share","Close_FFD",*OPTIONAL_CONTEXT]:
            if c in out: out[c]=pd.to_numeric(out[c],errors="coerce")
        return out.dropna(subset=req).sort_values("Datetime").reset_index(drop=True)
    def _event_vol(self,close):
        h=self._state.close_history
        ret=float(np.log((close+self.eps)/(h[-self.event_vol_lookback]+self.eps))) if len(h)>=self.event_vol_lookback else math.nan
        var=float(_ewm_var_update(self._state.ewm_var,ret))
        ev=max(math.sqrt(var),self.min_vol) if np.isfinite(var) and var>=0 else math.nan
        h.append(close)
        if len(h)>self.event_vol_lookback: del h[:len(h)-self.event_vol_lookback]
        return ev
    def _advance(self,row,enforce):
        s=self._state; ts=self._ts(row["Datetime"]); bs=self._ts(row["Bar_Start"])
        if enforce and s.last_timestamp is not None and ts<=s.last_timestamp: raise CandidateContinuationError("update_one requires increasing Datetime")
        open_=self._num(row["Open"],"Open"); high=self._num(row["High"],"High"); low=self._num(row["Low"],"Low"); close=self._num(row["Close"],"Close"); atr=max(self._num(row["ATR_14"],"ATR_14"),self.eps)
        pb=self._num(row["Log_Return_5"],"Log_Return_5"); slope=self._num(row["Trend_Slope_50"],"Trend_Slope_50"); gap=self._num(row["EMA_20_50_Gap"],"EMA_20_50_Gap")
        ch20=self._num(row["Channel_Position_20"],"Channel_Position_20"); ch50=self._num(row["Channel_Position_50"],"Channel_Position_50")
        cl=self._num(row["Close_Location_Recalc"],"Close_Location_Recalc"); lw=self._num(row["Lower_Wick_Share"],"Lower_Wick_Share")
        ev=self._event_vol(close)
        if len(s.pullback_history)>=self.pullback_z_lookback:
            ser=pd.Series(s.pullback_history[-self.pullback_z_lookback:],dtype="float64")
            pbz=float((pb-float(ser.mean()))/(float(ser.std())+self.eps))
        else: pbz=math.nan
        s.pullback_history.append(pb)
        if len(s.pullback_history)>self.pullback_z_lookback: del s.pullback_history[:len(s.pullback_history)-self.pullback_z_lookback]
        s.high_history.append(high)
        if len(s.high_history)>self.pullback_lookback: del s.high_history[:len(s.high_history)-self.pullback_lookback]
        recent_high=max(s.high_history) if len(s.high_history)>=2 else math.nan
        depth=max(recent_high-close,0.0)/(atr+self.eps) if np.isfinite(recent_high) else math.nan
        ffd_signal=math.nan
        if "Close_FFD" in row and pd.notna(row["Close_FFD"]):
            ffd=self._num(row["Close_FFD"],"Close_FFD"); s.ffd_history.append(ffd)
            if len(s.ffd_history)>self.trend_lookback: del s.ffd_history[:len(s.ffd_history)-self.trend_lookback]
            if len(s.ffd_history)>=self.trend_lookback: ffd_signal=float(ffd-np.mean(s.ffd_history))
        positive=int(slope>self.min_trend_slope_50 or gap>self.min_ema_20_50_gap or (np.isfinite(ffd_signal) and ffd_signal>0))
        weak=bool(pb<=self.max_pullback_return or (np.isfinite(pbz) and pbz<=self.max_pullback_return_z))
        structure=bool(ch20<=self.max_channel_position_20 and ch50>=self.min_channel_position_50 and np.isfinite(depth) and depth>=self.min_pullback_depth_atr)
        reclaim=int(cl>=self.min_close_location or lw>=self.min_lower_wick_share)
        flag=int(np.isfinite(ev) and positive==1 and weak and structure and reclaim==1)
        emitted=None
        if flag and s.processed_rows-s.last_selected_event_index>=self.min_gap_bars:
            # The batch source's generate_v1_*() selects this bar for spacing
            # purposes (consuming the min_gap_bars slot) BEFORE its final
            # dropna(subset=[...,"Pullback_Return_Z",...]) drops the row from
            # the returned events -- so the gap slot must advance here even
            # when pbz is still NaN (PULLBACK_Z_LOOKBACK warmup), but the
            # event itself must not be emitted in that case.
            s.last_selected_event_index=s.processed_rows
            if not np.isfinite(pbz):
                s.processed_rows+=1;s.last_timestamp=ts;return None
            pbb=self._bucket_pb(pbz); cb=self._bucket_channel(ch20); rb=self._bucket_reclaim(cl,lw)
            emitted={"Datetime":ts,"Bar_Start":bs,"edge_id":self.identity.edge_id,"side":1,"Event_Vol":float(ev),
            "event_id":f"{self.identity.edge_id}|{ts.isoformat()}|trend_pullback_continuation","candidate_event_generator":self.identity.candidate_event_generator,
            "Trend_Slope_50_Context":slope,"EMA_20_50_Gap_Context":gap,"FFD_Trend_Signal_50":ffd_signal,"Positive_Drift_State":positive,
            "Pullback_Return":pb,"Pullback_Return_Z":pbz,"Pullback_Depth_ATR":depth,"Channel_Position_20_Context":ch20,"Channel_Position_50_Context":ch50,
            "Current_Close_Location":cl,"Lower_Wick_Share_Context":lw,"Pullback_Reclaim":reclaim,
            "Trend_Pullback_State_Key":"|".join([pbb,cb,rb]),
            "Trend_Pullback_Continuation_Event":1,"event_known_after_close":True,"uses_future_information":False,
            "Open":open_,"High":high,"Low":low,"Close":close}
            for c in OPTIONAL_CONTEXT:
                if c in row: emitted[c]=self._opt(row[c])
            emitted.update({"trend_lookback":self.trend_lookback,"pullback_lookback":self.pullback_lookback,
            "max_pullback_return_z":self.max_pullback_return_z,"max_pullback_return":self.max_pullback_return,
            "max_channel_position_20":self.max_channel_position_20,"min_channel_position_50":self.min_channel_position_50,
            "min_close_location":self.min_close_location,"min_lower_wick_share":self.min_lower_wick_share,
            "min_pullback_depth_atr":self.min_pullback_depth_atr,"min_gap_bars":self.min_gap_bars,
            "event_vol_lookback":self.event_vol_lookback,"event_vol_ewm_span":self.event_vol_ewm_span})
        s.processed_rows+=1;s.last_timestamp=ts;return emitted
    def bootstrap(self,f):
        x=self._normalize(f);self.reset()
        for r in x.to_dict("records"):self._advance(r,False)
        return self.state_summary()
    def update_one(self,row):
        return self._advance(row.to_dict() if isinstance(row,pd.Series) else dict(row),True)
    def replay(self,f):
        x=self._normalize(f);self.reset();rows=[]
        for r in x.to_dict("records"):
            ev=self._advance(r,False)
            if ev is not None:rows.append(ev)
        return pd.DataFrame(rows)
    def state_summary(self):
        s=self._state;return {"provider_id":PROVIDER_SPEC["provider_id"],"provider_version":"1","edge_id":self.identity.edge_id,
        "processed_rows":s.processed_rows,"pullback_history_rows":len(s.pullback_history),"high_history_rows":len(s.high_history),
        "ffd_history_rows":len(s.ffd_history),"close_history_rows":len(s.close_history),"normal_path_history_disk_reads":False,"normal_path_full_candidate_fallback":False}

def _c(c,n,d=None): return c[n] if n in c else d
def create_provider(*,candidate_logic_signature,d1_edge,v2_root):
    sig=str(candidate_logic_signature or "").strip()
    if sig not in PROVIDER_SPEC["supported_signatures"]:raise CandidateContinuationError(f"unsupported signature {sig}")
    c=(d1_edge.get("candidate") or {}).get("semantic_constants") or {}; ident=extract_candidate_source_identity(v2_root=v2_root,d1_edge=d1_edge)
    return TrendPullbackLongCandidateContinuation(identity=ident,eps=float(_c(c,"EPS",1e-12)),trend_lookback=int(_c(c,"TREND_LOOKBACK",50)),
    pullback_lookback=int(_c(c,"PULLBACK_LOOKBACK",5)),pullback_z_lookback=int(_c(c,"PULLBACK_Z_LOOKBACK",50)),
    min_trend_slope_50=float(_c(c,"MIN_TREND_SLOPE_50",0)),min_ema_20_50_gap=float(_c(c,"MIN_EMA_20_50_GAP",0)),
    max_pullback_return_z=float(_c(c,"MAX_PULLBACK_RETURN_Z",-0.2)),max_pullback_return=float(_c(c,"MAX_PULLBACK_RETURN",0)),
    max_channel_position_20=float(_c(c,"MAX_CHANNEL_POSITION_20",.7)),min_channel_position_50=float(_c(c,"MIN_CHANNEL_POSITION_50",.3)),
    min_close_location=float(_c(c,"MIN_CLOSE_LOCATION",.4)),min_lower_wick_share=float(_c(c,"MIN_LOWER_WICK_SHARE",.05)),
    min_pullback_depth_atr=float(_c(c,"MIN_PULLBACK_DEPTH_ATR",.05)),min_gap_bars=int(_c(c,"MIN_GAP_BARS",3)),
    event_vol_lookback=int(_c(c,"EVENT_VOL_LOOKBACK",50)),event_vol_ewm_span=int(_c(c,"EVENT_VOL_EWM_SPAN",100)),min_vol=float(_c(c,"MIN_VOL",1e-8)))
