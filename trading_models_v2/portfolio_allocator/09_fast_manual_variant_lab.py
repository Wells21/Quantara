from __future__ import annotations

import argparse
import heapq
import json
import os
import re
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
    daily_drawdown_config,
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
SCRIPT_VERSION = "2026-07-21_generic_active_account_percent_buffer_cost_grid_guard"
PIPELINE_MODE = "v2_generic_active_account_single_manual_variant_tester"

INPUT_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
INPUT_HRP_WEIGHTS = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs" / "hrp_linkage_weights_step08_primary.csv"
STEP08_OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_rolling_optimizer_outputs"
INPUT_STEP08_MANIFEST = STEP08_OUTPUT_DIR / "prop_challenge_rolling_optimizer_manifest.json"
INPUT_STEP08_FAST_FRONTIER = STEP08_OUTPUT_DIR / "best_fast_frontier_scenario.csv"
INPUT_STEP08_FAST_SAFE = STEP08_OUTPUT_DIR / "best_fast_safe_scenario.csv"
INPUT_STEP08_ROBUST = STEP08_OUTPUT_DIR / "best_robust_scenario.csv"
INPUT_STEP08_JOINT = STEP08_OUTPUT_DIR / "best_joint_scenario.csv"
INPUT_STEP08_STAGE2_AGGREGATE = STEP08_OUTPUT_DIR / "stage2_aggregate.csv"
CANONICAL_OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_fast_manual_variant_lab_outputs"
QUICK_PROBE_OUTPUT_DIR = CANONICAL_OUTPUT_DIR / "quick_probe_latest"
TRIAL_HISTORY_CSV = CANONICAL_OUTPUT_DIR / "single_variant_trial_history.csv"
OUTPUT_DIR = CANONICAL_OUTPUT_DIR

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
MIN_REMAINING_OBSERVATION_DAYS_AFTER_START = 0
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

ACCOUNT_RULE_PROFILES = {
    ACCOUNT_RULE_PROFILE: {
        "account_stage": ACCOUNT_STAGE,
        "description": str(ACCOUNT_RULES.get("description") or ""),
        "daily_drawdown": daily_drawdown_config(ACCOUNT_RULES),
    }
}
DEFAULT_ACCOUNT_RULE_PROFILE = ACCOUNT_RULE_PROFILE

# Broker executable risk policy: target risk becomes broker-rounded lot risk.
# If ceil rounding exceeds the effective cap, volume backs off to the largest safe broker step.
# The variant rejects only when even the broker minimum lot cannot fit.
BROKER_VOLUME_ROUNDING_MODE = "ceil_then_backoff_to_largest_safe_step"
# Requested risk is always clipped to the tightest active cap, then converted to
# the largest broker-valid lot that fits. A trade is skipped only when the cap is
# zero/non-positive or even the broker minimum lot cannot fit inside it.
REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP = True
SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS = True
BROKER_RISK_SOURCE_POLICY = (
    "04B per-trade risk_per_1_lot derived from the 00A MT5 broker profile and "
    "00B causal historical profit-currency-to-account-currency conversion"
)

RISK_SIZING_MODE = "direct_weight_calibrated_afml_broker_executable"
AFML_BET_SIZE_COLUMN = "afml_bet_size"
AFML_SIZE_FLOOR = 0.0
AFML_SIZE_CAP = 1.0

# -----------------------------------------------------------------------------
# SINGLE MANUAL VARIANT — EDIT ONLY THIS BLOCK BETWEEN EXPERIMENTS
# -----------------------------------------------------------------------------
# quick_probe: 30 evenly distributed rolling starts, written to quick_probe_latest
#              and NEVER overwrites the canonical files consumed by Script 10.
# full_single: 120 rolling starts and all canonical outputs required by Script 10.
MANUAL_RUN_MODE = "quick_probe"  # quick_probe | full_single

# Manual guard experiment. Throttle thresholds and the fixed reserve are
# expressed as fractions of the account's INITIAL OVERALL DRAWDOWN BUFFER, not
# fixed dollar amounts and not a 100k-account reference. This keeps the same
# protective geometry across 5k, 10k, 100k, static and trailing accounts.
#
# Decimal examples:
#   0.675000 = 67.50% of the initial overall-DD buffer remains
#   0.003125 = 0.3125% of the initial overall-DD buffer is reserved
#
# Step 08 risk budget, single-trade cap, overlap limits, HRP weights and AFML
# sizing remain locked and cannot be changed here.
#
# This fallback is used ONLY when 08c_joint_risk_throttle_grid_search.py has
# never been run (no best_joint_variant_spec.json present) or that file is
# structurally invalid. When present, _load_active_variant_spec() below
# prefers 08c's discovered winner automatically, so the 08 -> 08c -> 09 chain
# runs unattended -- see that function's docstring.
_FALLBACK_ACTIVE_VARIANT_SPEC = {
    # Winner of 08c_joint_risk_throttle_grid_search.py's joint scan, RECHECKED
    # at full 400-rolling-start density (the initial 150-start scan was
    # slightly optimistic -- see trial history for the superseded
    # early_soft/0.3125%/0.05 and delayed_emergency/0.625%/0.15 attempts):
    # 70.5% base pass rate, hard-fail capped at 2.5% across ALL tested cost
    # stresses (1.0%/2.5%/2.25% at 0.00/0.02/0.05R), 18.0 median days to
    # target. Reached via Step 08's best_joint_scenario.csv (foundation
    # candidate pairwise_cap1p70_budget100_trade20: 100% budget, 0.20%
    # single-trade cap, cap1p70_diversified overlap), which
    # load_step08_selected() prefers automatically.
    "name": "joint_delayed_hard_dynamic_buffer125pct_cost15",
    "guard_profile": "delayed_hard",
    "trailing_buffer_admission_enabled": True,
    "trailing_buffer_reserve_mode": "dynamic_projected_open_risk",
    "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.0125,
    "trailing_buffer_cost_allowance_r": 0.15,
}

_REQUIRED_VARIANT_SPEC_FIELDS = {
    "name", "guard_profile", "trailing_buffer_admission_enabled",
    "trailing_buffer_reserve_mode", "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer",
    "trailing_buffer_cost_allowance_r",
}


def _load_active_variant_spec() -> dict[str, Any]:
    """Prefers 08c_joint_risk_throttle_grid_search.py's discovered winning
    guard/reserve combo (best_joint_variant_spec.json, written into Step 08's
    output dir) over the hand-edited _FALLBACK_ACTIVE_VARIANT_SPEC above.
    This is what lets 08 -> 08b -> 08c -> 09 -> 10 -> 11 run without manual
    intervention once 08c has produced a winner; the hardcoded fallback stays
    for when 08c hasn't been run yet, or its output is missing/invalid."""
    path = STEP08_OUTPUT_DIR / "best_joint_variant_spec.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and _REQUIRED_VARIANT_SPEC_FIELDS.issubset(payload.keys()):
                return payload
            print(f"WARNING: {path} is missing required fields; falling back to the hardcoded ACTIVE_VARIANT_SPEC.")
        except Exception as exc:
            print(f"WARNING: could not read {path} ({type(exc).__name__}: {exc}); falling back to the hardcoded ACTIVE_VARIANT_SPEC.")
    return _FALLBACK_ACTIVE_VARIANT_SPEC


ACTIVE_VARIANT_SPEC = _load_active_variant_spec()

FAST_PROBE_MODE = True
FAST_PROBE_USE_SELECTED_STEP08_RISK_SETTINGS = True
FAST_PROBE_RISK_SETTINGS_SOURCE = "selected_step08_exact_active_account_contract"
FAST_PROBE_VARIANTS = [ACTIVE_VARIANT_SPEC]

RUN_STAGE1 = False
STAGE1_START_EVERY_N_ACTIVE_DAYS = 12
STAGE1_MAX_ROLLING_STARTS = 100
STAGE1_COST_STRESS_R_PER_TRADE = [0.00, 0.02]
STAGE2_TOP_N_VARIANTS = 1
STAGE2_START_EVERY_N_ACTIVE_DAYS = 2
QUICK_PROBE_MAX_ROLLING_STARTS = 30
# Widened from 120 now that run_variant_set is parallelized (matches Step 08's
# density: every 2nd active day, up to 400 starts subsampled evenly if more
# are available -- see 08_rolling_start_prop_challenge_optimizer.py).
FULL_SINGLE_MAX_ROLLING_STARTS = 400
STAGE2_MAX_ROLLING_STARTS = QUICK_PROBE_MAX_ROLLING_STARTS
STAGE2_COST_STRESS_R_PER_TRADE = [0.00, 0.02, 0.05]
SELECTION_COSTS_FOR_HARD_FAIL_GATE = [0.00, 0.02, 0.05]
# Script 09 now evaluates 0.00R, 0.02R and 0.05R for approval. Script 10's
# established replay file still receives only 0.00R/0.02R rows; the all-cost
# file preserves the complete three-cost evidence.
DOWNSTREAM_REPLAY_COMPATIBILITY_COSTS = [0.00, 0.02]
RUN_BOOTSTRAP_ON_TOP_N = 0
BOOTSTRAP_SAMPLES = 0
BOOTSTRAP_RANDOM_SEED = 42
DRY_RUN = False
DRY_RUN_MAX_VARIANTS = 1
DRY_RUN_MAX_STARTS = 10
ACTIVE_RUN_MODE = MANUAL_RUN_MODE
EPS = 1e-12
DAY_NS = 24 * 60 * 60 * 1_000_000_000

OVERLAP_MODE_LIBRARY = {
    "balanced": {"max_total_open_risk_pct": 0.0200, "max_asset_open_risk_pct": 0.0100, "max_family_open_risk_pct": 0.0080, "max_edge_open_risk_pct": 0.0040, "max_open_trades_per_edge": 3, "max_open_trades_per_asset": 10},
    "cap1p50": {"max_total_open_risk_pct": 0.0150, "max_asset_open_risk_pct": 0.0120, "max_family_open_risk_pct": 0.0100, "max_edge_open_risk_pct": 0.0100, "max_open_trades_per_edge": 3, "max_open_trades_per_asset": 10},
    "cap2p00": {"max_total_open_risk_pct": 0.0200, "max_asset_open_risk_pct": 0.0120, "max_family_open_risk_pct": 0.0100, "max_edge_open_risk_pct": 0.0100, "max_open_trades_per_edge": 2, "max_open_trades_per_asset": 15},
    "cap2p50": {"max_total_open_risk_pct": 0.0250, "max_asset_open_risk_pct": 0.0150, "max_family_open_risk_pct": 0.0120, "max_edge_open_risk_pct": 0.0100, "max_open_trades_per_edge": 3, "max_open_trades_per_asset": 15},
    "cap3p00": {"max_total_open_risk_pct": 0.0300, "max_asset_open_risk_pct": 0.0180, "max_family_open_risk_pct": 0.0150, "max_edge_open_risk_pct": 0.0120, "max_open_trades_per_edge": 999, "max_open_trades_per_asset": 999},
    "tight1p00": {
        "max_total_open_risk_pct": 0.0100,
        "max_asset_open_risk_pct": 0.0060,
        "max_family_open_risk_pct": 0.0045,
        "max_edge_open_risk_pct": 0.0025,
        "max_open_trades_per_edge": 2,
        "max_open_trades_per_asset": 6,
    },
    "tight1p25": {
        "max_total_open_risk_pct": 0.0125,
        "max_asset_open_risk_pct": 0.0075,
        "max_family_open_risk_pct": 0.0055,
        "max_edge_open_risk_pct": 0.0030,
        "max_open_trades_per_edge": 2,
        "max_open_trades_per_asset": 7,
    },
    # Step 08 foundation-investigation modes. These values must match the
    # selected Step 08 candidate exactly; Step 09 validates the CSV contract
    # before constructing the manual guard variant.
    "cap1p50_relaxed_counts": {
        "max_total_open_risk_pct": 0.0150,
        "max_asset_open_risk_pct": 0.0120,
        "max_family_open_risk_pct": 0.0100,
        "max_edge_open_risk_pct": 0.0100,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p60_diversified": {
        "max_total_open_risk_pct": 0.0160,
        "max_asset_open_risk_pct": 0.0090,
        "max_family_open_risk_pct": 0.0075,
        "max_edge_open_risk_pct": 0.0035,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p70_diversified": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p80_diversified": {
        "max_total_open_risk_pct": 0.0180,
        "max_asset_open_risk_pct": 0.0100,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    # Added alongside Step 08's lower-total-open-risk probes -- must match
    # 08_rolling_start_prop_challenge_optimizer.py's OVERLAP_MODE_LIBRARY
    # entries of the same name exactly, or _make_variant_from_spec's drift
    # check (below) will fail closed the moment Step 08 selects one of these.
    "cap1p00_diversified": {
        "max_total_open_risk_pct": 0.0100,
        "max_asset_open_risk_pct": 0.0080,
        "max_family_open_risk_pct": 0.0065,
        "max_edge_open_risk_pct": 0.0025,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p20_diversified": {
        "max_total_open_risk_pct": 0.0120,
        "max_asset_open_risk_pct": 0.0090,
        "max_family_open_risk_pct": 0.0070,
        "max_edge_open_risk_pct": 0.0030,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    # 2026-09-25 max_open_trades_per_edge probes -- must match
    # 08_rolling_start_prop_challenge_optimizer.py's OVERLAP_MODE_LIBRARY
    # cap1p70_edge{1,2,4,6,8,10} entries exactly.
    "cap1p70_edge1": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 1,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge2": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 2,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge4": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 4,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge6": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 6,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge8": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 8,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge10": {
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 10,
        "max_open_trades_per_asset": 20,
    },
    # AFML Ch.10.2/10.4 concurrency-aware sizing modes -- must match Step 08's OVERLAP_MODE_LIBRARY exactly.
    "afmlconc_cap1p50": {
        "max_total_open_risk_pct": 0.0150,
        "max_asset_open_risk_pct": 0.0120,
        "max_family_open_risk_pct": 0.0100,
        "max_edge_open_risk_pct": 0.0100,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap2p00": {
        "max_total_open_risk_pct": 0.0200,
        "max_asset_open_risk_pct": 0.0160,
        "max_family_open_risk_pct": 0.0130,
        "max_edge_open_risk_pct": 0.0130,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap2p50": {
        "max_total_open_risk_pct": 0.0250,
        "max_asset_open_risk_pct": 0.0200,
        "max_family_open_risk_pct": 0.0160,
        "max_edge_open_risk_pct": 0.0160,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap3p00": {
        "max_total_open_risk_pct": 0.0300,
        "max_asset_open_risk_pct": 0.0240,
        "max_family_open_risk_pct": 0.0190,
        "max_edge_open_risk_pct": 0.0190,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap3p50": {
        "max_total_open_risk_pct": 0.0350,
        "max_asset_open_risk_pct": 0.0280,
        "max_family_open_risk_pct": 0.0220,
        "max_edge_open_risk_pct": 0.0220,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap4p00": {
        "max_total_open_risk_pct": 0.0400,
        "max_asset_open_risk_pct": 0.0320,
        "max_family_open_risk_pct": 0.0250,
        "max_edge_open_risk_pct": 0.0250,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
}
# Throttle state is the fraction of the INITIAL OVERALL DRAWDOWN BUFFER that
# remains after conservative open-stop-risk is deducted:
#
#   remaining_buffer = active breach value - active overall-DD floor
#   remaining_fraction = remaining_buffer / initial_overall_dd_buffer
#
# A threshold of 0.675 therefore means "activate when less than 67.5% of the
# original safety buffer remains." The old 100k/8%-DD calibration is preserved
# exactly, but the representation is now account-size and drawdown-rule neutral.
# Keep the downstream execution/replay basis identifier unchanged: Script 10,
# Script 11 and the live executor already understand this account-currency state.
THROTTLE_BASIS = "remaining_overall_dd_buffer_account_currency_equity_proxy"

# This separate field explains how the account-currency thresholds are configured.
THROTTLE_CONFIGURATION_BASIS = (
    "remaining_overall_dd_buffer_fraction_of_initial_overall_dd_buffer"
)
INITIAL_OVERALL_DD_BUFFER_AMOUNT = float(
    PROP_START_BALANCE - INITIAL_TRAILING_DRAWDOWN_FLOOR
)
if not np.isfinite(INITIAL_OVERALL_DD_BUFFER_AMOUNT) or INITIAL_OVERALL_DD_BUFFER_AMOUNT <= 0.0:
    raise ValueError(
        "Initial overall drawdown buffer must be positive for percentage-based "
        f"throttles, got {INITIAL_OVERALL_DD_BUFFER_AMOUNT!r}."
    )

THROTTLE_LIBRARY_FRACTION_OF_INITIAL_DD_BUFFER = {
    "no_throttle": [],
    "protective": [
        (0.6250, 0.75),
        (0.5000, 0.55),
        (0.3750, 0.35),
        (0.2500, 0.25),
        (0.1250, 0.15),
    ],
    "protective_deep_hard": [
        (0.6250, 0.75),
        (0.5000, 0.55),
        (0.3750, 0.35),
        (0.2500, 0.20),
        (0.1250, 0.10),
    ],
    "protective_extra_deep": [
        (0.6250, 0.70),
        (0.5000, 0.50),
        (0.3750, 0.30),
        (0.2500, 0.15),
        (0.1250, 0.08),
    ],
    "early_soft": [
        (0.6750, 0.90),
        (0.6250, 0.70),
        (0.5625, 0.55),
        (0.5000, 0.40),
        (0.3750, 0.28),
        (0.2500, 0.20),
        (0.1250, 0.10),
    ],
    "early_hard": [
        (0.6750, 0.65),
        (0.6250, 0.45),
        (0.5625, 0.30),
        (0.5000, 0.20),
        (0.3750, 0.12),
        (0.2500, 0.06),
        (0.1250, 0.03),
    ],
    "delayed_hard": [
        (0.3750, 0.65),
        (0.3125, 0.45),
        (0.2500, 0.30),
        (0.1875, 0.15),
        (0.1250, 0.06),
        (0.0625, 0.03),
    ],
    "delayed_emergency": [
        (0.3125, 0.70),
        (0.2500, 0.45),
        (0.1875, 0.25),
        (0.1250, 0.10),
        (0.0625, 0.03),
    ],
}

# The simulator still consumes account-currency thresholds internally so the core
# event loop is unchanged. They are materialized from the percentage library.
THROTTLE_LIBRARY = {
    name: [
        (
            float(remaining_fraction) * INITIAL_OVERALL_DD_BUFFER_AMOUNT,
            float(multiplier),
        )
        for remaining_fraction, multiplier in tiers
    ]
    for name, tiers in THROTTLE_LIBRARY_FRACTION_OF_INITIAL_DD_BUFFER.items()
}
TARGET_LOCK_LIBRARY = {
    "no_target_lock": [],
}
GUARD_PROFILE_LIBRARY = {
    "no_guard": {"throttle_name": "no_throttle", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "protective": {"throttle_name": "protective", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "protective_deep_hard": {"throttle_name": "protective_deep_hard", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "protective_extra_deep": {"throttle_name": "protective_extra_deep", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "early_soft": {"throttle_name": "early_soft", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "early_hard": {"throttle_name": "early_hard", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "delayed_hard": {"throttle_name": "delayed_hard", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
    "delayed_emergency": {"throttle_name": "delayed_emergency", "pause_below_balance": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR, "target_lock_name": "no_target_lock"},
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required JSON input: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return payload


def configure_run_mode(mode: str) -> None:
    """Select quick-probe or canonical full-single output without changing simulation logic."""
    global ACTIVE_RUN_MODE, OUTPUT_DIR, STAGE2_MAX_ROLLING_STARTS
    normalized = str(mode).strip().lower()
    if normalized not in {"quick_probe", "full_single"}:
        raise ValueError(f"Unknown run mode {mode!r}; use 'quick_probe' or 'full_single'.")
    ACTIVE_RUN_MODE = normalized
    if normalized == "quick_probe":
        OUTPUT_DIR = QUICK_PROBE_OUTPUT_DIR
        STAGE2_MAX_ROLLING_STARTS = QUICK_PROBE_MAX_ROLLING_STARTS
    else:
        OUTPUT_DIR = CANONICAL_OUTPUT_DIR
        STAGE2_MAX_ROLLING_STARTS = FULL_SINGLE_MAX_ROLLING_STARTS


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CANONICAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(V2_ROOT.resolve()))
    except ValueError:
        return str(path)


def safe_div(a: Any, b: Any) -> float:
    try:
        if b is None or pd.isna(b) or abs(float(b)) < EPS:
            return np.nan
        return float(a) / float(b)
    except Exception:
        return np.nan


def safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    text_value = str(value).strip().lower()
    if text_value in {"true", "1", "yes", "y"}:
        return True
    if text_value in {"false", "0", "no", "n"}:
        return False
    return default


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    out = float(value)
    return out if np.isfinite(out) else None


def risk_label(x: float) -> str:
    return f"{float(x) * 100:.4f}".replace(".", "p")


def ns_to_ts(ns: int | None) -> pd.Timestamp:
    return to_datetime_iso(int(ns), utc=True) if ns is not None and not pd.isna(ns) else pd.NaT


def day_from_ns(ns: int) -> int:
    return int((int(ns) + DAILY_RESET_OFFSET_NS) // DAY_NS)


def normalize_cost(x: float) -> str:
    return "base" if abs(float(x)) < EPS else f"cost{float(x):.2f}".replace(".", "p")


def slug(text: str) -> str:
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


def daily_loss_floor(
    daily_start_balance: float,
    daily_start_equity: float | None = None,
) -> float:
    return account_daily_drawdown_floor(
        ACCOUNT_RULES,
        day_start_balance=daily_start_balance,
        day_start_equity=daily_start_balance if daily_start_equity is None else daily_start_equity,
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



def multiplier_from_value(value: float, tiers: list[tuple[float, float]], mode: str) -> float:
    """Return the most protective multiplier triggered by a scalar state value."""
    if not tiers:
        return 1.0
    if not np.isfinite(value):
        raise ValueError(f"Multiplier input must be finite, got {value!r}")
    mult = 1.0
    if mode == "below":
        for threshold, tier_mult in tiers:
            if value < float(threshold):
                mult = min(mult, float(tier_mult))
    elif mode == "above":
        for threshold, tier_mult in tiers:
            if value >= float(threshold):
                mult = min(mult, float(tier_mult))
    else:
        raise ValueError(f"Unknown multiplier mode: {mode}")
    return float(np.clip(mult, 0.0, 1.0))


def current_trailing_buffer(balance: float, open_risk_total: float, trailing_high_watermark: float) -> float:
    """Conservative distance between equity proxy and the live trailing drawdown floor."""
    return float(drawdown_stats(balance, open_risk_total, trailing_high_watermark)["remaining_dd_buffer"])


TRAILING_BUFFER_RESERVE_MODES = {
    "off",
    "static",
    "dynamic_projected_open_risk",
}


def required_trailing_buffer_reserve(
    projected_open_risk_total: float,
    reserve_mode: str,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Required post-entry buffer for the configured static or exposure-aware reserve."""
    mode = str(reserve_mode)
    fixed = max(0.0, float(fixed_reserve_amount))
    allowance = max(0.0, float(cost_allowance_r))
    projected_open = max(0.0, float(projected_open_risk_total))
    if mode == "off":
        return 0.0
    if mode == "static":
        return fixed
    if mode == "dynamic_projected_open_risk":
        return fixed + allowance * projected_open
    raise ValueError(f"Unknown trailing-buffer reserve mode: {mode!r}")


def maximum_admissible_new_risk(
    trailing_buffer: float,
    current_open_risk_total: float,
    reserve_mode: str,
    fixed_reserve_amount: float,
    cost_allowance_r: float,
) -> float:
    """Solve the exact pre-rounding new-risk ceiling imposed by the reserve formula.

    For dynamic reserve:
        buffer - x >= fixed + allowance * (current_open + x)
    therefore:
        x <= (buffer - fixed - allowance * current_open) / (1 + allowance)
    """
    mode = str(reserve_mode)
    buffer = float(trailing_buffer)
    current_open = max(0.0, float(current_open_risk_total))
    fixed = max(0.0, float(fixed_reserve_amount))
    allowance = max(0.0, float(cost_allowance_r))
    if mode == "off":
        return np.inf
    if mode == "static":
        return buffer - fixed
    if mode == "dynamic_projected_open_risk":
        return (buffer - fixed - allowance * current_open) / (1.0 + allowance)
    raise ValueError(f"Unknown trailing-buffer reserve mode: {mode!r}")



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

def daily_drawdown_from_selected(selected: dict[str, Any]) -> dict[str, Any]:
    """Inherit the current generic account contract emitted by Step 08."""
    profile = str(selected.get("account_rule_profile") or "").strip()
    account_stage = str(selected.get("account_stage") or "").strip().lower()
    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    symbol_rule_enabled = safe_bool(
        selected.get("daily_symbol_risk_rule_enabled"),
        default=bool(expected_symbol["enabled"]),
    )
    enforcement_mode = str(
        selected.get("daily_symbol_risk_enforcement_mode")
        or expected_symbol["enforcement_mode"]
    ).strip()

    if profile != ACCOUNT_RULE_PROFILE:
        raise ValueError(
            f"Step 09 requires account_rule_profile={ACCOUNT_RULE_PROFILE!r}; "
            f"received {profile!r}. Rerun Script 08 after editing active_account_rules.json."
        )
    if account_stage != ACCOUNT_STAGE:
        raise ValueError(
            f"Step 09 requires account_stage={ACCOUNT_STAGE!r}; received {account_stage!r}."
        )
    if symbol_rule_enabled != bool(expected_symbol["enabled"]):
        raise ValueError(
            "Step 08 daily-symbol-risk enabled state does not match active_account_rules.json."
        )
    if enforcement_mode != str(expected_symbol["enforcement_mode"]):
        raise ValueError(
            "Step 08 daily-symbol-risk enforcement mode does not match active_account_rules.json: "
            f"{enforcement_mode!r} != {expected_symbol['enforcement_mode']!r}."
        )

    cfg = daily_drawdown_config(ACCOUNT_RULES)
    cfg["name"] = str(selected.get("daily_drawdown_name") or cfg["name"])
    cfg["enabled"] = safe_bool(
        selected.get("daily_drawdown_enabled"), default=bool(cfg["enabled"])
    )
    cfg["pct"] = float(selected.get("daily_drawdown_pct", cfg["pct"]) or 0.0)
    cfg["amount"] = float(selected.get("daily_drawdown_amount", cfg["amount"]) or 0.0)
    cfg["mode"] = str(selected.get("daily_drawdown_mode") or cfg["mode"])
    cfg["breach_basis"] = str(
        selected.get("daily_drawdown_breach_basis") or cfg.get("breach_basis")
    )
    return cfg

def variant_label(v: dict[str, Any]) -> str:
    return f"{v['name']}__budget{risk_label(v['base_portfolio_risk_budget_pct'])}pct__singlecap{risk_label(v['max_single_trade_risk_pct'])}pct"


def validate_variant(v: dict[str, Any]) -> None:
    required = ["name", "base_portfolio_risk_budget_pct", "max_single_trade_risk_pct", "max_min_lot_risk_inflation", "cap_name", "max_total_open_risk_pct", "max_asset_open_risk_pct", "max_family_open_risk_pct", "max_edge_open_risk_pct", "max_open_trades_per_edge", "max_open_trades_per_asset", "guard_profile", "throttle_name", "throttle_configuration_basis", "throttle_reference_initial_overall_dd_buffer_amount", "throttle_tiers_fraction_of_initial_dd_buffer", "throttle_tiers", "trailing_buffer_admission_enabled", "trailing_buffer_reserve_mode", "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer", "trailing_buffer_fixed_reserve_amount", "trailing_buffer_cost_allowance_r", "trailing_buffer_reserve_amount", "trailing_buffer_admission_policy", "daily_buffer_admission_enabled", "daily_buffer_reserve_mode", "daily_buffer_fixed_reserve_pct_of_initial_dd_buffer", "daily_buffer_fixed_reserve_amount", "daily_buffer_cost_allowance_r", "daily_buffer_admission_policy", "pause_below_balance", "target_lock_name", "target_lock_tiers", "account_rule_profile", "account_stage", "daily_drawdown_name", "daily_drawdown_enabled", "daily_drawdown_pct", "daily_drawdown_mode", "daily_symbol_risk_rule_enabled", "daily_symbol_risk_enforcement_mode"]
    missing = [x for x in required if x not in v]
    if missing:
        raise ValueError(f"Variant {v.get('name', '<unnamed>')} missing fields: {missing}")
    for field in ["base_portfolio_risk_budget_pct", "max_single_trade_risk_pct", "max_total_open_risk_pct", "max_asset_open_risk_pct", "max_family_open_risk_pct", "max_edge_open_risk_pct"]:
        if not (0.0 <= float(v[field]) <= 1.0):
            raise ValueError(f"Variant {v['name']} has invalid {field}: {v[field]}")
    max_min_lot_inflation = optional_float(
        v.get("max_min_lot_risk_inflation")
    )
    if max_min_lot_inflation is not None and max_min_lot_inflation < 1.0 - EPS:
        raise ValueError(
            f"Variant {v['name']} has invalid max_min_lot_risk_inflation="
            f"{max_min_lot_inflation}; use None/off or a value >= 1.0."
        )
    if bool(v.get("min_lot_risk_inflation_guard_enabled", False)) != bool(
        max_min_lot_inflation is not None
    ):
        raise ValueError(
            f"Variant {v['name']} minimum-lot guard flag does not match its threshold."
        )
    if str(v["account_rule_profile"]) != ACCOUNT_RULE_PROFILE:
        raise ValueError(f"Variant {v['name']} has wrong account_rule_profile={v['account_rule_profile']!r}")
    if str(v["account_stage"]).lower() != ACCOUNT_STAGE:
        raise ValueError(f"Variant {v['name']} must use account_stage={ACCOUNT_STAGE!r}.")
    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    if safe_bool(v["daily_symbol_risk_rule_enabled"], default=False) != bool(expected_symbol["enabled"]):
        raise ValueError(f"Variant {v['name']} daily-symbol-risk enabled state differs from active rules.")
    if str(v["daily_symbol_risk_enforcement_mode"]) != str(expected_symbol["enforcement_mode"]):
        raise ValueError(f"Variant {v['name']} has invalid daily symbol-risk enforcement mode.")
    expected_symbol_fields = {
        "max_daily_risk_per_symbol_pct": expected_symbol.get("max_pct"),
        "max_daily_risk_per_symbol_amount": expected_symbol.get("max_amount"),
        "internal_operating_risk_per_symbol_pct": expected_symbol.get("internal_operating_pct"),
        "internal_operating_risk_per_symbol_amount": expected_symbol.get("internal_operating_amount"),
    }
    for field, expected in expected_symbol_fields.items():
        actual = optional_float(v.get(field))
        if expected is None:
            if actual is not None:
                raise ValueError(f"Variant {v['name']} must leave {field}=None when the rule is disabled.")
        elif actual is None or not np.isclose(actual, float(expected), rtol=0.0, atol=EPS):
            raise ValueError(f"Variant {v['name']} {field}={actual!r} != active rule {expected!r}.")
    if PROP_MAX_SINGLE_TRADE_RISK_PCT is not None and float(v["max_single_trade_risk_pct"]) > PROP_MAX_SINGLE_TRADE_RISK_PCT + EPS:
        raise ValueError(
            f"Variant {v['name']} single-trade cap exceeds the active prop maximum: "
            f"{v['max_single_trade_risk_pct']} > {PROP_MAX_SINGLE_TRADE_RISK_PCT}."
        )
    if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
        guard_pct = FLOATING_LOSS_GUARD_AMOUNT / PROP_START_BALANCE
        if float(v["max_total_open_risk_pct"]) > guard_pct + EPS:
            raise ValueError(
                f"Variant {v['name']} total-open-risk cap exceeds the active floating-loss guard: "
                f"{v['max_total_open_risk_pct']} > {guard_pct}."
            )
    admission_enabled = bool(v["trailing_buffer_admission_enabled"])
    reserve_mode = str(v["trailing_buffer_reserve_mode"])
    fixed_reserve = float(v["trailing_buffer_fixed_reserve_amount"])
    cost_allowance = float(v["trailing_buffer_cost_allowance_r"])
    legacy_reserve = float(v["trailing_buffer_reserve_amount"])
    if reserve_mode not in TRAILING_BUFFER_RESERVE_MODES:
        raise ValueError(
            f"Variant {v['name']} has invalid trailing_buffer_reserve_mode={reserve_mode!r}. "
            f"Available: {sorted(TRAILING_BUFFER_RESERVE_MODES)}"
        )
    for field, value in [
        ("trailing_buffer_fixed_reserve_amount", fixed_reserve),
        ("trailing_buffer_cost_allowance_r", cost_allowance),
        ("trailing_buffer_reserve_amount", legacy_reserve),
    ]:
        if not np.isfinite(value) or value < 0.0:
            raise ValueError(f"Variant {v['name']} has invalid {field}: {value}")
    if not admission_enabled:
        if reserve_mode != "off" or fixed_reserve > EPS or cost_allowance > EPS:
            raise ValueError(
                f"Variant {v['name']} disables admission but has an active reserve formula."
            )
    else:
        if reserve_mode == "off":
            raise ValueError(
                f"Variant {v['name']} enables admission but reserve mode is 'off'."
            )
        if reserve_mode == "static" and cost_allowance > EPS:
            raise ValueError(
                f"Variant {v['name']} static reserve cannot use a positive cost allowance."
            )
        if reserve_mode == "dynamic_projected_open_risk" and cost_allowance <= EPS:
            raise ValueError(
                f"Variant {v['name']} dynamic reserve requires a positive cost allowance."
            )
    if not np.isclose(legacy_reserve, fixed_reserve, rtol=0.0, atol=EPS):
        raise ValueError(
            f"Variant {v['name']} legacy reserve alias must equal the fixed reserve component."
        )
    # The daily guard deliberately mirrors the selected trailing reserve so it
    # adds no independent tuning degree of freedom.
    if bool(v["daily_buffer_admission_enabled"]) != bool(admission_enabled):
        raise ValueError(f"Variant {v['name']} daily admission must mirror trailing admission.")
    if str(v["daily_buffer_reserve_mode"]) != reserve_mode:
        raise ValueError(f"Variant {v['name']} daily reserve mode must mirror trailing reserve mode.")
    if not np.isclose(float(v["daily_buffer_fixed_reserve_amount"]), fixed_reserve, rtol=0.0, atol=EPS):
        raise ValueError(f"Variant {v['name']} daily fixed reserve must mirror trailing fixed reserve.")
    if not np.isclose(float(v["daily_buffer_cost_allowance_r"]), cost_allowance, rtol=0.0, atol=EPS):
        raise ValueError(f"Variant {v['name']} daily cost allowance must mirror trailing cost allowance.")
    fixed_reserve_pct = float(
        v["trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer"]
    )
    if not np.isfinite(fixed_reserve_pct) or not (0.0 <= fixed_reserve_pct <= 1.0):
        raise ValueError(
            f"Variant {v['name']} has invalid fixed reserve fraction: "
            f"{fixed_reserve_pct}"
        )
    expected_fixed_reserve = (
        fixed_reserve_pct * INITIAL_OVERALL_DD_BUFFER_AMOUNT
    )
    if not np.isclose(
        fixed_reserve, expected_fixed_reserve, rtol=0.0, atol=EPS
    ):
        raise ValueError(
            f"Variant {v['name']} fixed reserve amount does not match its "
            "percentage-of-initial-DD-buffer setting."
        )
    if not np.isclose(
        float(v["daily_buffer_fixed_reserve_pct_of_initial_dd_buffer"]),
        fixed_reserve_pct if bool(v["daily_drawdown_enabled"]) else 0.0,
        rtol=0.0,
        atol=EPS,
    ):
        raise ValueError(
            f"Variant {v['name']} daily fixed-reserve percentage must mirror "
            "the overall-buffer reserve percentage."
        )
    if str(v["throttle_basis"]) != THROTTLE_BASIS:
        raise ValueError(
            f"Variant {v['name']} has unsupported throttle execution basis: "
            f"{v['throttle_basis']!r}"
        )
    if str(v["throttle_configuration_basis"]) != THROTTLE_CONFIGURATION_BASIS:
        raise ValueError(
            f"Variant {v['name']} has unsupported throttle configuration basis: "
            f"{v['throttle_configuration_basis']!r}"
        )
    reference_buffer = float(
        v["throttle_reference_initial_overall_dd_buffer_amount"]
    )
    if not np.isclose(
        reference_buffer,
        INITIAL_OVERALL_DD_BUFFER_AMOUNT,
        rtol=0.0,
        atol=EPS,
    ):
        raise ValueError(
            f"Variant {v['name']} uses the wrong throttle reference buffer."
        )
    throttle_fraction_tiers = list(
        v["throttle_tiers_fraction_of_initial_dd_buffer"]
    )
    for remaining_fraction, multiplier in throttle_fraction_tiers:
        if not (0.0 <= float(remaining_fraction) <= 1.0):
            raise ValueError(
                f"Variant {v['name']} has invalid throttle fraction: "
                f"{remaining_fraction}"
            )
        if not (0.0 <= float(multiplier) <= 1.0):
            raise ValueError(
                f"Variant {v['name']} has invalid throttle multiplier: "
                f"{multiplier}"
            )
    expected_amount_tiers = [
        (
            float(remaining_fraction) * INITIAL_OVERALL_DD_BUFFER_AMOUNT,
            float(multiplier),
        )
        for remaining_fraction, multiplier in throttle_fraction_tiers
    ]
    if len(expected_amount_tiers) != len(v["throttle_tiers"]) or any(
        not (
            np.isclose(float(actual[0]), float(expected[0]), rtol=0.0, atol=EPS)
            and np.isclose(float(actual[1]), float(expected[1]), rtol=0.0, atol=EPS)
        )
        for actual, expected in zip(v["throttle_tiers"], expected_amount_tiers)
    ):
        raise ValueError(
            f"Variant {v['name']} materialized throttle amounts do not match "
            "their percentage tiers."
        )
    throttle_tiers = list(v["throttle_tiers"])
    for threshold, multiplier in throttle_tiers:
        if not np.isfinite(float(threshold)) or float(threshold) < 0.0:
            raise ValueError(f"Variant {v['name']} has invalid trailing-buffer threshold: {threshold}")
        if not (0.0 <= float(multiplier) <= 1.0):
            raise ValueError(f"Variant {v['name']} has invalid throttle multiplier: {multiplier}")
    if throttle_tiers:
        thresholds = [float(x[0]) for x in throttle_tiers]
        multipliers = [float(x[1]) for x in throttle_tiers]
        if any(a <= b for a, b in zip(thresholds, thresholds[1:])):
            raise ValueError(
                f"Variant {v['name']} throttle thresholds must be strictly descending: {thresholds}"
            )
        if any(a < b - EPS for a, b in zip(multipliers, multipliers[1:])):
            raise ValueError(
                f"Variant {v['name']} throttle multipliers must not increase as buffer falls: {multipliers}"
            )


def read_first_row_csv(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return None
    if df.empty:
        return None
    return df.iloc[0].to_dict()




def load_step08_manifest_contract() -> dict[str, Any]:
    """Validate the completed Step 08 manifest against the active hashless rules."""
    payload = read_json_object(INPUT_STEP08_MANIFEST)
    settings = payload.get("settings", {})
    if payload.get("rolling_optimizer_status") != "PASS":
        raise ValueError(
            f"Step 08 manifest is not PASS: {payload.get('rolling_optimizer_status')!r}"
        )
    upstream_rules = payload.get("active_account_rules") or settings.get("active_account_rules")
    if not structurally_equal(upstream_rules, ACCOUNT_RULE_CONTRACT):
        raise ValueError(
            "Step 08 active account rules differ from the current active_account_rules.json. "
            "Rerun Script 08 before Script 09."
        )
    if str(settings.get("account_rule_profile", "")) != ACCOUNT_RULE_PROFILE:
        raise ValueError("Step 08 account-rule profile does not match the active rules.")
    if str(settings.get("account_stage", "")).lower() != ACCOUNT_STAGE:
        raise ValueError("Step 08 account stage does not match the active rules.")
    expected_symbol = ACCOUNT_RULES["daily_symbol_risk"]
    if safe_bool(settings.get("daily_symbol_risk_rule_enabled"), default=False) != bool(expected_symbol["enabled"]):
        raise ValueError("Step 08 daily-symbol-risk enabled state does not match active rules.")
    if str(settings.get("daily_symbol_risk_enforcement_mode", "")) != str(expected_symbol["enforcement_mode"]):
        raise ValueError("Step 08 daily-symbol-risk enforcement mode does not match active rules.")
    reset_offset = optional_float(settings.get("daily_reset_offset_hours"))
    if reset_offset is None:
        raise ValueError("Step 08 manifest is missing daily_reset_offset_hours.")
    if not np.isclose(reset_offset, DAILY_RESET_OFFSET_HOURS, rtol=0.0, atol=EPS):
        raise ValueError(
            "Step 09 daily reset offset does not match Step 08: "
            f"{DAILY_RESET_OFFSET_HOURS} != {reset_offset}."
        )
    state_checks = {
        "current_balance": CURRENT_BALANCE,
        "account_start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "account_start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
    }
    if not safe_bool(settings.get("reduce_trade_to_fit_effective_risk_cap"), default=False):
        raise ValueError("Step 08 must reduce requested risk to the effective cap before broker rounding.")
    if not safe_bool(settings.get("skip_trade_if_no_executable_lot_fits"), default=False):
        raise ValueError("Step 08 must fail closed when no positive broker-valid lot fits the cap.")
    if str(settings.get("broker_risk_source_policy", "")) != BROKER_RISK_SOURCE_POLICY:
        raise ValueError("Step 08 broker/currency risk source policy mismatch.")
    for field, expected in state_checks.items():
        actual = optional_float(settings.get(field))
        if actual is None or not np.isclose(actual, expected, rtol=0.0, atol=EPS):
            raise ValueError(f"Step 09 account-state mismatch for {field}: {actual!r} != {expected}")
    active_profiles = settings.get("active_account_rule_profiles", [])
    if ACCOUNT_RULE_PROFILE not in list(active_profiles or []):
        raise ValueError("Step 08 manifest does not list the current account profile as active.")
    return {
        "manifest_path": rel_path(INPUT_STEP08_MANIFEST),
        "pipeline_mode": payload.get("pipeline_mode"),
        "rolling_optimizer_status": payload.get("rolling_optimizer_status"),
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "account_rule_profile": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "current_balance": CURRENT_BALANCE,
        "account_start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "account_start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "reduce_trade_to_fit_effective_risk_cap": True,
        "skip_trade_if_no_executable_lot_fits": True,
        "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
        "daily_reset_timezone": settings.get("daily_reset_timezone"),
        "daily_reset_offset_hours": reset_offset,
        "daily_symbol_risk_rule_enabled": bool(expected_symbol["enabled"]),
        "daily_symbol_risk_enforcement_mode": str(expected_symbol["enforcement_mode"]),
    }

_STEP08_CANDIDATE_REQUIRED_FIELDS = [
    "scenario_id", "return_mode", "universe", "profile_role", "linkage_method",
    "base_portfolio_risk_budget_pct", "max_single_trade_risk_pct", "overlap_mode",
    "max_total_open_risk_pct", "max_asset_open_risk_pct",
    "max_family_open_risk_pct", "max_edge_open_risk_pct",
    "max_open_trades_per_edge", "max_open_trades_per_asset",
    "account_rule_profile", "account_stage", "daily_symbol_risk_rule_enabled",
    "daily_symbol_risk_enforcement_mode",
]


def _normalize_step08_candidate_row(row: dict[str, Any], kind: str, source: str) -> dict[str, Any] | str:
    """Shared normalization for a raw Step08 candidate row, however it was
    located (one of the best_*.csv files, or a by-name lookup in
    stage2_aggregate.csv for a manual override). Returns the normalized row,
    or an error string if the row is unusable."""
    missing = [c for c in _STEP08_CANDIDATE_REQUIRED_FIELDS if c not in row or pd.isna(row[c])]
    if missing:
        return f"{kind}: missing_active_account_contract_fields={missing}"
    try:
        daily_cfg = daily_drawdown_from_selected(row)
    except Exception as exc:
        return f"{kind}: active_account_contract_invalid={type(exc).__name__}:{exc}"

    row["account_rule_profile"] = daily_cfg["account_rule_profile"]
    row["account_stage"] = daily_cfg["account_stage"]
    row["daily_drawdown_name"] = daily_cfg["name"]
    row["daily_drawdown_enabled"] = bool(daily_cfg["enabled"])
    row["daily_drawdown_pct"] = float(daily_cfg["pct"])
    row["daily_drawdown_amount"] = float(daily_cfg["amount"])
    row["daily_drawdown_mode"] = str(daily_cfg["mode"])
    row["daily_drawdown_breach_basis"] = str(daily_cfg.get("breach_basis", ACCOUNT_RULES["daily_drawdown"]["breach_basis"]))
    row["daily_symbol_risk_rule_enabled"] = bool(daily_cfg["daily_symbol_risk_rule_enabled"])
    row["daily_symbol_risk_enforcement_mode"] = str(daily_cfg["daily_symbol_risk_enforcement_mode"])
    row["max_daily_risk_per_symbol_pct"] = daily_cfg.get("max_daily_risk_per_symbol_pct")
    row["max_daily_risk_per_symbol_amount"] = daily_cfg.get("max_daily_risk_per_symbol_amount")
    row["internal_operating_risk_per_symbol_pct"] = daily_cfg.get("internal_operating_risk_per_symbol_pct")
    row["internal_operating_risk_per_symbol_amount"] = daily_cfg.get("internal_operating_risk_per_symbol_amount")
    row["symbol_risk_safety_reserve_amount"] = daily_cfg.get("symbol_risk_safety_reserve_amount")
    # Foundation-investigation contract. NaN/blank means the selected Step 08
    # source deliberately had no minimum-lot inflation guard.
    row["max_min_lot_risk_inflation"] = optional_float(row.get("max_min_lot_risk_inflation"))
    row["min_lot_risk_inflation_guard_enabled"] = bool(row["max_min_lot_risk_inflation"] is not None)
    row["afml_concurrency_quantile"] = AFMLC.validate_quantile(row.get("afml_concurrency_quantile"))
    row["step08_candidate_kind"] = kind
    row["step08_candidate_file"] = source
    return row


def load_step08_selected() -> tuple[dict[str, Any], str]:
    candidates = [
        # 08c's joint risk/throttle grid search (if it has been run) found a
        # Step08 candidate that performs best once a Step 09 guard/reserve is
        # layered on top -- prefer it over Step 08's own throttle-blind
        # robust/fast_safe/fast_frontier picks when present.
        ("joint_search", INPUT_STEP08_JOINT),
        ("fast_frontier", INPUT_STEP08_FAST_FRONTIER),
        ("fast_safe", INPUT_STEP08_FAST_SAFE),
        ("robust", INPUT_STEP08_ROBUST),
    ]
    errors: list[str] = []
    for kind, path in candidates:
        row = read_first_row_csv(path)
        if row is None:
            errors.append(f"{kind}: missing_or_empty={path}")
            continue
        result = _normalize_step08_candidate_row(row, kind, rel_path(path))
        if isinstance(result, str):
            errors.append(result)
            continue
        return result, kind
    raise RuntimeError(
        "No valid current-account Step 08 candidate found. Checked: " + "; ".join(errors)
    )


def load_step08_candidate_by_name(name: str) -> dict[str, Any]:
    """Explicit human override (see --manual-step08-candidate): looks up ANY
    candidate Step 08 tested -- not just its own best_robust/fast_safe/
    fast_frontier picks or 08c's joint-search winner -- by
    foundation_candidate_name in stage2_aggregate.csv, so a human reviewing
    08c's full results (or Step 08's own) can pick a different one to send
    through Step 09/10/11 despite what the automated search selected."""
    if not INPUT_STEP08_STAGE2_AGGREGATE.exists():
        raise FileNotFoundError(f"Missing {INPUT_STEP08_STAGE2_AGGREGATE}; run Step 08 first.")
    df = pd.read_csv(INPUT_STEP08_STAGE2_AGGREGATE, float_precision="round_trip")
    base = df[df["cost_stress_r"] == 0.0].copy()
    matches = base[base["foundation_candidate_name"].astype(str) == str(name)]
    if matches.empty:
        available = sorted(base["foundation_candidate_name"].astype(str).unique())
        raise ValueError(f"--manual-step08-candidate {name!r} not found in Step 08's results. Available: {available}")
    row = matches.iloc[0].to_dict()
    result = _normalize_step08_candidate_row(row, "manual_override", rel_path(INPUT_STEP08_STAGE2_AGGREGATE))
    if isinstance(result, str):
        raise RuntimeError(f"--manual-step08-candidate {name!r} failed validation: {result}")
    return result


def rounded_unique(values: list[float], min_value: float, max_value: float) -> list[float]:
    out = sorted({round(float(np.clip(v, min_value, max_value)), 6) for v in values if np.isfinite(v)})
    return [x for x in out if x > EPS]


def _make_variant_from_spec(spec: dict[str, Any], selected: dict[str, Any], daily_cfg: dict[str, Any]) -> dict[str, Any]:
    """Build one validated broker/account-profile-aware variant from a small manual spec."""
    selected_budget = float(selected["base_portfolio_risk_budget_pct"])
    selected_single_cap = float(selected["max_single_trade_risk_pct"])
    selected_overlap = str(selected.get("overlap_mode") or "balanced")
    selected_min_lot_inflation = optional_float(
        selected.get("max_min_lot_risk_inflation")
    )
    guard_name = str(spec.get("guard_profile") or "no_guard")

    if FAST_PROBE_MODE and FAST_PROBE_USE_SELECTED_STEP08_RISK_SETTINGS:
        # Deliberately ignore any accidental hard-coded risk values in a probe spec.
        # This prevents Step 09 from silently drifting away from the Step 08 winner.
        cap_name = selected_overlap
        budget = selected_budget
        single_cap = selected_single_cap
        max_min_lot_risk_inflation = selected_min_lot_inflation
        risk_settings_source = FAST_PROBE_RISK_SETTINGS_SOURCE
    else:
        cap_name = str(spec.get("cap_name") or selected_overlap)
        budget = float(spec.get("base_portfolio_risk_budget_pct", selected_budget))
        single_cap = float(spec.get("max_single_trade_risk_pct", selected_single_cap))
        max_min_lot_risk_inflation = optional_float(
            spec.get("max_min_lot_risk_inflation", selected_min_lot_inflation)
        )
        risk_settings_source = "manual_or_generated_variation"

    if PROP_MAX_SINGLE_TRADE_RISK_PCT is not None:
        single_cap = min(single_cap, PROP_MAX_SINGLE_TRADE_RISK_PCT)

    if cap_name not in OVERLAP_MODE_LIBRARY:
        raise ValueError(
            f"Selected/manual overlap mode {cap_name!r} is not available. "
            f"Available: {sorted(OVERLAP_MODE_LIBRARY)}"
        )
    if guard_name not in GUARD_PROFILE_LIBRARY:
        raise ValueError(
            f"FAST_PROBE_VARIANT has unknown guard_profile={guard_name!r}. "
            f"Available: {sorted(GUARD_PROFILE_LIBRARY)}"
        )

    cap = OVERLAP_MODE_LIBRARY[cap_name]
    # The selected Step 08 CSV carries the exact overlap limits used in the
    # foundation run. Fail closed if the local library drifts from that source.
    if FAST_PROBE_MODE and FAST_PROBE_USE_SELECTED_STEP08_RISK_SETTINGS:
        overlap_float_fields = [
            "max_total_open_risk_pct",
            "max_asset_open_risk_pct",
            "max_family_open_risk_pct",
            "max_edge_open_risk_pct",
        ]
        overlap_int_fields = [
            "max_open_trades_per_edge",
            "max_open_trades_per_asset",
        ]
        for field in overlap_float_fields:
            source_value = optional_float(selected.get(field))
            if source_value is None or not np.isclose(
                float(cap[field]), source_value, rtol=0.0, atol=EPS
            ):
                raise ValueError(
                    f"Step 09 overlap library drift for {cap_name}.{field}: "
                    f"{cap[field]!r} != Step08 {source_value!r}"
                )
        for field in overlap_int_fields:
            source_value = selected.get(field)
            if source_value is None or int(cap[field]) != int(source_value):
                raise ValueError(
                    f"Step 09 overlap library drift for {cap_name}.{field}: "
                    f"{cap[field]!r} != Step08 {source_value!r}"
                )
    guard = GUARD_PROFILE_LIBRARY[guard_name]
    throttle_name = guard["throttle_name"]
    target_lock_name = guard["target_lock_name"]
    trailing_buffer_admission_enabled = bool(spec.get("trailing_buffer_admission_enabled", False))
    trailing_buffer_reserve_mode = str(
        spec.get(
            "trailing_buffer_reserve_mode",
            "static" if trailing_buffer_admission_enabled else "off",
        )
    )
    trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer = float(
        spec.get("trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer", 0.0)
        or 0.0
    )
    if not (
        np.isfinite(trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer)
        and 0.0 <= trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer <= 1.0
    ):
        raise ValueError(
            "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer must be "
            "between 0 and 1 inclusive."
        )
    trailing_buffer_fixed_reserve_amount = (
        trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer
        * INITIAL_OVERALL_DD_BUFFER_AMOUNT
    )
    trailing_buffer_cost_allowance_r = float(
        spec.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0
    )
    # Backward-compatible alias retained in CSV/JSON outputs. For dynamic modes
    # this is the fixed component, not the total exposure-aware reserve.
    trailing_buffer_reserve_amount = trailing_buffer_fixed_reserve_amount
    trailing_buffer_admission_policy = (
        "solve_target_ceiling_then_validate_broker_risk_against_dynamic_reserve"
        if trailing_buffer_admission_enabled
        else "off"
    )
    name = str(
        spec.get("name")
        or f"probe_{slug(cap_name)}_{slug(guard_name)}_b{risk_label(budget)}_sc{risk_label(single_cap)}"
    )
    return {
        "name": name,
        "risk_settings_source": risk_settings_source,
        "source_step08_base_portfolio_risk_budget_pct": selected_budget,
        "source_step08_max_single_trade_risk_pct": selected_single_cap,
        "source_step08_overlap_mode": selected_overlap,
        "source_step08_max_min_lot_risk_inflation": selected_min_lot_inflation,
        "base_portfolio_risk_budget_pct": budget,
        "max_single_trade_risk_pct": single_cap,
        # AFML Ch.10.2/10.4 concurrency-aware sizing (2026-10-17): 0.0 = off (original direct-weight sizing).
        "afml_concurrency_quantile": AFMLC.validate_quantile(selected.get("afml_concurrency_quantile")),
        "afml_sizing_mode": AFMLC.sizing_mode(selected.get("afml_concurrency_quantile")),
        "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
        "min_lot_risk_inflation_guard_enabled": bool(
            max_min_lot_risk_inflation is not None
        ),
        "cap_name": cap_name,
        **cap,
        "guard_profile": guard_name,
        "throttle_name": throttle_name,
        "throttle_basis": THROTTLE_BASIS,
        "throttle_configuration_basis": THROTTLE_CONFIGURATION_BASIS,
        "throttle_reference_initial_overall_dd_buffer_amount": INITIAL_OVERALL_DD_BUFFER_AMOUNT,
        "throttle_tiers_fraction_of_initial_dd_buffer": THROTTLE_LIBRARY_FRACTION_OF_INITIAL_DD_BUFFER[throttle_name],
        "throttle_tiers": THROTTLE_LIBRARY[throttle_name],
        "trailing_buffer_admission_enabled": trailing_buffer_admission_enabled,
        "trailing_buffer_reserve_mode": trailing_buffer_reserve_mode,
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer,
        "trailing_buffer_fixed_reserve_amount": trailing_buffer_fixed_reserve_amount,
        "trailing_buffer_cost_allowance_r": trailing_buffer_cost_allowance_r,
        "trailing_buffer_reserve_amount": trailing_buffer_reserve_amount,
        "trailing_buffer_admission_policy": trailing_buffer_admission_policy,
        # Daily-drawdown admission mirrors the selected trailing reserve. This
        # protects the second hard floor without changing core model sizing.
        "daily_buffer_admission_enabled": bool(daily_cfg["enabled"] and trailing_buffer_admission_enabled),
        "daily_buffer_reserve_mode": trailing_buffer_reserve_mode if bool(daily_cfg["enabled"]) else "off",
        "daily_buffer_fixed_reserve_pct_of_initial_dd_buffer": trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer if bool(daily_cfg["enabled"]) else 0.0,
        "daily_buffer_fixed_reserve_amount": trailing_buffer_fixed_reserve_amount if bool(daily_cfg["enabled"]) else 0.0,
        "daily_buffer_cost_allowance_r": trailing_buffer_cost_allowance_r if bool(daily_cfg["enabled"]) else 0.0,
        "daily_buffer_admission_policy": (
            "solve_target_ceiling_then_validate_broker_risk_against_dynamic_daily_reserve"
            if bool(daily_cfg["enabled"] and trailing_buffer_admission_enabled)
            else "off"
        ),
        "pause_below_balance": float(guard["pause_below_balance"]),
        "target_lock_name": target_lock_name,
        "target_lock_tiers": TARGET_LOCK_LIBRARY[target_lock_name],
        "account_rule_profile": daily_cfg["account_rule_profile"],
        "account_stage": daily_cfg["account_stage"],
        "daily_drawdown_name": daily_cfg["name"],
        "daily_drawdown_enabled": bool(daily_cfg["enabled"]),
        "daily_drawdown_pct": float(daily_cfg["pct"]),
        "daily_drawdown_amount": float(daily_cfg["amount"]),
        "daily_drawdown_mode": str(daily_cfg["mode"]),
        "daily_drawdown_breach_basis": str(daily_cfg.get("breach_basis", ACCOUNT_RULES["daily_drawdown"]["breach_basis"])),
        "daily_symbol_risk_rule_enabled": bool(daily_cfg["daily_symbol_risk_rule_enabled"]),
        "daily_symbol_risk_enforcement_mode": str(daily_cfg["daily_symbol_risk_enforcement_mode"]),
        "max_daily_risk_per_symbol_pct": daily_cfg.get("max_daily_risk_per_symbol_pct"),
        "max_daily_risk_per_symbol_amount": daily_cfg.get("max_daily_risk_per_symbol_amount"),
        "internal_operating_risk_per_symbol_pct": daily_cfg.get("internal_operating_risk_per_symbol_pct"),
        "internal_operating_risk_per_symbol_amount": daily_cfg.get("internal_operating_risk_per_symbol_amount"),
        "symbol_risk_safety_reserve_amount": daily_cfg.get("symbol_risk_safety_reserve_amount"),
    }


def build_variants_from_selected(selected: dict[str, Any]) -> list[dict[str, Any]]:
    selected_budget = float(selected["base_portfolio_risk_budget_pct"])
    selected_single_cap = float(selected["max_single_trade_risk_pct"])
    selected_overlap = str(selected.get("overlap_mode", "balanced"))
    daily_cfg = daily_drawdown_from_selected(selected)

    if FAST_PROBE_MODE:
        variants = [_make_variant_from_spec(spec, selected, daily_cfg) for spec in FAST_PROBE_VARIANTS]
        if not variants:
            raise RuntimeError("FAST_PROBE_MODE=True but FAST_PROBE_VARIANTS is empty.")

        if FAST_PROBE_USE_SELECTED_STEP08_RISK_SETTINGS:
            # Hard invariant: every fast probe must use the selected Step 08 risk
            # budget, single-trade cap, and overlap mode exactly.
            for v in variants:
                if not np.isclose(float(v["base_portfolio_risk_budget_pct"]), selected_budget, rtol=0.0, atol=EPS):
                    raise RuntimeError(
                        f"Fast probe {v['name']} drifted from Step 08 budget: "
                        f"{v['base_portfolio_risk_budget_pct']} != {selected_budget}"
                    )
                if not np.isclose(float(v["max_single_trade_risk_pct"]), selected_single_cap, rtol=0.0, atol=EPS):
                    raise RuntimeError(
                        f"Fast probe {v['name']} drifted from Step 08 single-trade cap: "
                        f"{v['max_single_trade_risk_pct']} != {selected_single_cap}"
                    )
                if str(v["cap_name"]) != selected_overlap:
                    raise RuntimeError(
                        f"Fast probe {v['name']} drifted from Step 08 overlap mode: "
                        f"{v['cap_name']} != {selected_overlap}"
                    )
                selected_guard = optional_float(
                    selected.get("max_min_lot_risk_inflation")
                )
                variant_guard = optional_float(
                    v.get("max_min_lot_risk_inflation")
                )
                if (
                    selected_guard is None
                    and variant_guard is not None
                ) or (
                    selected_guard is not None
                    and (
                        variant_guard is None
                        or not np.isclose(
                            variant_guard, selected_guard, rtol=0.0, atol=EPS
                        )
                    )
                ):
                    raise RuntimeError(
                        f"Fast probe {v['name']} drifted from Step 08 minimum-lot "
                        f"inflation guard: {variant_guard!r} != {selected_guard!r}"
                    )
        return variants

    overlap_names = [x for x in list(dict.fromkeys([selected_overlap, "cap1p50", "cap2p00", "cap2p50", "balanced"])) if x in OVERLAP_MODE_LIBRARY]
    budget_candidates = rounded_unique([selected_budget * m for m in [0.60, 0.75, 0.90, 1.00, 1.10, 1.25]], 0.01, 1.00)
    single_cap_candidates = rounded_unique([0.0025, selected_single_cap * 0.50, selected_single_cap * 0.75, selected_single_cap, selected_single_cap * 1.25, selected_single_cap * 1.50], 0.0010, 0.0200)
    variants = []
    for overlap_name in overlap_names:
        cap = OVERLAP_MODE_LIBRARY[overlap_name]
        for budget in budget_candidates:
            for single_cap in single_cap_candidates:
                for guard_name, guard in GUARD_PROFILE_LIBRARY.items():
                    throttle_name = guard["throttle_name"]
                    target_lock_name = guard["target_lock_name"]
                    name = f"auto_{slug(overlap_name)}_{slug(guard_name)}_b{risk_label(budget)}_sc{risk_label(single_cap)}"
                    variants.append({
                        "name": name,
                        "base_portfolio_risk_budget_pct": float(budget),
                        "max_single_trade_risk_pct": float(single_cap),
                        "max_min_lot_risk_inflation": optional_float(
                            selected.get("max_min_lot_risk_inflation")
                        ),
                        "min_lot_risk_inflation_guard_enabled": bool(
                            optional_float(
                                selected.get("max_min_lot_risk_inflation")
                            ) is not None
                        ),
                        "cap_name": overlap_name,
                        **cap,
                        "guard_profile": guard_name,
                        "throttle_name": throttle_name,
                        "throttle_basis": THROTTLE_BASIS,
                        "throttle_configuration_basis": THROTTLE_CONFIGURATION_BASIS,
                        "throttle_reference_initial_overall_dd_buffer_amount": INITIAL_OVERALL_DD_BUFFER_AMOUNT,
                        "throttle_tiers_fraction_of_initial_dd_buffer": THROTTLE_LIBRARY_FRACTION_OF_INITIAL_DD_BUFFER[throttle_name],
                        "throttle_tiers": THROTTLE_LIBRARY[throttle_name],
                        "trailing_buffer_admission_enabled": False,
                        "trailing_buffer_reserve_mode": "off",
                        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": 0.0,
                        "trailing_buffer_fixed_reserve_amount": 0.0,
                        "trailing_buffer_cost_allowance_r": 0.0,
                        "trailing_buffer_reserve_amount": 0.0,
                        "trailing_buffer_admission_policy": "off",
                        "pause_below_balance": float(guard["pause_below_balance"]),
                        "target_lock_name": target_lock_name,
                        "target_lock_tiers": TARGET_LOCK_LIBRARY[target_lock_name],
                        "account_rule_profile": daily_cfg["account_rule_profile"],
                        "account_stage": daily_cfg["account_stage"],
                        "daily_drawdown_name": daily_cfg["name"],
                        "daily_drawdown_enabled": bool(daily_cfg["enabled"]),
                        "daily_drawdown_pct": float(daily_cfg["pct"]),
                        "daily_drawdown_amount": float(daily_cfg["amount"]),
                        "daily_drawdown_mode": str(daily_cfg["mode"]),
                        "daily_drawdown_breach_basis": str(daily_cfg.get("breach_basis", ACCOUNT_RULES["daily_drawdown"]["breach_basis"])),
                        "daily_symbol_risk_rule_enabled": bool(daily_cfg["daily_symbol_risk_rule_enabled"]),
                        "daily_symbol_risk_enforcement_mode": str(daily_cfg["daily_symbol_risk_enforcement_mode"]),
                        "max_daily_risk_per_symbol_pct": daily_cfg.get("max_daily_risk_per_symbol_pct"),
                        "max_daily_risk_per_symbol_amount": daily_cfg.get("max_daily_risk_per_symbol_amount"),
                        "internal_operating_risk_per_symbol_pct": daily_cfg.get("internal_operating_risk_per_symbol_pct"),
                        "internal_operating_risk_per_symbol_amount": daily_cfg.get("internal_operating_risk_per_symbol_amount"),
                        "symbol_risk_safety_reserve_amount": daily_cfg.get("symbol_risk_safety_reserve_amount"),
                    })
    if not variants:
        raise RuntimeError("No variants were generated from Step 08 selected scenario.")
    return variants

def load_ledger(path: Path) -> tuple[pd.DataFrame, dict[str, int], dict[str, int], dict[str, int]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing representative ledger: {path}")
    df = pd.read_csv(path)
    required = [
        "representative_id", "edge_id", "asset", "family", "side_name", "threshold",
        "edge_classification", "representative_roles", "entry_time", "exit_time",
        "executed_r", AFML_BET_SIZE_COLUMN,
        "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "broker_contract_size",
        "risk_per_min_lot", "risk_per_lot_step", "min_executable_risk_amount",
        "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            "Representative ledger missing required broker/min-lot-aware columns: "
            f"{missing}. Rerun patched Steps 04B, 04C, and 05 first."
        )
    df["entry_time"] = to_datetime_iso(df["entry_time"], utc=True, errors="coerce")
    df["exit_time"] = to_datetime_iso(df["exit_time"], utc=True, errors="coerce")
    numeric_cols = [
        "executed_r", "threshold", AFML_BET_SIZE_COLUMN,
        "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "broker_contract_size",
        "risk_per_min_lot", "risk_per_lot_step", "min_executable_risk_amount",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    validate_broker_risk_handoff(df)
    valid_bool = df["has_valid_broker_min_lot_risk"].astype(str).str.lower().isin(["true", "1", "yes", "y"])
    positive_broker = (
        (df["risk_per_1_lot"] > EPS) &
        (df["broker_min_lot"] > EPS) &
        (df["broker_lot_step"] > EPS) &
        (df["broker_contract_size"] > EPS) &
        (df["risk_per_min_lot"] > EPS) &
        (df["risk_per_lot_step"] > EPS) &
        (df["min_executable_risk_amount"] > EPS)
    )
    invalid = df[~(valid_bool & positive_broker)].copy()
    if not invalid.empty:
        sample_cols = [c for c in ["representative_id", "edge_id", "asset", "entry_time", "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "min_executable_risk_amount", "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk"] if c in invalid.columns]
        raise ValueError(
            "Representative ledger contains invalid broker/min-lot risk rows. "
            f"Invalid rows: {len(invalid)}. Sample: {invalid[sample_cols].head(5).to_dict(orient='records')}"
        )
    df["afml_bet_size_for_sizing"] = df[AFML_BET_SIZE_COLUMN].clip(lower=AFML_SIZE_FLOOR, upper=AFML_SIZE_CAP)
    df = df.dropna(subset=[
        "representative_id", "edge_id", "asset", "family", "entry_time", "exit_time",
        "executed_r", "afml_bet_size_for_sizing", "risk_per_1_lot", "broker_min_lot", "broker_lot_step",
    ]).copy()
    df = df[(df["exit_time"] > df["entry_time"]) & (df["afml_bet_size_for_sizing"] > AFML_SIZE_FLOOR + EPS)].copy()
    if df.empty:
        raise RuntimeError("Ledger is empty after broker/min-lot-aware cleaning.")
    df = df.sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    asset_to_code = {a: i for i, a in enumerate(sorted(df["asset"].astype(str).unique()))}
    family_to_code = {a: i for i, a in enumerate(sorted(df["family"].astype(str).unique()))}
    edge_to_code = {a: i for i, a in enumerate(sorted(df["edge_id"].astype(str).unique()))}
    df["asset_code"] = df["asset"].astype(str).map(asset_to_code).astype(int)
    df["family_code"] = df["family"].astype(str).map(family_to_code).astype(int)
    df["edge_code"] = df["edge_id"].astype(str).map(edge_to_code).astype(int)
    return df, asset_to_code, family_to_code, edge_to_code


def load_hrp_weights(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing HRP weights: {path}")
    df = pd.read_csv(path)
    required = ["return_mode", "universe", "profile_role", "linkage_method", "edge_id", "weight", "asset", "family", "side_name", "threshold", "representative_id", "edge_classification", "representative_roles"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"HRP weights missing required columns: {missing}")
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna(subset=["return_mode", "universe", "profile_role", "linkage_method", "edge_id", "representative_id", "weight"]).copy()
    df = df[df["weight"] > 0].copy()
    if df.empty:
        raise RuntimeError("No valid HRP weights found.")
    parts = []
    for _, g in df.groupby(["return_mode", "universe", "profile_role", "linkage_method"], sort=False):
        g = g.copy()
        s = g["weight"].sum()
        if s > 0:
            g["weight"] = g["weight"] / s
            parts.append(g)
    if not parts:
        raise RuntimeError("No normalizable HRP weight groups found.")
    return pd.concat(parts, ignore_index=True)


def prepare_selected_arrays(selected: dict[str, Any], ledger: pd.DataFrame, weights: pd.DataFrame) -> dict[str, Any]:
    sw = weights[(weights["return_mode"] == selected["return_mode"]) & (weights["universe"] == selected["universe"]) & (weights["profile_role"] == selected["profile_role"]) & (weights["linkage_method"] == selected["linkage_method"])].copy()
    if sw.empty:
        raise RuntimeError(f"No HRP weights found for selected scenario {selected.get('scenario_id')}")
    sw["weight"] = pd.to_numeric(sw["weight"], errors="coerce")
    sw = sw.dropna(subset=["weight"])
    sw = sw[sw["weight"] > 0].copy()
    sw["weight"] = sw["weight"] / sw["weight"].sum()
    weight_by_rep = dict(zip(sw["representative_id"].astype(str), sw["weight"]))
    trades = ledger[ledger["representative_id"].astype(str).isin(weight_by_rep.keys())].copy()
    if trades.empty:
        raise RuntimeError("No ledger trades found for selected HRP representatives.")
    trades["hrp_weight"] = trades["representative_id"].astype(str).map(weight_by_rep)
    trades = trades.dropna(subset=["hrp_weight"]).sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    final_data_end_ns = int(max(trades["entry_time"].astype("int64").max(), trades["exit_time"].astype("int64").max()))
    return {
        "weights": sw,
        "trades": trades,
        "n_edges": int(sw["edge_id"].nunique()),
        "n_representatives": int(sw["representative_id"].nunique()),
        "hrp_effective_n": safe_div(1.0, float((sw["weight"] ** 2).sum())),
        "hrp_top_weight": float(sw["weight"].max()),
        "hrp_min_weight": float(sw["weight"].min()),
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
        "asset": trades["asset"].astype(str).to_numpy(),
        "trade_count_available": int(len(trades)),
        "final_data_end_ns": final_data_end_ns,
    }


def build_rolling_start_times(arr: dict[str, Any], every_n_active_days: int, max_starts: int | None) -> list[int]:
    entry_ns = arr["entry_ns"]
    latest_allowed = int(arr["final_data_end_ns"]) - int(MIN_REMAINING_OBSERVATION_DAYS_AFTER_START * DAY_NS)
    mask = entry_ns <= latest_allowed
    if not mask.any():
        raise RuntimeError("No eligible rolling starts after observation-tail filter.")
    days = pd.Series(arr["entry_day"][mask]).drop_duplicates().sort_values().to_numpy(dtype=np.int64)
    selected_days = days[::max(1, int(every_n_active_days))]
    starts = []
    for day in selected_days:
        left = int(np.searchsorted(entry_ns, int(day * DAY_NS), side="left"))
        right = int(np.searchsorted(entry_ns, int((day + 1) * DAY_NS), side="left"))
        if left < right:
            starts.append(int(entry_ns[left]))
    starts = sorted(set(starts))
    if max_starts is not None and len(starts) > int(max_starts):
        idx = np.linspace(0, len(starts) - 1, int(max_starts)).round().astype(int)
        starts = [starts[i] for i in sorted(set(idx))]
    if DRY_RUN:
        starts = starts[:DRY_RUN_MAX_STARTS]
    return starts


def simulate_from_start(arr: dict[str, Any], base_raw_pct: np.ndarray, variant: dict[str, Any], start_ns: int, cost_stress_r: float, n_assets: int, n_families: int, n_edges_global: int, code_to_asset: dict[int, str]) -> dict[str, Any]:
    _afml_div = AFMLC.divisor_for_arr(arr, variant.get("afml_concurrency_quantile"))  # AFML Ch.10.2/10.4; None = off
    entry_ns, exit_ns = arr["entry_ns"], arr["exit_ns"]
    executed_r_arr, afml_size_arr = arr["executed_r"], arr["afml_bet_size"]
    asset_arr, family_arr, edge_arr = arr["asset_code"], arr["family_code"], arr["edge_code"]
    final_data_end_ns = int(arr["final_data_end_ns"])
    start_idx = int(np.searchsorted(entry_ns, start_ns, side="left"))
    end_idx = len(entry_ns)

    balance = float(CURRENT_BALANCE)
    min_balance = max_balance = peak_balance = balance
    trailing_high_watermark = max(ACCOUNT_START_TRAILING_HIGH_WATERMARK, balance)
    trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
    st0 = drawdown_stats(balance, 0.0, trailing_high_watermark)
    max_challenge_dd_used = st0["challenge_dd_used"]
    min_remaining_buffer = st0["remaining_dd_buffer"]
    max_realized_peak_to_valley_dd = 0.0

    open_risk_total = 0.0
    open_risk_by_asset = np.zeros(n_assets, dtype=float)
    open_risk_by_family = np.zeros(n_families, dtype=float)
    open_risk_by_edge = np.zeros(n_edges_global, dtype=float)
    open_count_by_asset = np.zeros(n_assets, dtype=np.int32)
    open_count_by_edge = np.zeros(n_edges_global, dtype=np.int32)
    open_heap: list[tuple[int, int, float, float, int, int, int]] = []

    heap_counter = attempted = accepted = skipped = closed = 0
    skip_total_cap = skip_asset_cap = skip_family_cap = skip_edge_cap = skip_edge_count = skip_asset_count = skip_pause = skip_zero = 0
    skip_invalid_broker_risk = 0
    skip_min_lot_risk_cap = 0
    skip_overlap_risk_cap = 0
    skip_daily_symbol_risk_cap = 0
    skip_projected_trailing_buffer_reserve = 0
    skip_projected_daily_buffer_reserve = 0
    daily_buffer_target_clipped_attempted = 0
    daily_buffer_target_clip_amount_sum = 0.0
    trailing_buffer_target_clipped_attempted = 0
    trailing_buffer_target_clip_amount_sum = 0.0
    min_available_new_risk_at_attempt = np.inf
    min_projected_trailing_buffer_after_accept = np.inf
    min_required_trailing_buffer_reserve_after_accept = np.inf
    max_required_trailing_buffer_reserve_after_accept = 0.0
    required_trailing_buffer_reserve_sum_accepted = 0.0
    min_projected_buffer_excess_over_required_reserve_after_accept = np.inf
    max_open_risk = max_open_risk_pct = 0.0
    max_open_trades = open_trade_obs = 0
    sum_open_trades = 0.0
    active_days = {day_from_ns(start_ns)}
    target_hit_time = failure_time = None
    target_hit_balance = np.nan
    failure_reason = ""
    open_trades_at_target = open_risk_pct_at_target = np.nan

    asset_trades = np.zeros(n_assets, dtype=np.int32)
    asset_wins = np.zeros(n_assets, dtype=np.int32)
    asset_losses = np.zeros(n_assets, dtype=np.int32)
    asset_net_pnl = np.zeros(n_assets, dtype=float)
    asset_gross_profit = np.zeros(n_assets, dtype=float)
    asset_gross_loss = np.zeros(n_assets, dtype=float)
    asset_risk_sum = np.zeros(n_assets, dtype=float)

    afml_size_sum = raw_risk_pct_sum = target_risk_pct_sum = actual_risk_pct_sum = throttle_mult_sum = target_lock_mult_sum = 0.0
    throttle_mult_attempted_sum = 0.0
    throttle_reduced_attempted = throttle_reduced_accepted = 0
    min_trailing_buffer_at_attempt = np.inf
    min_trailing_buffer_at_accept = np.inf
    clipped_attempted = clipped_accepted = 0
    max_raw_trade_risk_pct = max_target_trade_risk_pct = max_actual_trade_risk_pct = 0.0
    target_risk_values: list[float] = []
    actual_risk_values: list[float] = []
    risk_inflation_values: list[float] = []
    actual_lot_values: list[float] = []
    min_lot_binding_count = 0
    skip_min_lot_inflation_guard = 0
    max_min_lot_risk_inflation = optional_float(
        variant.get("max_min_lot_risk_inflation")
    )

    daily_enabled = bool(variant.get("daily_drawdown_enabled", True))
    daily_pct = float(variant.get("daily_drawdown_pct", DAILY_LOSS_PCT) or DAILY_LOSS_PCT)
    daily_amount = float(variant.get("daily_drawdown_amount", DAILY_LOSS_AMOUNT) or DAILY_LOSS_AMOUNT)
    daily_mode = str(variant.get("daily_drawdown_mode", "equity_proxy_fixed_initial_amount"))
    daily_symbol_risk_rule_enabled = safe_bool(
        variant.get("daily_symbol_risk_rule_enabled"), default=False
    )
    daily_symbol_risk_enforcement_mode = str(
        variant.get("daily_symbol_risk_enforcement_mode", ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])
    )
    symbol_daily_hard_cap_amount = optional_float(variant.get("max_daily_risk_per_symbol_amount"))
    symbol_internal_operating_cap_amount = optional_float(
        variant.get("internal_operating_risk_per_symbol_amount")
    )
    if daily_symbol_risk_rule_enabled:
        if symbol_daily_hard_cap_amount is None or symbol_internal_operating_cap_amount is None:
            raise ValueError("Enabled symbol-risk enforcement requires legal and internal cap amounts.")
        if symbol_internal_operating_cap_amount > symbol_daily_hard_cap_amount + EPS:
            raise ValueError(
                "Internal symbol operating cap cannot exceed the legal hard cap: "
                f"{symbol_internal_operating_cap_amount} > {symbol_daily_hard_cap_amount}"
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
        return account_daily_breach_value(
            ACCOUNT_RULES,
            balance=balance,
            equity=equity,
        )

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
        nonlocal failure_reason, failure_time, min_remaining_buffer, max_daily_drawdown_pct, daily_worst_buffer
        nonlocal trailing_high_watermark, trailing_floor, max_daily_symbol_risk_amount, max_daily_symbol_risk_pct
        reset_daily_if_needed(event_ns)
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        st = drawdown_stats(balance, open_risk_total, trailing_high_watermark)
        min_remaining_buffer = min(min_remaining_buffer, st["remaining_dd_buffer"])
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
            max_daily_symbol_risk_pct = max(max_daily_symbol_risk_pct, safe_div(observed, PROP_START_BALANCE))
            if (
                daily_symbol_risk_rule_enabled
                and symbol_daily_hard_cap_amount is not None
                and observed > symbol_daily_hard_cap_amount + EPS
                and failure_reason == ""
            ):
                failure_reason = "max_daily_symbol_risk_breached"
                failure_time = event_ns

    def update_balance_after_batch(event_ns: int, batch_pnl: float) -> None:
        nonlocal balance, min_balance, max_balance, peak_balance, max_challenge_dd_used, max_realized_peak_to_valley_dd, target_hit_time, target_hit_balance, open_trades_at_target, open_risk_pct_at_target, trailing_high_watermark, trailing_floor
        reset_daily_if_needed(event_ns)
        balance += batch_pnl
        peak_balance = max(peak_balance, balance)
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        min_balance = min(min_balance, balance)
        max_balance = max(max_balance, balance)
        st = drawdown_stats(balance, open_risk_total, trailing_high_watermark)
        max_challenge_dd_used = max(max_challenge_dd_used, st["challenge_dd_used"])
        max_realized_peak_to_valley_dd = max(max_realized_peak_to_valley_dd, peak_balance - balance)
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
            open_trades_at_target = len(open_heap)
            open_risk_pct_at_target = safe_div(open_risk_total, balance)

    def close_due(current_ns: int) -> bool:
        nonlocal open_risk_total, closed
        while open_heap and open_heap[0][0] <= current_ns:
            this_exit_ns = open_heap[0][0]
            reset_daily_if_needed(this_exit_ns)
            batch_pnl = 0.0
            while open_heap and open_heap[0][0] == this_exit_ns:
                _, _, risk_amount, executed_r_stressed, ac, fc, ec = heapq.heappop(open_heap)
                open_risk_total -= risk_amount
                open_risk_by_asset[ac] -= risk_amount
                open_risk_by_family[fc] -= risk_amount
                open_risk_by_edge[ec] -= risk_amount
                open_count_by_asset[ac] -= 1
                open_count_by_edge[ec] -= 1
                pnl = risk_amount * executed_r_stressed
                batch_pnl += pnl
                if pnl < 0.0:
                    daily_closed_loss_by_asset[ac] += -pnl
                closed += 1
                asset_trades[ac] += 1
                asset_net_pnl[ac] += pnl
                asset_risk_sum[ac] += risk_amount
                if pnl > 0:
                    asset_wins[ac] += 1
                    asset_gross_profit[ac] += pnl
                elif pnl < 0:
                    asset_losses[ac] += 1
                    asset_gross_loss[ac] += pnl
            if abs(open_risk_total) < 1e-8:
                open_risk_total = 0.0
            update_balance_after_batch(this_exit_ns, batch_pnl)
            if (STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason):
                return True
        return False

    max_single_trade_risk_pct = float(variant["max_single_trade_risk_pct"])
    trailing_buffer_admission_enabled = bool(
        variant.get("trailing_buffer_admission_enabled", False)
    )
    trailing_buffer_reserve_mode = str(
        variant.get(
            "trailing_buffer_reserve_mode",
            "static" if trailing_buffer_admission_enabled else "off",
        )
    )
    trailing_buffer_fixed_reserve_amount = float(
        variant.get(
            "trailing_buffer_fixed_reserve_amount",
            variant.get("trailing_buffer_reserve_amount", 0.0),
        )
        or 0.0
    )
    trailing_buffer_cost_allowance_r = float(
        variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0
    )
    trailing_buffer_reserve_amount = trailing_buffer_fixed_reserve_amount
    trailing_buffer_admission_policy = str(
        variant.get("trailing_buffer_admission_policy", "off")
    )
    daily_buffer_admission_enabled = bool(
        variant.get(
            "daily_buffer_admission_enabled",
            daily_enabled and trailing_buffer_admission_enabled,
        )
    )
    daily_buffer_reserve_mode = str(
        variant.get(
            "daily_buffer_reserve_mode",
            trailing_buffer_reserve_mode if daily_buffer_admission_enabled else "off",
        )
    )
    daily_buffer_fixed_reserve_amount = float(
        variant.get(
            "daily_buffer_fixed_reserve_amount",
            trailing_buffer_fixed_reserve_amount if daily_buffer_admission_enabled else 0.0,
        )
        or 0.0
    )
    daily_buffer_cost_allowance_r = float(
        variant.get(
            "daily_buffer_cost_allowance_r",
            trailing_buffer_cost_allowance_r if daily_buffer_admission_enabled else 0.0,
        )
        or 0.0
    )
    check_rules(start_ns)

    for i in range(start_idx, end_idx):
        current_ns = int(entry_ns[i])
        if close_due(current_ns):
            break
        if (STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason):
            break
        reset_daily_if_needed(current_ns)
        attempted += 1
        active_days.add(day_from_ns(current_ns))
        if balance < float(variant["pause_below_balance"]) - EPS:
            skip_pause += 1; skipped += 1; continue

        # Correct trailing-drawdown throttle: use the current conservative equity
        # proxy's distance above the live trailing floor, not absolute balance.
        # This reacts immediately when a new high raises the floor.
        trailing_buffer = current_trailing_buffer(balance, open_risk_total, trailing_high_watermark)
        min_trailing_buffer_at_attempt = min(min_trailing_buffer_at_attempt, trailing_buffer)
        throttle_mult = multiplier_from_value(trailing_buffer, variant["throttle_tiers"], mode="below")
        throttle_mult_attempted_sum += throttle_mult
        if throttle_mult < 1.0 - EPS:
            throttle_reduced_attempted += 1

        # Target locks, when introduced later, remain balance/profit based.
        target_mult = multiplier_from_value(balance, variant["target_lock_tiers"], mode="above")
        _brp = float(base_raw_pct[i]) if _afml_div is None else float(base_raw_pct[i]) / float(_afml_div[i])
        raw_pct = float(variant["base_portfolio_risk_budget_pct"]) * _brp * throttle_mult * target_mult
        if raw_pct > max_single_trade_risk_pct + EPS:
            clipped_attempted += 1
        target_pct = min(raw_pct, max_single_trade_risk_pct)
        if not np.isfinite(target_pct) or target_pct <= EPS:
            skip_zero += 1; skipped += 1; continue

        requested_target_risk_amount = balance * target_pct
        target_risk_amount = requested_target_risk_amount

        # Projected post-trade trailing-buffer admission.
        #
        # Static mode:
        #   projected_buffer >= fixed_reserve
        #
        # Dynamic mode:
        #   projected_buffer >= fixed_reserve
        #                       + cost_allowance_r * projected_total_open_risk
        #
        # Since the proposed trade appears on both sides of the dynamic inequality,
        # maximum_admissible_new_risk solves the exact target ceiling algebraically.
        # The broker-rounded executable risk is still validated afterward.
        available_new_risk = np.inf
        if trailing_buffer_admission_enabled:
            available_new_risk = maximum_admissible_new_risk(
                trailing_buffer=trailing_buffer,
                current_open_risk_total=open_risk_total,
                reserve_mode=trailing_buffer_reserve_mode,
                fixed_reserve_amount=trailing_buffer_fixed_reserve_amount,
                cost_allowance_r=trailing_buffer_cost_allowance_r,
            )
            min_available_new_risk_at_attempt = min(
                min_available_new_risk_at_attempt,
                available_new_risk,
            )
            if available_new_risk <= EPS:
                skip_projected_trailing_buffer_reserve += 1
                skipped += 1
                continue
            if target_risk_amount > available_new_risk + EPS:
                trailing_buffer_target_clipped_attempted += 1
                trailing_buffer_target_clip_amount_sum += (
                    target_risk_amount - available_new_risk
                )
                target_risk_amount = available_new_risk
                target_pct = safe_div(target_risk_amount, balance)
                if not np.isfinite(target_pct) or target_pct <= EPS:
                    skip_projected_trailing_buffer_reserve += 1
                    skipped += 1
                    continue

        # Cost-aware daily-loss admission. The conservative daily equity
        # proxy already subtracts open stop risk; this reserve additionally
        # protects the expected 0.05R stressed execution cost on that risk.
        daily_buffer = daily_reference_value() - daily_floor
        available_daily_new_risk = np.inf
        if daily_buffer_admission_enabled:
            available_daily_new_risk = maximum_admissible_new_risk(
                trailing_buffer=daily_buffer,
                current_open_risk_total=open_risk_total,
                reserve_mode=daily_buffer_reserve_mode,
                fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
                cost_allowance_r=daily_buffer_cost_allowance_r,
            )
            if available_daily_new_risk <= EPS:
                skip_projected_daily_buffer_reserve += 1
                skipped += 1
                continue
            if target_risk_amount > available_daily_new_risk + EPS:
                daily_buffer_target_clipped_attempted += 1
                daily_buffer_target_clip_amount_sum += (
                    target_risk_amount - available_daily_new_risk
                )
                target_risk_amount = available_daily_new_risk
                target_pct = safe_div(target_risk_amount, balance)
                if not np.isfinite(target_pct) or target_pct <= EPS:
                    skip_projected_daily_buffer_reserve += 1
                    skipped += 1
                    continue

        ac, fc, ec = int(asset_arr[i]), int(family_arr[i]), int(edge_arr[i])
        effective_risk_cap = min(
            balance * max_single_trade_risk_pct,
            max(0.0, balance * float(variant["max_total_open_risk_pct"]) - open_risk_total),
            max(0.0, balance * float(variant["max_asset_open_risk_pct"]) - open_risk_by_asset[ac]),
            max(0.0, balance * float(variant["max_family_open_risk_pct"]) - open_risk_by_family[fc]),
            max(0.0, balance * float(variant["max_edge_open_risk_pct"]) - open_risk_by_edge[ec]),
        )
        if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
            effective_risk_cap = min(
                effective_risk_cap,
                max(0.0, FLOATING_LOSS_GUARD_AMOUNT - open_risk_total),
            )
        if trailing_buffer_admission_enabled:
            effective_risk_cap = min(effective_risk_cap, max(0.0, available_new_risk))
        if daily_buffer_admission_enabled:
            effective_risk_cap = min(
                effective_risk_cap,
                max(0.0, available_daily_new_risk),
            )
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
        if effective_risk_cap <= EPS:
            skip_min_lot_risk_cap += 1; skipped += 1; continue
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
            skip_invalid_broker_risk += 1; skipped += 1; continue
        risk_amount = float(br["actual_risk_amount"])
        actual_pct = safe_div(risk_amount, balance)
        if not np.isfinite(actual_pct) or actual_pct <= EPS:
            skip_invalid_broker_risk += 1; skipped += 1; continue
        if SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS and not bool(br["cap_fit"]):
            skip_min_lot_risk_cap += 1; skipped += 1; continue
        if (
            max_min_lot_risk_inflation is not None
            and bool(br["min_lot_binding"])
            and np.isfinite(float(br["risk_inflation"]))
            and float(br["risk_inflation"]) > max_min_lot_risk_inflation + EPS
        ):
            skip_min_lot_inflation_guard += 1
            skipped += 1
            continue

        projected_open_risk_total_after_trade = open_risk_total + risk_amount
        projected_trailing_buffer_after_trade = trailing_buffer - risk_amount
        required_reserve_after_trade = required_trailing_buffer_reserve(
            projected_open_risk_total=projected_open_risk_total_after_trade,
            reserve_mode=trailing_buffer_reserve_mode,
            fixed_reserve_amount=trailing_buffer_fixed_reserve_amount,
            cost_allowance_r=trailing_buffer_cost_allowance_r,
        )
        projected_buffer_excess_over_required_reserve = (
            projected_trailing_buffer_after_trade - required_reserve_after_trade
        )
        if (
            trailing_buffer_admission_enabled
            and projected_buffer_excess_over_required_reserve < -EPS
        ):
            skip_projected_trailing_buffer_reserve += 1
            skipped += 1
            continue

        projected_daily_buffer_after_trade = daily_buffer - risk_amount
        required_daily_reserve_after_trade = required_trailing_buffer_reserve(
            projected_open_risk_total=projected_open_risk_total_after_trade,
            reserve_mode=daily_buffer_reserve_mode,
            fixed_reserve_amount=daily_buffer_fixed_reserve_amount,
            cost_allowance_r=daily_buffer_cost_allowance_r,
        )
        if (
            daily_buffer_admission_enabled
            and projected_daily_buffer_after_trade
            < required_daily_reserve_after_trade - EPS
        ):
            skip_projected_daily_buffer_reserve += 1
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
            skip_daily_symbol_risk_cap += 1
            skipped += 1
            continue
        if open_risk_total + risk_amount > balance * float(variant["max_total_open_risk_pct"]) + EPS:
            skip_total_cap += 1; skip_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_asset[ac] + risk_amount > balance * float(variant["max_asset_open_risk_pct"]) + EPS:
            skip_asset_cap += 1; skip_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_family[fc] + risk_amount > balance * float(variant["max_family_open_risk_pct"]) + EPS:
            skip_family_cap += 1; skip_overlap_risk_cap += 1; skipped += 1; continue
        if open_risk_by_edge[ec] + risk_amount > balance * float(variant["max_edge_open_risk_pct"]) + EPS:
            skip_edge_cap += 1; skip_overlap_risk_cap += 1; skipped += 1; continue
        if open_count_by_edge[ec] >= int(variant["max_open_trades_per_edge"]):
            skip_edge_count += 1; skipped += 1; continue
        if open_count_by_asset[ac] >= int(variant["max_open_trades_per_asset"]):
            skip_asset_count += 1; skipped += 1; continue

        accepted += 1
        accepted_trade_days.add(day_from_ns(current_ns))
        afml_size_sum += float(afml_size_arr[i])
        raw_risk_pct_sum += raw_pct
        target_risk_pct_sum += target_pct
        actual_risk_pct_sum += actual_pct
        throttle_mult_sum += throttle_mult
        target_lock_mult_sum += target_mult
        min_trailing_buffer_at_accept = min(min_trailing_buffer_at_accept, trailing_buffer)
        min_projected_trailing_buffer_after_accept = min(
            min_projected_trailing_buffer_after_accept,
            projected_trailing_buffer_after_trade,
        )
        min_required_trailing_buffer_reserve_after_accept = min(
            min_required_trailing_buffer_reserve_after_accept,
            required_reserve_after_trade,
        )
        max_required_trailing_buffer_reserve_after_accept = max(
            max_required_trailing_buffer_reserve_after_accept,
            required_reserve_after_trade,
        )
        required_trailing_buffer_reserve_sum_accepted += required_reserve_after_trade
        min_projected_buffer_excess_over_required_reserve_after_accept = min(
            min_projected_buffer_excess_over_required_reserve_after_accept,
            projected_buffer_excess_over_required_reserve,
        )
        if throttle_mult < 1.0 - EPS:
            throttle_reduced_accepted += 1
        max_raw_trade_risk_pct = max(max_raw_trade_risk_pct, raw_pct)
        max_target_trade_risk_pct = max(max_target_trade_risk_pct, target_pct)
        max_actual_trade_risk_pct = max(max_actual_trade_risk_pct, actual_pct)
        if raw_pct > max_single_trade_risk_pct + EPS:
            clipped_accepted += 1
        target_risk_values.append(float(target_pct))
        actual_risk_values.append(float(actual_pct))
        if np.isfinite(br["risk_inflation"]):
            risk_inflation_values.append(float(br["risk_inflation"]))
        if np.isfinite(br["actual_lot"]):
            actual_lot_values.append(float(br["actual_lot"]))
        if bool(br["min_lot_binding"]):
            min_lot_binding_count += 1

        stressed_r = float(executed_r_arr[i]) - float(cost_stress_r)
        heapq.heappush(open_heap, (int(exit_ns[i]), heap_counter, risk_amount, stressed_r, ac, fc, ec))
        heap_counter += 1
        open_risk_total += risk_amount
        open_risk_by_asset[ac] += risk_amount
        open_risk_by_family[fc] += risk_amount
        open_risk_by_edge[ec] += risk_amount
        open_count_by_asset[ac] += 1
        open_count_by_edge[ec] += 1
        open_trade_obs += 1
        sum_open_trades += len(open_heap)
        max_open_trades = max(max_open_trades, len(open_heap))
        max_open_risk = max(max_open_risk, open_risk_total)
        max_open_risk_pct = max(max_open_risk_pct, safe_div(open_risk_total, balance))
        check_rules(current_ns)
        if STOP_ON_RULE_BREACH and failure_reason:
            break

    if not ((STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason)):
        close_due(final_data_end_ns)
    if target_hit_time is None and not failure_reason:
        failure_reason = "target_not_reached_by_end_of_history"
        failure_time = final_data_end_ns
        active_days.add(day_from_ns(final_data_end_ns))
        refresh_qualifying_day()

    max_drawdown_failed = failure_reason == "overall_drawdown_floor_breached"
    daily_drawdown_failed = failure_reason == "daily_drawdown_floor_breached"
    daily_symbol_risk_failed = bool(
        daily_symbol_risk_rule_enabled and failure_reason == "max_daily_symbol_risk_breached"
    )
    hard_failed = max_drawdown_failed or daily_drawdown_failed or daily_symbol_risk_failed
    end_of_history_fail = failure_reason == "target_not_reached_by_end_of_history"
    passed = target_hit_time is not None and failure_reason == ""
    trading_days_to_target = len([d for d in active_days if target_hit_time is not None and d <= day_from_ns(target_hit_time)]) if target_hit_time is not None else np.nan
    calendar_days_to_target = safe_div(target_hit_time - start_ns, DAY_NS) if target_hit_time is not None else np.nan

    def pctile(values: list[float], q: float) -> float:
        return float(np.nanpercentile(values, q)) if values else np.nan

    out = {
        "start_time": ns_to_ts(start_ns), "start_ns": int(start_ns), "cost_stress_r": float(cost_stress_r),
        "passed": bool(passed), "failed": bool(not passed), "hard_failed": bool(hard_failed),
        "max_drawdown_failed": bool(max_drawdown_failed), "daily_drawdown_failed": bool(daily_drawdown_failed),
        "daily_symbol_risk_failed": bool(daily_symbol_risk_failed),
        "end_of_history_fail": bool(end_of_history_fail), "failure_reason": failure_reason,
        "target_hit_time": ns_to_ts(target_hit_time), "failure_time": ns_to_ts(failure_time),
        "trading_days_to_target": trading_days_to_target, "calendar_days_to_target": calendar_days_to_target,
        "observation_calendar_days": safe_div(final_data_end_ns - start_ns, DAY_NS),
        "start_balance": CURRENT_BALANCE,
        "start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
        "ending_balance": balance, "target_hit_balance": target_hit_balance,
        "minimum_trading_days_enabled": MINIMUM_TRADING_DAYS_ENABLED,
        "minimum_trading_days_required": MINIMUM_TRADING_DAYS_REQUIRED,
        "qualifying_trading_days": int(len(qualifying_days)),
        "minimum_trading_days_satisfied": minimum_days_satisfied(ACCOUNT_RULES, len(qualifying_days)),
        "net_profit": balance - CURRENT_BALANCE, "min_balance": min_balance, "max_balance": max_balance,
        "max_challenge_dd_used": max_challenge_dd_used, "max_realized_peak_to_valley_dd": max_realized_peak_to_valley_dd,
        "remaining_buffer_at_end": drawdown_stats(balance, open_risk_total, trailing_high_watermark)["remaining_dd_buffer"], "worst_buffer_above_floor": min_remaining_buffer,
        "trailing_high_watermark": trailing_high_watermark, "trailing_drawdown_floor_at_end": trailing_floor,
        "trailing_floor_locked_at_initial_balance": bool(
            drawdown_stats(balance, open_risk_total, trailing_high_watermark)["trailing_floor_locked_at_initial_balance"]
        ),
        "floor_breached": bool(max_drawdown_failed),
        "account_rule_profile": variant.get("account_rule_profile", ACCOUNT_RULE_PROFILE),
        "account_stage": variant.get("account_stage", ACCOUNT_STAGE),
        "daily_drawdown_name": variant.get("daily_drawdown_name", ""),
        "daily_drawdown_enabled": bool(daily_enabled), "daily_drawdown_pct": float(daily_pct), "daily_drawdown_amount": float(daily_amount), "daily_drawdown_mode": daily_mode,
        "daily_drawdown_breach_basis": str(ACCOUNT_RULES["daily_drawdown"]["breach_basis"]),
        "daily_worst_buffer_above_floor": daily_worst_buffer, "max_daily_drawdown_pct": max_daily_drawdown_pct,
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
        "attempted_trades": attempted, "accepted_trades": accepted, "skipped_trades": skipped, "closed_trades": closed,
        "active_trading_days": len(active_days),
        "closed_trades_per_active_day": safe_div(closed, max(len(active_days), 1)),
        "accepted_trades_per_active_day": safe_div(accepted, max(len(active_days), 1)),
        "max_open_risk_usd": max_open_risk, "max_open_risk_pct": max_open_risk_pct, "max_open_trades": max_open_trades,
        "avg_open_trades_observed": safe_div(sum_open_trades, open_trade_obs),
        "open_trades_at_target": open_trades_at_target, "open_risk_pct_at_target": open_risk_pct_at_target,
        "avg_afml_bet_size_accepted": safe_div(afml_size_sum, accepted),
        "avg_raw_trade_risk_pct_before_single_cap": safe_div(raw_risk_pct_sum, accepted),
        "avg_target_trade_risk_pct": safe_div(target_risk_pct_sum, accepted),
        "avg_final_accepted_trade_risk_pct": safe_div(actual_risk_pct_sum, accepted),
        "avg_actual_trade_risk_pct": safe_div(actual_risk_pct_sum, accepted),
        "max_raw_trade_risk_pct_before_single_cap": max_raw_trade_risk_pct,
        "max_target_trade_risk_pct": max_target_trade_risk_pct,
        "max_final_accepted_trade_risk_pct": max_actual_trade_risk_pct,
        "max_actual_trade_risk_pct": max_actual_trade_risk_pct,
        "median_target_trade_risk_pct": pctile(target_risk_values, 50),
        "median_actual_trade_risk_pct": pctile(actual_risk_values, 50),
        "p90_actual_trade_risk_pct": pctile(actual_risk_values, 90),
        "median_risk_inflation_accepted": pctile(risk_inflation_values, 50),
        "p90_risk_inflation_accepted": pctile(risk_inflation_values, 90),
        "max_risk_inflation_accepted": max(risk_inflation_values) if risk_inflation_values else np.nan,
        "min_lot_binding_accepted": int(min_lot_binding_count),
        "min_lot_binding_rate_accepted": safe_div(min_lot_binding_count, accepted),
        "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
        "min_lot_risk_inflation_guard_enabled": bool(
            max_min_lot_risk_inflation is not None
        ),
        "skip_min_lot_inflation_guard": int(skip_min_lot_inflation_guard),
        "median_actual_lot_accepted": pctile(actual_lot_values, 50),
        "p90_actual_lot_accepted": pctile(actual_lot_values, 90),
        "single_trade_clip_rate_accepted": safe_div(clipped_accepted, accepted),
        "single_trade_clip_rate_attempted": safe_div(clipped_attempted, attempted),
        "throttle_basis": variant.get("throttle_basis", THROTTLE_BASIS),
        "trailing_buffer_admission_enabled": bool(trailing_buffer_admission_enabled),
        "trailing_buffer_reserve_mode": trailing_buffer_reserve_mode,
        "trailing_buffer_fixed_reserve_amount": float(trailing_buffer_fixed_reserve_amount),
        "trailing_buffer_cost_allowance_r": float(trailing_buffer_cost_allowance_r),
        "trailing_buffer_reserve_amount": float(trailing_buffer_reserve_amount),
        "trailing_buffer_admission_policy": trailing_buffer_admission_policy,
        "daily_buffer_admission_enabled": bool(daily_buffer_admission_enabled),
        "daily_buffer_reserve_mode": daily_buffer_reserve_mode,
        "daily_buffer_fixed_reserve_amount": float(daily_buffer_fixed_reserve_amount),
        "daily_buffer_cost_allowance_r": float(daily_buffer_cost_allowance_r),
        "daily_buffer_admission_policy": str(
            variant.get("daily_buffer_admission_policy", "off")
        ),
        "trailing_buffer_target_clipped_attempted": int(trailing_buffer_target_clipped_attempted),
        "trailing_buffer_target_clip_rate_attempted": safe_div(
            trailing_buffer_target_clipped_attempted,
            attempted - skip_pause,
        ),
        "trailing_buffer_target_clip_amount_usd": float(trailing_buffer_target_clip_amount_sum),
        "skip_projected_trailing_buffer_reserve": int(skip_projected_trailing_buffer_reserve),
        "skip_projected_daily_buffer_reserve": int(skip_projected_daily_buffer_reserve),
        "daily_buffer_target_clipped_attempted": int(daily_buffer_target_clipped_attempted),
        "daily_buffer_target_clip_rate_attempted": safe_div(
            daily_buffer_target_clipped_attempted,
            attempted,
        ),
        "daily_buffer_target_clip_amount_usd": float(daily_buffer_target_clip_amount_sum),
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
        "max_required_trailing_buffer_reserve_after_accept": (
            float(max_required_trailing_buffer_reserve_after_accept)
            if accepted > 0
            else np.nan
        ),
        "avg_required_trailing_buffer_reserve_after_accept": safe_div(
            required_trailing_buffer_reserve_sum_accepted,
            accepted,
        ),
        "min_projected_buffer_excess_over_required_reserve_after_accept": (
            float(min_projected_buffer_excess_over_required_reserve_after_accept)
            if np.isfinite(min_projected_buffer_excess_over_required_reserve_after_accept)
            else np.nan
        ),
        "avg_throttle_multiplier_attempted": safe_div(throttle_mult_attempted_sum, attempted - skip_pause),
        "avg_throttle_multiplier_accepted": safe_div(throttle_mult_sum, accepted),
        "throttle_reduced_attempted": int(throttle_reduced_attempted),
        "throttle_reduced_accepted": int(throttle_reduced_accepted),
        "throttle_activation_rate_attempted": safe_div(throttle_reduced_attempted, attempted - skip_pause),
        "throttle_activation_rate_accepted": safe_div(throttle_reduced_accepted, accepted),
        "min_trailing_buffer_at_trade_attempt": float(min_trailing_buffer_at_attempt) if np.isfinite(min_trailing_buffer_at_attempt) else np.nan,
        "min_trailing_buffer_at_trade_accept": float(min_trailing_buffer_at_accept) if np.isfinite(min_trailing_buffer_at_accept) else np.nan,
        "avg_target_lock_multiplier_accepted": safe_div(target_lock_mult_sum, accepted),
        "skip_total_open_risk_cap": skip_total_cap, "skip_asset_open_risk_cap": skip_asset_cap,
        "skip_family_open_risk_cap": skip_family_cap, "skip_edge_open_risk_cap": skip_edge_cap,
        "skip_max_open_trades_per_edge": skip_edge_count, "skip_max_open_trades_per_asset": skip_asset_count,
        "skip_pause_threshold": skip_pause, "skip_zero_risk": skip_zero,
        "skip_invalid_broker_risk": skip_invalid_broker_risk,
        "skip_min_lot_risk_cap": skip_min_lot_risk_cap,
        "skip_min_lot_inflation_guard": skip_min_lot_inflation_guard,
        "skip_overlap_risk_cap": skip_overlap_risk_cap,
        "skip_daily_symbol_risk_cap": skip_daily_symbol_risk_cap,
        "skip_internal_symbol_risk_cap": skip_daily_symbol_risk_cap,
    }
    for code, asset in code_to_asset.items():
        trades, wins, losses = int(asset_trades[code]), int(asset_wins[code]), int(asset_losses[code])
        gp, gl = float(asset_gross_profit[code]), float(asset_gross_loss[code])
        a = re.sub(r"[^a-z0-9]+", "_", str(asset).lower()).strip("_")
        out[f"{a}_closed_trades"] = trades
        out[f"{a}_traded"] = bool(trades > 0)
        out[f"{a}_win_rate"] = safe_div(wins, wins + losses)
        out[f"{a}_net_pnl"] = float(asset_net_pnl[code])
        out[f"{a}_profit_factor"] = safe_div(gp, abs(gl)) if gl < 0 else np.nan
        out[f"{a}_risk_sum"] = float(asset_risk_sum[code])
    return out


def prob_under_days(s: pd.Series, days: int) -> float:
    x = pd.to_numeric(s, errors="coerce")
    return float((x <= days).fillna(False).mean())


def aggregate_run_results(run_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    group_cols = [
        "variant_label", "variant_name", "source_scenario_id", "source_candidate_kind", "return_mode", "universe",
        "profile_role", "linkage_method", "risk_sizing_mode", "risk_settings_source",
        "source_step08_base_portfolio_risk_budget_pct", "source_step08_max_single_trade_risk_pct",
        "source_step08_overlap_mode", "source_step08_max_min_lot_risk_inflation",
        "base_portfolio_risk_budget_pct", "base_risk_pct",
        "max_single_trade_risk_pct", "max_min_lot_risk_inflation",
        "min_lot_risk_inflation_guard_enabled", "cap_name", "guard_profile", "throttle_name",
        "throttle_basis", "throttle_configuration_basis", "throttle_reference_initial_overall_dd_buffer_amount",
        "throttle_tiers_fraction_of_initial_dd_buffer",
        "trailing_buffer_admission_enabled", "trailing_buffer_reserve_mode",
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer",
        "trailing_buffer_fixed_reserve_amount", "trailing_buffer_cost_allowance_r",
        "trailing_buffer_reserve_amount", "trailing_buffer_admission_policy", "target_lock_name",
        "pause_below_balance", "account_rule_profile", "account_stage", "daily_drawdown_name", "daily_drawdown_enabled",
        "daily_drawdown_pct", "daily_drawdown_mode", "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode", "cost_stress_r",
    ]
    for keys, g in run_df.groupby(group_cols, dropna=False):
        d = dict(zip(group_cols, keys))
        passed = g["passed"].astype(bool)
        failed = ~passed
        hard_failed = g["hard_failed"].astype(bool)
        max_dd_failed = g["max_drawdown_failed"].astype(bool) if "max_drawdown_failed" in g.columns else hard_failed
        daily_dd_failed = g["daily_drawdown_failed"].astype(bool) if "daily_drawdown_failed" in g.columns else pd.Series(False, index=g.index)
        end_of_history_fail = g["end_of_history_fail"].astype(bool)
        pass_days = pd.to_numeric(g.loc[passed, "trading_days_to_target"], errors="coerce")
        row = dict(d)
        row.update({
            "runs": int(len(g)),
            "pass_rate": float(passed.mean()),
            "fail_rate": float(failed.mean()),
            "hard_fail_rate": float(hard_failed.mean()),
            "max_drawdown_fail_rate": float(max_dd_failed.mean()),
            "daily_drawdown_fail_rate": float(daily_dd_failed.mean()),
            "daily_symbol_risk_fail_rate": float(g.get("daily_symbol_risk_failed", pd.Series(False, index=g.index)).astype(bool).mean()),
            "end_of_history_fail_rate": float(end_of_history_fail.mean()),
            "median_trading_days_to_target": float(pass_days.median()) if len(pass_days.dropna()) else np.nan,
            "mean_trading_days_to_target": float(pass_days.mean()) if len(pass_days.dropna()) else np.nan,
            "p25_trading_days_to_target": float(pass_days.quantile(0.25)) if len(pass_days.dropna()) else np.nan,
            "p75_trading_days_to_target": float(pass_days.quantile(0.75)) if len(pass_days.dropna()) else np.nan,
            "p95_trading_days_to_target": float(pass_days.quantile(0.95)) if len(pass_days.dropna()) else np.nan,
            "p99_trading_days_to_target": float(pass_days.quantile(0.99)) if len(pass_days.dropna()) else np.nan,
            "worst_trading_days_to_target": float(pass_days.max()) if len(pass_days.dropna()) else np.nan,
            "prob_pass_under_10_trading_days": prob_under_days(g["trading_days_to_target"], 10),
            "prob_pass_under_20_trading_days": prob_under_days(g["trading_days_to_target"], 20),
            "prob_pass_under_30_trading_days": prob_under_days(g["trading_days_to_target"], 30),
            "prob_pass_under_40_trading_days": prob_under_days(g["trading_days_to_target"], 40),
            "median_closed_trades": float(g["closed_trades"].median()),
            "median_accepted_trades": float(g["accepted_trades"].median()),
            "median_closed_trades_per_active_day": float(g["closed_trades_per_active_day"].median()),
            "median_accepted_trades_per_active_day": float(g["accepted_trades_per_active_day"].median()),
            "median_observation_calendar_days": float(g["observation_calendar_days"].median()),
            "worst_buffer_above_floor": float(g["worst_buffer_above_floor"].min()),
            "median_worst_buffer_above_floor": float(g["worst_buffer_above_floor"].median()),
            "daily_worst_buffer_above_floor": float(g["daily_worst_buffer_above_floor"].min()) if "daily_worst_buffer_above_floor" in g.columns else np.nan,
            "max_daily_drawdown_pct": float(g["max_daily_drawdown_pct"].max()) if "max_daily_drawdown_pct" in g.columns else 0.0,
            "max_daily_symbol_risk_pct_observed": float(g["max_daily_symbol_risk_pct_observed"].max()) if "max_daily_symbol_risk_pct_observed" in g.columns else 0.0,
            "funded_reference_symbol_risk_exceed_rate": float(g.get("funded_reference_symbol_risk_exceeded", pd.Series(False, index=g.index)).astype(bool).mean()),
            "median_skipped_daily_symbol_risk_cap": float(g.get("skip_daily_symbol_risk_cap", pd.Series(0.0, index=g.index)).median()),
            "min_balance_observed": float(g["min_balance"].min()),
            "max_observed_open_risk_pct": float(g["max_open_risk_pct"].max()),
            "median_max_open_risk_pct": float(g["max_open_risk_pct"].median()),
            "median_avg_afml_bet_size": float(g["avg_afml_bet_size_accepted"].median()),
            "median_raw_trade_risk_pct_before_single_cap": float(g["avg_raw_trade_risk_pct_before_single_cap"].median()),
            "median_target_trade_risk_pct": float(g["avg_target_trade_risk_pct"].median()) if "avg_target_trade_risk_pct" in g.columns else np.nan,
            "median_final_accepted_trade_risk_pct": float(g["avg_final_accepted_trade_risk_pct"].median()),
            "median_actual_trade_risk_pct": float(g["avg_actual_trade_risk_pct"].median()) if "avg_actual_trade_risk_pct" in g.columns else float(g["avg_final_accepted_trade_risk_pct"].median()),
            "max_actual_trade_risk_pct": float(g["max_actual_trade_risk_pct"].max()) if "max_actual_trade_risk_pct" in g.columns else float(g["max_final_accepted_trade_risk_pct"].max()),
            "median_single_trade_clip_rate_accepted": float(g["single_trade_clip_rate_accepted"].median()),
            "max_single_trade_clip_rate_accepted": float(g["single_trade_clip_rate_accepted"].max()),
            "median_throttle_multiplier": float(g["avg_throttle_multiplier_accepted"].median()),
            "median_throttle_multiplier_attempted": float(g["avg_throttle_multiplier_attempted"].median()) if "avg_throttle_multiplier_attempted" in g.columns else np.nan,
            "median_throttle_activation_rate_attempted": float(g["throttle_activation_rate_attempted"].median()) if "throttle_activation_rate_attempted" in g.columns else 0.0,
            "median_throttle_activation_rate_accepted": float(g["throttle_activation_rate_accepted"].median()) if "throttle_activation_rate_accepted" in g.columns else 0.0,
            "min_trailing_buffer_at_trade_attempt": float(g["min_trailing_buffer_at_trade_attempt"].min()) if "min_trailing_buffer_at_trade_attempt" in g.columns else np.nan,
            "min_trailing_buffer_at_trade_accept": float(g["min_trailing_buffer_at_trade_accept"].min()) if "min_trailing_buffer_at_trade_accept" in g.columns else np.nan,
            "median_target_lock_multiplier": float(g["avg_target_lock_multiplier_accepted"].median()),
            "median_min_lot_binding_rate": float(g["min_lot_binding_rate_accepted"].median()) if "min_lot_binding_rate_accepted" in g.columns else np.nan,
            "median_skipped_min_lot_inflation_guard": float(
                pd.to_numeric(
                    g.get("skip_min_lot_inflation_guard", pd.Series(0, index=g.index)),
                    errors="coerce",
                ).median()
            ),
            "median_risk_inflation_accepted": float(g["median_risk_inflation_accepted"].median()) if "median_risk_inflation_accepted" in g.columns else np.nan,
            "p90_risk_inflation_accepted": float(g["p90_risk_inflation_accepted"].median()) if "p90_risk_inflation_accepted" in g.columns else np.nan,
            "max_risk_inflation_accepted": float(g["max_risk_inflation_accepted"].max()) if "max_risk_inflation_accepted" in g.columns else np.nan,
            "median_skipped_min_lot_risk_cap": float(g["skip_min_lot_risk_cap"].median()) if "skip_min_lot_risk_cap" in g.columns else 0.0,
            "median_skipped_invalid_broker_risk": float(g["skip_invalid_broker_risk"].median()) if "skip_invalid_broker_risk" in g.columns else 0.0,
            "median_trailing_buffer_target_clip_rate_attempted": float(g["trailing_buffer_target_clip_rate_attempted"].median()) if "trailing_buffer_target_clip_rate_attempted" in g.columns else 0.0,
            "median_trailing_buffer_target_clipped_attempted": float(g["trailing_buffer_target_clipped_attempted"].median()) if "trailing_buffer_target_clipped_attempted" in g.columns else 0.0,
            "median_skip_projected_trailing_buffer_reserve": float(g["skip_projected_trailing_buffer_reserve"].median()) if "skip_projected_trailing_buffer_reserve" in g.columns else 0.0,
            "min_available_new_risk_at_attempt": float(g["min_available_new_risk_at_attempt"].min()) if "min_available_new_risk_at_attempt" in g.columns else np.nan,
            "min_projected_trailing_buffer_after_accept": float(g["min_projected_trailing_buffer_after_accept"].min()) if "min_projected_trailing_buffer_after_accept" in g.columns else np.nan,
            "min_required_trailing_buffer_reserve_after_accept": float(g["min_required_trailing_buffer_reserve_after_accept"].min()) if "min_required_trailing_buffer_reserve_after_accept" in g.columns else np.nan,
            "max_required_trailing_buffer_reserve_after_accept": float(g["max_required_trailing_buffer_reserve_after_accept"].max()) if "max_required_trailing_buffer_reserve_after_accept" in g.columns else np.nan,
            "median_avg_required_trailing_buffer_reserve_after_accept": float(g["avg_required_trailing_buffer_reserve_after_accept"].median()) if "avg_required_trailing_buffer_reserve_after_accept" in g.columns else np.nan,
            "min_projected_buffer_excess_over_required_reserve_after_accept": float(g["min_projected_buffer_excess_over_required_reserve_after_accept"].min()) if "min_projected_buffer_excess_over_required_reserve_after_accept" in g.columns else np.nan,
            "max_open_trades_observed": int(g["max_open_trades"].max()),
        })
        rows.append(row)
    return pd.DataFrame(rows)

def build_ranked_summary(agg_cost: pd.DataFrame) -> pd.DataFrame:
    meta_cols = [
        "variant_label", "variant_name", "source_scenario_id", "source_candidate_kind", "return_mode", "universe",
        "profile_role", "linkage_method", "risk_sizing_mode", "risk_settings_source",
        "source_step08_base_portfolio_risk_budget_pct", "source_step08_max_single_trade_risk_pct",
        "source_step08_overlap_mode", "source_step08_max_min_lot_risk_inflation",
        "base_portfolio_risk_budget_pct", "base_risk_pct",
        "max_single_trade_risk_pct", "max_min_lot_risk_inflation",
        "min_lot_risk_inflation_guard_enabled", "cap_name", "guard_profile", "throttle_name",
        "throttle_basis", "throttle_configuration_basis", "throttle_reference_initial_overall_dd_buffer_amount",
        "throttle_tiers_fraction_of_initial_dd_buffer",
        "trailing_buffer_admission_enabled", "trailing_buffer_reserve_mode",
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer",
        "trailing_buffer_fixed_reserve_amount", "trailing_buffer_cost_allowance_r",
        "trailing_buffer_reserve_amount", "trailing_buffer_admission_policy", "target_lock_name",
        "pause_below_balance", "account_rule_profile", "account_stage", "daily_drawdown_name", "daily_drawdown_enabled",
        "daily_drawdown_pct", "daily_drawdown_mode", "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
    ]
    base = agg_cost[agg_cost["cost_stress_r"] == 0.0].copy()
    if base.empty:
        raise RuntimeError("Ranking requires cost_stress_r == 0.0 rows.")
    out = base[meta_cols].copy()
    metrics = [
        "pass_rate", "fail_rate", "hard_fail_rate", "max_drawdown_fail_rate", "daily_drawdown_fail_rate",
        "end_of_history_fail_rate", "median_trading_days_to_target", "mean_trading_days_to_target",
        "p25_trading_days_to_target", "p75_trading_days_to_target", "p95_trading_days_to_target",
        "p99_trading_days_to_target", "worst_trading_days_to_target", "prob_pass_under_10_trading_days",
        "prob_pass_under_20_trading_days", "prob_pass_under_30_trading_days", "prob_pass_under_40_trading_days",
        "worst_buffer_above_floor", "median_worst_buffer_above_floor", "daily_worst_buffer_above_floor",
        "max_daily_drawdown_pct", "max_daily_symbol_risk_pct_observed",
        "funded_reference_symbol_risk_exceed_rate", "median_skipped_daily_symbol_risk_cap",
        "max_observed_open_risk_pct", "median_max_open_risk_pct", "median_closed_trades",
        "median_closed_trades_per_active_day", "median_accepted_trades", "median_accepted_trades_per_active_day",
        "median_avg_afml_bet_size", "median_raw_trade_risk_pct_before_single_cap", "median_target_trade_risk_pct",
        "median_final_accepted_trade_risk_pct", "median_actual_trade_risk_pct", "max_actual_trade_risk_pct",
        "median_single_trade_clip_rate_accepted", "max_single_trade_clip_rate_accepted",
        "median_min_lot_binding_rate", "median_risk_inflation_accepted", "p90_risk_inflation_accepted",
        "max_risk_inflation_accepted", "median_skipped_min_lot_risk_cap", "median_skipped_invalid_broker_risk",
        "median_trailing_buffer_target_clip_rate_attempted",
        "median_trailing_buffer_target_clipped_attempted",
        "median_skip_projected_trailing_buffer_reserve",
        "min_available_new_risk_at_attempt",
        "min_projected_trailing_buffer_after_accept",
        "min_required_trailing_buffer_reserve_after_accept",
        "max_required_trailing_buffer_reserve_after_accept",
        "median_avg_required_trailing_buffer_reserve_after_accept",
        "min_projected_buffer_excess_over_required_reserve_after_accept",
        "median_throttle_multiplier", "median_throttle_multiplier_attempted",
        "median_throttle_activation_rate_attempted", "median_throttle_activation_rate_accepted",
        "min_trailing_buffer_at_trade_attempt", "min_trailing_buffer_at_trade_accept",
        "median_target_lock_multiplier", "max_open_trades_observed",
    ]
    metrics = [m for m in metrics if m in base.columns]
    for m in metrics:
        out[f"base_{m}"] = base[m].values
    for cost in sorted([c for c in agg_cost["cost_stress_r"].dropna().unique() if float(c) != 0.0]):
        tag = normalize_cost(float(cost))
        cdf = agg_cost[agg_cost["cost_stress_r"] == cost][["variant_label"] + metrics].copy().rename(columns={m: f"{tag}_{m}" for m in metrics})
        out = out.merge(cdf, on="variant_label", how="left")

    hard_cols_selection = [f"{normalize_cost(float(c))}_hard_fail_rate" for c in SELECTION_COSTS_FOR_HARD_FAIL_GATE if f"{normalize_cost(float(c))}_hard_fail_rate" in out.columns]
    buffer_cols_selection = [f"{normalize_cost(float(c))}_worst_buffer_above_floor" for c in SELECTION_COSTS_FOR_HARD_FAIL_GATE if f"{normalize_cost(float(c))}_worst_buffer_above_floor" in out.columns]
    pass_cols_selection = [f"{normalize_cost(float(c))}_pass_rate" for c in SELECTION_COSTS_FOR_HARD_FAIL_GATE if f"{normalize_cost(float(c))}_pass_rate" in out.columns]
    end_cols_selection = [f"{normalize_cost(float(c))}_end_of_history_fail_rate" for c in SELECTION_COSTS_FOR_HARD_FAIL_GATE if f"{normalize_cost(float(c))}_end_of_history_fail_rate" in out.columns]
    daily_cols_selection = [f"{normalize_cost(float(c))}_daily_drawdown_fail_rate" for c in SELECTION_COSTS_FOR_HARD_FAIL_GATE if f"{normalize_cost(float(c))}_daily_drawdown_fail_rate" in out.columns]
    all_hard_cols = [c for c in out.columns if c.endswith("_hard_fail_rate")]
    all_buffer_cols = [c for c in out.columns if c.endswith("_worst_buffer_above_floor")]
    all_pass_cols = [c for c in out.columns if c.endswith("_pass_rate") and not c.endswith("hard_fail_rate")]
    all_end_cols = [c for c in out.columns if c.endswith("_end_of_history_fail_rate")]
    all_daily_cols = [c for c in out.columns if c.endswith("_daily_drawdown_fail_rate")]

    out["max_hard_fail_rate_selection_costs"] = out[hard_cols_selection].max(axis=1) if hard_cols_selection else np.nan
    out["min_worst_buffer_selection_costs"] = out[buffer_cols_selection].min(axis=1) if buffer_cols_selection else np.nan
    out["min_pass_rate_selection_costs"] = out[pass_cols_selection].min(axis=1) if pass_cols_selection else np.nan
    out["max_end_of_history_fail_rate_selection_costs"] = out[end_cols_selection].max(axis=1) if end_cols_selection else np.nan
    out["max_daily_drawdown_fail_rate_selection_costs"] = out[daily_cols_selection].max(axis=1) if daily_cols_selection else 0.0
    out["max_hard_fail_rate_all_costs"] = out[all_hard_cols].max(axis=1) if all_hard_cols else np.nan
    out["min_worst_buffer_all_costs"] = out[all_buffer_cols].min(axis=1) if all_buffer_cols else np.nan
    out["min_pass_rate_all_costs"] = out[all_pass_cols].min(axis=1) if all_pass_cols else np.nan
    out["max_end_of_history_fail_rate_all_costs"] = out[all_end_cols].max(axis=1) if all_end_cols else np.nan
    out["max_daily_drawdown_fail_rate_all_costs"] = out[all_daily_cols].max(axis=1) if all_daily_cols else 0.0

    out["selection_tier"] = "tier_5_review"
    out["selection_tier_rank"] = 5
    zero_all_positive = (out["max_hard_fail_rate_all_costs"].fillna(1.0) <= EPS) & (out["max_daily_drawdown_fail_rate_all_costs"].fillna(1.0) <= EPS) & (out["min_worst_buffer_all_costs"].fillna(-999999.0) >= 0.0)
    zero_selection_positive = (out["max_hard_fail_rate_selection_costs"].fillna(1.0) <= EPS) & (out["max_daily_drawdown_fail_rate_selection_costs"].fillna(1.0) <= EPS) & (out["min_worst_buffer_selection_costs"].fillna(-999999.0) >= 0.0)
    zero_selection = (out["max_hard_fail_rate_selection_costs"].fillna(1.0) <= EPS) & (out["max_daily_drawdown_fail_rate_selection_costs"].fillna(1.0) <= EPS)
    low_hard_selection = (out["max_hard_fail_rate_selection_costs"].fillna(1.0) <= 0.01) & (out["max_daily_drawdown_fail_rate_selection_costs"].fillna(1.0) <= 0.01)
    moderate_hard_selection = (out["max_hard_fail_rate_selection_costs"].fillna(1.0) <= 0.03) & (out["max_daily_drawdown_fail_rate_selection_costs"].fillna(1.0) <= 0.03)
    out.loc[moderate_hard_selection, ["selection_tier", "selection_tier_rank"]] = ["tier_4_moderate_hard_fail_selection", 4]
    out.loc[low_hard_selection, ["selection_tier", "selection_tier_rank"]] = ["tier_3_low_hard_fail_selection", 3]
    out.loc[zero_selection, ["selection_tier", "selection_tier_rank"]] = ["tier_2_zero_hard_fail_selection_costs", 2]
    out.loc[zero_selection_positive, ["selection_tier", "selection_tier_rank"]] = ["tier_1_zero_hard_fail_selection_positive_buffer", 1]
    out.loc[zero_all_positive, ["selection_tier", "selection_tier_rank"]] = ["tier_0_zero_hard_fail_all_costs_positive_buffer", 0]
    sort_cols = [
        "selection_tier_rank", "base_median_trading_days_to_target", "base_prob_pass_under_20_trading_days",
        "base_prob_pass_under_30_trading_days", "base_pass_rate", "min_pass_rate_selection_costs",
        "max_end_of_history_fail_rate_selection_costs", "max_hard_fail_rate_selection_costs",
        "max_daily_drawdown_fail_rate_selection_costs", "min_worst_buffer_selection_costs",
        "max_hard_fail_rate_all_costs", "base_max_observed_open_risk_pct", "base_median_skipped_min_lot_risk_cap",
        "max_single_trade_risk_pct",
    ]
    sort_cols = [c for c in sort_cols if c in out.columns]
    ascending_map = {
        "selection_tier_rank": True,
        "base_median_trading_days_to_target": True,
        "base_prob_pass_under_20_trading_days": False,
        "base_prob_pass_under_30_trading_days": False,
        "base_pass_rate": False,
        "min_pass_rate_selection_costs": False,
        "max_end_of_history_fail_rate_selection_costs": True,
        "max_hard_fail_rate_selection_costs": True,
        "max_daily_drawdown_fail_rate_selection_costs": True,
        "min_worst_buffer_selection_costs": False,
        "max_hard_fail_rate_all_costs": True,
        "base_max_observed_open_risk_pct": True,
        "base_median_skipped_min_lot_risk_cap": True,
        "max_single_trade_risk_pct": True,
    }
    out = out.sort_values(sort_cols, ascending=[ascending_map[c] for c in sort_cols], na_position="last").copy()
    out["fast_manual_rank"] = np.arange(1, len(out) + 1)
    return out

def best_and_worst_starts(run_df: pd.DataFrame, ranked: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    top_ids = ranked.head(20)["variant_label"].tolist()
    base = run_df[(run_df["variant_label"].isin(top_ids)) & (run_df["cost_stress_r"] == 0.0)].copy()
    if base.empty:
        return pd.DataFrame(), pd.DataFrame()
    best = base[base["passed"]].sort_values(["variant_label", "trading_days_to_target", "worst_buffer_above_floor"], ascending=[True, True, False]).groupby("variant_label", as_index=False).head(5)
    worst = base.sort_values(["variant_label", "worst_buffer_above_floor", "ending_balance"], ascending=[True, True, True]).groupby("variant_label", as_index=False).head(5)
    return best, worst


def bootstrap_top(stage2_run: pd.DataFrame, ranked: pd.DataFrame) -> pd.DataFrame:
    if RUN_BOOTSTRAP_ON_TOP_N <= 0 or BOOTSTRAP_SAMPLES <= 0:
        return pd.DataFrame()
    top_ids = ranked.head(RUN_BOOTSTRAP_ON_TOP_N)["variant_label"].tolist()
    base_runs = stage2_run[(stage2_run["variant_label"].isin(top_ids)) & (stage2_run["cost_stress_r"] == 0.0)].copy()
    if base_runs.empty:
        return pd.DataFrame()
    rng = np.random.default_rng(BOOTSTRAP_RANDOM_SEED)
    rows = []
    for variant_label_, g in base_runs.groupby("variant_label"):
        passed = g["passed"].astype(int).to_numpy(); hard_failed = g["hard_failed"].astype(int).to_numpy(); end_failed = g["end_of_history_fail"].astype(int).to_numpy(); days = pd.to_numeric(g["trading_days_to_target"], errors="coerce").to_numpy(); n = len(g)
        pass_rates, hard_rates, end_rates, median_days = [], [], [], []
        for _ in range(BOOTSTRAP_SAMPLES):
            idx = rng.integers(0, n, size=n)
            pass_rates.append(passed[idx].mean()); hard_rates.append(hard_failed[idx].mean()); end_rates.append(end_failed[idx].mean())
            d = days[idx]; d = d[np.isfinite(d)]
            median_days.append(np.median(d) if len(d) else np.nan)
        finite_days = np.array(median_days, dtype=float)
        rows.append({"variant_label": variant_label_, "bootstrap_samples": BOOTSTRAP_SAMPLES, "pass_rate_ci05": float(np.nanquantile(pass_rates, 0.05)), "pass_rate_ci50": float(np.nanquantile(pass_rates, 0.50)), "pass_rate_ci95": float(np.nanquantile(pass_rates, 0.95)), "hard_fail_rate_ci05": float(np.nanquantile(hard_rates, 0.05)), "hard_fail_rate_ci50": float(np.nanquantile(hard_rates, 0.50)), "hard_fail_rate_ci95": float(np.nanquantile(hard_rates, 0.95)), "end_of_history_fail_rate_ci05": float(np.nanquantile(end_rates, 0.05)), "end_of_history_fail_rate_ci50": float(np.nanquantile(end_rates, 0.50)), "end_of_history_fail_rate_ci95": float(np.nanquantile(end_rates, 0.95)), "median_days_ci05": float(np.nanquantile(finite_days, 0.05)) if np.isfinite(finite_days).any() else np.nan, "median_days_ci50": float(np.nanquantile(finite_days, 0.50)) if np.isfinite(finite_days).any() else np.nan, "median_days_ci95": float(np.nanquantile(finite_days, 0.95)) if np.isfinite(finite_days).any() else np.nan})
    return pd.DataFrame(rows)


def row_payload(v: dict[str, Any], selected: dict[str, Any], result: dict[str, Any], cost: float) -> dict[str, Any]:
    return {
        "variant_label": variant_label(v),
        "variant_name": v["name"],
        "source_scenario_id": selected.get("scenario_id", ""),
        "source_candidate_kind": selected.get("step08_candidate_kind", ""),
        "return_mode": selected["return_mode"],
        "universe": selected["universe"],
        "profile_role": selected["profile_role"],
        "linkage_method": selected["linkage_method"],
        "risk_sizing_mode": RISK_SIZING_MODE,
        "risk_settings_source": v.get("risk_settings_source", "manual_or_generated_variation"),
        "source_step08_base_portfolio_risk_budget_pct": float(v.get("source_step08_base_portfolio_risk_budget_pct", selected["base_portfolio_risk_budget_pct"])),
        "source_step08_max_single_trade_risk_pct": float(v.get("source_step08_max_single_trade_risk_pct", selected["max_single_trade_risk_pct"])),
        "source_step08_overlap_mode": str(v.get("source_step08_overlap_mode", selected.get("overlap_mode", ""))),
        "source_step08_max_min_lot_risk_inflation": optional_float(
            v.get(
                "source_step08_max_min_lot_risk_inflation",
                selected.get("max_min_lot_risk_inflation"),
            )
        ),
        "base_portfolio_risk_budget_pct": float(v["base_portfolio_risk_budget_pct"]),
        "base_risk_pct": float(v["base_portfolio_risk_budget_pct"]),
        "max_single_trade_risk_pct": float(v["max_single_trade_risk_pct"]),
        "max_min_lot_risk_inflation": optional_float(
            v.get("max_min_lot_risk_inflation")
        ),
        "min_lot_risk_inflation_guard_enabled": bool(
            optional_float(v.get("max_min_lot_risk_inflation")) is not None
        ),
        "cap_name": v["cap_name"],
        "guard_profile": v["guard_profile"],
        "throttle_name": v["throttle_name"],
        "throttle_basis": v.get("throttle_basis", THROTTLE_BASIS),
        "throttle_configuration_basis": v.get("throttle_configuration_basis", THROTTLE_CONFIGURATION_BASIS),
        "throttle_reference_initial_overall_dd_buffer_amount": float(v.get("throttle_reference_initial_overall_dd_buffer_amount", INITIAL_OVERALL_DD_BUFFER_AMOUNT)),
        "throttle_tiers_fraction_of_initial_dd_buffer": json.dumps(v.get("throttle_tiers_fraction_of_initial_dd_buffer", [])),
        "trailing_buffer_admission_enabled": bool(v.get("trailing_buffer_admission_enabled", False)),
        "trailing_buffer_reserve_mode": str(v.get("trailing_buffer_reserve_mode", "off")),
        "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": float(v.get("trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer", 0.0) or 0.0),
        "trailing_buffer_fixed_reserve_amount": float(v.get("trailing_buffer_fixed_reserve_amount", 0.0) or 0.0),
        "trailing_buffer_cost_allowance_r": float(v.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0),
        "trailing_buffer_reserve_amount": float(v.get("trailing_buffer_reserve_amount", 0.0) or 0.0),
        "trailing_buffer_admission_policy": str(v.get("trailing_buffer_admission_policy", "off")),
        "daily_buffer_admission_enabled": bool(v.get("daily_buffer_admission_enabled", False)),
        "daily_buffer_reserve_mode": str(v.get("daily_buffer_reserve_mode", "off")),
        "daily_buffer_fixed_reserve_pct_of_initial_dd_buffer": float(v.get("daily_buffer_fixed_reserve_pct_of_initial_dd_buffer", 0.0) or 0.0),
        "daily_buffer_fixed_reserve_amount": float(v.get("daily_buffer_fixed_reserve_amount", 0.0) or 0.0),
        "daily_buffer_cost_allowance_r": float(v.get("daily_buffer_cost_allowance_r", 0.0) or 0.0),
        "daily_buffer_admission_policy": str(v.get("daily_buffer_admission_policy", "off")),
        "target_lock_name": v["target_lock_name"],
        "pause_below_balance": float(v["pause_below_balance"]),
        "account_rule_profile": v.get("account_rule_profile", ACCOUNT_RULE_PROFILE),
        "account_stage": v.get("account_stage", ACCOUNT_STAGE),
        "daily_drawdown_name": v.get("daily_drawdown_name", ACCOUNT_RULE_PROFILE),
        "daily_drawdown_enabled": bool(v.get("daily_drawdown_enabled", False)),
        "daily_drawdown_pct": float(v.get("daily_drawdown_pct", 0.0) or 0.0),
        "daily_drawdown_mode": str(v.get("daily_drawdown_mode", "off")),
        "daily_symbol_risk_rule_enabled": bool(v.get("daily_symbol_risk_rule_enabled", False)),
        "daily_symbol_risk_enforcement_mode": str(v.get("daily_symbol_risk_enforcement_mode", ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])),
        **result,
    }


_POOL09_ARR: dict[str, Any] | None = None
_POOL09_BASE_RAW_PCT: np.ndarray | None = None
_POOL09_CODE_TO_ASSET: dict[int, str] | None = None
_POOL09_DIMS: tuple[int, int, int] | None = None


def _pool09_init(
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    code_to_asset: dict[int, str],
    n_assets: int,
    n_families: int,
    n_edges_global: int,
) -> None:
    """ProcessPoolExecutor initializer: stashes the (large, read-only, shared
    across every variant/cost/start) ledger-derived arrays in each worker
    process ONCE, so per-task payloads only carry the small (variant, cost,
    start-chunk) pieces."""
    global _POOL09_ARR, _POOL09_BASE_RAW_PCT, _POOL09_CODE_TO_ASSET, _POOL09_DIMS
    _POOL09_ARR = arr
    _POOL09_BASE_RAW_PCT = base_raw_pct
    _POOL09_CODE_TO_ASSET = code_to_asset
    _POOL09_DIMS = (n_assets, n_families, n_edges_global)


def _pool09_run_chunk(
    args: tuple[dict[str, Any], dict[str, Any], list[int], list[float]],
) -> list[dict[str, Any]]:
    """Worker task: run one variant across one slice of its rolling starts
    (all cost-stress values). Pure function of its arguments plus the
    process-local arrays stashed by _pool09_init -- simulate_from_start has
    no RNG and no shared mutable state, so results are identical regardless
    of chunking/worker assignment (verified against the serial baseline)."""
    v, selected, start_slice, cost_values = args
    assert _POOL09_ARR is not None and _POOL09_BASE_RAW_PCT is not None and _POOL09_CODE_TO_ASSET is not None and _POOL09_DIMS is not None
    n_assets, n_families, n_edges_global = _POOL09_DIMS
    rows = []
    for cost in cost_values:
        for start_ns in start_slice:
            res = simulate_from_start(
                _POOL09_ARR, _POOL09_BASE_RAW_PCT, v, int(start_ns), float(cost),
                n_assets, n_families, n_edges_global, _POOL09_CODE_TO_ASSET,
            )
            rows.append(row_payload(v, selected, res, float(cost)))
    return rows


def run_variant_set(
    variants: list[dict[str, Any]],
    arr: dict[str, Any],
    base_raw_pct: np.ndarray,
    starts: list[int],
    costs: list[float],
    n_assets: int,
    n_families: int,
    n_edges_global: int,
    code_to_asset: dict[int, str],
    selected: dict[str, Any],
    desc: str,
    *,
    n_workers: int = 1,
    chunk_size: int = 25,
) -> pd.DataFrame:
    """n_workers<=1 runs the original single-process loop (used for small
    jobs and for cross-checking parallel output against serial ground
    truth). n_workers>1 fans the (variant, cost, start-chunk) work out
    across a ProcessPoolExecutor -- fine-grained enough that the speedup
    scales with n_workers regardless of how many variants are being tested
    (relevant once FAST_PROBE_VARIANTS holds more than one spec)."""
    if n_workers <= 1:
        rows = []
        for v in tqdm(variants, desc=desc, unit="variant"):
            for cost in costs:
                for start_ns in starts:
                    rows.append(row_payload(v, selected, simulate_from_start(arr, base_raw_pct, v, int(start_ns), float(cost), n_assets, n_families, n_edges_global, code_to_asset), float(cost)))
        return pd.DataFrame(rows)

    tasks: list[tuple[dict[str, Any], dict[str, Any], list[int], list[float]]] = []
    for v in variants:
        for i in range(0, len(starts), chunk_size):
            tasks.append((v, selected, list(starts[i:i + chunk_size]), list(costs)))

    total = len(variants) * len(starts) * len(costs)
    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_pool09_init,
        initargs=(arr, base_raw_pct, code_to_asset, n_assets, n_families, n_edges_global),
    ) as executor:
        futures = [executor.submit(_pool09_run_chunk, task) for task in tasks]
        with tqdm(total=total, desc=f"{desc} [{n_workers} workers]", unit="run") as pbar:
            for future in as_completed(futures):
                chunk_rows = future.result()
                rows.extend(chunk_rows)
                pbar.update(len(chunk_rows))
    return pd.DataFrame(rows)



def validate_all_cost_run_grid(
    runs: pd.DataFrame,
    variants: list[dict[str, Any]],
    starts: list[int],
) -> None:
    """Fail closed unless every requested cost/start/variant combination ran.

    This prevents a two-cost run from being mislabeled and approved as a
    0.00R/0.02R/0.05R validation.
    """
    if runs.empty:
        raise RuntimeError("Script 09 produced no all-cost simulation rows.")
    if "cost_stress_r" not in runs.columns:
        raise RuntimeError("Script 09 all-cost results lack cost_stress_r.")

    expected_costs = sorted(float(x) for x in STAGE2_COST_STRESS_R_PER_TRADE)
    observed_costs = sorted(
        float(x)
        for x in pd.to_numeric(runs["cost_stress_r"], errors="coerce")
        .dropna()
        .unique()
        .tolist()
    )

    costs_match = (
        len(observed_costs) == len(expected_costs)
        and all(
            np.isclose(observed, expected, rtol=0.0, atol=EPS)
            for observed, expected in zip(observed_costs, expected_costs)
        )
    )
    if not costs_match:
        raise RuntimeError(
            "Script 09 cost-grid mismatch. "
            f"Expected {expected_costs}, observed {observed_costs}. "
            "No candidate may be approved until all configured costs run."
        )

    expected_rows = int(len(variants) * len(starts) * len(expected_costs))
    if len(runs) != expected_rows:
        raise RuntimeError(
            "Script 09 all-cost row-count mismatch. "
            f"Expected {expected_rows} = {len(variants)} variant(s) × "
            f"{len(starts)} start(s) × {len(expected_costs)} cost(s), "
            f"observed {len(runs)}."
        )

    duplicate_keys = [
        key for key in ["variant_label", "start_ns", "cost_stress_r"]
        if key in runs.columns
    ]
    if len(duplicate_keys) == 3 and runs.duplicated(duplicate_keys).any():
        duplicate_count = int(runs.duplicated(duplicate_keys).sum())
        raise RuntimeError(
            "Script 09 all-cost grid contains duplicate "
            f"variant/start/cost rows: {duplicate_count}."
        )


def select_stage2_variants(stage1_agg: pd.DataFrame, variants: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    if not RUN_STAGE1 or stage1_agg.empty:
        return variants, pd.DataFrame()
    ranked = build_ranked_summary(stage1_agg)
    labels = ranked.head(STAGE2_TOP_N_VARIANTS)["variant_label"].tolist()
    selected = [v for v in variants if variant_label(v) in set(labels)]
    if not selected:
        raise RuntimeError("Stage 1 did not select any variants for Stage 2.")
    return selected, ranked


# Explicit, opt-in, logged human override: this project's default gate
# requires LITERALLY zero hard-failures across every tested rolling start and
# cost stress before approving a candidate for Script 10. That is not always
# achievable without trading away most of the speed a fast risk budget buys
# (see 08c_joint_risk_throttle_grid_search.py's findings on
# pairwise_cap1p70_budget100_trade20). When set, this constant accepts a
# candidate whose hard-fail rate is capped at MODERATE_HARD_FAIL_TOLERANCE
# (matching build_ranked_summary's own "tier_4_moderate_hard_fail_selection"
# definition) at every cost stress, with no reserve-contract violations, as
# a deliberate, informed trade of a small amount of historical tail risk
# for materially faster challenge completion. Toggle via
# --accept-moderate-hard-fail; the default stays strict.
#
# 2026-09-25: previously this override ALSO required daily-drawdown-fail to
# be literally zero even when accept_moderate_hard_fail was set -- stricter
# than Scripts 10/11's own gates, which never check daily-drawdown-fail
# separately at all (hard_failed = max_drawdown_failed OR daily_drawdown_
# failed OR daily_symbol_risk_failed is already computed per-run upstream,
# so daily-drawdown-fail is already a SUBSET of hard_fail_rate, not an
# independent risk dimension). That extra check made this function's own
# docstring claim -- "the same...gate that Script 10 ultimately requires"
# -- false, and caused Script 10 to hard-refuse a candidate the user had
# explicitly reviewed and accepted (2026-09-25 "Option A" decision: a small
# nonzero daily-drawdown-fail rate, in this case 1.25%/5 of 400 paths, all
# tracing to one historical multi-asset stress date, is covered under the
# overall <=3% ceiling and does not need to be driven to literal zero).
# The override now applies the SAME MODERATE_HARD_FAIL_TOLERANCE to
# daily-drawdown-fail that it already applies to hard-fail, matching
# Scripts 10/11's real behavior exactly. See feedback_prop_challenge_
# speed_priority.md in the project's Claude memory for the full decision.
ACCEPT_MODERATE_HARD_FAIL = False
MODERATE_HARD_FAIL_TOLERANCE = 0.03


def evaluate_single_variant_gate(
    ranked: pd.DataFrame,
    all_cost_runs: pd.DataFrame,
    run_mode: str,
    *,
    accept_moderate_hard_fail: bool = False,
) -> dict[str, Any]:
    """Apply the same zero-hard-failure gate that Script 10 ultimately requires,
    with an optional explicit human override (see ACCEPT_MODERATE_HARD_FAIL)."""
    if ranked.empty:
        return {
            "approved": False,
            "status": "NO_RESULT",
            "reasons": ["ranked summary is empty"],
            "reserve_contract_violation_count": 0,
        }
    row = ranked.iloc[0]
    reasons: list[str] = []
    max_hard = optional_float(row.get("max_hard_fail_rate_all_costs"))
    max_daily = optional_float(row.get("max_daily_drawdown_fail_rate_all_costs"))
    min_buffer = optional_float(row.get("min_worst_buffer_all_costs"))
    reserve_excess = pd.to_numeric(
        all_cost_runs.get(
            "min_projected_buffer_excess_over_required_reserve_after_accept",
            pd.Series(np.nan, index=all_cost_runs.index),
        ),
        errors="coerce",
    )
    reserve_violations = int((reserve_excess < -EPS).fillna(False).sum())

    if max_hard is None or max_hard > EPS:
        reasons.append(f"hard failures remain across the 0.00/0.02/0.05R grid: {max_hard}")
    if max_daily is None or max_daily > EPS:
        reasons.append(f"daily-drawdown failures remain across the grid: {max_daily}")
    if min_buffer is None or min_buffer < -EPS:
        reasons.append(f"minimum trailing buffer is negative: {min_buffer}")
    if reserve_violations:
        reasons.append(f"accepted-trade reserve contract violations: {reserve_violations}")

    approved = not reasons
    manual_override_applied = False
    if (
        not approved
        and accept_moderate_hard_fail
        and max_hard is not None
        and max_hard <= MODERATE_HARD_FAIL_TOLERANCE + EPS
        and (max_daily is not None and max_daily <= MODERATE_HARD_FAIL_TOLERANCE + EPS)
        and not reserve_violations
    ):
        approved = True
        manual_override_applied = True
        reasons = reasons + [
            f"MANUAL OVERRIDE ACCEPTED: hard-fail <= {MODERATE_HARD_FAIL_TOLERANCE:.2%} "
            f"(observed max {max_hard:.4f}) and daily-drawdown-fail <= {MODERATE_HARD_FAIL_TOLERANCE:.2%} "
            f"(observed max {max_daily:.4f}) at every cost stress, accepted as a deliberate "
            "speed/tail-risk trade-off; not a literal zero-failure pass."
        ]

    if run_mode == "full_single":
        if approved and manual_override_applied:
            status = "APPROVED_FOR_SCRIPT10_MANUAL_OVERRIDE_MODERATE_HARD_FAIL"
        elif approved:
            status = "APPROVED_FOR_SCRIPT10"
        else:
            status = "TESTED_NOT_APPROVED"
    else:
        status = "QUICK_PROBE_PROMISING" if approved else "QUICK_PROBE_FAILED"
    return {
        "approved": bool(approved),
        "manual_override_applied": bool(manual_override_applied),
        "status": status,
        "reasons": reasons,
        "reserve_contract_violation_count": reserve_violations,
        "max_hard_fail_rate_all_costs": max_hard,
        "max_daily_drawdown_fail_rate_all_costs": max_daily,
        "min_worst_buffer_all_costs": min_buffer,
    }


def build_trial_history_row(
    trial_id: str,
    run_mode: str,
    selected_variant: dict[str, Any],
    ranked: pd.DataFrame,
    gate: dict[str, Any],
    starts: list[int],
    all_cost_runs: pd.DataFrame,
) -> dict[str, Any]:
    summary = ranked.iloc[0].to_dict() if not ranked.empty else {}
    row: dict[str, Any] = {
        "trial_id": trial_id,
        "created_at": now_utc(),
        "run_mode": run_mode,
        "variant_name": selected_variant.get("name"),
        "variant_label": variant_label(selected_variant),
        "guard_profile": selected_variant.get("guard_profile"),
        "throttle_name": selected_variant.get("throttle_name"),
        "reserve_mode": selected_variant.get("trailing_buffer_reserve_mode"),
        "fixed_reserve_pct_of_initial_dd_buffer": selected_variant.get("trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer"),
        "fixed_reserve_amount": selected_variant.get("trailing_buffer_fixed_reserve_amount"),
        "cost_allowance_r": selected_variant.get("trailing_buffer_cost_allowance_r"),
        "rolling_starts": len(starts),
        "all_cost_run_rows": len(all_cost_runs),
        "approval_status": gate.get("status"),
        "approved_for_script10": bool(run_mode == "full_single" and gate.get("approved")),
        "gate_reasons": " | ".join(gate.get("reasons", [])),
        "reserve_contract_violation_count": gate.get("reserve_contract_violation_count", 0),
    }
    for key in [
        "base_pass_rate", "base_hard_fail_rate", "base_end_of_history_fail_rate",
        "base_median_trading_days_to_target", "base_worst_buffer_above_floor",
        "cost0p02_pass_rate", "cost0p02_hard_fail_rate", "cost0p02_end_of_history_fail_rate",
        "cost0p02_median_trading_days_to_target", "cost0p02_worst_buffer_above_floor",
        "cost0p05_pass_rate", "cost0p05_hard_fail_rate", "cost0p05_end_of_history_fail_rate",
        "cost0p05_median_trading_days_to_target", "cost0p05_worst_buffer_above_floor",
        "max_hard_fail_rate_all_costs", "min_worst_buffer_all_costs", "selection_tier",
    ]:
        row[key] = summary.get(key)
    return row


def append_trial_history(row: dict[str, Any]) -> None:
    new = pd.DataFrame([row])
    if TRIAL_HISTORY_CSV.exists():
        try:
            old = pd.read_csv(TRIAL_HISTORY_CSV)
            new = pd.concat([old, new], ignore_index=True, sort=False)
        except Exception:
            pass
    new.to_csv(TRIAL_HISTORY_CSV, index=False)


def build(
    write: bool = True,
    n_workers: int = 1,
    accept_moderate_hard_fail: bool = False,
    manual_step08_candidate: str | None = None,
    manual_variant_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """manual_step08_candidate / manual_variant_spec are the explicit human
    override this project's automated 08 -> 08c -> 09 chain is missing
    without them: a human who has reviewed 08c's full joint_grid_ranked.csv
    (or Step 08's own stage2_aggregate.csv) can send a DIFFERENT Step08
    candidate and/or guard/reserve combo through the same validated Step
    09/10/11 pipeline the automated winner would have used, instead of
    accepting the automated search's pick. Both are optional and independent
    -- pass one, the other, both, or neither (the default: fully automated).
    """
    global FAST_PROBE_VARIANTS
    ensure_dirs()
    validate_account_start_state()
    step08_manifest_contract = load_step08_manifest_contract()
    manual_candidate_override_applied = bool(manual_step08_candidate)
    if manual_step08_candidate:
        selected = load_step08_candidate_by_name(manual_step08_candidate)
        selected_kind = "manual_override"
    else:
        selected, selected_kind = load_step08_selected()

    manual_guard_reserve_override_applied = manual_variant_spec is not None
    active_variant_spec = manual_variant_spec if manual_variant_spec is not None else ACTIVE_VARIANT_SPEC
    _original_fast_probe_variants = FAST_PROBE_VARIANTS
    FAST_PROBE_VARIANTS = [active_variant_spec]
    try:
        variants = build_variants_from_selected(selected)
    finally:
        FAST_PROBE_VARIANTS = _original_fast_probe_variants
    if len(variants) != 1:
        raise RuntimeError(
            f"Single-variant mode requires exactly one ACTIVE_VARIANT_SPEC; generated {len(variants)} variants."
        )
    for v in variants:
        validate_variant(v)

    ledger, asset_to_code, family_to_code, edge_to_code = load_ledger(INPUT_LEDGER)
    weights = load_hrp_weights(INPUT_HRP_WEIGHTS)
    arr = prepare_selected_arrays(selected, ledger, weights)
    base_raw_pct = arr["hrp_weight"].astype(float) * arr["afml_bet_size"].astype(float)
    n_assets, n_families, n_edges_global = len(asset_to_code), len(family_to_code), len(edge_to_code)
    code_to_asset = {v: k for k, v in asset_to_code.items()}

    stage1_starts: list[int] = []
    stage2_starts = build_rolling_start_times(
        arr, STAGE2_START_EVERY_N_ACTIVE_DAYS, STAGE2_MAX_ROLLING_STARTS
    )
    stage1_run = pd.DataFrame()
    stage1_agg = pd.DataFrame()
    stage1_ranked = pd.DataFrame()
    stage2_variants = variants

    all_cost_runs = run_variant_set(
        stage2_variants,
        arr,
        base_raw_pct,
        stage2_starts,
        STAGE2_COST_STRESS_R_PER_TRADE,
        n_assets,
        n_families,
        n_edges_global,
        code_to_asset,
        selected,
        f"single variant {ACTIVE_RUN_MODE}: 0.00/0.02/0.05R",
        n_workers=n_workers,
    )
    validate_all_cost_run_grid(
        all_cost_runs,
        stage2_variants,
        stage2_starts,
    )
    stage2_agg = aggregate_run_results(all_cost_runs)
    ranked = build_ranked_summary(stage2_agg)
    best = ranked.head(1).copy()
    best_starts, worst_starts = best_and_worst_starts(all_cost_runs, ranked)
    boot = bootstrap_top(all_cost_runs, ranked)

    # Keep the established Script 10 replay file on its two-cost contract. The
    # additional 0.05R rows remain available in the new all-cost output and drive
    # Script 09's approval gate.
    compatibility_mask = all_cost_runs["cost_stress_r"].apply(
        lambda x: any(np.isclose(float(x), c, rtol=0.0, atol=EPS) for c in DOWNSTREAM_REPLAY_COMPATIBILITY_COSTS)
    )
    stage2_run_compat = all_cost_runs[compatibility_mask].copy()

    selected_variant = variants[0]
    trial_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trial_id = f"{trial_stamp}__{slug(selected_variant['name'])}__{ACTIVE_RUN_MODE}"
    gate = evaluate_single_variant_gate(ranked, all_cost_runs, ACTIVE_RUN_MODE, accept_moderate_hard_fail=accept_moderate_hard_fail)
    full_single_approved = bool(ACTIVE_RUN_MODE == "full_single" and gate["approved"])
    manual_status = (
        "PASS" if full_single_approved else
        "FAIL" if ACTIVE_RUN_MODE == "full_single" else
        "QUICK_PROBE_PASS" if gate["approved"] else "QUICK_PROBE_FAIL"
    )
    selected_label = variant_label(selected_variant)

    active_daily_cfg = daily_drawdown_config(ACCOUNT_RULES)
    live_config = {
        "created_at": now_utc(),
        "schema_version": SCHEMA_VERSION,
        "artifact_policy": "hashless_structural_account_rules",
        "script": "09_fast_manual_variant_lab.py",
        "script_version": SCRIPT_VERSION,
        "trial_id": trial_id,
        "run_mode": ACTIVE_RUN_MODE,
        "selection_status": gate["status"],
        "approved_for_script10": full_single_approved,
        "approval_gate": gate,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "account_rule_profile": ACCOUNT_RULE_PROFILE,
        "account_stage": ACCOUNT_STAGE,
        "step08_manifest_contract": step08_manifest_contract,
        "source_scenario": selected,
        "source_candidate_kind": selected_kind,
        "step08_risk_settings_inherited": {
            "mode": FAST_PROBE_RISK_SETTINGS_SOURCE,
            "base_portfolio_risk_budget_pct": float(selected["base_portfolio_risk_budget_pct"]),
            "max_single_trade_risk_pct": float(selected["max_single_trade_risk_pct"]),
            "max_min_lot_risk_inflation": optional_float(selected.get("max_min_lot_risk_inflation")),
            "min_lot_risk_inflation_guard_enabled": bool(optional_float(selected.get("max_min_lot_risk_inflation")) is not None),
            "overlap_mode": str(selected.get("overlap_mode", "")),
            "overlap_limits": OVERLAP_MODE_LIBRARY.get(str(selected.get("overlap_mode", "")), {}),
        },
        "selected_variant_label": selected_label,
        "selected_variant": selected_variant,
        "selected_hrp_weights": arr["weights"].to_dict(orient="records"),
        "active_account_rule_contract": {
            "profile_label": ACCOUNT_RULE_PROFILE,
            "account_stage": ACCOUNT_STAGE,
            "starting_capital": PROP_START_BALANCE,
            "current_balance": CURRENT_BALANCE,
            "current_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
            "profit_target_pct": ACCOUNT_RULES["profit_target"].get("pct"),
            "target_balance": ACCOUNT_RULES["profit_target"].get("target_balance"),
            "overall_drawdown": ACCOUNT_RULES["overall_drawdown"],
            "daily_drawdown": ACCOUNT_RULES["daily_drawdown"],
            "minimum_trading_days": ACCOUNT_RULES["minimum_trading_days"],
            "maximum_risk_per_trade": ACCOUNT_RULES["maximum_risk_per_trade"],
            "daily_symbol_risk": ACCOUNT_RULES["daily_symbol_risk"],
            "floating_loss_guard": ACCOUNT_RULES["floating_loss_guard"],
        },
        "risk_formula": (
            "remaining_overall_buffer = active overall-drawdown breach value minus active floor; "
            "throttle_multiplier = percentage-of-initial-DD-buffer tier; target_risk_pct = min(base budget * "
            "HRP weight * calibrated AFML bet size * throttle * target lock, all active caps); "
            "broker-rounded actual risk drives PnL, overlap caps, daily rules and optional symbol/floating guards"
        ),
        "throttle_basis": THROTTLE_BASIS,
        "throttle_configuration_basis": THROTTLE_CONFIGURATION_BASIS,
        "throttle_library_fraction_of_initial_dd_buffer": THROTTLE_LIBRARY_FRACTION_OF_INITIAL_DD_BUFFER,
        "throttle_library_materialized_account_currency": THROTTLE_LIBRARY,
        "throttle_reference_initial_overall_dd_buffer_amount": INITIAL_OVERALL_DD_BUFFER_AMOUNT,
        "broker_volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
        "reduce_trade_to_fit_effective_risk_cap": REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP,
        "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
        "skip_trade_if_no_executable_lot_fits": SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS,
        "ceil_risk_cap_breach_policy": "backoff_to_largest_safe_executable_lot",
        "minimum_lot_fallback_preserved": True,
        "account_rule_profiles_available": ACCOUNT_RULE_PROFILES,
        "daily_drawdown_name": active_daily_cfg["name"],
        "daily_drawdown_enabled": bool(active_daily_cfg["enabled"]),
        "daily_drawdown_pct": float(active_daily_cfg["pct"]),
        "daily_drawdown_amount": float(active_daily_cfg["amount"]),
        "daily_drawdown_mode": str(active_daily_cfg["mode"]),
        "daily_drawdown_breach_basis": str(active_daily_cfg["breach_basis"]),
        "daily_reset_timezone": DAILY_RESET_TIMEZONE,
        "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
        "daily_symbol_risk_rule_enabled": bool(active_daily_cfg["daily_symbol_risk_rule_enabled"]),
        "daily_symbol_risk_enforcement_mode": str(active_daily_cfg["daily_symbol_risk_enforcement_mode"]),
        "max_daily_risk_per_symbol_pct": active_daily_cfg.get("max_daily_risk_per_symbol_pct"),
        "max_daily_risk_per_symbol_amount": active_daily_cfg.get("max_daily_risk_per_symbol_amount"),
        "internal_operating_risk_per_symbol_pct": active_daily_cfg.get("internal_operating_risk_per_symbol_pct"),
        "internal_operating_risk_per_symbol_amount": active_daily_cfg.get("internal_operating_risk_per_symbol_amount"),
        "symbol_risk_safety_reserve_amount": active_daily_cfg.get("symbol_risk_safety_reserve_amount"),
        "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
        "failure_definition": (
            "hard_fail = active overall drawdown, active daily drawdown, or enabled daily-symbol-risk breach; "
            "end_of_history_fail = configured profit target plus minimum valid days not reached before history ends"
        ),
        "tested_cost_stress_r_per_trade": STAGE2_COST_STRESS_R_PER_TRADE,
        "summary": best.iloc[0].to_dict() if not best.empty else {},
        "all_ranked_variants": ranked.to_dict(orient="records"),
    }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "script_version": SCRIPT_VERSION,
        "created_at": now_utc(),
        "trial_id": trial_id,
        "run_mode": ACTIVE_RUN_MODE,
        "pipeline_mode": PIPELINE_MODE,
        "manual_variant_lab_status": manual_status,
        "selection_status": gate["status"],
        "approved_for_script10": full_single_approved,
        "approval_gate": gate,
        "input_ledger": str(INPUT_LEDGER.relative_to(V2_ROOT)),
        "input_hrp_weights": str(INPUT_HRP_WEIGHTS.relative_to(V2_ROOT)),
        "input_step08_manifest": str(INPUT_STEP08_MANIFEST.relative_to(V2_ROOT)),
        "input_step08_fast_frontier": str(INPUT_STEP08_FAST_FRONTIER.relative_to(V2_ROOT)),
        "input_step08_fast_safe": str(INPUT_STEP08_FAST_SAFE.relative_to(V2_ROOT)),
        "input_step08_robust": str(INPUT_STEP08_ROBUST.relative_to(V2_ROOT)),
        "source_scenario": selected,
        "source_candidate_kind": selected_kind,
        "manual_step08_candidate_override_applied": manual_candidate_override_applied,
        "manual_guard_reserve_override_applied": manual_guard_reserve_override_applied,
        "fast_probe_risk_settings_mode": FAST_PROBE_RISK_SETTINGS_SOURCE,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "selected_step08_account_contract": {
            **step08_manifest_contract,
            "account_rule_profile": selected.get("account_rule_profile"),
            "account_stage": selected.get("account_stage"),
            "daily_symbol_risk_rule_enabled": selected.get("daily_symbol_risk_rule_enabled"),
            "daily_symbol_risk_enforcement_mode": selected.get("daily_symbol_risk_enforcement_mode"),
        },
        "selected_step08_risk_settings": {
            "base_portfolio_risk_budget_pct": float(selected["base_portfolio_risk_budget_pct"]),
            "max_single_trade_risk_pct": float(selected["max_single_trade_risk_pct"]),
            "max_min_lot_risk_inflation": optional_float(
                selected.get("max_min_lot_risk_inflation")
            ),
            "min_lot_risk_inflation_guard_enabled": bool(
                optional_float(selected.get("max_min_lot_risk_inflation")) is not None
            ),
            "overlap_mode": str(selected.get("overlap_mode", "")),
            "overlap_limits": OVERLAP_MODE_LIBRARY.get(str(selected.get("overlap_mode", "")), {}),
        },
        "active_variant_spec": ACTIVE_VARIANT_SPEC,
        "manual_variants_generated": variants,
        "stage2_variants_tested": [selected_label],
        "selected_variant_live_config": live_config,
        "stage_settings": {
            "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
            "current_balance": CURRENT_BALANCE,
            "account_start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
            "account_start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
            "run_mode": ACTIVE_RUN_MODE,
            "run_stage1": False,
            "stage2_start_every_n_active_days": STAGE2_START_EVERY_N_ACTIVE_DAYS,
            "stage2_max_rolling_starts": STAGE2_MAX_ROLLING_STARTS,
            "stage2_cost_stress_r_per_trade": STAGE2_COST_STRESS_R_PER_TRADE,
            "downstream_replay_compatibility_costs": DOWNSTREAM_REPLAY_COMPATIBILITY_COSTS,
            "selection_costs_for_hard_fail_gate": SELECTION_COSTS_FOR_HARD_FAIL_GATE,
            "fast_probe_use_selected_step08_risk_settings": FAST_PROBE_USE_SELECTED_STEP08_RISK_SETTINGS,
            "fast_probe_risk_settings_source": FAST_PROBE_RISK_SETTINGS_SOURCE,
            "throttle_basis": THROTTLE_BASIS,
            "throttle_configuration_basis": THROTTLE_CONFIGURATION_BASIS,
            "broker_volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
            "reduce_trade_to_fit_effective_risk_cap": REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP,
            "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
            "skip_trade_if_no_executable_lot_fits": SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS,
            "selected_step08_max_min_lot_risk_inflation": optional_float(
                selected.get("max_min_lot_risk_inflation")
            ),
            "selected_step08_min_lot_risk_inflation_guard_enabled": bool(
                optional_float(selected.get("max_min_lot_risk_inflation")) is not None
            ),
            "account_rule_profile": ACCOUNT_RULE_PROFILE,
            "account_stage": ACCOUNT_STAGE,
            "daily_reset_timezone": DAILY_RESET_TIMEZONE,
            "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
            "daily_symbol_risk_rule_enabled": bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"]),
            "daily_symbol_risk_enforcement_mode": str(ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"]),
        },
        "counts": {
            "ledger_rows_loaded": int(len(ledger)),
            "scenario_trades_available": int(arr["trade_count_available"]),
            "hrp_representatives": int(arr["n_representatives"]),
            "hrp_edges": int(arr["n_edges"]),
            "manual_variants_generated": 1,
            "stage2_variants_tested": 1,
            "stage1_starts": 0,
            "stage2_starts": int(len(stage2_starts)),
            "stage1_run_rows": 0,
            "stage2_run_rows": int(len(stage2_run_compat)),
            "all_cost_run_rows": int(len(all_cost_runs)),
            "stage2_aggregate_rows": int(len(stage2_agg)),
            "ranked_variants": int(len(ranked)),
        },
        "outputs": {
            "stage2_run_results": rel_path(OUTPUT_DIR / "stage2_run_results.csv"),
            "single_variant_all_cost_run_results": rel_path(OUTPUT_DIR / "single_variant_all_cost_run_results.csv"),
            "stage2_aggregate_by_variant_cost": rel_path(OUTPUT_DIR / "stage2_aggregate_by_variant_cost.csv"),
            "manual_variants_ranked": rel_path(OUTPUT_DIR / "manual_variants_ranked.csv"),
            "best_manual_variant": rel_path(OUTPUT_DIR / "best_manual_variant.csv"),
            "selected_manual_variant_live_config": rel_path(OUTPUT_DIR / "selected_manual_variant_live_config.json"),
            "manifest": rel_path(OUTPUT_DIR / "fast_manual_variant_lab_manifest.json"),
            "trial_history": rel_path(TRIAL_HISTORY_CSV),
        },
        "notes": [
            "Exactly one manually edited variant is tested per run.",
            "Quick-probe mode writes to quick_probe_latest and never overwrites Script 10 canonical inputs.",
            "Full-single mode writes every canonical Script 09 output required by Script 10.",
            "Approval requires zero hard failures and a non-negative trailing buffer at 0.00R, 0.02R and 0.05R.",
            "stage2_run_results.csv deliberately retains the established 0.00R/0.02R replay contract for the current Script 10; single_variant_all_cost_run_results.csv contains all three costs.",
            "Step 08 budget, single-trade cap, overlap mode, HRP weights and AFML sizing remain fixed.",
            "Throttle thresholds and fixed reserves are configured as fractions of the initial overall drawdown buffer.",
            "Broker-rounded executable risk drives PnL and all risk caps.",
            "Daily-symbol-risk behavior comes only from active_account_rules.json.",
        ],
    }

    if write:
        stage2_run_compat.to_csv(OUTPUT_DIR / "stage2_run_results.csv", index=False)
        all_cost_runs.to_csv(OUTPUT_DIR / "single_variant_all_cost_run_results.csv", index=False)
        stage2_agg.to_csv(OUTPUT_DIR / "stage2_aggregate_by_variant_cost.csv", index=False)
        ranked.to_csv(OUTPUT_DIR / "manual_variants_ranked.csv", index=False)
        best.to_csv(OUTPUT_DIR / "best_manual_variant.csv", index=False)
        best_starts.to_csv(OUTPUT_DIR / "best_start_dates.csv", index=False)
        worst_starts.to_csv(OUTPUT_DIR / "worst_start_dates.csv", index=False)
        boot.to_csv(OUTPUT_DIR / "bootstrap_confidence_intervals.csv", index=False)
        write_json(OUTPUT_DIR / "selected_manual_variant_live_config.json", live_config)
        write_json(OUTPUT_DIR / "fast_manual_variant_lab_manifest.json", manifest)
        append_trial_history(
            build_trial_history_row(
                trial_id, ACTIVE_RUN_MODE, selected_variant, ranked, gate, stage2_starts, all_cost_runs
            )
        )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Run a tiny no-write smoke test.")
    parser.add_argument(
        "--mode",
        choices=["quick_probe", "full_single"],
        default=None,
        help="Override MANUAL_RUN_MODE for this invocation.",
    )
    parser.add_argument("--quick-probe", action="store_true", help="Shortcut for --mode quick_probe.")
    parser.add_argument("--full-single", action="store_true", help="Shortcut for --mode full_single.")
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1),
        help="ProcessPoolExecutor workers for the rolling-start simulation grid (default: cpu_count-1). Pass 1 to force the original serial path.",
    )
    parser.add_argument(
        "--accept-moderate-hard-fail", action="store_true",
        help=f"Explicit human override: approve for Script 10 even if hard-fail rate is nonzero, as long as it stays <= {MODERATE_HARD_FAIL_TOLERANCE:.0%} at every tested cost stress with zero daily-drawdown failures and no reserve violations. Logged clearly in the manifest as a manual override, not a literal zero-hard-fail pass.",
    )
    parser.add_argument(
        "--manual-step08-candidate", type=str, default=None,
        help="Explicit human override of the AUTOMATED Step08 candidate selection: send a DIFFERENT foundation_candidate_name (any candidate Step 08 tested, from stage2_aggregate.csv -- not just its own best_robust/fast_safe/fast_frontier picks or 08c's joint-search winner) through Step 09/10/11 instead.",
    )
    parser.add_argument(
        "--manual-guard-profile", type=str, default=None,
        help="Explicit human override of the AUTOMATED guard/reserve selection. Requires also specifying the reserve settings below (or omit all four --manual-reserve-* args to use guard_profile='off' with no reserve).",
    )
    parser.add_argument(
        "--manual-reserve-mode", type=str, default=None, choices=["off", "static", "dynamic_projected_open_risk"],
        help="Paired with --manual-guard-profile: the trailing-buffer admission reserve mode to use instead of the automated pick.",
    )
    parser.add_argument(
        "--manual-reserve-pct", type=float, default=0.0,
        help="Paired with --manual-guard-profile: trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer.",
    )
    parser.add_argument(
        "--manual-reserve-cost-allowance", type=float, default=0.0,
        help="Paired with --manual-guard-profile: trailing_buffer_cost_allowance_r.",
    )
    args = parser.parse_args()
    if args.quick_probe and args.full_single:
        parser.error("Choose only one of --quick-probe or --full-single.")
    requested_mode = args.mode or (
        "quick_probe" if args.quick_probe else "full_single" if args.full_single else MANUAL_RUN_MODE
    )
    configure_run_mode(requested_mode)
    global DRY_RUN
    DRY_RUN = bool(args.dry_run)
    manual_variant_spec = None
    if args.manual_guard_profile:
        reserve_mode = args.manual_reserve_mode or "off"
        manual_variant_spec = {
            "name": f"manual_override_{args.manual_guard_profile}_{reserve_mode}",
            "guard_profile": args.manual_guard_profile,
            "trailing_buffer_admission_enabled": reserve_mode != "off",
            "trailing_buffer_reserve_mode": reserve_mode,
            "trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer": float(args.manual_reserve_pct),
            "trailing_buffer_cost_allowance_r": float(args.manual_reserve_cost_allowance),
        }
    manifest = build(
        write=not args.dry_run,
        n_workers=int(args.workers),
        accept_moderate_hard_fail=bool(args.accept_moderate_hard_fail),
        manual_step08_candidate=args.manual_step08_candidate,
        manual_variant_spec=manual_variant_spec,
    )
    counts = manifest["counts"]
    summary = manifest["selected_variant_live_config"]["summary"]
    spec = (
        manifest.get("selected_variant_live_config", {}).get("selected_variant", {})
        or manifest.get("active_variant_spec", {})
    )

    print("V2 Phase B2.09 single manual variant tester")
    print(f"Run mode: {manifest['run_mode']}")
    print(f"Status: {manifest['manual_variant_lab_status']}")
    print(
        "Account start: "
        f"balance=${CURRENT_BALANCE:,.2f}, "
        f"high-water=${ACCOUNT_START_TRAILING_HIGH_WATERMARK:,.2f}, "
        f"trailing floor=${ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR:,.2f}, "
        f"buffer=${ACCOUNT_START_REMAINING_BALANCE_BUFFER:,.2f}, "
        f"fresh-day floor=${ACCOUNT_START_DAILY_LOSS_FLOOR:,.2f}"
    )
    print(f"Selection status: {manifest['selection_status']}")
    print(f"Approved for Script 10: {manifest['approved_for_script10']}")
    print(f"Trial ID: {manifest['trial_id']}")
    print(f"Source Step08 scenario: {manifest['source_scenario'].get('scenario_id')}")
    print(f"Source candidate kind: {manifest['source_candidate_kind']}")
    print(
        "Active manual setting: "
        f"guard={spec.get('guard_profile')}, reserve={spec.get('trailing_buffer_reserve_mode')}, "
        f"fixed={float(spec.get('trailing_buffer_fixed_reserve_pct_of_initial_dd_buffer', 0.0)):.4%} "
        f"of initial-DD buffer (${float(spec.get('trailing_buffer_fixed_reserve_amount', 0.0)):,.2f}), "
        f"allowance={float(spec.get('trailing_buffer_cost_allowance_r', 0.0)):.3f}R"
    )
    observed_costs = manifest.get("stage_settings", {}).get(
        "stage2_cost_stress_r_per_trade", []
    )
    print(
        "Test costs: "
        + ", ".join(f"{float(x):.2f}R" for x in observed_costs)
    )
    print(f"Rolling starts: {counts['stage2_starts']}")
    print(f"All-cost simulation rows: {counts['all_cost_run_rows']}")
    print(f"Script 10 compatibility rows: {counts['stage2_run_rows']}")
    print(f"Manual variants generated/tested/ranked: 1 / 1 / {counts['ranked_variants']}")
    if summary:
        for tag, label in [("base", "Cost00"), ("cost0p02", "Cost02"), ("cost0p05", "Cost05")]:
            print(
                f"{label} pass/hard/end/median/buffer: "
                f"{float(summary.get(f'{tag}_pass_rate', float('nan'))):.2%} / "
                f"{float(summary.get(f'{tag}_hard_fail_rate', float('nan'))):.2%} / "
                f"{float(summary.get(f'{tag}_end_of_history_fail_rate', float('nan'))):.2%} / "
                f"{summary.get(f'{tag}_median_trading_days_to_target')} / "
                f"${float(summary.get(f'{tag}_worst_buffer_above_floor', float('nan'))):,.2f}"
            )
        print(f"Selection tier: {summary.get('selection_tier')}")
    gate = manifest.get("approval_gate", {})
    if gate.get("reasons"):
        print("Gate failures:")
        for reason in gate["reasons"]:
            print(f"  - {reason}")
    print(f"Output dir: {rel_path(OUTPUT_DIR)}")
    print(f"Trial history: {rel_path(TRIAL_HISTORY_CSV)}")


if __name__ == "__main__":
    main()
