"""Label head-to-head (2026-10-17): build per-trade outcomes for an edge under
  B  = Dukascopy MID path (zero spread, no swap/commission), entry delay 0 ms  (firm-neutral label)
  C  = Moneta broker cost model (spread + swap + commission), entry delay 5700 ms (live-truth label AND yardstick)
using the exact Task 2A resolver via portfolio_allocator/00d (same rows, same geometry). Writes ONLY to the scratchpad.
usage: build_labels.py OUTDIR ASSET/edge"""
import copy, importlib.util, json, sys
from pathlib import Path
V2 = Path(r"C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"); sys.path.insert(0, str(V2))
out = Path(sys.argv[1]); edge = sys.argv[2]; asset = edge.split("/")[0]
spec = importlib.util.spec_from_file_location("reprice00d", V2 / "portfolio_allocator" / "00d_reprice_edge_trades_for_broker.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
prof = out / "profiles"; (prof / "moneta_cost_models").mkdir(parents=True, exist_ok=True); (prof / "midpath_cost_models").mkdir(parents=True, exist_ok=True)
cm = json.loads((V2 / "portfolio_allocator/config/broker_profiles/moneta_cost_models" / f"{asset}.json").read_text(encoding="utf-8"))
(prof / "moneta_cost_models" / f"{asset}.json").write_text(json.dumps(cm), encoding="utf-8")
mid = copy.deepcopy(cm); mid.update(model="FIXED_POINTS", fixed_points=0.0, commission_bps_rt=0.0)
mid["swap"] = dict(mid["swap"], mode=0, long=0.0, short=0.0)
(prof / "midpath_cost_models" / f"{asset}.json").write_text(json.dumps(mid), encoding="utf-8")
m.PROFILES = prof; m.OUT_ROOT = out / "repriced"
import pandas as pd
for firm, delay in (("midpath", 0), ("moneta", 5700)):
    op = m.OUT_ROOT / firm / f"{asset}__{edge.split('/')[1]}.csv"
    try:
        m.reprice_edge(edge, firm, delay)
    except ValueError as ex:  # 00d's summary uses relative_to(V2_ROOT); our scratch output is outside the repo AFTER the CSV is written
        if "is not in the subpath" not in str(ex) or not op.exists():
            raise
    d = pd.read_csv(op); ok = d.execution_status.eq("resolved")
    print("LABELS", edge, firm, delay, json.dumps({"rows": len(d), "coverage": round(float(ok.mean()), 5),
          "mean_executed_r_net": round(float(d.loc[ok, "executed_r"].mean()), 6), "mean_spread_R": round(float(d.loc[ok, "spread_R"].mean()), 6),
          "mean_swap_R": round(float(d.loc[ok, "swap_R"].mean()), 6)}), flush=True)
