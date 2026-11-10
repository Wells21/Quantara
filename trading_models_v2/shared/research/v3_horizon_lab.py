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
import ast
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
    """REVISED 2026-09-24 (calendar-time labeling rewrite): also checks
    EVENT_VOL_LOOKBACK_HOURS (the pilot edge's new calendar-time constant,
    see config/us500_edge_search_ledger.json's
    calendar_time_labeling_REWRITE_2026-09-24 entry) before falling back to
    the legacy bar-count names. Returns "calendar_time": True and populates
    "lookback_hours" (with "lookback" left None) when the calendar-time
    constant is what was found -- every edge not yet migrated keeps
    returning bar-count "lookback" exactly as before."""
    # Precedence matters. EVENT_VOL_LOOKBACK_HOURS (calendar-time) is
    # checked first now, then EVENT_VOL_* (bar-count, explicit), then VOL_*
    # (a few older packages use this name for the same target, fallback only).
    hours_attr = None if override_lookback is not None else _numeric_attr(
        candidate_events, ["EVENT_VOL_LOOKBACK_HOURS"],
    )
    if hours_attr is not None:
        span = (
            ("CLI", float(override_span))
            if override_span is not None
            else _numeric_attr(candidate_events, ["EVENT_VOL_EWM_SPAN", "VOL_EWM_SPAN"])
        )
        if span is None:
            raise HorizonLabError("Could not infer the edge's current Event_Vol control span.")
        return {
            "calendar_time": True,
            "lookback_source": hours_attr[0],
            "lookback_hours": float(hours_attr[1]),
            "lookback": None,
            "span_source": span[0],
            "span": int(span[1]),
        }

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
        "calendar_time": False,
        "lookback_source": lookback[0],
        "lookback": int(lookback[1]),
        "lookback_hours": None,
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
    """REVISED 2026-09-24 (calendar-time labeling rewrite, see
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry): dual-compatible with both the legacy bar-count vertical_horizon
    (VERTICAL_HORIZON / "vertical_horizon") and the new calendar-time
    vertical_horizon_hours (VERTICAL_HORIZON_HOURS / "vertical_horizon_hours"),
    since only the pilot edge has been migrated so far and every other edge
    must keep working unchanged. The returned dict's "calendar_time" flag
    tells callers which geometry field is actually populated."""
    config: dict[str, Any] = {}
    if hasattr(model_rows, "model_row_config"):
        try:
            value = model_rows.model_row_config()
            if isinstance(value, dict):
                config = value
        except Exception:
            config = {}

    vertical_hours = config.get(
        "vertical_horizon_hours",
        getattr(model_rows, "VERTICAL_HORIZON_HOURS", None),
    )
    vertical_bars = config.get(
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

    if (vertical_hours is None and vertical_bars is None) or pt is None or sl is None:
        raise HorizonLabError(
            "Could not infer control vertical/PT/SL geometry from model_rows.py."
        )

    calendar_time = vertical_hours is not None
    return {
        "calendar_time": calendar_time,
        "vertical_horizon_hours": float(vertical_hours) if calendar_time else None,
        "vertical_horizon": int(vertical_bars) if vertical_bars is not None else None,
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


# =============================================================================
# Generic same-bar ambiguity resolver dispatch
# =============================================================================
# Marker: V3_SAME_BAR_RESOLVER_DISPATCH_V2

def _v3_select_same_bar_resolver(model_rows: Any) -> tuple[str | None, Any | None]:
    """Return the edge-local tick resolver without mutating the edge module."""
    for name in ("_resolve_tick_first_touch", "_tick_first_touch"):
        resolver = getattr(model_rows, name, None)
        if callable(resolver):
            return name, resolver
    return None, None


def _v3_has_fallback_same_bar_contract(model_rows: Any) -> bool:
    if callable(getattr(model_rows, "_resolve_fallback_tie", None)):
        return True
    if callable(getattr(model_rows, "_fallback", None)):
        return True
    return str(getattr(model_rows, "FALLBACK_TIE_POLICY", "")).strip().lower() in {
        "sl_first", "pt_first", "drop"
    }


def build_tick_catalog_if_needed(model_rows: Any) -> Any:
    """Build raw-tick catalog only when the edge actually uses a tick resolver."""
    resolver_name, resolver = _v3_select_same_bar_resolver(model_rows)
    if resolver is None:
        if _v3_has_fallback_same_bar_contract(model_rows):
            return None
        raise HorizonLabError(
            "Same-bar PT/SL ambiguity occurred but model_rows.py exposes neither "
            "a supported tick resolver (_resolve_tick_first_touch / _tick_first_touch) "
            "nor a canonical fallback tie contract."
        )

    builder = getattr(model_rows, "build_tick_file_catalog", None)
    if not callable(builder):
        raise HorizonLabError(
            "Same-bar PT/SL ambiguity requires raw ticks for resolver "
            f"{resolver_name}, but model_rows.py does not expose "
            "build_tick_file_catalog()."
        )
    return builder()

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
    """Resolve same-bar ambiguity using the edge's own frozen contract."""
    import inspect

    requested_side = int(side)
    if requested_side not in (-1, 1):
        raise HorizonLabError(
            f"Unsupported event side for same-bar resolution: {side!r}"
        )

    resolver_name, resolver = _v3_select_same_bar_resolver(model_rows)
    if resolver is not None:
        try:
            signature = inspect.signature(resolver)
        except Exception as exc:
            raise HorizonLabError(
                f"Could not inspect {resolver_name} signature."
            ) from exc

        params = list(signature.parameters.values())
        positional = [
            p for p in params
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params
        )

        args5 = (
            tick_catalog,
            bar_start,
            bar_end,
            float(pt_price),
            float(sl_price),
        )

        if has_varargs or len(positional) == 6:
            result = resolver(*args5, requested_side)
        elif len(positional) == 5:
            expected_side = getattr(model_rows, "SIDE_VALUE", None)
            try:
                expected_side = int(expected_side)
            except Exception as exc:
                raise HorizonLabError(
                    f"Legacy 5-argument resolver {resolver_name} requires "
                    "model_rows.SIDE_VALUE so side semantics can be verified."
                ) from exc
            if expected_side != requested_side:
                raise HorizonLabError(
                    f"Legacy resolver {resolver_name} is side-specialized: "
                    f"SIDE_VALUE={expected_side}, requested_side={requested_side}."
                )
            result = resolver(*args5)
        else:
            raise HorizonLabError(
                f"Unsupported {resolver_name} signature {signature}; expected "
                "5 legacy positional args or 6 current positional args."
            )

        if not isinstance(result, tuple) or len(result) != 3:
            raise HorizonLabError(
                f"{resolver_name} returned unsupported result {result!r}; "
                "expected (label, hit, source)."
            )
        label, hit, source = result
        return (
            float(label) if label is not None else float("nan"),
            hit,
            str(source),
        )

    fallback = getattr(model_rows, "_resolve_fallback_tie", None)
    if callable(fallback):
        result = fallback()
        if not isinstance(result, tuple) or len(result) not in (2, 3):
            raise HorizonLabError(
                "_resolve_fallback_tie() returned an unsupported result; "
                "expected (label, hit) or (label, hit, source)."
            )
        label, hit = result[0], result[1]
        source = result[2] if len(result) == 3 else "fallback_edge_contract"
        return (
            float(label) if label is not None else float("nan"),
            hit,
            str(source),
        )

    fallback = getattr(model_rows, "_fallback", None)
    if callable(fallback):
        result = fallback()
        if not isinstance(result, tuple) or len(result) not in (2, 3):
            raise HorizonLabError(
                "_fallback() returned an unsupported result; expected "
                "(label, hit) or (label, hit, source)."
            )
        label, hit = result[0], result[1]
        source = result[2] if len(result) == 3 else "fallback_edge_contract"
        return (
            float(label) if label is not None else float("nan"),
            hit,
            str(source),
        )

    policy = str(getattr(model_rows, "FALLBACK_TIE_POLICY", "")).strip().lower()
    if policy == "sl_first":
        return 0.0, "sl", "fallback_sl_first"
    if policy == "pt_first":
        return 1.0, "pt", "fallback_pt_first"
    if policy == "drop":
        return float("nan"), None, "fallback_drop"

    raise HorizonLabError(
        "Same-bar PT/SL ambiguity occurred but no supported edge-local tick "
        "resolver or fallback contract is available."
    )

def compute_first_touch_paths(
    *,
    model_rows: Any,
    bars: dict[str, Any],
    events: pd.DataFrame,
    max_horizon: int,
    pt: float,
    sl: float,
    max_horizon_hours: float | None = None,
) -> dict[str, Any]:
    """max_horizon_hours: REVISED 2026-09-24 (calendar-time labeling
    rewrite). When provided, the per-event PT/SL search window is a real
    calendar-time deadline (converted to a per-event bar offset via
    shared.labels.calendar_time_barriers, since a fixed bar count spans a
    variable amount of real time -- see
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry) instead of the uniform max_horizon bar count. max_horizon is
    still required in this case as an outer bound (the largest calendar-time
    horizon in the grid, pre-converted by the caller) so array sizing stays
    identical either way. Default None preserves the original bar-count-only
    behavior exactly, for every edge not yet migrated to calendar-time."""
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

    per_event_horizon: np.ndarray | None = None
    if max_horizon_hours is not None:
        from shared.labels.calendar_time_barriers import calendar_time_horizon_bar_offsets
        # QUANTARA_V3_ASI8_NS_FIX: DatetimeIndex.asi8 is not guaranteed nanoseconds
        # across pandas versions (venv39=ns, venv312=microseconds by default) --
        # normalize explicitly or horizons compute far too wide on venv312.
        dt_ns = dt.as_unit("ns").asi8
        valid_locs = np.where(locs >= 0, locs, 0)
        per_event_horizon = calendar_time_horizon_bar_offsets(dt_ns, valid_locs, hours=max_horizon_hours)

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

        if per_event_horizon is not None:
            event_horizon = int(per_event_horizon[j])
            if event_horizon < 0:
                continue  # deadline runs past end of history, mirrors AFML's t1 filter
            available = min(event_horizon, n - int(loc) - 1)
        else:
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
    horizon_hours: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """horizon_hours: REVISED 2026-09-24 (calendar-time labeling rewrite).
    When provided, the vertical-barrier deadline is a real calendar-time
    offset from each event's own entry time (converted to a per-event bar
    offset, since a fixed bar count spans a variable amount of real time --
    see config/us500_edge_search_ledger.json's
    calendar_time_labeling_REWRITE_2026-09-24 entry), instead of the
    uniform `horizon` bar count used for every event alike. `horizon` is
    still required in this case (paths were computed with a matching
    max_horizon_hours upper bound) but is not itself used for eligibility
    or truncation when horizon_hours is set. Default None preserves the
    original bar-count-only behavior exactly, for every edge not yet
    migrated to calendar-time."""
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

    per_event_offset: np.ndarray | None = None
    if horizon_hours is not None:
        from shared.labels.calendar_time_barriers import calendar_time_horizon_bar_offsets
        # QUANTARA_V3_ASI8_NS_FIX: see compute_first_touch_paths above.
        dt_ns = dt.as_unit("ns").asi8
        valid_locs = np.where(locs >= 0, locs, 0)
        per_event_offset = calendar_time_horizon_bar_offsets(dt_ns, valid_locs, hours=horizon_hours)
        eligible = (
            (locs >= 0)
            & (per_event_offset >= 0)
            & feature_valid
            & np.isfinite(vols)
            & (vols > 0)
        )
    else:
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
            f"No eligible rows for vertical_horizon={horizon_hours if horizon_hours is not None else horizon}."
        )

    rows: list[dict[str, Any]] = []

    for j in idxs:
        loc = int(locs[j])
        side = int(sides[j])
        trgt = float(vols[j])
        entry = float(close[loc])
        effective_horizon = int(per_event_offset[j]) if per_event_offset is not None else int(horizon)

        if side == 1:
            pt_price = entry * (1.0 + float(pt) * trgt)
            sl_price = entry * (1.0 - float(sl) * trgt)
        else:
            pt_price = entry * (1.0 - float(pt) * trgt)
            sl_price = entry * (1.0 + float(sl) * trgt)

        if int(first_offset[j]) <= effective_horizon:
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
            end_idx = loc + effective_horizon

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
            # AFML Snippet 3.7 (p.51): a vertical exit is labeled by the sign of the side-adjusted return,
            # the same rule as model_rows and Task 2A (was hard-coded 0 until 2026-10-23; Ch.3 walkthrough).
            label = int(realized_r > 0.0)

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

    # Real wall-clock hold time per event, from the bars' own genuine
    # Datetime/t1 timestamps (already computed above, just never aggregated
    # before 2026-09-24). NOT the same as hold_bars: Quantara's bars are
    # information-driven (causal daily tick-count threshold, see
    # shared/bars/standard_quote_tick_builder.py) rather than fixed-calendar-
    # time bars, so a fixed bar count spans a variable amount of real time
    # depending on intraday tick-arrival rate (fast during volatile/liquid
    # periods, slow during quiet ones) -- bar count is a sampling unit for
    # feature/label construction (the actual AFML Ch.2 argument for
    # information-driven bars), never a valid stand-in for elapsed calendar
    # time. hold_bars is kept for backward compatibility/diagnostics only;
    # any ranking that cares about real trading speed must use hold_hours.
    hold_hours = (frame["t1"] - frame["Datetime"]).dt.total_seconds() / 3600.0

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
        "median_hold_hours": float(hold_hours.median()),
        "mean_hold_hours": float(hold_hours.mean()),
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
        # REVISED 2026-09-24 (calendar-time labeling rewrite): H and Hv
        # parsed as float, not int -- for a calendar-time edge these are
        # real hours (may be fractional in principle), for a bar-count edge
        # they're still whole-number bar counts (float(5)==5 either way).
        # Caller (run_execution_finalist_mode) decides which interpretation
        # applies based on the edge's own module, same detection as Gate A/B.
        h, v = float(p[0]), float(p[2])
        e = int(p[1])
        pt, sl = float(p[3]), float(p[4])
        if min(h, e, v) <= 0 or pt <= 0 or sl <= 0:
            raise HorizonLabError(f"Invalid execution finalist: {raw}")
        out.append({
            "label": f"H{h:g}_E{e}_V{v:g}_PT{pt:g}_SL{sl:g}",
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


def _xef_prepare_inputs(
    edge_root: Path,
    runtime: Any,
    feature_names: list[str],
    finalists: list[dict[str, Any]],
    control_geom: dict[str, Any],
) -> tuple[pd.DataFrame, set[pd.Timestamp], list[str]]:
    """Load execution-finalist inputs (events + valid_times), same artifact-
    first-with-runtime-fallback contract as run_hybrid_fast_grid_mode's
    _hfg_prepare(). A brand-new edge with no persisted research_dataset
    (export_research_dataset.py never run) falls back to calling
    runtime.build_features()/generate_candidate_events() live, exactly like
    Stage-1's hybrid-fast-grid mode already does -- Stage-2 execution-
    finalist mode no longer requires a persisted export as a prerequisite.

    Only the "manifest file does not exist at all" case triggers the
    fallback. If the manifest exists but references a missing or unreadable
    output file, that is a genuine corruption/staleness signal and still
    raises -- silently papering over a broken persisted state would hide
    real bugs, not just skip an inconvenient prerequisite step.
    """

    mp = edge_root / "artifacts" / "research_dataset" / "research_dataset_manifest.json"
    if mp.exists():
        events, paths = _xef_artifacts(edge_root)
        valid = _xef_valid_times(paths, feature_names, finalists, control_geom)
        return events, valid, ["events=persisted_artifact", "features=persisted_artifact"]

    print("No research_dataset_manifest.json found -> runtime fallback for execution-finalist inputs "
          "(brand-new edge, export_research_dataset.py not yet run).")
    features = runtime.build_features(drop_warmup_rows=False)
    events = normalize_base_events(runtime.generate_candidate_events(features))
    valid = _hfg_valid_times(features, feature_names)
    return events, valid, ["events=runtime_fallback", "features=runtime_fallback"]


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
    # REVISED 2026-09-24 (calendar-time labeling rewrite): control_geometry's
    # "vertical_horizon" is None for a calendar-time edge (see
    # infer_control_geometry) -- compare against vertical_horizon_hours
    # instead when that's the populated field. See
    # config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    # entry.
    control_hv = control_geometry.get("vertical_horizon_hours") if control_geometry.get("calendar_time") else control_geometry.get("vertical_horizon")
    if all(float(x["vertical_horizon"]) == float(control_hv) for x in finalists):
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


def _xef_vol_calendar_time(bars: dict[str, Any], hours: float, ewm: int, min_vol: float) -> pd.Series:
    """Calendar-time sibling of _xef_vol -- see
    shared.labels.calendar_time_barriers.calendar_time_event_vol and
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry."""
    from shared.labels.calendar_time_barriers import calendar_time_event_vol
    # QUANTARA_V3_ASI8_NS_FIX: see compute_first_touch_paths above.
    dt_ns = pd.DatetimeIndex(bars["dt"]).as_unit("ns").asi8
    close = np.asarray(bars["close"], dtype="float64")
    v = calendar_time_event_vol(dt_ns, close, hours=float(hours), ewm_span=int(ewm), min_vol=float(min_vol))
    return pd.Series(v, index=bars["dt"])


def _xef_prepare(
    finalists: list[dict[str, Any]],
    events: pd.DataFrame,
    bars: dict[str, Any],
    valid_times: set[pd.Timestamp],
    min_vol: float,
    is_calendar_time: bool = False,
) -> dict[str, dict[pd.Timestamp, dict[str, Any]]]:
    """is_calendar_time: REVISED 2026-09-24 (calendar-time labeling
    rewrite). When True, cfg["event_vol_lookback"]/cfg["vertical_horizon"]
    are real hours, not bar counts -- see
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry. Default False preserves the original bar-count-only behavior
    exactly, for every edge not yet migrated to calendar-time."""
    base = events.copy()
    base["Datetime"] = to_datetime_iso(base["Datetime"], utc=True, errors="coerce")
    base["side"] = pd.to_numeric(base["side"], errors="coerce")
    base = base.dropna(subset=["Datetime", "side"]).sort_values("Datetime").reset_index(drop=True)
    dt = bars["dt"]
    # QUANTARA_V3_ASI8_NS_FIX: see compute_first_touch_paths above.
    dt_ns = dt.as_unit("ns").asi8
    locs = dt.get_indexer(pd.DatetimeIndex(base["Datetime"]))
    result: dict[str, dict[pd.Timestamp, dict[str, Any]]] = {}
    for cfg in finalists:
        if is_calendar_time:
            from shared.labels.calendar_time_barriers import calendar_time_vertical_deadline_positions
            vol = _xef_vol_calendar_time(bars, cfg["event_vol_lookback"], cfg["event_vol_ewm_span"], min_vol)
        else:
            vol = _xef_vol(bars, cfg["event_vol_lookback"], cfg["event_vol_ewm_span"], min_vol)
        hv = cfg["vertical_horizon"]  # hours (float) if calendar-time, else bar count (int)
        m: dict[pd.Timestamp, dict[str, Any]] = {}
        for i, row in base.iterrows():
            t = pd.Timestamp(row["Datetime"])
            loc = int(locs[i])
            if loc < 0 or t not in valid_times:
                continue
            if is_calendar_time:
                deadline_pos = int(calendar_time_vertical_deadline_positions(dt_ns, np.array([loc]), hours=float(hv))[0])
                if deadline_pos < 0 or deadline_pos >= len(dt):
                    continue
            else:
                deadline_pos = loc + int(hv)
                if deadline_pos >= len(dt):
                    continue
            trgt = float(vol.get(t, np.nan))
            side = int(row["side"])
            sig = float(bars["close"][loc])
            if side not in (-1, 1) or not np.isfinite(trgt) or trgt <= 0 or not np.isfinite(sig) or sig <= 0:
                continue
            deadline = pd.Timestamp(dt[deadline_pos])
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


def _xef_parse_tick_ts(col: pd.Series) -> np.ndarray:
    """ADDED 2026-11-09 (Gate C speed): Dukascopy tick stamps are 'YYYY-MM-DD HH:MM:SS[.ffffff]+00:00'. When every
    stamp has that UTC suffix, numpy's C ISO parser gives the identical ns values about 1.7x faster; otherwise (or on
    any parse problem) fall back to the general parser. Returns int64 ns with NaT as INT64_MIN."""
    s = col.astype(str)
    try:
        if bool(s.str.endswith("+00:00").all()):
            return np.array(s.str.slice(0, -6).to_numpy(), dtype="datetime64[ns]").astype("int64")
    except (ValueError, TypeError):
        pass
    return to_datetime_iso(col, utc=True, errors="coerce").to_numpy(dtype="datetime64[ns]").astype("int64")


_XEF_CACHE_SIZE = 300


def _xef_loader():
    from functools import lru_cache
    # maxsize=6 caused severe cache-thrashing re-parsing of the same ~20-30MB
    # weekly tick CSVs whenever a long vertical_horizon needed to look ahead
    # across more than 6 files per event (confirmed 2026-09-14: US500's 235-file
    # catalog with vertical_horizon=1000 geometries stalled for 20+ minutes on
    # a few thousand events). Raised so a full asset's tick catalog can stay
    # resident once loaded; each cached entry is 3 numeric numpy arrays, not
    # the raw CSV text, so this is far smaller in memory than the file sizes.
    @lru_cache(maxsize=_XEF_CACHE_SIZE)
    def load(path_str: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        p = Path(path_str)
        h = pd.read_csv(p, nrows=0)
        tc, bc, ac = _xef_cols(list(h.columns))
        d = pd.read_csv(p, usecols=[tc, bc, ac], dtype={tc: str})
        tsn = _xef_parse_tick_ts(d[tc])
        ts = pd.Series(pd.DatetimeIndex(tsn.astype("datetime64[ns]")).tz_localize("UTC"), index=d.index)
        bid = pd.to_numeric(d[bc], errors="coerce")
        ask = pd.to_numeric(d[ac], errors="coerce")
        good = ts.notna() & bid.notna() & ask.notna() & (bid > 0) & (ask > 0) & (ask >= bid)
        if not bool(good.any()):
            return np.array([], dtype="int64"), np.array([], dtype="float64"), np.array([], dtype="float64")
        q = pd.DataFrame({"ts": ts[good], "bid": bid[good], "ask": ask[good]}).sort_values("ts")
        return (
            # Must force ns resolution before the int64 view: pandas >=2.0 can
            # infer a coarser storage unit (e.g. datetime64[us]) from the input
            # strings' precision, and Series.astype("int64") reflects THAT
            # storage unit, not nanoseconds -- unlike Timestamp.value, which
            # always normalizes to ns. threshold_ns in _xef_event is built from
            # Timestamp.value, so comparing it against a non-ns-forced int64
            # view silently compares mismatched units (confirmed 2026-09-14:
            # pandas 3.0.2 produced datetime64[us] here, making every entry
            # threshold ~1000x too large and causing 100% "missing_entry_quote"
            # across every event/finalist with zero visible error).
            q["ts"].to_numpy(dtype="datetime64[ns]").astype("int64"),
            q["bid"].to_numpy(dtype="float64"),
            q["ask"].to_numpy(dtype="float64"),
        )
    return load


def _xef_spread_loader(base: Any, mode: str, level_bps: float | None):
    """ADDED 2026-10-16 (broker-independent spread certification).
    Wrap the raw tick loader so Bid/Ask are rebuilt around the Dukascopy MID:
        s'_t = L * spread_bps_t / median(spread_bps over that weekly file)
        bid' = mid - mid*s'/2e4,  ask' = mid + mid*s'/2e4
    The per-tick spread PATTERN (rollover/news/session widening, quiet tightening) is kept exactly; only its
    level moves to L bps. The weekly-file median is the normaliser so old wide-spread eras do not dominate.
    mode 'self' uses each file's own median as L -> identity (must reproduce the raw replay)."""
    from functools import lru_cache
    if mode == "raw":
        return base

    @lru_cache(maxsize=_XEF_CACHE_SIZE)
    def load(path_str: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ts, bid, ask = base(path_str)
        if ts.size == 0:
            return ts, bid, ask
        mid = (bid + ask) / 2.0
        sb = (ask - bid) / mid * 1e4
        ref = float(np.median(sb))
        if not np.isfinite(ref) or ref <= 0:
            return ts, bid, ask
        lvl = ref if mode == "self" else float(level_bps)
        half = mid * (lvl * sb / ref) / 2e4
        return ts, mid - half, mid + half
    return load


def _xef_broker_loader(base: Any, cm: dict[str, Any]):
    """ADDED 2026-10-16: Bid/Ask = Dukascopy MID +/- half the BROKER spread from a fitted, validated cost model.
    FIXED_POINTS: constant point spread. TABLE: mean spread by (UTC hour-of-week x trailing-1-day realized-vol state),
    state read from a per-minute Dukascopy-mid volatility cache (causal). Unit pts or bps as fitted."""
    from functools import lru_cache
    fixed = cm["model"] == "FIXED_POINTS"
    if not fixed:
        vc = Path(cm["vol_cache"]); vc = vc if vc.is_absolute() else Path(__file__).resolve().parents[2] / vc  # repo-relative
        vol = np.load(vc); vt = vol["t"].astype("int64"); vr = vol["rv1d"].astype("float64")
        q = np.asarray(cm["vol_state_thresholds_rv1d_bps"], dtype="float64"); ns = len(q) + 1
        tab = np.full((168, ns), np.nan)
        for k, v in cm["table"].items():
            h, st = (int(x) for x in k.split("|"))
            if int(cm.get("table_counts", {}).get(k, 0)) >= 30:
                tab[h, st] = float(v)
        hm = np.array([float(cm["hour_mean"].get(str(h), np.nan)) for h in range(168)])
        mult = np.array([float(cm["state_multiplier"].get(str(st), 1.0)) for st in range(ns)])
        tab = np.where(np.isfinite(tab), tab, hm[:, None] * mult[None, :])
        tab = np.where(np.isfinite(tab), tab, float(np.nanmean(hm)))
        rv_fill = float(np.nanmedian(vr))
    # ADDED 2026-11-09 (STANDING RULE, owner "yes i approve the era-scaled cost"): full-history Gate C multiplies the
    # broker half-spread by a monthly era factor (docs/tools/build_era_spread_scale.py; floor 1, so no era is cheaper).
    era = cm.get("_era_scale_factors")
    if era:
        _ek = sorted(era)
        era_keys = np.array([int(k[:4]) * 12 + int(k[5:7]) - 1 for k in _ek], dtype="int64")
        era_vals = np.array([float(era[k]) for k in _ek], dtype="float64")

    @lru_cache(maxsize=_XEF_CACHE_SIZE)
    def load(path_str: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ts, bid, ask = base(path_str)
        if ts.size == 0:
            return ts, bid, ask
        mid = (bid + ask) / 2.0
        ref = float(cm["ref_price"])  # forward-looking: today's cost as a FRACTION of notional, applied to every era
        if fixed:
            half = mid * (float(cm["fixed_points"]) / ref) / 2.0
        else:
            minute = ts - (ts % 60_000_000_000)
            i = np.searchsorted(vt, minute, side="right") - 1
            rv = np.where(i >= 0, vr[np.clip(i, 0, None)], rv_fill)
            st = np.digitize(rv, q)
            dti = pd.DatetimeIndex(pd.to_datetime(ts, unit="ns", utc=True))
            val = tab[(dti.dayofweek * 24 + dti.hour).to_numpy(), st]
            half = mid * (val / ref) / 2.0 if cm["unit"] == "pts" else mid * val / 2e4
        if era:
            mk = ts.astype("datetime64[ns]").astype("datetime64[M]").astype("int64") + 1970 * 12
            k = np.searchsorted(era_keys, mk, side="right") - 1
            half = half * np.where(k >= 0, era_vals[np.clip(k, 0, None)], 1.0)
        return ts, mid - half, mid + half
    return load


def _xef_server_offset_hours(t_utc: pd.Timestamp, rule: str = "NY_CLOSE") -> int:
    """Trade-server clock minus UTC under the firm's DETECTED rule (portfolio_allocator/config/broker_profiles/
    <firm>_server_tz.json): NY_CLOSE = 2 + US DST (server midnight = 17:00 New York; Moneta, verified on its own ticks
    2026-10-16), EET = 2 + EU DST, FIXED_n = n."""
    t = t_utc.tz_convert("UTC") if t_utc.tzinfo else t_utc.tz_localize("UTC")
    if rule.startswith("FIXED_"):
        return int(rule.split("_")[1])
    if rule == "EET":
        def last_sun(y: int, m: int) -> pd.Timestamp:
            d = pd.Timestamp(year=y, month=m, day=31, tz="UTC")
            return (d - pd.Timedelta(days=(d.dayofweek + 1) % 7)).normalize() + pd.Timedelta(hours=1)
        return 3 if last_sun(t.year, 3) <= t < last_sun(t.year, 10) else 2
    if rule == "NY_CLOSE":
        def nth_sun(y: int, m: int, n: int) -> pd.Timestamp:
            d = pd.Timestamp(year=y, month=m, day=1, tz="UTC"); d = d + pd.Timedelta(days=(6 - d.dayofweek) % 7)
            return d + pd.Timedelta(weeks=n - 1)
        return 3 if nth_sun(t.year, 3, 2) + pd.Timedelta(hours=7) <= t < nth_sun(t.year, 11, 1) + pd.Timedelta(hours=6) else 2
    raise HorizonLabError(f"Unknown server_tz_rule {rule!r}")


def _xef_rollovers(entry_utc: pd.Timestamp, exit_utc: pd.Timestamp, triple_mt5_weekday: int, rule: str = "NY_CLOSE") -> int:
    """Swap nights actually charged between entry and exit: one per server-midnight crossed, 0 for Sat/Sun rollovers,
    3 on the triple-swap day (MT5 weekday Sun=0..Sat=6). Calibrated 2026-10-16 against 409 real Moneta positions."""
    a = entry_utc + pd.Timedelta(hours=_xef_server_offset_hours(entry_utc, rule))
    b = exit_utc + pd.Timedelta(hours=_xef_server_offset_hours(exit_utc, rule))
    n = 0
    for dd in pd.date_range(a.normalize() + pd.Timedelta(days=1), b.normalize(), freq="D"):
        prev = dd - pd.Timedelta(days=1)
        if prev.dayofweek in (5, 6):
            continue
        n += 3 if (prev.dayofweek + 1) % 7 == int(triple_mt5_weekday) else 1
    return n


def _xef_apply_carry_costs(d: pd.DataFrame, cm: dict[str, Any]) -> pd.DataFrame:
    """Subtract swap (per rollover actually crossed, at the broker's CURRENT spec rate converted at the trade's entry
    price) and round-trip commission from each resolved trade's exec_r (in R). Keeps the pre-carry value for audit."""
    if d.empty or "status" not in d.columns:
        return d
    g = d["status"].eq("resolved")
    if not g.any():
        return d
    sw = cm.get("swap", {}); mode = int(sw.get("mode", 0)); trip = int(sw.get("triple_day", 3))
    comm_bps = float(cm.get("commission_bps_rt", 0.0))
    out = d.copy(); out["exec_r_pre_carry"] = out["exec_r"]
    sR, cR, nN = [], [], []
    for _, r in out.loc[g].iterrows():
        ep = float(r["entry_price"]); sl = float(r["sl_dist"]); side = int(r["side"])
        rate = float(sw.get("long" if side == 1 else "short", 0.0)); ref = float(sw.get("ref_price", ep))
        # financing is a fraction of notional: convert today's spec at today's price (ref), apply in bps of this trade
        if mode == 1:
            bps = -rate * float(sw["point"]) / ref * 1e4
        elif mode == 2:
            bps = -rate / (ref * float(sw["contract_size"])) * 1e4
        elif mode == 5:
            bps = -rate / 100.0 / 360.0 * 1e4
        else:
            bps = 0.0
        n = _xef_rollovers(pd.Timestamp(r["entry_ts"]), pd.Timestamp(r["exit_ts"]), trip, str(cm.get("server_tz_rule", "NY_CLOSE")))
        sR.append(bps * n * ep / 1e4 / sl); cR.append(comm_bps * ep / 1e4 / sl); nN.append(n)
    out.loc[g, "swap_R"] = sR; out.loc[g, "commission_R"] = cR; out.loc[g, "swap_nights"] = nN
    out.loc[g, "exec_r"] = out.loc[g, "exec_r"].astype(float) - out.loc[g, "swap_R"] - out.loc[g, "commission_R"]
    return out


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
    guard_bps: float | None = None,
    guard_window_s: float = 300.0,
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
    if guard_bps is not None and guard_bps > 0:
        # ADDED 2026-10-16: mirror the live spread guard -- the bot refuses entries while the recent (rolling
        # median) spread runs above its limit; such events never trade, so they are not losses either.
        gts, gbid, gask = loader(str(catalog[fi0]["path"]))
        lo = int(np.searchsorted(gts, ets - int(guard_window_s * 1e9), side="left"))
        gm = (gbid[lo:ix0 + 1] + gask[lo:ix0 + 1]) / 2.0
        if gm.size and float(np.median((gask[lo:ix0 + 1] - gbid[lo:ix0 + 1]) / gm * 1e4)) > float(guard_bps):
            return {k: {"status": "guard_blocked", "event_ts": t} for k in recs}
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
    blocked = int(df["status"].eq("guard_blocked").sum()) if "status" in df.columns else 0
    expected = int(expected) - blocked  # guard-blocked events never trade (live guard mirror) -> not a coverage gap
    if d.empty: return {"label": cfg["label"], "is_control": cfg.get("is_control",False), "coverage_rate":0.0, "guard_blocked":blocked}
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
        "guard_blocked":blocked, "guard_blocked_rate":float(blocked/max(1,expected+blocked)),
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


def _xef_research_cost_bps_by_asset() -> dict[str, float]:
    # Read RESEARCH_COST_BPS_BY_ASSET literally from portfolio_live/live_settings.py
    # (AST only, no import -- importing live settings has side effects).
    p=Path(__file__).resolve().parents[2]/"portfolio_live"/"live_settings.py"
    try:
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="RESEARCH_COST_BPS_BY_ASSET" for t in node.targets):
                return {str(k):float(v) for k,v in ast.literal_eval(node.value).items()}
    except (OSError,SyntaxError,ValueError):
        pass
    return {}


def _xef_resolve_extra_cost_bps(args: Any, edge_root: Path) -> float:
    # ADDED 2026-10-14: Gate C (exact Bid/Ask replay) already pays the real spread
    # (long enters Ask/exits Bid, short the reverse). --cost-bps is the Gate A/B
    # mid-bar cost and must NOT be reused here -- doing so double-charged every
    # Gate C run from 2026-09-29 to 2026-10-14 (all 13 live edges were certified
    # at extra 0.0). Any extra charge goes through --extra-cost-bps (default 0).
    if float(args.cost_bps)!=0.0:
        raise SystemExit("--cost-bps is the Gate A/B mid-bar cost and is not accepted in --execution-finalists mode "
                         "(real Bid/Ask spread is already paid). Use --extra-cost-bps for a deliberate extra charge (default 0).")
    extra=float(args.extra_cost_bps)
    if extra<0: raise SystemExit("--extra-cost-bps must be >= 0")
    if extra>0 and not args.allow_extra_cost_equal_to_research_spread:
        parts=edge_root.parts; asset=parts[parts.index("edges")-1] if "edges" in parts and parts.index("edges")>0 else None
        research=_xef_research_cost_bps_by_asset().get(str(asset)) if asset else None
        if research is not None and abs(extra-float(research))<1e-9:
            raise SystemExit(f"--extra-cost-bps {extra:g} equals {asset}'s research spread cost_bps ({research:g}); "
                             "the spread is already paid by the Bid/Ask replay, so this would double-charge it. "
                             "Pass --allow-extra-cost-equal-to-research-spread only if this is deliberate.")
    return extra


def _xef_chunk(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """ADDED 2026-11-09 (Gate C speed): replay one contiguous time chunk of events in a worker process. Events are
    independent given the quotes, so splitting by time and concatenating the chunks in order gives identical rows."""
    global _XEF_CACHE_SIZE
    _XEF_CACHE_SIZE = int(payload["cache_size"])
    base = _xef_loader()
    mode, lvl, cm = payload["mode"], payload["lvl"], payload["cost_model"]
    loader = _xef_broker_loader(base, cm) if mode == "broker" else _xef_spread_loader(base, mode, lvl)
    rows: dict[str, list[dict[str, Any]]] = {k: [] for k in payload["labels"]}
    for t, recs in payload["events"]:
        rr = _xef_event(t, recs, payload["catalog"], loader, payload["delay_ms"], payload["extra_bps"],
                        payload["gbps"], payload["guard_win"])
        for k, v in rr.items():
            v["config_label"] = k + payload["sfx"]
            rows[k].append(v)
    return rows


def run_execution_finalist_mode(args: Any) -> None:
    edge_root=Path(args.edge_root).resolve()
    extra_cost_bps=_xef_resolve_extra_cost_bps(args,edge_root)
    runtime,model_rows,candidate_events=import_edge(edge_root)
    edge_id=str(getattr(runtime,"EDGE_ID",edge_root.as_posix())); feature_names=schema_feature_names(runtime)
    control_vol=infer_control_event_vol(candidate_events,args.control_event_vol_lookback,args.control_event_vol_ewm_span)
    control_geom=infer_control_geometry(model_rows); min_vol=infer_min_event_vol(candidate_events)
    # REVISED 2026-09-24 (calendar-time labeling rewrite): auto-detected
    # from the edge's own module, same mechanism as Gate A/B -- see
    # config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    # entry. is_calendar_time governs BOTH the Event_Vol lookback and the
    # vertical barrier for this edge (they were migrated together).
    is_calendar_time=bool(control_vol.get("calendar_time"))
    finalists=_xef_parse(args.execution_finalists)
    if not args.execution_no_control:
        if is_calendar_time:
            cv_lookback=float(control_vol["lookback_hours"]); cg_horizon=float(control_geom["vertical_horizon_hours"])
            c_label=f"V2_CONTROL_H{cv_lookback:g}h_E{control_vol['span']}_V{cg_horizon:g}h_PT{control_geom['pt']:g}_SL{control_geom['sl']:g}"
        else:
            cv_lookback=int(control_vol["lookback"]); cg_horizon=int(control_geom["vertical_horizon"])
            c_label=f"V2_CONTROL_H{cv_lookback}_E{control_vol['span']}_V{cg_horizon}_PT{control_geom['pt']:g}_SL{control_geom['sl']:g}"
        c={"label":c_label,
           "event_vol_lookback":cv_lookback,"event_vol_ewm_span":int(control_vol["span"]),
           "vertical_horizon":cg_horizon,"pt":float(control_geom["pt"]),"sl":float(control_geom["sl"]),"is_control":True}
        sig=lambda x:(x["event_vol_lookback"],x["event_vol_ewm_span"],x["vertical_horizon"],x["pt"],x["sl"])
        if sig(c) not in {sig(x) for x in finalists}: finalists=[c,*finalists]

    print("QUANTARA V3 — EXACT BID/ASK EXECUTION + CAPITAL VELOCITY")
    print(f"edge: {edge_id}"); print(f"entry_delay_ms: {args.execution_entry_delay_ms}")
    print(f"extra_cost_bps: {extra_cost_bps} (raw Bid/Ask spread already embedded)")
    for x in finalists: print(" -",x["label"],"[CONTROL]" if x.get("is_control") else "")

    bars=_xef_bars(runtime)
    events,valid,_xef_input_notes=_xef_prepare_inputs(edge_root,runtime,feature_names,finalists,control_geom)
    # ADDED 2026-11-09 (history extension, owner-approved design docs/mldp/HISTORY_EXTENSION_DESIGN.md): Gate C economics
    # on the broker-cost period only. Events are filtered by entry time AFTER the full-history feature / Event_Vol build,
    # so the window never changes a target or a warm-up.
    exec_start=getattr(args,"execution_start",None)
    if exec_start:
        t0=pd.Timestamp(exec_start,tz="UTC"); n0=len(events)
        events=events[to_datetime_iso(events["Datetime"],utc=True,errors="coerce")>=t0].copy()
        print(f"execution_start: {t0.isoformat()} -> events {n0} -> {len(events)}")
        if events.empty: raise HorizonLabError(f"No events at or after --execution-start {exec_start}")
    maps=_xef_prepare(finalists,events,bars,valid,min_vol,is_calendar_time=is_calendar_time)
    if not hasattr(model_rows,"build_tick_file_catalog"): raise HorizonLabError("model_rows has no build_tick_file_catalog")
    catalog=model_rows.build_tick_file_catalog(); base_loader=_xef_loader(); cfg={x["label"]:x for x in finalists}
    times=sorted(set().union(*(set(m.keys()) for m in maps.values())))
    obs_days=max(1e-9,(times[-1]-times[0]).total_seconds()/86400.0)
    # ADDED 2026-10-16: spread model. raw = Dukascopy Bid/Ask exactly as before (single pass, labels unchanged).
    spread_model=str(getattr(args,"execution_spread_model","raw") or "raw")
    cost_model=None
    if spread_model=="level":
        specs=[("level",float(x)) for x in str(args.execution_spread_levels_bps).split(",") if x.strip()]
    elif spread_model=="broker":
        if not getattr(args,"execution_cost_model",None): raise SystemExit("--execution-spread-model broker needs --execution-cost-model")
        cost_model=json.loads(Path(args.execution_cost_model).read_text(encoding="utf-8")); specs=[("broker",None)]
        if getattr(args,"execution_era_scale",None):
            _es=json.loads(Path(args.execution_era_scale).read_text(encoding="utf-8"))
            if _es.get("asset")!=cost_model.get("asset",_es.get("asset")): raise SystemExit("era scale asset != cost model asset")
            cost_model["_era_scale_factors"]=_es["factors"]
            print(f"era_scale: {args.execution_era_scale} months={len(_es['factors'])} max={max(_es['factors'].values()):.3f}")
    else:
        specs=[(spread_model,None)]
    guard_mult=float(getattr(args,"execution_spread_guard_multiple",0.0) or 0.0)
    guard_win=float(getattr(args,"execution_spread_guard_window_s",300.0))
    print(f"spread_model: {spread_model}" + (f" levels_bps={[l for _,l in specs]} guard={guard_mult}x over {guard_win:g}s" if spread_model=="level" else ""))
    frames={}; summaries=[]
    for mode,lvl in specs:
        loader=_xef_broker_loader(base_loader,cost_model) if mode=="broker" else _xef_spread_loader(base_loader,mode,lvl)
        gbps=(guard_mult*lvl) if (mode=="level" and guard_mult>0) else None
        # broker mode: NO guard -- the modelled spread is the broker's NORMAL spread for that hour/vol state by
        # construction (the live guard only blocks ABNORMAL spreads); a bps guard vs today's level would wrongly block
        # older eras where a fixed point spread was a larger fraction of a lower price (smoke test 2026-10-16).
        sfx=f"@S{lvl:g}" if mode=="level" else ""
        rows={k:[] for k in cfg}; started=time.time()
        print(f"Exact quote replay events: {len(times)}" + (f" [spread level {lvl:g} bps]" if mode=="level" else ""))
        workers=int(getattr(args,"execution_workers",1) or 1)
        if workers>1 and len(times)>=2*workers:
            # ADDED 2026-11-09: contiguous time chunks in parallel; identical rows, concatenated in time order
            from concurrent.futures import ProcessPoolExecutor
            bounds=np.linspace(0,len(times),workers+1).astype(int)
            payloads=[{"events":[(t,{k:m[t] for k,m in maps.items() if t in m}) for t in times[bounds[w]:bounds[w+1]]],
                       "catalog":catalog,"mode":mode,"lvl":lvl,"cost_model":cost_model,"labels":list(cfg),
                       "delay_ms":int(args.execution_entry_delay_ms),"extra_bps":extra_cost_bps,"gbps":gbps,
                       "guard_win":guard_win,"sfx":sfx,"cache_size":int(getattr(args,"execution_worker_cache",8))}
                      for w in range(workers)]
            print(f"  parallel replay: {workers} workers, chunk sizes {[len(x['events']) for x in payloads]}")
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for part in ex.map(_xef_chunk,payloads):
                    for k,v in part.items(): rows[k].extend(v)
            print(f"  {len(times)}/{len(times)} elapsed={time.time()-started:.1f}s")
        else:
          for i,t in enumerate(times,1):
            recs={k:m[t] for k,m in maps.items() if t in m}
            rr=_xef_event(t,recs,catalog,loader,int(args.execution_entry_delay_ms),extra_cost_bps,gbps,guard_win)
            for k,v in rr.items(): v["config_label"]=k+sfx; rows[k].append(v)
            if i%250==0 or i==len(times): print(f"  {i}/{len(times)} elapsed={time.time()-started:.1f}s")
        for x in finalists:
            d=pd.DataFrame(rows[x["label"]]); good=d["status"].eq("resolved") if "status" in d.columns else pd.Series(False,index=d.index)
            if good.any():
                res=_xef_hold_bars(d.loc[good].copy(),bars); d=pd.concat([res,d.loc[~good]],ignore_index=True,sort=False)
            if mode=="broker": d=_xef_apply_carry_costs(d,cost_model)
            xs={**x,"label":x["label"]+sfx}
            sm=_xef_summary(d,xs,len(maps[x["label"]]),obs_days,int(args.execution_bootstrap_reps))
            if mode=="level": sm["spread_level_bps"]=lvl
            frames[xs["label"]]=d; summaries.append(sm)
        if hasattr(loader,"cache_clear") and loader is not base_loader: loader.cache_clear()

    ctrl=next((s for s in summaries if s.get("is_control")),None)
    if ctrl:
        cv=float(ctrl.get("capital_time_velocity_R_per_occupied_day",np.nan)); ch=float(ctrl.get("mean_hold_hours",np.nan))
        for s in summaries:
            v=float(s.get("capital_time_velocity_R_per_occupied_day",np.nan)); h=float(s.get("mean_hold_hours",np.nan))
            s["velocity_multiple_vs_v2_control"] = v/cv if np.isfinite(v) and np.isfinite(cv) and cv!=0 else float("nan")
            s["mean_hold_speedup_vs_v2_control"] = ch/h if np.isfinite(ch) and np.isfinite(h) and h>0 else float("nan")

    print("\nEXECUTABLE SUMMARY")
    print("label | meanR | 95%CI | PF | mean_hold_h | p95_hold_h | R/occupied_day | R/week | velocity_xV2 | hold_speedup_xV2 | coverage | guard_blocked")
    for s in summaries:
        print(f"{s['label']} | {s.get('exec_mean_R',np.nan):+.6f} | [{s.get('weekly_cluster_bootstrap_mean_R_95_lo',np.nan):+.6f},{s.get('weekly_cluster_bootstrap_mean_R_95_hi',np.nan):+.6f}] | {s.get('profit_factor_R',np.nan):.4f} | {s.get('mean_hold_hours',np.nan):.3f} | {s.get('p95_hold_hours',np.nan):.3f} | {s.get('capital_time_velocity_R_per_occupied_day',np.nan):+.6f} | {s.get('calendar_exec_R_per_week',np.nan):+.6f} | {s.get('velocity_multiple_vs_v2_control',np.nan):.3f} | {s.get('mean_hold_speedup_vs_v2_control',np.nan):.3f} | {s.get('coverage_rate',np.nan):.4f} | {s.get('guard_blocked',0)}")

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
          "raw_bid_ask_spread_embedded":spread_model=="raw","spread_model":spread_model,
          "spread_levels_bps":[l for _,l in specs] if spread_model=="level" else None,
          "spread_guard_multiple":guard_mult if spread_model in ("level","broker") else None,
          "cost_model":({k:cost_model.get(k) for k in ("asset","broker","model","unit","mean_spread_bps_time_weighted","swap","commission_bps_rt","fitted_at")} if cost_model else None),
          "extra_cost_bps":extra_cost_bps,"entry_delay_ms":int(args.execution_entry_delay_ms),
          "execution_start":getattr(args,"execution_start",None),
          "execution_era_scale":getattr(args,"execution_era_scale",None),
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


def _hfg_parse_floats(text: str, name: str) -> list[float]:
    """REVISED 2026-09-24 (calendar-time labeling rewrite): float-valued
    sibling of _hfg_parse_ints, used when the SAME CLI flags
    (--event-vol-lookbacks / --vertical-horizons) carry real-hour values
    instead of bar counts, auto-detected per edge (see
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry) -- no separate CLI flags needed."""
    vals = [float(x.strip()) for x in str(text).split(",") if x.strip()]
    if not vals or any(v <= 0 for v in vals):
        raise HorizonLabError(f"{name} must contain positive comma-separated numbers.")
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


def _hfg_event_vol_calendar_time(bars: dict[str, Any], lookback_hours: float, span: int, min_vol: float) -> pd.Series:
    """REVISED 2026-09-24 (calendar-time labeling rewrite): calendar-time
    equivalent of _hfg_event_vol, using the shared, AFML-Snippet-3.1-verified
    mechanism in shared.labels.calendar_time_barriers -- see
    config/us500_edge_search_ledger.json's calendar_time_labeling_REWRITE_2026-09-24
    entry. Used only for edges whose candidate_events.py exposes
    EVENT_VOL_LOOKBACK_HOURS instead of a bar-count lookback."""
    from shared.labels.calendar_time_barriers import calendar_time_event_vol
    # QUANTARA_V3_ASI8_NS_FIX: DatetimeIndex.asi8 is not guaranteed nanoseconds
    # across pandas versions (venv39=ns, venv312=microseconds by default) --
    # normalize explicitly or lookbacks compute far too wide on venv312.
    dt_ns = pd.DatetimeIndex(bars["dt"]).as_unit("ns").asi8
    close = np.asarray(bars["close"], dtype="float64")
    vol = calendar_time_event_vol(dt_ns, close, hours=float(lookback_hours), ewm_span=int(span), min_vol=float(min_vol))
    return pd.Series(vol, index=bars["dt"], name="Event_Vol")


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



def _hfg_control_geometry(
    reference: dict[str, Any],
    model_rows: Any,
) -> dict[str, Any]:
    # Resolve control geometry for the hybrid-fast parity gate.
    #
    # When a persisted model-row manifest exists, it is the frozen artifact
    # being reproduced, so its labeling geometry is authoritative for parity.
    # New/incomplete packages without one continue to use model_rows.py.
    source_geometry = infer_control_geometry(model_rows)

    if reference.get("source") != "persisted_model_row_manifest":
        return source_geometry

    manifest = reference.get("manifest") or {}
    # REVISED 2026-09-24 (calendar-time labeling rewrite): a migrated edge's
    # manifest carries vertical_horizon_hours instead of vertical_horizon --
    # check for either, since not every edge is migrated yet.
    has_hours = "vertical_horizon_hours" in manifest
    has_bars = "vertical_horizon" in manifest
    required = ("pt_multiplier", "sl_multiplier")
    missing = [key for key in required if key not in manifest]
    if missing or not (has_hours or has_bars):
        print(
            " - persisted control manifest missing geometry fields "
            f"{missing + (['vertical_horizon or vertical_horizon_hours'] if not (has_hours or has_bars) else [])}; "
            "falling back to model_rows.py control geometry"
        )
        return source_geometry

    frozen_geometry = {
        "calendar_time": has_hours,
        "vertical_horizon_hours": float(manifest["vertical_horizon_hours"]) if has_hours else None,
        "vertical_horizon": int(manifest["vertical_horizon"]) if has_bars else None,
        "pt": float(manifest["pt_multiplier"]),
        "sl": float(manifest["sl_multiplier"]),
    }

    if frozen_geometry != source_geometry:
        print(
            " - CONTROL GEOMETRY SOURCE DRIFT: "
            f"model_rows={source_geometry} "
            f"persisted_manifest={frozen_geometry}"
        )
        print(
            " - parity will use persisted_model_row_manifest geometry "
            "because that is the frozen reference being reproduced"
        )

    return frozen_geometry

def _hfg_parity(
    model_rows: Any,
    bars: dict[str, Any],
    prepared: dict[str, Any],
    reference: dict[str, Any],
    control_vol: dict[str, Any],
    control_geometry: dict[str, Any],
    min_vol: float,
) -> None:
    if bool(control_vol.get("calendar_time")):
        vol = _hfg_event_vol_calendar_time(bars, control_vol["lookback_hours"], control_vol["span"], min_vol)
    else:
        vol = _hfg_event_vol(bars, control_vol["lookback"], control_vol["span"], min_vol)
    events = _hfg_attach_target(prepared["events"], vol)

    is_calendar_time = bool(control_geometry.get("calendar_time"))
    if is_calendar_time:
        hours = float(control_geometry["vertical_horizon_hours"])
        paths = compute_first_touch_paths(
            model_rows=model_rows, bars=bars, events=events,
            max_horizon=0, max_horizon_hours=hours,
            pt=float(control_geometry["pt"]), sl=float(control_geometry["sl"]),
        )
        _, summary = materialize_horizon(
            bars=bars, events=events, paths=paths,
            valid_feature_times=prepared["valid_times"],
            horizon=0, horizon_hours=hours,
            pt=float(control_geometry["pt"]), sl=float(control_geometry["sl"]),
            cost_bps=0.0,
        )
    else:
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

    control_vol = infer_control_event_vol(
        candidate_events,
        args.control_event_vol_lookback,
        args.control_event_vol_ewm_span,
    )
    min_vol = infer_min_event_vol(candidate_events)

    # REVISED 2026-09-24 (calendar-time labeling rewrite): auto-detected
    # per edge from control_vol/control_geometry (which already inspect the
    # edge's own module for EVENT_VOL_LOOKBACK_HOURS /
    # VERTICAL_HORIZON_HOURS) -- same --event-vol-lookbacks/--vertical-horizons
    # CLI flags carry hour values instead of bar counts for a migrated edge,
    # no new flags needed. See config/us500_edge_search_ledger.json's
    # calendar_time_labeling_REWRITE_2026-09-24 entry.
    is_calendar_time = bool(control_vol.get("calendar_time"))
    if is_calendar_time:
        lookbacks = _hfg_parse_floats(args.event_vol_lookbacks, "--event-vol-lookbacks (hours)")
        horizons = _hfg_parse_floats(args.vertical_horizons, "--vertical-horizons (hours)")
    else:
        lookbacks = _hfg_parse_ints(args.event_vol_lookbacks, "--event-vol-lookbacks")
        horizons = _hfg_parse_ints(args.vertical_horizons, "--vertical-horizons")
    spans = _hfg_parse_ints(args.event_vol_ewm_spans, "--event-vol-ewm-spans")
    barriers = _hfg_parse_barriers(args.barriers)

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
    control_geometry = _hfg_control_geometry(
        reference,
        model_rows,
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
            if is_calendar_time:
                events = _hfg_attach_target(
                    prepared["events"],
                    _hfg_event_vol_calendar_time(bars, hvol, span, min_vol),
                )
            else:
                events = _hfg_attach_target(
                    prepared["events"],
                    _hfg_event_vol(bars, hvol, span, min_vol),
                )
            print()
            print("=" * 100)
            unit_label = "h" if is_calendar_time else ""
            print(f"Hvol={hvol}{unit_label} | EWM={span} | events={len(events)}")
            print("=" * 100)

            for pt, sl in barriers:
                if is_calendar_time:
                    paths = compute_first_touch_paths(
                        model_rows=model_rows,
                        bars=bars,
                        events=events,
                        max_horizon=0,
                        max_horizon_hours=max(horizons),
                        pt=pt,
                        sl=sl,
                    )
                else:
                    paths = compute_first_touch_paths(
                        model_rows=model_rows,
                        bars=bars,
                        events=events,
                        max_horizon=max(horizons),
                        pt=pt,
                        sl=sl,
                    )
                for hv in horizons:
                    if is_calendar_time:
                        _, summary = materialize_horizon(
                            bars=bars,
                            events=events,
                            paths=paths,
                            valid_feature_times=prepared["valid_times"],
                            horizon=0,
                            horizon_hours=hv,
                            pt=pt,
                            sl=sl,
                            cost_bps=float(args.cost_bps),
                        )
                    else:
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
                    mean_hold_bars = float(summary["mean_hold_bars"])
                    mean_hold_hours = float(summary["mean_hold_hours"])
                    net_r = float(summary["net_mean_R"])
                    # REVISED 2026-09-24: ranking metric moved from bar count
                    # to real wall-clock hours. Quantara's bars are
                    # information-driven (causal daily tick-count threshold),
                    # so a fixed bar count spans a variable amount of real
                    # time depending on intraday tick-arrival rate -- bar
                    # count is not a valid stand-in for elapsed calendar
                    # time (AFML Ch.2 argues for info-driven bars on
                    # sampling-quality grounds, never as a time proxy).
                    # net_R_per_mean_hold_bar is kept for diagnostics only;
                    # net_R_per_mean_hold_hour is now the ranking metric.
                    velocity_bar = (
                        net_r / mean_hold_bars
                        if np.isfinite(net_r) and np.isfinite(mean_hold_bars) and mean_hold_bars > 0
                        else float("nan")
                    )
                    velocity_hour = (
                        net_r / mean_hold_hours
                        if np.isfinite(net_r) and np.isfinite(mean_hold_hours) and mean_hold_hours > 0
                        else float("nan")
                    )
                    row = {
                        "edge_id": edge_id,
                        "calendar_time": is_calendar_time,
                        # REVISED 2026-09-24: for a calendar-time edge, these
                        # two columns hold HOUR values (not bar counts) --
                        # kept under the same column names for downstream
                        # tooling compatibility, but the "calendar_time" flag
                        # above disambiguates the units unambiguously.
                        "event_vol_lookback": hvol,
                        "event_vol_ewm_span": span,
                        "vertical_horizon": hv,
                        "pt": pt,
                        "sl": sl,
                        "cost_bps": float(args.cost_bps),
                        **summary,
                        "net_R_per_mean_hold_bar": velocity_bar,
                        "net_R_per_mean_hold_hour": velocity_hour,
                    }
                    results.append(row)
                    print(
                        f"H={hvol:>.2f}{unit_label} EWM={span:>3} Hv={hv:>.2f}{unit_label} {pt:g}:{sl:g} | "
                        f"netR={net_r:+.5f} | meanHoldH={mean_hold_hours:.2f}h | "
                        f"R/meanHoldHour={velocity_hour:+.6f}"
                    )

    frame = pd.DataFrame(results)
    frontier = frame[
        (pd.to_numeric(frame["net_mean_R"], errors="coerce") > 0)
        & np.isfinite(pd.to_numeric(frame["net_R_per_mean_hold_hour"], errors="coerce"))
    ].copy().sort_values(
        ["net_R_per_mean_hold_hour", "net_mean_R"],
        ascending=[False, False],
    )

    print()
    print("PRE-LIFECYCLE CAPITAL-TURNOVER FRONTIER (ranked by real wall-clock R/hour)")
    frontier_unit = "h" if is_calendar_time else ""
    for rank, row in enumerate(frontier.head(15).itertuples(index=False), start=1):
        print(
            f"{rank:>2}. H={row.event_vol_lookback:g}{frontier_unit} EWM={row.event_vol_ewm_span} "
            f"Hv={row.vertical_horizon:g}{frontier_unit} PT:SL={row.pt:g}:{row.sl:g} | "
            f"netR={row.net_mean_R:+.6f} | meanHoldH={row.mean_hold_hours:.3f}h | "
            f"R/meanHoldHour={row.net_R_per_mean_hold_hour:+.6f} | "
            f"(R/meanHoldBar={row.net_R_per_mean_hold_bar:+.6f}, meanHoldBars={row.mean_hold_bars:.2f})"
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
        "--extra-cost-bps",
        type=float,
        default=0.0,
        help=("Execution-finalist mode only: extra cost ON TOP of the real Bid/Ask spread "
              "(default 0). --cost-bps is rejected in that mode."),
    )
    parser.add_argument(
        "--allow-extra-cost-equal-to-research-spread",
        action="store_true",
        help="Override the guard refusing --extra-cost-bps equal to the asset's research spread cost.",
    )
    parser.add_argument(
        "--execution-spread-model",
        choices=["raw", "level", "self", "broker"],
        default="raw",
        help=("Execution-finalist mode only. raw (default): replay on Dukascopy's own Bid/Ask (unchanged). "
              "level: rebuild Bid/Ask as Dukascopy mid +/- s'/2 with s'_t = L * spread_t / weekly-file median spread, "
              "for every L in --execution-spread-levels-bps (keeps the real per-tick spread pattern, moves its level). "
              "self: identity check -- L = each file's own median, must reproduce raw."),
    )
    parser.add_argument(
        "--execution-cost-model",
        default=None,
        help=("broker mode only (ADDED 2026-10-16): JSON broker cost model -- spread model (FIXED_POINTS or hour-of-week x "
              "volatility-state TABLE, fitted on the broker's own ticks), per-minute Dukascopy realized-vol cache path, swap "
              "spec (charged per rollover actually crossed, weekday + triple-day rule) and round-trip commission bps."),
    )
    parser.add_argument(
        "--execution-spread-levels-bps",
        default="0.25,0.5,1,2,3,5",
        help="Spread ladder (median spread level in bps) for --execution-spread-model level.",
    )
    parser.add_argument(
        "--execution-spread-guard-multiple",
        type=float,
        default=1.5,
        help=("level mode only: mirror the live spread guard -- skip an entry when the median modelled spread over "
              "the preceding --execution-spread-guard-window-s exceeds this multiple of L (0 disables)."),
    )
    parser.add_argument(
        "--execution-spread-guard-window-s",
        type=float,
        default=300.0,
        help="Look-back window (seconds) for the spread-guard median.",
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
        "--execution-workers",
        type=int,
        default=1,
        help="Execution-finalist mode (ADDED 2026-11-09): replay contiguous event-time chunks in N processes (identical rows).",
    )
    parser.add_argument(
        "--execution-worker-cache",
        type=int,
        default=8,
        help="Per-worker tick-file LRU size (events are chronological; a 168 h vertical spans at most ~3 weekly files).",
    )
    parser.add_argument(
        "--execution-era-scale",
        default=None,
        help=("broker mode only (ADDED 2026-11-09, STANDING RULE): era_scale/<ASSET>.json from "
              "docs/tools/build_era_spread_scale.py; the broker half-spread is multiplied by its monthly factor."),
    )
    parser.add_argument(
        "--execution-start",
        default=None,
        help=("Execution-finalist mode only (ADDED 2026-11-09, history extension): replay only events with entry time at "
              "or after this UTC date, e.g. 2022-01-01 (Gate C / PBO on the broker-cost period). Default: all events."),
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
