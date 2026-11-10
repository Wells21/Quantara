"""AFML Ch.14 walkthrough, Test 1 Level 1 ROBUSTNESS DIAGNOSTIC (post-hoc: written after the s.4N.1 results were seen, 2026-10-26).

Why: in all four edges ONC returned E[K] = 2 and both min-variance cluster portfolios (MLAM p.114, pseudo-inverse on singular
covariances from duplicate trials) had strongly negative Sharpe ratios, so V[SR] is the variance of two unstable numbers.
Same trials, same ONC clusters (seed 0) as shared/research/dsr_all_trials.py; variants:
  A book       : min-variance cluster series (MLAM s.8.7.2) -- reproduces the main run
  B eq_weight  : equal-weight cluster series (diagnostic for the min-variance weights)
  C afml_V_K   : AFML s.14.7.3 literal V = variance across ALL trials' Sharpe ratios, N = E[K]
  D afml_V_N   : AFML literal V, N = every trial (no clustering; strictest)
plus min-variance weight extremes per cluster. Not a gate; reported to the owner.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from shared.backtest_statistics.afml_ch14_dsr import cluster_trial_variance, dsr_report, frequency, min_variance_weights, moments
from shared.models.denoise_onc import cluster_kmeans_top
from shared.research.gate_c_pbo import MIN_COVERAGE, build_performance_matrix, load_run_summary

V2 = Path(__file__).resolve().parents[2]


def run(edge_root: Path, stamps: list[str]) -> dict:
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, stamps)
    ctrl = summ["is_control"].fillna(False).astype(bool) if "is_control" in summ.columns else pd.Series(False, index=summ.index)
    elig = summ[~ctrl & (summ["exec_mean_R"] > 0) & (summ["coverage_rate"] >= MIN_COVERAGE)]
    perf, _ = build_performance_matrix(art, summ)
    _, scores = build_performance_matrix(art, elig)
    winner = max(scores, key=scores.get)
    perf = perf.loc[:, perf.std(ddof=1) > 0]
    _, clusters, _ = cluster_kmeans_top(perf.corr(), n_init=10, random_state=0)
    sel = perf[winner].to_numpy()
    out = {"edge": f"{edge_root.parent.parent.name}/{edge_root.name}", "selected_trial": winner, "E_K": len(clusters)}

    book = cluster_trial_variance(perf, clusters)
    out["A_book"] = {"V_annualised_sr": book["V_annualised_sr"], "cluster_asr": [c["annualised_sr"] for c in book["clusters"]],
                     **{k: v for k, v in dsr_report(sel, perf.index, len(clusters), book["V_annualised_sr"]).items() if k in ("dsr", "psr_vs_zero", "sr_star_annualised")}}
    out["A_book"]["alpha_K"] = dsr_report(sel, perf.index, len(clusters), book["V_annualised_sr"])["fwer"]["alpha_K"]
    w_stats = []
    for members in clusters.values():
        if len(members) > 1:
            w, singular = min_variance_weights(perf[list(members)].cov().to_numpy())
            w_stats.append({"n": len(members), "singular": singular, "w_min": float(w.min()), "w_max": float(w.max()),
                            "sum_negative_w": float(w[w < 0].sum())})
    out["A_book"]["min_variance_weights"] = w_stats

    eq = []
    for members in clusters.values():
        m = moments(perf[list(members)].mean(axis=1).to_numpy())
        eq.append(m["sr"] * np.sqrt(frequency(perf.index, m["T"])))
    v_eq = float(np.var(eq, ddof=1))
    r = dsr_report(sel, perf.index, len(clusters), v_eq)
    out["B_eq_weight"] = {"V_annualised_sr": v_eq, "cluster_asr": eq, "dsr": r["dsr"], "sr_star_annualised": r["sr_star_annualised"], "alpha_K": r["fwer"]["alpha_K"]}

    trial_asr = perf.apply(lambda c: moments(c.to_numpy())["sr"] * np.sqrt(frequency(perf.index, len(c))))
    v_all = float(np.var(trial_asr, ddof=1))
    for key, k in (("C_afml_V_K", len(clusters)), ("D_afml_V_N", perf.shape[1])):
        r = dsr_report(sel, perf.index, k, v_all)
        out[key] = {"N": k, "V_annualised_sr": v_all, "dsr": r["dsr"], "sr_star_annualised": r["sr_star_annualised"], "alpha_K": r["fwer"]["alpha_K"]}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-root", required=True)
    ap.add_argument("--stamps", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    er = Path(a.edge_root)
    er = er if er.is_absolute() else V2 / er
    res = run(er, [s.strip() for s in a.stamps.split(",") if s.strip()])
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("edge", "E_K")} | {v: res[v]["dsr"] for v in ("A_book", "B_eq_weight", "C_afml_V_K", "D_afml_V_N")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
