"""Standalone Colab timing probe for AFML Ch.4 SS4.5 sequential bootstrap.

Run this on Colab BEFORE running 02_train_meta_model_optuna.py's full
N_ESTIMATORS_BAGGING loop, to get a real Colab-hardware timing number
instead of trusting a benchmark measured on a different machine. Takes
under a minute; reports projected cost for a full n_estimators run.

Flat Colab usage
-----------------
Upload alongside the edge's usual Step02/03 inputs:
    <edge_stem>_mldp_execution_resolution.csv
    <asset>_standard_quote_tick_bars.csv

Then run:
    !python /content/sequential_bootstrap_timing_probe.py <edge_stem> <asset>

Example:
    !python /content/sequential_bootstrap_timing_probe.py us500_volatilitycarry_long US500
"""

from __future__ import annotations

import sys
import time
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

CWD = Path(".")


def _sequential_bootstrap_one_draw_timing(start_pos: np.ndarray, end_pos: np.ndarray, n_bars: int) -> float:
    """Times exactly one full sequential-bootstrap draw (sample_length ==
    n_events), using the same sqrt-decomposition algorithm inlined into
    02_train_meta_model_optuna.py / 03_generate_oof_predictions.py -- not a
    simplified stand-in, so this timing is representative of the real cost."""
    n_events = len(start_pos)
    block_size = max(1, int(np.sqrt(n_bars)))
    n_blocks = (n_bars + block_size - 1) // block_size
    concurrency = np.zeros(n_bars, dtype="float64")
    inv = np.ones(n_bars, dtype="float64")
    local_prefix = np.empty(n_bars, dtype="float64")
    block_sum = np.empty(n_blocks, dtype="float64")
    block_start = np.arange(n_blocks, dtype="int64") * block_size
    block_end = np.minimum(block_start + block_size, n_bars)
    for b in range(n_blocks):
        lo, hi = block_start[b], block_end[b]
        local_prefix[lo:hi] = np.cumsum(inv[lo:hi])
        block_sum[b] = local_prefix[hi - 1]
    block_prefix = np.concatenate(([0.0], np.cumsum(block_sum)))

    event_block_end = end_pos // block_size
    start_minus_1 = start_pos - 1
    has_prefix = start_minus_1 >= 0
    safe_start_minus_1 = np.where(has_prefix, start_minus_1, 0)
    event_block_start_minus_1 = safe_start_minus_1 // block_size
    lengths = (end_pos - start_pos + 1).astype("float64")
    rng = np.random.default_rng(0)

    def _full_prefix(pos, blocks):
        return block_prefix[blocks] + local_prefix[pos]

    t0 = time.time()
    for _ in range(n_events):
        end_sum = _full_prefix(end_pos, event_block_end)
        start_sum = np.where(has_prefix, _full_prefix(safe_start_minus_1, event_block_start_minus_1), 0.0)
        avg_u = (end_sum - start_sum) / lengths
        total = float(avg_u.sum())
        cs = np.cumsum(avg_u)
        r = rng.random() * total
        choice = int(np.searchsorted(cs, r, side="right"))
        choice = min(choice, n_events - 1)
        lo, hi = int(start_pos[choice]), int(end_pos[choice]) + 1
        concurrency[lo:hi] += 1.0
        np.add(concurrency[lo:hi], 1.0, out=inv[lo:hi])
        np.reciprocal(inv[lo:hi], out=inv[lo:hi])
        b_lo, b_hi = lo // block_size, (hi - 1) // block_size
        for b in range(b_lo, b_hi + 1):
            blo, bhi = int(block_start[b]), int(block_end[b])
            np.cumsum(inv[blo:bhi], out=local_prefix[blo:bhi])
            block_sum[b] = local_prefix[bhi - 1]
        block_prefix[1:] = np.cumsum(block_sum)
    return time.time() - t0


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python sequential_bootstrap_timing_probe.py <edge_stem> <asset>")
    edge_stem, asset = sys.argv[1], sys.argv[2]

    resolution_path = CWD / f"{edge_stem}_mldp_execution_resolution.csv"
    bars_path = CWD / f"{asset.lower()}_standard_quote_tick_bars.csv"
    if not resolution_path.exists():
        raise SystemExit(f"Missing {resolution_path}. Upload it to /content first.")
    if not bars_path.exists():
        raise SystemExit(f"Missing {bars_path}. Upload it to /content first.")

    res = pd.read_csv(resolution_path, usecols=["Datetime", "gross_t1"])
    res["Datetime"] = to_datetime_iso(res["Datetime"], utc=True, errors="coerce")
    res["gross_t1"] = to_datetime_iso(res["gross_t1"], utc=True, errors="coerce")
    res = res.dropna()

    bars = pd.read_csv(bars_path, usecols=["Datetime"])
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    bar_index = pd.DatetimeIndex(bars["Datetime"])

    start_pos = bar_index.get_indexer(res["Datetime"])
    end_pos = bar_index.get_indexer(res["gross_t1"])
    mask = (start_pos >= 0) & (end_pos >= 0)
    start_pos, end_pos = start_pos[mask].astype("int64"), end_pos[mask].astype("int64")
    n_bars = len(bar_index)
    n_events = len(start_pos)

    print(f"edge_stem={edge_stem} asset={asset}")
    print(f"n_events={n_events:,}  n_bars={n_bars:,}")
    print("Timing one full sequential-bootstrap draw (this is what runs once per bagged tree)...")

    elapsed = _sequential_bootstrap_one_draw_timing(start_pos, end_pos, n_bars)
    print(f"\nOne draw: {elapsed:.2f}s (measured on THIS Colab runtime, not extrapolated from another machine)")
    for n_estimators in (50, 100, 200, 500, 1000):
        total_minutes = elapsed * n_estimators / 60.0
        print(f"  n_estimators={n_estimators:5d}  ->  ~{total_minutes:.1f} min per bagging round "
              f"(Step02 final model: 1 round; Step03 OOF: ~10 rounds, one per CPCV fold, "
              f"each somewhat cheaper since folds train on ~60% of events)")


if __name__ == "__main__":
    main()
