"""Research model rows for NASDAQ/downsidevolatilitycascade_short.

Selected grid-scan geometry: 1000 bars, 1.5R profit target and 1R stop.
The selected cell produced 2,953 rows, net/gross mean R of 0.03285,
zero vertical exits and a 27-bar median holding period. Same-bar barrier
ambiguity is resolved from raw NASDAQ MID ticks where available and otherwise
falls back to sl_first. Labels remain research-only.
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402

EDGE_ID = "NASDAQ/downsidevolatilitycascade_short"
ASSET = "NASDAQ"
SIDE_VALUE = -1
DOWNSIDE_SHARE_THRESHOLD = 0.60
VOL_ACCELERATION_THRESHOLD = 1.10
VERTICAL_HORIZON = 1000
PT_MULTIPLIER = 1.5
SL_MULTIPLIER = 1.0
FALLBACK_TIE_POLICY = "sl_first"
TICK_RESOLUTION_PRICE = "mid"
ROW_BUILDER = "downsidevolatilitycascade_short_v3_1p5r_1000_triple_barrier_model_rows"
PRIMARY_SIDE_RULE = (
    "negative completed-bar impulses inside downside variance share >=0.60 and "
    "non-overlapping volatility acceleration >=1.10 map to side=-1 short; "
    "both initial state entries and spaced renewed impulses are valid"
)
GEOMETRY_STATUS = "selected_from_grid_scan"
GRID_SCAN_SELECTED_RANK = 2
GRID_SCAN_SELECTED_ROWS = 2953
GRID_SCAN_SELECTED_NET_MEAN_R = 0.03285
GRID_SCAN_SELECTED_GROSS_MEAN_R = 0.03285
GRID_SCAN_SELECTED_VERTICAL_RATE = 0.0
GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS = 27.0
EPS = 1e-12


class ModelRowsError(RuntimeError):
    pass


def _normalize_datetime(df: pd.DataFrame, column: str) -> pd.DataFrame:
    out = df.copy()
    out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out.dropna(subset=[column]).copy()


def _parse_tick_range(name: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", name)
    if not match:
        return None
    return pd.Timestamp(match.group(1), tz="UTC"), pd.Timestamp(match.group(2), tz="UTC")


def build_tick_file_catalog(asset: str = ASSET) -> list[dict[str, Any]]:
    folder = get_asset_layout(asset).tick_root
    if not folder.exists():
        raise ModelRowsError(f"Tick data folder not found: {folder}")
    broad_start = pd.Timestamp("1900-01-01", tz="UTC")
    broad_end = pd.Timestamp("2100-01-01", tz="UTC")
    catalog: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.csv")):
        start, end = _parse_tick_range(path.name) or (broad_start, broad_end)
        catalog.append({"path": path, "start": start, "end": end})
    if not catalog:
        raise ModelRowsError(f"No raw tick CSV files found under: {folder}")
    return catalog


def _find_column(columns: list[str], candidates: list[str]) -> str | None:
    lowered = {column.strip().lower(): column for column in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


@lru_cache(maxsize=256)
def _load_tick_file(path_text: str) -> pd.DataFrame:
    path = Path(path_text)
    raw = pd.read_csv(path)
    timestamp = _find_column(list(raw.columns), ["timestamp", "time", "datetime", "date_time"])
    bid = _find_column(list(raw.columns), ["bidprice", "bid_price", "bid"])
    ask = _find_column(list(raw.columns), ["askprice", "ask_price", "ask"])
    if timestamp is None or bid is None or ask is None:
        raise ModelRowsError(f"Tick file {path.name} lacks timestamp/bid/ask columns")
    out = pd.DataFrame({
        "timestamp": pd.to_datetime(raw[timestamp], utc=True, errors="coerce"),
        "bid": pd.to_numeric(raw[bid], errors="coerce"),
        "ask": pd.to_numeric(raw[ask], errors="coerce"),
    }).dropna()
    out = out[(out["bid"] > 0) & (out["ask"] > 0)].copy()
    out["mid"] = (out["bid"] + out["ask"]) / 2.0
    return out.sort_values("timestamp").reset_index(drop=True)


def _ticks_for_interval(
    catalog: list[dict[str, Any]], start: pd.Timestamp, end: pd.Timestamp,
) -> pd.DataFrame:
    start_day = pd.Timestamp(start.date(), tz="UTC")
    end_day = pd.Timestamp(end.date(), tz="UTC")
    chunks: list[pd.DataFrame] = []
    for item in catalog:
        if item["end"] < start_day or item["start"] > end_day:
            continue
        ticks = _load_tick_file(str(item["path"]))
        mask = ticks["timestamp"].between(start, end)
        if mask.any():
            chunks.append(ticks.loc[mask, ["timestamp", "mid"]])
    if not chunks:
        return pd.DataFrame(columns=["timestamp", "mid"])
    return pd.concat(chunks, ignore_index=True).sort_values("timestamp")


def _fallback_tie() -> tuple[float, str | None]:
    if FALLBACK_TIE_POLICY == "sl_first":
        return 0.0, "sl"
    if FALLBACK_TIE_POLICY == "pt_first":
        return 1.0, "pt"
    if FALLBACK_TIE_POLICY == "drop":
        return np.nan, None
    raise ModelRowsError(f"Unknown tie policy: {FALLBACK_TIE_POLICY}")


def _resolve_tick_first_touch(
    catalog: list[dict[str, Any]], start: pd.Timestamp, end: pd.Timestamp,
    pt_price: float, sl_price: float,
) -> tuple[float, str | None, str]:
    ticks = _ticks_for_interval(catalog, start, end)
    if ticks.empty:
        label, hit = _fallback_tie()
        return label, hit, "fallback_no_ticks"
    for price in ticks["mid"].to_numpy(dtype="float64"):
        if price <= pt_price:
            return 1.0, "pt", "tick"
        if price >= sl_price:
            return 0.0, "sl", "tick"
    label, hit = _fallback_tie()
    return label, hit, "fallback_unresolved"


def _validate_events(events: pd.DataFrame) -> None:
    required = [
        "side", "Event_Vol", "Downside_Volatility_Cascade_Event",
        "Cascade_State_Entry", "Cascade_Renewed_Impulse",
        "Cascade_Active_State", "Log_Return_1", "Negative_Event_Bar",
        "Downside_Variance_Share_20", "Vol_Acceleration_20_100",
    ]
    missing = [column for column in required if column not in events.columns]
    if missing:
        raise ModelRowsError(f"Events missing cascade fields: {missing}")
    if events.empty:
        raise ModelRowsError("downsidevolatilitycascade_short events are empty")
    if "edge_id" in events.columns and not events["edge_id"].eq(EDGE_ID).all():
        raise ModelRowsError("Events contain wrong edge_id")
    if not pd.to_numeric(events["side"], errors="coerce").eq(SIDE_VALUE).all():
        raise ModelRowsError("Events must have side=-1")
    for column in [
        "Downside_Volatility_Cascade_Event", "Cascade_Active_State",
        "Negative_Event_Bar",
    ]:
        if not pd.to_numeric(events[column], errors="coerce").eq(1).all():
            raise ModelRowsError(f"Events must satisfy {column}=1")
    state_entry = pd.to_numeric(events["Cascade_State_Entry"], errors="coerce")
    renewed = pd.to_numeric(events["Cascade_Renewed_Impulse"], errors="coerce")
    if not state_entry.isin([0, 1]).all() or not renewed.isin([0, 1]).all():
        raise ModelRowsError("Cascade event-type flags must be binary")
    if not (state_entry + renewed).eq(1).all():
        raise ModelRowsError(
            "Each event must be exactly one of state entry or renewed impulse"
        )
    if not pd.to_numeric(events["Log_Return_1"], errors="coerce").lt(0).all():
        raise ModelRowsError("Event-bar return must be negative")
    if not pd.to_numeric(events["Downside_Variance_Share_20"], errors="coerce").ge(DOWNSIDE_SHARE_THRESHOLD).all():
        raise ModelRowsError("Downside variance share threshold violated")
    if not pd.to_numeric(events["Vol_Acceleration_20_100"], errors="coerce").ge(VOL_ACCELERATION_THRESHOLD).all():
        raise ModelRowsError("Volatility acceleration threshold violated")
    event_vol = pd.to_numeric(events["Event_Vol"], errors="coerce")
    if event_vol.isna().any() or not event_vol.gt(0).all():
        raise ModelRowsError("Invalid Event_Vol")


def _barriers(entry: float, target: float) -> tuple[float, float]:
    return (
        entry * (1.0 - PT_MULTIPLIER * target),
        entry * (1.0 + SL_MULTIPLIER * target),
    )


def apply_triple_barrier_labels(
    bars: pd.DataFrame,
    events: pd.DataFrame,
    *,
    tick_catalog: list[dict[str, Any]] | None = None,
    vertical_horizon: int = VERTICAL_HORIZON,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if vertical_horizon <= 0:
        raise ModelRowsError("vertical_horizon must be positive")
    _validate_events(events)
    required_bars = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    missing = [column for column in required_bars if column not in bars.columns]
    if missing:
        raise ModelRowsError(f"Bars missing required columns: {missing}")
    full = _normalize_datetime(bars, "Datetime")
    full["Bar_Start"] = pd.to_datetime(full["Bar_Start"], utc=True, errors="coerce")
    for column in ["Open", "High", "Low", "Close"]:
        full[column] = pd.to_numeric(full[column], errors="coerce")
    full = full.dropna(subset=required_bars).sort_values("Datetime").drop_duplicates("Datetime").set_index("Datetime")
    event_frame = _normalize_datetime(events, "Datetime").sort_values("Datetime").drop_duplicates("Datetime").set_index("Datetime")
    catalog = build_tick_file_catalog() if tick_catalog is None else tick_catalog
    index = full.index
    rows: list[dict[str, Any]] = []
    stats = {
        "events_seen": 0, "events_labeled": 0,
        "events_dropped_missing_timestamp": 0, "events_dropped_horizon": 0,
        "events_dropped_invalid_target": 0, "ambiguous_same_bar_count": 0,
        "tick_resolved_count": 0, "fallback_resolved_count": 0,
    }
    for event_time, event in event_frame.iterrows():
        stats["events_seen"] += 1
        if event_time not in full.index:
            stats["events_dropped_missing_timestamp"] += 1
            continue
        location = index.get_loc(event_time)
        if not isinstance(location, (int, np.integer)):
            stats["events_dropped_missing_timestamp"] += 1
            continue
        if location + vertical_horizon >= len(full):
            stats["events_dropped_horizon"] += 1
            continue
        target = float(event["Event_Vol"])
        entry = float(full.at[event_time, "Close"])
        if not np.isfinite(target) or target <= 0 or not np.isfinite(entry):
            stats["events_dropped_invalid_target"] += 1
            continue
        pt_price, sl_price = _barriers(entry, target)
        future = index[location + 1: location + vertical_horizon + 1]
        label, t1, hit, source = 0.0, future[-1], "vertical", "vertical"
        for timestamp in future:
            pt_touch = float(full.at[timestamp, "Low"]) <= pt_price
            sl_touch = float(full.at[timestamp, "High"]) >= sl_price
            if pt_touch and sl_touch:
                stats["ambiguous_same_bar_count"] += 1
                label, hit, source = _resolve_tick_first_touch(
                    catalog, pd.Timestamp(full.at[timestamp, "Bar_Start"]),
                    pd.Timestamp(timestamp), pt_price, sl_price,
                )
                stats["tick_resolved_count" if source == "tick" else "fallback_resolved_count"] += 1
                t1 = timestamp
                break
            if pt_touch:
                label, hit, source, t1 = 1.0, "pt", "ohlc", timestamp
                break
            if sl_touch:
                label, hit, source, t1 = 0.0, "sl", "ohlc", timestamp
                break
        if pd.isna(label):
            continue
        if hit == "pt":
            realized_r = PT_MULTIPLIER / SL_MULTIPLIER
        elif hit == "sl":
            realized_r = -1.0
        else:
            exit_close = float(full.at[t1, "Close"])
            realized_r = SIDE_VALUE * ((exit_close / entry) - 1.0) / (SL_MULTIPLIER * target + EPS)
        record = event.to_dict()
        record.update({
            "Datetime": event_time, "meta_label": int(label), "t1": t1,
            "barrier_hit": hit, "resolution_source": source,
            "entry_price": entry, "pt_price": pt_price,
            "sl_price": sl_price, "realized_r": realized_r,
        })
        rows.append(record)
        stats["events_labeled"] += 1
    labeled = pd.DataFrame(rows)
    if labeled.empty:
        raise ModelRowsError("Triple-barrier labeling produced zero rows")
    labeled["Datetime"] = pd.to_datetime(labeled["Datetime"], utc=True)
    labeled["t1"] = pd.to_datetime(labeled["t1"], utc=True)
    labeled["side"] = SIDE_VALUE
    if not labeled["pt_price"].lt(labeled["entry_price"]).all():
        raise ModelRowsError("Short PT must be below entry")
    if not labeled["sl_price"].gt(labeled["entry_price"]).all():
        raise ModelRowsError("Short SL must be above entry")
    return labeled.sort_values("Datetime").reset_index(drop=True), stats


def build_event_model_rows(
    *,
    features: pd.DataFrame,
    events: pd.DataFrame,
    bars: pd.DataFrame,
    feature_names: list[str],
    vertical_horizon: int = VERTICAL_HORIZON,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    labeled, stats = apply_triple_barrier_labels(bars, events, vertical_horizon=vertical_horizon)
    feature_frame = _normalize_datetime(features, "Datetime")
    missing = [name for name in feature_names if name not in feature_frame.columns]
    if missing:
        raise ModelRowsError(f"Feature table missing schema features: {missing}")
    feature_columns = list(dict.fromkeys([
        column for column in [
            "Datetime", "Bar_Start", "Open", "High", "Low", "Close", "edge_id", *feature_names
        ] if column in feature_frame.columns
    ]))
    context_columns = [
        "Datetime", "side", "Event_Vol", "meta_label", "t1", "barrier_hit",
        "resolution_source", "entry_price", "pt_price", "sl_price", "realized_r",
        "event_id", "candidate_event_generator", "Downside_Volatility_Cascade_Event",
        "Cascade_State_Entry", "Cascade_Renewed_Impulse", "Cascade_Active_State",
        "Negative_Event_Bar", "Downside_Variance_Share_20",
        "Vol_Acceleration_20_100", "Bad_Vol_Ratio_20",
        "Largest_Negative_Variance_Share_20",
        "Current_Return_Share_Of_Downside_Variance", "primary_side",
        "primary_side_filter", "primary_side_rule",
    ]
    context_columns = [column for column in context_columns if column in labeled.columns]
    overlap = sorted((set(feature_columns) & set(context_columns)) - {"Datetime"})
    if overlap:
        raise ModelRowsError(
            f"Feature/event merge would create suffixed duplicate columns: {overlap}"
        )
    modeling = feature_frame[feature_columns].merge(
        labeled[context_columns], on="Datetime", how="inner", validate="one_to_one"
    )
    before_required = len(modeling)
    modeling["t1"] = pd.to_datetime(modeling["t1"], utc=True, errors="coerce")
    modeling = modeling.dropna(subset=["side", "Event_Vol", "meta_label", "t1"])
    after_required = len(modeling)
    before_features = len(modeling)
    modeling = modeling.dropna(subset=feature_names).copy()
    if modeling.empty:
        raise ModelRowsError("All model rows were dropped after feature filtering")
    modeling["side"] = modeling["side"].astype("int64")
    modeling["meta_label"] = modeling["meta_label"].astype("int64")
    modeling = modeling.sort_values("Datetime").reset_index(drop=True)
    manifest = {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "primary_side_rule": PRIMARY_SIDE_RULE,
        "row_builder": ROW_BUILDER,
        "geometry_status": GEOMETRY_STATUS,
        "geometry_selection_source": "label_geometry_grid_scan",
        "grid_scan_selected_rank": GRID_SCAN_SELECTED_RANK,
        "grid_scan_selected_rows": GRID_SCAN_SELECTED_ROWS,
        "grid_scan_selected_net_mean_R": GRID_SCAN_SELECTED_NET_MEAN_R,
        "grid_scan_selected_gross_mean_R": GRID_SCAN_SELECTED_GROSS_MEAN_R,
        "grid_scan_selected_vertical_rate": GRID_SCAN_SELECTED_VERTICAL_RATE,
        "grid_scan_selected_median_hold_bars": GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS,
        "rows_candidate_events_input": int(len(events)),
        "rows_labeled_events": int(len(labeled)),
        "rows_after_required_filter": int(after_required),
        "rows_after_feature_nan_filter": int(len(modeling)),
        "required_rows_dropped": int(before_required - after_required),
        "feature_nan_rows_dropped": int(before_features - len(modeling)),
        "feature_count": int(len(feature_names)),
        "vertical_horizon": int(vertical_horizon),
        "pt_multiplier": PT_MULTIPLIER,
        "sl_multiplier": SL_MULTIPLIER,
        "reward_risk_ratio": PT_MULTIPLIER / SL_MULTIPLIER,
        "fallback_tie_policy": FALLBACK_TIE_POLICY,
        "tick_resolution_price": TICK_RESOLUTION_PRICE,
        "short_barrier_contract": "pt_price_below_entry_sl_price_above_entry",
        "labeling_stats": stats,
        "meta_label_distribution": {
            str(key): float(value)
            for key, value in modeling["meta_label"].value_counts(normalize=True).sort_index().items()
        },
        "uses_future_information_for_labels": True,
        "live_scoring_must_not_use_label_columns": True,
    }
    return modeling, manifest


def model_row_config() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "primary_side_rule": PRIMARY_SIDE_RULE,
        "row_builder": ROW_BUILDER,
        "vertical_horizon": VERTICAL_HORIZON,
        "pt_multiplier": PT_MULTIPLIER,
        "sl_multiplier": SL_MULTIPLIER,
        "reward_risk_ratio": PT_MULTIPLIER / SL_MULTIPLIER,
        "geometry_status": GEOMETRY_STATUS,
        "geometry_selection_source": "label_geometry_grid_scan",
        "grid_scan_selected_rank": GRID_SCAN_SELECTED_RANK,
        "grid_scan_selected_rows": GRID_SCAN_SELECTED_ROWS,
        "grid_scan_selected_net_mean_R": GRID_SCAN_SELECTED_NET_MEAN_R,
        "grid_scan_selected_gross_mean_R": GRID_SCAN_SELECTED_GROSS_MEAN_R,
        "grid_scan_selected_vertical_rate": GRID_SCAN_SELECTED_VERTICAL_RATE,
        "grid_scan_selected_median_hold_bars": GRID_SCAN_SELECTED_MEDIAN_HOLD_BARS,
        "fallback_tie_policy": FALLBACK_TIE_POLICY,
        "tick_resolution_price": TICK_RESOLUTION_PRICE,
        "short_barrier_contract": "pt_price_below_entry_sl_price_above_entry",
        "uses_future_information_for_labels": True,
        "live_scoring_must_not_use_label_columns": True,
    }


if __name__ == "__main__":
    print("✅ NASDAQ/downsidevolatilitycascade_short model_rows.py import check passed")
    print(model_row_config())
