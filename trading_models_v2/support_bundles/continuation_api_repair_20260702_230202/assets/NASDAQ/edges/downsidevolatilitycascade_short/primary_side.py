"""Primary-side mapping for NASDAQ/downsidevolatilitycascade_short.

The candidate generator emits side-pending cascade impulses. This module
validates the signed-semivariance state and accepts either an initial state-entry
impulse or a causally spaced renewed impulse, then maps valid rows to side=-1.
FFD, HMM, trend, candle geometry and jump concentration remain context.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: E402

EDGE_ID = "NASDAQ/downsidevolatilitycascade_short"
ASSET = "NASDAQ"
SIDE_VALUE = -1
RAW_SIDE_VALUE = 0
DOWNSIDE_SHARE_THRESHOLD = 0.60
VOL_ACCELERATION_THRESHOLD = 1.10
LIVE_BUFFER_BARS = 300
PRIMARY_SIDE_RULE = (
    "negative completed-bar impulses inside downside variance share >=0.60 and "
    "non-overlapping volatility acceleration >=1.10 map to side=-1 short; "
    "both initial state entries and spaced renewed impulses are valid"
)
FORBIDDEN_PRIMARY_SIDE_COLUMNS = {
    "meta_label", "label", "target", "t1", "barrier_hit",
    "resolution_source", "entry_price", "pt_price", "sl_price",
    "realized_r", "raw_probability", "calibrated_probability",
    "afml_bet_size", "approved_signal", "selected_threshold",
    "Ticks", "Proxy_Value", "Tick_Activity_Ratio", "Proxy_Value_Ratio",
    "Activity_Impulse",
}


class PrimarySideError(RuntimeError):
    pass


def load_verified_ffd_features(asset: str = ASSET) -> pd.DataFrame:
    verification = verify_asset_ffd_manifest(asset)
    path = _V2_ROOT / verification["features"]
    if not path.exists():
        raise PrimarySideError(f"Verified FFD features path does not exist: {path}")
    frame = pd.read_csv(path)
    required = ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise PrimarySideError(f"FFD features missing required columns: {missing}")
    frame["Datetime"] = pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required).sort_values("Datetime").reset_index(drop=True)
    if frame.empty:
        raise PrimarySideError("Verified FFD features are empty")
    return frame


def _validate_primary_side_input(events: pd.DataFrame) -> pd.DataFrame:
    required = [
        "Datetime", "edge_id", "side", "Event_Vol",
        "Downside_Volatility_Cascade_Event", "Cascade_State_Entry",
        "Cascade_Renewed_Impulse", "Cascade_Active_State", "Log_Return_1",
        "Negative_Event_Bar", "Downside_Variance_Share_20",
        "Vol_Acceleration_20_100",
    ]
    missing = [column for column in required if column not in events.columns]
    if missing:
        raise PrimarySideError(f"Primary-side input missing fields: {missing}")
    forbidden = sorted(FORBIDDEN_PRIMARY_SIDE_COLUMNS.intersection(events.columns))
    if forbidden:
        raise PrimarySideError(f"Primary-side input contains forbidden fields: {forbidden}")
    out = events.copy()
    out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
    if "Bar_Start" in out.columns:
        out["Bar_Start"] = pd.to_datetime(out["Bar_Start"], utc=True, errors="coerce")
    numeric = [
        "side", "Event_Vol", "Downside_Volatility_Cascade_Event",
        "Cascade_State_Entry", "Cascade_Renewed_Impulse",
        "Cascade_Active_State", "Log_Return_1", "Negative_Event_Bar",
        "Downside_Variance_Share_20", "Vol_Acceleration_20_100",
    ]
    for column in numeric:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=required).sort_values("Datetime").reset_index(drop=True)
    if out.empty:
        raise PrimarySideError("Primary-side input is empty")
    if not out["edge_id"].eq(EDGE_ID).all():
        raise PrimarySideError("Primary side received the wrong edge_id")
    if not out["side"].isin([RAW_SIDE_VALUE, SIDE_VALUE]).all():
        raise PrimarySideError("Input side must be raw 0 or mapped -1")
    state_entry = out["Cascade_State_Entry"]
    renewed = out["Cascade_Renewed_Impulse"]
    if not state_entry.isin([0, 1]).all() or not renewed.isin([0, 1]).all():
        raise PrimarySideError("Cascade event-type flags must be binary")
    if not (state_entry + renewed).eq(1).all():
        raise PrimarySideError(
            "Each event must be exactly one of state entry or renewed impulse"
        )
    return out


def assign_downsidevolatilitycascade_short_primary_side(
    events: pd.DataFrame,
) -> pd.DataFrame:
    out = _validate_primary_side_input(events)
    valid = (
        out["Downside_Volatility_Cascade_Event"].eq(1)
        & out["Cascade_Active_State"].eq(1)
        & out["Negative_Event_Bar"].eq(1)
        & out["Log_Return_1"].lt(0)
        & out["Downside_Variance_Share_20"].ge(DOWNSIDE_SHARE_THRESHOLD)
        & out["Vol_Acceleration_20_100"].ge(VOL_ACCELERATION_THRESHOLD)
        & out["Event_Vol"].gt(0)
        & (out["Cascade_State_Entry"] + out["Cascade_Renewed_Impulse"]).eq(1)
    )
    out["primary_side_rule"] = PRIMARY_SIDE_RULE
    out["primary_side"] = 0
    out.loc[valid, "primary_side"] = SIDE_VALUE
    out["primary_side"] = out["primary_side"].astype("int64")
    out["primary_side_filter"] = out["primary_side"].eq(SIDE_VALUE).astype("int64")
    out = out[out["primary_side_filter"].eq(1)].copy()
    if out.empty:
        raise PrimarySideError("No events survived cascade primary-side mapping")
    out["side"] = SIDE_VALUE
    return out.sort_values("Datetime").reset_index(drop=True)


def filter_short_events_with_primary_side(
    events: pd.DataFrame,
    features: pd.DataFrame | None = None,
) -> pd.DataFrame:
    del features
    return assign_downsidevolatilitycascade_short_primary_side(events)


def validate_primary_side_events(events: pd.DataFrame) -> None:
    if assign_downsidevolatilitycascade_short_primary_side(events).empty:
        raise PrimarySideError("Validated primary-side output is empty")


def primary_side_config() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "asset": ASSET,
        "raw_side_value": RAW_SIDE_VALUE,
        "side_value": SIDE_VALUE,
        "side_rule": PRIMARY_SIDE_RULE,
        "downside_share_threshold": DOWNSIDE_SHARE_THRESHOLD,
        "vol_acceleration_threshold": VOL_ACCELERATION_THRESHOLD,
        "current_return_must_be_negative": True,
        "accepts_initial_state_entries": True,
        "accepts_spaced_renewed_impulses": True,
        "uses_verified_shared_ffd": True,
        "live_buffer_bars": LIVE_BUFFER_BARS,
        "price_only_event_gate": True,
        "uses_ffd_as_hard_filter": False,
        "uses_regime_as_hard_filter": False,
        "uses_trend_as_hard_filter": False,
        "uses_candle_geometry_as_hard_filter": False,
        "uses_jump_concentration_as_hard_filter": False,
        "uses_future_information": False,
        "contains_labels": False,
        "contains_probabilities": False,
        "contains_execution_decisions": False,
    }


if __name__ == "__main__":
    print("✅ NASDAQ/downsidevolatilitycascade_short primary_side.py import check passed")
    print(primary_side_config())
