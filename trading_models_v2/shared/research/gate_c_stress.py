"""Gate C STRESS gate (D079, owner 2026-11-04; runbook Section 6A Step 7 STRESS): replay the Gate C winner with the
broker spread levels scaled by (1 + level_uncertainty_pct / 100) of the firm's cost model. exec mean R <= 0 there = KILL
(was: flagged fragile). Also records the approximate break-even spread multiple for the live per-trade guard.

The scaled cost model is a copy of the asset's model with every spread level multiplied (FIXED_POINTS: fixed_points;
TABLE: table cells and hour means; state multipliers are relative and unchanged); swap and commission are not scaled.
Break-even spread multiple (approximation): 1 + mean(exec_r) / mean(spread_R) at the base model, spread_R = entry_spread
/ sl_dist (one full spread per round trip), from the winner's base Gate C trade file.

usage (from trading_models_v2, venv312):
  python shared/research/gate_c_stress.py --edge-root assets/<A>/edges/<edge> --cell H:E:V:PT:SL --base-stamp <Gate C stamp>
         [--firm moneta] [--out <json>] [--schema-commit <sha>]   (--schema-commit only for a dataset exported under an
                                                                    older feature schema, as the RECERT reuse did)
exit 0 = PASS, 1 = KILL, 2 = could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[2]
LAB = V2 / "shared" / "research" / "v3_horizon_lab.py"
SHIM = V2 / "shared" / "research" / "grid_dimension_sensitivity" / "lab_with_schema_at_commit.py"


def scaled_model(cost_model: Path, out_dir: Path) -> tuple[Path, float]:
    m = json.loads(cost_model.read_text(encoding="utf-8"))
    f = 1.0 + float(m["level_uncertainty_pct"]) / 100.0
    if m["model"] == "FIXED_POINTS":
        m["fixed_points"] = float(m["fixed_points"]) * f
    else:
        m["table"] = {k: float(v) * f for k, v in m["table"].items()}
        m["hour_mean"] = {k: float(v) * f for k, v in m["hour_mean"].items()}
    m["mean_spread_bps_time_weighted"] = float(m["mean_spread_bps_time_weighted"]) * f
    m["stress_note"] = f"Gate C stress: spread levels x {f:.4f} (1 + level_uncertainty_pct)"
    p = out_dir / f"{cost_model.stem}_stress_x{f:.3f}.json"
    p.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return p, f


def label_of(cell: str) -> str:
    h, e, v, pt, sl = cell.split(":")
    return f"H{h}_E{e}_V{v}_PT{pt}_SL{sl}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--edge-root", required=True)
    ap.add_argument("--cell", required=True, help="Gate C winner as lookback_h:ewm:vertical_h:pt:sl, e.g. 8:200:168:2:1")
    ap.add_argument("--base-stamp", required=True, help="Gate C run stamp holding the winner's base trade file")
    ap.add_argument("--firm", default="moneta")
    ap.add_argument("--out", default=None)
    ap.add_argument("--schema-commit", default=None)
    ap.add_argument("--era-scale", default="",
                    help="era_scale/<ASSET>.json: STANDING 2026-11-09 -- stress the deciding full-history run (era-scaled spread)")
    ap.add_argument("--workers", type=int, default=1, help="parallel replay processes (identical rows)")
    a = ap.parse_args(argv)
    root = (V2 / a.edge_root) if not Path(a.edge_root).is_absolute() else Path(a.edge_root)
    asset = root.parent.parent.name
    art = root / "artifacts" / "v3_horizon_lab"
    out = Path(a.out) if a.out else art / f"gate_c_stress_{label_of(a.cell)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cm, f = scaled_model(V2 / "portfolio_allocator" / "config" / "broker_profiles" / f"{a.firm}_cost_models" / f"{asset}.json",
                         out.parent)
    before = set(art.glob("v3_execution_velocity_summary_*.csv"))
    env = dict(os.environ)
    lab = LAB
    if a.schema_commit:
        lab, env["SCHEMA_COMMIT"] = SHIM, a.schema_commit
    cmd = [sys.executable, str(lab), "--edge-root", str(root), "--execution-finalists", a.cell,
           "--execution-entry-delay-ms", "5700", "--execution-bootstrap-reps", "2000",
           "--execution-spread-model", "broker", "--execution-cost-model", str(cm), "--execution-no-control", "--save"]
    if a.era_scale:
        cmd += ["--execution-era-scale", str(V2 / a.era_scale) if not Path(a.era_scale).is_absolute() else a.era_scale]
    if a.workers > 1:
        cmd += ["--execution-workers", str(a.workers)]
    log = out.with_suffix(".log")
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.call(cmd, cwd=V2, stdout=fh, stderr=subprocess.STDOUT, env=env)
    new = sorted(set(art.glob("v3_execution_velocity_summary_*.csv")) - before)
    lab_label = label_of(a.cell)
    res = {"rule": "D079 Gate C stress (KILL if exec mean R <= 0)", "edge_root": str(root), "winner": lab_label,
           "spread_factor": f, "cost_model": str(cm), "era_scale": a.era_scale or None, "returncode": rc, "log": str(log)}
    if rc != 0 or not new:
        res["verdict"] = "ERROR"
        out.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(json.dumps(res), flush=True)
        return 2
    s = pd.read_csv(new[-1])
    r = s[s.label == lab_label].iloc[0]
    base = pd.read_csv(art / f"v3_execution_trades_{lab_label}_{a.base_stamp}.csv")
    base = base[base.status == "resolved"]
    spread_r = (base.entry_spread / base.sl_dist).mean()
    res.update(stress_stamp=new[-1].stem.split("_")[-1], stress_exec_mean_R=float(r.exec_mean_R),
               stress_coverage=float(r.coverage_rate), base_exec_mean_R=float(base.exec_r.mean()),
               base_mean_spread_R=float(spread_r), break_even_spread_multiple_approx=float(1.0 + base.exec_r.mean() / spread_r),
               verdict="KILL" if r.exec_mean_R <= 0 else "PASS")
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res), flush=True)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
