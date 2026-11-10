from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = lambda x, **kwargs: x

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.validation.v3_semantic_contract import validate_payoff_context_frame  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1
SCHEMA_VERSION = "v2.2"
PIPELINE_MODE = "v2_manifest_driven_calibrated_afml_calendar_broker_optimized_drawdown_adjusted_compound_velocity"
PROBABILITY_COLUMN = "calibrated_probability"
AFML_COLUMN = "afml_bet_size"
ROLE_ORDER = ["compound_velocity"]

# AFML Ch.12 walkthrough (owner decision D052, 2026-10-26): AFML s.12.4.2 step 5 / s.12.5 -- judge every threshold on the
# distribution of its CPCV backtest paths, not on one path. 04B streams every threshold's trades on every path; here each
# (threshold, path) is scored with the same broker-executable growth formula at the main path's optimized f*, and the
# selection score becomes the MEAN path score. The primary/fallback gates stay on the main (averaged) path; the worst path
# and the share of losing paths are reported. Fail closed if the path ledger is missing, unless --allow-legacy-single-path.
CPCV_PATH_LEDGER_NAME = "v2_threshold_grid_cpcv_path_trade_ledger.csv"
CPCV_PATH_READ_CHUNK_ROWS = 500_000
REQUIRE_CPCV_PATHS = True
CPCV_SELECTION_SCORE_BASIS = "cpcv_path_mean_drawdown_adjusted_compound_velocity_at_main_path_f (AFML s.12.4.2/12.5, D052)"

PLATEAU_JACCARD_MIN = 0.93
PLATEAU_TRADE_COUNT_REL_DIFF_MAX = 0.12
PLATEAU_CUMULATIVE_R_REL_DIFF_MAX = 0.10
PLATEAU_EXPECTANCY_ABS_DIFF_MAX = 0.035
PLATEAU_CALMAR_REL_DIFF_MAX = 0.35

EPS = 1e-12
CURRENT_BALANCE = 100_000.00
TARGET_BALANCE = 105_000.00
DRAWDOWN_FLOOR = 92_000.00
USABLE_DRAWDOWN_SAFETY_FRACTION = 0.50
COMPOUND_COST_STRESS_R = 0.00
BROKER_OPTIMIZED_F_COARSE_GRID_SIZE = 241
BROKER_OPTIMIZED_F_REFINED_GRID_SIZE = 201
BROKER_OPTIMIZED_F_UPPER_SAFETY_FRACTION = 0.999
BROKER_OPTIMIZED_F_LOG_FLOOR_FRACTION = 1e-8
EXPECTANCY_LCB_Z = 1.00
ROBUST_PROFIT_FACTOR_CAP = 10.0

BROKER_EXECUTABLE_RISK_REQUIRED = True
BROKER_RISK_ROUNDING_MODE = "ceil_to_lot_step_with_min_lot"
BROKER_RISK_METADATA_REQUIRED_COLUMNS = [
    "risk_per_1_lot",
    "broker_min_lot",
    "broker_lot_step",
    "has_valid_broker_min_lot_risk",
]
BROKER_RISK_DIAGNOSTIC_COLUMNS = [
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
    "broker_contract_size",
    "broker_min_lot_risk_status",
]

PRIMARY_MIN_TRADE_COUNT = 960
FALLBACK_MIN_TRADE_COUNT = 750
PRIMARY_MIN_ACTIVE_DAYS = 250
FALLBACK_MIN_ACTIVE_DAYS = 150
PRIMARY_MIN_TRADES_PER_ACTIVE_DAY = 0.25
FALLBACK_MIN_TRADES_PER_ACTIVE_DAY = 0.15
PRIMARY_MIN_STRESSED_PROFIT_FACTOR = 1.05
FALLBACK_MIN_STRESSED_PROFIT_FACTOR = 1.00
PRIMARY_MIN_LOCAL_POSITIVE_RATIO = 0.55
FALLBACK_MIN_LOCAL_POSITIVE_RATIO = 0.50

PHASE3B_CONTEXT_REQUIRED_COLUMNS = [
    "payoff_context_version", "payoff_context_id", "payoff_context_edge_id",
    "gross_label_contract_version", "execution_contract_version",
    "executed_target_contract_version", "geometry_hash", "afml_sizing_method",
    "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
    "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count",
    "payoff_zero_rate", "min_wins_required", "min_losses_required",
    "empirical_avg_win_r", "empirical_avg_loss_r",
    "empirical_reward_risk_ratio", "empirical_break_even_probability",
]

REPRESENTATIVE_REQUIRED_COLUMNS = [
    "representative_id", "candidate_id", "source_grid_candidate_id", "grid_candidate_id",
    "edge_id", "asset", "family", "side_name", "side_value", "edge_classification",
    "candidate_type", "representative_roles", "primary_representative_role", "role_count",
    "threshold", "calibrated_threshold", "threshold_probability_column", "afml_bet_size_column",
    "calibrated_oof_path", "threshold_sweep_path", "edge_manifest_path",
    "trade_count", "expectancy_r", "profit_factor", "cumulative_r", "max_drawdown_r",
    "calmar_r", "avg_calibrated_probability", "avg_afml_bet_size",
    *PHASE3B_CONTEXT_REQUIRED_COLUMNS,
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def normalize_bool(x: Any) -> bool:
    return str(x).strip().lower() in {"true", "1", "yes", "y"}


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()[:n]


def clean_threshold_for_id(threshold: float) -> str:
    return f"{float(threshold):.2f}".replace(".", "p")


def clean_edge_for_id(edge_id: str) -> str:
    return str(edge_id).replace("/", "_").replace(" ", "_")


def trade_set_hash(keys: set[str]) -> str:
    return short_hash("|".join(sorted(keys)), 20) if keys else ""


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    union = len(a.union(b))
    return float(len(a.intersection(b)) / union) if union else 0.0


def rel_diff(a: Any, b: Any) -> float:
    a = safe_float(a)
    b = safe_float(b)
    if not np.isfinite(a) or not np.isfinite(b):
        return np.inf
    return abs(a - b) / max(abs(a), abs(b), EPS)


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        if abs(b) < EPS or not np.isfinite(b):
            return np.nan
        return float(a) / b
    except Exception:
        return np.nan


def max_drawdown_from_values(values: np.ndarray) -> float:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return 0.0
    equity = np.cumsum(x)
    dd = equity - np.maximum.accumulate(equity)
    return float(dd.min()) if dd.size else 0.0


def profit_factor_from_values(values: np.ndarray) -> float:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return np.nan
    gross_profit = float(x[x > 0].sum()) if np.any(x > 0) else 0.0
    gross_loss = float(abs(x[x <= 0].sum())) if np.any(x <= 0) else 0.0
    if gross_loss <= EPS:
        return np.inf if gross_profit > 0 else np.nan
    return gross_profit / gross_loss


def parse_time_series(s: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(s, utc=True, errors="coerce", format="mixed")
    except TypeError:
        return to_datetime_iso(s, utc=True, errors="coerce")


def span_days_from_times(ts: pd.Series) -> int:
    t = to_datetime_iso(ts, utc=True, errors="coerce").dropna()
    if t.empty:
        return 0
    return int(max(1, (t.max().floor("D") - t.min().floor("D")).days + 1))


def usable_drawdown_budget_pct() -> float:
    remaining = max(0.0, CURRENT_BALANCE - DRAWDOWN_FLOOR)
    return (remaining / max(CURRENT_BALANCE, EPS)) * USABLE_DRAWDOWN_SAFETY_FRACTION


def target_log_return() -> float:
    return float(np.log(TARGET_BALANCE / CURRENT_BALANCE)) if TARGET_BALANCE > CURRENT_BALANCE > 0 else np.nan


def load_grid_viability(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 04b_build_threshold_grid_trade_ledger.py first. Missing: {path}")
    # float_precision="round_trip": pandas' default C float parser is not
    # always bit-exact, which breaks payoff_context_id hash identity checks
    # that hash these fields at full float64 precision. See
    # config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    required = [
        "grid_candidate_id", "edge_id", "asset", "family", "side_name", "side_value",
        "edge_classification", "classification_reason", "threshold", "trade_count",
        "expectancy_r", "profit_factor", "cumulative_r", "max_drawdown_r", "calmar_r",
        "avg_pred_prob", "avg_afml_bet_size", "local_positive_ratio",
        "threshold_probability_column", "afml_bet_size_column", "calibrated_oof_path",
        "threshold_sweep_path", "edge_manifest_path", "passes_grid_gates",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")
    if df.empty:
        raise ValueError("Threshold-grid viability file is empty.")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    numeric = [
        "side_value", "threshold", "take_rate", "precision", "avg_pred_prob", "avg_afml_bet_size",
        "trade_count", "win_rate", "expectancy_r", "profit_factor", "gross_profit_r",
        "gross_loss_r", "cumulative_r", "max_drawdown_r", "calmar_r", "local_positive_ratio",
        "local_expectancy_mean", "local_pf_mean", "classification_rank_score",
        "max_trade_count_in_edge_sweep", "min_trade_count_required",
    ]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["passes_grid_gates"] = df["passes_grid_gates"].apply(normalize_bool)
    df["edge_classification"] = df["edge_classification"].astype(str).str.lower().str.strip()
    if df["grid_candidate_id"].duplicated().any():
        bad = df[df["grid_candidate_id"].duplicated(keep=False)]
        raise ValueError("Duplicate grid_candidate_id rows:\n" + bad[["grid_candidate_id", "edge_id", "threshold"]].to_string(index=False))
    if set(df["threshold_probability_column"].dropna().unique()) != {PROBABILITY_COLUMN}:
        raise ValueError("Threshold-grid viability must use calibrated_probability only.")
    if set(df["afml_bet_size_column"].dropna().unique()) != {AFML_COLUMN}:
        raise ValueError("Threshold-grid viability must use afml_bet_size only.")
    validate_payoff_context_frame(df, context="04C threshold-grid viability")
    return df.sort_values(["asset", "family", "edge_id", "threshold"]).reset_index(drop=True)


def load_grid_ledger_summary(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 04b_build_threshold_grid_trade_ledger.py first. Missing: {path}")
    df = pd.read_csv(path, float_precision="round_trip")
    required = [
        "grid_candidate_id", "ledger_trade_count", "ledger_expectancy_r", "ledger_cumulative_r",
        "ledger_max_drawdown_r", "ledger_avg_calibrated_probability", "ledger_avg_afml_bet_size",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} missing required columns: {missing}")
    if df["grid_candidate_id"].duplicated().any():
        raise ValueError("Duplicate grid_candidate_id in threshold-grid ledger summary.")
    return df



def _empty_formula_stats() -> dict[str, Any]:
    return {
        "formula_trade_count": 0,
        "active_exit_days": 0,
        "calendar_span_days": 0,
        "edge_backtest_span_days": 0,
        "trades_per_active_day": np.nan,
        "trades_per_calendar_day": np.nan,
        "stressed_expectancy_r": np.nan,
        "stressed_expectancy_lcb_r": np.nan,
        "stressed_profit_factor": np.nan,
        "stressed_log_profit_factor": np.nan,
        "stressed_cumulative_r": 0.0,
        "stressed_max_drawdown_r": 0.0,
        "stressed_max_drawdown_abs_r": 0.0,
        "afml_weighted_stressed_expectancy_r": np.nan,
        "afml_weighted_stressed_profit_factor": np.nan,
        "afml_weighted_stressed_cumulative_r": 0.0,
        "afml_weighted_stressed_max_drawdown_r": 0.0,
        "compound_usable_drawdown_budget_pct": usable_drawdown_budget_pct(),
        "compound_allowed_risk_per_trade": np.nan,
        "compound_log_growth": np.nan,
        "compound_velocity": np.nan,
        "compound_velocity_calendar": np.nan,
        "compound_velocity_active": np.nan,
        "compound_equity_return_proxy": np.nan,
        "compound_power": np.nan,
        "expected_active_days_to_target": np.nan,
        "expected_calendar_days_to_target": np.nan,
        "arithmetic_compound_speed_proxy": np.nan,
        "arithmetic_compound_speed_calendar": np.nan,

        # Legacy/diagnostic columns retained for backward-friendly CSVs.
        "compound_log_growth_theoretical": np.nan,
        "compound_velocity_theoretical": np.nan,
        "compound_velocity_calendar_theoretical": np.nan,
        "compound_velocity_active_theoretical": np.nan,
        "compound_equity_return_proxy_theoretical": np.nan,
        "expected_active_days_to_target_theoretical": np.nan,
        "expected_calendar_days_to_target_theoretical": np.nan,

        # Broker-optimized drawdown-aware objective.
        "broker_executable_risk_required": BROKER_EXECUTABLE_RISK_REQUIRED,
        "broker_optimized_f": np.nan,
        "broker_optimized_f_search_upper": np.nan,
        "broker_optimized_f_search_points": 0,
        "broker_optimized_f_search_upper_reason": "",
        "broker_optimized_objective": "drawdown_adjusted_compound_velocity",
        "broker_optimized_log_growth": np.nan,
        "broker_optimized_compound_velocity": np.nan,
        "broker_optimized_compound_velocity_calendar": np.nan,
        "broker_optimized_compound_velocity_active": np.nan,
        "broker_optimized_drawdown_adjusted_compound_velocity": np.nan,
        "broker_optimized_drawdown_adjusted_log_growth": np.nan,
        "broker_optimized_max_drawdown_log": np.nan,
        "broker_optimized_max_drawdown_abs_log": np.nan,
        "broker_optimized_max_drawdown_pct": np.nan,
        "broker_optimized_equity_return_proxy": np.nan,
        "broker_optimized_expected_active_days_to_target": np.nan,
        "broker_optimized_expected_calendar_days_to_target": np.nan,
        "broker_optimized_bankruptcy_margin_min": np.nan,
        "broker_optimized_survival_feasible": False,

        # Backward-friendly aliases for prior broker executable names.
        "compound_log_growth_broker_executable": np.nan,
        "compound_velocity_broker_executable": np.nan,
        "compound_velocity_calendar_broker_executable": np.nan,
        "compound_velocity_active_broker_executable": np.nan,
        "compound_equity_return_proxy_broker_executable": np.nan,
        "expected_active_days_to_target_broker_executable": np.nan,
        "expected_calendar_days_to_target_broker_executable": np.nan,

        "broker_valid_risk_row_count": 0,
        "broker_invalid_risk_row_count": 0,
        "broker_valid_risk_row_rate": np.nan,
        "broker_min_lot_binding_trade_count": 0,
        "broker_min_lot_binding_trade_rate": np.nan,
        "broker_median_target_risk_amount": np.nan,
        "broker_median_actual_risk_amount": np.nan,
        "broker_p90_actual_risk_amount": np.nan,
        "broker_max_actual_risk_amount": np.nan,
        "broker_median_risk_inflation": np.nan,
        "broker_p90_risk_inflation": np.nan,
        "broker_max_risk_inflation": np.nan,
        "broker_mean_actual_risk_pct": np.nan,
        "broker_median_actual_risk_pct": np.nan,
        "broker_p90_actual_risk_pct": np.nan,
        "broker_max_actual_risk_pct": np.nan,
        "broker_mean_target_risk_pct": np.nan,
        "broker_median_target_risk_pct": np.nan,
        "broker_p90_target_risk_pct": np.nan,
        "broker_max_target_risk_pct": np.nan,
        "broker_risk_metadata_status_summary": "",
    }


def pct_or_nan(numer: int, denom: int) -> float:
    return float(numer / denom) if denom else np.nan


def percentile_or_nan(values: np.ndarray, q: float) -> float:
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return np.nan
    return float(np.nanpercentile(x, q))


def ceil_to_lot_step(volume: np.ndarray, lot_step: np.ndarray) -> np.ndarray:
    step = np.asarray(lot_step, dtype=float)
    vol = np.asarray(volume, dtype=float)
    return np.ceil(np.maximum(vol, 0.0) / step) * step


def broker_executable_risk_amounts(
    target_risk_amount: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return actual risk, actual lot, and raw lot for target risk amounts.

    Trades with target_risk_amount <= 0 remain zero-risk. Positive targets are
    rounded up to the broker lot step and at least broker min_lot.
    """
    target = np.asarray(target_risk_amount, dtype=float)
    risk_1 = np.asarray(risk_per_1_lot, dtype=float)
    min_volume = np.asarray(min_lot, dtype=float)
    step = np.asarray(lot_step, dtype=float)
    valid = (target > EPS) & (risk_1 > EPS) & (min_volume > EPS) & (step > EPS)
    raw_lot = np.zeros_like(target, dtype=float)
    actual_lot = np.zeros_like(target, dtype=float)
    actual_risk = np.zeros_like(target, dtype=float)
    raw_lot[valid] = target[valid] / risk_1[valid]
    rounded = ceil_to_lot_step(raw_lot[valid], step[valid])
    actual_lot[valid] = np.maximum(min_volume[valid], rounded)
    actual_risk[valid] = actual_lot[valid] * risk_1[valid]
    actual_risk[~np.isfinite(actual_risk)] = np.nan
    actual_lot[~np.isfinite(actual_lot)] = np.nan
    raw_lot[~np.isfinite(raw_lot)] = np.nan
    return actual_risk, actual_lot, raw_lot


def max_drawdown_abs_from_log_returns(log_returns: np.ndarray) -> tuple[float, float, float]:
    """Return max drawdown in log units, abs log DD, and abs pct DD."""
    mdd_log = max_drawdown_from_values(np.asarray(log_returns, dtype=float))
    mdd_abs_log = abs(min(float(mdd_log), 0.0))
    mdd_pct = float(1.0 - np.exp(-mdd_abs_log)) if mdd_abs_log > 0 else 0.0
    return float(mdd_log), float(mdd_abs_log), mdd_pct


def broker_actual_risk_pct_for_f(
    f: float,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    target_risk_pct = float(max(f, 0.0)) * afml
    target_risk_amount = CURRENT_BALANCE * target_risk_pct
    actual_risk_amount, _actual_lot, raw_lot = broker_executable_risk_amounts(
        target_risk_amount,
        risk_per_1_lot,
        min_lot,
        lot_step,
    )
    actual_risk_pct = actual_risk_amount / max(CURRENT_BALANCE, EPS)
    return target_risk_pct, actual_risk_pct, actual_risk_amount, raw_lot


def broker_growth_eval_for_f(
    f: float,
    stressed_r: np.ndarray,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
    active_days: int,
    edge_span_days: int,
) -> dict[str, Any]:
    target_risk_pct, actual_risk_pct, actual_risk_amount, raw_lot = broker_actual_risk_pct_for_f(
        f, afml, risk_per_1_lot, min_lot, lot_step
    )
    growth_inputs = 1.0 + actual_risk_pct * stressed_r
    feasible = bool(growth_inputs.size and np.all(np.isfinite(growth_inputs)) and np.nanmin(growth_inputs) > EPS)
    if not feasible:
        return {
            "f": float(f),
            "feasible": False,
            "score": -np.inf,
            "log_growth": -np.inf,
            "velocity_calendar": -np.inf,
            "velocity_active": -np.inf,
            "mdd_log": -np.inf,
            "mdd_abs_log": np.inf,
            "mdd_pct": np.inf,
            "bankruptcy_margin_min": float(np.nanmin(growth_inputs)) if growth_inputs.size else np.nan,
        }

    log_returns = np.log(growth_inputs)
    log_growth = float(np.sum(log_returns))
    velocity_calendar = safe_div(log_growth, edge_span_days)
    velocity_active = safe_div(log_growth, active_days)
    mdd_log, mdd_abs_log, mdd_pct = max_drawdown_abs_from_log_returns(log_returns)
    # Main 04C objective: fastest broker-executable compounding per unit drawdown pain.
    # No max-actual-risk cap is applied here; hard portfolio/risk caps belong to 08+.
    score = safe_div(velocity_calendar, max(mdd_abs_log, EPS)) if velocity_calendar > 0 else -np.inf
    return {
        "f": float(f),
        "feasible": True,
        "score": score,
        "log_growth": log_growth,
        "velocity_calendar": velocity_calendar,
        "velocity_active": velocity_active,
        "mdd_log": mdd_log,
        "mdd_abs_log": mdd_abs_log,
        "mdd_pct": mdd_pct,
        "bankruptcy_margin_min": float(np.nanmin(growth_inputs)),
        "target_risk_pct": target_risk_pct,
        "actual_risk_pct": actual_risk_pct,
        "actual_risk_amount": actual_risk_amount,
        "raw_lot": raw_lot,
    }


def broker_f_upper_from_survival(
    stressed_r: np.ndarray,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
) -> tuple[float, str]:
    """Derive an f-search upper bound from log-survival, not a chosen risk percent."""
    active = afml > EPS
    losing = (stressed_r < 0.0) & active
    if not np.any(active):
        return 0.0, "all_afml_zero"
    if not np.any(losing):
        # With no losing stressed trades, growth is formally unbounded. Use the
        # largest min-lot activation point as a finite data-derived diagnostic
        # boundary and record the reason.
        minlot_activation = (min_lot[active] * risk_per_1_lot[active]) / (CURRENT_BALANCE * np.maximum(afml[active], EPS))
        finite = minlot_activation[np.isfinite(minlot_activation) & (minlot_activation > EPS)]
        if finite.size == 0:
            return 0.0, "no_losses_no_finite_minlot_activation"
        return float(np.nanmax(finite)), "no_losing_stressed_trades_upper_is_max_minlot_activation"

    continuous_limits = (-1.0 / stressed_r[losing]) / np.maximum(afml[losing], EPS)
    continuous_limits = continuous_limits[np.isfinite(continuous_limits) & (continuous_limits > EPS)]
    if continuous_limits.size == 0:
        return 0.0, "no_positive_survival_limit"
    hi = float(np.nanmin(continuous_limits) * BROKER_OPTIMIZED_F_UPPER_SAFETY_FRACTION)

    def feasible(f: float) -> bool:
        return bool(broker_growth_eval_for_f(
            f, stressed_r, afml, risk_per_1_lot, min_lot, lot_step, active_days=1, edge_span_days=1
        )["feasible"])

    if hi <= EPS:
        return 0.0, "survival_upper_nonpositive"
    # Broker rounding can make the continuous-limit upper infeasible, so shrink
    # with binary search until all log inputs stay positive.
    lo = 0.0
    if feasible(hi):
        return hi, "loss_distribution_survival_upper"
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if feasible(mid):
            lo = mid
        else:
            hi = mid
    return float(lo), "loss_distribution_survival_upper_after_broker_rounding"


def broker_f_search_grid(
    f_upper: float,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
) -> np.ndarray:
    if not np.isfinite(f_upper) or f_upper <= EPS:
        return np.array([0.0], dtype=float)

    points: list[np.ndarray] = [np.array([0.0, f_upper], dtype=float)]
    points.append(np.linspace(0.0, f_upper, BROKER_OPTIMIZED_F_COARSE_GRID_SIZE))

    log_floor = max(f_upper * BROKER_OPTIMIZED_F_LOG_FLOOR_FRACTION, EPS)
    if log_floor < f_upper:
        points.append(np.geomspace(log_floor, f_upper, BROKER_OPTIMIZED_F_COARSE_GRID_SIZE))

    active = afml > EPS
    if np.any(active):
        minlot_activation = (min_lot[active] * risk_per_1_lot[active]) / (CURRENT_BALANCE * np.maximum(afml[active], EPS))
        finite = minlot_activation[np.isfinite(minlot_activation) & (minlot_activation > 0.0) & (minlot_activation <= f_upper)]
        if finite.size:
            qs = np.linspace(0, 100, min(101, max(11, finite.size)))
            points.append(np.nanpercentile(finite, qs))
            points.append(finite[: min(500, finite.size)])

    grid = np.unique(np.concatenate(points))
    grid = grid[np.isfinite(grid)]
    grid = grid[(grid >= 0.0) & (grid <= f_upper)]
    return np.sort(grid)


def optimize_broker_drawdown_adjusted_f(
    stressed_r: np.ndarray,
    afml: np.ndarray,
    risk_per_1_lot: np.ndarray,
    min_lot: np.ndarray,
    lot_step: np.ndarray,
    active_days: int,
    edge_span_days: int,
) -> dict[str, Any]:
    f_upper, upper_reason = broker_f_upper_from_survival(stressed_r, afml, risk_per_1_lot, min_lot, lot_step)
    grid = broker_f_search_grid(f_upper, afml, risk_per_1_lot, min_lot)
    evals = [
        broker_growth_eval_for_f(float(f), stressed_r, afml, risk_per_1_lot, min_lot, lot_step, active_days, edge_span_days)
        for f in grid
    ]
    feasible_evals = [e for e in evals if e.get("feasible") and np.isfinite(e.get("score", -np.inf))]
    if not feasible_evals:
        best = broker_growth_eval_for_f(0.0, stressed_r, afml, risk_per_1_lot, min_lot, lot_step, active_days, edge_span_days)
        best["f_search_upper"] = f_upper
        best["f_search_upper_reason"] = upper_reason
        best["f_search_points"] = int(len(grid))
        return best

    # First pass: maximize drawdown-adjusted compound velocity.
    best = max(
        feasible_evals,
        key=lambda e: (
            safe_float(e.get("score"), -np.inf),
            safe_float(e.get("velocity_calendar"), -np.inf),
            -safe_float(e.get("mdd_abs_log"), np.inf),
            safe_float(e.get("f"), -np.inf),
        ),
    )

    # Local refinement around the winning coarse-grid neighborhood.
    grid_sorted = np.sort(grid)
    idx = int(np.argmin(np.abs(grid_sorted - best["f"])))
    lo = float(grid_sorted[max(0, idx - 1)])
    hi = float(grid_sorted[min(len(grid_sorted) - 1, idx + 1)])
    grid_points = int(len(grid))
    if hi > lo:
        refined = np.linspace(lo, hi, BROKER_OPTIMIZED_F_REFINED_GRID_SIZE)
        refined_evals = [
            broker_growth_eval_for_f(float(f), stressed_r, afml, risk_per_1_lot, min_lot, lot_step, active_days, edge_span_days)
            for f in refined
        ]
        refined_feasible = [e for e in refined_evals if e.get("feasible") and np.isfinite(e.get("score", -np.inf))]
        if refined_feasible:
            best = max(
                refined_feasible + [best],
                key=lambda e: (
                    safe_float(e.get("score"), -np.inf),
                    safe_float(e.get("velocity_calendar"), -np.inf),
                    -safe_float(e.get("mdd_abs_log"), np.inf),
                    safe_float(e.get("f"), -np.inf),
                ),
            )
            grid_points += int(len(refined))

    best["f_search_upper"] = f_upper
    best["f_search_upper_reason"] = upper_reason
    best["f_search_points"] = grid_points
    return best


def formula_stats_for_group(g: pd.DataFrame, edge_backtest_span_days: int | float | None = None) -> dict[str, Any]:
    if "afml_bet_size" not in g.columns:
        raise ValueError("04C compound velocity requires trade-level afml_bet_size in the threshold-grid ledger; refusing to default AFML to 1.0.")
    missing_broker_cols = [c for c in BROKER_RISK_METADATA_REQUIRED_COLUMNS if c not in g.columns]
    if BROKER_EXECUTABLE_RISK_REQUIRED and missing_broker_cols:
        edge = str(g["edge_id"].iloc[0]) if "edge_id" in g.columns and not g.empty else "unknown_edge"
        grid_id = str(g["grid_candidate_id"].iloc[0]) if "grid_candidate_id" in g.columns and not g.empty else "unknown_grid_candidate"
        raise ValueError(
            "04C broker-executable compound velocity requires broker min-lot risk columns from patched 04B. "
            f"edge={edge}, grid_candidate_id={grid_id}, missing={missing_broker_cols}"
        )

    valid = pd.to_numeric(g["executed_r"], errors="coerce").notna()
    work = g.loc[valid].copy()
    r = pd.to_numeric(work["executed_r"], errors="coerce").to_numpy(dtype=float)
    if r.size == 0:
        return _empty_formula_stats()

    afml_series = pd.to_numeric(work["afml_bet_size"], errors="coerce")
    if afml_series.isna().any():
        bad = int(afml_series.isna().sum())
        edge = str(work["edge_id"].iloc[0]) if "edge_id" in work.columns and not work.empty else "unknown_edge"
        grid_id = str(work["grid_candidate_id"].iloc[0]) if "grid_candidate_id" in work.columns and not work.empty else "unknown_grid_candidate"
        raise ValueError(f"04C compound velocity requires non-null numeric afml_bet_size. edge={edge}, grid_candidate_id={grid_id}, bad_rows={bad}")
    afml = afml_series.clip(0.0, 1.0).to_numpy(dtype=float)
    stressed_r = r - COMPOUND_COST_STRESS_R
    afml_weighted_r = afml * stressed_r

    std_r = float(np.std(stressed_r, ddof=1)) if r.size > 1 else 0.0
    stressed_expectancy = float(np.mean(stressed_r))
    stressed_expectancy_lcb = stressed_expectancy - EXPECTANCY_LCB_Z * safe_div(std_r, np.sqrt(max(r.size, 1)))
    stressed_pf = profit_factor_from_values(stressed_r)
    pf_for_log = np.clip(stressed_pf if np.isfinite(stressed_pf) else ROBUST_PROFIT_FACTOR_CAP, EPS, ROBUST_PROFIT_FACTOR_CAP)
    stressed_cum = float(np.sum(stressed_r))
    stressed_mdd = max_drawdown_from_values(stressed_r)
    stressed_mdd_abs = abs(stressed_mdd)

    active_days = 0
    time_col = None
    if "exit_time" in work.columns:
        exit_ts = parse_time_series(work["exit_time"])
        active_days = int(exit_ts.dt.floor("D").nunique())
        time_col = exit_ts
    if active_days <= 0 and "event_time" in work.columns:
        event_ts = parse_time_series(work["event_time"])
        active_days = int(event_ts.dt.floor("D").nunique())
        time_col = event_ts
    if active_days <= 0:
        active_days = 1

    threshold_span_days = span_days_from_times(time_col) if time_col is not None else active_days
    if edge_backtest_span_days is None or not np.isfinite(float(edge_backtest_span_days)) or float(edge_backtest_span_days) <= 0:
        edge_span_days = threshold_span_days
    else:
        edge_span_days = int(max(1, float(edge_backtest_span_days)))

    risk_per_1_lot = pd.to_numeric(work["risk_per_1_lot"], errors="coerce")
    min_lot = pd.to_numeric(work["broker_min_lot"], errors="coerce")
    lot_step = pd.to_numeric(work["broker_lot_step"], errors="coerce")
    broker_valid_series = work["has_valid_broker_min_lot_risk"].apply(normalize_bool)
    valid_broker = broker_valid_series & risk_per_1_lot.gt(EPS) & min_lot.gt(EPS) & lot_step.gt(EPS)
    invalid_count = int((~valid_broker).sum())
    if invalid_count:
        status_summary: Any = ""
        if "broker_min_lot_risk_status" in work.columns:
            status_summary = (
                work.loc[~valid_broker, "broker_min_lot_risk_status"]
                .astype(str)
                .value_counts(dropna=False)
                .to_dict()
            )
        edge = str(work["edge_id"].iloc[0]) if "edge_id" in work.columns and not work.empty else "unknown_edge"
        grid_id = str(work["grid_candidate_id"].iloc[0]) if "grid_candidate_id" in work.columns and not work.empty else "unknown_grid_candidate"
        raise ValueError(
            "04C broker-executable optimized-f selection requires valid broker risk metadata for every selected trade. "
            f"edge={edge}, grid_candidate_id={grid_id}, invalid_rows={invalid_count}, statuses={status_summary}"
        )

    risk_per_1 = risk_per_1_lot.to_numpy(dtype=float)
    min_lot_arr = min_lot.to_numpy(dtype=float)
    lot_step_arr = lot_step.to_numpy(dtype=float)

    best = optimize_broker_drawdown_adjusted_f(
        stressed_r=stressed_r,
        afml=afml,
        risk_per_1_lot=risk_per_1,
        min_lot=min_lot_arr,
        lot_step=lot_step_arr,
        active_days=active_days,
        edge_span_days=edge_span_days,
    )

    f_star = safe_float(best.get("f"), 0.0)
    target_risk_pct, actual_risk_pct, actual_risk_amount, raw_lot = broker_actual_risk_pct_for_f(
        f_star, afml, risk_per_1, min_lot_arr, lot_step_arr
    )
    target_risk_amount = CURRENT_BALANCE * target_risk_pct
    positive_target = target_risk_amount > EPS
    risk_inflation = np.full_like(actual_risk_amount, np.nan, dtype=float)
    risk_inflation[positive_target] = actual_risk_amount[positive_target] / np.maximum(target_risk_amount[positive_target], EPS)
    min_lot_binding = positive_target & (raw_lot < min_lot_arr - EPS)

    log_growth = safe_float(best.get("log_growth"))
    compound_velocity_calendar = safe_float(best.get("velocity_calendar"))
    compound_velocity_active = safe_float(best.get("velocity_active"))
    compound_velocity = compound_velocity_calendar
    drawdown_adjusted_velocity = safe_float(best.get("score"))
    mdd_log = safe_float(best.get("mdd_log"))
    mdd_abs_log = safe_float(best.get("mdd_abs_log"))
    mdd_pct = safe_float(best.get("mdd_pct"))

    tlog = target_log_return()
    expected_active_days = safe_div(tlog, compound_velocity_active) if compound_velocity_active > 0 else np.inf
    expected_calendar_days = safe_div(tlog, compound_velocity_calendar) if compound_velocity_calendar > 0 else np.inf
    trades_per_active_day = safe_div(int(r.size), active_days)
    trades_per_calendar_day = safe_div(int(r.size), edge_span_days)

    broker_status_summary = ""
    if "broker_min_lot_risk_status" in work.columns:
        broker_status_summary = json.dumps(
            work["broker_min_lot_risk_status"].astype(str).value_counts(dropna=False).to_dict(),
            sort_keys=True,
        )

    dd_budget = usable_drawdown_budget_pct()

    return {
        "formula_trade_count": int(r.size),
        "active_exit_days": int(active_days),
        "calendar_span_days": int(threshold_span_days),
        "edge_backtest_span_days": int(edge_span_days),
        "trades_per_active_day": trades_per_active_day,
        "trades_per_calendar_day": trades_per_calendar_day,
        "stressed_expectancy_r": stressed_expectancy,
        "stressed_expectancy_lcb_r": stressed_expectancy_lcb,
        "stressed_profit_factor": stressed_pf,
        "stressed_log_profit_factor": float(np.log(pf_for_log)),
        "stressed_cumulative_r": stressed_cum,
        "stressed_max_drawdown_r": stressed_mdd,
        "stressed_max_drawdown_abs_r": stressed_mdd_abs,
        "afml_weighted_stressed_expectancy_r": float(np.mean(afml_weighted_r)),
        "afml_weighted_stressed_profit_factor": profit_factor_from_values(afml_weighted_r),
        "afml_weighted_stressed_cumulative_r": float(np.sum(afml_weighted_r)),
        "afml_weighted_stressed_max_drawdown_r": max_drawdown_from_values(afml_weighted_r),
        "compound_usable_drawdown_budget_pct": dd_budget,
        # Compatibility column: this is now the optimized requested full-AFML f,
        # not a fixed proxy cap.
        "compound_allowed_risk_per_trade": f_star,
        "compound_log_growth": log_growth,
        "compound_velocity": compound_velocity,
        "compound_velocity_calendar": compound_velocity_calendar,
        "compound_velocity_active": compound_velocity_active,
        "compound_equity_return_proxy": float(np.exp(log_growth) - 1.0) if np.isfinite(log_growth) else np.nan,
        "compound_power": drawdown_adjusted_velocity,
        "expected_active_days_to_target": expected_active_days,
        "expected_calendar_days_to_target": expected_calendar_days,
        "arithmetic_compound_speed_proxy": stressed_expectancy * trades_per_active_day * float(np.nanmean(actual_risk_pct[positive_target])) if np.any(positive_target) else np.nan,
        "arithmetic_compound_speed_calendar": stressed_expectancy * trades_per_calendar_day * float(np.nanmean(actual_risk_pct[positive_target])) if np.any(positive_target) else np.nan,

        # Legacy theoretical fields are intentionally not computed from a fixed arbitrary risk proxy.
        "compound_log_growth_theoretical": np.nan,
        "compound_velocity_theoretical": np.nan,
        "compound_velocity_calendar_theoretical": np.nan,
        "compound_velocity_active_theoretical": np.nan,
        "compound_equity_return_proxy_theoretical": np.nan,
        "expected_active_days_to_target_theoretical": np.nan,
        "expected_calendar_days_to_target_theoretical": np.nan,

        "broker_executable_risk_required": True,
        "broker_optimized_f": f_star,
        "broker_optimized_f_search_upper": safe_float(best.get("f_search_upper")),
        "broker_optimized_f_search_points": int(best.get("f_search_points", 0)),
        "broker_optimized_f_search_upper_reason": str(best.get("f_search_upper_reason", "")),
        "broker_optimized_objective": "drawdown_adjusted_compound_velocity",
        "broker_optimized_log_growth": log_growth,
        "broker_optimized_compound_velocity": compound_velocity,
        "broker_optimized_compound_velocity_calendar": compound_velocity_calendar,
        "broker_optimized_compound_velocity_active": compound_velocity_active,
        "broker_optimized_drawdown_adjusted_compound_velocity": drawdown_adjusted_velocity,
        "broker_optimized_drawdown_adjusted_log_growth": safe_div(log_growth, max(mdd_abs_log, EPS)) if np.isfinite(log_growth) else np.nan,
        "broker_optimized_max_drawdown_log": mdd_log,
        "broker_optimized_max_drawdown_abs_log": mdd_abs_log,
        "broker_optimized_max_drawdown_pct": mdd_pct,
        "broker_optimized_equity_return_proxy": float(np.exp(log_growth) - 1.0) if np.isfinite(log_growth) else np.nan,
        "broker_optimized_expected_active_days_to_target": expected_active_days,
        "broker_optimized_expected_calendar_days_to_target": expected_calendar_days,
        "broker_optimized_bankruptcy_margin_min": safe_float(best.get("bankruptcy_margin_min")),
        "broker_optimized_survival_feasible": bool(best.get("feasible", False)),

        # Backward-friendly aliases for the previously patched broker-executable columns.
        "compound_log_growth_broker_executable": log_growth,
        "compound_velocity_broker_executable": compound_velocity,
        "compound_velocity_calendar_broker_executable": compound_velocity_calendar,
        "compound_velocity_active_broker_executable": compound_velocity_active,
        "compound_equity_return_proxy_broker_executable": float(np.exp(log_growth) - 1.0) if np.isfinite(log_growth) else np.nan,
        "expected_active_days_to_target_broker_executable": expected_active_days,
        "expected_calendar_days_to_target_broker_executable": expected_calendar_days,

        "broker_valid_risk_row_count": int(valid_broker.sum()),
        "broker_invalid_risk_row_count": invalid_count,
        "broker_valid_risk_row_rate": pct_or_nan(int(valid_broker.sum()), int(len(work))),
        "broker_min_lot_binding_trade_count": int(min_lot_binding.sum()),
        "broker_min_lot_binding_trade_rate": pct_or_nan(int(min_lot_binding.sum()), int(positive_target.sum())),
        "broker_median_target_risk_amount": percentile_or_nan(target_risk_amount[positive_target], 50),
        "broker_median_actual_risk_amount": percentile_or_nan(actual_risk_amount[positive_target], 50),
        "broker_p90_actual_risk_amount": percentile_or_nan(actual_risk_amount[positive_target], 90),
        "broker_max_actual_risk_amount": percentile_or_nan(actual_risk_amount[positive_target], 100),
        "broker_median_risk_inflation": percentile_or_nan(risk_inflation, 50),
        "broker_p90_risk_inflation": percentile_or_nan(risk_inflation, 90),
        "broker_max_risk_inflation": percentile_or_nan(risk_inflation, 100),
        "broker_mean_actual_risk_pct": float(np.nanmean(actual_risk_pct[positive_target])) if np.any(positive_target) else np.nan,
        "broker_median_actual_risk_pct": percentile_or_nan(actual_risk_pct[positive_target], 50),
        "broker_p90_actual_risk_pct": percentile_or_nan(actual_risk_pct[positive_target], 90),
        "broker_max_actual_risk_pct": percentile_or_nan(actual_risk_pct[positive_target], 100),
        "broker_mean_target_risk_pct": float(np.nanmean(target_risk_pct[positive_target])) if np.any(positive_target) else np.nan,
        "broker_median_target_risk_pct": percentile_or_nan(target_risk_pct[positive_target], 50),
        "broker_p90_target_risk_pct": percentile_or_nan(target_risk_pct[positive_target], 90),
        "broker_max_target_risk_pct": percentile_or_nan(target_risk_pct[positive_target], 100),
        "broker_risk_metadata_status_summary": broker_status_summary,
    }


def build_trade_sets_and_formula_stats_from_ledger(path: Path) -> tuple[dict[str, set[str]], pd.DataFrame]:
    if not path.exists():
        raise FileNotFoundError(f"04C requires threshold-grid ledger for trade-set plateau compression: {path}")
    header = pd.read_csv(path, nrows=0)
    columns = [
        "grid_candidate_id", "edge_id", "asset", "source_oof_row_number",
        "event_time", "exit_time", "executed_r", "afml_bet_size",
        *BROKER_RISK_METADATA_REQUIRED_COLUMNS,
        *BROKER_RISK_DIAGNOSTIC_COLUMNS,
    ]
    available = [c for c in columns if c in header.columns]
    required = ["grid_candidate_id", "edge_id", "executed_r", "afml_bet_size"]
    if BROKER_EXECUTABLE_RISK_REQUIRED:
        required += BROKER_RISK_METADATA_REQUIRED_COLUMNS
    missing = [c for c in required if c not in available]
    if missing:
        raise ValueError(
            f"{path} missing required compound-velocity trade-level columns: {missing}. "
            "Run the broker-min-lot-aware 04B first; 04C refuses to default AFML or broker risk."
        )
    df = pd.read_csv(path, usecols=available, float_precision="round_trip")
    if df.empty:
        raise ValueError(f"Threshold-grid trade ledger is empty: {path}")

    if "exit_time" in df.columns:
        span_ts = parse_time_series(df["exit_time"])
    elif "event_time" in df.columns:
        span_ts = parse_time_series(df["event_time"])
    else:
        span_ts = pd.Series(pd.NaT, index=df.index)
    df["_span_time"] = span_ts
    edge_span_days = (
        df.dropna(subset=["_span_time"])
        .groupby("edge_id")["_span_time"]
        .apply(span_days_from_times)
        .to_dict()
    )

    if "source_oof_row_number" in df.columns:
        df["trade_set_key"] = df["edge_id"].astype(str) + "|" + df["source_oof_row_number"].astype(str)
    else:
        key = df["edge_id"].astype(str)
        for col in ["event_time", "exit_time", "executed_r"]:
            if col in df.columns:
                key = key + "|" + df[col].astype(str)
        df["trade_set_key"] = key
    trade_sets = df.groupby("grid_candidate_id")["trade_set_key"].apply(lambda s: set(s.astype(str))).to_dict()

    stats_rows = []
    for grid_candidate_id, g in tqdm(df.groupby("grid_candidate_id", sort=False), desc="Computing broker-executable compound-velocity stats", unit="threshold"):
        edge_id = str(g["edge_id"].iloc[0])
        row = {"grid_candidate_id": grid_candidate_id}
        row.update(formula_stats_for_group(g.copy(), edge_span_days.get(edge_id)))
        stats_rows.append(row)
    return trade_sets, pd.DataFrame(stats_rows)


def cpcv_path_growth_score(g: pd.DataFrame, f_star: float, edge_span_days: int) -> dict[str, float]:
    """One (threshold, path): the main-path growth formula at f*, signed (a losing path scores below zero)."""
    r = pd.to_numeric(g["executed_r"], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(r)
    work = g.loc[ok]
    r = r[ok]
    if r.size == 0:
        return {"score": 0.0, "log_growth": 0.0}
    valid = work["has_valid_broker_min_lot_risk"].apply(normalize_bool)
    if not bool(valid.all()):
        raise ValueError(
            "04C CPCV path scoring requires valid broker risk metadata on every path trade: "
            f"grid_candidate_id={work['grid_candidate_id'].iloc[0]}, path={work['cpcv_path'].iloc[0]}, invalid_rows={int((~valid).sum())}"
        )
    afml = pd.to_numeric(work["afml_bet_size"], errors="coerce").clip(0.0, 1.0).to_numpy(dtype=float)
    if not np.all(np.isfinite(afml)):
        raise ValueError(f"04C CPCV path scoring: non-numeric afml_bet_size, grid_candidate_id={work['grid_candidate_id'].iloc[0]}")
    active_days = max(1, int(parse_time_series(work["exit_time"]).dt.floor("D").nunique()))
    ev = broker_growth_eval_for_f(
        float(f_star) if np.isfinite(f_star) else 0.0,
        r - COMPOUND_COST_STRESS_R,
        afml,
        pd.to_numeric(work["risk_per_1_lot"], errors="coerce").to_numpy(dtype=float),
        pd.to_numeric(work["broker_min_lot"], errors="coerce").to_numpy(dtype=float),
        pd.to_numeric(work["broker_lot_step"], errors="coerce").to_numpy(dtype=float),
        active_days,
        int(max(1, edge_span_days)),
    )
    if not ev.get("feasible"):
        return {"score": -np.inf, "log_growth": -np.inf}
    score = safe_div(ev["velocity_calendar"], max(ev["mdd_abs_log"], EPS))
    return {"score": float(score), "log_growth": float(ev["log_growth"])}


def summarize_cpcv_paths(scores: list[float], growths: list[float]) -> dict[str, Any]:
    s = np.asarray(scores, dtype=float)
    lg = np.asarray(growths, dtype=float)
    return {
        "cpcv_path_count": int(s.size),
        "cpcv_path_mean_score": float(np.mean(s)) if s.size else np.nan,
        "cpcv_path_median_score": float(np.median(s)) if s.size else np.nan,
        "cpcv_path_worst_score": float(np.min(s)) if s.size else np.nan,
        "cpcv_path_std_score": float(np.std(s, ddof=1)) if s.size > 1 and np.all(np.isfinite(s)) else np.nan,
        "cpcv_path_losing_share": float(np.mean(lg <= 0.0)) if lg.size else np.nan,
        "cpcv_path_mean_log_growth": float(np.mean(lg)) if lg.size else np.nan,
    }


def cpcv_path_stats_from_ledger(path: Path, formula_stats: pd.DataFrame, paths_by_edge: dict[str, int]) -> pd.DataFrame:
    """Stream the 04B path ledger (rows are contiguous per grid_candidate_id) and score every threshold on every path.
    A path on which a threshold takes no trade scores 0 (no growth, no drawdown)."""
    if not path.exists():
        raise FileNotFoundError(
            f"04C requires the CPCV path ledger {path} (D052). Re-run 04B on D052 Step 03/04 artifacts, or pass "
            "--allow-legacy-single-path."
        )
    f_by = formula_stats.set_index("grid_candidate_id")["broker_optimized_f"].to_dict()
    span_by = formula_stats.set_index("grid_candidate_id")["edge_backtest_span_days"].to_dict()
    usecols = ["grid_candidate_id", "edge_id", "cpcv_path", "exit_time", "executed_r", "afml_bet_size",
               *BROKER_RISK_METADATA_REQUIRED_COLUMNS]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def score_block(block: pd.DataFrame) -> None:
        for gid, g in block.groupby("grid_candidate_id", sort=False):
            gid = str(gid)
            if gid in seen:
                raise RuntimeError(f"04C CPCV path ledger is not contiguous per grid_candidate_id: {gid}")
            seen.add(gid)
            n_paths = int(paths_by_edge.get(str(g["edge_id"].iloc[0]), 0))
            if n_paths <= 0:
                raise ValueError(f"04C: 04B manifest has no CPCV path count for edge {g['edge_id'].iloc[0]}")
            f_star = safe_float(f_by.get(gid), 0.0)
            span = int(safe_float(span_by.get(gid), 1.0)) if np.isfinite(safe_float(span_by.get(gid))) else 1
            by_path = {int(k): v for k, v in g.groupby("cpcv_path", sort=True)}
            scores, growths = [], []
            for path_no in range(n_paths):
                res = cpcv_path_growth_score(by_path[path_no], f_star, span) if path_no in by_path else {"score": 0.0, "log_growth": 0.0}
                scores.append(res["score"])
                growths.append(res["log_growth"])
            rows.append({"grid_candidate_id": gid, **summarize_cpcv_paths(scores, growths)})

    carry: pd.DataFrame | None = None
    for chunk in tqdm(pd.read_csv(path, usecols=usecols, float_precision="round_trip", chunksize=CPCV_PATH_READ_CHUNK_ROWS),
                      desc="Scoring CPCV paths", unit="chunk"):
        if carry is not None and not carry.empty:
            chunk = pd.concat([carry, chunk], ignore_index=True)
        last = chunk["grid_candidate_id"].iloc[-1]
        tail = chunk["grid_candidate_id"] == last
        score_block(chunk.loc[~tail])
        carry = chunk.loc[tail]
    if carry is not None and not carry.empty:
        score_block(carry)
    out = pd.DataFrame(rows)
    # thresholds with no trade on any path never reach the path ledger
    missing = sorted(set(formula_stats["grid_candidate_id"].astype(str)) - seen)
    if missing:
        out = pd.concat([out, pd.DataFrame([{"grid_candidate_id": m, **summarize_cpcv_paths([], [])} for m in missing])],
                        ignore_index=True)
    return out


def apply_cpcv_path_selection_score(out: pd.DataFrame) -> pd.DataFrame:
    """D052: the selection score is the mean across CPCV paths; gates stay on the main path."""
    out = out.copy()
    out["compound_velocity_score_single_path"] = out["compound_velocity_score"]
    if "cpcv_path_mean_score" not in out.columns:
        out["compound_velocity_score_basis"] = out["compound_velocity_score_basis"] + " (legacy single path)"
        return out
    mean = pd.to_numeric(out["cpcv_path_mean_score"], errors="coerce").fillna(-np.inf)
    out["compound_velocity_score"] = mean.where(out["compound_velocity_gate_pass"], -np.inf)
    out["compound_velocity_score_basis"] = CPCV_SELECTION_SCORE_BASIS
    return out


def add_compound_velocity_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    local_pos = pd.to_numeric(out.get("local_positive_ratio", 0.0), errors="coerce").fillna(0.0)
    trade_count = pd.to_numeric(out.get("formula_trade_count", out["trade_count"]), errors="coerce").fillna(0.0)
    active_days = pd.to_numeric(out.get("active_exit_days", 0), errors="coerce").fillna(0.0)
    trades_per_day = pd.to_numeric(out.get("trades_per_active_day", 0), errors="coerce").fillna(0.0)
    exp_lcb = pd.to_numeric(out.get("stressed_expectancy_lcb_r", 0), errors="coerce").fillna(-np.inf)
    pf = pd.to_numeric(out.get("stressed_profit_factor", 0), errors="coerce").replace([np.inf, -np.inf], ROBUST_PROFIT_FACTOR_CAP).fillna(0.0)

    raw_velocity = pd.to_numeric(out.get("broker_optimized_compound_velocity_calendar", out.get("compound_velocity", 0)), errors="coerce").fillna(-np.inf)
    dd_adjusted_velocity = pd.to_numeric(out.get("broker_optimized_drawdown_adjusted_compound_velocity", np.nan), errors="coerce").fillna(-np.inf)
    survival_ok = out.get("broker_optimized_survival_feasible", True)
    if isinstance(survival_ok, pd.Series):
        survival_ok = survival_ok.apply(normalize_bool)
    else:
        survival_ok = pd.Series(True, index=out.index)

    primary = (
        (trade_count >= PRIMARY_MIN_TRADE_COUNT) &
        (active_days >= PRIMARY_MIN_ACTIVE_DAYS) &
        (trades_per_day >= PRIMARY_MIN_TRADES_PER_ACTIVE_DAY) &
        (exp_lcb > 0) &
        (pf >= PRIMARY_MIN_STRESSED_PROFIT_FACTOR) &
        (local_pos >= PRIMARY_MIN_LOCAL_POSITIVE_RATIO) &
        (raw_velocity > 0) &
        (dd_adjusted_velocity > 0) &
        survival_ok
    )
    fallback = (
        ~primary &
        (trade_count >= FALLBACK_MIN_TRADE_COUNT) &
        (active_days >= FALLBACK_MIN_ACTIVE_DAYS) &
        (trades_per_day >= FALLBACK_MIN_TRADES_PER_ACTIVE_DAY) &
        (exp_lcb > 0) &
        (pf >= FALLBACK_MIN_STRESSED_PROFIT_FACTOR) &
        (local_pos >= FALLBACK_MIN_LOCAL_POSITIVE_RATIO) &
        (raw_velocity > 0) &
        (dd_adjusted_velocity > 0) &
        survival_ok
    )
    out["compound_velocity_gate_level"] = np.select([primary, fallback], ["primary", "fallback"], default="failed")
    out["compound_velocity_gate_pass"] = out["compound_velocity_gate_level"].isin(["primary", "fallback"])
    # Main selection score is now drawdown-adjusted broker-executable compound velocity,
    # not raw velocity and not a fixed-risk proxy.
    out["compound_velocity_score"] = dd_adjusted_velocity.where(out["compound_velocity_gate_pass"], -np.inf)
    out["compound_velocity_score_basis"] = "broker_optimized_drawdown_adjusted_compound_velocity"
    out["compound_velocity_gate_reason"] = ""
    out.loc[trade_count < FALLBACK_MIN_TRADE_COUNT, "compound_velocity_gate_reason"] += "trade_count_below_500;"
    out.loc[(trade_count >= FALLBACK_MIN_TRADE_COUNT) & (trade_count < PRIMARY_MIN_TRADE_COUNT), "compound_velocity_gate_reason"] += "fallback_trade_count;"
    out.loc[active_days < FALLBACK_MIN_ACTIVE_DAYS, "compound_velocity_gate_reason"] += "active_days_below_150;"
    out.loc[exp_lcb <= 0, "compound_velocity_gate_reason"] += "stressed_expectancy_lcb_nonpositive;"
    out.loc[pf < FALLBACK_MIN_STRESSED_PROFIT_FACTOR, "compound_velocity_gate_reason"] += "stressed_pf_below_1;"
    out.loc[local_pos < FALLBACK_MIN_LOCAL_POSITIVE_RATIO, "compound_velocity_gate_reason"] += "local_positive_ratio_below_0p50;"
    out.loc[raw_velocity <= 0, "compound_velocity_gate_reason"] += "broker_optimized_calendar_velocity_nonpositive;"
    out.loc[dd_adjusted_velocity <= 0, "compound_velocity_gate_reason"] += "broker_optimized_drawdown_adjusted_velocity_nonpositive;"
    out.loc[~survival_ok, "compound_velocity_gate_reason"] += "broker_optimized_survival_infeasible;"
    out.loc[out["compound_velocity_gate_reason"] == "", "compound_velocity_gate_reason"] = out["compound_velocity_gate_level"]
    return out


def rows_same_plateau(prev: pd.Series, curr: pd.Series) -> tuple[bool, dict[str, Any]]:
    prev_set = prev.get("_trade_set", set())
    curr_set = curr.get("_trade_set", set())
    has_sets = isinstance(prev_set, set) and isinstance(curr_set, set) and len(prev_set) > 0 and len(curr_set) > 0
    jac = jaccard(prev_set, curr_set) if has_sets else np.nan
    trade_rel = rel_diff(prev.get("trade_count"), curr.get("trade_count"))
    cum_rel = rel_diff(prev.get("cumulative_r"), curr.get("cumulative_r"))
    exp_abs = abs(safe_float(prev.get("expectancy_r")) - safe_float(curr.get("expectancy_r")))
    calmar_rel = rel_diff(prev.get("calmar_r"), curr.get("calmar_r"))
    if has_sets:
        same = jac >= PLATEAU_JACCARD_MIN and trade_rel <= PLATEAU_TRADE_COUNT_REL_DIFF_MAX and cum_rel <= PLATEAU_CUMULATIVE_R_REL_DIFF_MAX and exp_abs <= PLATEAU_EXPECTANCY_ABS_DIFF_MAX and calmar_rel <= PLATEAU_CALMAR_REL_DIFF_MAX
    else:
        same = trade_rel <= PLATEAU_TRADE_COUNT_REL_DIFF_MAX and cum_rel <= PLATEAU_CUMULATIVE_R_REL_DIFF_MAX and exp_abs <= PLATEAU_EXPECTANCY_ABS_DIFF_MAX and calmar_rel <= PLATEAU_CALMAR_REL_DIFF_MAX
    return same, {
        "adjacent_jaccard_with_previous": jac,
        "adjacent_trade_count_rel_diff": trade_rel,
        "adjacent_cumulative_r_rel_diff": cum_rel,
        "adjacent_expectancy_abs_diff": exp_abs,
        "adjacent_calmar_rel_diff": calmar_rel,
        "new_plateau_reason": "" if same else f"jaccard={jac:.4f};trade_rel={trade_rel:.4f};cum_rel={cum_rel:.4f};exp_abs={exp_abs:.4f};calmar_rel={calmar_rel:.4f}",
    }


def assign_plateaus(grid: pd.DataFrame, trade_sets: dict[str, set[str]]) -> pd.DataFrame:
    frames = []
    for edge_id, g in tqdm(grid.groupby("edge_id", sort=True), desc="Compressing threshold plateaus", unit="edge"):
        work = g.sort_values("threshold").copy().reset_index(drop=True)
        work["_trade_set"] = work["grid_candidate_id"].map(lambda x: trade_sets.get(x, set()))
        work["trade_set_size"] = work["_trade_set"].apply(len)
        work["trade_set_hash"] = work["_trade_set"].apply(trade_set_hash)
        plateau_index = 0
        plateau_indices = []
        diagnostics = []
        for i, row in work.iterrows():
            if i == 0:
                plateau_indices.append(plateau_index)
                diagnostics.append({
                    "adjacent_jaccard_with_previous": np.nan,
                    "adjacent_trade_count_rel_diff": np.nan,
                    "adjacent_cumulative_r_rel_diff": np.nan,
                    "adjacent_expectancy_abs_diff": np.nan,
                    "adjacent_calmar_rel_diff": np.nan,
                    "new_plateau_reason": "first_threshold_for_edge",
                })
                continue
            same, diag = rows_same_plateau(work.iloc[i - 1], row)
            if not same:
                plateau_index += 1
            plateau_indices.append(plateau_index)
            diagnostics.append(diag)
        work["plateau_index"] = plateau_indices
        work["plateau_id"] = clean_edge_for_id(edge_id) + "_plateau_" + work["plateau_index"].astype(str).str.zfill(2)
        work = pd.concat([work, pd.DataFrame(diagnostics)], axis=1)
        stats = work.groupby("plateau_id").agg(
            plateau_min_threshold=("threshold", "min"),
            plateau_max_threshold=("threshold", "max"),
            plateau_row_count=("threshold", "count"),
            plateau_trade_count_min=("trade_count", "min"),
            plateau_trade_count_max=("trade_count", "max"),
            plateau_cumulative_r_min=("cumulative_r", "min"),
            plateau_cumulative_r_max=("cumulative_r", "max"),
            plateau_expectancy_min=("expectancy_r", "min"),
            plateau_expectancy_max=("expectancy_r", "max"),
            plateau_afml_bet_size_min=("avg_afml_bet_size", "min"),
            plateau_afml_bet_size_max=("avg_afml_bet_size", "max"),
        ).reset_index()
        work = work.merge(stats, on="plateau_id", how="left")
        frames.append(work.drop(columns=["_trade_set"], errors="ignore"))
    return pd.concat(frames, ignore_index=True).sort_values(["asset", "family", "edge_id", "threshold"]).reset_index(drop=True)


def sort_compound_velocity_candidates(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(
        [
            "compound_velocity_score",
            "broker_optimized_compound_velocity_calendar",
            "broker_optimized_expected_calendar_days_to_target",
            "broker_optimized_max_drawdown_abs_log",
            "broker_optimized_f",
            "stressed_profit_factor",
            "formula_trade_count",
            "local_positive_ratio",
            "threshold",
        ],
        ascending=[False, False, True, True, False, False, False, False, False],
        na_position="last",
    )


def edge_rejection_report(g: pd.DataFrame, reason: str) -> dict[str, Any]:
    edge_id = g["edge_id"].iloc[0]
    top = sort_compound_velocity_candidates(g.copy()).head(5)
    top_cols = [
        "threshold", "formula_trade_count", "active_exit_days", "edge_backtest_span_days",
        "trades_per_active_day", "trades_per_calendar_day", "stressed_expectancy_lcb_r",
        "stressed_profit_factor", "local_positive_ratio", "afml_weighted_stressed_expectancy_r",
        "afml_weighted_stressed_profit_factor", "compound_allowed_risk_per_trade",
        "compound_log_growth", "compound_velocity", "compound_velocity_active",
        "broker_optimized_f", "broker_optimized_f_search_upper", "broker_optimized_f_search_upper_reason",
        "broker_optimized_compound_velocity_calendar", "broker_optimized_drawdown_adjusted_compound_velocity",
        "broker_optimized_max_drawdown_abs_log", "broker_optimized_max_drawdown_pct",
        "broker_optimized_expected_calendar_days_to_target", "broker_optimized_survival_feasible",
        "broker_min_lot_binding_trade_rate", "broker_median_risk_inflation",
        "broker_p90_risk_inflation", "broker_max_actual_risk_pct",
        "arithmetic_compound_speed_calendar", "compound_velocity_gate_reason",
    ]
    top_cols = [c for c in top_cols if c in top.columns]
    best = top.iloc[0].to_dict() if not top.empty else {}
    return {
        "edge_id": edge_id,
        "asset": g["asset"].iloc[0],
        "family": g["family"].iloc[0],
        "side_name": g["side_name"].iloc[0],
        "edge_classification": g["edge_classification"].iloc[0],
        "selection_status": "rejected_no_drawdown_adjusted_broker_compound_velocity_threshold",
        "rejection_reason": reason,
        "threshold_rows": int(len(g)),
        "eligible_primary_rows": int((g["compound_velocity_gate_level"] == "primary").sum()),
        "eligible_fallback_rows": int((g["compound_velocity_gate_level"] == "fallback").sum()),
        "plateau_count": int(g["plateau_id"].nunique()),
        "representative_count": 0,
        "selected_representatives": "",
        "selected_thresholds": "",
        "selected_gate_level": "rejected",
        "selected_compound_velocity": safe_float(best.get("compound_velocity")),
        "selected_drawdown_adjusted_compound_velocity": safe_float(best.get("broker_optimized_drawdown_adjusted_compound_velocity")),
        "selected_expected_calendar_days_to_target": safe_float(best.get("expected_calendar_days_to_target")),
        "selected_expected_active_days_to_target": safe_float(best.get("expected_active_days_to_target")),
        "best_threshold": safe_float(best.get("threshold")),
        "best_formula_trade_count": safe_float(best.get("formula_trade_count")),
        "best_active_exit_days": safe_float(best.get("active_exit_days")),
        "best_edge_backtest_span_days": safe_float(best.get("edge_backtest_span_days")),
        "best_trades_per_active_day": safe_float(best.get("trades_per_active_day")),
        "best_trades_per_calendar_day": safe_float(best.get("trades_per_calendar_day")),
        "best_stressed_expectancy_lcb_r": safe_float(best.get("stressed_expectancy_lcb_r")),
        "best_stressed_profit_factor": safe_float(best.get("stressed_profit_factor")),
        "best_afml_weighted_stressed_expectancy_r": safe_float(best.get("afml_weighted_stressed_expectancy_r")),
        "best_afml_weighted_stressed_profit_factor": safe_float(best.get("afml_weighted_stressed_profit_factor")),
        "best_broker_optimized_f": safe_float(best.get("broker_optimized_f")),
        "best_broker_optimized_compound_velocity": safe_float(best.get("broker_optimized_compound_velocity_calendar")),
        "best_broker_optimized_drawdown_adjusted_compound_velocity": safe_float(best.get("broker_optimized_drawdown_adjusted_compound_velocity")),
        "best_broker_optimized_max_drawdown_abs_log": safe_float(best.get("broker_optimized_max_drawdown_abs_log")),
        "best_broker_optimized_max_drawdown_pct": safe_float(best.get("broker_optimized_max_drawdown_pct")),
        "best_broker_optimized_expected_calendar_days_to_target": safe_float(best.get("broker_optimized_expected_calendar_days_to_target")),
        "best_broker_min_lot_binding_trade_rate": safe_float(best.get("broker_min_lot_binding_trade_rate")),
        "best_broker_median_risk_inflation": safe_float(best.get("broker_median_risk_inflation")),
        "best_broker_p90_risk_inflation": safe_float(best.get("broker_p90_risk_inflation")),
        "best_broker_max_actual_risk_pct": safe_float(best.get("broker_max_actual_risk_pct")),
        "best_arithmetic_compound_speed_calendar": safe_float(best.get("arithmetic_compound_speed_calendar")),
        "best_gate_reason": str(best.get("compound_velocity_gate_reason", "")),
        "top_threshold_diagnostics_json": top[top_cols].to_json(orient="records"),
        "role_to_grid_candidate": json.dumps({}),
        "role_to_plateau": json.dumps({}),
    }


def select_representatives_for_edge(g: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    primary = g[g["compound_velocity_gate_level"] == "primary"].copy()
    fallback = g[g["compound_velocity_gate_level"] == "fallback"].copy()
    if not primary.empty:
        pool = primary
        selection_gate = "primary"
    elif not fallback.empty:
        pool = fallback
        selection_gate = "fallback"
    else:
        empty = pd.DataFrame(columns=list(g.columns) + ["representative_id"])
        return empty, edge_rejection_report(g, "no threshold produced positive gated drawdown-adjusted broker compound velocity")

    winner = sort_compound_velocity_candidates(pool).iloc[0].copy()
    single_path_winner = pool.sort_values("compound_velocity_score_single_path", ascending=False, kind="mergesort").iloc[0] \
        if "compound_velocity_score_single_path" in pool.columns else winner
    winner["representative_roles"] = "compound_velocity"
    winner["primary_representative_role"] = "compound_velocity"
    winner["role_count"] = 1
    winner["role_selected_plateau_id"] = winner["plateau_id"]
    winner["selection_gate_level"] = selection_gate
    rep_df = pd.DataFrame([winner])
    rep_df["representative_id"] = rep_df.apply(lambda r: f"{clean_edge_for_id(r['edge_id'])}_rep_thr{clean_threshold_for_id(r['threshold'])}", axis=1)

    report = {
        "edge_id": g["edge_id"].iloc[0],
        "asset": g["asset"].iloc[0],
        "family": g["family"].iloc[0],
        "side_name": g["side_name"].iloc[0],
        "edge_classification": g["edge_classification"].iloc[0],
        "selection_status": "selected",
        "rejection_reason": "",
        "threshold_rows": int(len(g)),
        "eligible_primary_rows": int(len(primary)),
        "eligible_fallback_rows": int(len(fallback)),
        "plateau_count": int(g["plateau_id"].nunique()),
        "representative_count": int(len(rep_df)),
        "selected_representatives": "|".join(rep_df["representative_id"].astype(str)),
        "selected_thresholds": "|".join([f"{x:.2f}" for x in rep_df["threshold"].astype(float)]),
        "selected_gate_level": selection_gate,
        "selected_compound_velocity": safe_float(rep_df["compound_velocity"].iloc[0]),
        "selected_drawdown_adjusted_compound_velocity": safe_float(rep_df.get("broker_optimized_drawdown_adjusted_compound_velocity", pd.Series([np.nan])).iloc[0]),
        "selected_broker_optimized_f": safe_float(rep_df.get("broker_optimized_f", pd.Series([np.nan])).iloc[0]),
        "selected_broker_optimized_compound_velocity": safe_float(rep_df.get("broker_optimized_compound_velocity_calendar", pd.Series([np.nan])).iloc[0]),
        "selected_broker_optimized_max_drawdown_abs_log": safe_float(rep_df.get("broker_optimized_max_drawdown_abs_log", pd.Series([np.nan])).iloc[0]),
        "selected_broker_optimized_max_drawdown_pct": safe_float(rep_df.get("broker_optimized_max_drawdown_pct", pd.Series([np.nan])).iloc[0]),
        "selected_compound_velocity_active": safe_float(rep_df.get("compound_velocity_active", pd.Series([np.nan])).iloc[0]),
        "selected_broker_min_lot_binding_trade_rate": safe_float(rep_df.get("broker_min_lot_binding_trade_rate", pd.Series([np.nan])).iloc[0]),
        "selected_broker_median_risk_inflation": safe_float(rep_df.get("broker_median_risk_inflation", pd.Series([np.nan])).iloc[0]),
        "selected_broker_p90_risk_inflation": safe_float(rep_df.get("broker_p90_risk_inflation", pd.Series([np.nan])).iloc[0]),
        "selected_broker_max_actual_risk_pct": safe_float(rep_df.get("broker_max_actual_risk_pct", pd.Series([np.nan])).iloc[0]),
        "selected_expected_calendar_days_to_target": safe_float(rep_df["expected_calendar_days_to_target"].iloc[0]),
        "selected_expected_active_days_to_target": safe_float(rep_df["expected_active_days_to_target"].iloc[0]),
        "best_threshold": safe_float(rep_df["threshold"].iloc[0]),
        "best_formula_trade_count": safe_float(rep_df["formula_trade_count"].iloc[0]),
        "best_active_exit_days": safe_float(rep_df["active_exit_days"].iloc[0]),
        "best_edge_backtest_span_days": safe_float(rep_df.get("edge_backtest_span_days", pd.Series([np.nan])).iloc[0]),
        "best_trades_per_active_day": safe_float(rep_df["trades_per_active_day"].iloc[0]),
        "best_trades_per_calendar_day": safe_float(rep_df.get("trades_per_calendar_day", pd.Series([np.nan])).iloc[0]),
        "best_stressed_expectancy_lcb_r": safe_float(rep_df["stressed_expectancy_lcb_r"].iloc[0]),
        "best_stressed_profit_factor": safe_float(rep_df["stressed_profit_factor"].iloc[0]),
        "best_afml_weighted_stressed_expectancy_r": safe_float(rep_df.get("afml_weighted_stressed_expectancy_r", pd.Series([np.nan])).iloc[0]),
        "best_afml_weighted_stressed_profit_factor": safe_float(rep_df.get("afml_weighted_stressed_profit_factor", pd.Series([np.nan])).iloc[0]),
        "best_broker_optimized_f": safe_float(rep_df.get("broker_optimized_f", pd.Series([np.nan])).iloc[0]),
        "best_broker_optimized_compound_velocity": safe_float(rep_df.get("broker_optimized_compound_velocity_calendar", pd.Series([np.nan])).iloc[0]),
        "best_broker_optimized_drawdown_adjusted_compound_velocity": safe_float(rep_df.get("broker_optimized_drawdown_adjusted_compound_velocity", pd.Series([np.nan])).iloc[0]),
        "best_broker_optimized_max_drawdown_abs_log": safe_float(rep_df.get("broker_optimized_max_drawdown_abs_log", pd.Series([np.nan])).iloc[0]),
        "best_broker_optimized_max_drawdown_pct": safe_float(rep_df.get("broker_optimized_max_drawdown_pct", pd.Series([np.nan])).iloc[0]),
        "best_compound_log_growth": safe_float(rep_df["compound_log_growth"].iloc[0]),
        "best_arithmetic_compound_speed_calendar": safe_float(rep_df.get("arithmetic_compound_speed_calendar", pd.Series([np.nan])).iloc[0]),
        "best_gate_reason": str(rep_df["compound_velocity_gate_reason"].iloc[0]),
        "top_threshold_diagnostics_json": "",
        "role_to_grid_candidate": json.dumps({"compound_velocity": str(winner["grid_candidate_id"])}),
        "role_to_plateau": json.dumps({"compound_velocity": str(winner["plateau_id"])}),
        # D052 (AFML s.12.4.2 / 12.5): the path distribution of the selected threshold
        "selected_cpcv_path_count": safe_float(winner.get("cpcv_path_count")),
        "selected_cpcv_path_mean_score": safe_float(winner.get("cpcv_path_mean_score")),
        "selected_cpcv_path_median_score": safe_float(winner.get("cpcv_path_median_score")),
        "selected_cpcv_path_worst_score": safe_float(winner.get("cpcv_path_worst_score")),
        "selected_cpcv_path_std_score": safe_float(winner.get("cpcv_path_std_score")),
        "selected_cpcv_path_losing_share": safe_float(winner.get("cpcv_path_losing_share")),
        "selected_single_path_score": safe_float(winner.get("compound_velocity_score_single_path")),
        "single_path_would_select_threshold": safe_float(single_path_winner["threshold"]),
        "cpcv_path_mean_score_nonpositive": bool(safe_float(winner.get("cpcv_path_mean_score"), np.inf) <= 0.0),
    }
    return rep_df, report


def select_all_representatives(plateau_report: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    reps = []
    reports = []
    for _, g in tqdm(plateau_report.groupby("edge_id", sort=True), desc="Selecting calendar compound-velocity representatives", unit="edge"):
        rep_df, report = select_representatives_for_edge(g.copy())
        if not rep_df.empty:
            reps.append(rep_df)
        reports.append(report)
    if reps:
        rep_all = pd.concat(reps, ignore_index=True).sort_values(["asset", "family", "edge_id", "threshold"]).reset_index(drop=True)
    else:
        raise RuntimeError("No edge produced a calendar compound-velocity representative. Inspect v2_threshold_plateau_report.csv diagnostics.")
    sel_report = pd.DataFrame(reports).sort_values(["asset", "family", "edge_id"]).reset_index(drop=True)
    rejected = sel_report[sel_report["selection_status"] != "selected"].copy()
    return rep_all, sel_report, rejected


def finalize_representatives(rep: pd.DataFrame) -> pd.DataFrame:
    out = rep.copy()
    out["candidate_id"] = out["representative_id"]
    out["source_grid_candidate_id"] = out["grid_candidate_id"]
    out["candidate_type"] = out["primary_representative_role"]
    out["avg_calibrated_probability"] = out["avg_pred_prob"]
    out["calibrated_threshold"] = out["threshold"]
    front = REPRESENTATIVE_REQUIRED_COLUMNS + [
        "classification_reason", "watchlist_quality_tag", "selection_gate_level", "plateau_id",
        "plateau_index", "plateau_min_threshold", "plateau_max_threshold", "plateau_row_count",
        "plateau_trade_count_min", "plateau_trade_count_max", "trade_set_size", "trade_set_hash",
        "formula_trade_count", "active_exit_days", "calendar_span_days", "edge_backtest_span_days",
        "trades_per_active_day", "trades_per_calendar_day", "take_rate", "win_rate", "stressed_expectancy_r",
        "stressed_expectancy_lcb_r", "stressed_profit_factor", "stressed_log_profit_factor",
        "gross_profit_r", "gross_loss_r", "stressed_cumulative_r", "stressed_max_drawdown_r",
        "stressed_max_drawdown_abs_r", "afml_weighted_stressed_expectancy_r",
        "afml_weighted_stressed_profit_factor", "afml_weighted_stressed_cumulative_r",
        "afml_weighted_stressed_max_drawdown_r", "compound_power", "precision", "avg_pred_prob",
        "local_positive_ratio", "local_expectancy_mean", "local_pf_mean", "compound_usable_drawdown_budget_pct",
        "compound_allowed_risk_per_trade", "compound_log_growth", "compound_velocity",
        "compound_velocity_calendar", "compound_velocity_active", "compound_equity_return_proxy",
        "compound_log_growth_theoretical", "compound_velocity_theoretical",
        "compound_velocity_calendar_theoretical", "compound_velocity_active_theoretical",
        "compound_equity_return_proxy_theoretical", "expected_active_days_to_target_theoretical",
        "expected_calendar_days_to_target_theoretical", "compound_log_growth_broker_executable",
        "compound_velocity_broker_executable", "compound_velocity_calendar_broker_executable",
        "compound_velocity_active_broker_executable", "compound_equity_return_proxy_broker_executable",
        "expected_active_days_to_target_broker_executable",
        "expected_calendar_days_to_target_broker_executable",
        "broker_executable_risk_required", "broker_optimized_f", "broker_optimized_f_search_upper",
        "broker_optimized_f_search_points", "broker_optimized_f_search_upper_reason",
        "broker_optimized_objective", "broker_optimized_log_growth",
        "broker_optimized_compound_velocity", "broker_optimized_compound_velocity_calendar",
        "broker_optimized_compound_velocity_active", "broker_optimized_drawdown_adjusted_compound_velocity",
        "broker_optimized_drawdown_adjusted_log_growth", "broker_optimized_max_drawdown_log",
        "broker_optimized_max_drawdown_abs_log", "broker_optimized_max_drawdown_pct",
        "broker_optimized_equity_return_proxy", "broker_optimized_expected_active_days_to_target",
        "broker_optimized_expected_calendar_days_to_target", "broker_optimized_bankruptcy_margin_min",
        "broker_optimized_survival_feasible", "broker_valid_risk_row_count", "broker_invalid_risk_row_count",
        "broker_valid_risk_row_rate", "broker_min_lot_binding_trade_count",
        "broker_min_lot_binding_trade_rate", "broker_median_target_risk_amount",
        "broker_median_actual_risk_amount", "broker_p90_actual_risk_amount",
        "broker_max_actual_risk_amount", "broker_median_risk_inflation",
        "broker_p90_risk_inflation", "broker_max_risk_inflation",
        "broker_mean_actual_risk_pct", "broker_median_actual_risk_pct",
        "broker_p90_actual_risk_pct", "broker_max_actual_risk_pct",
        "broker_mean_target_risk_pct", "broker_median_target_risk_pct",
        "broker_p90_target_risk_pct", "broker_max_target_risk_pct",
        "broker_risk_metadata_status_summary",
        "expected_active_days_to_target", "expected_calendar_days_to_target", "arithmetic_compound_speed_proxy",
        "arithmetic_compound_speed_calendar", "compound_velocity_gate_level", "compound_velocity_gate_pass",
        "compound_velocity_gate_reason", "compound_velocity_score", "compound_velocity_score_basis",
    ]
    return out[[c for c in front if c in out.columns] + [c for c in out.columns if c not in front]]


def build(write: bool = True) -> dict[str, Any]:
    out = V2_ROOT / "portfolio_allocator" / "outputs"
    grid = load_grid_viability(out / "v2_threshold_grid_viability.csv")
    ledger_summary = load_grid_ledger_summary(out / "v2_threshold_grid_trade_ledger_summary.csv")
    ledger_cols = [c for c in ledger_summary.columns if c == "grid_candidate_id" or c.startswith("ledger_")]
    grid = grid.merge(ledger_summary[ledger_cols], on="grid_candidate_id", how="left", validate="one_to_one")
    trade_sets, formula_stats = build_trade_sets_and_formula_stats_from_ledger(out / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv")
    if not formula_stats.empty:
        grid = grid.merge(formula_stats, on="grid_candidate_id", how="left", validate="one_to_one")
    ledger_manifest_path = out / "v2_threshold_grid_trade_ledger_manifest.json"
    cpcv_info = (json.loads(ledger_manifest_path.read_text(encoding="utf-8")).get("cpcv_paths") or {}) \
        if ledger_manifest_path.exists() else {}
    cpcv_enabled = bool(cpcv_info.get("enabled"))
    if not cpcv_enabled and REQUIRE_CPCV_PATHS:
        raise RuntimeError(
            "04C: the 04B manifest reports no CPCV paths (D052). Re-run Step 03/04 and 04B, or pass --allow-legacy-single-path."
        )
    if cpcv_enabled:
        path_stats = cpcv_path_stats_from_ledger(out / CPCV_PATH_LEDGER_NAME, formula_stats, cpcv_info.get("paths_by_edge", {}))
        grid["grid_candidate_id"] = grid["grid_candidate_id"].astype(str)
        grid = grid.merge(path_stats, on="grid_candidate_id", how="left", validate="one_to_one")
    grid = add_compound_velocity_scores(grid)
    grid = apply_cpcv_path_selection_score(grid)
    plateau_report = assign_plateaus(grid, trade_sets)
    representatives, edge_selection_report, rejected_edges_report = select_all_representatives(plateau_report)
    representatives = finalize_representatives(representatives)
    reps_all = representatives.copy()
    reps_core = reps_all[reps_all["edge_classification"] == "core"].copy()
    reps_watchlist = reps_all[reps_all["edge_classification"] == "watchlist"].copy()
    reps_core_plus_watchlist = reps_all[reps_all["edge_classification"].isin(["core", "watchlist"])].copy()
    if reps_all["representative_id"].duplicated().any():
        raise RuntimeError("Duplicate representative_id values found.")
    for required_col in REPRESENTATIVE_REQUIRED_COLUMNS:
        if required_col not in reps_all.columns:
            raise RuntimeError(f"Internal 04C error: representative output missing required downstream column: {required_col}")
    validate_payoff_context_frame(reps_all, context="04C selected representative thresholds")
    selected_edges = set(reps_all["edge_id"].unique())
    rejected_edges = set(rejected_edges_report["edge_id"].unique()) if not rejected_edges_report.empty else set()
    input_edges = set(grid["edge_id"].unique())
    if selected_edges.union(rejected_edges) != input_edges:
        raise RuntimeError("Internal selection accounting mismatch: selected + rejected edges does not match input edge set.")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "representative_threshold_status": "PASS",
        "counts": {
            "input_viable_threshold_rows": int(len(grid)),
            "unique_input_edges": int(grid["edge_id"].nunique()),
            "plateau_rows": int(len(plateau_report)),
            "plateau_count": int(plateau_report["plateau_id"].nunique()),
            "representative_rows_all": int(len(reps_all)),
            "representative_rows_core": int(len(reps_core)),
            "representative_rows_watchlist": int(len(reps_watchlist)),
            "representative_rows_core_plus_watchlist": int(len(reps_core_plus_watchlist)),
            "representative_edges_all": int(reps_all["edge_id"].nunique()),
            "rejected_edges": int(len(rejected_edges_report)),
            "selected_edges": int(len(selected_edges)),
            "primary_gate_representatives": int((reps_all["selection_gate_level"] == "primary").sum()),
            "fallback_gate_representatives": int((reps_all["selection_gate_level"] == "fallback").sum()),
        },
        "settings": {
            "role_order": ROLE_ORDER,
            "profile": "compound_velocity",
            "formula": "for each threshold, search f; broker-round each trade to executable risk; maximize calendar velocity divided by abs(log max drawdown)",
            "diagnostic_active_formula": "same optimized-f broker-executable log-growth path divided by active_exit_days",
            "optimized_f_objective": "max broker_optimized_drawdown_adjusted_compound_velocity = broker_optimized_compound_velocity_calendar / broker_optimized_max_drawdown_abs_log",
            "optimized_f_upper": "derived from log-survival: all 1 + actual_executable_risk_pct_i(f) * stressed_r_i must remain positive; no fixed risk-percent upper is used",
            "current_balance": CURRENT_BALANCE,
            "target_balance": TARGET_BALANCE,
            "drawdown_floor": DRAWDOWN_FLOOR,
            "usable_drawdown_safety_fraction": USABLE_DRAWDOWN_SAFETY_FRACTION,
            "usable_drawdown_budget_pct": usable_drawdown_budget_pct(),
            "target_log_return": target_log_return(),
            "cost_stress_r": COMPOUND_COST_STRESS_R,
            "broker_optimized_f_coarse_grid_size": BROKER_OPTIMIZED_F_COARSE_GRID_SIZE,
            "broker_optimized_f_refined_grid_size": BROKER_OPTIMIZED_F_REFINED_GRID_SIZE,
            "broker_optimized_f_upper_safety_fraction": BROKER_OPTIMIZED_F_UPPER_SAFETY_FRACTION,
            "expectancy_lcb_z": EXPECTANCY_LCB_Z,
            "primary_min_trade_count": PRIMARY_MIN_TRADE_COUNT,
            "fallback_min_trade_count": FALLBACK_MIN_TRADE_COUNT,
            "primary_min_active_days": PRIMARY_MIN_ACTIVE_DAYS,
            "fallback_min_active_days": FALLBACK_MIN_ACTIVE_DAYS,
            "primary_min_trades_per_active_day": PRIMARY_MIN_TRADES_PER_ACTIVE_DAY,
            "fallback_min_trades_per_active_day": FALLBACK_MIN_TRADES_PER_ACTIVE_DAY,
            "primary_min_stressed_profit_factor": PRIMARY_MIN_STRESSED_PROFIT_FACTOR,
            "fallback_min_stressed_profit_factor": FALLBACK_MIN_STRESSED_PROFIT_FACTOR,
            "primary_min_local_positive_ratio": PRIMARY_MIN_LOCAL_POSITIVE_RATIO,
            "fallback_min_local_positive_ratio": FALLBACK_MIN_LOCAL_POSITIVE_RATIO,
            "plateau_jaccard_min": PLATEAU_JACCARD_MIN,
            "probability_column": PROBABILITY_COLUMN,
            "afml_column": AFML_COLUMN,
            "afml_bet_size_required_in_trade_ledger": True,
            "afml_missing_policy": "fail_closed_no_default_1p0",
            "broker_executable_risk_required": BROKER_EXECUTABLE_RISK_REQUIRED,
            "broker_risk_rounding_mode": BROKER_RISK_ROUNDING_MODE,
            "broker_required_columns": BROKER_RISK_METADATA_REQUIRED_COLUMNS,
            "selection_score_basis": CPCV_SELECTION_SCORE_BASIS if cpcv_enabled else "broker_optimized_drawdown_adjusted_compound_velocity (legacy single path)",
            "cpcv_paths": {
                "enabled": cpcv_enabled,
                "paths_by_edge": cpcv_info.get("paths_by_edge", {}),
                "policy": "AFML s.12.4.2 step 5 / s.12.5, D052: selection score = mean over CPCV paths of the drawdown-adjusted "
                          "broker compound velocity at the main path's optimized f*; gates on the main path; worst path and "
                          "losing-path share reported in the edge selection report",
                "path_ledger": f"portfolio_allocator/outputs/{CPCV_PATH_LEDGER_NAME}" if cpcv_enabled else None,
            },
            "max_actual_risk_pct_gate_in_04c": False,
        },
        "outputs": {
            "representative_threshold_candidates_all": "portfolio_allocator/outputs/v2_representative_threshold_candidates_all.csv",
            "representative_threshold_candidates_core": "portfolio_allocator/outputs/v2_representative_threshold_candidates_core.csv",
            "representative_threshold_candidates_watchlist": "portfolio_allocator/outputs/v2_representative_threshold_candidates_watchlist.csv",
            "representative_threshold_candidates_core_plus_watchlist": "portfolio_allocator/outputs/v2_representative_threshold_candidates_core_plus_watchlist.csv",
            "threshold_plateau_report": "portfolio_allocator/outputs/v2_threshold_plateau_report.csv",
            "representative_threshold_edge_selection_report": "portfolio_allocator/outputs/v2_representative_threshold_edge_selection_report.csv",
            "rejected_edge_selection_report": "portfolio_allocator/outputs/v2_rejected_compound_velocity_edges.csv",
            "manifest": "portfolio_allocator/outputs/v2_representative_threshold_candidates_manifest.json",
        },
        "notes": [
            "04C still performs threshold-grid plateau compression and emits the same representative files expected by Steps 05+.",
            "Only one representative profile is used: compound_velocity.",
            "compound_velocity now means broker-optimized calendar compound velocity at the f that maximizes drawdown-adjusted broker-executable compound velocity.",
            "trade-level afml_bet_size is now mandatory; 04C fails closed if it is missing or non-numeric instead of defaulting to 1.0.",
            "old fixed-risk-proxy theoretical compound velocity is intentionally not used for selection; optimized-f broker fields drive 04C.",
            "broker risk inflation and min-lot binding diagnostics are reported but not hard-capped in 04C; hard risk caps belong to Steps 08+.",
            "compound_velocity_active remains as a diagnostic for active-day edge efficiency.",
            "Edges with no positive gated calendar compound velocity are skipped and written to v2_rejected_compound_velocity_edges.csv instead of aborting the pipeline.",
            "File-content hashes are optional metadata only. trade_set_hash and geometry_hash are deterministic semantic identity fingerprints, not artifact invalidation gates.",
        ],
    }
    if write:
        plateau_report.to_csv(out / "v2_threshold_plateau_report.csv", index=False)
        edge_selection_report.to_csv(out / "v2_representative_threshold_edge_selection_report.csv", index=False)
        rejected_edges_report.to_csv(out / "v2_rejected_compound_velocity_edges.csv", index=False)
        reps_all.to_csv(out / "v2_representative_threshold_candidates_all.csv", index=False)
        reps_core.to_csv(out / "v2_representative_threshold_candidates_core.csv", index=False)
        reps_watchlist.to_csv(out / "v2_representative_threshold_candidates_watchlist.csv", index=False)
        reps_core_plus_watchlist.to_csv(out / "v2_representative_threshold_candidates_core_plus_watchlist.csv", index=False)
        write_json(out / "v2_representative_threshold_candidates_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-legacy-single-path", action="store_true",
                        help="select on the main path only when 04B ran on pre-D052 artifacts without CPCV paths")
    args = parser.parse_args()
    global REQUIRE_CPCV_PATHS
    REQUIRE_CPCV_PATHS = not args.allow_legacy_single_path
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.04C representative threshold plateau selection")
    print(f"Status: {manifest['representative_threshold_status']}")
    c = manifest["counts"]
    print(f"Input viable threshold rows: {c['input_viable_threshold_rows']}")
    print(f"Plateaus after compression: {c['plateau_count']}")
    print(f"Representative rows: {c['representative_rows_all']}")
    print(f"Representative edges: {c['representative_edges_all']}")
    print(f"Rejected edges: {c['rejected_edges']}")
    print(f"Primary/fallback representatives: {c['primary_gate_representatives']}/{c['fallback_gate_representatives']}")
    print(f"Profiles: {', '.join(manifest['settings']['role_order'])}")
    print(f"Main score: {manifest['settings']['selection_score_basis']}")
    print("AFML policy: required trade-level afml_bet_size; no default 1.0")
    print("Broker risk policy: required 04B min-lot metadata; no max-actual-risk cap in 04C")


if __name__ == "__main__":
    main()
