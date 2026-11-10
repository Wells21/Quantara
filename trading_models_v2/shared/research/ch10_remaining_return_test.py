"""AFML Ch.10 walkthrough, plan step A (owner-approved 2026-10-26): can an in-trade forecast of the REMAINING return
beat the martingale? Pre-registered in docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4J.4. Research period only (holdout sealed).

AFML s.10.6 sizes an open bet from the divergence between a forecast price f and the market price p. f - p_j is the
forecast of the remaining move, so the target here is the trade's remaining mid return in R from bar close j to its exit:
    r_rem = label_r - side (close_j - entry_mid) / SL
(a martingale price gives E[r_rem] = 0, i.e. f = p and a zero s.10.6 position).
Forecasts (pooled OOS, purged + embargoed 10-fold on the row span [t_j, label_t1]):
  zero    : the martingale;
  drift   : the training folds' weighted mean of r_rem (a constant edge);
  (b')    : bagged regression trees on trade state (see STATE_COLS);
  (c')    : (b') + the edge's selected features at bar j.
GO_state: (b') beats zero AND drift on weighted MSE; GO_features: (c') beats (b'); each with the trade-clustered bootstrap
95 % CI of the MSE gain above zero.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch10_remaining_return_test --edge XAUUSD/rangestructure_long --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import BaggingRegressor
from sklearn.tree import DecisionTreeRegressor

from shared.features.clustered_feature_importance import purged_kfold_splits
from shared.models.sequential_bootstrap import get_avg_uniqueness
from shared.research.ch10_intrade_forecast_test import _ts, build_rows, event_mask

V2 = Path(__file__).resolve().parents[2]
N_SPLITS = 10
N_ESTIMATORS = 1000
N_BOOT = 1000
SEED = 20261026
VOL_BARS = 100
TREE = dict(max_features="sqrt", min_weight_fraction_leaf=0.05)
STATE_COLS = ["s_random_walk", "time_left_frac", "entry_oof_prob", "r_so_far", "best_r_so_far", "worst_r_so_far",
              "hours_in_trade", "vol_since_over_pre", "z_to_tp", "z_to_sl", "hour_of_week", "hours_to_weekend",
              "hours_to_2100utc"]


def add_trade_state(rows: pd.DataFrame, asset: str, edge: str, approved_only: bool = False) -> pd.DataFrame:
    root = V2 / "assets" / asset / "edges" / edge
    cal = next((root / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["entry_time", "vertical_deadline", "label_r", "side", "entry_bid", "entry_ask",
                                   "take_profit_distance_price", "stop_distance_price", "oof_prob_raw", "execution_status",
                                   "live_calibrated_probability", "afml_bet_size"])
    ev = ev[event_mask(ev, f"{asset}/{edge}", approved_only)].reset_index(drop=True)  # same order as build_rows
    # the edge's own research bars (feature matrix OHLC; JPN research data predates the 2026-10-23 bar rebuild)
    bars = pd.read_csv(next((root / "artifacts" / "research_dataset").glob("*_feature_matrix.csv")),
                       usecols=["Datetime", "High", "Low", "Close"])
    bars = bars.assign(t_ns=_ts(bars.Datetime)).sort_values("t_ns").reset_index(drop=True)
    t, hi, lo, cl = bars.t_ns.to_numpy(), bars.High.to_numpy(float), bars.Low.to_numpy(float), bars.Close.to_numpy(float)
    lr = np.concatenate(([0.0], np.diff(np.log(cl))))
    sig_bar = pd.Series(lr).rolling(VOL_BARS).std().to_numpy()
    span_h = np.full(len(t), np.nan)
    span_h[VOL_BARS:] = (t[VOL_BARS:] - t[:-VOL_BARS]) / 3.6e12
    sig_hour = sig_bar * np.sqrt(VOL_BARS / span_h)                       # per sqrt(hour), past 100 bars
    c1, c2 = np.cumsum(lr), np.cumsum(lr ** 2)

    i, j = rows.trade.to_numpy(), rows.bar.to_numpy()
    side = ev.side.to_numpy(float)[i]
    sl, tp = ev.stop_distance_price.to_numpy()[i], ev.take_profit_distance_price.to_numpy()[i]
    entry = ((ev.entry_bid + ev.entry_ask) / 2).to_numpy()[i]
    e_ns, v_ns = _ts(ev.entry_time)[i], _ts(ev.vertical_deadline)[i]
    first = np.searchsorted(t, _ts(ev.entry_time), side="right")[i]          # first bar close after entry
    out = rows.copy()
    out["r_so_far"] = side * (cl[j] - entry) / sl
    out["r_rem"] = ev.label_r.to_numpy()[i] - out["r_so_far"].to_numpy()
    best, worst = np.empty(len(rows)), np.empty(len(rows))
    order = np.argsort(i, kind="stable")
    for idx in np.split(order, np.flatnonzero(np.diff(i[order])) + 1):
        f0, jj, s, e0, d = first[idx[0]], j[idx], side[idx[0]], entry[idx[0]], sl[idx[0]]
        seg_f = (hi if s > 0 else lo)[f0: jj.max() + 1]
        seg_a = (lo if s > 0 else hi)[f0: jj.max() + 1]
        best[idx] = (np.maximum.accumulate(s * (seg_f - e0)) / d)[jj - f0]
        worst[idx] = (np.minimum.accumulate(s * (seg_a - e0)) / d)[jj - f0]
    out["best_r_so_far"], out["worst_r_so_far"] = best, worst
    out["hours_in_trade"] = (t[j] - e_ns) / 3.6e12
    n_since = j - first + 1
    s1 = c1[j] - c1[first - 1]
    s2 = c2[j] - c2[first - 1]
    var_since = np.where(n_since >= 3, (s2 - s1 ** 2 / np.maximum(n_since, 1)) / np.maximum(n_since - 1, 1), np.nan)
    pre = sig_bar[np.maximum(first - 1, 0)]
    out["vol_since_over_pre"] = np.where(np.isfinite(var_since) & (pre > 0), np.sqrt(np.maximum(var_since, 0)) / pre, 1.0)
    h_left = np.maximum((v_ns - t[j]) / 3.6e12, 1.0 / 60.0)
    denom = cl[j] * sig_hour[j] * np.sqrt(h_left)
    out["z_to_tp"] = (tp - side * (cl[j] - entry)) / denom
    out["z_to_sl"] = (sl + side * (cl[j] - entry)) / denom
    dt = pd.DatetimeIndex(pd.to_datetime(t[j], unit="ns", utc=True))
    hour = dt.hour.to_numpy() + dt.minute.to_numpy() / 60.0
    out["hour_of_week"] = dt.dayofweek.to_numpy() * 24 + hour
    out["hours_to_weekend"] = ((4 - dt.dayofweek.to_numpy()) % 7) * 24 + (21.0 - hour)
    out.loc[out.hours_to_weekend < 0, "hours_to_weekend"] += 7 * 24
    out["hours_to_2100utc"] = (21.0 - hour) % 24
    return out.replace([np.inf, -np.inf], np.nan).dropna(subset=STATE_COLS + ["r_rem"]).reset_index(drop=True)


def fit_predict(xtr, ytr, wtr, xte, avg_u: float, seed: int) -> np.ndarray:
    reg = BaggingRegressor(DecisionTreeRegressor(**TREE), n_estimators=N_ESTIMATORS, max_samples=avg_u, random_state=seed, n_jobs=1)
    reg.fit(xtr, ytr, sample_weight=wtr)
    return reg.predict(xte)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--approved-only", action="store_true", help="train and test on live-approved trades only")
    args = ap.parse_args(argv)
    asset, edge = args.edge.split("/")
    started = time.time()
    rows, feats, info = build_rows(asset, edge, args.approved_only)
    rows = add_trade_state(rows, asset, edge, args.approved_only).sort_values(["t0_ns", "trade"]).reset_index(drop=True)
    n = len(rows)
    bar_of = rows.bar.to_numpy()
    n_bars = int(max(bar_of.max(), rows.t1_bar.max()) + 1)
    w = get_avg_uniqueness(bar_of, np.maximum(rows.t1_bar.to_numpy(), bar_of), n_bars, np.arange(n))
    avg_u = float(np.clip(w.mean(), 1e-4, 1.0))
    w = w / w.mean()
    embargo = max(100, int(0.01 * n))
    splits = purged_kfold_splits(rows.t0_ns.to_numpy(), rows.t1_ns.to_numpy(), N_SPLITS, embargo)
    y = rows.r_rem.to_numpy()
    xb, xc = rows[STATE_COLS].to_numpy(), rows[STATE_COLS + feats].to_numpy()
    p0, pd_, pb, pc = np.zeros(n), np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    folds = []
    for k, (tr, te) in enumerate(splits):
        pd_[te] = np.average(y[tr], weights=w[tr])
        pb[te] = fit_predict(xb[tr], y[tr], w[tr], xb[te], avg_u, SEED + k)
        pc[te] = fit_predict(xc[tr], y[tr], w[tr], xc[te], avg_u, SEED + 100 + k)
        rec = {"fold": k, "n_test": int(len(te))}
        for name, p in (("zero", p0), ("drift", pd_), ("b", pb), ("c", pc)):
            rec[f"mse_{name}"] = float(np.average((y[te] - p[te]) ** 2, weights=w[te]))
        folds.append(rec)
        print(f"[{args.edge}] fold {k + 1}/{N_SPLITS} {rec} elapsed {time.time() - started:.0f}s", flush=True)

    se = {name: (y - p) ** 2 * w for name, p in (("zero", p0), ("drift", pd_), ("b", pb), ("c", pc))}
    trades = rows.trade.to_numpy()
    order = np.argsort(trades, kind="stable")
    groups = np.split(order, np.flatnonzero(np.diff(trades[order])) + 1)
    gsum = {k: np.array([v[g].sum() for g in groups]) for k, v in se.items()}
    gw = np.array([w[g].sum() for g in groups])

    def gains(pick: np.ndarray) -> tuple[float, float, float]:
        W = gw[pick].sum()
        return ((gsum["zero"][pick].sum() - gsum["b"][pick].sum()) / W,
                (gsum["drift"][pick].sum() - gsum["b"][pick].sum()) / W,
                (gsum["b"][pick].sum() - gsum["c"][pick].sum()) / W)

    allg = np.arange(len(groups))
    point = gains(allg)
    rng = np.random.default_rng(SEED)
    boot = np.array([gains(rng.integers(0, len(groups), len(groups))) for _ in range(N_BOOT)])
    names = ["mse_gain_b_over_zero", "mse_gain_b_over_drift", "mse_gain_c_over_b"]
    ci = {nm: [float(np.percentile(boot[:, k], 2.5)), float(np.percentile(boot[:, k], 97.5))] for k, nm in enumerate(names)}
    mse = {k: float(v.sum() / w.sum()) for k, v in se.items()}

    def quint(p: np.ndarray) -> list[float]:
        q = pd.qcut(pd.Series(p).rank(method="first"), 5, labels=False).to_numpy()
        return [float(np.average(y[q == k], weights=w[q == k])) for k in range(5)]

    report = {
        "edge": args.edge, "test": "AFML Ch.10 plan step A: remaining-return forecast vs martingale (research period only)",
        "population": "approved_only" if args.approved_only else "all_events", "rows": {**info, "n_rows_after_state": int(n)}, "avg_uniqueness": avg_u, "embargo_rows": embargo,
        "state_cols": STATE_COLS, "n_features": len(feats), "tree": TREE, "n_estimators": N_ESTIMATORS,
        "mean_r_rem_weighted": float(np.average(y, weights=w)),
        "pooled_oos_mse": mse, "r2_oos_vs_zero": {k: 1 - mse[k] / mse["zero"] for k in ("drift", "b", "c")},
        "gains": dict(zip(names, point)), "bootstrap_95ci": ci,
        "mean_r_rem_by_prediction_quintile": {"b": quint(pb), "c": quint(pc)},
        "GO_state": bool(ci["mse_gain_b_over_zero"][0] > 0 and ci["mse_gain_b_over_drift"][0] > 0),
        "GO_features": bool(ci["mse_gain_c_over_b"][0] > 0), "folds": folds, "elapsed_s": round(time.time() - started, 1)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("edge", "pooled_oos_mse", "r2_oos_vs_zero", "gains", "bootstrap_95ci",
                                             "mean_r_rem_by_prediction_quintile", "GO_state", "GO_features")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
