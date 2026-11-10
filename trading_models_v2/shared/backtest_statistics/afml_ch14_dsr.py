"""AFML Ch.14 s.14.7 (pp.203-206) PSR / DSR with MLAM Ch.8 (pp.108-123) trial estimation, FWER and Type II errors.

Owner rule: MLAM over AFML where MLAM revises -- MLAM s.8.7 replaces AFML's "N independent trials, variance across their
Sharpe ratios" with: E[K] = number of ONC clusters on the trials' return correlation matrix (s.8.7.1 p.114); one
min-variance portfolio per cluster, w_k = Sigma_k^-1 1 / 1' Sigma_k^-1 1 (s.8.7.2 p.114); annualised cluster Sharpe
ratios aSR_k = SR_k sqrt(Frequency_k), Frequency_k = T_k / Years_k; E[V[SR]] = V[aSR_k] / Frequency_selected (p.116).
MLAM s.8.8 adds the familywise error rate with Sidak's correction and the Type II error under multiple testing.

Pure functions; used by shared/research/dsr_all_trials.py (per edge, all Gate C trials) and
portfolio_allocator/07b_dsr_psr_validation.py. Owner decisions 2026-10-26 (AFML Ch.14 walkthrough, s.4N.1).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from shared.models.denoise_onc import (
    cluster_kmeans_top, denoised_corr_constant_residual_eigenvalue, detone_corr, find_max_eval, get_pca)

EULER_GAMMA = 0.5772156649015328606065120900824024
DAYS_PER_YEAR = 365.25
DSR_PASS = 0.95   # AFML p.206: PSR / DSR "should exceed 0.95, for the standard significance level of 5%"
FWER_PASS = 0.05


def moments(r: np.ndarray) -> dict[str, float]:
    """Non-annualised SR = mean / std (AFML p.203), skewness, kurtosis (Pearson: 3 for Gaussian returns, AFML p.203)."""
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 4:
        return {"T": int(r.size), "sr": float("nan"), "skew": float("nan"), "kurt": float("nan")}
    sd = float(np.std(r, ddof=1))
    return {"T": int(r.size), "sr": float(np.mean(r) / sd) if sd > 0 else float("nan"),
            "skew": float(stats.skew(r, bias=False)), "kurt": float(stats.kurtosis(r, fisher=False, bias=False))}


def frequency(index: pd.DatetimeIndex, t: int) -> float:
    """MLAM p.116: Frequency_k = T_k / Years_k, Years_k = (Last Date - First Date) / 365.25."""
    years = (pd.Timestamp(index.max()) - pd.Timestamp(index.min())).days / DAYS_PER_YEAR
    return float(t / years) if years > 0 else float("nan")


def z_stat(sr: float, t: int, sr_ref: float, skew: float, kurt: float) -> float:
    """MLAM Snippet 8.3 getZStat; Z[z_stat] is AFML p.203's PSR[sr_ref]."""
    den = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2
    return float((sr - sr_ref) * np.sqrt(t - 1) / np.sqrt(den)) if den > 0 and t > 1 else float("nan")


def expected_max_sr(k: int) -> float:
    """MLAM Snippet 8.1 getExpectedMaxSR(meanSR=0, stdSR=1) -- the False Strategy theorem (s.8.5 p.109)."""
    if k < 2:
        return 0.0
    return float((1 - EULER_GAMMA) * stats.norm.ppf(1 - 1.0 / k) + EULER_GAMMA * stats.norm.ppf(1 - 1.0 / (k * np.e)))


def min_variance_weights(cov: np.ndarray) -> tuple[np.ndarray, bool]:
    """MLAM p.114; pseudo-inverse when Sigma_k is singular (duplicate trials) -- flagged in the output."""
    ones = np.ones(cov.shape[0])
    singular = bool(np.linalg.matrix_rank(cov) < cov.shape[0])
    w = (np.linalg.pinv(cov) if singular else np.linalg.inv(cov)) @ ones
    return w / (ones @ w), singular


def get_ivp(cov: np.ndarray, use_extended_terms: bool = False) -> np.ndarray:
    """Lopez de Prado & Lewis (2018, SSRN 3167017) Appendix A.4 Snippet 4 getIVP -- the "robust method of computing w_k"
    that MLAM p.114 points to. Intra-cluster correlations are high, so Sigma_k "may be ill-conditioned and difficult to
    invert"; Sigma_k is approximated by rho sigma sigma' + (1 - rho) diag(sigma^2) (rho = average off-diagonal correlation)
    and inverted with Sherman-Morrison: w_i ~ 1/sigma_i^2 - rho sum_j(1/sigma_j) / ((1 + (N_k - 1) rho) sigma_i).
    use_extended_terms=False is plain inverse variance (rho = 0)."""
    cov = np.asarray(cov, dtype=float)
    ivp = 1.0 / np.diag(cov)
    if use_extended_terms:
        n = float(cov.shape[0])
        sd = np.sqrt(np.diag(cov))
        corr = cov / np.outer(sd, sd)
        rho = (np.sum(np.sum(corr)) - n) / (n ** 2 - n)  # average off-diagonal correlation
        inv_sigma = np.sqrt(ivp)
        ivp = ivp - rho * inv_sigma * np.sum(inv_sigma) / (1.0 + (n - 1) * rho)
    return ivp / ivp.sum()


# MLAM Ch.2 (owner approved 2026-11-02, D076): the trial correlation matrix is denoised before ONC (p.24 "Throughout this
# Element, we assume that empirical covariance and correlation matrices have been subjected to this procedure"; p.87
# "denoise and detone the correlation matrix before clustering"), exactly as Step 07: Snippets 2.4-2.5 (bWidth .01,
# nFacts = eigenvalues above lambda+, no floor, constant residual eigenvalue), then detoned (s.2.6, one component) only
# if a market component exists -- first eigenvalue > lambda+ AND first-eigenvector loadings of one sign in >= 95% of
# 1,000 iid row-bootstrap resamples (seed 0), Step 07's test. T <= N: no Marcenko-Pastur fit, raw correlation, reported.
TRIAL_CORR_RULE = "mlam_ch2_denoise_conditional_detone_v1"
TRIAL_CORR_BWIDTH = 0.01
MARKET_TEST_BOOTSTRAPS = 1000
MARKET_TEST_SEED = 0
MARKET_TEST_MIN_SAME_SIGN_SHARE = 0.95
ONC_N_INIT = 10


def first_eigenvector_same_sign_share(x: np.ndarray, n_boot: int = MARKET_TEST_BOOTSTRAPS,
                                      seed: int = MARKET_TEST_SEED) -> float:
    """Step 07's market test, looped (a trial matrix is too wide for the vectorised version): share of iid row-bootstrap
    resamples whose first correlation eigenvector has all loadings of one sign; a resample with a zero-variance column
    counts as mixed sign."""
    x = np.asarray(x, dtype="float64")
    rng = np.random.default_rng(seed)
    hits = 0
    for _ in range(n_boot):
        s = x[rng.integers(0, x.shape[0], size=x.shape[0])]
        if (s.std(axis=0) == 0).any():
            continue
        v = np.linalg.eigh(np.corrcoef(s, rowvar=False))[1][:, -1]
        hits += bool(np.all(v > 0) or np.all(v < 0))
    return hits / n_boot


def trial_clustering_corr(perf: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The correlation matrix ONC clusters the trials on (rule above TRIAL_CORR_RULE), with every test recorded."""
    corr = perf.corr()
    n, t = corr.shape[0], len(perf)
    diag: dict[str, Any] = {"rule": TRIAL_CORR_RULE, "N": int(n), "T": int(t), "denoised": False, "detoned": False}
    if t <= n:
        diag["reason"] = f"T={t} <= N={n}: no Marcenko-Pastur fit, raw correlation"
        return corr, diag
    e_val, e_vec = get_pca(corr.to_numpy(dtype="float64"))
    raw = np.diag(e_val)
    e_max, var = find_max_eval(raw, t / n, TRIAL_CORR_BWIDTH)
    n_facts = int(raw.shape[0] - raw[::-1].searchsorted(e_max))   # Snippet 2.4, no floor
    den = denoised_corr_constant_residual_eigenvalue(e_val, e_vec, n_facts)
    share = first_eigenvector_same_sign_share(perf.to_numpy(dtype="float64"))
    market = bool(raw[0] > e_max and share >= MARKET_TEST_MIN_SAME_SIGN_SHARE)
    diag.update({"denoised": True, "q": t / n, "fitted_noise_variance": float(var), "lambda_plus": float(e_max),
                 "n_facts": n_facts, "top_eigenvalues": [float(v) for v in raw[:5]],
                 "first_eigenvector_same_sign_share": share, "market_component": market})
    out = den
    if market and n_facts >= 1:
        d_val, d_vec = get_pca(den)
        out = detone_corr(den, d_val, d_vec, 1)
        diag["detoned"] = True
    return pd.DataFrame(out, index=corr.index, columns=corr.columns), diag


def onc_trial_clusters(perf: pd.DataFrame, seed: int = 0) -> tuple[dict[Any, list[str]], dict[str, Any]]:
    """MLAM s.8.7.1 p.114: E[K] = ONC clusters (clusterKMeansTop, n_init 10) on the denoised/detoned trial correlation."""
    corr, diag = trial_clustering_corr(perf)
    _, clusters, _ = cluster_kmeans_top(corr, n_init=ONC_N_INIT, random_state=seed)
    return clusters, diag


CLUSTER_WEIGHTING = "ldp_lewis_2018_getIVP_extended"  # owner decision 2026-10-26 (AFML Ch.14 walkthrough s.4N.3)


def cluster_trial_variance(perf: pd.DataFrame, clusters: dict[Any, list[str]],
                           weighting: str = CLUSTER_WEIGHTING) -> dict[str, Any]:
    """MLAM s.8.7.2 / Lopez de Prado & Lewis (2018) s.8.2: one aggregate series per cluster, annualised cluster SRs and
    their variance (annualised units). weighting: 'ldp_lewis_2018_getIVP_extended' (default; the paper's robust
    minimum-variance approximation), 'exact_min_variance' (Sigma_k^-1 1 / 1' Sigma_k^-1 1, pseudo-inverse if singular),
    'equal'."""
    rows, asr = [], []
    for cid, members in clusters.items():
        sub = perf[list(members)]
        if len(members) == 1:
            w, singular = np.ones(1), False
        elif weighting == "ldp_lewis_2018_getIVP_extended":
            w, singular = get_ivp(sub.cov().to_numpy(), use_extended_terms=True), False
        elif weighting == "exact_min_variance":
            w, singular = min_variance_weights(sub.cov().to_numpy())
        elif weighting == "equal":
            w, singular = np.full(len(members), 1.0 / len(members)), False
        else:
            raise ValueError(f"unknown cluster weighting: {weighting}")
        m = moments(sub.to_numpy() @ w)
        a = m["sr"] * np.sqrt(frequency(perf.index, m["T"]))
        asr.append(a)
        rows.append({"cluster": int(cid) if str(cid).isdigit() else str(cid), "n_members": len(members),
                     "members": list(members), "singular_cov_pinv": singular, "sr": m["sr"], "annualised_sr": float(a)})
    a = np.asarray(asr, dtype=float)
    a = a[np.isfinite(a)]
    return {"E_K": len(clusters), "clusters": rows, "weighting": weighting,
            "V_annualised_sr": float(np.var(a, ddof=1)) if a.size > 1 else float("nan")}


def dsr_report(r: np.ndarray, index: pd.DatetimeIndex, e_k: int, v_annualised_sr: float,
               sr_alternatives_annualised: dict[str, float] | None = None) -> dict[str, Any]:
    """PSR[0], DSR (AFML p.204, SR* de-annualised to the target's own frequency as MLAM p.116), FWER (MLAM s.8.8.2) and
    Type II (s.8.8.3) for one return series selected out of E[K] effective trials."""
    m = moments(r)
    freq = frequency(index, m["T"])
    v = v_annualised_sr / freq if np.isfinite(v_annualised_sr) and freq > 0 else float("nan")
    sr_star = float(np.sqrt(v) * expected_max_sr(e_k)) if e_k > 1 and np.isfinite(v) else 0.0
    z0 = z_stat(m["sr"], m["T"], 0.0, m["skew"], m["kurt"])
    psr0 = float(stats.norm.cdf(z0))
    dsr = float(stats.norm.cdf(z_stat(m["sr"], m["T"], sr_star, m["skew"], m["kurt"])))
    alpha = float(1.0 - stats.norm.cdf(z0))
    alpha_k = float(1.0 - (1.0 - alpha) ** max(e_k, 1))
    out: dict[str, Any] = {
        **m, "frequency": freq, "annualised_sr": float(m["sr"] * np.sqrt(freq)) if np.isfinite(freq) else float("nan"),
        "E_K": int(e_k), "V_sr_nonannualised": v, "sr_star_nonannualised": sr_star,
        "sr_star_annualised": float(sr_star * np.sqrt(freq)) if np.isfinite(freq) else float("nan"),
        "psr_vs_zero": psr0, "dsr": dsr, "dsr_pass_0p95": bool(np.isfinite(dsr) and dsr > DSR_PASS),
        "fwer": {"z0": z0, "alpha_single": alpha, "alpha_K": alpha_k, "pass_0p05": bool(alpha_k < FWER_PASS)},
        "type2": {},
    }
    den = 1.0 - m["skew"] * m["sr"] + (m["kurt"] - 1.0) / 4.0 * m["sr"] ** 2
    z_a = float(stats.norm.ppf((1.0 - alpha_k) ** (1.0 / max(e_k, 1))))
    for name, sr_ann in (sr_alternatives_annualised or {"annualised_1p0": 1.0}).items():
        sr_alt = sr_ann / np.sqrt(freq) if np.isfinite(freq) and freq > 0 else float("nan")
        theta = float(sr_alt * np.sqrt(m["T"] - 1) / np.sqrt(den)) if den > 0 else float("nan")
        beta = float(stats.norm.cdf(z_a - theta))
        out["type2"][name] = {"sr_alternative_annualised": sr_ann, "theta": theta, "beta": beta,
                              "beta_K": float(beta ** max(e_k, 1))}
    return out
