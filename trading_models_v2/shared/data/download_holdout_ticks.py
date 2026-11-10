"""Holdout (embargo) tick download for the 10 Quantara assets.

AFML Ch.1 s.1.3.1.6 (p.8): stage 1 of the cursus honorum runs a frozen strategy
on data observed after the end of its backtest. This script fetches that data.

The fetch is the one in trading_models_v2/auto_downloader.py, unchanged:
dukascopy_python.fetch(INTERVAL_TICK, OFFER_SIDE_BID, max_retries=10), volumes
x 1e6, CSV columns timestamp,bidPrice,askPrice,bidVolume,askVolume. Two things
differ: datetimes are timezone-aware UTC (the library reads naive datetimes as
local time), and the instrument comes from INSTRUMENTS below.

Modes
-----
--parity    Re-fetch the time span of the newest full-week file in
            assets/{ASSET}/raw/ticks/ and compare it tick-for-tick with that
            file. Proves the instrument and processing match the research data.
            Report: assets/{ASSET}/holdout/parity_check.json
--download  Fetch from the last raw tick (exclusive) to --end into
            assets/{ASSET}/holdout/ticks/ (never raw/ticks/, which research
            reads). Resumable: finished chunk files are skipped.

Run from trading_models_v2:
    python ./shared/data/download_holdout_ticks.py --parity --assets XAUUSD GBPJPY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import dukascopy_python
from dukascopy_python import instruments

ROOT = Path(__file__).resolve().parents[2]

INSTRUMENTS = {
    "XAUUSD": instruments.INSTRUMENT_FX_METALS_XAU_USD,
    "GBPJPY": instruments.INSTRUMENT_FX_CROSSES_GBP_JPY,
    "USDMXN": instruments.INSTRUMENT_FX_CROSSES_USD_MXN,
    "BTCUSD": instruments.INSTRUMENT_VCCY_BTC_USD,
    "DAX": instruments.INSTRUMENT_IDX_EUROPE_E_DAAX,
    "JPN": instruments.INSTRUMENT_IDX_ASIA_E_N225JAP,
    "HK": instruments.INSTRUMENT_IDX_ASIA_E_H_KONG,
    "US500": instruments.INSTRUMENT_IDX_AMERICA_E_SANDP_500,
    "NASDAQ": instruments.INSTRUMENT_IDX_AMERICA_E_NQ_100,
    "USOIL": instruments.INSTRUMENT_CMD_ENERGY_E_LIGHT,
}
COLUMNS = ["bidPrice", "askPrice", "bidVolume", "askVolume"]
CHUNK_DAYS = 7


def set_below_normal_priority() -> None:
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception as exc:  # research must never outrank the live bot
        print(f"warning: could not lower priority: {exc}", file=sys.stderr)


def fetch_ticks(asset: str, start: datetime, end: datetime) -> pd.DataFrame:
    df = dukascopy_python.fetch(
        instrument=INSTRUMENTS[asset],
        interval=dukascopy_python.INTERVAL_TICK,
        offer_side=dukascopy_python.OFFER_SIDE_BID,
        start=start,
        end=end,
        max_retries=10,
        debug=False,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="timestamp"))
    df.index.name = "timestamp"
    df["bidVolume"] = df["bidVolume"] * 1000000
    df["askVolume"] = df["askVolume"] * 1000000
    return df[COLUMNS]


def raw_tick_files(asset: str) -> list[Path]:
    files = sorted((ROOT / "assets" / asset / "raw" / "ticks").glob("*_ticks_*_to_*.csv"))
    if not files:
        raise SystemExit(f"{asset}: no raw tick files")
    return files


def file_span_days(path: Path) -> int:
    start, end = path.stem.split("_ticks_")[1].split("_to_")
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days


def read_tick_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    return df.set_index("timestamp")[COLUMNS]


def run_parity(asset: str) -> dict:
    files = raw_tick_files(asset)
    ref_path = next(p for p in reversed(files) if file_span_days(p) == CHUNK_DAYS)
    ref = read_tick_csv(ref_path)
    first, last = ref.index.min(), ref.index.max()
    new = fetch_ticks(asset, first.to_pydatetime(), last.to_pydatetime())
    new = new[(new.index >= first) & (new.index <= last)]

    report: dict = {
        "asset": asset,
        "instrument": INSTRUMENTS[asset],
        "reference_file": str(ref_path.relative_to(ROOT)).replace("\\", "/"),
        "span_utc": [str(first), str(last)],
        "ticks_reference": int(len(ref)),
        "ticks_refetched": int(len(new)),
        "checked_utc": datetime.now(timezone.utc).isoformat(),
    }
    same_len = len(ref) == len(new)
    report["same_tick_count"] = same_len
    if same_len:
        report["same_timestamps"] = bool((ref.index == new.index).all())
        for col in COLUMNS:
            a, b = ref[col].to_numpy(float), new[col].to_numpy(float)
            report[f"{col}_max_abs_diff"] = float(np.nanmax(np.abs(a - b))) if len(a) else 0.0
    else:
        joined = ref.join(new, how="inner", lsuffix="_ref", rsuffix="_new")
        report["timestamps_in_both"] = int(len(joined))
        for col in ("bidPrice", "askPrice"):
            d = (joined[f"{col}_ref"] - joined[f"{col}_new"]).abs()
            report[f"{col}_max_abs_diff_on_common"] = float(d.max()) if len(d) else None
    prices_identical = (
        same_len
        and report.get("same_timestamps", False)
        and report["bidPrice_max_abs_diff"] == 0.0
        and report["askPrice_max_abs_diff"] == 0.0
    )
    report["verdict"] = "PASS" if prices_identical else "FAIL"
    if prices_identical:
        # x1e6 then CSV round-trip leaves ~1e-9 float noise on volumes; anything larger is a real difference
        volumes_identical = all(
            np.allclose(ref[c].to_numpy(float), new[c].to_numpy(float), rtol=1e-12, atol=1e-6)
            for c in ("bidVolume", "askVolume")
        )
        report["volumes_identical_within_float_rounding"] = bool(volumes_identical)
        if not volumes_identical:
            report["note"] = "prices/timestamps identical; volumes differ (bars use tick counts, not volume)"
    out = ROOT / "assets" / asset / "holdout" / "parity_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def last_raw_tick(asset: str) -> pd.Timestamp:
    tail = pd.read_csv(raw_tick_files(asset)[-1], usecols=["timestamp"])["timestamp"].iloc[-1]
    return pd.Timestamp(tail).tz_convert("UTC")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run_download(asset: str, end: datetime) -> dict:
    parity = ROOT / "assets" / asset / "holdout" / "parity_check.json"
    if not parity.exists() or json.loads(parity.read_text(encoding="utf-8")).get("verdict") != "PASS":
        raise SystemExit(f"{asset}: parity check has not passed; refusing to download")
    out_dir = ROOT / "assets" / asset / "holdout" / "ticks"
    out_dir.mkdir(parents=True, exist_ok=True)
    start_after = last_raw_tick(asset)
    current = start_after.to_pydatetime()
    while current < end:
        chunk_end = min(current + timedelta(days=CHUNK_DAYS), end)
        # Same naming as raw/ticks and auto_downloader.py: every edge's model_rows.build_tick_file_catalog
        # parses the date range from this name (a time part makes some edges skip the file).
        name = f"{asset.lower()}_ticks_{current:%Y-%m-%d}_to_{chunk_end:%Y-%m-%d}.csv"
        path = out_dir / name
        if not path.exists():
            df = fetch_ticks(asset, current, chunk_end)
            df = df[(df.index > start_after) & (df.index < chunk_end)]  # half-open chunks: no boundary duplicates
            tmp = path.with_suffix(".part")
            df.to_csv(tmp, index=True)
            os.replace(tmp, path)
            print(f"{asset}: {len(df):,} ticks -> {name}", flush=True)
        current = chunk_end
        start_after = pd.Timestamp(chunk_end) - pd.Timedelta(microseconds=1)
    files = sorted(out_dir.glob("*.csv"))
    manifest = {
        "asset": asset,
        "instrument": INSTRUMENTS[asset],
        "purpose": "sealed holdout (AFML Ch.1 p.8 embargo stage); never read by research",
        "starts_after_raw_tick_utc": str(last_raw_tick(asset)),
        "end_exclusive_utc": end.isoformat(),
        "files": [{"name": p.name, "sha256": sha256(p)} for p in files],
        "written_utc": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir.parent / "holdout_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parity", action="store_true")
    mode.add_argument("--download", action="store_true")
    parser.add_argument("--assets", nargs="+", default=list(INSTRUMENTS))
    parser.add_argument("--end", default="2026-10-23T00:00:00+00:00", help="holdout end, exclusive, UTC")
    args = parser.parse_args()
    set_below_normal_priority()
    end = datetime.fromisoformat(args.end).astimezone(timezone.utc)
    for asset in args.assets:
        if asset not in INSTRUMENTS:
            raise SystemExit(f"unknown asset {asset}")
        if args.parity:
            r = run_parity(asset)
            print(f"{asset}: {r['verdict']} ref={r['ticks_reference']:,} new={r['ticks_refetched']:,} {r.get('note', '')}", flush=True)
        else:
            m = run_download(asset, end)
            print(f"{asset}: holdout manifest written, {len(m['files'])} files", flush=True)


if __name__ == "__main__":
    main()
