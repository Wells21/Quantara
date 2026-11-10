"""AFML Ch.10 walkthrough, s.10.6 limit price at entry (design owner-approved 2026-10-26; pre-registered in
docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4J.4). Research period only (holdout sealed).

Per approved bet (live calibrated p >= live threshold), barriers anchored at the signal mid p0:
  f      = SL + p (TP - SL)                 the expected exit price implied by the barriers and p (AFML p.145)
  x0     = side (f - p0) = (p - SL/(TP+SL)) (TP + SL)
  omega  = x0^2 (m^-2 - 1)                  AFML getW, calibrated so the s.10.6 size at p0 equals the s.10.3 size m
  Q      = 1 / (c_q SL)                     so that m Q = today's D017 units
At the fill 5.7 s later (ask for longs, bid for shorts): x1 = side (f - fill); m1 = x1 / sqrt(omega + x1^2) (AFML betSize);
no trade if x1 <= 0. The book limit price p_bar = average of L(f, omega, u) = f - side u sqrt(omega / (1 - u^2)) over the
units u in (0, m] (Snippet 10.4 limitPrice, continuous units); "full fill" if the fill is within p_bar.
Outcome per unit risk = Gate C executed_r (broker costs). Compared with today's always-fill entry at size m.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_limit_price_test --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]


def bet_size(w: np.ndarray, x: np.ndarray) -> np.ndarray:
    return x * (w + x ** 2) ** -0.5


def limit_price(f: np.ndarray, w: np.ndarray, m: np.ndarray, side: np.ndarray, n_grid: int = 400) -> np.ndarray:
    u = (np.arange(n_grid) + 0.5) / n_grid  # midpoint rule on (0, 1], scaled by m
    uu = u[None, :] * m[:, None]
    lp = f[:, None] - side[:, None] * uu * np.sqrt(w[:, None] / (1.0 - uu ** 2))
    return lp.mean(axis=1)


def daily_sharpe(exit_time: pd.Series, pnl: np.ndarray) -> float:
    d = pd.Series(pnl, index=pd.to_datetime(exit_time, utc=True, format="ISO8601").dt.floor("D")).groupby(level=0).sum()
    d = d.reindex(pd.date_range(d.index.min(), d.index.max(), freq="D"), fill_value=0.0)
    return float(d.mean() / d.std(ddof=1) * np.sqrt(252)) if d.std(ddof=1) > 0 else float("nan")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
    me = next(e for e in manifest["selected_edges"] if e["edge_id"] == args.edge)
    thr, c_q = float(me["final_live_threshold"]), float(me["risk_caps"]["afml_concurrency_divisor"])
    cal = next((V2 / "assets" / asset / "edges" / edge / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["signal_mid", "entry_bid", "entry_ask", "exit_time", "side", "take_profit_distance_price",
                                   "stop_distance_price", "live_calibrated_probability", "afml_bet_size", "executed_r", "execution_status"])
    ev = ev[ev.execution_status.eq("resolved") & (ev.live_calibrated_probability >= thr) & (ev.afml_bet_size > 0)].reset_index(drop=True)
    side = ev.side.to_numpy(float)
    tp, sl = ev.take_profit_distance_price.to_numpy(), ev.stop_distance_price.to_numpy()
    p0, p = ev.signal_mid.to_numpy(), ev.live_calibrated_probability.to_numpy()
    m = ev.afml_bet_size.to_numpy()
    tp_px, sl_px = p0 + side * tp, p0 - side * sl
    f = sl_px + p * (tp_px - sl_px)
    x0 = side * (f - p0)
    w = x0 ** 2 * (m ** -2 - 1.0)
    fill = np.where(side > 0, ev.entry_ask.to_numpy(), ev.entry_bid.to_numpy())
    x1 = side * (f - fill)
    m1 = np.where(x1 > 0, bet_size(w, np.maximum(x1, 0.0)), 0.0)
    p_bar = limit_price(f, w, m, side)
    full = side * (fill - p_bar) <= 0
    r = ev.executed_r.to_numpy()
    pnl_now, pnl_lim = m / c_q * r, m1 / c_q * r
    ratio = m1 / m
    skipped = m1 <= 0
    shrunk = ratio < 0.999
    report = {
        "edge": args.edge, "test": "AFML Ch.10 s.10.6 limit price at entry vs always-fill (research period only)",
        "n_bets": int(len(ev)), "live_threshold": thr, "c_q": c_q,
        "x0_over_sl_median": float(np.median(x0 / sl)),
        "fill_slippage_vs_signal_mid_over_sl": {"median": float(np.median(side * (fill - p0) / sl)),
                                                "p95": float(np.percentile(side * (fill - p0) / sl, 95))},
        "limit_minus_signal_over_sl_median": float(np.median(side * (p_bar - p0) / sl)),
        "share_full_fill_within_book_limit": float(full.mean()),
        "share_skipped": float(skipped.mean()), "share_shrunk": float(shrunk.mean()),
        "size_ratio_new_over_now": {"mean": float(ratio.mean()), "p5": float(np.percentile(ratio, 5)), "median": float(np.median(ratio))},
        "mean_r_skipped": float(r[skipped].mean()) if skipped.any() else None,
        "mean_r_most_shrunk_decile": float(r[ratio <= np.percentile(ratio, 10)].mean()),
        "mean_r_least_shrunk_decile": float(r[ratio >= np.percentile(ratio, 90)].mean()),
        "corr_size_ratio_vs_r": float(np.corrcoef(ratio, r)[0, 1]),
        "always_fill": {"net_r_budget": float(pnl_now.sum()), "risk_committed": float((m / c_q).sum()),
                        "net_per_risk": float(pnl_now.sum() / (m / c_q).sum()), "daily_sharpe": daily_sharpe(ev.exit_time, pnl_now)},
        "limit_price_rule": {"net_r_budget": float(pnl_lim.sum()), "risk_committed": float((m1 / c_q).sum()),
                             "net_per_risk": float(pnl_lim.sum() / max((m1 / c_q).sum(), 1e-12)), "daily_sharpe": daily_sharpe(ev.exit_time, pnl_lim)},
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
