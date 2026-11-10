from __future__ import annotations

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
import argparse
import heapq
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
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
for p in (V2_ROOT, REPO_ROOT, THIS_FILE.parent):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

import afml_concurrency_sizing as AFMLC  # noqa: E402  (AFML Ch.10.2/10.4 concurrency-aware sizing, 2026-10-17)

from shared.prop_account_rules import (  # noqa: E402
    active_rule_contract,
    daily_breach_value as account_daily_breach_value,
    daily_drawdown_amount as account_daily_drawdown_amount,
    daily_drawdown_floor as account_daily_drawdown_floor,
    drawdown_stats as account_drawdown_stats,
    load_active_account_rules,
    minimum_days_satisfied,
    overall_drawdown_floor as account_overall_drawdown_floor,
    structurally_equal,
    target_satisfied,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.validation.v3_semantic_contract import (  # noqa: E402
    validate_representative_payoff_context_handoff,
)

SCHEMA_VERSION = "v2.3"
SCRIPT_VERSION = "2026-07-21_generic_active_account_step11_console_status_fix"
PIPELINE_MODE = "v2_generic_active_account_monte_carlo_blocking_validation"

INPUT_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
INPUT_HRP_WEIGHTS = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs" / "hrp_linkage_weights_step08_primary.csv"
INPUT_WALKTHROUGH_CONFIG = V2_ROOT / "portfolio_allocator" / "outputs" / "final_candidate_walkthrough_outputs" / "selected_candidate_live_config.json"
INPUT_WALKTHROUGH_RESULTS = V2_ROOT / "portfolio_allocator" / "outputs" / "final_candidate_walkthrough_outputs" / "walkthrough_run_results.csv"
INPUT_WALKTHROUGH_MANIFEST = V2_ROOT / "portfolio_allocator" / "outputs" / "final_candidate_walkthrough_outputs" / "final_candidate_walkthrough_manifest.json"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs"
DETAIL_DIR = OUTPUT_DIR / "worst_run_details"

ACCOUNT_RULES = load_active_account_rules()
ACCOUNT_RULE_CONTRACT = active_rule_contract(ACCOUNT_RULES)
ACCOUNT_RULE_PROFILE = str(ACCOUNT_RULES["profile_label"])
ACCOUNT_STAGE = str(ACCOUNT_RULES["account_stage"])

PROP_START_BALANCE = float(ACCOUNT_RULES["initial_balance"])
CURRENT_BALANCE = float(ACCOUNT_RULES["current_balance"])
ACCOUNT_START_TRAILING_HIGH_WATERMARK = float(ACCOUNT_RULES["current_high_watermark"])
PROFIT_TARGET_PCT = float(ACCOUNT_RULES["profit_target"].get("pct") or 0.0)
TARGET_BALANCE = ACCOUNT_RULES["profit_target"].get("target_balance")
TARGET_BALANCE = float(TARGET_BALANCE) if TARGET_BALANCE is not None else float("inf")
MAX_OVERALL_LOSS_PCT = float(ACCOUNT_RULES["overall_drawdown"].get("pct") or 0.0)
MAX_OVERALL_LOSS_AMOUNT = float(ACCOUNT_RULES["overall_drawdown"].get("amount") or 0.0)
INITIAL_TRAILING_DRAWDOWN_FLOOR = account_overall_drawdown_floor(ACCOUNT_RULES, PROP_START_BALANCE)
TRAILING_DRAWDOWN_STOP_FLOOR = PROP_START_BALANCE
ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR = account_overall_drawdown_floor(
    ACCOUNT_RULES, ACCOUNT_START_TRAILING_HIGH_WATERMARK
)
ACCOUNT_START_REMAINING_BALANCE_BUFFER = CURRENT_BALANCE - ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR
ACCOUNT_START_PROFIT_TO_TARGET = TARGET_BALANCE - CURRENT_BALANCE if np.isfinite(TARGET_BALANCE) else float("inf")
ACCOUNT_START_OPEN_RISK_AMOUNT = 0.0
ASSUME_FRESH_DAILY_RESET_AT_SIMULATION_START = bool(
    ACCOUNT_RULES["assume_fresh_daily_reset_at_simulation_start"]
)
DAILY_LOSS_PCT = float(ACCOUNT_RULES["daily_drawdown"].get("pct") or 0.0)
DAILY_LOSS_AMOUNT = account_daily_drawdown_amount(
    ACCOUNT_RULES, day_start_balance=CURRENT_BALANCE, day_start_equity=CURRENT_BALANCE
)
ACCOUNT_START_DAILY_LOSS_FLOOR = account_daily_drawdown_floor(
    ACCOUNT_RULES, day_start_balance=CURRENT_BALANCE, day_start_equity=CURRENT_BALANCE
)
DAILY_RESET_TIMEZONE = str(ACCOUNT_RULES["daily_drawdown"]["reset_timezone"])
DAILY_RESET_OFFSET_HOURS = float(ACCOUNT_RULES["daily_drawdown"]["bucket_offset_hours"])
DAILY_RESET_OFFSET_NS = int(round(DAILY_RESET_OFFSET_HOURS * 60 * 60 * 1_000_000_000))
TRAILING_DRAWDOWN_MODE = str(ACCOUNT_RULES["overall_drawdown"]["type"])
FLOATING_LOSS_PROXY_MODE = "open_stop_risk"
NO_DEADLINE_EVALUATION = True
STOP_IMMEDIATELY_ON_TARGET = bool(
    ACCOUNT_RULES["profit_target"].get("stop_immediately_when_eligible", True)
)
STOP_ON_RULE_BREACH = True
MINIMUM_TRADING_DAYS_ENABLED = bool(ACCOUNT_RULES["minimum_trading_days"]["enabled"])
MINIMUM_TRADING_DAYS_REQUIRED = int(ACCOUNT_RULES["minimum_trading_days"]["required_days"])
MINIMUM_TRADING_DAY_MODE = str(ACCOUNT_RULES["minimum_trading_days"]["day_count_mode"])
MINIMUM_TRADING_DAY_PROFIT_AMOUNT = float(
    ACCOUNT_RULES["minimum_trading_days"]["minimum_day_profit_amount"]
)
PROP_MAX_SINGLE_TRADE_RISK_PCT = (
    float(ACCOUNT_RULES["maximum_risk_per_trade"]["pct"])
    if ACCOUNT_RULES["maximum_risk_per_trade"]["enabled"] else None
)
FLOATING_LOSS_GUARD_ENABLED = bool(ACCOUNT_RULES["floating_loss_guard"]["enabled"])
FLOATING_LOSS_GUARD_AMOUNT = ACCOUNT_RULES["floating_loss_guard"].get("internal_operating_amount")
FLOATING_LOSS_GUARD_AMOUNT = (
    float(FLOATING_LOSS_GUARD_AMOUNT) if FLOATING_LOSS_GUARD_AMOUNT is not None else None
)

# Broker executable risk policy mirrors Steps 08-10.
BROKER_VOLUME_ROUNDING_MODE = "ceil_then_backoff_to_largest_safe_step"
REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP = True
SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS = True
BROKER_RISK_SOURCE_POLICY = (
    "04B per-trade risk_per_1_lot derived from the 00A MT5 broker profile and "
    "00B causal historical profit-currency-to-account-currency conversion"
)
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"
DEFAULT_ACCOUNT_RULE_PROFILE = ACCOUNT_RULE_PROFILE
EXPECTED_ACCOUNT_STAGE = ACCOUNT_STAGE
EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE = str(
    ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"]
)

# Step 11 is a blocking validation gate. It never auto-accepts arbitrary failure
# rates and never upgrades challenge approval into funded-stage approval.
REQUIRED_WALKTHROUGH_STATUS = "PASS"
REQUIRED_WALKTHROUGH_LIVE_STATUS = "WALKTHROUGH_PASS_MONTE_CARLO_REQUIRED"

AFML_BET_SIZE_COLUMN = "afml_bet_size"
AFML_SIZE_FLOOR = 0.0
AFML_SIZE_CAP = 1.0
RANDOM_SEED = 20260519
HISTORICAL_BOOTSTRAP_SAMPLES = 1500
EXACT_MC_RUNS_PER_SCENARIO = 180
SAVE_WORST_RUN_TRADE_LOGS = True
WORST_RUNS_TO_REPLAY_PER_SCENARIO = 3
# Mandatory controls must reproduce Script 10 and must not hard-fail.
MANDATORY_MAX_HARD_FAIL_RATE = 0.0
MANDATORY_MIN_WORST_BUFFER = 0.0
MANDATORY_MIN_P01_BUFFER = 0.0
DIAGNOSTIC_MAX_HARD_FAIL_RATE_WARNING = 0.0100
# Mirrors 09_fast_manual_variant_lab.py's MODERATE_HARD_FAIL_TOLERANCE /
# 10_final_candidate_walkthrough.py's MODERATE_HARD_FAIL_TOLERANCE -- see
# validate_gates()'s accept_moderate_hard_fail arg.
MODERATE_HARD_FAIL_TOLERANCE = 0.03
DRY_RUNS_PER_SCENARIO = 5
CONTROL_SCENARIO_COSTS = {
    "mc_cost00_control": 0.00,
    "mc_cost02_control": 0.02,
    "mc_cost05_control": 0.05,
}
EPS = 1e-12
DAY_NS = 24 * 60 * 60 * 1_000_000_000

STRESS_SCENARIOS = [
    {"name": "mc_cost00_control", "validation_class": "mandatory", "description": "Monte Carlo resampling with 0.00R cost stress.", "cost_stress_r": 0.00},
    {"name": "mc_cost02_control", "validation_class": "mandatory", "description": "Monte Carlo resampling with 0.02R cost stress.", "cost_stress_r": 0.02},
    {"name": "mc_cost05_control", "validation_class": "mandatory", "description": "Monte Carlo resampling with 0.05R cost stress.", "cost_stress_r": 0.05},
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    value = float(value)
    return value if np.isfinite(value) else None


def compact_contract(value: Any) -> Any:
    """Normalize JSON-compatible values for deterministic contract comparison."""
    if isinstance(value, dict):
        return {str(k): compact_contract(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [compact_contract(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def source_contract(source: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "scenario_id",
        "return_mode",
        "universe",
        "profile_role",
        "linkage_method",
        "base_portfolio_risk_budget_pct",
        "max_single_trade_risk_pct",
        "max_min_lot_risk_inflation",
        "overlap_mode",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "max_open_trades_per_edge",
        "max_open_trades_per_asset",
        "account_rule_profile",
        "account_stage",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
    ]
    return compact_contract({key: source.get(key) for key in keys})


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        return np.nan if abs(b) < EPS or not np.isfinite(b) else float(a) / b
    except Exception:
        return np.nan


def ns_to_ts(ns: int | None) -> pd.Timestamp:
    return to_datetime_iso(int(ns), utc=True) if ns is not None and not pd.isna(ns) else pd.NaT



def day_from_ns(ns: int) -> int:
    return int((int(ns) + DAILY_RESET_OFFSET_NS) // DAY_NS)



def slug(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text)).strip("_")


def account_equity_proxy(balance: float, open_risk_total: float) -> float:
    """Conservative equity proxy: closed balance less worst-case open stop risk."""
    return float(balance) - max(0.0, float(open_risk_total))


def trailing_drawdown_floor(trailing_high_watermark: float) -> float:
    """Backward-compatible name for the active static/trailing overall floor."""
    return account_overall_drawdown_floor(ACCOUNT_RULES, trailing_high_watermark)


def drawdown_stats(
    balance: float,
    open_risk_total: float = 0.0,
    trailing_high_watermark: float | None = None,
) -> dict[str, Any]:
    return account_drawdown_stats(
        ACCOUNT_RULES,
        balance=balance,
        open_risk_total=open_risk_total,
        high_watermark=trailing_high_watermark,
    )


def current_trailing_buffer(
    balance: float,
    open_risk_total: float,
    trailing_high_watermark: float,
) -> float:
    """Conservative distance from the equity proxy to the active overall floor."""
    return float(
        drawdown_stats(balance, open_risk_total, trailing_high_watermark)[
            "remaining_dd_buffer"
        ]
    )


def required_trailing_buffer_reserve(
    projected_open_risk: float,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Required post-entry reserve for the configured static/dynamic policy."""
    return float(
        max(0.0, float(fixed_reserve_amount))
        + max(0.0, float(cost_allowance_r))
        * max(0.0, float(projected_open_risk))
    )


def maximum_admissible_new_risk(
    trailing_buffer: float,
    current_open_risk: float,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Solve the exact pre-rounding ceiling for proposed new stop risk."""
    allowance = max(0.0, float(cost_allowance_r))
    numerator = (
        float(trailing_buffer)
        - max(0.0, float(fixed_reserve_amount))
        - allowance * max(0.0, float(current_open_risk))
    )
    return float(numerator / (1.0 + allowance))


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Two-sided Wilson interval for a binomial proportion."""
    n = int(trials)
    if n <= 0:
        return np.nan, np.nan
    k = int(successes)
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = z * np.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)) / denom
    return float(max(0.0, centre - half)), float(min(1.0, centre + half))


def daily_loss_floor(
    daily_start_balance: float,
    daily_start_equity: float | None = None,
) -> float:
    return account_daily_drawdown_floor(
        ACCOUNT_RULES,
        day_start_balance=daily_start_balance,
        day_start_equity=(
            daily_start_balance if daily_start_equity is None else daily_start_equity
        ),
    )


def symbol_daily_risk_usage(closed_loss_amount: float, floating_stop_risk: float) -> float:
    """Closed loss plus conservative floating stop risk for one symbol/day."""
    return max(0.0, float(closed_loss_amount)) + max(0.0, float(floating_stop_risk))


def validate_account_start_state() -> None:
    """Fail closed when the live account snapshot is internally inconsistent."""
    if not np.isfinite(CURRENT_BALANCE) or CURRENT_BALANCE <= 0.0:
        raise ValueError(f"CURRENT_BALANCE must be positive, got {CURRENT_BALANCE!r}")
    if (
        not np.isfinite(ACCOUNT_START_TRAILING_HIGH_WATERMARK)
        or ACCOUNT_START_TRAILING_HIGH_WATERMARK < max(PROP_START_BALANCE, CURRENT_BALANCE) - EPS
    ):
        raise ValueError(
            "ACCOUNT_START_TRAILING_HIGH_WATERMARK must be at least both initial and current balance: "
            f"{ACCOUNT_START_TRAILING_HIGH_WATERMARK}"
        )
    expected_floor = trailing_drawdown_floor(ACCOUNT_START_TRAILING_HIGH_WATERMARK)
    if not np.isclose(
        expected_floor,
        ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        rtol=0.0,
        atol=EPS,
    ):
        raise ValueError(
            "Account-start trailing floor mismatch: "
            f"{ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR} != {expected_floor}"
        )
    if CURRENT_BALANCE < ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR - EPS:
        raise ValueError(
            "Current balance is already below the live trailing floor: "
            f"{CURRENT_BALANCE} < {ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR}"
        )


def validate_broker_risk_handoff(df: pd.DataFrame) -> None:
    """Verify that 04B's currency-converted broker-risk fields stayed coherent."""
    expected_min = df["risk_per_1_lot"] * df["broker_min_lot"]
    expected_step = df["risk_per_1_lot"] * df["broker_lot_step"]
    checks = {
        "risk_per_min_lot": np.isclose(
            df["risk_per_min_lot"], expected_min, rtol=1e-9, atol=1e-8
        ),
        "risk_per_lot_step": np.isclose(
            df["risk_per_lot_step"], expected_step, rtol=1e-9, atol=1e-8
        ),
        "min_executable_risk_amount": np.isclose(
            df["min_executable_risk_amount"], expected_min, rtol=1e-9, atol=1e-8
        ),
    }
    failures = {name: int((~mask).sum()) for name, mask in checks.items() if (~mask).any()}
    if failures:
        raise ValueError(
            "Representative ledger broker/currency risk handoff is inconsistent: "
            f"{failures}. Rerun Steps 00A, 00B, 04B, 04BB, 04C and 05."
        )



def multiplier_from_balance(balance: float, tiers: list[list[float]] | list[tuple[float, float]], mode: str) -> float:
    mult = 1.0
    for threshold, tier_mult in tiers or []:
        if (mode == "below" and balance < float(threshold)) or (mode == "above" and balance >= float(threshold)):
            mult = min(mult, float(tier_mult))
    return float(np.clip(mult, 0.0, 1.0))



def parse_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return default
    try:
        if pd.isna(x):
            return default
    except Exception:
        pass
    text = str(x).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return default


def broker_executable_risk(
    target_risk_amount: float,
    risk_per_1_lot: float,
    broker_min_lot: float,
    broker_lot_step: float,
    max_risk_amount: float | None = None,
) -> dict[str, Any]:
    """Convert target risk into the largest safe broker-executable lot.

    Policy:
    - clip requested risk to the tightest active cap;
    - round to a broker-valid lot;
    - when ceil rounding breaches the cap, step down to the largest lot that fits;
    - return ``cap_fit=False`` only when no positive broker-valid lot can fit,
      normally because the minimum lot itself exceeds the remaining cap.
    """
    invalid = {
        "valid": False,
        "raw_lot": np.nan,
        "initial_ceil_lot": np.nan,
        "actual_lot": np.nan,
        "actual_risk_amount": np.nan,
        "risk_inflation": np.nan,
        "min_lot_binding": False,
        "backoff_applied": False,
        "backoff_steps": 0,
        "cap_fit": False,
        "minimum_lot_exceeds_cap": False,
        "target_reduced_to_cap": False,
        "requested_target_risk_amount": np.nan,
        "sizing_target_risk_amount": np.nan,
    }
    try:
        target = float(target_risk_amount)
        r1 = float(risk_per_1_lot)
        min_lot = float(broker_min_lot)
        step = float(broker_lot_step)
        cap = np.inf if max_risk_amount is None else max(0.0, float(max_risk_amount))
    except Exception:
        return invalid

    if (
        not all(np.isfinite(x) for x in [target, r1, min_lot, step])
        or target <= EPS
        or r1 <= EPS
        or min_lot <= EPS
        or step <= EPS
        or (not np.isfinite(cap) and cap != np.inf)
    ):
        return invalid

    # The effective target may be smaller than the model target because existing
    # total/asset/family/edge exposure or a drawdown reserve has consumed capacity.
    sizing_target = min(target, cap) if np.isfinite(cap) else target
    target_reduced_to_cap = bool(np.isfinite(cap) and target > cap + EPS)
    raw_lot = sizing_target / r1
    rounded_lot = float(np.ceil(raw_lot / step - 1e-12) * step)
    initial_ceil_lot = max(min_lot, rounded_lot)
    actual_lot = initial_ceil_lot
    actual_risk_amount = actual_lot * r1
    min_lot_binding = bool(
        actual_lot <= min_lot + (step * 0.5) and raw_lot < min_lot - EPS
    )
    backoff_applied = False
    backoff_steps = 0

    if np.isfinite(cap) and actual_risk_amount > cap + EPS:
        # Preserve the minimum-lot fallback. If the minimum itself breaches the
        # hard cap, no smaller broker-valid volume exists and the caller rejects.
        if initial_ceil_lot > min_lot + EPS:
            max_safe_lot = float(
                np.floor((cap / r1) / step + 1e-12) * step
            )
            candidate = min(initial_ceil_lot - step, max_safe_lot)
            decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
            candidate = round(candidate, decimals)
            if candidate >= min_lot - EPS:
                actual_lot = max(min_lot, candidate)
                actual_risk_amount = actual_lot * r1
                # Floating-point guard: continue stepping down only if needed.
                while actual_risk_amount > cap + EPS and actual_lot > min_lot + EPS:
                    actual_lot = round(actual_lot - step, decimals)
                    actual_risk_amount = actual_lot * r1
                backoff_applied = actual_lot < initial_ceil_lot - EPS
                backoff_steps = max(
                    0,
                    int(round((initial_ceil_lot - actual_lot) / step)),
                )

    cap_fit = bool(not np.isfinite(cap) or actual_risk_amount <= cap + EPS)
    minimum_lot_exceeds_cap = bool(
        not cap_fit and actual_lot <= min_lot + (step * 0.5)
    )
    return {
        "valid": bool(np.isfinite(actual_risk_amount) and actual_risk_amount > EPS),
        "raw_lot": float(raw_lot),
        "initial_ceil_lot": float(initial_ceil_lot),
        "actual_lot": float(actual_lot),
        "actual_risk_amount": float(actual_risk_amount),
        "risk_inflation": float(actual_risk_amount / sizing_target)
        if sizing_target > EPS
        else np.nan,
        "min_lot_binding": min_lot_binding,
        "backoff_applied": backoff_applied,
        "backoff_steps": backoff_steps,
        "cap_fit": cap_fit,
        "minimum_lot_exceeds_cap": minimum_lot_exceeds_cap,
        "target_reduced_to_cap": target_reduced_to_cap,
        "requested_target_risk_amount": float(target),
        "sizing_target_risk_amount": float(sizing_target),
    }

def normalize_variant(v: dict[str, Any]) -> dict[str, Any]:
    """Normalize, but never overwrite, the selected Script 09 candidate contract."""
    out = dict(v)
    numeric = [
        "base_portfolio_risk_budget_pct",
        "max_single_trade_risk_pct",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "pause_below_balance",
    ]
    for key in numeric:
        if key not in out:
            raise ValueError(f"selected_variant missing {key}")
        out[key] = float(out[key])

    for key in ["max_open_trades_per_edge", "max_open_trades_per_asset"]:
        if key not in out:
            raise ValueError(f"selected_variant missing {key}")
        out[key] = int(out[key])

    out["max_min_lot_risk_inflation"] = optional_float(
        out.get("max_min_lot_risk_inflation")
    )
    out["min_lot_risk_inflation_guard_enabled"] = bool(
        out["max_min_lot_risk_inflation"] is not None
    )
    if (
        out["max_min_lot_risk_inflation"] is not None
        and out["max_min_lot_risk_inflation"] < 1.0 - EPS
    ):
        raise ValueError(
            "max_min_lot_risk_inflation must be None/off or at least 1.0."
        )

    out["throttle_tiers"] = [
        (float(a), float(b)) for a, b in out.get("throttle_tiers", [])
    ]
    out["target_lock_tiers"] = [
        (float(a), float(b)) for a, b in out.get("target_lock_tiers", [])
    ]
    out["trailing_buffer_admission_enabled"] = parse_bool(
        out.get("trailing_buffer_admission_enabled"), default=False
    )
    out["trailing_buffer_reserve_mode"] = str(
        out.get("trailing_buffer_reserve_mode", "off")
    )
    out["trailing_buffer_fixed_reserve_amount"] = float(
        out.get(
            "trailing_buffer_fixed_reserve_amount",
            out.get("trailing_buffer_reserve_amount", 0.0),
        )
        or 0.0
    )
    out["trailing_buffer_reserve_amount"] = float(
        out.get(
            "trailing_buffer_reserve_amount",
            out["trailing_buffer_fixed_reserve_amount"],
        )
        or 0.0
    )
    out["trailing_buffer_cost_allowance_r"] = float(
        out.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0
    )
    out["trailing_buffer_admission_policy"] = str(
        out.get("trailing_buffer_admission_policy", "off")
    )
    out["daily_buffer_admission_enabled"] = parse_bool(
        out.get("daily_buffer_admission_enabled"),
        default=out["trailing_buffer_admission_enabled"],
    )
    out["daily_buffer_reserve_mode"] = str(
        out.get("daily_buffer_reserve_mode", out["trailing_buffer_reserve_mode"])
    )
    out["daily_buffer_fixed_reserve_amount"] = float(
        out.get("daily_buffer_fixed_reserve_amount", out["trailing_buffer_fixed_reserve_amount"]) or 0.0
    )
    out["daily_buffer_cost_allowance_r"] = float(
        out.get("daily_buffer_cost_allowance_r", out["trailing_buffer_cost_allowance_r"]) or 0.0
    )
    out["daily_buffer_admission_policy"] = str(
        out.get(
            "daily_buffer_admission_policy",
            "solve_target_ceiling_then_validate_broker_risk_against_dynamic_daily_reserve"
            if out["daily_buffer_admission_enabled"] else "off",
        )
    )
    out["throttle_basis"] = str(
        out.get("throttle_basis", "remaining_overall_dd_buffer_account_currency_equity_proxy")
    )

    if out["trailing_buffer_reserve_mode"] not in {
        "off",
        "static",
        "dynamic_projected_open_risk",
    }:
        raise ValueError(
            "Unsupported selected trailing_buffer_reserve_mode: "
            f"{out['trailing_buffer_reserve_mode']!r}"
        )
    if out["trailing_buffer_fixed_reserve_amount"] < 0.0:
        raise ValueError("Trailing-buffer fixed reserve cannot be negative.")
    if out["trailing_buffer_cost_allowance_r"] < 0.0:
        raise ValueError("Trailing-buffer cost allowance cannot be negative.")
    if out["throttle_basis"] != "remaining_overall_dd_buffer_account_currency_equity_proxy":
        raise ValueError(
            "Script 10 requires the Step 09 trailing-buffer throttle basis; "
            f"got {out['throttle_basis']!r}."
        )

    enabled = out["trailing_buffer_admission_enabled"]
    mode = out["trailing_buffer_reserve_mode"]
    fixed = out["trailing_buffer_fixed_reserve_amount"]
    allowance = out["trailing_buffer_cost_allowance_r"]
    if not enabled and (mode != "off" or fixed > EPS or allowance > EPS):
        raise ValueError(
            "Selected candidate disables trailing-buffer admission but retains an active reserve formula."
        )
    if enabled and mode == "off":
        raise ValueError(
            "Selected candidate enables trailing-buffer admission with reserve_mode='off'."
        )
    if mode == "static" and allowance > EPS:
        raise ValueError("Static reserve candidate cannot use a positive cost allowance.")
    if mode == "dynamic_projected_open_risk" and allowance <= EPS:
        raise ValueError("Dynamic reserve candidate requires a positive cost allowance.")
    if out["daily_buffer_admission_enabled"] != enabled:
        raise ValueError("Daily-buffer admission must mirror trailing-buffer admission.")
    if out["daily_buffer_reserve_mode"] != mode:
        raise ValueError("Daily-buffer reserve mode must mirror trailing-buffer reserve mode.")
    if not np.isclose(out["daily_buffer_fixed_reserve_amount"], fixed, rtol=0.0, atol=EPS):
        raise ValueError("Daily-buffer fixed reserve must mirror trailing-buffer fixed reserve.")
    if not np.isclose(out["daily_buffer_cost_allowance_r"], allowance, rtol=0.0, atol=EPS):
        raise ValueError("Daily-buffer cost allowance must mirror trailing-buffer cost allowance.")

    # Preserve and validate the current generic account-rule contract.
    out["account_rule_profile"] = str(out.get("account_rule_profile", ""))
    out["account_stage"] = str(out.get("account_stage", ""))
    out["daily_drawdown_name"] = str(out.get("daily_drawdown_name", ""))
    out["daily_drawdown_enabled"] = parse_bool(
        out.get("daily_drawdown_enabled"), default=False
    )
    out["daily_drawdown_pct"] = float(out.get("daily_drawdown_pct", 0.0) or 0.0)
    out["daily_drawdown_amount"] = float(
        out.get("daily_drawdown_amount", DAILY_LOSS_AMOUNT) or DAILY_LOSS_AMOUNT
    )
    out["daily_drawdown_mode"] = str(out.get("daily_drawdown_mode", "off"))
    out["daily_drawdown_breach_basis"] = str(
        out.get("daily_drawdown_breach_basis", ACCOUNT_RULES["daily_drawdown"]["breach_basis"])
    )
    out["daily_symbol_risk_rule_enabled"] = parse_bool(
        out.get("daily_symbol_risk_rule_enabled"), default=False
    )
    out["daily_symbol_risk_enforcement_mode"] = str(
        out.get("daily_symbol_risk_enforcement_mode", "")
    )
    for key in [
        "max_daily_risk_per_symbol_pct",
        "max_daily_risk_per_symbol_amount",
        "internal_operating_risk_per_symbol_pct",
        "internal_operating_risk_per_symbol_amount",
        "symbol_risk_safety_reserve_amount",
    ]:
        out[key] = optional_float(out.get(key))
    out["symbol_risk_admission_policy"] = str(
        out.get("symbol_risk_admission_policy", "")
    )

    if out["account_rule_profile"] != ACCOUNT_RULE_PROFILE:
        raise ValueError(
            "Script 10 requires the current active account profile; got "
            f"{out['account_rule_profile']!r}."
        )
    if out["account_stage"] != ACCOUNT_STAGE:
        raise ValueError(
            f"Script 10 requires account_stage={ACCOUNT_STAGE!r}; got {out['account_stage']!r}."
        )
    if out["daily_drawdown_enabled"] != bool(ACCOUNT_RULES["daily_drawdown"]["enabled"]):
        raise ValueError("Script 10 daily-drawdown enabled state differs from active rules.")
    if not np.isclose(out["daily_drawdown_pct"], DAILY_LOSS_PCT, rtol=0.0, atol=EPS):
        raise ValueError(
            f"Daily drawdown mismatch: {out['daily_drawdown_pct']} != {DAILY_LOSS_PCT}"
        )
    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    if out["daily_symbol_risk_rule_enabled"] != bool(expected_symbol["enabled"]):
        raise ValueError("Selected Script 09 candidate symbol-risk enabled state differs from active rules.")
    if (
        out["daily_symbol_risk_enforcement_mode"]
        != EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE
    ):
        raise ValueError(
            "Unexpected daily symbol-risk enforcement mode: "
            f"{out['daily_symbol_risk_enforcement_mode']!r}"
        )
    expected_fields = {
        "max_daily_risk_per_symbol_pct": expected_symbol.get("max_pct"),
        "max_daily_risk_per_symbol_amount": expected_symbol.get("max_amount"),
        "internal_operating_risk_per_symbol_pct": expected_symbol.get("internal_operating_pct"),
        "internal_operating_risk_per_symbol_amount": expected_symbol.get("internal_operating_amount"),
    }
    for key, expected in expected_fields.items():
        actual = out[key]
        if expected is None:
            if actual is not None:
                raise ValueError(f"Inactive rule requires {key}=None; got {actual!r}.")
        elif actual is None or not np.isclose(actual, float(expected), rtol=0.0, atol=EPS):
            raise ValueError(f"{key}={actual!r} differs from active rule {expected!r}.")
    if PROP_MAX_SINGLE_TRADE_RISK_PCT is not None and out["max_single_trade_risk_pct"] > PROP_MAX_SINGLE_TRADE_RISK_PCT + EPS:
        raise ValueError("Selected candidate exceeds the active maximum risk per trade.")
    if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
        if out["max_total_open_risk_pct"] > FLOATING_LOSS_GUARD_AMOUNT / PROP_START_BALANCE + EPS:
            raise ValueError("Selected candidate exceeds the active floating-loss operating cap.")
    return out



def resolve_symbol_risk_contract(
    payload: dict[str, Any],
    *,
    payload_name: str,
) -> tuple[bool, str]:
    """Resolve symbol-risk settings from the canonical available location.

    Script 10's selected config stores these fields inside ``selected_variant``.
    Its manifest also exposes them at the top level. Active-account rules are a
    final structural fallback because they were already checked for exact
    equality before this function is used.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"{payload_name} must be a JSON object.")

    candidates: list[tuple[str, dict[str, Any]]] = [(payload_name, payload)]

    selected_variant = payload.get("selected_variant")
    if isinstance(selected_variant, dict):
        candidates.append((f"{payload_name}.selected_variant", selected_variant))

    active_rules = payload.get("active_account_rules")
    if isinstance(active_rules, dict):
        nested_symbol = active_rules.get("daily_symbol_risk")
        if isinstance(nested_symbol, dict):
            candidates.append(
                (f"{payload_name}.active_account_rules.daily_symbol_risk", nested_symbol)
            )

    resolved_enabled: bool | None = None
    resolved_mode: str | None = None
    resolved_source: str | None = None

    for source_name, candidate in candidates:
        if (
            "daily_symbol_risk_rule_enabled" in candidate
            or "daily_symbol_risk_enforcement_mode" in candidate
        ):
            enabled_raw = candidate.get("daily_symbol_risk_rule_enabled")
            mode_raw = candidate.get("daily_symbol_risk_enforcement_mode")
        elif "enabled" in candidate or "enforcement_mode" in candidate:
            enabled_raw = candidate.get("enabled")
            mode_raw = candidate.get("enforcement_mode")
        else:
            continue

        if enabled_raw is None or mode_raw is None:
            continue

        enabled = parse_bool(enabled_raw, default=False)
        mode = str(mode_raw).strip()

        if resolved_enabled is None:
            resolved_enabled = enabled
            resolved_mode = mode
            resolved_source = source_name
            continue

        if enabled != resolved_enabled or mode != resolved_mode:
            raise ValueError(
                f"{payload_name} contains conflicting symbol-risk contracts: "
                f"{resolved_source} resolved to "
                f"(enabled={resolved_enabled}, mode={resolved_mode!r}), while "
                f"{source_name} resolved to (enabled={enabled}, mode={mode!r})."
            )

    if resolved_enabled is None or resolved_mode is None:
        raise ValueError(
            f"{payload_name} does not contain a resolvable symbol-risk contract."
        )

    return bool(resolved_enabled), str(resolved_mode)


def load_walkthrough_config(*, accept_moderate_hard_fail: bool = False) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    """Load Script 10 outputs and fail closed on structural account/candidate drift.

    accept_moderate_hard_fail mirrors the same flag in Steps 09/10: Script 10
    itself already applies this override to its OWN walkthrough_status gate,
    but this function independently re-checks Script 10's raw
    hard_fail_count against a strict-zero requirement, so the same override
    must be threaded through here too or a moderate-hard-fail-approved
    Script 10 output would still be rejected here."""
    cfg = load_json(INPUT_WALKTHROUGH_CONFIG)
    manifest = load_json(INPUT_WALKTHROUGH_MANIFEST)
    if not INPUT_WALKTHROUGH_RESULTS.exists():
        raise FileNotFoundError(f"Missing walkthrough results: {INPUT_WALKTHROUGH_RESULTS}")

    if "source_scenario" not in cfg or "selected_variant" not in cfg:
        raise ValueError(
            "selected_candidate_live_config.json must contain source_scenario and selected_variant"
        )
    if str(manifest.get("walkthrough_status")) != REQUIRED_WALKTHROUGH_STATUS:
        raise ValueError(
            f"Script 10 walkthrough is not approved: {manifest.get('walkthrough_status')}"
        )
    if str(cfg.get("live_approval_status")) != REQUIRED_WALKTHROUGH_LIVE_STATUS:
        raise ValueError(
            "Script 10 live approval status is not the expected Monte-Carlo-required state: "
            f"{cfg.get('live_approval_status')}"
        )

    source = dict(cfg["source_scenario"])
    variant = normalize_variant(cfg["selected_variant"])
    label = str(cfg.get("selected_variant_label") or variant.get("name") or "selected_candidate")

    for name, payload in [
        ("Script 10 config", cfg.get("active_account_rules")),
        ("Script 10 manifest", manifest.get("active_account_rules")),
    ]:
        if not structurally_equal(payload, ACCOUNT_RULE_CONTRACT):
            raise ValueError(
                f"{name} active account rules differ from active_account_rules.json. Rerun Scripts 08-10."
            )
    if str(cfg.get("account_rule_profile")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Script 10 config account profile differs from active rules.")
    if str(cfg.get("account_stage")) != ACCOUNT_STAGE:
        raise ValueError("Script 10 config account stage differs from active rules.")
    if str(manifest.get("account_rule_profile")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Script 10 manifest account profile differs from active rules.")
    if str(manifest.get("account_stage")) != ACCOUNT_STAGE:
        raise ValueError("Script 10 manifest account stage differs from active rules.")

    if str(cfg.get("selected_variant_label")) != str(manifest.get("selected_variant_label")):
        raise ValueError("Script 10 config/manifest selected labels do not match.")
    if compact_contract(cfg.get("selected_variant")) != compact_contract(manifest.get("selected_variant")):
        raise ValueError("Script 10 config/manifest selected variants do not match.")
    if source_contract(source) != source_contract(manifest.get("source_scenario", {}) or {}):
        raise ValueError("Script 10 config/manifest source scenarios do not match.")

    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    expected_symbol_enabled = bool(expected_symbol["enabled"])
    expected_symbol_mode = str(expected_symbol["enforcement_mode"])

    for name, payload in [
        ("config", cfg),
        ("manifest", manifest),
        ("variant", variant),
    ]:
        actual_enabled, actual_mode = resolve_symbol_risk_contract(
            payload,
            payload_name=f"Script 10 {name}",
        )
        if actual_enabled != expected_symbol_enabled:
            raise ValueError(
                f"Script 10 {name} symbol-risk enabled state differs from active rules: "
                f"{actual_enabled} != {expected_symbol_enabled}."
            )
        if actual_mode != expected_symbol_mode:
            raise ValueError(
                f"Script 10 {name} symbol-risk mode differs from active rules: "
                f"{actual_mode!r} != {expected_symbol_mode!r}."
            )

    cfg_broker_policy = cfg.get("broker_risk_policy", {}) or {}
    manifest_broker_policy = manifest.get("broker_risk_policy", {}) or {}
    if compact_contract(cfg_broker_policy) != compact_contract(manifest_broker_policy):
        raise ValueError("Script 10 broker-risk policies do not match.")
    if not parse_bool(cfg_broker_policy.get("reduce_trade_to_fit_effective_risk_cap"), default=False):
        raise ValueError("Script 10 did not enable reduce-to-fit effective risk sizing.")
    if not parse_bool(cfg_broker_policy.get("skip_trade_if_no_executable_lot_fits"), default=False):
        raise ValueError("Script 10 did not fail closed when no broker-valid lot fits.")
    if str(cfg_broker_policy.get("broker_risk_source_policy", "")) != BROKER_RISK_SOURCE_POLICY:
        raise ValueError("Script 10 broker/currency risk source policy mismatch.")

    rule_contract = cfg.get("active_account_rule_contract", {}) or {}
    manifest_rule_contract = manifest.get("active_account_rule_contract", {}) or {}
    if compact_contract(rule_contract) != compact_contract(manifest_rule_contract):
        raise ValueError("Script 10 active account rule summaries do not match.")
    for field, expected in {
        "profile_label": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
    }.items():
        if str(rule_contract.get(field)) != str(expected):
            raise ValueError(f"Script 10 account rule summary mismatch for {field}.")
    for field, expected in {
        "current_balance": CURRENT_BALANCE,
        "current_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "account_start_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
    }.items():
        actual = optional_float(rule_contract.get(field))
        if actual is None or not np.isclose(actual, expected, rtol=0.0, atol=EPS):
            raise ValueError(f"Script 10 account-state mismatch for {field}: {actual!r} != {expected}")

    daily_manifest = manifest.get("daily_drawdown", {}) or {}
    if str(daily_manifest.get("reset_timezone")) != DAILY_RESET_TIMEZONE:
        raise ValueError("Script 10 manifest daily reset timezone mismatch.")
    manifest_offset = optional_float(daily_manifest.get("reset_offset_hours"))
    if manifest_offset is None or not np.isclose(
        manifest_offset, DAILY_RESET_OFFSET_HOURS, rtol=0.0, atol=EPS
    ):
        raise ValueError("Script 10 manifest daily reset offset mismatch.")

    integrity_cfg = cfg.get("walkthrough_integrity", {}) or {}
    integrity_manifest = manifest.get("walkthrough_integrity", {}) or {}
    if compact_contract(integrity_cfg) != compact_contract(integrity_manifest):
        raise ValueError("Script 10 walkthrough integrity objects do not match.")
    required_zero_fields = [
        "projected_buffer_reserve_violation_count",
        "start_timestamp_mismatch_count",
        "start_cost_mismatch_count",
    ]
    bad_integrity = {
        key: integrity_cfg.get(key)
        for key in required_zero_fields
        if int(integrity_cfg.get(key, -1)) != 0
    }
    hard_fail_count = int(integrity_cfg.get("hard_fail_count", -1))
    total_trials = int((manifest.get("counts") or {}).get("replay_starts", 0))
    hard_fail_rate = safe_div(hard_fail_count, total_trials) if total_trials else float("nan")
    hard_fail_ok = hard_fail_count == 0
    if (
        not hard_fail_ok
        and accept_moderate_hard_fail
        and np.isfinite(hard_fail_rate)
        and hard_fail_rate <= MODERATE_HARD_FAIL_TOLERANCE + EPS
    ):
        # Mirrors Steps 09/10's ACCEPT_MODERATE_HARD_FAIL: an explicit,
        # logged human decision to accept nonzero but capped (<=3%)
        # hard-fail as a deliberate speed/tail-risk trade-off, already
        # applied upstream at Script 10 -- this just stops that decision
        # from being silently re-rejected here.
        hard_fail_ok = True
    elif not hard_fail_ok:
        bad_integrity["hard_fail_count"] = hard_fail_count
    if bad_integrity:
        raise ValueError(f"Script 10 integrity check failed: {bad_integrity}")
    for key in ["candidate_contract_match", "exact_replay_source", "step09_replay_starts_found"]:
        if not parse_bool(integrity_cfg.get(key), default=False):
            raise ValueError(f"Script 10 integrity requires {key}=True.")

    if str(integrity_cfg.get("selected_variant_label")) != label:
        raise ValueError("Script 10 integrity selected label mismatch.")
    if str(integrity_cfg.get("selected_variant_name")) != str(variant.get("name")):
        raise ValueError("Script 10 integrity selected variant name mismatch.")
    if compact_contract(integrity_cfg.get("selected_variant_contract")) != compact_contract(variant):
        raise ValueError("Script 10 integrity selected variant contract mismatch.")
    if compact_contract(integrity_cfg.get("selected_source_contract")) != source_contract(source):
        raise ValueError("Script 10 integrity selected source contract mismatch.")
    if str(integrity_cfg.get("selected_throttle_name")) != str(variant.get("throttle_name")):
        raise ValueError("Script 10 integrity throttle mismatch.")
    if str(integrity_cfg.get("selected_reserve_mode")) != str(variant.get("trailing_buffer_reserve_mode")):
        raise ValueError("Script 10 integrity reserve mode mismatch.")
    if not np.isclose(
        float(integrity_cfg.get("selected_fixed_reserve_amount", np.nan)),
        float(variant.get("trailing_buffer_fixed_reserve_amount", np.nan)),
        rtol=0.0,
        atol=EPS,
    ):
        raise ValueError("Script 10 integrity fixed reserve mismatch.")
    if not np.isclose(
        float(integrity_cfg.get("selected_cost_allowance_r", np.nan)),
        float(variant.get("trailing_buffer_cost_allowance_r", np.nan)),
        rtol=0.0,
        atol=EPS,
    ):
        raise ValueError("Script 10 integrity cost allowance mismatch.")

    return source, variant, label, cfg, manifest

def load_ledger() -> tuple[pd.DataFrame, dict[str, int], dict[str, int], dict[str, int]]:
    if not INPUT_LEDGER.exists():
        raise FileNotFoundError(f"Missing representative ledger: {INPUT_LEDGER}")
    df = pd.read_csv(INPUT_LEDGER)
    required = [
        "representative_id", "edge_id", "asset", "family", "entry_time", "exit_time",
        "executed_r", AFML_BET_SIZE_COLUMN,
        "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "broker_contract_size",
        "risk_per_min_lot", "risk_per_lot_step", "min_executable_risk_amount",
        "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk",
        "payoff_context_version", "payoff_context_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            "Representative ledger missing broker/min-lot-aware columns: "
            f"{missing}. Rerun patched Steps 04B, 04C, and 05 first."
        )
    df["entry_time"] = to_datetime_iso(df["entry_time"], utc=True, errors="coerce")
    df["exit_time"] = to_datetime_iso(df["exit_time"], utc=True, errors="coerce")
    numeric_cols = [
        "executed_r", AFML_BET_SIZE_COLUMN, "calibrated_probability", "raw_probability",
        "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "broker_contract_size",
        "risk_per_min_lot", "risk_per_lot_step", "min_executable_risk_amount",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    validate_broker_risk_handoff(df)
    valid_bool = df["has_valid_broker_min_lot_risk"].astype(str).str.lower().isin(["true", "1", "yes", "y"])
    positive_broker = (
        (df["risk_per_1_lot"] > EPS)
        & (df["broker_min_lot"] > EPS)
        & (df["broker_lot_step"] > EPS)
        & (df["broker_contract_size"] > EPS)
        & (df["risk_per_min_lot"] > EPS)
        & (df["risk_per_lot_step"] > EPS)
        & (df["min_executable_risk_amount"] > EPS)
    )
    invalid = df[~(valid_bool & positive_broker)].copy()
    if not invalid.empty:
        sample_cols = [
            c for c in [
                "representative_id", "edge_id", "asset", "entry_time", "risk_per_1_lot",
                "broker_min_lot", "broker_lot_step", "min_executable_risk_amount",
                "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk",
            ] if c in invalid.columns
        ]
        raise ValueError(
            "Representative ledger contains invalid broker/min-lot risk rows. "
            f"Invalid rows: {len(invalid)}. Sample: {invalid[sample_cols].head(5).to_dict(orient='records')}"
        )

    df["afml_bet_size_for_sizing"] = df[AFML_BET_SIZE_COLUMN].clip(lower=AFML_SIZE_FLOOR, upper=AFML_SIZE_CAP)
    df = df.dropna(subset=[
        "representative_id", "edge_id", "asset", "family", "entry_time", "exit_time",
        "executed_r", "afml_bet_size_for_sizing", "risk_per_1_lot", "broker_min_lot", "broker_lot_step",
    ]).copy()
    df = df[(df["exit_time"] > df["entry_time"]) & (df["afml_bet_size_for_sizing"] > EPS)].copy()
    if df.empty:
        raise RuntimeError("Representative ledger is empty after broker/min-lot-aware cleaning.")
    df = df.sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    asset_to_code = {a: i for i, a in enumerate(sorted(df["asset"].astype(str).unique()))}
    family_to_code = {a: i for i, a in enumerate(sorted(df["family"].astype(str).unique()))}
    edge_to_code = {a: i for i, a in enumerate(sorted(df["edge_id"].astype(str).unique()))}
    df["asset_code"] = df["asset"].astype(str).map(asset_to_code).astype(int)
    df["family_code"] = df["family"].astype(str).map(family_to_code).astype(int)
    df["edge_code"] = df["edge_id"].astype(str).map(edge_to_code).astype(int)
    return df, asset_to_code, family_to_code, edge_to_code

def load_hrp_weights() -> pd.DataFrame:
    df = pd.read_csv(INPUT_HRP_WEIGHTS)
    required = [
        "return_mode", "universe", "profile_role", "linkage_method",
        "representative_id", "weight", "payoff_context_version", "payoff_context_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"HRP weights missing columns: {missing}")
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna(subset=required)
    df = df[df["weight"] > 0].copy()
    parts = []
    for _, g in df.groupby(["return_mode", "universe", "profile_role", "linkage_method"], sort=False):
        g = g.copy()
        g["weight"] = g["weight"] / g["weight"].sum()
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def prepare_arrays(source: dict[str, Any], ledger: pd.DataFrame, weights: pd.DataFrame) -> dict[str, Any]:
    sw = weights[
        (weights["return_mode"] == source["return_mode"])
        & (weights["universe"] == source["universe"])
        & (weights["profile_role"] == source["profile_role"])
        & (weights["linkage_method"] == source["linkage_method"])
    ].copy()
    if sw.empty:
        raise RuntimeError(f"No HRP weights match selected source scenario: {source.get('scenario_id')}")
    sw["weight"] = sw["weight"] / sw["weight"].sum()
    wmap = dict(zip(sw["representative_id"].astype(str), sw["weight"]))
    trades = ledger[ledger["representative_id"].astype(str).isin(wmap.keys())].copy()
    if trades.empty:
        raise RuntimeError("No ledger trades found for selected HRP representatives.")
    validate_representative_payoff_context_handoff(
        trades, sw, context=f"Step11 selected stress source {source.get('scenario_id')}",
    )
    trades["hrp_weight"] = trades["representative_id"].astype(str).map(wmap)
    trades = trades.dropna(subset=["hrp_weight"]).sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    final_data_end_ns = int(max(trades["entry_time"].astype("int64").max(), trades["exit_time"].astype("int64").max()))
    return {
        "weights": sw,
        "entry_ns": trades["entry_time"].astype("int64").to_numpy(dtype=np.int64),
        "exit_ns": trades["exit_time"].astype("int64").to_numpy(dtype=np.int64),
        "entry_day": ((trades["entry_time"].astype("int64") + DAILY_RESET_OFFSET_NS) // DAY_NS).to_numpy(dtype=np.int64),
        "executed_r": trades["executed_r"].to_numpy(dtype=float),
        "hrp_weight": trades["hrp_weight"].to_numpy(dtype=float),
        "afml_bet_size": trades["afml_bet_size_for_sizing"].to_numpy(dtype=float),
        "risk_per_1_lot": trades["risk_per_1_lot"].to_numpy(dtype=float),
        "broker_min_lot": trades["broker_min_lot"].to_numpy(dtype=float),
        "broker_lot_step": trades["broker_lot_step"].to_numpy(dtype=float),
        "risk_per_min_lot": trades["risk_per_min_lot"].to_numpy(dtype=float),
        "risk_per_lot_step": trades["risk_per_lot_step"].to_numpy(dtype=float),
        "min_executable_risk_amount": trades["min_executable_risk_amount"].to_numpy(dtype=float),
        "asset_code": trades["asset_code"].to_numpy(dtype=np.int32),
        "family_code": trades["family_code"].to_numpy(dtype=np.int32),
        "edge_code": trades["edge_code"].to_numpy(dtype=np.int32),
        "asset": trades["asset"].astype(str).to_numpy(),
        "representative_id": trades["representative_id"].astype(str).to_numpy(),
        "edge_id": trades["edge_id"].astype(str).to_numpy(),
        "trade_count_available": int(len(trades)),
        "final_data_end_ns": final_data_end_ns,
    }

def apply_trade_stress(
    r: float,
    asset: str,
    scenario: dict[str, Any],
    rng: np.random.Generator,
    entry_day: int,
    cluster_windows: list[tuple[int, int]],
) -> float | None:
    """Apply deterministic and stochastic execution/edge degradation.

    Stresses are applied to the realized R outcome. Gap-loss shocks may push a
    losing trade beyond -1R, while cluster windows create correlated degradation
    across all assets for contiguous calendar periods.
    """
    if (
        float(scenario.get("random_drop_prob", 0.0)) > 0
        and rng.random() < float(scenario.get("random_drop_prob", 0.0))
    ):
        return None

    out = float(r) - float(scenario.get("cost_stress_r", 0.0))
    if out > 0:
        out *= 1.0 - float(scenario.get("win_haircut", 0.0))
    elif out < 0:
        out *= float(scenario.get("loss_multiplier", 1.0))

    sigma = float(scenario.get("adverse_abs_slippage_sigma_r", 0.0))
    if sigma > 0:
        out -= abs(float(rng.normal(0.0, sigma)))

    shock = (
        scenario.get("asset_shocks", {}).get(str(asset), {})
        if isinstance(scenario.get("asset_shocks", {}), dict)
        else {}
    )
    if shock:
        out -= float(shock.get("adverse_shift_r", 0.0))
        if out > 0:
            out *= 1.0 - float(shock.get("win_haircut", 0.0))
        elif out < 0:
            out *= float(shock.get("loss_multiplier", 1.0))

    in_cluster = any(a <= int(entry_day) <= b for a, b in cluster_windows)
    if in_cluster:
        out -= float(scenario.get("cluster_adverse_shift_r", 0.0))
        if out > 0:
            out *= 1.0 - float(scenario.get("cluster_win_haircut", 0.0))
        elif out < 0:
            out *= float(scenario.get("cluster_loss_multiplier", 1.0))

    gap_probability = float(scenario.get("loss_gap_probability", 0.0))
    gap_mean = float(scenario.get("loss_gap_extra_r_mean", 0.0))
    if out < 0 and gap_probability > 0 and gap_mean > 0:
        if rng.random() < gap_probability:
            out -= float(rng.exponential(gap_mean))

    return out


def simulate_path(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    start_ns: int,
    scenario: dict[str, Any],
    rng: np.random.Generator,
    n_assets: int,
    n_families: int,
    n_edges_global: int,
    capture_logs: bool = False,
) -> tuple[dict[str, Any], pd.DataFrame]:
    _afml_div = AFMLC.divisor_for_arr(arr, variant.get("afml_concurrency_quantile"))  # AFML Ch.10.2/10.4; None = off
    entry_ns, exit_ns = arr["entry_ns"], arr["exit_ns"]
    start_idx = int(np.searchsorted(entry_ns, start_ns, side="left"))

    cluster_windows: list[tuple[int, int]] = []
    cluster_count = int(scenario.get("cluster_window_count", 0) or 0)
    cluster_length = int(scenario.get("cluster_window_days", 0) or 0)
    if cluster_count > 0 and cluster_length > 0:
        available_days = np.unique(arr["entry_day"][start_idx:])
        if available_days.size:
            replace = cluster_count > available_days.size
            chosen = rng.choice(
                available_days,
                size=cluster_count,
                replace=replace,
            )
            cluster_windows = [
                (int(d), int(d) + cluster_length - 1) for d in chosen
            ]

    balance = min_balance = max_balance = peak_balance = float(CURRENT_BALANCE)
    trailing_high_watermark = max(ACCOUNT_START_TRAILING_HIGH_WATERMARK, balance)
    trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
    min_buffer = drawdown_stats(balance, 0.0, trailing_high_watermark)["remaining_dd_buffer"]
    max_challenge_dd_used = drawdown_stats(balance, 0.0, trailing_high_watermark)["challenge_dd_used"]
    max_realized_peak_to_valley_dd = 0.0

    open_risk_total = 0.0
    open_risk_by_asset = np.zeros(n_assets, dtype=float)
    open_risk_by_family = np.zeros(n_families, dtype=float)
    open_risk_by_edge = np.zeros(n_edges_global, dtype=float)
    open_count_by_asset = np.zeros(n_assets, dtype=np.int32)
    open_count_by_edge = np.zeros(n_edges_global, dtype=np.int32)
    open_heap: list[tuple[int, int, float, float, int, int, int, int, float, float, float, float]] = []

    attempted = accepted = skipped = closed = dropped_by_stress = 0
    skipped_pause = skipped_zero_risk = skipped_invalid_broker_risk = 0
    skipped_min_lot_risk_cap = skipped_overlap_risk_cap = 0
    skipped_total_cap = skipped_asset_cap = skipped_family_cap = skipped_edge_cap = 0
    skipped_edge_count = skipped_asset_count = 0
    skipped_daily_symbol_risk_cap = 0
    skipped_projected_trailing_buffer_reserve = 0
    skipped_projected_daily_buffer_reserve = 0
    clipped_attempted = clipped_accepted = 0
    trailing_buffer_target_clipped_attempted = 0
    throttle_reduced_attempted = throttle_reduced_accepted = 0

    min_trailing_buffer_at_attempt = np.inf
    min_trailing_buffer_at_accept = np.inf
    min_available_new_risk_at_attempt = np.inf
    min_projected_trailing_buffer_after_accept = np.inf
    min_required_trailing_buffer_reserve_after_accept = np.inf
    min_projected_buffer_excess_over_required_reserve_after_accept = np.inf

    max_open_risk_pct = 0.0
    max_open_trades = 0
    active_days = {day_from_ns(start_ns)}
    target_hit_time = failure_time = None
    target_hit_balance = np.nan
    failure_reason = ""
    trade_logs: list[dict[str, Any]] = []

    afml_size_sum = raw_risk_pct_sum = target_risk_pct_sum = actual_risk_pct_sum = 0.0
    throttle_mult_sum = target_lock_mult_sum = 0.0
    target_risk_values: list[float] = []
    actual_risk_values: list[float] = []
    risk_inflation_values: list[float] = []
    actual_lot_values: list[float] = []
    min_lot_binding_count = 0
    skipped_min_lot_inflation_guard = 0
    max_min_lot_risk_inflation = optional_float(
        variant.get("max_min_lot_risk_inflation")
    )
    max_raw_trade_risk_pct = max_target_trade_risk_pct = max_actual_trade_risk_pct = 0.0

    trailing_buffer_admission_enabled = bool(
        variant["trailing_buffer_admission_enabled"]
    )
    trailing_buffer_fixed_reserve_amount = float(
        variant["trailing_buffer_fixed_reserve_amount"]
    )
    trailing_buffer_cost_allowance_r = float(
        variant["trailing_buffer_cost_allowance_r"]
    )
    trailing_buffer_reserve_mode = str(variant["trailing_buffer_reserve_mode"])

    daily_enabled = bool(variant.get("daily_drawdown_enabled", True))
    daily_pct = float(variant.get("daily_drawdown_pct", DAILY_LOSS_PCT) or DAILY_LOSS_PCT)
    daily_amount = float(variant.get("daily_drawdown_amount", DAILY_LOSS_AMOUNT) or DAILY_LOSS_AMOUNT)
    daily_mode = str(variant.get("daily_drawdown_mode", ACCOUNT_RULES["daily_drawdown"]["mode"]))
    daily_buffer_admission_enabled = bool(
        variant.get("daily_buffer_admission_enabled", daily_enabled and trailing_buffer_admission_enabled)
    )
    daily_buffer_fixed_reserve_amount = float(
        variant.get("daily_buffer_fixed_reserve_amount", trailing_buffer_fixed_reserve_amount) or 0.0
    )
    daily_buffer_cost_allowance_r = float(
        variant.get("daily_buffer_cost_allowance_r", trailing_buffer_cost_allowance_r) or 0.0
    )
    daily_symbol_risk_rule_enabled = bool(
        variant.get("daily_symbol_risk_rule_enabled", False)
    )
    daily_symbol_risk_enforcement_mode = str(
        variant.get(
            "daily_symbol_risk_enforcement_mode",
            EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE,
        )
    )
    symbol_daily_hard_cap_amount = optional_float(
        variant.get("max_daily_risk_per_symbol_amount")
    )
    symbol_internal_operating_cap_amount = optional_float(
        variant.get("internal_operating_risk_per_symbol_amount")
    )
    if daily_symbol_risk_rule_enabled:
        if symbol_daily_hard_cap_amount is None or symbol_internal_operating_cap_amount is None:
            raise ValueError("Enabled symbol-risk rule requires legal and internal caps.")
        if symbol_internal_operating_cap_amount > symbol_daily_hard_cap_amount + EPS:
            raise ValueError(
                "Internal symbol operating cap cannot exceed legal hard cap: "
                f"{symbol_internal_operating_cap_amount} > {symbol_daily_hard_cap_amount}"
            )
    elif daily_symbol_risk_enforcement_mode != EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE:
        raise ValueError(
            f"Unexpected active-account symbol-risk mode: {daily_symbol_risk_enforcement_mode!r}"
        )

    daily_day = day_from_ns(start_ns)
    daily_start_balance = balance
    daily_start_equity = account_equity_proxy(balance, open_risk_total)
    daily_floor = daily_loss_floor(daily_start_balance, daily_start_equity) if daily_enabled else -np.inf
    daily_amount = account_daily_drawdown_amount(
        ACCOUNT_RULES,
        day_start_balance=daily_start_balance,
        day_start_equity=daily_start_equity,
    )
    daily_start_value = daily_floor + daily_amount if daily_enabled else balance
    daily_worst_buffer = daily_amount if daily_enabled else np.inf
    max_daily_drawdown_pct = 0.0
    daily_closed_loss_by_asset = np.zeros(n_assets, dtype=float)
    max_daily_symbol_risk_amount = 0.0
    max_daily_symbol_risk_pct = 0.0
    qualifying_days: set[int] = set()
    accepted_trade_days: set[int] = set()

    def daily_reference_value() -> float:
        equity = account_equity_proxy(balance, open_risk_total)
        return account_daily_breach_value(ACCOUNT_RULES, balance=balance, equity=equity)

    def refresh_qualifying_day() -> None:
        if not MINIMUM_TRADING_DAYS_ENABLED:
            return
        if MINIMUM_TRADING_DAY_MODE == "any_trade":
            qualifies = daily_day in accepted_trade_days
        else:
            qualifies = balance - daily_start_balance >= MINIMUM_TRADING_DAY_PROFIT_AMOUNT - EPS
        if qualifies:
            qualifying_days.add(daily_day)
        else:
            qualifying_days.discard(daily_day)

    def reset_daily_if_needed(event_ns: int) -> None:
        nonlocal daily_day, daily_start_balance, daily_start_equity
        nonlocal daily_start_value, daily_floor, daily_worst_buffer, daily_amount
        d = day_from_ns(event_ns)
        if d != daily_day:
            refresh_qualifying_day()
            daily_day = d
            daily_start_balance = balance
            daily_start_equity = account_equity_proxy(balance, open_risk_total)
            daily_floor = daily_loss_floor(daily_start_balance, daily_start_equity) if daily_enabled else -np.inf
            daily_amount = account_daily_drawdown_amount(
                ACCOUNT_RULES,
                day_start_balance=daily_start_balance,
                day_start_equity=daily_start_equity,
            )
            daily_start_value = daily_floor + daily_amount if daily_enabled else balance
            daily_worst_buffer = min(
                daily_worst_buffer,
                daily_amount if daily_enabled else np.inf,
            )
            daily_closed_loss_by_asset.fill(0.0)

    def check_rules(event_ns: int) -> None:
        nonlocal failure_reason, failure_time, min_buffer, daily_worst_buffer, max_daily_drawdown_pct
        nonlocal trailing_high_watermark, trailing_floor, max_daily_symbol_risk_amount, max_daily_symbol_risk_pct
        reset_daily_if_needed(event_ns)
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        st = drawdown_stats(balance, open_risk_total, trailing_high_watermark)
        min_buffer = min(min_buffer, st["remaining_dd_buffer"])
        if st["floor_breached"] and failure_reason == "":
            failure_reason = "overall_drawdown_floor_breached"
            failure_time = event_ns
            return
        if daily_enabled:
            ref = daily_reference_value()
            daily_worst_buffer = min(daily_worst_buffer, ref - daily_floor)
            max_daily_drawdown_pct = max(
                max_daily_drawdown_pct,
                safe_div(daily_start_value - ref, PROP_START_BALANCE),
            )
            if ref < daily_floor - EPS and failure_reason == "":
                failure_reason = "daily_drawdown_floor_breached"
                failure_time = event_ns
                return
        symbol_usage = daily_closed_loss_by_asset + np.maximum(open_risk_by_asset, 0.0)
        if symbol_usage.size:
            observed = float(np.max(symbol_usage))
            max_daily_symbol_risk_amount = max(max_daily_symbol_risk_amount, observed)
            max_daily_symbol_risk_pct = max(
                max_daily_symbol_risk_pct,
                safe_div(observed, PROP_START_BALANCE),
            )
            if (
                daily_symbol_risk_rule_enabled
                and symbol_daily_hard_cap_amount is not None
                and observed > symbol_daily_hard_cap_amount + EPS
                and failure_reason == ""
            ):
                failure_reason = "max_daily_symbol_risk_breached"
                failure_time = event_ns
    def update_balance(event_ns: int, pnl: float) -> None:
        nonlocal balance, min_balance, max_balance, peak_balance, min_buffer
        nonlocal target_hit_time, target_hit_balance, failure_time, failure_reason
        nonlocal max_challenge_dd_used, max_realized_peak_to_valley_dd
        nonlocal trailing_high_watermark, trailing_floor
        reset_daily_if_needed(event_ns)
        balance += pnl
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        min_balance = min(min_balance, balance)
        max_balance = max(max_balance, balance)
        peak_balance = max(peak_balance, balance)
        max_realized_peak_to_valley_dd = max(
            max_realized_peak_to_valley_dd, peak_balance - balance
        )
        st = drawdown_stats(balance, open_risk_total, trailing_high_watermark)
        max_challenge_dd_used = max(max_challenge_dd_used, st["challenge_dd_used"])
        min_buffer = min(min_buffer, st["remaining_dd_buffer"])
        active_days.add(day_from_ns(event_ns))
        refresh_qualifying_day()
        check_rules(event_ns)
        refresh_qualifying_day()
        if (
            target_hit_time is None
            and failure_reason == ""
            and target_satisfied(
                ACCOUNT_RULES,
                balance=balance,
                qualifying_day_count=len(qualifying_days),
            )
        ):
            target_hit_time = event_ns
            target_hit_balance = balance

    def close_due(current_ns: int) -> bool:
        nonlocal open_risk_total, closed
        while open_heap and open_heap[0][0] <= current_ns:
            this_exit_ns = open_heap[0][0]
            reset_daily_if_needed(this_exit_ns)
            batch_pnl = 0.0
            while open_heap and open_heap[0][0] == this_exit_ns:
                _, _, risk_amount, stressed_r, ac, fc, ec, trade_index, target_pct, actual_pct, actual_lot, risk_inflation = heapq.heappop(open_heap)
                open_risk_total -= risk_amount
                open_risk_by_asset[ac] -= risk_amount
                open_risk_by_family[fc] -= risk_amount
                open_risk_by_edge[ec] -= risk_amount
                open_count_by_asset[ac] -= 1
                open_count_by_edge[ec] -= 1
                pnl = risk_amount * stressed_r
                batch_pnl += pnl
                if pnl < 0.0:
                    daily_closed_loss_by_asset[ac] += -pnl
                closed += 1
                if capture_logs:
                    trade_logs.append({
                        "event_type": "close",
                        "event_time": ns_to_ts(this_exit_ns),
                        "trade_index": int(trade_index),
                        "representative_id": arr["representative_id"][trade_index],
                        "edge_id": arr["edge_id"][trade_index],
                        "asset": arr["asset"][trade_index],
                        "target_trade_risk_pct": target_pct,
                        "actual_trade_risk_pct": actual_pct,
                        "actual_lot": actual_lot,
                        "risk_inflation": risk_inflation,
                        "risk_amount": risk_amount,
                        "stressed_r": stressed_r,
                        "pnl": pnl,
                        "balance_before_batch_close": balance,
                    })
            if abs(open_risk_total) < 1e-8:
                open_risk_total = 0.0
            update_balance(this_exit_ns, batch_pnl)
            if (STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason):
                return True
        return False

    check_rules(start_ns)
    max_single_trade_risk_pct = float(variant["max_single_trade_risk_pct"])

    for i in range(start_idx, len(entry_ns)):
        current_ns = int(entry_ns[i])
        if close_due(current_ns) or target_hit_time is not None or failure_reason:
            break
        reset_daily_if_needed(current_ns)
        attempted += 1
        active_days.add(day_from_ns(current_ns))
        if balance < float(variant["pause_below_balance"]) - EPS:
            skipped_pause += 1
            skipped += 1
            continue
        stressed_r = apply_trade_stress(
            float(arr["executed_r"][i]),
            str(arr["asset"][i]),
            scenario,
            rng,
            int(arr["entry_day"][i]),
            cluster_windows,
        )
        if stressed_r is None:
            dropped_by_stress += 1
            skipped += 1
            continue

        # Exact Script 09/10 throttle basis: remaining trailing-DD buffer,
        # never the absolute account balance.
        trailing_buffer = current_trailing_buffer(
            balance, open_risk_total, trailing_high_watermark
        )
        min_trailing_buffer_at_attempt = min(
            min_trailing_buffer_at_attempt, trailing_buffer
        )
        throttle_mult = multiplier_from_balance(
            trailing_buffer, variant["throttle_tiers"], mode="below"
        )
        if throttle_mult < 1.0 - EPS:
            throttle_reduced_attempted += 1

        # Target locks remain balance/profit based.
        target_mult = multiplier_from_balance(
            balance, variant["target_lock_tiers"], mode="above"
        )
        raw_pct = (
            float(variant["base_portfolio_risk_budget_pct"])
            * (float(base_raw_pct[i]) if _afml_div is None else float(base_raw_pct[i]) / float(_afml_div[i]))
            * throttle_mult
            * target_mult
        )
        if raw_pct > max_single_trade_risk_pct + EPS:
            clipped_attempted += 1
        target_pct = min(raw_pct, max_single_trade_risk_pct)
        if not np.isfinite(target_pct) or target_pct <= EPS:
            skipped_zero_risk += 1
            skipped += 1
            continue

        target_risk_amount = balance * target_pct

        # Exact selected projected-buffer admission. First solve the theoretical
        # ceiling for off/static/dynamic mode, then validate broker-rounded risk.
        available_new_risk = np.inf
        if trailing_buffer_admission_enabled:
            available_new_risk = maximum_admissible_new_risk(
                trailing_buffer=trailing_buffer,
                current_open_risk=open_risk_total,
                fixed_reserve_amount=trailing_buffer_fixed_reserve_amount,
                cost_allowance_r=trailing_buffer_cost_allowance_r,
            )
            min_available_new_risk_at_attempt = min(
                min_available_new_risk_at_attempt, available_new_risk
            )
            if available_new_risk <= EPS:
                skipped_projected_trailing_buffer_reserve += 1
                skipped += 1
                continue
            if target_risk_amount > available_new_risk + EPS:
                trailing_buffer_target_clipped_attempted += 1
                target_risk_amount = available_new_risk
                target_pct = safe_div(target_risk_amount, balance)
                if not np.isfinite(target_pct) or target_pct <= EPS:
                    skipped_projected_trailing_buffer_reserve += 1
                    skipped += 1
                    continue

        daily_buffer = daily_reference_value() - daily_floor
        available_daily_new_risk = np.inf
        if daily_buffer_admission_enabled:
            available_daily_new_risk = maximum_admissible_new_risk(
                trailing_buffer=daily_buffer,
                current_open_risk=open_risk_total,
                fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
                cost_allowance_r=daily_buffer_cost_allowance_r,
            )
            if available_daily_new_risk <= EPS:
                skipped_projected_daily_buffer_reserve += 1
                skipped += 1
                continue
            if target_risk_amount > available_daily_new_risk + EPS:
                target_risk_amount = available_daily_new_risk
                target_pct = safe_div(target_risk_amount, balance)
                if not np.isfinite(target_pct) or target_pct <= EPS:
                    skipped_projected_daily_buffer_reserve += 1
                    skipped += 1
                    continue

        ac, fc, ec = int(arr["asset_code"][i]), int(arr["family_code"][i]), int(arr["edge_code"][i])
        effective_risk_cap = min(
            balance * max_single_trade_risk_pct,
            max(0.0, balance * float(variant["max_total_open_risk_pct"]) - open_risk_total),
            max(0.0, balance * float(variant["max_asset_open_risk_pct"]) - open_risk_by_asset[ac]),
            max(0.0, balance * float(variant["max_family_open_risk_pct"]) - open_risk_by_family[fc]),
            max(0.0, balance * float(variant["max_edge_open_risk_pct"]) - open_risk_by_edge[ec]),
        )
        if trailing_buffer_admission_enabled:
            effective_risk_cap = min(effective_risk_cap, max(0.0, available_new_risk))
        if daily_buffer_admission_enabled:
            effective_risk_cap = min(effective_risk_cap, max(0.0, available_daily_new_risk))
        if daily_symbol_risk_rule_enabled and symbol_internal_operating_cap_amount is not None:
            effective_risk_cap = min(
                effective_risk_cap,
                max(
                    0.0,
                    symbol_internal_operating_cap_amount
                    - daily_closed_loss_by_asset[ac]
                    - open_risk_by_asset[ac],
                ),
            )
        if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
            effective_risk_cap = min(
                effective_risk_cap,
                max(0.0, FLOATING_LOSS_GUARD_AMOUNT - open_risk_total),
            )
        if effective_risk_cap <= EPS:
            skipped_min_lot_risk_cap += 1
            skipped += 1
            continue
        if target_risk_amount > effective_risk_cap + EPS:
            target_risk_amount = effective_risk_cap
            target_pct = safe_div(target_risk_amount, balance)

        br = broker_executable_risk(
            target_risk_amount,
            float(arr["risk_per_1_lot"][i]),
            float(arr["broker_min_lot"][i]),
            float(arr["broker_lot_step"][i]),
            max_risk_amount=effective_risk_cap,
        )
        if not br["valid"]:
            skipped_invalid_broker_risk += 1
            skipped += 1
            continue
        risk_amount = float(br["actual_risk_amount"])
        actual_pct = safe_div(risk_amount, balance)
        if not np.isfinite(actual_pct) or actual_pct <= EPS:
            skipped_invalid_broker_risk += 1
            skipped += 1
            continue
        if SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS and not bool(br["cap_fit"]):
            skipped_min_lot_risk_cap += 1
            skipped += 1
            continue
        if (
            max_min_lot_risk_inflation is not None
            and bool(br["min_lot_binding"])
            and np.isfinite(float(br["risk_inflation"]))
            and float(br["risk_inflation"]) > max_min_lot_risk_inflation + EPS
        ):
            skipped_min_lot_inflation_guard += 1
            skipped += 1
            continue

        projected_open_risk = open_risk_total + risk_amount
        projected_trailing_buffer_after_trade = trailing_buffer - risk_amount
        required_reserve_after_trade = required_trailing_buffer_reserve(
            projected_open_risk=projected_open_risk,
            fixed_reserve_amount=trailing_buffer_fixed_reserve_amount,
            cost_allowance_r=trailing_buffer_cost_allowance_r,
        )
        if (
            trailing_buffer_admission_enabled
            and projected_trailing_buffer_after_trade
            < required_reserve_after_trade - EPS
        ):
            skipped_projected_trailing_buffer_reserve += 1
            skipped += 1
            continue

        projected_daily_buffer_after_trade = daily_buffer - risk_amount
        required_daily_reserve_after_trade = required_trailing_buffer_reserve(
            projected_open_risk=projected_open_risk,
            fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
            cost_allowance_r=daily_buffer_cost_allowance_r,
        )
        if daily_buffer_admission_enabled and projected_daily_buffer_after_trade < required_daily_reserve_after_trade - EPS:
            skipped_projected_daily_buffer_reserve += 1
            skipped += 1
            continue

        proposed_symbol_usage = symbol_daily_risk_usage(
            daily_closed_loss_by_asset[ac],
            open_risk_by_asset[ac] + risk_amount,
        )
        if (
            daily_symbol_risk_rule_enabled
            and symbol_internal_operating_cap_amount is not None
            and proposed_symbol_usage > symbol_internal_operating_cap_amount + EPS
        ):
            skipped_daily_symbol_risk_cap += 1
            skipped += 1
            continue
        if open_risk_total + risk_amount > balance * float(variant["max_total_open_risk_pct"]) + EPS:
            skipped_total_cap += 1; skipped_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_asset[ac] + risk_amount > balance * float(variant["max_asset_open_risk_pct"]) + EPS:
            skipped_asset_cap += 1; skipped_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_family[fc] + risk_amount > balance * float(variant["max_family_open_risk_pct"]) + EPS:
            skipped_family_cap += 1; skipped_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_edge[ec] + risk_amount > balance * float(variant["max_edge_open_risk_pct"]) + EPS:
            skipped_edge_cap += 1; skipped_overlap_risk_cap += 1; skipped += 1; continue
        if open_count_by_edge[ec] >= int(variant["max_open_trades_per_edge"]):
            skipped_edge_count += 1; skipped += 1; continue
        if open_count_by_asset[ac] >= int(variant["max_open_trades_per_asset"]):
            skipped_asset_count += 1; skipped += 1; continue

        accepted += 1
        accepted_trade_days.add(day_from_ns(current_ns))
        if raw_pct > max_single_trade_risk_pct + EPS:
            clipped_accepted += 1
        if throttle_mult < 1.0 - EPS:
            throttle_reduced_accepted += 1
        min_trailing_buffer_at_accept = min(
            min_trailing_buffer_at_accept, trailing_buffer
        )
        min_projected_trailing_buffer_after_accept = min(
            min_projected_trailing_buffer_after_accept,
            projected_trailing_buffer_after_trade,
        )
        min_required_trailing_buffer_reserve_after_accept = min(
            min_required_trailing_buffer_reserve_after_accept,
            required_reserve_after_trade,
        )
        min_projected_buffer_excess_over_required_reserve_after_accept = min(
            min_projected_buffer_excess_over_required_reserve_after_accept,
            projected_trailing_buffer_after_trade - required_reserve_after_trade,
        )

        afml_size_sum += float(arr["afml_bet_size"][i])
        raw_risk_pct_sum += raw_pct
        target_risk_pct_sum += target_pct
        actual_risk_pct_sum += actual_pct
        throttle_mult_sum += throttle_mult
        target_lock_mult_sum += target_mult
        target_risk_values.append(float(target_pct))
        actual_risk_values.append(float(actual_pct))
        actual_lot_values.append(float(br["actual_lot"]))
        risk_inflation_values.append(float(br["risk_inflation"]))
        if bool(br["min_lot_binding"]):
            min_lot_binding_count += 1
        max_raw_trade_risk_pct = max(max_raw_trade_risk_pct, raw_pct)
        max_target_trade_risk_pct = max(max_target_trade_risk_pct, target_pct)
        max_actual_trade_risk_pct = max(max_actual_trade_risk_pct, actual_pct)

        heapq.heappush(open_heap, (
            int(exit_ns[i]), i, risk_amount, float(stressed_r), ac, fc, ec, int(i),
            float(target_pct), float(actual_pct), float(br["actual_lot"]), float(br["risk_inflation"]),
        ))
        open_risk_total += risk_amount
        open_risk_by_asset[ac] += risk_amount
        open_risk_by_family[fc] += risk_amount
        open_risk_by_edge[ec] += risk_amount
        open_count_by_asset[ac] += 1
        open_count_by_edge[ec] += 1
        max_open_trades = max(max_open_trades, len(open_heap))
        max_open_risk_pct = max(max_open_risk_pct, safe_div(open_risk_total, balance))
        if capture_logs:
            trade_logs.append({
                "event_type": "entry",
                "event_time": ns_to_ts(current_ns),
                "trade_index": int(i),
                "representative_id": arr["representative_id"][i],
                "edge_id": arr["edge_id"][i],
                "asset": arr["asset"][i],
                "raw_trade_risk_pct": raw_pct,
                "target_trade_risk_pct": target_pct,
                "actual_trade_risk_pct": actual_pct,
                "actual_lot": float(br["actual_lot"]),
                "risk_inflation": float(br["risk_inflation"]),
                "risk_amount": risk_amount,
                "min_lot_binding": bool(br["min_lot_binding"]),
                "stressed_r": stressed_r,
                "balance_at_entry": balance,
                "open_risk_pct_after_entry": safe_div(open_risk_total, balance),
                "trailing_buffer_before_entry": trailing_buffer,
                "projected_trailing_buffer_after_entry": projected_trailing_buffer_after_trade,
                "required_trailing_buffer_reserve_after_entry": required_reserve_after_trade,
                "projected_buffer_excess_after_entry": (
                    projected_trailing_buffer_after_trade - required_reserve_after_trade
                ),
            })

        # Preserve the same rule-check timing as Script 10.
        check_rules(current_ns)
        if STOP_ON_RULE_BREACH and failure_reason:
            break

    if target_hit_time is None and not failure_reason:
        close_due(int(arr["final_data_end_ns"]))
    refresh_qualifying_day()
    if (
        target_hit_time is None
        and not failure_reason
        and target_satisfied(
            ACCOUNT_RULES,
            balance=balance,
            qualifying_day_count=len(qualifying_days),
        )
    ):
        target_hit_time = int(arr["final_data_end_ns"])
        target_hit_balance = balance
    if target_hit_time is None and not failure_reason:
        failure_reason = "target_not_reached_by_end_of_history"
        failure_time = int(arr["final_data_end_ns"])
        active_days.add(day_from_ns(failure_time))

    passed = target_hit_time is not None and failure_reason == ""
    max_drawdown_failed = failure_reason == "overall_drawdown_floor_breached"
    daily_drawdown_failed = failure_reason == "daily_drawdown_floor_breached"
    daily_symbol_risk_failed = bool(
        daily_symbol_risk_rule_enabled
        and failure_reason == "max_daily_symbol_risk_breached"
    )
    hard_failed = max_drawdown_failed or daily_drawdown_failed or daily_symbol_risk_failed
    end_history_fail = failure_reason == "target_not_reached_by_end_of_history"

    target_arr = np.array(target_risk_values, dtype=float)
    actual_arr = np.array(actual_risk_values, dtype=float)
    infl_arr = np.array(risk_inflation_values, dtype=float)
    lot_arr = np.array(actual_lot_values, dtype=float)
    result = {
        "scenario_name": scenario["name"],
        "validation_class": scenario.get("validation_class", "diagnostic"),
        "start_ns": int(start_ns),
        "start_time": ns_to_ts(start_ns),
        "passed": bool(passed),
        "failed": bool(not passed),
        "hard_failed": bool(hard_failed),
        "max_drawdown_failed": bool(max_drawdown_failed),
        "daily_drawdown_failed": bool(daily_drawdown_failed),
        "daily_symbol_risk_failed": bool(daily_symbol_risk_failed),
        "end_of_history_fail": bool(end_history_fail),
        "failure_reason": failure_reason,
        "target_hit_time": ns_to_ts(target_hit_time),
        "failure_time": ns_to_ts(failure_time),
        "trading_days_to_target": len([d for d in active_days if target_hit_time is not None and d <= day_from_ns(target_hit_time)]) if target_hit_time is not None else np.nan,
        "calendar_days_to_target": safe_div(target_hit_time - start_ns, DAY_NS) if target_hit_time is not None else np.nan,
        "observation_calendar_days": safe_div(arr["final_data_end_ns"] - start_ns, DAY_NS),
        "start_balance": CURRENT_BALANCE,
        "start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
        "ending_balance": balance,
        "target_hit_balance": target_hit_balance,
        "net_profit": balance - CURRENT_BALANCE,
        "min_balance": min_balance,
        "max_balance": max_balance,
        "worst_buffer_above_floor": min_buffer,
        "remaining_buffer_at_end": drawdown_stats(balance, open_risk_total, trailing_high_watermark)["remaining_dd_buffer"],
        "trailing_high_watermark": trailing_high_watermark,
        "trailing_drawdown_floor_at_end": trailing_floor,
        "trailing_floor_locked_at_initial_balance": bool(
            drawdown_stats(balance, open_risk_total, trailing_high_watermark)[
                "trailing_floor_locked_at_initial_balance"
            ]
        ),
        "max_challenge_dd_used": max_challenge_dd_used,
        "max_realized_peak_to_valley_dd": max_realized_peak_to_valley_dd,
        "daily_drawdown_enabled": bool(daily_enabled),
        "daily_drawdown_pct": float(daily_pct),
        "daily_drawdown_amount": float(daily_amount),
        "daily_drawdown_mode": daily_mode,
        "daily_worst_buffer_above_floor": daily_worst_buffer,
        "max_daily_drawdown_pct": max_daily_drawdown_pct,
        "account_rule_profile": variant.get("account_rule_profile"),
        "account_stage": variant.get("account_stage"),
        "minimum_trading_days_enabled": MINIMUM_TRADING_DAYS_ENABLED,
        "minimum_trading_days_required": MINIMUM_TRADING_DAYS_REQUIRED,
        "qualifying_trading_days": int(len(qualifying_days)),
        "minimum_trading_days_satisfied": minimum_days_satisfied(
            ACCOUNT_RULES, len(qualifying_days)
        ),
        "daily_reset_timezone": DAILY_RESET_TIMEZONE,
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        "daily_symbol_risk_rule_enabled": bool(daily_symbol_risk_rule_enabled),
        "daily_symbol_risk_enforcement_mode": daily_symbol_risk_enforcement_mode,
        "max_daily_risk_per_symbol_pct": optional_float(
            variant.get("max_daily_risk_per_symbol_pct")
        ),
        "max_daily_risk_per_symbol_amount": symbol_daily_hard_cap_amount,
        "internal_operating_risk_per_symbol_pct": optional_float(
            variant.get("internal_operating_risk_per_symbol_pct")
        ),
        "internal_operating_risk_per_symbol_amount": symbol_internal_operating_cap_amount,
        "symbol_risk_safety_reserve_amount": optional_float(
            variant.get("symbol_risk_safety_reserve_amount")
        ),
        "max_daily_symbol_risk_amount_observed": max_daily_symbol_risk_amount,
        "max_daily_symbol_risk_pct_observed": max_daily_symbol_risk_pct,
        "attempted_trades": attempted,
        "accepted_trades": accepted,
        "closed_trades": closed,
        "skipped_trades": skipped,
        "dropped_by_stress": dropped_by_stress,
        "skipped_pause": skipped_pause,
        "skipped_zero_risk": skipped_zero_risk,
        "skipped_invalid_broker_risk": skipped_invalid_broker_risk,
        "skipped_min_lot_risk_cap": skipped_min_lot_risk_cap,
        "skipped_overlap_risk_cap": skipped_overlap_risk_cap,
        "skipped_total_cap": skipped_total_cap,
        "skipped_asset_cap": skipped_asset_cap,
        "skipped_family_cap": skipped_family_cap,
        "skipped_edge_cap": skipped_edge_cap,
        "skipped_edge_count": skipped_edge_count,
        "skipped_asset_count": skipped_asset_count,
        "skipped_daily_symbol_risk_cap": skipped_daily_symbol_risk_cap,
        "skipped_internal_symbol_risk_cap": skipped_daily_symbol_risk_cap,
        "skipped_projected_trailing_buffer_reserve": skipped_projected_trailing_buffer_reserve,
        "skipped_projected_daily_buffer_reserve": skipped_projected_daily_buffer_reserve,
        "daily_buffer_admission_enabled": bool(daily_buffer_admission_enabled),
        "daily_buffer_fixed_reserve_amount": float(daily_buffer_fixed_reserve_amount),
        "daily_buffer_cost_allowance_r": float(daily_buffer_cost_allowance_r),
        "trailing_buffer_admission_enabled": bool(trailing_buffer_admission_enabled),
        "trailing_buffer_reserve_mode": trailing_buffer_reserve_mode,
        "trailing_buffer_fixed_reserve_amount": trailing_buffer_fixed_reserve_amount,
        "trailing_buffer_cost_allowance_r": trailing_buffer_cost_allowance_r,
        "trailing_buffer_target_clipped_attempted": trailing_buffer_target_clipped_attempted,
        "throttle_activation_rate_attempted": safe_div(
            throttle_reduced_attempted, attempted
        ),
        "throttle_activation_rate_accepted": safe_div(
            throttle_reduced_accepted, accepted
        ),
        "min_trailing_buffer_at_attempt": (
            float(min_trailing_buffer_at_attempt)
            if np.isfinite(min_trailing_buffer_at_attempt)
            else np.nan
        ),
        "min_trailing_buffer_at_accept": (
            float(min_trailing_buffer_at_accept)
            if np.isfinite(min_trailing_buffer_at_accept)
            else np.nan
        ),
        "min_available_new_risk_at_attempt": (
            float(min_available_new_risk_at_attempt)
            if np.isfinite(min_available_new_risk_at_attempt)
            else np.nan
        ),
        "min_projected_trailing_buffer_after_accept": (
            float(min_projected_trailing_buffer_after_accept)
            if np.isfinite(min_projected_trailing_buffer_after_accept)
            else np.nan
        ),
        "min_required_trailing_buffer_reserve_after_accept": (
            float(min_required_trailing_buffer_reserve_after_accept)
            if np.isfinite(min_required_trailing_buffer_reserve_after_accept)
            else np.nan
        ),
        "min_projected_buffer_excess_over_required_reserve_after_accept": (
            float(min_projected_buffer_excess_over_required_reserve_after_accept)
            if np.isfinite(
                min_projected_buffer_excess_over_required_reserve_after_accept
            )
            else np.nan
        ),
        "active_trading_days": len(active_days),
        "accepted_trades_per_active_day": safe_div(accepted, max(len(active_days), 1)),
        "max_open_risk_pct": max_open_risk_pct,
        "max_open_trades": max_open_trades,
        "avg_afml_bet_size_accepted": safe_div(afml_size_sum, accepted),
        "avg_raw_trade_risk_pct_before_single_cap": safe_div(raw_risk_pct_sum, accepted),
        "avg_target_trade_risk_pct": safe_div(target_risk_pct_sum, accepted),
        "avg_actual_trade_risk_pct": safe_div(actual_risk_pct_sum, accepted),
        "avg_throttle_multiplier": safe_div(throttle_mult_sum, accepted),
        "avg_target_lock_multiplier": safe_div(target_lock_mult_sum, accepted),
        "single_trade_clip_rate_accepted": safe_div(clipped_accepted, accepted),
        "single_trade_clip_rate_attempted": safe_div(clipped_attempted, attempted),
        "median_target_trade_risk_pct": float(np.nanmedian(target_arr)) if target_arr.size else np.nan,
        "median_actual_trade_risk_pct": float(np.nanmedian(actual_arr)) if actual_arr.size else np.nan,
        "p90_actual_trade_risk_pct": float(np.nanpercentile(actual_arr, 90)) if actual_arr.size else np.nan,
        "max_actual_trade_risk_pct": max_actual_trade_risk_pct,
        "median_actual_lot": float(np.nanmedian(lot_arr)) if lot_arr.size else np.nan,
        "p90_actual_lot": float(np.nanpercentile(lot_arr, 90)) if lot_arr.size else np.nan,
        "min_lot_binding_rate": safe_div(min_lot_binding_count, accepted),
        "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
        "min_lot_risk_inflation_guard_enabled": bool(
            max_min_lot_risk_inflation is not None
        ),
        "skipped_min_lot_inflation_guard": int(
            skipped_min_lot_inflation_guard
        ),
        "median_risk_inflation_accepted": float(np.nanmedian(infl_arr)) if infl_arr.size else np.nan,
        "p90_risk_inflation_accepted": float(np.nanpercentile(infl_arr, 90)) if infl_arr.size else np.nan,
        "max_risk_inflation_accepted": float(np.nanmax(infl_arr)) if infl_arr.size else np.nan,
    }
    return result, pd.DataFrame(trade_logs)


def summarize_results(df: pd.DataFrame, source: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, g in df.groupby("scenario_name", dropna=False):
        scenario = next((s for s in STRESS_SCENARIOS if s["name"] == name), {})
        passed = g["passed"].astype(bool)
        hard = g["hard_failed"].astype(bool)
        maxdd = g.get(
            "max_drawdown_failed", pd.Series(False, index=g.index)
        ).astype(bool)
        daily = g.get(
            "daily_drawdown_failed", pd.Series(False, index=g.index)
        ).astype(bool)
        symbol = g.get(
            "daily_symbol_risk_failed", pd.Series(False, index=g.index)
        ).astype(bool)
        end = g["end_of_history_fail"].astype(bool)
        days = pd.to_numeric(
            g.loc[passed, "trading_days_to_target"], errors="coerce"
        )
        buffers = pd.to_numeric(
            g["worst_buffer_above_floor"], errors="coerce"
        )

        pass_lo, pass_hi = wilson_interval(int(passed.sum()), len(g))
        hard_lo, hard_hi = wilson_interval(int(hard.sum()), len(g))
        reserve_excess = pd.to_numeric(
            g.get(
                "min_projected_buffer_excess_over_required_reserve_after_accept",
                pd.Series(np.nan, index=g.index),
            ),
            errors="coerce",
        )
        reserve_violations = int((reserve_excess < -EPS).fillna(False).sum())

        rows.append(
            {
                "scenario_name": name,
                "validation_class": scenario.get(
                    "validation_class", "diagnostic"
                ),
                "summary_source": source,
                "runs": int(len(g)),
                "pass_count": int(passed.sum()),
                "pass_rate": float(passed.mean()),
                "pass_rate_wilson95_low": pass_lo,
                "pass_rate_wilson95_high": pass_hi,
                "fail_rate": float((~passed).mean()),
                "hard_fail_count": int(hard.sum()),
                "hard_fail_rate": float(hard.mean()),
                "hard_fail_rate_wilson95_low": hard_lo,
                "hard_fail_rate_wilson95_high": hard_hi,
                "max_drawdown_fail_rate": float(maxdd.mean()),
                "daily_drawdown_fail_rate": float(daily.mean()),
                "daily_symbol_risk_fail_rate": float(symbol.mean()),
                "end_of_history_fail_rate": float(end.mean()),
                "median_trading_days_to_target": (
                    float(days.median()) if len(days.dropna()) else np.nan
                ),
                "mean_trading_days_to_target": (
                    float(days.mean()) if len(days.dropna()) else np.nan
                ),
                "p01_worst_buffer_above_floor": (
                    float(buffers.quantile(0.01))
                    if len(buffers.dropna())
                    else np.nan
                ),
                "p05_worst_buffer_above_floor": (
                    float(buffers.quantile(0.05))
                    if len(buffers.dropna())
                    else np.nan
                ),
                "worst_buffer_above_floor": (
                    float(buffers.min()) if len(buffers.dropna()) else np.nan
                ),
                "median_buffer_above_floor": (
                    float(buffers.median())
                    if len(buffers.dropna())
                    else np.nan
                ),
                "daily_worst_buffer_above_floor": (
                    float(
                        pd.to_numeric(
                            g.get("daily_worst_buffer_above_floor"),
                            errors="coerce",
                        ).min()
                    )
                    if "daily_worst_buffer_above_floor" in g
                    else np.nan
                ),
                "max_daily_drawdown_pct": (
                    float(
                        pd.to_numeric(
                            g.get("max_daily_drawdown_pct"), errors="coerce"
                        ).max()
                    )
                    if "max_daily_drawdown_pct" in g
                    else np.nan
                ),
                "max_daily_symbol_risk_amount_observed": (
                    float(
                        pd.to_numeric(
                            g.get("max_daily_symbol_risk_amount_observed"),
                            errors="coerce",
                        ).max()
                    )
                    if "max_daily_symbol_risk_amount_observed" in g
                    else np.nan
                ),
                "max_daily_symbol_risk_pct_observed": (
                    float(
                        pd.to_numeric(
                            g.get("max_daily_symbol_risk_pct_observed"),
                            errors="coerce",
                        ).max()
                    )
                    if "max_daily_symbol_risk_pct_observed" in g
                    else np.nan
                ),
                "max_open_risk_pct": (
                    float(
                        pd.to_numeric(
                            g["max_open_risk_pct"], errors="coerce"
                        ).max()
                    )
                    if "max_open_risk_pct" in g.columns
                    else np.nan
                ),
                "median_accepted_trades": (
                    float(
                        pd.to_numeric(
                            g["accepted_trades"], errors="coerce"
                        ).median()
                    )
                    if "accepted_trades" in g.columns
                    else np.nan
                ),
                "median_accepted_trades_per_active_day": (
                    float(
                        pd.to_numeric(
                            g.get("accepted_trades_per_active_day"),
                            errors="coerce",
                        ).median()
                    )
                    if "accepted_trades_per_active_day" in g
                    else np.nan
                ),
                "median_actual_trade_risk_pct": (
                    float(
                        pd.to_numeric(
                            g.get("median_actual_trade_risk_pct"),
                            errors="coerce",
                        ).median()
                    )
                    if "median_actual_trade_risk_pct" in g
                    else np.nan
                ),
                "p90_actual_trade_risk_pct": (
                    float(
                        pd.to_numeric(
                            g.get("p90_actual_trade_risk_pct"),
                            errors="coerce",
                        ).median()
                    )
                    if "p90_actual_trade_risk_pct" in g
                    else np.nan
                ),
                "max_actual_trade_risk_pct": (
                    float(
                        pd.to_numeric(
                            g.get("max_actual_trade_risk_pct"),
                            errors="coerce",
                        ).max()
                    )
                    if "max_actual_trade_risk_pct" in g
                    else np.nan
                ),
                "median_min_lot_binding_rate": (
                    float(
                        pd.to_numeric(
                            g.get("min_lot_binding_rate"), errors="coerce"
                        ).median()
                    )
                    if "min_lot_binding_rate" in g
                    else np.nan
                ),
                "median_risk_inflation_accepted": (
                    float(
                        pd.to_numeric(
                            g.get("median_risk_inflation_accepted"),
                            errors="coerce",
                        ).median()
                    )
                    if "median_risk_inflation_accepted" in g
                    else np.nan
                ),
                "p90_risk_inflation_accepted": (
                    float(
                        pd.to_numeric(
                            g.get("p90_risk_inflation_accepted"),
                            errors="coerce",
                        ).median()
                    )
                    if "p90_risk_inflation_accepted" in g
                    else np.nan
                ),
                "max_risk_inflation_accepted": (
                    float(
                        pd.to_numeric(
                            g.get("max_risk_inflation_accepted"),
                            errors="coerce",
                        ).max()
                    )
                    if "max_risk_inflation_accepted" in g
                    else np.nan
                ),
                "median_skipped_min_lot_risk_cap": (
                    float(
                        pd.to_numeric(
                            g.get("skipped_min_lot_risk_cap"),
                            errors="coerce",
                        ).median()
                    )
                    if "skipped_min_lot_risk_cap" in g
                    else np.nan
                ),
                "median_skipped_invalid_broker_risk": (
                    float(
                        pd.to_numeric(
                            g.get("skipped_invalid_broker_risk"),
                            errors="coerce",
                        ).median()
                    )
                    if "skipped_invalid_broker_risk" in g
                    else np.nan
                ),
                "median_skipped_overlap_risk_cap": (
                    float(
                        pd.to_numeric(
                            g.get("skipped_overlap_risk_cap"),
                            errors="coerce",
                        ).median()
                    )
                    if "skipped_overlap_risk_cap" in g
                    else np.nan
                ),
                "median_skipped_projected_trailing_buffer_reserve": (
                    float(
                        pd.to_numeric(
                            g.get(
                                "skipped_projected_trailing_buffer_reserve"
                            ),
                            errors="coerce",
                        ).median()
                    )
                    if "skipped_projected_trailing_buffer_reserve" in g
                    else np.nan
                ),
                "min_projected_trailing_buffer_after_accept": (
                    float(
                        pd.to_numeric(
                            g.get(
                                "min_projected_trailing_buffer_after_accept"
                            ),
                            errors="coerce",
                        ).min()
                    )
                    if "min_projected_trailing_buffer_after_accept" in g
                    else np.nan
                ),
                "min_projected_buffer_excess_over_required_reserve_after_accept": (
                    float(reserve_excess.min())
                    if len(reserve_excess.dropna())
                    else np.nan
                ),
                "projected_buffer_reserve_violation_count": reserve_violations,
            }
        )
    return pd.DataFrame(rows)


def validate_gates(
    exact_summary: pd.DataFrame,
    control_reproduction: pd.DataFrame,
    *,
    accept_moderate_hard_fail: bool = False,
) -> tuple[pd.DataFrame, str, dict[str, Any]]:
    """Apply generic blocking safety gates to the active account contract.

    accept_moderate_hard_fail mirrors 09_fast_manual_variant_lab.py's
    ACCEPT_MODERATE_HARD_FAIL / 10_final_candidate_walkthrough.py's
    --accept-moderate-hard-fail: an explicit, logged human override accepting
    a mandatory-class scenario whose hard-fail rate is capped at
    MODERATE_HARD_FAIL_TOLERANCE (matching Steps 09/10's own tier_4
    definition), as a deliberate speed/tail-risk trade-off rather than the
    default literal-zero requirement. Reserve-contract violations are never
    overridden by this flag.
    """
    rows: list[dict[str, Any]] = []
    any_manual_override_applied = False

    control_mismatch_count = int(
        pd.to_numeric(
            control_reproduction["mismatch_count"], errors="coerce"
        ).fillna(0).sum()
    )
    control_reproduction_ok = bool(
        control_mismatch_count == 0
        and control_reproduction["control_reproduction_passed"].astype(bool).all()
    )

    any_mandatory_failure = False
    any_reserve_violation = False
    any_diagnostic_warning = False

    for row in exact_summary.itertuples(index=False):
        validation_class = str(getattr(row, "validation_class", "diagnostic"))
        hard_fail_rate = float(getattr(row, "hard_fail_rate"))
        worst_buffer = float(getattr(row, "worst_buffer_above_floor"))
        p01_buffer = float(getattr(row, "p01_worst_buffer_above_floor"))
        reserve_violations = int(
            getattr(row, "projected_buffer_reserve_violation_count", 0)
        )
        max_symbol_risk = float(
            getattr(row, "max_daily_symbol_risk_amount_observed", np.nan)
        )

        reserve_ok = reserve_violations == 0
        any_reserve_violation |= not reserve_ok

        if validation_class == "mandatory":
            strict_gate_passed = bool(
                hard_fail_rate <= MANDATORY_MAX_HARD_FAIL_RATE + EPS
                and worst_buffer >= MANDATORY_MIN_WORST_BUFFER - EPS
                and p01_buffer >= MANDATORY_MIN_P01_BUFFER - EPS
                and reserve_ok
            )
            manual_override_applied = False
            gate_passed = strict_gate_passed
            if (
                not strict_gate_passed
                and accept_moderate_hard_fail
                and reserve_ok
                and hard_fail_rate <= MODERATE_HARD_FAIL_TOLERANCE + EPS
            ):
                gate_passed = True
                manual_override_applied = True
                any_manual_override_applied = True
            gate_reason = (
                "mandatory_safety_gate_manual_override_moderate_hard_fail"
                if manual_override_applied
                else "mandatory_safety_gate_passed"
                if gate_passed
                else "mandatory_safety_gate_failed"
            )
            any_mandatory_failure |= not gate_passed
            diagnostic_warning = False
        else:
            manual_override_applied = False
            gate_passed = bool(reserve_ok)
            diagnostic_warning = bool(
                hard_fail_rate > DIAGNOSTIC_MAX_HARD_FAIL_RATE_WARNING + EPS
            )
            gate_reason = (
                "diagnostic_high_hard_fail_rate_review_required"
                if diagnostic_warning
                else (
                    "diagnostic_reserve_contract_violation"
                    if not gate_passed
                    else "diagnostic_recorded"
                )
            )
            any_diagnostic_warning |= diagnostic_warning

        rows.append(
            {
                "scenario_name": getattr(row, "scenario_name"),
                "validation_class": validation_class,
                "gate_passed": gate_passed,
                "gate_reason": gate_reason,
                "manual_override_applied": manual_override_applied,
                "diagnostic_warning": diagnostic_warning,
                "hard_fail_rate": hard_fail_rate,
                "hard_fail_rate_wilson95_high": float(
                    getattr(row, "hard_fail_rate_wilson95_high", np.nan)
                ),
                "pass_rate": float(getattr(row, "pass_rate")),
                "worst_buffer_above_floor": worst_buffer,
                "p01_worst_buffer_above_floor": p01_buffer,
                "projected_buffer_reserve_violation_count": reserve_violations,
                "max_daily_symbol_risk_amount_observed": max_symbol_risk,
                "active_daily_symbol_risk_enabled": bool(
                    ACCOUNT_RULES["daily_symbol_risk"]["enabled"]
                ),
                "active_daily_symbol_risk_limit_amount": ACCOUNT_RULES[
                    "daily_symbol_risk"
                ].get("max_amount"),
            }
        )

    if not control_reproduction_ok or any_mandatory_failure or any_reserve_violation:
        status = "STRESS_FAIL"
    elif any_diagnostic_warning:
        status = "STRESS_REVIEW_REQUIRED"
    else:
        status = "STRESS_PASS"

    integrity = {
        "artifact_policy": "hashless_structural_account_rules",
        "control_reproduction_passed": control_reproduction_ok,
        "control_reproduction_mismatch_count": control_mismatch_count,
        "mandatory_gate_failure_present": any_mandatory_failure,
        "manual_override_applied": any_manual_override_applied,
        "projected_buffer_reserve_violation_present": any_reserve_violation,
        "diagnostic_high_hard_fail_warning_present": any_diagnostic_warning,
        "active_account_rules_match": True,
    }
    return pd.DataFrame(rows), status, integrity


def historical_bootstrap(
    walkthrough: pd.DataFrame,
    rng: np.random.Generator,
    samples: int,
) -> pd.DataFrame:
    """Cluster-bootstrap Script 10 rolling starts by start month.

    Rolling starts overlap heavily, so iid row bootstrapping would materially
    overstate precision. Sampling whole start-month clusters preserves more of
    the dependence between neighbouring paths. These estimates apply only to
    the three historical cost controls; diagnostic trade-level shocks are
    evaluated by exact path Monte Carlo below.
    """
    required = {
        "start_ns",
        "start_time",
        "cost_stress_r",
        "passed",
        "hard_failed",
        "end_of_history_fail",
        "trading_days_to_target",
        "worst_buffer_above_floor",
    }
    missing = sorted(required - set(walkthrough.columns))
    if missing:
        raise ValueError(
            f"Walkthrough results missing bootstrap columns: {missing}"
        )

    frame = walkthrough.copy()
    frame["start_ns"] = pd.to_numeric(
        frame["start_ns"], errors="raise"
    ).astype(np.int64)
    frame["start_time"] = to_datetime_iso(
        frame["start_time"], utc=True, errors="raise"
    )
    frame["start_cluster"] = frame["start_time"].dt.strftime("%Y-%m")

    rows: list[dict[str, Any]] = []
    control_costs = sorted(set(CONTROL_SCENARIO_COSTS.values()))
    for cost in control_costs:
        eligible = frame[
            np.isclose(
                pd.to_numeric(
                    frame["cost_stress_r"], errors="coerce"
                ).astype(float),
                float(cost),
                rtol=0.0,
                atol=EPS,
            )
        ].copy()
        if eligible.empty:
            raise RuntimeError(
                f"No Script 10 walkthrough rows found for control cost {cost}."
            )

        groups = {
            str(cluster): g.copy()
            for cluster, g in eligible.groupby("start_cluster", sort=True)
        }
        cluster_names = np.array(sorted(groups), dtype=object)
        if cluster_names.size == 0:
            raise RuntimeError(
                f"No start clusters available for control cost {cost}."
            )

        for sample_id in range(int(samples)):
            chosen = rng.choice(
                cluster_names,
                size=len(cluster_names),
                replace=True,
            )
            sampled = pd.concat(
                [groups[str(name)] for name in chosen],
                ignore_index=True,
            )
            passed = sampled["passed"].astype(bool)
            hard = sampled["hard_failed"].astype(bool)
            end = sampled["end_of_history_fail"].astype(bool)
            days = pd.to_numeric(
                sampled.loc[passed, "trading_days_to_target"],
                errors="coerce",
            ).dropna()
            buffers = pd.to_numeric(
                sampled["worst_buffer_above_floor"], errors="coerce"
            ).dropna()

            rows.append(
                {
                    "scenario_name": f"historical_cost{int(round(cost * 100)):02d}_cluster_bootstrap",
                    "cost_stress_r": float(cost),
                    "sample_id": int(sample_id),
                    "clusters_sampled": int(len(cluster_names)),
                    "rows_sampled": int(len(sampled)),
                    "pass_rate": float(passed.mean()),
                    "hard_fail_rate": float(hard.mean()),
                    "end_of_history_fail_rate": float(end.mean()),
                    "median_trading_days_to_target": (
                        float(days.median()) if len(days) else np.nan
                    ),
                    "worst_buffer_above_floor": (
                        float(buffers.min()) if len(buffers) else np.nan
                    ),
                    "p01_worst_buffer_above_floor": (
                        float(buffers.quantile(0.01))
                        if len(buffers)
                        else np.nan
                    ),
                }
            )

    return pd.DataFrame(rows)


def summarize_bootstrap_estimates(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for name, g in df.groupby("scenario_name", sort=True):
        row: dict[str, Any] = {
            "scenario_name": name,
            "cost_stress_r": float(g["cost_stress_r"].iloc[0]),
            "bootstrap_samples": int(len(g)),
            "bootstrap_method": "start_month_cluster_bootstrap",
        }
        for col in [
            "pass_rate",
            "hard_fail_rate",
            "end_of_history_fail_rate",
            "median_trading_days_to_target",
            "worst_buffer_above_floor",
            "p01_worst_buffer_above_floor",
        ]:
            values = pd.to_numeric(g[col], errors="coerce").dropna()
            row[f"{col}_median"] = (
                float(values.median()) if len(values) else np.nan
            )
            row[f"{col}_p025"] = (
                float(values.quantile(0.025)) if len(values) else np.nan
            )
            row[f"{col}_p975"] = (
                float(values.quantile(0.975)) if len(values) else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows)


_POOL_ARR: dict[str, Any] | None = None
_POOL_BASE_RAW_PCT: np.ndarray | None = None
_POOL_VARIANT: dict[str, Any] | None = None
_POOL_DIMS: tuple[int, int, int] | None = None


def _pool_init(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    n_assets: int,
    n_families: int,
    n_edges: int,
) -> None:
    """ProcessPoolExecutor initializer: stashes the (large, read-only)
    ledger-derived arrays and the fixed selected variant in each worker
    process ONCE, so per-task payloads only need to carry the small
    (scenario, start_ns, run_seed) tuple -- not the full array dict per
    task. No RNG state is shared here: every task carries its own
    already-drawn `run_seed`, minted sequentially on the main process
    before any task is dispatched (see exact_monte_carlo)."""
    global _POOL_ARR, _POOL_BASE_RAW_PCT, _POOL_VARIANT, _POOL_DIMS
    _POOL_ARR = arr
    _POOL_BASE_RAW_PCT = base_raw_pct
    _POOL_VARIANT = variant
    _POOL_DIMS = (n_assets, n_families, n_edges)


def _pool_run_chunk(
    task_slice: list[tuple[int, dict[str, Any], int, int, int, bool]],
) -> list[tuple[int, dict[str, Any]]]:
    """Worker task: run simulate_path() for one slice of already-seeded
    Monte Carlo tasks. Each task supplies its own `run_seed`, drawn
    sequentially on the main process in exact scenario/run_index order
    before dispatch -- the worker only does
    ``run_rng = np.random.default_rng(run_seed)`` and calls
    simulate_path(...), exactly what the serial code does per-call. This
    is a pure function of its arguments plus the process-local arrays
    stashed by _pool_init, so results are bit-identical regardless of
    chunking or completion order."""
    assert (
        _POOL_ARR is not None
        and _POOL_BASE_RAW_PCT is not None
        and _POOL_VARIANT is not None
        and _POOL_DIMS is not None
    )
    n_assets, n_families, n_edges = _POOL_DIMS
    out: list[tuple[int, dict[str, Any]]] = []
    for task_index, scenario, start_ns_value, run_seed, run_index, is_control in task_slice:
        run_rng = np.random.default_rng(run_seed)
        res, _ = simulate_path(
            _POOL_ARR,
            _POOL_BASE_RAW_PCT,
            _POOL_VARIANT,
            int(start_ns_value),
            scenario,
            run_rng,
            n_assets,
            n_families,
            n_edges,
            capture_logs=False,
        )
        res["run_index"] = int(run_index)
        res["run_seed"] = int(run_seed)
        res["control_replay"] = bool(is_control)
        out.append((task_index, res))
    return out


def run_mc_tasks(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    tasks: list[tuple[int, dict[str, Any], int, int, int, bool]],
    n_assets: int,
    n_families: int,
    n_edges: int,
    desc: str,
    *,
    n_workers: int = 1,
    chunk_size: int = 25,
) -> list[dict[str, Any]]:
    """Runs simulate_path() for every already-seeded Monte Carlo task.

    Every task already carries its own `run_seed`, drawn strictly
    sequentially on the main process (see exact_monte_carlo) before this
    function is ever called -- no worker process draws from a shared RNG.
    n_workers <= 1 runs the original single-process loop unchanged (used
    for small jobs and for cross-checking parallel output against serial
    ground truth). n_workers > 1 fans the work out across a
    ProcessPoolExecutor, chunked at a fine grain over the flattened task
    list -- not one task per scenario -- so speedup scales with n_workers
    regardless of how many stress scenarios exist. Results are always
    returned in the exact original task order (rebuilt via `task_index`
    after collection), since ProcessPoolExecutor.as_completed() does not
    preserve submission order.
    """
    total = len(tasks)

    if n_workers <= 1:
        rows_by_index: dict[int, dict[str, Any]] = {}
        with tqdm(total=total, desc=desc, unit="run") as pbar:
            for task_index, scenario, start_ns_value, run_seed, run_index, is_control in tasks:
                run_rng = np.random.default_rng(run_seed)
                res, _ = simulate_path(
                    arr,
                    base_raw_pct,
                    variant,
                    int(start_ns_value),
                    scenario,
                    run_rng,
                    n_assets,
                    n_families,
                    n_edges,
                    capture_logs=False,
                )
                res["run_index"] = int(run_index)
                res["run_seed"] = int(run_seed)
                res["control_replay"] = bool(is_control)
                rows_by_index[task_index] = res
                pbar.update(1)
        return [rows_by_index[i] for i in range(total)]

    chunks = [tasks[i:i + chunk_size] for i in range(0, total, chunk_size)]
    rows_by_index: dict[int, dict[str, Any]] = {}
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_pool_init,
        initargs=(arr, base_raw_pct, variant, n_assets, n_families, n_edges),
    ) as executor:
        futures = [executor.submit(_pool_run_chunk, chunk) for chunk in chunks]
        with tqdm(total=total, desc=f"{desc} [{n_workers} workers]", unit="run") as pbar:
            for future in as_completed(futures):
                chunk_out = future.result()
                for task_index, res in chunk_out:
                    rows_by_index[task_index] = res
                pbar.update(len(chunk_out))
    return [rows_by_index[i] for i in range(total)]


def exact_monte_carlo(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    starts: list[int],
    n_assets: int,
    n_families: int,
    n_edges: int,
    rng: np.random.Generator,
    write: bool,
    runs_per_scenario: int,
    dry_run: bool,
    *,
    n_workers: int = 1,
    chunk_size: int = 25,
) -> pd.DataFrame:
    """Run deterministic controls plus stochastic path-level stress scenarios.

    Control scenarios replay every unique Script 10 start exactly once so they
    can be compared path-for-path. Diagnostic scenarios sample starts with
    replacement and apply independent run seeds for execution/edge shocks.

    CRITICAL RNG CONTRACT: `rng` (the master, caller-owned generator) is used
    ONLY on this main process, and ONLY to (a) sample starts for non-control
    scenarios and (b) mint one `run_seed` per (scenario, run_index) pair, in
    exactly the order below. That sequential draw order is what makes results
    bit-reproducible and independent of task-completion order. Nothing below
    this point (the actual simulate_path() executions, optionally fanned out
    to worker processes via run_mc_tasks) ever touches `rng` -- each task
    receives its own already-drawn `run_seed` and builds its own independent
    `run_rng = np.random.default_rng(run_seed)`, identical to what the serial
    code has always done per-call.
    """
    if not starts:
        raise RuntimeError("No starts available for exact Monte Carlo.")

    start_array = np.array(sorted(set(int(x) for x in starts)), dtype=np.int64)
    if dry_run:
        start_array = start_array[: min(DRY_RUNS_PER_SCENARIO, len(start_array))]

    scenario_by_name = {str(s["name"]): s for s in STRESS_SCENARIOS}

    tasks: list[tuple[int, dict[str, Any], int, int, int, bool]] = []
    for scenario in tqdm(
        STRESS_SCENARIOS,
        desc="V2 exact no-deadline Monte Carlo stress (seeding)",
        unit="scenario",
    ):
        name = str(scenario["name"])
        is_control = name in CONTROL_SCENARIO_COSTS

        if is_control:
            sampled_starts = start_array.copy()
        else:
            count = (
                min(DRY_RUNS_PER_SCENARIO, len(start_array))
                if dry_run
                else int(runs_per_scenario)
            )
            sampled_starts = rng.choice(
                start_array,
                size=count,
                replace=True,
            ).astype(np.int64)

        for run_index, start_ns_value in enumerate(sampled_starts):
            run_seed = int(rng.integers(0, 2**31 - 1))
            tasks.append(
                (
                    len(tasks),
                    scenario,
                    int(start_ns_value),
                    run_seed,
                    int(run_index),
                    bool(is_control),
                )
            )

    rows = run_mc_tasks(
        arr,
        base_raw_pct,
        variant,
        tasks,
        n_assets,
        n_families,
        n_edges,
        "V2 exact no-deadline Monte Carlo stress",
        n_workers=n_workers,
        chunk_size=chunk_size,
    )

    exact = pd.DataFrame(rows)

    if write and SAVE_WORST_RUN_TRADE_LOGS and not exact.empty:
        for scenario_name, g in exact.groupby("scenario_name", sort=True):
            scenario = scenario_by_name[str(scenario_name)]
            worst = g.nsmallest(
                max(1, int(WORST_RUNS_TO_REPLAY_PER_SCENARIO)),
                "worst_buffer_above_floor",
            )
            for local_rank, row in enumerate(
                worst.itertuples(index=False), start=1
            ):
                start_ns_value = int(row.start_ns)
                run_seed = int(row.run_seed)
                replay_rng = np.random.default_rng(run_seed)
                replay_result, logs = simulate_path(
                    arr,
                    base_raw_pct,
                    variant,
                    start_ns_value,
                    scenario,
                    replay_rng,
                    n_assets,
                    n_families,
                    n_edges,
                    capture_logs=True,
                )
                if not np.isclose(
                    float(replay_result["worst_buffer_above_floor"]),
                    float(row.worst_buffer_above_floor),
                    rtol=0.0,
                    atol=1e-8,
                ):
                    raise RuntimeError(
                        "Worst-run replay was not reproducible for "
                        f"{scenario_name}, start={start_ns_value}, seed={run_seed}."
                    )
                logs.insert(0, "run_seed", run_seed)
                logs.insert(0, "scenario_name", str(scenario_name))
                logs.to_csv(
                    DETAIL_DIR
                    / (
                        f"worst_run_{slug(scenario_name)}_"
                        f"{local_rank:02d}_{start_ns_value}_seed{run_seed}.csv"
                    ),
                    index=False,
                )

    return exact



def generate_charts(
    exact: pd.DataFrame,
    walkthrough: pd.DataFrame,
    exact_summary: pd.DataFrame,
    output_dir: Path,
) -> list[str]:
    """Saves PNG charts summarizing this Monte Carlo validation run directly
    into output_dir/charts/, as an ordinary pipeline artifact -- so anyone
    can pull them from the outputs folder after a run rather than needing
    them generated ad hoc outside the pipeline. The caller wraps this in
    try/except: a charting failure must never fail the actual validation
    gate, since these are visualizations of already-computed results, not
    part of the pass/fail decision itself."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    # 1. Days-to-target distribution (Step 10 exact replay, passing runs only)
    passed_days = pd.to_numeric(
        walkthrough.loc[walkthrough["passed"].astype(bool), "trading_days_to_target"],
        errors="coerce",
    ).dropna()
    if len(passed_days):
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(passed_days, bins=30, color="#2b6cb0", edgecolor="white")
        for q, style in [(0.5, "-"), (0.95, "--"), (0.99, ":")]:
            v = float(passed_days.quantile(q))
            ax.axvline(v, color="#c53030", linestyle=style, linewidth=1.5, label=f"p{int(q * 100)}={v:.0f}d")
        ax.set_xlabel("Trading days to target")
        ax.set_ylabel("Count of passing runs")
        ax.set_title("Days-to-target distribution (Step 10 exact replay)")
        ax.legend()
        fig.tight_layout()
        path = charts_dir / "days_to_target_histogram.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(str(path))

    # 2. Pass / hard-fail rate by cost-stress tier, with Wilson 95% CI whiskers
    if not exact_summary.empty and {"pass_rate", "hard_fail_rate"}.issubset(exact_summary.columns):
        summ = exact_summary.sort_values("scenario_name").reset_index(drop=True)
        fig, ax = plt.subplots(figsize=(8, 5))
        x = np.arange(len(summ))
        width = 0.35
        ax.bar(x - width / 2, summ["pass_rate"], width, label="Pass rate", color="#2f855a")
        hard_err = None
        if {"hard_fail_rate_wilson95_low", "hard_fail_rate_wilson95_high"}.issubset(summ.columns):
            lo = (summ["hard_fail_rate"] - summ["hard_fail_rate_wilson95_low"]).clip(lower=0).to_numpy()
            hi = (summ["hard_fail_rate_wilson95_high"] - summ["hard_fail_rate"]).clip(lower=0).to_numpy()
            hard_err = np.vstack([lo, hi])
        ax.bar(
            x + width / 2, summ["hard_fail_rate"], width, label="Hard-fail rate", color="#c53030",
            yerr=hard_err, capsize=4,
        )
        ax.set_xticks(x)
        ax.set_xticklabels(summ["scenario_name"], rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("Rate")
        ax.set_title("Pass / hard-fail rate by Monte Carlo scenario (error bars = Wilson 95% CI)")
        ax.legend()
        fig.tight_layout()
        path = charts_dir / "pass_hardfail_by_scenario.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(str(path))

    # 3. Drawdown-buffer distribution across all exact Monte Carlo runs
    if "worst_buffer_above_floor" in exact.columns and not exact.empty:
        vals = pd.to_numeric(exact["worst_buffer_above_floor"], errors="coerce").dropna()
        if len(vals):
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.hist(vals, bins=40, color="#805ad5", edgecolor="white")
            ax.axvline(0.0, color="#c53030", linewidth=2, label="Drawdown floor (breach if below this line)")
            ax.set_xlabel("Worst buffer above drawdown floor ($)")
            ax.set_ylabel("Count of Monte Carlo runs")
            ax.set_title("Drawdown-buffer distribution across exact Monte Carlo runs")
            ax.legend()
            fig.tight_layout()
            path = charts_dir / "drawdown_buffer_distribution.png"
            fig.savefig(path, dpi=150)
            plt.close(fig)
            saved.append(str(path))

    # 4. Sample equity curves from the saved worst-run trade logs
    detail_files = sorted(DETAIL_DIR.glob("worst_run_*.csv"))[:6] if DETAIL_DIR.exists() else []
    if detail_files:
        fig, ax = plt.subplots(figsize=(9, 5))
        for f in detail_files:
            try:
                log = pd.read_csv(f)
                if not {"balance_before_batch_close", "pnl"}.issubset(log.columns):
                    continue
                balance_after = log["balance_before_batch_close"] + log["pnl"]
                ax.plot(
                    range(len(balance_after)), balance_after.to_numpy(),
                    alpha=0.8, linewidth=1.2, label=f.stem.replace("worst_run_", "")[:28],
                )
            except Exception:
                continue
        ax.set_xlabel("Closed trade index")
        ax.set_ylabel("Account balance ($)")
        ax.set_title("Sample equity curves -- worst Monte Carlo runs per scenario")
        ax.legend(fontsize=7, loc="best")
        fig.tight_layout()
        path = charts_dir / "worst_run_equity_curves.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        saved.append(str(path))

    return saved


def validate_control_reproduction(
    exact: pd.DataFrame,
    walkthrough: pd.DataFrame,
) -> pd.DataFrame:
    """Compare deterministic Script 11 controls with Script 10 path-for-path."""
    required_walkthrough = {
        "start_ns",
        "cost_stress_r",
        "passed",
        "hard_failed",
        "daily_symbol_risk_failed",
        "end_of_history_fail",
        "failure_reason",
        "accepted_trades",
        "closed_trades",
        "skipped_daily_symbol_risk_cap",
        "ending_balance",
        "worst_buffer_above_floor",
        "trading_days_to_target",
        "max_daily_symbol_risk_amount_observed",
        "min_projected_buffer_excess_over_required_reserve_after_accept",
    }
    missing = sorted(required_walkthrough - set(walkthrough.columns))
    if missing:
        raise ValueError(
            f"Walkthrough results missing reproduction columns: {missing}"
        )

    walk = walkthrough.copy()
    walk["start_ns"] = pd.to_numeric(walk["start_ns"], errors="raise").astype(np.int64)
    walk["cost_stress_r"] = pd.to_numeric(
        walk["cost_stress_r"], errors="raise"
    ).astype(float)

    rows: list[dict[str, Any]] = []
    for scenario_name, cost in CONTROL_SCENARIO_COSTS.items():
        ex = exact[exact["scenario_name"].astype(str) == scenario_name].copy()
        ww = walk[
            np.isclose(
                walk["cost_stress_r"], float(cost), rtol=0.0, atol=EPS
            )
        ].copy()

        ex["start_ns"] = pd.to_numeric(ex["start_ns"], errors="raise").astype(np.int64)
        if ex["start_ns"].duplicated().any():
            raise RuntimeError(
                f"Control scenario {scenario_name} contains duplicate starts."
            )

        ww = ww[ww["start_ns"].isin(ex["start_ns"])].copy()
        merged = ex.merge(
            ww,
            on="start_ns",
            how="outer",
            suffixes=("_mc", "_walk"),
            indicator=True,
        )

        mismatch = merged["_merge"] != "both"
        for col in [
            "passed",
            "hard_failed",
            "daily_symbol_risk_failed",
            "end_of_history_fail",
        ]:
            mismatch |= (
                merged[f"{col}_mc"].fillna(False).astype(bool)
                != merged[f"{col}_walk"].fillna(False).astype(bool)
            )

        mismatch |= (
            merged["failure_reason_mc"].fillna("").astype(str)
            != merged["failure_reason_walk"].fillna("").astype(str)
        )

        for col in [
            "accepted_trades",
            "closed_trades",
            "skipped_daily_symbol_risk_cap",
        ]:
            mismatch |= (
                pd.to_numeric(merged[f"{col}_mc"], errors="coerce")
                .fillna(-1)
                .astype(np.int64)
                != pd.to_numeric(merged[f"{col}_walk"], errors="coerce")
                .fillna(-1)
                .astype(np.int64)
            )

        for col in [
            "ending_balance",
            "worst_buffer_above_floor",
            "trading_days_to_target",
            "max_daily_symbol_risk_amount_observed",
            "min_projected_buffer_excess_over_required_reserve_after_accept",
        ]:
            a = pd.to_numeric(merged[f"{col}_mc"], errors="coerce").to_numpy(dtype=float)
            b = pd.to_numeric(merged[f"{col}_walk"], errors="coerce").to_numpy(dtype=float)
            equal = np.isclose(a, b, rtol=0.0, atol=1e-8, equal_nan=True)
            mismatch |= ~equal

        rows.append(
            {
                "scenario_name": scenario_name,
                "cost_stress_r": float(cost),
                "control_rows": int(len(ex)),
                "walkthrough_rows_matched": int(len(ww)),
                "mismatch_count": int(mismatch.sum()),
                "control_reproduction_passed": bool(
                    int(mismatch.sum()) == 0 and len(ex) == len(ww)
                ),
            }
        )

    return pd.DataFrame(rows)



def build(
    write: bool = True,
    dry_run: bool = False,
    runs_per_scenario: int = EXACT_MC_RUNS_PER_SCENARIO,
    bootstrap_samples: int = HISTORICAL_BOOTSTRAP_SAMPLES,
    n_workers: int = 1,
    accept_moderate_hard_fail: bool = False,
) -> dict[str, Any]:
    if write:
        ensure_dirs()
    validate_account_start_state()

    rng = np.random.default_rng(RANDOM_SEED)
    source, variant, label, walkthrough_config, walkthrough_manifest = (
        load_walkthrough_config(accept_moderate_hard_fail=accept_moderate_hard_fail)
    )
    walkthrough_integrity = walkthrough_config.get("walkthrough_integrity", {}) or {}

    if not INPUT_WALKTHROUGH_RESULTS.exists():
        raise FileNotFoundError(
            f"Missing walkthrough results: {INPUT_WALKTHROUGH_RESULTS}. Run Step 10 first."
        )

    walkthrough = pd.read_csv(INPUT_WALKTHROUGH_RESULTS)
    required_walkthrough_columns = {
        "start_ns",
        "start_time",
        "cost_stress_r",
        "passed",
        "hard_failed",
        "end_of_history_fail",
        "account_rule_profile",
        "account_stage",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
    }
    missing_walkthrough = sorted(required_walkthrough_columns - set(walkthrough.columns))
    if missing_walkthrough:
        raise ValueError(
            f"Walkthrough results missing required columns: {missing_walkthrough}"
        )

    walkthrough["start_ns"] = pd.to_numeric(
        walkthrough["start_ns"], errors="raise"
    ).astype(np.int64)
    walkthrough["cost_stress_r"] = pd.to_numeric(
        walkthrough["cost_stress_r"], errors="raise"
    ).astype(float)

    for col, expected in [
        ("account_rule_profile", ACCOUNT_RULE_PROFILE),
        ("account_stage", ACCOUNT_STAGE),
        (
            "daily_symbol_risk_enforcement_mode",
            EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE,
        ),
    ]:
        if not (walkthrough[col].astype(str) == str(expected)).all():
            raise ValueError(f"Walkthrough row contract mismatch for {col}.")
    expected_symbol_enabled = bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"])
    if not (
        walkthrough["daily_symbol_risk_rule_enabled"]
        .map(parse_bool)
        .eq(expected_symbol_enabled)
        .all()
    ):
        raise ValueError(
            "Walkthrough rows daily-symbol-risk enabled state differs from active rules."
        )

    ledger, asset_to_code, family_to_code, edge_to_code = load_ledger()
    weights = load_hrp_weights()
    arr = prepare_arrays(source, ledger, weights)
    base_raw_pct = arr["hrp_weight"] * arr["afml_bet_size"]

    starts = sorted(int(x) for x in walkthrough["start_ns"].drop_duplicates().tolist())
    if not starts:
        raise RuntimeError("No exact Script 10 starts were available.")

    effective_bootstrap_samples = (
        min(50, int(bootstrap_samples)) if dry_run else int(bootstrap_samples)
    )
    hist_boot = historical_bootstrap(
        walkthrough,
        rng,
        samples=effective_bootstrap_samples,
    )
    hist_summary = summarize_bootstrap_estimates(hist_boot)

    exact = exact_monte_carlo(
        arr,
        base_raw_pct,
        variant,
        starts,
        len(asset_to_code),
        len(family_to_code),
        len(edge_to_code),
        rng,
        write=write,
        runs_per_scenario=int(runs_per_scenario),
        dry_run=dry_run,
        n_workers=n_workers,
    )
    exact_summary = summarize_results(exact, "exact_path_stress_mc")
    control_reproduction = validate_control_reproduction(exact, walkthrough)
    gates, status, stress_integrity = validate_gates(
        exact_summary, control_reproduction, accept_moderate_hard_fail=accept_moderate_hard_fail
    )

    if status == "STRESS_PASS":
        live_approval_status = (
            "APPROVED_FOR_ACTIVE_ACCOUNT_RULES_LIVE_MANIFEST_EXPORT"
        )
        gate_meaning = (
            "All mandatory integrity and safety gates passed for the current "
            "active_account_rules.json contract."
        )
    elif status == "STRESS_REVIEW_REQUIRED":
        live_approval_status = "STRESS_REVIEW_REQUIRED_DO_NOT_AUTO_ARM"
        gate_meaning = (
            "Mandatory gates passed, but one or more diagnostic scenarios "
            "exceeded the configured hard-fail warning threshold."
        )
    else:
        live_approval_status = "STRESS_FAILED_DO_NOT_PROCEED"
        gate_meaning = (
            "A mandatory integrity, safety, or projected-reserve gate failed."
        )

    reserve_violation_count = int(
        pd.to_numeric(
            exact.get(
                "min_projected_buffer_excess_over_required_reserve_after_accept",
                pd.Series(np.nan, index=exact.index),
            ),
            errors="coerce",
        )
        .lt(-EPS)
        .fillna(False)
        .sum()
    )
    total_hard_fail_count = int(exact["hard_failed"].astype(bool).sum())

    approval_scope = f"active_account_rules:{ACCOUNT_RULE_PROFILE}:{ACCOUNT_STAGE}"
    active_rule_summary = {
        "profile_label": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "starting_capital": PROP_START_BALANCE,
        "current_balance": CURRENT_BALANCE,
        "current_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "account_start_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "account_start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
        "account_start_profit_to_target": ACCOUNT_START_PROFIT_TO_TARGET,
        "account_start_daily_loss_floor": ACCOUNT_START_DAILY_LOSS_FLOOR,
        "assume_fresh_daily_reset_at_simulation_start": ASSUME_FRESH_DAILY_RESET_AT_SIMULATION_START,
        "profit_target": ACCOUNT_RULES["profit_target"],
        "overall_drawdown": ACCOUNT_RULES["overall_drawdown"],
        "daily_drawdown": ACCOUNT_RULES["daily_drawdown"],
        "minimum_trading_days": ACCOUNT_RULES["minimum_trading_days"],
        "maximum_risk_per_trade": ACCOUNT_RULES["maximum_risk_per_trade"],
        "daily_symbol_risk": ACCOUNT_RULES["daily_symbol_risk"],
        "floating_loss_guard": ACCOUNT_RULES["floating_loss_guard"],
        "operational_permissions": ACCOUNT_RULES["operational_permissions"],
    }

    broker_risk_policy = {
        "volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
        "target_risk_is_not_assumed_executable": True,
        "skip_trade_if_no_executable_lot_fits": SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS,
        "reduce_trade_to_fit_effective_risk_cap": REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP,
        "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
        "ceil_risk_cap_breach_policy": "backoff_to_largest_safe_executable_lot",
        "minimum_lot_fallback_preserved": True,
        "reject_only_when_no_broker_valid_lot_fits": True,
        "actual_risk_source": (
            "broker rounded lot using risk_per_1_lot, broker_min_lot, broker_lot_step"
        ),
    }

    structural_integrity = {
        **stress_integrity,
        "artifact_policy": "hashless_structural_account_rules",
        "total_hard_fail_count_all_scenarios": total_hard_fail_count,
        "projected_buffer_reserve_violation_count": reserve_violation_count,
        "selected_variant_name": str(variant.get("name")),
        "selected_variant_label": label,
        "selected_variant_contract": compact_contract(variant),
        "selected_source_contract": source_contract(source),
        "selected_throttle_name": str(variant.get("throttle_name", "")),
        "selected_reserve_mode": str(
            variant.get("trailing_buffer_reserve_mode", "off")
        ),
        "selected_fixed_reserve_amount": float(
            variant.get("trailing_buffer_fixed_reserve_amount", 0.0)
        ),
        "selected_cost_allowance_r": float(
            variant.get("trailing_buffer_cost_allowance_r", 0.0)
        ),
        "candidate_contract_match": bool(
            walkthrough_integrity.get("candidate_contract_match")
        ),
        "active_account_rules_match": True,
        "random_seed": RANDOM_SEED,
    }

    final_config = {
        "created_at": now_utc(),
        "schema_version": SCHEMA_VERSION,
        "artifact_policy": "hashless_structural_account_rules",
        "script_version": SCRIPT_VERSION,
        "source": "11_monte_carlo_stress_validation.py",
        "pipeline_mode": PIPELINE_MODE,
        "approved_account_stage": ACCOUNT_STAGE,
        "live_approval_scope": approval_scope,
        "account_rule_profile": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "active_account_rule_contract": active_rule_summary,
        "daily_reset_timezone": DAILY_RESET_TIMEZONE,
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        "daily_symbol_risk_rule_enabled": expected_symbol_enabled,
        "daily_symbol_risk_enforcement_mode": EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE,
        "source_scenario": source,
        "selected_variant_label": label,
        "selected_variant": variant,
        "step10_candidate_integrity": walkthrough_integrity,
        "selected_hrp_weights": arr["weights"].to_dict(orient="records"),
        "walkthrough_summary_by_cost": walkthrough_config.get(
            "walkthrough_summary_by_cost", []
        ),
        "historical_cluster_bootstrap_summary": hist_summary.to_dict(
            orient="records"
        ),
        "monte_carlo_exact_summary": exact_summary.to_dict(orient="records"),
        "control_reproduction": control_reproduction.to_dict(orient="records"),
        "stress_gate_results": gates.to_dict(orient="records"),
        "stress_validation_status": status,
        "stress_validation_policy": (
            "blocking_mandatory_controls_with_diagnostic_review"
        ),
        "monte_carlo_gate": {
            "gate_status": status,
            "gate_policy": (
                "mandatory controls require exact Script 10 reproduction, zero hard "
                "failures, non-negative buffers, and zero projected-reserve violations"
            ),
            "gate_meaning": gate_meaning,
        },
        "stress_integrity": structural_integrity,
        "live_approval_status": live_approval_status,
        "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
        "failure_definition": (
            "hard_fail = breach of any enabled active overall drawdown, daily drawdown, "
            "daily-symbol-risk, or floating-loss safety rule; end_of_history_fail = "
            "configured target and minimum valid days not achieved before data ends"
        ),
        "risk_contract_version": RISK_CONTRACT_VERSION,
        "broker_risk_policy": broker_risk_policy,
        "risk_formula": (
            "overall_buffer = active drawdown breach value minus active floor after open stop risk; "
            f"guard_multiplier = {variant.get('throttle_name')} multiplier(overall_buffer); "
            "target risk uses HRP weight and calibrated AFML, is clipped by all active overlap, "
            "drawdown, reserve, symbol and optional floating-loss caps, then converted to the "
            "largest safe broker-executable lot"
        ),
        "monte_carlo_methodology": {
            "control_paths": "all unique Script 10 start timestamps replayed exactly once",
            "diagnostic_paths": (
                "historical starts sampled with replacement plus stochastic cost, missed-fill, "
                "slippage, gap, asset-decay, and contiguous cluster-window perturbations"
            ),
            "historical_uncertainty": (
                "start-month cluster bootstrap to reduce false precision from overlapping rolling starts"
            ),
            "limitation": (
                "Chronological trade order is retained; this is path stress Monte Carlo, "
                "not arbitrary iid trade-order permutation."
            ),
        },
    }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_policy": "hashless_structural_account_rules",
        "script_version": SCRIPT_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "approved_account_stage": ACCOUNT_STAGE,
        "live_approval_scope": approval_scope,
        "account_rule_profile": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "active_account_rule_contract": active_rule_summary,
        "daily_reset_timezone": DAILY_RESET_TIMEZONE,
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        "daily_symbol_risk_rule_enabled": expected_symbol_enabled,
        "daily_symbol_risk_enforcement_mode": EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE,
        "execution_mode": "dry_run" if dry_run else "full",
        "stress_validation_status": status,
        "stress_validation_policy": (
            "blocking_mandatory_controls_with_diagnostic_review"
        ),
        "live_approval_status": live_approval_status,
        "input_walkthrough_config": str(
            INPUT_WALKTHROUGH_CONFIG.relative_to(V2_ROOT)
        ),
        "input_walkthrough_results": str(
            INPUT_WALKTHROUGH_RESULTS.relative_to(V2_ROOT)
        ),
        "input_walkthrough_manifest": str(
            INPUT_WALKTHROUGH_MANIFEST.relative_to(V2_ROOT)
        ),
        "source_scenario": source,
        "selected_variant_label": label,
        "selected_variant": variant,
        "step10_candidate_integrity": walkthrough_integrity,
        "broker_risk_policy": broker_risk_policy,
        "stress_integrity": structural_integrity,
        "counts": {
            "walkthrough_rows": int(len(walkthrough)),
            "historical_bootstrap_rows": int(len(hist_boot)),
            "exact_mc_rows": int(len(exact)),
            "stress_scenarios": int(len(STRESS_SCENARIOS)),
            "starts_available": int(len(starts)),
            "control_reproduction_mismatches": int(
                stress_integrity["control_reproduction_mismatch_count"]
            ),
            "projected_buffer_reserve_violations": reserve_violation_count,
        },
        "settings": {
            "historical_bootstrap_samples": effective_bootstrap_samples,
            "exact_mc_runs_per_diagnostic_scenario": (
                min(DRY_RUNS_PER_SCENARIO, len(starts))
                if dry_run
                else int(runs_per_scenario)
            ),
            "control_runs_per_scenario": (
                min(DRY_RUNS_PER_SCENARIO, len(starts))
                if dry_run
                else int(len(starts))
            ),
            "random_seed": RANDOM_SEED,
            "mandatory_max_hard_fail_rate": MANDATORY_MAX_HARD_FAIL_RATE,
            "mandatory_min_worst_buffer": MANDATORY_MIN_WORST_BUFFER,
            "mandatory_min_p01_buffer": MANDATORY_MIN_P01_BUFFER,
            "diagnostic_max_hard_fail_rate_warning": DIAGNOSTIC_MAX_HARD_FAIL_RATE_WARNING,
            "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
            "broker_volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
            "account_rule_profile": ACCOUNT_RULE_PROFILE,
            "account_stage": ACCOUNT_STAGE,
            "daily_reset_timezone": DAILY_RESET_TIMEZONE,
            "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
            "daily_symbol_risk_rule_enabled": expected_symbol_enabled,
            "daily_symbol_risk_enforcement_mode": EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE,
            "max_daily_risk_per_symbol_pct": ACCOUNT_RULES["daily_symbol_risk"].get("max_pct"),
            "internal_operating_risk_per_symbol_pct": ACCOUNT_RULES["daily_symbol_risk"].get("internal_operating_pct"),
            "floating_loss_guard_enabled": FLOATING_LOSS_GUARD_ENABLED,
            "floating_loss_guard_internal_amount": FLOATING_LOSS_GUARD_AMOUNT,
            "selected_reserve_mode": str(
                variant.get("trailing_buffer_reserve_mode", "off")
            ),
            "selected_fixed_reserve_amount": float(
                variant.get("trailing_buffer_fixed_reserve_amount", 0.0)
            ),
            "selected_cost_allowance_r": float(
                variant.get("trailing_buffer_cost_allowance_r", 0.0)
            ),
        },
        "stress_scenarios": STRESS_SCENARIOS,
        "outputs": {
            "historical_bootstrap_results": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/historical_bootstrap_results.csv"
            ),
            "historical_bootstrap_summary": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/historical_bootstrap_summary.csv"
            ),
            "exact_monte_carlo_results": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/exact_monte_carlo_results.csv"
            ),
            "exact_monte_carlo_summary": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/exact_monte_carlo_summary.csv"
            ),
            "control_reproduction": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/control_reproduction.csv"
            ),
            "stress_gate_results": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/stress_gate_results.csv"
            ),
            "candidate_stress_validated_live_config": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/candidate_stress_validated_live_config.json"
            ),
            "manifest": (
                "portfolio_allocator/outputs/monte_carlo_stress_outputs/monte_carlo_stress_manifest.json"
            ),
        },
    }

    if write:
        hist_boot.to_csv(OUTPUT_DIR / "historical_bootstrap_results.csv", index=False)
        hist_summary.to_csv(OUTPUT_DIR / "historical_bootstrap_summary.csv", index=False)
        exact.to_csv(OUTPUT_DIR / "exact_monte_carlo_results.csv", index=False)
        exact_summary.to_csv(OUTPUT_DIR / "exact_monte_carlo_summary.csv", index=False)
        control_reproduction.to_csv(OUTPUT_DIR / "control_reproduction.csv", index=False)
        gates.to_csv(OUTPUT_DIR / "stress_gate_results.csv", index=False)
        write_json(
            OUTPUT_DIR / "candidate_stress_validated_live_config.json",
            final_config,
        )
        chart_paths: list[str] = []
        try:
            chart_paths = generate_charts(exact, walkthrough, exact_summary, OUTPUT_DIR)
        except Exception as exc:  # pragma: no cover - charts are a visualization aid, never a gate
            print(f"WARNING: chart generation failed ({type(exc).__name__}: {exc}); continuing without charts.")
        manifest["charts"] = chart_paths
        write_json(OUTPUT_DIR / "monte_carlo_stress_manifest.json", manifest)

    return manifest

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run five starts per scenario and a small bootstrap without "
            "writing final outputs."
        ),
    )
    parser.add_argument(
        "--runs-per-scenario",
        type=int,
        default=EXACT_MC_RUNS_PER_SCENARIO,
        help=(
            "Stochastic runs for each diagnostic scenario. Deterministic "
            "controls always replay every unique Script 10 start."
        ),
    )
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=HISTORICAL_BOOTSTRAP_SAMPLES,
    )
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1),
        help=(
            "ProcessPoolExecutor workers for the exact Monte Carlo simulate_path "
            "grid (default: cpu_count-1). Pass 1 to force the original serial "
            "path. The historical bootstrap loop always runs serially. RNG "
            "seeding for every simulated path stays strictly sequential on the "
            "main process regardless of this setting."
        ),
    )
    parser.add_argument(
        "--accept-moderate-hard-fail", action="store_true",
        help=f"Explicit human override mirroring Steps 09/10's: pass mandatory-class scenarios even with nonzero hard-fail, as long as it stays <= {MODERATE_HARD_FAIL_TOLERANCE:.0%}. Logged clearly in the manifest.",
    )
    args = parser.parse_args()

    if args.runs_per_scenario <= 0:
        raise ValueError("--runs-per-scenario must be positive.")
    if args.bootstrap_samples <= 0:
        raise ValueError("--bootstrap-samples must be positive.")

    manifest = build(
        write=not args.dry_run,
        dry_run=args.dry_run,
        runs_per_scenario=args.runs_per_scenario,
        bootstrap_samples=args.bootstrap_samples,
        n_workers=int(args.workers),
        accept_moderate_hard_fail=bool(args.accept_moderate_hard_fail),
    )
    print("V2 Phase B2.11 no-deadline Monte Carlo stress validation")
    print(f"Status: {manifest['stress_validation_status']}")
    print(
        "Account start: "
        f"balance=${CURRENT_BALANCE:,.2f}, "
        f"high-water=${ACCOUNT_START_TRAILING_HIGH_WATERMARK:,.2f}, "
        f"trailing floor=${ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR:,.2f}, "
        f"buffer=${ACCOUNT_START_REMAINING_BALANCE_BUFFER:,.2f}, "
        f"fresh-day floor=${ACCOUNT_START_DAILY_LOSS_FLOOR:,.2f}"
    )
    print(f"Selected variant: {manifest['selected_variant_label']}")
    print(f"Execution mode: {manifest['execution_mode']}")
    print(f"Approved account stage: {manifest['approved_account_stage']}")
    print(
        "Selected throttle/reserve: "
        f"{manifest['stress_integrity']['selected_throttle_name']} / "
        f"{manifest['stress_integrity']['selected_reserve_mode']} "
        f"(fixed={manifest['stress_integrity']['selected_fixed_reserve_amount']}, "
        f"allowance={manifest['stress_integrity']['selected_cost_allowance_r']})"
    )
    print(
        "Active daily symbol-risk rule: "
        f"enabled={manifest['daily_symbol_risk_rule_enabled']}, "
        f"mode={manifest['daily_symbol_risk_enforcement_mode']}"
    )
    print(f"Live approval status: {manifest['live_approval_status']}")
    print(f"Exact MC rows: {manifest['counts']['exact_mc_rows']}")
    print(
        "Integrity: "
        f"control_mismatches={manifest['counts']['control_reproduction_mismatches']}, "
        f"reserve_violations={manifest['counts']['projected_buffer_reserve_violations']}"
    )
    print(
        "Gate mode: blocking mandatory controls; diagnostic hard-fail "
        "rates above the warning threshold require review"
    )
    print(
        "Output dir: portfolio_allocator/outputs/monte_carlo_stress_outputs"
    )


if __name__ == "__main__":
    main()
