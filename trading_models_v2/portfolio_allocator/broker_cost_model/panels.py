"""READ-ONLY MT5 extraction for the broker cost model.

Attach-only by default (no login/password/server passed -> a live bot on the same terminal is never re-logged); only
symbol_info / copy_ticks_range / history_deals_get / account_info are called; mt5.shutdown() disconnects this process only.
Outputs (portfolio_allocator/outputs/broker_cost_inputs/<firm>/):
  symbol_cost_specs.json, deal_history.csv, panel_<asset>_broker.pkl (per-minute, SERVER clock), panel_<asset>_duka.pkl
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SPEC_FIELDS = ["name", "currency_base", "currency_profit", "currency_margin", "digits", "point", "trade_tick_size",
               "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss", "trade_contract_size", "trade_calc_mode",
               "swap_mode", "swap_long", "swap_short", "swap_rollover3days", "volume_min", "volume_step", "trade_stops_level"]



def _tick_root(v2_root: Path, asset: str) -> Path:
    """Research tick folder from AssetLayout (curated per config/asset_data_policy.json; AFML Ch.2 walkthrough)."""
    import sys

    if str(v2_root) not in sys.path:
        sys.path.insert(0, str(v2_root))
    from shared.assets.asset_layout import get_asset_layout

    return get_asset_layout(asset, v2_root).tick_root

def per_minute(ts: Any, bid: Any, ask: Any) -> pd.DataFrame:
    """ts: tz-aware datetimes (never raw int64: venv312 storage unit can be us, not ns)."""
    bid = np.asarray(bid, dtype="float64"); ask = np.asarray(ask, dtype="float64")
    mid = (bid + ask) / 2.0; sb = (ask - bid) / mid * 1e4
    m = pd.DataFrame({"m": pd.DatetimeIndex(ts).floor("min"), "sb": sb, "mid": mid})
    g = m.groupby("m")
    return pd.DataFrame({"spread_bps": g.sb.median(), "n": g.sb.size(), "mid": g.mid.last()})


def capture_specs_and_deals(mt5: Any, symbol_map: dict[str, str], out_dir: Path) -> dict[str, Any]:
    acct = mt5.account_info()
    specs = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "server": acct.server,
             "company": acct.company, "account_currency": acct.currency, "symbols": {}}
    for asset, sym in symbol_map.items():
        mt5.symbol_select(sym, True); i = mt5.symbol_info(sym)
        specs["symbols"][asset] = {k: (getattr(i, k, None) if i else None) for k in SPEC_FIELDS}
    (out_dir / "symbol_cost_specs.json").write_text(json.dumps(specs, indent=1, default=str), encoding="utf-8")
    deals = mt5.history_deals_get(dt.datetime(2015, 1, 1, tzinfo=dt.timezone.utc), dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
    if deals:
        d = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys()); d.to_csv(out_dir / "deal_history.csv", index=False)
    return specs


def measured_server_offset(mt5: Any, symbols: list[str]) -> int | None:
    """Trade-server clock minus UTC from a FRESH live quote (market open); None if no fresh quote."""
    now = time.time()
    for s in symbols:
        t = mt5.symbol_info_tick(s)
        if t is None or not getattr(t, "time", 0):
            continue
        diff = (float(t.time) - now) / 3600.0; w = round(diff)
        if abs(diff - w) < 0.1:
            return int(w)
    return None


def broker_minute_panel(mt5: Any, symbol: str, start: dt.datetime, end: dt.datetime) -> pd.DataFrame:
    parts, day = [], start
    while day < end:
        t = mt5.copy_ticks_range(symbol, day, day + dt.timedelta(days=1), mt5.COPY_TICKS_INFO)
        if t is not None and len(t):
            ok = (t["bid"] > 0) & (t["ask"] >= t["bid"])
            parts.append(per_minute(pd.to_datetime(t["time_msc"][ok], unit="ms", utc=True), t["bid"][ok], t["ask"][ok]))
        day += dt.timedelta(days=1); time.sleep(0.15)
    b = pd.concat(parts).sort_index() if parts else pd.DataFrame(columns=["spread_bps", "n", "mid"])
    b = b[~b.index.duplicated(keep="last")]; b.index.name = "minute_server_time"   # NOTE: SERVER clock (labelled UTC)
    return b


def dukascopy_minute_panel(v2_root: Path, asset: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    parts = []
    for f in sorted(glob.glob(str(_tick_root(v2_root, asset) / "*.csv"))):
        s = pd.Timestamp(re.search(r"_ticks_(\d{4}-\d{2}-\d{2})", f).group(1), tz="UTC")
        e = pd.Timestamp(re.search(r"_to_(\d{4}-\d{2}-\d{2})", f).group(1), tz="UTC")
        if e < start - pd.Timedelta(days=8) or s > end + pd.Timedelta(days=1):
            continue
        d = pd.read_csv(f, usecols=[0, 1, 2]); d.columns = ["ts", "bid", "ask"]
        d["ts"] = pd.to_datetime(d.ts, utc=True, format="mixed"); d = d[(d.bid > 0) & (d.ask >= d.bid)]
        if len(d):
            parts.append(per_minute(d.ts, d.bid.to_numpy(), d.ask.to_numpy()))
    k = pd.concat(parts).sort_index() if parts else pd.DataFrame(columns=["spread_bps", "n", "mid"])
    k = k[~k.index.duplicated(keep="last")]; k.index.name = "minute_utc"
    return k
