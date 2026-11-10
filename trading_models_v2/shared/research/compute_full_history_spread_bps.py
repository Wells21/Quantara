"""
Full-history (not sampled) empirical bid/ask spread distribution per asset,
computed directly from every Dukascopy weekly tick CSV under
assets/<ASSET>/raw/ticks/ -- supersedes the 9-of-~230-file sample in
config/us500_edge_search_ledger.json's per_asset_empirical_spread_bps_2026-09-14
entry, which that entry itself flagged as "treat as directional, re-verify
with a fuller sample before hard-coding into production Gate B calls."

Built 2026-09-24 as part of moving RESEARCH_COST_BPS_BY_ASSET from a p95-p99
tail assumption to the Dukascopy MEDIAN (see docs/mldp/ discussion): the
tail-risk role that p95-p99 was playing is now covered by the live
spread_parity_guard's own multiplier on top of whatever base number is
chosen here, so the base number itself should be the honest center of the
distribution, not pre-padded -- and it needs to be a real, full-sample
number since it is about to become a permanent live_settings.py constant.

Filters spread_bps = (ask-bid)/mid*10000, using the exact same tick validity
filter as v3_horizon_lab._xef_loader (bid>0, ask>0, ask>=bid, both parse as
numbers) so this number is computed the same way the real Gate C replay
would see the same ticks.

Usage:
    python -m shared.research.compute_full_history_spread_bps [--assets DAX,US500,...]

Writes shared/research/outputs/full_history_spread_bps_<UTCSTAMP>.json,
one entry per asset, updated incrementally after each asset finishes so a
partial run is never lost.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

from shared.assets.asset_layout import get_asset_layout  # noqa: E402

DEFAULT_ASSETS = ["DAX", "NASDAQ", "XAUUSD", "US500", "JPN", "GBPJPY", "HK"]
OUTPUT_DIR = _THIS_FILE.parent / "outputs"


def compute_asset_spread_bps(asset: str) -> dict:
    layout = get_asset_layout(asset)
    files = sorted(layout.tick_root.glob("*.csv"))
    if not files:
        return {"asset": asset, "error": f"no tick files under {layout.tick_root}"}

    parts: list[np.ndarray] = []
    total_ticks = 0
    total_valid = 0
    t0 = time.time()
    for i, f in enumerate(files):
        df = pd.read_csv(f, usecols=["bidPrice", "askPrice"])
        bid = pd.to_numeric(df["bidPrice"], errors="coerce")
        ask = pd.to_numeric(df["askPrice"], errors="coerce")
        good = bid.notna() & ask.notna() & (bid > 0) & (ask > 0) & (ask >= bid)
        total_ticks += len(df)
        total_valid += int(good.sum())
        b = bid[good].to_numpy(dtype="float64")
        a = ask[good].to_numpy(dtype="float64")
        mid = (a + b) / 2.0
        spread_bps = (a - b) / mid * 10000.0
        parts.append(spread_bps.astype("float32"))
        if (i + 1) % 50 == 0 or i == len(files) - 1:
            elapsed = time.time() - t0
            print(f"  [{asset}] {i+1}/{len(files)} files, {total_valid:,} valid ticks so far, {elapsed:.0f}s elapsed", flush=True)

    all_spread = np.concatenate(parts) if parts else np.array([], dtype="float32")
    if all_spread.size == 0:
        return {"asset": asset, "error": "zero valid ticks across all files"}

    result = {
        "asset": asset,
        "n_files": len(files),
        "total_ticks_seen": total_ticks,
        "total_valid_ticks": total_valid,
        "pct_valid": round(100.0 * total_valid / max(total_ticks, 1), 3),
        "median_bps": float(np.median(all_spread)),
        "mean_bps": float(np.mean(all_spread)),
        "p90_bps": float(np.percentile(all_spread, 90)),
        "p95_bps": float(np.percentile(all_spread, 95)),
        "p99_bps": float(np.percentile(all_spread, 99)),
        "max_bps": float(np.max(all_spread)),
        "elapsed_seconds": round(time.time() - t0, 1),
        "method": "full_history_pooled_ticks_no_sampling",
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=str, default=",".join(DEFAULT_ASSETS))
    args = parser.parse_args()
    assets = [a.strip() for a in args.assets.split(",") if a.strip()]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUTPUT_DIR / f"full_history_spread_bps_{stamp}.json"

    results: dict[str, dict] = {}
    for asset in assets:
        print(f"=== {asset} ===", flush=True)
        results[asset] = compute_asset_spread_bps(asset)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        r = results[asset]
        if "error" not in r:
            print(f"  DONE {asset}: median={r['median_bps']:.3f} p95={r['p95_bps']:.3f} p99={r['p99_bps']:.3f} (n={r['total_valid_ticks']:,} ticks, {r['elapsed_seconds']}s)", flush=True)
        else:
            print(f"  ERROR {asset}: {r['error']}", flush=True)

    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
