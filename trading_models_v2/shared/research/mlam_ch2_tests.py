"""MLAM Ch.2 tests (logs/mlam_ch2_tests_20261102/PREREGISTRATION.md).

t1 EDGE_ROOT OUT   E[K] / DSR on the edge's all-trials record with the trial correlation raw (V0), denoised (V1) and
                   denoised + detoned when the market-component test passes (V2)
t2 OUT             KDE bandwidth sensitivity of nFacts / lambda+ (Step 07 core matrix + every T1 trial matrix)

usage (from trading_models_v2): python shared/research/mlam_ch2_tests.py t1 assets/HK/edges/drawdownhazardcontinuation_short logs/mlam_ch2_tests_20261102
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from shared.backtest_statistics.afml_ch14_dsr import cluster_trial_variance, dsr_report  # noqa: E402
from shared.models.denoise_onc import (  # noqa: E402
    cluster_kmeans_top, denoised_corr_constant_residual_eigenvalue, detone_corr, find_max_eval, get_pca)
from shared.research.gate_c_pbo import build_performance_matrix, load_run_summary  # noqa: E402

ONC_SEED = 0
BWIDTH = 0.01
MARKET_BOOTSTRAPS = 1000
MARKET_SEED = 0
MARKET_MIN_SHARE = 0.95


def trial_matrix(edge_root: Path) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    rec = json.loads((edge_root / "artifacts" / "feature_importance_audit" / "ch14_dsr_all_trials.json").read_text(encoding="utf-8"))
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, rec["stamps"])
    perf, _ = build_performance_matrix(art, summ)
    perf = perf.loc[:, perf.std(ddof=1) > 0]
    return perf, rec["selected_trial"], rec


def mp_fit(corr: np.ndarray, n_obs: int, bwidth: float = BWIDTH) -> dict[str, Any]:
    e_val, e_vec = get_pca(corr)
    raw = np.diag(e_val)
    q = n_obs / corr.shape[0]
    e_max, var = find_max_eval(raw, q, bwidth)
    n_facts = int(raw.shape[0] - raw[::-1].searchsorted(e_max))   # Snippet 2.4, no floor
    return {"e_val": e_val, "e_vec": e_vec, "raw": raw, "q": q, "lambda_plus": float(e_max), "var": float(var),
            "n_facts": n_facts}


def same_sign_share(x: np.ndarray) -> float:
    """Step 07's market test (iid row bootstrap, first eigenvector all one sign), looped to bound memory."""
    rng = np.random.default_rng(MARKET_SEED)
    hits = 0
    for _ in range(MARKET_BOOTSTRAPS):
        s = x[rng.integers(0, x.shape[0], size=x.shape[0])]
        sd = s.std(axis=0)
        if (sd == 0).any():
            continue
        c = np.corrcoef(s, rowvar=False)
        v = np.linalg.eigh(c)[1][:, -1]
        hits += bool(np.all(v > 0) or np.all(v < 0))
    return hits / MARKET_BOOTSTRAPS


def variant(perf: pd.DataFrame, corr: pd.DataFrame, selected: str) -> dict[str, Any]:
    t0 = time.time()
    _, clusters, _ = cluster_kmeans_top(corr, n_init=10, random_state=ONC_SEED)
    tv = cluster_trial_variance(perf, clusters)
    rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"])
    return {"E_K": tv["E_K"], "cluster_sizes": sorted((len(m) for m in clusters.values()), reverse=True),
            "V_annualised_sr": tv["V_annualised_sr"], "dsr": rep["dsr"], "dsr_pass_0p95": rep["dsr_pass_0p95"],
            "fwer_alpha_K": rep["fwer"]["alpha_K"], "fwer_pass_0p05": rep["fwer"]["pass_0p05"],
            "selected_in_cluster_of_size": next(len(m) for m in clusters.values() if selected in m),
            "onc_seconds": round(time.time() - t0, 1)}


def t1(edge_root: Path, out: Path) -> None:
    perf, selected, rec = trial_matrix(edge_root)
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    corr = perf.corr()
    res: dict[str, Any] = {"edge": name, "stamps": rec["stamps"], "n_trials": int(perf.shape[1]), "T": int(perf.shape[0]),
                           "stored": {"E_K": rec["E_K_onc"], "dsr": rec["dsr"]}}
    res["V0_raw"] = variant(perf, corr, selected)
    res["V0_reproduces_stored"] = bool(res["V0_raw"]["E_K"] == rec["E_K_onc"] and abs(res["V0_raw"]["dsr"] - rec["dsr"]) < 1e-9)
    print(name, "V0", res["V0_raw"], "reproduces", res["V0_reproduces_stored"], flush=True)
    fit = mp_fit(corr.to_numpy(), len(perf))
    den = denoised_corr_constant_residual_eigenvalue(fit["e_val"], fit["e_vec"], fit["n_facts"])
    res["mp_fit"] = {k: fit[k] for k in ("q", "lambda_plus", "var", "n_facts")}
    res["mp_fit"]["top_eigenvalues"] = [round(float(v), 3) for v in fit["raw"][:5]]
    res["V1_denoised"] = variant(perf, pd.DataFrame(den, index=corr.index, columns=corr.columns), selected)
    print(name, "V1", res["V1_denoised"], flush=True)
    share = same_sign_share(perf.to_numpy(dtype="float64"))
    market = bool(fit["raw"][0] > fit["lambda_plus"] and share >= MARKET_MIN_SHARE)
    res["market_test"] = {"first_eval_above_lambda_plus": bool(fit["raw"][0] > fit["lambda_plus"]),
                          "same_sign_share": share, "market_component": market}
    if market and fit["n_facts"] >= 1:
        d_val, d_vec = get_pca(den)
        dt = detone_corr(den, d_val, d_vec, 1)
        res["V2_denoised_detoned"] = variant(perf, pd.DataFrame(dt, index=corr.index, columns=corr.columns), selected)
    else:
        res["V2_denoised_detoned"] = "not applied (no market component by the Step 07 test)"
    print(name, "V2", res["V2_denoised_detoned"], flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"t1_{name}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def cv_bandwidth(e_val: np.ndarray) -> float:
    from sklearn.model_selection import GridSearchCV
    from sklearn.neighbors import KernelDensity
    grid = GridSearchCV(KernelDensity(kernel="gaussian"), {"bandwidth": np.logspace(-3, 0, 31)}, cv=5)
    grid.fit(np.asarray(e_val).reshape(-1, 1))
    return float(grid.best_params_["bandwidth"])


def t2(out: Path) -> None:
    mats = {}
    oa = V2 / "portfolio_allocator" / "outputs"
    core = pd.read_csv(oa / "return_streams" / "candidate_return_streams_corr_core_afml_weighted_r.csv", index_col=0)
    diag = json.loads((oa / "hrp_linkage_robustness_outputs"
                       / "hrp_denoise_diagnostics_afml_weighted_r_core_compound_velocity_single.json").read_text(encoding="utf-8"))
    q = None
    stack = [diag]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            if "q_n_over_t" in o:
                q = float(o["q_n_over_t"]); break
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    mats["step07_core"] = (core.to_numpy(dtype="float64"), int(round(q * core.shape[0])))
    for f in sorted(V2.glob("assets/*/edges/*/artifacts/feature_importance_audit/ch14_dsr_all_trials.json")):
        er = f.parents[2]
        perf, _, _ = trial_matrix(er)
        mats[f"trials_{er.parent.parent.name}_{er.name}"] = (perf.corr().to_numpy(), len(perf))
    res = {}
    for k, (c, n) in mats.items():
        ev = np.sort(np.linalg.eigvalsh(c))[::-1]
        row = {"N": int(c.shape[0]), "T": int(n)}
        for bw in (0.005, 0.01, 0.02, 0.05, 0.1):
            f = mp_fit(c, n, bw)
            row[f"bw_{bw}"] = {"n_facts": f["n_facts"], "lambda_plus": round(f["lambda_plus"], 4), "var": round(f["var"], 5)}
        bw = cv_bandwidth(ev)
        f = mp_fit(c, n, bw)
        row["cv"] = {"bandwidth": bw, "n_facts": f["n_facts"], "lambda_plus": round(f["lambda_plus"], 4), "var": round(f["var"], 5)}
        row["same_nfacts_0p01_vs_cv"] = row["bw_0.01"]["n_facts"] == row["cv"]["n_facts"]
        res[k] = row
        print(k, json.dumps(row), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "t2_bandwidth.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1] == "t1":
        er = Path(sys.argv[2])
        t1(er if er.is_absolute() else V2 / er, Path(sys.argv[3]))
    else:
        t2(Path(sys.argv[2]))
