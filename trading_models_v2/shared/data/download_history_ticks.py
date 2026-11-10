"""Download OLDER Dukascopy research ticks into a staging folder (history extension, owner 2026-11-05).

Owner 2026-11-05: extend history (bars only in the end); "start them at 2015" for US500 / JPN; "go ahead with gold
first (but dont delete the tick files first)". This script only DOWNLOADS: it never touches assets/<ASSET>/raw/ticks,
curated ticks, bars or any edge. Files go to assets/<ASSET>/raw_history_staging/ticks with the raw naming and format
(same fetch as shared/data/download_holdout_ticks.py: bid side query, bid/ask prices, volumes x 1e6), in weekly
half-open chunks ending exactly where the existing raw ticks begin. Resumable: existing chunk files are skipped.

usage: python shared/data/download_history_ticks.py --asset XAUUSD --start 2005-01-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import functools

import dukascopy_python
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_holdout_ticks import (  # noqa: E402
    CHUNK_DAYS, INSTRUMENTS, fetch_ticks, raw_tick_files, set_below_normal_priority)

# 2026-11-06: dukascopy_python calls requests.get WITHOUT a timeout; a silently dead connection hung all three downloads
# for ~24 h (no exception, so no retry). Every request now times out (30 s connect, 120 s read) and raises, which the
# library's own retries and the backoff below handle. Content of a successful response is unchanged.
dukascopy_python.requests.get = functools.partial(requests.get, timeout=(30, 120))


def first_raw_chunk_start(asset: str) -> datetime:
    name = raw_tick_files(asset)[0].stem                 # <asset>_ticks_YYYY-MM-DD_to_YYYY-MM-DD
    return datetime.fromisoformat(name.split("_ticks_")[1].split("_to_")[0]).replace(tzinfo=timezone.utc)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


BACKOFF_SECONDS = (30, 60, 120, 240, 480)


class ChunkFetchError(RuntimeError):
    pass


def _with_backoff(asset: str, start: datetime, end: datetime, what: str) -> pd.DataFrame:
    last: Exception | None = None
    for attempt, wait in enumerate((0, *BACKOFF_SECONDS)):
        if wait:
            time.sleep(wait)
        try:
            return fetch_ticks(asset, start, end)
        except Exception as exc:  # noqa: BLE001 -- dukascopy_python raises KeyError / TypeError when throttled
            last = exc
            print(f"{asset}: {what} {start:%Y-%m-%d} attempt {attempt + 1} failed ({type(exc).__name__})", flush=True)
    raise ChunkFetchError(f"{asset}: {what} {start:%Y-%m-%d} failed after {len(BACKOFF_SECONDS) + 1} attempts: {last!r}")


def fetch_chunk(asset: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Weekly fetch with backoff; then day by day with backoff. NEVER saves a failed day as empty (2026-11-05: under
    parallel load Dukascopy throttling made the library raise, and saving those days empty created holes). If any day
    still fails, ChunkFetchError is raised and no file is written for the week; a rerun resumes there."""
    try:
        return _with_backoff(asset, start, end, "week")
    except ChunkFetchError:
        pass
    parts = []
    day = start
    while day < end:
        nxt = min(day + timedelta(days=1), end)
        parts.append(_with_backoff(asset, day, nxt, "day"))
        day = nxt
    parts = [x for x in parts if len(x)]
    if not parts:
        return pd.DataFrame(columns=["bidPrice", "askPrice", "bidVolume", "askVolume"],
                            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    df = pd.concat(parts)
    return df[~df.index.duplicated(keep="first")].sort_index()


def run(asset: str, start: datetime) -> dict:
    end = first_raw_chunk_start(asset)
    out_dir = ROOT / "assets" / asset / "raw_history_staging" / "ticks"
    out_dir.mkdir(parents=True, exist_ok=True)
    current = start
    total = 0
    while current < end:
        chunk_end = min(current + timedelta(days=CHUNK_DAYS), end)
        name = f"{asset.lower()}_ticks_{current:%Y-%m-%d}_to_{chunk_end:%Y-%m-%d}.csv"
        path = out_dir / name
        if not path.exists():
            df = fetch_chunk(asset, current, chunk_end)
            df = df[(df.index >= pd.Timestamp(current)) & (df.index < pd.Timestamp(chunk_end))]   # half-open
            tmp = path.with_suffix(".part")
            df.to_csv(tmp, index=True)
            os.replace(tmp, path)
            total += len(df)
            print(f"{asset}: {len(df):,} ticks -> {name}", flush=True)
        current = chunk_end
    files = sorted(out_dir.glob("*.csv"))
    manifest = {
        "asset": asset, "instrument": INSTRUMENTS[asset],
        "purpose": "history extension staging (owner 2026-11-05); not yet research data",
        "start_utc": start.isoformat(), "end_exclusive_utc": end.isoformat(),
        "files": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in files],
        "total_bytes": int(sum(p.stat().st_size for p in files)),
        "written_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir.parent / "history_staging_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", required=True, choices=sorted(INSTRUMENTS))
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    a = ap.parse_args()
    set_below_normal_priority()
    m = run(a.asset, datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc))
    print(json.dumps({k: m[k] for k in ("asset", "start_utc", "end_exclusive_utc", "total_bytes")}, indent=1))
    print(f"files: {len(m['files'])}")


if __name__ == "__main__":
    main()
