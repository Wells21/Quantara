"""
Primary-side logic for XAUUSD/roundlevelcascade_long.

T13 card (docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md): an up-break of an
untouched round level (Round_Break_Type = +1) maps to side=+1 long. Down-breaks failed Gate 0 and are dropped.
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

EDGE_ID = "XAUUSD/roundlevelcascade_long"
ASSET = "XAUUSD"
SIDE_VALUE = 1
LIVE_BUFFER_BARS = 300
EPS = 1e-12
PRIMARY_SIDE_RULE = "Round_Break_Type == +1 (first close above an untouched round level) -> side=+1 long"

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


def _validate_primary_side_input(events: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "edge_id", "side", "Event_Vol", "Round_Break_Type", "Round_Level"]
    missing = [col for col in required if col not in events.columns]
    if missing:
        raise PrimarySideError(f"Primary-side input missing required columns: {missing}")
    forbidden_present = sorted(FORBIDDEN_PRIMARY_SIDE_COLUMNS.intersection(events.columns))
    if forbidden_present:
        raise PrimarySideError(f"Primary-side input contains forbidden research/live-decision columns: {forbidden_present}")
    out = events.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    if "Bar_Start" in out.columns:
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
    for col in ["side", "Event_Vol", "Round_Break_Type", "Round_Level"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["Datetime", "side", "Event_Vol", "Round_Break_Type", "Round_Level"]).copy()
    if out.empty:
        raise PrimarySideError("Primary-side input is empty after normalization.")
    if not (out["edge_id"] == EDGE_ID).all():
        bad = out.loc[out["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise PrimarySideError(f"roundlevelcascade_long primary side received wrong edge_id values: {bad}")
    if not out["Round_Break_Type"].isin([-1, 1]).all():
        raise PrimarySideError("Round_Break_Type must be -1/+1 on raw events.")
    return out.sort_values("Datetime").reset_index(drop=True)


def assign_roundlevelcascade_long_primary_side(events: pd.DataFrame) -> pd.DataFrame:
    out = _validate_primary_side_input(events)
    out["primary_side_rule"] = PRIMARY_SIDE_RULE
    out["primary_side"] = np.where(out["Round_Break_Type"] == 1, SIDE_VALUE, 0).astype("int64")
    out["primary_side_filter"] = (out["primary_side"] == SIDE_VALUE).astype("int64")
    out = out[out["primary_side_filter"] == 1].copy()
    if out.empty:
        raise PrimarySideError("No roundlevelcascade_long events survived the up-break primary-side mapping.")
    if not (out["Close"] >= out["Round_Level"]).all() if "Close" in out.columns else False:
        raise PrimarySideError("roundlevelcascade_long events must close at or above the broken round level.")
    out["side"] = SIDE_VALUE
    return out.sort_values("Datetime").reset_index(drop=True)


def filter_long_events_with_primary_side(events: pd.DataFrame, features: pd.DataFrame | None = None) -> pd.DataFrame:
    return assign_roundlevelcascade_long_primary_side(events)


def validate_primary_side_events(events: pd.DataFrame) -> None:
    out = assign_roundlevelcascade_long_primary_side(events)
    if out.empty:
        raise PrimarySideError("Validated roundlevelcascade_long primary-side output is empty.")


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
        "thesis_card": "docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md",
    }


if __name__ == "__main__":
    print(f"✅ {EDGE_ID} primary_side.py import/self-contract check passed.")
    print(primary_side_config())
