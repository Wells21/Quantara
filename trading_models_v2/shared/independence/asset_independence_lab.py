"""
Cross-asset alpha independence lab for trading_models_v2.

V3 statistical engine. This replaces the earlier heuristic version, which
classified edge pairs with hardcoded correlation/overlap thresholds
(e.g. corr >= 0.80 => "likely_duplicate"). Those thresholds had no null
distribution and no p-value: a correlation of 0.42 was labeled the same
way regardless of whether it was estimated from 15 trades or 1,500, and
zero-filled sparse trade series made naive Pearson correlation dominated
by mutual non-trading days rather than genuine co-movement.

This version asks a real statistical question for every edge pair:
"is the observed trade-timing overlap / return correlation distinguishable
from what independent, unrelated trade timing would produce?"

Method: circular-shift permutation testing (a standard nonparametric
technique for testing cross-dependence between two time series while
preserving each series' own autocorrelation/clustering structure -- the
same reason Gate D moved from an ad hoc calibration-curve threshold to
AUC-ROC/Mann-Whitney: assumption-light, rank/permutation-based, reports
a p-value instead of a magic-number cutoff).

For each pair of edges:
  1. Trade-timing overlap: circularly shift edge B's active-trade calendar
     by every possible offset (or, if the sample is too long for an
     exhaustive test, a large random sample of offsets), recompute the
     overlap statistic each time, and report the empirical p-value of the
     observed overlap against this null of "independent timing."
  2. Daily-return correlation: same circular-shift procedure applied to
     the aligned daily return series, reporting an empirical p-value for
     the observed Pearson correlation.
  Both p-value families get a Benjamini-Hochberg FDR q-value across all
  pairs tested in the run, since testing many pairs simultaneously
  inflates the false-positive rate of "looks dependent" pairs.

Additionally, for the full set of edges in a run, this module fits a
Marcenko-Pastur (Random Matrix Theory) bound to the eigenvalue spectrum
of the aligned daily-return correlation matrix (MLAM, "Denoising and
Detoning"). Eigenvalues within the theoretical noise band are
indistinguishable from what a purely random correlation matrix of the
same shape (N edges, T observations) would produce; eigenvalues above
the band indicate genuine common structure. This gives a portfolio-wide
"how many effectively independent bets do we have" number, complementing
the pairwise tests.

This lab is a diagnostic, not a gate. It does not kill, promote, or
reweight edges -- it reports statistics for a human to read. Portfolio-
level treatment (how these numbers should feed the allocator/HRP) is a
separate, later design conversation scoped to portfolio_allocator/.

This lab consumes artifacts only (calibrated OOF predictions, each edge's
selected threshold, edge_manifest.json, and config/edge_universe.json).
It does not score models or engineer features.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

PROBABILITY_CANDIDATES = [
    "calibrated_probability",
    "live_calibrated_probability",
    "approved_probability",
    "probability",
    "oof_prob",
    "raw_probability",
    "proba",
    "pred_proba",
]
# NOTE: "calibrated_probability" is the CPCV-fold-averaged research diagnostic
# (shared/calibration/fit_sigmoid_calibrator_and_thresholds.py CAL_PROB_COL) --
# per-fold sigmoid coefficients were empirically shown to vary wildly
# fold-to-fold on some edges, so this column does not track raw_probability as
# faithfully as "live_calibrated_probability" (LIVE_CAL_PROB_COL, raw
# probability through the single final live sigmoid fit) does. This lab always
# prefers whichever probability_column each edge's own selected_threshold.json
# declares (so the threshold value and the column it was computed against stay
# in the same probability space) -- this candidate list is only a fallback for
# the rare case that field is missing. Do not silently default to
# "calibrated_probability" for anything meant to reflect live/allocator-facing
# probability space; use "live_calibrated_probability" for that.
R_CANDIDATES = ["realized_r", "executed_r", "r", "R", "trade_r", "r_multiple", "R_multiple"]
SIDE_CANDIDATES = ["side", "Side", "signal_side", "primary_side"]
DATETIME_CANDIDATES = ["Datetime", "datetime", "date", "timestamp", "event_timestamp"]
T1_CANDIDATES = ["t1", "exit_time", "end_time", "barrier_end_time"]

THRESHOLD_POLICY_SELECTED = "selected"
THRESHOLD_POLICY_OVERRIDE = "override"
THRESHOLD_POLICY_ALL = "all"
THRESHOLD_POLICIES = [THRESHOLD_POLICY_SELECTED, THRESHOLD_POLICY_OVERRIDE, THRESHOLD_POLICY_ALL]

DEPLOYED_STATUS_VALUES = {"deployability_pass"}

DEFAULT_ALPHA = 0.05
DEFAULT_N_PERMUTATION_CAP = 5000
DEFAULT_SEED = 7


@dataclass(frozen=True)
class EdgeSource:
    edge_id: str
    asset: str
    edge_name: str
    edge_root: Path
    side_value: int | None
    family: str | None
    status: str
    mode: str
    data_path: Path
    threshold_policy: str
    threshold: float | None
    probability_col: str | None
    side_col: str | None
    r_col: str | None
    datetime_col: str | None
    t1_col: str | None
    source_manifest: Path | None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _find_col(df: pd.DataFrame, preferred: str | None, candidates: list[str]) -> str | None:
    if preferred and preferred in df.columns:
        return preferred
    for col in candidates:
        if col in df.columns:
            return col
    lower = {str(col).lower(): col for col in df.columns}
    for col in candidates:
        if col.lower() in lower:
            return lower[col.lower()]
    return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if not np.isfinite(out):
            return None
        return out
    except Exception:
        return None


def _coerce_datetime_index(index_like: Any, *, name: str | None = None) -> pd.DatetimeIndex:
    dt = to_datetime_iso(index_like, utc=True, errors="coerce")
    idx = pd.DatetimeIndex(dt)
    idx = idx[~idx.isna()]
    if name is not None:
        idx.name = name
    return idx


def _ensure_datetime_series(series: pd.Series, name: str) -> pd.Series:
    out = series.copy()
    out.index = _coerce_datetime_index(out.index, name="Datetime")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.name = name
    return out


def _load_selected_threshold(edge_root: Path, edge_name: str) -> tuple[float | None, str | None, Path | None]:
    threshold_dir = edge_root / "artifacts" / "thresholds"
    candidates = sorted(threshold_dir.glob("*selected_threshold.json"))
    if not candidates:
        candidates = sorted(threshold_dir.glob("*.json"))
    for path in candidates:
        try:
            payload = _read_json(path)
        except Exception:
            continue
        selected = payload.get("selected", payload)
        threshold = _safe_float(selected.get("selected_threshold", selected.get("threshold")))
        prob_col = selected.get("probability_column") or payload.get("threshold_probability_column")
        if threshold is not None:
            return threshold, str(prob_col) if prob_col else None, path
    return None, None, None


def _edge_manifest_status(edge_root: Path) -> tuple[str, dict[str, Any] | None, Path | None]:
    path = edge_root / "artifacts" / "edge_manifest.json"
    if not path.exists():
        return "NO_MANIFEST", None, None
    try:
        manifest = _read_json(path)
    except Exception:
        return "MANIFEST_READ_ERROR", None, path
    status = (
        manifest.get("live_compatibility", {}).get("deployment_status")
        or manifest.get("deployment_status")
        or manifest.get("status")
        or "UNKNOWN"
    )
    return str(status), manifest, path


def _manifest_artifact_path(v2_root: Path, manifest: dict[str, Any] | None, key: str) -> Path | None:
    if not manifest:
        return None
    value = manifest.get("artifacts", {}).get(key)
    if value:
        return v2_root / value
    return None


def _find_calibrated_oof(edge_root: Path, v2_root: Path, manifest: dict[str, Any] | None) -> Path | None:
    from_manifest = _manifest_artifact_path(v2_root, manifest, "calibrated_oof_predictions")
    if from_manifest and from_manifest.exists():
        return from_manifest
    candidates = sorted((edge_root / "artifacts" / "calibration").glob("*calibrated_oof_predictions.csv"))
    return candidates[0] if candidates else None


def _find_model_rows(edge_root: Path) -> Path | None:
    candidates = sorted((edge_root / "artifacts" / "research_dataset").glob("*model_rows.csv"))
    return candidates[0] if candidates else None


def _normalize_threshold_policy(mode: str, threshold_policy: str, probability_threshold: float | None) -> tuple[str, float | None]:
    if threshold_policy not in THRESHOLD_POLICIES:
        raise ValueError(f"Unsupported threshold_policy={threshold_policy!r}. Expected one of {THRESHOLD_POLICIES}.")
    if mode == "research_events":
        return THRESHOLD_POLICY_ALL, None
    if mode == "scored_candidates" and threshold_policy == THRESHOLD_POLICY_SELECTED:
        return THRESHOLD_POLICY_OVERRIDE, 0.10 if probability_threshold is None else float(probability_threshold)
    if threshold_policy == THRESHOLD_POLICY_OVERRIDE:
        return THRESHOLD_POLICY_OVERRIDE, 0.10 if probability_threshold is None else float(probability_threshold)
    if threshold_policy == THRESHOLD_POLICY_ALL:
        return THRESHOLD_POLICY_ALL, None
    return THRESHOLD_POLICY_SELECTED, None


def _load_deployed_edge_names(v2_root: Path, asset: str) -> set[str] | None:
    """Edge folder names (family_side) registered as deployability_pass for this asset.

    Returns None if the registry cannot be read (caller should then not filter).
    """
    registry_path = v2_root / "config" / "edge_universe.json"
    if not registry_path.exists():
        return None
    try:
        registry = _read_json(registry_path)
    except Exception:
        return None
    names: set[str] = set()
    for entry in registry.get("edges", []):
        if entry.get("asset") != asset:
            continue
        if str(entry.get("status")) not in DEPLOYED_STATUS_VALUES:
            continue
        family = entry.get("family")
        side = entry.get("side")
        if family and side:
            names.add(f"{family}_{side}")
    return names


def _discover_edges(
    *,
    v2_root: Path,
    asset: str,
    mode: str,
    include_non_pass: bool,
    deployed_only: bool,
    include_edges: set[str] | None,
    exclude_edges: set[str] | None,
    threshold_policy: str,
    probability_threshold: float | None,
) -> list[EdgeSource]:
    edges_root = v2_root / "assets" / asset / "edges"
    if not edges_root.exists():
        return []

    effective_policy, effective_threshold = _normalize_threshold_policy(mode, threshold_policy, probability_threshold)
    deployed_names = _load_deployed_edge_names(v2_root, asset) if deployed_only else None
    if deployed_only and not deployed_names:
        return []

    sources: list[EdgeSource] = []
    for edge_root in sorted([p for p in edges_root.iterdir() if p.is_dir()]):
        edge_name = edge_root.name
        edge_id = f"{asset}/{edge_name}"
        if deployed_only and edge_name not in deployed_names:
            continue
        if include_edges and edge_name not in include_edges and edge_id not in include_edges:
            continue
        if exclude_edges and (edge_name in exclude_edges or edge_id in exclude_edges):
            continue

        status, manifest, manifest_path = _edge_manifest_status(edge_root)
        identity = manifest.get("identity", {}) if manifest else {}
        side_value = identity.get("side_value", manifest.get("side_value") if manifest else None)
        family = identity.get("family", manifest.get("family") if manifest else None)
        if family is None and "_" in edge_name:
            family = edge_name.rsplit("_", 1)[0]

        if mode in {"selected_trades", "scored_candidates"}:
            if not deployed_only and not include_non_pass and str(status).upper() != "PASS":
                continue
            data_path = _find_calibrated_oof(edge_root, v2_root, manifest)
            selected_threshold, prob_col, threshold_path = _load_selected_threshold(edge_root, edge_name)
            if not data_path:
                continue
            if effective_policy == THRESHOLD_POLICY_SELECTED and selected_threshold is None:
                continue
            threshold = selected_threshold if effective_policy == THRESHOLD_POLICY_SELECTED else effective_threshold
            sources.append(EdgeSource(
                edge_id=edge_id,
                asset=asset,
                edge_name=edge_name,
                edge_root=edge_root,
                side_value=int(side_value) if side_value is not None else None,
                family=str(family) if family is not None else None,
                status=str(status),
                mode=mode,
                data_path=data_path,
                threshold_policy=effective_policy,
                threshold=threshold,
                probability_col=str(prob_col) if prob_col else None,
                side_col=None,
                r_col=None,
                datetime_col=None,
                t1_col=None,
                source_manifest=threshold_path or manifest_path,
            ))
        elif mode == "research_events":
            data_path = _find_model_rows(edge_root)
            if not data_path:
                continue
            sources.append(EdgeSource(
                edge_id=edge_id,
                asset=asset,
                edge_name=edge_name,
                edge_root=edge_root,
                side_value=int(side_value) if side_value is not None else None,
                family=str(family) if family is not None else None,
                status=str(status),
                mode=mode,
                data_path=data_path,
                threshold_policy=THRESHOLD_POLICY_ALL,
                threshold=None,
                probability_col=None,
                side_col=None,
                r_col=None,
                datetime_col=None,
                t1_col=None,
                source_manifest=manifest_path,
            ))
        else:
            raise ValueError(f"Unsupported mode: {mode}")
    return sources


def _compute_profit_factor(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").dropna()
    gains = r[r > 0].sum()
    losses = r[r < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / abs(losses))


def _compute_max_drawdown(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").dropna()
    if r.empty:
        return float("nan")
    equity = r.cumsum()
    dd = equity - equity.cummax()
    return float(dd.min())


def _sharpe_proxy(r: pd.Series) -> float:
    r = pd.to_numeric(r, errors="coerce").dropna()
    std = r.std(ddof=0)
    if std == 0 or not np.isfinite(std):
        return float("nan")
    return float(r.mean() / std)


def _load_edge_trades(source: EdgeSource) -> tuple[pd.Series, pd.DataFrame, dict[str, Any]]:
    df = pd.read_csv(source.data_path)
    datetime_col = _find_col(df, source.datetime_col, DATETIME_CANDIDATES)
    if not datetime_col:
        raise ValueError(f"{source.edge_id}: no datetime column found in {source.data_path}")
    side_col = _find_col(df, source.side_col, SIDE_CANDIDATES)
    r_col = _find_col(df, source.r_col, R_CANDIDATES)
    prob_col = _find_col(df, source.probability_col, PROBABILITY_CANDIDATES)
    t1_col = _find_col(df, source.t1_col, T1_CANDIDATES)

    df[datetime_col] = to_datetime_iso(df[datetime_col], utc=True, errors="coerce")
    df = df.dropna(subset=[datetime_col]).copy().sort_values(datetime_col)
    df = df.set_index(pd.DatetimeIndex(df[datetime_col], name="Datetime"), drop=False)

    if side_col:
        df[side_col] = pd.to_numeric(df[side_col], errors="coerce")
    if r_col:
        df[r_col] = pd.to_numeric(df[r_col], errors="coerce")
    if prob_col:
        df[prob_col] = pd.to_numeric(df[prob_col], errors="coerce")
    if t1_col:
        df[t1_col] = to_datetime_iso(df[t1_col], utc=True, errors="coerce")

    if source.mode in {"selected_trades", "scored_candidates"}:
        if not r_col:
            raise ValueError(f"{source.edge_id}: {source.mode} mode requires an R column")
        if source.threshold_policy == THRESHOLD_POLICY_ALL:
            mask = pd.Series(True, index=df.index)
        else:
            if not prob_col:
                raise ValueError(f"{source.edge_id}: threshold policy {source.threshold_policy!r} requires a probability column")
            if source.threshold is None:
                raise ValueError(f"{source.edge_id}: threshold policy {source.threshold_policy!r} requires a numeric threshold")
            mask = df[prob_col] >= float(source.threshold)
        if source.side_value is not None and side_col:
            mask &= df[side_col] == source.side_value
        mask &= df[r_col].notna()
        trades = df.loc[mask].copy()
        r_for_series = trades[r_col]
    else:
        trades = df.copy()
        if source.side_value is not None and side_col:
            trades = trades.loc[trades[side_col] == source.side_value].copy()
        r_for_series = trades[r_col].fillna(0.0) if r_col else pd.Series(1.0, index=trades.index)

    if trades.empty:
        r_series = pd.Series(dtype="float64", name=source.edge_name)
        r_series.index = pd.DatetimeIndex([], name="Datetime")
    else:
        r_for_series.index = _coerce_datetime_index(r_for_series.index, name="Datetime")
        r_series = r_for_series.groupby(level=0).sum().sort_index()
        r_series = _ensure_datetime_series(r_series, source.edge_name)

    n_days = max((df.index.max() - df.index.min()).days, 1) if len(df) else 1
    threshold_used = source.threshold if source.threshold_policy != THRESHOLD_POLICY_ALL else None
    summary = {
        "asset": source.asset,
        "edge_id": source.edge_id,
        "edge_name": source.edge_name,
        "family": source.family,
        "side_value": source.side_value,
        "mode": source.mode,
        "deployment_status": source.status,
        "data_path": str(source.data_path),
        "source_manifest": str(source.source_manifest) if source.source_manifest else None,
        "threshold_policy": source.threshold_policy,
        "threshold": threshold_used,
        "probability_col": prob_col,
        "side_col": side_col,
        "r_col": r_col,
        "datetime_col": datetime_col,
        "t1_col": t1_col,
        "total_rows": int(len(df)),
        "selected_trades": int(len(trades)),
        "total_days": int(n_days),
        "trades_per_day": float(len(trades) / n_days),
        "win_rate": float((trades[r_col] > 0).mean()) if r_col and len(trades) else float("nan"),
        "avg_r": float(trades[r_col].mean()) if r_col and len(trades) else float("nan"),
        "total_r": float(trades[r_col].sum()) if r_col and len(trades) else float("nan"),
        "profit_factor": _compute_profit_factor(trades[r_col]) if r_col and len(trades) else float("nan"),
        "max_drawdown_r": _compute_max_drawdown(trades[r_col]) if r_col and len(trades) else float("nan"),
        "sharpe_proxy_per_trade": _sharpe_proxy(trades[r_col]) if r_col and len(trades) else float("nan"),
        "start_date": str(df.index.min()) if len(df) else None,
        "end_date": str(df.index.max()) if len(df) else None,
    }
    return r_series, trades, summary


def _aligned_frame(series_by_edge: dict[str, pd.Series]) -> pd.DataFrame:
    if not series_by_edge:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="Datetime"))
    normalized = {name: _ensure_datetime_series(series, name) for name, series in series_by_edge.items()}
    all_index = pd.DatetimeIndex([], name="Datetime")
    for series in normalized.values():
        all_index = all_index.union(series.index)
    all_index = _coerce_datetime_index(all_index, name="Datetime").sort_values()
    out = pd.DataFrame(index=all_index)
    for name, series in normalized.items():
        out[name] = series.reindex(all_index).fillna(0.0).astype("float64")
    out.index = _coerce_datetime_index(out.index, name="Datetime")
    return out.sort_index()


def _daily_frame(return_df: pd.DataFrame) -> pd.DataFrame:
    if return_df.empty:
        return return_df.copy()
    out = return_df.copy()
    out.index = _coerce_datetime_index(out.index, name="Datetime")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.resample("1D").sum().fillna(0.0)


def _portfolio_stats(r: pd.Series) -> dict[str, float]:
    r = pd.to_numeric(r, errors="coerce").dropna()
    return {
        "total_r": float(r.sum()) if len(r) else float("nan"),
        "avg_r": float(r.mean()) if len(r) else float("nan"),
        "profit_factor": _compute_profit_factor(r) if len(r) else float("nan"),
        "max_drawdown_r": _compute_max_drawdown(r) if len(r) else float("nan"),
        "sharpe_proxy": _sharpe_proxy(r) if len(r) else float("nan"),
    }


def _marginal_contribution(return_df: pd.DataFrame) -> pd.DataFrame:
    if return_df.empty:
        return pd.DataFrame()
    full = return_df.sum(axis=1)
    full_stats = _portfolio_stats(full)
    rows = []
    for edge in return_df.columns:
        without = return_df.drop(columns=[edge]).sum(axis=1)
        without_stats = _portfolio_stats(without)
        rows.append({
            "edge_name": edge,
            "full_total_r": full_stats["total_r"],
            "without_edge_total_r": without_stats["total_r"],
            "marginal_total_r": full_stats["total_r"] - without_stats["total_r"],
            "full_max_drawdown_r": full_stats["max_drawdown_r"],
            "without_edge_max_drawdown_r": without_stats["max_drawdown_r"],
            "marginal_drawdown_change_r": full_stats["max_drawdown_r"] - without_stats["max_drawdown_r"],
            "full_profit_factor": full_stats["profit_factor"],
            "without_edge_profit_factor": without_stats["profit_factor"],
            "marginal_pf_change": full_stats["profit_factor"] - without_stats["profit_factor"],
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Statistical engine: circular-shift permutation tests
# ---------------------------------------------------------------------------

def _circular_shift_indices(t: int, n_perm_cap: int, rng: np.random.Generator) -> tuple[np.ndarray, bool]:
    """Return an array of shift offsets in [1, t-1] and whether it is exhaustive."""
    max_shift = t - 1
    if max_shift <= 0:
        return np.array([], dtype=int), True
    if max_shift <= n_perm_cap:
        return np.arange(1, t), True
    shifts = rng.choice(np.arange(1, t), size=n_perm_cap, replace=False)
    return shifts, False


def _pairwise_circular_shift_test(
    a: np.ndarray,
    b: np.ndarray,
    n_perm_cap: int,
    rng: np.random.Generator,
) -> dict[str, Any]:
    """Circular-shift permutation test of independence between two aligned return series.

    Shifts `b` circularly against a fixed `a` many times, preserving each
    series' own autocorrelation/clustering, and computes an empirical
    two-sided p-value for both the observed Pearson correlation and the
    observed max trade-timing overlap fraction against the null of
    independent timing.
    """
    t = len(a)
    result: dict[str, Any] = {
        "n_obs_days": int(t),
        "n_permutations_used": 0,
        "exhaustive": False,
    }

    active_a = a != 0.0
    active_b = b != 0.0
    n_a = int(active_a.sum())
    n_b = int(active_b.sum())
    common_obs = int((active_a & active_b).sum())
    overlap_ab_obs = float(common_obs / n_a) if n_a > 0 else float("nan")
    overlap_ba_obs = float(common_obs / n_b) if n_b > 0 else float("nan")
    max_overlap_obs = float(np.nanmax([overlap_ab_obs, overlap_ba_obs])) if n_a > 0 or n_b > 0 else float("nan")

    std_a = float(a.std())
    std_b = float(b.std())
    if std_a > 0 and std_b > 0:
        corr_obs = float(np.corrcoef(a, b)[0, 1])
    else:
        corr_obs = float("nan")

    result.update({
        "n_common_trade_days": common_obs,
        "n_active_days_a": n_a,
        "n_active_days_b": n_b,
        "overlap_a_given_a_active": overlap_ab_obs,
        "overlap_b_given_b_active": overlap_ba_obs,
        "max_overlap": max_overlap_obs,
        "return_corr_daily": corr_obs,
    })

    if t < 10 or (n_a == 0 or n_b == 0) or not np.isfinite(max_overlap_obs):
        result.update({"p_overlap": float("nan"), "p_corr": float("nan"), "insufficient_data": True})
        return result

    shifts, exhaustive = _circular_shift_indices(t, n_perm_cap, rng)
    n_shifts = len(shifts)
    result["n_permutations_used"] = int(n_shifts)
    result["exhaustive"] = bool(exhaustive)
    if n_shifts == 0:
        result.update({"p_overlap": float("nan"), "p_corr": float("nan"), "insufficient_data": True})
        return result

    idx = shifts[:, None] + np.arange(t)[None, :]

    # Overlap null distribution.
    active_bb = np.concatenate([active_b, active_b])
    shifted_active_b = active_bb[idx]
    common_null = (active_a[None, :] & shifted_active_b).sum(axis=1)
    shifted_n_b = shifted_active_b.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        overlap_ab_null = np.where(n_a > 0, common_null / max(n_a, 1), np.nan)
        overlap_ba_null = np.where(shifted_n_b > 0, common_null / np.where(shifted_n_b > 0, shifted_n_b, 1), np.nan)
    max_overlap_null = np.nanmax(np.vstack([overlap_ab_null, overlap_ba_null]), axis=0)
    p_overlap = float((1 + np.sum(max_overlap_null >= max_overlap_obs)) / (n_shifts + 1))
    result["p_overlap"] = p_overlap

    # Correlation null distribution (only meaningful when both series vary).
    if std_a > 0 and std_b > 0:
        bb = np.concatenate([b, b])
        shifted_b = bb[idx]
        mean_a = float(a.mean())
        shifted_mean = shifted_b.mean(axis=1)
        shifted_std = shifted_b.std(axis=1)
        cov = ((shifted_b - shifted_mean[:, None]) * (a - mean_a)[None, :]).mean(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            null_corr = np.where(shifted_std > 0, cov / (std_a * np.where(shifted_std > 0, shifted_std, 1)), np.nan)
        valid_null_corr = null_corr[np.isfinite(null_corr)]
        if valid_null_corr.size > 0 and np.isfinite(corr_obs):
            p_corr = float((1 + np.sum(np.abs(valid_null_corr) >= abs(corr_obs))) / (valid_null_corr.size + 1))
        else:
            p_corr = float("nan")
    else:
        p_corr = float("nan")
    result["p_corr"] = p_corr
    result["insufficient_data"] = False
    return result


def _benjamini_hochberg(pvalues: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg FDR-adjusted q-values. NaN input stays NaN."""
    n = len(pvalues)
    q = np.full(n, np.nan)
    valid_idx = np.where(np.isfinite(pvalues))[0]
    if valid_idx.size == 0:
        return q
    p_valid = pvalues[valid_idx]
    m = p_valid.size
    order = np.argsort(p_valid)
    ranked = p_valid[order]
    raw_q = ranked * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(raw_q[::-1])[::-1]
    adjusted = np.clip(adjusted, 0.0, 1.0)
    out = np.empty(m)
    out[order] = adjusted
    q[valid_idx] = out
    return q


def _independence_note(p_corr: float, p_overlap: float, alpha: float) -> str:
    corr_dependent = np.isfinite(p_corr) and p_corr < alpha
    overlap_dependent = np.isfinite(p_overlap) and p_overlap < alpha
    if not np.isfinite(p_corr) and not np.isfinite(p_overlap):
        return "insufficient_data"
    if corr_dependent and overlap_dependent:
        return "dependence_detected: both return-correlation and trade-timing overlap"
    if corr_dependent:
        return "dependence_detected: return-correlation only"
    if overlap_dependent:
        return "dependence_detected: trade-timing overlap only"
    return "no_dependence_detected"


def _pairwise_scorecard(
    return_df: pd.DataFrame,
    summary_by_edge: dict[str, dict[str, Any]],
    *,
    n_perm_cap: int,
    seed: int,
    alpha: float,
) -> pd.DataFrame:
    names = list(return_df.columns)
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    for i, a_name in enumerate(names):
        for b_name in names[i + 1:]:
            a = return_df[a_name].to_numpy(dtype="float64")
            b = return_df[b_name].to_numpy(dtype="float64")
            stats = _pairwise_circular_shift_test(a, b, n_perm_cap, rng)
            asset_a = summary_by_edge.get(a_name, {}).get("asset")
            asset_b = summary_by_edge.get(b_name, {}).get("asset")
            rows.append({
                "edge_a": a_name,
                "edge_b": b_name,
                "asset_a": asset_a,
                "asset_b": asset_b,
                "same_asset": bool(asset_a == asset_b),
                **stats,
            })
    scorecard = pd.DataFrame(rows)
    if scorecard.empty:
        return scorecard
    scorecard["q_corr_bh"] = _benjamini_hochberg(scorecard["p_corr"].to_numpy(dtype="float64"))
    scorecard["q_overlap_bh"] = _benjamini_hochberg(scorecard["p_overlap"].to_numpy(dtype="float64"))
    scorecard["independence_note"] = [
        _independence_note(row.p_corr, row.p_overlap, alpha) for row in scorecard.itertuples()
    ]
    scorecard["min_p_value"] = scorecard[["p_corr", "p_overlap"]].min(axis=1, skipna=True)
    return scorecard.sort_values("min_p_value", na_position="last").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Random Matrix Theory (Marcenko-Pastur) portfolio-wide diagnostic
# ---------------------------------------------------------------------------

def _marcenko_pastur_lambda_plus(q: float) -> float:
    """Upper eigenvalue bound of the Marcenko-Pastur distribution for a unit-variance
    correlation matrix, where q = N (variables) / T (observations)."""
    return float((1.0 + np.sqrt(q)) ** 2)


def _denoise_correlation_matrix(corr: np.ndarray, lambda_plus: float) -> tuple[np.ndarray, np.ndarray, int]:
    """MLAM-style denoising: shrink noise-band eigenvalues to their average, keep
    signal eigenvalues, reconstruct and renormalize to a valid correlation matrix.
    Returns (denoised_corr, eigenvalues_ascending, n_signal_eigenvalues)."""
    eigvals, eigvecs = np.linalg.eigh(corr)
    is_noise = eigvals <= lambda_plus
    n_signal = int((~is_noise).sum())
    denoised_eigvals = eigvals.copy()
    if is_noise.any():
        denoised_eigvals[is_noise] = eigvals[is_noise].mean()
    reconstructed = eigvecs @ np.diag(denoised_eigvals) @ eigvecs.T
    d = np.sqrt(np.clip(np.diag(reconstructed), 1e-12, None))
    denoised_corr = reconstructed / np.outer(d, d)
    np.fill_diagonal(denoised_corr, 1.0)
    return denoised_corr, eigvals, n_signal


def _effective_number_of_bets(eigvals: np.ndarray) -> float:
    """Participation-ratio diversification measure: N^2 / sum(lambda_i^2).
    Equals N when all eigenvalues are equal (fully independent bets), and
    approaches 1 when a single eigenvalue dominates (one common factor)."""
    total_sq = float(np.sum(eigvals ** 2))
    if total_sq <= 0:
        return float("nan")
    return float((np.sum(eigvals) ** 2) / total_sq)


def _random_matrix_diagnostic(return_df: pd.DataFrame) -> dict[str, Any]:
    names = list(return_df.columns)
    n = len(names)
    t = len(return_df)
    if n < 2 or t < 2:
        return {"applicable": False, "reason": "need at least 2 edges and 2 observations"}
    corr = return_df.corr().to_numpy()
    corr = np.nan_to_num(corr, nan=0.0)
    np.fill_diagonal(corr, 1.0)
    q = n / t
    lambda_plus = _marcenko_pastur_lambda_plus(q)
    denoised_corr, eigvals, n_signal = _denoise_correlation_matrix(corr, lambda_plus)
    enb_raw = _effective_number_of_bets(eigvals)
    denoised_eigvals = np.linalg.eigvalsh(denoised_corr)
    enb_denoised = _effective_number_of_bets(denoised_eigvals)
    eigen_df = pd.DataFrame({
        "eigenvalue": eigvals[::-1],
        "marcenko_pastur_upper_bound": lambda_plus,
        "is_signal": (eigvals[::-1] > lambda_plus),
    })
    return {
        "applicable": True,
        "n_edges": n,
        "n_observations": t,
        "aspect_ratio_q_n_over_t": float(q),
        "marcenko_pastur_lambda_plus": lambda_plus,
        "n_signal_eigenvalues": n_signal,
        "n_noise_eigenvalues": n - n_signal,
        "effective_number_of_bets_raw": enb_raw,
        "effective_number_of_bets_denoised": enb_denoised,
        "correlation_matrix": pd.DataFrame(corr, index=names, columns=names),
        "denoised_correlation_matrix": pd.DataFrame(denoised_corr, index=names, columns=names),
        "eigenvalue_spectrum": eigen_df,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _resolve_assets(v2_root: Path, assets: list[str] | None) -> list[str]:
    if assets:
        return list(assets)
    assets_root = v2_root / "assets"
    return sorted(p.name for p in assets_root.iterdir() if p.is_dir() and (p / "edges").exists())


def run_independence_lab(
    *,
    v2_root: str | Path,
    assets: list[str] | None = None,
    output_dir: str | Path | None = None,
    mode: str = "selected_trades",
    deployed_only: bool = True,
    include_non_pass: bool = False,
    include_edges: list[str] | None = None,
    exclude_edges: list[str] | None = None,
    threshold_policy: str = THRESHOLD_POLICY_SELECTED,
    probability_threshold: float | None = None,
    n_permutation_cap: int = DEFAULT_N_PERMUTATION_CAP,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Run the cross-asset edge independence lab.

    Default behaviour (matching the "is my deployed universe actually
    independent" question): scan every asset, keep only edges registered
    as deployability_pass in config/edge_universe.json, load each one's
    selected-threshold trade stream, and run circular-shift permutation
    tests plus a Marcenko-Pastur diagnostic across all of them together
    (cross-asset pairs included).
    """
    v2_root = Path(v2_root).resolve()
    effective_threshold_policy, effective_probability_threshold = _normalize_threshold_policy(
        mode, threshold_policy, probability_threshold
    )
    resolved_assets = _resolve_assets(v2_root, assets)
    output_dir = (
        Path(output_dir).resolve()
        if output_dir
        else v2_root / "shared" / "independence" / "outputs" / (
            "deployed_cross_asset" if deployed_only else f"{mode}_all_edges"
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    all_sources: list[EdgeSource] = []
    for asset in resolved_assets:
        all_sources.extend(_discover_edges(
            v2_root=v2_root,
            asset=asset,
            mode=mode,
            include_non_pass=include_non_pass,
            deployed_only=deployed_only,
            include_edges=set(include_edges) if include_edges else None,
            exclude_edges=set(exclude_edges) if exclude_edges else None,
            threshold_policy=effective_threshold_policy,
            probability_threshold=effective_probability_threshold,
        ))

    if not all_sources:
        raise RuntimeError(
            f"No eligible {mode} sources found across assets={resolved_assets} (deployed_only={deployed_only})."
        )

    series_by_edge: dict[str, pd.Series] = {}
    summaries: list[dict[str, Any]] = []
    load_errors: list[dict[str, str]] = []
    for source in all_sources:
        try:
            r_series, trades, summary = _load_edge_trades(source)
            if trades.empty:
                load_errors.append({"edge_id": source.edge_id, "error": "no trades after filtering"})
                continue
            series_by_edge[source.edge_id] = r_series.rename(source.edge_id)
            summaries.append(summary)
        except Exception as exc:
            load_errors.append({"edge_id": source.edge_id, "error": str(exc)})

    if not series_by_edge:
        raise RuntimeError(f"No edges could be loaded. Errors: {load_errors}")

    summary_by_edge = {s["edge_id"]: s for s in summaries}
    summary_df = pd.DataFrame(summaries)
    return_df = _aligned_frame(series_by_edge)
    daily_df = _daily_frame(return_df)

    scorecard_df = _pairwise_scorecard(
        daily_df, summary_by_edge, n_perm_cap=n_permutation_cap, seed=seed, alpha=alpha
    )
    marginal_df = _marginal_contribution(return_df)
    rmt = _random_matrix_diagnostic(daily_df)

    summary_df.to_csv(output_dir / "edge_summary.csv", index=False)
    return_df.to_csv(output_dir / "combined_returns.csv", index_label="Datetime")
    daily_df.to_csv(output_dir / "combined_daily_returns.csv", index_label="Date")
    scorecard_df.to_csv(output_dir / "pairwise_independence_scorecard.csv", index=False)
    marginal_df.to_csv(output_dir / "marginal_contribution.csv", index=False)

    rmt_summary: dict[str, Any] = {"applicable": rmt.get("applicable", False)}
    if rmt.get("applicable"):
        rmt["correlation_matrix"].to_csv(output_dir / "correlation_matrix.csv", index_label="edge")
        rmt["denoised_correlation_matrix"].to_csv(output_dir / "correlation_matrix_denoised.csv", index_label="edge")
        rmt["eigenvalue_spectrum"].to_csv(output_dir / "eigenvalue_spectrum.csv", index=False)
        rmt_summary.update({k: v for k, v in rmt.items() if k not in {"correlation_matrix", "denoised_correlation_matrix", "eigenvalue_spectrum"}})
    else:
        rmt_summary["reason"] = rmt.get("reason")

    n_pairs = len(scorecard_df)
    n_dependent_pairs = int((scorecard_df["independence_note"].str.startswith("dependence_detected")).sum()) if n_pairs else 0

    manifest = {
        "asset_scope": resolved_assets,
        "mode": mode,
        "deployed_only": deployed_only,
        "threshold_policy": effective_threshold_policy,
        "probability_threshold": effective_probability_threshold,
        "alpha": alpha,
        "n_permutation_cap": n_permutation_cap,
        "seed": seed,
        "edge_count_loaded": len(series_by_edge),
        "edge_ids": list(series_by_edge.keys()),
        "load_errors": load_errors,
        "n_pairs_tested": n_pairs,
        "n_pairs_dependence_detected_at_alpha": n_dependent_pairs,
        "random_matrix_diagnostic": rmt_summary,
        "methodology": {
            "pairwise_test": (
                "Circular-shift permutation test. Edge B's daily return/active-trade "
                "series is circularly shifted by every possible offset (exhaustive) "
                "or, if that exceeds n_permutation_cap, a random sample of that many "
                "offsets, preserving edge B's own autocorrelation/clustering while "
                "nulling any real cross-edge timing relationship. Two-sided empirical "
                "p-values are reported for both the observed Pearson correlation and "
                "the observed max trade-timing overlap fraction."
            ),
            "multiple_comparison_correction": (
                "Benjamini-Hochberg FDR q-values (q_corr_bh, q_overlap_bh) computed "
                "across all pairs tested in this run, since testing many pairs "
                "simultaneously inflates false-positive 'looks dependent' findings."
            ),
            "random_matrix_theory": (
                "Marcenko-Pastur eigenvalue bound fit to the full edge daily-return "
                "correlation matrix (N edges, T observations, q=N/T). Eigenvalues at "
                "or below the bound are statistically indistinguishable from a purely "
                "random correlation matrix of the same shape; eigenvalues above it "
                "indicate genuine common structure. effective_number_of_bets is a "
                "participation-ratio diversification measure (N^2/sum(lambda_i^2))."
            ),
            "not_a_gate": (
                "This lab is diagnostic only. It does not kill, promote, or reweight "
                "edges. Portfolio-level treatment of these statistics is decided "
                "separately in portfolio_allocator/."
            ),
        },
        "outputs": {
            "edge_summary": str(output_dir / "edge_summary.csv"),
            "combined_returns": str(output_dir / "combined_returns.csv"),
            "combined_daily_returns": str(output_dir / "combined_daily_returns.csv"),
            "pairwise_independence_scorecard": str(output_dir / "pairwise_independence_scorecard.csv"),
            "marginal_contribution": str(output_dir / "marginal_contribution.csv"),
            "correlation_matrix": str(output_dir / "correlation_matrix.csv") if rmt.get("applicable") else None,
            "correlation_matrix_denoised": str(output_dir / "correlation_matrix_denoised.csv") if rmt.get("applicable") else None,
            "eigenvalue_spectrum": str(output_dir / "eigenvalue_spectrum.csv") if rmt.get("applicable") else None,
        },
    }
    _write_json(output_dir / "independence_manifest.json", manifest)
    return manifest


def run_asset_independence_lab(
    *,
    asset: str,
    v2_root: str | Path,
    output_dir: str | Path | None = None,
    mode: str = "selected_trades",
    include_non_pass: bool = False,
    include_edges: list[str] | None = None,
    exclude_edges: list[str] | None = None,
    threshold_policy: str = THRESHOLD_POLICY_SELECTED,
    probability_threshold: float | None = None,
    n_permutation_cap: int = DEFAULT_N_PERMUTATION_CAP,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Backward-compatible single-asset entry point (used by each asset's
    alpha_independence_lab/run_asset_independence_lab.py wrapper). Runs the
    same v3 statistical engine scoped to one asset, with deployed_only=False
    so it can still be used for pre-deployment candidate-overlap screening
    (mode=research_events/scored_candidates), not just deployed edges.
    """
    v2_root = Path(v2_root).resolve()
    output_dir = (
        Path(output_dir).resolve()
        if output_dir
        else v2_root / "assets" / asset / "alpha_independence_lab" / "outputs" / mode
    )
    return run_independence_lab(
        v2_root=v2_root,
        assets=[asset],
        output_dir=output_dir,
        mode=mode,
        deployed_only=False,
        include_non_pass=include_non_pass,
        include_edges=include_edges,
        exclude_edges=exclude_edges,
        threshold_policy=threshold_policy,
        probability_threshold=probability_threshold,
        n_permutation_cap=n_permutation_cap,
        seed=seed,
        alpha=alpha,
    )


LIVE_PROBABILITY_COL = "live_calibrated_probability"
DEFAULT_SWEEP_QUANTILE_LEVELS = [0.50, 0.65, 0.80, 0.90, 0.95]


def _load_edge_population(source: EdgeSource, v2_root: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load every side-matched, economically-valid row for an edge from its
    calibrated_oof_predictions.csv, unfiltered by any threshold, keyed on
    live_calibrated_probability (see config/mldp_deviation_register.json D015
    -- live_calibrated_probability is the single-final-sigmoid column that
    live scoring and the portfolio_allocator threshold grid actually use;
    calibrated_probability is a CPCV-fold-averaged research diagnostic that
    does not track raw_probability faithfully and must not be used here).

    Returns a DataFrame with columns [Datetime, r, prob] (one row per
    economically-resolved candidate event) plus a summary dict. This is the
    full population a quantile-based sweep threshold is later applied to.
    """
    df = pd.read_csv(source.data_path)
    datetime_col = _find_col(df, None, DATETIME_CANDIDATES)
    side_col = _find_col(df, None, SIDE_CANDIDATES)
    r_col = _find_col(df, None, R_CANDIDATES)
    prob_col = _find_col(df, None, [LIVE_PROBABILITY_COL])
    if not datetime_col or not r_col:
        raise ValueError(f"{source.edge_id}: missing datetime/R column in {source.data_path}")
    if not prob_col:
        raise ValueError(
            f"{source.edge_id}: {LIVE_PROBABILITY_COL} column not found in {source.data_path} "
            "-- cannot run the live-probability threshold sweep for this edge."
        )

    df[datetime_col] = to_datetime_iso(df[datetime_col], utc=True, errors="coerce")
    df[r_col] = pd.to_numeric(df[r_col], errors="coerce")
    df[prob_col] = pd.to_numeric(df[prob_col], errors="coerce")
    if side_col:
        df[side_col] = pd.to_numeric(df[side_col], errors="coerce")
        if source.side_value is not None:
            df = df.loc[df[side_col] == source.side_value]

    df = df.dropna(subset=[datetime_col, r_col, prob_col]).copy()
    population = pd.DataFrame({
        "Datetime": _coerce_datetime_index(df[datetime_col]),
        "r": df[r_col].to_numpy(dtype="float64"),
        "prob": df[prob_col].to_numpy(dtype="float64"),
    }).sort_values("Datetime").reset_index(drop=True)

    summary = {
        "asset": source.asset,
        "edge_id": source.edge_id,
        "edge_name": source.edge_name,
        "probability_col": prob_col,
        "data_path": str(source.data_path),
        "n_population_rows": int(len(population)),
        "prob_min": float(population["prob"].min()) if len(population) else float("nan"),
        "prob_max": float(population["prob"].max()) if len(population) else float("nan"),
        "start_date": str(population["Datetime"].min()) if len(population) else None,
        "end_date": str(population["Datetime"].max()) if len(population) else None,
    }
    return population, summary


def _population_daily_series(population: pd.DataFrame, quantile_level: float, edge_id: str) -> tuple[pd.Series, dict[str, Any]]:
    """Apply a per-edge quantile cutoff (e.g. 0.90 = keep this edge's own top
    10% most confident rows) to a population frame and return the resulting
    daily-summed R series plus the realized threshold/trade-count stats."""
    if population.empty:
        return pd.Series(dtype="float64", name=edge_id), {"threshold": float("nan"), "n_trades": 0}
    threshold = float(population["prob"].quantile(quantile_level))
    selected = population.loc[population["prob"] >= threshold]
    stats = {
        "threshold_live_calibrated_probability": threshold,
        "n_trades": int(len(selected)),
        "take_rate": float(len(selected) / len(population)) if len(population) else float("nan"),
    }
    if selected.empty:
        return pd.Series(dtype="float64", name=edge_id), stats
    r = selected.set_index(pd.DatetimeIndex(selected["Datetime"]))["r"]
    r = r.groupby(level=0).sum().sort_index()
    r = _ensure_datetime_series(r, edge_id)
    return r, stats


def run_threshold_sweep_independence_lab(
    *,
    v2_root: str | Path,
    assets: list[str] | None = None,
    output_dir: str | Path | None = None,
    deployed_only: bool = True,
    include_edges: list[str] | None = None,
    exclude_edges: list[str] | None = None,
    quantile_levels: list[float] | None = None,
    n_permutation_cap: int = DEFAULT_N_PERMUTATION_CAP,
    seed: int = DEFAULT_SEED,
    alpha: float = DEFAULT_ALPHA,
) -> dict[str, Any]:
    """Threshold-robustness sweep: instead of testing independence at one
    single, non-authoritative per-edge selected_threshold (sparse, and in
    the wrong -- CPCV-fold-averaged -- probability space), test it at
    several quantile cutoffs of each edge's OWN live_calibrated_probability
    distribution (the column live scoring and the portfolio_allocator
    threshold grid actually use). Reports, per pair, whether the
    independence verdict is stable across the sweep or threshold-sensitive
    -- the real question, since the allocator has not fixed a live
    threshold yet and any single fixed cutoff could be wrong.

    Quantile levels are per-edge population quantiles (e.g. 0.90 = each
    edge's own top 10% most confident rows), so edges with different raw
    probability scales remain comparable at each sweep level.
    """
    v2_root = Path(v2_root).resolve()
    levels = list(quantile_levels) if quantile_levels else list(DEFAULT_SWEEP_QUANTILE_LEVELS)
    resolved_assets = _resolve_assets(v2_root, assets)
    output_dir = (
        Path(output_dir).resolve()
        if output_dir
        else v2_root / "shared" / "independence" / "outputs" / "deployed_cross_asset_threshold_sweep"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    all_sources: list[EdgeSource] = []
    for asset in resolved_assets:
        all_sources.extend(_discover_edges(
            v2_root=v2_root,
            asset=asset,
            mode="scored_candidates",
            include_non_pass=False,
            deployed_only=deployed_only,
            include_edges=set(include_edges) if include_edges else None,
            exclude_edges=set(exclude_edges) if exclude_edges else None,
            threshold_policy=THRESHOLD_POLICY_ALL,
            probability_threshold=None,
        ))

    if not all_sources:
        raise RuntimeError(f"No eligible sources found across assets={resolved_assets} (deployed_only={deployed_only}).")

    populations: dict[str, pd.DataFrame] = {}
    population_summaries: list[dict[str, Any]] = []
    load_errors: list[dict[str, str]] = []
    for source in all_sources:
        try:
            population, summary = _load_edge_population(source, v2_root)
            if population.empty:
                load_errors.append({"edge_id": source.edge_id, "error": "no economically-valid rows"})
                continue
            populations[source.edge_id] = population
            population_summaries.append(summary)
        except Exception as exc:
            load_errors.append({"edge_id": source.edge_id, "error": str(exc)})

    if not populations:
        raise RuntimeError(f"No edges could be loaded. Errors: {load_errors}")

    population_summary_df = pd.DataFrame(population_summaries)
    population_summary_df.to_csv(output_dir / "population_summary.csv", index=False)

    level_scorecards: list[pd.DataFrame] = []
    rmt_rows: list[dict[str, Any]] = []
    level_trade_stats: list[dict[str, Any]] = []
    edge_ids = list(populations.keys())

    for level in levels:
        series_by_edge: dict[str, pd.Series] = {}
        summary_by_edge: dict[str, dict[str, Any]] = {}
        for edge_id in edge_ids:
            r_series, trade_stats = _population_daily_series(populations[edge_id], level, edge_id)
            level_trade_stats.append({"quantile_level": level, "edge_id": edge_id, **trade_stats})
            if r_series.empty:
                continue
            series_by_edge[edge_id] = r_series
            asset = edge_id.split("/", 1)[0]
            summary_by_edge[edge_id] = {"asset": asset}

        if len(series_by_edge) < 2:
            continue

        return_df = _aligned_frame(series_by_edge)
        daily_df = _daily_frame(return_df)
        scorecard = _pairwise_scorecard(daily_df, summary_by_edge, n_perm_cap=n_permutation_cap, seed=seed, alpha=alpha)
        if not scorecard.empty:
            scorecard.insert(0, "quantile_level", level)
            level_scorecards.append(scorecard)

        rmt = _random_matrix_diagnostic(daily_df)
        rmt_row: dict[str, Any] = {"quantile_level": level, "applicable": rmt.get("applicable", False)}
        if rmt.get("applicable"):
            rmt_row.update({k: v for k, v in rmt.items() if k not in {"correlation_matrix", "denoised_correlation_matrix", "eigenvalue_spectrum"}})
        rmt_rows.append(rmt_row)

    if not level_scorecards:
        raise RuntimeError("No pairs could be tested at any sweep level (too few edges with active trades).")

    sweep_scorecard = pd.concat(level_scorecards, ignore_index=True)
    sweep_scorecard.to_csv(output_dir / "sweep_pairwise_scorecard.csv", index=False)

    trade_stats_df = pd.DataFrame(level_trade_stats)
    trade_stats_df.to_csv(output_dir / "sweep_trade_counts_by_level.csv", index=False)

    rmt_df = pd.DataFrame(rmt_rows)
    rmt_df.to_csv(output_dir / "sweep_random_matrix_diagnostic.csv", index=False)

    def _robustness_label(group: pd.DataFrame) -> pd.Series:
        dependent = (group["q_corr_bh"] < alpha) | (group["q_overlap_bh"] < alpha)
        n_tested = len(group)
        n_dependent = int(dependent.sum())
        if n_dependent == 0:
            label = "robustly_independent"
        elif n_dependent == n_tested:
            label = "robustly_dependent"
        else:
            label = "threshold_sensitive"
        return pd.Series({
            "n_levels_tested": n_tested,
            "n_levels_dependent": n_dependent,
            "levels_dependent": ",".join(str(v) for v in group.loc[dependent, "quantile_level"].tolist()),
            "min_q_corr_bh": float(group["q_corr_bh"].min()),
            "min_q_overlap_bh": float(group["q_overlap_bh"].min()),
            "robustness_label": label,
        })

    robustness_df = sweep_scorecard.drop_duplicates(["edge_a", "edge_b"])[["edge_a", "edge_b", "asset_a", "asset_b", "same_asset"]].merge(
        (
            sweep_scorecard.groupby(["edge_a", "edge_b"], as_index=False)
            .apply(_robustness_label, include_groups=False)
        ),
        on=["edge_a", "edge_b"],
    )
    label_order = {"robustly_dependent": 0, "threshold_sensitive": 1, "robustly_independent": 2}
    robustness_df["label_rank"] = robustness_df["robustness_label"].map(label_order).fillna(9).astype("int64")
    robustness_df = robustness_df.sort_values(
        ["label_rank", "min_q_corr_bh", "min_q_overlap_bh"]
    ).drop(columns=["label_rank"]).reset_index(drop=True)
    robustness_df.to_csv(output_dir / "pair_robustness_summary.csv", index=False)

    n_robustly_dependent = int((robustness_df["robustness_label"] == "robustly_dependent").sum())
    n_threshold_sensitive = int((robustness_df["robustness_label"] == "threshold_sensitive").sum())
    n_robustly_independent = int((robustness_df["robustness_label"] == "robustly_independent").sum())

    manifest = {
        "asset_scope": resolved_assets,
        "deployed_only": deployed_only,
        "probability_column": LIVE_PROBABILITY_COL,
        "quantile_levels": levels,
        "alpha": alpha,
        "n_permutation_cap": n_permutation_cap,
        "seed": seed,
        "edge_count_loaded": len(populations),
        "edge_ids": edge_ids,
        "load_errors": load_errors,
        "n_pairs": int(len(robustness_df)),
        "n_pairs_robustly_dependent": n_robustly_dependent,
        "n_pairs_threshold_sensitive": n_threshold_sensitive,
        "n_pairs_robustly_independent": n_robustly_independent,
        "methodology": {
            "why_a_sweep": (
                "A single per-edge selected_threshold is a sparse, "
                "non-authoritative diagnostic point (see config/"
                "mldp_deviation_register.json D015 and the edge-default "
                "threshold's own threshold_role note) -- the real live "
                "threshold is decided later by the portfolio_allocator. "
                "Testing independence at every plausible operating point "
                "instead of one arbitrary cutoff answers whether the "
                "independence verdict is robust to that not-yet-made "
                "decision, and uses far more data at every level than the "
                "sparse single-threshold approach."
            ),
            "probability_space": (
                "live_calibrated_probability (raw_probability through the "
                "single final live sigmoid calibrator), NOT calibrated_"
                "probability (a CPCV-fold-averaged research diagnostic that "
                "does not track raw_probability faithfully) -- see D015. "
                "This is the same column live scoring and the "
                "portfolio_allocator threshold grid use."
            ),
            "quantile_levels": (
                "Per-edge population quantiles, not fixed probability "
                "values, so edges with different raw probability scales "
                "stay comparable at each sweep level (e.g. 0.90 always "
                "means 'this edge's own top 10% most confident rows')."
            ),
            "pairwise_test": (
                "Same circular-shift permutation test as the single-"
                "threshold lab (see asset_independence_lab module "
                "docstring), run independently at each quantile level with "
                "its own Benjamini-Hochberg FDR correction across pairs."
            ),
            "robustness_label": (
                "robustly_dependent = dependence detected (q<alpha, either "
                "test) at every level tested. robustly_independent = "
                "dependence detected at no level. threshold_sensitive = "
                "dependence detected at some but not all levels -- the "
                "verdict for this pair depends on where the allocator ends "
                "up setting its threshold."
            ),
            "not_a_gate": "This lab is diagnostic only. It does not kill, promote, or reweight edges.",
        },
        "outputs": {
            "population_summary": str(output_dir / "population_summary.csv"),
            "sweep_pairwise_scorecard": str(output_dir / "sweep_pairwise_scorecard.csv"),
            "sweep_trade_counts_by_level": str(output_dir / "sweep_trade_counts_by_level.csv"),
            "sweep_random_matrix_diagnostic": str(output_dir / "sweep_random_matrix_diagnostic.csv"),
            "pair_robustness_summary": str(output_dir / "pair_robustness_summary.csv"),
        },
    }
    _write_json(output_dir / "independence_sweep_manifest.json", manifest)
    return manifest


def _parse_csv_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the V3 statistical edge independence lab.")
    parser.add_argument("--asset", default=None, help="Single asset (legacy single-asset mode).")
    parser.add_argument("--assets", default=None, help="Comma-separated assets for cross-asset mode. Omit for all assets.")
    parser.add_argument("--cross-asset", action="store_true", help="Run the cross-asset deployed-edge lab instead of single-asset legacy mode.")
    parser.add_argument("--sweep", action="store_true", help="Run the live_calibrated_probability threshold-robustness sweep instead of a single-threshold lab.")
    parser.add_argument("--quantile-levels", default=None, help="Comma-separated per-edge population quantiles for --sweep (default: 0.50,0.65,0.80,0.90,0.95).")
    parser.add_argument("--v2-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--mode", choices=["selected_trades", "research_events", "scored_candidates"], default="selected_trades")
    parser.add_argument("--deployed-only", dest="deployed_only", action="store_true", default=True)
    parser.add_argument("--include-non-deployed", dest="deployed_only", action="store_false")
    parser.add_argument("--include-non-pass", action="store_true")
    parser.add_argument("--include-edges", default=None, help="Comma-separated edge names or edge_ids to include.")
    parser.add_argument("--exclude-edges", default=None, help="Comma-separated edge names or edge_ids to exclude.")
    parser.add_argument(
        "--threshold-policy",
        choices=THRESHOLD_POLICIES,
        default=THRESHOLD_POLICY_SELECTED,
    )
    parser.add_argument("--probability-threshold", type=float, default=None)
    parser.add_argument("--n-permutation-cap", type=int, default=DEFAULT_N_PERMUTATION_CAP)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    return parser


def _print_sweep_summary(manifest: dict[str, Any]) -> None:
    print("V3 statistical edge independence THRESHOLD SWEEP completed.")
    print(f"Assets: {manifest['asset_scope']}")
    print(f"Probability column: {manifest['probability_column']}")
    print(f"Quantile levels: {manifest['quantile_levels']}")
    print(f"Deployed only: {manifest['deployed_only']}")
    print(f"Edges loaded: {manifest['edge_count_loaded']}")
    for name in manifest["edge_ids"]:
        print(f"- {name}")
    print(f"Pairs tested: {manifest['n_pairs']}")
    print(f"Robustly dependent (dependent at every level): {manifest['n_pairs_robustly_dependent']}")
    print(f"Threshold-sensitive (dependent at some levels only): {manifest['n_pairs_threshold_sensitive']}")
    print(f"Robustly independent (dependent at no level): {manifest['n_pairs_robustly_independent']}")
    print("Outputs:")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")
    if manifest["load_errors"]:
        print("Load warnings/errors:")
        for err in manifest["load_errors"]:
            print(f"- {err['edge_id']}: {err['error']}")


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)

    if args.sweep:
        manifest = run_threshold_sweep_independence_lab(
            v2_root=args.v2_root,
            assets=_parse_csv_list(args.assets),
            output_dir=args.output_dir,
            deployed_only=args.deployed_only,
            include_edges=_parse_csv_list(args.include_edges),
            exclude_edges=_parse_csv_list(args.exclude_edges),
            quantile_levels=[float(v) for v in _parse_csv_list(args.quantile_levels)] if args.quantile_levels else None,
            n_permutation_cap=args.n_permutation_cap,
            seed=args.seed,
            alpha=args.alpha,
        )
        _print_sweep_summary(manifest)
        return

    if args.cross_asset or (args.asset is None and args.assets is not None):
        manifest = run_independence_lab(
            v2_root=args.v2_root,
            assets=_parse_csv_list(args.assets),
            output_dir=args.output_dir,
            mode=args.mode,
            deployed_only=args.deployed_only,
            include_non_pass=args.include_non_pass,
            include_edges=_parse_csv_list(args.include_edges),
            exclude_edges=_parse_csv_list(args.exclude_edges),
            threshold_policy=args.threshold_policy,
            probability_threshold=args.probability_threshold,
            n_permutation_cap=args.n_permutation_cap,
            seed=args.seed,
            alpha=args.alpha,
        )
    else:
        manifest = run_asset_independence_lab(
            asset=args.asset or "XAUUSD",
            v2_root=args.v2_root,
            output_dir=args.output_dir,
            mode=args.mode,
            include_non_pass=args.include_non_pass,
            include_edges=_parse_csv_list(args.include_edges),
            exclude_edges=_parse_csv_list(args.exclude_edges),
            threshold_policy=args.threshold_policy,
            probability_threshold=args.probability_threshold,
            n_permutation_cap=args.n_permutation_cap,
            seed=args.seed,
            alpha=args.alpha,
        )

    print("V3 statistical edge independence lab completed.")
    print(f"Assets: {manifest.get('asset_scope', manifest.get('asset'))}")
    print(f"Mode: {manifest['mode']}")
    print(f"Deployed only: {manifest.get('deployed_only')}")
    print(f"Edges loaded: {manifest['edge_count_loaded']}")
    for name in manifest["edge_ids"]:
        print(f"- {name}")
    print(f"Pairs tested: {manifest['n_pairs_tested']}")
    print(f"Pairs with dependence detected (alpha={manifest['alpha']}): {manifest['n_pairs_dependence_detected_at_alpha']}")
    rmt = manifest.get("random_matrix_diagnostic", {})
    if rmt.get("applicable"):
        print(
            f"Effective number of independent bets (denoised): "
            f"{rmt['effective_number_of_bets_denoised']:.2f} out of {rmt['n_edges']} edges "
            f"({rmt['n_signal_eigenvalues']} signal eigenvalue(s))."
        )
    print("Outputs:")
    for name, path in manifest["outputs"].items():
        print(f"{name}: {path}")
    if manifest["load_errors"]:
        print("Load warnings/errors:")
        for err in manifest["load_errors"]:
            print(f"- {err['edge_id']}: {err['error']}")


if __name__ == "__main__":
    main()
