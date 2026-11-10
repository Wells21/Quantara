"""AFML Ch.11 walkthrough (owner-approved 2026-10-26): PBO / CSCV with EVERY configuration tried (AFML s.11.6 p.155:
M collects "the performance series from the N trials").

Re-uses the Gate C run files and the exact Gate C machinery (shared/research/gate_c_pbo.py: daily prorated log growth,
compound-velocity statistic per block, S = 16). Only the candidate set changes:
  recorded     : the Gate C rule (non-control cells with exec_mean_R > 0 and coverage >= 0.99) -- must reproduce the
                 recorded PBO (check);
  coverage_only: every replayed non-control cell with coverage >= 0.99 (drops the performance filter only);
  all_replayed : every replayed cell of the run, controls included (the book's N, primary).
Pass rule unchanged (project convention): PBO < 0.5 AND Prob[OOS <= 0] < 0.5.

usage (from trading_models_v2, venv312):
  python -m shared.research.pbo_all_trials --edge-root assets/JPN/edges/volatilitycarry_long --stamps S1,S2 --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd

from shared.research.gate_c_pbo import MIN_COVERAGE, build_performance_matrix, load_run_summary, pbo_from_matrix

V2 = Path(__file__).resolve().parents[2]


def run(edge_root: Path, stamps: list[str]) -> dict:
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, stamps)
    ctrl = summ["is_control"].fillna(False).astype(bool) if "is_control" in summ.columns else pd.Series(False, index=summ.index)
    sets = {
        "recorded": summ[~ctrl & (summ["exec_mean_R"] > 0) & (summ["coverage_rate"] >= MIN_COVERAGE)],
        "coverage_only": summ[~ctrl & (summ["coverage_rate"] >= MIN_COVERAGE)],
        "all_replayed": summ,
    }
    out = {"edge": f"{edge_root.parent.parent.name}/{edge_root.name}", "stamps": stamps, "n_replayed_cells": int(len(summ)),
           "n_controls": int(ctrl.sum())}
    for name, elig in sets.items():
        perf, scores = build_performance_matrix(art, elig)
        rec = {"n_candidates": int(perf.shape[1]), "matrix_days": int(perf.shape[0])}
        if perf.shape[1] >= 2:
            p = pbo_from_matrix(perf)
            rec.update({"pbo": p["pbo"], "prob_oos_nonpositive": p["prob_oos_winner_nonpositive"],
                        "degradation_slope": p["performance_degradation_slope"],
                        "winner_compound_velocity": max(scores, key=scores.get),
                        "PASS": bool(p["pbo"] < 0.5 and p["prob_oos_winner_nonpositive"] < 0.5)})
        out[name] = rec
        print(f"[{out['edge']}] {name}: {rec}", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-root", required=True)
    ap.add_argument("--stamps", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    t0 = time.time()
    er = Path(a.edge_root)
    er = er if er.is_absolute() else V2 / er
    res = run(er, [s.strip() for s in a.stamps.split(",") if s.strip()])
    res["elapsed_s"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
