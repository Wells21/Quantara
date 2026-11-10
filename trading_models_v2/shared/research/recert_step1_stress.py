"""RECERT step 1, stress (runbook Section 6A Step 7 STRESS): replay each re-selected winner with the broker spread level
scaled by (1 + level_uncertainty_pct / 100) of its Moneta cost model; net R <= 0 there = flagged fragile.

The scaled cost model is a copy of the asset's model with every spread level multiplied (FIXED_POINTS: fixed_points;
TABLE: table cells and hour means; state multipliers are relative and unchanged). Swap and commission are not scaled.
The replay uses the same lab call as the 2026-10-31 runs (feature schema at 63f4a570 until the RECERT re-export).

Break-even spread multiple (approximation, recorded for the live per-trade guard): 1 + mean(exec_r) / mean(spread_R) at
the base model, spread_R = entry_spread / sl_dist (one full spread per round trip).

usage (from trading_models_v2, venv312): python shared/research/recert_step1_stress.py ASSET/edge OUT_DIR
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[2]
CM = V2 / "portfolio_allocator" / "config" / "broker_profiles" / "moneta_cost_models"
SHIM = V2 / "shared" / "research" / "grid_dimension_sensitivity" / "lab_with_schema_at_commit.py"


def scaled_model(asset: str, out: Path) -> tuple[Path, float]:
    m = json.loads((CM / f"{asset}.json").read_text(encoding="utf-8"))
    f = 1.0 + float(m["level_uncertainty_pct"]) / 100.0
    if m["model"] == "FIXED_POINTS":
        m["fixed_points"] = float(m["fixed_points"]) * f
    else:
        m["table"] = {k: float(v) * f for k, v in m["table"].items()}
        m["hour_mean"] = {k: float(v) * f for k, v in m["hour_mean"].items()}
    m["mean_spread_bps_time_weighted"] = float(m["mean_spread_bps_time_weighted"]) * f
    m["stress_note"] = f"RECERT step 1 stress: spread levels x {f:.4f} (1 + level_uncertainty_pct)"
    p = out / f"{asset}_stress_x{f:.3f}.json"
    p.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return p, f


def main(edge: str, out: Path) -> None:
    asset, name = edge.split("/")
    root = V2 / "assets" / asset / "edges" / name
    art = root / "artifacts" / "v3_horizon_lab"
    sel = json.loads((V2 / "logs" / "recert_20261103" / "step1_reselection.json").read_text(encoding="utf-8"))[edge]
    w = sel["winner"]
    out.mkdir(parents=True, exist_ok=True)
    cm, f = scaled_model(asset, out)
    before = set(art.glob("v3_execution_velocity_summary_*.csv"))
    cell = ":".join(f"{w[k]:g}" for k in ("event_vol_lookback", "event_vol_ewm_span", "vertical_horizon", "pt", "sl"))
    cmd = [sys.executable, str(SHIM), "--edge-root", str(root), "--execution-finalists", cell,   # gate_c_runner's call
           "--execution-entry-delay-ms", "5700", "--execution-bootstrap-reps", "2000",
           "--execution-spread-model", "broker", "--execution-cost-model", str(cm), "--execution-no-control", "--save"]
    log = out / f"{asset}_{name}_stress.log"
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.call(cmd, cwd=V2, stdout=fh, stderr=subprocess.STDOUT)
    new = sorted(set(art.glob("v3_execution_velocity_summary_*.csv")) - before)
    res = {"edge": edge, "winner": w["label"], "spread_factor": f, "returncode": rc, "cost_model": str(cm), "log": str(log)}
    if rc == 0 and new:
        s = pd.read_csv(new[-1])
        r = s[s.label == w["label"]].iloc[0]
        res.update(stress_stamp=new[-1].stem.split("_")[-1], stress_exec_mean_R=float(r.exec_mean_R),
                   stress_coverage=float(r.coverage_rate), base_exec_mean_R=w["exec_mean_R"],
                   fragile=bool(r.exec_mean_R <= 0))
        t = pd.read_csv(art / f"v3_execution_trades_{w['label']}_{w['run_stamp']}.csv")
        t = t[t.status == "resolved"]
        spread_r = (t.entry_spread / t.sl_dist).mean()
        res["base_mean_spread_R"] = float(spread_r)
        res["break_even_spread_multiple_approx"] = float(1.0 + t.exec_r.mean() / spread_r)
    (out / f"{asset}_{name}_stress.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
