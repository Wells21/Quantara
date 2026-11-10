"""Gate C batch runner (RUNBOOK v5.3, Section 6A STEP 7).

Takes EVERY Gate-B-positive cell (net_mean_R > 0) from ONE fresh Gate A grid of the edge (never pooled older grids),
replays them through v3_horizon_lab in broker cost-model mode (spread + swap per rollover + commission, 5700 ms entry delay,
bootstrap 2000, --save), and appends one record per batch -- including the run stamp -- to a jsonl. That jsonl is the input
of shared/research/gate_c_pbo.py --jsonl, so PBO only ever sees files produced by this run.

Moved into the repo 2026-10-22 from the session scratchpad (gatec_rerun.py, used for every Gate C lane 2026-10-14..10-17).
Same cell selection, batching, control handling and record format; the retired spread-level / re-score modes were removed.

usage:
  python shared/research/gate_c_runner.py --edge-root assets/<ASSET>/edges/<edge> --grid-stamp <Gate A stamp> \
      --cost-model portfolio_allocator/config/broker_profiles/moneta_cost_models/<ASSET>.json --out-jsonl <file> [--batch-size 120]
  python shared/research/gate_c_pbo.py --jsonl <file> <edge>
Resumable: batches already recorded in --out-jsonl are skipped. Run edges of one asset serially (shared tick store).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
LAB = V2_ROOT / "shared" / "research" / "v3_horizon_lab.py"
ENTRY_DELAY_MS = "5700"     # measured live bar-close -> fill median (RUNBOOK v5)
BOOTSTRAP_REPS = "2000"


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def fmt(x) -> str:
    return f"{float(x):g}"


def gate_b_cells(grid: pd.DataFrame, all_cells: bool = False) -> list[str]:
    """All net_mean_R > 0 cells (or every grid cell with all_cells, AFML s.11.6 N trials, D051) as H:EWM:Hv:PT:SL
    strings, de-duplicated, grid order."""
    pos = grid if all_cells else grid[grid["net_mean_R"] > 0]
    cells = [f"{fmt(r.event_vol_lookback)}:{int(r.event_vol_ewm_span)}:{fmt(r.vertical_horizon)}:{fmt(r.pt)}:{fmt(r.sl)}"
             for r in pos.itertuples()]
    return list(dict.fromkeys(cells))


def cell_label(cell: str) -> str:
    h, e, v, pt, sl = cell.split(":")
    return f"H{h}_E{e}_V{v}_PT{pt}_SL{sl}"


def lab_command(python: str, edge_root: Path, batch: list[str], cost_model: Path, entry_delay_ms: str, first: bool,
                start: str = "", era_scale: str = "", workers: int = 1) -> list[str]:
    cmd = [python, str(LAB), "--edge-root", str(edge_root), "--execution-finalists", ",".join(batch), "--save",
           "--execution-entry-delay-ms", entry_delay_ms, "--execution-bootstrap-reps", BOOTSTRAP_REPS,
           "--execution-spread-model", "broker", "--execution-cost-model", str(cost_model)]
    if not first:
        cmd.append("--execution-no-control")   # the control cell is replayed once, in batch 0
    if workers > 1:
        cmd += ["--execution-workers", str(workers)]   # 2026-11-09 Gate C speed: identical rows, parallel chunks
    if era_scale:
        cmd += ["--execution-era-scale", era_scale]   # STANDING 2026-11-09: era-scaled broker spread (full history)
    if start:
        cmd += ["--execution-start", start]    # history extension 2026-11-09: Gate C on the broker-cost period
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gate C batch runner (broker cost model)")
    ap.add_argument("--edge-root", required=True)
    ap.add_argument("--grid-stamp", required=True, help="stamp of THIS run's v3_hybrid_fast_grid_<stamp>.csv (Gate A)")
    ap.add_argument("--cost-model", required=True, help="broker cost model json, e.g. .../moneta_cost_models/<ASSET>.json")
    ap.add_argument("--out-jsonl", required=True)
    ap.add_argument("--batch-size", type=int, default=0, help="cells per lab call (0 = all in one call)")
    ap.add_argument("--entry-delay-ms", default=ENTRY_DELAY_MS)
    ap.add_argument("--python", default=sys.executable, help="interpreter for v3_horizon_lab (venv312)")
    ap.add_argument("--dry-run", action="store_true", help="print the selected cells and lab commands only")
    ap.add_argument("--all-cells", action="store_true",
                    help="replay every grid cell, not only Gate-B-positive ones (AFML s.11.6 all N trials, D051)")
    ap.add_argument("--skip-replayed-stamps", default="",
                    help="comma-separated Gate C summary stamps of THIS grid whose cells are already replayed (skipped; "
                         "their control too)")
    ap.add_argument("--start", default="",
                    help="replay only events at or after this UTC date (history extension, owner 2026-11-09: 2022-01-01)")
    ap.add_argument("--workers", type=int, default=1, help="parallel replay processes per lab call (identical rows)")
    ap.add_argument("--era-scale", default="",
                    help="era_scale/<ASSET>.json (STANDING 2026-11-09: full-history Gate C uses the era-scaled spread)")
    a = ap.parse_args(argv)

    edge_root = Path(a.edge_root)
    if not edge_root.is_absolute():
        edge_root = V2_ROOT / edge_root
    asset, edge = edge_root.parent.parent.name, edge_root.name
    art = edge_root / "artifacts" / "v3_horizon_lab"
    cost_model = Path(a.cost_model)
    if not cost_model.is_absolute():
        cost_model = V2_ROOT / cost_model
    if not cost_model.is_file():
        raise SystemExit(f"cost model not found: {cost_model}")
    grid = pd.read_csv(art / f"v3_hybrid_fast_grid_{a.grid_stamp}.csv")
    cells = gate_b_cells(grid, all_cells=a.all_cells)
    skip_stamps = [x.strip() for x in a.skip_replayed_stamps.split(",") if x.strip()]
    if skip_stamps:
        done_labels = set()
        for st in skip_stamps:
            done_labels |= set(pd.read_csv(art / f"v3_execution_velocity_summary_{st}.csv")["label"].astype(str))
        cells = [c for c in cells if cell_label(c) not in done_labels]
    bsz = a.batch_size if a.batch_size > 0 else max(len(cells), 1)
    batches = [cells[i:i + bsz] for i in range(0, len(cells), bsz)]
    log(f"EDGE {asset}/{edge}: grid={a.grid_stamp} rows={len(grid)} gateB_cost={sorted(grid['cost_bps'].unique())} "
        f"gateB_positive={len(cells)} batches={len(batches)}")
    if not cells:
        log("KILLED_GATE_B: no net_mean_R > 0 cell to replay")
        return 0
    out_jsonl = Path(a.out_jsonl)
    done = set()
    if out_jsonl.exists():
        for line in out_jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["edge"], r["batch"]))
    for bi, batch in enumerate(batches):
        cmd = lab_command(a.python, edge_root, batch, cost_model, a.entry_delay_ms, first=(bi == 0 and not skip_stamps),
                          start=a.start, era_scale=a.era_scale, workers=a.workers)
        if a.dry_run:
            print(f"batch {bi}: {len(batch)} cells"); print("  " + " ".join(cmd[:4]) + " <finalists> " + " ".join(cmd[5:]))
            continue
        if (edge, bi) in done:
            log(f"  skip batch {bi} (done)")
            continue
        before = set(p.name for p in art.iterdir())
        t0 = time.time()
        log(f"  batch {bi}: {len(batch)} cells -> lab")
        p = subprocess.run(cmd, cwd=str(V2_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        raw = art.parent / f"gate_c_runner_{edge}_b{bi}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
        raw.write_text(p.stdout + "\n----STDERR----\n" + p.stderr, encoding="utf-8")
        new = sorted(set(p2.name for p2 in art.iterdir()) - before)
        summ = [n for n in new if n.startswith("v3_execution_velocity_summary_")]
        audit = [n for n in new if n.startswith("v3_execution_velocity_audit_")]
        rec = {"asset": asset, "edge": edge, "batch": bi, "grid_stamp": a.grid_stamp, "n_cells": len(batch),
               "returncode": p.returncode, "secs": round(time.time() - t0, 1), "raw_log": str(raw),
               "cost_model": str(cost_model), "entry_delay_ms": a.entry_delay_ms, "execution_start": a.start or None, "era_scale": a.era_scale or None,
               "summary_files": summ, "audit_files": audit,
               "stamp": summ[0][len("v3_execution_velocity_summary_"):-4] if summ else None}
        if audit:
            ad = json.loads((art / audit[0]).read_text(encoding="utf-8"))
            rec["audit_extra_cost_bps"] = ad.get("extra_cost_bps")
            s = pd.DataFrame(ad["summary"])
            ctrl = s["is_control"].fillna(False).astype(bool) if "is_control" in s.columns else pd.Series(False, index=s.index)
            nc = s[~ctrl]
            rec["n_exec_positive_cov99"] = int(((nc["exec_mean_R"] > 0) & (nc["coverage_rate"] >= .99)).sum())
            rec["best_exec_mean_R"] = float(nc["exec_mean_R"].max())
            rec["best_label"] = str(nc.loc[nc["exec_mean_R"].idxmax(), "label"])
        with out_jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        log(f"  batch {bi} rc={p.returncode} secs={rec['secs']} pos={rec.get('n_exec_positive_cov99')} "
            f"best={rec.get('best_label')} {rec.get('best_exec_mean_R')}")
        if p.returncode != 0:
            log("  LAB_FAILED tail: " + p.stderr[-1500:].replace("\n", " | "))
            return p.returncode
    log(f"EDGE_DONE {edge}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
