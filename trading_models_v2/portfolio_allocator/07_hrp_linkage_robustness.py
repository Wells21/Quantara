from __future__ import annotations

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import pdist

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    class _NoOpTqdm:
        def __init__(self, iterable=None, *args, **kwargs):
            self.iterable = iterable
        def __iter__(self):
            return iter(self.iterable) if self.iterable is not None else iter(())
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def update(self, n=1):
            return None
    tqdm = _NoOpTqdm

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


from shared.validation.v3_semantic_contract import PAYOFF_CONTEXT_VERSION  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.models.denoise_onc import (  # noqa: E402
    DenoiseONCError,
    corr_to_cov,
    denoised_corr_constant_residual_eigenvalue,
    detone_corr,
    find_max_eval,
    get_pca,
)

SCHEMA_VERSION = "v2.2"
PIPELINE_MODE = "v2_manifest_driven_calibrated_afml_compound_velocity"
RETURN_STREAMS_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "return_streams"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs"
INPUT_PROFILE_SELECTION = RETURN_STREAMS_DIR / "profile_representative_selection.csv"
INPUT_RETURN_STREAMS_MANIFEST = RETURN_STREAMS_DIR / "return_streams_manifest.json"
INPUT_BROKER_METADATA_SUMMARY = RETURN_STREAMS_DIR / "representative_broker_minlot_metadata_summary.csv"

BROKER_MINLOT_VALIDATION_REQUIRED = True
BROKER_MIN_VALID_RISK_ROW_RATE = 1.0 - 1e-9
# Broker-aware threshold diagnostics are intentionally OPTIONAL at Step 07.
# Step 07 must fail only if Step 06 did not validate broker min-lot metadata.
# Different 04C versions may expose slightly different diagnostic column names,
# so this script carries any known columns that are present instead of making
# the HRP stage depend on a specific diagnostic naming set.
BROKER_PROFILE_METADATA_COLUMNS = [
    # Optimized drawdown-aware 04C column names.
    "broker_optimized_drawdown_adjusted_compound_velocity",
    "broker_optimized_f",
    "broker_optimized_f_search_upper",
    "broker_optimized_f_search_points",
    "broker_optimized_f_search_upper_reason",
    "broker_optimized_objective",
    "broker_optimized_log_growth",
    "broker_optimized_compound_velocity",
    "broker_optimized_compound_velocity_calendar",
    "broker_optimized_compound_velocity_active",
    "broker_optimized_drawdown_adjusted_log_growth",
    "broker_optimized_max_drawdown_log",
    "broker_optimized_max_drawdown_abs_log",
    "broker_optimized_max_drawdown_pct",
    "broker_optimized_equity_return_proxy",
    "broker_optimized_expected_active_days_to_target",
    "broker_optimized_expected_calendar_days_to_target",
    "broker_optimized_bankruptcy_margin_min",
    "broker_optimized_survival_feasible",

    # Broker executable risk diagnostics from 04C.
    "broker_valid_risk_row_count",
    "broker_invalid_risk_row_count",
    "broker_valid_risk_row_rate",
    "broker_min_lot_binding_trade_count",
    "broker_min_lot_binding_trade_rate",
    "broker_median_target_risk_amount",
    "broker_median_actual_risk_amount",
    "broker_p90_actual_risk_amount",
    "broker_max_actual_risk_amount",
    "broker_median_risk_inflation",
    "broker_p90_risk_inflation",
    "broker_max_risk_inflation",
    "broker_mean_actual_risk_pct",
    "broker_median_actual_risk_pct",
    "broker_p90_actual_risk_pct",
    "broker_max_actual_risk_pct",
    "broker_mean_target_risk_pct",
    "broker_median_target_risk_pct",
    "broker_p90_target_risk_pct",
    "broker_max_target_risk_pct",
    "broker_risk_metadata_status_summary",

    # Backward-compatible aliases from earlier drafts. Carry if present, but do not require.
    "broker_optimal_f",
    "broker_optimized_calmar_velocity",
    "broker_optimized_compound_power",
    "broker_actual_risk_pct_mean",
    "broker_actual_risk_pct_p90",
    "broker_actual_risk_pct_max",
]
BROKER_PROFILE_METADATA_OPTIONAL_COLUMNS = [
    "compound_velocity_theoretical",
    "compound_velocity_broker_executable",
    "compound_velocity_score",
    "compound_velocity_score_basis",
    "expected_calendar_days_to_target",
    "expected_active_days_to_target",
    "expected_calendar_days_to_target_broker_executable",
    "expected_active_days_to_target_broker_executable",
]

UNIVERSES = ["core", "watchlist", "core_plus_watchlist"]
STEP08_ELIGIBLE_UNIVERSES = ["core", "core_plus_watchlist"]
PROFILE_ROLES = ["compound_velocity"]
RETURN_MODES = {
    "raw_r": {"suffix": "", "description": "Primary structural HRP mode using daily executed_r streams.", "step08_primary": True},
    "afml_weighted_r": {"suffix": "_afml_weighted_r", "description": "Diagnostic HRP mode using executed_r * afml_bet_size streams.", "step08_primary": False},
}
LINKAGE_METHODS = ["single", "complete", "average", "weighted", "ward"]
CANONICAL_METHOD = "single"
CORR_METHOD = "pearson"
MIN_NONZERO_DAYS = 20
DROP_ZERO_VARIANCE_COLUMNS = True
MIN_COLUMNS_FOR_HRP = 2
CONCENTRATION_WARN_TOP_WEIGHT = 0.45
CONCENTRATION_WARN_TOP_ASSET_WEIGHT = 0.70
CONCENTRATION_WARN_EFFECTIVE_N = 3.0
EPS = 1e-12

# MLAM Ch.2 "Denoising and Detoning" (book p.24-37) (AFML Ch.16 walkthrough s.4P.3, owner approved 2026-10-30):
# - Denoise exactly as Snippets 2.4-2.5 (p.27-29): fit the Marcenko-Pastur noise variance (findMaxEval, whose
#   fallback when the fit fails is var=1), nFacts = the count of eigenvalues above lambda+ with no floor of 1, then
#   the constant residual eigenvalue method. With nFacts=0 the book's formula returns the identity matrix (every
#   correlation is noise), so HRP reduces to the inverse-variance portfolio.
# - Detone (s.2.6, p.30) the clustering matrix only if a market component exists ("a hypothesis that can be tested
#   statistically", p.30): the first eigenvalue exceeds lambda+ AND, in at least MARKET_TEST_MIN_SAME_SIGN_SHARE of
#   MARKET_TEST_BOOTSTRAPS iid row-bootstrap resamples of the returns, every loading of the first eigenvector has
#   the same sign (p.30: market loadings ~N^-1/2 each). The book names no test; the bootstrap and the 95% cutoff are
#   the project's operationalisation (owner approved 2026-10-30), because a single-sample sign check reports a
#   market component for two equal-strength clusters. A detoned matrix is singular (p.32), so it feeds distance /
#   linkage / quasi-diagonalisation only, never the recursive-bisection risk weighting.
DENOISE_METHOD = "constant_residual_eigenvalue"  # MLAM's canonical/recommended method, p.29
DENOISE_KDE_BWIDTH = 0.01
DETONE_MARKET_COMPONENTS = 1
DENOISE_RULE = "mlam_ch2_book_v2"
MARKET_TEST_BOOTSTRAPS = 1000
MARKET_TEST_SEED = 0
MARKET_TEST_MIN_SAME_SIGN_SHARE = 0.95


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        if abs(b) < EPS or not np.isfinite(b):
            return np.nan
        return float(a) / b
    except Exception:
        return np.nan


def coerce_if_fully_numeric(series: pd.Series) -> pd.Series:
    """Convert a metadata column only when every populated value is numeric."""
    converted = pd.to_numeric(series, errors="coerce")
    populated = series.notna()
    if int(converted[populated].notna().sum()) == int(populated.sum()):
        return converted
    return series


def pct_rank(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").astype(float)
    valid = s.replace([np.inf, -np.inf], np.nan).dropna()
    if valid.empty:
        return pd.Series(0.5, index=s.index)
    mn, mx = valid.min(), valid.max()
    if abs(mx - mn) < EPS:
        return pd.Series(0.5, index=s.index)
    score = (s - mn) / (mx - mn)
    return (score if higher_is_better else 1.0 - score).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def max_drawdown_from_series(x: pd.Series) -> float:
    x = pd.to_numeric(x, errors="coerce").fillna(0.0)
    equity = x.cumsum()
    dd = equity - equity.cummax()
    return float(dd.min()) if len(dd) else 0.0


def portfolio_metrics(x: pd.Series) -> dict[str, Any]:
    x = pd.to_numeric(x, errors="coerce").fillna(0.0)
    total = float(x.sum())
    mean = float(x.mean()) if len(x) else np.nan
    std = float(x.std(ddof=1)) if len(x) > 1 else np.nan
    max_dd = max_drawdown_from_series(x)
    pos = int((x > 0).sum())
    neg = int((x < 0).sum())
    downside = x[x < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else np.nan
    return {
        "total_r": total,
        "mean_daily_r": mean,
        "median_daily_r": float(x.median()) if len(x) else np.nan,
        "std_daily_r": std,
        "sharpe_like_daily": safe_div(mean, std),
        "sortino_like_daily": safe_div(mean, downside_std),
        "max_drawdown_r": max_dd,
        "calmar_r": safe_div(total, abs(max_dd)) if max_dd < 0 else np.nan,
        "nonzero_days": int((x != 0).sum()),
        "positive_days": pos,
        "negative_days": neg,
        "zero_days": int((x == 0).sum()),
        "positive_day_rate": safe_div(pos, pos + neg),
    }


def equal_weight_metrics(returns: pd.DataFrame) -> dict[str, Any]:
    weights = pd.Series(1.0 / returns.shape[1], index=returns.columns)
    return {f"equal_weight_{k}": v for k, v in portfolio_metrics(returns.mul(weights, axis=1).sum(axis=1)).items()}


def clean_corr(corr: pd.DataFrame) -> pd.DataFrame:
    corr = corr.copy().replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-1.0, 1.0)
    corr = (corr + corr.T) / 2.0
    np.fill_diagonal(corr.values, 1.0)
    return corr


def clean_cov(cov: pd.DataFrame) -> pd.DataFrame:
    cov = cov.copy().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    cov = (cov + cov.T) / 2.0
    for i in range(cov.shape[0]):
        if cov.iat[i, i] <= EPS:
            cov.iat[i, i] = EPS
    return cov


def correl_distance(corr: pd.DataFrame) -> pd.DataFrame:
    dist = np.sqrt((1.0 - corr) / 2.0).clip(0.0, 1.0)
    dist = (dist + dist.T) / 2.0
    np.fill_diagonal(dist.values, 0.0)
    return dist


def get_quasi_diag(linkage_matrix: np.ndarray) -> list[int]:
    link = linkage_matrix.astype(int)
    sort_ix = pd.Series([link[-1, 0], link[-1, 1]])
    num_items = int(link[-1, 3])
    while sort_ix.max() >= num_items:
        sort_ix.index = range(0, sort_ix.shape[0] * 2, 2)
        df0 = sort_ix[sort_ix >= num_items]
        i = df0.index
        j = df0.values - num_items
        sort_ix[i] = link[j, 0]
        df1 = pd.Series(link[j, 1], index=i + 1)
        sort_ix = pd.concat([sort_ix, df1]).sort_index()
        sort_ix.index = range(sort_ix.shape[0])
    return sort_ix.astype(int).tolist()


def get_inverse_variance_portfolio(cov: pd.DataFrame) -> pd.Series:
    diag = np.diag(cov.values).astype(float)
    diag = np.where(diag <= EPS, EPS, diag)
    inv = 1.0 / diag
    return pd.Series(inv / inv.sum(), index=cov.index)


def get_cluster_variance(cov: pd.DataFrame, cluster_items: list[str]) -> float:
    cluster_cov = cov.loc[cluster_items, cluster_items]
    weights = get_inverse_variance_portfolio(cluster_cov).values.reshape(-1, 1)
    variance = (weights.T @ cluster_cov.values @ weights).item()
    return max(float(variance), EPS)


def recursive_bisection(cov: pd.DataFrame, sorted_items: list[str]) -> pd.Series:
    weights = pd.Series(1.0, index=sorted_items)
    clusters = [sorted_items]
    while clusters:
        new_clusters = []
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            split = len(cluster) // 2
            left, right = cluster[:split], cluster[split:]
            left_var = get_cluster_variance(cov, left)
            right_var = get_cluster_variance(cov, right)
            alpha = 1.0 - left_var / (left_var + right_var)
            weights[left] *= alpha
            weights[right] *= 1.0 - alpha
            new_clusters.extend([left, right])
        clusters = new_clusters
    return weights / weights.sum()


def first_eigenvector_same_sign_share(
    returns: Any, n_boot: int = MARKET_TEST_BOOTSTRAPS, seed: int = MARKET_TEST_SEED
) -> float:
    """Share of iid row-bootstrap resamples (T rows drawn with replacement) whose
    Pearson correlation matrix has a first eigenvector with all loadings of one
    sign. A zero-variance column in a resample gets zero correlation (as
    clean_corr does), which makes its loading zero, so that resample counts
    as mixed-sign."""
    x = np.asarray(returns, dtype="float64")
    n_rows, n_cols = x.shape
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_rows, size=(n_boot, n_rows))
    flat = (idx + np.arange(n_boot)[:, None] * n_rows).ravel()
    counts = np.bincount(flat, minlength=n_boot * n_rows).reshape(n_boot, n_rows).astype("float64")
    mean = counts @ x / n_rows
    second = (counts @ (x[:, :, None] * x[:, None, :]).reshape(n_rows, n_cols * n_cols)).reshape(n_boot, n_cols, n_cols)
    cov = second / n_rows - mean[:, :, None] * mean[:, None, :]
    var = np.einsum("bii->bi", cov)
    sd = np.sqrt(np.clip(var, 0.0, None))
    valid = sd > EPS
    denom = sd[:, :, None] * sd[:, None, :]
    corr = np.where(valid[:, :, None] & valid[:, None, :], cov / np.where(denom > 0, denom, 1.0), 0.0)
    corr = np.clip(corr, -1.0, 1.0)
    corr[:, np.arange(n_cols), np.arange(n_cols)] = 1.0
    _, vecs = np.linalg.eigh(corr)
    first = vecs[:, :, -1]
    same = np.all(first > 0, axis=1) | np.all(first < 0, axis=1)
    return float(same.mean())


def denoise_and_detone(raw_corr: pd.DataFrame, cov: pd.DataFrame, n_obs: int, returns: Any) -> dict[str, Any]:
    """MLAM Ch.2 (p.24-37) as in the rule above DENOISE_METHOD. Returns the
    correlation/covariance for risk weighting (denoised, Snippets 2.4-2.5) and
    the correlation for clustering (additionally detoned only if a market
    component exists; `returns` are the T x N observations behind raw_corr,
    used for the bootstrap market test), plus diagnostics recording every
    test, so a skipped step is visible rather than silent."""
    n_assets = raw_corr.shape[0]
    diagnostics: dict[str, Any] = {"applied": False, "detoned": False, "rule": DENOISE_RULE}
    corr_for_weights, corr_for_clustering, cov_for_weights = raw_corr, raw_corr, cov

    if n_obs <= n_assets:
        diagnostics["reason"] = (
            f"insufficient observations for Marcenko-Pastur fit (T={n_obs}, N={n_assets}; "
            "requires T>N)"
        )
    else:
        try:
            q = float(n_obs) / float(n_assets)
            eigenvalues, eigenvectors = get_pca(raw_corr.values.astype("float64"))
            raw_eigenvalues = np.diag(eigenvalues)
            e_max, fitted_var = find_max_eval(raw_eigenvalues, q, DENOISE_KDE_BWIDTH)
            # Snippet 2.4 verbatim: nFacts0 = eVal0.shape[0] - np.diag(eVal0)[::-1].searchsorted(eMax0)
            n_facts = int(raw_eigenvalues.shape[0] - raw_eigenvalues[::-1].searchsorted(e_max))
            first_loadings = eigenvectors[:, 0]
            same_sign_share = first_eigenvector_same_sign_share(returns)
            market_component = bool(
                raw_eigenvalues[0] > e_max and same_sign_share >= MARKET_TEST_MIN_SAME_SIGN_SHARE
            )
            denoised = denoised_corr_constant_residual_eigenvalue(eigenvalues, eigenvectors, n_facts)
            denoised_df = clean_corr(pd.DataFrame(denoised, index=raw_corr.index, columns=raw_corr.columns))
            std = np.sqrt(np.clip(np.diag(cov.values), EPS, None))
            cov_for_weights = clean_cov(pd.DataFrame(
                corr_to_cov(denoised_df.values, std), index=cov.index, columns=cov.columns
            ))
            corr_for_weights = denoised_df
            corr_for_clustering = denoised_df
            d_eval, d_evec = get_pca(denoised)
            d_eigenvalues = np.diag(d_eval)
            diagnostics.update({
                "applied": True,
                "method": DENOISE_METHOD,
                "q_n_over_t": q,
                "fitted_noise_variance": float(fitted_var),
                "lambda_plus": float(e_max),
                "raw_eigenvalues": [float(x) for x in raw_eigenvalues],
                "n_signal_eigenvalues": n_facts,
                "first_eigenvector_loadings": [float(x) for x in first_loadings],
                "market_test_bootstraps": MARKET_TEST_BOOTSTRAPS,
                "market_test_seed": MARKET_TEST_SEED,
                "market_test_min_same_sign_share": MARKET_TEST_MIN_SAME_SIGN_SHARE,
                "first_eigenvector_same_sign_share": same_sign_share,
                "market_component_exists": market_component,
                "market_components_removed": 0,
                "condition_number_before": float(raw_eigenvalues.max() / max(raw_eigenvalues.min(), 1e-16)),
                "condition_number_after": float(d_eigenvalues.max() / max(d_eigenvalues.min(), 1e-16)),
            })
            if n_facts == 0:
                diagnostics["reason"] = "no eigenvalue above lambda+: denoised matrix is the identity (HRP = IVP)"
            if market_component and DETONE_MARKET_COMPONENTS <= n_facts:
                corr_for_clustering = clean_corr(pd.DataFrame(
                    detone_corr(denoised, d_eval, d_evec, DETONE_MARKET_COMPONENTS),
                    index=raw_corr.index, columns=raw_corr.columns,
                ))
                diagnostics["detoned"] = True
                diagnostics["market_components_removed"] = DETONE_MARKET_COMPONENTS
        except DenoiseONCError as exc:
            corr_for_weights, corr_for_clustering, cov_for_weights = raw_corr, raw_corr, cov
            diagnostics = {"applied": False, "detoned": False, "rule": DENOISE_RULE, "error": str(exc)}

    return {
        "corr_for_weights": corr_for_weights,
        "corr_for_clustering": corr_for_clustering,
        "cov_for_weights": cov_for_weights,
        "diagnostics": diagnostics,
    }


def hrp_allocation(returns: pd.DataFrame, linkage_method: str) -> dict[str, Any]:
    cov = clean_cov(returns.cov())
    raw_corr = clean_corr(returns.corr(method=CORR_METHOD))
    denoise_result = denoise_and_detone(raw_corr, cov, int(returns.shape[0]), returns.to_numpy(dtype="float64"))
    corr = denoise_result["corr_for_weights"]
    corr_for_clustering = denoise_result["corr_for_clustering"]
    cov = denoise_result["cov_for_weights"]
    dist = correl_distance(corr_for_clustering)
    # AFML Snippet 16.4 calls sch.linkage(dist, 'single') on the square matrix D, so scipy treats its rows as
    # observations: the tree is built on d~ (s.16.4.1), the Euclidean distance between the columns of D.
    link = linkage(pdist(dist.values, metric="euclidean"), method=linkage_method)
    sorted_labels = corr_for_clustering.index[get_quasi_diag(link)].tolist()
    weights = recursive_bisection(cov, sorted_labels).reindex(returns.columns).fillna(0.0)
    weights = weights / weights.sum()
    return {
        "weights": weights,
        "cov": cov,
        "corr": corr,
        "raw_corr": raw_corr,
        "corr_for_clustering": corr_for_clustering,
        "dist": dist,
        "linkage": link,
        "sorted_labels": sorted_labels,
        "denoise_diagnostics": denoise_result["diagnostics"],
    }


def matrix_path(universe: str, role: str, return_mode: str) -> Path:
    return RETURN_STREAMS_DIR / f"profile_return_streams_daily_{universe}_{role}{RETURN_MODES[return_mode]['suffix']}.csv"


def load_profile_matrix(universe: str, role: str, return_mode: str) -> pd.DataFrame:
    path = matrix_path(universe, role, return_mode)
    if not path.exists():
        raise FileNotFoundError(path)
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    if "date" not in df.columns:
        raise ValueError(f"{path} must contain a date column")
    df["date"] = to_datetime_iso(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def clean_return_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, keep_cols = [], []
    for col in df.columns:
        x = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        nonzero = int((x != 0).sum())
        variance = float(x.var(ddof=1)) if len(x) > 1 else 0.0
        total = float(x.sum())
        keep, reason = True, "kept"
        if nonzero < MIN_NONZERO_DAYS:
            keep, reason = False, f"nonzero_days_below_{MIN_NONZERO_DAYS}"
        if DROP_ZERO_VARIANCE_COLUMNS and variance <= EPS:
            keep, reason = False, "zero_or_near_zero_variance"
        if keep:
            keep_cols.append(col)
        rows.append({"column": col, "nonzero_days": nonzero, "variance": variance, "total_r": total, "kept_for_hrp": keep, "reason": reason})
    report = pd.DataFrame(rows)
    cleaned = df[keep_cols].copy()
    if cleaned.shape[1] < MIN_COLUMNS_FOR_HRP:
        raise ValueError(f"Need at least {MIN_COLUMNS_FOR_HRP} usable return streams for HRP after cleaning. Only {cleaned.shape[1]} available.")
    return cleaned, report


def load_profile_selection() -> pd.DataFrame:
    if not INPUT_PROFILE_SELECTION.exists():
        raise FileNotFoundError(f"Missing profile representative selection: {INPUT_PROFILE_SELECTION}")
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(INPUT_PROFILE_SELECTION, float_precision="round_trip")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""

    # Only the identity columns are required here. Broker/min-lot validity is
    # validated from Step 06's broker metadata summary, not from optional 04C
    # diagnostic columns in profile_representative_selection.csv.
    required = [
        "profile_universe", "profile_role", "edge_id", "representative_id",
        "asset", "family", "side_name", "threshold", "edge_classification",
        "representative_roles", "primary_representative_role",
        "avg_afml_bet_size", "avg_calibrated_probability",
        "payoff_context_version", "payoff_context_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"profile_representative_selection missing required identity columns: {missing}")
    empty_context = df["payoff_context_id"].astype(str).str.strip().isin({"", "nan", "None"})
    if empty_context.any():
        raise ValueError(
            f"profile_representative_selection contains {int(empty_context.sum())} empty payoff_context_id rows."
        )
    if not df["payoff_context_version"].astype(str).eq(PAYOFF_CONTEXT_VERSION).all():
        raise ValueError("profile_representative_selection contains stale payoff_context_version rows.")

    # Normalize numeric columns that are present. Do not require optional broker
    # diagnostics; carry them forward opportunistically in weight_report().
    numeric_candidates = [
        "threshold", "avg_afml_bet_size", "avg_calibrated_probability", "side_value",
        *BROKER_PROFILE_METADATA_COLUMNS, *BROKER_PROFILE_METADATA_OPTIONAL_COLUMNS,
    ]
    for col in numeric_candidates:
        if col in df.columns and col not in {
            "broker_optimized_f_search_upper_reason",
            "broker_optimized_objective",
            "broker_risk_metadata_status_summary",
            "compound_velocity_score_basis",
        }:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Provide one backward-compatible alias when the optimized 04C name is present.
    if "broker_optimal_f" not in df.columns and "broker_optimized_f" in df.columns:
        df["broker_optimal_f"] = df["broker_optimized_f"]

    return df


def load_return_streams_manifest() -> dict[str, Any]:
    return json.loads(INPUT_RETURN_STREAMS_MANIFEST.read_text(encoding="utf-8")) if INPUT_RETURN_STREAMS_MANIFEST.exists() else {}


def load_broker_metadata_summary() -> pd.DataFrame:
    if not INPUT_BROKER_METADATA_SUMMARY.exists():
        if BROKER_MINLOT_VALIDATION_REQUIRED:
            raise FileNotFoundError(
                "Step 07 requires the broker min-lot metadata summary written by patched Step 06. "
                f"Missing: {INPUT_BROKER_METADATA_SUMMARY}"
            )
        return pd.DataFrame()
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(INPUT_BROKER_METADATA_SUMMARY, float_precision="round_trip")
    if df.empty and BROKER_MINLOT_VALIDATION_REQUIRED:
        raise ValueError(f"Broker min-lot metadata summary is empty: {INPUT_BROKER_METADATA_SUMMARY}")
    for col in df.columns:
        if col not in {"representative_id", "edge_id", "asset", "family", "side_name", "broker_symbols", "broker_risk_status_summary"}:
            df[col] = coerce_if_fully_numeric(df[col])
    return df


def validate_broker_minlot_handoff(return_streams_manifest: dict[str, Any], broker_summary: pd.DataFrame) -> dict[str, Any]:
    if not BROKER_MINLOT_VALIDATION_REQUIRED:
        return {
            "broker_minlot_validation_required": False,
            "broker_minlot_ready": True,
            "broker_summary_rows": int(len(broker_summary)),
            "reason": "validation_disabled",
        }

    if broker_summary.empty:
        raise ValueError("Step 07 broker min-lot validation failed: broker metadata summary has no rows.")

    diagnostics: dict[str, Any] = {
        "broker_minlot_validation_required": True,
        "broker_minlot_ready": True,
        "broker_metadata_summary": str(INPUT_BROKER_METADATA_SUMMARY.relative_to(V2_ROOT)),
        "broker_summary_rows": int(len(broker_summary)),
        "return_streams_manifest_loaded": bool(return_streams_manifest),
    }

    if "broker_valid_risk_row_rate" in broker_summary.columns:
        valid_rate = pd.to_numeric(broker_summary["broker_valid_risk_row_rate"], errors="coerce")
        bad = broker_summary[valid_rate < BROKER_MIN_VALID_RISK_ROW_RATE]
        if not bad.empty:
            raise ValueError(
                "Step 07 broker min-lot validation failed: some representative streams are not 100% broker-risk valid.\n"
                + bad.head(30).to_string(index=False)
            )
        diagnostics["min_broker_valid_risk_row_rate"] = float(valid_rate.min())
    else:
        raise ValueError(
            "Step 07 broker min-lot validation failed: representative_broker_minlot_metadata_summary.csv "
            "is missing broker_valid_risk_row_rate."
        )

    positive_cols = [
        "broker_min_executable_risk_amount_median",
        "broker_min_executable_risk_amount_p90",
        "broker_min_executable_risk_amount_max",
        "broker_risk_per_1_lot_median",
        "broker_risk_per_min_lot_median",
    ]
    for col in positive_cols:
        if col in broker_summary.columns:
            vals = pd.to_numeric(broker_summary[col], errors="coerce")
            bad = broker_summary[vals <= 0]
            if not bad.empty:
                raise ValueError(
                    f"Step 07 broker min-lot validation failed: {col} must be positive for every representative.\n"
                    + bad.head(30).to_string(index=False)
                )
            diagnostics[f"{col}_min"] = float(vals.min())

    if "broker_symbols" in broker_summary.columns:
        diagnostics["broker_symbols"] = sorted(set("|".join(broker_summary["broker_symbols"].dropna().astype(str)).split("|")) - {""})

    manifest_outputs = return_streams_manifest.get("outputs", {}) if isinstance(return_streams_manifest, dict) else {}
    diagnostics["return_streams_manifest_mentions_broker_summary"] = any(
        "broker" in str(k).lower() or "broker" in str(v).lower()
        for k, v in manifest_outputs.items()
    )
    return diagnostics


def weight_report(weights: pd.Series, selection: pd.DataFrame, return_mode: str, universe: str, role: str, linkage_method: str) -> pd.DataFrame:
    sel = selection[(selection["profile_universe"] == universe) & (selection["profile_role"] == role)].copy()
    rows = []
    carry_cols = BROKER_PROFILE_METADATA_COLUMNS + BROKER_PROFILE_METADATA_OPTIONAL_COLUMNS
    for edge_id, weight in weights.sort_values(ascending=False).items():
        md = sel[sel["edge_id"] == edge_id]
        first = md.iloc[0].to_dict() if not md.empty else {}
        row = {
            "return_mode": return_mode,
            "universe": universe,
            "profile_role": role,
            "linkage_method": linkage_method,
            "eligible_for_step08": bool(return_mode == "raw_r" and universe in STEP08_ELIGIBLE_UNIVERSES),
            "edge_id": edge_id,
            "weight": float(weight),
            "asset": first.get("asset", ""),
            "family": first.get("family", ""),
            "side_name": first.get("side_name", ""),
            "side_value": first.get("side_value", np.nan),
            "threshold": first.get("threshold", np.nan),
            "representative_id": first.get("representative_id", ""),
            "edge_classification": first.get("edge_classification", ""),
            "representative_roles": first.get("representative_roles", ""),
            "primary_representative_role": first.get("primary_representative_role", ""),
            "avg_afml_bet_size": first.get("avg_afml_bet_size", np.nan),
            "avg_calibrated_probability": first.get("avg_calibrated_probability", np.nan),
            "payoff_context_version": first.get("payoff_context_version", ""),
            "payoff_context_id": first.get("payoff_context_id", ""),
            "edge_manifest_hash": first.get("edge_manifest_hash", ""),
        }
        for col in carry_cols:
            if col in first:
                row[col] = first.get(col)
        rows.append(row)
    return pd.DataFrame(rows)

def concentration_metrics(weights_df: pd.DataFrame) -> dict[str, Any]:
    w = pd.to_numeric(weights_df["weight"], errors="coerce").fillna(0.0)
    asset_w = weights_df.groupby("asset", dropna=False)["weight"].sum()
    family_w = weights_df.groupby("family", dropna=False)["weight"].sum()
    top_weight = float(w.max())
    effective_n = safe_div(1.0, float((w ** 2).sum()))
    top_asset_weight = float(asset_w.max()) if len(asset_w) else np.nan
    top_family_weight = float(family_w.max()) if len(family_w) else np.nan
    return {"top_weight": top_weight, "effective_n": effective_n, "top_asset_weight": top_asset_weight, "top_family_weight": top_family_weight, "asset_hhi": float((asset_w ** 2).sum()) if len(asset_w) else np.nan, "family_hhi": float((family_w ** 2).sum()) if len(family_w) else np.nan, "asset_count": int(asset_w.shape[0]), "family_count": int(family_w.shape[0]), "concentration_warning": bool(top_weight >= CONCENTRATION_WARN_TOP_WEIGHT or top_asset_weight >= CONCENTRATION_WARN_TOP_ASSET_WEIGHT or (pd.notna(effective_n) and effective_n <= CONCENTRATION_WARN_EFFECTIVE_N))}


def principal_component_concentration(returns: pd.DataFrame, weights: pd.Series) -> dict[str, Any]:
    """AFML s.18.8.3 (p.275-276), report only (D070): eigen-decompose the covariance V of the returns
    (V W = W Lambda), factor loadings f = W' w, risk share of each principal component
    theta_i = f_i^2 Lambda_ii / sum_n f_n^2 Lambda_nn, effective number of bets exp(-sum theta log theta)
    and Meucci (2009) concentration H = 1 - exp(-sum theta log theta) / N. Never used in a score or gate."""
    cov = returns.cov().to_numpy(dtype="float64")
    w = weights.reindex(returns.columns).fillna(0.0).to_numpy(dtype="float64")
    eigenvalues, eigenvectors = np.linalg.eigh((cov + cov.T) / 2.0)
    eigenvalues = np.clip(eigenvalues, 0.0, None)
    risk = (eigenvectors.T @ w) ** 2 * eigenvalues
    if not np.isfinite(risk).all() or risk.sum() <= EPS:
        return {"pc_effective_number_of_bets": np.nan, "pc_meucci_concentration": np.nan}
    theta = risk / risk.sum()
    positive = theta[theta > 0]
    n_bets = float(np.exp(-np.sum(positive * np.log(positive))))
    return {"pc_effective_number_of_bets": n_bets, "pc_meucci_concentration": 1.0 - n_bets / len(theta)}


def correlation_diagnostics(corr: pd.DataFrame) -> dict[str, Any]:
    if corr.shape[0] < 2:
        return {"mean_abs_corr": np.nan, "median_abs_corr": np.nan, "max_abs_corr": np.nan, "mean_corr": np.nan}
    vals = corr.values[~np.eye(corr.shape[0], dtype=bool)]
    vals = vals[np.isfinite(vals)]
    if len(vals) == 0:
        return {"mean_abs_corr": np.nan, "median_abs_corr": np.nan, "max_abs_corr": np.nan, "mean_corr": np.nan}
    return {"mean_abs_corr": float(np.mean(np.abs(vals))), "median_abs_corr": float(np.median(np.abs(vals))), "max_abs_corr": float(np.max(np.abs(vals))), "mean_corr": float(np.mean(vals))}


def asset_family_weight_summary(weights_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    asset = weights_df.groupby(["return_mode", "universe", "profile_role", "linkage_method", "asset"], dropna=False)["weight"].sum().reset_index().sort_values(["return_mode", "universe", "profile_role", "linkage_method", "weight"], ascending=[True, True, True, True, False])
    family = weights_df.groupby(["return_mode", "universe", "profile_role", "linkage_method", "family"], dropna=False)["weight"].sum().reset_index().sort_values(["return_mode", "universe", "profile_role", "linkage_method", "weight"], ascending=[True, True, True, True, False])
    return asset, family


def save_matrix(df: pd.DataFrame, path: Path, index_label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index_label=index_label)


def add_single_baseline_comparison(summary: pd.DataFrame, weights_all: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (return_mode, universe, role), g in weights_all.groupby(["return_mode", "universe", "profile_role"]):
        single = g[g["linkage_method"] == CANONICAL_METHOD]
        if single.empty:
            continue
        single_w = single.set_index("edge_id")["weight"]
        for method, current in g.groupby("linkage_method"):
            current_w = current.set_index("edge_id")["weight"]
            idx = sorted(set(single_w.index).union(current_w.index))
            sw, cw = single_w.reindex(idx).fillna(0.0), current_w.reindex(idx).fillna(0.0)
            rows.append({"return_mode": return_mode, "universe": universe, "profile_role": role, "linkage_method": method, "weight_l1_distance_from_single": float(np.abs(cw - sw).sum()), "weight_l2_distance_from_single": float(np.sqrt(((cw - sw) ** 2).sum())), "max_abs_weight_diff_from_single": float(np.abs(cw - sw).max())})
    comp = pd.DataFrame(rows)
    return summary.merge(comp, on=["return_mode", "universe", "profile_role", "linkage_method"], how="left") if not comp.empty else summary


def add_robustness_scores(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return summary
    parts = []
    for _, g in summary.groupby(["return_mode", "universe", "profile_role"]):
        g = g.copy()
        g["score_calmar"] = pct_rank(g["calmar_r"], True)
        g["score_total_r"] = pct_rank(g["total_r"], True)
        g["score_effective_n"] = pct_rank(g["effective_n"], True)
        g["score_top_weight"] = pct_rank(g["top_weight"], False)
        g["score_top_asset_weight"] = pct_rank(g["top_asset_weight"], False)
        g["score_top_family_weight"] = pct_rank(g["top_family_weight"], False)
        g["score_distance_from_single"] = pct_rank(g.get("weight_l1_distance_from_single", pd.Series(np.nan, index=g.index)), False)
        g["score_mean_abs_corr"] = pct_rank(g["mean_abs_corr"], False)
        g["robustness_preference_score"] = 0.18*g["score_calmar"] + 0.14*g["score_total_r"] + 0.18*g["score_effective_n"] + 0.14*g["score_top_weight"] + 0.12*g["score_top_asset_weight"] + 0.10*g["score_top_family_weight"] + 0.07*g["score_distance_from_single"] + 0.07*g["score_mean_abs_corr"]
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def build(write: bool = True) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selection = load_profile_selection()
    return_streams_manifest = load_return_streams_manifest()
    broker_summary = load_broker_metadata_summary()
    broker_validation = validate_broker_minlot_handoff(return_streams_manifest, broker_summary)
    all_weights, all_assets, all_families, all_portfolio_rows, all_cleaning_reports, failures = [], [], [], [], [], []
    manifest: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "created_at": now_utc(), "pipeline_mode": PIPELINE_MODE, "hrp_linkage_robustness_status": "PASS", "return_streams_dir": str(RETURN_STREAMS_DIR.relative_to(V2_ROOT)), "output_dir": str(OUTPUT_DIR.relative_to(V2_ROOT)), "input_profile_selection": str(INPUT_PROFILE_SELECTION.relative_to(V2_ROOT)), "input_return_streams_manifest": str(INPUT_RETURN_STREAMS_MANIFEST.relative_to(V2_ROOT)), "input_broker_metadata_summary": str(INPUT_BROKER_METADATA_SUMMARY.relative_to(V2_ROOT)), "return_streams_manifest_loaded": bool(return_streams_manifest), "broker_minlot_validation": broker_validation, "universes": UNIVERSES, "step08_eligible_universes": STEP08_ELIGIBLE_UNIVERSES, "profile_roles": PROFILE_ROLES, "return_modes": RETURN_MODES, "linkage_methods": LINKAGE_METHODS, "canonical_method": CANONICAL_METHOD, "corr_method": CORR_METHOD, "min_nonzero_days": MIN_NONZERO_DAYS, "drop_zero_variance_columns": DROP_ZERO_VARIANCE_COLUMNS, "denoise_method": DENOISE_METHOD, "denoise_kde_bwidth": DENOISE_KDE_BWIDTH, "detone_market_components": DETONE_MARKET_COMPONENTS, "denoise_rule": DENOISE_RULE, "linkage_input": "d_tilde_afml_snippet_16_4", "outputs": {}, "notes": ["HRP runs on the single compound_velocity edge_id return stream per universe.", "raw_r is primary for Step 08; afml_weighted_r is diagnostic.", "Step 07 remains an R-space structural HRP weighting step; no broker dollar sizing or lot calculation is applied here.", "Broker min-lot metadata is validated from Step 06 and broker-aware 04C diagnostics are carried into HRP weight reports for Step 08.", "Final executable-risk deployment is decided by downstream rolling/stress simulation.", "Artifact-file hashes are optional provenance only and are not HRP inputs or validation gates.", "AFML Snippet 16.4: the tree is built on d~, the Euclidean distance between the columns of D = sqrt((1 - rho)/2). MLAM Ch.2 (p.24-37): the correlation matrix is denoised exactly as Snippets 2.4-2.5 (Marcenko-Pastur fit with the book's var=1 fallback; nFacts = eigenvalues above lambda+, no floor; constant residual eigenvalue; nFacts=0 gives the identity, so HRP = IVP); the clustering matrix is additionally detoned only when a market component exists (first eigenvalue > lambda+ and first-eigenvector loadings of one sign in >= 95% of 1,000 iid row-bootstrap resamples, seed 0); a detoned matrix is never used for risk weighting (singular, MLAM p.32). hrp_raw_corr_*.csv (pre-denoise), hrp_corr_for_clustering_*.csv and hrp_corr_*.csv (used for weighting) are saved for audit; hrp_denoise_diagnostics_*.json records every test (fitted variance, lambda+, eigenvalues, nFacts, bootstrap same-sign share).", "AFML s.18.8.3 (D070): pc_effective_number_of_bets = exp(entropy of the principal-component risk shares) on the sample covariance of the streams and pc_meucci_concentration = 1 - that / N are reported in the portfolio summary only; they enter no score, warning or gate."]}
    total = len(RETURN_MODES) * len(UNIVERSES) * len(PROFILE_ROLES) * len(LINKAGE_METHODS)
    with tqdm(total=total, desc="Running V2 HRP scenarios", unit="scenario") as pbar:
        for return_mode in RETURN_MODES:
            for universe in UNIVERSES:
                for role in PROFILE_ROLES:
                    try:
                        raw = load_profile_matrix(universe, role, return_mode)
                        returns, cleaning = clean_return_matrix(raw)
                    except Exception as exc:
                        reason = str(exc)
                        failures.append({"return_mode": return_mode, "universe": universe, "profile_role": role, "error": reason})
                        for method in LINKAGE_METHODS:
                            all_portfolio_rows.append({"return_mode": return_mode, "universe": universe, "profile_role": role, "linkage_method": method, "eligible_for_step08": bool(return_mode == "raw_r" and universe in STEP08_ELIGIBLE_UNIVERSES), "status": "skipped", "error": reason, "columns_used": 0, "days": 0})
                            pbar.update(1)
                        continue
                    cleaning["return_mode"] = return_mode
                    cleaning["universe"] = universe
                    cleaning["profile_role"] = role
                    all_cleaning_reports.append(cleaning)
                    for method in LINKAGE_METHODS:
                        try:
                            result = hrp_allocation(returns, method)
                            weights = result["weights"]
                            corr, cov, dist, link, sorted_labels = result["corr"], result["cov"], result["dist"], result["linkage"], result["sorted_labels"]
                            raw_corr, corr_for_clustering, denoise_diag = result["raw_corr"], result["corr_for_clustering"], result["denoise_diagnostics"]
                            portfolio_returns = returns.mul(weights, axis=1).sum(axis=1)
                            weights_df = weight_report(weights, selection, return_mode, universe, role, method)
                            all_weights.append(weights_df)
                            asset_summary, family_summary = asset_family_weight_summary(weights_df)
                            all_assets.append(asset_summary)
                            all_families.append(family_summary)
                            all_portfolio_rows.append({"return_mode": return_mode, "universe": universe, "profile_role": role, "linkage_method": method, "eligible_for_step08": bool(return_mode == "raw_r" and universe in STEP08_ELIGIBLE_UNIVERSES), "status": "ok", "error": "", "columns_used": int(returns.shape[1]), "days": int(returns.shape[0]), "denoise_applied": bool(denoise_diag.get("applied")), "denoise_detoned": bool(denoise_diag.get("detoned", False)), "denoise_n_signal_eigenvalues": denoise_diag.get("n_signal_eigenvalues"), "denoise_skip_reason": denoise_diag.get("reason") or denoise_diag.get("error"), **portfolio_metrics(portfolio_returns), **equal_weight_metrics(returns), **concentration_metrics(weights_df), **principal_component_concentration(returns, weights), **correlation_diagnostics(corr)})
                            prefix = f"{return_mode}_{universe}_{role}_{method}"
                            paths = {"weights": OUTPUT_DIR / f"hrp_weights_{prefix}.csv", "ordered_labels": OUTPUT_DIR / f"hrp_ordered_labels_{prefix}.csv", "corr": OUTPUT_DIR / f"hrp_corr_{prefix}.csv", "raw_corr": OUTPUT_DIR / f"hrp_raw_corr_{prefix}.csv", "corr_for_clustering": OUTPUT_DIR / f"hrp_corr_for_clustering_{prefix}.csv", "cov": OUTPUT_DIR / f"hrp_cov_{prefix}.csv", "distance": OUTPUT_DIR / f"hrp_distance_{prefix}.csv", "linkage": OUTPUT_DIR / f"hrp_linkage_{prefix}.csv", "portfolio_returns": OUTPUT_DIR / f"hrp_portfolio_returns_{prefix}.csv", "denoise_diagnostics": OUTPUT_DIR / f"hrp_denoise_diagnostics_{prefix}.json"}
                            if write:
                                weights_df.to_csv(paths["weights"], index=False)
                                pd.DataFrame({"order": range(len(sorted_labels)), "edge_id": sorted_labels}).to_csv(paths["ordered_labels"], index=False)
                                save_matrix(corr, paths["corr"], "edge_id")
                                save_matrix(raw_corr, paths["raw_corr"], "edge_id")
                                save_matrix(corr_for_clustering, paths["corr_for_clustering"], "edge_id")
                                save_matrix(cov, paths["cov"], "edge_id")
                                save_matrix(dist, paths["distance"], "edge_id")
                                pd.DataFrame(link, columns=["cluster_left", "cluster_right", "distance", "sample_count"]).to_csv(paths["linkage"], index=False)
                                portfolio_returns.to_frame("hrp_portfolio_r").to_csv(paths["portfolio_returns"], index_label="date")
                                write_json(paths["denoise_diagnostics"], denoise_diag)
                            manifest["outputs"].update({f"{k}_{prefix}": str(v.relative_to(V2_ROOT)) for k, v in paths.items()})
                        except Exception as exc:
                            all_portfolio_rows.append({"return_mode": return_mode, "universe": universe, "profile_role": role, "linkage_method": method, "eligible_for_step08": bool(return_mode == "raw_r" and universe in STEP08_ELIGIBLE_UNIVERSES), "status": "failed", "error": str(exc), "columns_used": int(returns.shape[1]), "days": int(returns.shape[0])})
                        pbar.update(1)
    if not all_weights:
        raise RuntimeError("No successful HRP weights were produced.")
    weights_all = pd.concat(all_weights, ignore_index=True)
    asset_summary_all = pd.concat(all_assets, ignore_index=True) if all_assets else pd.DataFrame()
    family_summary_all = pd.concat(all_families, ignore_index=True) if all_families else pd.DataFrame()
    cleaning_all = pd.concat(all_cleaning_reports, ignore_index=True) if all_cleaning_reports else pd.DataFrame()
    portfolio_summary = add_robustness_scores(add_single_baseline_comparison(pd.DataFrame(all_portfolio_rows), weights_all))
    weights_step08 = weights_all[(weights_all["return_mode"] == "raw_r") & (weights_all["universe"].isin(STEP08_ELIGIBLE_UNIVERSES))].copy()
    summary_step08 = portfolio_summary[(portfolio_summary["return_mode"] == "raw_r") & (portfolio_summary["universe"].isin(STEP08_ELIGIBLE_UNIVERSES)) & (portfolio_summary["status"] == "ok")].copy()
    best_by_group = portfolio_summary[portfolio_summary["status"] == "ok"].sort_values("robustness_preference_score", ascending=False).groupby(["return_mode", "universe", "profile_role"], as_index=False).head(1).reset_index(drop=True)
    paths = {"weights_all": OUTPUT_DIR / "hrp_linkage_weights_all.csv", "weights_step08_primary": OUTPUT_DIR / "hrp_linkage_weights_step08_primary.csv", "asset_summary": OUTPUT_DIR / "hrp_linkage_asset_weight_summary.csv", "family_summary": OUTPUT_DIR / "hrp_linkage_family_weight_summary.csv", "portfolio_summary": OUTPUT_DIR / "hrp_linkage_robustness_summary.csv", "portfolio_step08_primary_summary": OUTPUT_DIR / "hrp_linkage_robustness_step08_primary_summary.csv", "input_cleaning_report": OUTPUT_DIR / "hrp_linkage_input_cleaning_report.csv", "broker_minlot_validation_report": OUTPUT_DIR / "hrp_linkage_broker_minlot_validation_report.json", "best_method_by_group": OUTPUT_DIR / "hrp_linkage_best_method_by_group.csv", "manifest": OUTPUT_DIR / "hrp_linkage_robustness_manifest.json"}
    manifest["outputs"].update({k: str(v.relative_to(V2_ROOT)) for k, v in paths.items()})
    manifest["counts"] = {"successful_hrp_scenarios": int((portfolio_summary["status"] == "ok").sum()), "failed_or_skipped_hrp_scenarios": int((portfolio_summary["status"] != "ok").sum()), "weight_rows_all": int(len(weights_all)), "weight_rows_step08_primary": int(len(weights_step08)), "portfolio_summary_rows": int(len(portfolio_summary)), "step08_primary_summary_rows": int(len(summary_step08)), "scenario_failure_groups": int(len(failures))}
    manifest["scenario_failures"] = failures
    if write:
        weights_all.to_csv(paths["weights_all"], index=False)
        weights_step08.to_csv(paths["weights_step08_primary"], index=False)
        asset_summary_all.to_csv(paths["asset_summary"], index=False)
        family_summary_all.to_csv(paths["family_summary"], index=False)
        portfolio_summary.to_csv(paths["portfolio_summary"], index=False)
        summary_step08.to_csv(paths["portfolio_step08_primary_summary"], index=False)
        cleaning_all.to_csv(paths["input_cleaning_report"], index=False)
        write_json(paths["broker_minlot_validation_report"], broker_validation)
        best_by_group.to_csv(paths["best_method_by_group"], index=False)
        write_json(paths["manifest"], manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.07 HRP linkage robustness")
    print(f"Status: {manifest['hrp_linkage_robustness_status']}")
    c = manifest["counts"]
    print(f"Successful HRP scenarios: {c['successful_hrp_scenarios']}")
    print(f"Failed/skipped HRP scenarios: {c['failed_or_skipped_hrp_scenarios']}")
    print(f"Step08 primary rows: {c['weight_rows_step08_primary']}")
    print(f"Profiles: {', '.join(PROFILE_ROLES)}")
    print(f"Output dir: {manifest['output_dir']}")


if __name__ == "__main__":
    main()
