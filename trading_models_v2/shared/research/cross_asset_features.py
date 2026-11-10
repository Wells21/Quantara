"""Cross-asset driver features for research (owner-approved experiment 2026-10-26; NOT part of any edge schema yet).

Every feature for a target time t uses only driver bars that CLOSED STRICTLY BEFORE t (as-of join on each driver's own
standard_quote_tick research bar clock), so the same value can be computed live from the same broker's quotes.
Research bars only: assets/<A>/bars/standard_quote_tick/*_bars.csv (the sealed holdout under assets/<A>/holdout is never
read).

Per driver D (the 9 other assets of the 10):
  {D}_z1h, {D}_z4h, {D}_z24h : log return of D over the last 1 / 4 / 24 hours, divided by D's hourly volatility x sqrt(h)
  {D}_stale_h               : hours since D's last bar close (closed sessions, weekends)
  {D}_vol_ratio             : D's hourly vol over the last 120 h / over the last 480 h
  {D}_corr_480h             : correlation of hourly returns of the target and D over the last 480 h
  {D}_resid_z4h             : target z4h - corr x D z4h (the target moved more / less than its usual co-move with D)
plus XA_equity_risk_z24h      : mean z24h of the equity indices among the drivers (US500, NASDAQ, DAX, JPN, HK).
Hourly grid: as-of closes at each UTC hour (last bar close <= the hour); volatility / correlation from rolling windows on
that grid, read at floor(t) (<= t, so causal).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
ASSETS = ["BTCUSD", "DAX", "GBPJPY", "HK", "JPN", "NASDAQ", "US500", "USDMXN", "USOIL", "XAUUSD"]
EQUITY = {"US500", "NASDAQ", "DAX", "JPN", "HK"}
HOUR = 3_600_000_000_000
HORIZONS = (1, 4, 24)
VOL_LONG, VOL_SHORT = 480, 120


def _ts(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True, format="ISO8601").to_numpy(dtype="datetime64[ns]").astype(np.int64)


def load_bars(asset: str) -> tuple[np.ndarray, np.ndarray]:
    f = V2 / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    b = pd.read_csv(f, usecols=["Datetime", "Close"])
    t = _ts(b.Datetime)
    o = np.argsort(t, kind="stable")
    return t[o], b.Close.to_numpy(float)[o]


def research_end(asset: str) -> int:
    return int(load_bars(asset)[0][-1])


class HourlyGrid:
    def __init__(self, t: np.ndarray, px: np.ndarray, start: int, end: int):
        self.h = np.arange(start // HOUR * HOUR, end // HOUR * HOUR + HOUR, HOUR)
        k = np.searchsorted(t, self.h, side="right") - 1
        lp = np.where(k >= 0, np.log(px[np.clip(k, 0, None)]), np.nan)
        self.lp = lp
        r = pd.Series(np.diff(lp, prepend=np.nan))
        self.ret = r.to_numpy()
        self.sig_long = r.rolling(VOL_LONG, min_periods=VOL_LONG // 2).std().to_numpy()
        self.sig_short = r.rolling(VOL_SHORT, min_periods=VOL_SHORT // 2).std().to_numpy()

    def at(self, t_ns: np.ndarray) -> np.ndarray:
        return np.clip((t_ns // HOUR * HOUR - self.h[0]) // HOUR, 0, len(self.h) - 1).astype(np.int64)


def asof_logprice(t: np.ndarray, px: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """log close of the last bar closing strictly before q, and that bar's close time."""
    k = np.searchsorted(t, q, side="left") - 1
    ok = k >= 0
    lp = np.full(len(q), np.nan)
    lt = np.full(len(q), np.nan)
    lp[ok] = np.log(px[k[ok]])
    lt[ok] = t[k[ok]]
    return lp, lt


def build(target: str, times: np.ndarray, drivers: list[str] | None = None) -> pd.DataFrame:
    """Cross-asset features at the target's decision times (UTC ns)."""
    drivers = drivers or [a for a in ASSETS if a != target]
    start, end = int(times.min()) - 40 * 24 * HOUR, int(times.max()) + HOUR
    tt, tp = load_bars(target)
    tgrid = HourlyGrid(tt, tp, start, end)
    hi = tgrid.at(times)
    t_lp0, _ = asof_logprice(tt, tp, times)
    t_lp4, _ = asof_logprice(tt, tp, times - 4 * HOUR)
    t_z4 = (t_lp0 - t_lp4) / (tgrid.sig_long[hi] * 2.0)
    out = {}
    eq = []
    for d in drivers:
        dt, dp = load_bars(d)
        g = HourlyGrid(dt, dp, start, end)
        lp0, last_t = asof_logprice(dt, dp, times)
        sig = g.sig_long[hi]
        for h in HORIZONS:
            lph, _ = asof_logprice(dt, dp, times - h * HOUR)
            out[f"{d}_z{h}h"] = (lp0 - lph) / (sig * np.sqrt(h))
        out[f"{d}_stale_h"] = (times - last_t) / HOUR
        out[f"{d}_vol_ratio"] = g.sig_short[hi] / sig
        corr = pd.Series(tgrid.ret).rolling(VOL_LONG, min_periods=VOL_LONG // 2).corr(pd.Series(g.ret)).to_numpy()
        out[f"{d}_corr_480h"] = corr[hi]
        out[f"{d}_resid_z4h"] = t_z4 - corr[hi] * out[f"{d}_z4h"]
        if d in EQUITY:
            eq.append(out[f"{d}_z24h"])
    out["XA_equity_risk_z24h"] = np.nanmean(np.vstack(eq), axis=0) if eq else np.nan
    df = pd.DataFrame(out)
    return df.replace([np.inf, -np.inf], np.nan)
