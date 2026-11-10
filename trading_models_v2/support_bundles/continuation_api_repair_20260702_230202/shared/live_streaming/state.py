"""Shared state and IO helpers for the V2 live streaming hot path."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

import pandas as pd

HOTPATH_VERSION = "v2_live_streaming_hotpath_v1"
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


def copy_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(result))


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


def normalized_live_bars(asset: str) -> pd.DataFrame:
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
    return FFD_MODULE._normalize_bar_table(raw, source="portfolio_live_bars_hotpath")


def frame_set(kind: str, asset: str, path: Path, frame: pd.DataFrame) -> None:
    FRAME_CACHE[(kind, asset_key(asset))] = {
        "path": str(path.resolve()),
        "signature": path_signature(path),
        "frame": frame,
    }


def frame_extend(kind: str, asset: str, path: Path, rows: pd.DataFrame) -> None:
    if rows.empty:
        return
    key = (kind, asset_key(asset))
    record = FRAME_CACHE.get(key)
    if not record or record.get("path") != str(path.resolve()):
        return
    current = record.get("frame")
    if not isinstance(current, pd.DataFrame) or any(
        column not in rows.columns for column in current.columns
    ):
        FRAME_CACHE.pop(key, None)
        return
    output = pd.concat([current, rows[list(current.columns)]], ignore_index=True)
    output["Datetime"] = pd.to_datetime(output["Datetime"], utc=True, errors="coerce")
    output = output.dropna(subset=["Datetime"]).sort_values(
        "Datetime", kind="mergesort"
    )
    output = output.drop_duplicates("Datetime", keep="first").reset_index(drop=True)
    frame_set(kind, asset, path, output)


def frame_load(kind: str, asset: str, path: Path, columns: list[str]) -> pd.DataFrame:
    key = (kind, asset_key(asset))
    record = FRAME_CACHE.get(key)
    if (
        record
        and record.get("path") == str(path.resolve())
        and record.get("signature") == path_signature(path)
    ):
        frame = record.get("frame")
        if isinstance(frame, pd.DataFrame) and all(
            column in frame.columns for column in columns
        ):
            return frame[columns].copy(deep=False)
    frame = pd.read_csv(path, usecols=columns)
    frame["Datetime"] = pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    for column in columns:
        if column != "Datetime":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=columns).sort_values("Datetime", kind="mergesort")
    frame = frame.drop_duplicates("Datetime", keep="first").reset_index(drop=True)
    if frame.empty:
        raise RuntimeError(f"{kind} verified frame is empty: {path}")
    frame_set(kind, asset, path, frame)
    return frame[columns].copy(deep=False)


def invalidate(asset: str, *, ffd: bool, hmm: bool) -> None:
    key = asset_key(asset)
    if ffd:
        FRAME_CACHE.pop(("ffd", key), None)
        FFD_CACHE.pop(key, None)
        FFD_DELTA[key] = pd.DataFrame()
    if hmm:
        FRAME_CACHE.pop(("hmm", key), None)
        HMM_CACHE.pop(key, None)


def cache_status() -> dict[str, Any]:
    return {
        "version": HOTPATH_VERSION,
        "installed": INSTALLED,
        "ffd_assets": sorted(FFD_CACHE),
        "hmm_assets": sorted(HMM_CACHE),
        "frame_cache_entries": sorted(f"{kind}:{asset}" for kind, asset in FRAME_CACHE),
    }
