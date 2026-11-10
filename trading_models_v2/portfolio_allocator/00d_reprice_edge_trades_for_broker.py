"""Step 00d -- re-price every deployable edge's historical trades at the CURRENT broker's real costs.

ADDED 2026-10-16 (RUNBOOK v5.1 broker cost model). Research certifies edges on the Dukascopy mid; what you actually pay
depends on the prop firm. This step replays each edge's labelled events EXACTLY like Task 2A
(shared/execution/oof_exact_execution._resolve_one: same entry/exit rules, same calendar-time vertical barrier, same
geometry from edge.yaml), but with:
  * Bid/Ask = Dukascopy MID +/- the firm's modelled spread (portfolio_allocator/config/broker_profiles/<firm>_cost_models/
    <ASSET>.json: FIXED_POINTS or hour-of-week x volatility TABLE fitted and validated on the firm's own MT5 ticks),
  * entry delayed by the measured live bar-close->fill latency (--entry-delay-ms, default 5700),
  * swap charged per rollover actually crossed (firm's spec + detected server clock) and round-trip commission.
Edge folders are NEVER written. Output: portfolio_allocator/outputs/broker_repricing/<firm>/<asset>__<family_side>.csv
(Datetime, side, executed_r [net, firm], executed_r_pre_carry, swap_R, commission_R, ...), plus a summary json.
04b uses these values instead of the Dukascopy-priced executed_r ONLY when
portfolio_allocator/config/broker_repricing.json says {"enabled": true, "firm": "<firm>"}.

Run after 00a/00c whenever you switch firm (or refresh the firm's cost models).
usage: python portfolio_allocator/00d_reprice_edge_trades_for_broker.py --firm moneta [--edge ASSET/family_side ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.edge_yaml_contract import validate_edge_yaml_file  # noqa: E402
from shared.edges.trade_geometry import load_trade_geometry  # noqa: E402
from shared.execution import oof_exact_execution as exact  # noqa: E402
from shared.execution.executable_target_dataset import SOURCE_ROW_COL, load_gross_model_rows  # noqa: E402
from shared.research.v3_horizon_lab import _xef_apply_carry_costs, _xef_broker_loader  # noqa: E402

SCHEMA_VERSION = "v1.0"
PROFILES = V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles"
OUT_ROOT = V2_ROOT / "portfolio_allocator" / "outputs" / "broker_repricing"
UNIVERSE = V2_ROOT / "config" / "edge_universe.json"


def deployable_edges() -> list[str]:
    u = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    return [f'{e["asset"]}/{e["family"]}_{e["side"]}' for e in u["edges"]
            if e.get("enabled", True) and e.get("status") == "deployability_pass"]


def reprice_edge(edge: str, firm: str, delay_ms: int) -> dict[str, Any]:
    asset, name = edge.split("/")
    cm_path = PROFILES / f"{firm}_cost_models" / f"{asset}.json"
    if not cm_path.exists():
        raise FileNotFoundError(f"No {firm} cost model for {asset}: {cm_path} (run 00c first)")
    cm = json.loads(cm_path.read_text(encoding="utf-8"))
    edge_root = V2_ROOT / "assets" / asset / "edges" / name
    layout = get_edge_layout_from_edge_root(edge_root)
    validate_edge_yaml_file(layout.edge_root)
    geometry = load_trade_geometry(layout.edge_yaml, expected_edge_id=layout.edge_id, expected_side_name=layout.side_name)
    layout, gross, _ = load_gross_model_rows(layout.edge_root, str(geometry["geometry_hash"]))
    runtime, model_rows_module = exact._import_edge_modules(layout.edge_root)
    max_age = exact._effective_live_max_signal_age_seconds(layout.v2_root)
    if max_age * 1000 <= delay_ms:
        raise ValueError(f"max signal age {max_age}s <= entry delay {delay_ms}ms: every entry would be stale")
    bars_dt, bars_close = exact._load_bars(runtime)
    catalog = exact._build_tick_catalog(model_rows_module)
    bars_dt_ns = bars_dt.as_unit("ns").asi8 if bool(geometry.get("calendar_time")) else None

    original = exact._load_tick_file                       # Dukascopy loader (lru-cached)
    exact._load_tick_file = _xef_broker_loader(original, cm)  # firm's bid/ask around the Dukascopy mid
    try:
        rows, t0 = [], time.time()
        for _, src in gross.iterrows():
            r = src.copy(); r["source_oof_row_number"] = int(src[SOURCE_ROW_COL])
            rows.append(exact._resolve_one(r, bars_dt=bars_dt, bars_close=bars_close, catalog=catalog, geometry=geometry,
                                           max_signal_age_seconds=max_age, bars_dt_ns=bars_dt_ns, entry_delay_ms=delay_ms))
    finally:
        exact._load_tick_file = original
    ex = pd.DataFrame(rows)
    ok = ex["execution_status"].eq("resolved")
    carry_in = pd.DataFrame({"status": np.where(ok, "resolved", "unresolved"), "entry_price": ex.get("entry_price"),
                             "sl_dist": ex.get("stop_distance_price"), "side": ex["side"],
                             "entry_ts": ex.get("entry_time"), "exit_ts": ex.get("exit_time"), "exec_r": ex.get("executed_r")})
    carry = _xef_apply_carry_costs(carry_in, cm)
    out = pd.DataFrame({
        exact.TIME_COL: ex[exact.TIME_COL], "side": ex["side"], "execution_status": ex["execution_status"],
        "executed_r": carry["exec_r"], "executed_r_pre_carry": carry.get("exec_r_pre_carry"),
        "swap_R": carry.get("swap_R"), "commission_R": carry.get("commission_R"), "swap_nights": carry.get("swap_nights"),
        "entry_time": ex.get("entry_time"), "exit_time": ex.get("exit_time"), "exit_reason": ex.get("exit_reason"),
        "entry_spread_bps": ex.get("entry_spread_bps"), "source_event_row_number": gross[SOURCE_ROW_COL].to_numpy(),
        # modelled round-trip spread cost of this trade in R (spread / stop distance) -> live per-trade cost guard baseline
        "spread_R": pd.to_numeric(ex.get("entry_spread"), errors="coerce") / pd.to_numeric(ex.get("stop_distance_price"), errors="coerce"),
    })
    od = OUT_ROOT / firm; od.mkdir(parents=True, exist_ok=True)
    op = od / f"{asset}__{name}.csv"; out.to_csv(op, index=False)
    r = pd.to_numeric(out.loc[ok, "executed_r"], errors="coerce")
    return {"edge": edge, "rows": int(len(out)), "resolved": int(ok.sum()), "coverage": round(float(ok.mean()), 5),
            "mean_executed_r_net": round(float(r.mean()), 6) if len(r) else None,
            "mean_pre_carry": round(float(pd.to_numeric(out.loc[ok, "executed_r_pre_carry"]).mean()), 6) if ok.any() else None,
            "mean_swap_R": round(float(pd.to_numeric(out.loc[ok, "swap_R"]).mean()), 6) if ok.any() else None,
            "mean_commission_R": round(float(pd.to_numeric(out.loc[ok, "commission_R"]).mean()), 6) if ok.any() else None,
            "mean_spread_R": round(float(pd.to_numeric(out.loc[ok, "spread_R"]).mean()), 6) if ok.any() else None,
            "cost_model": str(cm_path.relative_to(V2_ROOT)), "file": str(op.relative_to(V2_ROOT)), "secs": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--firm", required=True, help="Broker profile name, e.g. moneta (uses <firm>_cost_models/).")
    ap.add_argument("--edge", action="append", help="ASSET/family_side (default: every deployable edge in edge_universe.json).")
    ap.add_argument("--entry-delay-ms", type=int, default=5700, help="Measured live bar-close->fill latency (default 5700).")
    args = ap.parse_args()
    edges = args.edge or deployable_edges()
    summary = {"schema_version": SCHEMA_VERSION, "firm": args.firm, "entry_delay_ms": args.entry_delay_ms,
               "created_at": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"), "edges": {}}
    for e in edges:
        try:
            res = reprice_edge(e, args.firm, args.entry_delay_ms)
        except Exception as ex:  # noqa: BLE001  (record and continue; 04b falls back to the research value for this edge)
            res = {"edge": e, "error": f"{type(ex).__name__}: {ex}"[:400]}
        summary["edges"][e] = res
        print(f"REPRICE {e}: " + (f"coverage={res['coverage']} net={res['mean_executed_r_net']} pre={res['mean_pre_carry']} "
                                   f"swap={res['mean_swap_R']} comm={res['mean_commission_R']}" if "error" not in res else res["error"]), flush=True)
    od = OUT_ROOT / args.firm; od.mkdir(parents=True, exist_ok=True)
    (od / "reprice_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Saved: {od / 'reprice_summary.json'}")


if __name__ == "__main__":
    main()
