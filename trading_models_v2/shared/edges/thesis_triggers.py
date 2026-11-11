"""Causal clock / signal triggers for the D090 post-hoc Gate A edges (owner 2026-11-09: "send them to Gate A").

One implementation per Gate 0 card, written to reproduce shared/research/gate0/gate0_tool.py's EDGE event set exactly
(same clock times, same "last bar at or before" signal reads, same "first bar at or after" entry, same 30-minute
staleness bound, same de-duplication). Parity is checked by docs/tools/check_thesis_trigger_parity.py.

Every trigger is decided at a bar close and uses only bars at or before the check time t (signal) plus the first bar
closing at or after t (entry), exactly as the live bot trades a clock event.

entries(thesis, asset, side, ts, close) -> DataFrame[pos, ref_ns, signal]
  pos     entry bar position in ts
  ref_ns  the clock time t (UTC ns)
  signal  the card's conditioning value (log return or z-score; 0.0 for pure clock theses)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_STALE_NS = pd.Timedelta(minutes=30).value
DAY_NS = pd.Timedelta(days=1).value
HOUR_NS = pd.Timedelta(hours=1).value
NY = "America/New_York"

# T18 (overnight drift): REF close + T12 open tables of gate0_tool
T18_CLOSE = {"NASDAQ": (NY, [("2000-01-01", "16:00")]), "US500": (NY, [("2000-01-01", "16:00")]),
             "DAX": ("Europe/Berlin", [("2000-01-01", "17:30")]),
             "JPN": ("Asia/Tokyo", [("2000-01-01", "15:00"), ("2024-11-05", "15:30")]),
             "HK": ("Asia/Hong_Kong", [("2000-01-01", "16:00")])}
T18_OPEN = {"NASDAQ": (NY, [("2000-01-01", "09:30")]), "US500": (NY, [("2000-01-01", "09:30")]),
            "DAX": ("Europe/Berlin", [("2000-01-01", "09:00")]), "JPN": ("Asia/Tokyo", [("2000-01-01", "09:00")]),
            "HK": ("Asia/Hong_Kong", [("2000-01-01", "09:30")])}
T16_BOUNDARIES = [  # name, (tz, hh, mm) of B, (tz, hh, mm) of A, A on previous weekday?
    ("london", ("Europe/London", 8, 0), ("Asia/Tokyo", 9, 0), False),
    ("newyork", (NY, 8, 0), ("Europe/London", 8, 0), False),
    ("tokyo", ("Asia/Tokyo", 9, 0), (NY, 8, 0), True),
]
T21_RISK_ON = {"GBPJPY": 1, "USOIL": 1, "BTCUSD": 1, "USDMXN": -1}
CHECK_HOURS_UTC = (0, 8, 16)          # T17 and T22
T22_LOOKBACK_NS = pd.Timedelta(hours=168).value
T22_Z = 0.5
T22_NORM_DAYS = "365D"
T22_NORM_MIN = 100


class TriggerError(RuntimeError):
    pass


def _local(day: pd.Timestamp, tz: str, hh: int, mm: int) -> int:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm, tz=tz).tz_convert("UTC").value


def _at_or_after(ts: np.ndarray, t: np.ndarray) -> np.ndarray:
    p = np.searchsorted(ts, t, side="left")
    pc = np.clip(p, 0, len(ts) - 1)
    return np.where((p < len(ts)) & (ts[pc] - t <= MAX_STALE_NS), p, -1)


def _at_or_before(ts: np.ndarray, t: np.ndarray) -> np.ndarray:
    p = np.searchsorted(ts, t, side="right") - 1
    pc = np.clip(p, 0, None)
    return np.where((p >= 0) & (t - ts[pc] <= MAX_STALE_NS), p, -1)


def _days(ts: np.ndarray, tz: str) -> pd.DatetimeIndex:
    first = pd.Timestamp(ts[0], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    last = pd.Timestamp(ts[-1], tz="UTC").tz_convert(tz).normalize().tz_localize(None)
    d = pd.date_range(first, last, freq="D")
    return d[d.weekday < 5]


def _clock(ts: np.ndarray, day_tz: str, tz: str, sched: list[tuple[str, str]]) -> pd.DataFrame:
    """First bar at or after a local clock time on each weekday (days counted in day_tz)."""
    days = _days(ts, day_tz)
    ref = np.array([_local(d, tz, *map(int, [s for since, s in sched if d >= pd.Timestamp(since)][-1].split(":")))
                    for d in days], dtype="int64")
    pos = _at_or_after(ts, ref)
    ok = pos >= 0
    f = pd.DataFrame({"pos": pos[ok], "ref_ns": ref[ok], "signal": 0.0})
    return f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)


def _check_grid(ts: np.ndarray, start_days: int) -> np.ndarray:
    day0 = (ts[0] // DAY_NS + start_days) * DAY_NS
    return np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS))
                     for h in CHECK_HOURS_UTC], dtype="int64")


def _lookback_return(ts, close, t, lookback_ns):
    pb, pa, pe = _at_or_before(ts, t), _at_or_before(ts, t - lookback_ns), _at_or_after(ts, t)
    ok = (pa >= 0) & (pb >= 0) & (pe >= 0)
    pa_c, pb_c = np.clip(pa, 0, None), np.clip(pb, 0, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(ok, np.log(close[pb_c] / close[pa_c]), np.nan)
    return pa, pb, pe, ok, q


def t16(ts, close, side):
    first = pd.Timestamp(ts[0], tz="UTC").normalize().tz_localize(None) + pd.Timedelta(days=1)
    last = pd.Timestamp(ts[-1], tz="UTC").normalize().tz_localize(None)
    days = [d for d in pd.date_range(first, last, freq="D") if d.weekday() < 5]
    rows = []
    for prev, day in zip(days[:-1], days[1:]):
        for _name, (btz, bh, bm), (atz, ah, am), a_prev in T16_BOUNDARIES:
            B, A = _local(day, btz, bh, bm), _local(prev if a_prev else day, atz, ah, am)
            if not A < B:
                continue
            pa, pb = _at_or_before(ts, np.array([A]))[0], _at_or_before(ts, np.array([B]))[0]
            pe = _at_or_after(ts, np.array([B]))[0]
            if min(pa, pb, pe) < 0 or not pa < pb:
                continue
            s = float(np.log(close[pb] / close[pa]))
            if s == 0 or not np.isfinite(s) or np.sign(s) != side:
                continue
            rows.append((int(pe), B, s))
    f = pd.DataFrame(rows, columns=["pos", "ref_ns", "signal"])
    return f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)


def t17(ts, close, side):
    t = _check_grid(ts, 2)
    pa, pb, pe, ok, q = _lookback_return(ts, close, t, DAY_NS)
    ok &= np.isfinite(q) & (q != 0) & (pa < pb) & (np.sign(q) == side)
    f = pd.DataFrame({"pos": pe[ok], "ref_ns": t[ok], "signal": q[ok]})
    return f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)


def t22(ts, close, side):
    t = _check_grid(ts, 8)
    _pa, _pb, pe, ok, q = _lookback_return(ts, close, t, T22_LOOKBACK_NS)
    ok &= np.isfinite(q)
    f = pd.DataFrame({"pos": pe[ok], "ref_ns": t[ok], "q": q[ok],
                      "Datetime": pd.to_datetime(ts[pe[ok]], utc=True)})
    sd = f.set_index("Datetime").q.rolling(T22_NORM_DAYS, closed="left", min_periods=T22_NORM_MIN).std().to_numpy()
    f["signal"] = f.q / sd
    f = f[np.isfinite(f.signal)].drop_duplicates("pos").sort_values("pos")
    f = f[side * f.signal >= T22_Z]
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


T24_HOURS = (0, 4, 8, 12, 16, 20)
T24_WIN, T24_WIN_MIN = 120, 60
T24_NORM_DAYS, T24_NORM_MIN = "365D", 100


def t24(ts, close, side):
    """T24 volatility-managed premium: LONG in the low-vol state, SHORT in the high-vol state (gate0_tool.t24_events)."""
    one_h = HOUR_NS
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hr = np.log(hc).diff()
    hr = hr[hr.index.to_series().diff() <= pd.Timedelta(hours=1)]
    sig = hr.rolling(T24_WIN, min_periods=T24_WIN_MIN).std()
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * one_h + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in T24_HOURS],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0)
    t, pe = t[ok], pe[ok]
    si = sig.index.asi8 if hasattr(sig.index, "asi8") else sig.index.view("int64")
    ps = np.searchsorted(si, t, side="right") - 1
    v = np.where(ps >= 0, sig.to_numpy()[np.clip(ps, 0, None)], np.nan)
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "sigma": v, "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.sigma)].copy()
    f["med"] = f.set_index("t").sigma.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos")
    f = f[(f.sigma < f.med)] if side == 1 else f[(f.sigma >= f.med)]
    f["signal"] = f.sigma / f.med - 1.0
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


def t25(ts, close, side):
    """T25 52-week-high anchor: LONG when nearer than usual to the 52-week high, SHORT when farther (gate0_tool.t25_events)."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hi = hc.rolling("365D").max()
    warm = hc.index[0] + pd.Timedelta(days=365)
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in T24_HOURS],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0) & (t >= warm.value)
    t, pb, pe = t[ok], pb[ok], pe[ok]
    hix = hi.index.asi8 if hasattr(hi.index, "asi8") else hi.index.view("int64")
    ph = np.searchsorted(hix, t, side="right") - 1
    h = np.where(ph >= 0, hi.to_numpy()[np.clip(ph, 0, None)], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        x = np.log(close[pb] / np.maximum(h, close[pb]))
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "x": x, "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.x)].copy()
    f["med"] = f.set_index("t").x.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos")
    f = f[f.x >= f.med] if side == 1 else f[f.x < f.med]
    f["signal"] = f.x - f.med
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


T27_WIN, T27_WIN_MIN = 168, 100


def t27(ts, close, side):
    """T27 frog in the pan: LONG after a continuous up-week, SHORT after a continuous down-week (gate0_tool.t27_events).
    ID = sign(PRET) * (share of negative - share of positive hourly returns) over the last 168 hourly returns; low ID =
    continuous. Split against the median ID of the same week-sign's checks over the prior 365 days."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hr = np.log(hc).diff()
    hr = hr[hr.index.to_series().diff() <= pd.Timedelta(hours=1)]
    pret = hr.rolling(T27_WIN, min_periods=T27_WIN_MIN).sum()
    n = hr.rolling(T27_WIN, min_periods=T27_WIN_MIN).count()
    neg = (hr < 0).astype(float).rolling(T27_WIN, min_periods=T27_WIN_MIN).sum() / n
    pos = (hr > 0).astype(float).rolling(T27_WIN, min_periods=T27_WIN_MIN).sum() / n
    idv = np.sign(pret) * (neg - pos)
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in T24_HOURS],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0)
    t, pe = t[ok], pe[ok]
    ix = idv.index.asi8 if hasattr(idv.index, "asi8") else idv.index.view("int64")
    ps = np.searchsorted(ix, t, side="right") - 1
    good = ps >= 0
    psc = np.clip(ps, 0, None)
    v = np.where(good, idv.to_numpy()[psc], np.nan)
    pr = np.where(good, pret.to_numpy()[psc], np.nan)
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "idv": v, "pret": pr, "t": pd.to_datetime(t, utc=True)})
    f = f[np.isfinite(f.idv) & np.isfinite(f.pret) & (f.pret != 0)].copy()
    f = f[f.pret > 0] if side == 1 else f[f.pret < 0]
    f["med"] = f.set_index("t").idv.rolling(T24_NORM_DAYS, closed="left", min_periods=T24_NORM_MIN).median().to_numpy()
    f = f[np.isfinite(f.med)].drop_duplicates("pos").sort_values("pos")
    f = f[f.idv < f.med]
    f["signal"] = f.med - f.idv
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


T28_WINTER = (11, 12, 1, 2, 3, 4)


def t28(ts, close, side):
    """T28 Halloween season: LONG at every 4-hourly UTC check in November-April, SHORT in May-October
    (gate0_tool.t28_events)."""
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in T24_HOURS],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0)
    t, pe = t[ok], pe[ok]
    win = np.isin(pd.to_datetime(t, utc=True).month, T28_WINTER)
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "win": win})
    f = f.drop_duplicates("pos").sort_values("pos")
    f = f[f.win] if side == 1 else f[~f.win]
    f["signal"] = 1.0
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


def t29(ts, close, side):
    """T29 12-month time-series momentum: LONG when log(close / last hourly close at or before t - 365 d) > 0, SHORT
    when < 0; every 4-hourly UTC check after a 365-day warm-up (gate0_tool.t29_events)."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hix = hc.index.asi8 if hasattr(hc.index, "asi8") else hc.index.view("int64")
    warm = hix[0] + 365 * DAY_NS
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in T24_HOURS],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0) & (t >= warm)
    t, pb, pe = t[ok], pb[ok], pe[ok]
    p0 = np.searchsorted(hix, t - 365 * DAY_NS, side="right") - 1
    c0 = np.where(p0 >= 0, hc.to_numpy()[np.clip(p0, 0, None)], np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        r12 = np.log(close[pb] / c0)
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "r12": r12})
    f = f[np.isfinite(f.r12) & (f.r12 != 0)].drop_duplicates("pos").sort_values("pos")
    f = f[f.r12 > 0] if side == 1 else f[f.r12 < 0]
    f["signal"] = f.r12
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


T33_OPEN = {"NASDAQ": (NY, 9, 30), "US500": (NY, 9, 30), "DAX": ("Europe/Berlin", 9, 0),
            "JPN": ("Asia/Tokyo", 9, 0), "HK": ("Asia/Hong_Kong", 9, 30)}
T33_FIRST_NS = pd.Timedelta(minutes=30).value
T33_LAST_NS = pd.Timedelta(minutes=30).value


def t33(ts, close, asset, side):
    """T33 market intraday momentum (gate0_tool.t33_days): r1 = log(close at open + 30 min / close at the previous
    weekday's session close); trade sign(r1) == side at the first bar at or after the session close - 30 min."""
    tz, oh, om = T33_OPEN[asset]
    ctz, sched = T1C_REF[asset]
    days = _days(ts, tz)
    op = np.array([_local(d, tz, oh, om) for d in days], dtype="int64")
    ref = np.array([_local(d, ctz, *map(int, [s for since, s in sched if d >= pd.Timestamp(since)][-1].split(":")))
                    for d in days], dtype="int64")
    ps = _at_or_before(ts, op[1:] + T33_FIRST_NS)
    pp = _at_or_before(ts, ref[:-1])
    pe = _at_or_after(ts, ref[1:] - T33_LAST_NS)
    ok = (ps >= 0) & (pp >= 0) & (pe >= 0) & (pp < ps) & (ps < pe)
    ps_c, pp_c = np.clip(ps, 0, None), np.clip(pp, 0, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(ok, np.log(close[ps_c] / close[pp_c]), np.nan)
    keep = ok & np.isfinite(r) & (r != 0) & (np.sign(r) == side)
    f = pd.DataFrame({"pos": pe[keep], "ref_ns": (ref[1:] - T33_LAST_NS)[keep], "signal": r[keep]})
    return f.sort_values("pos").reset_index(drop=True)


T32_DAYS, T32_Z = "5D", 1.5


def t32(ts, close, side):
    """T32 overextension reversal (gate0_tool.t32_events): hourly close z vs the prior 5 days' mean / std; SHORT at the
    first cross up through +1.5, LONG at the first cross down through -1.5; entry = first bar at or after the hour end."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    z = (hc - hc.rolling(T32_DAYS, closed="left").mean()) / hc.rolling(T32_DAYS, closed="left").std()
    z = z[(hc.index >= hc.index[0] + pd.Timedelta(T32_DAYS)) & np.isfinite(z.to_numpy())]
    prev = z.shift(1)
    mask = ((z <= -T32_Z) & (prev > -T32_Z)) if side == 1 else ((z >= T32_Z) & (prev < T32_Z))
    sel = z.index[mask.to_numpy()]
    t = sel.asi8 if hasattr(sel, "asi8") else sel.view("int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0)
    f = pd.DataFrame({"pos": pe[ok], "ref_ns": t[ok], "signal": z.to_numpy()[mask.to_numpy()][ok]})
    return f.drop_duplicates("pos").sort_values("pos").reset_index(drop=True)


T30_DAYS = "10D"


def t30(ts, close, side):
    """T30 10-day trading-range breakout (gate0_tool.t30_events): hourly UTC checks; LONG when the close is a new
    10-day closing high (above the max hourly close of the 10 days before the last full hour), SHORT on a new low."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hi = hc.rolling(T30_DAYS, closed="left").max().to_numpy()
    lo = hc.rolling(T30_DAYS, closed="left").min().to_numpy()
    warm = (hc.index[0] + pd.Timedelta(T30_DAYS)).value
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in range(24)],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0) & (t >= warm)
    t, pb, pe = t[ok], pb[ok], pe[ok]
    hix = hc.index.asi8 if hasattr(hc.index, "asi8") else hc.index.view("int64")
    ph = np.searchsorted(hix, t, side="right") - 1
    phc = np.clip(ph, 0, None)
    h = np.where(ph >= 0, hi[phc], np.nan)
    l = np.where(ph >= 0, lo[phc], np.nan)
    c = close[pb]
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "c": c, "h": h, "l": l})
    f = f[np.isfinite(f.h) & np.isfinite(f.l) & (f.h > f.l)].drop_duplicates("pos").sort_values("pos")
    f = f[f.c > f.h] if side == 1 else f[f.c < f.l]
    f["signal"] = np.log(f.c / (f.h if side == 1 else f.l))
    return f[["pos", "ref_ns", "signal"]].reset_index(drop=True)


# T1c (session-close hedging flow, causal entry): gate0_tool REF table, entry 1 h before the session close.
T1C_REF = {"NASDAQ": (NY, [("2000-01-01", "16:00")]), "US500": (NY, [("2000-01-01", "16:00")]),
           "DAX": ("Europe/Berlin", [("2000-01-01", "17:30")]),
           "JPN": ("Asia/Tokyo", [("2000-01-01", "15:00"), ("2024-11-05", "15:30")]),
           "HK": ("Asia/Hong_Kong", [("2000-01-01", "16:00")]), "XAUUSD": ("Europe/London", [("2000-01-01", "15:00")]),
           "GBPJPY": ("Europe/London", [("2000-01-01", "16:00")]), "USDMXN": ("Europe/London", [("2000-01-01", "16:00")]),
           "USOIL": (NY, [("2000-01-01", "14:30")]), "BTCUSD": ("Europe/London", [("2000-01-01", "16:00")])}
T1C_ENTRY_OFFSET_NS = pd.Timedelta(hours=1).value


def t1c(ts, close, asset, side):
    """Weekday d: r = log(close at the first bar at or after ref(d) - 1 h / close at the last bar at or before ref(prev
    weekday)); the edge trades sign(r) == side at that entry bar (the day's move continues into the close)."""
    tz, sched = T1C_REF[asset]
    days = _days(ts, tz)
    ref = np.array([_local(d, tz, *map(int, [s for since, s in sched if d >= pd.Timestamp(since)][-1].split(":")))
                    for d in days], dtype="int64")
    pe = _at_or_after(ts, ref[1:] - T1C_ENTRY_OFFSET_NS)
    pp = _at_or_before(ts, ref[:-1])
    ok = (pe >= 0) & (pp >= 0) & (pe > pp)
    pe_c, pp_c = np.clip(pe, 0, None), np.clip(pp, 0, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(ok, np.log(close[pe_c] / close[pp_c]), np.nan)
    keep = ok & np.isfinite(r) & (r != 0) & (np.sign(r) == side)
    f = pd.DataFrame({"pos": pe[keep], "ref_ns": ref[1:][keep] - T1C_ENTRY_OFFSET_NS, "signal": r[keep]})  # entry clock time
    return f.sort_values("pos").reset_index(drop=True)


# T13 / T14 / T31 SHORT edges (D094 re-read, owner 2026-11-11). Gate 0 cells are the cards' own (T13, T14: 1 h
# Event_Vol, EWM 100, 1 h vertical; T31: 4 h, EWM 150, 24 h); where a trigger depends on Event_Vol it uses that FIXED
# card cell, never the edge's (Gate C-locked) labelling geometry.
T13_SPACING = {"NASDAQ": 100.0, "US500": 50.0, "DAX": 100.0, "JPN": 500.0, "HK": 500.0, "XAUUSD": 10.0,
               "GBPJPY": 1.0, "USDMXN": 0.10, "USOIL": 1.0, "BTCUSD": 1000.0}
T13_MAX_GAP_NS = pd.Timedelta(hours=2).value
CARD_VOL_H, CARD_VOL_SPAN, MIN_VOL = 1.0, 100, 1e-8      # T13 / T14 Gate 0 cell Event_Vol


def _card_vol(ts, close, hours=CARD_VOL_H, span=CARD_VOL_SPAN):
    from shared.labels.calendar_time_barriers import calendar_time_event_vol
    return calendar_time_event_vol(ts, close, hours=hours, ewm_span=span, min_vol=MIN_VOL)


def t13(ts, close, asset, side, high, low):
    """T13 round-level stop cascade (gate0_tool.t13_events): first close beyond the nearest round level (card spacing)
    above the prior 24 h high (LONG) / below the prior 24 h low (SHORT); bar within 2 h of its predecessor."""
    s = T13_SPACING[asset]
    idx = pd.to_datetime(ts, utc=True)
    h24 = pd.Series(high, index=idx).rolling("24h", closed="left").max().to_numpy()
    l24 = pd.Series(low, index=idx).rolling("24h", closed="left").min().to_numpy()
    cont = np.r_[False, np.diff(ts) <= T13_MAX_GAP_NS]
    vol = _card_vol(ts, close)
    ok = cont & np.isfinite(h24) & np.isfinite(l24) & np.isfinite(vol) & (vol > 0)
    with np.errstate(invalid="ignore"):
        if side == 1:
            lvl = (np.floor(h24 / s) + 1.0) * s
            pos = np.flatnonzero(ok & (close >= lvl))
        else:
            lvl = (np.ceil(l24 / s) - 1.0) * s
            pos = np.flatnonzero(ok & (close <= lvl))
    return pd.DataFrame({"pos": pos, "ref_ns": ts[pos], "signal": np.log(close[pos] / lvl[pos])})


T14_Z, T14_REGIME_SPAN, T14_REGIME_DAYS, T14_REGIME_MIN_DAYS = 2.0, 1000, 250, 120
T14_GAP_NS = HOUR_NS                                         # card vertical 1 h: one event per hour at most


def t14(ts, close, side):
    """T14 high-vol liquidity-provision reversal (gate0_tool.t14_frame / t14_sets): fresh |1 h return / prior Event_Vol|
    >= 2 inside a high-volatility regime (24 h vol above its prior 250-day median); SHORT fades a rise, LONG a drop."""
    vol = _card_vol(ts, close)
    ref = np.searchsorted(ts, ts - HOUR_NS, side="right") - 1
    ref_c = np.clip(ref, 0, None)
    fresh = (ref >= 0) & ((ts - HOUR_NS) - ts[ref_c] <= MAX_STALE_NS)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(fresh, np.log(close / close[ref_c]), np.nan)
        z = r / np.r_[np.nan, vol[:-1]]
        z_prev = np.r_[np.nan, z[:-1]]
        big = (np.abs(z) >= T14_Z) & (np.abs(z_prev) < T14_Z)
    reg = _card_vol(ts, close, hours=24.0, span=T14_REGIME_SPAN)
    dt = pd.to_datetime(ts, utc=True)
    daily = pd.Series(reg, index=dt).groupby(dt.normalize()).last().dropna()
    med = daily.rolling(f"{T14_REGIME_DAYS}D", min_periods=T14_REGIME_MIN_DAYS).median().shift(1, freq="D")
    med_at = med.reindex(dt.normalize(), method="ffill").to_numpy()
    known = np.isfinite(reg) & np.isfinite(med_at) & np.isfinite(z)
    with np.errstate(invalid="ignore"):
        cand = np.flatnonzero(big & (reg > med_at) & known & (np.sign(z) == -side))
    keep, last = [], None
    for p in cand:
        if last is None or ts[p] - ts[last] >= T14_GAP_NS:
            keep.append(p)
            last = p
    keep = np.asarray(keep, dtype=int)
    return pd.DataFrame({"pos": keep, "ref_ns": ts[keep], "signal": z[keep]})


T31_DAYS, T31_WIN_NS = "5D", pd.Timedelta(hours=24).value


def _range_checks(ts, close, days):
    """gate0_tool.t30_events: hourly UTC checks, prior-window hourly-close max / min (closed='left'), deduped."""
    s = pd.Series(close, index=pd.to_datetime(ts, utc=True))
    hc = s.resample("1h", label="right", closed="right").last().dropna()
    hi = hc.rolling(days, closed="left").max().to_numpy()
    lo = hc.rolling(days, closed="left").min().to_numpy()
    warm = (hc.index[0] + pd.Timedelta(days)).value
    day0 = (ts[0] // DAY_NS + 1) * DAY_NS
    t = np.array([day0 + h * HOUR_NS + k * DAY_NS for k in range(int((ts[-1] - day0) // DAY_NS)) for h in range(24)],
                 dtype="int64")
    pb, pe = _at_or_before(ts, t), _at_or_after(ts, t)
    ok = (pb >= 0) & (pe >= 0) & (t >= warm)
    t, pb, pe = t[ok], pb[ok], pe[ok]
    hix = hc.index.asi8 if hasattr(hc.index, "asi8") else hc.index.view("int64")
    ph = np.searchsorted(hix, t, side="right") - 1
    phc = np.clip(ph, 0, None)
    f = pd.DataFrame({"pos": pe, "ref_ns": t, "c": close[pb], "h": np.where(ph >= 0, hi[phc], np.nan),
                      "l": np.where(ph >= 0, lo[phc], np.nan)})
    f = f[np.isfinite(f.h) & np.isfinite(f.l) & (f.h > f.l)].drop_duplicates("pos").sort_values("pos")
    return f.reset_index(drop=True)


def t31(ts, close, side):
    """T31 failed 5-day range breakout (gate0_tool.t31_events): SHORT = first hourly check within 24 h after an
    up-break (close > prior 5-day high) at which the close is back below the broken level; then disarmed. LONG mirrors."""
    f = _range_checks(ts, close, T31_DAYS)
    c, H, L, t = f.c.to_numpy(), f.h.to_numpy(), f.l.to_numpy(), f.ref_ns.to_numpy()
    brk = (c > H) if side == -1 else (c < L)
    fail, last_t, level, armed = [], None, None, False
    for i in range(len(c)):
        if brk[i]:
            last_t, level, armed = t[i], (H[i] if side == -1 else L[i]), True
            continue
        if armed and t[i] - last_t <= T31_WIN_NS:
            if (side == -1 and c[i] < level) or (side == 1 and c[i] > level):
                fail.append(i)
                armed = False
        elif armed:
            armed = False
    g = f.iloc[fail]
    return pd.DataFrame({"pos": g.pos.to_numpy(), "ref_ns": g.ref_ns.to_numpy(),
                         "signal": np.log(g.c.to_numpy() / (g.h.to_numpy() if side == -1 else g.l.to_numpy()))})


def entries(thesis: str, asset: str, side: int, ts: np.ndarray, close: np.ndarray,
            high: np.ndarray | None = None, low: np.ndarray | None = None) -> pd.DataFrame:
    ts = np.asarray(ts, dtype="int64")
    close = np.asarray(close, dtype="float64")
    if thesis == "T13":
        if high is None or low is None:
            raise TriggerError("T13 needs high and low")
        return t13(ts, close, asset, side, np.asarray(high, dtype="float64"), np.asarray(low, dtype="float64"))
    if thesis == "T14":
        return t14(ts, close, side)
    if thesis == "T31":
        return t31(ts, close, side)
    if thesis == "T18":
        tz, sched = (T18_CLOSE if side == 1 else T18_OPEN)[asset]
        return _clock(ts, tz, tz, sched)
    if thesis == "T20":     # XAUUSD: LONG at the Asia open (09:00 Shanghai), SHORT at 08:20 New York; UTC weekdays
        return _clock(ts, "UTC", "Asia/Shanghai", [("2000-01-01", "09:00")]) if side == 1 else \
            _clock(ts, "UTC", NY, [("2000-01-01", "08:20")])
    if thesis == "T21":     # risk-on side enters at the NY close (16:00), the other side at the NY open (09:30)
        d = T21_RISK_ON.get(asset, 1)
        return _clock(ts, NY, NY, [("2000-01-01", "16:00" if side == d else "09:30")])
    if thesis == "T16":
        return t16(ts, close, side)
    if thesis == "T17":
        return t17(ts, close, side)
    if thesis == "T22":
        return t22(ts, close, side)
    if thesis == "T24":
        return t24(ts, close, side)
    if thesis == "T25":
        return t25(ts, close, side)
    if thesis == "T27":
        return t27(ts, close, side)
    if thesis == "T28":
        return t28(ts, close, side)
    if thesis == "T29":
        return t29(ts, close, side)
    if thesis == "T33":
        return t33(ts, close, asset, side)
    if thesis == "T32":
        return t32(ts, close, side)
    if thesis == "T30":
        return t30(ts, close, side)
    if thesis == "T1c":
        return t1c(ts, close, asset, side)
    raise TriggerError(f"unknown thesis {thesis}")
