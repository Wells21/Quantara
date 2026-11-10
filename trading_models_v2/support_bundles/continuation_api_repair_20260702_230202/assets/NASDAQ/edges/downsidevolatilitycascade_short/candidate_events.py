"""Causal high-recall candidate events for NASDAQ/downsidevolatilitycascade_short.

The event state is downside-dominated realised volatility:
- current 20-bar variance rate exceeds the non-overlapping prior 100-bar baseline;
- at least 60% of current realised variance comes from negative returns;
- the completed event bar has a negative return.

For MLDP-style high recall, the sampler emits both the first negative impulse at
state entry and later negative impulses inside the same active cascade, subject
to a causal minimum spacing rule. FFD, HMM, trend, candle quality and jump
concentration remain context only.
"""
from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

EDGE_ID = "NASDAQ/downsidevolatilitycascade_short"
SIDE_VALUE = -1
RAW_SIDE_VALUE = 0
CANDIDATE_EVENT_GENERATOR = (
    "downsidevolatilitycascade_short_v3_high_recall_signed_semivariance_impulses"
)
SEMIVARIANCE_WINDOW = 20
BASELINE_WINDOW = 100
BASELINE_EXCLUDE_RECENT_BARS = 20
DOWNSIDE_SHARE_THRESHOLD = 0.60
VOL_ACCELERATION_THRESHOLD = 1.10
MIN_EVENT_GAP_BARS = 3
EVENT_VOL_LOOKBACK_BARS = 50
EVENT_VOL_EWM_SPAN = 100
MIN_EVENT_VOL = 1e-8
EPS = 1e-12

EVENT_CONTEXT_COLUMNS = [
    "Event_Vol", "Downside_Volatility_Cascade_Event", "Cascade_State_Entry",
    "Cascade_Renewed_Impulse", "Cascade_Active_State", "Log_Return_1",
    "Negative_Event_Bar", "Downside_Semivariance_5", "Upside_Semivariance_5",
    "Realized_Variance_5", "Downside_Semivariance_10", "Upside_Semivariance_10",
    "Realized_Variance_10", "Downside_Semivariance_20", "Upside_Semivariance_20",
    "Realized_Variance_20", "Downside_Variance_Share_5",
    "Downside_Variance_Share_10", "Downside_Variance_Share_20",
    "Bad_Vol_Ratio_5", "Bad_Vol_Ratio_10", "Bad_Vol_Ratio_20",
    "RV_Per_Bar_5", "RV_Per_Bar_10", "RV_Per_Bar_20",
    "Baseline_RV_Per_Bar_100", "Vol_Acceleration_5_100",
    "Vol_Acceleration_10_100", "Vol_Acceleration_20_100",
    "Negative_Return_Count_5", "Negative_Return_Count_10",
    "Negative_Return_Count_20", "Negative_Return_Fraction_20",
    "Consecutive_Negative_Returns", "Largest_Negative_Return_20",
    "Largest_Positive_Return_20", "Cumulative_Return_5", "Cumulative_Return_10",
    "Cumulative_Return_20", "Largest_Negative_Variance_Share_20",
    "Current_Return_Share_Of_Downside_Variance", "Range_ATR", "Body_Direction",
    "Body_Share", "Close_Location", "Regime_State", "Regime_Entropy",
    "Regime_Duration", "Prob_State_0", "Prob_State_1", "Prob_State_2",
    "Prob_State_3",
]
_LAST_STATE_MACHINE_DIAGNOSTICS: dict[str, int] = {}


class CandidateEventError(RuntimeError):
    pass


def get_last_state_machine_diagnostics() -> dict[str, int]:
    return dict(_LAST_STATE_MACHINE_DIAGNOSTICS)


def _forbidden_event_columns() -> set[str]:
    return {
        "meta_label", "label", "target", "t1", "barrier_hit",
        "resolution_source", "entry_price", "pt_price", "sl_price",
        "realized_r", "raw_probability", "calibrated_probability",
        "afml_bet_size", "approved_signal", "selected_threshold",
        "Ticks", "Proxy_Value", "Tick_Activity_Ratio", "Proxy_Value_Ratio",
        "Activity_Impulse",
    }


def _normalize_feature_input(features: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "ATR_14"]
    missing = [column for column in required if column not in features.columns]
    if missing:
        raise CandidateEventError(f"Input features missing required columns: {missing}")
    out = features.copy()
    out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
    out["Bar_Start"] = pd.to_datetime(out["Bar_Start"], utc=True, errors="coerce")
    numeric = [
        "Open", "High", "Low", "Close", "ATR_14", "Regime_State",
        "Regime_Entropy", "Regime_Duration", "Prob_State_0", "Prob_State_1",
        "Prob_State_2", "Prob_State_3",
    ]
    for column in [name for name in numeric if name in out.columns]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=required).sort_values("Datetime").reset_index(drop=True)
    out = out[(out["Open"] > 0) & (out["High"] > 0) & (out["Low"] > 0)
              & (out["Close"] > 0) & (out["ATR_14"] > 0)].copy()
    if out.empty:
        raise CandidateEventError("Input features are empty after normalization")
    return out


def _consecutive_negative(values: np.ndarray) -> float:
    count = 0
    for value in values[::-1]:
        if np.isfinite(value) and value < 0:
            count += 1
        else:
            break
    return float(count)


def add_downside_volatility_cascade_features(features: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_feature_input(features)
    open_ = out["Open"].astype("float64")
    high = out["High"].astype("float64")
    low = out["Low"].astype("float64")
    close = out["Close"].astype("float64")
    shifted_atr = out["ATR_14"].astype("float64").shift(1)
    returns = np.log((close + EPS) / (close.shift(1) + EPS))
    negative = returns.clip(upper=0.0)
    positive = returns.clip(lower=0.0)
    negative_sq = negative.pow(2)
    positive_sq = positive.pow(2)
    squared = returns.pow(2)

    out["Log_Return_1"] = returns
    out["Shifted_ATR_14"] = shifted_atr
    for window in (5, 10, 20):
        downside = negative_sq.rolling(window, min_periods=window).sum()
        upside = positive_sq.rolling(window, min_periods=window).sum()
        total = downside + upside
        out[f"Downside_Semivariance_{window}"] = downside
        out[f"Upside_Semivariance_{window}"] = upside
        out[f"Realized_Variance_{window}"] = total
        out[f"Downside_Variance_Share_{window}"] = downside / (total + EPS)
        out[f"Bad_Vol_Ratio_{window}"] = downside / (upside + EPS)
        out[f"RV_Per_Bar_{window}"] = total / float(window)
        out[f"Negative_Return_Count_{window}"] = returns.lt(0).rolling(
            window, min_periods=window
        ).sum()
        out[f"Cumulative_Return_{window}"] = returns.rolling(
            window, min_periods=window
        ).sum()

    baseline = squared.shift(BASELINE_EXCLUDE_RECENT_BARS).rolling(
        BASELINE_WINDOW, min_periods=BASELINE_WINDOW
    ).mean()
    out["Baseline_RV_Per_Bar_100"] = baseline
    for window in (5, 10, 20):
        out[f"Vol_Acceleration_{window}_100"] = (
            out[f"RV_Per_Bar_{window}"] / (baseline + EPS)
        )

    out["Negative_Return_Fraction_20"] = out["Negative_Return_Count_20"] / 20.0
    out["Consecutive_Negative_Returns"] = returns.rolling(20, min_periods=1).apply(
        _consecutive_negative, raw=True
    )
    out["Largest_Negative_Return_20"] = returns.rolling(20, min_periods=20).min()
    out["Largest_Positive_Return_20"] = returns.rolling(20, min_periods=20).max()
    out["Largest_Negative_Variance_Share_20"] = (
        negative_sq.rolling(20, min_periods=20).max()
        / (out["Downside_Semivariance_20"] + EPS)
    )
    out["Current_Return_Share_Of_Downside_Variance"] = (
        negative_sq / (out["Downside_Semivariance_20"] + EPS)
    )
    bar_range = (high - low).clip(lower=EPS)
    body = close - open_
    out["Range_ATR"] = bar_range / (shifted_atr + EPS)
    out["Body_Direction"] = np.sign(body)
    out["Body_Share"] = body.abs() / (bar_range + EPS)
    out["Close_Location"] = (close - low) / (bar_range + EPS)
    event_ret = np.log((close + EPS) / (close.shift(EVENT_VOL_LOOKBACK_BARS) + EPS))
    out["Event_Vol"] = event_ret.ewm(
        span=EVENT_VOL_EWM_SPAN, adjust=False
    ).std().clip(lower=MIN_EVENT_VOL)
    out["Downside_Dominance"] = (
        out["Downside_Variance_Share_20"] >= DOWNSIDE_SHARE_THRESHOLD
    )
    out["Volatility_Accelerating"] = (
        out["Vol_Acceleration_20_100"] >= VOL_ACCELERATION_THRESHOLD
    )
    out["Negative_Event_Bar"] = returns < 0
    out["Cascade_Active_State"] = (
        out["Downside_Dominance"] & out["Volatility_Accelerating"]
    ).astype("int64")
    out["Raw_Cascade_State_Entry"] = (
        out["Cascade_Active_State"].eq(1)
        & out["Cascade_Active_State"].shift(1, fill_value=0).eq(0)
    ).astype("int64")
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def _extract_high_recall_impulse_events(features: pd.DataFrame) -> pd.DataFrame:
    diagnostics = {
        "bars_seen": int(len(features)),
        "raw_downside_dominance_bars": int(features["Downside_Dominance"].fillna(False).sum()),
        "raw_vol_acceleration_bars": int(features["Volatility_Accelerating"].fillna(False).sum()),
        "raw_joint_active_bars": int(features["Cascade_Active_State"].fillna(0).sum()),
        "negative_joint_active_bars": 0,
        "raw_state_entries": int(features["Raw_Cascade_State_Entry"].fillna(0).sum()),
        "initial_state_entry_events": 0,
        "renewed_impulse_events": 0,
        "events_emitted": 0,
        "min_gap_suppressed": 0,
    }
    rows: list[pd.Series] = []
    last_event_index = -10**9
    for index, row in features.iterrows():
        active = bool(row["Cascade_Active_State"] == 1)
        negative_bar = bool(row["Negative_Event_Bar"])
        if active and negative_bar:
            diagnostics["negative_joint_active_bars"] += 1
        if not (active and negative_bar):
            continue
        if int(index) - last_event_index < MIN_EVENT_GAP_BARS:
            diagnostics["min_gap_suppressed"] += 1
            continue
        event = row.copy()
        is_state_entry = int(row["Raw_Cascade_State_Entry"] == 1)
        event["Downside_Volatility_Cascade_Event"] = 1
        event["Cascade_State_Entry"] = is_state_entry
        event["Cascade_Renewed_Impulse"] = 1 - is_state_entry
        rows.append(event)
        last_event_index = int(index)
        diagnostics["initial_state_entry_events" if is_state_entry else "renewed_impulse_events"] += 1
        diagnostics["events_emitted"] += 1
    _LAST_STATE_MACHINE_DIAGNOSTICS.clear()
    _LAST_STATE_MACHINE_DIAGNOSTICS.update(diagnostics)
    if not rows:
        empty = features.iloc[[]].copy()
        empty.attrs["state_machine_diagnostics"] = diagnostics
        return empty
    out = pd.DataFrame(rows).reset_index(drop=True)
    out.attrs["state_machine_diagnostics"] = diagnostics
    return out


def _validate_candidate_events(events: pd.DataFrame) -> None:
    required = [
        "Datetime", "Bar_Start", "edge_id", "side", "Event_Vol", "event_id",
        "candidate_event_generator", "Downside_Volatility_Cascade_Event",
        "Cascade_State_Entry", "Cascade_Renewed_Impulse", "Cascade_Active_State",
        "Log_Return_1", "Negative_Event_Bar", "Downside_Variance_Share_20",
        "Vol_Acceleration_20_100", "event_known_after_close",
        "uses_future_information",
    ]
    missing = [column for column in required if column not in events.columns]
    if missing:
        raise CandidateEventError(f"Candidate events missing required columns: {missing}")
    if events.empty:
        raise CandidateEventError("Candidate generator produced zero cascade events")
    forbidden = sorted(_forbidden_event_columns().intersection(events.columns))
    if forbidden:
        raise CandidateEventError(f"Candidate events contain forbidden columns: {forbidden}")
    if not events["edge_id"].eq(EDGE_ID).all():
        raise CandidateEventError("Unexpected edge_id")
    if not pd.to_numeric(events["side"], errors="coerce").eq(RAW_SIDE_VALUE).all():
        raise CandidateEventError("Raw candidate side must remain zero")
    if not pd.to_numeric(events["Downside_Volatility_Cascade_Event"], errors="coerce").eq(1).all():
        raise CandidateEventError("All rows must be cascade events")
    state_entry = pd.to_numeric(events["Cascade_State_Entry"], errors="coerce")
    renewed = pd.to_numeric(events["Cascade_Renewed_Impulse"], errors="coerce")
    if not state_entry.isin([0, 1]).all() or not renewed.isin([0, 1]).all():
        raise CandidateEventError("Cascade event-type flags must be binary")
    if not (state_entry + renewed).eq(1).all():
        raise CandidateEventError(
            "Each cascade event must be exactly one of state entry or renewed impulse"
        )
    if not pd.to_numeric(events["Cascade_Active_State"], errors="coerce").eq(1).all():
        raise CandidateEventError("All events must occur inside the active cascade state")
    if not pd.to_numeric(events["Negative_Event_Bar"], errors="coerce").eq(1).all():
        raise CandidateEventError("All events must occur on a negative completed bar")
    if not pd.to_numeric(events["Downside_Variance_Share_20"], errors="coerce").ge(DOWNSIDE_SHARE_THRESHOLD).all():
        raise CandidateEventError("Downside variance share threshold violated")
    if not pd.to_numeric(events["Vol_Acceleration_20_100"], errors="coerce").ge(VOL_ACCELERATION_THRESHOLD).all():
        raise CandidateEventError("Volatility acceleration threshold violated")
    if not pd.to_numeric(events["Log_Return_1"], errors="coerce").lt(0).all():
        raise CandidateEventError("Event bar return must be negative")
    if events["uses_future_information"].astype(bool).any():
        raise CandidateEventError("Future information is forbidden")
    if not events["event_known_after_close"].astype(bool).all():
        raise CandidateEventError("Events are known only after close")


def generate_v2_downsidevolatilitycascade_short_candidate_events(
    features: pd.DataFrame,
    **_: Any,
) -> pd.DataFrame:
    expanded = add_downside_volatility_cascade_features(features)
    selected = _extract_high_recall_impulse_events(expanded)
    diagnostics = get_last_state_machine_diagnostics()
    if selected.empty:
        selected = pd.DataFrame(columns=["Datetime", "Bar_Start", *EVENT_CONTEXT_COLUMNS])
    keep = [
        "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
        *[column for column in EVENT_CONTEXT_COLUMNS if column in selected.columns],
    ]
    events = selected[[column for column in keep if column in selected.columns]].copy()
    events["edge_id"] = EDGE_ID
    events["side"] = RAW_SIDE_VALUE
    events["event_id"] = events["Datetime"].apply(
        lambda value: f"{EDGE_ID}|{pd.Timestamp(value).isoformat()}|cascade_impulse"
    )
    events["candidate_event_generator"] = CANDIDATE_EVENT_GENERATOR
    events["event_known_after_close"] = True
    events["uses_future_information"] = False
    events["semivariance_window"] = SEMIVARIANCE_WINDOW
    events["baseline_window"] = BASELINE_WINDOW
    events["baseline_exclude_recent_bars"] = BASELINE_EXCLUDE_RECENT_BARS
    events["downside_share_threshold"] = DOWNSIDE_SHARE_THRESHOLD
    events["vol_acceleration_threshold"] = VOL_ACCELERATION_THRESHOLD
    events["min_gap_bars"] = MIN_EVENT_GAP_BARS
    events["Datetime"] = pd.to_datetime(events["Datetime"], utc=True, errors="coerce")
    events["Bar_Start"] = pd.to_datetime(events["Bar_Start"], utc=True, errors="coerce")
    for column in [name for name in EVENT_CONTEXT_COLUMNS if name in events.columns]:
        events[column] = pd.to_numeric(events[column], errors="coerce")
    events = events.dropna(subset=[
        "Datetime", "Bar_Start", "Event_Vol", "Log_Return_1",
        "Downside_Variance_Share_20", "Vol_Acceleration_20_100",
    ]).sort_values("Datetime").reset_index(drop=True)
    events.attrs["state_machine_diagnostics"] = diagnostics
    _validate_candidate_events(events)
    return events


def generate_v1_downsidevolatilitycascade_short_candidate_events(
    features: pd.DataFrame, **kwargs: Any,
) -> pd.DataFrame:
    return generate_v2_downsidevolatilitycascade_short_candidate_events(features, **kwargs)


def validate_candidate_events(events: pd.DataFrame) -> None:
    _validate_candidate_events(events)


def candidate_event_contract() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "raw_side_value": RAW_SIDE_VALUE,
        "final_side_value": SIDE_VALUE,
        "candidate_event_generator": CANDIDATE_EVENT_GENERATOR,
        "semivariance_window": SEMIVARIANCE_WINDOW,
        "baseline_window": BASELINE_WINDOW,
        "baseline_exclude_recent_bars": BASELINE_EXCLUDE_RECENT_BARS,
        "downside_share_threshold": DOWNSIDE_SHARE_THRESHOLD,
        "vol_acceleration_threshold": VOL_ACCELERATION_THRESHOLD,
        "current_return_must_be_negative": True,
        "sampling_mode": "spaced_negative_impulses_inside_active_cascade",
        "min_event_gap_bars": MIN_EVENT_GAP_BARS,
        "ffd_and_regime_are_context_not_hard_filters": True,
        "uses_future_information": False,
    }


if __name__ == "__main__":
    print("✅ NASDAQ/downsidevolatilitycascade_short candidate_events.py import check passed")
    print(candidate_event_contract())
