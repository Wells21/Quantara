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



# =============================================================================
# Generic edge-local same-bar tick-resolver signature adapter
# =============================================================================
# Marker: V3_TICK_RESOLVER_SIGNATURE_ADAPTER_V1

def _v3_adapt_tick_resolver_signature(model_rows: Any) -> None:
    """Normalize legacy/new edge-local tick resolvers to the shared 6-arg API."""
    import inspect

    resolver = getattr(model_rows, "_resolve_tick_first_touch", None)
    if resolver is None:
        return

    if bool(getattr(resolver, "_v3_signature_adapter", False)):
        return

    try:
        signature = inspect.signature(resolver)
    except Exception as exc:
        raise HorizonLabError(
            "Could not inspect edge-local _resolve_tick_first_touch signature."
        ) from exc

    params = list(signature.parameters.values())
    positional = [
        p
        for p in params
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    has_varargs = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL
        for p in params
    )

    if has_varargs:
        print(
            "tick resolver API: variadic resolver preserved | "
            f"module={getattr(model_rows, '__name__', '<unknown>')}"
        )
        return

    if len(positional) == 6:
        return

    if len(positional) != 5:
        raise HorizonLabError(
            "Unsupported edge-local _resolve_tick_first_touch signature: "
            f"{signature}. Expected 5 legacy positional args or 6 current args."
        )

    expected_side = getattr(model_rows, "SIDE_VALUE", None)
    try:
        expected_side = int(expected_side)
    except Exception as exc:
        raise HorizonLabError(
            "Legacy 5-argument tick resolver requires model_rows.SIDE_VALUE."
        ) from exc

    if expected_side not in (-1, 1):
        raise HorizonLabError(
            "Legacy 5-argument tick resolver has invalid SIDE_VALUE="
            f"{expected_side!r}."
        )

    original = resolver

    def _wrapped_tick_first_touch(
        catalog: Any,
        start: pd.Timestamp,
        end: pd.Timestamp,
        pt_price: float,
        sl_price: float,
        side: int,
    ) -> tuple[float, str | None, str]:
        try:
            requested_side = int(side)
        except Exception as exc:
            raise HorizonLabError(
                f"Invalid event side passed to tick resolver: {side!r}"
            ) from exc

        if requested_side != expected_side:
            raise HorizonLabError(
                "Legacy edge-local tick resolver is side-specialized and cannot "
                "serve the requested event side: "
                f"module_SIDE_VALUE={expected_side}, event_side={requested_side}."
            )

        return original(
            catalog,
            start,
            end,
            pt_price,
            sl_price,
        )

    _wrapped_tick_first_touch.__name__ = getattr(
        original,
        "__name__",
        "_resolve_tick_first_touch",
    )
    _wrapped_tick_first_touch.__doc__ = getattr(original, "__doc__", None)
    _wrapped_tick_first_touch._v3_signature_adapter = True  # type: ignore[attr-defined]
    _wrapped_tick_first_touch._v3_original_resolver = original  # type: ignore[attr-defined]

    setattr(
        model_rows,
        "_resolve_tick_first_touch",
        _wrapped_tick_first_touch,
    )

    print(
        "tick resolver API adapter: legacy 5-arg -> shared 6-arg | "
        f"SIDE_VALUE={expected_side} | "
        f"module={getattr(model_rows, '__name__', '<unknown>')}"
    )


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
    _v3_adapt_tick_resolver_signature(model_rows)
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



# =============================================================================
# Exact Bid/Ask finalist replay + capital-time velocity
# =============================================================================
# Marker: V3_EXECUTION_FINALIST_MODE_V1


def _xef_parse(text: str) -> list[dict[str, Any]]:
    out = []
    for raw in str(text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        p = raw.split(":")
        if len(p) != 5:
            raise HorizonLabError(
                "--execution-finalists must be Hvol:EWM:Hv:PT:SL comma-separated"
            )
        h, e, v = int(p[0]), int(p[1]), int(p[2])
        pt, sl = float(p[3]), float(p[4])
        if min(h, e, v) <= 0 or pt <= 0 or sl <= 0:
            raise HorizonLabError(f"Invalid execution finalist: {raw}")
        out.append({
            "label": f"H{h}_E{e}_V{v}_PT{pt:g}_SL{sl:g}",
            "event_vol_lookback": h,
            "event_vol_ewm_span": e,
            "vertical_horizon": v,
            "pt": pt,
            "sl": sl,
            "is_control": False,
        })
    if not out:
        raise HorizonLabError("No execution finalists supplied.")
    return out


def _xef_v2_root(edge_root: Path) -> Path:
    root = edge_root.parents[3]
    if not (root / "assets").exists():
        raise HorizonLabError(f"Cannot resolve trading_models_v2 root from {edge_root}")
    return root


def _xef_artifacts(edge_root: Path) -> tuple[pd.DataFrame, dict[str, Path]]:
    mp = edge_root / "artifacts" / "research_dataset" / "research_dataset_manifest.json"
    if not mp.exists():
        raise HorizonLabError(f"Missing research dataset manifest: {mp}")
    man = json.loads(mp.read_text(encoding="utf-8-sig"))
    outputs = man.get("outputs") or {}
    root = _xef_v2_root(edge_root)
    paths: dict[str, Path] = {"manifest": mp}
    for key in ("candidate_events", "model_rows", "feature_matrix"):
        raw = outputs.get(key)
        if not raw:
            raise HorizonLabError(f"research_dataset_manifest missing output {key}")
        p = Path(str(raw))
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            raise HorizonLabError(f"Required local artifact missing: {p}")
        paths[key] = p
    events = normalize_base_events(pd.read_csv(paths["candidate_events"]))
    return events, paths


def _xef_bars(runtime: Any) -> dict[str, Any]:
    if not hasattr(runtime, "_load_asset_bars"):
        raise HorizonLabError("Execution mode requires runtime._load_asset_bars().")
    b = runtime._load_asset_bars().copy()  # noqa: SLF001
    req = ["Datetime", "Bar_Start", "High", "Low", "Close"]
    miss = [c for c in req if c not in b.columns]
    if miss:
        raise HorizonLabError(f"Bars missing {miss}")
    b["Datetime"] = to_datetime_iso(b["Datetime"], utc=True, errors="coerce")
    b["Bar_Start"] = to_datetime_iso(b["Bar_Start"], utc=True, errors="coerce")
    for c in ["High", "Low", "Close"]:
        b[c] = pd.to_numeric(b[c], errors="coerce")
    b = b.dropna(subset=req).sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)
    return {
        "frame": b,
        "dt": pd.DatetimeIndex(b["Datetime"]),
        "close": b["Close"].to_numpy(dtype="float64"),
    }


def _xef_valid_times(
    paths: dict[str, Path],
    feature_names: list[str],
    finalists: list[dict[str, Any]],
    control_geometry: dict[str, Any],
) -> set[pd.Timestamp]:
    if all(int(x["vertical_horizon"]) == int(control_geometry["vertical_horizon"]) for x in finalists):
        print("Eligibility fast path: persisted model_rows timestamps (same Hv as control).")
        d = pd.read_csv(paths["model_rows"], usecols=["Datetime"])
        dt = to_datetime_iso(d["Datetime"], utc=True, errors="coerce").dropna()
        return set(pd.Timestamp(x) for x in dt)

    print("Eligibility generic path: persisted feature_matrix (no feature rebuild).")
    header = pd.read_csv(paths["feature_matrix"], nrows=0)
    cols = ["Datetime", *feature_names]
    miss = [c for c in cols if c not in header.columns]
    if miss:
        raise HorizonLabError(f"Feature matrix missing schema columns: {miss[:20]}")
    d = pd.read_csv(paths["feature_matrix"], usecols=cols)
    d["Datetime"] = to_datetime_iso(d["Datetime"], utc=True, errors="coerce")
    for c in feature_names:
        d[c] = pd.to_numeric(d[c], errors="coerce")
    good = d[feature_names].replace([np.inf, -np.inf], np.nan).notna().all(axis=1) & d["Datetime"].notna()
    return set(pd.Timestamp(x) for x in d.loc[good, "Datetime"])


def _xef_vol(bars: dict[str, Any], h: int, ewm: int, min_vol: float) -> pd.Series:
    c = pd.Series(bars["close"], dtype="float64")
    r = np.log((c + EPS) / (c.shift(int(h)) + EPS))
    v = r.ewm(span=int(ewm), adjust=False).std().clip(lower=float(min_vol))
    return pd.Series(v.to_numpy(dtype="float64"), index=bars["dt"])


def _xef_prepare(
    finalists: list[dict[str, Any]],
    events: pd.DataFrame,
    bars: dict[str, Any],
    valid_times: set[pd.Timestamp],
    min_vol: float,
) -> dict[str, dict[pd.Timestamp, dict[str, Any]]]:
    base = events.copy()
    base["Datetime"] = to_datetime_iso(base["Datetime"], utc=True, errors="coerce")
    base["side"] = pd.to_numeric(base["side"], errors="coerce")
    base = base.dropna(subset=["Datetime", "side"]).sort_values("Datetime").reset_index(drop=True)
    dt = bars["dt"]
    locs = dt.get_indexer(pd.DatetimeIndex(base["Datetime"]))
    result: dict[str, dict[pd.Timestamp, dict[str, Any]]] = {}
    for cfg in finalists:
        vol = _xef_vol(bars, cfg["event_vol_lookback"], cfg["event_vol_ewm_span"], min_vol)
        hv = int(cfg["vertical_horizon"])
        m: dict[pd.Timestamp, dict[str, Any]] = {}
        for i, row in base.iterrows():
            t = pd.Timestamp(row["Datetime"])
            loc = int(locs[i])
            if loc < 0 or t not in valid_times or loc + hv >= len(dt):
                continue
            trgt = float(vol.get(t, np.nan))
            side = int(row["side"])
            sig = float(bars["close"][loc])
            if side not in (-1, 1) or not np.isfinite(trgt) or trgt <= 0 or not np.isfinite(sig) or sig <= 0:
                continue
            deadline = pd.Timestamp(dt[loc + hv])
            m[t] = {
                "event_loc": loc,
                "side": side,
                "signal_mid": sig,
                "event_vol": trgt,
                "deadline_ns": int(deadline.value),
                "sl_dist": sig * float(cfg["sl"]) * trgt,
                "tp_dist": sig * float(cfg["pt"]) * trgt,
            }
        if not m:
            raise HorizonLabError(f"No eligible events for {cfg['label']}")
        result[cfg["label"]] = m
    return result


def _xef_cols(columns: list[str]) -> tuple[str, str, str]:
    lower = {str(c).strip().lower(): c for c in columns}
    def p(names: list[str]) -> str | None:
        for x in names:
            if x in lower:
                return str(lower[x])
        return None
    t = p(["timestamp", "time", "datetime", "date_time"])
    b = p(["bidprice", "bid_price", "bid", "bid price"])
    a = p(["askprice", "ask_price", "ask", "ask price"])
    if t is None or b is None or a is None:
        raise HorizonLabError(f"Tick file missing timestamp/bid/ask. Columns={columns}")
    return t, b, a


def _xef_loader():
    from functools import lru_cache
    @lru_cache(maxsize=6)
    def load(path_str: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p = Path(path_str)
        h = pd.read_csv(p, nrows=0)
        tc, bc, ac = _xef_cols(list(h.columns))
        d = pd.read_csv(p, usecols=[tc, bc, ac])
        ts = to_datetime_iso(d[tc], utc=True, errors="coerce")
        bid = pd.to_numeric(d[bc], errors="coerce")
        ask = pd.to_numeric(d[ac], errors="coerce")
        good = ts.notna() & bid.notna() & ask.notna() & (bid > 0) & (ask > 0) & (ask >= bid)
        if not bool(good.any()):
            return np.array([], dtype="int64"), np.array([], dtype="float64"), np.array([], dtype="float64")
        q = pd.DataFrame({"ts": ts[good], "bid": bid[good], "ask": ask[good]}).sort_values("ts")
        return (
            q["ts"].astype("int64").to_numpy(dtype="int64"),
            q["bid"].to_numpy(dtype="float64"),
            q["ask"].to_numpy(dtype="float64"),
        )
    return load


def _xef_catalog_start(catalog: list[dict[str, Any]], t: pd.Timestamp) -> int:
    day = pd.Timestamp(t.date(), tz="UTC")
    for i, rec in enumerate(catalog):
        if pd.Timestamp(rec["end_date"]) >= day:
            return i
    return max(0, len(catalog) - 1)


def _xef_first_hit(px: np.ndarray, side: int, pt: float, sl: float) -> tuple[int | None, str | None]:
    if px.size == 0:
        return None, None
    if side == 1:
        pi = np.flatnonzero(px >= pt); si = np.flatnonzero(px <= sl)
    else:
        pi = np.flatnonzero(px <= pt); si = np.flatnonzero(px >= sl)
    p = int(pi[0]) if pi.size else None
    s = int(si[0]) if si.size else None
    if p is None and s is None: return None, None
    if s is None or (p is not None and p < s): return p, "pt"
    if p is None or s < p: return s, "sl"
    raise HorizonLabError("Executable quote touched separated PT and SL simultaneously.")


def _xef_event(
    t: pd.Timestamp,
    recs: dict[str, dict[str, Any]],
    catalog: list[dict[str, Any]],
    loader: Any,
    delay_ms: int,
    extra_bps: float,
) -> dict[str, dict[str, Any]]:
    threshold_ns = int(t.value) + int(delay_ms) * 1_000_000
    start_file = _xef_catalog_start(catalog, t)
    entry = None
    for fi in range(start_file, len(catalog)):
        ts, bid, ask = loader(str(catalog[fi]["path"]))
        if ts.size == 0: continue
        ix = int(np.searchsorted(ts, threshold_ns, side="right"))
        if ix < len(ts):
            entry = (fi, ix, int(ts[ix]), float(bid[ix]), float(ask[ix])); break
    if entry is None:
        return {k: {"status": "missing_entry_quote", "event_ts": t} for k in recs}

    fi0, ix0, ets, ebid, eask = entry
    sides = {int(v["side"]) for v in recs.values()}
    if len(sides) != 1: raise HorizonLabError("Inconsistent side for same event across configs")
    side = next(iter(sides))
    eprice = eask if side == 1 else ebid
    states = {}
    results = {}
    for label, r in recs.items():
        sl = eprice - r["sl_dist"] if side == 1 else eprice + r["sl_dist"]
        pt = eprice + r["tp_dist"] if side == 1 else eprice - r["tp_dist"]
        states[label] = {**r, "sl_level": float(sl), "pt_level": float(pt), "done": False}

    for fi in range(fi0, len(catalog)):
        ts, bid, ask = loader(str(catalog[fi]["path"]))
        if ts.size == 0: continue
        begin = ix0 if fi == fi0 else 0
        for label, s in states.items():
            if s["done"]: continue
            deadline = int(s["deadline_ns"])
            end = int(np.searchsorted(ts, deadline, side="right"))
            if end > begin:
                px = bid[begin:end] if side == 1 else ask[begin:end]
                rel, hit = _xef_first_hit(px, side, s["pt_level"], s["sl_level"])
                if hit is not None:
                    j = begin + int(rel)
                    observed = float(bid[j] if side == 1 else ask[j])
                    exit_px = float(s["pt_level"]) if hit == "pt" else observed
                    gross = (exit_px - eprice) if side == 1 else (eprice - exit_px)
                    extra = float(s["signal_mid"]) * float(extra_bps) / 10000.0
                    results[label] = {
                        "status": "resolved", "event_ts": t,
                        "entry_ts": pd.Timestamp(ets, tz="UTC"),
                        "exit_ts": pd.Timestamp(int(ts[j]), tz="UTC"),
                        "side": side, "signal_mid": s["signal_mid"], "event_vol": s["event_vol"],
                        "entry_bid": ebid, "entry_ask": eask, "entry_price": eprice,
                        "entry_spread": eask - ebid, "sl_dist": s["sl_dist"], "tp_dist": s["tp_dist"],
                        "sl_level": s["sl_level"], "pt_level": s["pt_level"],
                        "exit_price": exit_px, "exit_reason": hit,
                        "exec_r": (gross - extra) / float(s["sl_dist"]),
                    }
                    s["done"] = True
                    continue
            vix = int(np.searchsorted(ts, deadline, side="right"))
            if vix < len(ts):
                exit_px = float(bid[vix] if side == 1 else ask[vix])
                gross = (exit_px - eprice) if side == 1 else (eprice - exit_px)
                extra = float(s["signal_mid"]) * float(extra_bps) / 10000.0
                results[label] = {
                    "status": "resolved", "event_ts": t,
                    "entry_ts": pd.Timestamp(ets, tz="UTC"),
                    "exit_ts": pd.Timestamp(int(ts[vix]), tz="UTC"),
                    "side": side, "signal_mid": s["signal_mid"], "event_vol": s["event_vol"],
                    "entry_bid": ebid, "entry_ask": eask, "entry_price": eprice,
                    "entry_spread": eask - ebid, "sl_dist": s["sl_dist"], "tp_dist": s["tp_dist"],
                    "sl_level": s["sl_level"], "pt_level": s["pt_level"],
                    "exit_price": exit_px, "exit_reason": "vertical",
                    "exec_r": (gross - extra) / float(s["sl_dist"]),
                }
                s["done"] = True
        if all(x["done"] for x in states.values()): break
    for label, s in states.items():
        if label not in results:
            results[label] = {"status": "unresolved_tick_coverage", "event_ts": t}
    return results


def _xef_hold_bars(df: pd.DataFrame, bars: dict[str, Any]) -> pd.DataFrame:
    out = df.copy()
    if out.empty: return out
    dt = bars["dt"].astype("int64")
    ev = to_datetime_iso(out["event_ts"], utc=True, errors="coerce").astype("int64").to_numpy()
    ex = to_datetime_iso(out["exit_ts"], utc=True, errors="coerce").astype("int64").to_numpy()
    out["hold_bars"] = np.maximum(0, np.searchsorted(dt, ex, side="left") - np.searchsorted(dt, ev, side="left"))
    return out


def _xef_bootstrap(df: pd.DataFrame, reps: int) -> tuple[float, float]:
    if df.empty or reps <= 0: return float("nan"), float("nan")
    d = df.copy(); d["entry_ts"] = to_datetime_iso(d["entry_ts"], utc=True)
    week = d["entry_ts"].dt.floor("D") - pd.to_timedelta(d["entry_ts"].dt.weekday, unit="D")
    g = pd.DataFrame({"week": week, "r": d["exec_r"]}).groupby("week")["r"].agg(["sum", "count"])
    full = pd.date_range(g.index.min(), g.index.max(), freq="7D", tz="UTC")
    g = g.reindex(full, fill_value=0.0)
    sums, cnt = g["sum"].to_numpy(float), g["count"].to_numpy(float)
    rng = np.random.default_rng(42); vals = []
    for _ in range(int(reps)):
        ix = rng.integers(0, len(g), size=len(g)); n = cnt[ix].sum()
        if n > 0: vals.append(float(sums[ix].sum()/n))
    if not vals: return float("nan"), float("nan")
    return float(np.quantile(vals, .025)), float(np.quantile(vals, .975))


def _xef_dd(r: np.ndarray) -> float:
    if not len(r): return float("nan")
    eq = np.concatenate(([0.0], np.cumsum(r))); peak = np.maximum.accumulate(eq)
    return float(np.min(eq - peak))


def _xef_loss_streak(r: np.ndarray) -> int:
    best = cur = 0
    for x in r:
        if x < 0: cur += 1; best = max(best, cur)
        else: cur = 0
    return int(best)


def _xef_concurrency(df: pd.DataFrame) -> tuple[int, float]:
    import heapq
    heap = []; before = []; mx = 0
    for row in df.sort_values("entry_ts").itertuples(index=False):
        en = int(pd.Timestamp(row.entry_ts).value); ex = int(pd.Timestamp(row.exit_ts).value)
        while heap and heap[0] <= en: heapq.heappop(heap)
        before.append(len(heap)); heapq.heappush(heap, ex); mx = max(mx, len(heap))
    return int(mx), float(np.quantile(before, .95)) if before else float("nan")


def _xef_slots(df: pd.DataFrame, slots: int, weeks: float) -> tuple[int, float, float]:
    import heapq
    heap=[]; n=0; rs=[]
    for row in df.sort_values("entry_ts").itertuples(index=False):
        en=int(pd.Timestamp(row.entry_ts).value); ex=int(pd.Timestamp(row.exit_ts).value)
        while heap and heap[0] <= en: heapq.heappop(heap)
        if len(heap) >= slots: continue
        heapq.heappush(heap, ex); n += 1; rs.append(float(row.exec_r))
    return int(n), float(n/max(1,len(df))), float(np.sum(rs)/weeks) if weeks>0 else float("nan")


def _xef_summary(df: pd.DataFrame, cfg: dict[str, Any], expected: int, obs_days: float, reps: int) -> dict[str, Any]:
    d = df[df["status"] == "resolved"].copy()
    if d.empty: return {"label": cfg["label"], "is_control": cfg.get("is_control",False), "coverage_rate":0.0}
    d["entry_ts"] = to_datetime_iso(d["entry_ts"], utc=True); d["exit_ts"] = to_datetime_iso(d["exit_ts"], utc=True)
    d["hold_seconds"] = (d["exit_ts"]-d["entry_ts"]).dt.total_seconds().clip(lower=.001)
    r = pd.to_numeric(d["exec_r"], errors="coerce").to_numpy(float)
    hold_days = d["hold_seconds"].to_numpy(float)/86400.0; hold_h=d["hold_seconds"].to_numpy(float)/3600.0
    hb = pd.to_numeric(d["hold_bars"], errors="coerce").dropna().to_numpy(float)
    weeks = obs_days/7.0
    lo,hi=_xef_bootstrap(d,reps); mx,p95c=_xef_concurrency(d); s1=_xef_slots(d,1,weeks); s5=_xef_slots(d,5,weeks)
    spread_bps = d["entry_spread"].to_numpy(float)/d["signal_mid"].to_numpy(float)*10000.0
    drift_bps = np.abs(d["entry_price"].to_numpy(float)-d["signal_mid"].to_numpy(float))/d["signal_mid"].to_numpy(float)*10000.0
    lag_ms=(d["entry_ts"]-to_datetime_iso(d["event_ts"],utc=True)).dt.total_seconds().to_numpy(float)*1000.0
    pos=r[r>0].sum(); neg=abs(r[r<0].sum())
    total_hold=float(np.sum(hold_days)); total_r=float(np.sum(r))
    return {
        "label":cfg["label"], "is_control":bool(cfg.get("is_control",False)),
        "event_vol_lookback":cfg["event_vol_lookback"], "event_vol_ewm_span":cfg["event_vol_ewm_span"],
        "vertical_horizon":cfg["vertical_horizon"], "pt":cfg["pt"], "sl":cfg["sl"],
        "expected_events":int(expected), "resolved_trades":int(len(d)), "coverage_rate":float(len(d)/max(1,expected)),
        "exec_mean_R":float(np.mean(r)), "exec_total_R":total_r, "profit_factor_R":float(pos/neg) if neg>0 else float("inf"),
        "win_rate":float(np.mean(r>0)), "pt_rate":float(d["exit_reason"].eq("pt").mean()),
        "sl_rate":float(d["exit_reason"].eq("sl").mean()), "vertical_rate":float(d["exit_reason"].eq("vertical").mean()),
        "max_drawdown_R":_xef_dd(r), "max_loss_streak":_xef_loss_streak(r),
        "weekly_cluster_bootstrap_mean_R_95_lo":lo, "weekly_cluster_bootstrap_mean_R_95_hi":hi,
        "median_hold_bars":float(np.median(hb)), "p75_hold_bars":float(np.quantile(hb,.75)),
        "p90_hold_bars":float(np.quantile(hb,.90)), "p95_hold_bars":float(np.quantile(hb,.95)),
        "p99_hold_bars":float(np.quantile(hb,.99)), "max_hold_bars":float(np.max(hb)),
        "mean_hold_hours":float(np.mean(hold_h)), "median_hold_hours":float(np.median(hold_h)),
        "p90_hold_hours":float(np.quantile(hold_h,.90)), "p95_hold_hours":float(np.quantile(hold_h,.95)),
        "p99_hold_hours":float(np.quantile(hold_h,.99)), "max_hold_hours":float(np.max(hold_h)),
        "capital_time_velocity_R_per_occupied_day":float(total_r/total_hold) if total_hold>0 else float("nan"),
        "turnover_cycles_per_occupied_day":float(len(d)/total_hold) if total_hold>0 else float("nan"),
        "calendar_exec_R_per_week":float(total_r/weeks) if weeks>0 else float("nan"),
        "candidate_trades_per_week":float(len(d)/weeks) if weeks>0 else float("nan"),
        "slot1_accept_rate":s1[1], "slot1_exec_R_per_week":s1[2], "slot5_accept_rate":s5[1], "slot5_exec_R_per_week":s5[2],
        "max_candidate_concurrency":mx, "p95_entry_concurrency_before_entry":p95c,
        "median_entry_spread_bps":float(np.median(spread_bps)), "p95_entry_spread_bps":float(np.quantile(spread_bps,.95)),
        "median_entry_drift_bps":float(np.median(drift_bps)), "p95_entry_drift_bps":float(np.quantile(drift_bps,.95)),
        "median_entry_lag_ms":float(np.median(lag_ms)), "p95_entry_lag_ms":float(np.quantile(lag_ms,.95)),
    }


def run_execution_finalist_mode(args: Any) -> None:
    edge_root=Path(args.edge_root).resolve(); runtime,model_rows,candidate_events=import_edge(edge_root)
    edge_id=str(getattr(runtime,"EDGE_ID",edge_root.as_posix())); feature_names=schema_feature_names(runtime)
    control_vol=infer_control_event_vol(candidate_events,args.control_event_vol_lookback,args.control_event_vol_ewm_span)
    control_geom=infer_control_geometry(model_rows); min_vol=infer_min_event_vol(candidate_events)
    finalists=_xef_parse(args.execution_finalists)
    if not args.execution_no_control:
        c={"label":f"V2_CONTROL_H{control_vol['lookback']}_E{control_vol['span']}_V{control_geom['vertical_horizon']}_PT{control_geom['pt']:g}_SL{control_geom['sl']:g}",
           "event_vol_lookback":int(control_vol["lookback"]),"event_vol_ewm_span":int(control_vol["span"]),
           "vertical_horizon":int(control_geom["vertical_horizon"]),"pt":float(control_geom["pt"]),"sl":float(control_geom["sl"]),"is_control":True}
        sig=lambda x:(x["event_vol_lookback"],x["event_vol_ewm_span"],x["vertical_horizon"],x["pt"],x["sl"])
        if sig(c) not in {sig(x) for x in finalists}: finalists=[c,*finalists]

    print("QUANTARA V3 — EXACT BID/ASK EXECUTION + CAPITAL VELOCITY")
    print(f"edge: {edge_id}"); print(f"entry_delay_ms: {args.execution_entry_delay_ms}")
    print(f"extra_cost_bps: {args.cost_bps} (raw Bid/Ask spread already embedded)")
    for x in finalists: print(" -",x["label"],"[CONTROL]" if x.get("is_control") else "")

    events,paths=_xef_artifacts(edge_root); bars=_xef_bars(runtime)
    valid=_xef_valid_times(paths,feature_names,finalists,control_geom)
    maps=_xef_prepare(finalists,events,bars,valid,min_vol)
    if not hasattr(model_rows,"build_tick_file_catalog"): raise HorizonLabError("model_rows has no build_tick_file_catalog")
    catalog=model_rows.build_tick_file_catalog(); loader=_xef_loader(); cfg={x["label"]:x for x in finalists}
    times=sorted(set().union(*(set(m.keys()) for m in maps.values())))
    obs_days=max(1e-9,(times[-1]-times[0]).total_seconds()/86400.0)
    rows={k:[] for k in cfg}; started=time.time()
    print(f"Exact quote replay events: {len(times)}")
    for i,t in enumerate(times,1):
        recs={k:m[t] for k,m in maps.items() if t in m}
        rr=_xef_event(t,recs,catalog,loader,int(args.execution_entry_delay_ms),float(args.cost_bps))
        for k,v in rr.items(): v["config_label"]=k; rows[k].append(v)
        if i%250==0 or i==len(times): print(f"  {i}/{len(times)} elapsed={time.time()-started:.1f}s")

    frames={}; summaries=[]
    for x in finalists:
        d=pd.DataFrame(rows[x["label"]]); good=d["status"].eq("resolved") if "status" in d.columns else pd.Series(False,index=d.index)
        if good.any():
            res=_xef_hold_bars(d.loc[good].copy(),bars); d=pd.concat([res,d.loc[~good]],ignore_index=True,sort=False)
        frames[x["label"]]=d; summaries.append(_xef_summary(d,x,len(maps[x["label"]]),obs_days,int(args.execution_bootstrap_reps)))

    ctrl=next((s for s in summaries if s.get("is_control")),None)
    if ctrl:
        cv=float(ctrl.get("capital_time_velocity_R_per_occupied_day",np.nan)); ch=float(ctrl.get("mean_hold_hours",np.nan))
        for s in summaries:
            v=float(s.get("capital_time_velocity_R_per_occupied_day",np.nan)); h=float(s.get("mean_hold_hours",np.nan))
            s["velocity_multiple_vs_v2_control"] = v/cv if np.isfinite(v) and np.isfinite(cv) and cv!=0 else float("nan")
            s["mean_hold_speedup_vs_v2_control"] = ch/h if np.isfinite(ch) and np.isfinite(h) and h>0 else float("nan")

    print("\nEXECUTABLE SUMMARY")
    print("label | meanR | 95%CI | PF | mean_hold_h | p95_hold_h | R/occupied_day | R/week | velocity_xV2 | hold_speedup_xV2 | coverage")
    for s in summaries:
        print(f"{s['label']} | {s.get('exec_mean_R',np.nan):+.6f} | [{s.get('weekly_cluster_bootstrap_mean_R_95_lo',np.nan):+.6f},{s.get('weekly_cluster_bootstrap_mean_R_95_hi',np.nan):+.6f}] | {s.get('profit_factor_R',np.nan):.4f} | {s.get('mean_hold_hours',np.nan):.3f} | {s.get('p95_hold_hours',np.nan):.3f} | {s.get('capital_time_velocity_R_per_occupied_day',np.nan):+.6f} | {s.get('calendar_exec_R_per_week',np.nan):+.6f} | {s.get('velocity_multiple_vs_v2_control',np.nan):.3f} | {s.get('mean_hold_speedup_vs_v2_control',np.nan):.3f} | {s.get('coverage_rate',np.nan):.4f}")

    ranked=[s for s in summaries if not s.get("is_control") and s.get("coverage_rate",0)>=.99 and s.get("exec_mean_R",-1)>0]
    ranked.sort(key=lambda s:s.get("capital_time_velocity_R_per_occupied_day",-np.inf),reverse=True)
    print("\nPROVISIONAL CAPITAL-VELOCITY RANKING (geometry only; full 00->09 still required)")
    for i,s in enumerate(ranked,1): print(f" {i}. {s['label']} | meanR={s['exec_mean_R']:+.6f} | R/occupied_day={s['capital_time_velocity_R_per_occupied_day']:+.6f} | R/week={s['calendar_exec_R_per_week']:+.6f} | mean_hold_h={s['mean_hold_hours']:.3f}")
    if not ranked: print(" No finalist has positive executable mean with >=99% coverage.")

    if args.save:
        out=edge_root/"artifacts"/"v3_horizon_lab"; out.mkdir(parents=True,exist_ok=True); stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sp=out/f"v3_execution_velocity_summary_{stamp}.csv"; pd.DataFrame(summaries).to_csv(sp,index=False)
        for k,d in frames.items(): d.to_csv(out/f"v3_execution_trades_{k}_{stamp}.csv",index=False)
        ap=out/f"v3_execution_velocity_audit_{stamp}.json"
        ap.write_text(json.dumps({"status":"V3_EXECUTION_FINALIST_REPLAY_COMPLETE_RESEARCH_ONLY","edge_id":edge_id,
          "entry_contract":"first valid quote strictly after event close + delay; long Ask / short Bid",
          "distance_contract":"signal-mid volatility distances rebuilt around executable entry, matching live executor",
          "exit_contract":"long Bid / short Ask; PT capped at target; adverse SL quote retained; vertical first quote after deadline",
          "raw_bid_ask_spread_embedded":True,"extra_cost_bps":float(args.cost_bps),"entry_delay_ms":int(args.execution_entry_delay_ms),
          "capital_velocity":"sum executable R / sum occupied risk-days (equal-1R slot diagnostic)","summary":summaries,
          "ranking":[s["label"] for s in ranked],"note":"Candidate-event geometry selection only. Full 00->09 retraining/calibration/replay remains mandatory."},indent=2,default=str)+"\n",encoding="utf-8")
        print("\nSaved:",sp,"\n",ap,"\n per-config trade CSVs under",out)
    print("\nNo edge/model/live artifact was modified.")



# =============================================================================
# V3 HYBRID FAST GRID
# =============================================================================
# Marker: V3_HYBRID_FAST_GRID_V2

def _hfg_parse_ints(text: str, name: str) -> list[int]:
    vals = [int(x.strip()) for x in str(text).split(",") if x.strip()]
    if not vals or any(v <= 0 for v in vals):
        raise HorizonLabError(f"{name} must contain positive comma-separated integers.")
    return list(dict.fromkeys(vals))


def _hfg_parse_barriers(text: str) -> list[tuple[float, float]]:
    out = []
    for raw in str(text).split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(":")
        if len(parts) != 2:
            raise HorizonLabError("--barriers must use PT:SL format.")
        pt, sl = float(parts[0]), float(parts[1])
        if pt <= 0 or sl <= 0:
            raise HorizonLabError("PT/SL multipliers must be > 0.")
        out.append((pt, sl))
    if not out:
        raise HorizonLabError("--barriers produced no values.")
    return list(dict.fromkeys(out))


def _hfg_v2_root(edge_root: Path) -> Path:
    root = edge_root.resolve().parents[3]
    if not (root / "assets").exists():
        raise HorizonLabError(f"Could not resolve trading_models_v2 root: {root}")
    return root


def _hfg_read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _hfg_artifact_paths(edge_root: Path) -> dict[str, Path | None]:
    v2_root = _hfg_v2_root(edge_root)
    manifest_path = edge_root / "artifacts" / "research_dataset" / "research_dataset_manifest.json"
    manifest = _hfg_read_json(manifest_path)
    outputs = (manifest or {}).get("outputs") or {}

    def resolve(key: str) -> Path | None:
        raw = outputs.get(key)
        if not raw:
            return None
        p = Path(str(raw))
        if not p.is_absolute():
            p = v2_root / p
        return p if p.exists() else None

    mr_manifest = edge_root / "artifacts" / "research_dataset" / "model_row_manifest.json"
    return {
        "feature_matrix": resolve("feature_matrix"),
        "candidate_events": resolve("candidate_events"),
        "model_rows": resolve("model_rows"),
        "model_row_manifest": mr_manifest if mr_manifest.exists() else None,
    }


def _hfg_load_bars(runtime: Any) -> dict[str, Any]:
    if not hasattr(runtime, "_load_asset_bars"):
        raise HorizonLabError("Hybrid fast grid requires runtime._load_asset_bars().")
    b = runtime._load_asset_bars()  # noqa: SLF001
    required = ["Datetime", "Bar_Start", "High", "Low", "Close"]
    missing = [c for c in required if c not in b.columns]
    if missing:
        raise HorizonLabError(f"Bar table missing columns: {missing}")
    b = b.copy()
    b["Datetime"] = to_datetime_iso(b["Datetime"], utc=True, errors="coerce")
    b["Bar_Start"] = to_datetime_iso(b["Bar_Start"], utc=True, errors="coerce")
    for c in ["High", "Low", "Close"]:
        b[c] = pd.to_numeric(b[c], errors="coerce")
    b = b.dropna(subset=required).sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)
    if b.empty:
        raise HorizonLabError("Bar table is empty.")
    return {
        "frame": b,
        "dt": pd.DatetimeIndex(b["Datetime"]),
        "bar_start": pd.DatetimeIndex(b["Bar_Start"]),
        "high": b["High"].to_numpy(dtype="float64"),
        "low": b["Low"].to_numpy(dtype="float64"),
        "close": b["Close"].to_numpy(dtype="float64"),
    }


def _hfg_valid_times(frame: pd.DataFrame, feature_names: list[str]) -> set[pd.Timestamp]:
    missing = [c for c in ["Datetime", *feature_names] if c not in frame.columns]
    if missing:
        raise HorizonLabError(f"Feature frame missing schema columns: {missing[:20]}")
    f = frame[["Datetime", *feature_names]].copy()
    f["Datetime"] = to_datetime_iso(f["Datetime"], utc=True, errors="coerce")
    for c in feature_names:
        f[c] = pd.to_numeric(f[c], errors="coerce")
    ok = (
        f[feature_names].replace([np.inf, -np.inf], np.nan).notna().all(axis=1)
        & f["Datetime"].notna()
    )
    return set(pd.Timestamp(x) for x in f.loc[ok, "Datetime"])


def _hfg_normalize_events(events: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in ["Datetime", "side"] if c not in events.columns]
    if missing:
        raise HorizonLabError(f"Candidate events missing columns: {missing}")
    e = events.copy()
    e["Datetime"] = to_datetime_iso(e["Datetime"], utc=True, errors="coerce")
    e["side"] = pd.to_numeric(e["side"], errors="coerce")
    e = e.dropna(subset=["Datetime", "side"]).sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)
    if e.empty:
        raise HorizonLabError("Candidate event table is empty.")
    if not set(e["side"].astype(int).unique()).issubset({-1, 1}):
        raise HorizonLabError("Candidate events contain invalid side values.")
    return e


def _hfg_event_vol(bars: dict[str, Any], lookback: int, span: int, min_vol: float) -> pd.Series:
    close = pd.Series(bars["close"], dtype="float64")
    ret = np.log((close + EPS) / (close.shift(int(lookback)) + EPS))
    vol = ret.ewm(span=int(span), adjust=False).std().clip(lower=float(min_vol))
    return pd.Series(vol.to_numpy(dtype="float64"), index=bars["dt"], name="Event_Vol")


def _hfg_attach_target(base_events: pd.DataFrame, vol: pd.Series) -> pd.DataFrame:
    e = base_events.copy()
    e["Event_Vol"] = pd.DatetimeIndex(e["Datetime"]).map(vol)
    e["Event_Vol"] = pd.to_numeric(e["Event_Vol"], errors="coerce")
    e = e[np.isfinite(e["Event_Vol"].to_numpy(dtype="float64")) & (e["Event_Vol"] > 0)].copy()
    if e.empty:
        raise HorizonLabError("No target-valid candidate events.")
    return e.reset_index(drop=True)


def _hfg_prepare(edge_root: Path, runtime: Any, feature_names: list[str]) -> dict[str, Any]:
    paths = _hfg_artifact_paths(edge_root)
    features = None
    events = None
    valid_times = None
    runtime_builds = 0
    notes = []

    if paths["feature_matrix"] is not None:
        try:
            features = pd.read_csv(paths["feature_matrix"])
            valid_times = _hfg_valid_times(features, feature_names)
            notes.append("features=persisted_artifact")
        except Exception as exc:
            notes.append(f"feature_artifact_rejected={type(exc).__name__}")
            features = None
            valid_times = None

    if paths["candidate_events"] is not None:
        try:
            events = _hfg_normalize_events(pd.read_csv(paths["candidate_events"]))
            notes.append("events=persisted_artifact")
        except Exception as exc:
            notes.append(f"event_artifact_rejected={type(exc).__name__}")
            events = None

    if features is None or valid_times is None or events is None:
        print("Fast artifacts incomplete -> runtime fallback for missing pre-model inputs.")
        runtime_features = runtime.build_features(drop_warmup_rows=False)
        runtime_builds += 1
        if features is None or valid_times is None:
            features = runtime_features
            valid_times = _hfg_valid_times(runtime_features, feature_names)
            notes.append("features=runtime_fallback")
        if events is None:
            events = _hfg_normalize_events(runtime.generate_candidate_events(runtime_features))
            notes.append("events=runtime_fallback")

    return {
        "paths": paths,
        "features": features,
        "events": events,
        "valid_times": valid_times,
        "runtime_feature_builds": runtime_builds,
        "notes": notes,
    }


def _hfg_reference(
    edge_root: Path,
    runtime: Any,
    bars: dict[str, Any],
    prepared: dict[str, Any],
    feature_names: list[str],
) -> dict[str, Any]:
    path = prepared["paths"]["model_row_manifest"]
    if path is not None:
        manifest = _hfg_read_json(path)
        if manifest:
            return {"source": "persisted_model_row_manifest", "manifest": manifest}

    print("No model_row_manifest -> building current control in memory for parity.")
    try:
        rows, manifest = runtime.build_model_rows(
            features=prepared["features"],
            events=prepared["events"],
            bars=bars["frame"],
        )
    except Exception:
        runtime_features = runtime.build_features(drop_warmup_rows=False)
        prepared["runtime_feature_builds"] += 1
        prepared["features"] = runtime_features
        prepared["valid_times"] = _hfg_valid_times(runtime_features, feature_names)
        prepared["events"] = _hfg_normalize_events(runtime.generate_candidate_events(runtime_features))
        rows, manifest = runtime.build_model_rows(
            features=runtime_features,
            events=prepared["events"],
            bars=bars["frame"],
        )

    if rows.empty:
        raise HorizonLabError("In-memory control model-row build is empty.")
    if not isinstance(manifest, dict):
        manifest = {}
    if "rows_after_feature_nan_filter" not in manifest:
        manifest["rows_after_feature_nan_filter"] = int(len(rows))
    if "barrier_hit_distribution" not in manifest:
        hit = rows["barrier_hit"].astype(str)
        manifest["barrier_hit_distribution"] = {
            "pt": float(hit.eq("pt").mean()),
            "sl": float(hit.eq("sl").mean()),
            "vertical": float(hit.eq("vertical").mean()),
        }
    return {"source": "runtime_in_memory_control", "manifest": manifest}


def _hfg_parity(
    model_rows: Any,
    bars: dict[str, Any],
    prepared: dict[str, Any],
    reference: dict[str, Any],
    control_vol: dict[str, Any],
    control_geometry: dict[str, Any],
    min_vol: float,
) -> None:
    vol = _hfg_event_vol(bars, control_vol["lookback"], control_vol["span"], min_vol)
    events = _hfg_attach_target(prepared["events"], vol)
    paths = compute_first_touch_paths(
        model_rows=model_rows,
        bars=bars,
        events=events,
        max_horizon=int(control_geometry["vertical_horizon"]),
        pt=float(control_geometry["pt"]),
        sl=float(control_geometry["sl"]),
    )
    _, summary = materialize_horizon(
        bars=bars,
        events=events,
        paths=paths,
        valid_feature_times=prepared["valid_times"],
        horizon=int(control_geometry["vertical_horizon"]),
        pt=float(control_geometry["pt"]),
        sl=float(control_geometry["sl"]),
        cost_bps=0.0,
    )

    manifest = reference["manifest"]
    expected_rows = int(manifest.get("rows_after_feature_nan_filter", -1))
    if int(summary["rows"]) != expected_rows:
        raise HorizonLabError(
            f"HYBRID FAST CONTROL PARITY FAILED: rows expected={expected_rows} actual={summary['rows']}"
        )

    dist = manifest.get("barrier_hit_distribution") or {}
    for key, actual_key in (("pt", "pt_rate"), ("sl", "sl_rate"), ("vertical", "vertical_rate")):
        if key in dist and not math.isclose(
            float(dist[key]), float(summary[actual_key]), rel_tol=0.0, abs_tol=1e-10
        ):
            raise HorizonLabError(
                f"HYBRID FAST CONTROL PARITY FAILED: {key} expected={dist[key]} actual={summary[actual_key]}"
            )

    print(
        "HYBRID FAST CONTROL PARITY: PASS | "
        f"reference={reference['source']} | rows={summary['rows']}"
    )


def run_hybrid_fast_grid_mode(args: Any) -> None:
    edge_root = Path(args.edge_root).resolve()
    runtime, model_rows, candidate_events = import_edge(edge_root)
    edge_id = str(getattr(runtime, "EDGE_ID", edge_root.as_posix()))
    feature_names = schema_feature_names(runtime)

    lookbacks = _hfg_parse_ints(args.event_vol_lookbacks, "--event-vol-lookbacks")
    spans = _hfg_parse_ints(args.event_vol_ewm_spans, "--event-vol-ewm-spans")
    horizons = _hfg_parse_ints(args.vertical_horizons, "--vertical-horizons")
    barriers = _hfg_parse_barriers(args.barriers)

    control_vol = infer_control_event_vol(
        candidate_events,
        args.control_event_vol_lookback,
        args.control_event_vol_ewm_span,
    )
    control_geometry = infer_control_geometry(model_rows)
    min_vol = infer_min_event_vol(candidate_events)

    print("QUANTARA V3 — HYBRID FAST GENERIC GEOMETRY GRID")
    print("PRE-LIFECYCLE ONLY")
    print("existing edge => artifact-first; new/incomplete edge => runtime fallback")
    print(f"edge: {edge_id}")
    print()

    started = time.time()
    bars = _hfg_load_bars(runtime)
    prepared = _hfg_prepare(edge_root, runtime, feature_names)

    for note in prepared["notes"]:
        print(" -", note)
    print(" - runtime_feature_builds:", prepared["runtime_feature_builds"])
    print(" - candidate_events:", len(prepared["events"]))

    reference = _hfg_reference(
        edge_root, runtime, bars, prepared, feature_names
    )

    print()
    print("CONTROL PARITY GATE")
    _hfg_parity(
        model_rows, bars, prepared, reference,
        control_vol, control_geometry, min_vol
    )

    results = []

    for hvol in lookbacks:
        for span in spans:
            events = _hfg_attach_target(
                prepared["events"],
                _hfg_event_vol(bars, hvol, span, min_vol),
            )
            print()
            print("=" * 100)
            print(f"Hvol={hvol} | EWM={span} | events={len(events)}")
            print("=" * 100)

            for pt, sl in barriers:
                paths = compute_first_touch_paths(
                    model_rows=model_rows,
                    bars=bars,
                    events=events,
                    max_horizon=max(horizons),
                    pt=pt,
                    sl=sl,
                )
                for hv in horizons:
                    _, summary = materialize_horizon(
                        bars=bars,
                        events=events,
                        paths=paths,
                        valid_feature_times=prepared["valid_times"],
                        horizon=hv,
                        pt=pt,
                        sl=sl,
                        cost_bps=float(args.cost_bps),
                    )
                    mean_hold = float(summary["mean_hold_bars"])
                    net_r = float(summary["net_mean_R"])
                    velocity = (
                        net_r / mean_hold
                        if np.isfinite(net_r) and np.isfinite(mean_hold) and mean_hold > 0
                        else float("nan")
                    )
                    row = {
                        "edge_id": edge_id,
                        "event_vol_lookback": hvol,
                        "event_vol_ewm_span": span,
                        "vertical_horizon": hv,
                        "pt": pt,
                        "sl": sl,
                        "cost_bps": float(args.cost_bps),
                        **summary,
                        "net_R_per_mean_hold_bar": velocity,
                    }
                    results.append(row)
                    print(
                        f"H={hvol:>3} EWM={span:>3} Hv={hv:>4} {pt:g}:{sl:g} | "
                        f"netR={net_r:+.5f} | meanHold={mean_hold:.2f} | "
                        f"R/meanHoldBar={velocity:+.6f}"
                    )

    frame = pd.DataFrame(results)
    frontier = frame[
        (pd.to_numeric(frame["net_mean_R"], errors="coerce") > 0)
        & np.isfinite(pd.to_numeric(frame["net_R_per_mean_hold_bar"], errors="coerce"))
    ].copy().sort_values(
        ["net_R_per_mean_hold_bar", "net_mean_R"],
        ascending=[False, False],
    )

    print()
    print("PRE-LIFECYCLE CAPITAL-TURNOVER FRONTIER")
    for rank, row in enumerate(frontier.head(15).itertuples(index=False), start=1):
        print(
            f"{rank:>2}. H={row.event_vol_lookback} EWM={row.event_vol_ewm_span} "
            f"Hv={row.vertical_horizon} PT:SL={row.pt:g}:{row.sl:g} | "
            f"netR={row.net_mean_R:+.6f} | meanHold={row.mean_hold_bars:.3f} | "
            f"R/meanHoldBar={row.net_R_per_mean_hold_bar:+.6f}"
        )

    if args.save:
        out_dir = edge_root / "artifacts" / "v3_horizon_lab"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        csv_path = out_dir / f"v3_hybrid_fast_grid_{stamp}.csv"
        json_path = out_dir / f"v3_hybrid_fast_grid_{stamp}.json"
        frame.to_csv(csv_path, index=False)
        json_path.write_text(
            json.dumps(
                {
                    "edge_id": edge_id,
                    "status": "V3_PRE_LIFECYCLE_HYBRID_FAST_GRID_COMPLETE",
                    "control_reference": reference["source"],
                    "runtime_feature_builds": prepared["runtime_feature_builds"],
                    "input_notes": prepared["notes"],
                    "selection_boundary": (
                        "Geometry screen only. No model/OOF/calibration/threshold/"
                        "allocator/live decision is made here."
                    ),
                    "rows": results,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ) + "\n",
            encoding="utf-8",
        )
        print()
        print("Saved:")
        print("-", csv_path)
        print("-", json_path)

    print()
    print(f"TOTAL ELAPSED: {time.time() - started:.2f}s")
    print("No canonical research/model/threshold/allocator/live artifact was modified.")


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

    parser.add_argument(
        "--execution-finalists",
        default=None,
        help=("Run exact Bid/Ask finalist mode instead of grid. Format: "
              "Hvol:EWM:Hv:PT:SL comma-separated."),
    )
    parser.add_argument(
        "--execution-entry-delay-ms",
        type=int,
        default=0,
        help="Delay after event-bar close; entry is first valid quote strictly after close+delay.",
    )
    parser.add_argument(
        "--execution-bootstrap-reps",
        type=int,
        default=2000,
        help="Calendar-week cluster bootstrap repetitions.",
    )
    parser.add_argument(
        "--execution-no-control",
        action="store_true",
        help="Do not automatically include frozen V2 geometry benchmark.",
    )

    parser.add_argument(
        "--hybrid-fast-grid",
        action="store_true",
        help=(
            "Artifact-first pre-lifecycle grid with automatic in-memory runtime "
            "fallback for new/incomplete edges."
        ),
    )

    args = parser.parse_args()

    if getattr(args, "hybrid_fast_grid", False):
        run_hybrid_fast_grid_mode(args)
        return


    if args.execution_finalists:
        if int(args.execution_entry_delay_ms) < 0:
            raise SystemExit("--execution-entry-delay-ms must be >= 0")
        if int(args.execution_bootstrap_reps) < 0:
            raise SystemExit("--execution-bootstrap-reps must be >= 0")
        run_execution_finalist_mode(args)
        return

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
