"""Canonical Marcenko-Pastur denoising / detoning / ONC clustering.

Source: Marcos Lopez de Prado, *Machine Learning for Asset Managers*
(MLAM, 2020), Chapter 2 ("Denoising and Detoning", book p.24-37) and
Chapter 4 ("Optimal Clustering", book p.52-64). Every formula/algorithm
below is transcribed from those chapters (see
docs/mldp/... blueprint reference in the parent repo for page-cited
derivations); this module does not introduce any new mathematics.

WHY THIS FILE IS CANONICAL AND STANDALONE (read before "fixing" this by
splitting it up or adding shared/-internal imports):
Edge Step 01 (feature surgery / clustered feature importance) is one of the
two research stations (see shared/colab_portable/purge_embargo_snippet.py
for the other, Step 02) that Quantara runs standalone on Google Colab --
only the single script plus its data files travel there, never the rest of
the `shared/` package. Any edge script that adopts this denoising/ONC
utility must copy the needed functions from THIS file verbatim, and any such
adoption must add a drift-detection test (see
tests/test_colab_portable_snippet_sync.py for the established pattern)
asserting the copy stays in sync with this file. Do not assume a runtime
`import shared.*` is available to a Step 01 script.

Dependencies are deliberately limited to numpy, pandas, scipy, and
scikit-learn -- all present in a stock Google Colab runtime.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_samples
from sklearn.neighbors import KernelDensity


class DenoiseONCError(RuntimeError):
    """Raised on invalid inputs or a failed fit in this module."""


# ---------------------------------------------------------------------------
# Correlation/covariance utilities (MLAM p.29, standard rescaling helpers)
# ---------------------------------------------------------------------------


def cov_to_corr(cov: np.ndarray) -> np.ndarray:
    """Derive the correlation matrix from a covariance matrix."""
    std = np.sqrt(np.diag(cov))
    corr = cov / np.outer(std, std)
    corr[corr < -1] = -1
    corr[corr > 1] = 1
    return corr


def corr_to_cov(corr: np.ndarray, std: np.ndarray) -> np.ndarray:
    """Recover a covariance matrix from a correlation matrix and std vector."""
    return corr * np.outer(std, std)


def get_pca(matrix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Eigendecomposition of a Hermitian matrix, sorted by descending
    eigenvalue. Returns (eVal, eVec) where eVal is an N x N diagonal matrix.
    """
    eigenvalues, eigenvectors = np.linalg.eigh(matrix)
    indices = eigenvalues.argsort()[::-1]
    eigenvalues, eigenvectors = eigenvalues[indices], eigenvectors[:, indices]
    eigenvalues = np.diagflat(eigenvalues)
    return eigenvalues, eigenvectors


# ---------------------------------------------------------------------------
# Marcenko-Pastur theorem (MLAM Section 2.2, book p.24-26)
# ---------------------------------------------------------------------------


def mp_pdf(var: float, q: float, pts: int) -> pd.Series:
    """Theoretical Marcenko-Pastur probability density function.

    q = T/N (observations / variables). Eigenvalues outside [eMin, eMax] are
    consistent with signal, not randomness.
    """
    if q <= 0:
        raise DenoiseONCError("q (T/N) must be > 0.")
    e_min = var * (1 - (1.0 / q) ** 0.5) ** 2
    e_max = var * (1 + (1.0 / q) ** 0.5) ** 2
    e_val = np.linspace(e_min, e_max, pts)
    pdf = q / (2 * np.pi * var * e_val) * ((e_max - e_val) * (e_val - e_min)) ** 0.5
    pdf = np.nan_to_num(pdf, nan=0.0)
    return pd.Series(pdf, index=e_val)


def fit_kde(
    observations: np.ndarray,
    bwidth: float = 0.25,
    kernel: str = "gaussian",
    x: np.ndarray | None = None,
) -> pd.Series:
    """Fit a kernel density estimate to a series of observations (typically
    empirical eigenvalues), evaluated at `x` if supplied."""
    obs = np.asarray(observations).reshape(-1, 1)
    kde = KernelDensity(kernel=kernel, bandwidth=bwidth).fit(obs)
    if x is None:
        x = np.unique(obs).reshape(-1, 1)
    else:
        x = np.asarray(x).reshape(-1, 1)
    log_prob = kde.score_samples(x)
    return pd.Series(np.exp(log_prob), index=x.flatten())


def _err_pdfs(var: float, eigenvalues: np.ndarray, q: float, bwidth: float, pts: int = 1000) -> float:
    theoretical = mp_pdf(var, q, pts)
    empirical = fit_kde(eigenvalues, bwidth, x=theoretical.index.values)
    return float(np.sum((empirical - theoretical) ** 2))


def find_max_eval(eigenvalues: np.ndarray, q: float, bwidth: float) -> tuple[float, float]:
    """Fit the Marcenko-Pastur distribution's free parameter (var, the
    fraction of variance attributable to noise) by minimizing the SSE
    between the theoretical MP PDF and a KDE of the empirical eigenvalue
    distribution (MLAM p.27-28). Returns (eMax, var)."""
    result = minimize(
        lambda v: _err_pdfs(v[0], eigenvalues, q, bwidth),
        x0=np.array([0.5]),
        bounds=((1e-5, 1 - 1e-5),),
    )
    var = float(result.x[0]) if result.success else 1.0
    e_max = var * (1 + (1.0 / q) ** 0.5) ** 2
    return e_max, var


# ---------------------------------------------------------------------------
# Denoising (MLAM Section 2.5, book p.28-31)
# ---------------------------------------------------------------------------


def denoised_corr_constant_residual_eigenvalue(
    eigenvalues: np.ndarray, eigenvectors: np.ndarray, n_facts: int
) -> np.ndarray:
    """Constant residual eigenvalue method (MLAM p.29) -- the book's
    preferred/canonical denoising method. Replaces every eigenvalue below
    the fitted noise cutoff with their common average, preserving the
    matrix's trace, then rescales the diagonal back to 1s."""
    eigenvalues_ = np.diag(eigenvalues).copy()
    if n_facts < len(eigenvalues_):
        eigenvalues_[n_facts:] = eigenvalues_[n_facts:].sum() / float(len(eigenvalues_) - n_facts)
    eigenvalues_ = np.diag(eigenvalues_)
    corr = eigenvectors @ eigenvalues_ @ eigenvectors.T
    return cov_to_corr(corr)


def denoised_corr_targeted_shrinkage(
    eigenvalues: np.ndarray, eigenvectors: np.ndarray, n_facts: int, alpha: float = 0.0
) -> np.ndarray:
    """Targeted shrinkage method (MLAM p.29-31) -- an alternative denoising
    method that shrinks only the noise eigenvector block toward its
    diagonal, controlled by alpha (alpha=0 gives total shrinkage of the
    noise block to its diagonal). Presented as an alternative to, not
    superior to, the constant-residual-eigenvalue method above."""
    eigenvalues_l = eigenvalues[:n_facts, :n_facts]
    eigenvectors_l = eigenvectors[:, :n_facts]
    eigenvalues_r = eigenvalues[n_facts:, n_facts:]
    eigenvectors_r = eigenvectors[:, n_facts:]
    corr_left = eigenvectors_l @ eigenvalues_l @ eigenvectors_l.T
    corr_right = eigenvectors_r @ eigenvalues_r @ eigenvectors_r.T
    corr = corr_left + alpha * corr_right + (1 - alpha) * np.diag(np.diag(corr_right))
    return corr


def detone_corr(
    corr: np.ndarray, eigenvalues: np.ndarray, eigenvectors: np.ndarray, market_components: int = 1
) -> np.ndarray:
    """Remove the dominant "market" component(s) before clustering (MLAM
    p.30-32). The result is singular by construction and must not be used
    directly for mean-variance optimization -- map optimal weights back via
    the surviving (non-market) eigenvectors instead."""
    market_eigenvalues = eigenvalues[:market_components, :market_components]
    market_eigenvectors = eigenvectors[:, :market_components]
    corr_market = market_eigenvectors @ market_eigenvalues @ market_eigenvectors.T
    corr_detoned = corr - corr_market
    return cov_to_corr(corr_detoned)


def denoise_correlation(
    corr: np.ndarray,
    n_obs: int,
    *,
    bwidth: float = 0.01,
    detone: bool = False,
    market_components: int = 1,
    method: str = "constant_residual_eigenvalue",
    alpha: float = 0.0,
) -> dict[str, Any]:
    """Top-level orchestration: fit the Marcenko-Pastur noise cutoff on an
    empirical correlation matrix, denoise it, and optionally detone it.

    Parameters
    ----------
    corr : N x N empirical correlation matrix.
    n_obs : T, the number of observations the correlation matrix was
        estimated from (q = T/N feeds the Marcenko-Pastur fit).
    method : "constant_residual_eigenvalue" (canonical/recommended) or
        "targeted_shrinkage" (alternative).

    Returns a dict with the denoised (and, if requested, detoned)
    correlation matrix plus fit diagnostics (n_facts, fitted noise variance,
    condition numbers before/after) so callers can audit the fit rather than
    trust it blindly.
    """
    corr = np.asarray(corr, dtype="float64")
    n = corr.shape[0]
    if corr.shape != (n, n):
        raise DenoiseONCError("corr must be a square matrix.")
    if n_obs <= n:
        raise DenoiseONCError(
            f"n_obs (T={n_obs}) must exceed the number of variables (N={n}) "
            "for the Marcenko-Pastur fit to be meaningful (q=T/N must be > 1)."
        )
    q = float(n_obs) / float(n)

    eigenvalues, eigenvectors = get_pca(corr)
    raw_eigenvalues = np.diag(eigenvalues)
    condition_number_before = float(raw_eigenvalues.max() / max(raw_eigenvalues.min(), 1e-16))

    e_max, fitted_var = find_max_eval(raw_eigenvalues, q, bwidth)
    # Snippet 2.4: nFacts = eigenvalues above lambda+, no floor (D076); 0 gives the identity matrix.
    n_facts = int((raw_eigenvalues > e_max).sum())

    if method == "constant_residual_eigenvalue":
        denoised = denoised_corr_constant_residual_eigenvalue(eigenvalues, eigenvectors, n_facts)
    elif method == "targeted_shrinkage":
        denoised = denoised_corr_targeted_shrinkage(eigenvalues, eigenvectors, n_facts, alpha)
    else:
        raise DenoiseONCError(f"Unknown denoising method: {method!r}")

    denoised_eigenvalues_diag, denoised_eigenvectors = get_pca(denoised)
    denoised_eigenvalues = np.diag(denoised_eigenvalues_diag)
    condition_number_after = float(
        denoised_eigenvalues.max() / max(denoised_eigenvalues.min(), 1e-16)
    )

    result: dict[str, Any] = {
        "denoised_corr": denoised,
        "q": q,
        "n_facts": n_facts,
        "fitted_noise_variance": fitted_var,
        "e_max": e_max,
        "condition_number_before": condition_number_before,
        "condition_number_after": condition_number_after,
        "detoned": False,
    }

    if detone and n_facts == 0:
        result["detone_skipped"] = "no eigenvalue above lambda+: no market component to remove"
    elif detone:
        if market_components > n_facts:
            raise DenoiseONCError(
                "market_components cannot exceed the number of signal "
                f"factors found (n_facts={n_facts}). Detoning all detected "
                "signal (market_components == n_facts) is valid -- it "
                "leaves a fully singular, noise-only matrix by design."
            )
        detoned = detone_corr(
            denoised, denoised_eigenvalues_diag, denoised_eigenvectors, market_components
        )
        result["detoned_corr"] = detoned
        result["detoned"] = True

    return result


# ---------------------------------------------------------------------------
# Optimal Number of Clusters -- ONC (MLAM Section 4.4, book p.54-59)
# ---------------------------------------------------------------------------


def _cluster_quality(silhouette: np.ndarray) -> float:
    std = silhouette.std()
    if std <= 0 or not np.isfinite(std):
        return float("-inf")
    return float(silhouette.mean() / std)


def cluster_kmeans_base(
    corr0: pd.DataFrame,
    max_num_clusters: int = 10,
    n_init: int = 10,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    """ONC base clustering (MLAM p.55-57): a double loop over random k-means
    initializations and candidate cluster counts k, scoring each attempt by
    the silhouette-based quality metric q = mean(silhouette)/std(silhouette)
    and keeping only the single best-scoring (init, k) combination."""
    corr0 = corr0.copy()
    x = ((1 - corr0.fillna(0)) / 2.0) ** 0.5
    max_num_clusters = min(max_num_clusters, corr0.shape[1] - 1)
    if max_num_clusters < 2:
        raise DenoiseONCError("Need at least 3 items to form 2+ clusters.")

    best_stat = float("-inf")
    best_silh: pd.Series | None = None
    best_kmeans: KMeans | None = None
    rng = np.random.default_rng(random_state)

    for _ in range(n_init):
        for k in range(2, max_num_clusters + 1):
            seed = int(rng.integers(0, 2**31 - 1))
            kmeans_ = KMeans(n_clusters=k, n_init=1, random_state=seed).fit(x)
            silh_ = silhouette_samples(x, kmeans_.labels_)
            stat = _cluster_quality(silh_)
            if stat > best_stat:
                best_stat = stat
                best_silh = pd.Series(silh_, index=x.index)
                best_kmeans = kmeans_

    if best_kmeans is None or best_silh is None:
        raise DenoiseONCError("ONC base clustering failed to find any valid clustering.")

    new_idx = [corr0.index[i] for i in np.argsort(best_kmeans.labels_)]
    corr1 = corr0.loc[new_idx, new_idx]
    clstrs = {
        i: corr0.columns[np.where(best_kmeans.labels_ == i)[0]].tolist()
        for i in np.unique(best_kmeans.labels_)
    }
    return corr1, clstrs, best_silh


def _make_new_outputs(
    corr0: pd.DataFrame,
    clstrs: dict[int, list[Any]],
    clstrs2: dict[int, list[Any]],
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    clstrs_new: dict[int, list[Any]] = {}
    for i in clstrs.keys():
        clstrs_new[len(clstrs_new)] = list(clstrs[i])
    for i in clstrs2.keys():
        clstrs_new[len(clstrs_new)] = list(clstrs2[i])
    new_idx = [j for members in clstrs_new.values() for j in members]
    corr_new = corr0.loc[new_idx, new_idx]
    x = ((1 - corr0.fillna(0)) / 2.0) ** 0.5
    labels = pd.Series(index=corr0.index, dtype="int64")
    for cluster_id, members in clstrs_new.items():
        for member in members:
            labels.loc[member] = cluster_id
    silh_new = pd.Series(
        silhouette_samples(x, labels.loc[x.index].to_numpy()), index=x.index
    )
    return corr_new, clstrs_new, silh_new


def cluster_kmeans_top(
    corr0: pd.DataFrame,
    max_num_clusters: int | None = None,
    n_init: int = 10,
    random_state: int | None = None,
) -> tuple[pd.DataFrame, dict[int, list[Any]], pd.Series]:
    """Full ONC algorithm (MLAM p.55-59): base clustering, then recursive
    re-clustering of only the below-average-quality clusters, accepted only
    if it genuinely improves overall quality (else the base clustering is
    returned unchanged)."""
    if max_num_clusters is None:
        max_num_clusters = corr0.shape[1] - 1
    corr1, clstrs, silh = cluster_kmeans_base(
        corr0,
        max_num_clusters=min(max_num_clusters, corr0.shape[1] - 1),
        n_init=n_init,
        random_state=random_state,
    )
    cluster_t_stats = {
        i: _cluster_quality(silh.loc[members].to_numpy()) for i, members in clstrs.items()
    }
    t_stat_mean = float(np.mean(list(cluster_t_stats.values())))
    redo_clusters = [i for i, stat in cluster_t_stats.items() if stat < t_stat_mean]

    if len(redo_clusters) <= 1:
        return corr1, clstrs, silh

    keys_redo = [j for i in redo_clusters for j in clstrs[i]]
    corr_tmp = corr0.loc[keys_redo, keys_redo]
    redo_t_stat_mean = float(np.mean([cluster_t_stats[i] for i in redo_clusters]))
    corr2, clstrs2, _silh2 = cluster_kmeans_top(
        corr_tmp,
        max_num_clusters=min(max_num_clusters, corr_tmp.shape[1] - 1),
        n_init=n_init,
        random_state=random_state,
    )
    kept_clusters = {i: members for i, members in clstrs.items() if i not in redo_clusters}
    corr_new, clstrs_new, silh_new = _make_new_outputs(corr0, kept_clusters, clstrs2)
    new_t_stat_mean = float(
        np.mean(
            [
                _cluster_quality(silh_new.loc[members].to_numpy())
                for members in clstrs_new.values()
            ]
        )
    )
    if new_t_stat_mean <= redo_t_stat_mean:
        return corr1, clstrs, silh
    return corr_new, clstrs_new, silh_new
