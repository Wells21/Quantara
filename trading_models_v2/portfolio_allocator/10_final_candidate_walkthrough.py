from __future__ import annotations

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

SCHEMA_VERSION = "v2.3"
SCRIPT_VERSION = "2026-07-21_generic_active_account_step10_event_timestamp_fix"
PIPELINE_MODE = "v2_generic_active_account_final_candidate_walkthrough"

INPUT_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
INPUT_HRP_WEIGHTS = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs" / "hrp_linkage_weights_step08_primary.csv"
INPUT_SELECTED_CONFIG = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_fast_manual_variant_lab_outputs" / "selected_manual_variant_live_config.json"
INPUT_09_MANIFEST = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_fast_manual_variant_lab_outputs" / "fast_manual_variant_lab_manifest.json"
INPUT_09_STAGE2_RUNS = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_fast_manual_variant_lab_outputs" / "stage2_run_results.csv"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "final_candidate_walkthrough_outputs"

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
ASSUME_FRESH_DAILY_RESET_AT_SIMULATION_START = bool(ACCOUNT_RULES["assume_fresh_daily_reset_at_simulation_start"])
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
STOP_IMMEDIATELY_ON_TARGET = bool(ACCOUNT_RULES["profit_target"].get("stop_immediately_when_eligible", True))
STOP_ON_RULE_BREACH = True
MINIMUM_TRADING_DAYS_ENABLED = bool(ACCOUNT_RULES["minimum_trading_days"]["enabled"])
MINIMUM_TRADING_DAYS_REQUIRED = int(ACCOUNT_RULES["minimum_trading_days"]["required_days"])
MINIMUM_TRADING_DAY_MODE = str(ACCOUNT_RULES["minimum_trading_days"]["day_count_mode"])
MINIMUM_TRADING_DAY_PROFIT_AMOUNT = float(ACCOUNT_RULES["minimum_trading_days"]["minimum_day_profit_amount"])
PROP_MAX_SINGLE_TRADE_RISK_PCT = (
    float(ACCOUNT_RULES["maximum_risk_per_trade"]["pct"])
    if ACCOUNT_RULES["maximum_risk_per_trade"]["enabled"] else None
)
FLOATING_LOSS_GUARD_ENABLED = bool(ACCOUNT_RULES["floating_loss_guard"]["enabled"])
FLOATING_LOSS_GUARD_AMOUNT = ACCOUNT_RULES["floating_loss_guard"].get("internal_operating_amount")
FLOATING_LOSS_GUARD_AMOUNT = float(FLOATING_LOSS_GUARD_AMOUNT) if FLOATING_LOSS_GUARD_AMOUNT is not None else None

AFML_BET_SIZE_COLUMN = "afml_bet_size"
AFML_SIZE_FLOOR = 0.0
AFML_SIZE_CAP = 1.0
DEFAULT_COSTS = [0.00, 0.02, 0.05]
EPS = 1e-12
DAY_NS = 24 * 60 * 60 * 1_000_000_000
# Mirrors 09_fast_manual_variant_lab.py's ACCEPT_MODERATE_HARD_FAIL /
# MODERATE_HARD_FAIL_TOLERANCE -- see build()'s accept_moderate_hard_fail arg.
MODERATE_HARD_FAIL_TOLERANCE = 0.03

BROKER_VOLUME_ROUNDING_MODE = "ceil_then_backoff_to_largest_safe_step"
REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP = True
SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS = True
BROKER_RISK_SOURCE_POLICY = (
    "04B per-trade risk_per_1_lot derived from the 00A MT5 broker profile and "
    "00B causal historical profit-currency-to-account-currency conversion"
)
RISK_SIZING_MODE = "direct_weight_calibrated_afml_broker_executable"
DEFAULT_ACCOUNT_RULE_PROFILE = ACCOUNT_RULE_PROFILE
EXPECTED_ACCOUNT_STAGE = ACCOUNT_STAGE
EXPECTED_SYMBOL_RISK_ENFORCEMENT_MODE = str(ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])
DRY_RUN_MAX_STARTS_PER_COST = 5


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        return np.nan if abs(b) < EPS or not np.isfinite(b) else float(a) / b
    except Exception:
        return np.nan


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


def risk_label(x: float) -> str:
    return f"{float(x) * 100:.4f}".replace(".", "p")


def variant_label(v: dict[str, Any]) -> str:
    return (
        f"{v['name']}__budget{risk_label(v['base_portfolio_risk_budget_pct'])}pct"
        f"__singlecap{risk_label(v['max_single_trade_risk_pct'])}pct"
    )


def compact_contract(value: Any) -> Any:
    """Normalize JSON-compatible values for deterministic contract comparison."""
    if isinstance(value, dict):
        return {str(k): compact_contract(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [compact_contract(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def ns_to_ts(ns: int | None) -> pd.Timestamp:
    return to_datetime_iso(int(ns), utc=True) if ns is not None and not pd.isna(ns) else pd.NaT


def day_from_ns(ns: int) -> int:
    return int((int(ns) + DAILY_RESET_OFFSET_NS) // DAY_NS)


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



def multiplier_from_value(value: float, tiers: list[list[float]] | list[tuple[float, float]], mode: str) -> float:
    """Return the most protective tier multiplier for a scalar state value."""
    if not np.isfinite(float(value)):
        raise ValueError(f"Multiplier input must be finite, got {value!r}")
    mult = 1.0
    for threshold, tier_mult in tiers or []:
        if (mode == "below" and value < float(threshold)) or (mode == "above" and value >= float(threshold)):
            mult = min(mult, float(tier_mult))
    return float(np.clip(mult, 0.0, 1.0))


def current_trailing_buffer(balance: float, open_risk_total: float, trailing_high_watermark: float) -> float:
    """Conservative distance above the active static/trailing overall floor."""
    return float(
        drawdown_stats(balance, open_risk_total, trailing_high_watermark)["remaining_dd_buffer"]
    )


def required_trailing_buffer_reserve(
    projected_open_risk: float,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Reserve required after admitting a trade for the selected Step 09 mode.

    Static mode uses only the fixed component. Dynamic mode adds the configured
    cost allowance multiplied by projected total open stop risk. Off mode is
    represented by admission_enabled=False and zero reserve components.
    """
    return float(
        max(0.0, fixed_reserve_amount)
        + max(0.0, cost_allowance_r) * max(0.0, projected_open_risk)
    )


def maximum_admissible_new_risk(
    trailing_buffer: float,
    current_open_risk: float,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Solve the exact pre-rounding ceiling for new risk.

    buffer - new_risk >= fixed + allowance * (current_open + new_risk)
    """
    allowance = max(0.0, float(cost_allowance_r))
    numerator = (
        float(trailing_buffer)
        - max(0.0, float(fixed_reserve_amount))
        - allowance * max(0.0, float(current_open_risk))
    )
    return float(numerator / (1.0 + allowance))


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


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


def validate_selected_step09_candidate(
    cfg: dict[str, Any],
    manifest: dict[str, Any],
    source: dict[str, Any],
    variant: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Fail closed on structural drift without file/contract hashes."""
    # APPROVED_FOR_SCRIPT10_MANUAL_OVERRIDE_MODERATE_HARD_FAIL is Script 09's
    # explicit, logged human override (see evaluate_single_variant_gate /
    # ACCEPT_MODERATE_HARD_FAIL in 09_fast_manual_variant_lab.py): a candidate
    # with nonzero but capped hard-fail (<=3% at every cost stress) accepted
    # as a deliberate speed/tail-risk trade-off rather than a literal
    # zero-hard-fail pass. approved_for_script10 is still required below, so
    # this does not weaken the actual approval boolean Script 10 checks.
    accepted_statuses = {"APPROVED_FOR_SCRIPT10", "APPROVED_FOR_SCRIPT10_MANUAL_OVERRIDE_MODERATE_HARD_FAIL"}
    if cfg.get("selection_status") not in accepted_statuses:
        raise ValueError(
            f"Script 09 selection_status is not an approved status {sorted(accepted_statuses)}: {cfg.get('selection_status')!r}"
        )
    if not bool(cfg.get("approved_for_script10")):
        raise ValueError("Script 09 selected config is not approved_for_script10.")
    if manifest.get("manual_variant_lab_status") != "PASS":
        raise ValueError("Script 09 manifest is not PASS.")
    if not bool(manifest.get("approved_for_script10")):
        raise ValueError("Script 09 manifest is not approved_for_script10.")

    for name, payload in [
        ("selected config", cfg.get("active_account_rules")),
        ("Script 09 manifest", manifest.get("active_account_rules")),
        ("nested selected config", (manifest.get("selected_variant_live_config") or {}).get("active_account_rules")),
    ]:
        if not structurally_equal(payload, ACCOUNT_RULE_CONTRACT):
            raise ValueError(
                f"{name} active account rules differ from active_account_rules.json. Rerun Scripts 08 and 09."
            )

    if str(cfg.get("account_rule_profile")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Selected Script 09 config account profile mismatch.")
    if str(cfg.get("account_stage")) != ACCOUNT_STAGE:
        raise ValueError("Selected Script 09 config account stage mismatch.")
    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    if parse_bool(cfg.get("daily_symbol_risk_rule_enabled"), default=False) != bool(expected_symbol["enabled"]):
        raise ValueError("Selected Script 09 config symbol-risk enabled state mismatch.")
    if str(cfg.get("daily_symbol_risk_enforcement_mode")) != str(expected_symbol["enforcement_mode"]):
        raise ValueError("Selected Script 09 config symbol-risk mode mismatch.")

    rule_contract = cfg.get("active_account_rule_contract", {}) or {}
    if str(rule_contract.get("profile_label")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Script 09 active account rule contract profile mismatch.")
    if str(rule_contract.get("account_stage")) != ACCOUNT_STAGE:
        raise ValueError("Script 09 active account rule contract stage mismatch.")
    for field, expected in {
        "current_balance": CURRENT_BALANCE,
        "current_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
    }.items():
        actual = optional_float(rule_contract.get(field))
        if actual is None or not np.isclose(actual, expected, rtol=0.0, atol=EPS):
            raise ValueError(f"Script 09 account-state mismatch for {field}: {actual!r} != {expected}")

    policy_sources = {
        "selected config": cfg,
        "manifest stage settings": manifest.get("stage_settings", {}) or {},
    }
    for source_name, policy in policy_sources.items():
        if not parse_bool(policy.get("reduce_trade_to_fit_effective_risk_cap"), default=False):
            raise ValueError(f"{source_name} does not enable reduce-to-fit effective risk sizing.")
        if not parse_bool(policy.get("skip_trade_if_no_executable_lot_fits"), default=False):
            raise ValueError(f"{source_name} does not fail closed when no broker-valid lot fits.")
        if str(policy.get("broker_risk_source_policy", "")) != BROKER_RISK_SOURCE_POLICY:
            raise ValueError(f"{source_name} broker/currency risk source mismatch.")

    for name, value in [
        ("selected config", optional_float(cfg.get("daily_reset_offset_hours"))),
        ("manifest", optional_float((manifest.get("stage_settings", {}) or {}).get("daily_reset_offset_hours"))),
    ]:
        if value is None or not np.isclose(value, DAILY_RESET_OFFSET_HOURS, rtol=0.0, atol=EPS):
            raise ValueError(f"Daily reset offset mismatch in {name}: {value!r} != {DAILY_RESET_OFFSET_HOURS}")

    nested = manifest.get("selected_variant_live_config", {}) or {}
    if str(nested.get("selected_variant_label")) != label:
        raise ValueError("Script 09 manifest selected label does not match selected config.")
    if compact_contract(nested.get("selected_variant")) != compact_contract(cfg.get("selected_variant")):
        raise ValueError("Script 09 manifest selected variant does not match selected config.")
    if source_contract(manifest.get("source_scenario", {}) or {}) != source_contract(source):
        raise ValueError("Script 09 manifest source scenario does not match selected config.")

    expected_label = variant_label(variant)
    if label != expected_label:
        raise ValueError(f"Selected variant label mismatch: {label!r} != {expected_label!r}")
    tested_labels = {str(x) for x in manifest.get("stage2_variants_tested", [])}
    if label not in tested_labels:
        raise ValueError("Selected variant label was not present in Script 09 Stage 2 tests.")
    generated = manifest.get("manual_variants_generated", []) or []
    generated_matches = [
        row for row in generated
        if isinstance(row, dict)
        and str(row.get("name")) == str(variant.get("name"))
        and variant_label(row) == label
    ]
    if len(generated_matches) != 1 or compact_contract(generated_matches[0]) != compact_contract(cfg.get("selected_variant")):
        raise ValueError("Selected variant does not exactly match the generated Script 09 contract.")
    ranked = cfg.get("all_ranked_variants", []) or []
    if not ranked or str(ranked[0].get("variant_label")) != label:
        raise ValueError("Selected Script 09 label is not the first ranked variant.")

    return {
        "candidate_contract_match": True,
        "artifact_policy": "hashless_structural_account_rules",
        "selected_variant_name": str(variant.get("name")),
        "selected_variant_label": label,
        "selected_variant_contract": compact_contract(variant),
        "selected_source_contract": source_contract(source),
        "selected_throttle_name": str(variant.get("throttle_name", "")),
        "selected_reserve_mode": str(variant.get("trailing_buffer_reserve_mode", "off")),
        "selected_fixed_reserve_amount": float(variant.get("trailing_buffer_fixed_reserve_amount", 0.0) or 0.0),
        "selected_cost_allowance_r": float(variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0),
        "selected_max_min_lot_risk_inflation": optional_float(variant.get("max_min_lot_risk_inflation")),
        "selected_min_lot_risk_inflation_guard_enabled": bool(optional_float(variant.get("max_min_lot_risk_inflation")) is not None),
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
    }

def load_selected() -> tuple[
    dict[str, Any],
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    cfg = load_json(INPUT_SELECTED_CONFIG)
    manifest = load_json(INPUT_09_MANIFEST)
    if "source_scenario" not in cfg or "selected_variant" not in cfg:
        raise ValueError(
            "selected_manual_variant_live_config.json must contain source_scenario and selected_variant"
        )
    source = dict(cfg["source_scenario"])
    variant = normalize_variant(cfg["selected_variant"])
    label = str(
        cfg.get("selected_variant_label")
        or cfg.get("summary", {}).get("variant_label")
        or variant.get("name", "selected_variant")
    )
    required_source = [
        "scenario_id",
        "return_mode",
        "universe",
        "profile_role",
        "linkage_method",
        "account_rule_profile",
        "account_stage",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
    ]
    missing = [key for key in required_source if key not in source or pd.isna(source[key])]
    if missing:
        raise ValueError(f"source_scenario missing active-account fields: {missing}")
    if str(source.get("account_rule_profile")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Source scenario account profile differs from active rules.")
    if str(source.get("account_stage")) != ACCOUNT_STAGE:
        raise ValueError("Source scenario account stage differs from active rules.")
    if parse_bool(source.get("daily_symbol_risk_rule_enabled"), default=False) != bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"]):
        raise ValueError("Source scenario symbol-risk enabled state differs from active rules.")

    integrity = validate_selected_step09_candidate(
        cfg, manifest, source, variant, label
    )
    return source, variant, label, cfg, manifest, integrity



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
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

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


def load_weights() -> pd.DataFrame:
    if not INPUT_HRP_WEIGHTS.exists():
        raise FileNotFoundError(f"Missing HRP weights: {INPUT_HRP_WEIGHTS}")
    df = pd.read_csv(INPUT_HRP_WEIGHTS)
    required = ["return_mode", "universe", "profile_role", "linkage_method", "representative_id", "weight"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"HRP weights missing columns: {missing}")
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna(subset=required)
    df = df[df["weight"] > 0].copy()
    if df.empty:
        raise RuntimeError("No valid HRP weights found.")
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
    trades["hrp_weight"] = trades["representative_id"].astype(str).map(wmap)
    trades = trades.dropna(subset=["hrp_weight"]).sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    end_ns = int(max(trades["entry_time"].astype("int64").max(), trades["exit_time"].astype("int64").max()))
    return {
        "weights": sw,
        "entry_ns": trades["entry_time"].astype("int64").to_numpy(dtype=np.int64),
        "exit_ns": trades["exit_time"].astype("int64").to_numpy(dtype=np.int64),
        "entry_day": (trades["entry_time"].astype("int64") // DAY_NS).to_numpy(dtype=np.int64),
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
        "trade_count_available": int(len(trades)),
        "final_data_end_ns": end_ns,
    }


def replay_grid(selected_label: str) -> tuple[pd.DataFrame, str]:
    """Replay the exact Script 09 Stage 2 starts; no silent ledger fallback."""
    if not INPUT_09_STAGE2_RUNS.exists():
        raise FileNotFoundError(
            f"Missing Script 09 Stage 2 runs required for exact replay: {INPUT_09_STAGE2_RUNS}"
        )
    df = pd.read_csv(INPUT_09_STAGE2_RUNS)
    required = {"variant_label", "start_ns", "cost_stress_r"}
    missing = sorted(required.difference(df.columns))
    if missing:
        raise ValueError(f"Script 09 Stage 2 runs missing columns: {missing}")
    selected = df[df["variant_label"].astype(str) == str(selected_label)].copy()
    if selected.empty:
        raise ValueError(
            f"Selected variant {selected_label!r} was not found in Script 09 Stage 2 runs."
        )
    selected["start_ns"] = pd.to_numeric(selected["start_ns"], errors="coerce")
    selected["cost_stress_r"] = pd.to_numeric(
        selected["cost_stress_r"], errors="coerce"
    )
    selected = selected.dropna(subset=["start_ns", "cost_stress_r"])
    if selected.empty:
        raise ValueError("Selected Script 09 Stage 2 rows contain no valid start timestamps.")

    stage09_costs = sorted(set(selected["cost_stress_r"].astype(float).tolist()))
    expected_stage09_costs = [0.00, 0.02]
    if len(stage09_costs) != len(expected_stage09_costs) or any(
        not np.isclose(a, b, rtol=0.0, atol=EPS)
        for a, b in zip(stage09_costs, expected_stage09_costs)
    ):
        raise ValueError(
            f"Unexpected Script 09 cost grid for selected candidate: {stage09_costs}"
        )

    start_values = sorted(set(selected["start_ns"].astype(np.int64).tolist()))
    if not start_values:
        raise ValueError("Script 09 selected candidate has no replay starts.")
    grid = pd.DataFrame(
        [
            {"start_ns": int(start_ns), "cost_stress_r": float(cost)}
            for cost in DEFAULT_COSTS
            for start_ns in start_values
        ]
    )
    return (
        grid.sort_values(["cost_stress_r", "start_ns"]).reset_index(drop=True),
        "exact_step09_stage2_start_times_replayed_at_step10_costs",
    )



def simulate(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    start_ns: int,
    cost: float,
    n_assets: int,
    n_families: int,
    n_edges: int,
) -> dict[str, Any]:
    _afml_div = AFMLC.divisor_for_arr(arr, variant.get("afml_concurrency_quantile"))  # AFML Ch.10.2/10.4; None = off
    entry_ns, exit_ns = arr["entry_ns"], arr["exit_ns"]
    start_idx = int(np.searchsorted(entry_ns, start_ns, side="left"))
    balance = float(CURRENT_BALANCE)
    min_balance = max_balance = peak_balance = balance
    trailing_high_watermark = max(ACCOUNT_START_TRAILING_HIGH_WATERMARK, balance)
    trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
    min_buffer = drawdown_stats(balance, 0.0, trailing_high_watermark)["remaining_dd_buffer"]
    max_realized_peak_to_valley_dd = 0.0
    max_challenge_dd_used = drawdown_stats(balance, 0.0, trailing_high_watermark)["challenge_dd_used"]

    open_total = 0.0
    open_asset = np.zeros(n_assets, dtype=float)
    open_family = np.zeros(n_families, dtype=float)
    open_edge = np.zeros(n_edges, dtype=float)
    count_asset = np.zeros(n_assets, dtype=np.int32)
    count_edge = np.zeros(n_edges, dtype=np.int32)
    heap: list[tuple[int, int, float, float, int, int, int]] = []

    attempted = accepted = skipped = closed = 0
    skipped_pause = skipped_zero_risk = skipped_invalid_broker_risk = 0
    skipped_min_lot_risk_cap = skipped_overlap_risk_cap = 0
    skipped_total_cap = skipped_asset_cap = skipped_family_cap = skipped_edge_cap = 0
    skipped_edge_count = skipped_asset_count = 0
    skipped_daily_symbol_risk_cap = 0
    skipped_projected_trailing_buffer_reserve = 0
    skipped_projected_daily_buffer_reserve = 0
    clipped_attempted = clipped_accepted = 0
    trailing_buffer_target_clipped_attempted = 0
    trailing_buffer_target_clip_amount_sum = 0.0
    throttle_reduced_attempted = throttle_reduced_accepted = 0
    throttle_mult_attempted_sum = 0.0
    min_trailing_buffer_at_attempt = np.inf
    min_trailing_buffer_at_accept = np.inf
    min_available_new_risk_at_attempt = np.inf
    min_projected_trailing_buffer_after_accept = np.inf
    min_required_trailing_buffer_reserve_after_accept = np.inf
    min_projected_buffer_excess_over_required_reserve_after_accept = np.inf

    max_open_risk_pct = 0.0
    max_open_trades = 0
    active_days = {day_from_ns(start_ns)}
    target_time = fail_time = None
    target_hit_balance = np.nan
    fail_reason = ""

    target_risk_pct_values: list[float] = []
    actual_risk_pct_values: list[float] = []
    target_risk_amount_values: list[float] = []
    actual_risk_amount_values: list[float] = []
    actual_lot_values: list[float] = []
    risk_inflation_values: list[float] = []
    min_lot_binding_count = 0
    skipped_min_lot_inflation_guard = 0
    max_min_lot_risk_inflation = optional_float(
        variant.get("max_min_lot_risk_inflation")
    )
    sum_throttle_mult = sum_target_lock_mult = sum_afml = 0.0

    daily_enabled = bool(variant.get("daily_drawdown_enabled", True))
    daily_pct = float(variant.get("daily_drawdown_pct", DAILY_LOSS_PCT) or DAILY_LOSS_PCT)
    daily_amount = float(variant.get("daily_drawdown_amount", DAILY_LOSS_AMOUNT) or DAILY_LOSS_AMOUNT)
    daily_mode = str(variant.get("daily_drawdown_mode", "equity_proxy_fixed_initial_amount"))
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
    daily_start_equity = account_equity_proxy(balance, open_total)
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
        equity = account_equity_proxy(balance, open_total)
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

    def reset_daily_if_needed(t: int) -> None:
        nonlocal daily_day, daily_start_balance, daily_start_equity
        nonlocal daily_start_value, daily_floor, daily_worst_buffer, daily_amount
        d = day_from_ns(t)
        if d != daily_day:
            refresh_qualifying_day()
            daily_day = d
            daily_start_balance = balance
            daily_start_equity = account_equity_proxy(balance, open_total)
            daily_floor = daily_loss_floor(daily_start_balance, daily_start_equity) if daily_enabled else -np.inf
            daily_amount = account_daily_drawdown_amount(
                ACCOUNT_RULES,
                day_start_balance=daily_start_balance,
                day_start_equity=daily_start_equity,
            )
            daily_start_value = daily_floor + daily_amount if daily_enabled else balance
            daily_worst_buffer = min(daily_worst_buffer, daily_amount if daily_enabled else np.inf)
            daily_closed_loss_by_asset.fill(0.0)

    def check_rules(t: int) -> None:
        nonlocal fail_reason, fail_time, min_buffer, daily_worst_buffer, max_daily_drawdown_pct
        nonlocal trailing_high_watermark, trailing_floor, max_daily_symbol_risk_amount, max_daily_symbol_risk_pct
        reset_daily_if_needed(t)
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        st = drawdown_stats(balance, open_total, trailing_high_watermark)
        min_buffer = min(min_buffer, st["remaining_dd_buffer"])
        if st["floor_breached"] and not fail_reason:
            fail_reason = "overall_drawdown_floor_breached"
            fail_time = t
            return
        if daily_enabled:
            ref = daily_reference_value()
            daily_worst_buffer = min(daily_worst_buffer, ref - daily_floor)
            max_daily_drawdown_pct = max(
                max_daily_drawdown_pct,
                safe_div(daily_start_value - ref, PROP_START_BALANCE),
            )
            if ref < daily_floor - EPS and not fail_reason:
                fail_reason = "daily_drawdown_floor_breached"
                fail_time = t
                return
        symbol_usage = daily_closed_loss_by_asset + np.maximum(open_asset, 0.0)
        if symbol_usage.size:
            observed = float(np.max(symbol_usage))
            max_daily_symbol_risk_amount = max(max_daily_symbol_risk_amount, observed)
            max_daily_symbol_risk_pct = max(max_daily_symbol_risk_pct, safe_div(observed, PROP_START_BALANCE))
            if (
                daily_symbol_risk_rule_enabled
                and symbol_daily_hard_cap_amount is not None
                and observed > symbol_daily_hard_cap_amount + EPS
                and not fail_reason
            ):
                fail_reason = "max_daily_symbol_risk_breached"
                fail_time = t

    def close_due(t: int) -> bool:
        nonlocal balance, min_balance, max_balance, peak_balance, min_buffer, open_total, closed, target_time, target_hit_balance, fail_time, fail_reason, max_realized_peak_to_valley_dd, max_challenge_dd_used, trailing_high_watermark, trailing_floor
        while heap and heap[0][0] <= t:
            et = heap[0][0]
            reset_daily_if_needed(et)
            pnl_batch = 0.0
            while heap and heap[0][0] == et:
                _, _, risk_amt, r, ac, fc, ec = heapq.heappop(heap)
                open_total -= risk_amt
                open_asset[ac] -= risk_amt
                open_family[fc] -= risk_amt
                open_edge[ec] -= risk_amt
                count_asset[ac] -= 1
                count_edge[ec] -= 1
                pnl = risk_amt * r
                pnl_batch += pnl
                if pnl < 0.0:
                    daily_closed_loss_by_asset[ac] += -pnl
                closed += 1
            if abs(open_total) < 1e-8:
                open_total = 0.0
            balance += pnl_batch
            trailing_high_watermark = max(trailing_high_watermark, balance)
            trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
            min_balance = min(min_balance, balance)
            max_balance = max(max_balance, balance)
            peak_balance = max(peak_balance, balance)
            max_realized_peak_to_valley_dd = max(max_realized_peak_to_valley_dd, peak_balance - balance)
            max_challenge_dd_used = max(max_challenge_dd_used, drawdown_stats(balance, open_total, trailing_high_watermark)["challenge_dd_used"])
            active_days.add(day_from_ns(et))
            refresh_qualifying_day()
            check_rules(et)
            refresh_qualifying_day()
            if target_time is None and not fail_reason and target_satisfied(
                ACCOUNT_RULES,
                balance=balance,
                qualifying_day_count=len(qualifying_days),
            ):
                target_time = et
                target_hit_balance = balance
            if (STOP_IMMEDIATELY_ON_TARGET and target_time is not None) or (STOP_ON_RULE_BREACH and fail_reason):
                return True
        return False

    trailing_buffer_admission_enabled = bool(
        variant.get("trailing_buffer_admission_enabled", False)
    )
    trailing_buffer_fixed_reserve_amount = float(
        variant.get("trailing_buffer_fixed_reserve_amount", 0.0) or 0.0
    )
    trailing_buffer_cost_allowance_r = float(
        variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0
    )
    trailing_buffer_reserve_mode = str(
        variant.get("trailing_buffer_reserve_mode", "off")
    )
    daily_buffer_admission_enabled = bool(
        variant.get("daily_buffer_admission_enabled", daily_enabled and trailing_buffer_admission_enabled)
    )
    daily_buffer_fixed_reserve_amount = float(
        variant.get("daily_buffer_fixed_reserve_amount", trailing_buffer_fixed_reserve_amount) or 0.0
    )
    daily_buffer_cost_allowance_r = float(
        variant.get("daily_buffer_cost_allowance_r", trailing_buffer_cost_allowance_r) or 0.0
    )

    check_rules(start_ns)

    for i in range(start_idx, len(entry_ns)):
        t = int(entry_ns[i])
        if close_due(t) or target_time is not None or fail_reason:
            break
        reset_daily_if_needed(t)
        attempted += 1
        active_days.add(day_from_ns(t))
        if balance < float(variant["pause_below_balance"]) - EPS:
            skipped += 1
            skipped_pause += 1
            continue

        # Step 09 throttle basis: remaining conservative trailing-DD buffer,
        # not absolute account balance.
        trailing_buffer = current_trailing_buffer(
            balance, open_total, trailing_high_watermark
        )
        min_trailing_buffer_at_attempt = min(
            min_trailing_buffer_at_attempt, trailing_buffer
        )
        throttle_mult = multiplier_from_value(
            trailing_buffer, variant["throttle_tiers"], "below"
        )
        throttle_mult_attempted_sum += throttle_mult
        if throttle_mult < 1.0 - EPS:
            throttle_reduced_attempted += 1

        # Target locks remain balance/profit based.
        target_lock_mult = multiplier_from_value(
            balance, variant["target_lock_tiers"], "above"
        )
        raw_pct = (
            float(variant["base_portfolio_risk_budget_pct"])
            * (float(base_raw_pct[i]) if _afml_div is None else float(base_raw_pct[i]) / float(_afml_div[i]))
            * throttle_mult
            * target_lock_mult
        )
        if raw_pct > float(variant["max_single_trade_risk_pct"]) + EPS:
            clipped_attempted += 1
        target_pct = min(raw_pct, float(variant["max_single_trade_risk_pct"]))
        if target_pct <= EPS or not np.isfinite(target_pct):
            skipped += 1
            skipped_zero_risk += 1
            continue

        requested_target_amount = balance * target_pct
        target_amount = requested_target_amount

        # Exact projected-buffer admission inherited from the frozen Script 09
        # candidate. Clip the theoretical target first, then revalidate the
        # broker-rounded executable risk.
        available_new_risk = np.inf
        if trailing_buffer_admission_enabled:
            available_new_risk = maximum_admissible_new_risk(
                trailing_buffer=trailing_buffer,
                current_open_risk=open_total,
                fixed_reserve_amount=trailing_buffer_fixed_reserve_amount,
                cost_allowance_r=trailing_buffer_cost_allowance_r,
            )
            min_available_new_risk_at_attempt = min(
                min_available_new_risk_at_attempt, available_new_risk
            )
            if available_new_risk <= EPS:
                skipped += 1
                skipped_projected_trailing_buffer_reserve += 1
                continue
            if target_amount > available_new_risk + EPS:
                trailing_buffer_target_clipped_attempted += 1
                trailing_buffer_target_clip_amount_sum += (
                    target_amount - available_new_risk
                )
                target_amount = available_new_risk
                target_pct = safe_div(target_amount, balance)
                if target_pct <= EPS or not np.isfinite(target_pct):
                    skipped += 1
                    skipped_projected_trailing_buffer_reserve += 1
                    continue

        daily_buffer = daily_reference_value() - daily_floor
        available_daily_new_risk = np.inf
        if daily_buffer_admission_enabled:
            available_daily_new_risk = maximum_admissible_new_risk(
                trailing_buffer=daily_buffer,
                current_open_risk=open_total,
                fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
                cost_allowance_r=daily_buffer_cost_allowance_r,
            )
            if available_daily_new_risk <= EPS:
                skipped += 1
                skipped_projected_daily_buffer_reserve += 1
                continue
            if target_amount > available_daily_new_risk + EPS:
                target_amount = available_daily_new_risk
                target_pct = safe_div(target_amount, balance)
                if target_pct <= EPS or not np.isfinite(target_pct):
                    skipped += 1
                    skipped_projected_daily_buffer_reserve += 1
                    continue

        ac, fc, ec = int(arr["asset_code"][i]), int(arr["family_code"][i]), int(arr["edge_code"][i])
        effective_risk_cap = min(
            balance * float(variant["max_single_trade_risk_pct"]),
            max(0.0, balance * float(variant["max_total_open_risk_pct"]) - open_total),
            max(0.0, balance * float(variant["max_asset_open_risk_pct"]) - open_asset[ac]),
            max(0.0, balance * float(variant["max_family_open_risk_pct"]) - open_family[fc]),
            max(0.0, balance * float(variant["max_edge_open_risk_pct"]) - open_edge[ec]),
        )
        if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
            effective_risk_cap = min(
                effective_risk_cap,
                max(0.0, FLOATING_LOSS_GUARD_AMOUNT - open_total),
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
                    - open_asset[ac],
                ),
            )
        if effective_risk_cap <= EPS:
            skipped += 1
            skipped_min_lot_risk_cap += 1
            continue
        if target_amount > effective_risk_cap + EPS:
            target_amount = effective_risk_cap
            target_pct = safe_div(target_amount, balance)

        br = broker_executable_risk(
            target_amount,
            float(arr["risk_per_1_lot"][i]),
            float(arr["broker_min_lot"][i]),
            float(arr["broker_lot_step"][i]),
            max_risk_amount=effective_risk_cap,
        )
        if not br["valid"]:
            skipped += 1
            skipped_invalid_broker_risk += 1
            continue
        risk_amt = float(br["actual_risk_amount"])
        actual_pct = safe_div(risk_amt, balance)
        if actual_pct <= EPS or not np.isfinite(actual_pct):
            skipped += 1
            skipped_invalid_broker_risk += 1
            continue
        if SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS and not bool(br["cap_fit"]):
            skipped += 1
            skipped_min_lot_risk_cap += 1
            continue
        if (
            max_min_lot_risk_inflation is not None
            and bool(br["min_lot_binding"])
            and np.isfinite(float(br["risk_inflation"]))
            and float(br["risk_inflation"]) > max_min_lot_risk_inflation + EPS
        ):
            skipped += 1
            skipped_min_lot_inflation_guard += 1
            continue

        projected_open_risk = open_total + risk_amt
        projected_trailing_buffer_after_trade = trailing_buffer - risk_amt
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
            skipped += 1
            skipped_projected_trailing_buffer_reserve += 1
            continue

        projected_daily_buffer_after_trade = daily_buffer - risk_amt
        required_daily_reserve_after_trade = required_trailing_buffer_reserve(
            projected_open_risk=projected_open_risk,
            fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
            cost_allowance_r=daily_buffer_cost_allowance_r,
        )
        if daily_buffer_admission_enabled and projected_daily_buffer_after_trade < required_daily_reserve_after_trade - EPS:
            skipped += 1
            skipped_projected_daily_buffer_reserve += 1
            continue

        proposed_symbol_usage = symbol_daily_risk_usage(
            daily_closed_loss_by_asset[ac], open_asset[ac] + risk_amt
        )
        if (
            daily_symbol_risk_rule_enabled
            and symbol_internal_operating_cap_amount is not None
            and proposed_symbol_usage > symbol_internal_operating_cap_amount + EPS
        ):
            skipped += 1
            skipped_daily_symbol_risk_cap += 1
            continue
        if open_total + risk_amt > balance * variant["max_total_open_risk_pct"] + EPS:
            skipped += 1; skipped_overlap_risk_cap += 1; skipped_total_cap += 1; continue
        if open_asset[ac] + risk_amt > balance * variant["max_asset_open_risk_pct"] + EPS:
            skipped += 1; skipped_overlap_risk_cap += 1; skipped_asset_cap += 1; continue
        if open_family[fc] + risk_amt > balance * variant["max_family_open_risk_pct"] + EPS:
            skipped += 1; skipped_overlap_risk_cap += 1; skipped_family_cap += 1; continue
        if open_edge[ec] + risk_amt > balance * variant["max_edge_open_risk_pct"] + EPS:
            skipped += 1; skipped_overlap_risk_cap += 1; skipped_edge_cap += 1; continue
        if count_edge[ec] >= variant["max_open_trades_per_edge"]:
            skipped += 1; skipped_edge_count += 1; continue
        if count_asset[ac] >= variant["max_open_trades_per_asset"]:
            skipped += 1; skipped_asset_count += 1; continue

        accepted += 1
        accepted_trade_days.add(day_from_ns(t))
        if raw_pct > float(variant["max_single_trade_risk_pct"]) + EPS:
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
        sum_throttle_mult += throttle_mult
        sum_target_lock_mult += target_lock_mult
        sum_afml += float(arr["afml_bet_size"][i])
        target_risk_pct_values.append(float(target_pct))
        actual_risk_pct_values.append(float(actual_pct))
        target_risk_amount_values.append(float(target_amount))
        actual_risk_amount_values.append(float(risk_amt))
        actual_lot_values.append(float(br["actual_lot"]))
        risk_inflation_values.append(float(br["risk_inflation"]))
        if bool(br["min_lot_binding"]):
            min_lot_binding_count += 1

        heapq.heappush(heap, (int(exit_ns[i]), i, risk_amt, float(arr["executed_r"][i]) - float(cost), ac, fc, ec))
        open_total += risk_amt
        open_asset[ac] += risk_amt
        open_family[fc] += risk_amt
        open_edge[ec] += risk_amt
        count_asset[ac] += 1
        count_edge[ec] += 1
        max_open_risk_pct = max(max_open_risk_pct, safe_div(open_total, balance))
        max_open_trades = max(max_open_trades, len(heap))
        check_rules(t)
        if STOP_ON_RULE_BREACH and fail_reason:
            break

    if target_time is None and not fail_reason:
        close_due(int(arr["final_data_end_ns"]))
    if target_time is None and not fail_reason:
        fail_reason = "target_not_reached_by_end_of_history"
        fail_time = int(arr["final_data_end_ns"])
        active_days.add(day_from_ns(fail_time))
        refresh_qualifying_day()

    passed = target_time is not None and fail_reason == ""
    max_drawdown_failed = fail_reason == "overall_drawdown_floor_breached"
    daily_drawdown_failed = fail_reason == "daily_drawdown_floor_breached"
    daily_symbol_risk_failed = fail_reason == "max_daily_symbol_risk_breached"
    hard_failed = max_drawdown_failed or daily_drawdown_failed or (
        daily_symbol_risk_rule_enabled and daily_symbol_risk_failed
    )
    end_failed = fail_reason == "target_not_reached_by_end_of_history"

    actual_arr = np.asarray(actual_risk_pct_values, dtype=float)
    target_arr = np.asarray(target_risk_pct_values, dtype=float)
    infl_arr = np.asarray(risk_inflation_values, dtype=float)
    lot_arr = np.asarray(actual_lot_values, dtype=float)

    return {
        "start_time": ns_to_ts(start_ns),
        "start_ns": int(start_ns),
        "cost_stress_r": float(cost),
        "passed": bool(passed),
        "failed": bool(not passed),
        "hard_failed": bool(hard_failed),
        "max_drawdown_failed": bool(max_drawdown_failed),
        "daily_drawdown_failed": bool(daily_drawdown_failed),
        "daily_symbol_risk_failed": bool(daily_symbol_risk_failed),
        "end_of_history_fail": bool(end_failed),
        "failure_reason": fail_reason,
        "target_hit_time": ns_to_ts(target_time),
        "failure_time": ns_to_ts(fail_time),
        "trading_days_to_target": len([d for d in active_days if target_time is not None and d <= day_from_ns(target_time)]) if target_time is not None else np.nan,
        "calendar_days_to_target": safe_div(target_time - start_ns, DAY_NS) if target_time is not None else np.nan,
        "observation_calendar_days": safe_div(arr["final_data_end_ns"] - start_ns, DAY_NS),
        "start_balance": CURRENT_BALANCE,
        "start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
        "ending_balance": balance,
        "target_hit_balance": target_hit_balance,
        "minimum_trading_days_enabled": MINIMUM_TRADING_DAYS_ENABLED,
        "minimum_trading_days_required": MINIMUM_TRADING_DAYS_REQUIRED,
        "qualifying_trading_days": int(len(qualifying_days)),
        "minimum_trading_days_satisfied": minimum_days_satisfied(ACCOUNT_RULES, len(qualifying_days)),
        "net_profit": balance - CURRENT_BALANCE,
        "min_balance": min_balance,
        "max_balance": max_balance,
        "worst_buffer_above_floor": min_buffer,
        "remaining_buffer_at_end": drawdown_stats(balance, open_total, trailing_high_watermark)["remaining_dd_buffer"],
        "trailing_high_watermark": trailing_high_watermark,
        "trailing_drawdown_floor_at_end": trailing_floor,
        "trailing_floor_locked_at_initial_balance": bool(
            drawdown_stats(balance, open_total, trailing_high_watermark)["trailing_floor_locked_at_initial_balance"]
        ),
        "max_challenge_dd_used": max_challenge_dd_used,
        "max_realized_peak_to_valley_dd": max_realized_peak_to_valley_dd,
        "daily_drawdown_enabled": bool(daily_enabled),
        "daily_drawdown_pct": float(daily_pct),
        "daily_drawdown_amount": float(daily_amount),
        "daily_drawdown_mode": daily_mode,
        "daily_drawdown_breach_basis": str(ACCOUNT_RULES["daily_drawdown"]["breach_basis"]),
        "daily_worst_buffer_above_floor": daily_worst_buffer,
        "max_daily_drawdown_pct": max_daily_drawdown_pct,
        "account_rule_profile": variant.get("account_rule_profile"),
        "account_stage": variant.get("account_stage"),
        "daily_reset_timezone": DAILY_RESET_TIMEZONE,
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        "daily_symbol_risk_rule_enabled": bool(daily_symbol_risk_rule_enabled),
        "daily_symbol_risk_enforcement_mode": daily_symbol_risk_enforcement_mode,
        "max_daily_risk_per_symbol_pct": optional_float(variant.get("max_daily_risk_per_symbol_pct")),
        "max_daily_risk_per_symbol_amount": symbol_daily_hard_cap_amount,
        "internal_operating_risk_per_symbol_pct": optional_float(variant.get("internal_operating_risk_per_symbol_pct")),
        "internal_operating_risk_per_symbol_amount": symbol_internal_operating_cap_amount,
        "symbol_risk_safety_reserve_amount": optional_float(variant.get("symbol_risk_safety_reserve_amount")),
        "max_daily_symbol_risk_amount_observed": max_daily_symbol_risk_amount,
        "max_daily_symbol_risk_pct_observed": max_daily_symbol_risk_pct,
        "floating_loss_guard_enabled": FLOATING_LOSS_GUARD_ENABLED,
        "floating_loss_guard_internal_amount": FLOATING_LOSS_GUARD_AMOUNT,
        "attempted_trades": attempted,
        "accepted_trades": accepted,
        "skipped_trades": skipped,
        "closed_trades": closed,
        "active_trading_days": len(active_days),
        "accepted_trades_per_active_day": safe_div(accepted, len(active_days)),
        "max_open_risk_pct": max_open_risk_pct,
        "max_open_trades": max_open_trades,
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
        "trailing_buffer_target_clip_rate_attempted": safe_div(
            trailing_buffer_target_clipped_attempted, attempted
        ),
        "trailing_buffer_target_clip_amount_usd": trailing_buffer_target_clip_amount_sum,
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
            if np.isfinite(min_projected_buffer_excess_over_required_reserve_after_accept)
            else np.nan
        ),
        "single_trade_clip_rate_attempted": safe_div(clipped_attempted, attempted),
        "single_trade_clip_rate_accepted": safe_div(clipped_accepted, accepted),
        "avg_afml_bet_size": safe_div(sum_afml, accepted),
        "avg_throttle_multiplier": safe_div(sum_throttle_mult, accepted),
        "avg_throttle_multiplier_attempted": safe_div(
            throttle_mult_attempted_sum, attempted
        ),
        "throttle_activation_rate_attempted": safe_div(
            throttle_reduced_attempted, attempted
        ),
        "throttle_activation_rate_accepted": safe_div(
            throttle_reduced_accepted, accepted
        ),
        "avg_target_lock_multiplier": safe_div(sum_target_lock_mult, accepted),
        "median_target_trade_risk_pct": float(np.nanmedian(target_arr)) if len(target_arr) else np.nan,
        "median_actual_trade_risk_pct": float(np.nanmedian(actual_arr)) if len(actual_arr) else np.nan,
        "p90_actual_trade_risk_pct": float(np.nanpercentile(actual_arr, 90)) if len(actual_arr) else np.nan,
        "max_actual_trade_risk_pct": float(np.nanmax(actual_arr)) if len(actual_arr) else np.nan,
        "median_target_trade_risk_amount": float(np.nanmedian(target_risk_amount_values)) if target_risk_amount_values else np.nan,
        "median_actual_trade_risk_amount": float(np.nanmedian(actual_risk_amount_values)) if actual_risk_amount_values else np.nan,
        "median_actual_lot": float(np.nanmedian(lot_arr)) if len(lot_arr) else np.nan,
        "min_lot_binding_rate": safe_div(min_lot_binding_count, accepted),
        "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
        "min_lot_risk_inflation_guard_enabled": bool(
            max_min_lot_risk_inflation is not None
        ),
        "skipped_min_lot_inflation_guard": int(
            skipped_min_lot_inflation_guard
        ),
        "median_risk_inflation_accepted": float(np.nanmedian(infl_arr)) if len(infl_arr) else np.nan,
        "p90_risk_inflation_accepted": float(np.nanpercentile(infl_arr, 90)) if len(infl_arr) else np.nan,
        "max_risk_inflation_accepted": float(np.nanmax(infl_arr)) if len(infl_arr) else np.nan,
    }



def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cost, g in df.groupby("cost_stress_r", dropna=False):
        passed = g["passed"].astype(bool)
        hard = g["hard_failed"].astype(bool)
        maxdd = g.get("max_drawdown_failed", pd.Series(False, index=g.index)).astype(bool)
        daily = g.get("daily_drawdown_failed", pd.Series(False, index=g.index)).astype(bool)
        end = g["end_of_history_fail"].astype(bool)
        days = pd.to_numeric(g.loc[passed, "trading_days_to_target"], errors="coerce")
        rows.append({
            "cost_stress_r": float(cost),
            "runs": int(len(g)),
            "pass_rate": float(passed.mean()),
            "fail_rate": float((~passed).mean()),
            "hard_fail_rate": float(hard.mean()),
            "max_drawdown_fail_rate": float(maxdd.mean()),
            "daily_drawdown_fail_rate": float(daily.mean()),
            "daily_symbol_risk_fail_rate": float(g.get("daily_symbol_risk_failed", pd.Series(False, index=g.index)).astype(bool).mean()),
            "max_daily_symbol_risk_amount_observed": float(
                pd.to_numeric(g.get("max_daily_symbol_risk_amount_observed"), errors="coerce").max()
            ),
            "max_daily_symbol_risk_pct_observed": float(
                pd.to_numeric(g.get("max_daily_symbol_risk_pct_observed"), errors="coerce").max()
            ),
            "end_of_history_fail_rate": float(end.mean()),
            "median_trading_days_to_target": float(days.median()) if len(days.dropna()) else np.nan,
            "mean_trading_days_to_target": float(days.mean()) if len(days.dropna()) else np.nan,
            "worst_buffer_above_floor": float(g["worst_buffer_above_floor"].min()),
            "median_worst_buffer_above_floor": float(g["worst_buffer_above_floor"].median()),
            "daily_worst_buffer_above_floor": float(pd.to_numeric(g.get("daily_worst_buffer_above_floor"), errors="coerce").min()) if "daily_worst_buffer_above_floor" in g else np.nan,
            "max_daily_drawdown_pct": float(pd.to_numeric(g.get("max_daily_drawdown_pct"), errors="coerce").max()) if "max_daily_drawdown_pct" in g else np.nan,
            "max_observed_open_risk_pct": float(g["max_open_risk_pct"].max()),
            "median_accepted_trades": float(g["accepted_trades"].median()),
            "median_accepted_trades_per_active_day": float(g["accepted_trades_per_active_day"].median()),
            "median_actual_trade_risk_pct": float(pd.to_numeric(g["median_actual_trade_risk_pct"], errors="coerce").median()),
            "p90_actual_trade_risk_pct": float(pd.to_numeric(g["p90_actual_trade_risk_pct"], errors="coerce").median()),
            "max_actual_trade_risk_pct": float(pd.to_numeric(g["max_actual_trade_risk_pct"], errors="coerce").max()),
            "median_min_lot_binding_rate": float(pd.to_numeric(g["min_lot_binding_rate"], errors="coerce").median()),
            "median_risk_inflation_accepted": float(pd.to_numeric(g["median_risk_inflation_accepted"], errors="coerce").median()),
            "p90_risk_inflation_accepted": float(pd.to_numeric(g["p90_risk_inflation_accepted"], errors="coerce").median()),
            "max_risk_inflation_accepted": float(pd.to_numeric(g["max_risk_inflation_accepted"], errors="coerce").max()),
            "median_skipped_min_lot_risk_cap": float(pd.to_numeric(g["skipped_min_lot_risk_cap"], errors="coerce").median()),
            "median_skipped_invalid_broker_risk": float(pd.to_numeric(g["skipped_invalid_broker_risk"], errors="coerce").median()),
            "median_skipped_overlap_risk_cap": float(pd.to_numeric(g["skipped_overlap_risk_cap"], errors="coerce").median()),
            "median_skipped_projected_trailing_buffer_reserve": float(
                pd.to_numeric(
                    g["skipped_projected_trailing_buffer_reserve"], errors="coerce"
                ).median()
            ),
            "median_throttle_activation_rate_attempted": float(
                pd.to_numeric(
                    g["throttle_activation_rate_attempted"], errors="coerce"
                ).median()
            ),
            "median_throttle_activation_rate_accepted": float(
                pd.to_numeric(
                    g["throttle_activation_rate_accepted"], errors="coerce"
                ).median()
            ),
            "min_trailing_buffer_at_attempt": float(
                pd.to_numeric(g["min_trailing_buffer_at_attempt"], errors="coerce").min()
            ),
            "min_projected_trailing_buffer_after_accept": float(
                pd.to_numeric(
                    g["min_projected_trailing_buffer_after_accept"], errors="coerce"
                ).min()
            ),
            "min_projected_buffer_excess_over_required_reserve_after_accept": float(
                pd.to_numeric(
                    g[
                        "min_projected_buffer_excess_over_required_reserve_after_accept"
                    ],
                    errors="coerce",
                ).min()
            ),
        })
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
    (start_ns, cost_stress_r) chunk -- not the full array dict per task."""
    global _POOL_ARR, _POOL_BASE_RAW_PCT, _POOL_VARIANT, _POOL_DIMS
    _POOL_ARR = arr
    _POOL_BASE_RAW_PCT = base_raw_pct
    _POOL_VARIANT = variant
    _POOL_DIMS = (n_assets, n_families, n_edges)


def _pool_run_chunk(
    pair_slice: list[tuple[int, float]],
) -> list[dict[str, Any]]:
    """Worker task: run simulate() for one slice of (start_ns, cost_stress_r)
    pairs. Pure function of its arguments plus the process-local arrays
    stashed by _pool_init -- no shared mutable state, so results are
    identical regardless of how work is chunked or which worker executes
    which chunk."""
    assert (
        _POOL_ARR is not None
        and _POOL_BASE_RAW_PCT is not None
        and _POOL_VARIANT is not None
        and _POOL_DIMS is not None
    )
    n_assets, n_families, n_edges = _POOL_DIMS
    rows = []
    for start_ns, cost_stress_r in pair_slice:
        rows.append(
            simulate(
                _POOL_ARR,
                _POOL_BASE_RAW_PCT,
                _POOL_VARIANT,
                int(start_ns),
                float(cost_stress_r),
                n_assets,
                n_families,
                n_edges,
            )
        )
    return rows


def run_replay_grid(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    variant: dict[str, Any],
    pairs: list[tuple[int, float]],
    n_assets: int,
    n_families: int,
    n_edges: int,
    desc: str,
    *,
    n_workers: int = 1,
    chunk_size: int = 25,
) -> list[dict[str, Any]]:
    """Runs simulate() for every (start_ns, cost_stress_r) replay pair.

    n_workers <= 1 runs the original single-process loop (used for small
    jobs and for cross-checking parallel output against serial ground
    truth). n_workers > 1 fans the work out across a ProcessPoolExecutor,
    chunked at a fine grain over the flattened pair list -- not one task
    per top-level item -- so speedup scales with n_workers regardless of
    how many replay pairs are in the grid.

    Results are always returned in the exact input `pairs` order (rebuilt
    via a (start_ns, cost_stress_r) lookup after collection), since
    ProcessPoolExecutor.as_completed() does not preserve submission order
    and downstream code relies on positional alignment with `pairs`.
    """
    total = len(pairs)

    if n_workers <= 1:
        rows = []
        with tqdm(total=total, desc=desc, unit="start") as pbar:
            for start_ns, cost_stress_r in pairs:
                rows.append(
                    simulate(
                        arr,
                        base_raw_pct,
                        variant,
                        int(start_ns),
                        float(cost_stress_r),
                        n_assets,
                        n_families,
                        n_edges,
                    )
                )
                pbar.update(1)
        return rows

    tasks = [pairs[i:i + chunk_size] for i in range(0, len(pairs), chunk_size)]
    row_map: dict[tuple[int, float], dict[str, Any]] = {}
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_pool_init,
        initargs=(arr, base_raw_pct, variant, n_assets, n_families, n_edges),
    ) as executor:
        futures = [executor.submit(_pool_run_chunk, task) for task in tasks]
        with tqdm(total=total, desc=f"{desc} [{n_workers} workers]", unit="start") as pbar:
            for future in as_completed(futures):
                chunk_rows = future.result()
                for r in chunk_rows:
                    row_map[(int(r["start_ns"]), float(r["cost_stress_r"]))] = r
                pbar.update(len(chunk_rows))
    return [row_map[(int(start_ns), float(cost_stress_r))] for start_ns, cost_stress_r in pairs]


def build(write: bool = True, dry_run: bool = False, n_workers: int = 1, accept_moderate_hard_fail: bool = False) -> dict[str, Any]:
    ensure_dirs()
    validate_account_start_state()
    source, variant, label, step09_config, step09_manifest, candidate_integrity = load_selected()
    ledger, _, _, _ = load_ledger()
    weights = load_weights()
    arr = prepare_arrays(source, ledger, weights)
    base_raw_pct = arr["hrp_weight"] * arr["afml_bet_size"]
    starts, start_source = replay_grid(label)
    if dry_run:
        starts = (
            starts.sort_values(["cost_stress_r", "start_ns"])
            .groupby("cost_stress_r", group_keys=False)
            .head(DRY_RUN_MAX_STARTS_PER_COST)
            .reset_index(drop=True)
        )
        start_source += "_dry_run_subset"
    n_assets = int(ledger["asset_code"].nunique())
    n_families = int(ledger["family_code"].nunique())
    n_edges = int(ledger["edge_code"].nunique())

    # Preserve nanosecond start timestamps exactly. DataFrame.iterrows() coerces a
    # mixed int64/float64 row to float64, which can shift ~1e18 nanosecond values
    # by 64-128 ns before int(...). itertuples() preserves the int64 scalar.
    starts = starts.copy()
    starts["start_ns"] = pd.to_numeric(starts["start_ns"], errors="raise").astype(np.int64)
    starts["cost_stress_r"] = pd.to_numeric(starts["cost_stress_r"], errors="raise").astype(float)
    expected_start_ns = starts["start_ns"].to_numpy(dtype=np.int64, copy=True)
    expected_costs = starts["cost_stress_r"].to_numpy(dtype=float, copy=True)

    start_cost_pairs = list(
        starts[["start_ns", "cost_stress_r"]].itertuples(index=False, name=None)
    )
    sim_rows = run_replay_grid(
        arr,
        base_raw_pct,
        variant,
        start_cost_pairs,
        n_assets,
        n_families,
        n_edges,
        "Replaying final no-deadline candidate",
        n_workers=n_workers,
    )
    rows = [
        {
            "selected_variant_label": label,
            "source_scenario_id": source.get("scenario_id", ""),
            **r,
        }
        for r in sim_rows
    ]
    results = pd.DataFrame(rows)
    summary = aggregate(results)

    actual_start_ns = pd.to_numeric(results["start_ns"], errors="raise").astype(np.int64).to_numpy()
    actual_costs = pd.to_numeric(results["cost_stress_r"], errors="raise").astype(float).to_numpy()
    start_timestamp_mismatch_count = int(np.count_nonzero(actual_start_ns != expected_start_ns))
    start_cost_mismatch_count = int(
        np.count_nonzero(~np.isclose(actual_costs, expected_costs, rtol=0.0, atol=EPS))
    )

    hard_fail_count = int(results["hard_failed"].astype(bool).sum())
    hard_fail_rate = safe_div(hard_fail_count, len(results)) if len(results) else float("nan")
    reserve_violation_mask = (
        pd.to_numeric(
            results["min_projected_buffer_excess_over_required_reserve_after_accept"],
            errors="coerce",
        )
        < -EPS
    )
    reserve_violation_count = int(reserve_violation_mask.fillna(False).sum())
    exact_replay_source = start_source.startswith("exact_step09_stage2_start_times")
    candidate_contract_match = bool(candidate_integrity.get("candidate_contract_match"))
    hard_fail_ok = hard_fail_count == 0
    manual_override_applied = False
    if (
        not hard_fail_ok
        and accept_moderate_hard_fail
        and np.isfinite(hard_fail_rate)
        and hard_fail_rate <= MODERATE_HARD_FAIL_TOLERANCE + EPS
    ):
        # Mirrors 09_fast_manual_variant_lab.py's ACCEPT_MODERATE_HARD_FAIL:
        # an explicit, logged human decision to accept nonzero but capped
        # (<=3%) hard-fail as a deliberate speed/tail-risk trade-off, since
        # Step 10 replays the EXACT same grid Step 09 was approved on and
        # would otherwise always re-reject an override that already happened
        # upstream.
        hard_fail_ok = True
        manual_override_applied = True
    walkthrough_status = (
        "PASS"
        if (
            hard_fail_ok
            and reserve_violation_count == 0
            and start_timestamp_mismatch_count == 0
            and start_cost_mismatch_count == 0
            and candidate_contract_match
            and exact_replay_source
        )
        else "FAIL"
    )

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
    }

    selected_config = {
        "created_at": now_utc(),
        "schema_version": SCHEMA_VERSION,
        "artifact_policy": "hashless_structural_account_rules",
        "source": "10_final_candidate_walkthrough.py",
        "script_version": SCRIPT_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "source_scenario": source,
        "selected_variant_label": label,
        "selected_variant": variant,
        "selected_hrp_weights": arr["weights"].to_dict(orient="records"),
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "account_rule_profile": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "walkthrough_summary_by_cost": summary.to_dict(orient="records"),
        "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
        "failure_definition": (
            "hard_fail = active overall drawdown, active daily drawdown, or enabled daily-symbol-risk breach; "
            "end_of_history_fail = configured profit target plus minimum valid days not reached before history ends"
        ),
        "risk_contract_version": RISK_SIZING_MODE,
        "broker_risk_policy": {
            "volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
            "reduce_trade_to_fit_effective_risk_cap": REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP,
            "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
            "skip_trade_if_no_executable_lot_fits": bool(
                SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS
            ),
            "ceil_risk_cap_breach_policy": "backoff_to_largest_safe_executable_lot",
            "minimum_lot_fallback_preserved": True,
            "reject_only_when_no_broker_valid_lot_fits": True,
            "target_risk_is_not_assumed_executable": True,
            "actual_risk_source": (
                "broker rounded lot using risk_per_1_lot, broker_min_lot, broker_lot_step"
            ),
        },
        "active_account_rule_contract": active_rule_summary,
        "risk_formula": (
            "overall_buffer = active drawdown breach value minus active floor after open stop risk; "
            f"guard_multiplier = {variant.get('throttle_name')} multiplier(overall_buffer); "
            "target_risk_pct = min(base_budget * guard_multiplier * HRP_weight * calibrated_AFML_bet_size, max_single_trade_cap); "
            "target_risk_amount is clipped to the selected projected-buffer ceiling when admission is enabled; "
            "actual_lot = max(broker_min_lot, ceil(target_risk_amount / risk_per_1_lot / broker_lot_step) * broker_lot_step); "
            "actual_risk_amount = actual_lot * risk_per_1_lot; "
            f"selected reserve mode = {variant.get('trailing_buffer_reserve_mode')}, "
            f"fixed reserve = {variant.get('trailing_buffer_fixed_reserve_amount')}, "
            f"cost allowance R = {variant.get('trailing_buffer_cost_allowance_r')}; "
            "PnL and all portfolio, daily, symbol and optional floating-loss caps use actual_risk_amount"
        ),
        "step09_candidate_integrity": candidate_integrity,
        "walkthrough_integrity": {
            **candidate_integrity,
            "hard_fail_count": hard_fail_count,
            "projected_buffer_reserve_violation_count": reserve_violation_count,
            "start_timestamp_mismatch_count": start_timestamp_mismatch_count,
            "start_cost_mismatch_count": start_cost_mismatch_count,
            "start_timestamp_precision_policy": "int64_preserved_via_itertuples",
            "candidate_contract_match": candidate_contract_match,
            "exact_replay_source": exact_replay_source,
            "step09_replay_starts_found": bool(len(starts) > 0),
        },
        "live_approval_status": (
            "WALKTHROUGH_PASS_MONTE_CARLO_REQUIRED"
            if walkthrough_status == "PASS"
            else "WALKTHROUGH_FAILED_DO_NOT_PROCEED"
        ),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_policy": "hashless_structural_account_rules",
        "script_version": SCRIPT_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "walkthrough_status": walkthrough_status,
        "hard_fail_count": hard_fail_count,
        "hard_fail_rate": hard_fail_rate,
        "manual_override_applied": manual_override_applied,
        "manual_override_note": (
            f"MANUAL OVERRIDE ACCEPTED: hard-fail rate {hard_fail_rate:.4f} <= {MODERATE_HARD_FAIL_TOLERANCE:.2%} tolerance; "
            "accepted as a deliberate speed/tail-risk trade-off, not a literal zero-hard-fail pass."
            if manual_override_applied else ""
        ),
        "input_selected_config": str(INPUT_SELECTED_CONFIG.relative_to(V2_ROOT)),
        "input_step09_manifest": str(INPUT_09_MANIFEST.relative_to(V2_ROOT)),
        "input_step09_stage2_runs": str(INPUT_09_STAGE2_RUNS.relative_to(V2_ROOT)),
        "replay_start_source": start_source,
        "source_scenario": source,
        "selected_variant_label": label,
        "selected_variant": variant,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "account_rule_profile": variant.get("account_rule_profile"),
        "account_stage": variant.get("account_stage"),
        "daily_symbol_risk_rule_enabled": bool(
            variant.get("daily_symbol_risk_rule_enabled", False)
        ),
        "daily_symbol_risk_enforcement_mode": variant.get(
            "daily_symbol_risk_enforcement_mode"
        ),
        "daily_drawdown": {
            "name": variant.get("daily_drawdown_name"),
            "enabled": bool(variant.get("daily_drawdown_enabled", False)),
            "pct": float(variant.get("daily_drawdown_pct", 0.0) or 0.0),
            "amount": float(variant.get("daily_drawdown_amount", 0.0) or 0.0),
            "mode": str(variant.get("daily_drawdown_mode", "off")),
            "breach_basis": str(ACCOUNT_RULES["daily_drawdown"]["breach_basis"]),
            "reset_timezone": DAILY_RESET_TIMEZONE,
            "reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        },
        "active_account_rule_contract": active_rule_summary,
        "broker_risk_policy": selected_config["broker_risk_policy"],
        "step09_candidate_integrity": candidate_integrity,
        "walkthrough_integrity": selected_config["walkthrough_integrity"],
        "counts": {
            "ledger_rows_loaded": int(len(ledger)),
            "scenario_trades_available": int(arr["trade_count_available"]),
            "hrp_edges": int(arr["weights"]["edge_id"].nunique())
            if "edge_id" in arr["weights"].columns
            else None,
            "hrp_representatives": int(arr["weights"]["representative_id"].nunique()),
            "replay_starts": int(len(starts)),
            "result_rows": int(len(results)),
        },
        "outputs": {
            "walkthrough_run_results": "portfolio_allocator/outputs/final_candidate_walkthrough_outputs/walkthrough_run_results.csv",
            "walkthrough_summary_by_cost": "portfolio_allocator/outputs/final_candidate_walkthrough_outputs/walkthrough_summary_by_cost.csv",
            "selected_candidate_live_config": "portfolio_allocator/outputs/final_candidate_walkthrough_outputs/selected_candidate_live_config.json",
            "manifest": "portfolio_allocator/outputs/final_candidate_walkthrough_outputs/final_candidate_walkthrough_manifest.json",
        },
        "notes": [
            "Step 10 dynamically freezes and replays the actual top-ranked Script 09 candidate.",
            "The selected throttle uses scaled remaining overall-drawdown buffer, not absolute balance.",
            "Projected-buffer admission exactly reproduces the selected off/static/dynamic reserve contract.",
            "Optional daily-symbol-risk and floating-loss rules come only from active_account_rules.json.",
            "No timeout bucket is used; outcomes are pass, hard_fail, or end_of_history_fail.",
            "Broker min-lot/lot-step risk is required and used for PnL, open-risk caps, and failure checks.",
            "Walkthrough PASS requires zero hard failures, zero reserve violations, exact int64 replay, exact Script 09 starts, and a matching candidate contract.",
            "Final live approval still requires Step 11 Monte Carlo stress validation.",
        ],
    }
    if write:
        results.to_csv(OUTPUT_DIR / "walkthrough_run_results.csv", index=False)
        summary.to_csv(OUTPUT_DIR / "walkthrough_summary_by_cost.csv", index=False)
        write_json(OUTPUT_DIR / "selected_candidate_live_config.json", selected_config)
        write_json(OUTPUT_DIR / "final_candidate_walkthrough_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1),
        help="ProcessPoolExecutor workers for the replay grid (default: cpu_count-1). Pass 1 to force the original serial path.",
    )
    parser.add_argument(
        "--accept-moderate-hard-fail", action="store_true",
        help=f"Explicit human override mirroring Step 09's: pass even with nonzero hard-fail, as long as it stays <= {MODERATE_HARD_FAIL_TOLERANCE:.0%}. Logged clearly in the manifest.",
    )
    args = parser.parse_args()
    manifest = build(write=not args.dry_run, dry_run=args.dry_run, n_workers=int(args.workers), accept_moderate_hard_fail=bool(args.accept_moderate_hard_fail))
    variant = manifest["selected_variant"]
    print("V2 Phase B2.10 no-deadline final candidate walkthrough")
    print(f"Status: {manifest['walkthrough_status']}")
    print(
        "Account start: "
        f"balance=${CURRENT_BALANCE:,.2f}, "
        f"high-water=${ACCOUNT_START_TRAILING_HIGH_WATERMARK:,.2f}, "
        f"trailing floor=${ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR:,.2f}, "
        f"buffer=${ACCOUNT_START_REMAINING_BALANCE_BUFFER:,.2f}, "
        f"fresh-day floor=${ACCOUNT_START_DAILY_LOSS_FLOOR:,.2f}"
    )
    print(f"Selected variant: {manifest['selected_variant_label']}")
    print(f"Replay start source: {manifest['replay_start_source']}")
    print(f"Replay starts: {manifest['counts']['replay_starts']}")
    print(f"Account stage: {manifest['account_stage']}")
    print(
        "Challenge funded-symbol rule enforced: "
        f"{manifest['daily_symbol_risk_rule_enabled']}"
    )
    print(f"Selected throttle: {variant.get('throttle_name')}")
    print(f"Selected reserve mode: {variant.get('trailing_buffer_reserve_mode')}")
    print(
        "Selected reserve components: fixed=$"
        f"{float(variant.get('trailing_buffer_fixed_reserve_amount', 0.0) or 0.0):.2f}, "
        "cost_allowance_r="
        f"{float(variant.get('trailing_buffer_cost_allowance_r', 0.0) or 0.0):.4f}"
    )
    print(
        "Integrity: candidate_match="
        f"{manifest['walkthrough_integrity']['candidate_contract_match']}, "
        "exact_replay="
        f"{manifest['walkthrough_integrity']['exact_replay_source']}, "
        "hard_fail_count="
        f"{manifest['walkthrough_integrity']['hard_fail_count']}, "
        "reserve_violations="
        f"{manifest['walkthrough_integrity']['projected_buffer_reserve_violation_count']}, "
        "start_timestamp_mismatches="
        f"{manifest['walkthrough_integrity']['start_timestamp_mismatch_count']}, "
        "start_cost_mismatches="
        f"{manifest['walkthrough_integrity']['start_cost_mismatch_count']}"
    )
    print(
        "Broker risk policy: requested risk is reduced to the largest broker-valid "
        "lot inside every active cap; only zero capacity or an oversized minimum lot is skipped"
    )
    print("Outcome mode: no timeout; failed = hard_fail + end_of_history_fail")
    print("Output dir: portfolio_allocator/outputs/final_candidate_walkthrough_outputs")


if __name__ == "__main__":
    main()
