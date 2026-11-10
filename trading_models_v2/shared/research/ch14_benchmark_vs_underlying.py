"""AFML Ch.14 walkthrough: correlation-to-underlying benchmark test (owner-approved 2026-10-27; pre-registered in
docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4N.4 before any result).

AFML p.197: a strategy significantly correlated with its underlying may be "essentially holding or short-selling the
investment universe, without adding much value"; s.14.7.4: information ratio vs a benchmark. Benchmark = random TIMING:
each trade is replayed with the same side, holding duration and stop distance, entered at a uniformly random bar time;
benchmark R = side * (Close(t + dur) - Close(t)) / stop_distance. Edge R = the trade's mid-path label_r (no costs on either).

usage (from trading_models_v2, venv312):
  python -m shared.research.ch14_benchmark_vs_underlying --edge ASSET/edge --out <json>
"""
from __future__ import annotations

import argparse
import json
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

V2 = Path(__file__).resolve().parents[2]
LEDGER = V2 / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
N_DRAWS = 1000
SEED = 0


def load_trades(edge_id: str) -> pd.DataFrame:
    cols = ["edge_id", "side_value", "entry_time", "exit_time", "stop_distance_price"]
    t = pd.read_csv(LEDGER, usecols=cols)
    t = t[t["edge_id"] == edge_id].copy()
    for c in ("entry_time", "exit_time"):
        t[c] = to_datetime_iso(t[c], utc=True, errors="coerce")
    asset, edge = edge_id.split("/", 1)
    oof_file = next((V2 / "assets" / asset / "edges" / edge / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    oof = pd.read_csv(oof_file, usecols=["entry_time", "label_r"])
    oof["entry_time"] = to_datetime_iso(oof["entry_time"], utc=True, errors="coerce")
    t = t.merge(oof.drop_duplicates("entry_time"), on="entry_time", how="inner")
    return t.dropna(subset=["entry_time", "exit_time", "stop_distance_price", "label_r", "side_value"]).reset_index(drop=True)


def load_closes(asset: str) -> pd.Series:
    bars = pd.read_csv(V2 / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv",
                       usecols=["Datetime", "Close"])
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    return bars.dropna().set_index("Datetime")["Close"].sort_index()


def price_at(closes: pd.Series, times: np.ndarray) -> np.ndarray:
    """Last close at or before each time."""
    pos = np.searchsorted(closes.index.values, times, side="right") - 1
    return closes.values[np.clip(pos, 0, len(closes) - 1)]


def run(edge_id: str) -> dict:
    t = load_trades(edge_id)
    closes = load_closes(edge_id.split("/", 1)[0])
    side = np.sign(t["side_value"].to_numpy(float))
    dur = (t["exit_time"] - t["entry_time"]).to_numpy().astype("timedelta64[ns]")
    stop = t["stop_distance_price"].to_numpy(float)
    edge_r = t["label_r"].to_numpy(float)
    bar_t = closes.index.values
    last_t = bar_t[-1]
    rng = np.random.default_rng(SEED)
    draws = np.empty((N_DRAWS, len(t)))
    for d in range(N_DRAWS):
        # uniform random bar time per trade, such that entry + duration stays inside the data
        valid_hi = np.searchsorted(bar_t, last_t - dur, side="right")
        start_idx = (rng.random(len(t)) * np.maximum(valid_hi, 1)).astype(int)
        t0 = bar_t[start_idx]
        draws[d] = side * (price_at(closes, t0 + dur) - closes.values[start_idx]) / stop
    bench_mean_per_trade = draws.mean(axis=0)
    draw_means = draws.mean(axis=1)
    excess = edge_r - bench_mean_per_trade
    daily = pd.Series(excess, index=t["exit_time"].dt.normalize()).groupby(level=0).sum()
    daily = daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq="D", tz="UTC"), fill_value=0.0)
    ir = float(daily.mean() / daily.std(ddof=1) * np.sqrt(365.25)) if daily.std(ddof=1) > 0 else float("nan")
    return {
        "edge": edge_id, "n_trades": int(len(t)), "n_draws": N_DRAWS, "seed": SEED,
        "edge_mean_r_mid": float(edge_r.mean()),
        "benchmark_mean_r": float(draw_means.mean()),
        "benchmark_mean_r_p05_p95": [float(np.percentile(draw_means, 5)), float(np.percentile(draw_means, 95))],
        "excess_mean_r": float(edge_r.mean() - draw_means.mean()),
        "beta_share_of_edge_mean": float(draw_means.mean() / edge_r.mean()) if edge_r.mean() != 0 else float("nan"),
        "p_value_draws_ge_edge": float(np.mean(draw_means >= edge_r.mean())),
        "information_ratio_annualised": ir,
        "timing_adds_value_p05": bool(np.mean(draw_means >= edge_r.mean()) < 0.05),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    res = run(a.edge)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
