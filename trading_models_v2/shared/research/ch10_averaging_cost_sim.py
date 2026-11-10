"""AFML Ch.10 walkthrough, test (owner-approved 2026-10-26): s.10.4 averaging + s.10.5 discretisation vs D017 budgeting.

Research period only (the sealed holdout is NOT read). Bets = the edge's resolved research events with live calibrated
probability >= the live threshold; bet size m_i = the payoff-aware AFML size (afml_bet_size, D007). Every bet ends at its
first barrier touch on the Dukascopy MID tick path (mid-label contract, same replay as ch10_path_giveback_audit).
Fills are at the Moneta broker bid/ask (Dukascopy mid +/- the calibrated broker spread, the Gate C broker model), plus
commission and swap per rollover actually crossed (same rules as Gate C / 00d).

Arms (positions in units of base risk budget per price unit; 1.0 = the base 0.5 % risk per 1 x stop distance):
  D017   : today. Each bet is its own order of side * (m_i / c_q) / SL_i units, bought at its entry_time (5.7 s after the
           bar close) and closed by the broker stop / target at the touch tick (server-side, no delay).
  AVG_d  : AFML s.10.4 Snippet 10.2 -- one position per edge = side * round(mean m of active bets / d) * d * Q
           (s.10.5 Snippet 10.3; d = 0 means no discretisation; d in the book's exercise 10.5 values 0.01, 0.05, 0.10).
           Active = issued <= t < end (Snippet 10.2). Re-targeted at each bet entry and each bet end + 5.7 s (no broker
           stop exists for a net position, so the bot acts after the live latency). Q = 1 / median SL of bets entered in
           the trailing 30 days, refreshed once per UTC week (the book's Q is a fixed maximum position; weekly refresh
           keeps it in risk units over 4 years). At concurrency n = c_q the two arms hold the same exposure.
Outputs per arm: net and gross P&L (budget units), daily mark-to-market Sharpe (x sqrt(252)), max drawdown, spread /
commission / swap costs, number of orders, units traded.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_averaging_cost_sim --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from shared.execution import oof_exact_execution as exact
from shared.execution.executable_target_dataset import research_cost_setup
from shared.research.ch10_path_giveback_audit import resolve
from shared.research.v3_horizon_lab import _xef_broker_loader, _xef_server_offset_hours

V2 = Path(__file__).resolve().parents[2]
STEPS = (0.0, 0.01, 0.05, 0.10)
FILE_RE = re.compile(r"_ticks_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.csv$")
DAY_NS = 86_400_000_000_000


def _ts(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True, format="ISO8601").to_numpy(dtype="datetime64[ns]").astype(np.int64)


def swap_bps_per_night(cm: dict, side: int) -> float:
    """Cost in bps of notional per charged night (positive = cost), the _xef_apply_carry_costs conversion."""
    sw = cm.get("swap", {})
    mode = int(sw.get("mode", 0))
    rate = float(sw.get("long" if side == 1 else "short", 0.0))
    ref = float(sw.get("ref_price", cm.get("ref_price", 1.0)))
    if mode == 1:
        return -rate * float(sw["point"]) / ref * 1e4
    if mode == 2:
        return -rate / (ref * float(sw["contract_size"])) * 1e4
    if mode == 5:
        return -rate / 100.0 / 360.0 * 1e4
    return 0.0


def rollover_instants(t0: int, t1: int, cm: dict) -> list[tuple[int, int]]:
    """(UTC ns, nights charged) for every server midnight in [t0, t1]: Sat/Sun rollovers 0, triple day 3 (_xef_rollovers)."""
    rule = str(cm.get("server_tz_rule", "NY_CLOSE"))
    trip = int(cm.get("swap", {}).get("triple_day", 3))
    out = []
    for d in pd.date_range(pd.Timestamp(t0, tz="UTC").normalize() - pd.Timedelta(days=1),
                           pd.Timestamp(t1, tz="UTC").normalize() + pd.Timedelta(days=2), freq="D"):
        utc = d - pd.Timedelta(hours=_xef_server_offset_hours(d, rule))  # server-date d 00:00 in UTC
        prev = d - pd.Timedelta(days=1)
        mult = 0 if prev.dayofweek in (5, 6) else (3 if (prev.dayofweek + 1) % 7 == trip else 1)
        if t0 <= utc.value <= t1 and mult:
            out.append((utc.value, mult))
    return out


class Ticks:
    """Broker bid/ask + mid for the asset's curated weekly files, loaded on demand (window cache)."""

    def __init__(self, asset: str, cm: dict):
        self.files = sorted((V2 / "assets" / asset / "curated" / "ticks").glob("*_ticks_*.csv"))
        self.starts = np.array([pd.Timestamp(FILE_RE.search(f.name).group(1), tz="UTC").value for f in self.files])
        self.load = _xef_broker_loader(exact._load_tick_file, cm)
        self.cache: dict[int, tuple] = {}

    def window(self, idx: list[int]):
        for j in list(self.cache):
            if j not in idx:
                del self.cache[j]
        for j in idx:
            if j not in self.cache:
                t, b, a = self.load(str(self.files[j]))
                self.cache[j] = (t, b, a, (b + a) / 2.0)
        parts = [self.cache[j] for j in idx]
        return tuple(np.concatenate([p[k] for p in parts]) for k in range(4))

    def file_of(self, t_ns: np.ndarray) -> np.ndarray:
        return np.searchsorted(self.starts, t_ns, side="right") - 1


def lookup(ticks: Ticks, times: np.ndarray, how: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """bid/ask/mid at the first tick >= t ('next') or the last tick <= t ('last'), file by file."""
    bid, ask, mid = (np.full(len(times), np.nan) for _ in range(3))
    fidx = ticks.file_of(times)
    for f in np.unique(fidx):
        sel = np.flatnonzero(fidx == f)
        idx = [j for j in (f - 1, f, f + 1) if 0 <= j < len(ticks.files)]
        t, b, a, m = ticks.window(idx)
        k = np.searchsorted(t, times[sel], side="left" if how == "next" else "right")
        if how == "last":
            k = k - 1
        ok = (k >= 0) & (k < len(t))
        bid[sel[ok]], ask[sel[ok]], mid[sel[ok]] = b[k[ok]], a[k[ok]], m[k[ok]]
    return bid, ask, mid


def simulate(orders: pd.DataFrame, ticks: Ticks, cm: dict, side: int, days: np.ndarray, sl_ref: float) -> dict:
    """orders: time_ns, delta (units). Position path, cash at broker bid/ask, commission, swap, daily MTM."""
    o = orders[orders.delta.abs() > 1e-15].sort_values("time_ns", kind="stable").reset_index(drop=True)
    b, a, m = lookup(ticks, o.time_ns.to_numpy(), "next")
    good = np.isfinite(m)
    o, b, a, m = o[good].reset_index(drop=True), b[good], a[good], m[good]
    d = o.delta.to_numpy()
    fill = np.where(d > 0, a, b)
    comm_rt = float(cm.get("commission_bps_rt", 0.0))
    spread_cost = float(np.sum(np.abs(d) * np.abs(fill - m)))
    comm_cost = float(np.sum(np.abs(d) * fill * comm_rt / 2.0 / 1e4))
    pos = np.cumsum(d)
    t = o.time_ns.to_numpy()
    rolls = rollover_instants(int(t.min()), int(t.max()), cm)
    swap_cost = 0.0
    if rolls:
        rt = np.array([r[0] for r in rolls])
        k = np.searchsorted(t, rt, side="right") - 1
        held = np.where(k >= 0, pos[np.clip(k, 0, None)], 0.0)
        _, _, rm = lookup(ticks, rt, "last")
        bps = swap_bps_per_night(cm, side)
        mult = np.array([r[1] for r in rolls])
        swap_cost = float(np.nansum(np.abs(held) * rm * bps / 1e4 * mult))
        swap_daily = pd.Series(np.nan_to_num(np.abs(held) * rm * bps / 1e4 * mult), index=rt)
    else:
        swap_daily = pd.Series(dtype=float)
    # daily mark-to-market (net of spread + commission at fill time, swap at rollover)
    _, _, dm = lookup(ticks, days, "last")
    k = np.searchsorted(t, days, side="right") - 1
    held_d = np.where(k >= 0, pos[np.clip(k, 0, None)], 0.0)
    cash_flow = -d * fill - np.abs(d) * fill * comm_rt / 2.0 / 1e4
    cash_cum = np.concatenate(([0.0], np.cumsum(cash_flow)))[np.clip(k + 1, 0, None)]
    swap_cum = np.array([swap_daily[swap_daily.index <= x].sum() for x in days]) if len(swap_daily) else np.zeros(len(days))
    equity = cash_cum + held_d * np.nan_to_num(dm, nan=0.0) - swap_cum
    equity = pd.Series(equity).ffill().fillna(0.0).to_numpy()
    daily = np.diff(np.concatenate(([0.0], equity)))
    net = float(equity[-1])
    gross = float(np.sum(-d * m) + pos[-1] * m[-1])
    dd = float(np.max(np.maximum.accumulate(equity) - equity))
    sd = float(np.std(daily, ddof=1))
    return {"net_pnl": net, "gross_pnl_mid_no_costs": gross, "spread_cost": spread_cost, "commission_cost": comm_cost,
            "swap_cost": swap_cost, "orders": int(len(o)), "units_traded": float(np.sum(np.abs(d))),
            "final_position": float(pos[-1]), "daily_sharpe_ann": float(np.mean(daily) / sd * np.sqrt(252)) if sd > 0 else None,
            "max_drawdown": dd, "net_over_max_drawdown": net / dd if dd > 0 else None,
            "time_avg_risk_budget_in_use": float(np.sum(np.abs(pos[:-1]) * np.diff(t)) / (t[-1] - t[0]) * sl_ref)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
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
                                   "live_calibrated_probability", "afml_bet_size", "execution_status", "executed_r"])
    ev = ev[ev.execution_status.eq("resolved") & (ev.live_calibrated_probability >= thr) & (ev.afml_bet_size > 0)].reset_index(drop=True)
    side = int(ev.side.iloc[0])
    ev["entry_ns"], ev["vert_ns"] = _ts(ev.entry_time), _ts(ev.vertical_deadline)
    ticks = Ticks(asset, cm)

    # 1) bet ends on the mid path (touch tick)
    ends = np.full(len(ev), -1, dtype=np.int64)
    fidx = ticks.file_of(ev.entry_ns.to_numpy())
    max_hold = int((ev.vert_ns - ev.entry_ns).max())
    for f in np.unique(fidx):
        need = [j for j in range(f, len(ticks.files)) if ticks.starts[j] <= ticks.starts[f] + 7 * DAY_NS + max_hold]
        t, _, _, m = ticks.window(need)
        for i in np.flatnonzero(fidx == f):
            r = ev.iloc[i]
            res = resolve(t, m, int(r.entry_ns), side, float(r.take_profit_distance_price), float(r.stop_distance_price), int(r.vert_ns))
            if res is not None:
                ends[i] = res[3]
    ev["end_ns"] = ends
    ev = ev[ev.end_ns > ev.entry_ns].sort_values("entry_ns").reset_index(drop=True)
    n = len(ev)
    print(f"[{args.edge}] {n} approved bets resolved, elapsed {time.time() - started:.0f}s", flush=True)

    t_first, t_last = int(ev.entry_ns.min()), int(ev.end_ns.max() + delay)
    days = np.arange((t_first // DAY_NS + 1) * DAY_NS, (t_last // DAY_NS + 2) * DAY_NS, DAY_NS)
    results = {}

    # 2) D017: one order per bet, closed at the touch tick
    units = side * (ev.afml_bet_size / c_q) / ev.stop_distance_price
    d017 = pd.DataFrame({"time_ns": np.concatenate([ev.entry_ns, ev.end_ns]), "delta": np.concatenate([units, -units])})
    results["D017"] = simulate(d017, ticks, cm, side, days, float(np.median(ev.stop_distance_price)))
    results["D017"]["net_per_unit_risk_committed"] = results["D017"]["net_pnl"] / float(np.sum(ev.afml_bet_size / c_q))
    print(f"[{args.edge}] D017 {results['D017']}", flush=True)

    # 3) AFML s.10.4 averaging (+ s.10.5 discretisation)
    change = np.unique(np.concatenate([ev.entry_ns, ev.end_ns + delay]))
    week_starts = np.unique((change // (7 * DAY_NS)) * (7 * DAY_NS))
    q_week = {}
    entry_ns, sl, msz = ev.entry_ns.to_numpy(), ev.stop_distance_price.to_numpy(), ev.afml_bet_size.to_numpy()
    for w in week_starts:
        win = (entry_ns < w) & (entry_ns >= w - 30 * DAY_NS)
        ref = np.median(sl[win]) if win.any() else np.median(sl[entry_ns < w + 7 * DAY_NS][:20])
        q_week[int(w)] = 1.0 / ref
    grid = np.unique(np.concatenate([change, week_starts]))
    starts_sorted = entry_ns
    ends_eff = ev.end_ns.to_numpy() + delay
    # active set at each grid point: issued <= t < end (+ bot delay)
    msum = np.zeros(len(grid))
    mcnt = np.zeros(len(grid))
    o_s = np.searchsorted(grid, starts_sorted, side="left")
    o_e = np.searchsorted(grid, ends_eff, side="left")
    np.add.at(msum, o_s, msz); np.add.at(msum, o_e, -msz)
    np.add.at(mcnt, o_s, 1.0); np.add.at(mcnt, o_e, -1.0)
    msum, mcnt = np.cumsum(msum), np.cumsum(mcnt)
    mbar = np.where(mcnt > 0.5, msum / np.maximum(mcnt, 1.0), 0.0)
    qg = np.array([q_week[int((g // (7 * DAY_NS)) * (7 * DAY_NS))] for g in grid])
    for d in STEPS:
        mstar = np.minimum(np.round(mbar / d) * d, 1.0) if d > 0 else mbar
        target = side * mstar * qg
        delta = np.diff(np.concatenate(([0.0], target)))
        res = simulate(pd.DataFrame({"time_ns": grid, "delta": delta}), ticks, cm, side, days, float(np.median(sl)))
        res["mean_concurrency_when_active"] = float(mcnt[mcnt > 0.5].mean())
        results[f"AVG_d{d:g}"] = res
        print(f"[{args.edge}] AVG d={d} {res}", flush=True)

    report = {"edge": args.edge, "test": "AFML Ch.10 walkthrough: s.10.4 averaging + s.10.5 discretisation vs D017 budgeting, broker costs (research period only)",
              "firm": firm, "cost_model": str(cm_path.relative_to(V2)), "entry_delay_ms": int(delay_ms), "live_threshold": thr,
              "c_q": c_q, "n_bets": n, "mean_bet_size": float(ev.afml_bet_size.mean()),
              "gate_c_executed_r_mean_same_bets": float(ev.executed_r.mean()),
              "units": "base risk budget (1.0 = base 0.5 % risk at 1 x stop distance)", "arms": results,
              "elapsed_s": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
