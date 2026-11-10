"""AFML Ch.14 walkthrough, Test 1 Level 1 (owner-approved 2026-10-26; pre-registered in AFML_MLAM_WALKTHROUGH.md s.4N.1):
Deflated Sharpe ratio and familywise error over EVERY trial of an edge's Gate C run.

Book: AFML s.14.7.2-14.7.4 pp.203-206 (PSR, DSR, third law p.205, > 0.95 p.206); MLAM s.8.5-8.8 pp.108-123, which revises
AFML's N and V[{SR_n}] (owner rule: MLAM over AFML where it revises):
  trials     : every configuration replayed in the Gate C run (controls included), return series = the Gate C / PBO matrix
               (shared/research/gate_c_pbo.py: daily-prorated log(1 + 0.01 exec_r), calendar days, zero-filled);
  selected   : the Gate C winner (argmax compound velocity among eligible cells);
  E[K]       : ONC clusters on the trials' correlation matrix (MLAM s.8.7.1 p.114; clusterKMeansTop, Snippet 4.2), denoised
               and detoned when a market component exists (MLAM p.24, p.87; D076, owner 2026-11-02 -- afml_ch14_dsr.py);
  V[SR]      : min-variance cluster portfolios S_k (MLAM p.114), aSR_k = SR_k sqrt(Frequency_k), V = V[aSR_k] / Freq_sel
               (MLAM p.116);
  DSR        : PSR[SR*], SR* = sqrt(V) ((1 - g) Z^-1[1 - 1/K] + g Z^-1[1 - 1/(K e)]) (AFML p.204, MLAM p.109 Snippet 8.1);
  FWER       : alpha_K = 1 - (1 - alpha)^K, alpha = 1 - Z[z[0]] (MLAM s.8.8.2, Snippet 8.3);
  Type II    : beta = Z[z_alpha - theta], z_alpha = Z^-1[(1 - alpha_K)^(1/K)], beta_K = beta^K (MLAM s.8.8.3, Snippet 8.4).
Diagnostic only; pass marks reported, not applied.

usage (from trading_models_v2, venv312):
  python -m shared.research.dsr_all_trials --edge-root assets/HK/edges/<edge> --stamps S1,S2,... --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from shared.backtest_statistics.afml_ch14_dsr import cluster_trial_variance, dsr_report, frequency, moments, onc_trial_clusters
from shared.models.denoise_onc import cluster_kmeans_top
from shared.research.gate_c_pbo import MIN_COVERAGE, build_performance_matrix, load_run_summary

V2 = Path(__file__).resolve().parents[2]
ONC_SEED = 0


def run(edge_root: Path, stamps: list[str]) -> dict[str, Any]:
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, stamps)
    ctrl = summ["is_control"].fillna(False).astype(bool) if "is_control" in summ.columns else pd.Series(False, index=summ.index)
    elig = summ[~ctrl & (summ["exec_mean_R"] > 0) & (summ["coverage_rate"] >= MIN_COVERAGE)]
    perf, _ = build_performance_matrix(art, summ)            # all trials (D051 / third law)
    _, scores = build_performance_matrix(art, elig)          # Gate C winner rule
    winner = max(scores, key=scores.get)
    perf = perf.loc[:, perf.std(ddof=1) > 0]

    clusters, corr_diag = onc_trial_clusters(perf, seed=ONC_SEED)
    _, cl_raw, _ = cluster_kmeans_top(perf.corr(), n_init=10, random_state=ONC_SEED)   # pre-D076 E[K], diagnostic only

    tv = cluster_trial_variance(perf, clusters)
    for row in tv["clusters"]:
        row["has_selected"] = winner in row["members"]
    trial_sr = perf.apply(lambda c: moments(c.to_numpy())["sr"])
    median_ann = float(trial_sr.median() * np.sqrt(frequency(perf.index, len(perf))))
    rep = dsr_report(perf[winner].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"],
                     {"annualised_1p0": 1.0, "median_trial_sr": median_ann})  # MLAM exercise 8.5: SR_ = median trial
    return {
        "edge": f"{edge_root.parent.parent.name}/{edge_root.name}", "stamps": stamps,
        "n_trials": int(perf.shape[1]), "n_controls": int(ctrl.sum()), "matrix_days": int(perf.shape[0]),
        "selected_trial": winner,
        "selected_sr_rank_among_trials": int((trial_sr > trial_sr[winner]).sum() + 1),
        "trial_sr_median": float(trial_sr.median()), "trial_sr_max": float(trial_sr.max()),
        "E_K_onc": tv["E_K"], "trial_corr": corr_diag, "E_K_onc_raw_corr_diagnostic": len(cl_raw),
        "clusters": tv["clusters"], "cluster_weighting": tv["weighting"], "V_annualised_sr": tv["V_annualised_sr"],
        **{k: rep[k] for k in ("psr_vs_zero", "dsr", "dsr_pass_0p95", "fwer", "type2", "sr_star_annualised",
                               "sr_star_nonannualised", "V_sr_nonannualised")},
        "selected": {k: rep[k] for k in ("T", "sr", "skew", "kurt", "frequency", "annualised_sr")},
    }


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
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("edge", "n_trials", "selected_trial", "E_K_onc", "psr_vs_zero", "dsr", "fwer")}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
