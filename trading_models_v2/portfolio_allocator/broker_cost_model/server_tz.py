"""Broker trade-server time-zone rule: detection from the firm's OWN ticks (no Dukascopy overlap needed) + conversion.

Rules (offset = server clock - UTC, hours):
  NY_CLOSE : 2 + (1 if US DST)  -- server midnight = 17:00 New York (industry standard; Moneta, verified 2026-10-16:
             switched to +3 on the week of 2026-03-09 = US DST date, weekly open stays Mon 00:00 server).
  EET      : 2 + (1 if EU DST)  -- EU dates (last Sunday March / last Sunday October 01:00 UTC).
  FIXED_<n>: constant n.
Detection: 24/5 markets open at a fixed New-York time (Sun 17:00 NY). In the US-vs-EU DST gap weeks (US switched, EU not
yet), the weekly first tick's SERVER clock time stays constant under NY_CLOSE, shifts by 1h under EET, and under FIXED it
shifts with US DST at both switches. The rule whose implied UTC weekly-open best matches Sun 17:00 New York wins.
Needs history covering at least one DST transition; otherwise falls back to NY_CLOSE with a warning (pass the measured
live offset to sanity-check)."""
import numpy as np, pandas as pd


def _nth_sunday(y, m, n):
    d = pd.Timestamp(year=y, month=m, day=1, tz="UTC"); d = d + pd.Timedelta(days=(6 - d.dayofweek) % 7)
    return d + pd.Timedelta(weeks=n - 1)


def _last_sunday(y, m):
    d = pd.Timestamp(year=y, month=m, day=31 if m in (3, 10) else 30, tz="UTC")
    return d - pd.Timedelta(days=(d.dayofweek + 1) % 7)


def us_dst(t):   # 2nd Sunday March 07:00 UTC .. 1st Sunday November 06:00 UTC (2am local New York)
    t = pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t).tz_localize("UTC")
    return _nth_sunday(t.year, 3, 2) + pd.Timedelta(hours=7) <= t < _nth_sunday(t.year, 11, 1) + pd.Timedelta(hours=6)


def eu_dst(t):
    t = pd.Timestamp(t).tz_convert("UTC") if pd.Timestamp(t).tzinfo else pd.Timestamp(t).tz_localize("UTC")
    return _last_sunday(t.year, 3) + pd.Timedelta(hours=1) <= t < _last_sunday(t.year, 10) + pd.Timedelta(hours=1)


def offset_hours(rule, t_utc):
    if rule == "NY_CLOSE": return 3 if us_dst(t_utc) else 2
    if rule == "EET": return 3 if eu_dst(t_utc) else 2
    if rule.startswith("FIXED_"): return int(rule.split("_")[1])
    raise ValueError(rule)


def server_to_utc(index_server, rule):
    """index_server: DatetimeIndex of SERVER clock times (tz-naive or labelled UTC). Returns tz-aware UTC index."""
    idx = pd.DatetimeIndex(index_server); idx = idx.tz_localize(None) if idx.tz is not None else idx
    approx = (idx - pd.Timedelta(hours=3)).tz_localize("UTC")
    off = np.array([offset_hours(rule, t) for t in approx])
    return (idx - pd.to_timedelta(off, unit="h")).tz_localize("UTC")


def detect_rule(index_server, measured_offset=None):
    """Return (rule, diagnostics). Uses each week's first tick; the true weekly open is Sunday 17:00 New York."""
    idx = pd.DatetimeIndex(index_server); idx = idx.tz_localize(None) if idx.tz is not None else idx
    first = pd.Series(idx, index=idx).groupby(idx.to_period("W-SAT")).min()   # weeks start Sunday (24/5 open is Sunday evening NY)
    first = first[first.dt.dayofweek.isin([6, 0])]          # weeks whose first tick is Sun/Mon (24/5 market open)
    # Score = how CONSTANT the implied New-York-local weekly-open clock time is across weeks (the correct rule makes it
    # constant whatever the asset's own open hour is). Gap weeks (US DST != EU DST) are what separate NY_CLOSE from EET.
    scores, gap_scores = {}, {}
    gap = np.array([us_dst(pd.Timestamp(t).tz_localize("UTC")) != eu_dst(pd.Timestamp(t).tz_localize("UTC")) for t in first])
    for rule in ("NY_CLOSE", "EET", "FIXED_2", "FIXED_3"):
        hours = []
        for t in first:
            u = (t - pd.Timedelta(hours=offset_hours(rule, (t - pd.Timedelta(hours=3)).tz_localize("UTC")))).tz_localize("UTC")
            ny = u - pd.Timedelta(hours=4 if us_dst(u) else 5)
            hours.append(((ny.dayofweek + 1) % 7) * 24 + ny.hour + ny.minute / 60)   # hours since Sunday 00:00 NY
        h = np.array(hours); mode = float(pd.Series(np.round(h)).mode().iloc[0])
        scores[rule] = round(float(np.mean(np.abs(h - mode) > 0.5)), 3)            # share of weeks off the usual open
        gap_scores[rule] = round(float(np.mean(np.abs(h[gap] - mode) > 0.5)), 3) if gap.any() else None
    spans_dst = len({us_dst(pd.Timestamp(t).tz_localize("UTC")) for t in first}) > 1
    if not spans_dst:
        return "NY_CLOSE", {"scores": scores, "warning": "history does not span a DST transition; NY_CLOSE assumed"}
    order = ["NY_CLOSE", "EET", "FIXED_2", "FIXED_3"]
    best = min(order, key=lambda k: (scores[k], gap_scores[k] if gap_scores[k] is not None else 0, order.index(k)))
    diag = {"share_of_weeks_off_usual_open": scores, "gap_weeks_off": gap_scores, "gap_weeks": int(gap.sum()), "weeks": int(len(first)),
            "warning": None if gap.any() else "no US/EU DST gap weeks in history: NY_CLOSE vs EET not separable (NY_CLOSE kept)"}
    # A constant offset keeps the open consistent whatever n is, so FIXED_n is settled by the LIVE measured offset
    # (latest tick time - true UTC, measured by 00c); for DST rules the live offset is a consistency check.
    if best.startswith("FIXED_"):
        if measured_offset is None:
            diag["warning"] = "FIXED offset detected but n unknown without the live measured offset"
        else:
            best = f"FIXED_{int(measured_offset)}"
    if measured_offset is not None:
        diag["measured_offset_now"] = int(measured_offset)
        diag["rule_offset_now"] = offset_hours(best, pd.Timestamp.now(tz="UTC"))
        if diag["rule_offset_now"] != int(measured_offset):
            diag["warning"] = f"rule {best} implies offset {diag['rule_offset_now']} now but live measured {measured_offset}: CHECK"
    return best, diag
