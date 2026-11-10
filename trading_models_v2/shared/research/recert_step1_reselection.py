"""RECERT step 1 (runbook Section 16C, D074): Gate A-C re-selection on the 405-cell grid (EWM 100,150,200).

The 2026-10-31 EWM span test (logs/ewm_span_test_20261031) already ran Gate A and a Gate C replay of EVERY cell over EWM
100,150,200,300 (540 cells) on these edges, with the same code, bars and cost models as today. Owner rule 2026-11-03: a
test already run is not repeated. This reads those runs under the runbook's Step 7 rules:
  grid      = the runbook's 405 cells (lookbacks 1,2,4,6,8 h x EWM 100,150,200 x verticals x 1:1,1.5:1,2:1) + control
  Gate B    = cells with Gate A net_mean_R > 0 (Gate C replayed every cell, so the Gate B filter is applied here)
  winner    = argmax compound velocity among Gate B cells with exec_mean_R > 0 and coverage >= 0.99 (gate_c_pbo rule)
  PBO       = CSCV over every replayed configuration of the grid, control included (D051); also over all 540 replayed
              cells of the run (the strict 'every configuration replayed in the run' reading)
Stress and the four-place lock are separate steps.

usage (from trading_models_v2): python shared/research/recert_step1_reselection.py OUT.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from shared.research.gate_c_pbo import (MIN_COVERAGE, build_performance_matrix, load_run_summary,  # noqa: E402
                                        pbo_from_matrix)
import re  # noqa: E402

LOG = V2 / "logs" / "ewm_span_test_20261031"
EDGES = ["GBPJPY/dailyrangecompletionfade_long", "HK/drawdownhazardcontinuation_short", "JPN/rangestructure_long",
         "XAUUSD/rangestructure_long"]
SPANS = (100, 150, 200)
KEY = ["event_vol_lookback", "event_vol_ewm_span", "vertical_horizon", "pt", "sl"]


def _pbo(art: Path, cells: pd.DataFrame) -> dict:
    perf, _ = build_performance_matrix(art, cells[["label", "run_stamp"]])
    p = pbo_from_matrix(perf)
    return {"n_trials": int(perf.shape[1]), "pbo": round(float(p["pbo"]), 4),
            "prob_oos_nonpositive": round(float(p["prob_oos_winner_nonpositive"]), 4),
            "degradation_slope": round(float(p["performance_degradation_slope"]), 4),
            "pass": bool(p["pbo"] < 0.5 and p["prob_oos_winner_nonpositive"] < 0.5)}


def analyse(edge: str) -> dict:
    asset, name = edge.split("/")
    root = V2 / "assets" / asset / "edges" / name
    art = root / "artifacts" / "v3_horizon_lab"
    recs = [json.loads(x) for x in (LOG / f"{asset}_{name}_ewm.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    stamps, grid = [r["stamp"] for r in recs], recs[0]["grid_stamp"]
    summ = load_run_summary(art, stamps)
    ctrl = summ["is_control"].fillna(False).astype(bool)
    ga = pd.read_csv(art / f"v3_hybrid_fast_grid_{grid}.csv")
    for c in KEY:
        ga[c] = ga[c].astype(float)
        summ[c] = summ[c].astype(float)
    nc = summ[~ctrl].merge(ga[KEY + ["net_mean_R", "gross_mean_R"]], on=KEY, how="left")
    g405 = nc[nc.event_vol_ewm_span.isin(SPANS)]
    gate_b = g405[g405.net_mean_R > 0]
    elig = gate_b[(gate_b.exec_mean_R > 0) & (gate_b.coverage_rate >= MIN_COVERAGE)]
    _, scores = build_performance_matrix(art, elig[["label", "run_stamp"]])
    win = max(scores, key=scores.get) if scores else None
    elig_nofilter = g405[(g405.exec_mean_R > 0) & (g405.coverage_rate >= MIN_COVERAGE)]
    _, scores_nf = build_performance_matrix(art, elig_nofilter[["label", "run_stamp"]])
    win_nf = max(scores_nf, key=scores_nf.get) if scores_nf else None
    src = (root / "candidate_events.py").read_text(encoding="utf-8") + (root / "model_rows.py").read_text(encoding="utf-8")
    geo = {k: (m.group(1) if (m := re.search(rf"^{k}\s*(?::[^=]*)?=\s*([0-9.]+)", src, re.M)) else None)
           for k in ("EVENT_VOL_LOOKBACK_HOURS", "EVENT_VOL_EWM_SPAN", "VERTICAL_HORIZON_HOURS", "PT_MULTIPLIER", "SL_MULTIPLIER")}
    out = {"run_stamps": stamps, "gate_a_grid_stamp": grid, "gate_a_cost_bps": float(ga.cost_bps.iloc[0]),
           "gate_a_best_gross_mean_R_405": round(float(g405.gross_mean_R.max()), 4),
           "gate_a_kill_rule_pass": bool(g405.gross_mean_R.max() >= 0.05),
           "cells_405": int(len(g405)), "gate_b_cells": int(len(gate_b)), "gate_c_eligible": int(len(elig)),
           "current_locked_geometry": geo,
           "winner": None, "winner_without_gate_b_filter": win_nf}
    if win:
        w = elig[elig.label == win].iloc[0]
        out["winner"] = {"label": win, "cv_score": scores[win], "exec_mean_R": float(w.exec_mean_R),
                         "ci95": [float(w.weekly_cluster_bootstrap_mean_R_95_lo), float(w.weekly_cluster_bootstrap_mean_R_95_hi)],
                         "resolved_trades": int(w.resolved_trades), "coverage": float(w.coverage_rate),
                         "gate_a_net_mean_R": float(w.net_mean_R), "R_per_week": float(w.calendar_exec_R_per_week),
                         "mean_hold_h": float(w.mean_hold_hours), "run_stamp": w.run_stamp,
                         **{k: float(w[k]) for k in KEY}}
    grid_cells = pd.concat([summ[ctrl], summ[~ctrl][summ[~ctrl].event_vol_ewm_span.isin(SPANS)]])
    out["pbo_grid_405_plus_control"] = _pbo(art, grid_cells)
    out["pbo_all_replayed_540_plus_control"] = _pbo(art, summ)
    print(edge, json.dumps({k: out[k] for k in ("winner", "winner_without_gate_b_filter", "pbo_grid_405_plus_control",
                                                 "pbo_all_replayed_540_plus_control")}, default=str), flush=True)
    return out


if __name__ == "__main__":
    res = {e: analyse(e) for e in EDGES}
    Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)
    Path(sys.argv[1]).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
