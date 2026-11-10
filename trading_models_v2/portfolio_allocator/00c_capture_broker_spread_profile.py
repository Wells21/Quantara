"""Step 00c -- capture the CURRENT broker's spread profile from its own MT5 tick history.

ADDED 2026-10-16 (broker-independent spread certification). Edges are certified in research with a maximum
spread they tolerate (Gate C spread ladder on the Dukascopy mid). This step measures what the broker you are
about to trade actually charges, so the allocator can re-price each edge's trades at THIS broker's spread and
drop edges whose certified maximum is below it. Run it right after 00a whenever you switch prop firm/broker.

READ-ONLY: attaches to the running, logged-in MT5 terminal (no re-login unless --use-login-settings), only calls
symbol_info_tick / copy_ticks_range, never sends orders or changes settings, and disconnects only itself.

Per symbol (last --days of broker ticks): spread in bps (ask-bid)/mid -- overall median/p75/p95, and median/p95
by UTC hour (broker tick times are trade-server time; the offset is measured from live quotes or passed in).
Output: portfolio_allocator/config/broker_profiles/broker_spread_profile.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
SCHEMA_VERSION = "v1.0"
DEFAULT_LOCAL_SETTINGS_PATH = V2_ROOT / "portfolio_live" / "local_live_settings.json"
DEFAULT_OUTPUT_PATH = V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles" / "broker_spread_profile.json"
DEFAULT_ASSETS = ["XAUUSD", "NASDAQ", "BTCUSD", "DAX", "GBPJPY", "JPN", "USOIL", "HK", "US500", "USDMXN"]


def _load_00a() -> Any:
    spec = importlib.util.spec_from_file_location("capture_mt5_broker_profile_00a", THIS_FILE.with_name("00a_capture_mt5_broker_profile.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def server_offset_hours(mt5: Any, symbols: list[str]) -> int | None:
    """Trade-server clock minus UTC, from a FRESH live quote (market open). None if no fresh quote."""
    now = time.time()
    for sym in symbols:
        tick = mt5.symbol_info_tick(sym)
        if tick is None or not getattr(tick, "time", 0):
            continue
        diff_h = (float(tick.time) - now) / 3600.0
        whole = round(diff_h)
        if abs(diff_h - whole) < 0.1:  # quote is fresh (within ~6 min of a whole-hour offset)
            return int(whole)
    return None


def pull_ticks(mt5: Any, symbol: str, days: int) -> pd.DataFrame:
    end = datetime.now(timezone.utc) + timedelta(days=1)
    day = end - timedelta(days=days + 1)
    parts = []
    while day < end:
        t = mt5.copy_ticks_range(symbol, day, day + timedelta(days=1), mt5.COPY_TICKS_INFO)
        if t is not None and len(t):
            parts.append(pd.DataFrame({"time_msc": t["time_msc"], "bid": t["bid"], "ask": t["ask"]}))
        day += timedelta(days=1)
        time.sleep(0.2)
    if not parts:
        return pd.DataFrame(columns=["time_msc", "bid", "ask"])
    d = pd.concat(parts, ignore_index=True).drop_duplicates("time_msc", keep="last")
    return d[(d.bid > 0) & (d.ask >= d.bid)].sort_values("time_msc").reset_index(drop=True)


def profile(d: pd.DataFrame, offset_h: int) -> dict[str, Any]:
    mid = (d.bid + d.ask) / 2.0
    sb = (d.ask - d.bid) / mid * 1e4
    hour = (pd.to_datetime(d.time_msc, unit="ms", utc=True) - pd.Timedelta(hours=offset_h)).dt.hour
    g = sb.groupby(hour)
    q = lambda s, p: round(float(s.quantile(p)), 4)
    return {
        "n_ticks": int(len(d)),
        "first_tick_utc": str(pd.to_datetime(d.time_msc.iloc[0], unit="ms", utc=True) - pd.Timedelta(hours=offset_h)),
        "last_tick_utc": str(pd.to_datetime(d.time_msc.iloc[-1], unit="ms", utc=True) - pd.Timedelta(hours=offset_h)),
        "spread_bps_median": q(sb, .5), "spread_bps_p75": q(sb, .75), "spread_bps_p95": q(sb, .95),
        "spread_bps_median_by_utc_hour": {int(h): round(float(v), 4) for h, v in g.median().items()},
        "spread_bps_p95_by_utc_hour": {int(h): round(float(v), 4) for h, v in g.quantile(.95).items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--asset", action="append", help="Asset(s) to profile (default: all 10).")
    ap.add_argument("--days", type=int, default=28, help="Calendar days of broker tick history to use (default 28).")
    ap.add_argument("--server-utc-offset-hours", type=int, default=None,
                    help="Trade-server clock minus UTC. Measured from live quotes when omitted (market must be open).")
    ap.add_argument("--local-settings", default=str(DEFAULT_LOCAL_SETTINGS_PATH))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    ap.add_argument("--login", type=int); ap.add_argument("--password"); ap.add_argument("--server")
    ap.add_argument("--terminal-path"); ap.add_argument("--timeout-ms", type=int, default=None)
    ap.add_argument("--use-login-settings", action="store_true",
                    help=("Pass login/password/server (00a's resolution) to mt5.initialize. Default is ATTACH-ONLY to the "
                          "already-running, already-logged-in terminal, so a live bot on the same terminal is not re-logged."))
    args = ap.parse_args()

    import MetaTrader5 as mt5
    a00 = _load_00a()
    local = a00.load_json(Path(args.local_settings).expanduser().resolve(), required=False)
    symbol_map = local.get("mt5_symbol_map") or {}
    assets = args.asset or DEFAULT_ASSETS
    symbols = {a: symbol_map.get(a, a) for a in assets}

    init_kwargs = a00.mt5_initialize_kwargs(args, local)
    if not args.use_login_settings:
        init_kwargs = {k: v for k, v in init_kwargs.items() if k in ("timeout", "path")}
    if not mt5.initialize(**init_kwargs):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        acct = mt5.account_info()
        offset = args.server_utc_offset_hours
        if offset is None:
            offset = server_offset_hours(mt5, list(symbols.values()))
        if offset is None:
            raise SystemExit("Could not measure the trade-server UTC offset (no fresh quote; market closed?). "
                             "Pass --server-utc-offset-hours explicitly.")
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "broker_server": getattr(acct, "server", None), "broker_company": getattr(acct, "company", None),
            "server_utc_offset_hours": offset, "days": args.days,
            "purpose": "Broker spread profile for allocator re-pricing and per-edge max-spread matching (00c).",
            "symbols": {},
        }
        for asset, sym in symbols.items():
            mt5.symbol_select(sym, True)
            d = pull_ticks(mt5, sym, args.days)
            if d.empty:
                out["symbols"][asset] = {"broker_symbol": sym, "status": "no_ticks"}
                print(f"{asset:7s} {sym:10s} no ticks", flush=True)
                continue
            p = profile(d, offset)
            out["symbols"][asset] = {"broker_symbol": sym, "status": "ok", **p}
            print(f"{asset:7s} {sym:10s} ticks={p['n_ticks']:>9d} median={p['spread_bps_median']:.3f} "
                  f"p75={p['spread_bps_p75']:.3f} p95={p['spread_bps_p95']:.3f} bps", flush=True)
    finally:
        mt5.shutdown()
    op = Path(args.output).expanduser().resolve(); op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"Saved: {op}")


if __name__ == "__main__":
    main()
