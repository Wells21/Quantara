"""MLAM Ch.5 T3 analysis (PREREGISTRATION T3): seasonal-barrier runs vs the 2026-10-31 baseline, same 405 cells."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from shared.research.gate_c_pbo import MIN_COVERAGE, build_performance_matrix, load_run_summary, pbo_from_matrix  # noqa: E402
from shared.research.mlam_ch5_tests import cramers_v  # noqa: E402

OUT = Path(__file__).resolve().parent
EDGES = {"rangestructure_long": "logs/ewm_span_test_20261031/XAUUSD_rangestructure_long_ewm.jsonl",
         "volatilitycarry_long": "logs/ewm_span_test_20261031/XAUUSD_volatilitycarry_long_ewm.jsonl"}


def stamps_of(jsonl: Path, grid: str | None = None) -> list[str]:
    recs = [json.loads(x) for x in jsonl.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r["stamp"] for r in recs if r["returncode"] == 0 and (grid is None or r["grid_stamp"] == grid)]


def hour_dependence(art: Path, label: str, stamp: str) -> dict:
    t = pd.read_csv(art / f"v3_execution_trades_{label}_{stamp}.csv", usecols=["status", "event_ts", "exit_reason", "exec_r"])
    t = t[t.status == "resolved"].copy()
    block = (pd.to_datetime(t.event_ts, utc=True).dt.hour // 4) * 4
    reason = t.exit_reason.astype(str).str.lower().map(lambda s: "pt" if "pt" in s or "tp" in s else ("sl" if "sl" in s else "vertical"))
    p, v = cramers_v(pd.crosstab(block, reason))
    per = pd.DataFrame({"block": block, "r": reason}).groupby("block")["r"].value_counts(normalize=True).unstack().fillna(0)
    return {"trades": int(len(t)), "p": p, "cramers_v": v, "per_block_share": per.round(3).to_dict("index")}


def evaluate(art: Path, stamps: list[str]) -> dict:
    s = load_run_summary(art, stamps)
    s = s[~s["is_control"].fillna(False).astype(bool)]
    s = s[s["event_vol_ewm_span"].isin([100, 150, 200])].drop_duplicates("label")
    elig = s[(s.exec_mean_R > 0) & (s.coverage_rate >= MIN_COVERAGE)]
    res = {"n_cells": int(len(s)), "n_exec_positive_cov99": int(len(elig))}
    if elig.empty:
        return res
    _, scores = build_performance_matrix(art, elig)
    w = max(scores, key=scores.get)
    row = elig[elig.label == w].iloc[0]
    res["winner"] = {"label": w, "compound_velocity": scores[w], "exec_mean_R": float(row.exec_mean_R),
                     "trades": int(row.resolved_trades), "R_per_week": float(row.calendar_exec_R_per_week),
                     "vertical_rate": float(row.vertical_rate), "pt_rate": float(row.pt_rate), "run_stamp": row.run_stamp}
    perf, _ = build_performance_matrix(art, s)
    p = pbo_from_matrix(perf)
    res.update({"pbo_matrix_shape": list(perf.shape), "pbo": p["pbo"], "prob_oos_nonpositive": p["prob_oos_winner_nonpositive"]})
    res["hour_dependence_winner"] = hour_dependence(art, w, row.run_stamp)
    return res


if __name__ == "__main__":
    out = {}
    for edge, base_jsonl in EDGES.items():
        art = V2 / "assets" / "XAUUSD" / "edges" / edge / "artifacts" / "v3_horizon_lab"
        base = evaluate(art, stamps_of(V2 / base_jsonl))
        seas = evaluate(art, stamps_of(OUT / f"t3_{edge}_gatec.jsonl"))
        # same-cell view: the baseline winner's cell under seasonal barriers
        same = None
        if "winner" in base:
            ss = load_run_summary(art, stamps_of(OUT / f"t3_{edge}_gatec.jsonl"))
            hit = ss[ss.label == base["winner"]["label"]]
            if len(hit):
                same = {"label": base["winner"]["label"], "exec_mean_R": float(hit.iloc[0].exec_mean_R),
                        "hour_dependence": hour_dependence(art, base["winner"]["label"], hit.iloc[0].run_stamp)}
        b, s = base.get("winner", {}), seas.get("winner", {})
        verdict = {
            "V_below_0p10": bool(s and seas["hour_dependence_winner"]["cramers_v"] < 0.10),
            "velocity_not_worse": bool(s and b and s["compound_velocity"] >= b["compound_velocity"]),
            "pbo_ok": bool(s and seas["pbo"] < 0.5 and seas["prob_oos_nonpositive"] < 0.5),
        }
        out[edge] = {"baseline": base, "seasonal": seas, "baseline_winner_cell_under_seasonal": same, "reading_rule": verdict}
        print(edge, json.dumps({"baseline": {k: base.get(k) for k in ("winner", "pbo", "prob_oos_nonpositive")},
                                "seasonal": {k: seas.get(k) for k in ("winner", "pbo", "prob_oos_nonpositive")},
                                "V_base": base.get("hour_dependence_winner", {}).get("cramers_v"),
                                "V_seas": seas.get("hour_dependence_winner", {}).get("cramers_v"),
                                "rule": verdict}, default=str, indent=1), flush=True)
    (OUT / "t3_results.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
