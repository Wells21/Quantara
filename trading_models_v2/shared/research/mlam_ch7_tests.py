"""MLAM Ch.7 tests (docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4ZC, logs/mlam_ch7_tests_20261103/PREREGISTRATION.md).

t1a OUT                 condition numbers of Snippets 7.1-7.2 and the s.7.5 text (exact)
sim DGP CHUNK K OUT     Monte Carlo chunk (sims i with i % K == CHUNK), MLAM Snippets 7.7-7.9:
                        DGP book   = 10 blocks x 50, rho .5 (Snippet 7.7): T1b (Figures 7.2 / 7.3) + Ex.1, Ex.3, Ex.5
                        DGP rnd    = getRndCov(500, 10) (Snippet 2.3), no block structure: Ex.2
                        DGP book10 = 2 blocks x 5, DGP book100 = 10 blocks x 10: Ex.4
merge OUT               RMSE per DGP / method from the chunk files

Synthetic runs need venv312 (cvxpy for the long-only CLA of Ex.3). T3 (our inputs) is mlam_ch7_ours.py on venv39.
usage (from trading_models_v2): ..\\venv312\\Scripts\\python.exe shared/research/mlam_ch7_tests.py sim book 0 6 logs/mlam_ch7_tests_20261103
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from scipy.linalg import block_diag
from sklearn.covariance import LedoitWolf

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from shared.models.denoise_onc import (  # noqa: E402
    cluster_kmeans_base, corr_to_cov, cov_to_corr, denoised_corr_constant_residual_eigenvalue, detone_corr,
    find_max_eval, get_pca)

N_OBS = 1000                                   # Snippet 7.8 nObs
N_SIMS = {"book": 100, "rnd": 100, "book10": 1000, "book100": 1000}   # pre-registered (book: 1,000; cost, see PREREG)
DGP = {"book": (10, 50, .5), "book10": (2, 5, .5), "book100": (10, 10, .5)}
BWIDTH = .01                                   # Snippet 7.3


# --------------------------------------------------------------------------- MLAM Section 2 / 7 functions (as printed)

def form_block_matrix(n_blocks: int, b_size: int, b_corr: float) -> np.ndarray:
    """Snippet 2.7 formBlockMatrix."""
    block = np.ones((b_size, b_size)) * b_corr
    block[range(b_size), range(b_size)] = 1
    return block_diag(*([block] * n_blocks))


def form_true_matrix(n_blocks: int, b_size: int, b_corr: float) -> tuple[np.ndarray, np.ndarray]:
    """Snippet 2.7 formTrueMatrix (global np.random, as the book)."""
    corr0 = pd.DataFrame(form_block_matrix(n_blocks, b_size, b_corr))
    cols = corr0.columns.tolist()
    np.random.shuffle(cols)
    corr0 = corr0[cols].loc[cols].copy(deep=True)
    std0 = np.random.uniform(.05, .2, corr0.shape[0])
    cov0 = corr_to_cov(corr0.to_numpy(), std0)
    mu0 = np.random.normal(std0, std0, cov0.shape[0]).reshape(-1, 1)
    return mu0, cov0


def get_rnd_cov(n_cols: int, n_facts: int) -> np.ndarray:
    """Snippet 2.3 getRndCov."""
    w = np.random.normal(size=(n_cols, n_facts))
    cov = np.dot(w, w.T)
    cov += np.diag(np.random.uniform(size=n_cols))
    return cov


def opt_port(cov: np.ndarray, mu: np.ndarray | None = None) -> np.ndarray:
    """Snippet 2.10 optPort (flattened)."""
    inv = np.linalg.inv(cov)
    ones = np.ones(inv.shape[0])
    w = inv @ (ones if mu is None else np.asarray(mu, dtype=float).ravel())
    return w / (ones @ w)


def cla_long_only(cov: np.ndarray, mu: np.ndarray | None = None) -> np.ndarray:
    """Ex.3: the long-only (0 <= w <= 1, sum 1) CLA solutions, solved as the equivalent QPs: minimum variance = CLA's
    last turning point; maximum Sharpe = min w'Vw s.t. mu'w = 1, w >= 0, rescaled to sum 1 (needs some mu > 0, else
    the long-only min-variance is returned and counted)."""
    import cvxpy as cp
    n = cov.shape[0]
    if n == 1:
        return np.ones(1)
    v = (cov + cov.T) / 2
    w = cp.Variable(n)
    if mu is None or np.max(np.asarray(mu).ravel()) <= 0:
        prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(v))), [cp.sum(w) == 1, w >= 0])
    else:
        prob = cp.Problem(cp.Minimize(cp.quad_form(w, cp.psd_wrap(v))), [np.asarray(mu).ravel() @ w == 1, w >= 0])
    prob.solve(solver="CLARABEL")
    out = np.clip(np.asarray(w.value).ravel(), 0, None)
    return out / out.sum()


def deNoiseCov(cov0: np.ndarray, q: float, bwidth: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Snippet 2.9 deNoiseCov (+ the eigen-system of the denoised corr, for the Ex.1 detone)."""
    corr0 = cov_to_corr(cov0)
    e_val0, e_vec0 = get_pca(corr0)
    e_max0, _ = find_max_eval(np.diag(e_val0), q, bwidth)
    n_facts0 = e_val0.shape[0] - np.diag(e_val0)[::-1].searchsorted(e_max0)
    corr1 = denoised_corr_constant_residual_eigenvalue(e_val0, e_vec0, n_facts0)
    return corr_to_cov(corr1, np.diag(cov0) ** .5), corr1, e_vec0, int(n_facts0)


def clusters_of(corr: np.ndarray, max_k: int, seed: int) -> dict:
    n = corr.shape[0]
    _, cl, _ = cluster_kmeans_base(pd.DataFrame(corr, index=range(n), columns=range(n)), max_num_clusters=max_k,
                                   n_init=10, random_state=seed)
    return cl


def nco(cov: np.ndarray, mu: np.ndarray | None, clusters: dict, alloc: Callable = opt_port) -> np.ndarray:
    """Snippet 7.6 optPort_nco with the clustering supplied and the allocator pluggable (s.7.6.2 / Ex.3)."""
    n = cov.shape[0]
    keys = sorted(clusters)
    w_intra = np.zeros((n, len(keys)))
    for j, k in enumerate(keys):
        it = list(clusters[k])
        w_intra[it, j] = alloc(cov[np.ix_(it, it)], None if mu is None else np.asarray(mu).ravel()[it])
    cov_ = w_intra.T @ cov @ w_intra
    mu_ = None if mu is None else w_intra.T @ np.asarray(mu).ravel()
    return w_intra @ alloc(cov_, mu_)


# --------------------------------------------------------------------------- experiments

def true_process(dgp: str) -> tuple[np.ndarray, np.ndarray]:
    np.random.seed(0)                                       # Snippet 7.7
    if dgp == "rnd":
        cov0 = get_rnd_cov(500, 10)
        std0 = np.sqrt(np.diag(cov0))
        mu0 = np.random.normal(std0, std0, cov0.shape[0]).reshape(-1, 1)   # formTrueMatrix's mu rule
        return mu0, cov0
    return form_true_matrix(*DGP[dgp])


def t1a(out: Path) -> None:
    def cn(c):
        e = np.linalg.eigvalsh(c)
        return float(e.max() / e.min())
    res = {"snippet_7_1_2x2_rho_.5": cn(form_block_matrix(2, 2, .5)),
           "blocks_3_and_1_rho_.5": cn(block_diag(form_block_matrix(1, 3, .5), form_block_matrix(1, 1, .5))),
           "2x2_rho_.75": cn(form_block_matrix(2, 2, .75)),
           "500x500_2_blocks_rho_.5": cn(form_block_matrix(2, 250, .5)),
           "snippet_7_2_dominant_block": cn(block_diag(form_block_matrix(1, 2, .5), form_block_matrix(1, 2, .0)))}
    res["book"] = {"snippet_7_1_2x2_rho_.5": 3, "blocks_3_and_1_rho_.5": 4, "2x2_rho_.75": 7,
                   "500x500_2_blocks_rho_.5": 251, "snippet_7_2_dominant_block": 3}
    res["pass"] = all(abs(res[k] - v) < 1e-9 for k, v in res["book"].items())
    print(res, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "t1a_condition_numbers.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


def sim(dgp: str, chunk: int, k: int, out: Path) -> None:
    t = time.time()
    mu0, cov0 = true_process(dgp)
    n = cov0.shape[0]
    full = dgp == "book"
    w0 = {"mv": opt_port(cov0), "ms": opt_port(cov0, mu0)}
    w0_lo = {"mv": cla_long_only(cov0), "ms": cla_long_only(cov0, mu0)} if full else {}
    sse: dict[str, float] = {}
    meta: dict[str, list] = {"n_clusters_raw": [], "n_clusters_shrunk": [], "n_clusters_dn": [], "n_clusters_dndt": [],
                             "n_facts_dn": [], "sims": []}

    def add(name: str, w: np.ndarray, ref: np.ndarray) -> None:
        sse[name] = sse.get(name, 0.0) + float(np.sum((w - ref) ** 2))

    np.random.seed(0)                                       # Snippet 7.8
    for i in range(N_SIMS[dgp]):
        x = np.random.multivariate_normal(mu0.flatten(), cov0, size=N_OBS)   # simCovMu's draw
        if i % k != chunk:
            continue
        mu1 = x.mean(axis=0)
        cov_raw = np.cov(x, rowvar=0)
        cl = clusters_of(cov_to_corr(cov_raw), n // 2, seed=i)
        meta["sims"].append(i)
        meta["n_clusters_raw"].append(len(cl))
        for pt, mu in (("mv", None), ("ms", mu1)):
            add(f"markowitz_raw_{pt}", opt_port(cov_raw, mu), w0[pt])
            add(f"nco_raw_{pt}", nco(cov_raw, mu, cl), w0[pt])
        if not full:
            continue
        cov_lw = LedoitWolf().fit(x).covariance_
        cl_lw = clusters_of(cov_to_corr(cov_lw), n // 2, seed=i)
        meta["n_clusters_shrunk"].append(len(cl_lw))
        # Ex.1: Snippet 7.3 (denoise, bWidth .01) with and without a detone of the clustering matrix; Snippet 7.4 uses cov1
        cov_dn, corr_dn, _, nf = deNoiseCov(cov_raw, N_OBS / n, BWIDTH)
        e_val_dn, e_vec_dn = get_pca(corr_dn)
        corr_dt = detone_corr(corr_dn, e_val_dn, e_vec_dn, 1)
        cl_dn = clusters_of(corr_dn, n // 2, seed=i)
        cl_dt = clusters_of(corr_dt, n // 2, seed=i)
        meta["n_facts_dn"].append(nf)
        meta["n_clusters_dn"].append(len(cl_dn))
        meta["n_clusters_dndt"].append(len(cl_dt))
        cl2 = clusters_of(cov_to_corr(cov_raw), 2, seed=i)   # Ex.5
        for pt, mu in (("mv", None), ("ms", mu1)):
            add(f"markowitz_shrunk_{pt}", opt_port(cov_lw, mu), w0[pt])
            add(f"nco_shrunk_{pt}", nco(cov_lw, mu, cl_lw), w0[pt])
            add(f"markowitz_denoised_{pt}", opt_port(cov_dn, mu), w0[pt])
            add(f"nco_ex1_denoised_{pt}", nco(cov_dn, mu, cl_dn), w0[pt])
            add(f"nco_ex1_denoised_detoned_{pt}", nco(cov_dn, mu, cl_dt), w0[pt])
            add(f"nco_ex5_maxk2_{pt}", nco(cov_raw, mu, cl2), w0[pt])
            add(f"cla_lo_raw_{pt}", cla_long_only(cov_raw, mu), w0_lo[pt])        # Ex.3, vs the true long-only CLA
            add(f"nco_cla_lo_raw_{pt}", nco(cov_raw, mu, cl, cla_long_only), w0_lo[pt])
        print(dgp, chunk, i, round(time.time() - t, 1), len(cl), flush=True)
    res = {"dgp": dgp, "chunk": chunk, "k": k, "n_assets": n, "n_obs": N_OBS, "sse": sse, "n_weights_per_sim": n,
           "meta": meta, "true_weights_mv": w0["mv"].tolist(), "seconds": round(time.time() - t, 1)}
    (out / "chunks").mkdir(parents=True, exist_ok=True)
    (out / "chunks" / f"{dgp}_{chunk}of{k}.json").write_text(json.dumps(res), encoding="utf-8")


def merge(out: Path) -> None:
    rows: dict[str, dict] = {}
    for f in sorted((out / "chunks").glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        d = rows.setdefault(r["dgp"], {"sse": {}, "sims": [], "n": r["n_assets"], "meta": {}})
        for m, v in r["sse"].items():
            d["sse"][m] = d["sse"].get(m, 0.0) + v
        d["sims"] += r["meta"]["sims"]
        for m, v in r["meta"].items():
            if m != "sims":
                d["meta"].setdefault(m, []).extend(v)
    res = {}
    for dgp, d in rows.items():
        ns = len(set(d["sims"]))
        rmse = {m: (v / (ns * d["n"])) ** .5 for m, v in sorted(d["sse"].items())}
        res[dgp] = {"n_sims": ns, "expected_sims": N_SIMS[dgp], "rmse": rmse,
                    "meta_summary": {m: {"min": min(v), "median": float(np.median(v)), "max": max(v)}
                                     for m, v in d["meta"].items() if v}}
        print(dgp, ns, {m: f"{v:.3e}" for m, v in rmse.items()}, flush=True)
    (out / "sim_results.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "t1a":
        t1a(Path(sys.argv[2]))
    elif cmd == "sim":
        sim(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]))
    elif cmd == "merge":
        merge(Path(sys.argv[2]))
