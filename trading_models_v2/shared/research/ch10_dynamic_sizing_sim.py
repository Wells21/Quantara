"""AFML Ch.10 walkthrough, step 3 (owner-approved 2026-10-26): s.10.6 dynamic bet sizing per order vs fixed brackets.

Research period only (holdout sealed). Edges: those that passed plan step A (GBPJPY, HK, JPN volatilitycarry).
Bets = approved research events (live calibrated p >= live threshold), entered at entry_time (5.7 s after the bar close)
with m_i Q_i units, Q_i = 1 / (c_q SL_i) (D017 per order; m_i = payoff-aware s.10.3 size). The fixed TP / SL stay on
every order (broker-side) and close whatever is left at the first touch on the Dukascopy MID path. Fills at Moneta
bid/ask (broker cost model), commission per side, swap per rollover on the units actually held.
Arms:
  fixed        : today -- hold m_i Q_i until a barrier.
  frozen_fexp  : s.10.6 with a frozen forecast f = SL + p (TP - SL) (expected exit price at entry);
  frozen_ftp   : s.10.6 with a frozen forecast f = TP;
  updated_d*   : s.10.6 with the updated forecast f_j = p_j + side E_j[r_rem] SL from the plan-step-A trade-state model
                 (pooled OOS predictions, purged + embargoed 10-fold, same seeds as step A), d = s.10.5 step 0 / .01 / .05.
For every s.10.6 arm: omega_i is calibrated so the s.10.6 size equals m_i at the entry decision (AFML getW), the target
m_j = x_j / sqrt(omega_i + x_j^2) (betSize) floored at 0 (one-sided edge), re-targeted at each bar close + 5.7 s.
Metrics: net / gross P&L in base-risk units, daily Sharpe of realised P&L by exit day, max drawdown of the realised
curve, cost split, orders, units-time (exposure), and per-bet mean R per unit of initial risk.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_dynamic_sizing_sim --edge GBPJPY/dailyrangecompletionfade_long --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from shared.execution.executable_target_dataset import research_cost_setup
from shared.features.clustered_feature_importance import purged_kfold_splits
from shared.models.sequential_bootstrap import get_avg_uniqueness
from shared.research.ch10_averaging_cost_sim import Ticks, lookup, swap_bps_per_night
from shared.research.ch10_intrade_forecast_test import _ts, build_rows, event_mask
from shared.research.ch10_path_giveback_audit import resolve
from shared.research.ch10_remaining_return_test import N_SPLITS, SEED, STATE_COLS, add_trade_state, fit_predict
from shared.research.v3_horizon_lab import _xef_rollovers

V2 = Path(__file__).resolve().parents[2]
STEPS = (0.0, 0.01, 0.05)
DAY_NS = 86_400_000_000_000


def state_predictions(asset: str, edge: str, approved_only: bool = False) -> pd.DataFrame:
    """Plan-step-A (b') pooled OOS predictions of remaining R, reproduced with the same rows, weights, folds and seeds."""
    rows, feats, info = build_rows(asset, edge, approved_only)
    rows = add_trade_state(rows, asset, edge, approved_only).sort_values(["t0_ns", "trade"]).reset_index(drop=True)
    n = len(rows)
    bar_of = rows.bar.to_numpy()
    n_bars = int(max(bar_of.max(), rows.t1_bar.max()) + 1)
    w = get_avg_uniqueness(bar_of, np.maximum(rows.t1_bar.to_numpy(), bar_of), n_bars, np.arange(n))
    avg_u = float(np.clip(w.mean(), 1e-4, 1.0))
    w = w / w.mean()
    splits = purged_kfold_splits(rows.t0_ns.to_numpy(), rows.t1_ns.to_numpy(), N_SPLITS, max(100, int(0.01 * n)))
    y, xb = rows.r_rem.to_numpy(), rows[STATE_COLS].to_numpy()
    pb = np.full(n, np.nan)
    for k, (tr, te) in enumerate(splits):
        pb[te] = fit_predict(xb[tr], y[tr], w[tr], xb[te], avg_u, SEED + k)
    return pd.DataFrame({"trade": rows.trade, "t_ns": rows.t0_ns, "close": rows.r_so_far * 0 + np.nan, "pred": pb,
                         "r_so_far": rows.r_so_far, "bar": rows.bar})


def bet_size(w, x):
    return x * (w + x ** 2) ** -0.5


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--approved-only", action="store_true", help="forecaster trained on live-approved trades only")
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    started = time.time()
    firm, delay_ms, cm, cm_path = research_cost_setup(asset)
    delay = int(delay_ms) * 1_000_000
    manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
    me = next(e for e in manifest["selected_edges"] if e["edge_id"] == args.edge)
    thr, c_q = float(me["final_live_threshold"]), float(me["risk_caps"]["afml_concurrency_divisor"])
    root = V2 / "assets" / asset / "edges" / edge
    cal = next((root / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["entry_time", "vertical_deadline", "side", "take_profit_distance_price", "stop_distance_price",
                                   "live_calibrated_probability", "afml_bet_size", "execution_status", "oof_prob_raw"])
    ev = ev[event_mask(ev, args.edge, args.approved_only)].reset_index(drop=True)  # build_rows trade index
    ev["approved"] = (ev.live_calibrated_probability >= thr) & (ev.afml_bet_size > 0)
    ev["entry_ns"], ev["vert_ns"] = _ts(ev.entry_time), _ts(ev.vertical_deadline)

    preds = state_predictions(asset, edge, args.approved_only)
    fm = pd.read_csv(next((root / "artifacts" / "research_dataset").glob("*_feature_matrix.csv")), usecols=["Datetime", "Close"])
    fm = fm.assign(t_ns=_ts(fm.Datetime)).sort_values("t_ns").reset_index(drop=True)
    preds["close"] = fm.Close.to_numpy()[preds.bar.to_numpy()]
    print(f"[{args.edge}] state predictions ready ({len(preds)} rows), elapsed {time.time() - started:.0f}s", flush=True)

    ticks = Ticks(asset, cm)
    bets = ev[ev.approved].copy()
    side = int(bets.side.iloc[0])
    touch, emid = np.full(len(bets), -1, dtype=np.int64), np.full(len(bets), np.nan)
    fidx = ticks.file_of(bets.entry_ns.to_numpy())
    max_hold = int((bets.vert_ns - bets.entry_ns).max())
    for f in np.unique(fidx):
        need = [j for j in range(f, len(ticks.files)) if ticks.starts[j] <= ticks.starts[f] + 7 * DAY_NS + max_hold]
        t, _, _, m = ticks.window(need)
        for k in np.flatnonzero(fidx == f):
            r = bets.iloc[k]
            res = resolve(t, m, int(r.entry_ns), side, float(r.take_profit_distance_price), float(r.stop_distance_price), int(r.vert_ns))
            if res is not None:
                touch[k], emid[k] = res[3], res[4]
    bets["touch_ns"], bets["entry_mid"] = touch, emid
    bets = bets[bets.touch_ns > bets.entry_ns]
    print(f"[{args.edge}] {len(bets)} approved bets resolved, elapsed {time.time() - started:.0f}s", flush=True)

    sl, tp = bets.stop_distance_price.to_numpy(), bets.take_profit_distance_price.to_numpy()
    m0, p = bets.afml_bet_size.to_numpy(), bets.live_calibrated_probability.to_numpy()
    Q = 1.0 / (c_q * sl)
    x0_exp = (p - sl / (tp + sl)) * (tp + sl)          # side (f_exp - p0) with f_exp = SL + p (TP - SL)
    w_exp = x0_exp ** 2 * (m0 ** -2 - 1.0)
    w_tp = tp ** 2 * (m0 ** -2 - 1.0)                  # f = TP: x0 = TP distance
    pr = preds[preds.trade.isin(bets.index)]
    by_trade = {k: g for k, g in pr.groupby("trade")}

    arms = {"fixed": [], "frozen_fexp": [], "frozen_ftp": []}
    for d in STEPS:
        arms[f"updated_d{d:g}"] = []
    for k, (bi, r) in enumerate(bets.iterrows()):
        e_ns, t_ns, u0 = int(r.entry_ns), int(r.touch_ns), m0[k] * Q[k]
        g = by_trade.get(bi)
        if g is not None:
            g = g[(g.t_ns.to_numpy() + delay) < t_ns]
        times = (g.t_ns.to_numpy() + delay) if g is not None and len(g) else np.array([], dtype=np.int64)
        for name in arms:
            arms[name].append((bi, e_ns, u0))
        if len(times):
            mv = side * (g.close.to_numpy() - r.entry_mid)            # favourable move so far, price units
            tg = {"frozen_fexp": bet_size(w_exp[k], np.maximum(x0_exp[k] - mv, 0.0)),
                  "frozen_ftp": bet_size(w_tp[k], np.maximum(tp[k] - mv, 0.0))}
            xu = np.maximum(g.pred.to_numpy() * sl[k], 0.0)
            mu = bet_size(w_exp[k], xu)
            for d in STEPS:
                tg[f"updated_d{d:g}"] = np.minimum(np.round(mu / d) * d, 1.0) if d > 0 else mu
            for name, mt in tg.items():
                units = mt * Q[k]
                prev = np.concatenate(([u0], units[:-1]))
                for tt, dlt in zip(times, units - prev):
                    if abs(dlt) > 1e-15:
                        arms[name].append((bi, int(tt), float(dlt)))
        for name in arms:
            arms[name].append((bi, t_ns, None))  # close the rest at the touch tick (broker-side stop / target)

    swap_bps = swap_bps_per_night(cm, side)
    comm_rt = float(cm.get("commission_bps_rt", 0.0))
    rule, trip = str(cm.get("server_tz_rule", "NY_CLOSE")), int(cm.get("swap", {}).get("triple_day", 3))
    exit_day = pd.Series(pd.to_datetime(bets.touch_ns.to_numpy(), unit="ns", utc=True).floor("D"), index=bets.index)
    results = {}
    for name, orders in arms.items():
        o = pd.DataFrame(orders, columns=["bet", "t", "delta"])
        # resolve closing deltas = minus the units held
        held = {}
        deltas = []
        for bi, t, dl in o.itertuples(index=False):
            cur = held.get(bi, 0.0)
            dl = -cur if (dl is None or dl != dl) else dl  # NaN marks the closing order
            held[bi] = cur + dl
            deltas.append(dl)
        o["delta"] = np.array(deltas) * side
        b, a, mid = lookup(ticks, o.t.to_numpy(), "next")
        fill = np.where(o.delta > 0, a, b)
        o["cash"] = -o.delta * fill
        o["gross_cash"] = -o.delta * mid
        o["spread"] = np.abs(o.delta) * np.abs(fill - mid)
        o["comm"] = np.abs(o.delta) * fill * comm_rt / 2.0 / 1e4
        # swap on units held between consecutive orders of the same bet
        o = o.sort_values(["bet", "t"], kind="stable").reset_index(drop=True)
        o["pos"] = o.groupby("bet").delta.cumsum()
        o["t_next"] = o.groupby("bet").t.shift(-1)
        o["mid_px"] = mid[o.index] if False else lookup(ticks, o.t.to_numpy(), "next")[2]
        seg = o.dropna(subset=["t_next"])
        seg = seg[seg.pos.abs() > 1e-15]
        nights = np.array([_xef_rollovers(pd.Timestamp(int(a_), tz="UTC"), pd.Timestamp(int(b_), tz="UTC"), trip, rule)
                           for a_, b_ in zip(seg.t, seg.t_next)])
        swap = pd.Series(np.abs(seg.pos.to_numpy()) * seg.mid_px.to_numpy() * swap_bps / 1e4 * nights, index=seg.bet.to_numpy())
        per_bet = o.groupby("bet").agg(cash=("cash", "sum"), gross=("gross_cash", "sum"), spread=("spread", "sum"),
                                       comm=("comm", "sum"), orders=("t", "size"))
        per_bet["swap"] = swap.groupby(level=0).sum().reindex(per_bet.index).fillna(0.0)
        per_bet["net"] = per_bet.cash - per_bet.comm - per_bet.swap
        units_time = float(np.sum(np.abs(seg.pos.to_numpy()) * (seg.t_next.to_numpy() - seg.t.to_numpy())) / 3.6e12)
        daily = per_bet.net.groupby(exit_day.reindex(per_bet.index).to_numpy()).sum()
        daily = daily.reindex(pd.date_range(daily.index.min(), daily.index.max(), freq="D"), fill_value=0.0)
        curve = daily.cumsum().to_numpy()
        dd = float(np.max(np.maximum.accumulate(curve) - curve))
        risk0 = (m0 * Q * sl)  # initial risk per bet in base units = m0 / c_q
        r_per_bet = per_bet.net.to_numpy() / risk0[np.searchsorted(bets.index.to_numpy(), per_bet.index.to_numpy())]
        results[name] = {"net_pnl": float(per_bet.net.sum()), "gross_pnl_mid": float(per_bet.gross.sum()),
                         "spread_cost": float(per_bet.spread.sum()), "commission_cost": float(per_bet.comm.sum()),
                         "swap_cost": float(per_bet.swap.sum()), "orders": int(per_bet.orders.sum()),
                         "units_hours": units_time, "daily_sharpe_ann": float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)),
                         "max_drawdown": dd, "net_over_max_drawdown": float(per_bet.net.sum() / dd) if dd > 0 else None,
                         "mean_r_per_initial_risk": float(np.mean(r_per_bet)),
                         "share_bets_closed_early_by_resizing": float(np.mean(o.groupby("bet").pos.apply(lambda s: (s.abs() < 1e-15).any() and (s.iloc[:-1].abs() < 1e-15).any()))) if name != "fixed" else 0.0}
        print(f"[{args.edge}] {name} {results[name]}", flush=True)
    report = {"edge": args.edge, "test": "AFML Ch.10 step 3: s.10.6 dynamic sizing per order vs fixed brackets, Moneta costs (research period only)",
              "firm": firm, "cost_model": str(cm_path.relative_to(V2)), "entry_delay_ms": int(delay_ms), "live_threshold": thr,
              "forecaster_population": "approved_only" if args.approved_only else "all_events", "c_q": c_q, "n_bets": int(len(bets)), "units": "base risk budget (1.0 = base 0.5 % risk at 1 x stop distance)",
              "arms": results, "elapsed_s": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
