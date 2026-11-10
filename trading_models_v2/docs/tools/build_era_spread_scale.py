"""Era-scaled broker spread for full-history Gate C (STANDING RULE, owner 2026-11-09 "yes i approve the era-scaled cost").

The broker cost model (portfolio_allocator/config/broker_profiles/moneta_cost_models/<ASSET>.json) is fitted on the
broker's own ticks over a recent window (cm["span"]). Older years had wider market spreads (Dukascopy gold median about
10.8 bps in 2005, 5.4 in 2008, about 2 from 2011). Gate C on the full history multiplies the broker half-spread by a
monthly factor:

    factor[month] = max(1, min(duka_ratio[month], points_ratio[month]))
    duka_ratio[month]   = median over the month's minutes of  duka_spread(minute) / ref[state(minute)]
    points_ratio[month] = cm["ref_price"] / median Dukascopy mid of the month

AMENDED 2026-11-09 (owner "i approve your recommendation"): the points-ratio cap. Broker CFD spreads are quoted in
points, so an older month at a lower price costs at most ref_price/price times today's bps. Each proxy fails in a
known way: Dukascopy's own ratio jumps when Dukascopy changes its pricing policy (NASDAQ 2025->2026 cliff, BTCUSD
2018-20), and the points ratio ignores liquidity improving over time. The smaller of the two removes the Dukascopy
cliffs and still charges older eras more.

- duka_spread(minute) = the last Dukascopy quote of that UTC minute: (ask - bid) / mid, in bps;
- state(minute) = the broker model's trailing-1-day realized-vol state (same per-minute vol cache and thresholds the
  broker loader uses). FIXED_POINTS models have one state;
- ref[state] = the median Dukascopy minute spread in that state over the broker model's fit window.

Conditioning on the vol state stops crisis widening from being counted twice: the broker TABLE already widens with the
vol state. The floor of 1 means no era is priced cheaper than today. Months with fewer than MIN_MINUTES minutes inherit
the previous month's factor (the first months inherit 1 only if none came before).

usage (trading_models_v2, venv312):
  python docs/tools/build_era_spread_scale.py --asset XAUUSD
writes portfolio_allocator/config/broker_profiles/moneta_cost_models/era_scale/<ASSET>.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
CM_DIR = V2 / "portfolio_allocator" / "config" / "broker_profiles" / "moneta_cost_models"
MIN_MINUTES = 2000
MAX_SPREAD_BPS = 500.0


def _minute_spreads(path: Path) -> pd.DataFrame:
    head = path.open("r", encoding="utf-8").readline().strip().split(",")
    low = {c.lower(): c for c in head}
    tcol = next(low[k] for k in ("timestamp", "time", "datetime") if k in low)
    bcol = next(low[k] for k in ("bidprice", "bid_price", "bid") if k in low)
    acol = next(low[k] for k in ("askprice", "ask_price", "ask") if k in low)
    d = pd.read_csv(path, usecols=[tcol, bcol, acol], dtype={tcol: str, bcol: "float64", acol: "float64"},
                    engine="c")
    if d.empty:
        return pd.DataFrame(columns=["minute", "spread_bps"])
    key = d[tcol].str.slice(0, 16)                       # "YYYY-MM-DD HH:MM" (no per-tick datetime parsing)
    bid, ask = d[bcol].to_numpy(), d[acol].to_numpy()
    mid = (bid + ask) / 2.0
    df = pd.DataFrame({"minute": key.to_numpy(), "spread_bps": (ask - bid) / mid * 1e4, "mid": mid})
    df = df[np.isfinite(df.spread_bps) & (df.spread_bps > 0) & (df.spread_bps < MAX_SPREAD_BPS)]
    return df.groupby("minute", sort=False)[["spread_bps", "mid"]].last().reset_index()


def build(asset: str) -> dict:
    cm_path = CM_DIR / f"{asset}.json"
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    fixed = cm["model"] == "FIXED_POINTS"
    files = sorted((V2 / "assets" / asset / "raw" / "ticks").glob("*_ticks_*_to_*.csv"))
    parts = []
    for i, f in enumerate(files, 1):
        parts.append(_minute_spreads(f))
        if i % 100 == 0:
            print(f"  {asset}: {i}/{len(files)} files", flush=True)
    m = pd.concat(parts, ignore_index=True).drop_duplicates("minute", keep="last")
    m["t"] = pd.to_datetime(m.minute, format="%Y-%m-%d %H:%M", utc=True)
    m = m.sort_values("t").reset_index(drop=True)
    tn = m.t.to_numpy(dtype="datetime64[ns]").astype("int64")

    if fixed:
        m["state"] = 0
        q = []
    else:
        vc = Path(cm["vol_cache"])
        vc = vc if vc.is_absolute() else V2 / vc
        vol = np.load(vc)
        vt, vr = vol["t"].astype("int64"), vol["rv1d"].astype("float64")
        q = np.asarray(cm["vol_state_thresholds_rv1d_bps"], dtype="float64")
        j = np.searchsorted(vt, tn, side="right") - 1
        rv = np.where(j >= 0, vr[np.clip(j, 0, None)], np.nan)
        m["state"] = np.where(np.isfinite(rv), np.digitize(rv, q), -1)
        m = m[m.state >= 0].copy()

    lo, hi = (pd.Timestamp(x, tz="UTC") if pd.Timestamp(x).tzinfo is None else pd.Timestamp(x) for x in cm["span"])
    fit = m[(m.t >= lo) & (m.t <= hi)]
    ref = fit.groupby("state").spread_bps.median().to_dict()
    m = m[m.state.isin(list(ref))].copy()
    m["ratio"] = m.spread_bps / m.state.map(ref)
    m["month"] = m.t.dt.strftime("%Y-%m")
    g = m.groupby("month").agg(ratio=("ratio", "median"), minutes=("ratio", "size"),
                               duka_median_bps=("spread_bps", "median"), mid=("mid", "median"))
    ref_price = float(cm["ref_price"])
    g["points_ratio"] = ref_price / g["mid"]
    factors, prev = {}, 1.0
    for mo, r in g.iterrows():
        f = max(1.0, min(float(r.ratio), float(r.points_ratio))) if r.minutes >= MIN_MINUTES else prev
        factors[mo] = round(f, 4)
        prev = f
    rec = {
        "asset": asset,
        "rule": "STANDING 2026-11-09 (owner): full-history Gate C prices the broker half-spread x factor[UTC month]",
        "method": ("factor = max(1, min(monthly median of duka_minute_spread / duka_fit_window_median[same vol state],"
                   " ref_price / monthly median duka mid))  [points-ratio cap approved 2026-11-09]"),
        "ref_price": ref_price,
        "cost_model": str(cm_path.relative_to(V2)).replace("\\", "/"),
        "cost_model_fit_span": cm["span"],
        "vol_state_thresholds_rv1d_bps": list(map(float, q)),
        "fit_window_duka_median_bps_by_state": {str(k): round(float(v), 4) for k, v in ref.items()},
        "min_minutes_per_month": MIN_MINUTES,
        "built_utc": datetime.now(timezone.utc).isoformat(),
        "n_tick_files": len(files),
        "factors": factors,
        "monthly": {mo: {"ratio": round(float(r.ratio), 4), "minutes": int(r.minutes),
                         "duka_median_bps": round(float(r.duka_median_bps), 4),
                         "median_mid": round(float(r.mid), 6),
                         "points_ratio": round(float(r.points_ratio), 4)} for mo, r in g.iterrows()},
    }
    out = CM_DIR / "era_scale" / f"{asset}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    yr = pd.Series(factors)
    yr.index = yr.index.str[:4]
    print(asset, "ref by state", rec["fit_window_duka_median_bps_by_state"])
    print(asset, "mean factor by year", yr.groupby(level=0).mean().round(2).to_dict())
    print("written", out)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    build(ap.parse_args().asset)


if __name__ == "__main__":
    main()
