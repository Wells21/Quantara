"""AFML Ch.16 / MLAM Ch.7 walkthrough tests T1-T4 (owner-approved 2026-10-29; pre-registered in
docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4P.1 before any result). Diagnostic only: nothing here changes the allocator or live.

  S0  sanity   : the matrix rebuilt from the representative ledger with 06's booking (manifest date_aggregation:
                 exit_date -> A, mark_to_market_daily -> E) equals 06's file; this tool's HRP_current
                 reproduces 07's hrp_allocation on 06's columns (code self-consistency). The live manifest weights are
                 reported beside it for information only: they come from the last allocator run and go stale whenever
                 07 changes, until the allocator is rerun (s.4P.3; the T1-T4 rerun is standing at every universe change).
  T1  s.16.4   : book-exact HRP (Snippet 16.4: sch.linkage(dist, 'single') on the square matrix = single linkage on
                 d~, the Euclidean distance between columns of D; raw cov / corr) vs 07's HRP (denoise + detone, linkage
                 on d), with the 2 x 2 factorial {d, d~} x {denoise on, off}.
  T2  ex.16.1a : return alignment -- A exit-day daily, B daily prorated, C weekly, D weekly prorated, E daily
                 mark-to-market at bar closes, F weekly mark-to-market; plus the average-bet-frequency rule.
  T3  s.16.6 / MLAM s.7.7-7.8 : Monte Carlo on our own inputs. M1 = Snippet 16.5 design (520 obs, 260 lookback, 22 rebalance,
                 10,000 iterations; block-bootstrap and Gaussian generators); M2 = MLAM Snippets 7.8-7.9 weight RMSE.
  T4  ex.16.3  : spectral re-scaling stress of V.

usage (from trading_models_v2, venv312):
  python -m shared.research.ch16_allocation_tests --out artifacts/ch16/ch16_allocation_tests.json [--iters 10000] [--workers 6]
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import scipy.cluster.hierarchy as sch
from scipy.spatial.distance import squareform

V2 = Path(__file__).resolve().parents[2]
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from shared.backtest_statistics import afml_ch14_ch15_reference as ch15  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.models.denoise_onc import cluster_kmeans_base, cov_to_corr  # noqa: E402

# Snippet 16.4 passes the square distance matrix to sch.linkage on purpose (single linkage on d~); scipy warns about it.
warnings.filterwarnings("ignore", message=".*uncondensed distance matrix", category=sch.ClusterWarning)

OUT = V2 / "portfolio_allocator" / "outputs"
LEDGER = OUT / "v2_representative_trade_ledger_core_plus_watchlist.csv"
STREAMS_06 = OUT / "return_streams" / "profile_return_streams_daily_core_plus_watchlist_compound_velocity.csv"
LIVE_MANIFEST = V2 / "portfolio_live" / "live_manifest.json"
EDGE_UNIVERSE = V2 / "config" / "edge_universe.json"
ASSETS = V2 / "assets"
# Exercise 16.1(a) frequency rule (owner 2026-10-30, s.4P.3): observation period = the largest of these not longer than
# the average interval between bets (bets = AFML p.196 same-side runs, D056), never shorter than 1 day (06 is daily).
ALIGNMENT_PERIODS_DAYS = {"daily": 1.0, "weekly": 7.0, "monthly": 365.25 / 12.0}
SEED = 0
N_OBS, S_LENGTH, REBAL = 520, 260, 22          # AFML Snippet 16.5 hrpMC defaults
BLOCK = 22                                     # G1 circular block length (pre-registered)
M2_SIMS = 1000                                 # MLAM Snippet 7.8 nSims
T4_DRAWS = 1000


def load_07():
    spec = importlib.util.spec_from_file_location("hrp07", V2 / "portfolio_allocator" / "07_hrp_linkage_robustness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M07 = load_07()


def deployable_edges() -> list[str]:
    u = json.loads(EDGE_UNIVERSE.read_text(encoding="utf-8"))
    return [f"{e['asset']}/{e['family']}_{e['side']}" for e in u["edges"] if e.get("enabled", True)]


# --------------------------------------------------------------------------- allocation methods (cov, corr, mu, n_obs)

def opt_port(cov: np.ndarray, mu: np.ndarray | None = None) -> np.ndarray:
    """MLAM optPort (Section 2): w = V^-1 mu / 1'V^-1 mu, mu = 1 for the minimum-variance portfolio."""
    try:
        inv = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        inv = np.linalg.pinv(cov)
    ones = np.ones(cov.shape[0])
    m = ones if mu is None else np.asarray(mu, dtype=float)
    w = inv @ m
    return w / (ones @ w)


def ivp(cov: np.ndarray) -> np.ndarray:
    d = 1.0 / np.clip(np.diag(cov), 1e-12, None)
    return d / d.sum()


def cla_min_variance(cov: np.ndarray) -> np.ndarray:
    """Long-only (0 <= w <= 1, sum 1) minimum variance = CLA's last turning point (Snippet 16.5 getCLA); exact by active-set
    enumeration (with sum 1 and w >= 0 the upper bound never binds)."""
    n = cov.shape[0]
    best, best_var = None, np.inf
    for k in range(1, n + 1):
        for s in itertools.combinations(range(n), k):
            s = list(s)
            w_s = opt_port(cov[np.ix_(s, s)])
            if np.all(w_s >= -1e-12):
                w = np.zeros(n)
                w[s] = np.clip(w_s, 0.0, None)
                w /= w.sum()
                v = float(w @ cov @ w)
                if v < best_var - 1e-18:
                    best, best_var = w, v
    return best


def _quasi_diag_book(link: np.ndarray) -> list[int]:
    return M07.get_quasi_diag(link)


def hrp(cov: np.ndarray, corr: np.ndarray, *, tilde: bool) -> np.ndarray:
    """Snippet 16.4 stages 1-3 on (cov, corr). tilde=True: book (sch.linkage on the square D = single on d~);
    tilde=False: linkage on the condensed d (07's call)."""
    labels = [str(i) for i in range(cov.shape[0])]
    dist = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, None))
    np.fill_diagonal(dist, 0.0)
    if tilde:
        link = sch.linkage(dist, "single")                  # 2-D input = observation vectors -> Euclidean d~
    else:
        link = sch.linkage(squareform((dist + dist.T) / 2.0, checks=False), "single")
    order = [labels[i] for i in _quasi_diag_book(link)]
    w = M07.recursive_bisection(pd.DataFrame(cov, index=labels, columns=labels), order)
    return w.reindex(labels).to_numpy()


def hrp_current_parts(cov: np.ndarray, corr: np.ndarray, n_obs: int, x: np.ndarray) -> dict[str, np.ndarray]:
    """07's denoise + conditional detone (rule mlam_ch2_book_v2) -> (cov for weights, corr for clustering).
    x = the T x N returns behind (cov, corr), used by 07's bootstrap market-component test."""
    labels = [str(i) for i in range(cov.shape[0])]
    cdf = M07.clean_cov(pd.DataFrame(cov, index=labels, columns=labels))
    rdf = M07.clean_corr(pd.DataFrame(corr, index=labels, columns=labels))
    dn = M07.denoise_and_detone(rdf, cdf, int(n_obs), x)
    return {"cov": dn["cov_for_weights"].to_numpy(), "corr_cl": dn["corr_for_clustering"].to_numpy(),
            "applied": bool(dn["diagnostics"].get("applied", False))}


def hrp_current(cov: np.ndarray, corr: np.ndarray, n_obs: int, x: np.ndarray, *, tilde: bool = True,
                denoise: bool = True) -> np.ndarray:
    """07 as deployed: linkage on d~ (tilde=True, s.4P.3) after 07's denoise/detone."""
    labels = [str(i) for i in range(cov.shape[0])]
    if denoise:
        p = hrp_current_parts(cov, corr, n_obs, x)
        cov_w, corr_cl = p["cov"], p["corr_cl"]
    else:
        cov_w = M07.clean_cov(pd.DataFrame(cov, index=labels, columns=labels)).to_numpy()
        corr_cl = M07.clean_corr(pd.DataFrame(corr, index=labels, columns=labels)).to_numpy()
    dist = M07.correl_distance(pd.DataFrame(corr_cl, index=labels, columns=labels)).to_numpy()
    if tilde:
        link = sch.linkage(dist, "single")
    else:
        link = sch.linkage(squareform(dist, checks=False), "single")
    order = [labels[i] for i in M07.get_quasi_diag(link)]
    w = M07.recursive_bisection(pd.DataFrame(cov_w, index=labels, columns=labels), order)
    return (w.reindex(labels) / w.sum()).to_numpy()


def nco(cov: np.ndarray, mu: np.ndarray | None = None, clusters: dict | None = None) -> np.ndarray:
    """MLAM Snippet 7.6 optPort_nco (maxNumClusters = N/2, n_init 10)."""
    n = cov.shape[0]
    labels = list(range(n))
    if clusters is None:
        clusters = nco_clusters(cov)
    w_intra = np.zeros((n, len(clusters)))
    for j, (_, items) in enumerate(sorted(clusters.items())):
        items = list(items)
        mu_ = None if mu is None else np.asarray(mu)[items]
        w_intra[items, j] = opt_port(cov[np.ix_(items, items)], mu_)
    cov_ = w_intra.T @ cov @ w_intra
    mu_ = None if mu is None else w_intra.T @ np.asarray(mu)
    w_inter = opt_port(cov_, mu_)
    return w_intra @ w_inter


def nco_clusters(cov: np.ndarray) -> dict:
    n = cov.shape[0]
    corr = pd.DataFrame(cov_to_corr(cov), index=range(n), columns=range(n))
    _, cl, _ = cluster_kmeans_base(corr, max_num_clusters=n // 2, n_init=10, random_state=SEED)
    return cl


MINVAR_METHODS = ["EW", "IVP", "HRP_book", "HRP_current", "CLA_minvar_longonly", "Markowitz_minvar", "NCO_minvar"]
MAXSR_METHODS = ["Markowitz_maxsharpe", "NCO_maxsharpe"]
ALL_METHODS = MINVAR_METHODS + MAXSR_METHODS


def all_weights(cov: np.ndarray, corr: np.ndarray, mu: np.ndarray, n_obs: int, x: np.ndarray) -> dict[str, np.ndarray]:
    """x = the returns sample for 07's bootstrap market test (T4 perturbs (cov, corr) only, so it passes the base sample)."""
    n = cov.shape[0]
    cl = nco_clusters(cov)
    return {
        "EW": np.full(n, 1.0 / n),
        "IVP": ivp(cov),
        "HRP_book": hrp(cov, corr, tilde=True),
        "HRP_current": hrp_current(cov, corr, n_obs, x),
        "CLA_minvar_longonly": cla_min_variance(cov),
        "Markowitz_minvar": opt_port(cov),
        "NCO_minvar": nco(cov, None, cl),
        "Markowitz_maxsharpe": opt_port(cov, mu),
        "NCO_maxsharpe": nco(cov, mu, cl),
    }


def window_inputs(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cov = np.cov(x, rowvar=False)
    sd = np.sqrt(np.clip(np.diag(cov), 1e-24, None))
    corr = np.clip(cov / np.outer(sd, sd), -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)
    return cov, corr, x.mean(axis=0)


# --------------------------------------------------------------------------- streams (T2 alignments)

def load_trades(edges: list[str]) -> pd.DataFrame:
    t = pd.read_csv(LEDGER, usecols=["edge_id", "asset", "side_value", "entry_time", "exit_time", "entry_price",
                                     "exit_price", "stop_distance_price", "executed_r"], float_precision="round_trip")
    t = t[t["edge_id"].isin(edges)].copy().rename(columns={"side_value": "side"})
    for c in ("entry_time", "exit_time"):
        t[c] = pd.to_datetime(t[c], utc=True, format="mixed")
    return t.dropna(subset=["entry_time", "exit_time", "executed_r"])


def stream_exit_day(t: pd.DataFrame, edges: list[str], index: pd.DatetimeIndex) -> pd.DataFrame:
    g = t.assign(d=t["exit_time"].dt.floor("D")).groupby(["d", "edge_id"])["executed_r"].sum().unstack()
    return g.reindex(index).reindex(columns=edges).fillna(0.0)


def stream_prorated(t: pd.DataFrame, edges: list[str], index: pd.DatetimeIndex) -> pd.DataFrame:
    out = pd.DataFrame(0.0, index=index, columns=edges)
    pos = {d: i for i, d in enumerate(index)}
    arr = out.to_numpy()
    for e_i, e in enumerate(edges):
        s = t[t["edge_id"] == e]
        d0 = s["entry_time"].dt.floor("D")
        d1 = s["exit_time"].dt.floor("D")
        for a, b, r in zip(d0, d1, s["executed_r"].to_numpy(dtype=float)):
            n = int((b - a).days) + 1
            i0 = pos[a]
            arr[i0:i0 + n, e_i] += r / n
    return pd.DataFrame(arr, index=index, columns=edges)


def load_day_closes(asset: str) -> tuple[np.ndarray, np.ndarray]:
    """(bar close times as int64 ns UTC, mid Close) of the asset's research bars (Datetime = bar close)."""
    f = next((ASSETS / asset / "bars" / "standard_quote_tick").glob("*_standard_quote_tick_bars.csv"))
    b = pd.read_csv(f, usecols=["Datetime", "Close"], float_precision="round_trip")
    tt = _ns(b["Datetime"])
    o = np.argsort(tt, kind="stable")
    return tt[o], b["Close"].to_numpy(dtype=float)[o]


def _ns(x: pd.Series) -> np.ndarray:
    idx = pd.DatetimeIndex(pd.to_datetime(x, utc=True, format="mixed")).as_unit("ns")
    return idx.asi8


def stream_mtm(t: pd.DataFrame, edges: list[str], index: pd.DatetimeIndex) -> pd.DataFrame:
    """E: daily mark-to-market in R. An open trade is marked at the last bar close of each UTC day after its entry:
    day R = side * (mark - previous mark) / stop_distance_price, the first previous mark being entry_price; the exit day
    takes the move to exit_price plus executed_r - side * (exit_price - entry_price) / stop_distance_price (spread, swap,
    commission), so each trade's daily R sums exactly to its executed_r. A day without a bar after the previous mark
    books 0."""
    out = pd.DataFrame(0.0, index=index, columns=edges)
    arr = out.to_numpy()
    pos = {d: i for i, d in enumerate(index)}
    day = np.int64(86_400_000_000_000)
    closes = {a: load_day_closes(a) for a in sorted(t["asset"].unique())}
    for e_i, e in enumerate(edges):
        s = t[t["edge_id"] == e]
        bt, bc = closes[s["asset"].iloc[0]]
        te, tx = _ns(s["entry_time"]), _ns(s["exit_time"])
        cols = zip(te, tx, s["entry_time"].dt.floor("D"), s["side"].to_numpy(float), s["entry_price"].to_numpy(float),
                   s["exit_price"].to_numpy(float), s["stop_distance_price"].to_numpy(float), s["executed_r"].to_numpy(float))
        for a_ns, b_ns, d0, side, p0, p1, sd, r in cols:
            i0 = pos[d0]
            d0_ns = np.int64(d0.value)
            n_days = int((b_ns - d0_ns) // day)          # full UTC days before the exit day
            prev = p0
            for k in range(n_days):
                j = int(np.searchsorted(bt, d0_ns + (k + 1) * day, side="left")) - 1
                if j >= 0 and bt[j] > a_ns:
                    arr[i0 + k, e_i] += side * (bc[j] - prev) / sd
                    prev = bc[j]
            arr[i0 + n_days, e_i] += side * (p1 - prev) / sd + (r - side * (p1 - p0) / sd)
    return pd.DataFrame(arr, index=index, columns=edges)


def bet_frequency_rule(t: pd.DataFrame, edges: list[str]) -> dict[str, Any]:
    """Exercise 16.1(a): per edge AFML s.14.3 bets and bet frequency (bets / year, ch15.general_and_performance_stats);
    the average frequency over edges -> average interval between bets -> ALIGNMENT_PERIODS_DAYS rule."""
    per = {}
    for e in edges:
        g = ch15.general_and_performance_stats(t[t["edge_id"] == e])
        per[e] = {k: g[k] for k in ("n_trades", "n_bets", "frequency_bets_per_year", "avg_holding_period_days", "years")}
    avg = float(np.mean([v["frequency_bets_per_year"] for v in per.values()]))
    interval = 365.25 / avg
    fit = [k for k, d in ALIGNMENT_PERIODS_DAYS.items() if d <= interval]
    period = max(fit, key=ALIGNMENT_PERIODS_DAYS.get) if fit else "daily"
    return {"per_edge": per, "average_bets_per_year": avg, "average_interval_between_bets_days": interval,
            "periods_days": ALIGNMENT_PERIODS_DAYS, "selected_period": period}


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("W-SUN").sum()


def stream_diagnostics(df: pd.DataFrame) -> dict[str, Any]:
    corr = df.corr()
    ev = np.linalg.eigvalsh(corr.to_numpy())
    return {"T": int(len(df)), "zero_share": {c: round(float((df[c] == 0).mean()), 4) for c in df.columns},
            "corr": corr.round(4).to_dict(), "corr_condition_number": float(ev.max() / ev.min()),
            "corr_eigenvalues": [round(float(v), 4) for v in ev]}


def hrp_trace(df: pd.DataFrame, *, tilde: bool, denoise: bool) -> dict[str, Any]:
    cov, corr, _ = window_inputs(df.to_numpy())
    cols = list(df.columns)
    labels = [str(i) for i in range(len(cols))]
    if denoise:
        p = hrp_current_parts(cov, corr, len(df), df.to_numpy())
        cov_w, corr_cl, applied = p["cov"], p["corr_cl"], p["applied"]
    else:
        cov_w, corr_cl, applied = cov, corr, False
    dist = np.sqrt(np.clip((1.0 - corr_cl) / 2.0, 0.0, None))
    np.fill_diagonal(dist, 0.0)
    link = sch.linkage(dist, "single") if tilde else sch.linkage(squareform((dist + dist.T) / 2, checks=False), "single")
    order = [cols[i] for i in M07.get_quasi_diag(link)]
    w = M07.recursive_bisection(pd.DataFrame(cov_w, index=labels, columns=labels), [labels[cols.index(c)] for c in order])
    w = (w / w.sum()).reindex(labels).to_numpy()
    return {"weights": dict(zip(cols, map(float, w))), "quasi_diag_order": order, "denoise_applied": applied,
            "linkage": np.round(link, 6).tolist()}


# --------------------------------------------------------------------------- T3 Monte Carlo

def _draw(gen: str, rng: np.random.Generator, x: np.ndarray, mu0: np.ndarray, cov0: np.ndarray, n: int) -> np.ndarray:
    if gen == "G1_block_bootstrap":
        t = x.shape[0]
        starts = rng.integers(0, t, size=int(np.ceil(n / BLOCK)))
        idx = np.concatenate([(s + np.arange(BLOCK)) % t for s in starts])[:n]
        return x[idx]
    return rng.multivariate_normal(mu0, cov0, size=n)


def _m1_chunk(args: tuple) -> dict[str, list[float]]:
    gen, x, seed, n_iter = args
    rng = np.random.default_rng(seed)
    mu0, cov0 = x.mean(axis=0), np.cov(x, rowvar=False)
    res: dict[str, list[float]] = {m: [] for m in ALL_METHODS}
    res.update({f"{m}__oos_daily_var": [] for m in ALL_METHODS})
    for _ in range(n_iter):
        xs = _draw(gen, rng, x, mu0, cov0, N_OBS)
        r = {m: [] for m in ALL_METHODS}
        for p in range(S_LENGTH, N_OBS, REBAL):
            cov, corr, mu = window_inputs(xs[p - S_LENGTH:p])
            ws = all_weights(cov, corr, mu, S_LENGTH, xs[p - S_LENGTH:p])
            oos = xs[p:p + REBAL]
            for m in ALL_METHODS:
                r[m].append(oos @ ws[m])
        for m in ALL_METHODS:
            rr = np.concatenate(r[m])
            res[m].append(float(rr.sum()))
            res[f"{m}__oos_daily_var"].append(float(rr.var(ddof=1)))
    return res


def run_m1(x: np.ndarray, gen: str, iters: int, workers: int, seed: int) -> dict[str, Any]:
    chunks = max(1, workers * 4)
    per = [iters // chunks + (1 if i < iters % chunks else 0) for i in range(chunks)]
    seeds = np.random.SeedSequence(seed).spawn(chunks)
    args = [(gen, x, s, n) for s, n in zip(seeds, per) if n > 0]
    merged: dict[str, list[float]] = {}
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers, initializer=_below_normal) as ex:
            parts = list(ex.map(_m1_chunk, args))
    else:
        parts = [_m1_chunk(a) for a in args]
    for p in parts:
        for k, v in p.items():
            merged.setdefault(k, []).extend(v)
    stats = pd.DataFrame({m: merged[m] for m in ALL_METHODS})
    dvar = pd.DataFrame({m: merged[f"{m}__oos_daily_var"] for m in ALL_METHODS})
    var = stats.var(ddof=1)
    rng = np.random.default_rng(seed + 1)
    boot = []
    n = len(stats)
    arr = stats.to_numpy()
    for _ in range(1000):
        i = rng.integers(0, n, size=n)
        v = arr[i].var(axis=0, ddof=1)
        boot.append(v)
    boot = np.asarray(boot)
    cols = list(stats.columns)
    out = {}
    for j, m in enumerate(cols):
        ratio_hb = boot[:, j] / boot[:, cols.index("HRP_book")] - 1.0
        ratio_hc = boot[:, j] / boot[:, cols.index("HRP_current")] - 1.0
        out[m] = {"terminal_pnl_mean_R": float(stats[m].mean()), "terminal_pnl_std_R": float(stats[m].std(ddof=1)),
                  "terminal_pnl_var": float(var[m]),
                  "var_ratio_vs_HRP_book_minus_1": float(var[m] / var["HRP_book"] - 1.0),
                  "var_ratio_vs_HRP_book_ci95": [float(np.quantile(ratio_hb, .025)), float(np.quantile(ratio_hb, .975))],
                  "var_ratio_vs_HRP_current_minus_1": float(var[m] / var["HRP_current"] - 1.0),
                  "var_ratio_vs_HRP_current_ci95": [float(np.quantile(ratio_hc, .025)), float(np.quantile(ratio_hc, .975))],
                  "mean_oos_daily_var": float(dvar[m].mean()),
                  "terminal_sharpe_like": float(stats[m].mean() / stats[m].std(ddof=1))}
    return {"generator": gen, "iterations": int(n), "n_obs": N_OBS, "s_length": S_LENGTH, "rebal": REBAL, "methods": out}


def run_m2(x: np.ndarray, n_obs: int, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    mu0, cov0 = x.mean(axis=0), np.cov(x, rowvar=False)
    sd0 = np.sqrt(np.diag(cov0))
    corr0 = cov0 / np.outer(sd0, sd0)
    true_own = all_weights(cov0, corr0, mu0, len(x), x)
    true_mv, true_ms = opt_port(cov0), opt_port(cov0, mu0)
    err_book = {m: [] for m in ALL_METHODS}
    err_own = {m: [] for m in ALL_METHODS}
    for _ in range(M2_SIMS):
        xs = rng.multivariate_normal(mu0, cov0, size=n_obs)
        cov, corr, mu = window_inputs(xs)
        ws = all_weights(cov, corr, mu, n_obs, xs)
        for m in ALL_METHODS:
            ref = true_ms if m in MAXSR_METHODS else true_mv
            err_book[m].append((ws[m] - ref) ** 2)
            err_own[m].append((ws[m] - true_own[m]) ** 2)
    return {"n_obs": n_obs, "sims": M2_SIMS,
            "true_minvar_weights": true_mv.tolist(), "true_maxsharpe_weights": true_ms.tolist(),
            "true_own_weights": {m: v.tolist() for m, v in true_own.items()},
            "rmse_vs_book_target": {m: float(np.mean(np.concatenate(v)) ** .5) for m, v in err_book.items()},
            "rmse_vs_own_true_weights": {m: float(np.mean(np.concatenate(v)) ** .5) for m, v in err_own.items()}}


# --------------------------------------------------------------------------- T4

def run_t4(x: np.ndarray, seed: int) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    cov = np.cov(x, rowvar=False)
    mu = x.mean(axis=0)
    sd = np.sqrt(np.diag(cov))
    base = all_weights(cov, cov / np.outer(sd, sd), mu, len(x), x)
    lam, w_vec = np.linalg.eigh(cov)
    n = len(lam)
    rmse = {m: [] for m in ALL_METHODS}
    mx = {m: [] for m in ALL_METHODS}
    for _ in range(T4_DRAWS):
        eps = rng.uniform(0.0, 1.0, size=n)
        lam_t = n * eps * lam / eps.sum()
        v_t = w_vec @ np.diag(lam_t) @ np.linalg.inv(w_vec)
        v_t = (v_t + v_t.T) / 2.0
        s = np.sqrt(np.clip(np.diag(v_t), 1e-24, None))
        c_t = np.clip(v_t / np.outer(s, s), -1, 1)
        np.fill_diagonal(c_t, 1.0)
        ws = all_weights(v_t, c_t, mu, len(x), x)
        for m in ALL_METHODS:
            d = ws[m] - base[m]
            rmse[m].append(float(np.sqrt(np.mean(d ** 2))))
            mx[m].append(float(np.max(np.abs(d))))
    return {"draws": T4_DRAWS, "base_weights": {m: v.tolist() for m, v in base.items()},
            "mean_rmse_change": {m: float(np.mean(v)) for m, v in rmse.items()},
            "mean_max_abs_change": {m: float(np.mean(v)) for m, v in mx.items()},
            "ranking_most_affected_first": sorted(rmse, key=lambda m: -np.mean(rmse[m]))}


def _below_normal() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        except Exception:
            pass
    for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[v] = "1"


# --------------------------------------------------------------------------- main

def sanity(edges: list[str], trades: pd.DataFrame) -> dict[str, Any]:
    s06 = pd.read_csv(STREAMS_06, float_precision="round_trip")
    s06["date"] = to_datetime_iso(s06["date"], utc=True)
    s06 = s06.set_index("date").sort_index()
    m06 = json.loads((STREAMS_06.parent / "return_streams_manifest.json").read_text(encoding="utf-8"))
    booking = m06.get("date_aggregation", "exit_date")
    rebuild = stream_mtm(trades, edges, s06.index) if booking == "mark_to_market_daily" else stream_exit_day(trades, edges, s06.index)
    diff = float(np.abs(s06[edges].to_numpy() - rebuild.to_numpy()).max())
    hrp6 = M07.hrp_allocation(s06, "single")["weights"]
    man = json.loads(LIVE_MANIFEST.read_text(encoding="utf-8"))
    live = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "edge_id" in o and "portfolio_weight" in o:
                live[o["edge_id"]] = float(o["portfolio_weight"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(man)
    x6 = s06[edges].to_numpy(dtype="float64")
    cov6, corr6, _ = window_inputs(x6)
    tool6 = dict(zip(edges, hrp_current(cov6, corr6, len(x6), x6)))
    hrp_e = M07.hrp_allocation(s06[edges], "single")["weights"]       # same columns as the tool
    tdiff = float(max(abs(float(hrp_e[k]) - float(v)) for k, v in tool6.items()))
    wdiff = max(abs(float(hrp6[k]) - v) for k, v in live.items()) if live else float("nan")
    return {"booking_06": booking, "stream_rebuild_max_abs_diff_vs_06": diff, "stream_rebuild_ok": diff < 1e-9,
            "tool_hrp_current_vs_07_max_abs_diff": tdiff, "tool_reproduces_07": tdiff < 1e-9,
            "hrp07_vs_live_manifest_max_abs_diff_info_only": wdiff,
            "hrp07_weights_tested_edges": {k: float(v) for k, v in hrp_e.items()},
            "hrp07_weights_all_06_columns": {k: float(v) for k, v in hrp6.items()}, "live_manifest_weights": live}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--iters", type=int, default=10_000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--skip-m1", action="store_true")
    ap.add_argument("--t3-alignments", default="A_exit_day_daily,B_prorated_daily,E_mtm_daily")
    a = ap.parse_args(argv)
    _below_normal()
    t0 = time.time()
    edges = deployable_edges()
    trades = load_trades(edges)
    s06 = pd.read_csv(STREAMS_06, usecols=["date"])
    index = pd.DatetimeIndex(to_datetime_iso(s06["date"], utc=True)).sort_values()
    first = min(index.min(), trades["entry_time"].dt.floor("D").min())
    full = pd.date_range(first, index.max(), freq="D", tz="UTC")
    al = {"A_exit_day_daily": stream_exit_day(trades, edges, index)}
    res: dict[str, Any] = {"edges": edges, "preregistration": "docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4P.1", "seed": SEED}
    res["S0"] = sanity(edges, trades)
    print(json.dumps({k: v for k, v in res["S0"].items() if "weights" not in k}), flush=True)
    if not (res["S0"]["stream_rebuild_ok"] and res["S0"]["tool_reproduces_07"]):
        res["error"] = "S0 sanity failed -- no result is read"
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        return 1
    al["B_prorated_daily"] = stream_prorated(trades, edges, full)
    al["C_weekly"] = weekly(al["A_exit_day_daily"])
    al["D_weekly_prorated"] = weekly(al["B_prorated_daily"])
    al["E_mtm_daily"] = stream_mtm(trades, edges, full)
    al["F_weekly_mtm"] = weekly(al["E_mtm_daily"])
    res["alignment_rule"] = bet_frequency_rule(trades, edges)
    res["E_mtm_check"] = {
        "max_abs_total_r_diff_vs_ledger": float(max(abs(al["E_mtm_daily"][e].sum()
                                                       - trades.loc[trades.edge_id == e, "executed_r"].sum())
                                                   for e in edges)),
        "daily_corr_A_vs_E_per_edge": {e: float(np.corrcoef(al["A_exit_day_daily"][e].reindex(full).fillna(0.0),
                                                           al["E_mtm_daily"][e])[0, 1]) for e in edges}}
    print(json.dumps({"alignment_rule": res["alignment_rule"], "E_mtm_check": res["E_mtm_check"]}, default=str), flush=True)
    hold = (trades["exit_time"] - trades["entry_time"]).dt.total_seconds() / 86400.0
    weeks = (index.max() - index.min()).days / 7.0
    res["T2_bet_frequency"] = {e: {"trades": int((trades.edge_id == e).sum()),
                                   "trades_per_week": float((trades.edge_id == e).sum() / weeks),
                                   "mean_holding_days": float(hold[trades.edge_id == e].mean())} for e in edges}

    res["T1"] = {}
    for tilde, dn in itertools.product([False, True], [True, False]):
        key = f"{'d_tilde' if tilde else 'd'}__{'denoise_on' if dn else 'denoise_off'}"
        res["T1"][key] = hrp_trace(al["A_exit_day_daily"], tilde=tilde, denoise=dn)
    cur, book = res["T1"]["d_tilde__denoise_on"]["weights"], res["T1"]["d_tilde__denoise_off"]["weights"]
    res["T1"]["max_abs_diff_book_vs_current"] = max(abs(cur[e] - book[e]) for e in edges)

    res["T2"] = {}
    for name, df in al.items():
        cov, corr, mu = window_inputs(df.to_numpy())
        ws = all_weights(cov, corr, mu, len(df), df.to_numpy())
        res["T2"][name] = {**stream_diagnostics(df),
                           "weights": {m: dict(zip(edges, map(float, w))) for m, w in ws.items()},
                           "nco_clusters": {str(k): [edges[i] for i in v] for k, v in nco_clusters(cov).items()},
                           "hrp_book_trace": hrp_trace(df, tilde=True, denoise=False),
                           "hrp_current_trace": hrp_trace(df, tilde=True, denoise=True)}
    print("T1/T2 done", round(time.time() - t0, 1), flush=True)

    res["T3"], res["T4"] = {}, {}
    for name in a.t3_alignments.split(","):
        x = al[name].to_numpy()
        res["T4"][name] = run_t4(x, SEED)
        res["T3"][name] = {"M2": {str(n): run_m2(x, n, SEED) for n in (S_LENGTH, len(x))}}
        print("T4/M2", name, round(time.time() - t0, 1), flush=True)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        if not a.skip_m1:
            res["T3"][name]["M1"] = {g: run_m1(x, g, a.iters, a.workers, SEED)
                                     for g in ("G1_block_bootstrap", "G2_gaussian")}
            print("M1", name, round(time.time() - t0, 1), flush=True)
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    res["elapsed_s"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print("written", a.out, res["elapsed_s"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
