from __future__ import annotations

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
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
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

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
    target_satisfied,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.validation.v3_semantic_contract import (  # noqa: E402
    validate_representative_payoff_context_handoff,
)

SCHEMA_VERSION = "v2.3"
SCRIPT_VERSION = "2026-07-20_generic_active_account_rules_targeted_budget_probe"
PIPELINE_MODE = "v2_generic_active_account_foundation_probe_targeted_budget_overlap_minlot_diagnostics"
INPUT_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
INPUT_HRP_WEIGHTS = V2_ROOT / "portfolio_allocator" / "outputs" / "hrp_linkage_robustness_outputs" / "hrp_linkage_weights_step08_primary.csv"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "prop_challenge_rolling_optimizer_outputs"

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
INITIAL_TRAILING_DRAWDOWN_FLOOR = account_overall_drawdown_floor(
    ACCOUNT_RULES, PROP_START_BALANCE
)
TRAILING_DRAWDOWN_STOP_FLOOR = PROP_START_BALANCE
ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR = account_overall_drawdown_floor(
    ACCOUNT_RULES, ACCOUNT_START_TRAILING_HIGH_WATERMARK
)
ACCOUNT_START_REMAINING_BALANCE_BUFFER = (
    CURRENT_BALANCE - ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR
)
ACCOUNT_START_PROFIT_TO_TARGET = (
    TARGET_BALANCE - CURRENT_BALANCE
    if np.isfinite(TARGET_BALANCE)
    else float("inf")
)
ACCOUNT_START_OPEN_RISK_AMOUNT = 0.0
ASSUME_FRESH_DAILY_RESET_AT_SIMULATION_START = bool(
    ACCOUNT_RULES["assume_fresh_daily_reset_at_simulation_start"]
)
DAILY_LOSS_PCT = float(ACCOUNT_RULES["daily_drawdown"].get("pct") or 0.0)
DAILY_LOSS_AMOUNT = account_daily_drawdown_amount(
    ACCOUNT_RULES,
    day_start_balance=CURRENT_BALANCE,
    day_start_equity=CURRENT_BALANCE,
)
ACCOUNT_START_DAILY_LOSS_FLOOR = account_daily_drawdown_floor(
    ACCOUNT_RULES,
    day_start_balance=CURRENT_BALANCE,
    day_start_equity=CURRENT_BALANCE,
)
DAILY_RESET_TIMEZONE = str(ACCOUNT_RULES["daily_drawdown"]["reset_timezone"])
DAILY_RESET_OFFSET_HOURS = float(ACCOUNT_RULES["daily_drawdown"]["bucket_offset_hours"])
DAILY_RESET_OFFSET_NS = int(round(DAILY_RESET_OFFSET_HOURS * 60 * 60 * 1_000_000_000))
TRAILING_DRAWDOWN_MODE = str(ACCOUNT_RULES["overall_drawdown"]["type"])
FLOATING_LOSS_PROXY_MODE = "open_stop_risk"
NO_DEADLINE_EVALUATION = True
MIN_REMAINING_OBSERVATION_DAYS_AFTER_START = 0
STOP_IMMEDIATELY_ON_TARGET = bool(
    ACCOUNT_RULES["profit_target"].get("stop_immediately_when_eligible", True)
)
STOP_ON_RULE_BREACH = True
MINIMUM_TRADING_DAYS_ENABLED = bool(ACCOUNT_RULES["minimum_trading_days"]["enabled"])
MINIMUM_TRADING_DAYS_REQUIRED = int(ACCOUNT_RULES["minimum_trading_days"]["required_days"])
MINIMUM_TRADING_DAY_MODE = str(ACCOUNT_RULES["minimum_trading_days"]["day_count_mode"])
MINIMUM_TRADING_DAY_PROFIT_PCT = float(
    ACCOUNT_RULES["minimum_trading_days"]["minimum_day_profit_pct"]
)
MINIMUM_TRADING_DAY_PROFIT_AMOUNT = float(
    ACCOUNT_RULES["minimum_trading_days"]["minimum_day_profit_amount"]
)
PROP_MAX_SINGLE_TRADE_RISK_PCT = (
    float(ACCOUNT_RULES["maximum_risk_per_trade"]["pct"])
    if ACCOUNT_RULES["maximum_risk_per_trade"]["enabled"]
    else None
)
FLOATING_LOSS_GUARD_ENABLED = bool(ACCOUNT_RULES["floating_loss_guard"]["enabled"])
FLOATING_LOSS_GUARD_AMOUNT = ACCOUNT_RULES["floating_loss_guard"].get(
    "internal_operating_amount"
)
FLOATING_LOSS_GUARD_AMOUNT = (
    float(FLOATING_LOSS_GUARD_AMOUNT)
    if FLOATING_LOSS_GUARD_AMOUNT is not None
    else None
)

ACCOUNT_RULE_PROFILES = {
    ACCOUNT_RULE_PROFILE: {
        "account_stage": ACCOUNT_STAGE,
        "description": str(ACCOUNT_RULES.get("description") or ""),
        "daily_drawdown_configs": [daily_drawdown_config(ACCOUNT_RULES)],
    }
}
ACTIVE_ACCOUNT_RULE_PROFILES = [ACCOUNT_RULE_PROFILE]
DAILY_DRAWDOWN_CONFIGS = [daily_drawdown_config(ACCOUNT_RULES)]

# Broker executable risk policy:
# target risk = balance * min(base_budget * HRP weight * AFML, max_single_trade_risk_pct)
# actual risk = broker-rounded lot * trade-level risk_per_1_lot
# If ceil rounding exceeds an effective cap, volume backs off to the largest safe broker step; the trade is skipped only when even the minimum lot cannot fit.
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

RISK_SIZING_MODE = "direct_weight_calibrated_afml"

# Focused foundational investigation.
#
# The direct-weight calibrated-AFML sizing formula remains fixed intentionally:
# Step 08 is comparing risk parameters inside one approved sizing method, not
# mixing different sizing methodologies in the same ranking.
#
# This deliberately avoids a large Cartesian grid. It keeps the existing
# purpose-built candidates and adds only two one-variable budget probes:
# - preserve the selected 1.50% / 0.25% / budget-0.80 scenario as the control;
# - compare that exact control at budget 0.90 and 1.00, changing nothing else;
# - relax only position-count caps while preserving all monetary caps;
# - probe total open risk between 1.50% and 1.80%;
# - tighten asset/family/edge concentration as total capacity rises;
# - probe 0.15%, 0.20%, and 0.25% single-trade caps;
# - test a moderate 3x maximum minimum-lot risk-inflation guard.
#
# Candidate count rises only from 12 to 14, so the full Stage-2 run is about
# 16.7% larger rather than multiplying every overlap/trade-cap combination.
FOUNDATION_INVESTIGATION_MODE = True
FOUNDATION_RUN_ALL_CANDIDATES_IN_STAGE2 = True
MIN_LOT_RISK_INFLATION_GUARD_APPLIES_ONLY_WHEN_MIN_LOT_BINDS = True

SIMULATE_RETURN_MODES = ["raw_r"]
SIMULATE_UNIVERSES = ["core_plus_watchlist"]
SIMULATE_PROFILE_ROLES = ["compound_velocity"]
SIMULATE_LINKAGE_METHODS = ["single"]

OVERLAP_MODE_LIBRARY = {
    # Exact control from the latest 46-day selected scenario.
    "cap1p50": {
        "name": "cap1p50",
        "max_total_open_risk_pct": 0.0150,
        "max_asset_open_risk_pct": 0.0120,
        "max_family_open_risk_pct": 0.0100,
        "max_edge_open_risk_pct": 0.0100,
        "max_open_trades_per_edge": 3,
        "max_open_trades_per_asset": 10,
    },
    # Same monetary caps as the control; only trade-count packing is relaxed.
    "cap1p50_relaxed_counts": {
        "name": "cap1p50_relaxed_counts",
        "max_total_open_risk_pct": 0.0150,
        "max_asset_open_risk_pct": 0.0120,
        "max_family_open_risk_pct": 0.0100,
        "max_edge_open_risk_pct": 0.0100,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    # Intermediate total capacity with materially tighter concentration.
    "cap1p60_diversified": {
        "name": "cap1p60_diversified",
        "max_total_open_risk_pct": 0.0160,
        "max_asset_open_risk_pct": 0.0090,
        "max_family_open_risk_pct": 0.0075,
        "max_edge_open_risk_pct": 0.0035,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p70_diversified": {
        "name": "cap1p70_diversified",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p80_diversified": {
        "name": "cap1p80_diversified",
        "max_total_open_risk_pct": 0.0180,
        "max_asset_open_risk_pct": 0.0100,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    # Lower-total-open-risk probes: stage2_aggregate diagnostics on the
    # original 15-candidate grid showed asset/family/edge concentration caps
    # NEVER bind (median_binding_capacity_asset/family/edge_count == 0 across
    # every candidate) -- the only caps that actually bind are the single-
    # trade cap and the total-open-risk cap, and max_drawdown_fail_rate rises
    # almost monotonically with total open risk (25.8% at 1.50% total up to
    # 35.8% at 1.70-1.80%). These two modes push total capacity below the
    # previously-tested floor of 1.50% to test whether hard-fail can be
    # pushed toward the 84% fast-safe pass-rate bar.
    "cap1p00_diversified": {
        "name": "cap1p00_diversified",
        "max_total_open_risk_pct": 0.0100,
        "max_asset_open_risk_pct": 0.0080,
        "max_family_open_risk_pct": 0.0065,
        "max_edge_open_risk_pct": 0.0025,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    "cap1p20_diversified": {
        "name": "cap1p20_diversified",
        "max_total_open_risk_pct": 0.0120,
        "max_asset_open_risk_pct": 0.0090,
        "max_family_open_risk_pct": 0.0070,
        "max_edge_open_risk_pct": 0.0030,
        "max_open_trades_per_edge": 5,
        "max_open_trades_per_asset": 15,
    },
    # 2026-09-25, 12-edge universe: max_open_trades_per_edge has never been
    # independently swept -- every mode above fixes it at 3 or 5 alongside
    # other overlap-mode changes. Live diagnostics on the current universe
    # (single_variant_all_cost_run_results.csv) show skip_max_open_trades_
    # per_edge fires in 98.75% of simulated runs, rejecting ~5.7% of all
    # attempted trades -- a real, unexplored throughput bottleneck, distinct
    # from the asset/family/edge risk-pct caps (which still never bind).
    # These six modes hold cap1p70_diversified's monetary caps fixed and
    # vary ONLY max_open_trades_per_edge, keeping max_open_trades_per_asset
    # generously high (20) so it cannot itself become the binding constraint
    # and confound the read on the per-edge cap in isolation.
    "cap1p70_edge1": {
        "name": "cap1p70_edge1",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 1,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge2": {
        "name": "cap1p70_edge2",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 2,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge4": {
        "name": "cap1p70_edge4",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 4,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge6": {
        "name": "cap1p70_edge6",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 6,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge8": {
        "name": "cap1p70_edge8",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 8,
        "max_open_trades_per_asset": 20,
    },
    "cap1p70_edge10": {
        "name": "cap1p70_edge10",
        "max_total_open_risk_pct": 0.0170,
        "max_asset_open_risk_pct": 0.0095,
        "max_family_open_risk_pct": 0.0080,
        "max_edge_open_risk_pct": 0.0040,
        "max_open_trades_per_edge": 10,
        "max_open_trades_per_asset": 20,
    },
    # 2026-10-17 AFML Ch.10.2/10.4 concurrency-aware sizing (afml_concurrency_sizing.py). Each signal gets
    # Q_edge * m_i / c_q(edge), so stacked same-edge signals are sized DOWN instead of dropped. Position-count caps are
    # set above the observed maximum same-edge concurrency (34 on the 2026-10-17 universe) so they never pre-empt the
    # AFML budgeting; the monetary caps remain the safety limits.
    "afmlconc_cap1p50": {
        "name": "afmlconc_cap1p50",
        "max_total_open_risk_pct": 0.0150,
        "max_asset_open_risk_pct": 0.0120,
        "max_family_open_risk_pct": 0.0100,
        "max_edge_open_risk_pct": 0.0100,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap2p00": {
        "name": "afmlconc_cap2p00",
        "max_total_open_risk_pct": 0.0200,
        "max_asset_open_risk_pct": 0.0160,
        "max_family_open_risk_pct": 0.0130,
        "max_edge_open_risk_pct": 0.0130,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap2p50": {
        "name": "afmlconc_cap2p50",
        "max_total_open_risk_pct": 0.0250,
        "max_asset_open_risk_pct": 0.0200,
        "max_family_open_risk_pct": 0.0160,
        "max_edge_open_risk_pct": 0.0160,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    # Faster AFML Ch.10.2/10.4 region (2026-10-17 #2): the first AFML run topped out at ~60 median days (budget 0.30);
    # these extend the AFML frontier toward the 30-45 day range.
    "afmlconc_cap3p00": {
        "name": "afmlconc_cap3p00",
        "max_total_open_risk_pct": 0.0300,
        "max_asset_open_risk_pct": 0.0240,
        "max_family_open_risk_pct": 0.0190,
        "max_edge_open_risk_pct": 0.0190,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap3p50": {
        "name": "afmlconc_cap3p50",
        "max_total_open_risk_pct": 0.0350,
        "max_asset_open_risk_pct": 0.0280,
        "max_family_open_risk_pct": 0.0220,
        "max_edge_open_risk_pct": 0.0220,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
    "afmlconc_cap4p00": {
        "name": "afmlconc_cap4p00",
        "max_total_open_risk_pct": 0.0400,
        "max_asset_open_risk_pct": 0.0320,
        "max_family_open_risk_pct": 0.0250,
        "max_edge_open_risk_pct": 0.0250,
        "max_open_trades_per_edge": 40,
        "max_open_trades_per_asset": 80,
    },
}


FOUNDATION_SCENARIO_SPECS = [
    {
        "name": "pairwise_cap1p50_budget90_trade20",
        "base_portfolio_risk_budget_pct": 0.9000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p50",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    {
        "name": "pairwise_cap1p50_budget100_trade25",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0025,
        "overlap_mode": "cap1p50",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    # Higher-single-trade-risk probes, added after both discovered winners
    # (the drawn-down-account manual winner at 0.20% and the fresh-account
    # automated winner at 0.25%, its own max tested value) turned out to sit
    # at or near the previous ceiling -- worth testing whether a fresh (or
    # otherwise low-drawdown-consumed) account benefits from an even higher
    # per-trade cap, given more of its buffer is intact to absorb it. 0.10%
    # and 0.15% were removed in the same pass: neither is used by any winner
    # found in either account state, and they sit far from where the
    # frontier has actually been found to live.
    {
        "name": "pairwise_cap1p50_budget100_trade30",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0030,
        "overlap_mode": "cap1p50",
        "max_min_lot_risk_inflation": None,
        "research_role": "higher_single_trade_risk_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade35",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0035,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "higher_single_trade_risk_probe",
    },

    {
        "name": "pairwise_cap1p50relaxed_budget90_trade20",
        "base_portfolio_risk_budget_pct": 0.9000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p50_relaxed_counts",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    {
        "name": "pairwise_cap1p50relaxed_budget100_trade25",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0025,
        "overlap_mode": "cap1p50_relaxed_counts",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },

    {
        "name": "pairwise_cap1p60_budget80_trade20",
        "base_portfolio_risk_budget_pct": 0.8000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p60_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    {
        "name": "pairwise_cap1p60_budget90_trade25",
        "base_portfolio_risk_budget_pct": 0.9000,
        "max_single_trade_risk_pct": 0.0025,
        "overlap_mode": "cap1p60_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },

    {
        "name": "pairwise_cap1p70_budget80_trade25",
        "base_portfolio_risk_budget_pct": 0.8000,
        "max_single_trade_risk_pct": 0.0025,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    # Same risk settings as pairwise_cap1p70_budget100_trade20 (the 08c joint-
    # search winner) but with the broker minimum-lot risk-inflation guard
    # turned on. Diagnostics on the ungated version showed typical min-lot-
    # forced trades inflate risk mildly (median 1.09x, p90 1.71x target) but
    # the worst case per run reached up to 62,871x -- happening specifically
    # when the drawdown-buffer throttle had cut the target size to near zero
    # and the broker's minimum lot forced a full-size trade anyway, defeating
    # the throttle's protection exactly when it matters most. 6.0x is chosen
    # to sit comfortably above the legitimate p90 (1.71x) while cutting off
    # that pathological tail.
    {
        "name": "pairwise_cap1p70_budget100_trade20_minlotcap6x",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": 6.0,
        "research_role": "min_lot_inflation_guard_probe",
    },

    {
        "name": "pairwise_cap1p80_budget80_trade25",
        "base_portfolio_risk_budget_pct": 0.8000,
        "max_single_trade_risk_pct": 0.0025,
        "overlap_mode": "cap1p80_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },
    {
        "name": "pairwise_cap1p80_budget100_trade20",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p80_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "pairwise_covering_design",
    },

    # Lower-total-open-risk probes (see cap1p00/cap1p20_diversified above):
    # single-trade caps scaled down proportionally with total capacity,
    # since the single-trade cap is the OTHER binding constraint found in
    # the original grid's diagnostics.
    {
        "name": "pairwise_cap1p00_budget80_trade12",
        "base_portfolio_risk_budget_pct": 0.8000,
        "max_single_trade_risk_pct": 0.0012,
        "overlap_mode": "cap1p00_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "lower_total_risk_probe",
    },
    {
        "name": "pairwise_cap1p20_budget70_trade12",
        "base_portfolio_risk_budget_pct": 0.7000,
        "max_single_trade_risk_pct": 0.0012,
        "overlap_mode": "cap1p20_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "lower_total_risk_probe",
    },
    {
        "name": "pairwise_cap1p20_budget90_trade18",
        "base_portfolio_risk_budget_pct": 0.9000,
        "max_single_trade_risk_pct": 0.0018,
        "overlap_mode": "cap1p20_diversified",
        "max_min_lot_risk_inflation": None,
        "research_role": "lower_total_risk_probe",
    },

    # 2026-09-25: max_open_trades_per_edge sweep, one variable at a time
    # against the same risk-budget/single-trade-risk config as the 08c
    # joint-search winner (pairwise_cap1p70_budget100_trade20) -- see the
    # cap1p70_edge{1,2,4,6,8,10} overlap modes above. 5 is already covered
    # by pairwise_cap1p70_budget100_trade20 itself; 3 by the cap1p50 family.
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge1",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge1",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge2",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge2",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge4",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge4",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge6",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge6",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge8",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge8",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_edge10",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_edge10",
        "max_min_lot_risk_inflation": None,
        "research_role": "open_trades_per_edge_probe",
    },

    # 2026-09-25: max_min_lot_risk_inflation guard sweep at 2x/3x/4x,
    # bracketing the "moderate 3x" the cap1p70_budget100_trade20_minlotcap6x
    # comment originally called for testing but never added (only 6.0x
    # exists). Live diagnostics show min_lot_risk_cap skips 28.1% of all
    # attempted trades and accepted-trade risk inflation reaches up to 85x
    # in the worst observed run with the guard off -- this directly targets
    # that tail-risk amplifier, distinct from the pure speed probe above.
    {
        "name": "pairwise_cap1p70_budget100_trade20_minlotcap2x",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": 2.0,
        "research_role": "min_lot_inflation_guard_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_minlotcap3x",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": 3.0,
        "research_role": "min_lot_inflation_guard_probe",
    },
    {
        "name": "pairwise_cap1p70_budget100_trade20_minlotcap4x",
        "base_portfolio_risk_budget_pct": 1.0000,
        "max_single_trade_risk_pct": 0.0020,
        "overlap_mode": "cap1p70_diversified",
        "max_min_lot_risk_inflation": 4.0,
        "research_role": "min_lot_inflation_guard_probe",
    },
    # 2026-10-17: AFML Ch.10.2/10.4 concurrency-aware sizing candidates (see afml_concurrency_sizing.py). The base budget
    # is AFML's maximum position scale Q (Q_edge = base * hrp_weight); per-signal risk = Q_edge * afml_bet_size / c_q.
    # On the 2026-10-17 universe the full-concurrency portfolio exposure is ~0.10 x base, so bases 0.10-0.30 span ~1-3%.
    {
        "name": "afmlconc_q95_cap1p50_budget10_trade50",
        "base_portfolio_risk_budget_pct": 0.1000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap1p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q95_cap1p50_budget15_trade50",
        "base_portfolio_risk_budget_pct": 0.1500,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap1p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q95_cap2p00_budget15_trade50",
        "base_portfolio_risk_budget_pct": 0.1500,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q95_cap2p00_budget20_trade50",
        "base_portfolio_risk_budget_pct": 0.2000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q95_cap2p50_budget30_trade50",
        "base_portfolio_risk_budget_pct": 0.3000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q90_cap1p50_budget10_trade50",
        "base_portfolio_risk_budget_pct": 0.1000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap1p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q90_cap1p50_budget15_trade50",
        "base_portfolio_risk_budget_pct": 0.1500,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap1p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q90_cap2p00_budget15_trade50",
        "base_portfolio_risk_budget_pct": 0.1500,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q90_cap2p00_budget20_trade50",
        "base_portfolio_risk_budget_pct": 0.2000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q90_cap2p50_budget30_trade50",
        "base_portfolio_risk_budget_pct": 0.3000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_qmax_cap2p00_budget20_trade50",
        "base_portfolio_risk_budget_pct": 0.2000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 1.0,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_qmax_cap2p50_budget30_trade50",
        "base_portfolio_risk_budget_pct": 0.3000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap2p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 1.0,
        "research_role": "afml_ch10_concurrency_budget",
    },
    {
        "name": "afmlconc_q95_cap3p00_budget40_trade50",
        "base_portfolio_risk_budget_pct": 0.4000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap3p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
    {
        "name": "afmlconc_q95_cap3p50_budget50_trade50",
        "base_portfolio_risk_budget_pct": 0.5000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap3p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
    {
        "name": "afmlconc_q95_cap4p00_budget60_trade50",
        "base_portfolio_risk_budget_pct": 0.6000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap4p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.95,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
    {
        "name": "afmlconc_q90_cap3p00_budget40_trade50",
        "base_portfolio_risk_budget_pct": 0.4000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap3p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
    {
        "name": "afmlconc_q90_cap3p50_budget50_trade50",
        "base_portfolio_risk_budget_pct": 0.5000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap3p50",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
    {
        "name": "afmlconc_q90_cap4p00_budget60_trade50",
        "base_portfolio_risk_budget_pct": 0.6000,
        "max_single_trade_risk_pct": 0.0050,
        "overlap_mode": "afmlconc_cap4p00",
        "max_min_lot_risk_inflation": None,
        "afml_concurrency_quantile": 0.9,
        "research_role": "afml_ch10_concurrency_budget_fast",
    },
]


BASE_PORTFOLIO_RISK_BUDGET_PCTS = sorted(
    {float(x["base_portfolio_risk_budget_pct"]) for x in FOUNDATION_SCENARIO_SPECS}
)
MAX_SINGLE_TRADE_RISK_PCTS = sorted(
    {float(x["max_single_trade_risk_pct"]) for x in FOUNDATION_SCENARIO_SPECS}
)
OVERLAP_MODES = [OVERLAP_MODE_LIBRARY[name] for name in OVERLAP_MODE_LIBRARY]
MAX_MIN_LOT_RISK_INFLATION_VALUES = sorted(
    {
        float(x["max_min_lot_risk_inflation"])
        for x in FOUNDATION_SCENARIO_SPECS
        if x.get("max_min_lot_risk_inflation") is not None
    }
)

AFML_BET_SIZE_COLUMN = "afml_bet_size"
AFML_SIZE_FLOOR = 0.0
AFML_SIZE_CAP = 1.0
STAGE1_START_EVERY_N_ACTIVE_DAYS = 40
STAGE1_MAX_ROLLING_STARTS = 12
STAGE1_COST_STRESS_R_PER_TRADE = [0.00]
STAGE2_TOP_N_CANDIDATES = len(FOUNDATION_SCENARIO_SPECS)
# Widened per explicit VPS-compute-budget request: every active day is now a
# rolling-start candidate (previously every 5th), subsampled evenly down to
# 400 starts if more are available (previously 120) -- roughly 3-4x denser
# historical coverage of the ~1,217 unique active days in the ledger.
STAGE2_START_EVERY_N_ACTIVE_DAYS = 1
STAGE2_MAX_ROLLING_STARTS = 400
STAGE2_COST_STRESS_R_PER_TRADE = [0.00, 0.02]

BEST_SCENARIO_SELECTION_MODE = "robust_pass_rate_first"
FAST_SAFE_SELECTION_MODE = "fastest_after_safety_gates"
FAST_SAFE_BASE_PASS_MIN = 0.84
FAST_SAFE_STRESS02_PASS_MIN = 0.75
FAST_SAFE_BASE_HARD_FAIL_MAX = 0.10
FAST_SAFE_STRESS02_HARD_FAIL_MAX = 0.16

FAST_FRONTIER_SELECTION_MODE = "fastest_frontier_for_step09"
FAST_FRONTIER_BASE_PASS_MIN = 0.80
FAST_FRONTIER_STRESS02_PASS_MIN = 0.70
FAST_FRONTIER_BASE_HARD_FAIL_MAX = 0.15
FAST_FRONTIER_STRESS02_HARD_FAIL_MAX = 0.27

FAST_SAFE_STAGE1_BASE_PASS_MIN = 0.80
FAST_SAFE_STAGE1_HARD_FAIL_MAX = 0.15
STAGE2_ROBUST_CANDIDATE_SHARE = len(FOUNDATION_SCENARIO_SPECS)
SPEED_SAFETY_FRONTIER_MAX_ROWS = len(FOUNDATION_SCENARIO_SPECS)

EPS = 1e-12
DAY_NS = 24 * 60 * 60 * 1_000_000_000


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        if abs(b) < EPS or not np.isfinite(b):
            return np.nan
        return float(a) / b
    except Exception:
        return np.nan


def optional_float(value: Any) -> float | None:
    """Return a finite float or None without converting a disabled rule to zero."""
    try:
        if value is None or pd.isna(value):
            return None
        out = float(value)
        return out if np.isfinite(out) else None
    except Exception:
        return None


def pct_str(x: float) -> str:
    return f"{x * 100:.4f}%"


def risk_label(x: float) -> str:
    return f"{x * 100:.4f}".replace(".", "p")


def inflation_guard_label(value: Any) -> str:
    guard = optional_float(value)
    return "off" if guard is None else f"{guard:.2f}x".replace(".", "p")


def scenario_token(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def validate_foundation_scenario_specs() -> None:
    if not FOUNDATION_INVESTIGATION_MODE:
        return
    if not FOUNDATION_SCENARIO_SPECS:
        raise ValueError("FOUNDATION_SCENARIO_SPECS cannot be empty.")
    names: set[str] = set()
    for spec in FOUNDATION_SCENARIO_SPECS:
        name = str(spec.get("name", "")).strip()
        if not name or name in names:
            raise ValueError(f"Foundation candidate names must be non-empty and unique: {name!r}")
        names.add(name)
        overlap_name = str(spec.get("overlap_mode", ""))
        if overlap_name not in OVERLAP_MODE_LIBRARY:
            raise ValueError(
                f"Foundation candidate {name!r} references unknown overlap mode "
                f"{overlap_name!r}. Available: {sorted(OVERLAP_MODE_LIBRARY)}"
            )
        base_budget = float(spec.get("base_portfolio_risk_budget_pct", np.nan))
        trade_cap = float(spec.get("max_single_trade_risk_pct", np.nan))
        if not np.isfinite(base_budget) or base_budget <= 0.0:
            raise ValueError(f"Foundation candidate {name!r} has invalid base budget {base_budget!r}")
        if not np.isfinite(trade_cap) or not (0.0 < trade_cap <= 1.0):
            raise ValueError(f"Foundation candidate {name!r} has invalid trade cap {trade_cap!r}")
        AFMLC.validate_quantile(spec.get("afml_concurrency_quantile", 0.0))
        guard = optional_float(spec.get("max_min_lot_risk_inflation"))
        if guard is not None and guard < 1.0:
            raise ValueError(
                f"Foundation candidate {name!r} max_min_lot_risk_inflation "
                f"must be >= 1.0 or None, got {guard}"
            )
        mode = OVERLAP_MODE_LIBRARY[overlap_name]
        for key in [
            "max_total_open_risk_pct",
            "max_asset_open_risk_pct",
            "max_family_open_risk_pct",
            "max_edge_open_risk_pct",
        ]:
            value = float(mode[key])
            if not np.isfinite(value) or not (0.0 < value <= 1.0):
                raise ValueError(f"Overlap mode {overlap_name!r} has invalid {key}={value!r}")
        if int(mode["max_open_trades_per_edge"]) <= 0 or int(mode["max_open_trades_per_asset"]) <= 0:
            raise ValueError(f"Overlap mode {overlap_name!r} must use positive trade-count caps.")


def ns_to_ts(ns: int | None) -> pd.Timestamp:
    return to_datetime_iso(int(ns), utc=True) if ns is not None and not pd.isna(ns) else pd.NaT


def day_from_ns(ns: int) -> int:
    """Return the configured broker/server trading day bucket."""
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
            daily_start_balance
            if daily_start_equity is None
            else daily_start_equity
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



def compute_direct_trade_risk_pct(hrp_weight: float, afml_bet_size: float, base_budget_pct: float, max_single_trade_risk_pct: float) -> float:
    """Requested/target risk pct before broker lot rounding."""
    uncapped = float(base_budget_pct) * float(hrp_weight) * float(afml_bet_size)
    return float(np.clip(uncapped, 0.0, float(max_single_trade_risk_pct)))


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

def select_unique_ids(*series_list: pd.Series, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for series in series_list:
        for value in series.astype(str).tolist():
            if value not in seen:
                seen.add(value)
                out.append(value)
            if len(out) >= int(limit):
                return out
    return out


def row_metrics(row: pd.Series | dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    get = row.get
    return {
        "scenario_id": get("scenario_id"),
        "foundation_candidate_name": get("foundation_candidate_name"),
        "foundation_research_role": get("foundation_research_role"),
        "overlap_mode": get("overlap_mode"),
        "max_total_open_risk_pct": get("max_total_open_risk_pct"),
        "max_asset_open_risk_pct": get("max_asset_open_risk_pct"),
        "max_family_open_risk_pct": get("max_family_open_risk_pct"),
        "max_edge_open_risk_pct": get("max_edge_open_risk_pct"),
        "max_open_trades_per_edge": get("max_open_trades_per_edge"),
        "max_open_trades_per_asset": get("max_open_trades_per_asset"),
        "max_min_lot_risk_inflation": get("max_min_lot_risk_inflation"),
        "base_pass_rate": get("base_pass_rate"),
        "base_fail_rate": get("base_fail_rate"),
        "base_hard_fail_rate": get("base_hard_fail_rate"),
        "base_max_drawdown_fail_rate": get("base_max_drawdown_fail_rate"),
        "base_daily_drawdown_fail_rate": get("base_daily_drawdown_fail_rate"),
        "base_daily_symbol_risk_fail_rate": get("base_daily_symbol_risk_fail_rate"),
        "base_end_of_history_fail_rate": get("base_end_of_history_fail_rate"),
        "base_median_days": get("base_median_days"),
        "base_p95_days": get("base_p95_days"),
        "base_p99_days": get("base_p99_days"),
        "base_worst_days": get("base_worst_days"),
        "base_p99_calendar_days": get("base_p99_calendar_days"),
        "base_prob_under_30": get("base_prob_under_30"),
        "base_max_single_trade_risk_pct": get("max_single_trade_risk_pct"),
        "base_max_actual_trade_risk_pct": get("base_max_actual_trade_risk_pct"),
        "base_median_actual_trade_risk_pct": get("base_median_actual_trade_risk_pct"),
        "base_median_target_trade_risk_pct": get("base_median_target_trade_risk_pct"),
        "base_median_min_lot_binding_rate": get("base_median_min_lot_binding_rate"),
        "base_median_trade_acceptance_rate": get("base_median_trade_acceptance_rate"),
        "base_median_skipped_edge_trade_count": get("base_median_skipped_edge_trade_count"),
        "base_median_skipped_asset_trade_count": get("base_median_skipped_asset_trade_count"),
        "base_median_skipped_zero_total_capacity": get("base_median_skipped_zero_total_capacity"),
        "base_median_skipped_min_lot_exceeds_total_capacity": get("base_median_skipped_min_lot_exceeds_total_capacity"),
        "base_median_skipped_min_lot_inflation_guard": get("base_median_skipped_min_lot_inflation_guard"),
        "stress02_pass_rate": get("stress02_pass_rate"),
        "stress02_fail_rate": get("stress02_fail_rate"),
        "stress02_hard_fail_rate": get("stress02_hard_fail_rate"),
        "stress02_max_drawdown_fail_rate": get("stress02_max_drawdown_fail_rate"),
        "stress02_daily_drawdown_fail_rate": get("stress02_daily_drawdown_fail_rate"),
        "stress02_daily_symbol_risk_fail_rate": get("stress02_daily_symbol_risk_fail_rate"),
        "stress02_end_of_history_fail_rate": get("stress02_end_of_history_fail_rate"),
        "stress02_p99_days": get("stress02_p99_days"),
        "min_pass_rate_across_costs": get("min_pass_rate_across_costs"),
        "max_hard_fail_rate_across_costs": get("max_hard_fail_rate_across_costs"),
        "max_daily_drawdown_fail_rate_across_costs": get("max_daily_drawdown_fail_rate_across_costs"),
        "min_worst_buffer_across_costs": get("min_worst_buffer_across_costs"),
    }


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
        "payoff_context_version", "payoff_context_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            "Representative ledger missing required broker/min-lot-aware columns. "
            "Run patched 04B, optimized 04C, patched 05 first. Missing: " + str(missing)
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
        cols = [c for c in ["representative_id", "edge_id", "asset", "entry_time", "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "min_executable_risk_amount", "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk"] if c in invalid.columns]
        raise ValueError(
            "Representative ledger contains invalid broker/min-lot risk rows. "
            "Step 08 requires executable risk metadata for every simulated trade. Examples:\n"
            + invalid[cols].head(20).to_string(index=False)
        )
    df["afml_bet_size_for_sizing"] = df[AFML_BET_SIZE_COLUMN].clip(lower=AFML_SIZE_FLOOR, upper=AFML_SIZE_CAP)
    df = df.dropna(subset=[
        "representative_id", "edge_id", "asset", "family", "entry_time", "exit_time",
        "executed_r", "afml_bet_size_for_sizing", "risk_per_1_lot", "broker_min_lot", "broker_lot_step"
    ]).copy()
    df = df[(df["exit_time"] > df["entry_time"]) & (df["afml_bet_size_for_sizing"] > AFML_SIZE_FLOOR + EPS)].copy()
    df = df.sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    if df.empty:
        raise RuntimeError("Representative ledger is empty after broker/min-lot-aware cleaning.")
    assets = sorted(df["asset"].astype(str).unique())
    families = sorted(df["family"].astype(str).unique())
    edges = sorted(df["edge_id"].astype(str).unique())
    asset_to_code = {a: i for i, a in enumerate(assets)}
    family_to_code = {a: i for i, a in enumerate(families)}
    edge_to_code = {a: i for i, a in enumerate(edges)}
    df["asset_code"] = df["asset"].astype(str).map(asset_to_code).astype(int)
    df["family_code"] = df["family"].astype(str).map(family_to_code).astype(int)
    df["edge_code"] = df["edge_id"].astype(str).map(edge_to_code).astype(int)
    return df, asset_to_code, family_to_code, edge_to_code


def load_hrp_weights(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing HRP weights: {path}")
    df = pd.read_csv(path)
    required = [
        "return_mode", "universe", "profile_role", "linkage_method", "edge_id", "weight",
        "asset", "family", "side_name", "threshold", "representative_id",
        "edge_classification", "representative_roles",
        "payoff_context_version", "payoff_context_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"HRP weights missing required columns: {missing}")
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df["threshold"] = pd.to_numeric(df["threshold"], errors="coerce")
    df = df.dropna(subset=["return_mode", "universe", "profile_role", "linkage_method", "edge_id", "representative_id", "weight"]).copy()
    df = df[(df["weight"] > 0) & df["return_mode"].isin(SIMULATE_RETURN_MODES) & df["universe"].isin(SIMULATE_UNIVERSES) & df["profile_role"].isin(SIMULATE_PROFILE_ROLES) & df["linkage_method"].isin(SIMULATE_LINKAGE_METHODS)].copy()
    if df.empty:
        raise RuntimeError("No valid HRP weights after Step 08 filters. Confirm Steps 04C-07 were rerun with profile_role=compound_velocity.")
    parts = []
    for _, g in df.groupby(["return_mode", "universe", "profile_role", "linkage_method"], sort=False):
        g = g.copy()
        s = g["weight"].sum()
        if s > 0:
            g["weight"] = g["weight"] / s
            parts.append(g)
    if not parts:
        raise RuntimeError("No valid normalized HRP weight groups found.")
    return pd.concat(parts, ignore_index=True)


def make_scenario_id(
    return_mode: str,
    universe: str,
    profile_role: str,
    linkage_method: str,
    base_budget_pct: float,
    max_single_trade_risk_pct: float,
    overlap_mode_name: str,
    daily_drawdown_name: str,
    foundation_candidate_name: str,
    max_min_lot_risk_inflation: float | None,
) -> str:
    return (
        f"{return_mode}__{universe}__{profile_role}__{linkage_method}"
        f"__direct_weight__foundation_{scenario_token(foundation_candidate_name)}"
        f"__budget{risk_label(base_budget_pct)}pct"
        f"__maxtrade{risk_label(max_single_trade_risk_pct)}pct"
        f"__{overlap_mode_name}"
        f"__minlotinfl{inflation_guard_label(max_min_lot_risk_inflation)}"
        f"__{daily_drawdown_name}"
    )


def get_weight_groups(weights: pd.DataFrame) -> list[dict[str, Any]]:
    groups = []
    for keys, g in weights.groupby(["return_mode", "universe", "profile_role", "linkage_method"], sort=True):
        g = g.copy()
        g["weight"] = pd.to_numeric(g["weight"], errors="coerce")
        g = g.dropna(subset=["weight"])
        g = g[g["weight"] > 0].copy()
        if g.empty:
            continue
        g["weight"] = g["weight"] / g["weight"].sum()
        return_mode, universe, profile_role, linkage_method = keys
        groups.append({"return_mode": return_mode, "universe": universe, "profile_role": profile_role, "linkage_method": linkage_method, "weights": g})
    if not groups:
        raise RuntimeError("No HRP scenario groups were created.")
    return groups


def build_scenario_configs(weight_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validate_foundation_scenario_specs()
    configs: list[dict[str, Any]] = []
    for wg in weight_groups:
        for spec in FOUNDATION_SCENARIO_SPECS:
            overlap_name = str(spec["overlap_mode"])
            overlap_mode = dict(OVERLAP_MODE_LIBRARY[overlap_name])
            base_budget_pct = float(spec["base_portfolio_risk_budget_pct"])
            configured_max_single_trade_risk_pct = float(spec["max_single_trade_risk_pct"])
            max_single_trade_risk_pct = (
                min(configured_max_single_trade_risk_pct, PROP_MAX_SINGLE_TRADE_RISK_PCT)
                if PROP_MAX_SINGLE_TRADE_RISK_PCT is not None
                else configured_max_single_trade_risk_pct
            )
            if FLOATING_LOSS_GUARD_ENABLED and FLOATING_LOSS_GUARD_AMOUNT is not None:
                floating_guard_pct_at_start = FLOATING_LOSS_GUARD_AMOUNT / max(CURRENT_BALANCE, EPS)
                overlap_mode["max_total_open_risk_pct"] = min(
                    float(overlap_mode["max_total_open_risk_pct"]),
                    float(floating_guard_pct_at_start),
                )
            max_min_lot_risk_inflation = optional_float(
                spec.get("max_min_lot_risk_inflation")
            )
            for daily_dd in DAILY_DRAWDOWN_CONFIGS:
                scenario_id = make_scenario_id(
                    wg["return_mode"],
                    wg["universe"],
                    wg["profile_role"],
                    wg["linkage_method"],
                    base_budget_pct,
                    max_single_trade_risk_pct,
                    overlap_name,
                    str(daily_dd["name"]),
                    str(spec["name"]),
                    max_min_lot_risk_inflation,
                )
                configs.append({
                    "scenario_id": scenario_id,
                    "foundation_candidate_name": str(spec["name"]),
                    "foundation_research_role": str(spec.get("research_role", "")),
                    "return_mode": wg["return_mode"],
                    "universe": wg["universe"],
                    "profile_role": wg["profile_role"],
                    "linkage_method": wg["linkage_method"],
                    "risk_sizing_mode": (
                        RISK_SIZING_MODE
                        if not AFMLC.is_enabled(spec.get("afml_concurrency_quantile", 0.0))
                        else AFMLC.SIZING_MODE_AFML_CONCURRENCY
                    ),
                    # 0.0 = off (never NaN: it is a groupby key below).
                    "afml_concurrency_quantile": AFMLC.validate_quantile(spec.get("afml_concurrency_quantile", 0.0)),
                    "base_portfolio_risk_budget_pct": base_budget_pct,
                    "base_risk_pct": base_budget_pct,
                    "base_risk_pct_label": pct_str(base_budget_pct),
                    "configured_max_single_trade_risk_pct": configured_max_single_trade_risk_pct,
                    "max_single_trade_risk_pct": max_single_trade_risk_pct,
                    "prop_max_single_trade_risk_pct": PROP_MAX_SINGLE_TRADE_RISK_PCT,
                    "floating_loss_guard_enabled": FLOATING_LOSS_GUARD_ENABLED,
                    "floating_loss_guard_internal_amount": FLOATING_LOSS_GUARD_AMOUNT,
                    "max_single_trade_risk_pct_label": pct_str(
                        max_single_trade_risk_pct
                    ),
                    "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
                    "min_lot_risk_inflation_guard_enabled": (
                        max_min_lot_risk_inflation is not None
                    ),
                    "overlap_mode": overlap_name,
                    "overlap_config": overlap_mode,
                    "max_total_open_risk_pct": float(
                        overlap_mode["max_total_open_risk_pct"]
                    ),
                    "max_asset_open_risk_pct": float(
                        overlap_mode["max_asset_open_risk_pct"]
                    ),
                    "max_family_open_risk_pct": float(
                        overlap_mode["max_family_open_risk_pct"]
                    ),
                    "max_edge_open_risk_pct": float(
                        overlap_mode["max_edge_open_risk_pct"]
                    ),
                    "max_open_trades_per_edge": int(
                        overlap_mode["max_open_trades_per_edge"]
                    ),
                    "max_open_trades_per_asset": int(
                        overlap_mode["max_open_trades_per_asset"]
                    ),
                    "account_rule_profile": str(
                        daily_dd.get(
                            "account_rule_profile",
                            ACCOUNT_RULE_PROFILE,
                        )
                    ),
                    "account_stage": str(
                        daily_dd.get("account_stage", ACCOUNT_STAGE)
                    ),
                    "daily_drawdown_name": str(daily_dd["name"]),
                    "daily_drawdown_enabled": bool(
                        daily_dd.get("enabled", False)
                    ),
                    "daily_drawdown_pct": float(
                        daily_dd.get("pct", DAILY_LOSS_PCT) or DAILY_LOSS_PCT
                    ),
                    "daily_drawdown_amount": float(
                        daily_dd.get("amount", DAILY_LOSS_AMOUNT)
                        or DAILY_LOSS_AMOUNT
                    ),
                    "daily_drawdown_mode": str(
                        daily_dd.get(
                            "mode",
                            ACCOUNT_RULES["daily_drawdown"]["mode"],
                        )
                    ),
                    "daily_symbol_risk_rule_enabled": bool(
                        daily_dd.get(
                            "daily_symbol_risk_rule_enabled",
                            False,
                        )
                    ),
                    "daily_symbol_risk_enforcement_mode": str(
                        daily_dd.get(
                            "symbol_risk_mode",
                            ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"],
                        )
                    ),
                    "max_daily_risk_per_symbol_pct": optional_float(
                        daily_dd.get("max_daily_risk_per_symbol_pct")
                    ),
                    "max_daily_risk_per_symbol_amount": optional_float(
                        daily_dd.get("max_daily_risk_per_symbol_amount")
                    ),
                    "internal_operating_risk_per_symbol_pct": optional_float(
                        daily_dd.get(
                            "internal_operating_risk_per_symbol_pct"
                        )
                    ),
                    "internal_operating_risk_per_symbol_amount": optional_float(
                        daily_dd.get(
                            "internal_operating_risk_per_symbol_amount"
                        )
                    ),
                    "symbol_risk_safety_reserve_amount": optional_float(
                        daily_dd.get("symbol_risk_safety_reserve_amount")
                    ),
                    "daily_drawdown_config": dict(daily_dd),
                })
    scenario_ids = [str(c["scenario_id"]) for c in configs]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("Focused foundation scenario IDs are not unique.")
    return configs


def prepare_base_arrays_for_group(wg: dict[str, Any], ledger: pd.DataFrame) -> dict[str, Any]:
    weights = wg["weights"].copy()
    rep_ids = weights["representative_id"].astype(str).tolist()
    weight_by_rep = dict(zip(weights["representative_id"].astype(str), weights["weight"]))
    trades = ledger[ledger["representative_id"].astype(str).isin(rep_ids)].copy()
    if trades.empty:
        raise RuntimeError(f"No ledger trades found for {wg['return_mode']}/{wg['universe']}/{wg['profile_role']}/{wg['linkage_method']}.")
    validate_representative_payoff_context_handoff(
        trades, weights,
        context=(
            "Step08 HRP/ledger payoff context "
            f"{wg['return_mode']}/{wg['universe']}/{wg['profile_role']}/{wg['linkage_method']}"
        ),
    )
    trades["hrp_weight"] = trades["representative_id"].astype(str).map(weight_by_rep)
    trades = trades.dropna(subset=["hrp_weight"]).sort_values(["entry_time", "exit_time"]).reset_index(drop=True)
    return {
        "group_key": (wg["return_mode"], wg["universe"], wg["profile_role"], wg["linkage_method"]),
        "return_mode": wg["return_mode"],
        "universe": wg["universe"],
        "profile_role": wg["profile_role"],
        "linkage_method": wg["linkage_method"],
        "n_edges": int(weights["edge_id"].nunique()),
        "n_representatives": int(weights["representative_id"].nunique()),
        "hrp_effective_n": safe_div(1.0, float((weights["weight"] ** 2).sum())),
        "hrp_top_weight": float(weights["weight"].max()),
        "hrp_min_weight": float(weights["weight"].min()),
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
        "final_data_end_ns": int(max(trades["entry_time"].astype("int64").max(), trades["exit_time"].astype("int64").max())),
    }


def add_direct_risk_array(base_arrays: dict[str, Any], base_budget_pct: float, max_single_trade_risk_pct: float, afml_concurrency_quantile: float = 0.0) -> dict[str, Any]:
    arr = dict(base_arrays)
    # AFML Ch.10.2/10.4 (2026-10-17): with a positive quantile each signal's calibrated bet size is divided by its edge's
    # concurrency quantile c_q, so the edge's exposure is the 10.4 average position at c_q concurrent bets. Off (0.0)
    # leaves afml_bet_size untouched -> identical to the original direct-weight sizing.
    sizing_m = AFMLC.scaled_afml_size(base_arrays, afml_concurrency_quantile)
    target = np.array([
        compute_direct_trade_risk_pct(w, m, base_budget_pct, max_single_trade_risk_pct)
        for w, m in zip(base_arrays["hrp_weight"], sizing_m)
    ], dtype=float)
    arr["afml_concurrency_quantile"] = float(afml_concurrency_quantile or 0.0)
    # Backward-compatible alias: this is target/requested risk pct, not executable risk.
    arr["target_trade_risk_pct"] = target
    arr["trade_risk_pct"] = target
    arr["base_portfolio_risk_budget_pct"] = float(base_budget_pct)
    arr["max_single_trade_risk_pct"] = float(max_single_trade_risk_pct)
    return arr


def build_rolling_start_times(arr: dict[str, Any], step: int, max_starts: int | None) -> list[int]:
    entry_ns = arr["entry_ns"]
    latest_allowed = int(arr["final_data_end_ns"]) - int(MIN_REMAINING_OBSERVATION_DAYS_AFTER_START * DAY_NS)
    mask = entry_ns <= latest_allowed
    if not mask.any():
        raise RuntimeError("No eligible rolling starts after observation-tail filter.")
    days = pd.Series(arr["entry_day"][mask]).drop_duplicates().sort_values().to_numpy(dtype=np.int64)
    selected_days = days[::max(1, int(step))]
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
    return starts


def simulate_from_start_fast(arr: dict[str, Any], scenario: dict[str, Any], start_ns: int, cost_stress_r: float, n_assets: int, n_families: int, n_edges_global: int, code_to_asset: dict[int, str]) -> dict[str, Any]:
    entry_ns, exit_ns = arr["entry_ns"], arr["exit_ns"]
    final_data_end_ns = int(arr["final_data_end_ns"])
    start_idx = int(np.searchsorted(entry_ns, start_ns, side="left"))
    end_idx = len(entry_ns)

    balance = float(CURRENT_BALANCE)
    min_balance = max_balance = balance
    trailing_high_watermark = max(ACCOUNT_START_TRAILING_HIGH_WATERMARK, balance)
    trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
    min_buffer = drawdown_stats(balance, 0.0, trailing_high_watermark)["remaining_dd_buffer"]

    open_risk_total = 0.0
    open_risk_by_asset = np.zeros(n_assets, dtype=float)
    open_risk_by_family = np.zeros(n_families, dtype=float)
    open_risk_by_edge = np.zeros(n_edges_global, dtype=float)
    open_count_by_asset = np.zeros(n_assets, dtype=np.int32)
    open_count_by_edge = np.zeros(n_edges_global, dtype=np.int32)
    open_heap: list[tuple[int, int, float, float, int, int, int]] = []

    heap_counter = attempted = accepted = skipped = closed = 0
    skipped_zero_target_risk = 0
    skipped_invalid_broker_risk = 0
    skipped_min_lot_risk_cap = 0
    skipped_overlap_risk_cap = 0
    skipped_daily_symbol_risk_cap = 0
    skipped_min_lot_inflation_guard = 0
    target_reduced_to_cap_count = 0
    broker_backoff_applied_count = 0

    # Detailed admission diagnostics. Each rejected trade receives one primary
    # reason so the counters can be summed without double counting.
    admission_skip_counts = {
        "skipped_edge_trade_count": 0,
        "skipped_asset_trade_count": 0,
        "skipped_zero_single_trade_capacity": 0,
        "skipped_zero_total_capacity": 0,
        "skipped_zero_asset_capacity": 0,
        "skipped_zero_family_capacity": 0,
        "skipped_zero_edge_capacity": 0,
        "skipped_zero_daily_symbol_capacity": 0,
        "skipped_min_lot_exceeds_single_trade_capacity": 0,
        "skipped_min_lot_exceeds_total_capacity": 0,
        "skipped_min_lot_exceeds_asset_capacity": 0,
        "skipped_min_lot_exceeds_family_capacity": 0,
        "skipped_min_lot_exceeds_edge_capacity": 0,
        "skipped_min_lot_exceeds_daily_symbol_capacity": 0,
        "skipped_post_rounding_single_trade_cap": 0,
        "skipped_post_rounding_total_cap": 0,
        "skipped_post_rounding_asset_cap": 0,
        "skipped_post_rounding_family_cap": 0,
        "skipped_post_rounding_edge_cap": 0,
        "skipped_post_rounding_daily_symbol_cap": 0,
    }
    binding_capacity_counts = {
        "single_trade": 0,
        "total": 0,
        "asset": 0,
        "family": 0,
        "edge": 0,
        "daily_symbol": 0,
    }
    max_open_risk_pct = 0.0
    max_open_trades = 0
    active_days = {day_from_ns(start_ns)}
    target_hit_time = failure_time = None
    target_hit_balance = np.nan
    failure_reason = ""

    asset_trades = np.zeros(n_assets, dtype=np.int32)
    asset_wins = np.zeros(n_assets, dtype=np.int32)
    asset_losses = np.zeros(n_assets, dtype=np.int32)
    asset_net_pnl = np.zeros(n_assets, dtype=float)

    afml_sum = 0.0
    target_risk_pct_sum = 0.0
    actual_risk_pct_sum = 0.0
    target_risk_values: list[float] = []
    actual_risk_values: list[float] = []
    risk_inflation_values: list[float] = []
    actual_lot_values: list[float] = []
    min_lot_binding_count = 0
    max_actual_trade_risk_pct = 0.0
    max_target_trade_risk_pct = 0.0

    daily_cfg = scenario.get("daily_drawdown_config", {}) or {}
    daily_enabled = bool(daily_cfg.get("enabled", True))
    daily_pct = float(daily_cfg.get("pct", DAILY_LOSS_PCT) or DAILY_LOSS_PCT)
    daily_amount = float(daily_cfg.get("amount", DAILY_LOSS_AMOUNT) or DAILY_LOSS_AMOUNT)
    daily_mode = str(daily_cfg.get("mode", ACCOUNT_RULES["daily_drawdown"]["mode"]))
    daily_symbol_risk_rule_enabled = bool(daily_cfg.get("daily_symbol_risk_rule_enabled", False))
    daily_symbol_risk_enforcement_mode = str(
        daily_cfg.get("symbol_risk_mode", ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])
    )
    symbol_daily_hard_cap_amount = optional_float(daily_cfg.get("max_daily_risk_per_symbol_amount"))
    symbol_internal_operating_cap_amount = optional_float(
        daily_cfg.get("internal_operating_risk_per_symbol_amount")
    )
    if daily_symbol_risk_rule_enabled:
        if symbol_daily_hard_cap_amount is None or symbol_internal_operating_cap_amount is None:
            raise ValueError(
                "Enabled daily symbol-risk enforcement requires both legal and internal cap amounts."
            )
        if symbol_internal_operating_cap_amount > symbol_daily_hard_cap_amount + EPS:
            raise ValueError(
                "Internal symbol operating cap cannot exceed the legal hard cap: "
                f"{symbol_internal_operating_cap_amount} > {symbol_daily_hard_cap_amount}"
            )

    daily_day = day_from_ns(start_ns)
    daily_start_balance = balance
    daily_start_equity = account_equity_proxy(balance, open_risk_total)
    daily_floor = daily_loss_floor(daily_start_balance, daily_start_equity)
    daily_amount = account_daily_drawdown_amount(
        ACCOUNT_RULES,
        day_start_balance=daily_start_balance,
        day_start_equity=daily_start_equity,
    )
    daily_start_value = daily_floor + daily_amount if daily_enabled else balance
    daily_worst_buffer = daily_amount if daily_enabled else float("inf")
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
            daily_floor = daily_loss_floor(daily_start_balance, daily_start_equity)
            daily_amount = account_daily_drawdown_amount(
                ACCOUNT_RULES,
                day_start_balance=daily_start_balance,
                day_start_equity=daily_start_equity,
            )
            daily_start_value = daily_floor + daily_amount if daily_enabled else balance
            daily_worst_buffer = min(
                daily_worst_buffer,
                daily_amount if daily_enabled else float("inf"),
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
        if st["floor_breached"] and not failure_reason:
            failure_reason = "overall_drawdown_floor_breached"
            failure_time = event_ns
            return

        if daily_enabled:
            ref = daily_reference_value()
            daily_buffer = ref - daily_floor
            daily_worst_buffer = min(daily_worst_buffer, daily_buffer)
            max_daily_drawdown_pct = max(max_daily_drawdown_pct, safe_div(daily_start_value - ref, PROP_START_BALANCE))
            if daily_buffer < -EPS and not failure_reason:
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
                and not failure_reason
            ):
                failure_reason = "max_daily_symbol_risk_breached"
                failure_time = event_ns

    def apply_pnl(event_ns: int, pnl: float) -> None:
        nonlocal balance, min_balance, max_balance, target_hit_time, target_hit_balance, trailing_high_watermark, trailing_floor
        balance += pnl
        trailing_high_watermark = max(trailing_high_watermark, balance)
        trailing_floor = trailing_drawdown_floor(trailing_high_watermark)
        min_balance = min(min_balance, balance)
        max_balance = max(max_balance, balance)
        active_days.add(day_from_ns(event_ns))
        refresh_qualifying_day()
        check_rules(event_ns)
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
            target_hit_time = event_ns
            target_hit_balance = balance

    def close_due(current_ns: int) -> None:
        nonlocal open_risk_total, closed
        reset_daily_if_needed(current_ns)
        while open_heap and open_heap[0][0] <= current_ns:
            this_exit_ns = open_heap[0][0]
            reset_daily_if_needed(this_exit_ns)
            batch = 0.0
            while open_heap and open_heap[0][0] == this_exit_ns:
                _, _, risk_amount, r, ac, fc, ec = heapq.heappop(open_heap)
                open_risk_total -= risk_amount
                open_risk_by_asset[ac] -= risk_amount
                open_risk_by_family[fc] -= risk_amount
                open_risk_by_edge[ec] -= risk_amount
                open_count_by_asset[ac] -= 1
                open_count_by_edge[ec] -= 1
                pnl = risk_amount * r
                batch += pnl
                if pnl < 0.0:
                    daily_closed_loss_by_asset[ac] += -pnl
                closed += 1
                asset_trades[ac] += 1
                asset_net_pnl[ac] += pnl
                if pnl > 0:
                    asset_wins[ac] += 1
                elif pnl < 0:
                    asset_losses[ac] += 1
            if abs(batch) > EPS:
                apply_pnl(this_exit_ns, batch)
            else:
                check_rules(this_exit_ns)
            if (STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason):
                return

    mode = scenario["overlap_config"]
    max_single_trade_risk_pct = float(scenario["max_single_trade_risk_pct"])
    max_min_lot_risk_inflation = optional_float(
        scenario.get("max_min_lot_risk_inflation")
    )
    capacity_priority = {
        "single_trade": 0,
        "total": 1,
        "asset": 2,
        "family": 3,
        "edge": 4,
        "daily_symbol": 5,
    }

    check_rules(start_ns)

    for i in range(start_idx, end_idx):
        current_ns = int(entry_ns[i])
        close_due(current_ns)
        if (STOP_IMMEDIATELY_ON_TARGET and target_hit_time is not None) or (STOP_ON_RULE_BREACH and failure_reason):
            break

        reset_daily_if_needed(current_ns)
        attempted += 1
        active_days.add(day_from_ns(current_ns))
        refresh_qualifying_day()

        target_trade_risk_pct = float(arr["target_trade_risk_pct"][i])
        if not np.isfinite(target_trade_risk_pct) or target_trade_risk_pct <= EPS:
            skipped += 1
            skipped_zero_target_risk += 1
            continue

        target_risk_amount = balance * target_trade_risk_pct
        ac, fc, ec = (
            int(arr["asset_code"][i]),
            int(arr["family_code"][i]),
            int(arr["edge_code"][i]),
        )

        # Count restrictions are diagnosed independently from monetary risk
        # capacity. This reveals whether relaxed packing can help without
        # increasing any stop-risk cap.
        if open_count_by_edge[ec] >= int(mode["max_open_trades_per_edge"]):
            skipped += 1
            skipped_overlap_risk_cap += 1
            admission_skip_counts["skipped_edge_trade_count"] += 1
            continue
        if open_count_by_asset[ac] >= int(mode["max_open_trades_per_asset"]):
            skipped += 1
            skipped_overlap_risk_cap += 1
            admission_skip_counts["skipped_asset_trade_count"] += 1
            continue

        capacities = {
            "single_trade": max(
                0.0,
                balance * max_single_trade_risk_pct,
            ),
            "total": max(
                0.0,
                balance * float(mode["max_total_open_risk_pct"])
                - open_risk_total,
            ),
            "asset": max(
                0.0,
                balance * float(mode["max_asset_open_risk_pct"])
                - open_risk_by_asset[ac],
            ),
            "family": max(
                0.0,
                balance * float(mode["max_family_open_risk_pct"])
                - open_risk_by_family[fc],
            ),
            "edge": max(
                0.0,
                balance * float(mode["max_edge_open_risk_pct"])
                - open_risk_by_edge[ec],
            ),
        }
        if (
            daily_symbol_risk_rule_enabled
            and symbol_internal_operating_cap_amount is not None
        ):
            capacities["daily_symbol"] = max(
                0.0,
                symbol_internal_operating_cap_amount
                - daily_closed_loss_by_asset[ac]
                - open_risk_by_asset[ac],
            )

        binding_capacity_name, effective_risk_cap = min(
            capacities.items(),
            key=lambda item: (
                float(item[1]),
                capacity_priority[item[0]],
            ),
        )
        binding_capacity_counts[binding_capacity_name] += 1

        if effective_risk_cap <= EPS:
            skipped += 1
            skipped_min_lot_risk_cap += 1
            admission_skip_counts[
                f"skipped_zero_{binding_capacity_name}_capacity"
            ] += 1
            continue

        br = broker_executable_risk(
            target_risk_amount,
            float(arr["risk_per_1_lot"][i]),
            float(arr["broker_min_lot"][i]),
            float(arr["broker_lot_step"][i]),
            max_risk_amount=effective_risk_cap,
        )
        if not br["valid"]:
            skipped += 1
            skipped_invalid_broker_risk += 1
            continue

        if bool(br.get("target_reduced_to_cap", False)):
            target_reduced_to_cap_count += 1
        if bool(br.get("backoff_applied", False)):
            broker_backoff_applied_count += 1

        # Record the risk actually requested from the broker after clipping to
        # the tightest live cap.
        sizing_target_risk_amount = float(
            br.get("sizing_target_risk_amount", target_risk_amount)
        )
        target_trade_risk_pct = safe_div(
            sizing_target_risk_amount,
            balance,
        )

        actual_risk_amount = float(br["actual_risk_amount"])
        actual_risk_pct = safe_div(actual_risk_amount, balance)
        if not np.isfinite(actual_risk_pct) or actual_risk_pct <= EPS:
            skipped += 1
            skipped_invalid_broker_risk += 1
            continue

        # A failed cap fit means no positive broker-valid lot, normally the
        # minimum lot, fits inside the current binding capacity.
        if (
            SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS
            and not bool(br["cap_fit"])
        ):
            skipped += 1
            skipped_min_lot_risk_cap += 1
            admission_skip_counts[
                f"skipped_min_lot_exceeds_{binding_capacity_name}_capacity"
            ] += 1
            continue

        risk_inflation = optional_float(br.get("risk_inflation"))
        if (
            max_min_lot_risk_inflation is not None
            and bool(br.get("min_lot_binding", False))
            and risk_inflation is not None
            and risk_inflation > max_min_lot_risk_inflation + EPS
        ):
            skipped += 1
            skipped_min_lot_inflation_guard += 1
            continue

        # These post-rounding checks should be zero because broker sizing used
        # the minimum of the same capacities. Separate counters make numerical
        # or contract drift visible instead of hiding it in one overlap bucket.
        proposed_symbol_usage = symbol_daily_risk_usage(
            daily_closed_loss_by_asset[ac],
            open_risk_by_asset[ac] + actual_risk_amount,
        )
        post_rounding_breach: str | None = None
        if actual_risk_amount > capacities["single_trade"] + EPS:
            post_rounding_breach = "single_trade"
        elif (
            open_risk_total + actual_risk_amount
            > balance * float(mode["max_total_open_risk_pct"]) + EPS
        ):
            post_rounding_breach = "total"
        elif (
            open_risk_by_asset[ac] + actual_risk_amount
            > balance * float(mode["max_asset_open_risk_pct"]) + EPS
        ):
            post_rounding_breach = "asset"
        elif (
            open_risk_by_family[fc] + actual_risk_amount
            > balance * float(mode["max_family_open_risk_pct"]) + EPS
        ):
            post_rounding_breach = "family"
        elif (
            open_risk_by_edge[ec] + actual_risk_amount
            > balance * float(mode["max_edge_open_risk_pct"]) + EPS
        ):
            post_rounding_breach = "edge"
        elif (
            daily_symbol_risk_rule_enabled
            and symbol_internal_operating_cap_amount is not None
            and proposed_symbol_usage
            > symbol_internal_operating_cap_amount + EPS
        ):
            post_rounding_breach = "daily_symbol"

        if post_rounding_breach is not None:
            skipped += 1
            skipped_overlap_risk_cap += 1
            admission_skip_counts[
                f"skipped_post_rounding_{post_rounding_breach}_cap"
            ] += 1
            if post_rounding_breach == "daily_symbol":
                skipped_daily_symbol_risk_cap += 1
            continue

        accepted += 1
        accepted_trade_days.add(day_from_ns(current_ns))
        afml_sum += float(arr["afml_bet_size"][i])
        target_risk_pct_sum += target_trade_risk_pct
        actual_risk_pct_sum += actual_risk_pct
        target_risk_values.append(float(target_trade_risk_pct))
        actual_risk_values.append(float(actual_risk_pct))
        if np.isfinite(br["risk_inflation"]):
            risk_inflation_values.append(float(br["risk_inflation"]))
        if np.isfinite(br["actual_lot"]):
            actual_lot_values.append(float(br["actual_lot"]))
        if bool(br["min_lot_binding"]):
            min_lot_binding_count += 1
        max_actual_trade_risk_pct = max(max_actual_trade_risk_pct, actual_risk_pct)
        max_target_trade_risk_pct = max(max_target_trade_risk_pct, target_trade_risk_pct)

        heapq.heappush(
            open_heap,
            (int(exit_ns[i]), heap_counter, actual_risk_amount, float(arr["executed_r"][i]) - float(cost_stress_r), ac, fc, ec)
        )
        heap_counter += 1
        open_risk_total += actual_risk_amount
        open_risk_by_asset[ac] += actual_risk_amount
        open_risk_by_family[fc] += actual_risk_amount
        open_risk_by_edge[ec] += actual_risk_amount
        open_count_by_asset[ac] += 1
        open_count_by_edge[ec] += 1
        max_open_trades = max(max_open_trades, len(open_heap))
        max_open_risk_pct = max(max_open_risk_pct, safe_div(open_risk_total, balance))
        # For equity_proxy daily DD, opening risk can immediately reduce the conservative equity proxy.
        check_rules(current_ns)
        if (STOP_ON_RULE_BREACH and failure_reason):
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
    failed = not passed

    def pctile(values: list[float], q: float) -> float:
        return float(np.nanpercentile(values, q)) if values else np.nan

    out = {
        "start_time": ns_to_ts(start_ns),
        "start_ns": int(start_ns),
        "cost_stress_r": float(cost_stress_r),
        "passed": bool(passed),
        "failed": bool(failed),
        "hard_failed": bool(hard_failed),
        "max_drawdown_failed": bool(max_drawdown_failed),
        "daily_drawdown_failed": bool(daily_drawdown_failed),
        "daily_symbol_risk_failed": bool(daily_symbol_risk_failed),
        "end_of_history_fail": bool(end_of_history_fail),
        "failure_reason": failure_reason,
        "target_hit_time": ns_to_ts(target_hit_time),
        "failure_time": ns_to_ts(failure_time),
        "trading_days_to_target": len([d for d in active_days if target_hit_time is not None and d <= day_from_ns(target_hit_time)]) if target_hit_time is not None else np.nan,
        "calendar_days_to_target": safe_div(target_hit_time - start_ns, DAY_NS) if target_hit_time is not None else np.nan,
        "observation_calendar_days": safe_div(final_data_end_ns - start_ns, DAY_NS),
        "start_balance": CURRENT_BALANCE,
        "start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
        "start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
        "start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
        "ending_balance": balance,
        "target_hit_balance": target_hit_balance,
        "minimum_trading_days_enabled": MINIMUM_TRADING_DAYS_ENABLED,
        "minimum_trading_days_required": MINIMUM_TRADING_DAYS_REQUIRED,
        "qualifying_trading_days": int(len(qualifying_days)),
        "minimum_trading_days_satisfied": minimum_days_satisfied(
            ACCOUNT_RULES, len(qualifying_days)
        ),
        "net_profit": balance - CURRENT_BALANCE,
        "min_balance": min_balance,
        "max_balance": max_balance,
        "remaining_buffer_at_end": drawdown_stats(balance, open_risk_total, trailing_high_watermark)["remaining_dd_buffer"],
        "worst_buffer_above_floor": min_buffer,
        "trailing_high_watermark": trailing_high_watermark,
        "trailing_drawdown_floor_at_end": trailing_floor,
        "trailing_floor_locked_at_initial_balance": bool(trailing_floor >= TRAILING_DRAWDOWN_STOP_FLOOR - EPS),
        "daily_drawdown_name": scenario.get("daily_drawdown_name", ""),
        "daily_drawdown_enabled": bool(daily_enabled),
        "daily_drawdown_pct": float(daily_pct),
        "daily_drawdown_amount": float(daily_amount),
        "daily_drawdown_mode": daily_mode,
        "daily_worst_buffer_above_floor": daily_worst_buffer,
        "max_daily_drawdown_pct": max_daily_drawdown_pct,
        "account_stage": str(scenario.get("account_stage", daily_cfg.get("account_stage", "challenge"))),
        "daily_symbol_risk_rule_enabled": bool(daily_symbol_risk_rule_enabled),
        "daily_symbol_risk_enforcement_mode": daily_symbol_risk_enforcement_mode,
        "max_daily_risk_per_symbol_pct": optional_float(daily_cfg.get("max_daily_risk_per_symbol_pct")),
        "max_daily_risk_per_symbol_amount": symbol_daily_hard_cap_amount,
        "internal_operating_risk_per_symbol_pct": optional_float(daily_cfg.get("internal_operating_risk_per_symbol_pct")),
        "internal_operating_risk_per_symbol_amount": symbol_internal_operating_cap_amount,
        "symbol_risk_safety_reserve_amount": optional_float(daily_cfg.get("symbol_risk_safety_reserve_amount")),
        "max_daily_symbol_risk_amount_observed": max_daily_symbol_risk_amount,
        "max_daily_symbol_risk_pct_observed": max_daily_symbol_risk_pct,
        "attempted_trades": attempted,
        "accepted_trades": accepted,
        "skipped_trades": skipped,
        "skipped_zero_target_risk": skipped_zero_target_risk,
        "skipped_invalid_broker_risk": skipped_invalid_broker_risk,
        "skipped_min_lot_risk_cap": skipped_min_lot_risk_cap,
        "skipped_overlap_risk_cap": skipped_overlap_risk_cap,
        "skipped_daily_symbol_risk_cap": skipped_daily_symbol_risk_cap,
        "skipped_internal_symbol_risk_cap": skipped_daily_symbol_risk_cap,
        "skipped_min_lot_inflation_guard": skipped_min_lot_inflation_guard,
        "target_reduced_to_cap_count": target_reduced_to_cap_count,
        "target_reduced_to_cap_rate_attempted": safe_div(
            target_reduced_to_cap_count,
            attempted,
        ),
        "broker_backoff_applied_count": broker_backoff_applied_count,
        "broker_backoff_applied_rate_attempted": safe_div(
            broker_backoff_applied_count,
            attempted,
        ),
        "trade_acceptance_rate": safe_div(accepted, attempted),
        "max_min_lot_risk_inflation": max_min_lot_risk_inflation,
        **admission_skip_counts,
        **{
            f"binding_capacity_{name}_count": int(value)
            for name, value in binding_capacity_counts.items()
        },
        "closed_trades": closed,
        "active_trading_days": len(active_days),
        "closed_trades_per_active_day": safe_div(closed, max(len(active_days), 1)),
        "accepted_trades_per_active_day": safe_div(accepted, max(len(active_days), 1)),
        "max_open_risk_pct": max_open_risk_pct,
        "max_open_trades": max_open_trades,
        "avg_afml_bet_size_accepted": safe_div(afml_sum, accepted),
        "avg_target_trade_risk_pct": safe_div(target_risk_pct_sum, accepted),
        "avg_accepted_trade_risk_pct": safe_div(actual_risk_pct_sum, accepted),
        "median_target_trade_risk_pct": pctile(target_risk_values, 50),
        "median_actual_trade_risk_pct": pctile(actual_risk_values, 50),
        "p90_actual_trade_risk_pct": pctile(actual_risk_values, 90),
        "max_actual_trade_risk_pct": max_actual_trade_risk_pct,
        "max_target_trade_risk_pct": max_target_trade_risk_pct,
        "median_risk_inflation_accepted": pctile(risk_inflation_values, 50),
        "p90_risk_inflation_accepted": pctile(risk_inflation_values, 90),
        "max_risk_inflation_accepted": max(risk_inflation_values) if risk_inflation_values else np.nan,
        "min_lot_binding_accepted": int(min_lot_binding_count),
        "min_lot_binding_rate_accepted": safe_div(min_lot_binding_count, accepted),
        "median_actual_lot_accepted": pctile(actual_lot_values, 50),
        "p90_actual_lot_accepted": pctile(actual_lot_values, 90),
    }
    for code, asset in code_to_asset.items():
        a = re.sub(r"[^a-z0-9]+", "_", str(asset).lower()).strip("_")
        wins, losses = int(asset_wins[code]), int(asset_losses[code])
        out[f"{a}_closed_trades"] = int(asset_trades[code])
        out[f"{a}_traded"] = bool(asset_trades[code] > 0)
        out[f"{a}_win_rate"] = safe_div(wins, wins + losses)
        out[f"{a}_net_pnl"] = float(asset_net_pnl[code])
    return out



def aggregate_run_results(run_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    group_cols = [
        "scenario_id",
        "foundation_candidate_name",
        "foundation_research_role",
        "return_mode",
        "universe",
        "profile_role",
        "linkage_method",
        "risk_sizing_mode",
        "afml_concurrency_quantile",
        "base_portfolio_risk_budget_pct",
        "base_risk_pct",
        "max_single_trade_risk_pct",
        "max_min_lot_risk_inflation",
        "min_lot_risk_inflation_guard_enabled",
        "overlap_mode",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "max_open_trades_per_edge",
        "max_open_trades_per_asset",
        "account_rule_profile",
        "account_stage",
        "daily_drawdown_name",
        "daily_drawdown_enabled",
        "daily_drawdown_pct",
        "daily_drawdown_mode",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
        "cost_stress_r",
    ]
    missing_group_cols = [c for c in group_cols if c not in run_df.columns]
    if missing_group_cols:
        raise ValueError(
            f"Run results missing foundation aggregation columns: {missing_group_cols}"
        )

    median_diagnostic_columns = [
        "attempted_trades",
        "accepted_trades",
        "skipped_trades",
        "trade_acceptance_rate",
        "skipped_zero_target_risk",
        "skipped_invalid_broker_risk",
        "skipped_min_lot_risk_cap",
        "skipped_overlap_risk_cap",
        "skipped_daily_symbol_risk_cap",
        "skipped_min_lot_inflation_guard",
        "target_reduced_to_cap_count",
        "target_reduced_to_cap_rate_attempted",
        "broker_backoff_applied_count",
        "broker_backoff_applied_rate_attempted",
        "skipped_edge_trade_count",
        "skipped_asset_trade_count",
        "skipped_zero_single_trade_capacity",
        "skipped_zero_total_capacity",
        "skipped_zero_asset_capacity",
        "skipped_zero_family_capacity",
        "skipped_zero_edge_capacity",
        "skipped_zero_daily_symbol_capacity",
        "skipped_min_lot_exceeds_single_trade_capacity",
        "skipped_min_lot_exceeds_total_capacity",
        "skipped_min_lot_exceeds_asset_capacity",
        "skipped_min_lot_exceeds_family_capacity",
        "skipped_min_lot_exceeds_edge_capacity",
        "skipped_min_lot_exceeds_daily_symbol_capacity",
        "skipped_post_rounding_single_trade_cap",
        "skipped_post_rounding_total_cap",
        "skipped_post_rounding_asset_cap",
        "skipped_post_rounding_family_cap",
        "skipped_post_rounding_edge_cap",
        "skipped_post_rounding_daily_symbol_cap",
        "binding_capacity_single_trade_count",
        "binding_capacity_total_count",
        "binding_capacity_asset_count",
        "binding_capacity_family_count",
        "binding_capacity_edge_count",
        "binding_capacity_daily_symbol_count",
    ]

    for keys, g in run_df.groupby(group_cols, dropna=False):
        d = dict(zip(group_cols, keys))
        passed = g["passed"].astype(bool)
        failed = ~passed
        hard_failed = g["hard_failed"].astype(bool)
        end_of_history_fail = g["end_of_history_fail"].astype(bool)
        pass_days = pd.to_numeric(
            g.loc[passed, "trading_days_to_target"],
            errors="coerce",
        ).dropna()
        pass_calendar_days = pd.to_numeric(
            g.loc[passed, "calendar_days_to_target"],
            errors="coerce",
        ).dropna()
        d.update({
            "runs": int(len(g)),
            "pass_rate": float(passed.mean()),
            "fail_rate": float(failed.mean()),
            "hard_fail_rate": float(hard_failed.mean()),
            "max_drawdown_fail_rate": float(
                g["max_drawdown_failed"].astype(bool).mean()
            )
            if "max_drawdown_failed" in g.columns
            else float(hard_failed.mean()),
            "daily_drawdown_fail_rate": float(
                g["daily_drawdown_failed"].astype(bool).mean()
            )
            if "daily_drawdown_failed" in g.columns
            else 0.0,
            "daily_symbol_risk_fail_rate": float(
                g["daily_symbol_risk_failed"].astype(bool).mean()
            )
            if "daily_symbol_risk_failed" in g.columns
            else 0.0,
            "end_of_history_fail_rate": float(end_of_history_fail.mean()),
            "median_trading_days_to_target": float(pass_days.median())
            if len(pass_days)
            else np.nan,
            "mean_trading_days_to_target": float(pass_days.mean())
            if len(pass_days)
            else np.nan,
            "median_calendar_days_to_target": float(
                pass_calendar_days.median()
            )
            if len(pass_calendar_days)
            else np.nan,
            "mean_calendar_days_to_target": float(pass_calendar_days.mean())
            if len(pass_calendar_days)
            else np.nan,
            "p95_trading_days_to_target": float(pass_days.quantile(0.95))
            if len(pass_days)
            else np.nan,
            "p99_trading_days_to_target": float(pass_days.quantile(0.99))
            if len(pass_days)
            else np.nan,
            "worst_trading_days_to_target": float(pass_days.max())
            if len(pass_days)
            else np.nan,
            "p95_calendar_days_to_target": float(pass_calendar_days.quantile(0.95))
            if len(pass_calendar_days)
            else np.nan,
            "p99_calendar_days_to_target": float(pass_calendar_days.quantile(0.99))
            if len(pass_calendar_days)
            else np.nan,
            "prob_pass_under_10_trading_days": float(
                (pass_days <= 10).mean()
            )
            if len(pass_days)
            else 0.0,
            "prob_pass_under_20_trading_days": float(
                (pass_days <= 20).mean()
            )
            if len(pass_days)
            else 0.0,
            "prob_pass_under_30_trading_days": float(
                (pass_days <= 30).mean()
            )
            if len(pass_days)
            else 0.0,
            "prob_pass_under_60_trading_days": float(
                (pass_days <= 60).mean()
            )
            if len(pass_days)
            else 0.0,
            "median_closed_trades": float(g["closed_trades"].median()),
            "median_accepted_trades": float(g["accepted_trades"].median()),
            "median_closed_trades_per_trading_day": float(
                g["closed_trades_per_active_day"].median()
            ),
            "median_observation_calendar_days": float(
                g["observation_calendar_days"].median()
            ),
            "worst_buffer_above_floor": float(
                g["worst_buffer_above_floor"].min()
            ),
            "median_worst_buffer_above_floor": float(
                g["worst_buffer_above_floor"].median()
            ),
            "min_balance_observed": float(g["min_balance"].min()),
            "max_observed_open_risk_pct": float(
                g["max_open_risk_pct"].max()
            ),
            "median_max_open_risk_pct": float(
                g["max_open_risk_pct"].median()
            ),
            "median_avg_afml_bet_size": float(
                g["avg_afml_bet_size_accepted"].median()
            ),
            "median_avg_accepted_trade_risk_pct": float(
                g["avg_accepted_trade_risk_pct"].median()
            ),
            "median_avg_target_trade_risk_pct": float(
                g["avg_target_trade_risk_pct"].median()
            )
            if "avg_target_trade_risk_pct" in g.columns
            else np.nan,
            "median_risk_inflation": float(
                g["median_risk_inflation_accepted"].median()
            )
            if "median_risk_inflation_accepted" in g.columns
            else np.nan,
            "p90_risk_inflation": float(
                g["p90_risk_inflation_accepted"].median()
            )
            if "p90_risk_inflation_accepted" in g.columns
            else np.nan,
            "max_risk_inflation": float(
                g["max_risk_inflation_accepted"].max()
            )
            if "max_risk_inflation_accepted" in g.columns
            else np.nan,
            "median_min_lot_binding_rate": float(
                g["min_lot_binding_rate_accepted"].median()
            )
            if "min_lot_binding_rate_accepted" in g.columns
            else np.nan,
            "max_actual_trade_risk_pct_observed": float(
                g["max_actual_trade_risk_pct"].max()
            )
            if "max_actual_trade_risk_pct" in g.columns
            else np.nan,
            "median_daily_worst_buffer": float(
                g["daily_worst_buffer_above_floor"].median()
            )
            if "daily_worst_buffer_above_floor" in g.columns
            else np.nan,
            "min_daily_worst_buffer": float(
                g["daily_worst_buffer_above_floor"].min()
            )
            if "daily_worst_buffer_above_floor" in g.columns
            else np.nan,
            "max_open_trades_observed": int(g["max_open_trades"].max()),
            "max_daily_symbol_risk_pct_observed": float(
                g["max_daily_symbol_risk_pct_observed"].max()
            )
            if "max_daily_symbol_risk_pct_observed" in g.columns
            else 0.0,
        })
        for col in median_diagnostic_columns:
            if col in g.columns:
                d[f"median_{col}"] = float(
                    pd.to_numeric(g[col], errors="coerce").median()
                )
        rows.append(d)
    return pd.DataFrame(rows)


def robust_sort(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(
        [
            "stress02_pass_rate", "base_pass_rate", "stress02_hard_fail_rate", "base_hard_fail_rate",
            "stress02_daily_drawdown_fail_rate", "base_daily_drawdown_fail_rate",
            "stress02_end_of_history_fail_rate", "base_end_of_history_fail_rate", "base_median_days",
            "base_prob_under_30", "base_prob_under_20", "min_worst_buffer_across_costs", "base_max_open_risk_pct",
            "max_single_trade_risk_pct",
        ],
        ascending=[False, False, True, True, True, True, True, True, True, False, False, False, True, True],
        na_position="last",
    )


def fast_safe_sort(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(
        [
            "base_median_days", "stress02_pass_rate", "base_pass_rate", "stress02_hard_fail_rate", "base_hard_fail_rate",
            "stress02_daily_drawdown_fail_rate", "base_daily_drawdown_fail_rate",
            "stress02_end_of_history_fail_rate", "base_end_of_history_fail_rate", "base_prob_under_30",
            "base_prob_under_20", "min_worst_buffer_across_costs", "base_max_open_risk_pct", "max_single_trade_risk_pct",
        ],
        ascending=[True, False, False, True, True, True, True, True, True, False, False, False, True, True],
        na_position="last",
    )


def build_speed_safety_frontier(ranked: pd.DataFrame) -> pd.DataFrame:
    candidates = ranked.dropna(subset=["base_median_days", "base_pass_rate", "stress02_pass_rate", "base_hard_fail_rate", "stress02_hard_fail_rate"]).copy()
    if candidates.empty:
        return candidates
    rows = []
    records = candidates.to_dict(orient="records")
    for i, row in enumerate(records):
        dominated = False
        for j, other in enumerate(records):
            if i == j:
                continue
            no_worse = (
                other["base_median_days"] <= row["base_median_days"] + EPS and
                other["stress02_pass_rate"] >= row["stress02_pass_rate"] - EPS and
                other["base_pass_rate"] >= row["base_pass_rate"] - EPS and
                other["stress02_hard_fail_rate"] <= row["stress02_hard_fail_rate"] + EPS and
                other["base_hard_fail_rate"] <= row["base_hard_fail_rate"] + EPS and
                other.get("stress02_daily_drawdown_fail_rate", 0.0) <= row.get("stress02_daily_drawdown_fail_rate", 0.0) + EPS and
                other.get("base_daily_drawdown_fail_rate", 0.0) <= row.get("base_daily_drawdown_fail_rate", 0.0) + EPS
            )
            strictly_better = (
                other["base_median_days"] < row["base_median_days"] - EPS or
                other["stress02_pass_rate"] > row["stress02_pass_rate"] + EPS or
                other["base_pass_rate"] > row["base_pass_rate"] + EPS or
                other["stress02_hard_fail_rate"] < row["stress02_hard_fail_rate"] - EPS or
                other["base_hard_fail_rate"] < row["base_hard_fail_rate"] - EPS or
                other.get("stress02_daily_drawdown_fail_rate", 0.0) < row.get("stress02_daily_drawdown_fail_rate", 0.0) - EPS or
                other.get("base_daily_drawdown_fail_rate", 0.0) < row.get("base_daily_drawdown_fail_rate", 0.0) - EPS
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            rows.append(row)
    frontier = pd.DataFrame(rows)
    if frontier.empty:
        return frontier
    frontier = fast_safe_sort(frontier).head(SPEED_SAFETY_FRONTIER_MAX_ROWS).copy()
    frontier["frontier_rank"] = np.arange(1, len(frontier) + 1)
    return frontier.reset_index(drop=True)


def build_stage2_ranking(agg_cost_df: pd.DataFrame) -> pd.DataFrame:
    base = agg_cost_df[agg_cost_df["cost_stress_r"] == 0.0].copy()
    stress = agg_cost_df[agg_cost_df["cost_stress_r"] == 0.02].copy()
    if base.empty:
        raise RuntimeError("Stage 2 ranking requires cost_stress_r=0.00 rows.")

    keep = [
        "scenario_id",
        "foundation_candidate_name",
        "foundation_research_role",
        "return_mode",
        "universe",
        "profile_role",
        "linkage_method",
        "risk_sizing_mode",
        "afml_concurrency_quantile",
        "base_portfolio_risk_budget_pct",
        "base_risk_pct",
        "max_single_trade_risk_pct",
        "max_min_lot_risk_inflation",
        "min_lot_risk_inflation_guard_enabled",
        "overlap_mode",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "max_open_trades_per_edge",
        "max_open_trades_per_asset",
        "account_rule_profile",
        "account_stage",
        "daily_drawdown_name",
        "daily_drawdown_enabled",
        "daily_drawdown_pct",
        "daily_drawdown_mode",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
    ]
    out = base[keep].copy()

    mapping = {
        "pass_rate": "base_pass_rate",
        "fail_rate": "base_fail_rate",
        "hard_fail_rate": "base_hard_fail_rate",
        "max_drawdown_fail_rate": "base_max_drawdown_fail_rate",
        "daily_drawdown_fail_rate": "base_daily_drawdown_fail_rate",
        "daily_symbol_risk_fail_rate": "base_daily_symbol_risk_fail_rate",
        "end_of_history_fail_rate": "base_end_of_history_fail_rate",
        "median_trading_days_to_target": "base_median_days",
        "mean_trading_days_to_target": "base_mean_days",
        "p95_trading_days_to_target": "base_p95_days",
        "p99_trading_days_to_target": "base_p99_days",
        "worst_trading_days_to_target": "base_worst_days",
        "median_calendar_days_to_target": "base_median_calendar_days",
        "p99_calendar_days_to_target": "base_p99_calendar_days",
        "prob_pass_under_10_trading_days": "base_prob_under_10",
        "prob_pass_under_20_trading_days": "base_prob_under_20",
        "prob_pass_under_30_trading_days": "base_prob_under_30",
        "prob_pass_under_60_trading_days": "base_prob_under_60",
        "worst_buffer_above_floor": "base_worst_buffer",
        "median_worst_buffer_above_floor": "base_median_worst_buffer",
        "max_observed_open_risk_pct": "base_max_open_risk_pct",
        "median_max_open_risk_pct": "base_median_max_open_risk_pct",
        "median_closed_trades": "base_median_closed_trades",
        "median_accepted_trades": "base_median_accepted_trades",
        "median_avg_accepted_trade_risk_pct": "base_median_actual_trade_risk_pct",
        "median_avg_target_trade_risk_pct": "base_median_target_trade_risk_pct",
        "max_actual_trade_risk_pct_observed": "base_max_actual_trade_risk_pct",
        "median_min_lot_binding_rate": "base_median_min_lot_binding_rate",
        "median_attempted_trades": "base_median_attempted_trades",
        "median_skipped_trades": "base_median_skipped_trades",
        "median_trade_acceptance_rate": "base_median_trade_acceptance_rate",
        "median_skipped_min_lot_risk_cap": "base_median_skipped_min_lot_risk_cap",
        "median_skipped_overlap_risk_cap": "base_median_skipped_overlap_risk_cap",
        "median_skipped_min_lot_inflation_guard": "base_median_skipped_min_lot_inflation_guard",
        "median_target_reduced_to_cap_count": "base_median_target_reduced_to_cap_count",
        "median_broker_backoff_applied_count": "base_median_broker_backoff_applied_count",
        "median_skipped_edge_trade_count": "base_median_skipped_edge_trade_count",
        "median_skipped_asset_trade_count": "base_median_skipped_asset_trade_count",
        "median_skipped_zero_total_capacity": "base_median_skipped_zero_total_capacity",
        "median_skipped_zero_asset_capacity": "base_median_skipped_zero_asset_capacity",
        "median_skipped_zero_family_capacity": "base_median_skipped_zero_family_capacity",
        "median_skipped_zero_edge_capacity": "base_median_skipped_zero_edge_capacity",
        "median_skipped_min_lot_exceeds_single_trade_capacity": "base_median_skipped_min_lot_exceeds_single_trade_capacity",
        "median_skipped_min_lot_exceeds_total_capacity": "base_median_skipped_min_lot_exceeds_total_capacity",
        "median_skipped_min_lot_exceeds_asset_capacity": "base_median_skipped_min_lot_exceeds_asset_capacity",
        "median_skipped_min_lot_exceeds_family_capacity": "base_median_skipped_min_lot_exceeds_family_capacity",
        "median_skipped_min_lot_exceeds_edge_capacity": "base_median_skipped_min_lot_exceeds_edge_capacity",
        "median_binding_capacity_single_trade_count": "base_median_binding_capacity_single_trade_count",
        "median_binding_capacity_total_count": "base_median_binding_capacity_total_count",
        "median_binding_capacity_asset_count": "base_median_binding_capacity_asset_count",
        "median_binding_capacity_family_count": "base_median_binding_capacity_family_count",
        "median_binding_capacity_edge_count": "base_median_binding_capacity_edge_count",
    }
    for src, dst in mapping.items():
        out[dst] = base[src].values if src in base.columns else np.nan

    stress_mapping = {
        "pass_rate": "stress02_pass_rate",
        "fail_rate": "stress02_fail_rate",
        "hard_fail_rate": "stress02_hard_fail_rate",
        "max_drawdown_fail_rate": "stress02_max_drawdown_fail_rate",
        "daily_drawdown_fail_rate": "stress02_daily_drawdown_fail_rate",
        "daily_symbol_risk_fail_rate": "stress02_daily_symbol_risk_fail_rate",
        "end_of_history_fail_rate": "stress02_end_of_history_fail_rate",
        "median_trading_days_to_target": "stress02_median_days",
        "p99_trading_days_to_target": "stress02_p99_days",
        "prob_pass_under_30_trading_days": "stress02_prob_under_30",
        "worst_buffer_above_floor": "stress02_worst_buffer",
        "median_worst_buffer_above_floor": "stress02_median_worst_buffer",
        "median_trade_acceptance_rate": "stress02_median_trade_acceptance_rate",
        "median_skipped_min_lot_risk_cap": "stress02_median_skipped_min_lot_risk_cap",
        "median_skipped_overlap_risk_cap": "stress02_median_skipped_overlap_risk_cap",
        "median_skipped_min_lot_inflation_guard": "stress02_median_skipped_min_lot_inflation_guard",
        "median_skipped_edge_trade_count": "stress02_median_skipped_edge_trade_count",
        "median_skipped_asset_trade_count": "stress02_median_skipped_asset_trade_count",
    }
    if not stress.empty:
        stress_keep = ["scenario_id"] + [
            src for src in stress_mapping if src in stress.columns
        ]
        s = stress[stress_keep].rename(columns=stress_mapping)
        out = out.merge(s, on="scenario_id", how="left")
    else:
        for dst in stress_mapping.values():
            out[dst] = np.nan

    out["min_pass_rate_across_costs"] = out[
        ["base_pass_rate", "stress02_pass_rate"]
    ].min(axis=1)
    out["max_fail_rate_across_costs"] = out[
        ["base_fail_rate", "stress02_fail_rate"]
    ].max(axis=1)
    out["max_hard_fail_rate_across_costs"] = out[
        ["base_hard_fail_rate", "stress02_hard_fail_rate"]
    ].max(axis=1)
    out["max_daily_drawdown_fail_rate_across_costs"] = out[
        [
            "base_daily_drawdown_fail_rate",
            "stress02_daily_drawdown_fail_rate",
        ]
    ].max(axis=1)
    out["max_daily_symbol_risk_fail_rate_across_costs"] = out[
        [
            "base_daily_symbol_risk_fail_rate",
            "stress02_daily_symbol_risk_fail_rate",
        ]
    ].max(axis=1)
    out["min_worst_buffer_across_costs"] = out[
        ["base_worst_buffer", "stress02_worst_buffer"]
    ].min(axis=1)

    robust_ranked = robust_sort(out).copy()
    robust_ranked["robust_rank"] = np.arange(1, len(robust_ranked) + 1)
    out = out.merge(
        robust_ranked[["scenario_id", "robust_rank"]],
        on="scenario_id",
        how="left",
    )

    fast_safe_mask = (
        (out["base_pass_rate"] >= FAST_SAFE_BASE_PASS_MIN)
        & (out["stress02_pass_rate"] >= FAST_SAFE_STRESS02_PASS_MIN)
        & (out["base_hard_fail_rate"] <= FAST_SAFE_BASE_HARD_FAIL_MAX)
        & (
            out["stress02_hard_fail_rate"]
            <= FAST_SAFE_STRESS02_HARD_FAIL_MAX
        )
    )
    out["fast_safe_eligible"] = fast_safe_mask
    out["fast_safe_rank"] = np.nan
    if fast_safe_mask.any():
        fast_ranked = fast_safe_sort(out[fast_safe_mask].copy())
        fast_ranked["fast_safe_rank"] = np.arange(
            1,
            len(fast_ranked) + 1,
        )
        out = out.drop(columns=["fast_safe_rank"]).merge(
            fast_ranked[["scenario_id", "fast_safe_rank"]],
            on="scenario_id",
            how="left",
        )

    fast_frontier_mask = (
        (out["base_pass_rate"] >= FAST_FRONTIER_BASE_PASS_MIN)
        & (out["stress02_pass_rate"] >= FAST_FRONTIER_STRESS02_PASS_MIN)
        & (out["base_hard_fail_rate"] <= FAST_FRONTIER_BASE_HARD_FAIL_MAX)
        & (
            out["stress02_hard_fail_rate"]
            <= FAST_FRONTIER_STRESS02_HARD_FAIL_MAX
        )
    )
    out["fast_frontier_eligible"] = fast_frontier_mask
    out["fast_frontier_rank"] = np.nan
    if fast_frontier_mask.any():
        frontier_ranked = fast_safe_sort(
            out[fast_frontier_mask].copy()
        )
        frontier_ranked["fast_frontier_rank"] = np.arange(
            1,
            len(frontier_ranked) + 1,
        )
        out = out.drop(columns=["fast_frontier_rank"]).merge(
            frontier_ranked[["scenario_id", "fast_frontier_rank"]],
            on="scenario_id",
            how="left",
        )

    out["eligible_for_no_deadline_selection"] = out["robust_rank"].notna()
    out["selected_by"] = BEST_SCENARIO_SELECTION_MODE
    return out.sort_values(
        [
            "robust_rank",
            "fast_safe_rank",
            "fast_frontier_rank",
            "base_median_days",
        ],
        na_position="last",
    ).reset_index(drop=True)


_POOL_BASE_ARRAYS: dict[tuple[str, str, str, str], dict[str, Any]] | None = None
_POOL_CODE_TO_ASSET: dict[int, str] | None = None
_POOL_DIMS: tuple[int, int, int] | None = None


def _pool_init(
    base_arrays_by_group: dict[tuple[str, str, str, str], dict[str, Any]],
    code_to_asset: dict[int, str],
    n_assets: int,
    n_families: int,
    n_edges: int,
) -> None:
    """ProcessPoolExecutor initializer: stashes the (large, read-only)
    per-group base return arrays in each worker process ONCE, so per-task
    payloads only need to carry the small (scenario, start-chunk) pieces --
    not the full array dict on every task."""
    global _POOL_BASE_ARRAYS, _POOL_CODE_TO_ASSET, _POOL_DIMS
    _POOL_BASE_ARRAYS = base_arrays_by_group
    _POOL_CODE_TO_ASSET = code_to_asset
    _POOL_DIMS = (n_assets, n_families, n_edges)


def _pool_run_chunk(
    args: tuple[dict[str, Any], tuple[str, str, str, str], list[int], list[float]],
) -> list[dict[str, Any]]:
    """Worker task: run one scenario across one slice of its rolling starts
    (all cost-stress values). Pure function of its arguments plus the
    process-local base arrays stashed by _pool_init -- no shared mutable
    state, so results are identical regardless of how work is chunked or
    which worker executes which chunk (verified against the serial
    baseline before this became the default execution path)."""
    c, key, start_slice, cost_values = args
    assert _POOL_BASE_ARRAYS is not None and _POOL_CODE_TO_ASSET is not None and _POOL_DIMS is not None
    arr = add_direct_risk_array(
        _POOL_BASE_ARRAYS[key], float(c["base_portfolio_risk_budget_pct"]), float(c["max_single_trade_risk_pct"]),
        float(c.get("afml_concurrency_quantile", 0.0) or 0.0),
    )
    n_assets, n_families, n_edges = _POOL_DIMS
    rows = []
    for start_ns in start_slice:
        for cost in cost_values:
            res = simulate_from_start_fast(arr, c, start_ns, cost, n_assets, n_families, n_edges, _POOL_CODE_TO_ASSET)
            rows.append({k: v for k, v in c.items() if k != "overlap_config"} | res)
    return rows


def run_scenarios(
    configs: list[dict[str, Any]],
    base_arrays_by_group: dict[tuple[str, str, str, str], dict[str, Any]],
    starts_by_group: dict[tuple[str, str, str, str], list[int]],
    cost_values: list[float],
    n_assets: int,
    n_families: int,
    n_edges: int,
    code_to_asset: dict[int, str],
    desc: str,
    *,
    n_workers: int = 1,
    chunk_size: int = 25,
) -> pd.DataFrame:
    """Runs every (scenario, rolling-start, cost-stress) simulation.

    n_workers <= 1 runs the original single-process loop (used for small
    jobs and for cross-checking parallel output against serial ground
    truth). n_workers > 1 fans the work out across a ProcessPoolExecutor,
    chunked at the (scenario, start-slice) level -- fine-grained enough
    that the speedup scales with n_workers regardless of how many scenarios
    are in the grid (unlike one-task-per-scenario chunking, which caps
    speedup at len(configs))."""
    total = sum(len(starts_by_group[(c["return_mode"], c["universe"], c["profile_role"], c["linkage_method"])]) * len(cost_values) for c in configs)

    if n_workers <= 1:
        rows = []
        with tqdm(total=total, desc=desc, unit="run") as pbar:
            for c in configs:
                key = (c["return_mode"], c["universe"], c["profile_role"], c["linkage_method"])
                arr = add_direct_risk_array(base_arrays_by_group[key], float(c["base_portfolio_risk_budget_pct"]), float(c["max_single_trade_risk_pct"]), float(c.get("afml_concurrency_quantile", 0.0) or 0.0))
                for start_ns in starts_by_group[key]:
                    for cost in cost_values:
                        res = simulate_from_start_fast(arr, c, start_ns, cost, n_assets, n_families, n_edges, code_to_asset)
                        rows.append({k: v for k, v in c.items() if k != "overlap_config"} | res)
                        pbar.update(1)
        return pd.DataFrame(rows)

    tasks: list[tuple[dict[str, Any], tuple[str, str, str, str], list[int], list[float]]] = []
    for c in configs:
        key = (c["return_mode"], c["universe"], c["profile_role"], c["linkage_method"])
        starts = starts_by_group[key]
        for i in range(0, len(starts), chunk_size):
            tasks.append((c, key, starts[i:i + chunk_size], cost_values))

    rows: list[dict[str, Any]] = []
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_pool_init,
        initargs=(base_arrays_by_group, code_to_asset, n_assets, n_families, n_edges),
    ) as executor:
        futures = [executor.submit(_pool_run_chunk, task) for task in tasks]
        with tqdm(total=total, desc=f"{desc} [{n_workers} workers]", unit="run") as pbar:
            for future in as_completed(futures):
                chunk_rows = future.result()
                rows.extend(chunk_rows)
                pbar.update(len(chunk_rows))
    return pd.DataFrame(rows)


def select_stage2_candidate_ids(stage1_agg: pd.DataFrame) -> list[str]:
    base = stage1_agg[stage1_agg["cost_stress_r"] == 0.0].copy()
    if base.empty:
        raise RuntimeError("Stage 1 aggregation produced no base cost rows.")

    if FOUNDATION_RUN_ALL_CANDIDATES_IN_STAGE2:
        # The investigation list is already intentionally small. Running every
        # candidate on the same 120 starts avoids a noisy 12-start prefilter from
        # accidentally discarding the most useful foundational comparison.
        return base["scenario_id"].astype(str).drop_duplicates().tolist()

    robust_order = base.sort_values(
        [
            "pass_rate",
            "hard_fail_rate",
            "daily_drawdown_fail_rate",
            "end_of_history_fail_rate",
            "median_trading_days_to_target",
            "prob_pass_under_30_trading_days",
            "worst_buffer_above_floor",
            "max_observed_open_risk_pct",
            "max_single_trade_risk_pct",
        ],
        ascending=[
            False,
            True,
            True,
            True,
            True,
            False,
            False,
            True,
            True,
        ],
        na_position="last",
    )["scenario_id"]
    fast_pool = base[
        (base["pass_rate"] >= FAST_SAFE_STAGE1_BASE_PASS_MIN)
        & (base["hard_fail_rate"] <= FAST_SAFE_STAGE1_HARD_FAIL_MAX)
    ].copy()
    fast_order = fast_pool.sort_values(
        [
            "median_trading_days_to_target",
            "pass_rate",
            "hard_fail_rate",
            "daily_drawdown_fail_rate",
            "prob_pass_under_30_trading_days",
            "worst_buffer_above_floor",
            "max_observed_open_risk_pct",
            "max_single_trade_risk_pct",
        ],
        ascending=[
            True,
            False,
            True,
            True,
            False,
            False,
            True,
            True,
        ],
        na_position="last",
    )["scenario_id"]
    robust_n = min(
        STAGE2_ROBUST_CANDIDATE_SHARE,
        STAGE2_TOP_N_CANDIDATES,
    )
    fast_n = max(0, STAGE2_TOP_N_CANDIDATES - robust_n)
    return select_unique_ids(
        robust_order.head(robust_n),
        fast_order.head(fast_n),
        robust_order,
        limit=STAGE2_TOP_N_CANDIDATES,
    )


def select_fast_frontier_candidate(ranked: pd.DataFrame, frontier: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    if not frontier.empty and "fast_frontier_eligible" in frontier.columns:
        pool = frontier[frontier["fast_frontier_eligible"] == True].copy()
        if not pool.empty:
            return fast_safe_sort(pool).head(1).copy(), "speed_safety_frontier"
    pool = ranked[ranked["fast_frontier_eligible"] == True].copy()
    if not pool.empty:
        return fast_safe_sort(pool).head(1).copy(), "ranked_scenarios_fallback"
    return pd.DataFrame(), None


def build(write: bool = True, n_workers: int = 1) -> dict[str, Any]:
    ensure_dirs()
    validate_account_start_state()
    ledger, asset_to_code, family_to_code, edge_to_code = load_ledger(INPUT_LEDGER)
    code_to_asset = {v: k for k, v in asset_to_code.items()}
    weights = load_hrp_weights(INPUT_HRP_WEIGHTS)
    groups = get_weight_groups(weights)
    base_arrays: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    stage1_starts: dict[tuple[str, str, str, str], list[int]] = {}
    stage2_starts: dict[tuple[str, str, str, str], list[int]] = {}
    for g in tqdm(groups, desc="Preparing V2 HRP groups", unit="group"):
        key = (g["return_mode"], g["universe"], g["profile_role"], g["linkage_method"])
        arr = prepare_base_arrays_for_group(g, ledger)
        base_arrays[key] = arr
        stage1_starts[key] = build_rolling_start_times(arr, STAGE1_START_EVERY_N_ACTIVE_DAYS, STAGE1_MAX_ROLLING_STARTS)
        stage2_starts[key] = build_rolling_start_times(arr, STAGE2_START_EVERY_N_ACTIVE_DAYS, STAGE2_MAX_ROLLING_STARTS)
    configs = build_scenario_configs(groups)
    stage1_runs = run_scenarios(configs, base_arrays, stage1_starts, STAGE1_COST_STRESS_R_PER_TRADE, len(asset_to_code), len(family_to_code), len(edge_to_code), code_to_asset, "stage1: no-deadline scenario runs", n_workers=n_workers)
    stage1_agg = aggregate_run_results(stage1_runs)
    top_ids = select_stage2_candidate_ids(stage1_agg)
    stage2_configs = [c for c in configs if c["scenario_id"] in set(top_ids)]
    stage2_runs = run_scenarios(stage2_configs, base_arrays, stage2_starts, STAGE2_COST_STRESS_R_PER_TRADE, len(asset_to_code), len(family_to_code), len(edge_to_code), code_to_asset, "stage2: no-deadline scenario runs", n_workers=n_workers)
    stage2_agg = aggregate_run_results(stage2_runs)
    ranked = build_stage2_ranking(stage2_agg)

    comparison_columns = [
        "robust_rank",
        "fast_safe_rank",
        "fast_frontier_rank",
        "fast_safe_eligible",
        "fast_frontier_eligible",
        "foundation_candidate_name",
        "foundation_research_role",
        "overlap_mode",
        "base_portfolio_risk_budget_pct",
        "max_single_trade_risk_pct",
        "max_min_lot_risk_inflation",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "max_open_trades_per_edge",
        "max_open_trades_per_asset",
        "base_pass_rate",
        "stress02_pass_rate",
        "base_hard_fail_rate",
        "stress02_hard_fail_rate",
        "base_end_of_history_fail_rate",
        "stress02_end_of_history_fail_rate",
        "base_median_days",
        "stress02_median_days",
        "base_prob_under_30",
        "stress02_prob_under_30",
        "base_worst_buffer",
        "stress02_worst_buffer",
        "base_median_trade_acceptance_rate",
        "stress02_median_trade_acceptance_rate",
        "base_median_skipped_edge_trade_count",
        "base_median_skipped_asset_trade_count",
        "base_median_skipped_zero_total_capacity",
        "base_median_skipped_zero_asset_capacity",
        "base_median_skipped_zero_family_capacity",
        "base_median_skipped_zero_edge_capacity",
        "base_median_skipped_min_lot_exceeds_single_trade_capacity",
        "base_median_skipped_min_lot_exceeds_total_capacity",
        "base_median_skipped_min_lot_exceeds_asset_capacity",
        "base_median_skipped_min_lot_exceeds_family_capacity",
        "base_median_skipped_min_lot_exceeds_edge_capacity",
        "base_median_skipped_min_lot_inflation_guard",
        "base_median_target_reduced_to_cap_count",
        "base_median_broker_backoff_applied_count",
        "base_median_min_lot_binding_rate",
        "base_median_actual_trade_risk_pct",
        "base_max_actual_trade_risk_pct",
        "scenario_id",
    ]
    foundation_comparison = ranked[
        [c for c in comparison_columns if c in ranked.columns]
    ].copy()

    robust = ranked.sort_values(["robust_rank", "base_median_days"], na_position="last").head(1).copy()
    if robust.empty:
        raise RuntimeError("No robust ranked scenario was produced.")
    robust_row = robust.iloc[0].to_dict()

    fast_safe = ranked[ranked["fast_safe_eligible"] == True].sort_values(["fast_safe_rank", "base_median_days"], na_position="last").head(1).copy()
    fast_safe_row = fast_safe.iloc[0].to_dict() if not fast_safe.empty else None

    frontier = build_speed_safety_frontier(ranked)
    fast_frontier, fast_frontier_source = select_fast_frontier_candidate(ranked, frontier)
    fast_frontier_row = fast_frontier.iloc[0].to_dict() if not fast_frontier.empty else None

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "rolling_optimizer_status": "PASS",
        "selected_scenario_id": str(robust_row["scenario_id"]),
        "selected_by": BEST_SCENARIO_SELECTION_MODE,
        "robust_scenario_id": str(robust_row["scenario_id"]),
        "fast_safe_scenario_id": str(fast_safe_row["scenario_id"]) if fast_safe_row else None,
        "fast_safe_selected_by": FAST_SAFE_SELECTION_MODE if fast_safe_row else None,
        "fast_frontier_scenario_id": str(fast_frontier_row["scenario_id"]) if fast_frontier_row else None,
        "fast_frontier_selected_by": FAST_FRONTIER_SELECTION_MODE if fast_frontier_row else None,
        "fast_frontier_source": fast_frontier_source,
        "profile_roles": SIMULATE_PROFILE_ROLES,
        "no_deadline_evaluation": NO_DEADLINE_EVALUATION,
        "failure_definition": "FAIL = active-account overall drawdown breach, active daily-drawdown breach, enabled daily-symbol-risk breach, or the configured profit target plus minimum-valid-day requirement not reached before available history ends.",
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "counts": {
            "ledger_rows": int(len(ledger)),
            "hrp_scenario_groups": int(len(groups)),
            "foundation_candidate_specs": int(len(FOUNDATION_SCENARIO_SPECS)),
            "scenario_configs": int(len(configs)),
            "stage2_candidate_ids": int(len(top_ids)),
            "stage1_rows": int(len(stage1_runs)),
            "stage2_rows": int(len(stage2_runs)),
            "ranked_scenarios": int(len(ranked)),
            "fast_safe_eligible_scenarios": int((ranked["fast_safe_eligible"] == True).sum()),
            "fast_frontier_eligible_scenarios": int((ranked["fast_frontier_eligible"] == True).sum()),
            "speed_safety_frontier_rows": int(len(frontier)),
        },
        "selected_metrics": row_metrics(robust_row),
        "fast_safe_metrics": row_metrics(fast_safe_row),
        "fast_frontier_metrics": row_metrics(fast_frontier_row),
        "settings": {
            "initial_balance": PROP_START_BALANCE,
            "current_balance": CURRENT_BALANCE,
            "account_start_trailing_high_watermark": ACCOUNT_START_TRAILING_HIGH_WATERMARK,
            "account_start_trailing_drawdown_floor": ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR,
            "account_start_remaining_balance_buffer": ACCOUNT_START_REMAINING_BALANCE_BUFFER,
            "account_start_profit_to_target": ACCOUNT_START_PROFIT_TO_TARGET,
            "account_start_daily_loss_floor": ACCOUNT_START_DAILY_LOSS_FLOOR,
            "assume_fresh_daily_reset_at_simulation_start": ASSUME_FRESH_DAILY_RESET_AT_SIMULATION_START,
            "target_balance": TARGET_BALANCE,
            "initial_drawdown_floor": INITIAL_TRAILING_DRAWDOWN_FLOOR,
            "trailing_drawdown_stop_floor": TRAILING_DRAWDOWN_STOP_FLOOR,
            "max_overall_loss_pct": MAX_OVERALL_LOSS_PCT,
            "daily_loss_pct": DAILY_LOSS_PCT,
            "daily_loss_amount": DAILY_LOSS_AMOUNT,
            "daily_reset_timezone": DAILY_RESET_TIMEZONE,
            "daily_reset_offset_hours": DAILY_RESET_OFFSET_HOURS,
            "account_rule_profile": ACCOUNT_RULE_PROFILE,
            "account_stage": ACCOUNT_STAGE,
            "active_account_rules": ACCOUNT_RULE_CONTRACT,
            "overall_drawdown_type": ACCOUNT_RULES["overall_drawdown"]["type"],
            "daily_symbol_risk_rule_enabled": bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"]),
            "daily_symbol_risk_enforcement_mode": ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"],
            "max_daily_risk_per_symbol_pct": ACCOUNT_RULES["daily_symbol_risk"].get("max_pct"),
            "max_daily_risk_per_symbol_amount": ACCOUNT_RULES["daily_symbol_risk"].get("max_amount"),
            "internal_operating_risk_per_symbol_pct": ACCOUNT_RULES["daily_symbol_risk"].get("internal_operating_pct"),
            "internal_operating_risk_per_symbol_amount": ACCOUNT_RULES["daily_symbol_risk"].get("internal_operating_amount"),
            "minimum_trading_days": ACCOUNT_RULES["minimum_trading_days"],
            "maximum_risk_per_trade": ACCOUNT_RULES["maximum_risk_per_trade"],
            "floating_loss_guard": ACCOUNT_RULES["floating_loss_guard"],
            "foundation_investigation_mode": FOUNDATION_INVESTIGATION_MODE,
            "foundation_run_all_candidates_in_stage2": FOUNDATION_RUN_ALL_CANDIDATES_IN_STAGE2,
            "foundation_scenario_specs": FOUNDATION_SCENARIO_SPECS,
            "overlap_mode_library": OVERLAP_MODE_LIBRARY,
            "min_lot_risk_inflation_guard_applies_only_when_min_lot_binds": MIN_LOT_RISK_INFLATION_GUARD_APPLIES_ONLY_WHEN_MIN_LOT_BINDS,
            "max_min_lot_risk_inflation_values": MAX_MIN_LOT_RISK_INFLATION_VALUES,
            "base_portfolio_risk_budget_pcts": BASE_PORTFOLIO_RISK_BUDGET_PCTS,
            "max_single_trade_risk_pcts": MAX_SINGLE_TRADE_RISK_PCTS,
            "account_rule_profiles_available": ACCOUNT_RULE_PROFILES,
            "active_account_rule_profiles": ACTIVE_ACCOUNT_RULE_PROFILES,
            "daily_drawdown_configs": DAILY_DRAWDOWN_CONFIGS,
            "broker_volume_rounding_mode": BROKER_VOLUME_ROUNDING_MODE,
            "reduce_trade_to_fit_effective_risk_cap": REDUCE_TRADE_TO_FIT_EFFECTIVE_RISK_CAP,
            "broker_risk_source_policy": BROKER_RISK_SOURCE_POLICY,
            "skip_trade_if_no_executable_lot_fits": SKIP_TRADE_IF_NO_EXECUTABLE_LOT_FITS,
            "ceil_risk_cap_breach_policy": "backoff_to_largest_safe_executable_lot",
            "minimum_lot_fallback_preserved": True,
            "stage2_max_rolling_starts": STAGE2_MAX_ROLLING_STARTS,
            "stage2_top_n_candidates": STAGE2_TOP_N_CANDIDATES,
            "stage2_robust_candidate_share": STAGE2_ROBUST_CANDIDATE_SHARE,
            "min_remaining_observation_days_after_start": MIN_REMAINING_OBSERVATION_DAYS_AFTER_START,
            "cost_stress_values_stage2": STAGE2_COST_STRESS_R_PER_TRADE,
            "fast_safe_gates": {
                "base_pass_rate_min": FAST_SAFE_BASE_PASS_MIN,
                "stress02_pass_rate_min": FAST_SAFE_STRESS02_PASS_MIN,
                "base_hard_fail_rate_max": FAST_SAFE_BASE_HARD_FAIL_MAX,
                "stress02_hard_fail_rate_max": FAST_SAFE_STRESS02_HARD_FAIL_MAX,
            },
            "fast_frontier_gates": {
                "base_pass_rate_min": FAST_FRONTIER_BASE_PASS_MIN,
                "stress02_pass_rate_min": FAST_FRONTIER_STRESS02_PASS_MIN,
                "base_hard_fail_rate_max": FAST_FRONTIER_BASE_HARD_FAIL_MAX,
                "stress02_hard_fail_rate_max": FAST_FRONTIER_STRESS02_HARD_FAIL_MAX,
            },
            "robust_ranking_order": [
                "stress02_pass_rate DESC", "base_pass_rate DESC", "stress02_hard_fail_rate ASC", "base_hard_fail_rate ASC",
                "stress02_daily_drawdown_fail_rate ASC", "base_daily_drawdown_fail_rate ASC",
                "stress02_end_of_history_fail_rate ASC", "base_end_of_history_fail_rate ASC", "base_median_days ASC",
                "base_prob_under_30 DESC", "base_prob_under_20 DESC", "min_worst_buffer_across_costs DESC",
                "base_max_open_risk_pct ASC", "max_single_trade_risk_pct ASC",
            ],
            "fast_safe_ranking_order": [
                "base_median_days ASC", "stress02_pass_rate DESC", "base_pass_rate DESC", "stress02_hard_fail_rate ASC",
                "base_hard_fail_rate ASC", "stress02_daily_drawdown_fail_rate ASC", "base_daily_drawdown_fail_rate ASC",
                "stress02_end_of_history_fail_rate ASC", "base_end_of_history_fail_rate ASC",
                "base_prob_under_30 DESC", "base_prob_under_20 DESC", "min_worst_buffer_across_costs DESC",
                "base_max_open_risk_pct ASC", "max_single_trade_risk_pct ASC",
            ],
            "fast_frontier_ranking_order": [
                "base_median_days ASC", "stress02_pass_rate DESC", "base_pass_rate DESC", "stress02_hard_fail_rate ASC",
                "base_hard_fail_rate ASC", "stress02_daily_drawdown_fail_rate ASC", "base_daily_drawdown_fail_rate ASC",
                "stress02_end_of_history_fail_rate ASC", "base_end_of_history_fail_rate ASC",
                "base_prob_under_30 DESC", "base_prob_under_20 DESC", "min_worst_buffer_across_costs DESC",
                "base_max_open_risk_pct ASC", "max_single_trade_risk_pct ASC",
            ],
        },
        "outputs": {
            "stage1_run_results": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/stage1_run_results.csv",
            "stage1_aggregate": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/stage1_aggregate.csv",
            "stage2_run_results": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/stage2_run_results.csv",
            "stage2_aggregate": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/stage2_aggregate.csv",
            "ranked_scenarios": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/ranked_scenarios.csv",
            "best_robust_scenario": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/best_robust_scenario.csv",
            "best_fast_safe_scenario": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/best_fast_safe_scenario.csv",
            "best_fast_frontier_scenario": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/best_fast_frontier_scenario.csv",
            "speed_safety_frontier": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/speed_safety_frontier.csv",
            "foundation_candidate_comparison": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/foundation_candidate_comparison.csv",
            "manifest": "portfolio_allocator/outputs/prop_challenge_rolling_optimizer_outputs/prop_challenge_rolling_optimizer_manifest.json",
        },
    }
    if write:
        stage1_runs.to_csv(OUTPUT_DIR / "stage1_run_results.csv", index=False)
        stage1_agg.to_csv(OUTPUT_DIR / "stage1_aggregate.csv", index=False)
        stage2_runs.to_csv(OUTPUT_DIR / "stage2_run_results.csv", index=False)
        stage2_agg.to_csv(OUTPUT_DIR / "stage2_aggregate.csv", index=False)
        ranked.to_csv(OUTPUT_DIR / "ranked_scenarios.csv", index=False)
        robust.to_csv(OUTPUT_DIR / "best_robust_scenario.csv", index=False)
        fast_safe.to_csv(OUTPUT_DIR / "best_fast_safe_scenario.csv", index=False)
        fast_frontier.to_csv(OUTPUT_DIR / "best_fast_frontier_scenario.csv", index=False)
        frontier.to_csv(OUTPUT_DIR / "speed_safety_frontier.csv", index=False)
        foundation_comparison.to_csv(
            OUTPUT_DIR / "foundation_candidate_comparison.csv",
            index=False,
        )
        write_json(OUTPUT_DIR / "prop_challenge_rolling_optimizer_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 1) - 1),
        help="ProcessPoolExecutor workers for the rolling-start simulation grid (default: cpu_count-1). Pass 1 to force the original serial path.",
    )
    args = parser.parse_args()
    manifest = build(write=not args.dry_run, n_workers=int(args.workers))
    print("V2 Phase B2.08 no-deadline rolling-start optimizer")
    print(f"Status: {manifest['rolling_optimizer_status']}")
    print(
        "Account start: "
        f"balance=${CURRENT_BALANCE:,.2f}, "
        f"high-water=${ACCOUNT_START_TRAILING_HIGH_WATERMARK:,.2f}, "
        f"overall floor=${ACCOUNT_START_TRAILING_DRAWDOWN_FLOOR:,.2f}, "
        f"buffer=${ACCOUNT_START_REMAINING_BALANCE_BUFFER:,.2f}, "
        f"fresh-day floor=${ACCOUNT_START_DAILY_LOSS_FLOOR:,.2f}, "
        f"profit to target=${ACCOUNT_START_PROFIT_TO_TARGET:,.2f}"
    )
    c = manifest["counts"]
    print(f"Ledger rows: {c['ledger_rows']}")
    print(f"HRP scenario groups: {c['hrp_scenario_groups']}")
    print(f"Foundation candidate specs: {c['foundation_candidate_specs']}")
    print(f"Scenario configs: {c['scenario_configs']}")
    print(f"Stage 2 candidate IDs: {c['stage2_candidate_ids']}")
    print(f"Stage 1 rows: {c['stage1_rows']}")
    print(f"Stage 2 rows: {c['stage2_rows']}")
    print(f"Ranked scenarios: {c['ranked_scenarios']}")
    print(f"Fast-safe eligible scenarios: {c['fast_safe_eligible_scenarios']}")
    print(f"Fast-frontier eligible scenarios: {c['fast_frontier_eligible_scenarios']}")
    print(f"Speed-safety frontier rows: {c['speed_safety_frontier_rows']}")
    print(f"Robust scenario: {manifest['robust_scenario_id']}")
    m = manifest.get("selected_metrics", {})
    print(
        "Robust foundation candidate: "
        f"{m.get('foundation_candidate_name')} "
        f"(role={m.get('foundation_research_role')}, "
        f"overlap={m.get('overlap_mode')}, "
        f"total cap={float(m.get('max_total_open_risk_pct', float('nan'))):.2%}, "
        f"trade cap={float(m.get('base_max_single_trade_risk_pct', float('nan'))):.2%}, "
        f"min-lot inflation guard={m.get('max_min_lot_risk_inflation')})"
    )
    print(f"Robust base pass/fail/hard/end: {m.get('base_pass_rate'):.2%} / {m.get('base_fail_rate'):.2%} / {m.get('base_hard_fail_rate'):.2%} / {m.get('base_end_of_history_fail_rate'):.2%}")
    print(f"Robust stress02 pass/fail/hard/end: {m.get('stress02_pass_rate'):.2%} / {m.get('stress02_fail_rate'):.2%} / {m.get('stress02_hard_fail_rate'):.2%} / {m.get('stress02_end_of_history_fail_rate'):.2%}")
    print(f"Robust median/p95/p99/worst trading days: {m.get('base_median_days')} / {m.get('base_p95_days')} / {m.get('base_p99_days')} / {m.get('base_worst_days')}")
    print(f"Robust stress02 p99 trading days: {m.get('stress02_p99_days')}")
    fm = manifest.get("fast_safe_metrics", {}) or {}
    if manifest.get("fast_safe_scenario_id"):
        print(f"Fast-safe scenario: {manifest['fast_safe_scenario_id']}")
        print(f"Fast-safe base pass/fail/hard/end: {fm.get('base_pass_rate'):.2%} / {fm.get('base_fail_rate'):.2%} / {fm.get('base_hard_fail_rate'):.2%} / {fm.get('base_end_of_history_fail_rate'):.2%}")
        print(f"Fast-safe stress02 pass/fail/hard/end: {fm.get('stress02_pass_rate'):.2%} / {fm.get('stress02_fail_rate'):.2%} / {fm.get('stress02_hard_fail_rate'):.2%} / {fm.get('stress02_end_of_history_fail_rate'):.2%}")
        print(f"Fast-safe median/p95/p99 trading days: {fm.get('base_median_days')} / {fm.get('base_p95_days')} / {fm.get('base_p99_days')}")
    else:
        print("Fast-safe scenario: NONE PASSED GATES")
    ffm = manifest.get("fast_frontier_metrics", {}) or {}
    if manifest.get("fast_frontier_scenario_id"):
        print(f"Fast-frontier scenario: {manifest['fast_frontier_scenario_id']}")
        print(f"Fast-frontier source: {manifest.get('fast_frontier_source')}")
        print(f"Fast-frontier base pass/fail/hard/end: {ffm.get('base_pass_rate'):.2%} / {ffm.get('base_fail_rate'):.2%} / {ffm.get('base_hard_fail_rate'):.2%} / {ffm.get('base_end_of_history_fail_rate'):.2%}")
        print(f"Fast-frontier stress02 pass/fail/hard/end: {ffm.get('stress02_pass_rate'):.2%} / {ffm.get('stress02_fail_rate'):.2%} / {ffm.get('stress02_hard_fail_rate'):.2%} / {ffm.get('stress02_end_of_history_fail_rate'):.2%}")
        print(f"Fast-frontier median/p95/p99 trading days: {ffm.get('base_median_days')} / {ffm.get('base_p95_days')} / {ffm.get('base_p99_days')}")
    else:
        print("Fast-frontier scenario: NONE PASSED GATES")
    print(f"Profiles: {', '.join(SIMULATE_PROFILE_ROLES)}")
    print("Foundation mode: targeted explicit candidates; no Cartesian risk-grid expansion")
    print(f"Base-budget values tested: {', '.join(pct_str(x) for x in BASE_PORTFOLIO_RISK_BUDGET_PCTS)}")
    print(f"Max single-trade risk values tested: {', '.join(pct_str(x) for x in MAX_SINGLE_TRADE_RISK_PCTS)}")
    print(f"Active account rule profiles: {', '.join(ACTIVE_ACCOUNT_RULE_PROFILES)}")
    print(f"Daily drawdown configs: {', '.join(str(x.get('name')) for x in DAILY_DRAWDOWN_CONFIGS)}")
    print(f"Account profile/stage: {ACCOUNT_RULE_PROFILE} / {ACCOUNT_STAGE}")
    print(
        "Account rules: "
        f"overall={ACCOUNT_RULES['overall_drawdown']['type']} "
        f"daily={ACCOUNT_RULES['daily_drawdown']['mode']} "
        f"minimum_days={MINIMUM_TRADING_DAYS_REQUIRED if MINIMUM_TRADING_DAYS_ENABLED else 0}"
    )
    print("Broker risk policy: requested risk is reduced to the largest broker-valid lot inside every active cap; only zero capacity or an oversized minimum lot is skipped")
    print("Deadline mode: none; no timeout bucket; no-target-by-end-of-history counts as fail")
    print(f"Output dir: {OUTPUT_DIR.relative_to(V2_ROOT)}")


if __name__ == "__main__":
    main()
