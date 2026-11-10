"""Scheduled US macro release calendar (FOMC, CPI, NFP) -- timing only, never the released values.

One file, config/economic_event_calendar.json, read identically by research and live: edge runtimes compute any
calendar feature through this module inside runtime.build_features(), which the live bot already calls, so live needs
no separate feed. Owner approval 2026-11-05 ("yes start stage 1").

Point-in-time rules:
- Event times are public in advance (Fed and BLS publish a year ahead), so "time to the next release" is known at
  every bar. Exception: the 2025 shutdown-era releases (Sept 2025 CPI, Sept and Nov 2025 Employment Situation) were
  rescheduled at short notice; features looking more than ~2 weeks ahead around them are not strictly point-in-time
  (see the calendar's known_exceptions).
- Fail closed: any timestamp after a type's coverage_through_utc raises CalendarCoverageError, so a stale calendar can
  never feed a live decision silently.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

CALENDAR_PATH = Path(__file__).resolve().parents[2] / "config" / "economic_event_calendar.json"
EVENT_TYPES = ("FOMC", "CPI", "NFP")


class CalendarCoverageError(RuntimeError):
    pass


@lru_cache(maxsize=4)
def _load(path_text: str) -> tuple[dict, pd.DataFrame]:
    doc = json.loads(Path(path_text).read_text(encoding="utf-8"))
    ev = pd.DataFrame(doc["events"])
    ev["t_ns"] = to_datetime_iso(ev["release_utc"], utc=True).astype("datetime64[ns, UTC]").astype("int64")
    ev = ev.sort_values("t_ns").reset_index(drop=True)
    return doc, ev


def load_calendar(path: str | Path | None = None) -> tuple[dict, pd.DataFrame]:
    return _load(str(path or CALENDAR_PATH))


def event_times_ns(kind: str, path: str | Path | None = None) -> np.ndarray:
    _, ev = load_calendar(path)
    if kind not in EVENT_TYPES:
        raise ValueError(f"unknown event type {kind!r}")
    return ev.loc[ev["type"] == kind, "t_ns"].to_numpy(dtype="int64")


def coverage_ns(kind: str, path: str | Path | None = None) -> int:
    doc, _ = load_calendar(path)
    return int(pd.Timestamp(doc["coverage_through_utc"][kind]).value)


def assert_covered(ts_ns: np.ndarray, kinds: tuple[str, ...] = EVENT_TYPES, path: str | Path | None = None) -> None:
    ts_ns = np.asarray(ts_ns, dtype="int64")
    if ts_ns.size == 0:
        return
    latest = int(ts_ns.max())
    for kind in kinds:
        if latest > coverage_ns(kind, path):
            raise CalendarCoverageError(
                f"economic calendar coverage for {kind} ends {pd.Timestamp(coverage_ns(kind, path), tz='UTC')}; "
                f"timestamp {pd.Timestamp(latest, tz='UTC')} is past it -- update config/economic_event_calendar.json")


def hours_to_next(ts_ns: np.ndarray, kind: str, path: str | Path | None = None) -> np.ndarray:
    """Hours from each timestamp to the next scheduled release of `kind` (strictly after; NaN if none)."""
    ts_ns = np.asarray(ts_ns, dtype="int64")
    assert_covered(ts_ns, (kind,), path)
    ev = event_times_ns(kind, path)
    j = np.searchsorted(ev, ts_ns, side="right")
    out = np.full(len(ts_ns), np.nan)
    ok = j < len(ev)
    out[ok] = (ev[j[ok]] - ts_ns[ok]) / 3.6e12
    return out


def hours_since_last(ts_ns: np.ndarray, kind: str, path: str | Path | None = None) -> np.ndarray:
    """Hours since the latest release of `kind` at or before each timestamp (NaN if none)."""
    ts_ns = np.asarray(ts_ns, dtype="int64")
    assert_covered(ts_ns, (kind,), path)
    ev = event_times_ns(kind, path)
    j = np.searchsorted(ev, ts_ns, side="right") - 1
    out = np.full(len(ts_ns), np.nan)
    ok = j >= 0
    out[ok] = (ts_ns[ok] - ev[j[ok]]) / 3.6e12
    return out
