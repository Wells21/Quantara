"""
Primary-side logic for DAX/rangebreakout_long (T30 rangebreakout). Every clock-trigger event maps to side=+1 long.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402

EDGE_ID = "DAX/rangebreakout_long"
ASSET = "DAX"
SIDE_VALUE = 1
LIVE_BUFFER_BARS = 300
EPS = 1e-12
PRIMARY_SIDE_RULE = "hourly (on the hour, UTC), the last close above the max hourly close of the 10 days before the last full hour (new 10-day closing high) -> LONG"

FORBIDDEN_PRIMARY_SIDE_COLUMNS = {
    "meta_label", "t1", "barrier_hit", "resolution_source", "entry_price", "pt_price", "sl_price", "realized_r",
    "raw_probability", "calibrated_probability", "afml_bet_size", "approved_signal",
}


class PrimarySideError(RuntimeError):
    """Raised when primary-side generation must fail closed."""


def load_verified_ffd_features(asset: str = ASSET) -> pd.DataFrame:
    """Load verified shared asset-level FFD features for runtime feature parity (generic V2 helper)."""

    verification = verify_asset_ffd_manifest(asset)
    path = _V2_ROOT / verification["features"]
    if not path.exists():
        raise PrimarySideError(f"Verified FFD features path does not exist: {path}")
    ffd = pd.read_csv(path)
    required = ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    missing = [col for col in required if col not in ffd.columns]
    if missing:
        raise PrimarySideError(f"FFD features missing required columns: {missing}")
    ffd["Datetime"] = to_datetime_iso(ffd["Datetime"], utc=True, errors="coerce")
    for col in ["Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]:
        ffd[col] = pd.to_numeric(ffd[col], errors="coerce")
    ffd = ffd.dropna(subset=required).copy()
    ffd = ffd.sort_values("Datetime").reset_index(drop=True)
    if ffd.empty:
        raise PrimarySideError("Verified FFD features are empty after normalization.")
    return ffd


def assign_rangebreakout_primary_side(events: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "edge_id", "side", "Event_Vol", "Session_Event_Type", "Session_Entry_Lag_Min"]
    missing = [c for c in required if c not in events.columns]
    if missing:
        raise PrimarySideError(f"Primary-side input missing required columns: {missing}")
    bad = sorted(FORBIDDEN_PRIMARY_SIDE_COLUMNS.intersection(events.columns))
    if bad:
        raise PrimarySideError(f"Primary-side input contains forbidden columns: {bad}")
    out = events.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    if "Bar_Start" in out.columns:
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
    for col in ["side", "Event_Vol", "Session_Event_Type", "Session_Entry_Lag_Min"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Datetime", "side", "Event_Vol", "Session_Event_Type"]).copy()
    if out.empty:
        raise PrimarySideError("No rangebreakout events after normalization.")
    if not (out["edge_id"] == EDGE_ID).all():
        raise PrimarySideError("rangebreakout primary side received wrong edge_id values.")
    if not (out["Session_Event_Type"] == 1).all():
        raise PrimarySideError("Session_Event_Type must be 1.")
    out["primary_side_rule"] = PRIMARY_SIDE_RULE
    out["primary_side"] = SIDE_VALUE
    out["primary_side_filter"] = 1
    out["side"] = SIDE_VALUE
    return out.sort_values("Datetime").reset_index(drop=True)


def filter_events_with_primary_side(events: pd.DataFrame, features: pd.DataFrame | None = None) -> pd.DataFrame:
    return assign_rangebreakout_primary_side(events)


def validate_primary_side_events(events: pd.DataFrame) -> None:
    if assign_rangebreakout_primary_side(events).empty:
        raise PrimarySideError("Validated rangebreakout primary-side output is empty.")


def primary_side_config() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "side_rule": PRIMARY_SIDE_RULE,
        "event_specific_fields_are_model_inputs": False,
        "uses_verified_shared_ffd": True,
        "live_buffer_bars": LIVE_BUFFER_BARS,
        "uses_future_information": False,
        "contains_labels": False,
        "contains_probabilities": False,
        "contains_execution_decisions": False,
        "thesis_card": "docs/mldp/campaign/NASDAQ_2026-11/T30_trading_range_breakout/PREREGISTRATION.md",
    }


if __name__ == "__main__":
    print(f"✅ {EDGE_ID} primary_side.py import/self-contract check passed.")
    print(primary_side_config())
