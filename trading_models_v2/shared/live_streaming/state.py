"""Shared state and IO helpers for the V2 live streaming hot path."""

from __future__ import annotations

import copy
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

HOTPATH_VERSION = "v2_live_streaming_hotpath_v11_phase_b_bounded_dependency_rings"
PROBE_SCHEMA = "v2_absorption_event_probe_state_v1"
PROBE_MARKER = "__live_absorption_probe_only__"

INSTALL_LOCK = threading.RLock()
ASSET_GUARD = threading.RLock()
INSTALLED = False
ORIGINAL_FFD_VERIFY: Callable[[str], dict[str, Any]] | None = None
ORIGINAL_HMM_VERIFY: Callable[[str], dict[str, Any]] | None = None
FFD_MODULE: ModuleType | None = None
HMM_MODULE: ModuleType | None = None
ASSET_LOCKS: dict[str, threading.RLock] = {}
FFD_CACHE: dict[str, dict[str, Any]] = {}
HMM_CACHE: dict[str, dict[str, Any]] = {}
FFD_DELTA: dict[str, pd.DataFrame] = {}
FRAME_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
HMM_CONTEXT: dict[str, dict[str, Any]] = {}
HMM_INPUT_RING: dict[str, dict[str, Any]] = {}
FEATURE_SNAPSHOT_CONTEXT = threading.local()
ACTIVE_FEATURE_SNAPSHOTS_LOCK = threading.RLock()
ACTIVE_FEATURE_SNAPSHOTS: dict[str, dict[str, Any]] = {}
LIVE_BAR_AUTHORITY_READER_LOCK = threading.RLock()
LIVE_BAR_AUTHORITY_READER: Callable[[str], pd.DataFrame] | None = None
DEPENDENCY_GENERATION_LOCK = threading.RLock()
READY_DEPENDENCY_GENERATIONS: dict[str, dict[str, Any]] = {}


def asset_key(asset: str) -> str:
    return str(asset).strip().upper()


def asset_lock(asset: str) -> threading.RLock:
    key = asset_key(asset)
    with ASSET_GUARD:
        return ASSET_LOCKS.setdefault(key, threading.RLock())


def path_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)
    except FileNotFoundError:
        return None


def live_signature(asset: str) -> tuple[int, int] | None:
    if FFD_MODULE is None:
        return None
    path = (
        Path(FFD_MODULE.LIVE_BARS_DIR)
        / f"{asset_key(asset)}_live_standard_quote_tick_bars.csv"
    )
    return path_signature(path)


def utc_timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def parse_utc_series(values: Any) -> pd.Series:
    # LIVE_HOTPATH_MIXED_TIMESTAMP_PARSER_V1
    #
    # Persisted/recovery rows can use an ISO "T" separator while pandas append
    # writes can use a space separator. Whole-second and fractional-second
    # precision can also coexist. Pandas 2.x may infer one vector-wide format
    # and silently coerce the other valid representation to NaT. Dependency
    # readiness must never treat a serialization difference as a missing row.
    raw = values.copy() if isinstance(values, pd.Series) else pd.Series(values)
    try:
        parsed = pd.to_datetime(
            raw,
            utc=True,
            errors="coerce",
            format="mixed",
        )
    except (TypeError, ValueError):
        parsed = to_datetime_iso(raw, utc=True, errors="coerce")

    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        recovered: list[Any] = []
        for value in raw.loc[missed]:
            try:
                recovered.append(utc_timestamp(value))
            except Exception:
                recovered.append(pd.NaT)
        parsed.loc[missed] = recovered
    return parsed


def copy_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(result))


def publish_dependency_generation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Publish one coherent FFD+HMM generation for read-only edge consumers."""
    payload = copy_result(record)
    key = asset_key(str(payload.get("asset") or ""))
    if not key:
        raise RuntimeError("dependency generation is missing asset")
    if str(payload.get("status") or "") != "READY":
        raise RuntimeError(
            f"{key}: dependency generation cannot publish non-READY status="
            f"{payload.get('status')}"
        )
    generation_id = str(payload.get("generation_id") or "")
    if not generation_id:
        raise RuntimeError(f"{key}: dependency generation is missing generation_id")
    with DEPENDENCY_GENERATION_LOCK:
        READY_DEPENDENCY_GENERATIONS[key] = payload
    return copy_result(payload)


def current_dependency_generation(asset: str) -> dict[str, Any] | None:
    key = asset_key(asset)
    with DEPENDENCY_GENERATION_LOCK:
        value = READY_DEPENDENCY_GENERATIONS.get(key)
        return None if value is None else copy_result(value)


def clear_dependency_generation(asset: str) -> None:
    key = asset_key(asset)
    with DEPENDENCY_GENERATION_LOCK:
        READY_DEPENDENCY_GENERATIONS.pop(key, None)


def _copy_feature_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name, value in snapshot.items():
        output[name] = value.copy(deep=True) if isinstance(value, pd.DataFrame) else copy.deepcopy(value)
    return output


def current_feature_snapshot(asset: str) -> dict[str, Any] | None:
    snapshot = getattr(FEATURE_SNAPSHOT_CONTEXT, "snapshot", None)
    key = asset_key(asset)
    if isinstance(snapshot, Mapping) and asset_key(str(snapshot.get("asset") or "")) == key:
        return snapshot  # private worker authority; consumers receive copies
    # Edge scoring can fan out to child threads. The coordinating thread holds
    # the shared per-asset lock for the entire snapshot, making this registry a
    # safe propagation bridge to those child workers.
    with ACTIVE_FEATURE_SNAPSHOTS_LOCK:
        return ACTIVE_FEATURE_SNAPSHOTS.get(key)


@contextmanager
def feature_snapshot_scope(snapshot: Mapping[str, Any]):
    """Bind one verified dependency generation to the current scoring worker."""
    previous = getattr(FEATURE_SNAPSHOT_CONTEXT, "snapshot", None)
    bound = _copy_feature_snapshot(snapshot)
    key = asset_key(str(bound.get("asset") or ""))
    FEATURE_SNAPSHOT_CONTEXT.snapshot = bound
    with ACTIVE_FEATURE_SNAPSHOTS_LOCK:
        previous_active = ACTIVE_FEATURE_SNAPSHOTS.get(key)
        ACTIVE_FEATURE_SNAPSHOTS[key] = bound
    try:
        yield bound
    finally:
        FEATURE_SNAPSHOT_CONTEXT.snapshot = previous
        with ACTIVE_FEATURE_SNAPSHOTS_LOCK:
            if ACTIVE_FEATURE_SNAPSHOTS.get(key) is bound:
                if previous_active is None:
                    ACTIVE_FEATURE_SNAPSHOTS.pop(key, None)
                else:
                    ACTIVE_FEATURE_SNAPSHOTS[key] = previous_active


def cache_put(
    cache: dict[str, dict[str, Any]],
    asset: str,
    signature: Any,
    result: Mapping[str, Any],
) -> None:
    cache[asset_key(asset)] = {"signature": signature, "result": copy_result(result)}


def cache_get(
    cache: dict[str, dict[str, Any]], asset: str, signature: Any
) -> dict[str, Any] | None:
    record = cache.get(asset_key(asset))
    if record and record.get("signature") == signature:
        return copy_result(record["result"])
    return None


def append_csv_rows(path: Path, rows: pd.DataFrame, columns: list[str]) -> None:
    if rows.empty:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        rows[columns].to_csv(handle, index=False, header=header)
        handle.flush()
        os.fsync(handle.fileno())


def install_live_bar_authority_reader(
    reader: Callable[[str], pd.DataFrame],
) -> None:
    """Install the dashboard-owned in-memory live-bar reader."""
    if not callable(reader):
        raise TypeError("live bar authority reader must be callable")
    global LIVE_BAR_AUTHORITY_READER
    with LIVE_BAR_AUTHORITY_READER_LOCK:
        LIVE_BAR_AUTHORITY_READER = reader


def clear_live_bar_authority_reader() -> None:
    """Test/shutdown helper. Disk remains the recovery fallback."""
    global LIVE_BAR_AUTHORITY_READER
    with LIVE_BAR_AUTHORITY_READER_LOCK:
        LIVE_BAR_AUTHORITY_READER = None


def normalized_live_bars_disk(asset: str) -> pd.DataFrame:
    """Explicit disk authority for startup/recovery/oracle paths."""
    if FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    path = (
        Path(FFD_MODULE.LIVE_BARS_DIR)
        / f"{asset_key(asset)}_live_standard_quote_tick_bars.csv"
    )
    if not path.exists():
        return pd.DataFrame(columns=["Datetime", "Open", "High", "Low", "Close"])
    wanted = {"Datetime", "Open", "High", "Low", "Close"}
    raw = pd.read_csv(path, usecols=lambda name: name in wanted)
    return FFD_MODULE._normalize_bar_table(
        raw,
        source="portfolio_live_bars_disk_oracle",
    )


def normalized_live_bars(asset: str) -> pd.DataFrame:
    """Normal dependency read: in-memory authority once dashboard installs it."""
    if FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    key = asset_key(asset)
    with LIVE_BAR_AUTHORITY_READER_LOCK:
        reader = LIVE_BAR_AUTHORITY_READER
    if callable(reader):
        frame = reader(key)
        if not isinstance(frame, pd.DataFrame):
            raise RuntimeError(
                f"{key}: live bar authority returned {type(frame).__name__}"
            )
        wanted = ["Datetime", "Open", "High", "Low", "Close"]
        if frame.empty:
            return pd.DataFrame(columns=wanted)
        missing = [column for column in wanted if column not in frame.columns]
        if missing:
            raise RuntimeError(
                f"{key}: in-memory live bar authority missing columns={missing}"
            )
        return FFD_MODULE._normalize_bar_table(
            frame[wanted].copy(),
            source="dashboard_in_memory_live_bar_authority",
        )
    return normalized_live_bars_disk(key)



def _coerce_frame_limit(max_rows: int | None) -> int | None:
    if max_rows is None:
        return None
    limit = int(max_rows)
    if limit <= 0:
        raise ValueError(f"dependency frame max_rows must be > 0, got {limit}")
    return limit


def frame_set(
    kind: str,
    asset: str,
    path: Path,
    frame: pd.DataFrame,
    *,
    max_rows: int | None = None,
) -> None:
    """Store a dependency frame, optionally as a bounded live suffix.

    PHASE_B_BOUNDED_DEPENDENCY_FRAME_V1

    Persistent CSV authority remains complete. Only the normal live in-memory
    representation is bounded; startup/recovery may still read and verify the
    full authority before seeding this suffix.
    """
    limit = _coerce_frame_limit(max_rows)
    stored = frame
    if limit is not None:
        stored = frame.tail(limit).reset_index(drop=True).copy()
    FRAME_CACHE[(str(kind), asset_key(asset))] = {
        "path": str(Path(path).resolve()),
        "signature": path_signature(Path(path)),
        "frame": stored,
        "max_rows": limit,
    }


def frame_extend(kind: str, asset: str, path: Path, rows: pd.DataFrame) -> None:
    """Extend only the already-bounded normal dependency suffix."""
    if rows.empty:
        return
    key = (str(kind), asset_key(asset))
    record = FRAME_CACHE.get(key)
    if not record or record.get("path") != str(Path(path).resolve()):
        return
    current = record.get("frame")
    if not isinstance(current, pd.DataFrame) or any(
        column not in rows.columns for column in current.columns
    ):
        FRAME_CACHE.pop(key, None)
        return

    limit = _coerce_frame_limit(record.get("max_rows"))
    incoming = rows[list(current.columns)].copy()
    incoming["Datetime"] = parse_utc_series(incoming["Datetime"])
    incoming = (
        incoming.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )
    if incoming.empty:
        return

    output = pd.concat([current, incoming], ignore_index=True)
    output["Datetime"] = parse_utc_series(output["Datetime"])
    output = output.dropna(subset=["Datetime"]).sort_values(
        "Datetime", kind="mergesort"
    )
    output = output.drop_duplicates(
        "Datetime", keep="first"
    ).reset_index(drop=True)
    if limit is not None:
        output = output.tail(limit).reset_index(drop=True)

    frame_set(
        kind,
        asset,
        path,
        output,
        max_rows=limit,
    )


def frame_load(
    kind: str,
    asset: str,
    path: Path,
    columns: list[str],
    *,
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Startup/recovery authority load with optional bounded live retention."""
    key = (str(kind), asset_key(asset))
    requested_limit = _coerce_frame_limit(max_rows)
    resolved = str(Path(path).resolve())
    signature = path_signature(Path(path))
    record = FRAME_CACHE.get(key)

    if (
        record
        and record.get("path") == resolved
        and record.get("signature") == signature
    ):
        frame = record.get("frame")
        cached_limit = _coerce_frame_limit(record.get("max_rows"))
        if isinstance(frame, pd.DataFrame) and all(
            column in frame.columns for column in columns
        ):
            if requested_limit is None and cached_limit is None:
                return frame[columns].copy(deep=False)
            if requested_limit is not None and (
                cached_limit is None or cached_limit >= requested_limit
            ):
                if cached_limit != requested_limit:
                    frame_set(
                        kind,
                        asset,
                        path,
                        frame,
                        max_rows=requested_limit,
                    )
                    frame = FRAME_CACHE[key]["frame"]
                return frame[columns].copy(deep=False)

    # Full authority reads are allowed only for startup/recovery/oracle callers.
    frame = pd.read_csv(path, usecols=columns)
    frame["Datetime"] = parse_utc_series(frame["Datetime"])
    for column in columns:
        if column != "Datetime":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=columns).sort_values(
        "Datetime", kind="mergesort"
    )
    frame = frame.drop_duplicates(
        "Datetime", keep="first"
    ).reset_index(drop=True)
    if frame.empty:
        raise RuntimeError(f"{kind} verified frame is empty: {path}")

    frame_set(
        kind,
        asset,
        path,
        frame,
        max_rows=requested_limit,
    )
    stored = FRAME_CACHE[key]["frame"]
    return stored[columns].copy(deep=False)


def frame_cache_status() -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for (kind, asset), record in list(FRAME_CACHE.items()):
        frame = record.get("frame")
        rows = int(len(frame)) if isinstance(frame, pd.DataFrame) else 0
        first = None
        latest = None
        if rows and "Datetime" in frame.columns:
            try:
                first = utc_timestamp(frame["Datetime"].iloc[0]).isoformat()
                latest = utc_timestamp(frame["Datetime"].iloc[-1]).isoformat()
            except Exception:
                first = None
                latest = None
        output[f"{kind}:{asset}"] = {
            "rows": rows,
            "max_rows": record.get("max_rows"),
            "first": first,
            "latest": latest,
        }
    return output


def frame_cached(
    kind: str,
    asset: str,
    path: Path,
    columns: list[str],
) -> pd.DataFrame:
    """Return the bounded warm dependency suffix with no disk fallback.

    PHASE_B_NORMAL_DEPENDENCY_FRAME_CACHE_V2

    A normal scoring snapshot may never consume an unbounded cache. If startup
    or recovery has not seeded the bounded suffix, fail closed and let the
    dedicated recovery owner restore the contract.
    """
    key = (str(kind), asset_key(asset))
    record = FRAME_CACHE.get(key)
    resolved = str(Path(path).resolve())
    signature = path_signature(Path(path))
    if (
        not record
        or record.get("path") != resolved
        or record.get("signature") != signature
    ):
        raise RuntimeError(
            f"{asset_key(asset)}: {kind}_frame_cache_not_initialized_or_stale; "
            "startup/recovery hydration is required"
        )
    if _coerce_frame_limit(record.get("max_rows")) is None:
        raise RuntimeError(
            f"{asset_key(asset)}: {kind}_frame_cache_unbounded; "
            "Phase B startup/recovery bounded hydration is required"
        )
    frame = record.get("frame")
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeError(
            f"{asset_key(asset)}: {kind}_frame_cache_frame_invalid"
        )
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise RuntimeError(
            f"{asset_key(asset)}: {kind}_frame_cache_missing_columns={missing}"
        )
    return frame[columns].copy(deep=False)

def hmm_input_ring_set(
    asset: str,
    frame: pd.DataFrame,
    *,
    capacity: int,
) -> pd.DataFrame:
    """Seed the bounded engineered HMM input/eligibility ring.

    PHASE_B_HMM_INPUT_RING_V1

    The seed may come only from startup/recovery authority. Normal closed-bar
    updates extend this ring solely from the exact newly committed FFD delta.
    """
    key = asset_key(asset)
    limit = max(int(capacity), 1)
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{key}: HMM input ring seed must be a DataFrame")
    work = frame.copy()
    if "Datetime" not in work.columns:
        raise RuntimeError(f"{key}: HMM input ring is missing Datetime")
    work["Datetime"] = parse_utc_series(work["Datetime"])
    work = (
        work.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .tail(limit)
        .reset_index(drop=True)
    )
    with ASSET_GUARD:
        HMM_INPUT_RING[key] = {
            "capacity": limit,
            "frame": work,
        }
    return work.copy(deep=False)


def hmm_input_ring_extend(
    asset: str,
    rows: pd.DataFrame,
) -> pd.DataFrame:
    """Extend the warm HMM input ring without reading persistent authority."""
    key = asset_key(asset)
    with ASSET_GUARD:
        record = HMM_INPUT_RING.get(key)
        if not isinstance(record, dict):
            raise RuntimeError(
                f"{key}: hmm_input_ring_not_initialized; "
                "startup/recovery hydration is required"
            )
        current = record.get("frame")
        capacity = max(int(record.get("capacity", 0) or 0), 1)
        if not isinstance(current, pd.DataFrame):
            raise RuntimeError(f"{key}: HMM input ring frame is invalid")
        if rows is None or not isinstance(rows, pd.DataFrame) or rows.empty:
            return current.copy(deep=False)
        incoming = rows.copy()
        if "Datetime" not in incoming.columns:
            raise RuntimeError(
                f"{key}: HMM input ring extension is missing Datetime"
            )
        incoming["Datetime"] = parse_utc_series(incoming["Datetime"])
        incoming = incoming.dropna(subset=["Datetime"])
        output = pd.concat([current, incoming], ignore_index=True, sort=False)
        output["Datetime"] = parse_utc_series(output["Datetime"])
        output = (
            output.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .drop_duplicates("Datetime", keep="last")
            .tail(capacity)
            .reset_index(drop=True)
        )
        HMM_INPUT_RING[key] = {
            "capacity": capacity,
            "frame": output,
        }
        return output.copy(deep=False)


def hmm_input_ring_get(asset: str) -> pd.DataFrame | None:
    key = asset_key(asset)
    with ASSET_GUARD:
        record = HMM_INPUT_RING.get(key)
        if not isinstance(record, dict):
            return None
        frame = record.get("frame")
        return (
            frame.copy(deep=False)
            if isinstance(frame, pd.DataFrame)
            else None
        )


def hmm_input_ring_status() -> dict[str, dict[str, Any]]:
    with ASSET_GUARD:
        output: dict[str, dict[str, Any]] = {}
        for asset, record in HMM_INPUT_RING.items():
            frame = record.get("frame")
            latest = None
            rows = 0
            if isinstance(frame, pd.DataFrame):
                rows = int(len(frame))
                if rows and "Datetime" in frame.columns:
                    parsed = parse_utc_series(frame["Datetime"]).dropna()
                    if not parsed.empty:
                        latest = utc_timestamp(parsed.max()).isoformat()
            output[asset] = {
                "rows": rows,
                "capacity": int(record.get("capacity", 0) or 0),
                "latest": latest,
            }
        return output

def invalidate(asset: str, *, ffd: bool, hmm: bool) -> None:
    key = asset_key(asset)
    if ffd or hmm:
        clear_dependency_generation(key)
        with ASSET_GUARD:
            HMM_INPUT_RING.pop(key, None)
    if ffd:
        FRAME_CACHE.pop(("ffd", key), None)
        FFD_CACHE.pop(key, None)
        FFD_DELTA[key] = pd.DataFrame()
    if hmm:
        FRAME_CACHE.pop(("hmm", key), None)
        HMM_CACHE.pop(key, None)
        HMM_CONTEXT.pop(key, None)


def cache_status() -> dict[str, Any]:
    with ACTIVE_FEATURE_SNAPSHOTS_LOCK:
        active_generations = {
            asset: str(record.get("generation_id") or "")
            for asset, record in ACTIVE_FEATURE_SNAPSHOTS.items()
        }
    with DEPENDENCY_GENERATION_LOCK:
        ready_dependency_generations = {
            asset: str(record.get("generation_id") or "")
            for asset, record in READY_DEPENDENCY_GENERATIONS.items()
        }
    return {
        "version": HOTPATH_VERSION,
        "installed": INSTALLED,
        "ffd_assets": sorted(FFD_CACHE),
        "hmm_assets": sorted(HMM_CACHE),
        "frame_cache_entries": sorted(f"{kind}:{asset}" for kind, asset in FRAME_CACHE),
        "frame_cache": frame_cache_status(),
        "hmm_input_ring": hmm_input_ring_status(),
        "bound_feature_generation_id": (
            (getattr(FEATURE_SNAPSHOT_CONTEXT, "snapshot", None) or {}).get("generation_id")
        ),
        "active_feature_generations": active_generations,
        "ready_dependency_generations": ready_dependency_generations,
        "live_bar_authority_reader_installed": bool(
            callable(LIVE_BAR_AUTHORITY_READER)
        ),
    }
