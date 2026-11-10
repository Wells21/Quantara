"""Step 00c -- build + validate the CURRENT prop firm's broker cost model (RUNBOOK v5.1, 2026-10-16).

Runs as a normal step of every allocator run (00a -> 00c -> 00d -> 01..11), so the spread level is as fresh as the
allocation; no separate refresh schedule (the live per-trade guard checks the actual spread at every entry). Then run 00d
(re-price every edge's trades with this model) and enable portfolio_allocator/config/broker_repricing.json for the firm.

Chain (per asset):
  1. READ-ONLY MT5 (attach-only unless --use-login-settings): symbol specs (swaps), deal history (commission, fills),
     per-minute broker spread/mid panels from the firm's first available tick to now.
  2. Dukascopy per-minute panel over the same dates (for measured clock alignment) -- if Dukascopy coverage does not
     reach the firm's history, download those weeks first (auto_downloader) for a measured alignment; otherwise the
     detected rule is used for those weeks (flagged).
  3. Server clock: measured weekly vs Dukascopy; fallback = rule detected from the firm's own FX ticks + live offset.
  4. Spread model fit (FIXED_POINTS / hour-of-week x vol-state TABLE), blocked-week CV, walk-forward uncertainty band,
     level = last 14 days, live-entry calibration (firm's own executed trades, shrunk n/(n+30)).
  5. Swap spec (current rates) + commission (median round-trip bps from the firm's deals).
Outputs: portfolio_allocator/config/broker_profiles/<firm>_cost_models/<ASSET>.json, <firm>_server_tz.json,
         portfolio_allocator/outputs/broker_cost_inputs/<firm>/ (raw inputs + cost_model_report.json).
usage: python portfolio_allocator/00c_build_broker_cost_model.py --firm moneta [--asset XAUUSD ...] [--skip-extract]
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
for p in (V2_ROOT, THIS_FILE.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from broker_cost_model import carry, panels, server_tz, spread_model, vol_cache  # noqa: E402

DEFAULT_ASSETS = ["XAUUSD", "NASDAQ", "BTCUSD", "DAX", "GBPJPY", "JPN", "USOIL", "HK", "US500", "USDMXN"]
PROFILES = V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles"
VOL_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "dukascopy_minute_vol"
LOCAL_SETTINGS = V2_ROOT / "portfolio_live" / "local_live_settings.json"
LIVE_LEDGER = V2_ROOT / "portfolio_live" / "outputs" / "portfolio_execution_bot" / "portfolio_executed_trades.csv"
FX_FOR_TZ = ("GBPJPY", "USDMXN", "EURUSD", "XAUUSD")


def _load_00a():
    spec = importlib.util.spec_from_file_location("capture_00a", THIS_FILE.with_name("00a_capture_mt5_broker_profile.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def extract(args: argparse.Namespace, assets: list[str], inp: Path) -> int | None:
    import MetaTrader5 as mt5
    a00 = _load_00a()
    local = a00.load_json(LOCAL_SETTINGS, required=False)
    symbol_map = {a: (local.get("mt5_symbol_map") or {}).get(a, a) for a in assets}
    kw = a00.mt5_initialize_kwargs(args, local)
    if not args.use_login_settings:
        kw = {k: v for k, v in kw.items() if k in ("timeout", "path")}
    if not mt5.initialize(**kw):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        panels.capture_specs_and_deals(mt5, symbol_map, inp)
        offset = args.server_utc_offset_hours if args.server_utc_offset_hours is not None else panels.measured_server_offset(mt5, list(symbol_map.values()))
        start = dt.datetime.fromisoformat(args.history_start).replace(tzinfo=dt.timezone.utc)
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        for a, sym in symbol_map.items():
            b = panels.broker_minute_panel(mt5, sym, start, end); b.to_pickle(inp / f"panel_{a.lower()}_broker.pkl")
            print(f"PANEL {a}: broker minutes={len(b)} {b.index.min() if len(b) else None} .. {b.index.max() if len(b) else None}", flush=True)
    finally:
        mt5.shutdown()
    for a in assets:
        b = pd.read_pickle(inp / f"panel_{a.lower()}_broker.pkl")
        if len(b):
            idx = pd.DatetimeIndex(b.index); idx = idx.tz_localize(None) if idx.tz is not None else idx
            k = panels.dukascopy_minute_panel(V2_ROOT, a, (idx.min() - pd.Timedelta(days=1)).tz_localize("UTC"), (idx.max() + pd.Timedelta(days=1)).tz_localize("UTC"))
            k.to_pickle(inp / f"panel_{a.lower()}_duka.pkl")
    return offset


def live_entries(asset: str, deals_first: pd.Timestamp | None) -> pd.DataFrame:
    if not LIVE_LEDGER.exists():
        return pd.DataFrame()
    e = pd.read_csv(LIVE_LEDGER)
    e = e[e.edge_id.astype(str).str.startswith(asset + "/")].copy()
    e["t"] = to_datetime_iso(e.event_time, utc=True, errors="coerce")
    e = e.dropna(subset=["t"])
    if deals_first is not None:
        e = e[e.t >= deals_first]   # only trades made on THIS firm's account
    return e


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--firm", required=True)
    ap.add_argument("--asset", action="append")
    ap.add_argument("--history-start", default="2025-10-01", help="Earliest date to request broker ticks from (empty days skipped).")
    ap.add_argument("--skip-extract", action="store_true", help="Reuse inputs already in outputs/broker_cost_inputs/<firm>/.")
    ap.add_argument("--server-utc-offset-hours", type=int, default=None, help="Live measured offset if no fresh quote (market closed).")
    ap.add_argument("--no-live-calibration", action="store_true")
    ap.add_argument("--use-login-settings", action="store_true")
    ap.add_argument("--login", type=int); ap.add_argument("--password"); ap.add_argument("--server")
    ap.add_argument("--terminal-path"); ap.add_argument("--timeout-ms", type=int, default=None)
    args = ap.parse_args()
    assets = args.asset or DEFAULT_ASSETS
    inp = V2_ROOT / "portfolio_allocator" / "outputs" / "broker_cost_inputs" / args.firm; inp.mkdir(parents=True, exist_ok=True)
    offset = args.server_utc_offset_hours
    if not args.skip_extract:
        offset = extract(args, assets, inp)
    specs = json.loads((inp / "symbol_cost_specs.json").read_text(encoding="utf-8"))
    deals = pd.read_csv(inp / "deal_history.csv") if (inp / "deal_history.csv").exists() else None
    deals_first = pd.to_datetime(deals.time_msc.min(), unit="ms", utc=True) if deals is not None and len(deals) else None

    # server clock rule (fallback for weeks without a Dukascopy overlap)
    tz_rule, tz_diag = "NY_CLOSE", {"warning": "no FX panel available; NY_CLOSE assumed"}
    for fx in FX_FOR_TZ:
        pth = inp / f"panel_{fx.lower()}_broker.pkl"
        if pth.exists():
            tz_rule, tz_diag = server_tz.detect_rule(pd.DatetimeIndex(pd.read_pickle(pth).index), measured_offset=offset)
            tz_diag["detected_from_symbol"] = fx; break
    (PROFILES / f"{args.firm}_server_tz.json").write_text(json.dumps({"firm": args.firm, "server_tz_rule": tz_rule, "diagnostics": tz_diag,
        "detected_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")}, indent=1, default=str), encoding="utf-8")
    comm = carry.commission_bps_by_asset(deals, specs)
    out_dir = PROFILES / f"{args.firm}_cost_models"; out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"firm": args.firm, "server_tz_rule": tz_rule, "created_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"), "assets": {}}
    for a in assets:
        try:
            vc = VOL_DIR / f"minute_rv1d_{a.lower()}.npz"
            if not vc.exists():
                vol_cache.build(V2_ROOT, a, VOL_DIR)
            b = pd.read_pickle(inp / f"panel_{a.lower()}_broker.pkl")
            dk = inp / f"panel_{a.lower()}_duka.pkl"; d = pd.read_pickle(dk) if dk.exists() else None
            full = spread_model.add_state(spread_model.align(b, d, tz_rule))
            cm = spread_model.fit(full)
            band = spread_model.walkforward_band(full)
            cm = spread_model.rescale_to_recent(cm, full)
            sym = specs["symbols"][a]
            if not args.no_live_calibration:
                cm = spread_model.live_entry_calibration(cm, full, live_entries(a, deals_first), float(sym["point"]))
            else:
                cm.update(entry_calibration_factor=1.0, entry_calibration_note="disabled by --no-live-calibration")
            cm.update(asset=a, broker=f"{specs.get('company')} / {specs.get('server')}", fitted_at=pd.Timestamp.now(tz="UTC").date().isoformat(),
                      point=float(sym["point"]), level_uncertainty_pct=band.get("level_uncertainty_pct"), walkforward=band,
                      server_tz_rule=tz_rule, clock_alignment_sources={str(k): int(v) for k, v in full.shift_source.value_counts().items()},
                      vol_cache=str(vc.relative_to(V2_ROOT)).replace("\\", "/"), swap=carry.swap_spec(sym, cm["ref_price"]),
                      commission_bps_rt=float(comm.get(a, 0.0)), slippage_beyond_quote_bps=0.0,
                      application_note="Forward-looking: spread and swap converted to bps of notional at ref_price and applied in bps to every era.")
            (out_dir / f"{a}.json").write_text(json.dumps(cm, indent=1, default=str), encoding="utf-8")
            report["assets"][a] = {k: cm.get(k) for k in ("model", "unit", "mean_spread_bps_time_weighted", "level_uncertainty_pct",
                                   "entry_calibration_factor", "commission_bps_rt", "cv_mean_bias_pct_by_week_fold", "clock_alignment_sources")}
            print(f"COSTMODEL {a}: {cm['model']} unit={cm['unit']} level={cm['mean_spread_bps_time_weighted']}bps band=+-{cm['level_uncertainty_pct']}% "
                  f"calib={cm['entry_calibration_factor']} comm={cm['commission_bps_rt']} cv={cm['cv_mean_bias_pct_by_week_fold']}", flush=True)
        except Exception as ex:  # noqa: BLE001
            report["assets"][a] = {"error": f"{type(ex).__name__}: {ex}"[:400]}
            print(f"COSTMODEL {a}: ERROR {ex}", flush=True)
    (inp / "cost_model_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Saved models to {out_dir}")


if __name__ == "__main__":
    main()
