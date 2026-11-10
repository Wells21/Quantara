"""Bar-quality report: AFML Exercise 2.1 for every asset (Ch.2 walkthrough, D024).

Compares the asset's quote-tick bars with time bars built from the same quotes over the same window, with the
same number of bars (time bars over ACTIVE minutes only, so closed sessions are not counted):
  - lag-1 serial correlation of log returns          (AFML Ex. 2.1c: lower |AC1| is better)
  - Jarque-Bera statistic of log returns              (Ex. 2.1e: lower is closer to Normal)
  - coefficient of variation of monthly variances     (Ex. 2.1d: lower is more homoscedastic)
  - coefficient of variation of weekly bar counts     (Ex. 2.1b: tick bars only)
Ticks come from the asset's AssetLayout.tick_root (curated when config/asset_data_policy.json says so).
Output: assets/{ASSET}/bars/standard_quote_tick/bar_quality_report.json.

Run from trading_models_v2:
    python ./shared/bars/bar_quality_report.py --assets ALL --days 365
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))
from shared.assets.asset_layout import get_asset_layout  # noqa: E402


def _properties(close: pd.Series, times: pd.DatetimeIndex) -> dict[str, float]:
    r = np.log(close.to_numpy(dtype="float64"))
    r = pd.Series(np.diff(r), index=times[1:])
    r = r[np.isfinite(r)]
    monthly_var = r.groupby(r.index.strftime("%Y-%m")).var()
    return {"n_returns": int(len(r)), "ac1": float(r.autocorr(1)), "jarque_bera": float(stats.jarque_bera(r).statistic),
            "monthly_variance_cv": float(monthly_var.std() / monthly_var.mean())}


def report_asset(asset: str, days: int) -> dict:
    layout = get_asset_layout(asset)
    bars = pd.read_csv(layout.standard_quote_tick_bars_csv, usecols=["Datetime", "Close"])
    bars["Datetime"] = pd.to_datetime(bars["Datetime"], utc=True, format="ISO8601")
    end = bars["Datetime"].max()
    start = end - pd.Timedelta(days=days)
    bars = bars[bars["Datetime"] > start].reset_index(drop=True)
    minute_mid = []
    for f in sorted(layout.tick_root.glob("*_ticks_*_to_*.csv")):
        f_end = pd.Timestamp(f.stem.split("_to_")[-1], tz="UTC") + pd.Timedelta(days=1)
        if f_end < start:
            continue
        d = pd.read_csv(f, usecols=["timestamp", "bidPrice", "askPrice"])
        d["timestamp"] = pd.to_datetime(d["timestamp"], utc=True, format="ISO8601")
        d = d[(d["timestamp"] > start) & (d["timestamp"] <= end)]
        if len(d):
            minute_mid.append(((d["bidPrice"] + d["askPrice"]) / 2.0).groupby(d["timestamp"].dt.floor("min")).last())
    mm = pd.concat(minute_mid)
    mm = mm[~mm.index.duplicated(keep="last")].sort_index()
    minutes_per_bar = max(1, round(len(mm) / max(len(bars), 1)))
    grp = np.arange(len(mm)) // minutes_per_bar
    time_close = mm.groupby(grp).last()
    time_index = pd.DatetimeIndex(mm.index[::minutes_per_bar][: len(time_close)])
    weekly = bars.groupby(bars["Datetime"].dt.strftime("%G-%V")).size()
    tick = _properties(bars["Close"], pd.DatetimeIndex(bars["Datetime"]))
    tick["weekly_bar_count_cv"] = float(weekly.std() / weekly.mean())
    timeb = _properties(time_close, time_index)
    better = {k: ("tick" if abs(tick[k]) < abs(timeb[k]) else "time") for k in ("ac1", "jarque_bera", "monthly_variance_cv")}
    out = {"asset": asset, "generated_utc": datetime.now(timezone.utc).isoformat(), "window_days": days,
           "window": [str(start), str(end)], "tick_source": str(layout.tick_root.relative_to(V2_ROOT)).replace("\\", "/"),
           "time_bar_active_minutes": minutes_per_bar, "quote_tick_bars": tick, "time_bars": timeb, "better": better}
    (layout.standard_quote_tick_root / "bar_quality_report.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", nargs="+", required=True)
    ap.add_argument("--days", type=int, default=365)
    a = ap.parse_args()
    assets = sorted(p.name for p in (V2_ROOT / "assets").iterdir() if (p / "bars").exists()) if a.assets == ["ALL"] else a.assets
    for asset in assets:
        r = report_asset(asset, a.days)
        t, m = r["quote_tick_bars"], r["time_bars"]
        print(f"{asset:7s} tick: AC1={t['ac1']:+.3f} JB={t['jarque_bera']:>13,.0f} varCV={t['monthly_variance_cv']:.2f} "
              f"weekCV={t['weekly_bar_count_cv']:.2f} | time({r['time_bar_active_minutes']}m): AC1={m['ac1']:+.3f} "
              f"JB={m['jarque_bera']:>13,.0f} varCV={m['monthly_variance_cv']:.2f} | better: {r['better']}", flush=True)


if __name__ == "__main__":
    main()
