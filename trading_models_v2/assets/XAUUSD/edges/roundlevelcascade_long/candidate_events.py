"""
Candidate event generation for XAUUSD/roundlevelcascade_long.

Thesis card: docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md
(Gate 0 test 26, PASS 2026-11-07 on XAUUSD / HK / JPN LONG; owner 2026-11-09 "yes approve both").

Mechanism (Osler 2003 JF 58(5); Osler 2005 JIMF 24): stop-loss orders cluster just beyond round price levels. The
longer a round level stays untouched, the more buy stops accumulate above it. The FIRST close beyond a round level
that has not traded for 24 h triggers them, and the stop cascade continues the break.

Trigger (decided at the bar close, past-only, identical to gate0_tool.t13_events with grid offset 0):
- H24 / L24 = highest high / lowest low of the bars closing in [t - 24 h, t) (bar t itself excluded);
- the bar must follow its predecessor within 2 h (a break inside a trading market, not over a closure);
- U = first round level k * ROUND_SPACING strictly above H24; D = first round level strictly below L24;
- raw up-break: Close >= U (Round_Break_Type = +1); raw down-break: Close <= D (Round_Break_Type = -1).
primary_side.py keeps the up-breaks only (side = +1 long). Down-breaks failed Gate 0 (pooled drift) and are not traded.

Round_* fields are event/control/context fields, not model-schema features.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from shared.io.timeparse import to_datetime_iso
from shared.labels.calendar_time_barriers import calendar_time_event_vol

EDGE_ID = "XAUUSD/roundlevelcascade_long"
ASSET = "XAUUSD"
SIDE_VALUE = 1
CANDIDATE_EVENT_GENERATOR = "roundlevelcascade_long_v1_first_close_beyond_untouched_round_level"

# Pre-registered round spacing (absolute price, conventional figure), T13 card -- fixed before any run.
ROUND_SPACING_BY_ASSET = {"NASDAQ": 100.0, "US500": 50.0, "DAX": 100.0, "JPN": 500.0, "HK": 500.0, "XAUUSD": 10.0,
                          "GBPJPY": 1.0, "USDMXN": 0.10, "USOIL": 1.0, "BTCUSD": 1000.0}
ROUND_SPACING = ROUND_SPACING_BY_ASSET[ASSET]
UNTOUCHED_WINDOW_HOURS = 24.0
MAX_BAR_GAP_HOURS = 2.0
EVENT_VOL_LOOKBACK_HOURS = 8.0  # LOCKED 2026-11-09 Gate C winner H8_E150_V4_PT1_SL1 (full history, era-scaled broker cost; PBO 0.14, DSR 0.992; 2022+ +0.094 R)
EVENT_VOL_EWM_SPAN = 150  # LOCKED 2026-11-09 Gate C winner H8_E150_V4_PT1_SL1 (full history, era-scaled broker cost; PBO 0.14, DSR 0.992; 2022+ +0.094 R)
MIN_VOL = 1e-8
EPS = 1e-12

EVENT_CONTEXT_COLUMNS = [
    "Event_Vol",
    "Round_Break_Type",
    "Round_Level",
    "Round_Spacing",
    "Prior_24h_High",
    "Prior_24h_Low",
    "Break_Excess_Vol",
    "Regime_State",
    "Regime_Entropy",
    "Regime_Duration",
    "Prob_State_0",
    "Prob_State_1",
    "Prob_State_2",
    "Prob_State_3",
]


class CandidateEventError(RuntimeError):
    """Raised when candidate event generation must fail closed."""


def _required_event_columns() -> list[str]:
    return [
        "Datetime",
        "Bar_Start",
        "edge_id",
        "side",
        "Event_Vol",
        "event_id",
        "candidate_event_generator",
        "Round_Break_Type",
        "Round_Level",
        "event_known_after_close",
        "uses_future_information",
    ]


def _forbidden_event_columns() -> set[str]:
    return {
        "meta_label",
        "t1",
        "barrier_hit",
        "resolution_source",
        "entry_price",
        "pt_price",
        "sl_price",
        "realized_r",
        "raw_probability",
        "calibrated_probability",
        "afml_bet_size",
        "approved_signal",
    }


def _normalize_feature_input(features: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in features.columns]
    if missing:
        raise CandidateEventError(f"Input features missing required columns: {missing}")

    df = features.copy()
    df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True, errors="coerce")
    df["Bar_Start"] = to_datetime_iso(df["Bar_Start"], utc=True, errors="coerce")
    numeric_cols = [
        "Open", "High", "Low", "Close",
        "Regime_State", "Regime_Entropy", "Regime_Duration",
        "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3",
    ]
    for col in [c for c in numeric_cols if c in df.columns]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required).copy()
    df = df.sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    if df.empty:
        raise CandidateEventError("Input features are empty after normalization.")
    return df


def add_round_level_fields(features: pd.DataFrame) -> pd.DataFrame:
    """Add the past-only round-level break fields (24 h window excludes the current bar)."""

    out = _normalize_feature_input(features)
    high = out["High"].astype("float64")
    low = out["Low"].astype("float64")
    close = out["Close"].astype("float64")
    ts_ns = out["Datetime"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]").view("int64")

    out["Event_Vol"] = calendar_time_event_vol(
        ts_ns,
        close.to_numpy(dtype="float64"),
        hours=EVENT_VOL_LOOKBACK_HOURS,
        ewm_span=EVENT_VOL_EWM_SPAN,
        min_vol=MIN_VOL,
    )

    idx = pd.DatetimeIndex(out["Datetime"])
    window = f"{int(UNTOUCHED_WINDOW_HOURS * 3600)}s"
    h24 = pd.Series(high.to_numpy(), index=idx).rolling(window, closed="left").max().to_numpy()
    l24 = pd.Series(low.to_numpy(), index=idx).rolling(window, closed="left").min().to_numpy()
    out["Prior_24h_High"] = h24
    out["Prior_24h_Low"] = l24

    s = float(ROUND_SPACING)
    up = (np.floor(h24 / s) + 1.0) * s
    dn = (np.ceil(l24 / s) - 1.0) * s
    gap_ok = np.r_[False, np.diff(ts_ns) <= int(MAX_BAR_GAP_HOURS * 3600 * 1e9)]
    finite = np.isfinite(h24) & np.isfinite(l24)
    c = close.to_numpy()
    up_break = gap_ok & finite & (c >= up)
    dn_break = gap_ok & finite & (c <= dn) & ~up_break

    out["Round_Spacing"] = s
    out["Round_Break_Type"] = np.where(up_break, 1, np.where(dn_break, -1, 0)).astype("int64")
    out["Round_Level"] = np.where(up_break, up, np.where(dn_break, dn, np.nan))
    vol_px = out["Event_Vol"].to_numpy(dtype="float64") * c
    out["Break_Excess_Vol"] = np.where(up_break, (c - up) / (vol_px + EPS),
                                       np.where(dn_break, (dn - c) / (vol_px + EPS), np.nan))
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def _validate_candidate_events(events: pd.DataFrame) -> None:
    missing = [col for col in _required_event_columns() if col not in events.columns]
    if missing:
        raise CandidateEventError(f"Candidate events missing required columns: {missing}")
    if events.empty:
        raise CandidateEventError("Candidate event generator produced zero roundlevelcascade events.")
    forbidden_present = sorted(_forbidden_event_columns().intersection(events.columns))
    if forbidden_present:
        raise CandidateEventError(f"Candidate events contain forbidden research/live-decision columns: {forbidden_present}")
    if not (events["edge_id"] == EDGE_ID).all():
        raise CandidateEventError("Candidate events contain unexpected edge_id values.")
    sides = pd.to_numeric(events["side"], errors="coerce")
    if not sides.isin([-1, 1]).all():
        raise CandidateEventError("Raw round-break sides must be +/-1 (the break direction).")
    if not (pd.to_numeric(events["Round_Break_Type"], errors="coerce") == sides).all():
        raise CandidateEventError("Raw side must equal Round_Break_Type.")
    if not (events["candidate_event_generator"] == CANDIDATE_EVENT_GENERATOR).all():
        raise CandidateEventError("Candidate events contain unexpected generator values.")
    if events["uses_future_information"].astype(bool).any():
        raise CandidateEventError("Candidate events must not use future information.")
    if not events["event_known_after_close"].astype(bool).all():
        raise CandidateEventError("Candidate events must be known after bar close.")
    if not np.isfinite(pd.to_numeric(events["Event_Vol"], errors="coerce").to_numpy(dtype="float64")).all():
        raise CandidateEventError("Candidate events contain non-finite Event_Vol values.")


def generate_v1_roundlevelcascade_candidate_events(features: pd.DataFrame, **_: Any) -> pd.DataFrame:
    """Raw round-level break events, side = break direction; primary_side.py keeps the up-breaks."""

    expanded = add_round_level_fields(features)
    hits = expanded[expanded["Round_Break_Type"] != 0].copy()
    keep_cols = [
        "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
        *[col for col in EVENT_CONTEXT_COLUMNS if col in hits.columns],
    ]
    events = hits[[col for col in keep_cols if col in hits.columns]].copy()
    events["edge_id"] = EDGE_ID
    events["side"] = pd.to_numeric(events["Round_Break_Type"], errors="coerce").astype("int64")
    events["event_id"] = events["Datetime"].apply(lambda ts: f"{EDGE_ID}|{pd.Timestamp(ts).isoformat()}|round_break")
    events["candidate_event_generator"] = CANDIDATE_EVENT_GENERATOR
    events["event_known_after_close"] = True
    events["uses_future_information"] = False
    events["untouched_window_hours"] = float(UNTOUCHED_WINDOW_HOURS)
    events["max_bar_gap_hours"] = float(MAX_BAR_GAP_HOURS)
    events["event_vol_lookback_hours"] = float(EVENT_VOL_LOOKBACK_HOURS)
    events["event_vol_ewm_span"] = int(EVENT_VOL_EWM_SPAN)
    events = events.dropna(subset=["Datetime", "Bar_Start", "Event_Vol", "Round_Level", "side"]).copy()
    events = events[(events["Event_Vol"] > 0) & events["side"].isin([-1, 1])].copy()
    events = events.sort_values("Datetime").reset_index(drop=True)
    ordered = ["Datetime", "Bar_Start", "edge_id", "side", "Event_Vol", "event_id", "candidate_event_generator",
               "Round_Break_Type", "Round_Level", "Round_Spacing", "Prior_24h_High", "Prior_24h_Low", "Break_Excess_Vol",
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
        "event_logic": "first close beyond a round level untouched for 24 h (stop-cascade continuation)",
        "round_spacing": ROUND_SPACING,
        "untouched_window_hours": UNTOUCHED_WINDOW_HOURS,
        "max_bar_gap_hours": MAX_BAR_GAP_HOURS,
        "side_rule_raw": "side = Round_Break_Type (+1 up-break, -1 down-break); primary_side.py keeps side=+1",
        "event_fields_are_model_inputs": False,
        "uses_future_information": False,
        "contains_labels": False,
        "contains_probabilities": False,
        "contains_execution_decisions": False,
        "thesis_card": "docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md",
    }


if __name__ == "__main__":
    print(f"✅ {EDGE_ID} candidate_events.py import/self-contract check passed.")
    print(candidate_event_contract())
