# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
"""Quantara V3 Step 03A: exact Bid/Ask OOF executable economics.

Single shared implementation used by thin edge-local
research/03a_exact_oof_executable_economics.py wrappers.

No threshold is selected here. Every valid Step 03 CPCV OOF event receives an
exact quote-side executed_r so every downstream threshold evaluates the same
execution outcomes.
"""

import argparse
import hashlib
import importlib
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.edge_yaml_contract import validate_edge_yaml_file  # noqa: E402
from shared.edges.trade_geometry import load_trade_geometry  # noqa: E402
from shared.labels.calendar_time_barriers import (  # noqa: E402
    calendar_time_vertical_deadline_positions,
)
from shared.validation.v3_semantic_contract import (  # noqa: E402
    GROSS_LABEL_CONTRACT_VERSION,
    GROSS_TARGET_DEFINITION,
    validate_gross_label_frame,
    validate_step03_generation,
)

EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"
EXECUTED_TARGET_CONTRACT_VERSION = "quantara_v3_executed_positive_meta_label_v1"
TIME_COL = "Datetime"
RAW_PROB_COL = "raw_probability"
TARGET_COL = "meta_label"
T1_COL = "t1"
REALIZED_R_COL = "realized_r"
EVENT_VOL_COL = "Event_Vol"
CLEAR_EDGE_MODULES = ("runtime", "model_rows", "candidate_events", "primary_side")


class ExactOOFExecutionError(RuntimeError):
    """Fail-closed Step 03A error."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _import_edge_modules(edge_root: Path) -> tuple[Any, Any]:
    edge_root = edge_root.resolve()
    for required in ("runtime.py", "model_rows.py", "edge.yaml"):
        if not (edge_root / required).exists():
            raise ExactOOFExecutionError(f"Missing {required}: {edge_root / required}")

    for name in CLEAR_EDGE_MODULES:
        sys.modules.pop(name, None)
    for path in (edge_root, _V2_ROOT, _REPO_ROOT):
        s = str(path)
        if s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)

    return importlib.import_module("runtime"), importlib.import_module("model_rows")


def _effective_live_max_signal_age_seconds(v2_root: Path) -> int:
    settings_path = v2_root / "portfolio_live" / "live_settings.py"
    if not settings_path.exists():
        raise ExactOOFExecutionError(f"Missing live settings contract: {settings_path}")
    spec = importlib.util.spec_from_file_location(
        "_quantara_v3_exact_oof_live_settings",
        settings_path,
    )
    if spec is None or spec.loader is None:
        raise ExactOOFExecutionError(f"Could not load live settings: {settings_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = int(getattr(module, "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS", 0))
    if value <= 0:
        raise ExactOOFExecutionError(
            "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS must be > 0, "
            f"got {value!r}"
        )
    return value


def _load_bars(runtime: Any) -> tuple[pd.DatetimeIndex, np.ndarray]:
    if not hasattr(runtime, "_load_asset_bars"):
        raise ExactOOFExecutionError(
            "Edge runtime does not expose _load_asset_bars()."
        )
    bars = runtime._load_asset_bars().copy()  # noqa: SLF001
    missing = [c for c in ("Datetime", "Close") if c not in bars.columns]
    if missing:
        raise ExactOOFExecutionError(f"Asset bars missing required columns: {missing}")
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    bars["Close"] = pd.to_numeric(bars["Close"], errors="coerce")
    bars = (
        bars.dropna(subset=["Datetime", "Close"])
        .sort_values("Datetime")
        .drop_duplicates("Datetime")
        .reset_index(drop=True)
    )
    if bars.empty:
        raise ExactOOFExecutionError("Asset bar table is empty.")
    return pd.DatetimeIndex(bars["Datetime"]), bars["Close"].to_numpy(dtype="float64")


def _load_valid_oof(layout: Any) -> pd.DataFrame:
    path = layout.oof_predictions_csv
    if not path.exists():
        raise ExactOOFExecutionError(
            f"Missing Step 03 OOF predictions: {path}\nRun Step 03 first."
        )
    df = pd.read_csv(path)
    if df.empty:
        raise ExactOOFExecutionError(f"Step 03 OOF predictions are empty: {path}")

    if TIME_COL not in df.columns:
        df = df.rename(columns={df.columns[0]: TIME_COL})

    required = [
        TIME_COL,
        RAW_PROB_COL,
        TARGET_COL,
        T1_COL,
        REALIZED_R_COL,
        EVENT_VOL_COL,
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ExactOOFExecutionError(
            f"Step 03 OOF predictions missing required columns: {missing}"
        )

    df[TIME_COL] = to_datetime_iso(df[TIME_COL], utc=True, errors="coerce")
    df[T1_COL] = to_datetime_iso(df[T1_COL], utc=True, errors="coerce")
    for col in (RAW_PROB_COL, TARGET_COL, REALIZED_R_COL, EVENT_VOL_COL):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "side" in df.columns:
        df["side"] = pd.to_numeric(df["side"], errors="coerce")
    else:
        df["side"] = int(layout.side_value)

    df = df.dropna(
        subset=[
            TIME_COL,
            RAW_PROB_COL,
            TARGET_COL,
            T1_COL,
            REALIZED_R_COL,
            EVENT_VOL_COL,
            "side",
        ]
    ).copy()
    if df.empty:
        raise ExactOOFExecutionError(
            "No valid Step 03 OOF rows remain after the canonical Step 04 filter."
        )

    validate_gross_label_frame(df, context=f"{layout.edge_id} Step03 OOF")

    non_matching = df["side"].astype(int) != int(layout.side_value)
    if bool(non_matching.any()):
        raise ExactOOFExecutionError(
            f"{layout.edge_id}: {int(non_matching.sum())} OOF rows have the wrong side."
        )
    if not (np.isfinite(df[EVENT_VOL_COL].to_numpy(float)) & (df[EVENT_VOL_COL].to_numpy(float) > 0)).all():
        raise ExactOOFExecutionError("OOF Event_Vol must be finite and > 0.")

    df = df.sort_values(TIME_COL).reset_index(drop=True)
    if df[TIME_COL].duplicated().any():
        raise ExactOOFExecutionError(
            "OOF event timestamps must be unique for a side-specialized edge."
        )
    df["source_oof_row_number"] = np.arange(len(df), dtype=int)
    return df


def _tick_columns(columns: list[str]) -> tuple[str, str, str]:
    lower = {str(c).strip().lower(): str(c) for c in columns}

    def pick(names: tuple[str, ...]) -> str | None:
        for name in names:
            if name in lower:
                return lower[name]
        return None

    t = pick(("timestamp", "time", "datetime", "date_time"))
    b = pick(("bidprice", "bid_price", "bid", "bid price"))
    a = pick(("askprice", "ask_price", "ask", "ask price"))
    if t is None or b is None or a is None:
        raise ExactOOFExecutionError(
            f"Tick file missing timestamp/bid/ask. Columns={columns}"
        )
    return t, b, a


@lru_cache(maxsize=300)
def _load_tick_file(path_text: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    path = Path(path_text)
    header = pd.read_csv(path, nrows=0)
    tc, bc, ac = _tick_columns(list(header.columns))
    frame = pd.read_csv(path, usecols=[tc, bc, ac])
    ts = to_datetime_iso(frame[tc], utc=True, errors="coerce")
    bid = pd.to_numeric(frame[bc], errors="coerce")
    ask = pd.to_numeric(frame[ac], errors="coerce")
    good = (
        ts.notna() & bid.notna() & ask.notna()
        & (bid > 0) & (ask > 0) & (ask >= bid)
    )
    if not bool(good.any()):
        return (
            np.array([], dtype="int64"),
            np.array([], dtype="float64"),
            np.array([], dtype="float64"),
        )
    q = pd.DataFrame({"ts": ts[good], "bid": bid[good], "ask": ask[good]}).sort_values("ts")
    return (
        # Must force ns resolution before the int64 view -- see the identical
        # fix and its full explanation in shared/research/v3_horizon_lab.py's
        # _xef_loader (2026-09-14): pandas >=2.0 can infer a coarser storage
        # unit (e.g. datetime64[us]) from the input strings' precision, and
        # Series.astype("int64") reflects that storage unit, not nanoseconds.
        q["ts"].to_numpy(dtype="datetime64[ns]").astype("int64"),
        q["bid"].to_numpy(dtype="float64"),
        q["ask"].to_numpy(dtype="float64"),
    )


def _utc_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _build_tick_catalog(model_rows: Any) -> list[dict[str, Any]]:
    if not hasattr(model_rows, "build_tick_file_catalog"):
        raise ExactOOFExecutionError(
            "model_rows.py does not expose build_tick_file_catalog()."
        )
    raw = model_rows.build_tick_file_catalog()
    if not isinstance(raw, list) or not raw:
        raise ExactOOFExecutionError("build_tick_file_catalog() returned no tick files.")

    catalog = []
    for record in raw:
        if not isinstance(record, dict) or "path" not in record or "end_date" not in record:
            raise ExactOOFExecutionError(
                f"Invalid tick catalog record: {record!r}"
            )
        path = Path(str(record["path"])).resolve()
        if not path.exists():
            raise ExactOOFExecutionError(f"Tick catalog file does not exist: {path}")
        catalog.append({**record, "path": path, "_end": _utc_timestamp(record["end_date"])})
    catalog.sort(key=lambda rec: rec["_end"])
    return catalog


def _catalog_start(catalog: list[dict[str, Any]], event_time: pd.Timestamp) -> int:
    day = pd.Timestamp(event_time.date(), tz="UTC")
    for i, record in enumerate(catalog):
        if record["_end"] >= day:
            return i
    return max(0, len(catalog) - 1)


def _first_hit(
    prices: np.ndarray,
    side: int,
    pt_level: float,
    sl_level: float,
) -> tuple[int | None, str | None]:
    if prices.size == 0:
        return None, None
    if side == 1:
        p_idx = np.flatnonzero(prices >= pt_level)
        s_idx = np.flatnonzero(prices <= sl_level)
    else:
        p_idx = np.flatnonzero(prices <= pt_level)
        s_idx = np.flatnonzero(prices >= sl_level)

    p = int(p_idx[0]) if p_idx.size else None
    s = int(s_idx[0]) if s_idx.size else None
    if p is None and s is None:
        return None, None
    if s is None or (p is not None and p < s):
        return p, "pt"
    if p is None or s < p:
        return s, "sl"
    raise ExactOOFExecutionError(
        "A valid executable quote cannot touch separated PT and SL simultaneously."
    )


def _unresolved(
    row_number: int,
    event_time: pd.Timestamp,
    side: int,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "source_oof_row_number": int(row_number),
        TIME_COL: event_time,
        "side": int(side),
        "execution_status": status,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        **extra,
    }


def _resolve_one(
    row: pd.Series,
    *,
    bars_dt: pd.DatetimeIndex,
    bars_close: np.ndarray,
    catalog: list[dict[str, Any]],
    geometry: dict[str, Any],
    max_signal_age_seconds: int,
    bars_dt_ns: np.ndarray | None = None,
    entry_delay_ms: int = 0,
) -> dict[str, Any]:
    # entry_delay_ms (ADDED 2026-10-16, default 0 = byte-identical Task 2A behaviour): used only by the allocator's
    # per-broker re-pricing step (portfolio_allocator/00d) to mirror the measured live bar-close->fill latency.
    event_time = pd.Timestamp(row[TIME_COL])
    side = int(row["side"])
    row_number = int(row["source_oof_row_number"])
    event_vol = float(row[EVENT_VOL_COL])

    loc = int(bars_dt.get_indexer(pd.DatetimeIndex([event_time]))[0])
    if loc < 0:
        return _unresolved(row_number, event_time, side, "event_bar_not_found")

    # REVISED 2026-09-25 (calendar-time labeling rollout): when edge.yaml
    # carries labeling.vertical_horizon_hours, the deadline is a real
    # calendar-time offset (matching Gate A/B/C's v3_horizon_lab.py and
    # shared/labels/calendar_time_barriers.py exactly) instead of a fixed
    # bar-count offset. Every other edge (calendar_time=False) is byte-for-
    # byte unchanged.
    if bool(geometry.get("calendar_time")):
        # CRITICAL: pandas' DatetimeIndex.asi8 resolution is NOT guaranteed
        # nanoseconds across pandas versions (confirmed 2026-09-25: pandas
        # 2.3.3/venv39 returns ns, pandas 3.0.2/venv312 -- this module's own
        # venv -- returns microseconds by default). calendar_time_barriers.py
        # hardcodes a nanosecond assumption, so an un-normalized .asi8 here
        # would silently compute deadlines ~1000x too far out (nearly every
        # event unresolved). bars_dt_ns is precomputed once by the caller via
        # .as_unit("ns").asi8 -- never call bars_dt.asi8 directly in this file.
        if bars_dt_ns is None:
            bars_dt_ns = bars_dt.as_unit("ns").asi8
        deadline_pos = int(calendar_time_vertical_deadline_positions(
            bars_dt_ns, np.array([loc]), hours=float(geometry["vertical_horizon_hours"]),
        )[0])
        if deadline_pos < 0 or deadline_pos >= len(bars_dt):
            return _unresolved(
                row_number, event_time, side, "vertical_deadline_bar_not_found"
            )
    else:
        hv = int(geometry["vertical_horizon_bars"])
        if loc + hv >= len(bars_dt):
            return _unresolved(
                row_number, event_time, side, "vertical_deadline_bar_not_found"
            )
        deadline_pos = loc + hv

    signal_mid = float(bars_close[loc])
    if not np.isfinite(signal_mid) or signal_mid <= 0:
        return _unresolved(row_number, event_time, side, "invalid_signal_mid")

    research_entry = pd.to_numeric(
        pd.Series([row.get("entry_price", np.nan)]), errors="coerce"
    ).iloc[0]
    if pd.notna(research_entry) and not np.isclose(
        float(research_entry),
        signal_mid,
        rtol=1e-10,
        atol=max(1e-12, abs(signal_mid) * 1e-12),
    ):
        return _unresolved(
            row_number,
            event_time,
            side,
            "research_entry_signal_mid_mismatch",
            signal_mid=signal_mid,
            research_entry_price=float(research_entry),
        )

    sl_dist = signal_mid * float(geometry["sl_multiplier"]) * event_vol
    tp_dist = signal_mid * float(geometry["pt_multiplier"]) * event_vol
    if not np.isfinite(sl_dist) or not np.isfinite(tp_dist) or min(sl_dist, tp_dist) <= 0:
        return _unresolved(
            row_number,
            event_time,
            side,
            "invalid_barrier_distance",
            signal_mid=signal_mid,
            Event_Vol=event_vol,
        )

    deadline = pd.Timestamp(bars_dt[deadline_pos])
    threshold_ns = int(event_time.value) + int(entry_delay_ms) * 1_000_000
    start_file = _catalog_start(catalog, event_time)

    entry = None
    for file_index in range(start_file, len(catalog)):
        ts, bid, ask = _load_tick_file(str(catalog[file_index]["path"]))
        if ts.size == 0:
            continue
        ix = int(np.searchsorted(ts, threshold_ns, side="right"))
        if ix < len(ts):
            entry = (
                file_index,
                ix,
                int(ts[ix]),
                float(bid[ix]),
                float(ask[ix]),
            )
            break

    if entry is None:
        return _unresolved(
            row_number,
            event_time,
            side,
            "missing_entry_quote",
            signal_mid=signal_mid,
            Event_Vol=event_vol,
        )

    file0, ix0, entry_ns, entry_bid, entry_ask = entry
    entry_time = pd.Timestamp(entry_ns, tz="UTC")
    entry_age_seconds = float((entry_time - event_time).total_seconds())
    if entry_age_seconds > float(max_signal_age_seconds):
        return _unresolved(
            row_number,
            event_time,
            side,
            "stale_entry_quote",
            signal_mid=signal_mid,
            Event_Vol=event_vol,
            entry_time=entry_time,
            entry_age_seconds=entry_age_seconds,
            max_signal_age_seconds=int(max_signal_age_seconds),
        )

    entry_price = entry_ask if side == 1 else entry_bid
    if side == 1:
        sl_level = entry_price - sl_dist
        pt_level = entry_price + tp_dist
    else:
        sl_level = entry_price + sl_dist
        pt_level = entry_price - tp_dist

    deadline_ns = int(deadline.value)
    result = None
    for file_index in range(file0, len(catalog)):
        ts, bid, ask = _load_tick_file(str(catalog[file_index]["path"]))
        if ts.size == 0:
            continue
        begin = ix0 if file_index == file0 else 0

        horizontal_end = int(np.searchsorted(ts, deadline_ns, side="right"))
        if horizontal_end > begin:
            exit_side = bid[begin:horizontal_end] if side == 1 else ask[begin:horizontal_end]
            rel, hit = _first_hit(exit_side, side, float(pt_level), float(sl_level))
            if hit is not None:
                j = begin + int(rel)
                observed = float(bid[j] if side == 1 else ask[j])
                exit_price = float(pt_level) if hit == "pt" else observed
                gross = (
                    exit_price - entry_price
                    if side == 1
                    else entry_price - exit_price
                )
                result = {
                    "exit_time": pd.Timestamp(int(ts[j]), tz="UTC"),
                    "exit_price": exit_price,
                    "exit_reason": hit,
                    "executed_r": float(gross / sl_dist),
                }
                break

        vertical_ix = int(np.searchsorted(ts, deadline_ns, side="right"))
        if vertical_ix < len(ts):
            exit_price = float(bid[vertical_ix] if side == 1 else ask[vertical_ix])
            gross = (
                exit_price - entry_price
                if side == 1
                else entry_price - exit_price
            )
            result = {
                "exit_time": pd.Timestamp(int(ts[vertical_ix]), tz="UTC"),
                "exit_price": exit_price,
                "exit_reason": "vertical",
                "executed_r": float(gross / sl_dist),
            }
            break

    if result is None:
        return _unresolved(
            row_number,
            event_time,
            side,
            "unresolved_tick_coverage",
            signal_mid=signal_mid,
            Event_Vol=event_vol,
            entry_time=entry_time,
            vertical_deadline=deadline,
        )

    entry_spread = float(entry_ask - entry_bid)
    exit_time = pd.Timestamp(result["exit_time"])
    return {
        "source_oof_row_number": row_number,
        TIME_COL: event_time,
        "side": side,
        "execution_status": "resolved",
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "execution_geometry_hash": str(geometry["geometry_hash"]),
        "signal_mid": signal_mid,
        "Event_Vol": event_vol,
        "vertical_deadline": deadline,
        "entry_time": entry_time,
        "entry_bid": entry_bid,
        "entry_ask": entry_ask,
        "entry_price": float(entry_price),
        "entry_spread": entry_spread,
        "entry_spread_bps": float(entry_spread / signal_mid * 10000.0),
        "entry_lag_ms": entry_age_seconds * 1000.0,
        "entry_drift_bps": float(abs(entry_price - signal_mid) / signal_mid * 10000.0),
        "stop_distance_price": float(sl_dist),
        "take_profit_distance_price": float(tp_dist),
        "sl_price": float(sl_level),
        "pt_price": float(pt_level),
        "exit_time": exit_time,
        "exit_price": float(result["exit_price"]),
        "exit_reason": str(result["exit_reason"]),
        "executed_r": float(result["executed_r"]),
        "hold_seconds": float((exit_time - entry_time).total_seconds()),
    }


def _profit_factor(r: np.ndarray) -> float:
    if len(r) == 0:
        return float("nan")
    gp = float(r[r > 0].sum()) if np.any(r > 0) else 0.0
    gl = float(abs(r[r <= 0].sum())) if np.any(r <= 0) else 0.0
    if gl <= 0:
        return float("inf") if gp > 0 else float("nan")
    return gp / gl


def _max_drawdown(r: np.ndarray) -> float:
    if len(r) == 0:
        return 0.0
    equity = pd.Series(r).cumsum()
    return float((equity - equity.cummax()).min())


def run_step03a(edge_root: str | Path, *, write: bool = True) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    validate_edge_yaml_file(layout.edge_root)
    geometry = load_trade_geometry(
        layout.edge_yaml,
        expected_edge_id=layout.edge_id,
        expected_side_name=layout.side_name,
    )

    runtime, model_rows = _import_edge_modules(layout.edge_root)
    max_signal_age_seconds = _effective_live_max_signal_age_seconds(layout.v2_root)
    oof = _load_valid_oof(layout)
    upstream_generation = validate_step03_generation(layout)
    bars_dt, bars_close = _load_bars(runtime)
    catalog = _build_tick_catalog(model_rows)

    output_dir = layout.artifacts_dir / "execution"
    output_csv = output_dir / f"{layout.edge_stem}_oof_exact_execution.csv"
    output_manifest = output_dir / "oof_exact_execution_manifest.json"

    print("=" * 96, flush=True)
    print(
        f"V3 STEP 03A: EXACT BID/ASK OOF EXECUTABLE ECONOMICS — {layout.edge_id}",
        flush=True,
    )
    print("=" * 96, flush=True)
    print(f"OOF rows to replay: {len(oof):,}", flush=True)
    _hv_display = (
        f"{geometry['vertical_horizon_hours']:g}h" if geometry.get("calendar_time")
        else f"{geometry['vertical_horizon_bars']}bars"
    )
    print(
        f"Geometry: Hv={_hv_display} "
        f"PT={geometry['pt_multiplier']:g} SL={geometry['sl_multiplier']:g}",
        flush=True,
    )
    print(f"Live signal-age ceiling: {max_signal_age_seconds}s", flush=True)
    print(
        "Contract: first quote strictly after event; "
        "LONG Ask->Bid / SHORT Bid->Ask; raw spread embedded.",
        flush=True,
    )

    # Precompute ONCE, not per-event -- see the CRITICAL note in _resolve_one
    # about pandas' asi8 resolution not being guaranteed nanoseconds.
    bars_dt_ns = bars_dt.as_unit("ns").asi8 if bool(geometry.get("calendar_time")) else None

    rows = []
    started = time.time()
    for i, (_, row) in enumerate(oof.iterrows(), start=1):
        rows.append(
            _resolve_one(
                row,
                bars_dt=bars_dt,
                bars_close=bars_close,
                catalog=catalog,
                geometry=geometry,
                max_signal_age_seconds=max_signal_age_seconds,
                bars_dt_ns=bars_dt_ns,
            )
        )
        if i % 250 == 0 or i == len(oof):
            print(
                f"  {i:,}/{len(oof):,} | elapsed={time.time() - started:.1f}s",
                flush=True,
            )

    execution = pd.DataFrame(rows).sort_values("source_oof_row_number").reset_index(drop=True)
    resolved = execution["execution_status"].eq("resolved")
    executed_numeric = pd.to_numeric(
        execution.get("executed_r", pd.Series(np.nan, index=execution.index)),
        errors="coerce",
    )
    execution["executed_meta_label"] = pd.Series(
        np.where(resolved & executed_numeric.notna(), (executed_numeric > 0.0).astype(int), np.nan),
        index=execution.index,
    ).astype("Int64")
    execution["gross_meta_label"] = pd.to_numeric(oof[TARGET_COL], errors="coerce").astype("Int64").to_numpy()
    execution["gross_to_executed_label_flip"] = (
        execution["gross_meta_label"].notna()
        & execution["executed_meta_label"].notna()
        & (execution["gross_meta_label"] != execution["executed_meta_label"])
    )
    resolved_count = int(resolved.sum())
    total = int(len(execution))
    coverage = float(resolved_count / total) if total else 0.0
    status_counts = execution["execution_status"].astype(str).value_counts(dropna=False).to_dict()
    r = pd.to_numeric(
        execution.loc[resolved, "executed_r"], errors="coerce"
    ).dropna().to_numpy(dtype=float)

    audit_status = "PASS" if total > 0 and resolved_count == total else "FAIL"
    summary = {
        "rows_expected": total,
        "rows_resolved": resolved_count,
        "rows_unresolved": int(total - resolved_count),
        "coverage_rate": coverage,
        "status_counts": status_counts,
        "exec_mean_r": float(np.mean(r)) if len(r) else float("nan"),
        "exec_total_r": float(np.sum(r)) if len(r) else 0.0,
        "exec_win_rate": float(np.mean(r > 0)) if len(r) else float("nan"),
        "exec_profit_factor": _profit_factor(r),
        "exec_max_drawdown_r": _max_drawdown(r),
        "fail_count": 0 if audit_status == "PASS" else int(total - resolved_count),
        "executed_positive_count": int((execution.loc[resolved, "executed_meta_label"] == 1).sum()),
        "executed_negative_or_zero_count": int((execution.loc[resolved, "executed_meta_label"] == 0).sum()),
        "gross_to_executed_label_flip_count": int(execution.loc[resolved, "gross_to_executed_label_flip"].sum()),
    }
    if resolved_count:
        resolved_frame = execution.loc[resolved]
        summary["exit_reason_counts"] = (
            resolved_frame["exit_reason"].astype(str).value_counts().to_dict()
        )
        for column in (
            "entry_spread_bps",
            "entry_drift_bps",
            "entry_lag_ms",
            "hold_seconds",
        ):
            values = pd.to_numeric(
                resolved_frame[column], errors="coerce"
            ).dropna().to_numpy(float)
            if len(values):
                summary[f"median_{column}"] = float(np.median(values))
                summary[f"p95_{column}"] = float(np.quantile(values, 0.95))

    manifest = {
        "schema_version": "v3.1",
        "created_at": now_utc(),
        "execution_audit_status": audit_status,
        "summary": summary,
        "edge_id": layout.edge_id,
        "asset": layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
        "execution_contract_version": EXECUTION_CONTRACT_VERSION,
        "executed_target_contract_version": EXECUTED_TARGET_CONTRACT_VERSION,
        "executed_target_column": "executed_meta_label",
        "executed_target_definition": "int(executed_r > 0)",
        "gross_target_column_preserved_for_diagnostics": "gross_meta_label",
        "geometry_hash": str(geometry["geometry_hash"]),
        "trade_geometry": geometry,
        "input_oof_path": str(
            layout.oof_predictions_csv.resolve().relative_to(layout.v2_root.resolve())
        ).replace("\\", "/"),
        "input_oof_sha256": sha256_file(layout.oof_predictions_csv),
        "edge_yaml_sha256": sha256_file(layout.edge_yaml),
        "live_signal_age_max_seconds": int(max_signal_age_seconds),
        "threshold_selected_here": False,
        "entry_contract": (
            "first valid quote strictly after closed event timestamp; "
            "LONG Ask / SHORT Bid"
        ),
        "distance_contract": (
            "signal_mid * Event_Vol * multiplier frozen before execution; "
            "levels rebuilt around executable entry"
        ),
        "exit_contract": (
            "LONG Bid / SHORT Ask; PT capped at intended level; "
            "adverse SL quote retained; vertical first quote strictly after deadline"
        ),
        "raw_bid_ask_spread_embedded": True,
        "outputs": {
            "exact_execution_csv": str(
                output_csv.resolve().relative_to(layout.v2_root.resolve())
            ).replace("\\", "/"),
            "manifest": str(
                output_manifest.resolve().relative_to(layout.v2_root.resolve())
            ).replace("\\", "/"),
        },
        "next_ordered_step": "03b_oof_threshold_sweep.py",
    }

    manifest.update({
        "gross_label_contract_version": GROSS_LABEL_CONTRACT_VERSION,
        "gross_target_column": TARGET_COL,
        "gross_realized_r_column": REALIZED_R_COL,
        "gross_target_definition": GROSS_TARGET_DEFINITION,
        "step02_training_manifest_sha256": upstream_generation["step02_training_manifest_sha256"],
        "step03_oof_manifest_sha256": upstream_generation["step03_oof_manifest_sha256"],
        "phase3a_provenance_status": "PASS",
    })

    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        temp_csv = output_csv.with_suffix(output_csv.suffix + ".tmp")
        temp_json = output_manifest.with_suffix(output_manifest.suffix + ".tmp")
        execution.to_csv(temp_csv, index=False)
        # Phase 3C: bind the Step03A manifest to its own exact-execution bytes.
        manifest["exact_execution_sha256"] = sha256_file(temp_csv)
        temp_json.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temp_csv.replace(output_csv)
        temp_json.replace(output_manifest)

    print("", flush=True)
    print(
        f"03A coverage: {resolved_count:,}/{total:,} = {coverage:.6f}",
        flush=True,
    )
    print(f"03A status: {audit_status}", flush=True)
    if len(r):
        print(
            f"Exact OOF economics: mean={np.mean(r):+.6f}R "
            f"PF={_profit_factor(r):.4f} total={np.sum(r):+.3f}R "
            f"maxDD={_max_drawdown(r):+.3f}R",
            flush=True,
        )
    print(f"Saved: {output_csv}", flush=True)
    print(f"Saved: {output_manifest}", flush=True)

    if audit_status != "PASS":
        raise ExactOOFExecutionError(
            "Step 03A exact Bid/Ask coverage is incomplete. "
            "Detailed artifacts were written for diagnosis; stop the lifecycle here."
        )
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quantara V3 Step 03A exact Bid/Ask OOF executable economics."
    )
    parser.add_argument("--edge-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    run_step03a(args.edge_root, write=not args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except ExactOOFExecutionError as exc:
        raise SystemExit(f"❌ {exc}") from exc
