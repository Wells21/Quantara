"""Gate 0 tool (THEORY CHECK D075; the single Gate 0 tool of backlog D075) for session-anchored theses.

For a thesis whose events are defined by the clock and the bars alone, this tool:
- builds the event sets on all 10 assets straight from the research standard_quote_tick bars;
- labels them at the card's pre-registered Gate 0 cell (calendar-time Event_Vol, EWM span, vertical, barriers);
- runs the D075 tests per asset and pooled per side over the card's pre-registered universe (D075 rule C as amended
  2026-11-04; every asset is still computed and reported): the month-matched drift baseline and every card counterfactual,
  each with PASS = diff - 1.645 SE > 0 (one-sided 95 %); the drift baseline drops to one-sided 90 % (z 1.28) when
  every counterfactual passes at 95 % (tiered drift bar, owner 2026-11-04).

Labeling uses the AFML triple barrier on bar OHLC: Event_Vol = shared calendar_time_event_vol, vertical =
calendar_time_vertical_deadline_positions. A bar touching both barriers resolves stop-loss first (conservative,
counted). Gross mid R, no costs.

Theses are registered in THESES; each card's PREREGISTRATION.md (docs/mldp/campaign/...) defines its rule before any
run.

usage (from trading_models_v2, venv312): python shared/research/gate0/gate0_tool.py T1 OUT_DIR [START END TAG]
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
from shared.labels.calendar_time_barriers import calendar_time_event_vol, calendar_time_vertical_deadline_positions  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

ASSETS = ["NASDAQ", "US500", "DAX", "JPN", "HK", "XAUUSD", "GBPJPY", "USDMXN", "USOIL", "BTCUSD"]
POOL, DRAWS, SEED = 20_000, 10_000, 20261102
Z = 1.645
Z_DRIFT_TIERED = 1.28   # D075 tiered drift bar (owner 2026-11-04): one-sided 90 % when every counterfactual passes at 95 %
MIN_EVENTS = 100
MAX_STALE = pd.Timedelta(minutes=30)
MIN_VOL = 1e-8


@dataclass(frozen=True)
class Cell:
    lookback_h: float
    ewm_span: int
    vertical_h: float
    pt: float = 1.0
    sl: float = 1.0


# --------------------------------------------------------------------------- bars and labeling

def load_bars(asset: str) -> pd.DataFrame:
    f = V2 / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    b = pd.read_csv(f, usecols=["Datetime", "High", "Low", "Close"])
    b["Datetime"] = to_datetime_iso(b["Datetime"], utc=True)
    return b.dropna().sort_values("Datetime").drop_duplicates("Datetime").reset_index(drop=True)


class Labeler:
    def __init__(self, bars: pd.DataFrame, cell: Cell):
        self.ts = bars["Datetime"].dt.tz_convert("UTC").to_numpy(dtype="datetime64[ns]").astype("int64")
        self.hi, self.lo, self.cl = (bars[c].to_numpy(dtype="float64") for c in ("High", "Low", "Close"))
        self.cell = cell
        self.vol = calendar_time_event_vol(self.ts, self.cl, hours=cell.lookback_h, ewm_span=cell.ewm_span, min_vol=MIN_VOL)
        self.both_touch = 0

    def label(self, pos: np.ndarray, side: np.ndarray) -> np.ndarray:
        pos = np.asarray(pos, dtype=int)
        side = np.asarray(side, dtype=int)
        out = np.full(len(pos), np.nan)
        dl = calendar_time_vertical_deadline_positions(self.ts, pos, hours=self.cell.vertical_h)
        for i, (p, s, d) in enumerate(zip(pos, side, dl)):
            v = self.vol[p]
            if d < 0 or not np.isfinite(v) or v <= 0:
                continue
            e = self.cl[p]
            up, dn = (e * (1 + self.cell.pt * v), e * (1 - self.cell.sl * v)) if s == 1 else (e * (1 + self.cell.sl * v), e * (1 - self.cell.pt * v))
            r = np.nan
            for j in range(p + 1, d + 1):
                hit_up, hit_dn = self.hi[j] >= up, self.lo[j] <= dn
                if hit_up and hit_dn:
                    self.both_touch += 1
                    r = -1.0
                    break
                if hit_up:
                    r = self.cell.pt / self.cell.sl if s == 1 else -1.0
                    break
                if hit_dn:
                    r = self.cell.pt / self.cell.sl if s == -1 else -1.0
                    break
            if np.isnan(r):
                r = s * (self.cl[d] / e - 1.0) / v
            out[i] = r
        return out


# --------------------------------------------------------------------------- T1: session-close hedging flow

REF = {  # asset -> (tz, [(from_date, "HH:MM")])
    "NASDAQ": ("America/New_York", [("2000-01-01", "16:00")]),
    "US500": ("America/New_York", [("2000-01-01", "16:00")]),
    "DAX": ("Europe/Berlin", [("2000-01-01", "17:30")]),
    "JPN": ("Asia/Tokyo", [("2000-01-01", "15:00"), ("2024-11-05", "15:30")]),
    "HK": ("Asia/Hong_Kong", [("2000-01-01", "16:00")]),
    "XAUUSD": ("Europe/London", [("2000-01-01", "15:00")]),
    "GBPJPY": ("Europe/London", [("2000-01-01", "16:00")]),
    "USDMXN": ("Europe/London", [("2000-01-01", "16:00")]),
    "USOIL": ("America/New_York", [("2000-01-01", "14:30")]),
    "BTCUSD": ("Europe/London", [("2000-01-01", "16:00")]),
}


def _ref_time(asset: str, day: pd.Timestamp) -> pd.Timestamp:
    tz, sched = REF[asset]
    hhmm = [t for d, t in sched if day >= pd.Timestamp(d)][-1]
    h, m = map(int, hhmm.split(":"))
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=h, minute=m, tz=tz).tz_convert("UTC")


def _pos_at_or_before(ts: np.ndarray, t: pd.Timestamp) -> int:
    tn = t.value
    p = int(np.searchsorted(ts, tn, side="right") - 1)
    if p < 0 or tn - ts[p] > MAX_STALE.value:
        return -1
    return p


def t1_days(lab: Labeler, asset: str, entry_offset: pd.Timedelta) -> pd.DataFrame:
    """One row per weekday: entry bar at ref - entry_offset, r = log(close_entry / close_prev_ref)."""
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows = []
    for prev, day in zip(days[:-1], days[1:]):
        pe = _pos_at_or_before(lab.ts, _ref_time(asset, day) - entry_offset)
        pp = _pos_at_or_before(lab.ts, _ref_time(asset, prev))
        if pe < 0 or pp < 0 or pe <= pp:
            continue
        r = float(np.log(lab.cl[pe] / lab.cl[pp]))
        if r == 0 or not np.isfinite(r):
            continue
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": r})
    return pd.DataFrame(rows)


def t1_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    bars = load_bars(asset)
    lab = Labeler(bars, cell)
    close_days = t1_days(lab, asset, pd.Timedelta(hours=1))
    mid_days = t1_days(lab, asset, pd.Timedelta(hours=6))
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = close_days[np.sign(close_days.r) == side].copy()
        inv = close_days[np.sign(close_days.r) == -side].copy()          # C1: opposite-sign days, same side
        mid = mid_days[np.sign(mid_days.r) == side].copy()               # C2: same rule, mid-session
        for f in (edge, inv, mid):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid}
    return sets, lab


# --------------------------------------------------------------------------- T1c / T1bc: causal (live-tradable) entry
# Owner 2026-11-04 (post-hoc re-test): the entry bar is the FIRST bar closing at or after the entry time (at most
# 30 minutes after), as the edge trades live; "last bar at or before" needs the next bar's timestamp. Reference
# closes are unchanged (last bar at or before, in the past). Same card, cell, labeler, statistics.

def _pos_at_or_after(ts: np.ndarray, t: pd.Timestamp) -> int:
    tn = t.value
    p = int(np.searchsorted(ts, tn, side="left"))
    if p >= len(ts) or ts[p] - tn > MAX_STALE.value:
        return -1
    return p


def t1_days_causal(lab: Labeler, asset: str, entry_offset: pd.Timedelta) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows = []
    for prev, day in zip(days[:-1], days[1:]):
        pe = _pos_at_or_after(lab.ts, _ref_time(asset, day) - entry_offset)
        pp = _pos_at_or_before(lab.ts, _ref_time(asset, prev))
        if pe < 0 or pp < 0 or pe <= pp:
            continue
        r = float(np.log(lab.cl[pe] / lab.cl[pp]))
        if r == 0 or not np.isfinite(r):
            continue
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": r})
    return pd.DataFrame(rows)


def t1c_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    bars = load_bars(asset)
    lab = Labeler(bars, cell)
    close_days = t1_days_causal(lab, asset, pd.Timedelta(hours=1))
    mid_days = t1_days_causal(lab, asset, pd.Timedelta(hours=6))
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = close_days[np.sign(close_days.r) == side].copy()
        inv = close_days[np.sign(close_days.r) == -side].copy()
        mid = mid_days[np.sign(mid_days.r) == side].copy()
        for f in (edge, inv, mid):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid}
    return sets, lab


# --------------------------------------------------------------------------- T3: overnight dealer-inventory reversal
# Card: docs/mldp/campaign/NASDAQ_2026-11/T3_overnight_inventory_reversal/PREREGISTRATION.md (Boyarchenko, Larsen &
# Whelan, The Overnight Drift). Clock times are New York for every asset (the mechanism is the US close); the other
# assets are computed and reported only (rule C universe NASDAQ + US500). Entry bars are causal (first bar at or after).

NY = "America/New_York"


def _ny(day: pd.Timestamp, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm, tz=NY).tz_convert("UTC")


def _prev_weekday(day: pd.Timestamp) -> pd.Timestamp:
    return day - pd.Timedelta(days=3 if day.weekday() == 0 else 1)


def t3_days(lab: Labeler, entry_hh: int, entry_mm: int) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    rows = []
    for day in pd.date_range(first, last, freq="D"):
        if day.weekday() >= 5:
            continue
        p = _prev_weekday(day)
        pc = _pos_at_or_before(lab.ts, _ny(p, 16, 15))     # previous session close (16:15 ET)
        ph = _pos_at_or_before(lab.ts, _ny(p, 15, 15))     # start of the close hour (15:15 ET)
        pp = _pos_at_or_before(lab.ts, _ny(_prev_weekday(p), 16, 15))
        pe = _pos_at_or_after(lab.ts, _ny(day, entry_hh, entry_mm))
        if min(pc, ph, pe) < 0 or not (ph < pc < pe):
            continue
        x = float(np.log(lab.cl[pc] / lab.cl[ph]))
        if x == 0 or not np.isfinite(x):
            continue
        r_full = float(np.log(lab.cl[pc] / lab.cl[pp])) if pp >= 0 and pp < pc else np.nan
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": x, "r_full": r_full})
    return pd.DataFrame(rows, columns=["day", "pos", "Datetime", "r", "r_full"])


def t3_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    od = t3_days(lab, 2, 0)        # European open window (paper: 2:00 - 3:00 ET)
    mid = t3_days(lab, 12, 0)      # C2: US midday
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        # inventory reversal goes AGAINST the close-hour move: LONG after a close-hour selloff (x < 0)
        edge = od[np.sign(od.r) == -side].copy()
        inv = od[np.sign(od.r) == side].copy()           # C1: opposite close-hour sign, same side
        mid_s = mid[np.sign(mid.r) == -side].copy()      # C2: same rule at 12:00 ET
        for f in (edge, inv, mid_s):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid_s}
    return sets, lab


# --------------------------------------------------------------------------- T4: month-end payment-cycle liquidity
# Card: docs/mldp/campaign/NASDAQ_2026-11/T4_month_end_liquidity_cycle/PREREGISTRATION.md (Etula, Rinne, Suominen &
# Vaittinen 2020). Windows are the paper's T+3 windows shifted by (3 - s) days toward T, s = settlement days at T.

SETTLEMENT_DAYS = {  # asset -> [(from_date, s)]
    "NASDAQ": [("2000-01-01", 2), ("2024-05-28", 1)],
    "US500": [("2000-01-01", 2), ("2024-05-28", 1)],
}


def _settle(asset: str, day: pd.Timestamp) -> int:
    sched = SETTLEMENT_DAYS.get(asset, [("2000-01-01", 2)])
    return [v for d, v in sched if day >= pd.Timestamp(d)][-1]


def t4_days(lab: Labeler, asset: str) -> pd.DataFrame:
    """One row per trading day D (k = trading-day offset from the month end T). The event for D enters on the causal
    bar after the PREVIOUS trading day's reference close and covers day D."""
    tz = REF[asset][0]
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    td = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        # amended before any result (2026-11-05): US quote bars after 16:00 often span the maintenance break, so the
        # entry is the first bar closing at or after ref - 30 min, accepted if it closes by ref + 30 min (causal)
        ref = _ref_time(asset, d)
        pe = _pos_at_or_after(lab.ts, ref - MAX_STALE)
        if pe >= 0 and lab.ts[pe] <= (ref + MAX_STALE).value:
            td.append({"day": d, "close_pos": pe})
    f = pd.DataFrame(td)
    f["month"] = f.day.dt.to_period("M")
    f = f[f.month != f.month.iloc[-1]].reset_index(drop=True)          # drop the incomplete last month
    t_idx = f.groupby("month").apply(lambda g: g.index.max(), include_groups=False)
    months = list(t_idx.index)
    rows = []
    for i in range(1, len(f)):
        m = f.month.iloc[i]
        tm = int(t_idx[m])
        j = months.index(m)
        prev_t = int(t_idx[months[j - 1]]) if j > 0 else None
        if prev_t is not None and 1 <= i - prev_t <= 8:
            k, t_ref = i - prev_t, prev_t
        else:
            k, t_ref = i - tm, tm
        t_day = f.day.iloc[t_ref]
        pos = int(f.close_pos.iloc[i - 1])
        rows.append({"day": f.day.iloc[i], "pos": pos, "Datetime": pd.Timestamp(lab.ts[pos], tz="UTC"), "k": k,
                     "s": _settle(asset, t_day), "t_is_friday": bool(t_day.weekday() == 4)})
    return pd.DataFrame(rows, columns=["day", "pos", "Datetime", "k", "s", "t_is_friday"])


def t4_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    d = t4_days(lab, asset)
    long_win = (d.k >= -d.s) & (d.k <= 3)
    short_win = (d.k >= -(d.s + 5)) & (d.k <= -(d.s + 1))
    other = d.k < -(d.s + 6)                                           # C1: Figure-5 "other days"
    sets = {}
    for side, name, win in ((1, "LONG", long_win), (-1, "SHORT", short_win)):
        edge, c1 = d[win].copy(), d[other].copy()
        for x in (edge, c1):
            x["realized_r"] = lab.label(x.pos.to_numpy(), np.full(len(x), side))
        sets[name] = {"edge": edge, "C1": c1}
    return sets, lab


# --------------------------------------------------------------------------- T5: vol-target rebalancing flow
# Card: docs/mldp/campaign/NASDAQ_2026-11/T5_vol_target_rebalancing_flow/PREREGISTRATION.md (our own thesis).
# New York clock for every asset; universe NASDAQ + US500.

VOL_TARGET_WINDOW = 20


def t5_days(lab: Labeler, entry_hh: int, entry_mm: int) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    closes = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        pc = _pos_at_or_before(lab.ts, _ny(d, 16, 0))
        if pc >= 0:
            closes.append({"day": d, "close_pos": pc})
    if not closes:                                                 # no bars at the New York close (e.g. HK)
        return pd.DataFrame(columns=["day", "pos", "Datetime", "r", "day_move"])
    c = pd.DataFrame(closes)
    c["ret"] = np.log(lab.cl[c.close_pos.to_numpy()] / np.r_[np.nan, lab.cl[c.close_pos.to_numpy()[:-1]]])
    c["sigma"] = c.ret.rolling(VOL_TARGET_WINDOW, min_periods=VOL_TARGET_WINDOW).std()
    rows = []
    for i in range(2, len(c)):
        day = c.day.iloc[i]
        s1, s2 = c.sigma.iloc[i - 1], c.sigma.iloc[i - 2]          # sigma at the close of D-1 and D-2
        if not (np.isfinite(s1) and np.isfinite(s2)) or s1 <= 0:
            continue
        g = float(s2 / s1 - 1.0)
        if g == 0:
            continue
        pe = _pos_at_or_after(lab.ts, _ny(day, entry_hh, entry_mm))
        prev_close = int(c.close_pos.iloc[i - 1])
        if pe < 0 or pe <= prev_close:
            continue
        day_move = float(np.log(lab.cl[pe] / lab.cl[prev_close]))
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": g, "day_move": day_move})
    return pd.DataFrame(rows, columns=["day", "pos", "Datetime", "r", "day_move"])


def t5_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    close_days = t5_days(lab, 15, 0)
    mid_days = t5_days(lab, 12, 0)                                # C2: midday
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        # g > 0 (volatility fell) -> forced buying -> LONG; g < 0 -> forced selling -> SHORT
        edge = close_days[np.sign(close_days.r) == side].copy()
        inv = close_days[np.sign(close_days.r) == -side].copy()       # C1: opposite sign of g, same side
        mid = mid_days[np.sign(mid_days.r) == side].copy()
        for f in (edge, inv, mid):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid}
    return sets, lab


# --------------------------------------------------------------------------- T6: dealer gamma regime (fade / follow)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T6_dealer_gamma_regime/PREREGISTRATION.md (our own thesis).

T6_MARKS = [(10, 0), (10, 30), (11, 0), (11, 30), (12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (14, 30), (15, 0),
            (15, 30), (16, 0)]
T6_HOURS = [11, 12, 13, 14, 15]
T6_SESSIONS = 5
T6_STALE_LAG = 20


def _session_returns(lab: Labeler, day: pd.Timestamp) -> np.ndarray | None:
    pos = [_pos_at_or_before(lab.ts, _ny(day, hh, mm)) for hh, mm in T6_MARKS]
    if min(pos) < 0:
        return None
    px = lab.cl[np.array(pos)]
    return np.diff(np.log(px))


def t6_events(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    sess = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        r = _session_returns(lab, d)
        if r is not None:
            sess.append((d, r))

    def rho(block: list) -> float:
        x = np.concatenate([np.c_[r[:-1], r[1:]] for _, r in block])     # within-session consecutive pairs only
        if len(x) < 20 or np.std(x[:, 0]) == 0 or np.std(x[:, 1]) == 0:
            return np.nan
        return float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])

    rows = []
    for i in range(T6_SESSIONS + T6_STALE_LAG, len(sess)):
        day = sess[i][0]
        rho_now = rho(sess[i - T6_SESSIONS:i])
        rho_stale = rho(sess[i - T6_SESSIONS - T6_STALE_LAG:i - T6_STALE_LAG])
        if not (np.isfinite(rho_now) and np.isfinite(rho_stale)) or rho_now == 0 or rho_stale == 0:
            continue
        for h in T6_HOURS:
            pe = _pos_at_or_after(lab.ts, _ny(day, h, 0))
            pb = _pos_at_or_before(lab.ts, _ny(day, h - 1, 0))
            if pe < 0 or pb < 0 or pb >= pe:
                continue
            m = float(np.log(lab.cl[pe] / lab.cl[pb]))
            if m == 0 or not np.isfinite(m):
                continue
            rows.append({"day": day, "hour": h, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": m,
                         "rho": rho_now, "rho_stale": rho_stale,
                         "side_rule": int(np.sign(m) * (1 if rho_now > 0 else -1)),
                         "side_stale": int(np.sign(m) * (1 if rho_stale > 0 else -1))})
    return pd.DataFrame(rows, columns=["day", "hour", "pos", "Datetime", "r", "rho", "rho_stale", "side_rule",
                                       "side_stale"])


def t6_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t6_events(lab)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = ev[ev.side_rule == side].copy()
        inv = ev[ev.side_rule == -side].copy()        # C1: regime label flipped -> the rule says the other side
        stale = ev[ev.side_stale == side].copy()      # C2: rule with a 20-session-old regime
        for f in (edge, inv, stale):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": stale}
    return sets, lab


# --------------------------------------------------------------------------- T7: monthly options-expiry hedge cycle
# Card: docs/mldp/campaign/NASDAQ_2026-11/T7_opex_hedge_cycle/PREREGISTRATION.md (our formalisation).


def _trading_days(lab: Labeler, asset: str) -> pd.DataFrame:
    """T4 entry rule: first bar closing at or after ref - 30 min, accepted if it closes by ref + 30 min."""
    tz = REF[asset][0]
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    td = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        ref = _ref_time(asset, d)
        pe = _pos_at_or_after(lab.ts, ref - MAX_STALE)
        if pe >= 0 and lab.ts[pe] <= (ref + MAX_STALE).value:
            td.append({"day": d, "close_pos": pe})
    return pd.DataFrame(td, columns=["day", "close_pos"])


def _third_friday(year: int, month: int) -> pd.Timestamp:
    d = pd.Timestamp(year=year, month=month, day=1)
    return d + pd.Timedelta(days=(4 - d.weekday()) % 7 + 14)


def t7_days(lab: Labeler, asset: str) -> pd.DataFrame:
    f = _trading_days(lab, asset)
    if f.empty:
        return pd.DataFrame(columns=["day", "pos", "Datetime", "k_next", "k_prev", "quarterly"])
    days = f.day.to_numpy()
    opex_idx = []
    for m in pd.period_range(f.day.iloc[0], f.day.iloc[-1], freq="M"):
        tf = _third_friday(m.year, m.month)
        j = int(np.searchsorted(days, np.datetime64(tf), side="right") - 1)
        if j >= 0 and pd.Timestamp(days[j]).to_period("M") == m:
            opex_idx.append(j)
    opex_idx = np.array(sorted(set(opex_idx)))
    rows = []
    for i in range(1, len(f)):
        nxt = opex_idx[opex_idx >= i]
        prv = opex_idx[opex_idx < i]
        k_next = int(i - nxt[0]) if len(nxt) else None
        k_prev = int(i - prv[-1]) if len(prv) else None
        q_ref = nxt[0] if (k_next is not None and k_next >= -4) else (prv[-1] if len(prv) else None)
        quarterly = bool(q_ref is not None and pd.Timestamp(days[q_ref]).month in (3, 6, 9, 12))
        pos = int(f.close_pos.iloc[i - 1])
        rows.append({"day": f.day.iloc[i], "pos": pos, "Datetime": pd.Timestamp(lab.ts[pos], tz="UTC"),
                     "k_next": k_next, "k_prev": k_prev, "quarterly": quarterly})
    return pd.DataFrame(rows, columns=["day", "pos", "Datetime", "k_next", "k_prev", "quarterly"])


def t7_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    d = t7_days(lab, asset)
    kn = pd.to_numeric(d.k_next, errors="coerce")
    kp = pd.to_numeric(d.k_prev, errors="coerce")
    pre = kn.between(-4, 0)
    post = kp.between(1, 5)
    other = ~pre & ~post & kn.notna() & kp.notna()
    sets = {}
    for side, name, win, swap in ((1, "LONG", pre, post), (-1, "SHORT", post, pre)):
        edge, c1, c2 = d[win].copy(), d[other].copy(), d[swap].copy()
        for x in (edge, c1, c2):
            x["realized_r"] = lab.label(x.pos.to_numpy(), np.full(len(x), side))
        sets[name] = {"edge": edge, "C1": c1, "C2": c2}
    return sets, lab


# --------------------------------------------------------------------------- T8: release de-risking / re-risking
# Card: docs/mldp/campaign/NASDAQ_2026-11/T8_release_derisk_rerisk/PREREGISTRATION.md (our own; D086 calendar).


def _release_table() -> pd.DataFrame:
    from shared.features.economic_calendar import load_calendar
    _, ev = load_calendar()
    ev = ev[ev.status == "released"].copy()
    ev["t"] = to_datetime_iso(ev.release_utc, utc=True)
    ev["day"] = ev.t.dt.tz_convert(NY).dt.tz_localize(None).dt.normalize()
    return ev


def _daily_closes(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() < 5:
            pc = _pos_at_or_before(lab.ts, _ny(d, 16, 0))
            if pc >= 0:
                rows.append({"day": d, "close_pos": pc})
    return pd.DataFrame(rows, columns=["day", "close_pos"])


def t8_events(lab: Labeler, offset_min: int, placebo: bool) -> pd.DataFrame:
    """offset_min relative to the release time. placebo=True: the same clock times on weekdays with no release."""
    rel = _release_table()
    closes = _daily_closes(lab)
    if closes.empty:
        return pd.DataFrame(columns=["day", "pos", "Datetime", "trend", "kind"])
    cdays = closes.day.to_numpy()
    if placebo:
        rdays = set(rel.day)
        clock = {"CPI": (8, 30), "FOMC": (14, 0)}            # NFP shares 08:30 with CPI
        items = [(d, k, hh, mm) for d in closes.day if d not in rdays for k, (hh, mm) in clock.items()]
    else:
        items = []
        for r in rel.itertuples():
            lt = r.t.tz_convert(NY)
            items.append((r.day, r.type, lt.hour, lt.minute))
    rows = []
    for day, kind, hh, mm in items:
        t = _ny(day, hh, mm) + pd.Timedelta(minutes=offset_min)
        pe = _pos_at_or_after(lab.ts, t)
        j = int(np.searchsorted(cdays, np.datetime64(day), side="left")) - 1   # previous trading day's close
        if pe < 0 or j < 5:
            continue
        c_prev, c_old = int(closes.close_pos.iloc[j]), int(closes.close_pos.iloc[j - 5])
        trend = float(np.log(lab.cl[c_prev] / lab.cl[c_old]))
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "trend": trend, "kind": kind})
    out = pd.DataFrame(rows, columns=["day", "pos", "Datetime", "trend", "kind"])
    return out.drop_duplicates("pos").reset_index(drop=True)     # CPI + NFP on one day share one bar


def t8_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    pre, post = t8_events(lab, -90, False), t8_events(lab, 30, False)
    pre_pl, post_pl = t8_events(lab, -90, True), t8_events(lab, 30, True)
    s_edge, s_c1, s_c2 = pre[pre.trend > 0].copy(), pre[pre.trend < 0].copy(), pre_pl[pre_pl.trend > 0].copy()
    l_edge, l_c1, l_c2 = post.copy(), post_pl.copy(), pre.copy()
    for side, group in ((-1, (s_edge, s_c1, s_c2)), (1, (l_edge, l_c1, l_c2))):
        for f in group:
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
    return {"LONG": {"edge": l_edge, "C1": l_c1, "C2": l_c2},
            "SHORT": {"edge": s_edge, "C1": s_c1, "C2": s_c2}}, lab


# --------------------------------------------------------------------------- T9: release-reaction continuation
# Card: docs/mldp/campaign/NASDAQ_2026-11/T9_release_reaction_continuation/PREREGISTRATION.md (all 10 assets).


def t9_events(lab: Labeler, placebo: bool) -> pd.DataFrame:
    rel = _release_table()
    if placebo:
        rdays = set(rel.day)
        first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
        last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
        anchors = [_ny(d, hh, mm) for d in pd.date_range(first, last, freq="D") if d.weekday() < 5 and d not in rdays
                   for hh, mm in ((8, 30), (14, 0))]
    else:
        anchors = list(rel.t)
    rows = []
    for a in anchors:
        a = pd.Timestamp(a).tz_convert("UTC")
        pb = _pos_at_or_before(lab.ts, a)
        pe = _pos_at_or_after(lab.ts, a + pd.Timedelta(minutes=30))
        if pb < 0 or pe < 0 or pe <= pb:
            continue
        m = float(np.log(lab.cl[pe] / lab.cl[pb]))
        if m == 0 or not np.isfinite(m):
            continue
        rows.append({"pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": m})
    out = pd.DataFrame(rows, columns=["pos", "Datetime", "r"])
    return out.drop_duplicates("pos").reset_index(drop=True)


def t9_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev, pl = t9_events(lab, False), t9_events(lab, True)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = ev[np.sign(ev.r) == side].copy()
        inv = ev[np.sign(ev.r) == -side].copy()
        plc = pl[np.sign(pl.r) == side].copy()
        for f in (edge, inv, plc):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": plc}
    return sets, lab


# --------------------------------------------------------------------------- T10: Friday weekend de-risking
# Card: docs/mldp/campaign/NASDAQ_2026-11/T10_weekend_derisking/PREREGISTRATION.md (weekend-closing assets).


def t10_events(lab: Labeler, weekday: int) -> pd.DataFrame:
    """weekday 4 = Friday (edge), 2 = Wednesday (C2). Trend = close(D-1) / close(D-6 calendar days), 16:00 ET."""
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() != weekday:
            continue
        pe = _pos_at_or_after(lab.ts, _ny(d, 15, 0))
        c1 = _pos_at_or_before(lab.ts, _ny(d - pd.Timedelta(days=1), 16, 0))
        c0 = _pos_at_or_before(lab.ts, _ny(d - pd.Timedelta(days=7), 16, 0))
        if min(pe, c1, c0) < 0 or not (c0 < c1 < pe):
            continue
        w = float(np.log(lab.cl[c1] / lab.cl[c0]))
        if w == 0 or not np.isfinite(w):
            continue
        rows.append({"day": d, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": w})
    return pd.DataFrame(rows, columns=["day", "pos", "Datetime", "r"])


def t10_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    fri, wed = t10_events(lab, 4), t10_events(lab, 2)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        # crowd flattening goes AGAINST the week's trend: LONG after a down week
        edge = fri[np.sign(fri.r) == -side].copy()
        inv = fri[np.sign(fri.r) == side].copy()
        mid = wed[np.sign(wed.r) == -side].copy()
        for f in (edge, inv, mid):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid}
    return sets, lab


# --------------------------------------------------------------------------- T11: benchmark-fix pressure reversal
# Card: docs/mldp/campaign/NASDAQ_2026-11/T11_fix_pressure_reversal/PREREGISTRATION.md (XAUUSD, GBPJPY, USDMXN).


def t11_events(lab: Labeler, asset: str, shift: pd.Timedelta) -> pd.DataFrame:
    tz = REF[asset][0]
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        fix = _ref_time(asset, d) - shift
        p1 = _pos_at_or_before(lab.ts, fix)
        p0 = _pos_at_or_before(lab.ts, fix - pd.Timedelta(minutes=30))
        pe = _pos_at_or_after(lab.ts, fix)
        if min(p0, p1, pe) < 0 or not (p0 < p1 <= pe):
            continue
        pre = float(np.log(lab.cl[p1] / lab.cl[p0]))
        if pre == 0 or not np.isfinite(pre):
            continue
        rows.append({"day": d, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": pre})
    out = pd.DataFrame(rows, columns=["day", "pos", "Datetime", "r"])
    return out.drop_duplicates("pos").reset_index(drop=True)


def t11_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev, pl = t11_events(lab, asset, pd.Timedelta(0)), t11_events(lab, asset, pd.Timedelta(hours=3))
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = ev[np.sign(ev.r) == -side].copy()        # reversal: LONG after pre-fix selling pressure
        inv = ev[np.sign(ev.r) == side].copy()
        plc = pl[np.sign(pl.r) == -side].copy()
        for f in (edge, inv, plc):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": plc}
    return sets, lab


# --------------------------------------------------------------------------- T2: month-end rebalancing pressure

EQUITY_INDICES = ["NASDAQ", "US500", "DAX", "JPN", "HK"]


def trading_closes(lab: Labeler, asset: str) -> pd.DataFrame:
    """Weekdays with a bar within 30 min before the reference close: day, month, close bar position."""
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(REF[asset][0]).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        p = _pos_at_or_before(lab.ts, _ref_time(asset, d))
        if p >= 0:
            rows.append({"day": d, "pos": p})
    f = pd.DataFrame(rows)
    f["month"] = f.day.dt.to_period("M")
    f["rank_in_month"] = f.groupby("month").cumcount() + 1
    f["rank_from_end"] = f.groupby("month").day.rank(ascending=False, method="first").astype(int)
    return f


def t2_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    tc = trading_closes(lab, asset)
    months = sorted(tc.month.unique())
    prev_close = {}
    for m_prev, m in zip(months[:-1], months[1:]):
        last_prev = tc[tc.month == m_prev]
        prev_close[m] = lab.cl[int(last_prev.pos.iloc[-1])]
    tc = tc[tc.month.isin(prev_close)].copy()
    tc["r"] = np.log(lab.cl[tc.pos.to_numpy()] / tc.month.map(prev_close).to_numpy(dtype="float64"))
    tc = tc[np.isfinite(tc.r) & (tc.r != 0)]
    tc["Datetime"] = to_datetime_iso(lab.ts[tc.pos.to_numpy()], utc=True)
    end = tc[tc.rank_from_end <= 4]
    mid = tc[tc.rank_in_month.between(8, 11)]
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        # month-end pressure goes AGAINST the month-to-date move: LONG after a down month (r < 0)
        edge = end[np.sign(end.r) == -side].copy()
        inv = end[np.sign(end.r) == side].copy()             # C1: opposite month-to-date sign, same side
        mid_s = mid[np.sign(mid.r) == -side].copy()          # C2: same rule, trading days 8-11
        for f in (edge, inv, mid_s):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": mid_s}
    return sets, lab


# --------------------------------------------------------------------------- T12: cash-open gap acceptance
# Card: docs/mldp/campaign/NASDAQ_2026-11/T12_open_gap_acceptance/PREREGISTRATION.md (our own thesis).

T12_OPEN = {"NASDAQ": ("America/New_York", 9, 30), "US500": ("America/New_York", 9, 30), "DAX": ("Europe/Berlin", 9, 0),
            "JPN": ("Asia/Tokyo", 9, 0), "HK": ("Asia/Hong_Kong", 9, 30)}
T12_MEDIAN_DAYS = 60


def t12_days(lab: Labeler, asset: str) -> pd.DataFrame:
    cols = ["day", "pos", "Datetime", "r", "a", "big"]
    if asset not in T12_OPEN:
        return pd.DataFrame(columns=cols)
    tz, hh, mm = T12_OPEN[asset]
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows, hist = [], []
    for prev, day in zip(days[:-1], days[1:]):
        t_open = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm, tz=tz).tz_convert("UTC")
        pc = _pos_at_or_before(lab.ts, _ref_time(asset, prev))
        po = _pos_at_or_before(lab.ts, t_open)
        pe = _pos_at_or_after(lab.ts, t_open + pd.Timedelta(minutes=30))
        if pc < 0 or po < 0 or pe < 0 or not (pc < po < pe):
            continue
        g = float(np.log(lab.cl[po] / lab.cl[pc]))
        a = float(np.log(lab.cl[pe] / lab.cl[po]))
        if g == 0 or a == 0 or not (np.isfinite(g) and np.isfinite(a)):
            continue
        if len(hist) >= T12_MEDIAN_DAYS:
            big = abs(g) >= float(np.median(hist[-T12_MEDIAN_DAYS:]))      # strictly earlier days only
            rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": g, "a": a, "big": big})
        hist.append(abs(g))
    return pd.DataFrame(rows, columns=cols)


def t12_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    d = t12_days(lab, asset)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        gs = np.sign(d.r.astype(float)) == side
        acc = np.sign(d.a.astype(float)) == np.sign(d.r.astype(float))
        big = d.big.astype(bool)
        edge = d[gs & big & acc].copy()
        inv = d[gs & big & ~acc].copy()                                    # C1: big gap rejected, same side
        small = d[gs & ~big & acc].copy()                                  # C2: small gap accepted, same side
        for f in (edge, inv, small):
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": small}
    return sets, lab


# --------------------------------------------------------------------------- T13: round-level stop-cascade continuation
# Card: docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md (our own thesis, 10 assets).

T13_SPACING = {"NASDAQ": 100.0, "US500": 50.0, "DAX": 100.0, "JPN": 500.0, "HK": 500.0, "XAUUSD": 10.0,
               "GBPJPY": 1.0, "USDMXN": 0.10, "USOIL": 1.0, "BTCUSD": 1000.0}
T13_OFFSET = 0.37            # C1 grid offset, fraction of the spacing (not a half / quarter figure)
T13_NEAR = 0.25              # C2 near-miss distance, multiples of Event_Vol * Close
T13_MAX_GAP = pd.Timedelta(hours=2)


def t13_events(lab: Labeler, asset: str) -> dict[str, pd.DataFrame]:
    s = T13_SPACING[asset]
    idx = pd.to_datetime(lab.ts, utc=True)
    h24 = pd.Series(lab.hi, index=idx).rolling("24h", closed="left").max().to_numpy()
    l24 = pd.Series(lab.lo, index=idx).rolling("24h", closed="left").min().to_numpy()
    cont = np.r_[False, np.diff(lab.ts) <= T13_MAX_GAP.value]
    ok = cont & np.isfinite(h24) & np.isfinite(l24) & np.isfinite(lab.vol) & (lab.vol > 0)
    delta = T13_NEAR * lab.vol * lab.cl
    out = {}
    for name, o in (("round", 0.0), ("shifted", T13_OFFSET * s)):
        up = (np.floor((h24 - o) / s) + 1.0) * s + o
        dn = (np.ceil((l24 - o) / s) - 1.0) * s + o
        out[f"{name}_L"] = np.flatnonzero(ok & (lab.cl >= up))
        out[f"{name}_S"] = np.flatnonzero(ok & (lab.cl <= dn))
        if name == "round":
            out["near_L"] = np.flatnonzero(ok & (lab.hi >= up - delta) & (lab.cl < up) & (h24 < up - delta))
            out["near_S"] = np.flatnonzero(ok & (lab.lo <= dn + delta) & (lab.cl > dn) & (l24 > dn + delta))
    frames = {}
    for k, pos in out.items():
        dt = pd.to_datetime(lab.ts[pos], utc=True)
        frames[k] = pd.DataFrame({"day": dt.normalize().tz_localize(None), "pos": pos, "Datetime": dt, "r": 0.0})
    return frames


def t13_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t13_events(lab, asset)
    sets = {}
    for side, name, k in ((1, "LONG", "L"), (-1, "SHORT", "S")):
        edge, inv, near = ev[f"round_{k}"].copy(), ev[f"shifted_{k}"].copy(), ev[f"near_{k}"].copy()
        for f in (edge, inv, near):
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv, "C2": near}
    return sets, lab


# --------------------------------------------------------------------------- T14: high-vol liquidity-provision reversal
# Card: docs/mldp/campaign/NASDAQ_2026-11/T14_high_vol_liquidity_provision/PREREGISTRATION.md (our own thesis, 10 assets).

T14_Z = 2.0
T14_Z_MID = 1.0
T14_REGIME_SPAN = 1000          # bars (~20 trading days at 50 bars/day)
T14_REGIME_DAYS = 250
T14_REGIME_MIN_DAYS = 120


def t14_frame(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    n = len(ts)
    ref = np.searchsorted(ts, ts - pd.Timedelta(hours=1).value, side="right") - 1
    ok_ref = ref >= 0
    ref_c = np.clip(ref, 0, None)
    fresh_ref = ok_ref & ((ts - pd.Timedelta(hours=1).value) - ts[ref_c] <= MAX_STALE.value)
    r = np.where(fresh_ref, np.log(lab.cl / lab.cl[ref_c]), np.nan)
    v_prev = np.r_[np.nan, lab.vol[:-1]]
    z = r / v_prev
    z_prev = np.r_[np.nan, z[:-1]]
    big_fresh = (np.abs(z) >= T14_Z) & (np.abs(z_prev) < T14_Z)
    mid = (np.abs(z) >= T14_Z_MID) & (np.abs(z) < T14_Z)
    reg = calendar_time_event_vol(ts, lab.cl, hours=24.0, ewm_span=T14_REGIME_SPAN, min_vol=MIN_VOL)
    dt = pd.to_datetime(ts, utc=True)
    daily = pd.Series(reg, index=dt).groupby(dt.normalize()).last().dropna()
    med = daily.rolling(f"{T14_REGIME_DAYS}D", min_periods=T14_REGIME_MIN_DAYS).median().shift(1, freq="D")
    # median over the 250 days ending the PREVIOUS UTC day with data (ffill: Monday reads Friday's window)
    med_at = med.reindex(dt.normalize(), method="ffill").to_numpy()
    high = reg > med_at
    known = np.isfinite(reg) & np.isfinite(med_at) & np.isfinite(z)
    return pd.DataFrame({"pos": np.arange(n), "Datetime": dt, "z": z, "big": big_fresh & known,
                         "mid": mid & known, "high": high & known, "low": (~high) & known})


def _space(f: pd.DataFrame, lab: Labeler, gap: pd.Timedelta) -> pd.DataFrame:
    keep, last = [], None
    for p in f.pos.to_numpy():
        if last is None or lab.ts[p] - lab.ts[last] >= gap.value:
            keep.append(p)
            last = p
    return f[f.pos.isin(keep)].copy()


def t14_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    d = t14_frame(lab)
    gap = pd.Timedelta(hours=cell.vertical_h)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        fade = np.sign(d.z) == -side                      # LONG fades a drop, SHORT fades a rise
        edge = _space(d[d.big & d.high & fade], lab, gap)
        low = _space(d[d.big & d.low & fade], lab, gap)
        mid = _space(d[d.mid & d.high & fade], lab, gap)
        for f in (edge, low, mid):
            f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
            f["r"] = f.z
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": low, "C2": mid}
    return sets, lab


# --------------------------------------------------------------------------- T15: same-hour intraday periodicity
# Card: docs/mldp/campaign/NASDAQ_2026-11/T15_same_hour_periodicity/PREREGISTRATION.md (our own thesis, 10 assets).

T15_K = 10
T15_T = 1.0


def t15_frame(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    hr = pd.Timedelta(hours=1).value
    stale = MAX_STALE.value
    grid = np.arange((ts[0] // hr + 1) * hr, (ts[-1] // hr) * hr + 1, hr, dtype="int64")
    pp = np.searchsorted(ts, grid, side="right") - 1
    fresh = (pp >= 0) & (grid - ts[np.clip(pp, 0, None)] <= stale)
    P = np.where(fresh, lab.cl[np.clip(pp, 0, None)], np.nan)
    r = np.full(len(grid), np.nan)
    moved = pp[1:] != pp[:-1]                                  # a new bar inside the hour (not an idle / closed hour)
    r[:-1] = np.where(moved, np.log(P[1:] / P[:-1]), np.nan)
    hour = (grid // hr) % 24
    T = np.full(len(grid), np.nan)
    for h in range(24):
        idx = np.flatnonzero((hour == h) & np.isfinite(r))
        if len(idx) <= T15_K:
            continue
        s = pd.Series(r[idx])
        m = s.rolling(T15_K).mean().shift(1)
        sd = s.rolling(T15_K).std().shift(1)
        T[idx] = (m / (sd / np.sqrt(T15_K))).where(sd > 0).to_numpy()
    T_prev = np.r_[np.nan, T[:-1]]
    pe = np.searchsorted(ts, grid, side="left")
    ok_e = (pe < len(ts)) & np.isfinite(r)
    pe_c = np.clip(pe, 0, len(ts) - 1)
    ok_e &= (ts[pe_c] - grid) <= stale
    return pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "T": T, "T_prev": T_prev,
                         "ok": ok_e, "grid": grid})


def t15_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    d = t15_frame(lab)
    d = d[d.ok].copy()
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = d[(d["T"].abs() >= T15_T) & (np.sign(d["T"]) == side)]
        c1 = d[(d["T_prev"].abs() >= T15_T) & (np.sign(d["T_prev"]) == side)]
        c2 = d[(d["T"].abs() < T15_T) & (np.sign(d["T"]) == side)]
        out = []
        for f in (edge, c1, c2):
            f = f.drop_duplicates("pos").copy()
            f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
            f["r"] = f["T"]
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T16: session hand-over continuation
# Card: docs/mldp/campaign/NASDAQ_2026-11/T16_session_handover_continuation/PREREGISTRATION.md (our own thesis).

T16_BOUNDARIES = [  # name, (tz, hh, mm) of B, (tz, hh, mm) of A, A on previous weekday?
    ("london", ("Europe/London", 8, 0), ("Asia/Tokyo", 9, 0), False),
    ("newyork", ("America/New_York", 8, 0), ("Europe/London", 8, 0), False),
    ("tokyo", ("Asia/Tokyo", 9, 0), ("America/New_York", 8, 0), True),
]
T16_MID_SHIFT = pd.Timedelta(hours=3)


def _local(day: pd.Timestamp, tz: str, hh: int, mm: int) -> pd.Timestamp:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm, tz=tz).tz_convert("UTC")


def t16_events(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").normalize().tz_localize(None) + pd.Timedelta(days=1)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows = []
    for prev, day in zip(days[:-1], days[1:]):
        for name, (btz, bh, bm), (atz, ah, am), a_prev in T16_BOUNDARIES:
            B = _local(day, btz, bh, bm)
            A = _local(prev if a_prev else day, atz, ah, am)
            if not A < B:
                continue
            for kind, b, a in (("edge", B, A), ("mid", B + T16_MID_SHIFT, B + T16_MID_SHIFT - (B - A))):
                pa, pb, pe = _pos_at_or_before(lab.ts, a), _pos_at_or_before(lab.ts, b), _pos_at_or_after(lab.ts, b)
                if min(pa, pb, pe) < 0 or not pa < pb:
                    continue
                s = float(np.log(lab.cl[pb] / lab.cl[pa]))
                if s == 0 or not np.isfinite(s):
                    continue
                rows.append({"day": day, "boundary": name, "kind": kind, "pos": pe,
                             "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": s})
    ev = pd.DataFrame(rows, columns=["day", "boundary", "kind", "pos", "Datetime", "r"])
    edge = ev[ev.kind == "edge"].sort_values(["boundary", "day"]).copy()
    edge["r_prev"] = edge.groupby("boundary").r.shift(1)            # the previous weekday's signal at this boundary
    return pd.concat([edge, ev[ev.kind == "mid"]], ignore_index=True)


def t16_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t16_events(lab)
    e, m = ev[ev.kind == "edge"], ev[ev.kind == "mid"]
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = e[np.sign(e.r) == side]
        mid = m[np.sign(m.r) == side]
        stale = e[np.sign(e.r_prev) == side]
        out = []
        for f in (edge, mid, stale):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T17: Bitcoin perpetual-funding cycle
# Card: docs/mldp/campaign/NASDAQ_2026-11/T17_btc_funding_cycle/PREREGISTRATION.md (BTCUSD; other assets = placebo).

T17_HOURS = (0, 8, 16)
T17_MID = pd.Timedelta(hours=4)


def t17_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    day0 = (ts[0] // pd.Timedelta(days=1).value + 2) * pd.Timedelta(days=1).value
    rows = []
    for kind, off in (("F", 0), ("mid", T17_MID.value)):
        t = np.array([day0 + h * pd.Timedelta(hours=1).value + off + k * pd.Timedelta(days=1).value
                      for k in range(int((ts[-1] - day0) // pd.Timedelta(days=1).value)) for h in T17_HOURS], dtype="int64")
        pb = np.searchsorted(ts, t, side="right") - 1
        pa = np.searchsorted(ts, t - pd.Timedelta(hours=24).value, side="right") - 1
        pe = np.searchsorted(ts, t, side="left")
        ok = (pa >= 0) & (pb >= 0) & (pe < len(ts))
        pa_c, pb_c, pe_c = np.clip(pa, 0, None), np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
        ok &= (t - ts[pb_c] <= stale) & ((t - pd.Timedelta(hours=24).value) - ts[pa_c] <= stale) & (ts[pe_c] - t <= stale)
        q = np.log(lab.cl[pb_c] / lab.cl[pa_c])
        ok &= np.isfinite(q) & (q != 0) & (pa_c < pb_c)
        rows.append(pd.DataFrame({"kind": kind, "pos": pe_c[ok], "Datetime": pd.to_datetime(ts[pe_c[ok]], utc=True),
                                  "r": q[ok]}))
    return pd.concat(rows, ignore_index=True)


def t17_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t17_events(lab)
    f_, m_ = ev[ev.kind == "F"], ev[ev.kind == "mid"]
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        out = []
        for f in (f_[np.sign(f_.r) == side], m_[np.sign(m_.r) == side], f_[np.sign(f_.r) == -side]):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T18: overnight drift vs the cash session
# Card: docs/mldp/campaign/NASDAQ_2026-11/T18_overnight_drift/PREREGISTRATION.md (5 cash equity indices).


def t18_entries(lab: Labeler, asset: str) -> pd.DataFrame:
    cols = ["day", "kind", "pos", "Datetime", "r"]
    if asset not in T12_OPEN:
        return pd.DataFrame(columns=cols)
    tz, oh, om = T12_OPEN[asset]
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        for kind, t in (("close", _ref_time(asset, d)),
                        ("open", pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=oh, minute=om, tz=tz).tz_convert("UTC"))):
            p = _pos_at_or_after(lab.ts, t)
            if p >= 0:
                rows.append({"day": d, "kind": kind, "pos": p, "Datetime": pd.Timestamp(lab.ts[p], tz="UTC"), "r": 0.0})
    return pd.DataFrame(rows, columns=cols)


def t18_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t18_entries(lab, asset)
    cl, op = ev[ev.kind == "close"], ev[ev.kind == "open"]
    sets = {}
    for side, name, e, c in ((1, "LONG", cl, op), (-1, "SHORT", op, cl)):
        out = []
        for f in (e, c):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T19: Tokyo fix yen demand (GBPJPY)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T19_tokyo_fix_jpy_demand/PREREGISTRATION.md (GBPJPY; others = placebo).

TOKYO = "Asia/Tokyo"


def _gotobi(d: pd.Timestamp) -> bool:
    """d is a naive local date (weekday). Gotobi: 5/10/15/20/25/month-end, rolled back to Friday from a weekend."""
    month_end = (d + pd.offsets.MonthEnd(0)).day
    for k in (5, 10, 15, 20, 25, month_end):
        if k > month_end:
            continue
        g = pd.Timestamp(year=d.year, month=d.month, day=k)
        while g.weekday() >= 5:
            g -= pd.Timedelta(days=1)
        if g == d:
            return True
    return False


def t19_entries(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(TOKYO).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(TOKYO).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        g = _gotobi(d)
        for kind, hh, mm in (("L", 8, 55), ("S", 9, 55), ("L_c1", 10, 55), ("S_c1", 11, 55)):
            t = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hh, minute=mm, tz=TOKYO).tz_convert("UTC")
            p = _pos_at_or_after(lab.ts, t)
            if p >= 0:
                rows.append({"day": d, "kind": kind, "gotobi": g, "pos": p, "Datetime": pd.Timestamp(lab.ts[p], tz="UTC"), "r": 0.0})
    return pd.DataFrame(rows, columns=["day", "kind", "gotobi", "pos", "Datetime", "r"])


def t19_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t19_entries(lab)
    sets = {}
    for side, name, k in ((1, "LONG", "L"), (-1, "SHORT", "S")):
        e = ev[ev.kind == k]
        out = []
        for f in (e, ev[ev.kind == f"{k}_c1"], e[~e.gotobi.astype(bool)]):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T20: gold Asia vs New York session
# Card: docs/mldp/campaign/NASDAQ_2026-11/T20_gold_asia_vs_ny_session/PREREGISTRATION.md (XAUUSD; others = placebo).


def t20_entries(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        for kind, tz, hh, mm in (("asia", "Asia/Shanghai", 9, 0), ("ny", NY, 8, 20)):
            t = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hh, minute=mm, tz=tz).tz_convert("UTC")
            p = _pos_at_or_after(lab.ts, t)
            if p >= 0:
                rows.append({"day": d, "kind": kind, "pos": p, "Datetime": pd.Timestamp(lab.ts[p], tz="UTC"), "r": 0.0})
    return pd.DataFrame(rows, columns=["day", "kind", "pos", "Datetime", "r"])


def t20_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t20_entries(lab)
    asia, ny = ev[ev.kind == "asia"], ev[ev.kind == "ny"]
    sets = {}
    for side, name, e, c in ((1, "LONG", asia, ny), (-1, "SHORT", ny, asia)):
        out = []
        for f in (e, c):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T21: overnight risk premium, non-equity
# Card: docs/mldp/campaign/NASDAQ_2026-11/T21_risk_premium_overnight_non_equity/PREREGISTRATION.md

T21_RISK_ON = {"GBPJPY": 1, "USOIL": 1, "BTCUSD": 1, "USDMXN": -1}


def t21_entries(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(NY).normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        for kind, hh, mm in (("close", 16, 0), ("open", 9, 30)):
            p = _pos_at_or_after(lab.ts, _ny(d, hh, mm))
            if p >= 0:
                rows.append({"day": d, "kind": kind, "pos": p, "Datetime": pd.Timestamp(lab.ts[p], tz="UTC"), "r": 0.0})
    return pd.DataFrame(rows, columns=["day", "kind", "pos", "Datetime", "r"])


def t21_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t21_entries(lab)
    cl, op = ev[ev.kind == "close"], ev[ev.kind == "open"]
    d = T21_RISK_ON.get(asset, 1)            # placebo assets: plain T18 orientation (LONG overnight)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        # side == d: the risk-on overnight edge (close entry), C1 = open entry; side == -d: the risk-off session edge
        e, c = (cl, op) if side == d else (op, cl)
        out = []
        for f in (e, c):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T22: weekly time-series momentum
# Card: docs/mldp/campaign/NASDAQ_2026-11/T22_weekly_time_series_momentum/PREREGISTRATION.md (10 assets, both sides).

T22_HOURS = (0, 8, 16)                 # UTC check times
T22_LOOKBACK = pd.Timedelta(hours=168)
T22_Z = 0.5
T22_NORM_DAYS = "365D"
T22_NORM_MIN = 100


def t22_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d = pd.Timedelta(days=1).value
    day0 = (ts[0] // one_d + 8) * one_d
    t = np.array([day0 + h * pd.Timedelta(hours=1).value + k * one_d
                  for k in range(int((ts[-1] - day0) // one_d)) for h in T22_HOURS], dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1                    # last bar closing at or before t (signal)
    pa = np.searchsorted(ts, t - T22_LOOKBACK.value, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")                         # entry: first bar closing at or after t
    ok = (pa >= 0) & (pb >= 0) & (pe < len(ts))
    pa_c, pb_c, pe_c = np.clip(pa, 0, None), np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & ((t - T22_LOOKBACK.value) - ts[pa_c] <= stale) & (ts[pe_c] - t <= stale)
    q = np.where(ok, np.log(lab.cl[pb_c] / lab.cl[pa_c]), np.nan)
    ok &= np.isfinite(q)
    f = pd.DataFrame({"pos": pe_c[ok], "Datetime": pd.to_datetime(ts[pe_c[ok]], utc=True), "q": q[ok]})
    # normaliser: std of the same 1-week return over the PRIOR 365 days of checks (strictly before this check)
    sd = f.set_index("Datetime").q.rolling(T22_NORM_DAYS, closed="left", min_periods=T22_NORM_MIN).std().to_numpy()
    f["r"] = f.q / sd
    f = f[np.isfinite(f.r)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t22_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t22_events(lab)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        trend = side * ev.r
        out = []
        for m in (trend >= T22_Z, trend <= -T22_Z, trend.abs() < T22_Z):   # edge, C1 (counter-trend), C2 (weak trend)
            f = ev[m].copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T23: domestic-hours currency weakness
# Card: docs/mldp/campaign/NASDAQ_2026-11/T23_domestic_hours_currency_weakness/PREREGISTRATION.md (GBPJPY).

T23_SESSIONS = (("tokyo", TOKYO, 9, 0), ("london", "Europe/London", 8, 0), ("ny", NY, 9, 0))


def t23_entries(lab: Labeler) -> pd.DataFrame:
    first = pd.Timestamp(lab.ts[0], tz="UTC").normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").normalize().tz_localize(None)
    rows = []
    for d in pd.date_range(first, last, freq="D"):
        if d.weekday() >= 5:
            continue
        for kind, tz, hh, mm in T23_SESSIONS:
            t = pd.Timestamp(year=d.year, month=d.month, day=d.day, hour=hh, minute=mm, tz=tz).tz_convert("UTC")
            p = _pos_at_or_after(lab.ts, t)
            if p >= 0:
                rows.append({"day": d, "kind": kind, "pos": p, "Datetime": pd.Timestamp(lab.ts[p], tz="UTC"), "r": 0.0})
    return pd.DataFrame(rows, columns=["day", "kind", "pos", "Datetime", "r"])


def t23_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t23_entries(lab)
    tk, ld, ny = ev[ev.kind == "tokyo"], ev[ev.kind == "london"], ev[ev.kind == "ny"]
    sets = {}
    for side, name, e, c1 in ((1, "LONG", tk, ld), (-1, "SHORT", ld, tk)):
        out = []
        for f in (e, c1, ny):
            f = f.drop_duplicates("pos").sort_values("pos").copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1], "C2": out[2]}
    return sets, lab


# --------------------------------------------------------------------------- T24: volatility-managed premium
# Card: docs/mldp/campaign/NASDAQ_2026-11/T24_volatility_managed_premium/PREREGISTRATION.md (5 equity indices).

T24_HOURS = (0, 4, 8, 12, 16, 20)
T24_WIN, T24_WIN_MIN = 120, 60
T24_NORM_DAYS, T24_NORM_MIN = "365D", 100


def t24_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(lab.cl, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()   # close of each UTC hour, stamped at hour end
    hr = np.log(hc).diff()
    hr = hr[hr.index.to_series().diff() <= pd.Timedelta(hours=1)]           # only consecutive hours
    sig = hr.rolling(T24_WIN, min_periods=T24_WIN_MIN).std()
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
    t, pe_c = t[ok], pe_c[ok]
    si = sig.index.asi8 if hasattr(sig.index, "asi8") else sig.index.view("int64")
    ps = np.searchsorted(si, t, side="right") - 1                           # last full hour ending at or before t
    v = np.where(ps >= 0, sig.to_numpy()[np.clip(ps, 0, None)], np.nan)
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "sigma": v,
                      "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.sigma)].copy()
    f["med"] = f.set_index("t").sigma.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["r"] = f.sigma / f.med - 1.0
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t24_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t24_events(lab)
    low, high = ev[ev.sigma < ev.med], ev[ev.sigma >= ev.med]
    sets = {}
    for side, name, e, c in ((1, "LONG", low, high), (-1, "SHORT", high, low)):
        out = []
        for f in (e, c):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T25: 52-week-high anchor
# Card: docs/mldp/campaign/NASDAQ_2026-11/T25_52week_high_anchoring/PREREGISTRATION.md (5 equity indices).


def t25_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(lab.cl, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hi = hc.rolling("365D").max()
    warm = hc.index[0] + pd.Timedelta(days=365)
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale) & (t >= warm.value)
    t, pb_c, pe_c = t[ok], pb_c[ok], pe_c[ok]
    hix = hi.index.asi8 if hasattr(hi.index, "asi8") else hi.index.view("int64")
    ph = np.searchsorted(hix, t, side="right") - 1                         # last full hour ending at or before t
    h = np.where(ph >= 0, hi.to_numpy()[np.clip(ph, 0, None)], np.nan)
    x = np.log(lab.cl[pb_c] / np.maximum(h, lab.cl[pb_c]))                 # the signal close counts toward the high
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "x": x,
                      "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.x)].copy()
    f["med"] = f.set_index("t").x.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["r"] = f.x - f.med
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t25_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t25_events(lab)
    near, far = ev[ev.x >= ev.med], ev[ev.x < ev.med]
    sets = {}
    for side, name, e, c in ((1, "LONG", near, far), (-1, "SHORT", far, near)):
        out = []
        for f in (e, c):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T30: 10-day trading-range breakout
# Card: docs/mldp/campaign/NASDAQ_2026-11/T30_trading_range_breakout/PREREGISTRATION.md (all 10 assets).
# Every hourly UTC check t (on the hour): c = last close at or before t; H / L = max / min hourly close over the 10 days
# BEFORE the last full hour ending at or before t (that hour excluded). LONG edge: c > H (new 10-day closing high); C1: c <= H20
# and c > (H20 + L20) / 2 (upper half of the range, no break). SHORT mirrors (c < L20; C1 c >= L20 and c < mid).

T30_DAYS = "10D"
T30_HOURS = tuple(range(24))   # hourly checks: chosen with 10D from event COUNTS only (>= 2/week, D092), before any returns


def t30_events(ts: np.ndarray, close: np.ndarray) -> pd.DataFrame:
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hi = hc.rolling(T30_DAYS, closed="left").max()
    lo = hc.rolling(T30_DAYS, closed="left").min()
    warm = hc.index[0] + pd.Timedelta(T30_DAYS)
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T30_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale) & (t >= warm.value)
    t, pb_c, pe_c = t[ok], pb_c[ok], pe_c[ok]
    hix = hc.index.asi8 if hasattr(hc.index, "asi8") else hc.index.view("int64")
    ph = np.searchsorted(hix, t, side="right") - 1
    phc = np.clip(ph, 0, None)
    h20 = np.where(ph >= 0, hi.to_numpy()[phc], np.nan)
    l20 = np.where(ph >= 0, lo.to_numpy()[phc], np.nan)
    c = close[pb_c]
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "c": c, "h20": h20, "l20": l20,
                      "ref_ns": t})
    f = f[np.isfinite(f.h20) & np.isfinite(f.l20) & (f.h20 > f.l20)].drop_duplicates("pos").sort_values("pos")
    f["mid"] = 0.5 * (f.h20 + f.l20)
    f["r"] = 0.0
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f.reset_index(drop=True)


def t30_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t30_events(lab.ts, lab.cl)
    up, upc = ev[ev.c > ev.h20], ev[(ev.c <= ev.h20) & (ev.c > ev.mid)]
    dn, dnc = ev[ev.c < ev.l20], ev[(ev.c >= ev.l20) & (ev.c < ev.mid)]
    sets = {}
    for side, name, e, c1 in ((1, "LONG", up, upc), (-1, "SHORT", dn, dnc)):
        out = []
        for f in (e, c1):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T31: failed range breakout (trapped breakout traders)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T31_failed_range_breakout/PREREGISTRATION.md (all 10 assets).
# Hourly UTC checks, 5-day range (T30 machinery with a 5D window). A break = c > H (up) / c < L (down). SHORT edge: the
# first check within 24 h after an up-break at which c < H_break (the level that was broken) -- back inside the range;
# then disarmed until the next break. LONG mirrors (failed down-break: c > L_break). C1 SHORT: the first check of each
# UTC day with H - 0.1 (H - L) <= c < H and no up-break in the previous 24 h (near the top, no failed break). C1 LONG
# mirrors.

T31_DAYS, T31_WIN_NS, T31_BAND = "5D", pd.Timedelta(hours=24).value, 0.10


def t31_events(ts: np.ndarray, close: np.ndarray) -> dict[str, pd.DataFrame]:
    keep_days = globals()["T30_DAYS"]
    globals()["T30_DAYS"] = T31_DAYS
    try:
        ev = t30_events(ts, close)
    finally:
        globals()["T30_DAYS"] = keep_days
    c, H, L, t = ev.c.to_numpy(), ev.h20.to_numpy(), ev.l20.to_numpy(), ev.ref_ns.to_numpy()
    out = {}
    for name, side in (("S", -1), ("L", 1)):
        brk = (c > H) if side == -1 else (c < L)
        fail, last_t, level, armed = [], None, None, False
        last_brk = np.full(len(c), np.iinfo("int64").min, dtype="int64")
        lb = np.iinfo("int64").min
        for i in range(len(c)):
            last_brk[i] = lb                                  # most recent break strictly before check i
            if brk[i]:
                lb, last_t, level, armed = t[i], t[i], (H[i] if side == -1 else L[i]), True
                continue
            if armed and t[i] - last_t <= T31_WIN_NS:
                if (side == -1 and c[i] < level) or (side == 1 and c[i] > level):
                    fail.append(i)
                    armed = False
            elif armed:
                armed = False
        rng = H - L
        near = ((c >= H - T31_BAND * rng) & (c < H)) if side == -1 else ((c <= L + T31_BAND * rng) & (c > L))
        quiet = (t - last_brk) > T31_WIN_NS
        c1 = ev[near & quiet & ~brk].copy()
        c1 = c1.groupby("day", sort=True).head(1)
        out[f"edge_{name}"] = ev.iloc[fail].copy()
        out[f"c1_{name}"] = c1
    return out


def t31_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t31_events(lab.ts, lab.cl)
    sets = {}
    for side, name, k in ((1, "LONG", "L"), (-1, "SHORT", "S")):
        out = []
        for f in (ev[f"edge_{k}"], ev[f"c1_{k}"]):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T32: overextension reversal (Bollinger z)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T32_overextension_reversal/PREREGISTRATION.md (all 10 assets).
# Hourly closes (right-labelled); z = (hc - mean) / std over the 5 days BEFORE that hour (closed='left'). Check at each
# hour end where z first crosses up through +T32_Z (z >= Z and previous hour < Z): SHORT; first cross down through
# -T32_Z: LONG. Entry = first bar closing at or after that hour end (<= 30 min), signal close <= 30 min stale.
# C1 SHORT: first cross up through +1.0 (same direction of stretch, not extreme); C1 LONG: first cross down through -1.0.

T32_DAYS, T32_Z, T32_Z_C1 = "5D", 1.5, 1.0


def t32_events(ts: np.ndarray, close: np.ndarray) -> dict[str, pd.DataFrame]:
    stale = MAX_STALE.value
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    m = hc.rolling(T32_DAYS, closed="left").mean()
    sd = hc.rolling(T32_DAYS, closed="left").std()
    z = (hc - m) / sd
    z = z[(hc.index >= hc.index[0] + pd.Timedelta(T32_DAYS)) & np.isfinite(z.to_numpy())]
    prev = z.shift(1)
    sel = {"edge_S": (z >= T32_Z) & (prev < T32_Z), "c1_S": (z >= T32_Z_C1) & (prev < T32_Z_C1),
           "edge_L": (z <= -T32_Z) & (prev > -T32_Z), "c1_L": (z <= -T32_Z_C1) & (prev > -T32_Z_C1)}
    out = {}
    for k, mask in sel.items():
        t = z.index[mask.to_numpy()].asi8 if hasattr(z.index, "asi8") else z.index[mask.to_numpy()].view("int64")
        pb = np.searchsorted(ts, t, side="right") - 1
        pe = np.searchsorted(ts, t, side="left")
        ok = (pb >= 0) & (pe < len(ts))
        pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
        ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
        f = pd.DataFrame({"pos": pe_c[ok], "Datetime": pd.to_datetime(ts[pe_c[ok]], utc=True), "ref_ns": t[ok], "r": 0.0})
        f = f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
        f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
        out[k] = f
    return out


def t32_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t32_events(lab.ts, lab.cl)
    sets = {}
    for side, name, k in ((1, "LONG", "L"), (-1, "SHORT", "S")):
        out = []
        for f in (ev[f"edge_{k}"], ev[f"c1_{k}"]):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T33: market intraday momentum
# Card: docs/mldp/campaign/NASDAQ_2026-11/T33_market_intraday_momentum/PREREGISTRATION.md (5 equity indices).
# Weekday d (in the index's own time zone): r1 = log(close at the last bar at or before open(d) + 30 min / close at the
# last bar at or before the previous weekday's session close REF); entry = first bar closing at or after REF(d) - 30 min
# (<= 30 min after). Edge on side s: sign(r1) == s. C1: sign(r1) == -s, same side, same entry time (cause removed).

T33_OPEN = {"NASDAQ": ("America/New_York", "09:30"), "US500": ("America/New_York", "09:30"),
            "DAX": ("Europe/Berlin", "09:00"), "JPN": ("Asia/Tokyo", "09:00"), "HK": ("Asia/Hong_Kong", "09:30")}
T33_FIRST, T33_LAST = pd.Timedelta(minutes=30), pd.Timedelta(minutes=30)


def t33_days(lab: Labeler, asset: str) -> pd.DataFrame:
    tz, hhmm = T33_OPEN[asset]
    oh, om = map(int, hhmm.split(":"))
    first = pd.Timestamp(lab.ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(lab.ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows = []
    for prev, day in zip(days[:-1], days[1:]):
        op = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=oh, minute=om, tz=tz).tz_convert("UTC")
        ps = _pos_at_or_before(lab.ts, op + T33_FIRST)
        pp = _pos_at_or_before(lab.ts, _ref_time(asset, prev))
        pe = _pos_at_or_after(lab.ts, _ref_time(asset, day) - T33_LAST)
        if ps < 0 or pp < 0 or pe < 0 or not (pp < ps < pe):
            continue
        r = float(np.log(lab.cl[ps] / lab.cl[pp]))
        if r == 0 or not np.isfinite(r):
            continue
        rows.append({"day": day, "pos": pe, "Datetime": pd.Timestamp(lab.ts[pe], tz="UTC"), "r": r})
    return pd.DataFrame(rows)


def t33_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    days = t33_days(lab, asset)
    sets = {}
    for side, name in ((1, "LONG"), (-1, "SHORT")):
        edge = days[np.sign(days.r) == side].copy()
        inv = days[np.sign(days.r) == -side].copy()
        for f in (edge, inv):
            f["realized_r"] = lab.label(f.pos.to_numpy(), np.full(len(f), side))
        sets[name] = {"edge": edge, "C1": inv}
    return sets, lab


# --------------------------------------------------------------------------- T26: realized skewness / lottery demand
# Card: docs/mldp/campaign/NASDAQ_2026-11/T26_realized_skewness_lottery/PREREGISTRATION.md (all 10 assets).

T26_WIN, T26_WIN_MIN = 168, 100


def t26_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(lab.cl, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hr = np.log(hc).diff()
    hr = hr[hr.index.to_series().diff() <= pd.Timedelta(hours=1)]
    s3 = (hr ** 3).rolling(T26_WIN, min_periods=T26_WIN_MIN).sum()
    s2 = (hr ** 2).rolling(T26_WIN, min_periods=T26_WIN_MIN).sum()
    n = hr.rolling(T26_WIN, min_periods=T26_WIN_MIN).count()
    rsk = np.sqrt(n) * s3 / s2 ** 1.5
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
    t, pe_c = t[ok], pe_c[ok]
    ri = rsk.index.asi8 if hasattr(rsk.index, "asi8") else rsk.index.view("int64")
    ps = np.searchsorted(ri, t, side="right") - 1
    v = np.where(ps >= 0, rsk.to_numpy()[np.clip(ps, 0, None)], np.nan)
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "rsk": v,
                      "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.rsk)].copy()
    f["med"] = f.set_index("t").rsk.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["r"] = f.rsk - f.med
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t26_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t26_events(lab)
    low, high = ev[ev.rsk < ev.med], ev[ev.rsk >= ev.med]
    sets = {}
    for side, name, e, c in ((1, "LONG", low, high), (-1, "SHORT", high, low)):
        out = []
        for f in (e, c):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T27: frog in the pan (information discreteness)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T27_frog_in_the_pan_continuity/PREREGISTRATION.md (all 10 assets).


def t27_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(lab.cl, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hr = np.log(hc).diff()
    hr = hr[hr.index.to_series().diff() <= pd.Timedelta(hours=1)]
    pret = hr.rolling(T26_WIN, min_periods=T26_WIN_MIN).sum()
    n = hr.rolling(T26_WIN, min_periods=T26_WIN_MIN).count()
    neg = (hr < 0).astype(float).rolling(T26_WIN, min_periods=T26_WIN_MIN).sum() / n
    pos = (hr > 0).astype(float).rolling(T26_WIN, min_periods=T26_WIN_MIN).sum() / n
    idv = np.sign(pret) * (neg - pos)
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
    t, pe_c = t[ok], pe_c[ok]
    ri = idv.index.asi8 if hasattr(idv.index, "asi8") else idv.index.view("int64")
    ps = np.searchsorted(ri, t, side="right") - 1
    good = ps >= 0
    psc = np.clip(ps, 0, None)
    v = np.where(good, idv.to_numpy()[psc], np.nan)
    pr = np.where(good, pret.to_numpy()[psc], np.nan)
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "idv": v, "pret": pr,
                      "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.idv) & np.isfinite(f.pret) & (f.pret != 0)].copy()
    f["wk"] = np.where(f.pret > 0, 1, -1)
    parts = []
    for sgn in (1, -1):
        g = f[f.wk == sgn].copy()
        g["med"] = g.set_index("t").idv.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
        parts.append(g)
    f = pd.concat(parts)
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["r"] = f.idv - f.med
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t27_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t27_events(lab)
    up, dn = ev[ev.wk == 1], ev[ev.wk == -1]
    sets = {}
    for side, name, w in ((1, "LONG", up), (-1, "SHORT", dn)):
        out = []
        for f in (w[w.idv < w.med], w[w.idv >= w.med]):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T28: Halloween season (Sell in May)
# Card: docs/mldp/campaign/NASDAQ_2026-11/T28_halloween_season/PREREGISTRATION.md (equity indices).
T28_WINTER = (11, 12, 1, 2, 3, 4)


def t28_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts))
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
    t, pe_c = t[ok], pe_c[ok]
    tt = pd.to_datetime(t, utc=True)
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "t": tt,
                      "winter": np.isin(tt.month, T28_WINTER)})
    f = f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t28_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t28_events(lab)
    win, summ = ev[ev.winter], ev[~ev.winter]
    sets = {}
    for side, name, e, c in ((1, "LONG", win, summ), (-1, "SHORT", summ, win)):
        out = []
        for f in (e, c):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


# --------------------------------------------------------------------------- T29: 12-month time-series momentum
# Card: docs/mldp/campaign/NASDAQ_2026-11/T29_12month_time_series_momentum/PREREGISTRATION.md (all 10 assets).


def t29_events(lab: Labeler) -> pd.DataFrame:
    ts = lab.ts
    stale = MAX_STALE.value
    one_d, one_h = pd.Timedelta(days=1).value, pd.Timedelta(hours=1).value
    s = pd.Series(lab.cl, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hix = hc.index.asi8 if hasattr(hc.index, "asi8") else hc.index.view("int64")
    warm = hix[0] + 365 * one_d
    day0 = (ts[0] // one_d + 1) * one_d
    t = np.array([day0 + h * one_h + k * one_d for k in range(int((ts[-1] - day0) // one_d)) for h in T24_HOURS],
                 dtype="int64")
    pb = np.searchsorted(ts, t, side="right") - 1
    pe = np.searchsorted(ts, t, side="left")
    ok = (pb >= 0) & (pe < len(ts)) & (t >= warm)
    pb_c, pe_c = np.clip(pb, 0, None), np.clip(pe, 0, len(ts) - 1)
    ok &= (t - ts[pb_c] <= stale) & (ts[pe_c] - t <= stale)
    t, pb_c, pe_c = t[ok], pb_c[ok], pe_c[ok]
    p0 = np.searchsorted(hix, t - 365 * one_d, side="right") - 1
    good = p0 >= 0
    c0 = np.where(good, hc.to_numpy()[np.clip(p0, 0, None)], np.nan)
    r12 = np.log(lab.cl[pb_c] / c0)
    f = pd.DataFrame({"pos": pe_c, "Datetime": pd.to_datetime(ts[pe_c], utc=True), "r12": r12,
                      "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.r12) & (f.r12 != 0)].drop_duplicates("pos").sort_values("pos").reset_index(drop=True)
    f["day"] = f.Datetime.dt.normalize().dt.tz_localize(None)
    return f


def t29_sets(asset: str, cell: Cell) -> tuple[dict, Labeler]:
    lab = Labeler(load_bars(asset), cell)
    ev = t29_events(lab)
    up, dn = ev[ev.r12 > 0], ev[ev.r12 < 0]
    sets = {}
    for side, name, e, c in ((1, "LONG", up, dn), (-1, "SHORT", dn, up)):
        out = []
        for f in (e, c):
            f = f.copy()
            f["realized_r"] = lab.label(f.pos.to_numpy(dtype=int), np.full(len(f), side))
            out.append(f)
        sets[name] = {"edge": out[0], "C1": out[1]}
    return sets, lab


THESES: dict[str, dict] = {
    # D093 (owner 2026-11-10): cards from T30 on pre-register their Gate 0 cell with EWM 150 (the only span left in the
    # Gate A grid, shared/research/gate_a_grid.sh). Cards T1-T29 keep their pre-registered EWM 100 cell.
    "T33": {"build": t33_sets, "cell": Cell(1.0, 150, 1.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T33_market_intraday_momentum/PREREGISTRATION.md"},
    "T32": {"build": t32_sets, "cell": Cell(4.0, 150, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T32_overextension_reversal/PREREGISTRATION.md"},
    "T31": {"build": t31_sets, "cell": Cell(4.0, 150, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T31_failed_range_breakout/PREREGISTRATION.md"},
    "T30": {"build": t30_sets, "cell": Cell(4.0, 150, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T30_trading_range_breakout/PREREGISTRATION.md"},
    "T29": {"build": t29_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T29_12month_time_series_momentum/PREREGISTRATION.md"},
    "T28": {"build": t28_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T28_halloween_season/PREREGISTRATION.md"},
    "T27": {"build": t27_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T27_frog_in_the_pan_continuity/PREREGISTRATION.md"},
    "T26": {"build": t26_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T26_realized_skewness_lottery/PREREGISTRATION.md"},
    "T25": {"build": t25_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T25_52week_high_anchoring/PREREGISTRATION.md"},
    "T24": {"build": t24_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T24_volatility_managed_premium/PREREGISTRATION.md"},
    "T23": {"build": t23_sets, "cell": Cell(4.0, 100, 6.0), "counterfactuals": ["C1", "C2"], "universe": ["GBPJPY"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T23_domestic_hours_currency_weakness/PREREGISTRATION.md"},
    "T22": {"build": t22_sets, "cell": Cell(8.0, 100, 24.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T22_weekly_time_series_momentum/PREREGISTRATION.md"},
    "T21": {"build": t21_sets, "cell": Cell(4.0, 100, 16.0), "counterfactuals": ["C1"],
            "universe": ["GBPJPY", "USOIL", "BTCUSD", "USDMXN"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T21_risk_premium_overnight_non_equity/PREREGISTRATION.md"},
    "T20": {"build": t20_sets, "cell": Cell(4.0, 100, 4.0), "counterfactuals": ["C1"], "universe": ["XAUUSD"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T20_gold_asia_vs_ny_session/PREREGISTRATION.md"},
    "T19": {"build": t19_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["GBPJPY"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T19_tokyo_fix_jpy_demand/PREREGISTRATION.md"},
    "T18": {"build": t18_sets, "cell": Cell(4.0, 100, 16.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T18_overnight_drift/PREREGISTRATION.md"},
    "T17": {"build": t17_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["BTCUSD"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T17_btc_funding_cycle/PREREGISTRATION.md"},
    "T16": {"build": t16_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T16_session_handover_continuation/PREREGISTRATION.md"},
    "T15": {"build": t15_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T15_same_hour_periodicity/PREREGISTRATION.md"},
    "T14": {"build": t14_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T14_high_vol_liquidity_provision/PREREGISTRATION.md"},
    "T13": {"build": t13_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T13_round_level_stop_cascade/PREREGISTRATION.md"},
    "T1": {"build": t1_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T1_session_close_hedging_flow/PREREGISTRATION.md"},
    # owner-ordered post-hoc re-test (2026-11-04): same card, pooled over the US equity-index universe (rule C amended)
    "T1b": {"build": t1_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
            "post_hoc": True,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T1b_session_close_hedging_flow_US/PREREGISTRATION.md"},
    # owner-ordered post-hoc re-tests (2026-11-04): T1 / T1b with the causal (live-tradable) entry bar
    "T1c": {"build": t1c_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
            "post_hoc": True,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T1c_causal_entry_retest/PREREGISTRATION.md"},
    "T1bc": {"build": t1c_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"],
             "universe": ["NASDAQ", "US500"], "post_hoc": True,
             "card": "docs/mldp/campaign/NASDAQ_2026-11/T1c_causal_entry_retest/PREREGISTRATION.md"},
    "T3": {"build": t3_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T3_overnight_inventory_reversal/PREREGISTRATION.md"},
    "T4": {"build": t4_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1"], "universe": EQUITY_INDICES,
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T4_month_end_liquidity_cycle/PREREGISTRATION.md"},
    "T5": {"build": t5_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T5_vol_target_rebalancing_flow/PREREGISTRATION.md"},
    "T6": {"build": t6_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T6_dealer_gamma_regime/PREREGISTRATION.md"},
    "T7": {"build": t7_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T7_opex_hedge_cycle/PREREGISTRATION.md"},
    "T8": {"build": t8_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": ["NASDAQ", "US500"],
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T8_release_derisk_rerisk/PREREGISTRATION.md"},
    "T9": {"build": t9_sets, "cell": Cell(1.0, 100, 4.0), "counterfactuals": ["C1", "C2"], "universe": ASSETS,
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T9_release_reaction_continuation/PREREGISTRATION.md"},
    "T10": {"build": t10_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"],
            "universe": [a for a in ASSETS if a != "BTCUSD"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T10_weekend_derisking/PREREGISTRATION.md"},
    "T11": {"build": t11_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"],
            "universe": ["XAUUSD", "GBPJPY", "USDMXN"],
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T11_fix_pressure_reversal/PREREGISTRATION.md"},
    "T12": {"build": t12_sets, "cell": Cell(1.0, 100, 1.0), "counterfactuals": ["C1", "C2"], "universe": EQUITY_INDICES,
            "card": "docs/mldp/campaign/NASDAQ_2026-11/T12_open_gap_acceptance/PREREGISTRATION.md"},
    "T2": {"build": t2_sets, "cell": Cell(4.0, 100, 24.0), "counterfactuals": ["C1", "C2"], "universe": EQUITY_INDICES,
           "card": "docs/mldp/campaign/NASDAQ_2026-11/T2_month_end_rebalancing/PREREGISTRATION.md"},
}


# --------------------------------------------------------------------------- statistics (D075)

def _wk(s: pd.Series) -> np.ndarray:
    return to_datetime_iso(s, utc=True).dt.tz_convert(None).dt.to_period("W").astype(str).to_numpy()


def _mo(s: pd.Series) -> np.ndarray:
    return to_datetime_iso(s, utc=True).dt.tz_convert(None).dt.to_period("M").astype(str).to_numpy()


def clustered_mean(r: np.ndarray, weeks: np.ndarray) -> tuple[float, float]:
    ok = np.isfinite(r)
    r, weeks = r[ok], weeks[ok]
    m = float(r.mean())
    g = pd.Series(r - m).groupby(weeks).sum().to_numpy()
    return m, float(np.sqrt((g ** 2).sum()) / len(r))


def joint_diff(a: pd.DataFrame, b: pd.DataFrame) -> tuple[float, float]:
    """mean(a) - mean(b), one week-clustered SE over both sets (the approved D075 counterfactual rule)."""
    a, b = a[np.isfinite(a.realized_r)], b[np.isfinite(b.realized_r)]
    ma, mb = a.realized_r.mean(), b.realized_r.mean()
    g = pd.concat([((a.realized_r - ma) / len(a)).groupby(_wk(a.Datetime)).sum(),
                   (-(b.realized_r - mb) / len(b)).groupby(_wk(b.Datetime)).sum()]).groupby(level=0).sum()
    return float(ma - mb), float(np.sqrt((g ** 2).sum()))


def one_sided(d: float, se: float, n: int | None = None) -> dict:
    out = {"diff": round(d, 5), "se": round(se, 5), "lower_bound_1p645": round(d - Z * se, 5), "PASS": bool(d - Z * se > 0)}
    if n is not None:
        out["n"] = int(n)
        if n < MIN_EVENTS:
            out["PASS"] = None
    return out


def random_pool(lab: Labeler, events: pd.DataFrame, side: int, rng) -> pd.DataFrame:
    lo, hi = int(events.pos.min()), int(events.pos.max())
    cand = np.setdiff1d(np.arange(lo, hi + 1), events.pos.to_numpy())
    pos = np.sort(rng.choice(cand, size=min(POOL, len(cand)), replace=False))
    return pd.DataFrame({"pos": pos, "Datetime": to_datetime_iso(lab.ts[pos], utc=True),
                         "realized_r": lab.label(pos, np.full(len(pos), side))})


def matched(edge: pd.DataFrame, pool: pd.DataFrame, key: Callable, rng) -> np.ndarray:
    ek, pk, pr = key(edge.Datetime), key(pool.Datetime), pool.realized_r.to_numpy()
    ok = np.isfinite(pr)
    pk, pr = pk[ok], pr[ok]
    sums = np.zeros(DRAWS)
    cnt = 0
    for k, n in zip(*np.unique(ek, return_counts=True)):
        c = pr[pk == k]
        if len(c):
            sums += c[rng.integers(0, len(c), size=(DRAWS, n))].sum(axis=1)
            cnt += n
    return sums / cnt


def _window(sets: dict, start: str | None, end: str | None) -> dict:
    """Keep events (edge and every counterfactual) whose entry time is in [start, end). Labels come from the full bar
    history first, so the window never changes a label or the volatility warm-up (history extension 2026-11-07)."""
    if not start and not end:
        return sets
    lo = pd.Timestamp(start or "1900-01-01", tz="UTC")
    hi = pd.Timestamp(end or "2100-01-01", tz="UTC")
    out = {}
    for name, s in sets.items():
        out[name] = {}
        for k, f in s.items():
            t = to_datetime_iso(f["Datetime"], utc=True)
            out[name][k] = f[(t >= lo) & (t < hi)].copy()
    return out


def analyse(thesis: str, out: Path, start: str | None = None, end: str | None = None, tag: str = "") -> dict:
    spec = THESES[thesis]
    cell = spec["cell"]
    # D090 post-Gate C re-check / RECERT: GATE0_CELL="lookback_h:ewm:vertical_h[:pt:sl]" re-runs the card at the LOCKED
    # cell; GATE0_ASSETS="XAUUSD[,..]" restricts the assets computed. Default: the card's Gate 0 cell, all assets.
    _cell = os.environ.get("GATE0_CELL", "").strip()
    if _cell:
        _v = [float(x) for x in _cell.split(":")]
        cell = Cell(_v[0], int(_v[1]), _v[2], *(_v[3:5]))
    run_assets = [x for x in os.environ.get("GATE0_ASSETS", "").split(",") if x] or ASSETS
    pool_set = [x for x in run_assets if x in spec["universe"]] or run_assets   # pooled = diagnostic since D090
    t0 = time.time()
    res: dict = {"thesis": thesis, "card": spec["card"], "cell": cell.__dict__, "seed": SEED, "assets": {},
                 "universe": spec["universe"], "post_hoc": bool(spec.get("post_hoc", False)),
                 "window": {"start": start, "end": end}}
    pooled_parts: dict = {"LONG": [], "SHORT": []}
    for asset in run_assets:
        sets, lab = spec["build"](asset, cell)
        sets = _window(sets, start, end)
        ar: dict = {"both_touch_bars": 0}
        for name, side in (("LONG", 1), ("SHORT", -1)):
            s = sets[name]
            edge = s["edge"][np.isfinite(s["edge"].realized_r)]
            if len(edge) == 0:   # no bars at the thesis clock times (e.g. HK at New York times): reported, not pooled
                ar[name] = {"n_edge": 0, "note": "no events (no bars at the thesis clock times)"}
                continue
            rng = np.random.default_rng(SEED + (0 if side == 1 else 1))
            pool = random_pool(lab, s["edge"], side, rng)
            pool = pool[np.isfinite(pool.realized_r)]
            m_e, se_e = clustered_mean(edge.realized_r.to_numpy(), _wk(edge.Datetime))
            dm = matched(edge, pool, _mo, rng)
            dh = matched(edge, pool, lambda x: to_datetime_iso(x, utc=True).dt.hour.to_numpy(), rng)
            sr = {"n_edge": int(len(edge)), "edge_mean_R": round(m_e, 5), "random_all_mean_R": round(float(pool.realized_r.mean()), 5),
                  "drift_month_matched": one_sided(m_e - float(dm.mean()), float(np.sqrt(se_e ** 2 + dm.std(ddof=1) ** 2))),
                  "diag_drift_hour_matched": one_sided(m_e - float(dh.mean()), float(np.sqrt(se_e ** 2 + dh.std(ddof=1) ** 2))),
                  "counterfactuals": {}}
            for c in spec["counterfactuals"]:
                inv = s[c][np.isfinite(s[c].realized_r)]
                d, se = joint_diff(edge, inv)
                sr["counterfactuals"][c] = {**one_sided(d, se, len(inv)), "inverted_mean_R": round(float(inv.realized_r.mean()), 5)}
            if "r" in edge:
                med = edge.r.abs().median()
                d, se = joint_diff(edge[edge.r.abs() >= med], edge[edge.r.abs() < med])
                sr["diag_C3_magnitude_top_vs_bottom"] = one_sided(d, se)
            ar[name] = sr
            # pooled pieces: per-event drift residual vs own asset-month random mean
            pm = pool.assign(m=_mo(pool.Datetime)).groupby("m").realized_r.mean()
            resid = edge.realized_r.to_numpy() - pd.Series(_mo(edge.Datetime)).map(pm).to_numpy()
            if asset in pool_set:
                pooled_parts[name].append({"asset": asset, "resid": pd.DataFrame({"Datetime": edge.Datetime.values, "x": resid}),
                                           "draw_se": float(dm.std(ddof=1)), "n": len(edge), "edge": edge,
                                           "cf": {c: s[c][np.isfinite(s[c].realized_r)] for c in spec["counterfactuals"]}})
        ar["both_touch_bars"] = lab.both_touch
        res["assets"][asset] = ar
        print(asset, json.dumps({k: {kk: (vv["diff"], vv["PASS"]) if isinstance(vv, dict) and "diff" in vv else None
                                     for kk, vv in v.items() if kk in ("drift_month_matched",)} for k, v in ar.items()
                                 if isinstance(v, dict)}), round(time.time() - t0, 1), flush=True)
    res["pooled"] = {}
    for name, parts in pooled_parts.items():
        R = pd.concat([p["resid"] for p in parts])
        R = R[np.isfinite(R.x)]
        n = len(R)
        d = float(R.x.mean())
        g = ((R.x - d) / n).groupby(_wk(to_datetime_iso(R.Datetime, utc=True))).sum()
        dvar = sum((p["n"] / n) ** 2 * p["draw_se"] ** 2 for p in parts)
        pr = {"n_edge": int(n), "drift_month_matched": one_sided(d, float(np.sqrt((g ** 2).sum() + dvar))), "counterfactuals": {}}
        E = pd.concat([p["edge"][["Datetime", "realized_r"]] for p in parts])
        for c in spec["counterfactuals"]:
            I = pd.concat([p["cf"][c][["Datetime", "realized_r"]] for p in parts])
            dd, se = joint_diff(E, I)
            pr["counterfactuals"][c] = one_sided(dd, se, len(I))
        res["pooled"][name] = pr
    # NASDAQ verdict per side (card rule + D075 tiered drift bar, owner 2026-11-04): the drift baseline needs one-sided
    # 95 %, or one-sided 90 % (z 1.28) when EVERY counterfactual passes at one-sided 95 %
    res["verdict_NASDAQ"] = {}
    res["drift_bar"] = {}
    for name in ("LONG", "SHORT"):
        p, a = res["pooled"][name], res["assets"].get("NASDAQ", {}).get(name, {})
        cf_ok = all(v["PASS"] for v in p["counterfactuals"].values())
        dm = p["drift_month_matched"]
        z = Z_DRIFT_TIERED if cf_ok else Z
        dm["lower_bound_tiered"] = round(dm["diff"] - z * dm["se"], 5)
        dm["PASS_tiered"] = bool(dm["diff"] - z * dm["se"] > 0)
        res["drift_bar"][name] = {"z": z, "reason": "every counterfactual passes at 95 %" if cf_ok else "standard 95 %"}
        if not a.get("n_edge"):          # NASDAQ has no events in this window (e.g. before 2014): no NASDAQ verdict
            res["verdict_NASDAQ"][name] = "NO_EVENTS"
            continue
        own_ok = a["drift_month_matched"]["diff"] > 0 and all(v["diff"] > 0 for v in a["counterfactuals"].values())
        res["verdict_NASDAQ"][name] = "PASS" if (cf_ok and dm["PASS_tiered"] and own_ok) else "FAIL"
    # verdict for every universe asset (owner 2026-11-05): same rule as NASDAQ's -- pooled tests pass (tiered drift bar)
    # and the asset's own diffs > 0
    res["verdict_by_asset"] = {}
    for asset in spec["universe"]:
        res["verdict_by_asset"][asset] = {}
        for name in ("LONG", "SHORT"):
            a = res["assets"].get(asset, {}).get(name, {})
            p = res["pooled"][name]
            if not a.get("n_edge"):
                res["verdict_by_asset"][asset][name] = "NO_EVENTS"
                continue
            cf_ok = all(v["PASS"] for v in p["counterfactuals"].values())
            own_ok = a["drift_month_matched"]["diff"] > 0 and all(v["diff"] > 0 for v in a["counterfactuals"].values())
            res["verdict_by_asset"][asset][name] = "PASS" if (cf_ok and p["drift_month_matched"]["PASS_tiered"] and own_ok) else "FAIL"
    # Rev 47 rule (owner 2026-11-05 "approve all 5", prospective): pooled drift baseline AND pooled main counterfactual
    # C1 at one-sided 90 % (z 1.28); every other pooled counterfactual diff > 0; the asset's own drift and counterfactual
    # diffs > 0.
    res["verdict_by_asset_rev47"] = {}
    for asset in spec["universe"]:
        res["verdict_by_asset_rev47"][asset] = {}
        for name in ("LONG", "SHORT"):
            a = res["assets"].get(asset, {}).get(name, {})
            p = res["pooled"][name]
            if not a.get("n_edge"):
                res["verdict_by_asset_rev47"][asset][name] = "NO_EVENTS"
                continue
            dm, cfs = p["drift_month_matched"], p["counterfactuals"]
            ok = dm["diff"] - Z_DRIFT_TIERED * dm["se"] > 0
            for c, v in cfs.items():
                ok = ok and ((v["diff"] - Z_DRIFT_TIERED * v["se"] > 0) if c == "C1" else v["diff"] > 0)
                ok = ok and v.get("PASS") is not None          # too few counterfactual events -> no verdict
            own_ok = a["drift_month_matched"]["diff"] > 0 and all(v["diff"] > 0 for v in a["counterfactuals"].values())
            res["verdict_by_asset_rev47"][asset][name] = "PASS" if (ok and own_ok) else "FAIL"
    # D090 (owner 2026-11-09, IN FORCE): Gate 0 tests the THEORY on each asset's own events. Per asset and side, on
    # this window: the main counterfactual C1 own diff - 1.28 SE > 0 (one-sided 90 %); every other card counterfactual
    # own diff > 0; >= MIN_EVENTS edge events and >= MIN_EVENTS events in every counterfactual. No pooling requirement
    # (other assets need not agree; the pooled result is a diagnostic). The month-matched drift baseline is a reported
    # DIAGNOSTIC, not a gate. Recency (own C1 diff > 0 on 2022+) and the firing rate (>= 2 events/week on 2022+, D092) are
    # added by shared/research/gate0/gate0_verdict_d090.py, which combines this full-history file with the 2022+ run.
    res["window_tests_d090"] = {}
    for asset in run_assets:
        res["window_tests_d090"][asset] = {}
        for name in ("LONG", "SHORT"):
            a = res["assets"].get(asset, {}).get(name, {})
            n = int(a.get("n_edge") or 0)
            cfs = a.get("counterfactuals", {})
            if n < MIN_EVENTS or "C1" not in cfs:
                res["window_tests_d090"][asset][name] = {"verdict": "INCONCLUSIVE", "n_edge": n,
                                                         "reason": "fewer than MIN_EVENTS edge events or no C1"}
                continue
            thin = [c for c, v in cfs.items() if int(v.get("n") or 0) < MIN_EVENTS]
            c1 = cfs["C1"]
            z1 = c1["diff"] / c1["se"] if c1.get("se") else float("nan")
            c1_ok = bool(c1["diff"] - Z_DRIFT_TIERED * c1["se"] > 0)
            others_ok = all(v["diff"] > 0 for c, v in cfs.items() if c != "C1")
            v = "INCONCLUSIVE" if thin else ("PASS" if (c1_ok and others_ok) else "FAIL")
            res["window_tests_d090"][asset][name] = {
                "verdict": v, "n_edge": n, "C1_diff": c1["diff"], "C1_se": c1["se"], "C1_z": round(z1, 3),
                "C1_pass_90": c1_ok, "other_cf_diffs": {c: w["diff"] for c, w in cfs.items() if c != "C1"},
                "other_cf_ok": others_ok, "thin_counterfactuals": thin,
                "diag_drift_month_matched": a.get("drift_month_matched", {}).get("diff"),
                "in_card_universe": asset in spec["universe"]}
    res["seconds"] = round(time.time() - t0, 1)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"gate0_{thesis}{tag}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps({"pooled": res["pooled"], "verdict_NASDAQ": res["verdict_NASDAQ"]}, indent=1), flush=True)
    return res


if __name__ == "__main__":
    # optional: START END TAG (event-date window, e.g. 2014-01-01 2022-01-01 _2014_2021); empty string = open end
    a = sys.argv[1:] + [""] * 5
    analyse(a[0], Path(a[1]), a[2] or None, a[3] or None, a[4])
