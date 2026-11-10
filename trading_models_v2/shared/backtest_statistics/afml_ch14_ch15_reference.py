"""Canonical AFML Chapter 14 (Backtest Statistics) and Chapter 15
(Understanding Strategy Risk) reference functions.

Source: Marcos Lopez de Prado, Advances in Financial Machine Learning
(AFML, 2018). Every formula below is transcribed directly from the book
(page-cited per function), not from a secondhand summary.

This module is a pure-function reference, analogous in spirit to
shared/sizing/afml_ch10_reference.py: it implements the book's own
formulas without any Quantara-specific extension, so callers can apply
them to Quantara's R-space realized-return artifacts without
reimplementing the mathematics.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


class AFMLBacktestStatisticsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# AFML Ch.14 SS14.5.1 "Returns Concentration" (p.199-200)
# ---------------------------------------------------------------------------


def get_hhi(returns: pd.Series | np.ndarray) -> float:
    """AFML Snippet 14.3 getHHI: Herfindahl-Hirschman Index of a series of
    weights, rescaled to [0,1] where 0 = perfectly uniform across all
    observations and 1 = fully concentrated in one. Applied by the book to
    (a) positive returns, (b) negative returns, and (c) the count of bets
    per calendar period (time-between-bets concentration)."""
    r = pd.Series(returns).dropna()
    if r.shape[0] <= 2:
        return float("nan")
    weight = r / r.sum()
    hhi = float((weight ** 2).sum())
    n = r.shape[0]
    hhi = (hhi - 1.0 / n) / (1.0 - 1.0 / n)
    return float(hhi)


def returns_concentration(r: np.ndarray, dates: pd.DatetimeIndex) -> dict[str, float]:
    """AFML p.199-200: HHI on positive returns, HHI on negative returns, and
    HHI on the count of bets per calendar month (time-between-bets
    concentration). r must already be restricted to actual bet/trade
    observations (not a zero-padded daily grid) -- concentration of zeros
    would not measure what the book intends."""
    series = pd.Series(np.asarray(r, dtype="float64"), index=pd.DatetimeIndex(dates))
    pos_hhi = get_hhi(series[series > 0.0])
    neg_hhi = get_hhi(series[series < 0.0].abs())
    monthly_counts = series.groupby(series.index.tz_localize(None).to_period("M")).count()
    time_hhi = get_hhi(monthly_counts)
    return {
        "hhi_positive_returns": pos_hhi,
        "hhi_negative_returns": neg_hhi,
        "hhi_time_between_bets": time_hhi,
    }


# ---------------------------------------------------------------------------
# AFML Ch.14 SS14.5.2 "Drawdown and Time under Water" (p.201)
# ---------------------------------------------------------------------------


def compute_dd_tuw(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """AFML Snippet 14.4 computeDD_TuW, adapted for R-space equity (the book's
    own dollars=False branch is for percentage returns; Quantara's R-multiple
    equity curve is additive like a dollar P&L, so this uses the book's
    dollars=True branch: dd = hwm - trough, in R units, consistent with
    max_drawdown_from_r used elsewhere in this project).

    series: a cumulative R equity curve (cumsum of realized R), indexed by
    the event/exit time of each observation (real trade dates, not a
    zero-padded daily grid).

    Returns (dd, tuw): dd is the worst drawdown observed before each new
    high-water-mark is set (indexed by the time that HWM was reached); tuw is
    the calendar time (in years) between consecutive high-water-marks --
    i.e. how long the equity curve spent "under water" before recovering."""
    df0 = series.to_frame("pnl")
    df0["hwm"] = series.expanding().max()
    df1 = df0.groupby("hwm").min().reset_index()
    df1.columns = ["hwm", "min"]
    df1.index = df0["hwm"].drop_duplicates(keep="first").index
    df1 = df1[df1["hwm"] > df1["min"]]
    if df1.empty:
        return pd.Series(dtype="float64"), pd.Series(dtype="float64")
    dd = df1["hwm"] - df1["min"]
    if len(df1.index) > 1:
        deltas = df1.index[1:] - df1.index[:-1]
        tuw = np.array([td.total_seconds() / (365.25 * 86400.0) for td in deltas], dtype="float64")
        tuw = pd.Series(tuw, index=df1.index[:-1])
    else:
        tuw = pd.Series(dtype="float64")
    return dd, tuw


def drawdown_time_under_water_stats(series: pd.Series) -> dict[str, float]:
    """AFML p.202: 95th-percentile DD and 95th-percentile Time under Water,
    the two derived statistics the book names explicitly for performance
    evaluation, plus the worst (max) of each for context."""
    dd, tuw = compute_dd_tuw(series)
    return {
        "n_drawdown_cycles": int(len(dd)),
        "max_drawdown_r": float(dd.max()) if len(dd) else float("nan"),
        "p95_drawdown_r": float(dd.quantile(0.95)) if len(dd) else float("nan"),
        "n_time_under_water_cycles": int(len(tuw)),
        "max_time_under_water_years": float(tuw.max()) if len(tuw) else float("nan"),
        "p95_time_under_water_years": float(tuw.quantile(0.95)) if len(tuw) else float("nan"),
    }


# ---------------------------------------------------------------------------
# AFML Ch.15 "Understanding Strategy Risk" (pp.211-220). Rewritten 2026-10-27 (AFML Ch.15 walkthrough, owner-approved):
# book formulas with correct section / snippet citations, the s.15.4.1 bootstrap algorithm on BET outcomes, and the
# exercise 15.1-15.2 sensitivities.
# ---------------------------------------------------------------------------


def sr_symmetric(p: float, n: float) -> float:
    """AFML s.15.2 (p.212): symmetric payouts, theta[p, n] = (2p - 1) / (2 sqrt(p(1 - p))) sqrt(n) (pi cancels out)."""
    return float((2 * p - 1) / (2 * np.sqrt(p * (1 - p))) * np.sqrt(n))


def implied_precision_symmetric(n: float, t_sr: float) -> float:
    """AFML s.15.2 (p.213): p = (1 + sqrt(1 - n / (theta^2 + n))) / 2."""
    return float(0.5 * (1 + np.sqrt(1 - n / (t_sr ** 2 + n))))


def bin_sr(sl: float, pt: float, freq: float, p: float) -> float:
    """AFML s.15.3 (p.213): asymmetric payouts, theta[p, n, pi-, pi+] = ((pi+ - pi-) p + pi-) / ((pi+ - pi-) sqrt(p(1 - p)))
    sqrt(n); sl = pi- (< 0), pt = pi+ (> 0), freq = n bets per year. (Snippet 15.4 calls it binSR.)"""
    return float(((pt - sl) * p + sl) / ((pt - sl) * np.sqrt(p * (1 - p))) * np.sqrt(freq))


def bin_hr(sl: float, pt: float, freq: float, t_sr: float) -> float:
    """AFML s.15.3 (p.214), Snippet 15.3 binHR: the minimum precision p for a trading rule {sl, pt, freq} to achieve the
    annualised Sharpe ratio t_sr; p = (-b + sqrt(b^2 - 4ac)) / 2a, a = (n + theta^2)(pt - sl)^2,
    b = [2 n sl - theta^2 (pt - sl)](pt - sl), c = n sl^2. At t_sr = 0 it reduces to the break-even p = -sl / (pt - sl)."""
    a = (freq + t_sr ** 2) * (pt - sl) ** 2
    b = (2 * freq * sl - t_sr ** 2 * (pt - sl)) * (pt - sl)
    c = freq * sl ** 2
    disc = b ** 2 - 4 * a * c
    # at t_sr = 0 the discriminant is exactly 0 in theory; floating point can leave it slightly negative -> relative check
    if a == 0 or disc < -1e-9 * max(b ** 2, 4 * abs(a * c), 1e-300):
        return float("nan")
    return float((-b + max(disc, 0.0) ** 0.5) / (2.0 * a))


def bin_freq(sl: float, pt: float, p: float, t_sr: float) -> float | None:
    """AFML s.15.3 (pp.215-216), Snippet 15.4 binFreq: bets per year needed for t_sr at precision p;
    freq = (theta (pt - sl))^2 p (1 - p) / ((pt - sl) p + sl)^2, discarded if binSR(sl, pt, freq, p) != t_sr (the book's
    check for the extraneous root of an equation with radicals)."""
    denom = ((pt - sl) * p + sl) ** 2
    if denom == 0:
        return None
    freq = (t_sr * (pt - sl)) ** 2 * p * (1 - p) / denom
    if not np.isfinite(freq) or not np.isclose(bin_sr(sl, pt, freq, p), t_sr):
        return None
    return float(freq)


def mix_gaussians(mu1: float, mu2: float, sigma1: float, sigma2: float, prob1: float, n_obs: int,
                  seed: int | None = None) -> np.ndarray:
    """AFML Snippet 15.5 mixGaussians: random draws from a mixture of two Gaussians (shuffled)."""
    rng = np.random.default_rng(seed)
    ret1 = rng.normal(mu1, sigma1, size=int(n_obs * prob1))
    ret2 = rng.normal(mu2, sigma2, size=int(n_obs) - ret1.shape[0])
    ret = np.append(ret1, ret2)
    rng.shuffle(ret)
    return ret


def psr_vs_annual_target(bet_r: np.ndarray, years: float, t_sr_annual: float) -> float:
    """AFML p.218 / exercises 15.4-15.5: PSR[theta*] (s.14.7.2 p.203) on the same bet outcomes, the complementary
    method to P[p < p_theta*]; theta* (annual) is de-annualised to the per-bet frequency n."""
    x = np.asarray(bet_r, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 4 or years <= 0:
        return float("nan")
    n = x.size / years
    sr = float(np.mean(x) / np.std(x, ddof=1))
    g3, g4 = float(stats.skew(x)), float(stats.kurtosis(x, fisher=False))
    den = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr ** 2
    return float(stats.norm.cdf((sr - t_sr_annual / np.sqrt(n)) * np.sqrt(x.size - 1) / np.sqrt(den))) if den > 0 else float("nan")


def prob_failure_snippet_approximation(p: float, p_theta: float) -> float:
    """AFML Snippet 15.5 probFailure as printed: norm.cdf(thresP, p, p(1 - p)) "approximation to bootstrap". Kept only as
    a flagged diagnostic: its spread p(1 - p) is not the spread of the s.15.4.1 bootstrap it approximates
    (sqrt(p(1 - p) / floor(n k))) -- walkthrough s.4O. Not used for any decision."""
    return float(stats.norm.cdf(p_theta, p, p * (1 - p)))


def bet_outcome_parameters(bet_r: np.ndarray, years: float) -> dict[str, float]:
    """AFML s.15.4.1 step 1-2 (p.217): pi- = E[pi | pi <= 0], pi+ = E[pi | pi > 0], p = share > 0, n = T / years."""
    x = np.asarray(bet_r, dtype=float)
    x = x[np.isfinite(x)]
    pos, neg = x[x > 0], x[x <= 0]
    return {"T": int(x.size), "years": float(years), "n": float(x.size / years) if years > 0 else float("nan"),
            "p": float(pos.size / x.size) if x.size else float("nan"),
            "pi_plus": float(pos.mean()) if pos.size else float("nan"),
            "pi_minus": float(neg.mean()) if neg.size else float("nan")}


def prob_precision_below(bet_r: np.ndarray, p_star: float, nk: int, n_boot: int = 10_000, seed: int = 0) -> float:
    """AFML s.15.4.1 steps 3-5: bootstrap n_boot draws of nk bets with replacement, p_i = share > 0, Gaussian KDE f[p],
    return the integral of f[p] below p_star."""
    x = np.asarray(bet_r, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0 or not np.isfinite(p_star):
        return float("nan")
    rng = np.random.default_rng(seed)
    p_i = (x[rng.integers(0, x.size, size=(n_boot, max(int(nk), 1)))] > 0).mean(axis=1)
    if np.ptp(p_i) > 0:
        return float(stats.gaussian_kde(p_i).integrate_box_1d(-np.inf, p_star))
    return float(p_i[0] < p_star)


def prob_strategy_failure(bet_r: np.ndarray, years: float, t_sr: float, k_years: float,
                          n_boot: int = 10_000, seed: int = 0) -> dict[str, Any]:
    """AFML s.15.4.1 (p.217) strategy risk P[p < p_theta*] on a time series of BET outcomes:
    (1) pi-, pi+ (book means); (2) n = T / years; (3) bootstrap: n_boot draws of floor(n k) bets with replacement,
    p_i = share > 0; fit f[p] by a Gaussian KDE; (4) p_theta* = binHR(pi-, pi+, n, theta*); (5) P = integral of f[p] below
    p_theta*. s.15.4.2 (p.217): strategies with P > .05 are disregarded as too risky."""
    prm = bet_outcome_parameters(bet_r, years)
    out: dict[str, Any] = {**prm, "theta_star": float(t_sr), "k_years": float(k_years)}
    x = np.asarray(bet_r, dtype=float)
    x = x[np.isfinite(x)]
    if not (prm["T"] >= 2 and np.isfinite(prm["pi_plus"]) and np.isfinite(prm["pi_minus"]) and prm["n"] > 0):
        return out | {"p_theta_star": float("nan"), "prob_failure": float("nan")}
    p_star = bin_hr(prm["pi_minus"], prm["pi_plus"], prm["n"], t_sr)
    nk = max(int(np.floor(prm["n"] * k_years)), 1)
    risk = prob_precision_below(x, p_star, nk, n_boot=n_boot, seed=seed)
    return out | {
        "theta_observed": bin_sr(prm["pi_minus"], prm["pi_plus"], prm["n"], prm["p"]),
        "p_theta_star": p_star, "precision_margin": float(prm["p"] - p_star), "bootstrap_draws": int(n_boot),
        "bets_per_draw_floor_nk": nk, "prob_failure": risk,
        "prob_failure_normal_sqrt_p1p_over_nk": float(stats.norm.cdf(p_star, prm["p"], np.sqrt(prm["p"] * (1 - prm["p"]) / nk))),
        "prob_failure_snippet_15_5_approx_FLAGGED": prob_failure_snippet_approximation(prm["p"], p_star),
        "psr_vs_theta_star_same_bets": psr_vs_annual_target(x, years, t_sr),
        "passes_0p05": bool(risk <= 0.05),
    }


def strategy_sensitivity(p: float, n: float, sl: float, pt: float, t_sr: float) -> dict[str, Any]:
    """AFML exercises 15.1-15.2 (p.219): viability of {p, n, sl, pt} for target t_sr; required precision (binHR), required
    frequency (binFreq), the profit-taking threshold and the stop-loss that reach t_sr with the other parameters fixed
    (closed form from s.15.3: with c = theta sqrt(p(1-p)) / sqrt(n), pi+ - pi- = pi- / (c - p) for a given pi-, and
    = pi+ / (c - p + 1) for a given pi+), and the elasticity of theta to a 1 % change in each parameter."""
    theta = bin_sr(sl, pt, n, p)
    c = t_sr * np.sqrt(p * (1 - p)) / np.sqrt(n)
    d_given_sl = sl / (c - p) if (c - p) != 0 else float("nan")
    d_given_pt = pt / (c - p + 1) if (c - p + 1) != 0 else float("nan")
    req_pt = sl + d_given_sl if np.isfinite(d_given_sl) and d_given_sl > 0 else float("nan")
    req_sl = pt - d_given_pt if np.isfinite(d_given_pt) and d_given_pt > 0 and pt - d_given_pt < 0 else float("nan")

    def pct(f) -> float:
        base = bin_sr(sl, pt, n, p)
        return float(f / base - 1.0) if base else float("nan")

    return {
        "theta": theta, "viable": bool(theta >= t_sr), "target_theta": float(t_sr),
        "required_precision": bin_hr(sl, pt, n, t_sr), "required_bets_per_year": bin_freq(sl, pt, p, t_sr),
        "required_pt_given_sl": req_pt, "required_sl_given_pt": req_sl,
        "elasticity_theta_p_plus1pct": pct(bin_sr(sl, pt, n, min(p * 1.01, 0.999999))),
        "elasticity_theta_n_plus1pct": pct(bin_sr(sl, pt, n * 1.01, p)),
        "elasticity_theta_pt_plus1pct": pct(bin_sr(sl, pt * 1.01, n, p)),
        "elasticity_theta_sl_1pct_tighter": pct(bin_sr(sl * 0.99, pt, n, p)),
    }


def strategy_risk_report(bet_r: np.ndarray, years: float, t_sr: float, k_years: float,
                         n_boot: int = 10_000, seed: int = 0) -> dict[str, Any]:
    """s.15.4 strategy risk + exercise 15.1-15.2 sensitivities for one edge's bet outcomes."""
    risk = prob_strategy_failure(bet_r, years, t_sr, k_years, n_boot=n_boot, seed=seed)
    if np.isfinite(risk.get("p_theta_star", float("nan"))):
        risk.update({f"sens_{k}": v for k, v in strategy_sensitivity(
            risk["p"], risk["n"], risk["pi_minus"], risk["pi_plus"], t_sr).items()})
    return risk


# ---------------------------------------------------------------------------
# AFML Ch.14 SS14.3 "General Characteristics" (p.196-197), SS14.4 "Performance" (p.198-199),
# SS14.6 "Implementation Shortfall" (p.202-203), SS14.9 "Attribution" (p.207-208).
# Added 2026-10-26 (AFML Ch.14 walkthrough, owner-approved).
# ---------------------------------------------------------------------------


def target_positions_from_trades(trades: pd.DataFrame) -> pd.Series:
    """Target position series tPos (units of one trade) from trade intervals: +side at entry, -side at exit, summed at
    each timestamp. Input columns: entry_time, exit_time, side (+1 / -1)."""
    t = trades.dropna(subset=["entry_time", "exit_time", "side"])
    ev = pd.concat([
        pd.Series(t["side"].to_numpy(dtype=float), index=pd.DatetimeIndex(t["entry_time"])),
        pd.Series(-t["side"].to_numpy(dtype=float), index=pd.DatetimeIndex(t["exit_time"])),
    ])
    return ev.groupby(level=0).sum().sort_index().cumsum().round(12)


def bet_timestamps(t_pos: pd.Series) -> pd.DatetimeIndex:
    """AFML Snippet 14.1: a bet ends when the position is flattened or flipped (p.197)."""
    df0 = t_pos[t_pos == 0].index
    df1 = t_pos.shift(1)
    df1 = df1[df1 != 0].index
    bets = df0.intersection(df1)  # flattening
    df0 = t_pos.iloc[1:] * t_pos.iloc[:-1].values
    bets = bets.union(df0[df0 < 0].index).sort_values()  # flips
    if t_pos.index[-1] not in bets:
        bets = bets.append(t_pos.index[-1:])  # last bet
    return bets


def holding_period_days(t_pos: pd.Series) -> float:
    """AFML Snippet 14.2 getHoldingPeriod: average holding period (days), average-entry-time pairing (p.197)."""
    dts, ws = [], []
    t_entry = 0.0
    p_diff = t_pos.diff()
    t_diff = (t_pos.index - t_pos.index[0]) / np.timedelta64(1, "D")
    for i in range(1, t_pos.shape[0]):
        if p_diff.iloc[i] * t_pos.iloc[i - 1] >= 0:  # increased or unchanged
            if t_pos.iloc[i] != 0:
                t_entry = (t_entry * t_pos.iloc[i - 1] + t_diff[i] * p_diff.iloc[i]) / t_pos.iloc[i]
        else:  # decreased
            if t_pos.iloc[i] * t_pos.iloc[i - 1] < 0:  # flip
                dts.append(t_diff[i] - t_entry)
                ws.append(abs(t_pos.iloc[i - 1]))
                t_entry = t_diff[i]  # reset entry time
            else:
                dts.append(t_diff[i] - t_entry)
                ws.append(abs(p_diff.iloc[i]))
    w = np.asarray(ws, dtype=float)
    return float((np.asarray(dts) * w).sum() / w.sum()) if w.sum() > 0 else float("nan")


def bet_level_returns(trades: pd.DataFrame, t_pos: pd.Series) -> pd.DataFrame:
    """Group trades into bets (AFML p.196: a sequence of positions on the same side is one bet): a trade belongs to the
    first bet end at or after its exit time. One row per bet: side, n_trades, r (sum of the trades' R)."""
    ends = bet_timestamps(t_pos)
    t = trades.sort_values("exit_time").copy()
    pos = np.searchsorted(ends.values, pd.DatetimeIndex(t["exit_time"]).values, side="left")
    t["bet_end"] = ends.values[np.minimum(pos, len(ends) - 1)]
    g = t.groupby("bet_end")
    return pd.DataFrame({"side": g["side"].first(), "n_trades": g.size(), "r": g["executed_r"].sum()})


def general_and_performance_stats(trades: pd.DataFrame) -> dict[str, Any]:
    """AFML s.14.3 (bets, frequency, holding period, ratio of longs, max position) and s.14.4 (PnL, PnL from longs,
    annualised return, hit ratio, average hit / miss -- per BET, as the book defines them). R units replace dollars."""
    t_pos = target_positions_from_trades(trades)
    bets = bet_level_returns(trades, t_pos)
    start, end = pd.Timestamp(trades["entry_time"].min()), pd.Timestamp(trades["exit_time"].max())
    years = (end - start).total_seconds() / (365.25 * 86400.0)
    hits, misses = bets.loc[bets["r"] > 0, "r"], bets.loc[bets["r"] <= 0, "r"]
    return {
        "time_range_start": str(start), "time_range_end": str(end), "years": years,
        "n_trades": int(len(trades)), "n_bets": int(len(bets)),
        "trades_per_bet": float(len(trades) / len(bets)) if len(bets) else float("nan"),
        "frequency_bets_per_year": float(len(bets) / years) if years > 0 else float("nan"),
        "avg_holding_period_days": holding_period_days(t_pos),
        "ratio_of_longs_bets": float((bets["side"] > 0).mean()) if len(bets) else float("nan"),
        "max_position_trades": float(t_pos.abs().max()),
        "pnl_r": float(trades["executed_r"].sum()),
        "pnl_r_from_longs": float(trades.loc[trades["side"] > 0, "executed_r"].sum()),
        "annualised_return_r_per_year": float(trades["executed_r"].sum() / years) if years > 0 else float("nan"),
        "hit_ratio_bets": float((bets["r"] > 0).mean()) if len(bets) else float("nan"),
        "avg_return_hits_r": float(hits.mean()) if len(hits) else float("nan"),
        "avg_return_misses_r": float(misses.mean()) if len(misses) else float("nan"),
    }


def correlation_to_underlying(daily_strategy_r: pd.Series, underlying_close: pd.Series) -> dict[str, float]:
    """AFML p.197: correlation between strategy returns and the underlying's returns, on the underlying's trading days
    (UTC days with a close; strategy days without an exit count as 0)."""
    close = underlying_close.groupby(underlying_close.index.normalize()).last()
    u = np.log(close).diff().dropna()
    s = daily_strategy_r.groupby(daily_strategy_r.index.normalize()).sum().reindex(u.index, fill_value=0.0)
    ok = s.notna() & u.notna()
    if ok.sum() < 3 or s[ok].std() == 0:
        return {"corr_to_underlying": float("nan"), "corr_p_value": float("nan"), "corr_days": int(ok.sum())}
    r, p = stats.pearsonr(s[ok], u[ok])
    return {"corr_to_underlying": float(r), "corr_p_value": float(p), "corr_days": int(ok.sum())}


def implementation_shortfall_stats(net_r: np.ndarray, mid_r: np.ndarray) -> dict[str, float]:
    """AFML s.14.6 in R units: execution cost per trade = mid-path R - executed R (spread, slippage, fees, swap);
    'performance per turnover' = net R per trade (how much costlier execution can get before break-even);
    return on execution costs = total net R / total execution cost (should be a large multiple)."""
    net_r, mid_r = np.asarray(net_r, float), np.asarray(mid_r, float)
    ok = np.isfinite(net_r) & np.isfinite(mid_r)
    cost = mid_r[ok] - net_r[ok]
    total_cost = float(cost.sum())
    return {
        "cost_matched_trades": int(ok.sum()),
        "execution_cost_r_per_trade": float(cost.mean()) if ok.any() else float("nan"),
        "net_r_per_trade": float(net_r[ok].mean()) if ok.any() else float("nan"),
        "return_on_execution_costs": float(net_r[ok].sum() / total_cost) if total_cost > 0 else float("nan"),
    }


def attribution_table(trades: pd.DataFrame, category_cols: list[str]) -> pd.DataFrame:
    """AFML s.14.9 (simplified, flagged): PnL split over disjoint categories of each risk class (every trade in one and
    only one category per class). The book's index-vs-universe excess return needs universe weights we do not have."""
    rows = []
    for col in category_cols:
        for cat, g in trades.groupby(col, dropna=False):
            r = g["executed_r"].astype(float)
            sd = r.std(ddof=1) if len(r) > 1 else float("nan")
            rows.append({"risk_class": col, "category": str(cat), "n_trades": int(len(g)), "pnl_r": float(r.sum()),
                         "mean_r": float(r.mean()), "sr_per_trade": float(r.mean() / sd) if sd and sd > 0 else float("nan")})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# AFML Ch.14 second pass (owner request 2026-10-27: "go through the whole chapter, touch everywhere").
# ---------------------------------------------------------------------------


def bet_concentration(bets: pd.DataFrame) -> dict[str, float]:
    """AFML s.14.5.1 / Snippet 14.3 on BET returns, as the book defines them ("a time series of returns from bets"):
    getHHI(ret[ret >= 0]), getHHI(ret[ret < 0]), getHHI(bets per month). bets: index = bet end time, column r."""
    r = bets["r"].astype(float)
    month = pd.DatetimeIndex(bets.index).tz_localize(None).to_period("M")
    return {"hhi_positive_bets": get_hhi(r[r >= 0]), "hhi_negative_bets": get_hhi(r[r < 0]),
            "hhi_bets_per_month": get_hhi(r.groupby(month).count())}


def sizing_and_capacity_stats(trades: pd.DataFrame, f: float, balance: float) -> dict[str, Any]:
    """AFML s.14.3 average AUM, leverage, maximum dollar position size, annualised turnover and a capacity check, at the
    edge's standalone 04C sizing: target risk = f * afml_bet_size * balance, lots = target / risk_per_1_lot rounded UP to
    the lot step with the broker minimum lot (04C 'ceil_to_lot_step_with_min_lot'); notional (account currency) =
    lots * risk_per_1_lot * entry_price / stop_distance_price. AUM = the constant account balance (no external cash
    flows in a backtest). Capacity is NOT the book's 'highest AUM at target performance' (needs a market-impact model);
    reported instead: the largest lot size needed vs the broker maximum lot."""
    t = trades.dropna(subset=["risk_per_1_lot", "entry_price", "stop_distance_price", "afml_bet_size"]).copy()
    target = float(f) * t["afml_bet_size"].clip(0, 1).astype(float) * balance
    raw = target / t["risk_per_1_lot"].astype(float)
    step, mn = t["broker_lot_step"].astype(float), t["broker_min_lot"].astype(float)
    lots = np.where(target > 0, np.maximum(mn, np.ceil(raw / step - 1e-9) * step), 0.0)
    # plain arrays: a pandas Series here would re-align on its row labels inside pd.Series(..., index=timestamps) -> NaN
    lots = np.asarray(lots, dtype=float)
    notional = np.asarray(lots * t["risk_per_1_lot"].to_numpy(float) * t["entry_price"].to_numpy(float)
                          / t["stop_distance_price"].to_numpy(float), dtype=float)
    ev = pd.concat([pd.Series(notional, index=pd.DatetimeIndex(t["entry_time"])),
                    pd.Series(-notional, index=pd.DatetimeIndex(t["exit_time"]))]).groupby(level=0).sum().sort_index()
    open_notional = ev.cumsum().clip(lower=0)
    dur = np.diff(open_notional.index.values).astype("timedelta64[s]").astype(float)
    avg_open = float((open_notional.values[:-1] * dur).sum() / dur.sum()) if dur.sum() > 0 else float("nan")
    years = (t["exit_time"].max() - t["entry_time"].min()).total_seconds() / (365.25 * 86400.0)
    return {
        "sizing_f_04c": float(f), "aum_balance": float(balance),
        "avg_open_notional": avg_open, "leverage_avg_notional_over_aum": avg_open / balance,
        "max_dollar_position": float(open_notional.max()), "max_position_over_aum": float(open_notional.max()) / balance,
        "annualised_turnover": float(2.0 * notional.sum() / years / balance) if years > 0 else float("nan"),
        "max_lots_single_trade": float(np.max(lots)), "broker_max_lot": float(t["broker_max_lot"].astype(float).min()),
        "capacity_lot_headroom_x": float(t["broker_max_lot"].astype(float).min() / np.max(lots)) if np.max(lots) > 0 else float("nan"),
        "min_lot_binding_share": float(np.mean((target > 0) & (raw < mn))),
    }


def cost_split_stats(oof_taken: pd.DataFrame) -> dict[str, float]:
    """AFML s.14.6 split on the research-firm OOF economics (the only place the components are stored): spread, slippage and
    commission = label_r - executed_r_pre_carry; financing (swap) = executed_r_pre_carry - executed_r (negative = credit)."""
    t = oof_taken.dropna(subset=["label_r", "executed_r_pre_carry", "executed_r"])
    exec_cost = t["label_r"] - t["executed_r_pre_carry"]
    carry = t["executed_r_pre_carry"] - t["executed_r"]
    return {"split_basis_trades": int(len(t)), "spread_slippage_commission_r_per_trade": float(exec_cost.mean()),
            "swap_carry_r_per_trade": float(carry.mean())}


def classification_scores(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    """AFML s.14.8 for the meta-labelling overlay at the live threshold: accuracy, precision, recall, F1, neg log-loss.
    Degenerate cases follow Table 14.1 / scikit-learn (F1 = 0 when nothing is predicted 1). The book's switch-the-labels
    remedy (p.206-207) applies only when positives dominate; the positive rate is reported."""
    y = np.asarray(y, int)
    p = np.clip(np.asarray(p, float), 1e-12, 1 - 1e-12)
    pred = (p >= threshold).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum()); tn = int(((pred == 0) & (y == 0)).sum())
    fp = int(((pred == 1) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * prec * rec / (prec + rec) if (tp + fp) and (tp + fn) and (prec + rec) > 0 else 0.0
    return {"n_events": int(y.size), "positive_rate": float(y.mean()), "take_rate": float(pred.mean()),
            "accuracy": (tp + tn) / y.size, "precision": prec, "recall": rec, "f1": float(f1),
            "neg_log_loss": float(np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
            "positives_dominate": bool(y.mean() > 0.5)}


def attribution_vs_universe(universe: pd.DataFrame, taken_mask: np.ndarray, category_cols: list[str]) -> pd.DataFrame:
    """AFML s.14.9 with the book's portfolio-vs-universe construction: the universe = every primary event of the edge (the
    opportunities the primary model offers), the portfolio = the events the meta-model takes. Per disjoint category:
    portfolio index return (mean R of taken events) minus universe index return (mean R of all events) = excess."""
    rows = []
    taken_mask = np.asarray(taken_mask, bool)
    for col in category_cols:
        for cat, g in universe.groupby(col, dropna=False):
            m = taken_mask[g.index.to_numpy()] if len(g) else np.array([], bool)
            r_all = g["executed_r"].astype(float)
            r_take = r_all[m]
            rows.append({"risk_class": col, "category": str(cat), "n_universe": int(len(g)), "n_taken": int(m.sum()),
                         "universe_mean_r": float(r_all.mean()), "portfolio_mean_r": float(r_take.mean()) if m.any() else float("nan"),
                         "excess_mean_r": float(r_take.mean() - r_all.mean()) if m.any() else float("nan"),
                         "portfolio_pnl_r": float(r_take.sum())})
    return pd.DataFrame(rows)


def regime_coverage(universe: pd.DataFrame, taken_mask: np.ndarray, regime_col: str = "Regime_State") -> dict[str, Any]:
    """AFML s.14.3 time range 'should be sufficiently long to include a comprehensive number of regimes': share of events
    per HMM regime state, in the universe and among taken trades."""
    if regime_col not in universe.columns:
        return {"regimes_universe": None}
    u = universe[regime_col].value_counts(normalize=True).sort_index()
    t = universe.loc[np.asarray(taken_mask, bool), regime_col].value_counts(normalize=True).sort_index()
    return {"n_regimes_universe": int(u.size), "n_regimes_taken": int(t.size),
            "regime_share_universe": {str(k): round(float(v), 4) for k, v in u.items()},
            "regime_share_taken": {str(k): round(float(v), 4) for k, v in t.items()}}
