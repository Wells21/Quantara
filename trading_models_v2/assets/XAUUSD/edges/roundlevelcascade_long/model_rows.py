"""
Model-row and research-label construction for XAUUSD/roundlevelcascade_long (T13 round-level stop cascade).

Geometry LOCKED 2026-11-09 at the Gate C winner H8_E150_V4_PT1_SL1 (8 h Event_Vol, EWM 150, 4 h vertical, 1:1). Same-bar PT/SL ambiguity uses raw MID-tick first touch where ticks exist, sl_first otherwise.
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
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.labels.calendar_time_barriers import calendar_time_vertical_deadline_positions  # noqa: E402

EDGE_ID = "XAUUSD/roundlevelcascade_long"
ASSET = "XAUUSD"
SIDE_VALUE = 1
VERTICAL_HORIZON_HOURS = 4.0  # LOCKED 2026-11-09 Gate C winner H8_E150_V4_PT1_SL1 (full history, era-scaled broker cost; PBO 0.14, DSR 0.992; 2022+ +0.094 R)
PT_MULTIPLIER = 1.0  # LOCKED 2026-11-09 Gate C winner H8_E150_V4_PT1_SL1 (full history, era-scaled broker cost; PBO 0.14, DSR 0.992; 2022+ +0.094 R)
SL_MULTIPLIER = 1.0  # LOCKED 2026-11-09 Gate C winner H8_E150_V4_PT1_SL1 (full history, era-scaled broker cost; PBO 0.14, DSR 0.992; 2022+ +0.094 R)
FALLBACK_TIE_POLICY = "sl_first"
TICK_RESOLUTION_PRICE = "mid"
EPS = 1e-12


class ModelRowsError(RuntimeError):
    """Raised when model-row building must fail closed."""


def _normalize_datetime_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    out = df.copy()
    out[col] = to_datetime_iso(out[col], utc=True, errors="coerce")
    return out.dropna(subset=[col]).copy()


def _parse_tick_file_range(filename: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})", filename)
    if not match:
        return None
    return pd.Timestamp(match.group(1), tz="UTC"), pd.Timestamp(match.group(2), tz="UTC")


def build_tick_file_catalog(asset: str = ASSET) -> list[dict[str, Any]]:
    layout = get_asset_layout(asset)
    folder = layout.tick_root
    if not folder.exists():
        raise ModelRowsError(f"Tick data folder not found: {folder}")
    catalog: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.csv")):
        parsed = _parse_tick_file_range(path.name)
        if parsed is None:
            continue
        catalog.append({"path": path, "filename": path.name, "start_date": parsed[0], "end_date": parsed[1]})
    if not catalog:
        raise ModelRowsError(f"No tick CSV files with expected date-range naming found under: {folder}")
    return sorted(catalog, key=lambda row: row["start_date"])


def _find_column(columns: list[str], candidates: list[str]) -> str | None:
    lowered = {col.strip().lower(): col for col in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


@lru_cache(maxsize=256)
def _load_tick_file(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    df = pd.read_csv(path)
    timestamp_col = _find_column(list(df.columns), ["timestamp", "time", "datetime", "date_time"])
    bid_col = _find_column(list(df.columns), ["bidprice", "bid_price", "bid"])
    ask_col = _find_column(list(df.columns), ["askprice", "ask_price", "ask"])
    missing = []
    if timestamp_col is None:
        missing.append("timestamp")
    if bid_col is None:
        missing.append("bid")
    if ask_col is None:
        missing.append("ask")
    if missing:
        raise ModelRowsError(f"Tick file {path.name} missing columns: {missing}")
    out = pd.DataFrame({
        "timestamp": to_datetime_iso(df[timestamp_col], utc=True, errors="coerce"),
        "bidprice": pd.to_numeric(df[bid_col], errors="coerce"),
        "askprice": pd.to_numeric(df[ask_col], errors="coerce"),
    })
    out = out.dropna(subset=["timestamp", "bidprice", "askprice"]).copy()
    out = out[(out["bidprice"] > 0) & (out["askprice"] > 0)].copy()
    out["mid_price"] = (out["bidprice"] + out["askprice"]) / 2.0
    return out.sort_values("timestamp").reset_index(drop=True)


def _ticks_for_interval(catalog: list[dict[str, Any]], start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    start_day = pd.Timestamp(start_ts.date(), tz="UTC")
    end_day = pd.Timestamp(end_ts.date(), tz="UTC")
    chunks = []
    for item in catalog:
        if item["end_date"] < start_day or item["start_date"] > end_day:
            continue
        ticks = _load_tick_file(str(item["path"]))
        mask = (ticks["timestamp"] >= start_ts) & (ticks["timestamp"] <= end_ts)
        if mask.any():
            chunks.append(ticks.loc[mask, ["timestamp", "mid_price"]].copy())
    if not chunks:
        return pd.DataFrame(columns=["timestamp", "mid_price"])
    return pd.concat(chunks, ignore_index=True).sort_values("timestamp").reset_index(drop=True)


def _resolve_fallback_tie() -> tuple[float, str | None]:
    if FALLBACK_TIE_POLICY == "sl_first":
        return 0.0, "sl"
    if FALLBACK_TIE_POLICY == "pt_first":
        return 1.0, "pt"
    if FALLBACK_TIE_POLICY == "drop":
        return np.nan, None
    raise ModelRowsError(f"Unknown fallback tie policy: {FALLBACK_TIE_POLICY}")


def _resolve_tick_first_touch(
    catalog: list[dict[str, Any]],
    bar_start_ts: pd.Timestamp,
    bar_end_ts: pd.Timestamp,
    pt_price: float,
    sl_price: float,
    side: int,
) -> tuple[float, str | None, str]:
    ticks = _ticks_for_interval(catalog, bar_start_ts, bar_end_ts)
    if ticks.empty:
        label, hit = _resolve_fallback_tie()
        return label, hit, "fallback_no_ticks"

    for px in ticks["mid_price"].to_numpy(dtype="float64"):
        if side == 1:
            if px >= pt_price:
                return 1.0, "pt", "tick"
            if px <= sl_price:
                return 0.0, "sl", "tick"
        elif side == -1:
            if px <= pt_price:
                return 1.0, "pt", "tick"
            if px >= sl_price:
                return 0.0, "sl", "tick"

    label, hit = _resolve_fallback_tie()
    return label, hit, "fallback_unresolved"


def _validate_roundlevelcascade_long_events(events: pd.DataFrame) -> None:
    if events.empty:
        raise ModelRowsError("roundlevelcascade_long events are empty.")
    required = ["Datetime", "side", "Event_Vol", "Round_Break_Type", "Round_Level"]
    missing = [col for col in required if col not in events.columns]
    if missing:
        raise ModelRowsError(f"roundlevelcascade_long events missing required fields: {missing}")
    if "edge_id" in events.columns and not (events["edge_id"] == EDGE_ID).all():
        bad = events.loc[events["edge_id"] != EDGE_ID, "edge_id"].unique().tolist()
        raise ModelRowsError(f"roundlevelcascade_long events contain wrong edge_id values: {bad}")
    side = pd.to_numeric(events["side"], errors="coerce")
    if not (side == SIDE_VALUE).all():
        bad = events.loc[side != SIDE_VALUE, "side"].unique().tolist()
        raise ModelRowsError(f"roundlevelcascade_long events contain non-long side values: {bad}")
    brk = pd.to_numeric(events["Round_Break_Type"], errors="coerce")
    if not (brk == 1).all():
        raise ModelRowsError("roundlevelcascade_long events must all be up-breaks (Round_Break_Type == +1).")


def _barrier_prices(entry_price: float, trgt: float, side: int) -> tuple[float, float]:
    if side == 1:
        return entry_price * (1.0 + PT_MULTIPLIER * trgt), entry_price * (1.0 - SL_MULTIPLIER * trgt)
    if side == -1:
        return entry_price * (1.0 - PT_MULTIPLIER * trgt), entry_price * (1.0 + SL_MULTIPLIER * trgt)
    raise ModelRowsError(f"Unsupported side value: {side}")


def _ohlc_touches(bar_high: float, bar_low: float, pt_price: float, sl_price: float, side: int) -> tuple[bool, bool]:
    if side == 1:
        return bar_high >= pt_price, bar_low <= sl_price
    if side == -1:
        return bar_low <= pt_price, bar_high >= sl_price
    raise ModelRowsError(f"Unsupported side value: {side}")


def apply_triple_barrier_labels(
    bars: pd.DataFrame,
    events: pd.DataFrame,
    *,
    tick_catalog: list[dict[str, Any]] | None = None,
    vertical_horizon_hours: float = VERTICAL_HORIZON_HOURS,
) -> tuple[pd.DataFrame, dict[str, int]]:
    if vertical_horizon_hours <= 0:
        raise ModelRowsError("vertical_horizon_hours must be > 0")
    required_bars = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    required_events = ["Datetime", "side", "Event_Vol"]
    missing_bars = [col for col in required_bars if col not in bars.columns]
    missing_events = [col for col in required_events if col not in events.columns]
    if missing_bars:
        raise ModelRowsError(f"Bars missing required columns: {missing_bars}")
    if missing_events:
        raise ModelRowsError(f"Events missing required columns: {missing_events}")

    _validate_roundlevelcascade_long_events(events)

    full_df = _normalize_datetime_col(bars, "Datetime")
    full_df["Bar_Start"] = to_datetime_iso(full_df["Bar_Start"], utc=True, errors="coerce")
    for col in ["Open", "High", "Low", "Close"]:
        full_df[col] = pd.to_numeric(full_df[col], errors="coerce")
    full_df = full_df.dropna(subset=required_bars).sort_values("Datetime").drop_duplicates("Datetime").set_index("Datetime")

    event_df = _normalize_datetime_col(events, "Datetime")
    event_df["side"] = pd.to_numeric(event_df["side"], errors="coerce")
    event_df["Event_Vol"] = pd.to_numeric(event_df["Event_Vol"], errors="coerce")
    event_df = event_df.dropna(subset=required_events).sort_values("Datetime").drop_duplicates("Datetime").set_index("Datetime")

    if tick_catalog is None:
        tick_catalog = build_tick_file_catalog(ASSET)

    highs, lows, closes, bar_starts = full_df["High"], full_df["Low"], full_df["Close"], full_df["Bar_Start"]
    index = full_df.index
    # QUANTARA_V3_ASI8_NS_FIX: DatetimeIndex.asi8 is not guaranteed nanoseconds
    # across pandas versions (venv39=ns, venv312=microseconds by default) --
    # normalize explicitly or deadlines compute far too wide on venv312.
    dt_ns = index.as_unit("ns").asi8
    rows = []
    stats = {
        "events_seen": 0,
        "events_labeled": 0,
        "events_dropped_missing_timestamp": 0,
        "events_dropped_horizon": 0,
        "events_dropped_invalid_target": 0,
        "ambiguous_same_bar_count": 0,
        "tick_resolved_count": 0,
        "fallback_resolved_count": 0,
    }

    for t, row in event_df.iterrows():
        stats["events_seen"] += 1
        base = row.to_dict()
        base["Datetime"] = t
        if t not in full_df.index:
            stats["events_dropped_missing_timestamp"] += 1
            continue
        loc = index.get_loc(t)
        if not isinstance(loc, (int, np.integer)):
            stats["events_dropped_missing_timestamp"] += 1
            continue
        deadline_pos = int(calendar_time_vertical_deadline_positions(
            dt_ns, np.array([loc]), hours=vertical_horizon_hours,
            )[0])
        if deadline_pos < 0 or deadline_pos >= len(full_df):
            stats["events_dropped_horizon"] += 1
            continue

        side = int(row["side"])
        trgt = float(row["Event_Vol"])
        start_price = float(closes.loc[t])
        if side != SIDE_VALUE or not np.isfinite(trgt) or trgt <= 0 or not np.isfinite(start_price):
            stats["events_dropped_invalid_target"] += 1
            continue

        pt_price, sl_price = _barrier_prices(start_price, trgt, side)
        future_idx = index[loc + 1: deadline_pos + 1]
        label = 0.0
        end_time = future_idx[-1]
        barrier_hit = "vertical"
        resolution_source = "vertical"

        for future_t in future_idx:
            pt_touched, sl_touched = _ohlc_touches(float(highs.loc[future_t]), float(lows.loc[future_t]), pt_price, sl_price, side)
            if pt_touched and sl_touched:
                stats["ambiguous_same_bar_count"] += 1
                label, barrier_hit, resolution_source = _resolve_tick_first_touch(
                    tick_catalog,
                    bar_starts.loc[future_t],
                    future_t,
                    pt_price,
                    sl_price,
                    side,
                )
                if resolution_source == "tick":
                    stats["tick_resolved_count"] += 1
                else:
                    stats["fallback_resolved_count"] += 1
                end_time = future_t
                break
            if pt_touched:
                label, barrier_hit, resolution_source, end_time = 1.0, "pt", "ohlc", future_t
                break
            if sl_touched:
                label, barrier_hit, resolution_source, end_time = 0.0, "sl", "ohlc", future_t
                break

        if pd.isna(label):
            continue
        if barrier_hit == "pt":
            realized_r = PT_MULTIPLIER / SL_MULTIPLIER
        elif barrier_hit == "sl":
            realized_r = -1.0
        else:
            end_close = float(closes.loc[end_time])
            realized_r = side * ((end_close / start_price) - 1.0) / (trgt + EPS)

        base.update({
            "meta_label": int(realized_r > 0.0),
            "pt_hit_label": int(barrier_hit == "pt"),
            "t1": end_time,
            "barrier_hit": barrier_hit,
            "resolution_source": resolution_source,
            "entry_price": start_price,
            "pt_price": pt_price,
            "sl_price": sl_price,
            "realized_r": realized_r,
        })
        rows.append(base)
        stats["events_labeled"] += 1

    labeled = pd.DataFrame(rows)
    if labeled.empty:
        raise ModelRowsError("Triple-barrier labeling produced zero rows.")
    labeled["Datetime"] = to_datetime_iso(labeled["Datetime"], utc=True)
    labeled["t1"] = to_datetime_iso(labeled["t1"], utc=True)
    labeled["meta_label"] = labeled["meta_label"].astype("int64")
    labeled["side"] = labeled["side"].astype("int64")

    if not (labeled["side"] == SIDE_VALUE).all():
        raise ModelRowsError("roundlevelcascade_long labeled rows must remain side=+1.")
    if not (pd.to_numeric(labeled["Round_Break_Type"], errors="coerce") == 1).all():
        raise ModelRowsError("roundlevelcascade_long labeled rows must keep Round_Break_Type == +1.")
    if not (labeled["pt_price"] > labeled["entry_price"]).all():
        raise ModelRowsError("roundlevelcascade_long PT must be above entry price.")
    if not (labeled["sl_price"] < labeled["entry_price"]).all():
        raise ModelRowsError("roundlevelcascade_long SL must be below entry price.")

    return labeled.sort_values("Datetime").reset_index(drop=True), stats


def _safe_label_columns(labeled_events: pd.DataFrame, feature_names: list[str], keep_cols: list[str]) -> list[str]:
    base_label_cols = [
        "Datetime", "side", "Event_Vol", "meta_label", "pt_hit_label", "t1", "barrier_hit", "resolution_source",
        "entry_price", "pt_price", "sl_price", "realized_r", "event_id", "candidate_event_generator",
        "Round_Break_Type", "Round_Level", "Round_Spacing", "Prior_24h_High", "Prior_24h_Low", "Break_Excess_Vol",
        "primary_side", "primary_side_filter", "primary_side_rule",
    ]
    protected = set(feature_names) | set(keep_cols)
    cols: list[str] = []
    for col in base_label_cols:
        if col == "Datetime":
            cols.append(col)
        elif col in protected:
            continue
        elif col in labeled_events.columns:
            cols.append(col)
    return list(dict.fromkeys(cols))


def build_event_model_rows(
    *,
    features: pd.DataFrame,
    events: pd.DataFrame,
    bars: pd.DataFrame,
    feature_names: list[str],
    vertical_horizon_hours: float = VERTICAL_HORIZON_HOURS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    labeled_events, stats = apply_triple_barrier_labels(bars, events, vertical_horizon_hours=vertical_horizon_hours)
    feat = _normalize_datetime_col(features, "Datetime")
    missing_features = [name for name in feature_names if name not in feat.columns]
    if missing_features:
        raise ModelRowsError(f"Feature table missing schema features: {missing_features}")

    keep_cols = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "edge_id", *feature_names]
    keep_cols = list(dict.fromkeys([col for col in keep_cols if col in feat.columns]))
    label_cols = _safe_label_columns(labeled_events, feature_names=feature_names, keep_cols=keep_cols)
    modeling = feat[keep_cols].merge(labeled_events[label_cols], on="Datetime", how="inner", validate="one_to_one")

    missing_after_merge = [name for name in feature_names if name not in modeling.columns]
    if missing_after_merge:
        raise ModelRowsError(f"Model row merge lost schema feature columns: {missing_after_merge}")

    before_required = len(modeling)
    modeling["t1"] = to_datetime_iso(modeling["t1"], utc=True, errors="coerce")
    modeling = modeling.dropna(subset=["side", "Event_Vol", "meta_label", "pt_hit_label", "t1"]).copy()
    after_required = len(modeling)
    before_feature_drop = len(modeling)
    modeling = modeling.dropna(subset=feature_names).copy()
    after_feature_drop = len(modeling)
    if modeling.empty:
        raise ModelRowsError("All model rows were dropped after feature NaN filtering.")

    modeling["side"] = modeling["side"].astype("int64")
    modeling["meta_label"] = modeling["meta_label"].astype("int64")
    modeling = modeling.sort_values("Datetime").reset_index(drop=True)

    if not (modeling["side"] == SIDE_VALUE).all():
        raise ModelRowsError("roundlevelcascade_long model rows must have side=+1 only.")
    if not (pd.to_numeric(modeling["Round_Break_Type"], errors="coerce") == 1).all():
        raise ModelRowsError("roundlevelcascade_long model rows must have Round_Break_Type == +1 only.")
    if not (modeling["pt_price"] > modeling["entry_price"]).all():
        raise ModelRowsError("roundlevelcascade_long model rows must have pt_price above entry_price.")
    if not (modeling["sl_price"] < modeling["entry_price"]).all():
        raise ModelRowsError("roundlevelcascade_long model rows must have sl_price below entry_price.")

    manifest = {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "primary_side_rule": "Round_Break_Type == +1 (first close above an untouched round level) -> side=+1 long",
        "row_builder": "roundlevelcascade_long_v1_1r_triple_barrier_model_rows",
        "rows_candidate_events_input": int(len(events)),
        "rows_labeled_events": int(len(labeled_events)),
        "rows_after_required_filter": int(after_required),
        "rows_after_feature_nan_filter": int(after_feature_drop),
        "required_rows_dropped": int(before_required - after_required),
        "feature_nan_rows_dropped": int(before_feature_drop - after_feature_drop),
        "feature_count": int(len(feature_names)),
        "vertical_horizon_hours": float(vertical_horizon_hours),
        "pt_multiplier": PT_MULTIPLIER,
        "sl_multiplier": SL_MULTIPLIER,
        "fallback_tie_policy": FALLBACK_TIE_POLICY,
        "tick_resolution_price": TICK_RESOLUTION_PRICE,
        "long_barrier_contract": "pt_price_above_entry_sl_price_below_entry",
        "labeling_stats": stats,
        "meta_label_distribution": {str(k): float(v) for k, v in modeling["meta_label"].value_counts(normalize=True).sort_index().to_dict().items()},
        "barrier_hit_distribution": {str(k): float(v) for k, v in modeling["barrier_hit"].value_counts(normalize=True).sort_index().to_dict().items()},
        "resolution_source_distribution": {str(k): float(v) for k, v in modeling["resolution_source"].value_counts(normalize=True).sort_index().to_dict().items()},
        "uses_future_information_for_labels": True,
        "live_scoring_must_not_use_label_columns": True,
        "notes": [
            "Labels are research/model-row outputs only, not live candidate-event fields.",
            "1R target is Event_Vol from the round-level break event row.",
            "Long side = first close above a round level untouched for 24 h (Round_Break_Type == +1).",
            "Same-bar PT/SL ambiguity is resolved with raw tick MID path where available, matching V1.",
        ],
    }
    return modeling, manifest


def model_row_config() -> dict[str, Any]:
    return {
        "edge_id": EDGE_ID,
        "side_value": SIDE_VALUE,
        "primary_side_rule": "Round_Break_Type == +1 (first close above an untouched round level) -> side=+1 long",
        "row_builder": "roundlevelcascade_long_v1_1r_triple_barrier_model_rows",
        "vertical_horizon_hours": VERTICAL_HORIZON_HOURS,
        "pt_multiplier": PT_MULTIPLIER,
        "sl_multiplier": SL_MULTIPLIER,
        "fallback_tie_policy": FALLBACK_TIE_POLICY,
        "tick_resolution_price": TICK_RESOLUTION_PRICE,
        "long_barrier_contract": "pt_price_above_entry_sl_price_below_entry",
        "uses_future_information_for_labels": True,
        "live_scoring_must_not_use_label_columns": True,
    }


if __name__ == "__main__":
    print("✅ XAUUSD/roundlevelcascade_long model_rows.py import/self-contract check passed.")
    print(model_row_config())
