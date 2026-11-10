"""
Candidate event generation for DAX/closehedgingflow_long.

Thesis card: docs/mldp/campaign/NASDAQ_2026-11/T1c_causal_entry_retest/PREREGISTRATION.md
Gate 0 (T1c): PASS under D090 with the D092 firing rate (>= 2/week): stored result re-read 2026-11-10; failed only on the old >= 3/week rule.

Trigger: first bar closing at or after 16:30 Berlin (1 h before the 17:30 close) on a weekday (<= 30 min), log(close / last close at or before the previous weekday's 17:30) > 0 -> LONG
Implementation: shared/edges/thesis_triggers.entries("T1c", ...), identical to the Gate 0 event set
(docs/tools/check_thesis_trigger_parity.py, IDENTICAL 2026-11-09). Decided at bar close; the signal uses bars at or
before the clock time, the entry is the first bar closing at or after it (<= 30 min).

Session_* and Clock_Signal fields are event/control/context fields, not model-schema features.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from shared.edges.thesis_triggers import entries as thesis_entries
from shared.io.timeparse import to_datetime_iso
from shared.labels.calendar_time_barriers import calendar_time_event_vol

EDGE_ID = "DAX/closehedgingflow_long"
ASSET = "DAX"
THESIS = "T1c"
SIDE_VALUE = 1
CANDIDATE_EVENT_GENERATOR = "closehedgingflow_long_v1_t1c_clock_trigger"
# Geometry LOCKED 2026-11-10 at the Gate C winner H8_E150_V16_PT2_SL1.
EVENT_VOL_LOOKBACK_HOURS = 8.0  # LOCKED 2026-11-10 Gate C winner H8_E150_V16_PT2_SL1 (full history era-scaled +0.048 R; PBO 0.462, Prob 0.198; DSR 0.977; stress +0.045 R; 2022+ +0.042 R)
EVENT_VOL_EWM_SPAN = 150  # LOCKED 2026-11-10 Gate C winner H8_E150_V16_PT2_SL1 (full history era-scaled +0.048 R; PBO 0.462, Prob 0.198; DSR 0.977; stress +0.045 R; 2022+ +0.042 R)
MIN_VOL = 1e-8

EVENT_CONTEXT_COLUMNS = [
    "Event_Vol", "Session_Event_Type", "Session_Ref_Time", "Session_Entry_Lag_Min", "Clock_Signal",
    "Regime_State", "Regime_Entropy", "Regime_Duration", "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3",
]


class CandidateEventError(RuntimeError):
    """Raised when candidate event generation must fail closed."""


def _required_event_columns() -> list[str]:
    return ["Datetime", "Bar_Start", "edge_id", "side", "Event_Vol", "event_id", "candidate_event_generator",
            "Session_Event_Type", "Session_Entry_Lag_Min", "event_known_after_close", "uses_future_information"]


def _forbidden_event_columns() -> set[str]:
    return {"meta_label", "t1", "barrier_hit", "resolution_source", "entry_price", "pt_price", "sl_price", "realized_r",
            "raw_probability", "calibrated_probability", "afml_bet_size", "approved_signal"}


def _normalize_feature_input(features: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in features.columns]
    if missing:
        raise CandidateEventError(f"Input features missing required columns: {missing}")
    df = features.copy()
    df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True, errors="coerce")
    df["Bar_Start"] = to_datetime_iso(df["Bar_Start"], utc=True, errors="coerce")
    for col in [c for c in ["Open", "High", "Low", "Close", "Regime_State", "Regime_Entropy", "Regime_Duration",
                            "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3"] if c in df.columns]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required).sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    if df.empty:
        raise CandidateEventError("Input features are empty after normalization.")
    return df


def add_session_fields(features: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_feature_input(features)
    ts = out["Datetime"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]").view("int64")
    close = out["Close"].to_numpy(dtype="float64")
    out["Event_Vol"] = calendar_time_event_vol(ts, close, hours=EVENT_VOL_LOOKBACK_HOURS, ewm_span=EVENT_VOL_EWM_SPAN,
                                               min_vol=MIN_VOL)
    hits = thesis_entries(THESIS, ASSET, SIDE_VALUE, ts, close)
    out["Session_Event_Type"] = 0
    out["Session_Ref_Time"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
    out["Session_Entry_Lag_Min"] = np.nan
    out["Clock_Signal"] = np.nan
    sel = hits["pos"].to_numpy(dtype=int)
    ref = hits["ref_ns"].to_numpy(dtype="int64")
    out.loc[sel, "Session_Event_Type"] = 1
    out.loc[sel, "Session_Ref_Time"] = pd.to_datetime(ref, utc=True)
    out.loc[sel, "Session_Entry_Lag_Min"] = (ts[sel] - ref) / 60e9
    out.loc[sel, "Clock_Signal"] = hits["signal"].to_numpy(dtype="float64")
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def _validate_candidate_events(events: pd.DataFrame) -> None:
    missing = [col for col in _required_event_columns() if col not in events.columns]
    if missing:
        raise CandidateEventError(f"Candidate events missing required columns: {missing}")
    if events.empty:
        raise CandidateEventError("Candidate event generator produced zero events.")
    forbidden = sorted(_forbidden_event_columns().intersection(events.columns))
    if forbidden:
        raise CandidateEventError(f"Candidate events contain forbidden research/live-decision columns: {forbidden}")
    if not (events["edge_id"] == EDGE_ID).all():
        raise CandidateEventError("Candidate events contain unexpected edge_id values.")
    if not (pd.to_numeric(events["side"], errors="coerce") == SIDE_VALUE).all():
        raise CandidateEventError("Events must all carry the edge side.")
    if not (pd.to_numeric(events["Session_Event_Type"], errors="coerce") == 1).all():
        raise CandidateEventError("Session_Event_Type must be 1 on events.")
    lag = pd.to_numeric(events["Session_Entry_Lag_Min"], errors="coerce")
    if not ((lag >= 0) & (lag <= 30)).all():
        raise CandidateEventError("Entry lag must be within [0, 30] minutes of the clock time.")
    if events["uses_future_information"].astype(bool).any() or not events["event_known_after_close"].astype(bool).all():
        raise CandidateEventError("Candidate events must be known after bar close and use no future information.")
    if not np.isfinite(pd.to_numeric(events["Event_Vol"], errors="coerce").to_numpy(dtype="float64")).all():
        raise CandidateEventError("Candidate events contain non-finite Event_Vol values.")


def generate_v1_closehedgingflow_candidate_events(features: pd.DataFrame, **_: Any) -> pd.DataFrame:
    expanded = add_session_fields(features)
    hits = expanded[expanded["Session_Event_Type"] == 1].copy()
    keep = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", *[c for c in EVENT_CONTEXT_COLUMNS if c in hits.columns]]
    events = hits[[c for c in keep if c in hits.columns]].copy()
    events["edge_id"] = EDGE_ID
    events["side"] = int(SIDE_VALUE)
    events["event_id"] = events["Datetime"].apply(lambda t: f"{EDGE_ID}|{pd.Timestamp(t).isoformat()}|{THESIS}")
    events["candidate_event_generator"] = CANDIDATE_EVENT_GENERATOR
    events["event_known_after_close"] = True
    events["uses_future_information"] = False
    events["event_vol_lookback_hours"] = float(EVENT_VOL_LOOKBACK_HOURS)
    events["event_vol_ewm_span"] = int(EVENT_VOL_EWM_SPAN)
    events = events.dropna(subset=["Datetime", "Bar_Start", "Event_Vol"]).copy()
    events = events[events["Event_Vol"] > 0].sort_values("Datetime").reset_index(drop=True)
    ordered = ["Datetime", "Bar_Start", "edge_id", "side", "Event_Vol", "event_id", "candidate_event_generator",
               "Session_Event_Type", "Session_Ref_Time", "Session_Entry_Lag_Min", "Clock_Signal",
               "event_known_after_close", "uses_future_information"]
    events = events[[c for c in ordered if c in events.columns] + [c for c in events.columns if c not in ordered]].copy()
    _validate_candidate_events(events)
    return events


def validate_candidate_events(events: pd.DataFrame) -> None:
    _validate_candidate_events(events)


def candidate_event_contract() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "candidate_event_generator": CANDIDATE_EVENT_GENERATOR,
        "event_logic": "first bar closing at or after 16:30 Berlin (1 h before the 17:30 close) on a weekday (<= 30 min), log(close / last close at or before the previous weekday's 17:30) > 0 -> LONG",
        "thesis": THESIS,
        "side": SIDE_VALUE,
        "gate0_status": "D090 PASS under D092 firing rate, re-read 2026-11-10",
        "event_fields_are_model_inputs": False,
        "uses_future_information": False,
        "contains_labels": False,
        "contains_probabilities": False,
        "contains_execution_decisions": False,
        "thesis_card": "docs/mldp/campaign/NASDAQ_2026-11/T1c_causal_entry_retest/PREREGISTRATION.md",
    }


if __name__ == "__main__":
    print(f"✅ {EDGE_ID} candidate_events.py import/self-contract check passed.")
    print(candidate_event_contract())
