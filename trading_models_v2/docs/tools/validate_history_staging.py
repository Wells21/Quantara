"""Validate the history-extension staging ticks (assets/<ASSET>/raw_history_staging/ticks) before any move to raw/ticks.

Owner 2026-11-05/06: downloads go to staging first; the owner decides about raw/ticks after this check. Read-only.

Per file: header equal to raw/ticks, timestamps parse / non-decreasing / inside the half-open chunk, no NaN, prices > 0,
crossed quotes (ask < bid) counted, duplicate timestamps counted.
Per asset: chunk chain contiguous from the first staged chunk to the first raw chunk (2022-01-01); weekly tick counts
versus neighbours; weekdays with zero ticks; partial days; gaps > 1 h >= 3 h whose start (New York weekday, hour) occurs < 3 times in the same year (session breaks recur, holes
do not); the seam (last staged
tick versus first raw tick); quarantined files versus their re-downloads.

usage: python docs/tools/validate_history_staging.py --asset NASDAQ [--out logs/history_staging_validation_20261107]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
NY = "America/New_York"
HEADER = "timestamp,bidPrice,askPrice,bidVolume,askVolume"
GAP_NS = 3600 * 10**9


def below_normal() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)


def span(name: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = name.split("_ticks_")[1].removesuffix(".csv")
    a, b = s.split("_to_")
    return pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")


def read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # pandas 3 infers microsecond resolution here; force ns so the int64 gap arithmetic is in nanoseconds
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").dt.as_unit("ns")
    return df


def market_holidays(y0: int, y1: int) -> set:
    """US federal holidays + Good Friday + 24 Dec .. 2 Jan (NY dates); a gap touching one of these is explained."""
    from dateutil.easter import easter
    from pandas.tseries.holiday import USFederalHolidayCalendar
    out = {d.date() for d in USFederalHolidayCalendar().holidays(f"{y0}-01-01", f"{y1}-12-31")}
    for y in range(y0, y1 + 1):
        out.add(easter(y) - pd.Timedelta(days=2).to_pytimedelta())
        out.update(pd.Timestamp(f"{y}-12-{d}").date() for d in range(24, 32))
        out.update(pd.Timestamp(f"{y}-01-0{d}").date() for d in (1, 2))
    return out


def gap_keys(ts: pd.Series) -> list[tuple]:
    """(NY weekday, NY hour, gap hours rounded) for every gap > 1 h."""
    v = ts.astype("int64").to_numpy()
    if len(v) < 2:
        return []
    d = np.diff(v)
    idx = np.where(d > GAP_NS)[0]
    out = []
    for i in idx:
        t = pd.Timestamp(v[i], tz="UTC").tz_convert(NY)
        out.append((t.weekday(), t.hour, int(round(d[i] / 3.6e12)), t))
    return out


def validate(asset: str) -> dict:
    stage = ROOT / "assets" / asset / "raw_history_staging"
    files = sorted((stage / "ticks").glob("*.csv"))
    raw_files = sorted((ROOT / "assets" / asset / "raw" / "ticks").glob("*_ticks_*_to_*.csv"))
    res: dict = {"asset": asset, "n_files": len(files), "problems": []}

    # ---- chain
    spans = [span(p.name) for p in files]
    breaks = [f"{spans[i][1].date()} != {spans[i + 1][0].date()}" for i in range(len(spans) - 1)
              if spans[i][1] != spans[i + 1][0]]
    first_raw = span(raw_files[0].name)[0]
    res["chain"] = {"first": str(spans[0][0].date()), "last_end": str(spans[-1][1].date()),
                    "ends_at_first_raw": spans[-1][1] == first_raw, "breaks": breaks}
    if breaks or spans[-1][1] != first_raw:
        res["problems"].append("chunk chain broken or does not end at the first raw chunk")

    weekly, daily = [], Counter()
    all_gaps, n_gaps = [], 0
    bad_header, outside, nonmono, nans, nonpos, crossed, dups, total = [], 0, 0, 0, 0, 0, 0, 0
    last_tick = None
    for p, (a, b) in zip(files, spans):
        with p.open("r", encoding="utf-8") as fh:
            if fh.readline().strip() != HEADER:
                bad_header.append(p.name)
        df = read(p)
        n = len(df)
        total += n
        weekly.append({"file": p.name, "start": str(a.date()), "ticks": n})
        if n == 0:
            continue
        ts = df["timestamp"]
        outside += int(((ts < a) | (ts >= b)).sum())
        nonmono += int((ts.diff().dt.total_seconds() < 0).sum())
        dups += int(ts.duplicated().sum())
        nans += int(df[["bidPrice", "askPrice"]].isna().any(axis=1).sum())
        nonpos += int(((df.bidPrice <= 0) | (df.askPrice <= 0)).sum())
        crossed += int((df.askPrice < df.bidPrice).sum())
        daily.update(ts.dt.tz_convert(NY).dt.date.value_counts().to_dict())
        for w, h, hrs, t in gap_keys(ts):
            n_gaps += 1
            all_gaps.append((a.year, p.name, w, h, hrs, t))
        last_tick = df.iloc[-1]
    res["ticks_total"] = total
    res["format"] = {"bad_header": bad_header, "outside_chunk": outside, "non_monotonic": nonmono,
                     "duplicate_timestamps": dups, "nan_prices": nans, "nonpositive_prices": nonpos,
                     "crossed_quotes": crossed}
    if bad_header or outside or nonmono or nans or nonpos:
        res["problems"].append("format / timestamp problems")

    # ---- weekly counts versus neighbours (+-8 weeks median)
    wc = pd.Series([w["ticks"] for w in weekly], index=[w["start"] for w in weekly])
    med = wc.rolling(17, center=True, min_periods=5).median()
    low = wc[(wc < 0.25 * med)]
    res["weeks_zero"] = list(wc[wc == 0].index)
    res["weeks_below_25pct_of_neighbours"] = {k: int(v) for k, v in low.items()}

    # ---- weekdays (New York date) with zero / partial ticks
    first_day, last_day = spans[0][0].tz_convert(NY).date(), (spans[-1][1] - pd.Timedelta(days=1)).tz_convert(NY).date()
    days = [d.date() for d in pd.date_range(first_day, last_day, freq="D") if d.weekday() < 5]
    dc = pd.Series({d: daily.get(d, 0) for d in days})
    dmed = dc.rolling(41, center=True, min_periods=10).median()
    res["weekdays_zero"] = [str(d) for d in dc[dc == 0].index]
    res["weekdays_below_20pct"] = [str(d) for d in dc[(dc > 0) & (dc < 0.2 * dmed)].index]

    # ---- gaps: a gap >= 3 h whose (NY weekday, NY hour) start occurs < 3 times in its own year is unusual (session
    # hours changed over the years, so the schedule is learned per year, not from 2022)
    per_year = Counter((y, w, h) for y, _, w, h, _, _ in all_gaps)
    hol = market_holidays(spans[0][0].year - 1, spans[-1][1].year)

    def explained(t: pd.Timestamp, hrs: int) -> bool:
        end = t + pd.Timedelta(hours=hrs)
        days = {d.date() for d in pd.date_range(t.normalize().tz_localize(None) - pd.Timedelta(days=1),
                                                 end.normalize().tz_localize(None) + pd.Timedelta(days=1), freq="D")}
        return bool(days & hol)

    odd = [(f, w, h, hrs, t) for y, f, w, h, hrs, t in all_gaps if hrs >= 3 and per_year[(y, w, h)] < 3]
    res["gaps_unusual_holiday_explained"] = sum(explained(t, hrs) for *_, hrs, t in odd)
    odd_gaps = [(f"{t:%Y-%m-%d %H:%M} NY", w, h, hrs) for f, w, h, hrs, t in odd if not explained(t, hrs)]
    res["gaps_over_1h"] = n_gaps
    res["gaps_unusual_start"] = len(odd_gaps)
    res["gaps_unusual_list"] = [{"start": f, "ny_weekday": w, "ny_hour": h, "hours": hrs} for f, w, h, hrs in odd_gaps]

    # ---- seam
    r0 = read(raw_files[0]).iloc[0]
    if last_tick is not None:
        res["seam"] = {"last_staged": str(last_tick.timestamp), "first_raw": str(r0.timestamp),
                       "bid_ratio": float(r0.bidPrice / last_tick.bidPrice)}

    # ---- quarantined versus re-downloaded
    q = []
    for p in sorted((stage / "quarantine").glob("*.csv")):
        new = stage / "ticks" / p.name
        old_n = sum(1 for _ in p.open("r", encoding="utf-8")) - 1
        new_n = sum(1 for _ in new.open("r", encoding="utf-8")) - 1 if new.exists() else None
        q.append({"file": p.name, "old_ticks": old_n, "new_ticks": new_n})
    res["quarantine"] = q
    if any(x["new_ticks"] is None for x in q):
        res["problems"].append("a quarantined week was not re-downloaded")
    res["bytes"] = int(sum(p.stat().st_size for p in files))
    res["checked_utc"] = datetime.now(timezone.utc).isoformat()
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--out", default="logs/history_staging_validation_20261107")
    a = ap.parse_args()
    below_normal()
    res = validate(a.asset)
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.asset}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(a.asset, "problems:", res["problems"], "| zero weekdays", len(res["weekdays_zero"]),
          "| partial", len(res["weekdays_below_20pct"]), "| unexplained gaps", res["gaps_unusual_start"], "(holiday", res["gaps_unusual_holiday_explained"], ")", flush=True)


if __name__ == "__main__":
    sys.exit(main())
