from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
ALLOCATOR_DIR = V2_ROOT / "portfolio_allocator"
OUT = ALLOCATOR_DIR / "outputs"

STEP04C_PATH = ALLOCATOR_DIR / "04c_select_representative_threshold_candidates.py"
STEP08_PATH = ALLOCATOR_DIR / "08_rolling_start_prop_challenge_optimizer.py"

PLATEAU_PATH = OUT / "v2_threshold_plateau_report.csv"
SELECTION_PATH = OUT / "v2_representative_threshold_edge_selection_report.csv"
LEDGER_PATH = OUT / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv"

GATE_DETAIL_PATH = OUT / "v2_rejected_edge_gate_diagnostics.csv"
GATE_SUMMARY_PATH = OUT / "v2_rejected_edge_gate_failure_summary.csv"
DECOMPOSITION_PATH = OUT / "v2_rejected_edge_compounding_decomposition.csv"
CAP_SWEEP_PATH = OUT / "v2_rejected_edge_step08_cap_sweep.csv"
ATTRIBUTION_PATH = OUT / "v2_rejected_edge_failure_attribution.csv"
MANIFEST_PATH = OUT / "v2_rejected_edge_diagnostics_manifest.json"

GATE_DETAIL_COLUMNS = [
    "edge_id", "asset", "family", "side_name", "edge_classification",
    "closest_threshold", "grid_candidate_id", "viable_threshold_rows",
    "fallback_gate_pass_count", "fallback_gate_fail_count", "failed_gates",
    "trade_count", "active_exit_days", "trades_per_active_day",
    "stressed_expectancy_lcb_r", "stressed_profit_factor",
    "local_positive_ratio", "broker_optimized_compound_velocity_calendar",
    "broker_optimized_drawdown_adjusted_compound_velocity",
    "broker_optimized_survival_feasible",
]
GATE_SUMMARY_COLUMNS = ["failed_gate", "rejected_edges_affected"]
CAP_SWEEP_COLUMNS = [
    "edge_id", "threshold", "grid_candidate_id",
    "max_single_trade_risk_cap_pct", "best_f", "positive_growth",
    "log_growth", "velocity_calendar", "drawdown_adjusted_velocity",
    "max_drawdown_abs_log", "accepted_trade_count",
    "skipped_risk_cap_count", "accepted_rate",
    "accepted_min_lot_binding_count", "accepted_min_lot_binding_rate",
    "median_accepted_actual_risk_pct", "p90_accepted_actual_risk_pct",
    "median_accepted_risk_inflation",
]
ATTRIBUTION_COLUMNS = [
    "edge_id", "threshold", "failure_attribution",
    "failure_attribution_reason", "raw_positive",
    "afml_weighted_positive", "continuous_growth_positive",
    "current_broker_growth_positive", "step08_cap_0p25_growth_positive",
    "min_lot_binding_rate_at_continuous_f",
    "actual_risk_r_correlation_at_continuous_f",
]
DECOMPOSITION_COLUMNS = [
    "edge_id", "asset", "family", "side_name", "edge_classification",
    "threshold", "grid_candidate_id", "trade_count", "active_days",
    "edge_span_days", "compound_cost_stress_r",
    "account_balance_used_by_04c", "raw_expectancy_r",
    "raw_profit_factor", "raw_cumulative_r", "raw_max_drawdown_r",
    "raw_max_drawdown_abs_r", "afml_mean", "afml_median",
    "afml_zero_rate", "afml_weighted_expectancy_r",
    "afml_weighted_profit_factor", "afml_weighted_cumulative_r",
    "afml_weighted_max_drawdown_r", "afml_weighted_max_drawdown_abs_r",
    "f_search_upper", "f_search_upper_reason", "f_search_points",
    "continuous_best_f", "continuous_best_log_growth",
    "continuous_best_velocity_calendar",
    "continuous_best_drawdown_adjusted_velocity",
    "continuous_best_max_drawdown_abs_log", "current_broker_best_f",
    "current_broker_log_growth", "current_broker_velocity_calendar",
    "current_broker_drawdown_adjusted_velocity",
    "current_broker_max_drawdown_abs_log", "best_nonzero_rounded_f",
    "best_nonzero_rounded_log_growth",
    "best_nonzero_rounded_velocity_calendar",
    "rounded_at_continuous_f_log_growth",
    "rounded_at_continuous_f_velocity_calendar",
    "rounded_at_continuous_f_drawdown_adjusted_velocity",
    "step08_cap_0p25_best_f", "step08_cap_0p25_best_log_growth",
    "step08_cap_0p25_best_velocity_calendar",
    "step08_cap_0p25_best_drawdown_adjusted_velocity",
    "step08_cap_0p25_accepted_count", "step08_cap_0p25_skipped_count",
    "best_positive_step08_cap", "best_positive_step08_cap_f",
    "best_positive_step08_cap_velocity_calendar", "diagnostic_f",
    "active_target_trade_count", "min_lot_binding_count",
    "min_lot_binding_rate", "median_target_risk_pct",
    "median_actual_risk_pct", "p90_actual_risk_pct",
    "max_actual_risk_pct", "median_risk_inflation",
    "p90_risk_inflation", "max_risk_inflation",
    "winner_mean_actual_risk_pct", "loser_mean_actual_risk_pct",
    "winner_to_loser_risk_ratio", "actual_risk_r_correlation",
    "failure_attribution", "failure_attribution_reason",
]

EPS = 1e-12
DEFAULT_STEP08_CAPS = [0.0025, 0.0050, 0.0075, 0.0100, 0.0125, 0.0150, 0.0175, 0.0200]
PRIMARY_STEP08_CAP = 0.0025

FALLBACK_LIMITS = {
    "trade_count": 750.0,
    "active_exit_days": 150.0,
    "trades_per_active_day": 0.15,
    "stressed_expectancy_lcb_r": 0.0,
    "stressed_profit_factor": 1.0,
    "local_positive_ratio": 0.50,
    "broker_optimized_compound_velocity_calendar": 0.0,
    "broker_optimized_drawdown_adjusted_compound_velocity": 0.0,
}

GATE_LABELS = {
    "trade_count": "trade_count_below_750",
    "active_exit_days": "active_days_below_150",
    "trades_per_active_day": "trades_per_active_day_below_0p15",
    "stressed_expectancy_lcb_r": "stressed_expectancy_lcb_nonpositive",
    "stressed_profit_factor": "stressed_profit_factor_below_1p00",
    "local_positive_ratio": "local_positive_ratio_below_0p50",
    "broker_optimized_compound_velocity_calendar": "broker_calendar_velocity_nonpositive",
    "broker_optimized_drawdown_adjusted_compound_velocity": "broker_drawdown_adjusted_velocity_nonpositive",
    "broker_optimized_survival_feasible": "broker_survival_infeasible",
}


def load_module(path: Path, name: str) -> ModuleType:
    if not path.exists():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def normalize_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def number(value: Any, default: float = np.nan) -> float:
    try:
        return float(value)
    except Exception:
        return default


def finite_number(value: Any, default: float = np.nan) -> float:
    x = number(value, default)
    return x if np.isfinite(x) else default


def safe_div(a: Any, b: Any) -> float:
    aa = finite_number(a)
    bb = finite_number(b)
    if not np.isfinite(aa) or not np.isfinite(bb) or abs(bb) <= EPS:
        return np.nan
    return aa / bb


def numeric(df: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def profit_factor(values: np.ndarray) -> float:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return np.nan
    gross_profit = float(x[x > 0].sum()) if np.any(x > 0) else 0.0
    gross_loss = float(abs(x[x <= 0].sum())) if np.any(x <= 0) else 0.0
    if gross_loss <= EPS:
        return np.inf if gross_profit > 0 else np.nan
    return gross_profit / gross_loss


def max_drawdown_arithmetic(values: np.ndarray) -> tuple[float, float]:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 0.0, 0.0
    curve = np.cumsum(x)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], curve)))[:-1]
    dd = curve - peaks
    mdd = float(min(0.0, np.nanmin(dd)))
    return mdd, abs(mdd)


def max_drawdown_log(log_returns: np.ndarray) -> tuple[float, float, float]:
    x = np.asarray(log_returns, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 0.0, 0.0, 0.0
    curve = np.cumsum(x)
    peaks = np.maximum.accumulate(np.concatenate(([0.0], curve)))[:-1]
    dd = curve - peaks
    mdd = float(min(0.0, np.nanmin(dd)))
    abs_log = abs(mdd)
    pct = float(1.0 - np.exp(-abs_log)) if abs_log > 0 else 0.0
    return mdd, abs_log, pct


def percentile(values: np.ndarray, q: float) -> float:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.nanpercentile(x, q)) if x.size else np.nan


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    x = np.asarray(a, dtype=float)
    y = np.asarray(b, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    x = x[mask]
    y = y[mask]
    if x.size < 3 or np.std(x) <= EPS or np.std(y) <= EPS:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def parse_time(df: pd.DataFrame) -> pd.Series:
    for column in ("exit_time", "event_time", "entry_time"):
        if column in df.columns:
            ts = to_datetime_iso(df[column], utc=True, errors="coerce")
            if ts.notna().any():
                return ts
    return pd.Series(pd.NaT, index=df.index)


def sort_trades(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    ts = parse_time(work)
    if ts.notna().any():
        work["_sort_time"] = ts
        work = work.sort_values("_sort_time", kind="mergesort", na_position="last")
        work = work.drop(columns="_sort_time")
    return work.reset_index(drop=True)


def time_counts(df: pd.DataFrame) -> tuple[int, int]:
    ts = parse_time(df).dropna()
    if ts.empty:
        return 1, 1
    active = int(max(1, ts.dt.floor("D").nunique()))
    span = int(max(1, (ts.max().floor("D") - ts.min().floor("D")).days + 1))
    return active, span


def load_step08_caps() -> list[float]:
    if not STEP08_PATH.exists():
        return DEFAULT_STEP08_CAPS.copy()
    try:
        module = load_module(STEP08_PATH, "_step08_for_04ca")
        values = getattr(module, "MAX_SINGLE_TRADE_RISK_PCTS", DEFAULT_STEP08_CAPS)
        caps = sorted({float(x) for x in values if float(x) > 0})
        return caps or DEFAULT_STEP08_CAPS.copy()
    except Exception:
        return DEFAULT_STEP08_CAPS.copy()


def build_gate_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    formula_count = numeric(out, "formula_trade_count", np.nan)
    base_count = numeric(out, "trade_count", 0.0)
    count = formula_count.where(formula_count.notna(), base_count)

    out["gate_trade_count"] = count >= FALLBACK_LIMITS["trade_count"]
    out["gate_active_days"] = numeric(out, "active_exit_days", 0.0) >= FALLBACK_LIMITS["active_exit_days"]
    out["gate_trades_per_active_day"] = numeric(out, "trades_per_active_day", 0.0) >= FALLBACK_LIMITS["trades_per_active_day"]
    out["gate_expectancy_lcb"] = numeric(out, "stressed_expectancy_lcb_r", -np.inf) > 0.0
    out["gate_profit_factor"] = numeric(out, "stressed_profit_factor", -np.inf) >= FALLBACK_LIMITS["stressed_profit_factor"]
    out["gate_local_positive"] = numeric(out, "local_positive_ratio", -np.inf) >= FALLBACK_LIMITS["local_positive_ratio"]
    out["gate_calendar_velocity"] = numeric(out, "broker_optimized_compound_velocity_calendar", -np.inf) > 0.0
    out["gate_drawdown_adjusted_velocity"] = numeric(out, "broker_optimized_drawdown_adjusted_compound_velocity", -np.inf) > 0.0
    out["gate_survival"] = (
        out["broker_optimized_survival_feasible"].apply(normalize_bool)
        if "broker_optimized_survival_feasible" in out.columns
        else False
    )

    gate_columns = [
        "gate_trade_count",
        "gate_active_days",
        "gate_trades_per_active_day",
        "gate_expectancy_lcb",
        "gate_profit_factor",
        "gate_local_positive",
        "gate_calendar_velocity",
        "gate_drawdown_adjusted_velocity",
        "gate_survival",
    ]
    out["fallback_gate_pass_count"] = out[gate_columns].sum(axis=1).astype(int)
    out["fallback_gate_fail_count"] = len(gate_columns) - out["fallback_gate_pass_count"]
    return out


def failed_gates(row: pd.Series) -> list[str]:
    pairs = [
        ("gate_trade_count", GATE_LABELS["trade_count"]),
        ("gate_active_days", GATE_LABELS["active_exit_days"]),
        ("gate_trades_per_active_day", GATE_LABELS["trades_per_active_day"]),
        ("gate_expectancy_lcb", GATE_LABELS["stressed_expectancy_lcb_r"]),
        ("gate_profit_factor", GATE_LABELS["stressed_profit_factor"]),
        ("gate_local_positive", GATE_LABELS["local_positive_ratio"]),
        ("gate_calendar_velocity", GATE_LABELS["broker_optimized_compound_velocity_calendar"]),
        ("gate_drawdown_adjusted_velocity", GATE_LABELS["broker_optimized_drawdown_adjusted_compound_velocity"]),
        ("gate_survival", GATE_LABELS["broker_optimized_survival_feasible"]),
    ]
    return [label for column, label in pairs if not bool(row[column])]


def closest_threshold(group: pd.DataFrame) -> pd.Series:
    work = group.copy()
    work["_dd_velocity"] = numeric(work, "broker_optimized_drawdown_adjusted_compound_velocity", -np.inf).fillna(-np.inf)
    work["_calendar_velocity"] = numeric(work, "broker_optimized_compound_velocity_calendar", -np.inf).fillna(-np.inf)
    work["_expectancy_lcb"] = numeric(work, "stressed_expectancy_lcb_r", -np.inf).fillna(-np.inf)
    work["_profit_factor"] = numeric(work, "stressed_profit_factor", -np.inf).replace(np.inf, 10.0).fillna(-np.inf)
    work["_local_positive"] = numeric(work, "local_positive_ratio", -np.inf).fillna(-np.inf)
    formula_count = numeric(work, "formula_trade_count", np.nan)
    work["_trade_count"] = formula_count.where(formula_count.notna(), numeric(work, "trade_count", 0.0))
    work["_drawdown"] = numeric(work, "broker_optimized_max_drawdown_abs_log", np.inf).fillna(np.inf)
    return work.sort_values(
        [
            "fallback_gate_fail_count",
            "fallback_gate_pass_count",
            "_dd_velocity",
            "_calendar_velocity",
            "_expectancy_lcb",
            "_profit_factor",
            "_local_positive",
            "_trade_count",
            "_drawdown",
            "threshold",
        ],
        ascending=[True, False, False, False, False, False, False, False, True, False],
        na_position="last",
    ).iloc[0]


def continuous_eval(
    f: float,
    r: np.ndarray,
    afml: np.ndarray,
    active_days: int,
    span_days: int,
) -> dict[str, Any]:
    target = max(float(f), 0.0) * afml
    growth_inputs = 1.0 + target * r
    feasible = bool(
        growth_inputs.size
        and np.all(np.isfinite(growth_inputs))
        and np.min(growth_inputs) > EPS
    )
    if not feasible:
        return {
            "f": float(f),
            "feasible": False,
            "log_growth": -np.inf,
            "velocity_calendar": -np.inf,
            "velocity_active": -np.inf,
            "score": -np.inf,
            "mdd_abs_log": np.inf,
            "mdd_pct": np.inf,
        }
    logs = np.log(growth_inputs)
    log_growth = float(logs.sum())
    velocity_calendar = safe_div(log_growth, span_days)
    velocity_active = safe_div(log_growth, active_days)
    _, mdd_abs_log, mdd_pct = max_drawdown_log(logs)
    score = safe_div(velocity_calendar, max(mdd_abs_log, EPS)) if velocity_calendar > 0 else -np.inf
    return {
        "f": float(f),
        "feasible": True,
        "log_growth": log_growth,
        "velocity_calendar": velocity_calendar,
        "velocity_active": velocity_active,
        "score": score,
        "mdd_abs_log": mdd_abs_log,
        "mdd_pct": mdd_pct,
    }


def rounded_eval(
    step04c: ModuleType,
    f: float,
    r: np.ndarray,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
    active_days: int,
    span_days: int,
    cap: float | None = None,
) -> dict[str, Any]:
    target, actual, actual_amount, raw_lot = step04c.broker_actual_risk_pct_for_f(
        float(f), afml, risk_per_1_lot, min_lot, lot_step
    )

    positive_target = target > EPS
    valid_actual = np.isfinite(actual) & (actual > EPS)
    if cap is None:
        accepted = positive_target & valid_actual
        skipped_cap = np.zeros_like(accepted, dtype=bool)
    else:
        accepted = positive_target & valid_actual & (actual <= float(cap) + EPS)
        skipped_cap = positive_target & valid_actual & ~accepted

    invalid = positive_target & ~valid_actual
    accepted_count = int(accepted.sum())
    target_count = int(positive_target.sum())

    if accepted_count == 0:
        return {
            "f": float(f),
            "cap": cap,
            "feasible": True,
            "accepted_count": 0,
            "skipped_cap_count": int(skipped_cap.sum()),
            "invalid_count": int(invalid.sum()),
            "accepted_rate": 0.0,
            "log_growth": 0.0,
            "velocity_calendar": 0.0,
            "velocity_active": 0.0,
            "score": -np.inf,
            "mdd_abs_log": 0.0,
            "mdd_pct": 0.0,
            "target_risk_pct": target,
            "actual_risk_pct": actual,
            "actual_risk_amount": actual_amount,
            "raw_lot": raw_lot,
            "accepted_mask": accepted,
        }

    growth_inputs = 1.0 + actual[accepted] * r[accepted]
    feasible = bool(
        np.all(np.isfinite(growth_inputs))
        and np.min(growth_inputs) > EPS
    )
    if not feasible:
        return {
            "f": float(f),
            "cap": cap,
            "feasible": False,
            "accepted_count": accepted_count,
            "skipped_cap_count": int(skipped_cap.sum()),
            "invalid_count": int(invalid.sum()),
            "accepted_rate": safe_div(accepted_count, target_count),
            "log_growth": -np.inf,
            "velocity_calendar": -np.inf,
            "velocity_active": -np.inf,
            "score": -np.inf,
            "mdd_abs_log": np.inf,
            "mdd_pct": np.inf,
            "target_risk_pct": target,
            "actual_risk_pct": actual,
            "actual_risk_amount": actual_amount,
            "raw_lot": raw_lot,
            "accepted_mask": accepted,
        }

    logs = np.log(growth_inputs)
    log_growth = float(logs.sum())
    velocity_calendar = safe_div(log_growth, span_days)
    velocity_active = safe_div(log_growth, active_days)
    _, mdd_abs_log, mdd_pct = max_drawdown_log(logs)
    score = safe_div(velocity_calendar, max(mdd_abs_log, EPS)) if velocity_calendar > 0 else -np.inf
    return {
        "f": float(f),
        "cap": cap,
        "feasible": True,
        "accepted_count": accepted_count,
        "skipped_cap_count": int(skipped_cap.sum()),
        "invalid_count": int(invalid.sum()),
        "accepted_rate": safe_div(accepted_count, target_count),
        "log_growth": log_growth,
        "velocity_calendar": velocity_calendar,
        "velocity_active": velocity_active,
        "score": score,
        "mdd_abs_log": mdd_abs_log,
        "mdd_pct": mdd_pct,
        "target_risk_pct": target,
        "actual_risk_pct": actual,
        "actual_risk_amount": actual_amount,
        "raw_lot": raw_lot,
        "accepted_mask": accepted,
    }


def select_best(
    evaluations: list[dict[str, Any]],
    *,
    require_positive_score: bool,
) -> dict[str, Any]:
    pool = [
        e for e in evaluations
        if bool(e.get("feasible"))
        and number(e.get("f"), 0.0) > EPS
        and np.isfinite(number(e.get("velocity_calendar"), -np.inf))
    ]
    if require_positive_score:
        pool = [
            e for e in pool
            if np.isfinite(number(e.get("score"), -np.inf))
            and number(e.get("score"), -np.inf) > 0
        ]
    if not pool:
        return {}
    return max(
        pool,
        key=lambda e: (
            number(e.get("score"), -np.inf)
            if require_positive_score
            else number(e.get("velocity_calendar"), -np.inf),
            number(e.get("velocity_calendar"), -np.inf),
            -number(e.get("mdd_abs_log"), np.inf),
            number(e.get("f"), -np.inf),
        ),
    )


def refine_continuous(
    grid: np.ndarray,
    coarse_best: dict[str, Any],
    r: np.ndarray,
    afml: np.ndarray,
    active_days: int,
    span_days: int,
    points: int,
) -> dict[str, Any]:
    if not coarse_best:
        return {}
    grid = np.sort(np.unique(np.asarray(grid, dtype=float)))
    if grid.size < 2:
        return coarse_best
    f0 = number(coarse_best.get("f"), 0.0)
    idx = int(np.argmin(np.abs(grid - f0)))
    lo = float(grid[max(0, idx - 1)])
    hi = float(grid[min(grid.size - 1, idx + 1)])
    if hi <= lo:
        return coarse_best
    refined = np.linspace(lo, hi, max(3, int(points)))
    evals = [continuous_eval(float(f), r, afml, active_days, span_days) for f in refined]
    best = select_best(evals + [coarse_best], require_positive_score=True)
    return best or coarse_best


def risk_diagnostics(
    step04c: ModuleType,
    f: float,
    r: np.ndarray,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
) -> dict[str, Any]:
    target, actual, _, raw_lot = step04c.broker_actual_risk_pct_for_f(
        float(f), afml, risk_per_1_lot, min_lot, lot_step
    )
    active = (target > EPS) & np.isfinite(actual)
    if not np.any(active):
        return {
            "diagnostic_f": float(f),
            "active_target_trade_count": 0,
            "min_lot_binding_count": 0,
            "min_lot_binding_rate": np.nan,
            "median_target_risk_pct": np.nan,
            "median_actual_risk_pct": np.nan,
            "p90_actual_risk_pct": np.nan,
            "max_actual_risk_pct": np.nan,
            "median_risk_inflation": np.nan,
            "p90_risk_inflation": np.nan,
            "max_risk_inflation": np.nan,
            "winner_mean_actual_risk_pct": np.nan,
            "loser_mean_actual_risk_pct": np.nan,
            "winner_to_loser_risk_ratio": np.nan,
            "actual_risk_r_correlation": np.nan,
        }

    binding = active & (raw_lot < min_lot - EPS)
    inflation = actual[active] / np.maximum(target[active], EPS)
    wins = active & (r > 0)
    losses = active & (r <= 0)
    winner_risk = float(actual[wins].mean()) if np.any(wins) else np.nan
    loser_risk = float(actual[losses].mean()) if np.any(losses) else np.nan

    return {
        "diagnostic_f": float(f),
        "active_target_trade_count": int(active.sum()),
        "min_lot_binding_count": int(binding.sum()),
        "min_lot_binding_rate": safe_div(int(binding.sum()), int(active.sum())),
        "median_target_risk_pct": percentile(target[active], 50),
        "median_actual_risk_pct": percentile(actual[active], 50),
        "p90_actual_risk_pct": percentile(actual[active], 90),
        "max_actual_risk_pct": float(actual[active].max()),
        "median_risk_inflation": percentile(inflation, 50),
        "p90_risk_inflation": percentile(inflation, 90),
        "max_risk_inflation": float(inflation.max()),
        "winner_mean_actual_risk_pct": winner_risk,
        "loser_mean_actual_risk_pct": loser_risk,
        "winner_to_loser_risk_ratio": safe_div(winner_risk, loser_risk),
        "actual_risk_r_correlation": correlation(actual[active], r[active]),
    }


def classify(row: dict[str, Any]) -> tuple[str, str]:
    raw_positive = row["raw_expectancy_r"] > 0 and row["raw_profit_factor"] > 1
    afml_positive = row["afml_weighted_expectancy_r"] > 0 and row["afml_weighted_profit_factor"] > 1
    continuous_positive = row["continuous_best_velocity_calendar"] > 0
    current_positive = row["current_broker_velocity_calendar"] > 0
    rounded_at_cont_positive = row["rounded_at_continuous_f_velocity_calendar"] > 0
    cap025_positive = row["step08_cap_0p25_best_velocity_calendar"] > 0

    if not raw_positive:
        return (
            "raw_edge_not_positive",
            "The closest 04B-viable threshold is not positive in raw executed-R space.",
        )
    if not afml_positive:
        return (
            "afml_sizing_reversal",
            "Raw R-space is positive, but AFML-weighted R-space is nonpositive.",
        )
    if not continuous_positive:
        return (
            "continuous_geometric_drag",
            "Raw and AFML-weighted arithmetic edge are positive, but the tested continuous f-domain has no positive geometric-growth point.",
        )
    if current_positive:
        return (
            "selection_or_output_mapping_issue",
            "The current broker optimiser recomputes positive growth although the edge is rejected; inspect selection/output mapping.",
        )
    if cap025_positive:
        return (
            "step08_policy_mismatch_rescues_edge",
            "Continuous growth is positive and Step-08-style 0.25% cap skipping restores positive broker growth, while 04C always-execute rounding rejects it.",
        )
    if not rounded_at_cont_positive:
        return (
            "broker_rounding_or_minlot_damage",
            "Continuous unrounded growth is positive, but broker rounding/minimum-lot execution makes growth nonpositive.",
        )
    return (
        "optimizer_or_grid_discrepancy",
        "Broker-rounded growth at the continuous optimum is positive, but the current optimiser still returns zero; inspect the f-grid/search.",
    )


def empty_diagnostic_result(
    *,
    reason: str,
    configured_cost: float,
    caps: list[float],
    step04c: ModuleType,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Return schema-preserving PASS outputs when there is nothing to diagnose.

    Step 04CA is an optional rejection diagnostic. A portfolio with no rejected
    edges is a valid upstream outcome and must not break the ordered pipeline.
    """
    manifest = {
        "status": "PASS",
        "analysis_status": reason,
        "script": THIS_FILE.name,
        "artifact_file_hash_coupling": "none",
        "semantic_fingerprints_retained": ["trade_set_hash", "geometry_hash"],
        "step04c_compound_cost_stress_r": configured_cost,
        "step04c_account_balance": float(getattr(step04c, "CURRENT_BALANCE")),
        "rejected_edges_analyzed": 0,
        "step08_cap_grid": caps,
        "primary_step08_style_cap": PRIMARY_STEP08_CAP,
        "inputs": {
            "step04c": str(STEP04C_PATH),
            "plateau_report": str(PLATEAU_PATH),
            "selection_report": str(SELECTION_PATH),
            "grid_trade_ledger": str(LEDGER_PATH),
        },
        "outputs": {
            "gate_detail": str(GATE_DETAIL_PATH),
            "gate_summary": str(GATE_SUMMARY_PATH),
            "decomposition": str(DECOMPOSITION_PATH),
            "cap_sweep": str(CAP_SWEEP_PATH),
            "attribution": str(ATTRIBUTION_PATH),
        },
        "notes": [
            "No rejected 04C edge required decomposition; empty outputs retain stable CSV schemas.",
        ],
    }
    return (
        pd.DataFrame(columns=GATE_DETAIL_COLUMNS),
        pd.DataFrame(columns=GATE_SUMMARY_COLUMNS),
        pd.DataFrame(columns=DECOMPOSITION_COLUMNS),
        pd.DataFrame(columns=CAP_SWEEP_COLUMNS),
        pd.DataFrame(columns=ATTRIBUTION_COLUMNS),
        manifest,
    )


def build() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    step04c = load_module(STEP04C_PATH, "_step04c_for_04ca")

    configured_cost = number(getattr(step04c, "COMPOUND_COST_STRESS_R", np.nan))
    if not np.isfinite(configured_cost) or abs(configured_cost) > EPS:
        raise RuntimeError(
            "This audit requires local 04C to use COMPOUND_COST_STRESS_R = 0.00. "
            f"Imported value: {configured_cost!r}. Update 04C, rerun 04C, then rerun 04CA."
        )

    for path in (PLATEAU_PATH, SELECTION_PATH, LEDGER_PATH):
        if not path.exists():
            raise FileNotFoundError(f"Missing required file: {path}")

    # float_precision="round_trip": see config/mldp_deviation_register.json --
    # pandas' default C float parser is not always bit-exact.
    plateau = pd.read_csv(PLATEAU_PATH, float_precision="round_trip")
    selection = pd.read_csv(SELECTION_PATH, float_precision="round_trip")
    ledger = pd.read_csv(LEDGER_PATH, float_precision="round_trip")

    required_selection = {"edge_id", "selection_status"}
    if not required_selection.issubset(selection.columns):
        raise ValueError(
            f"Selection report missing: {sorted(required_selection - set(selection.columns))}"
        )

    required_ledger = {
        "grid_candidate_id",
        "edge_id",
        "executed_r",
        "afml_bet_size",
        "risk_per_1_lot",
        "broker_min_lot",
        "broker_lot_step",
    }
    if not required_ledger.issubset(ledger.columns):
        raise ValueError(
            f"Trade ledger missing: {sorted(required_ledger - set(ledger.columns))}"
        )

    caps = load_step08_caps()
    if PRIMARY_STEP08_CAP not in caps:
        caps = sorted(set(caps + [PRIMARY_STEP08_CAP]))

    rejected_ids = set(
        selection.loc[
            selection["selection_status"].astype(str) != "selected",
            "edge_id",
        ].astype(str)
    )
    if not rejected_ids:
        return empty_diagnostic_result(
            reason="NO_REJECTED_EDGES",
            configured_cost=configured_cost,
            caps=caps,
            step04c=step04c,
        )

    rejected = plateau[plateau["edge_id"].astype(str).isin(rejected_ids)].copy()
    if "passes_grid_gates" in rejected.columns:
        rejected = rejected[rejected["passes_grid_gates"].apply(normalize_bool)].copy()
    if rejected.empty:
        return empty_diagnostic_result(
            reason="NO_04B_VIABLE_REJECTED_THRESHOLDS",
            configured_cost=configured_cost,
            caps=caps,
            step04c=step04c,
        )

    evaluated = build_gate_frame(rejected)

    gate_rows: list[dict[str, Any]] = []
    decomposition_rows: list[dict[str, Any]] = []
    cap_rows: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []

    for edge_id, threshold_group in evaluated.groupby("edge_id", sort=True):
        best_threshold = closest_threshold(threshold_group)
        grid_id = str(best_threshold["grid_candidate_id"])
        trades = ledger[ledger["grid_candidate_id"].astype(str) == grid_id].copy()
        trades = sort_trades(trades)

        valid = (
            pd.to_numeric(trades["executed_r"], errors="coerce").notna()
            & pd.to_numeric(trades["afml_bet_size"], errors="coerce").notna()
            & pd.to_numeric(trades["risk_per_1_lot"], errors="coerce").gt(EPS)
            & pd.to_numeric(trades["broker_min_lot"], errors="coerce").gt(EPS)
            & pd.to_numeric(trades["broker_lot_step"], errors="coerce").gt(EPS)
        )
        if trades.empty or not valid.all():
            raise ValueError(
                f"Invalid ledger rows for edge={edge_id}, grid_candidate_id={grid_id}: "
                f"{int((~valid).sum()) if len(trades) else 'no rows'}"
            )

        r = pd.to_numeric(trades["executed_r"], errors="raise").to_numpy(dtype=float)
        afml = pd.to_numeric(trades["afml_bet_size"], errors="raise").clip(0.0, 1.0).to_numpy(dtype=float)
        risk_per_1_lot = pd.to_numeric(trades["risk_per_1_lot"], errors="raise").to_numpy(dtype=float)
        min_lot = pd.to_numeric(trades["broker_min_lot"], errors="raise").to_numpy(dtype=float)
        lot_step = pd.to_numeric(trades["broker_lot_step"], errors="raise").to_numpy(dtype=float)
        n_active_days, n_span_days = time_counts(trades)

        raw_mdd, raw_mdd_abs = max_drawdown_arithmetic(r)
        afml_r = afml * r
        afml_mdd, afml_mdd_abs = max_drawdown_arithmetic(afml_r)

        f_upper, f_upper_reason = step04c.broker_f_upper_from_survival(
            r, afml, risk_per_1_lot, min_lot, lot_step
        )
        f_grid = step04c.broker_f_search_grid(
            f_upper, afml, risk_per_1_lot, min_lot
        )

        continuous_evals = [
            continuous_eval(float(f), r, afml, n_active_days, n_span_days)
            for f in f_grid
            if float(f) > EPS
        ]
        continuous_best = select_best(
            continuous_evals,
            require_positive_score=True,
        )
        if not continuous_best:
            continuous_best = select_best(
                continuous_evals,
                require_positive_score=False,
            )
        if continuous_best:
            continuous_best = refine_continuous(
                f_grid,
                continuous_best,
                r,
                afml,
                n_active_days,
                n_span_days,
                int(getattr(step04c, "BROKER_OPTIMIZED_F_REFINED_GRID_SIZE", 201)),
            )

        current_broker = step04c.optimize_broker_drawdown_adjusted_f(
            r,
            afml,
            risk_per_1_lot,
            min_lot,
            lot_step,
            n_active_days,
            n_span_days,
        )

        rounded_evals = [
            rounded_eval(
                step04c,
                float(f),
                r,
                afml,
                risk_per_1_lot,
                min_lot,
                lot_step,
                n_active_days,
                n_span_days,
            )
            for f in f_grid
            if float(f) > EPS
        ]
        rounded_best = select_best(rounded_evals, require_positive_score=False)

        diagnostic_f = finite_number(
            continuous_best.get("f") if continuous_best else np.nan
        )
        if not np.isfinite(diagnostic_f) or diagnostic_f <= EPS:
            diagnostic_f = finite_number(
                rounded_best.get("f") if rounded_best else np.nan,
                0.0,
            )

        rounded_at_continuous = rounded_eval(
            step04c,
            diagnostic_f,
            r,
            afml,
            risk_per_1_lot,
            min_lot,
            lot_step,
            n_active_days,
            n_span_days,
        )
        risk_diag = risk_diagnostics(
            step04c,
            diagnostic_f,
            r,
            afml,
            risk_per_1_lot,
            min_lot,
            lot_step,
        )

        cap_best: dict[float, dict[str, Any]] = {}
        for cap in caps:
            evaluations = [
                rounded_eval(
                    step04c,
                    float(f),
                    r,
                    afml,
                    risk_per_1_lot,
                    min_lot,
                    lot_step,
                    n_active_days,
                    n_span_days,
                    cap=float(cap),
                )
                for f in f_grid
                if float(f) > EPS
            ]
            best_cap = select_best(evaluations, require_positive_score=True)
            if not best_cap:
                best_cap = select_best(evaluations, require_positive_score=False)
            cap_best[float(cap)] = best_cap

            accepted = (
                np.asarray(best_cap.get("accepted_mask"), dtype=bool)
                if best_cap and "accepted_mask" in best_cap
                else np.zeros(len(r), dtype=bool)
            )
            target = (
                np.asarray(best_cap.get("target_risk_pct"), dtype=float)
                if best_cap and "target_risk_pct" in best_cap
                else np.full(len(r), np.nan)
            )
            actual = (
                np.asarray(best_cap.get("actual_risk_pct"), dtype=float)
                if best_cap and "actual_risk_pct" in best_cap
                else np.full(len(r), np.nan)
            )
            raw_lot = (
                np.asarray(best_cap.get("raw_lot"), dtype=float)
                if best_cap and "raw_lot" in best_cap
                else np.full(len(r), np.nan)
            )
            binding = accepted & (raw_lot < min_lot - EPS)

            cap_rows.append(
                {
                    "edge_id": edge_id,
                    "threshold": finite_number(best_threshold.get("threshold")),
                    "grid_candidate_id": grid_id,
                    "max_single_trade_risk_cap_pct": float(cap),
                    "best_f": finite_number(best_cap.get("f") if best_cap else np.nan),
                    "positive_growth": bool(
                        number(best_cap.get("velocity_calendar") if best_cap else np.nan, -np.inf) > 0
                    ),
                    "log_growth": number(best_cap.get("log_growth") if best_cap else np.nan),
                    "velocity_calendar": number(best_cap.get("velocity_calendar") if best_cap else np.nan),
                    "drawdown_adjusted_velocity": number(best_cap.get("score") if best_cap else np.nan),
                    "max_drawdown_abs_log": number(best_cap.get("mdd_abs_log") if best_cap else np.nan),
                    "accepted_trade_count": int(best_cap.get("accepted_count", 0) if best_cap else 0),
                    "skipped_risk_cap_count": int(best_cap.get("skipped_cap_count", 0) if best_cap else 0),
                    "accepted_rate": finite_number(best_cap.get("accepted_rate") if best_cap else np.nan),
                    "accepted_min_lot_binding_count": int(binding.sum()),
                    "accepted_min_lot_binding_rate": safe_div(int(binding.sum()), int(accepted.sum())),
                    "median_accepted_actual_risk_pct": percentile(actual[accepted], 50),
                    "p90_accepted_actual_risk_pct": percentile(actual[accepted], 90),
                    "median_accepted_risk_inflation": percentile(
                        actual[accepted] / np.maximum(target[accepted], EPS),
                        50,
                    ),
                }
            )

        cap025 = cap_best.get(PRIMARY_STEP08_CAP, {})
        positive_caps = [
            value
            for value in cap_best.values()
            if value and number(value.get("velocity_calendar"), -np.inf) > 0
        ]
        best_positive_cap = max(
            positive_caps,
            key=lambda value: (
                number(value.get("score"), -np.inf),
                number(value.get("velocity_calendar"), -np.inf),
            ),
            default={},
        )

        gate_rows.append(
            {
                "edge_id": edge_id,
                "asset": best_threshold.get("asset", ""),
                "family": best_threshold.get("family", ""),
                "side_name": best_threshold.get("side_name", ""),
                "edge_classification": best_threshold.get("edge_classification", ""),
                "closest_threshold": finite_number(best_threshold.get("threshold")),
                "grid_candidate_id": grid_id,
                "viable_threshold_rows": int(len(threshold_group)),
                "fallback_gate_pass_count": int(best_threshold["fallback_gate_pass_count"]),
                "fallback_gate_fail_count": int(best_threshold["fallback_gate_fail_count"]),
                "failed_gates": ";".join(failed_gates(best_threshold)),
                "trade_count": finite_number(
                    best_threshold.get(
                        "formula_trade_count",
                        best_threshold.get("trade_count", len(r)),
                    )
                ),
                "active_exit_days": finite_number(best_threshold.get("active_exit_days")),
                "trades_per_active_day": finite_number(best_threshold.get("trades_per_active_day")),
                "stressed_expectancy_lcb_r": finite_number(best_threshold.get("stressed_expectancy_lcb_r")),
                "stressed_profit_factor": number(best_threshold.get("stressed_profit_factor")),
                "local_positive_ratio": finite_number(best_threshold.get("local_positive_ratio")),
                "broker_optimized_compound_velocity_calendar": number(
                    best_threshold.get("broker_optimized_compound_velocity_calendar")
                ),
                "broker_optimized_drawdown_adjusted_compound_velocity": number(
                    best_threshold.get("broker_optimized_drawdown_adjusted_compound_velocity")
                ),
                "broker_optimized_survival_feasible": normalize_bool(
                    best_threshold.get("broker_optimized_survival_feasible")
                ),
            }
        )

        decomposition = {
            "edge_id": edge_id,
            "asset": best_threshold.get("asset", ""),
            "family": best_threshold.get("family", ""),
            "side_name": best_threshold.get("side_name", ""),
            "edge_classification": best_threshold.get("edge_classification", ""),
            "threshold": finite_number(best_threshold.get("threshold")),
            "grid_candidate_id": grid_id,
            "trade_count": int(len(r)),
            "active_days": int(n_active_days),
            "edge_span_days": int(n_span_days),
            "compound_cost_stress_r": configured_cost,
            "account_balance_used_by_04c": float(getattr(step04c, "CURRENT_BALANCE")),
            "raw_expectancy_r": float(r.mean()),
            "raw_profit_factor": profit_factor(r),
            "raw_cumulative_r": float(r.sum()),
            "raw_max_drawdown_r": raw_mdd,
            "raw_max_drawdown_abs_r": raw_mdd_abs,
            "afml_mean": float(afml.mean()),
            "afml_median": float(np.median(afml)),
            "afml_zero_rate": float(np.mean(afml <= EPS)),
            "afml_weighted_expectancy_r": float(afml_r.mean()),
            "afml_weighted_profit_factor": profit_factor(afml_r),
            "afml_weighted_cumulative_r": float(afml_r.sum()),
            "afml_weighted_max_drawdown_r": afml_mdd,
            "afml_weighted_max_drawdown_abs_r": afml_mdd_abs,
            "f_search_upper": finite_number(f_upper),
            "f_search_upper_reason": str(f_upper_reason),
            "f_search_points": int(len(f_grid)),
            "continuous_best_f": finite_number(
                continuous_best.get("f") if continuous_best else np.nan
            ),
            "continuous_best_log_growth": number(
                continuous_best.get("log_growth") if continuous_best else np.nan
            ),
            "continuous_best_velocity_calendar": number(
                continuous_best.get("velocity_calendar") if continuous_best else np.nan
            ),
            "continuous_best_drawdown_adjusted_velocity": number(
                continuous_best.get("score") if continuous_best else np.nan
            ),
            "continuous_best_max_drawdown_abs_log": number(
                continuous_best.get("mdd_abs_log") if continuous_best else np.nan
            ),
            "current_broker_best_f": finite_number(current_broker.get("f")),
            "current_broker_log_growth": number(current_broker.get("log_growth")),
            "current_broker_velocity_calendar": number(current_broker.get("velocity_calendar")),
            "current_broker_drawdown_adjusted_velocity": number(current_broker.get("score")),
            "current_broker_max_drawdown_abs_log": number(current_broker.get("mdd_abs_log")),
            "best_nonzero_rounded_f": finite_number(
                rounded_best.get("f") if rounded_best else np.nan
            ),
            "best_nonzero_rounded_log_growth": number(
                rounded_best.get("log_growth") if rounded_best else np.nan
            ),
            "best_nonzero_rounded_velocity_calendar": number(
                rounded_best.get("velocity_calendar") if rounded_best else np.nan
            ),
            "rounded_at_continuous_f_log_growth": number(
                rounded_at_continuous.get("log_growth")
            ),
            "rounded_at_continuous_f_velocity_calendar": number(
                rounded_at_continuous.get("velocity_calendar")
            ),
            "rounded_at_continuous_f_drawdown_adjusted_velocity": number(
                rounded_at_continuous.get("score")
            ),
            "step08_cap_0p25_best_f": finite_number(cap025.get("f") if cap025 else np.nan),
            "step08_cap_0p25_best_log_growth": number(
                cap025.get("log_growth") if cap025 else np.nan
            ),
            "step08_cap_0p25_best_velocity_calendar": number(
                cap025.get("velocity_calendar") if cap025 else np.nan
            ),
            "step08_cap_0p25_best_drawdown_adjusted_velocity": number(
                cap025.get("score") if cap025 else np.nan
            ),
            "step08_cap_0p25_accepted_count": int(cap025.get("accepted_count", 0) if cap025 else 0),
            "step08_cap_0p25_skipped_count": int(cap025.get("skipped_cap_count", 0) if cap025 else 0),
            "best_positive_step08_cap": finite_number(
                best_positive_cap.get("cap") if best_positive_cap else np.nan
            ),
            "best_positive_step08_cap_f": finite_number(
                best_positive_cap.get("f") if best_positive_cap else np.nan
            ),
            "best_positive_step08_cap_velocity_calendar": number(
                best_positive_cap.get("velocity_calendar") if best_positive_cap else np.nan
            ),
            **risk_diag,
        }

        failure_code, failure_reason = classify(decomposition)
        decomposition["failure_attribution"] = failure_code
        decomposition["failure_attribution_reason"] = failure_reason
        decomposition_rows.append(decomposition)

        attribution_rows.append(
            {
                "edge_id": edge_id,
                "threshold": decomposition["threshold"],
                "failure_attribution": failure_code,
                "failure_attribution_reason": failure_reason,
                "raw_positive": bool(
                    decomposition["raw_expectancy_r"] > 0
                    and decomposition["raw_profit_factor"] > 1
                ),
                "afml_weighted_positive": bool(
                    decomposition["afml_weighted_expectancy_r"] > 0
                    and decomposition["afml_weighted_profit_factor"] > 1
                ),
                "continuous_growth_positive": bool(
                    decomposition["continuous_best_velocity_calendar"] > 0
                ),
                "current_broker_growth_positive": bool(
                    decomposition["current_broker_velocity_calendar"] > 0
                ),
                "step08_cap_0p25_growth_positive": bool(
                    decomposition["step08_cap_0p25_best_velocity_calendar"] > 0
                ),
                "min_lot_binding_rate_at_continuous_f": decomposition["min_lot_binding_rate"],
                "actual_risk_r_correlation_at_continuous_f": decomposition["actual_risk_r_correlation"],
            }
        )

    gate_detail = pd.DataFrame(gate_rows, columns=GATE_DETAIL_COLUMNS)
    if not gate_detail.empty:
        gate_detail = gate_detail.sort_values(
            ["fallback_gate_fail_count", "edge_id"]
        ).reset_index(drop=True)

    counts: dict[str, int] = {}
    for labels in gate_detail["failed_gates"].astype(str):
        for label in [item for item in labels.split(";") if item]:
            counts[label] = counts.get(label, 0) + 1
    gate_summary = pd.DataFrame(
        [
            {"failed_gate": key, "rejected_edges_affected": value}
            for key, value in counts.items()
        ],
        columns=GATE_SUMMARY_COLUMNS,
    )
    if not gate_summary.empty:
        gate_summary = gate_summary.sort_values(
            ["rejected_edges_affected", "failed_gate"],
            ascending=[False, True],
        ).reset_index(drop=True)

    decomposition_df = pd.DataFrame(
        decomposition_rows, columns=DECOMPOSITION_COLUMNS
    )
    if not decomposition_df.empty:
        decomposition_df = decomposition_df.sort_values(
            ["failure_attribution", "edge_id"]
        ).reset_index(drop=True)
    cap_df = pd.DataFrame(cap_rows, columns=CAP_SWEEP_COLUMNS)
    if not cap_df.empty:
        cap_df = cap_df.sort_values(
            ["edge_id", "max_single_trade_risk_cap_pct"]
        ).reset_index(drop=True)
    attribution_df = pd.DataFrame(
        attribution_rows, columns=ATTRIBUTION_COLUMNS
    )
    if not attribution_df.empty:
        attribution_df = attribution_df.sort_values(
            ["failure_attribution", "edge_id"]
        ).reset_index(drop=True)

    manifest = {
        "status": "PASS",
        "script": THIS_FILE.name,
        "artifact_file_hash_coupling": "none",
        "semantic_fingerprints_retained": ["trade_set_hash", "geometry_hash"],
        "step04c_compound_cost_stress_r": configured_cost,
        "step04c_account_balance": float(getattr(step04c, "CURRENT_BALANCE")),
        "rejected_edges_analyzed": int(decomposition_df["edge_id"].nunique()),
        "step08_cap_grid": caps,
        "primary_step08_style_cap": PRIMARY_STEP08_CAP,
        "inputs": {
            "step04c": str(STEP04C_PATH),
            "plateau_report": str(PLATEAU_PATH),
            "selection_report": str(SELECTION_PATH),
            "grid_trade_ledger": str(LEDGER_PATH),
        },
        "method": {
            "raw": "Unweighted executed_r.",
            "afml_weighted": "afml_bet_size multiplied by executed_r.",
            "continuous": "Unrounded target risk f multiplied by AFML over the same 04C f-domain.",
            "rounded": "04C broker minimum-lot and lot-step rounding with all positive-target trades executed.",
            "step08_style": "Same broker rounding, but trades are skipped when actual risk exceeds the tested max-single-trade cap.",
        },
        "outputs": {
            "gate_detail": str(GATE_DETAIL_PATH),
            "gate_summary": str(GATE_SUMMARY_PATH),
            "decomposition": str(DECOMPOSITION_PATH),
            "cap_sweep": str(CAP_SWEEP_PATH),
            "attribution": str(ATTRIBUTION_PATH),
        },
    }
    return (
        gate_detail,
        gate_summary,
        decomposition_df,
        cap_df,
        attribution_df,
        manifest,
    )


def main() -> None:
    (
        gate_detail,
        gate_summary,
        decomposition,
        cap_sweep,
        attribution,
        manifest,
    ) = build()

    OUT.mkdir(parents=True, exist_ok=True)
    gate_detail.to_csv(GATE_DETAIL_PATH, index=False)
    gate_summary.to_csv(GATE_SUMMARY_PATH, index=False)
    decomposition.to_csv(DECOMPOSITION_PATH, index=False)
    cap_sweep.to_csv(CAP_SWEEP_PATH, index=False)
    attribution.to_csv(ATTRIBUTION_PATH, index=False)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print("V2 Phase B2.04CA rejected-edge compounding decomposition")
    print("Status: PASS")
    if manifest.get("analysis_status"):
        print(f"Analysis status: {manifest['analysis_status']}")
    print(f"Rejected edges analyzed: {manifest['rejected_edges_analyzed']}")
    print(f"04C cost stress confirmed: {manifest['step04c_compound_cost_stress_r']:.4f}R")
    print(f"04C account balance: ${manifest['step04c_account_balance']:,.2f}")

    print("\nFailure attribution:")
    if attribution.empty:
        print("No rejected edges required failure attribution.")
    else:
        summary = (
            attribution["failure_attribution"]
            .value_counts(dropna=False)
            .rename_axis("failure_attribution")
            .reset_index(name="edges")
        )
        print(summary.to_string(index=False))

    columns = [
        "edge_id",
        "threshold",
        "raw_expectancy_r",
        "afml_weighted_expectancy_r",
        "continuous_best_velocity_calendar",
        "current_broker_velocity_calendar",
        "rounded_at_continuous_f_velocity_calendar",
        "step08_cap_0p25_best_velocity_calendar",
        "min_lot_binding_rate",
        "median_risk_inflation",
        "winner_mean_actual_risk_pct",
        "loser_mean_actual_risk_pct",
        "actual_risk_r_correlation",
        "failure_attribution",
    ]
    print("\nEdge-by-edge decomposition:")
    if decomposition.empty:
        print("No rejected edges required decomposition.")
    else:
        print(decomposition[columns].to_string(index=False))

    print("\nOutputs:")
    print(f"  Gate detail:   {GATE_DETAIL_PATH}")
    print(f"  Gate summary:  {GATE_SUMMARY_PATH}")
    print(f"  Decomposition: {DECOMPOSITION_PATH}")
    print(f"  Cap sweep:     {CAP_SWEEP_PATH}")
    print(f"  Attribution:   {ATTRIBUTION_PATH}")


if __name__ == "__main__":
    main()
