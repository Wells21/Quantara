"""Clustered feature importance (MLAM Ch.6) with MLAM Ch.2-4 preprocessing, plus AFML Ch.8 diagnostics.

Owner rule 2026-10-24 (docs/mldp/AFML_MLAM_WALKTHROUGH.md working rule 6): where MLAM revises an AFML method, follow
MLAM in full. MLAM s.6.5.1-6.5.2 (p.84) replaces AFML Ch.8's PCA orthogonalisation with clustered feature importance.

Book pages (MLAM printed page = pdf - 5; AFML printed page = pdf - 26):
  * observation matrix            MLAM s.4.4.1 pp.54-55: sqrt((1 - rho)/2) ("approach c"); detone first if a common
                                  component is present (p.54, s.2.6 pp.30-32); PCA-reduce the observation matrix to the
                                  signal dimensions (p.55); |rho| distance sqrt(1 - |rho|) (s.3.2 p.39); normalised
                                  variation of information, "ideally" information-theoretic (p.54 fn.11, p.85, s.3.8-3.9
                                  pp.42-46, Snippet 3.3)
  * denoise / detone              MLAM Snippets 2.1-2.5 (pp.25-29), s.2.6 (pp.30-32) - shared/colab_portable/denoise_onc_snippet.py
  * ONC                           MLAM Snippets 4.1-4.2 (pp.56-58); "ONC is agnostic as to how the observations matrix is
                                  formed" (p.54 fn.11)
  * residual features             MLAM s.6.5.2 p.85 (replace a cluster's features by their OLS residuals on the features of
                                  the clusters before it)
  * estimator                     MLAM Snippets 6.2/6.3/6.7/6.8 (pp.79, 82, 89, 90) = AFML Snippet 8.8 (p.123): bagged entropy
                                  trees, max_features=1, class_weight='balanced', min_weight_fraction_leaf=0, 1,000 trees;
                                  resampled with the AFML Ch.4 sequential bootstrap at avgU x n rows (AFML Ch.4/6 decisions
                                  D030/D034)
  * MDI / clustered MDI           MLAM Snippets 6.2 (p.79) / 6.4 (p.86); AFML Snippet 8.2 (pp.115-116)
  * MDA / clustered MDA           MLAM Snippets 6.3 (p.82) / 6.5 (p.87): relative score loss (scr0 - scr1)/(-scr1), each
                                  feature of a cluster shuffled independently; AFML Snippet 8.3 (pp.116-117): purged and
                                  embargoed CV, sample-weighted neg log loss
  * SFI                           AFML Snippet 8.4 (p.118)
  * PCA / weighted Kendall's tau  AFML Snippets 8.5-8.6 (pp.119-121) - diagnostic only (MLAM p.84 caveats)
CV: MLAM's snippets use plain KFold on IID synthetic data; for financial labels AFML Ch.7 applies (MLAM silent), so all
splits here are purged on the label t1 and embargoed (runbook 6A Step 12E).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd
import scipy.stats as ss
from scipy.stats import weightedtau
from sklearn.cluster import KMeans
from sklearn.metrics import mutual_info_score, silhouette_samples
from sklearn.tree import DecisionTreeClassifier

from shared.colab_portable.denoise_onc_snippet import denoise_correlation
from shared.models.sequential_bootstrap import get_avg_uniqueness, sequential_bootstrap

CFI_VERSION = "mlam_ch6_clustered_feature_importance_v2"  # v2: best-of-20 ONC restarts (D077)
# MLAM Snippet 6.8 estimator with AFML p.98 early stopping min_weight_fraction_leaf = 0.05 (approved deviation D041)
TREE_PARAMS = {"criterion": "entropy", "max_features": 1, "class_weight": "balanced", "min_weight_fraction_leaf": 0.05}
EPS = 1e-15


class ClusteredImportanceError(RuntimeError):
    """Raised when a clustered-importance computation cannot be done safely."""


# =============================================================================
# Codependence -> observation matrix (MLAM Ch.2-4)
# =============================================================================
def num_bins(n_obs: int, corr: float | None = None) -> int:
    """MLAM Snippet 3.3 (p.46): optimal number of bins (Hacine-Gharbi et al.)."""
    if corr is None:
        z = (8 + 324 * n_obs + 12 * (36 * n_obs + 729 * n_obs**2) ** 0.5) ** (1 / 3.0)
        b = round(z / 6.0 + 2.0 / (3 * z) + 1.0 / 3)
    else:
        corr = float(np.clip(corr, -1 + 1e-9, 1 - 1e-9))  # identical features: guard the 1/(1-rho^2) singularity
        b = round(2**-0.5 * (1 + (1 + 24 * n_obs / (1.0 - corr**2)) ** 0.5) ** 0.5)
    return int(max(b, 1))


def var_info(x: np.ndarray, y: np.ndarray, norm: bool = True) -> float:
    """MLAM Snippet 3.3 (p.46): (normalised) variation of information on discretised continuous variables."""
    b = num_bins(x.shape[0], corr=np.corrcoef(x, y)[0, 1])
    c_xy = np.histogram2d(x, y, b)[0]
    i_xy = mutual_info_score(None, None, contingency=c_xy)
    h_x = ss.entropy(np.histogram(x, b)[0])
    h_y = ss.entropy(np.histogram(y, b)[0])
    v_xy = h_x + h_y - 2 * i_xy
    if norm:
        h_xy = h_x + h_y - i_xy
        v_xy = v_xy / h_xy if h_xy > 0 else 0.0
    return float(max(v_xy, 0.0))


def vi_matrix(x: pd.DataFrame) -> pd.DataFrame:
    cols = list(x.columns)
    arr = x.to_numpy(dtype="float64")
    out = np.zeros((len(cols), len(cols)))
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            out[i, j] = out[j, i] = var_info(arr[:, i], arr[:, j], norm=True)
    return pd.DataFrame(out, index=cols, columns=cols)


def observation_signal_dims(obs: pd.DataFrame, bwidth: float = 0.01) -> int:
    """MLAM p.55: "the number of eigenvalues in X's correlation matrix that exceed lambda+ (see Section 2)" -- X is the
    observation matrix; lambda+ from the Marcenko-Pastur fit (Snippet 2.4) with q = rows / columns of X."""
    from shared.colab_portable.denoise_onc_snippet import find_max_eval

    corr = np.nan_to_num(np.corrcoef(obs.to_numpy(dtype="float64"), rowvar=False))
    e_val = np.sort(np.linalg.eigvalsh(corr))[::-1]
    q = obs.shape[0] / obs.shape[1]
    e_max, _ = find_max_eval(e_val, q, bwidth)
    return int(max(1, (e_val > e_max).sum()))


def _reduce_observations(obs: pd.DataFrame, n_dims: int) -> pd.DataFrame:
    """MLAM p.55: replace the observation matrix by its standardised orthogonal projection on n_dims principal components."""
    z = (obs - obs.mean(axis=0)) / obs.std(axis=0).replace(0.0, 1.0)
    e_val, e_vec = np.linalg.eigh(np.cov(z.to_numpy(), rowvar=False))
    order = np.argsort(e_val)[::-1][: max(1, int(n_dims))]
    proj = z.to_numpy() @ e_vec[:, order]
    proj = (proj - proj.mean(axis=0)) / np.where(proj.std(axis=0) > 0, proj.std(axis=0), 1.0)
    return pd.DataFrame(proj, index=obs.index, columns=[f"PC_{i + 1}" for i in range(proj.shape[1])])


def observation_matrix(x: pd.DataFrame, method: str, *, denoise: bool = True, detone: bool = True,
                       reduce: bool = True, bwidth: float = 0.01) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Feature observation matrix for ONC. method: corr_signed | corr_abs | vi.
    denoise/detone/reduce default to MLAM's full recipe; MLAM's synthetic experiment (p.87) uses the raw correlation."""
    corr = np.corrcoef(x.to_numpy(dtype="float64"), rowvar=False)  # MLAM Snippet 6.6 uses X.corr() (Pearson)
    corr = np.nan_to_num(np.clip(corr, -1, 1))
    np.fill_diagonal(corr, 1.0)
    den = denoise_correlation(corr, n_obs=len(x), bwidth=bwidth, detone=denoise and detone and method != "vi",
                              market_components=1)
    n_facts = int(den["n_facts"])
    diag: dict[str, Any] = {"method": method, "n_facts": n_facts, "denoised": bool(denoise and method != "vi"),
                            "detoned": bool(denoise and detone and method != "vi"), "reduced_to_dims": None}
    if method in ("corr_signed", "corr_abs"):
        if not diag["denoised"]:
            rho = corr
        else:
            rho = np.asarray(den["detoned_corr"] if diag["detoned"] else den["denoised_corr"], dtype="float64")
        d = np.sqrt(np.clip(np.diag(rho), EPS, None))
        rho = np.clip(rho / np.outer(d, d), -1, 1)
        np.fill_diagonal(rho, 1.0)
        dist = np.sqrt(np.clip((1 - rho) / 2.0, 0, None)) if method == "corr_signed" else np.sqrt(np.clip(1 - np.abs(rho), 0, None))
        obs = pd.DataFrame(dist, index=x.columns, columns=x.columns)
    elif method == "vi":
        obs = vi_matrix(x)
    else:
        raise ClusteredImportanceError(f"unknown observation-matrix method {method!r}")
    if reduce:
        diag["reduced_to_dims"] = observation_signal_dims(obs, bwidth)
        obs = _reduce_observations(obs, diag["reduced_to_dims"])
    return obs, diag


# =============================================================================
# ONC on an observation matrix (MLAM Snippets 4.1-4.2)
# =============================================================================
def _quality(s: np.ndarray) -> float:
    sd = s.std()
    return float(s.mean() / sd) if sd > 0 and np.isfinite(sd) else float("-inf")


def onc_base(obs: pd.DataFrame, max_k: int | None = None, n_init: int = 10, seed: int = 0):
    """MLAM Snippet 4.1: double loop over initialisations and k = 2..max_k, keep the highest-quality k-means."""
    n = obs.shape[0]
    max_k = n - 1 if max_k is None else min(max_k, n - 1)
    if max_k < 2:
        raise ClusteredImportanceError("need at least 3 items for ONC")
    rng = np.random.default_rng(seed)
    best_q, best_lab, best_s = float("-inf"), None, None
    arr = obs.to_numpy()
    for _ in range(n_init):
        for k in range(2, max_k + 1):
            lab = KMeans(n_clusters=k, n_init=1, random_state=int(rng.integers(0, 2**31 - 1))).fit(arr).labels_
            s = silhouette_samples(arr, lab)
            q = _quality(s)
            if q > best_q:
                best_q, best_lab, best_s = q, lab, s
    clusters = {int(i): [obs.index[j] for j in np.flatnonzero(best_lab == i)] for i in np.unique(best_lab)}
    return clusters, pd.Series(best_s, index=obs.index)


def _sub(obs: pd.DataFrame, keys: list) -> pd.DataFrame:
    square = obs.shape[0] == obs.shape[1] and list(obs.index) == list(obs.columns)
    return obs.loc[keys, keys] if square else obs.loc[keys]


def onc_top(obs: pd.DataFrame, max_k: int | None = None, n_init: int = 10, seed: int = 0):
    """MLAM Snippet 4.2: re-cluster the below-average-quality clusters; accept only if quality improves."""
    clusters, silh = onc_base(obs, max_k, n_init, seed)
    t_stats = {i: _quality(silh.loc[m].to_numpy()) for i, m in clusters.items()}
    t_mean = float(np.mean(list(t_stats.values())))
    redo = [i for i, t in t_stats.items() if t < t_mean]
    if len(redo) <= 1:
        return clusters, silh
    keys = [k for i in redo for k in clusters[i]]
    if len(keys) < 3:
        return clusters, silh
    sub = _sub(obs, keys)
    clusters2, _ = onc_top(sub, None if max_k is None else min(max_k, len(keys) - 1), n_init, seed + 1)
    new = {}
    for i, m in clusters.items():
        if i not in redo:
            new[len(new)] = list(m)
    for m in clusters2.values():
        new[len(new)] = list(m)
    labels = pd.Series(0, index=obs.index)
    for i, m in new.items():
        labels.loc[m] = i
    silh_new = pd.Series(silhouette_samples(obs.to_numpy(), labels.to_numpy()), index=obs.index)
    new_mean = float(np.mean([_quality(silh_new.loc[m].to_numpy()) for m in new.values()]))
    if new_mean <= float(np.mean([t_stats[i] for i in redo])):
        return clusters, silh
    return new, silh_new


def residualize(x: pd.DataFrame, clusters: dict[int, list[str]]) -> pd.DataFrame:
    """MLAM s.6.5.2 p.85: replace each cluster's features by their OLS residuals on the features of the earlier clusters."""
    out = x.copy()
    prior: list[str] = []
    for cid in sorted(clusters):
        members = clusters[cid]
        if prior:
            a = np.column_stack([np.ones(len(x)), x[prior].to_numpy(dtype="float64")])
            beta, *_ = np.linalg.lstsq(a, x[members].to_numpy(dtype="float64"), rcond=None)
            out[members] = x[members].to_numpy(dtype="float64") - a @ beta
        prior.extend(members)
    return out


# =============================================================================
# Purged, embargoed k-fold (AFML Snippets 7.1-7.3) on the label t1
# =============================================================================
def purged_kfold_splits(t0: np.ndarray, t1: np.ndarray, n_splits: int, embargo: int) -> list[tuple[np.ndarray, np.ndarray]]:
    t0 = np.asarray(t0).astype("datetime64[ns]")
    t1 = np.asarray(t1).astype("datetime64[ns]")
    n = len(t0)
    splits = []
    for test in np.array_split(np.arange(n), n_splits):
        lo, hi = t0[test].min(), t1[test].max()
        train = np.setdiff1d(np.arange(n), test)
        train = train[~((t0[train] <= hi) & (t1[train] >= lo))]  # contiguous test block: one interval (AFML p.106)
        end = test.max()
        train = train[(train <= end) | (train > end + embargo)]
        splits.append((train, test))
    return splits


# =============================================================================
# Bagged trees on pre-drawn sequential-bootstrap samples (shared across features for SFI)
# =============================================================================
@dataclass
class _Bag:
    trees: list = field(default_factory=list)

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        out = np.zeros(len(x))
        for t in self.trees:
            p = t.predict_proba(x)
            out += p[:, list(t.classes_).index(1)] if 1 in t.classes_ else 0.0
        return out / len(self.trees)


def _draws(start: np.ndarray, end: np.ndarray, n_bars: int, n_trees: int, seed: int) -> tuple[list[np.ndarray], list[int]]:
    rng = np.random.default_rng(seed)
    avg_u = float(np.mean(get_avg_uniqueness(start, end, n_bars, np.arange(len(start)))))
    length = max(1, int(round(avg_u * len(start))))
    draws, seeds = [], []
    for _ in range(n_trees):
        draws.append(sequential_bootstrap(start, end, n_bars, sample_length=length, random_state=rng))
        seeds.append(int(rng.integers(0, 2**31 - 1)))
    return draws, seeds


MLAM_TREE_PARAMS = {**TREE_PARAMS, "min_weight_fraction_leaf": 0.0}  # MLAM Snippet 6.8 as printed (synthetic replication)


def _fit_bag(x: np.ndarray, y: np.ndarray, w: np.ndarray, draws, seeds, params: dict | None = None) -> _Bag:
    bag = _Bag()
    for d, s in zip(draws, seeds):
        bag.trees.append(DecisionTreeClassifier(**(params or TREE_PARAMS), random_state=s).fit(x[d], y[d], sample_weight=w[d]))
    return bag


def _neg_log_loss(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> float:
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.average(-(y * np.log(p) + (1 - y) * np.log(1 - p)), weights=w))


def _mean_se(df: pd.DataFrame) -> pd.DataFrame:
    return pd.concat({"mean": df.mean(), "std": df.std() * df.shape[0] ** -0.5}, axis=1)  # CLT (MLAM p.79)


# =============================================================================
# Importance
# =============================================================================
def mdi(bags: Sequence[_Bag], feat_names: list[str], clusters: dict[int, list[str]] | None = None) -> pd.DataFrame:
    """MLAM Snippet 6.2 / 6.4 (clustered): per-tree MDI, zeros -> NaN (max_features=1), CLT std, normalised to sum 1."""
    rows = [t.feature_importances_ for b in bags for t in b.trees]
    df0 = pd.DataFrame(rows, columns=feat_names).replace(0, np.nan)
    if clusters is None:
        imp = _mean_se(df0)
    else:
        grouped = pd.DataFrame({f"C_{i}": df0[m].sum(axis=1) for i, m in clusters.items()})
        imp = _mean_se(grouped)
    return imp / imp["mean"].sum()


def mda(x: pd.DataFrame, y: np.ndarray, w: np.ndarray, splits, bags: Sequence[_Bag],
        groups: dict[str, list[str]], seed: int = 0) -> tuple[pd.DataFrame, float]:
    """MLAM Snippets 6.3/6.5 + AFML 8.3: OOS weighted neg log loss before/after shuffling each group's features
    (independently per feature); importance (scr0 - scr1)/(-scr1)."""
    rng = np.random.default_rng(seed)
    arr = x.to_numpy(dtype="float64")
    col = {c: i for i, c in enumerate(x.columns)}
    scr0, scr1 = [], []
    for (train, test), bag in zip(splits, bags):
        x1, y1, w1 = arr[test], y[test], w[test]
        s0 = _neg_log_loss(y1, bag.predict_proba(x1), w1)
        row = {}
        for name, members in groups.items():
            x1_ = x1.copy()
            for f in members:
                x1_[:, col[f]] = x1_[rng.permutation(len(x1_)), col[f]]
            row[name] = _neg_log_loss(y1, bag.predict_proba(x1_), w1)
        scr0.append(s0)
        scr1.append(row)
    s0 = pd.Series(scr0)
    s1 = pd.DataFrame(scr1)
    imp = (-s1).add(s0, axis=0) / (-s1)
    return _mean_se(imp), float(s0.mean())


def fit_fold_bags(x: pd.DataFrame, y: np.ndarray, w: np.ndarray, splits, start, end, n_bars, n_trees: int, seed: int = 0,
                  params: dict | None = None):
    arr = x.to_numpy(dtype="float64")
    bags, fold_draws = [], []
    for k, (train, _test) in enumerate(splits):
        draws, seeds = _draws(start[train], end[train], n_bars, n_trees, seed + k)
        draws = [train[d] for d in draws]
        fold_draws.append((draws, seeds))
        bags.append(_fit_bag(arr, y, w, draws, seeds, params))
    return bags, fold_draws


def sfi(x: pd.DataFrame, y: np.ndarray, w: np.ndarray, splits, fold_draws) -> pd.DataFrame:
    """AFML Snippet 8.4: OOS weighted neg log loss of a classifier fit on each feature alone (same bootstrap draws)."""
    out = {}
    for f in x.columns:
        a = x[[f]].to_numpy(dtype="float64")
        scores = []
        for (train, test), (draws, seeds) in zip(splits, fold_draws):
            bag = _fit_bag(a, y, w, draws, seeds)
            scores.append(_neg_log_loss(y[test], bag.predict_proba(a[test]), w[test]))
        out[f] = scores
    return _mean_se(pd.DataFrame(out))


def ortho_feats(x: pd.DataFrame, var_thres: float = 0.95) -> tuple[pd.DataFrame, np.ndarray]:
    """AFML Snippet 8.5 (p.119): standardise, eigen-decompose Z'Z, keep the PCs explaining var_thres of the variance."""
    z = (x - x.mean()) / x.std().replace(0.0, 1.0)
    dot = np.dot(z.T, z)
    e_val, e_vec = np.linalg.eigh(dot)
    order = np.argsort(e_val)[::-1]
    e_val, e_vec = e_val[order], e_vec[:, order]
    dim = int(np.searchsorted(np.cumsum(e_val) / e_val.sum(), var_thres))
    e_val, e_vec = e_val[: dim + 1], e_vec[:, : dim + 1]
    p = pd.DataFrame(np.dot(z.to_numpy(), e_vec), index=x.index, columns=[f"PC_{i + 1}" for i in range(dim + 1)])
    return p, e_val


def pca_kendall(x: pd.DataFrame, y: np.ndarray, w: np.ndarray, start, end, n_bars, n_trees: int = 1000,
                seed: int = 0) -> dict[str, Any]:
    """AFML s.8.4.2 (pp.118-121, Fig 8.1, Snippet 8.6): MDI of the orthogonal features (PCs) vs their PCA rank;
    weighted Kendall's tau of MDI against the inverse PCA rank. Near 1 = label-based importance agrees with the
    label-free PCA ranking (evidence against overfitting). Diagnostic only (MLAM p.84 caveats on PCA)."""
    p, e_val = ortho_feats(x)
    draws, seeds = _draws(start, end, n_bars, n_trees, seed)
    bag = _fit_bag(p.to_numpy(dtype="float64"), y, w, draws, seeds)
    imp = mdi([bag], list(p.columns))["mean"].fillna(0.0).to_numpy(dtype="float64")
    pc_rank = np.arange(1, len(imp) + 1, dtype="float64")
    tau = float(weightedtau(imp, pc_rank**-1.0)[0])
    pearson = float(np.corrcoef(np.log(e_val), np.log(np.clip(imp, EPS, None)))[0, 1])
    return {"components_95pct": int(len(e_val)), "weighted_kendall_tau": round(tau, 4),
            "pearson_log_eigenvalue_vs_log_mdi": round(pearson, 4)}


# =============================================================================
# Selection rules (owner to choose; all reported)
# =============================================================================
def select(clustered_mda: pd.DataFrame, clustered_mdi: pd.DataFrame, clusters: dict[int, list[str]], n_features: int,
           z: float = 1.645) -> dict[str, list[str]]:
    keep: dict[str, list[str]] = {"A_mda_positive": [], "B_mda_significant": [], "C_mdi_above_null": []}
    for cid, members in clusters.items():
        key = f"C_{cid}"
        m, se = clustered_mda.loc[key, "mean"], clustered_mda.loc[key, "std"]
        if m > 0:  # MLAM p.89: a cluster with essentially zero importance is discarded
            keep["A_mda_positive"] += members
        if m - z * se > 0:  # MLAM p.79: CLT significance test against a user-defined null
            keep["B_mda_significant"] += members
        if clustered_mdi.loc[key, "mean"] > len(members) / n_features:  # AFML p.125: 1/F per feature under the null
            keep["C_mdi_above_null"] += members
    return keep


def cluster_summary(clusters: dict[int, list[str]], silh: pd.Series) -> dict[str, Any]:
    sizes = sorted((len(m) for m in clusters.values()), reverse=True)
    return {"clusters": len(clusters), "sizes": sizes, "mean_silhouette": round(float(silh.mean()), 4),
            "cluster_quality": round(_quality(silh.to_numpy()), 4)}


def get_test_data(n_features: int = 40, n_informative: int = 5, n_redundant: int = 30, n_samples: int = 10000,
                  random_state: int = 0, sigma_std: float = 0.1) -> tuple[pd.DataFrame, pd.Series]:
    """MLAM Snippet 6.1 (p.77): informative (I_), redundant (R_ = I_ + noise) and noise (N_) features."""
    from sklearn.datasets import make_classification

    rng = np.random.RandomState(random_state)
    x, y = make_classification(n_samples=n_samples, n_features=n_features - n_redundant, n_informative=n_informative,
                               n_redundant=0, shuffle=False, random_state=random_state)
    cols = [f"I_{i}" for i in range(n_informative)] + [f"N_{i}" for i in range(n_features - n_informative - n_redundant)]
    x = pd.DataFrame(x, columns=cols)
    for k, j in enumerate(rng.choice(range(n_informative), size=n_redundant)):
        x[f"R_{k}"] = x[f"I_{j}"] + rng.normal(size=x.shape[0]) * sigma_std
    return x, pd.Series(y)


# =============================================================================
# Step 01 entry point (owner decisions 2026-10-24: VI clusters, raw features (D042), rule B, mwfl 0.05 (D041), 1,000 trees,
# purged + embargoed 10-fold (D039), single pass (MLAM p.89: discard clusters with essentially zero importance))
# =============================================================================
STEP01_N_TREES = 1000
STEP01_N_SPLITS = 10
STEP01_Z = 1.645
STEP01_METHOD = "vi"
# MDA shuffles per fold, averaged (approved deviation D045; MLAM Snippet 6.5 p.87 shuffles once). Shuffle-stability test
# 2026-10-24: same rule-B selection across 10 seeds 70-100% with 1 shuffle, 100% on every edge with 5.
STEP01_N_SHUFFLES = 5
# ONC search (D077, MLAM Ch.4 tests T5/T8/T9 2026-11-02): Snippet 4.1 keeps the highest-quality partition over its
# initialisations, but at n_init 10-100 a single run does not reach that optimum on our feature matrices (cluster count
# 2-26 at 10; kept feature set seed-dependent at 50). Step 01 runs onc_top at STEP01_ONC_RESTARTS seeds (seed .. seed+19),
# n_init STEP01_ONC_N_INIT each (1,000 initialisations), and keeps the highest-q partition, ties to the lowest seed:
# the same kept feature set in every 20-seed block on all 6 edges (T9).
STEP01_ONC_N_INIT = 50
STEP01_ONC_RESTARTS = 20


def _onc_restart_job(a: tuple) -> tuple:
    obs, n_init, seed = a
    clusters, silh = onc_top(obs, max_k=None, n_init=n_init, seed=seed)
    return _quality(silh.to_numpy()), seed, clusters, silh


def onc_best_of_restarts(obs: pd.DataFrame, n_restarts: int = STEP01_ONC_RESTARTS, n_init: int = STEP01_ONC_N_INIT,
                         seed: int = 0, workers: int = 1) -> tuple[dict[int, list], pd.Series, dict[str, Any]]:
    """onc_top at seeds seed .. seed + n_restarts - 1; keep the highest silhouette quality q (Snippet 4.1's objective),
    ties to the lowest seed (D077)."""
    from concurrent.futures import ProcessPoolExecutor

    jobs = [(obs, int(n_init), int(seed) + r) for r in range(int(n_restarts))]
    if workers and workers > 1:
        with ProcessPoolExecutor(max_workers=int(workers)) as ex:
            res = list(ex.map(_onc_restart_job, jobs))
    else:
        res = [_onc_restart_job(j) for j in jobs]
    q, best_seed, clusters, silh = max(res, key=lambda r: (r[0], -r[1]))
    return clusters, silh, {"onc_restarts": int(n_restarts), "onc_n_init": int(n_init), "onc_best_seed": int(best_seed),
                            "onc_best_q": round(float(q), 4),
                            "onc_q_by_seed": {int(r[1]): round(float(r[0]), 4) for r in res}}


def _step01_job(a: dict) -> dict:
    """One fold (test is None -> full-data fit for MDI). Bootstrap draws computed once per job."""
    train, test = a["train"], a["test"]
    draws, seeds = _draws(a["start"][train], a["end"][train], a["n_bars"], a["n_trees"], a["seed"])
    draws = [train[d] for d in draws]
    x, y, w = a["x"], a["y"], a["w"]
    trees = [DecisionTreeClassifier(**TREE_PARAMS, random_state=s).fit(x[d], y[d], sample_weight=w[d]) for d, s in zip(draws, seeds)]
    if test is None:
        return {"importances": np.array([t.feature_importances_ for t in trees])}

    def prob(arr):
        p = np.zeros(len(arr))
        for t in trees:
            p += t.predict_proba(arr)[:, list(t.classes_).index(1)] if 1 in t.classes_ else 0.0
        return p / len(trees)

    rng = np.random.default_rng(a["seed"] + 7)
    x1 = x[test]
    p1 = prob(x1)
    s1 = {}
    for g, cols in a["groups"].items():
        scores = []
        for _ in range(int(a.get("n_shuffles", 1))):  # D045: average over n_shuffles independent shuffles
            x1_ = x1.copy()
            for c in cols:
                x1_[:, c] = x1_[rng.permutation(len(x1_)), c]
            scores.append(_neg_log_loss(y[test], prob(x1_), w[test]))
        s1[g] = float(np.mean(scores))
    return {"scr0": _neg_log_loss(y[test], p1, w[test]), "scr1": s1, "prob": p1}


def select_features_clustered(x: pd.DataFrame, y: np.ndarray, w: np.ndarray, t0: np.ndarray, t1: np.ndarray,
                              start: np.ndarray, end: np.ndarray, n_bars: int, embargo: int, *, workers: int = 4,
                              seed: int = 0, n_trees: int = STEP01_N_TREES, n_splits: int = STEP01_N_SPLITS,
                              method: str = STEP01_METHOD, z: float = STEP01_Z,
                              n_shuffles: int = STEP01_N_SHUFFLES) -> dict[str, Any]:
    """Cluster the candidate features (MLAM Ch.4 ONC on the method's observation matrix), measure clustered MDA (OOS) and
    clustered MDI (in-sample), keep clusters whose MDA mean - z * se > 0 (rule B). Folds run in parallel processes."""
    from concurrent.futures import ProcessPoolExecutor

    from sklearn.metrics import roc_auc_score

    feats = list(x.columns)
    obs, diag = observation_matrix(x, method)
    clusters, silh, onc_search = onc_best_of_restarts(obs, seed=seed, workers=workers)
    col = {c: i for i, c in enumerate(feats)}
    groups = {f"C_{i}": [col[c] for c in m] for i, m in clusters.items()}
    splits = purged_kfold_splits(t0, t1, n_splits, embargo)
    base = dict(x=x.to_numpy(dtype="float64"), y=np.asarray(y), w=np.asarray(w, dtype="float64"), start=np.asarray(start),
                end=np.asarray(end), n_bars=int(n_bars), groups=groups, n_trees=int(n_trees), n_shuffles=int(n_shuffles))
    jobs = [dict(base, train=tr, test=te, seed=seed + k) for k, (tr, te) in enumerate(splits)]
    jobs.append(dict(base, train=np.arange(len(x)), test=None, seed=seed + 99))
    if workers and workers > 1:
        with ProcessPoolExecutor(max_workers=int(workers)) as ex:
            res = list(ex.map(_step01_job, jobs))
    else:
        res = [_step01_job(j) for j in jobs]
    folds, full = res[:-1], res[-1]
    s0 = pd.Series([f["scr0"] for f in folds])
    s1 = pd.DataFrame([f["scr1"] for f in folds])
    imp = (-s1).add(s0, axis=0) / (-s1)
    mda = _mean_se(imp)
    imps = pd.DataFrame(full["importances"], columns=feats).replace(0, np.nan)
    cmdi = _mean_se(pd.DataFrame({f"C_{i}": imps[m].sum(axis=1) for i, m in clusters.items()}))
    cmdi = cmdi / cmdi["mean"].sum()
    rows, selected = [], []
    for i, m in clusters.items():
        key = f"C_{i}"
        mean, se = float(mda.loc[key, "mean"]), float(mda.loc[key, "std"])
        kept = bool(mean - z * se > 0)
        if kept:
            selected += m
        rows.append({"cluster_id": int(i), "n_features": len(m), "mda_mean": mean, "mda_se": se,
                     "mda_t": mean / se if se > 0 else 0.0, "mdi_mean": float(cmdi.loc[key, "mean"]),
                     "mdi_null_share": len(m) / len(feats), "kept_rule_B": kept, "features": list(m)})
    p = np.full(len(x), np.nan)
    for (_tr, te), f in zip(splits, folds):
        p[te] = f["prob"]
    ordered = [f for f in feats if f in set(selected)]
    return {
        "version": CFI_VERSION, "method": method, "observation_matrix": diag, "selection_rule": f"B: clustered MDA mean - {z} x se > 0",
        "estimator": {**TREE_PARAMS, "n_estimators": int(n_trees), "bootstrap": "sequential, round(avgU x n) rows"},
        "cv": {"scheme": "purged + embargoed k-fold (label t1)", "n_splits": int(n_splits), "embargo_observations": int(embargo)},
        "mda_shuffles_per_fold": int(n_shuffles),
        "cluster_summary": {**cluster_summary(clusters, silh), "onc_search": onc_search}, "clusters": rows, "selected_features": ordered,
        "oos_neg_log_loss_weighted": float(s0.mean()), "oos_auc": float(roc_auc_score(np.asarray(y), p)),
    }
