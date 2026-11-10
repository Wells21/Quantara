"""MLAM Ch.7 T3 (logs/mlam_ch7_tests_20261103/PREREGISTRATION.md): NCO variants on our own inputs (footnote 24 p.101,
s.7.6.1 footnote 23 corr vs |corr|, s.7.6.2 / s.7.8 NCO as a wrapper around other allocators), min-variance only.

Inputs = the deployable edges' daily mark-to-market R (D059 alignment E, built exactly as ch16_allocation_tests).
M2 design of s.4P.1 (MLAM Snippets 7.8-7.9): 1,000 Gaussian draws of nObs in {260, T} from the full-sample (mu, V); RMSE vs
the true min-variance weights and vs each method's own weights on the true V.

venv39 (07's clean_corr needs pandas < 3): ..\\venv39\\Scripts\\python.exe shared/research/mlam_ch7_ours.py OUT_JSON
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from shared.models.denoise_onc import cluster_kmeans_base, cov_to_corr  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.research import ch16_allocation_tests as c16  # noqa: E402

SIMS, SEED = 1000, 0


def clusters(cov: np.ndarray, use_abs: bool) -> dict:
    n = cov.shape[0]
    corr = cov_to_corr(cov)
    corr = np.abs(corr) if use_abs else corr
    _, cl, _ = cluster_kmeans_base(pd.DataFrame(corr, index=range(n), columns=range(n)), max_num_clusters=n // 2,
                                   n_init=10, random_state=SEED)
    return cl


def nco(cov: np.ndarray, cl: dict, alloc) -> np.ndarray:
    n = cov.shape[0]
    keys = sorted(cl)
    w_intra = np.zeros((n, len(keys)))
    for j, k in enumerate(keys):
        it = list(cl[k])
        w_intra[it, j] = alloc(cov[np.ix_(it, it)])
    return w_intra @ alloc(w_intra.T @ cov @ w_intra)


def weights(x: np.ndarray) -> dict[str, np.ndarray]:
    cov, corr, _ = c16.window_inputs(x)
    cl, cla = clusters(cov, False), clusters(cov, True)
    return {"HRP_current": c16.hrp_current(cov, corr, len(x), x), "IVP": c16.ivp(cov),
            "Markowitz_minvar": c16.opt_port(cov), "CLA_minvar_longonly": c16.cla_min_variance(cov),
            "NCO_minvar": nco(cov, cl, c16.opt_port), "NCO_minvar_abs": nco(cov, cla, c16.opt_port),
            "NCO_ivp": nco(cov, cl, c16.ivp), "NCO_cla_longonly": nco(cov, cl, c16.cla_min_variance)}


def main(out: Path) -> None:
    t = time.time()
    c16._below_normal()
    edges = c16.deployable_edges()
    trades = c16.load_trades(edges)
    s06 = pd.read_csv(c16.STREAMS_06, usecols=["date"])
    index = pd.DatetimeIndex(to_datetime_iso(s06["date"], utc=True)).sort_values()
    first = min(index.min(), trades["entry_time"].dt.floor("D").min())
    full = pd.date_range(first, index.max(), freq="D", tz="UTC")
    e = c16.stream_mtm(trades, edges, full)
    x0 = e.to_numpy()
    mu0, cov0 = x0.mean(axis=0), np.cov(x0, rowvar=False)
    corr0 = cov_to_corr(cov0)
    ev = np.linalg.eigvalsh(corr0)
    true_own = weights(x0)
    true_mv = c16.opt_port(cov0)
    res = {"edges": edges, "T": len(x0), "corr": np.round(corr0, 4).tolist(), "condition_number": float(ev.max() / ev.min()),
           "min_offdiag_corr": float(corr0[~np.eye(len(edges), dtype=bool)].min()),
           "clusters_corr": {str(k): [edges[i] for i in v] for k, v in clusters(cov0, False).items()},
           "clusters_abs_corr": {str(k): [edges[i] for i in v] for k, v in clusters(cov0, True).items()},
           "true_weights": {m: dict(zip(edges, map(float, w))) for m, w in true_own.items()}, "M2": {}}
    rng = np.random.default_rng(SEED)
    for n_obs in (c16.S_LENGTH, len(x0)):
        eb = {m: [] for m in true_own}
        eo = {m: [] for m in true_own}
        for _ in range(SIMS):
            xs = rng.multivariate_normal(mu0, cov0, size=n_obs)
            for m, w in weights(xs).items():
                eb[m].append((w - true_mv) ** 2)
                eo[m].append((w - true_own[m]) ** 2)
        res["M2"][str(n_obs)] = {"rmse_vs_true_minvar": {m: float(np.mean(np.concatenate(v)) ** .5) for m, v in eb.items()},
                                 "rmse_vs_own_true_weights": {m: float(np.mean(np.concatenate(v)) ** .5) for m, v in eo.items()}}
        print(n_obs, json.dumps(res["M2"][str(n_obs)]), round(time.time() - t, 1), flush=True)
    res["seconds"] = round(time.time() - t, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print({k: res[k] for k in ("condition_number", "min_offdiag_corr", "clusters_corr", "clusters_abs_corr")}, flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
