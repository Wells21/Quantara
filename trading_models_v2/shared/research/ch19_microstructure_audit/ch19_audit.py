"""AFML Ch.19 audit (2026-10-31): what the High/Low features measure on our mid-quote tick bars.

Per asset, on the research bars (the FFD manifest's source bars; holdout never read):
  1. port vs book: shared/features/afml_ch19_microstructure vs Snippets 19.1-19.2 as printed;
  2. Corwin_Schultz_Spread_1 on mid bars: share of bars at 0 (alpha clipped), quantiles;
  3. over the last N_TICK_FILES weekly research tick files: the quoted spread actually observed in each bar,
     (ask - bid) / mid averaged over the bar's ticks, vs C-S on mid High/Low and vs C-S on ask-High / bid-Low
     (the paper's premise: highs at the offer, lows at the bid);
  4. Roll (p.283) on tick-to-tick mid changes and on bar closes; share of zero mid changes (tick rule carries b_t-1);
  5. exercise 19.3: Parkinson sigma (k1 and k2 forms, p.284) vs std of close-to-close log returns, on bars, days, weeks;
  6. exercise 19.4: daily C-S spread and Snippet 19.2 sigma vs the daily mean quoted spread and Parkinson.

usage: python ch19_audit.py ASSET OUT_JSON
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
V2 = HERE.parents[2]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(HERE))

from shared.features.afml_ch19_microstructure import (  # noqa: E402
    _get_beta,
    _get_gamma,
    becker_parkinson_volatility,
    corwin_schultz_spread,
    parkinson_volatility,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
import book_snippets_19_1_19_2 as book  # noqa: E402

N_TICK_FILES = 4
K1 = 4 * np.log(2)
K2 = np.sqrt(8 / np.pi)


def q(x, ps=(0.05, 0.25, 0.5, 0.75, 0.95)):
    x = np.asarray(x, dtype="float64")
    x = x[np.isfinite(x)]
    return {str(p): float(np.quantile(x, p)) for p in ps} if x.size else {}


def rho(a, b):
    a, b = np.asarray(a, dtype="float64"), np.asarray(b, dtype="float64")
    m = np.isfinite(a) & np.isfinite(b)
    return float(spearmanr(a[m], b[m])[0]) if m.sum() > 10 else None


def cs_from_hl(h, l):
    s = corwin_schultz_spread(pd.Series(h), pd.Series(l), 1)
    return s.to_numpy()


def roll(dp):
    dp = np.asarray(dp, dtype="float64")
    cov = float(np.cov(dp[1:], dp[:-1])[0, 1])
    var = float(np.var(dp, ddof=1))
    return {"serial_cov": cov, "c": float(np.sqrt(max(0.0, -cov))), "sigma2_u": var + 2 * cov,
            "lag1_autocorr": cov / var if var > 0 else None}


def parkinson_pair(h, l):
    lr = np.log(np.asarray(h) / np.asarray(l))
    return {"k1_sigma": float(np.sqrt(np.mean(lr ** 2) / K1)), "k2_sigma": float(np.mean(lr) / K2)}


def main(asset: str, out: Path) -> None:
    res: dict = {"asset": asset}
    ffd = json.loads((V2 / "assets" / asset / "bars" / "ffd" / "ffd_manifest.json").read_text(encoding="utf-8"))
    bars = pd.read_csv(V2 / ffd["source_bars_path"], usecols=["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks"])
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True)
    bars["Bar_Start"] = to_datetime_iso(bars["Bar_Start"], utc=True)
    h, l, c = (bars[k].to_numpy(dtype="float64") for k in ("High", "Low", "Close"))
    res["bars"] = {"rows": int(len(bars)), "first": str(bars["Datetime"].iloc[0]), "last": str(bars["Datetime"].iloc[-1]),
                   "ticks_per_bar": q(bars["Ticks"]), "minutes_per_bar": q((bars["Datetime"] - bars["Bar_Start"]).dt.total_seconds() / 60)}

    # 1. port vs book
    s = bars[["High", "Low"]].copy()
    port_cs = corwin_schultz_spread(s["High"], s["Low"], 1)
    book_cs = book.corwinSchultz(s, 1)["Spread"]
    port_bp = becker_parkinson_volatility(s["High"], s["Low"], 1)
    book_bp = book.getSigma(book.getBeta(s, 1), book.getGamma(s))
    common = book_cs.index
    res["port_vs_book"] = {
        "cs_max_abs_diff_on_book_index": float(np.nanmax(np.abs(port_cs.loc[common] - book_cs))),
        "bp_max_abs_diff_on_book_index": float(np.nanmax(np.abs(port_bp.loc[book_bp.index] - book_bp))),
        "book_cs_rows": int(len(book_cs)), "port_cs_rows_non_nan": int(port_cs.notna().sum()),
        "port_rows_book_drops": {str(i): [float(port_cs.iloc[i]), float(port_bp.iloc[i])] for i in range(len(s)) if s.index[i] not in common},
    }
    for sl in (2, 20):
        pc = corwin_schultz_spread(s["High"], s["Low"], sl)
        bc = book.corwinSchultz(s, sl)["Spread"]
        res["port_vs_book"][f"cs_sl{sl}_max_abs_diff"] = float(np.nanmax(np.abs(pc.loc[bc.index] - bc)))
        res["port_vs_book"][f"cs_sl{sl}_rows_book_drops_port_zero"] = int((pc.drop(bc.index) == 0).sum())

    # 2. C-S on mid bars
    cs = port_cs.to_numpy()[1:]
    beta = _get_beta(s["High"], s["Low"], 1).to_numpy()[1:]
    gamma = _get_gamma(s["High"], s["Low"]).to_numpy()[1:]
    den = 3 - 2 * np.sqrt(2)
    raw_alpha = (np.sqrt(2) - 1) * np.sqrt(beta) / den - np.sqrt(gamma / den)
    p20 = parkinson_volatility(s["High"], s["Low"], 20).to_numpy()
    bp = port_bp.to_numpy()
    r = np.diff(np.log(c))
    std20 = pd.Series(np.r_[np.nan, r]).rolling(20).std().to_numpy()
    res["cs_mid_bars"] = {"share_zero": float(np.mean(cs == 0)), "share_raw_alpha_negative": float(np.mean(raw_alpha < 0)),
                          "quantiles": q(cs), "mean": float(np.mean(cs)),
                          "spearman_cs_vs_parkinson20": rho(port_cs, p20), "spearman_bp1_vs_parkinson20": rho(bp, p20),
                          "spearman_parkinson20_vs_std20": rho(p20, std20), "spearman_bp1_vs_abs_return": rho(bp[1:], np.abs(r))}

    # 3. quoted spread in the last research tick files
    files = sorted((V2 / "assets" / asset / "raw" / "ticks").glob("*.csv"))[-N_TICK_FILES:]
    t = pd.concat([pd.read_csv(f, usecols=["timestamp", "bidPrice", "askPrice"]) for f in files], ignore_index=True)
    t["timestamp"] = pd.to_datetime(t["timestamp"], utc=True, format="mixed")
    t = t[(t.bidPrice > 0) & (t.askPrice >= t.bidPrice)].sort_values("timestamp", kind="stable").reset_index(drop=True)
    mid = (t.bidPrice + t.askPrice).to_numpy() / 2
    rel = (t.askPrice - t.bidPrice).to_numpy() / mid
    tsv = t["timestamp"].to_numpy()
    sel = bars[(bars["Bar_Start"] >= t["timestamp"].iloc[0]) & (bars["Datetime"] <= t["timestamp"].iloc[-1])]
    i0 = np.searchsorted(tsv, sel["Bar_Start"].to_numpy(), "left")
    i1 = np.searchsorted(tsv, sel["Datetime"].to_numpy(), "right")
    qs = np.array([rel[a:b].mean() for a, b in zip(i0, i1)])
    ha = np.array([t.askPrice.to_numpy()[a:b].max() for a, b in zip(i0, i1)])
    lb = np.array([t.bidPrice.to_numpy()[a:b].min() for a, b in zip(i0, i1)])
    hm = np.array([mid[a:b].max() for a, b in zip(i0, i1)])
    lm = np.array([mid[a:b].min() for a, b in zip(i0, i1)])
    cs_mid = cs_from_hl(hm, lm)
    cs_ab = cs_from_hl(ha, lb)
    res["quoted_spread_sample"] = {
        "tick_files": [f.name for f in files], "ticks": int(len(t)), "bars": int(len(sel)),
        "mid_high_low_match_bar_file": float(np.mean(np.isclose(hm, sel["High"].to_numpy()) & np.isclose(lm, sel["Low"].to_numpy()))),
        "quoted_rel_spread_per_bar": q(qs),
        "cs_on_mid_bars": q(cs_mid[1:]), "cs_on_mid_share_zero": float(np.mean(cs_mid[1:] == 0)),
        "cs_on_ask_high_bid_low": q(cs_ab[1:]), "cs_on_ask_high_bid_low_share_zero": float(np.mean(cs_ab[1:] == 0)),
        "spearman_quoted_vs_cs_mid": rho(qs[1:], cs_mid[1:]), "spearman_quoted_vs_cs_askbid": rho(qs[1:], cs_ab[1:]),
        "spearman_quoted_vs_parkinson_bar": rho(qs, np.log(hm / lm)),
    }

    # 4. Roll and the tick rule on mid
    dm = np.diff(mid)
    res["roll"] = {"tick_mid": roll(dm), "tick_mid_c_rel_to_mean_mid": float(np.sqrt(max(0.0, -roll(dm)["serial_cov"])) / mid.mean()),
                   "half_quoted_spread_rel_mean": float(rel.mean() / 2), "share_zero_mid_change": float(np.mean(dm == 0)),
                   "bar_close_log": roll(r)}
    del t

    # 5. exercise 19.3 and 6. exercise 19.4
    day = bars["Datetime"].dt.floor("D")
    d = bars.groupby(day).agg(High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"), n=("Close", "size"))
    d = d[d.n >= 10]
    wk = bars.groupby(bars["Datetime"].dt.to_period("W").astype(str)).agg(High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"))
    ex = {}
    for name, frame in (("bars", bars), ("daily", d), ("weekly", wk)):
        cc = float(np.std(np.diff(np.log(frame["Close"].to_numpy())), ddof=1))
        pk = parkinson_pair(frame["High"].to_numpy(), frame["Low"].to_numpy())
        ex[name] = {"rows": int(len(frame)), "close_to_close_std": cc, **pk,
                    "k1_over_cc": pk["k1_sigma"] / cc, "k2_over_cc": pk["k2_sigma"] / cc}
    res["exercise_19_3"] = ex
    dcs = corwin_schultz_spread(d["High"], d["Low"], 1)
    dbp = becker_parkinson_volatility(d["High"], d["Low"], 1)
    sday = sel["Datetime"].dt.floor("D").to_numpy()
    qs_day = pd.Series(qs, index=sday).groupby(level=0).mean()
    res["exercise_19_4"] = {"daily_cs_mean": float(dcs.iloc[1:].mean()), "daily_cs_share_zero": float((dcs.iloc[1:] == 0).mean()),
                            "daily_cs_quantiles": q(dcs.iloc[1:]), "daily_bp_sigma_mean": float(dbp.iloc[1:].mean()),
                            "daily_parkinson_k1": ex["daily"]["k1_sigma"], "daily_cc_std": ex["daily"]["close_to_close_std"],
                            "sample_days_mean_quoted_rel_spread": float(qs_day.mean()),
                            "sample_days_cs_mean": float(dcs.reindex(qs_day.index).mean())}
    out.write_text(json.dumps(res, indent=1, default=str) + "\n", encoding="utf-8")
    print(asset, "ok")


if __name__ == "__main__":
    main(sys.argv[1].upper(), Path(sys.argv[2]))
