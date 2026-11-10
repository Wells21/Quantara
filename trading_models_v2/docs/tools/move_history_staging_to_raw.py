"""Move validated history-extension ticks from raw_history_staging/ticks into raw/ticks (owner approval 2026-11-07).

Owner 2026-11-07 "approved": keep all raw ticks (the disk fits), move them into raw/ticks (no code changes), research
starts in 2014 for NASDAQ / DAX / USOIL (2013 is patchy at Dukascopy). Leading weeks with no ticks (GBPJPY / USDMXN
January 2012: Dukascopy has no data) are not moved. Files not moved stay in staging. Same volume -> os.replace is a
rename (no copy). Never overwrites a raw file. Writes assets/<ASSET>/raw/history_extension_move.json.

usage: python docs/tools/move_history_staging_to_raw.py --asset NASDAQ [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUTOFF = {"NASDAQ": "2014-01-01", "DAX": "2014-01-01", "USOIL": "2020-03-01"}   # owner 2026-11-07 (USOIL: roll
# detection misses the 2014-07..2020-02 roll signature; owner chose a March 2020 start; 2014-2020 files went back to staging)


def start_of(name: str) -> str:
    return name.split("_ticks_")[1][:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    stage = ROOT / "assets" / a.asset / "raw_history_staging" / "ticks"
    raw = ROOT / "assets" / a.asset / "raw" / "ticks"
    files = sorted(stage.glob("*_ticks_*_to_*.csv"))
    cutoff = CUTOFF.get(a.asset)
    if cutoff:
        files = [f for f in files if start_of(f.name) >= cutoff]
    while files and sum(1 for _ in files[0].open("r", encoding="utf-8")) <= 1:   # leading empty weeks
        files = files[1:]
    if not files:
        raise SystemExit(f"{a.asset}: nothing to move")
    first_raw = sorted(raw.glob("*_ticks_*_to_*.csv"))[0].name
    last_end = files[-1].name.split("_to_")[1][:10]
    if last_end != start_of(first_raw):
        raise SystemExit(f"{a.asset}: staged chain ends {last_end}, raw starts {start_of(first_raw)}")
    clash = [f.name for f in files if (raw / f.name).exists()]
    if clash:
        raise SystemExit(f"{a.asset}: {len(clash)} files already in raw/ticks, e.g. {clash[0]}")
    moved = []
    for f in files:
        moved.append({"name": f.name, "bytes": f.stat().st_size})
        if not a.dry_run:
            os.replace(f, raw / f.name)
    rec = {"asset": a.asset, "moved_utc": datetime.now(timezone.utc).isoformat(), "cutoff": cutoff,
           "first_moved": files[0].name, "last_moved": files[-1].name, "n_files": len(moved),
           "bytes": sum(m["bytes"] for m in moved), "left_in_staging": len(list(stage.glob("*.csv"))) if not a.dry_run else None,
           "validation": f"logs/history_staging_validation_20261107/{a.asset}.json", "files": moved}
    if not a.dry_run:
        (raw.parent / "history_extension_move.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in rec.items() if k != "files"}), flush=True)


if __name__ == "__main__":
    main()
