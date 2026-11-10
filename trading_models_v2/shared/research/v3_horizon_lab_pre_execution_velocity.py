"""
Quantara V3 generic short-horizon research laboratory.

ONE SCRIPT FOR ALL STANDARD QUANTARA EDGE PACKAGES
==================================================

Research dimensions:
- Event_Vol return horizon
- Event_Vol EWM smoothing span
- vertical barrier horizon
- PT:SL volatility multipliers

Core methodological rule:
The edge's alpha/candidate-event timestamps are generated ONCE by that edge's
own runtime and then frozen. The lab varies only the label target/geometry.

That means:
    alpha mechanism = frozen
    candidate timestamps = frozen
    feature machinery = frozen
    Event_Vol target = experimental
    vertical/PT/SL geometry = experimental

This is intentionally different from mutating edge-specific constants inside
candidate_events.py. Quantara edge families use different constant names and
some have unrelated volatility lookbacks. Freezing candidate timestamps makes
the experiment both scalable and scientifically cleaner.

AFML-style Event_Vol estimator used by the lab:
    r_H[t] = log(C[t] / C[t-H])
    Event_Vol[t] = EWMStd(r_H, span=S, adjust=False)

Before any experimental grid is allowed, the lab:
1. infers the edge's current Event_Vol control horizon/span;
2. recomputes that control target with the shared estimator;
3. builds the edge's exact current model rows through runtime.build_model_rows();
4. independently labels the same control with the fast generic engine;
5. requires exact event/t1/barrier parity (and near-exact Event_Vol parity).

If control parity fails, the edge is not scanned. This is the scalability
contract: one generic script, fail-closed per edge when an edge deviates from
the standard Quantara labeling contract.

This script never edits edge source, artifacts, registry, allocator, dashboard,
executor, or live manifests. It is a pre-lifecycle screen only.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import inspect
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

EPS = 1e-12
CLEAR_MODULES = ["runtime", "model_rows", "candidate_events", "primary_side"]

OUTPUT_FIELDS = [
    "event_vol_lookback",
    "event_vol_ewm_span",
    "vertical_horizon",
    "pt",
    "sl",
    "cost_bps",
    "base_candidate_events",
    "target_valid_events",
    "rows",
    "pt_rate",
    "sl_rate",
    "vertical_rate",
    "gross_mean_R",
    "cost_mean_R",
    "net_mean_R",
    "median_hold_bars",
    "mean_hold_bars",
    "median_event_vol_bps",
    "median_pt_distance_bps",
    "median_sl_distance_bps",
    "same_bar_ambiguous",
    "tick_resolved",
    "fallback_resolved",
]


class HorizonLabError(RuntimeError):
    """Fail-closed generic V3 horizon-lab error."""


def parse_ints(text: str) -> list[int]:
    vals = [int(x.strip()) for x in str(text).split(",") if x.strip()]
    if not vals or any(v <= 0 for v in vals):
        raise SystemExit("Integer grids must contain positive values.")
    return vals


def parse_barriers(text: str) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for raw in str(text).split(","):
        item = raw.strip()
        if not item:
            continue
        sep = ":" if ":" in item else "/" if "/" in item else None
        if sep is None:
            raise SystemExit(f"Barrier must use pt:sl or pt/sl format: {item}")
        left, right = item.split(sep, 1)
        pt = float(left)
        sl = float(right)
        if pt <= 0 or sl <= 0:
            raise SystemExit(f"Barrier values must be positive: {item}")
        pairs.append((pt, sl))
    if not pairs:
        raise SystemExit("At least one PT:SL pair is required.")
    return pairs


def import_edge(edge_root: Path) -> tuple[Any, Any, Any]:
    edge_root = edge_root.resolve()
    for required in ("runtime.py", "model_rows.py", "candidate_events.py"):
        if not (edge_root / required).exists():
            raise HorizonLabError(f"Missing {required} under {edge_root}")

    for name in CLEAR_MODULES:
        sys.modules.pop(name, None)

    root = str(edge_root)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)

    runtime = importlib.import_module("runtime")
    model_rows = importlib.import_module("model_rows")
    candidate_events = importlib.import_module("candidate_events")
    return runtime, model_rows, candidate_events


def _numeric_attr(module: Any, names: list[str]) -> tuple[str, float] | None:
    for name in names:
        if hasattr(module, name):
            value = getattr(module, name)
            if isinstance(value, (int, float, np.integer, np.floating)):
                return name, float(value)
    return None


def infer_control_event_vol(
    candidate_events: Any,
    override_lookback: int | None,
    override_span: int | None,
) -> dict[str, Any]:
    # Precedence matters. EVENT_VOL_* is explicit. VOL_* is used by a few
    # older packages for the same target, but only as a fallback.
    lookback = (
        ("CLI", float(override_lookback))
        if override_lookback is not None
        else _numeric_attr(
            candidate_events,
            ["EVENT_VOL_LOOKBACK", "EVENT_VOL_LOOKBACK_BARS", "VOL_LOOKBACK"],
        )
    )
    span = (
        ("CLI", float(override_span))
        if override_span is not None
        else _numeric_attr(
            candidate_events,
            ["EVENT_VOL_EWM_SPAN", "VOL_EWM_SPAN"],
        )
    )

    if lookback is None or span is None:
        raise HorizonLabError(
            "Could not infer the edge's current Event_Vol control lookback/span. "
            "Supply --control-event-vol-lookback and "
            "--control-event-vol-ewm-span explicitly."
        )

    return {
        "lookback_source": lookback[0],
        "lookback": int(lookback[1]),
        "span_source": span[0],
        "span": int(span[1]),
    }


def infer_min_event_vol(candidate_events: Any) -> float:
    found = _numeric_attr(
        candidate_events,
        ["MIN_EVENT_VOL", "MIN_VOL"],
    )
    if found is None:
        return 1e-8
    return float(found[1])


def infer_control_geometry(model_rows: Any) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if hasattr(model_rows, "model_row_config"):
        try:
            value = model_rows.model_row_config()
            if isinstance(value, dict):
                config = value
        except Exception:
            config = {}

    vertical = config.get(
        "vertical_horizon",
        getattr(model_rows, "VERTICAL_HORIZON", None),
    )
    pt = config.get(
        "pt_multiplier",
        getattr(model_rows, "PT_MULTIPLIER", None),
    )
    sl = config.get(
        "sl_multiplier",
        getattr(model_rows, "SL_MULTIPLIER", None),
    )

    if vertical is None or pt is None or sl is None:
        raise HorizonLabError(
            "Could not infer control vertical/PT/SL geometry from model_rows.py."
        )

    return {
        "vertical_horizon": int(vertical),
        "pt": float(pt),
        "sl": float(sl),
    }


def schema_feature_names(runtime: Any) -> list[str]:
    if not hasattr(runtime, "_schema_feature_names"):
        raise HorizonLabError(
            "Runtime does not expose _schema_feature_names(); "
            "cannot reproduce model-row feature eligibility safely."
        )
    names = [str(x) for x in runtime._schema_feature_names()]  # noqa: SLF001
    if not names:
        raise HorizonLabError("Edge feature schema contains zero features.")
    return names


def build_feature_cache(runtime: Any) -> tuple[pd.DataFrame, list[str], set[pd.Timestamp]]:
    print("Building expensive Quantara runtime feature table ONCE ...")
    started = time.time()
    features = runtime.build_features(drop_warmup_rows=False)
    print(f"feature build: {time.time() - started:.2f}s | rows={len(features)}")

    names = schema_feature_names(runtime)

    f = features.copy()
    if "Datetime" not in f.columns or "Close" not in f.columns:
        raise HorizonLabError("Runtime feature table must contain Datetime and Close.")

    f["Datetime"] = to_datetime_iso(f["Datetime"], utc=True, errors="coerce")
    f["Close"] = pd.to_numeric(f["Close"], errors="coerce")
    for name in names:
        if name not in f.columns:
            raise HorizonLabError(f"Runtime feature table missing schema feature: {name}")
        f[name] = pd.to_numeric(f[name], errors="coerce")

    valid_matrix = f[names].replace([np.inf, -np.inf], np.nan)
    valid_mask = valid_matrix.notna().all(axis=1) & f["Datetime"].notna()
    valid_times = set(pd.Timestamp(x) for x in f.loc[valid_mask, "Datetime"])

    print(
        f"schema_features={len(names)} | "
        f"feature-valid timestamps={len(valid_times)}"
    )
    return features, names, valid_times


def load_bars(runtime: Any, features: pd.DataFrame) -> dict[str, Any]:
    if hasattr(runtime, "_load_asset_bars"):
        bars = runtime._load_asset_bars()  # noqa: SLF001
    else:
        required = ["Datetime", "Bar_Start", "High", "Low", "Close"]
        missing = [c for c in required if c not in features.columns]
        if missing:
            raise HorizonLabError(
                "Runtime has no _load_asset_bars() and feature table cannot "
                f"supply bars; missing={missing}"
            )
        bars = features[required].copy()

    required = ["Datetime", "Bar_Start", "High", "Low", "Close"]
    missing = [c for c in required if c not in bars.columns]
    if missing:
        raise HorizonLabError(f"Bar table missing required columns: {missing}")

    b = bars.copy()
    b["Datetime"] = to_datetime_iso(b["Datetime"], utc=True, errors="coerce")
    b["Bar_Start"] = to_datetime_iso(b["Bar_Start"], utc=True, errors="coerce")
    for col in ["High", "Low", "Close"]:
        b[col] = pd.to_numeric(b[col], errors="coerce")

    b = (
        b.dropna(subset=required)
        .sort_values("Datetime")
        .drop_duplicates("Datetime")
        .reset_index(drop=True)
    )
    if b.empty:
        raise HorizonLabError("Verified bar table is empty.")

    return {
        "frame": b,
        "dt": pd.DatetimeIndex(b["Datetime"]),
        "bar_start": pd.DatetimeIndex(b["Bar_Start"]),
        "high": b["High"].to_numpy(dtype="float64"),
        "low": b["Low"].to_numpy(dtype="float64"),
        "close": b["Close"].to_numpy(dtype="float64"),
    }


def normalize_base_events(events: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "side", "Event_Vol"]
    missing = [c for c in required if c not in events.columns]
    if missing:
        raise HorizonLabError(f"Candidate events missing required columns: {missing}")

    e = events.copy()
    e["Datetime"] = to_datetime_iso(e["Datetime"], utc=True, errors="coerce")
    e["side"] = pd.to_numeric(e["side"], errors="coerce")
    e["Event_Vol"] = pd.to_numeric(e["Event_Vol"], errors="coerce")
    e = (
        e.dropna(subset=["Datetime", "side"])
        .sort_values("Datetime")
        .drop_duplicates("Datetime")
        .reset_index(drop=True)
    )

    if e.empty:
        raise HorizonLabError("Edge runtime generated zero candidate events.")
    if not set(e["side"].astype(int).unique()).issubset({-1, 1}):
        raise HorizonLabError("Candidate events contain side values outside {-1,+1}.")
    return e


def compute_event_vol_series(
    features: pd.DataFrame,
    *,
    lookback: int,
    span: int,
    min_vol: float,
) -> pd.Series:
    if lookback <= 0 or span <= 0 or min_vol <= 0:
        raise HorizonLabError("Event_Vol lookback/span/min_vol must be positive.")

    f = features[["Datetime", "Close"]].copy()
    f["Datetime"] = to_datetime_iso(f["Datetime"], utc=True, errors="coerce")
    f["Close"] = pd.to_numeric(f["Close"], errors="coerce")
    f = (
        f.dropna(subset=["Datetime", "Close"])
        .sort_values("Datetime")
        .drop_duplicates("Datetime")
        .reset_index(drop=True)
    )

    close = f["Close"].astype("float64")
    ret = np.log((close + EPS) / (close.shift(int(lookback)) + EPS))
    vol = (
        ret.ewm(span=int(span), adjust=False)
        .std()
        .clip(lower=float(min_vol))
    )
    return pd.Series(
        vol.to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(f["Datetime"]),
        name="Event_Vol",
    )


def events_with_target(
    base_events: pd.DataFrame,
    vol_series: pd.Series,
) -> pd.DataFrame:
    e = base_events.copy()
    e["Event_Vol"] = pd.DatetimeIndex(e["Datetime"]).map(vol_series)
    e["Event_Vol"] = pd.to_numeric(e["Event_Vol"], errors="coerce")
    e = e[
        np.isfinite(e["Event_Vol"].to_numpy(dtype="float64"))
        & (e["Event_Vol"].to_numpy(dtype="float64") > 0)
    ].copy()
    return e.reset_index(drop=True)


def call_exact_control_rows(
    runtime: Any,
    *,
    features: pd.DataFrame,
    events: pd.DataFrame,
    bars: pd.DataFrame,
    vertical_horizon: int,
) -> pd.DataFrame:
    fn = runtime.build_model_rows
    sig = inspect.signature(fn)
    kwargs: dict[str, Any] = {}

    if "features" in sig.parameters:
        kwargs["features"] = features
    if "events" in sig.parameters:
        kwargs["events"] = events
    if "bars" in sig.parameters:
        kwargs["bars"] = bars
    if "vertical_horizon" in sig.parameters:
        kwargs["vertical_horizon"] = int(vertical_horizon)

    result = fn(**kwargs)
    rows = result[0] if isinstance(result, tuple) else result
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        raise HorizonLabError(
            "runtime.build_model_rows() did not produce a non-empty DataFrame."
        )
    return rows.copy()


def build_tick_catalog_if_needed(model_rows: Any) -> Any:
    if not hasattr(model_rows, "build_tick_file_catalog"):
        raise HorizonLabError(
            "Same-bar PT/SL ambiguity occurred but model_rows.py does not "
            "expose build_tick_file_catalog()."
        )
    return model_rows.build_tick_file_catalog()


def resolve_same_bar(
    model_rows: Any,
    tick_catalog: Any,
    *,
    bar_start: pd.Timestamp,
    bar_end: pd.Timestamp,
    pt_price: float,
    sl_price: float,
    side: int,
) -> tuple[float, str | None, str]:
    if not hasattr(model_rows, "_resolve_tick_first_touch"):
        raise HorizonLabError(
            "Same-bar PT/SL ambiguity occurred but model_rows.py does not "
            "expose _resolve_tick_first_touch()."
        )

    label, hit, source = model_rows._resolve_tick_first_touch(  # noqa: SLF001
        tick_catalog,
        bar_start,
        bar_end,
        float(pt_price),
        float(sl_price),
        int(side),
    )
    return float(label) if label is not None else float("nan"), hit, str(source)


def compute_first_touch_paths(
    *,
    model_rows: Any,
    bars: dict[str, Any],
    events: pd.DataFrame,
    max_horizon: int,
    pt: float,
    sl: float,
) -> dict[str, Any]:
    dt: pd.DatetimeIndex = bars["dt"]
    starts: pd.DatetimeIndex = bars["bar_start"]
    high: np.ndarray = bars["high"]
    low: np.ndarray = bars["low"]
    close: np.ndarray = bars["close"]
    n = len(dt)

    event_times = pd.DatetimeIndex(events["Datetime"])
    locs = dt.get_indexer(event_times)
    sides = events["side"].to_numpy(dtype="int64")
    vols = events["Event_Vol"].to_numpy(dtype="float64")

    first_offset = np.full(len(events), np.iinfo(np.int32).max, dtype=np.int32)
    first_hit = np.full(len(events), "", dtype=object)
    first_source = np.full(len(events), "", dtype=object)
    first_label = np.full(len(events), np.nan, dtype="float64")

    ambiguous = 0
    tick_resolved = 0
    fallback_resolved = 0
    tick_catalog = None

    for j, loc in enumerate(locs):
        if loc < 0:
            continue

        side = int(sides[j])
        trgt = float(vols[j])
        entry = float(close[loc])
        if (
            side not in (-1, 1)
            or not np.isfinite(trgt)
            or trgt <= 0
            or not np.isfinite(entry)
        ):
            continue

        available = min(int(max_horizon), n - int(loc) - 1)
        if available <= 0:
            continue

        if side == 1:
            pt_price = entry * (1.0 + float(pt) * trgt)
            sl_price = entry * (1.0 - float(sl) * trgt)
            pt_mask = high[loc + 1 : loc + available + 1] >= pt_price
            sl_mask = low[loc + 1 : loc + available + 1] <= sl_price
        else:
            pt_price = entry * (1.0 - float(pt) * trgt)
            sl_price = entry * (1.0 + float(sl) * trgt)
            pt_mask = low[loc + 1 : loc + available + 1] <= pt_price
            sl_mask = high[loc + 1 : loc + available + 1] >= sl_price

        pt_idx = np.flatnonzero(pt_mask)
        sl_idx = np.flatnonzero(sl_mask)
        p = int(pt_idx[0] + 1) if pt_idx.size else None
        s = int(sl_idx[0] + 1) if sl_idx.size else None

        if p is None and s is None:
            continue
        if s is None or (p is not None and p < s):
            first_offset[j] = int(p)
            first_hit[j] = "pt"
            first_source[j] = "ohlc"
            first_label[j] = 1.0
            continue
        if p is None or s < p:
            first_offset[j] = int(s)
            first_hit[j] = "sl"
            first_source[j] = "ohlc"
            first_label[j] = 0.0
            continue

        # First PT and SL touches are in the same bar.
        ambiguous += 1
        if tick_catalog is None:
            tick_catalog = build_tick_catalog_if_needed(model_rows)

        future_idx = int(loc) + int(p)
        label, hit, source = resolve_same_bar(
            model_rows,
            tick_catalog,
            bar_start=pd.Timestamp(starts[future_idx]),
            bar_end=pd.Timestamp(dt[future_idx]),
            pt_price=float(pt_price),
            sl_price=float(sl_price),
            side=side,
        )

        # Preserve the edge's drop tie policy if it returns NaN/None.
        if hit is None or not np.isfinite(label):
            first_offset[j] = int(p)
            first_hit[j] = "drop"
            first_source[j] = source
            first_label[j] = np.nan
        else:
            first_offset[j] = int(p)
            first_hit[j] = str(hit)
            first_source[j] = source
            first_label[j] = float(label)

        if source == "tick":
            tick_resolved += 1
        else:
            fallback_resolved += 1

    return {
        "locs": locs,
        "sides": sides,
        "vols": vols,
        "first_offset": first_offset,
        "first_hit": first_hit,
        "first_source": first_source,
        "first_label": first_label,
        "same_bar_ambiguous": int(ambiguous),
        "tick_resolved": int(tick_resolved),
        "fallback_resolved": int(fallback_resolved),
    }


def materialize_horizon(
    *,
    bars: dict[str, Any],
    events: pd.DataFrame,
    paths: dict[str, Any],
    valid_feature_times: set[pd.Timestamp],
    horizon: int,
    pt: float,
    sl: float,
    cost_bps: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    dt: pd.DatetimeIndex = bars["dt"]
    close: np.ndarray = bars["close"]
    n = len(dt)

    locs: np.ndarray = paths["locs"]
    sides: np.ndarray = paths["sides"]
    vols: np.ndarray = paths["vols"]
    first_offset: np.ndarray = paths["first_offset"]
    first_hit: np.ndarray = paths["first_hit"]
    first_source: np.ndarray = paths["first_source"]
    first_label: np.ndarray = paths["first_label"]

    event_times = [pd.Timestamp(x) for x in events["Datetime"]]
    feature_valid = np.array(
        [ts in valid_feature_times for ts in event_times],
        dtype=bool,
    )

    eligible = (
        (locs >= 0)
        & ((locs + int(horizon)) < n)
        & feature_valid
        & np.isfinite(vols)
        & (vols > 0)
    )
    idxs = np.flatnonzero(eligible)
    if idxs.size == 0:
        raise HorizonLabError(
            f"No eligible rows for vertical_horizon={horizon}."
        )

    rows: list[dict[str, Any]] = []

    for j in idxs:
        loc = int(locs[j])
        side = int(sides[j])
        trgt = float(vols[j])
        entry = float(close[loc])

        if side == 1:
            pt_price = entry * (1.0 + float(pt) * trgt)
            sl_price = entry * (1.0 - float(sl) * trgt)
        else:
            pt_price = entry * (1.0 - float(pt) * trgt)
            sl_price = entry * (1.0 + float(sl) * trgt)

        if int(first_offset[j]) <= int(horizon):
            if first_hit[j] == "drop":
                continue
            hit = str(first_hit[j])
            source = str(first_source[j])
            label = int(first_label[j])
            end_idx = loc + int(first_offset[j])
        else:
            hit = "vertical"
            source = "vertical"
            label = 0
            end_idx = loc + int(horizon)

        if hit == "pt":
            realized_r = float(pt) / float(sl)
        elif hit == "sl":
            realized_r = -1.0
        else:
            realized_r = (
                side
                * ((float(close[end_idx]) / (entry + EPS)) - 1.0)
                / (float(sl) * trgt + EPS)
            )

        cost_r = (
            (float(cost_bps) / 10_000.0)
            / (float(sl) * trgt + EPS)
        )

        rows.append({
            "Datetime": pd.Timestamp(dt[loc]),
            "t1": pd.Timestamp(dt[end_idx]),
            "side": side,
            "Event_Vol": trgt,
            "meta_label": int(label),
            "barrier_hit": hit,
            "resolution_source": source,
            "entry_price": entry,
            "pt_price": float(pt_price),
            "sl_price": float(sl_price),
            "realized_r": float(realized_r),
            "cost_r": float(cost_r),
            "net_r": float(realized_r - cost_r),
            "hold_bars": int(end_idx - loc),
        })

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise HorizonLabError(
            f"All rows dropped for vertical_horizon={horizon}."
        )

    vol_bps = frame["Event_Vol"].to_numpy(dtype="float64") * 10_000.0
    hit = frame["barrier_hit"]

    summary = {
        "rows": int(len(frame)),
        "pt_rate": float(hit.eq("pt").mean()),
        "sl_rate": float(hit.eq("sl").mean()),
        "vertical_rate": float(hit.eq("vertical").mean()),
        "gross_mean_R": float(frame["realized_r"].mean()),
        "cost_mean_R": float(frame["cost_r"].mean()),
        "net_mean_R": float(frame["net_r"].mean()),
        "median_hold_bars": float(frame["hold_bars"].median()),
        "mean_hold_bars": float(frame["hold_bars"].mean()),
        "median_event_vol_bps": float(np.median(vol_bps)),
        "median_pt_distance_bps": float(np.median(float(pt) * vol_bps)),
        "median_sl_distance_bps": float(np.median(float(sl) * vol_bps)),
        "same_bar_ambiguous": int(paths["same_bar_ambiguous"]),
        "tick_resolved": int(paths["tick_resolved"]),
        "fallback_resolved": int(paths["fallback_resolved"]),
    }
    return frame, summary


def compare_control_rows(
    exact: pd.DataFrame,
    fast: pd.DataFrame,
) -> dict[str, Any]:
    required = ["Datetime", "t1", "barrier_hit", "Event_Vol"]
    for name, frame in (("exact", exact), ("fast", fast)):
        missing = [c for c in required if c not in frame.columns]
        if missing:
            raise HorizonLabError(
                f"{name} control rows missing required comparison columns: {missing}"
            )

    a = exact.copy()
    b = fast.copy()
    for frame in (a, b):
        frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
        frame["t1"] = to_datetime_iso(frame["t1"], utc=True, errors="coerce")
        frame["Event_Vol"] = pd.to_numeric(frame["Event_Vol"], errors="coerce")
        frame.sort_values("Datetime", inplace=True)
        frame.drop_duplicates("Datetime", inplace=True)

    a = a.set_index("Datetime")
    b = b.set_index("Datetime")

    missing_fast = a.index.difference(b.index)
    extra_fast = b.index.difference(a.index)
    common = a.index.intersection(b.index)

    t1_mismatch = int(
        (a.loc[common, "t1"] != b.loc[common, "t1"]).sum()
    )
    hit_mismatch = int(
        (
            a.loc[common, "barrier_hit"].astype(str)
            != b.loc[common, "barrier_hit"].astype(str)
        ).sum()
    )

    vol_diff = np.abs(
        a.loc[common, "Event_Vol"].to_numpy(dtype="float64")
        - b.loc[common, "Event_Vol"].to_numpy(dtype="float64")
    )
    max_vol_diff = (
        float(np.nanmax(vol_diff))
        if len(vol_diff)
        else float("nan")
    )

    # Price comparison is an additional strong check where available.
    price_checks: dict[str, Any] = {}
    for col in ("entry_price", "pt_price", "sl_price"):
        if col in a.columns and col in b.columns and len(common):
            diff = np.abs(
                pd.to_numeric(a.loc[common, col], errors="coerce").to_numpy(dtype="float64")
                - pd.to_numeric(b.loc[common, col], errors="coerce").to_numpy(dtype="float64")
            )
            price_checks[f"max_abs_{col}_diff"] = float(np.nanmax(diff))

    passed = (
        len(missing_fast) == 0
        and len(extra_fast) == 0
        and t1_mismatch == 0
        and hit_mismatch == 0
        and np.isfinite(max_vol_diff)
        and max_vol_diff <= 1e-12
    )

    return {
        "status": "PASS" if passed else "FAIL",
        "exact_rows": int(len(a)),
        "fast_rows": int(len(b)),
        "missing_fast_rows": int(len(missing_fast)),
        "extra_fast_rows": int(len(extra_fast)),
        "t1_mismatches": t1_mismatch,
        "barrier_hit_mismatches": hit_mismatch,
        "max_abs_event_vol_diff": max_vol_diff,
        **price_checks,
    }


def run_control_parity(
    *,
    runtime: Any,
    model_rows: Any,
    features: pd.DataFrame,
    bars: dict[str, Any],
    base_events: pd.DataFrame,
    valid_feature_times: set[pd.Timestamp],
    control_vol: dict[str, Any],
    control_geometry: dict[str, Any],
    min_vol: float,
) -> dict[str, Any]:
    print()
    print("CONTROL PARITY GATE")
    print(
        "Current edge control: "
        f"Hvol={control_vol['lookback']} | "
        f"EWM={control_vol['span']} | "
        f"Hv={control_geometry['vertical_horizon']} | "
        f"PT:SL={control_geometry['pt']:g}:{control_geometry['sl']:g}"
    )

    started = time.time()

    exact_rows = call_exact_control_rows(
        runtime,
        features=features,
        events=base_events,
        bars=bars["frame"],
        vertical_horizon=control_geometry["vertical_horizon"],
    )

    control_series = compute_event_vol_series(
        features,
        lookback=control_vol["lookback"],
        span=control_vol["span"],
        min_vol=min_vol,
    )
    control_events = events_with_target(base_events, control_series)

    paths = compute_first_touch_paths(
        model_rows=model_rows,
        bars=bars,
        events=control_events,
        max_horizon=control_geometry["vertical_horizon"],
        pt=control_geometry["pt"],
        sl=control_geometry["sl"],
    )
    fast_rows, _ = materialize_horizon(
        bars=bars,
        events=control_events,
        paths=paths,
        valid_feature_times=valid_feature_times,
        horizon=control_geometry["vertical_horizon"],
        pt=control_geometry["pt"],
        sl=control_geometry["sl"],
        cost_bps=0.0,
    )

    parity = compare_control_rows(exact_rows, fast_rows)
    parity.update({
        "control_event_vol": control_vol,
        "control_geometry": control_geometry,
        "elapsed_seconds": float(time.time() - started),
    })

    print(
        f"CONTROL PARITY: {parity['status']} | "
        f"exact_rows={parity['exact_rows']} | "
        f"fast_rows={parity['fast_rows']} | "
        f"t1_mismatch={parity['t1_mismatches']} | "
        f"hit_mismatch={parity['barrier_hit_mismatches']} | "
        f"max_Event_Vol_diff={parity['max_abs_event_vol_diff']:.3e}"
    )

    if parity["status"] != "PASS":
        raise HorizonLabError(
            "Generic V3 lab failed the edge's exact control parity gate. "
            "No experimental grid will be run. This edge needs an explicit "
            "adapter or contract audit rather than a silent approximation."
        )

    return parity


def save_results(
    edge_root: Path,
    results: list[dict[str, Any]],
    audit: dict[str, Any],
) -> tuple[Path, Path]:
    out_dir = edge_root / "artifacts" / "v3_horizon_lab"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    csv_path = out_dir / f"v3_horizon_grid_{stamp}.csv"
    json_path = out_dir / f"v3_horizon_grid_{stamp}.json"

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(results)

    payload = dict(audit)
    payload["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["outputs"] = {
        "csv": csv_path.as_posix(),
        "json": json_path.as_posix(),
    }
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return csv_path, json_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generic Quantara V3 Event_Vol horizon x EWM span x "
            "vertical horizon x PT/SL research lab."
        )
    )
    parser.add_argument("--edge-root", required=True)
    parser.add_argument(
        "--event-vol-lookbacks",
        default="20,10,5",
        help="Comma-separated experimental Event_Vol return horizons.",
    )
    parser.add_argument(
        "--event-vol-ewm-spans",
        default="20,50,100,200",
        help="Comma-separated EWM smoothing spans.",
    )
    parser.add_argument(
        "--vertical-horizons",
        default="20,10,5",
        help="Comma-separated experimental vertical horizons.",
    )
    parser.add_argument(
        "--barriers",
        default="1:1",
        help="Comma-separated PT:SL volatility multiplier pairs.",
    )
    parser.add_argument("--cost-bps", type=float, default=0.0)
    parser.add_argument("--save", action="store_true")
    parser.add_argument(
        "--control-event-vol-lookback",
        type=int,
        default=None,
        help="Override only when the edge's current control cannot be inferred.",
    )
    parser.add_argument(
        "--control-event-vol-ewm-span",
        type=int,
        default=None,
        help="Override only when the edge's current control cannot be inferred.",
    )
    args = parser.parse_args()

    started = time.time()
    edge_root = Path(args.edge_root).resolve()
    lookbacks = parse_ints(args.event_vol_lookbacks)
    spans = parse_ints(args.event_vol_ewm_spans)
    horizons = parse_ints(args.vertical_horizons)
    barriers = parse_barriers(args.barriers)

    runtime, model_rows, candidate_events = import_edge(edge_root)

    edge_id = str(getattr(runtime, "EDGE_ID", edge_root.as_posix()))
    control_vol = infer_control_event_vol(
        candidate_events,
        args.control_event_vol_lookback,
        args.control_event_vol_ewm_span,
    )
    control_geometry = infer_control_geometry(model_rows)
    min_vol = infer_min_event_vol(candidate_events)

    print("QUANTARA V3 — GENERIC SHORT-HORIZON LAB")
    print("ONE SCRIPT / STANDARD QUANTARA EDGE RUNTIME")
    print("RESEARCH ONLY — NO EDGE FILES WILL BE EDITED")
    print(f"edge: {edge_id}")
    print(f"edge_root: {edge_root}")
    print(
        "control Event_Vol: "
        f"H={control_vol['lookback']} "
        f"(source={control_vol['lookback_source']}), "
        f"EWM={control_vol['span']} "
        f"(source={control_vol['span_source']})"
    )
    print(
        "control geometry: "
        f"Hv={control_geometry['vertical_horizon']}, "
        f"PT:SL={control_geometry['pt']:g}:{control_geometry['sl']:g}"
    )
    print(f"experimental Event_Vol lookbacks: {lookbacks}")
    print(f"experimental EWM spans: {spans}")
    print(f"experimental vertical horizons: {horizons}")
    print(f"experimental PT:SL: {barriers}")
    print(f"diagnostic cost_bps: {args.cost_bps}")
    print()

    features, feature_names, valid_feature_times = build_feature_cache(runtime)
    bars = load_bars(runtime, features)

    print("Generating alpha candidate timestamps ONCE and freezing them ...")
    event_started = time.time()
    base_events = normalize_base_events(
        runtime.generate_candidate_events(features)
    )
    print(
        f"base candidate events={len(base_events)} | "
        f"generated in {time.time() - event_started:.2f}s"
    )

    parity = run_control_parity(
        runtime=runtime,
        model_rows=model_rows,
        features=features,
        bars=bars,
        base_events=base_events,
        valid_feature_times=valid_feature_times,
        control_vol=control_vol,
        control_geometry=control_geometry,
        min_vol=min_vol,
    )

    print()
    print("CONTROL PASSED — EXPERIMENTAL GRID MAY RUN")
    print(
        "Candidate timestamps remain frozen across every grid cell. "
        "Only Event_Vol target and exit geometry vary."
    )
    print()

    results: list[dict[str, Any]] = []
    pair_audits: list[dict[str, Any]] = []

    for lookback in lookbacks:
        for span in spans:
            pair_started = time.time()

            vol_series = compute_event_vol_series(
                features,
                lookback=lookback,
                span=span,
                min_vol=min_vol,
            )
            target_events = events_with_target(base_events, vol_series)

            median_vol_bps = float(
                np.median(
                    target_events["Event_Vol"].to_numpy(dtype="float64")
                )
                * 10_000.0
            )

            print("=" * 108)
            print(
                f"Event_Vol H={lookback} | EWM={span} | "
                f"target-valid events={len(target_events)}/{len(base_events)} | "
                f"median Event_Vol={median_vol_bps:.4f} bps"
            )
            print("=" * 108)

            barrier_audits: list[dict[str, Any]] = []

            for pt, sl in barriers:
                paths = compute_first_touch_paths(
                    model_rows=model_rows,
                    bars=bars,
                    events=target_events,
                    max_horizon=max(horizons),
                    pt=pt,
                    sl=sl,
                )

                for horizon in horizons:
                    _, summary = materialize_horizon(
                        bars=bars,
                        events=target_events,
                        paths=paths,
                        valid_feature_times=valid_feature_times,
                        horizon=horizon,
                        pt=pt,
                        sl=sl,
                        cost_bps=args.cost_bps,
                    )

                    row = {
                        "event_vol_lookback": int(lookback),
                        "event_vol_ewm_span": int(span),
                        "vertical_horizon": int(horizon),
                        "pt": float(pt),
                        "sl": float(sl),
                        "cost_bps": float(args.cost_bps),
                        "base_candidate_events": int(len(base_events)),
                        "target_valid_events": int(len(target_events)),
                        **summary,
                    }
                    results.append(row)

                    print(
                        f"Hvol={lookback:>3} | EWM={span:>3} | "
                        f"Hv={horizon:>4} | {pt:g}:{sl:g} | "
                        f"rows={row['rows']:>5} | "
                        f"netR={row['net_mean_R']:+.5f} | "
                        f"vertical={row['vertical_rate']:.3f} | "
                        f"median_hold={row['median_hold_bars']:.1f} | "
                        f"median_PT={row['median_pt_distance_bps']:.2f} bps"
                    )

                barrier_audits.append({
                    "pt": float(pt),
                    "sl": float(sl),
                    "same_bar_ambiguous": int(paths["same_bar_ambiguous"]),
                    "tick_resolved": int(paths["tick_resolved"]),
                    "fallback_resolved": int(paths["fallback_resolved"]),
                })

            elapsed = time.time() - pair_started
            pair_audits.append({
                "event_vol_lookback": int(lookback),
                "event_vol_ewm_span": int(span),
                "target_valid_events": int(len(target_events)),
                "median_event_vol_bps": median_vol_bps,
                "barrier_audits": barrier_audits,
                "elapsed_seconds": float(elapsed),
            })
            print(f"pair elapsed: {elapsed:.2f}s")
            print()

    print(",".join(OUTPUT_FIELDS))
    for r in results:
        print(
            f"{r['event_vol_lookback']},"
            f"{r['event_vol_ewm_span']},"
            f"{r['vertical_horizon']},"
            f"{r['pt']:.4f},"
            f"{r['sl']:.4f},"
            f"{r['cost_bps']:.4f},"
            f"{r['base_candidate_events']},"
            f"{r['target_valid_events']},"
            f"{r['rows']},"
            f"{r['pt_rate']:.6f},"
            f"{r['sl_rate']:.6f},"
            f"{r['vertical_rate']:.6f},"
            f"{r['gross_mean_R']:.8f},"
            f"{r['cost_mean_R']:.8f},"
            f"{r['net_mean_R']:.8f},"
            f"{r['median_hold_bars']:.4f},"
            f"{r['mean_hold_bars']:.4f},"
            f"{r['median_event_vol_bps']:.6f},"
            f"{r['median_pt_distance_bps']:.6f},"
            f"{r['median_sl_distance_bps']:.6f},"
            f"{r['same_bar_ambiguous']},"
            f"{r['tick_resolved']},"
            f"{r['fallback_resolved']}"
        )

    audit = {
        "status": "V3_GENERIC_HORIZON_LAB_COMPLETE_RESEARCH_ONLY",
        "edge_id": edge_id,
        "edge_root": edge_root.as_posix(),
        "methodology": {
            "candidate_timestamps": "generated_once_by_edge_runtime_then_frozen",
            "event_vol_estimator": (
                "log_return_over_H_bars_then_ewm_std_span_S_adjust_false"
            ),
            "alpha_mechanism_changed": False,
            "label_target_changed_in_memory_only": True,
            "feature_build_count": 1,
            "feature_count": len(feature_names),
            "selection": (
                "NO AUTOMATIC WINNER. Seek robust neighboring regions, "
                "then rerun full Quantara lifecycle and exact execution replay."
            ),
        },
        "control_parity": parity,
        "control_event_vol": control_vol,
        "control_geometry": control_geometry,
        "min_event_vol": float(min_vol),
        "grid": {
            "event_vol_lookbacks": lookbacks,
            "event_vol_ewm_spans": spans,
            "vertical_horizons": horizons,
            "barriers": [{"pt": pt, "sl": sl} for pt, sl in barriers],
            "cost_bps": float(args.cost_bps),
        },
        "pair_audits": pair_audits,
        "important_boundary": (
            "This is only a pre-lifecycle label/geometry screen. "
            "No current model/calibrator/threshold/edge_manifest/live manifest "
            "is valid for a changed target until regenerated and revalidated."
        ),
    }

    if args.save:
        csv_path, json_path = save_results(
            edge_root,
            results,
            audit,
        )
        print()
        print("Saved:")
        print("-", csv_path)
        print("-", json_path)

    print()
    print(f"TOTAL ELAPSED: {time.time() - started:.2f}s")
    print("No edge source or deployment artifact was modified.")


if __name__ == "__main__":
    main()
