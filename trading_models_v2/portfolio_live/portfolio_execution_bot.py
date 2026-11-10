from __future__ import annotations

"""
V2 PORTFOLIO EXECUTION BOT
version: 2026-05-20_v2_execution_bot_manifest_signal_consumer

Purpose
-------
Consumes approved V2 execution-ready signals exported by:

    trading_models_v2/portfolio_live/portfolio_live_dashboard_orchestrator.py
    portfolio_live/outputs/live_dashboard/live_approved_signals.csv/jsonl

and converts them into MT5 paper/live orders with portfolio-level risk controls.

SOTD boundaries
---------------
- This bot does NOT build bars.
- This bot does NOT build features.
- This bot does NOT generate candidate events.
- This bot does NOT load models/calibrators or recompute probabilities.
- This bot consumes dashboard-approved runtime score outputs only.
- This bot owns broker checks, sizing, duplicate blocking, risk caps, order_send,
  paper/live audit logs, and position metadata.

Live arming
-----------
Real MT5 order_send is fail-closed unless ALL are true:
    settings.PORTFOLIO_EXECUTION_MODE == "live"
    settings.ENABLE_ORDER_SEND is True
    settings.LIVE_MODE == "REAL_LIVE"
    settings.ALLOW_REAL_LIVE_TRADING_EXPORT is True
    live_manifest.deployment_status == "LIVE_APPROVED"
    live_manifest.live_ready is True
    live_manifest.live_trade_enabled is True
    live_manifest.stress_validation_status == "STRESS_PASS"
    live_bar_policy.order_execution_allowed_by_policy is True

Otherwise, approved signals are paper-audited only.
"""

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable, Mapping
from types import SimpleNamespace

import pandas as pd

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for _p in (LIVE_DIR, V2_ROOT, REPO_ROOT):
    _s = str(_p)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)

import live_settings as settings  # type: ignore  # noqa: E402
from shared.prop_account_rules import (  # noqa: E402
    active_rule_contract,
    daily_breach_value,
    daily_drawdown_amount,
    daily_drawdown_floor,
    load_active_account_rules,
    overall_breach_value,
    overall_drawdown_floor,
    overall_high_watermark_value,
    structurally_equal,
    target_satisfied,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.live_payoff_contract import (  # noqa: E402
    LIVE_PAYOFF_CONTRACT_VERSION,
    validate_live_payoff_edge,
    verify_frozen_live_afml,
)

# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1
from common import (  # type: ignore  # noqa: E402
    ensure_dirs,
    init_mt5,
    load_live_manifest,
    read_json,
    setup_logger,
    shutdown_mt5,
    write_json,
)
from broker_clock_contract import (  # type: ignore  # noqa: E402
    BrokerClockContractError,
    CLOCK_CONTRACT_INVALID,
    CLOCK_CONTRACT_RETRY,
    GENUINE_FUTURE_TICK,
    STALE_TICK,
    classify_contract_error,
    normalise_broker_timestamp,
    raw_tick_timestamp,
    read_contract as read_broker_clock_contract,
    validate_contract as validate_broker_clock_contract,
)
from production_observability import RuntimeMetricsSampler, install_production_log_filter  # noqa: E402
from production_release_identity import build_release_identity, recover_stale_temp_files  # noqa: E402
from production_state_integrity import atomic_write_state, load_state_with_recovery  # noqa: E402
from portfolio_closed_trade_ledger import reconcile_closed_trades  # noqa: E402
from executor_runtime_io import (  # noqa: E402
    AsyncNotificationWorker,
    ExecutorCriticalStateStore,
    RecentJsonlSignalFeed,
)

try:  # Telegram remains optional.
    import requests
except Exception:  # pragma: no cover
    requests = None


BOT_VERSION = "2026-08-20_v2_execution_phase_e_targeted_reconcile+async_telegram_v1+opposite_direction_guard_v1+allocator_frozen_live_payoff_v1"
BAR_SEQUENCE_CONTRACT_VERSION = "v2_executable_bar_sequence_v1"
BAR_TIME_CONTRACT_VERSION = "v2_bar_time_v1_datetime_identity_closed_at_observation"
DASHBOARD_BAR_ACK_SCHEMA_VERSION = "v2_dashboard_bar_ack_v2_sequence"
VERTICAL_SEQUENCE_MIGRATION_RETRY_SECONDS = max(
    float(getattr(settings, "PORTFOLIO_VERTICAL_SEQUENCE_MIGRATION_RETRY_SECONDS", 5.0)),
    1.0,
)
SIGNAL_SCHEMA = "v2_dashboard_approved_signal"
# Dashboard v2.1 writes schema_version="v2.1" while the execution-bot
# contract name remains v2_dashboard_approved_signal. Accept both explicit
# versions without weakening any downstream validation gates.
ACCEPTED_SIGNAL_SCHEMA_VERSIONS = {SIGNAL_SCHEMA, "v2.1", "v2.3"}
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"
PROBABILITY_COLUMN = "calibrated_probability"

DASHBOARD_OUTPUT_DIR = settings.OUTPUT_DIR / "live_dashboard"
APPROVED_SIGNALS_CSV = DASHBOARD_OUTPUT_DIR / "live_approved_signals.csv"
APPROVED_SIGNALS_JSONL = DASHBOARD_OUTPUT_DIR / "live_approved_signals.jsonl"
DASHBOARD_STATUS_JSON = DASHBOARD_OUTPUT_DIR / "latest_dashboard_status.json"
DASHBOARD_BAR_ACK_JSON = DASHBOARD_OUTPUT_DIR / "latest_dashboard_bar_ack.json"
BROKER_CLOCK_CONTRACT_JSON = DASHBOARD_OUTPUT_DIR / "account_broker_clock_contract.json"
PENDING_LIVE_BAR_DIR = settings.LIVE_STATE_DIR / "pending_live_bars"

BOT_OUTPUT_DIR = settings.OUTPUT_DIR / "portfolio_execution_bot"
EXECUTION_AUDIT_CSV = BOT_OUTPUT_DIR / "portfolio_execution_audit.csv"
EXECUTED_TRADES_CSV = BOT_OUTPUT_DIR / "portfolio_executed_trades.csv"
REJECTED_SIGNALS_CSV = BOT_OUTPUT_DIR / "portfolio_rejected_signals.csv"
LATEST_STATUS_JSON = BOT_OUTPUT_DIR / "latest_execution_bot_status.json"
EXECUTION_AUDIT_JSONL = BOT_OUTPUT_DIR / "portfolio_execution_audit.jsonl"
EXECUTED_TRADES_JSONL = BOT_OUTPUT_DIR / "portfolio_executed_trades.jsonl"
REJECTED_SIGNALS_JSONL = BOT_OUTPUT_DIR / "portfolio_rejected_signals.jsonl"
CLOSED_TRADES_CSV = BOT_OUTPUT_DIR / "portfolio_closed_trades.csv"
CLOSED_TRADES_JSONL = BOT_OUTPUT_DIR / "portfolio_closed_trades.jsonl"
RELEASE_IDENTITY_DIR = settings.OUTPUT_DIR / "release_identity"

STATE_PATH = settings.LIVE_STATE_DIR / "portfolio_execution_bot_state.json"
STATE_BACKUP_PATH = STATE_PATH.with_suffix(STATE_PATH.suffix + ".last_good")
STATE_CORRUPT_ARCHIVE_DIR = settings.LIVE_STATE_DIR / "corrupt_state_archives"
LOG_PATH = settings.LIVE_LOGS_DIR / "portfolio_execution_bot.log"
LIVE_BAR_POLICY_PATH = settings.LIVE_CONFIG_DIR / "live_bar_policy.json"

EXECUTION_MODE = str(getattr(settings, "PORTFOLIO_EXECUTION_MODE", "paper")).lower().strip()
ENABLE_ORDER_SEND = bool(getattr(settings, "ENABLE_ORDER_SEND", False))
POLL_SECONDS = float(getattr(settings, "PORTFOLIO_EXECUTION_POLL_SECONDS", 1.0))
HEARTBEAT_SECONDS = float(getattr(settings, "PORTFOLIO_EXECUTION_HEARTBEAT_SECONDS", 5.0))
MAX_SIGNAL_AGE_SECONDS = int(getattr(settings, "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS", 120))
FUTURE_SIGNAL_TOLERANCE_SECONDS = float(getattr(settings, "PORTFOLIO_EXECUTION_FUTURE_SIGNAL_TOLERANCE_SECONDS", 5.0))
CLOCK_CONTRACT_MAX_AGE_SECONDS = float(
    getattr(settings, "PORTFOLIO_BROKER_CLOCK_CONTRACT_MAX_AGE_SECONDS", 900.0)
)
MAGIC_NUMBER = int(getattr(settings, "PORTFOLIO_MAGIC_NUMBER", 260512))

INCLUDE_UNMANAGED_POSITIONS_IN_RISK = bool(getattr(settings, "PORTFOLIO_INCLUDE_UNMANAGED_POSITIONS_IN_RISK", True))
BLOCK_IF_UNKNOWN_OPEN_RISK = bool(getattr(settings, "PORTFOLIO_BLOCK_IF_UNKNOWN_OPEN_RISK", True))
ENFORCE_BROKER_MIN_STOP_LEVEL = bool(getattr(settings, "PORTFOLIO_ENFORCE_BROKER_MIN_STOP_LEVEL", True))
ALLOW_WIDEN_TO_BROKER_MIN_STOP = bool(getattr(settings, "PORTFOLIO_ALLOW_WIDEN_TO_BROKER_MIN_STOP", False))
# Optional extra operator reserve above the selected Script 09/10/11 reserve.
# Default zero preserves exact validated-contract parity. A non-zero legacy
# PORTFOLIO_STATIC_FLOOR_BUFFER_USD remains supported but is an additional,
# deliberately more conservative live-only overlay.
STATIC_FLOOR_BUFFER_USD = max(
    0.0,
    float(getattr(settings, "PORTFOLIO_STATIC_FLOOR_BUFFER_USD", 0.0)),
)
BLOCK_NEW_TRADES_AT_OR_ABOVE_TARGET = bool(getattr(settings, "PORTFOLIO_BLOCK_AT_TARGET_BALANCE", True))
ALLOW_MIN_LOT_FALLBACK = bool(getattr(settings, "PORTFOLIO_ALLOW_MIN_LOT_FALLBACK", True))
INCLUDE_PENDING_ORDERS_IN_RISK = bool(getattr(settings, "PORTFOLIO_INCLUDE_PENDING_ORDERS_IN_RISK", True))
BLOCK_IF_UNKNOWN_PENDING_RISK = bool(getattr(settings, "PORTFOLIO_BLOCK_IF_UNKNOWN_PENDING_RISK", True))
# QUANTARA_OPPOSITE_DIRECTION_SAME_SYMBOL_GUARD_V1
BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL = bool(
    getattr(settings, "PORTFOLIO_BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL", False)
)
REQUIRE_EXPLICIT_EXECUTION_GUARDS = bool(getattr(settings, "PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARDS", True))
INITIAL_EQUITY_HWM_SEED = getattr(settings, "PORTFOLIO_INITIAL_EQUITY_HIGH_WATERMARK", None)
EXPECTED_ACCOUNT_LOGIN = getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_LOGIN", None)
EXPECTED_ACCOUNT_SERVER = str(getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_SERVER", "") or "")
EXPECTED_ACCOUNT_CURRENCY = str(getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_CURRENCY", "") or "").upper()
EXPECTED_STARTING_CAPITAL = getattr(settings, "PORTFOLIO_EXPECTED_STARTING_CAPITAL", None)
VERTICAL_DEFER_LOG_COOLDOWN_SECONDS = max(float(getattr(settings, "PORTFOLIO_VERTICAL_DEFER_LOG_COOLDOWN_SECONDS", 300.0)), 1.0)

# Optional live symbol-risk safety overlay.  The active account-rules JSON is
# authoritative; the stress multiplier adds a conservative floating-risk reserve.
DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER = 1.25
ACCOUNT_RULES = load_active_account_rules()
ACCOUNT_RULE_CONTRACT = active_rule_contract(ACCOUNT_RULES)
EXPECTED_ACCOUNT_PROFILE = str(ACCOUNT_RULES["profile_label"])
EXPECTED_ACCOUNT_STAGE = str(ACCOUNT_RULES["account_stage"]).strip().lower()
EXPECTED_APPROVAL_SCOPE = f"active_account_rules:{EXPECTED_ACCOUNT_PROFILE}:{EXPECTED_ACCOUNT_STAGE}"
EXPECTED_SYMBOL_RULE_ENABLED = bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"])
EXPECTED_SYMBOL_RULE_MODE = str(ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])
POST_FILL_RISK_CHECK_ENABLED = True
POST_FILL_EMERGENCY_REDUCE_ENABLED = True
POST_FILL_POSITION_LOOKUP_RETRIES = 8
POST_FILL_POSITION_LOOKUP_SLEEP_SECONDS = 0.20

MAX_SPREAD_POINTS_BY_ASSET = dict(getattr(settings, "PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET", {}))
ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET = dict(getattr(settings, "PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET", {}))
ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET = dict(getattr(settings, "PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET", {}))
ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET = dict(getattr(settings, "PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET", {}))
DEVIATION_POINTS_BY_ASSET = dict(getattr(settings, "PORTFOLIO_DEVIATION_POINTS_BY_ASSET", {}))
MAX_LOT_CAP_BY_ASSET = dict(getattr(settings, "PORTFOLIO_MAX_LOT_CAP_BY_ASSET", {}))

RESEARCH_COST_BPS_BY_ASSET = dict(getattr(settings, "RESEARCH_COST_BPS_BY_ASSET", {}))
RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET = dict(getattr(settings, "RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET", {}))
RESEARCH_SPREAD_PARITY_WINDOW = int(getattr(settings, "RESEARCH_SPREAD_PARITY_WINDOW", 20))
RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS = int(getattr(settings, "RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS", 5))
RESEARCH_SPREAD_PARITY_DEFAULT_MULTIPLIER = 1.5

TELEGRAM_TOKEN = str(getattr(settings, "TELEGRAM_TOKEN", "") or "")
TELEGRAM_CHAT_IDS = list(getattr(settings, "TELEGRAM_CHAT_IDS", []) or [])
SEND_STARTUP_TELEGRAM = bool(getattr(settings, "PORTFOLIO_SEND_STARTUP_TELEGRAM", False))
SEND_EXECUTION_TELEGRAM = bool(getattr(settings, "PORTFOLIO_SEND_EXECUTION_TELEGRAM", False))
SEND_REJECTION_TELEGRAM = bool(getattr(settings, "PORTFOLIO_SEND_REJECTION_TELEGRAM", False))
TELEGRAM_ASYNC_QUEUE_MAX_SIZE = max(
    int(getattr(settings, "PORTFOLIO_TELEGRAM_ASYNC_QUEUE_MAX_SIZE", 256)),
    8,
)
TELEGRAM_ASYNC_SHUTDOWN_DRAIN_SECONDS = max(
    float(getattr(settings, "PORTFOLIO_TELEGRAM_ASYNC_SHUTDOWN_DRAIN_SECONDS", 20.0)),
    1.0,
)

LIVE_BAR_READ_RETRIES = max(int(getattr(settings, "PORTFOLIO_LIVE_BAR_READ_RETRIES", 4)), 1)
LIVE_BAR_READ_RETRY_SECONDS = max(float(getattr(settings, "PORTFOLIO_LIVE_BAR_READ_RETRY_SECONDS", 0.05)), 0.0)
TRANSIENT_ORDER_RETRY_INITIAL_SECONDS = max(float(getattr(settings, "PORTFOLIO_TRANSIENT_ORDER_RETRY_INITIAL_SECONDS", 2.0)), 0.25)
TRANSIENT_ORDER_RETRY_MAX_SECONDS = max(float(getattr(settings, "PORTFOLIO_TRANSIENT_ORDER_RETRY_MAX_SECONDS", 15.0)), TRANSIENT_ORDER_RETRY_INITIAL_SECONDS)
PLAN_WAIT_RETRY_INITIAL_SECONDS = max(float(getattr(settings, "PORTFOLIO_PLAN_WAIT_RETRY_INITIAL_SECONDS", 1.0)), 0.25)
PLAN_WAIT_RETRY_MAX_SECONDS = max(float(getattr(settings, "PORTFOLIO_PLAN_WAIT_RETRY_MAX_SECONDS", 30.0)), PLAN_WAIT_RETRY_INITIAL_SECONDS)
PLAN_WAIT_AUDIT_HEARTBEAT_SECONDS = max(float(getattr(settings, "PORTFOLIO_PLAN_WAIT_AUDIT_HEARTBEAT_SECONDS", 300.0)), 5.0)
CLOSED_TRADE_RECONCILE_SECONDS = max(float(getattr(settings, "PORTFOLIO_CLOSED_TRADE_RECONCILE_SECONDS", 30.0)), 5.0)
CLOSED_TRADE_HISTORY_OVERLAP_DAYS = max(int(getattr(settings, "PORTFOLIO_CLOSED_TRADE_HISTORY_OVERLAP_DAYS", 7)), 1)
CLOSED_TRADE_HISTORY_LOOKBACK_DAYS = max(int(getattr(settings, "PORTFOLIO_CLOSED_TRADE_HISTORY_LOOKBACK_DAYS", 120)), 1)
CLOSED_TRADE_FULL_AUDIT_SECONDS = max(
    float(getattr(settings, "PORTFOLIO_CLOSED_TRADE_FULL_AUDIT_SECONDS", 21600.0)),
    300.0,
)
MARKET_CLOSED_COOLDOWN_SECONDS = max(float(getattr(settings, "PORTFOLIO_MARKET_CLOSED_COOLDOWN_SECONDS", 900.0)), 30.0)
MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET = dict(getattr(settings, "PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET", {}))
ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET = dict(getattr(settings, "PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET", {}))
WEEKEND_ENTRY_GUARD_BY_ASSET = dict(getattr(settings, "PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET", {}))
# Compatibility/diagnostic constant only.
# production_release_identity remains useful forensic/observability metadata,
# but Git cleanliness or release identity can never arm/disarm real orders.
REQUIRE_IMMUTABLE_GIT_RELEASE = False
PRODUCTION_VERBOSE_LOGGING_ENABLED = bool(getattr(settings, "LIVE_PRODUCTION_VERBOSE_LOGGING_ENABLED", False))
PRODUCTION_LOG_REPEAT_INTERVAL_SECONDS = float(getattr(settings, "LIVE_PRODUCTION_LOG_REPEAT_INTERVAL_SECONDS", 300.0))

# Vertical-barrier exits remain bar-count based and anchored to the model event
# time. These guards only verify that counted bars are canonical, dashboard-
# acknowledged UTC live bars and that the broker position has experienced at
# least one genuinely new bar after its fill. Any ambiguity defers the exit;
# broker-side SL/TP protection remains active.
VERTICAL_BARRIER_REQUIRE_DASHBOARD_ACK = bool(
    getattr(settings, "PORTFOLIO_VERTICAL_BARRIER_REQUIRE_DASHBOARD_ACK", True)
)
VERTICAL_BARRIER_FUTURE_TOLERANCE_SECONDS = max(
    float(
        getattr(
            settings,
            "PORTFOLIO_VERTICAL_BARRIER_FUTURE_TOLERANCE_SECONDS",
            FUTURE_SIGNAL_TOLERANCE_SECONDS,
        )
    ),
    0.0,
)
VERTICAL_BARRIER_REQUIRE_POST_FILL_BAR = bool(
    getattr(settings, "PORTFOLIO_VERTICAL_BARRIER_REQUIRE_POST_FILL_BAR", True)
)
VERTICAL_BARRIER_STATUS_READ_RETRIES = max(
    int(getattr(settings, "PORTFOLIO_VERTICAL_BARRIER_STATUS_READ_RETRIES", 3)),
    1,
)
VERTICAL_BARRIER_ACK_CATCHUP_RETRIES = max(
    int(getattr(settings, "PORTFOLIO_VERTICAL_BARRIER_ACK_CATCHUP_RETRIES", 8)),
    1,
)
VERTICAL_BARRIER_ACK_CATCHUP_SLEEP_SECONDS = max(
    float(getattr(settings, "PORTFOLIO_VERTICAL_BARRIER_ACK_CATCHUP_SLEEP_SECONDS", 0.15)),
    0.01,
)

# Daily-drawdown runtime overrides are optional. By default the signed allocator
# contract supplies the percentage, while the dashboard's locked broker-time
# offset supplies the server-day rollover boundary.
DAILY_DRAWDOWN_PCT_OVERRIDE = getattr(settings, "PORTFOLIO_DAILY_DRAWDOWN_PCT", None)
DAILY_RESET_OFFSET_HOURS_OVERRIDE = getattr(settings, "PORTFOLIO_DAILY_RESET_OFFSET_HOURS", None)
RISK_CONTRACT_DERIVATION_VERSION = "v5_hashless_generic_active_account_rules"
HASH_VALIDATION_ENABLED = bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False))

EXECUTOR_FULL_STATE_PERSIST_SECONDS = max(
    float(getattr(settings, "PORTFOLIO_EXECUTION_FULL_STATE_PERSIST_SECONDS", 60.0)),
    15.0,
)
EXECUTOR_SIGNAL_BOOTSTRAP_MAX_BYTES = max(
    int(getattr(settings, "PORTFOLIO_EXECUTION_SIGNAL_BOOTSTRAP_MAX_BYTES", 64 * 1024 * 1024)),
    1024 * 1024,
)
EXECUTOR_CRITICAL_STATE_PATH = (
    settings.LIVE_STATE_DIR / "portfolio_execution_bot_critical_state.json"
)
EXECUTOR_CRITICAL_STATE_BACKUP_PATH = EXECUTOR_CRITICAL_STATE_PATH.with_suffix(
    EXECUTOR_CRITICAL_STATE_PATH.suffix + ".last_good"
)

_SIGNAL_FEED = RecentJsonlSignalFeed(
    jsonl_path=APPROVED_SIGNALS_JSONL,
    csv_fallback_path=APPROVED_SIGNALS_CSV,
    max_age_seconds=MAX_SIGNAL_AGE_SECONDS,
    bootstrap_max_bytes=EXECUTOR_SIGNAL_BOOTSTRAP_MAX_BYTES,
)
_CRITICAL_STATE_STORE = ExecutorCriticalStateStore(
    path=EXECUTOR_CRITICAL_STATE_PATH,
    backup_path=EXECUTOR_CRITICAL_STATE_BACKUP_PATH,
    atomic_writer=atomic_write_state,
)
_LAST_FULL_STATE_SAVE_MONOTONIC = 0.0
_TELEGRAM_WORKER = None


def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """Preserve existing logging behavior while bounding live log growth."""
    ensure_dirs()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)sZ | %(levelname)s | %(message)s")
    fmt.converter = time.gmtime
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = max(int(getattr(settings, "LIVE_LOG_ROTATION_MAX_BYTES", 25_000_000)), 1_000_000)
        backup_count = max(int(getattr(settings, "LIVE_LOG_ROTATION_BACKUP_COUNT", 8)), 1)
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    install_production_log_filter(
        logger,
        verbose=PRODUCTION_VERBOSE_LOGGING_ENABLED,
        repeat_interval_seconds=PRODUCTION_LOG_REPEAT_INTERVAL_SECONDS,
    )
    return logger


@dataclass
class CandidateSizing:
    base_portfolio_risk_budget_pct: float
    portfolio_weight: float
    afml_bet_size: float
    throttle_multiplier: float
    target_lock_multiplier: float
    target_risk_pct_before_cap: float
    target_risk_pct: float
    target_risk_usd: float
    max_single_risk_usd: float
    remaining_total_cap_usd: float
    remaining_asset_cap_usd: float
    remaining_family_cap_usd: float
    remaining_edge_cap_usd: float
    decommission_allocation: float = 1.0


@dataclass
class OrderPlan:
    signal_id: str
    edge_id: str
    asset: str
    symbol: str
    family: str
    side_name: str
    side_value: int
    order_type_name: str
    order_type: int
    entry_price: float
    signal_price: float
    stop_loss: float
    take_profit: float
    sl_distance: float
    tp_distance: float
    # Original model/allocator target after AFML/HRP/throttle/max-single logic.
    model_target_risk_pct: float
    model_target_risk_usd: float
    # Final target used for broker volume sizing after remaining total/asset/family/edge caps.
    target_risk_pct: float
    target_risk_usd: float
    actual_risk_usd: float
    actual_risk_pct: float
    volume: float
    used_min_lot_fallback: bool
    max_min_lot_risk_inflation: float | None
    observed_min_lot_risk_inflation: float | None
    spread_points: float
    entry_drift_price: float
    deviation_points: int
    magic: int
    comment: str
    type_filling: int
    geometry_hash: str | None
    pt_multiplier: float
    sl_multiplier: float
    reward_risk_ratio: float
    vertical_horizon_bars: int | None
    calendar_time: bool
    vertical_horizon_hours: float | None
    risk_contract_hash: str | None
    trailing_floor: float | None
    actual_trailing_buffer_before: float | None
    # Backward-compatible alias for projected buffer after existing positions hit SL.
    remaining_trailing_buffer_before: float | None
    projected_trailing_buffer_before: float | None
    projected_trailing_buffer_after: float | None
    required_trailing_buffer_reserve: float | None
    # Backward-compatible alias for projected remaining capacity before entry.
    daily_loss_remaining_before: float | None
    actual_daily_loss_remaining_before: float | None
    projected_daily_loss_remaining_before: float | None
    projected_daily_equity_proxy: float | None
    symbol_closed_losses_usd: float | None
    projected_symbol_risk_usd: float | None
    symbol_risk_stress_multiplier: float | None
    projected_stressed_symbol_risk_usd: float | None
    live_internal_symbol_risk_amount: float | None
    legal_symbol_risk_amount: float | None
    execution_plan_hash: str | None
    live_manifest_hash: str | None
    edge_manifest_hash: str | None
    bar_policy_hash: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def ensure_runtime_dirs() -> None:
    ensure_dirs()
    BOT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None:
            return default
        if isinstance(x, str) and x.strip().lower() in {"", "nan", "none", "null"}:
            return default
        val = float(x)
        if math.isnan(val) or math.isinf(val):
            return default
        return val
    except Exception:
        return default


def safe_int(x: Any, default: int | None = None) -> int | None:
    try:
        if x is None:
            return default
        if isinstance(x, str) and x.strip().lower() in {"", "nan", "none", "null"}:
            return default
        return int(float(x))
    except Exception:
        return default


def safe_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return default
    try:
        if pd.isna(x):
            return default
    except Exception:
        pass
    s = str(x).strip().lower()
    if s in {"true", "1", "yes", "y"}:
        return True
    if s in {"false", "0", "no", "n"}:
        return False
    return default


def ensure_utc_timestamp(ts: Any) -> pd.Timestamp | None:
    try:
        stamp = pd.Timestamp(ts)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return stamp
    except Exception:
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_or_none(path: Path) -> str | None:
    return sha256_file(path) if path.exists() else None


def read_json_object(path: Path) -> dict[str, Any]:
    return read_json(path)



_APPEND_ONLY_OUTPUT_LOCK = threading.RLock()
_APPEND_ONLY_HEADERS: dict[str, list[str]] = {}
_APPEND_ONLY_DEDUP_KEYS: dict[str, set[str]] = {}
_RUN_APPEND_ROW_COUNTS: dict[str, int] = {}
_RUN_APPEND_SIGNAL_IDS: dict[str, set[str]] = {}


def _append_only_jsonl_path(path: Path) -> Path:
    if path == EXECUTION_AUDIT_CSV:
        return EXECUTION_AUDIT_JSONL
    if path == EXECUTED_TRADES_CSV:
        return EXECUTED_TRADES_JSONL
    if path == REJECTED_SIGNALS_CSV:
        return REJECTED_SIGNALS_JSONL
    if path == CLOSED_TRADES_CSV:
        return CLOSED_TRADES_JSONL
    return path.with_suffix(".jsonl")


def _dedup_key(row: dict[str, Any], dedup_cols: list[str] | None) -> str | None:
    if not dedup_cols:
        return None
    values = [str(row.get(column, "")) for column in dedup_cols]
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def _load_dedup_keys(path: Path, dedup_cols: list[str] | None) -> set[str]:
    cache_key = f"{path.resolve()}|{','.join(dedup_cols or [])}"
    cached = _APPEND_ONLY_DEDUP_KEYS.get(cache_key)
    if cached is not None:
        return cached
    keys: set[str] = set()
    jsonl_path = _append_only_jsonl_path(path)
    if jsonl_path.exists() and dedup_cols:
        try:
            with jsonl_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        key = _dedup_key(payload, dedup_cols)
                        if key:
                            keys.add(key)
        except Exception:
            pass
    # One-time migration compatibility: older deployments may have only CSV.
    # This scan occurs once per process/path and never on each append.
    if not keys and path.exists() and path.stat().st_size > 0 and dedup_cols:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for payload in csv.DictReader(handle):
                    key = _dedup_key(dict(payload), dedup_cols)
                    if key:
                        keys.add(key)
        except Exception:
            pass
    _APPEND_ONLY_DEDUP_KEYS[cache_key] = keys
    return keys


def _csv_header(path: Path, rows: list[dict[str, Any]]) -> list[str]:
    key = str(path.resolve())
    cached = _APPEND_ONLY_HEADERS.get(key)
    if cached:
        return cached
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle))
            if header:
                _APPEND_ONLY_HEADERS[key] = header
                return header
        except Exception:
            pass
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    if "extra_json" not in columns:
        columns.append("extra_json")
    _APPEND_ONLY_HEADERS[key] = columns
    return columns


def append_rows_csv(path: Path, rows: list[dict[str, Any]], dedup_cols: list[str] | None = None) -> None:
    """Append-only operational writer.

    Full schema-evolving records are authoritative in JSONL. The CSV remains a
    stable append-only convenience view; fields introduced after its first header
    are preserved inside ``extra_json`` instead of forcing a whole-file rewrite.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_ONLY_OUTPUT_LOCK:
        dedup = _load_dedup_keys(path, dedup_cols)
        accepted: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            key = _dedup_key(row, dedup_cols)
            if key and key in dedup:
                continue
            accepted.append(row)
            if key:
                dedup.add(key)
        if not accepted:
            return
        append_jsonl(_append_only_jsonl_path(path), accepted)
        columns = _csv_header(path, accepted)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for row in accepted:
                extras = {key: value for key, value in row.items() if key not in columns}
                projected = {column: row.get(column) for column in columns}
                if "extra_json" in columns:
                    projected["extra_json"] = json.dumps(extras, default=str, sort_keys=True) if extras else None
                writer.writerow(projected)
        output_key = str(path.resolve())
        _RUN_APPEND_ROW_COUNTS[output_key] = (
            int(_RUN_APPEND_ROW_COUNTS.get(output_key, 0)) + len(accepted)
        )
        signal_ids = _RUN_APPEND_SIGNAL_IDS.setdefault(output_key, set())
        signal_ids.update(
            str(row.get("signal_id") or "").strip()
            for row in accepted
            if str(row.get("signal_id") or "").strip()
        )


def execution_output_parity(state: dict[str, Any]) -> dict[str, Any]:
    """Reconcile current-run terminal state with append-only audit outputs."""
    audit_key = str(EXECUTION_AUDIT_CSV.resolve())
    rejected_key = str(REJECTED_SIGNALS_CSV.resolve())
    audit_ids = set(_RUN_APPEND_SIGNAL_IDS.get(audit_key, set()))
    rejected_ids = set(_RUN_APPEND_SIGNAL_IDS.get(rejected_key, set()))
    run_seen = {
        str(value)
        for value in (state.get("run_seen_candidate_signal_ids") or [])
        if str(value)
    }
    terminals = state.get("terminal_signal_ids") or {}
    run_terminal_ids = run_seen.intersection(str(value) for value in terminals)
    negative_statuses = {
        "blocked",
        "stale",
        "order_rejected",
        "market_session_closed",
    }
    negative_terminal_ids = {
        signal_id
        for signal_id in run_terminal_ids
        if str((terminals.get(signal_id) or {}).get("status") or "")
        in negative_statuses
    }
    missing_audit = sorted(negative_terminal_ids - audit_ids)
    missing_rejected = sorted(negative_terminal_ids - rejected_ids)
    rejected_without_audit = sorted(rejected_ids - audit_ids)
    return {
        "scope": "current_process_run",
        "audit_rows_appended": int(_RUN_APPEND_ROW_COUNTS.get(audit_key, 0)),
        "rejected_rows_appended": int(_RUN_APPEND_ROW_COUNTS.get(rejected_key, 0)),
        "audit_signal_count": len(audit_ids),
        "rejected_signal_count": len(rejected_ids),
        "run_seen_candidate_count": len(run_seen),
        "run_terminal_candidate_count": len(run_terminal_ids),
        "run_negative_terminal_count": len(negative_terminal_ids),
        "missing_audit_signal_ids": missing_audit,
        "missing_rejected_signal_ids": missing_rejected,
        "rejected_without_audit_signal_ids": rejected_without_audit,
        "parity_passed": not (
            missing_audit or missing_rejected or rejected_without_audit
        ),
    }


def append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str, sort_keys=True) + "\n")


def get_retcode_name(mt5: Any, retcode: Any) -> str:
    try:
        retcode_int = int(retcode)
    except Exception:
        return ""
    for name in dir(mt5):
        if name.startswith("TRADE_RETCODE_") and isinstance(getattr(mt5, name), int):
            if getattr(mt5, name) == retcode_int:
                return name
    return ""


def _mask_telegram_chat_id(chat_id: Any) -> str:
    text = str(chat_id)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def telegram_config_snapshot() -> dict[str, Any]:
    return {
        "token_present": bool(TELEGRAM_TOKEN),
        "token_length": len(TELEGRAM_TOKEN),
        "chat_ids_count": len(TELEGRAM_CHAT_IDS),
        "masked_chat_ids": [_mask_telegram_chat_id(chat_id) for chat_id in TELEGRAM_CHAT_IDS],
        "requests_available": requests is not None,
        "send_startup_enabled": SEND_STARTUP_TELEGRAM,
        "send_execution_enabled": SEND_EXECUTION_TELEGRAM,
        "send_rejection_enabled": SEND_REJECTION_TELEGRAM,
    }


def redact_sensitive_text(value: Any) -> str:
    """Remove Telegram credentials from exception/HTTP diagnostics."""
    text = str(value or "")
    if TELEGRAM_TOKEN:
        text = text.replace(TELEGRAM_TOKEN, "<redacted-telegram-token>")
    return re.sub(
        r"https://api\.telegram\.org/bot[^/\s]+",
        "https://api.telegram.org/bot<redacted>",
        text,
        flags=re.IGNORECASE,
    )


def send_telegram(message: str) -> dict[str, str]:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS or requests is None:
        return {}
    out: dict[str, str] = {}
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=8)
            out[str(chat_id)] = (
                "ok"
                if resp.ok
                else f"HTTP {resp.status_code}: {redact_sensitive_text(resp.text)[:200]}"
            )
        except Exception as exc:
            out[str(chat_id)] = f"EXC: {redact_sensitive_text(exc)}"
    return out


def _store_telegram_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    state["last_telegram_send"] = record
    state.setdefault("telegram_send_history", []).append(record)
    state["telegram_send_history"] = state["telegram_send_history"][-50:]


def _record_telegram_delivery_result(
    logger: Any,
    state: dict[str, Any],
    *,
    purpose: str,
    message_length: int,
    result: Any,
    async_error: str | None = None,
    submitted_at: str | None = None,
    completed_at: str | None = None,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    config = telegram_config_snapshot()
    raw_result = result if isinstance(result, dict) else {}
    if async_error:
        record = {
            "created_at": completed_at or utc_now(),
            "submitted_at": submitted_at,
            "purpose": str(purpose),
            "status": "failed",
            "reason": "async_worker_sender_exception",
            "message_length": int(message_length),
            "elapsed_ms": elapsed_ms,
            **config,
            "result": {},
            "worker_error": redact_sensitive_text(async_error),
        }
        logger.warning("TELEGRAM_SEND_RESULT purpose=%s status=failed worker_error=%s", purpose, record["worker_error"])
        _store_telegram_record(state, record)
        return record

    if not raw_result:
        reasons = []
        if not TELEGRAM_TOKEN:
            reasons.append("missing_telegram_token")
        if not TELEGRAM_CHAT_IDS:
            reasons.append("missing_telegram_chat_ids")
        if requests is None:
            reasons.append("requests_not_available")
        if not reasons:
            reasons.append("send_returned_empty_result")
        record = {
            "created_at": completed_at or utc_now(),
            "submitted_at": submitted_at,
            "purpose": str(purpose),
            "status": "skipped",
            "reason": ";".join(reasons),
            "message_length": int(message_length),
            "elapsed_ms": elapsed_ms,
            **config,
            "result": {},
        }
        logger.warning("TELEGRAM_SEND_SKIPPED purpose=%s reason=%s", purpose, record["reason"])
        _store_telegram_record(state, record)
        return record

    masked_result = {_mask_telegram_chat_id(chat_id): status for chat_id, status in raw_result.items()}
    failed = {chat_id: status for chat_id, status in masked_result.items() if status != "ok"}
    status = "ok" if not failed else "partial_or_failed"
    record = {
        "created_at": completed_at or utc_now(),
        "submitted_at": submitted_at,
        "purpose": str(purpose),
        "status": status,
        "reason": "ok" if status == "ok" else "one_or_more_telegram_sends_failed",
        "message_length": int(message_length),
        "elapsed_ms": elapsed_ms,
        **config,
        "result": masked_result,
        "failed": failed,
    }
    if failed:
        logger.warning("TELEGRAM_SEND_RESULT purpose=%s status=%s failed=%s result=%s", purpose, status, failed, masked_result)
    else:
        logger.info("TELEGRAM_SEND_RESULT purpose=%s status=ok chat_ids_count=%s masked_chat_ids=%s elapsed_ms=%s", purpose, config["chat_ids_count"], config["masked_chat_ids"], elapsed_ms)
    _store_telegram_record(state, record)
    return record


def send_telegram_logged(logger: Any, state: dict[str, Any], purpose: str, message: str) -> dict[str, str]:
    """Queue Telegram without blocking the live execution thread."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS or requests is None:
        _record_telegram_delivery_result(logger, state, purpose=purpose, message_length=len(str(message)), result={})
        return {}

    worker = _TELEGRAM_WORKER
    if worker is not None:
        submitted = worker.submit(purpose=str(purpose), message=str(message))
        if submitted.get("queued") is True:
            state["last_telegram_enqueue"] = {
                "created_at": utc_now(),
                "purpose": str(purpose),
                "status": "queued",
                "message_length": len(str(message)),
            }
            logger.info("TELEGRAM_SEND_QUEUED purpose=%s message_length=%s pending_messages=%s", purpose, len(str(message)), worker.snapshot().get("pending_messages"))
            return {"__async__": "queued"}

        record = {
            "created_at": utc_now(),
            "purpose": str(purpose),
            "status": "queue_rejected",
            "reason": str(submitted.get("reason") or "unknown"),
            "message_length": len(str(message)),
            **telegram_config_snapshot(),
        }
        logger.warning("TELEGRAM_SEND_QUEUE_REJECTED purpose=%s reason=%s", purpose, record["reason"])
        _store_telegram_record(state, record)
        return {"__async__": "queue_rejected"}

    # Offline/import compatibility only. Production main() installs the worker.
    result = send_telegram(message)
    _record_telegram_delivery_result(logger, state, purpose=purpose, message_length=len(str(message)), result=result)
    return result


def drain_telegram_completions(logger: Any, state: dict[str, Any]) -> int:
    worker = _TELEGRAM_WORKER
    if worker is None:
        return 0
    completions = worker.drain_results()
    for completion in completions:
        _record_telegram_delivery_result(
            logger,
            state,
            purpose=str(completion.get("purpose") or ""),
            message_length=int(completion.get("message_length") or 0),
            result=completion.get("result"),
            async_error=completion.get("error"),
            submitted_at=completion.get("submitted_at"),
            completed_at=completion.get("completed_at"),
            elapsed_ms=safe_float(completion.get("elapsed_ms"), None),
        )
    return len(completions)



def _telegram_reason_family(reason: str) -> str:
    """Normalize numeric/repeating reason text for Telegram de-duplication.

    Plan-stage waits such as ``entry_drift_too_large:5.91>4.00`` can change
    every polling loop even though the underlying reason is the same. The full
    reason is still shown in the message/audit rows, but the notification key
    uses this stable family to prevent repeated Telegram spam for one signal.
    """
    text = str(reason or "").strip()
    if not text:
        return "unknown_reason"
    return text.split(":", 1)[0]


def _telegram_issue_key(signal: dict[str, Any], stage: str, status: str, reason: str) -> str:
    """Stable de-dup key for issue notifications.

    Transient plan-stage waits can repeat every polling loop. This key prevents
    Telegram spam while still sending a new alert if the same signal later fails
    for a different stage/reason family.
    """
    sid = str(signal.get("signal_id", "") or "").strip() or "unknown_signal"
    return f"{sid}|{stage}|{status}|{_telegram_reason_family(reason)}"


def send_signal_issue_telegram_once(
    logger: Any,
    state: dict[str, Any],
    *,
    signal: dict[str, Any],
    stage: str,
    status: str,
    reason: str,
    account: Any = None,
    open_risk: dict[str, Any] | None = None,
    live_armed: bool | None = None,
    live_blockers: list[str] | None = None,
) -> None:
    """Notify Telegram once for plan/open-risk/account-stage non-execution.

    This is observability only. It must not change validation, sizing, order
    submission, terminal marking, or retry behavior. Validation-stage blocks and
    broker order rejections keep their existing Telegram paths.
    """
    if not SEND_REJECTION_TELEGRAM:
        return

    key = _telegram_issue_key(signal, stage, status, reason)
    sent_keys = state.setdefault("telegram_signal_issue_notification_keys", [])
    if key in sent_keys:
        return

    sent_keys.append(key)
    state["telegram_signal_issue_notification_keys"] = sent_keys[-1000:]

    icon = "⏳" if status == "transient_wait" else "❌"
    title = "V2 signal waiting" if status == "transient_wait" else "V2 signal blocked"
    asset = signal.get("asset")
    edge_id = signal.get("edge_id")
    side_value = safe_int(signal.get("side_value"), None)
    side_text = "LONG" if side_value == 1 else "SHORT" if side_value == -1 else str(side_value)
    cp = safe_float(signal.get("calibrated_probability"), 0.0) or 0.0
    thr = safe_float(signal.get("final_live_threshold"), 0.0) or 0.0
    signal_price = safe_float(signal.get("signal_price"), None)
    equity = getattr(account, "equity", None) if account is not None else None
    open_risk_total = (open_risk or {}).get("total_open_risk_usd")

    message = (
        f"{icon} {title}\n"
        f"stage={stage} status={status}\n"
        f"{asset} {side_text} | {edge_id}\n"
        f"reason={reason}\n"
        f"p={cp:.4f} thr={thr:.4f}\n"
        f"signal_price={signal_price}\n"
        f"equity={equity} open_risk=${open_risk_total}\n"
        f"live_armed={live_armed}"
    )
    if live_blockers:
        message += f"\nlive_blockers={'; '.join(live_blockers[:3])}"

    send_telegram_logged(logger, state, f"{stage}_{status}", message)


def initial_state() -> dict[str, Any]:
    counter_template = {
        "seen_candidates": 0,
        "candidate_poll_observations": 0,
        "unique_candidates_seen": 0,
        "paper_trades": 0,
        "live_trades": 0,
        "blocked": 0,
        "transient_waits": 0,
        "errors": 0,
        "broker_clock_contract_waits": 0,
        "clock_contract_retries": 0,
        "clock_contract_invalid": 0,
        "genuine_future_ticks": 0,
        "stale_ticks": 0,
    }
    return {
        "created_at": utc_now(),
        "updated_at": None,
        "version": BOT_VERSION,
        "terminal_signal_ids": {},
        # Primary exact map: MT5 position ticket -> full execution metadata.
        "open_position_metadata_by_ticket": {},
        # Secondary maps preserve full edge attribution across MT5 restarts,
        # order/deal ticket differences, and broker-truncated comments.
        "open_position_metadata_by_comment": {},
        "open_position_metadata_by_order": {},
        "open_position_metadata_by_deal": {},
        "open_position_metadata_by_signal_id": {},
        "ambiguous_position_metadata_comments": {},
        "last_heartbeat_at": None,
        "last_manifest_hash": None,
        "last_bar_policy_hash": None,
        "last_telegram_send": None,
        "telegram_send_history": [],
        "telegram_signal_issue_notification_keys": [],
        "prop_risk_state": {},
        "vertical_exit_notification_keys": [],
        "equity_high_watermark_migration": {},
        "preferred_filling_mode_by_symbol": {},
        "transient_order_retry_by_signal": {},
        "transient_plan_retry_by_signal": {},
        "closed_trade_ledger": {},
        "market_closed_cooldown_by_symbol": {},
        "execution_state_integrity_blocked": False,
        "closed_trade_ledger_integrity_blocked": False,
        "seen_candidate_signal_ids": [],
        "run_seen_candidate_signal_ids": [],
        "last_state_integrity_recovery": {},
        "production_release_identity": {},
        "stale_temp_recovery": {},
        "runtime_observability": {},
        "vertical_bar_read_deferred_count_by_asset": {},
        "vertical_barrier_last_progress_by_ticket": {},
        "vertical_barrier_last_ack_sequence_by_asset": {},
        "vertical_barrier_sequence_startup_migration": {},
        "vertical_barrier_deferred_history": [],
        "vertical_barrier_last_defer_log_by_ticket": {},
        "unresolved_position_metadata_by_ticket": {},
        "position_fill_time_migration_history": [],
        "spread_observations_by_asset": {},
        "counters": dict(counter_template),
        "run_id": None,
        "run_started_at": None,
        "run_counters": dict(counter_template),
        "last_broker_clock_diagnostic": {},
    }


def increment_counter(state: dict[str, Any], key: str, amount: int = 1) -> None:
    """Maintain distinct lifetime and current-process counters."""
    for bucket in ("counters", "run_counters"):
        values = state.setdefault(bucket, {})
        values[key] = int(values.get(key, 0) or 0) + int(amount)

def load_state() -> dict[str, Any]:
    state, _state_created, recovery = load_state_with_recovery(
        STATE_PATH,
        fresh_state_factory=initial_state,
        backup_path=STATE_BACKUP_PATH,
        corrupt_dir=STATE_CORRUPT_ARCHIVE_DIR,
        logger=logging.getLogger("portfolio_execution_bot"),
    )
    state["last_state_integrity_recovery"] = recovery
    critical_recovery = _CRITICAL_STATE_STORE.restore_into(
        state,
        logger=logging.getLogger("portfolio_execution_bot"),
    )
    state["executor_critical_state_recovery"] = critical_recovery
    if recovery.get("status") == "initialized_fresh_after_corrupt_state":
        # Open-risk discovery still runs and fails closed on unknown positions,
        # but losing terminal signal IDs/metadata is material enough to require
        # explicit operator recovery before new live entries.
        state["execution_state_integrity_blocked"] = True
    state.setdefault("terminal_signal_ids", {})
    state.setdefault("open_position_metadata_by_ticket", {})
    state.setdefault("open_position_metadata_by_comment", {})
    state.setdefault("open_position_metadata_by_order", {})
    state.setdefault("open_position_metadata_by_deal", {})
    state.setdefault("open_position_metadata_by_signal_id", {})
    state.setdefault("ambiguous_position_metadata_comments", {})
    state.setdefault("last_telegram_send", None)
    state.setdefault("telegram_send_history", [])
    state.setdefault("telegram_signal_issue_notification_keys", [])
    state.setdefault("prop_risk_state", {})
    state.setdefault("vertical_exit_notification_keys", [])
    state.setdefault("equity_high_watermark_migration", {})
    state.setdefault("preferred_filling_mode_by_symbol", {})
    state.setdefault("transient_order_retry_by_signal", {})
    state.setdefault("transient_plan_retry_by_signal", {})
    state.setdefault("closed_trade_ledger", {})
    state.setdefault("market_closed_cooldown_by_symbol", {})
    state.setdefault("execution_state_integrity_blocked", False)
    state.setdefault("closed_trade_ledger_integrity_blocked", False)
    state.setdefault("seen_candidate_signal_ids", [])
    state.setdefault("last_state_integrity_recovery", recovery)
    state.setdefault("production_release_identity", {})
    state.setdefault("stale_temp_recovery", {})
    state.setdefault("runtime_observability", {})
    state.setdefault("vertical_bar_read_deferred_count_by_asset", {})
    state.setdefault("vertical_barrier_last_progress_by_ticket", {})
    state.setdefault("vertical_barrier_last_ack_sequence_by_asset", {})
    state.setdefault("vertical_barrier_sequence_startup_migration", {})
    state.setdefault("vertical_barrier_deferred_history", [])
    state.setdefault("vertical_barrier_last_defer_log_by_ticket", {})
    state.setdefault("unresolved_position_metadata_by_ticket", {})
    state.setdefault("position_fill_time_migration_history", [])
    state.setdefault("spread_observations_by_asset", {})
    state.setdefault("counters", {})
    for key in [
        "seen_candidates", "candidate_poll_observations", "unique_candidates_seen",
        "paper_trades", "live_trades", "blocked", "transient_waits", "errors",
        "broker_clock_contract_waits",
        "clock_contract_retries",
        "clock_contract_invalid",
        "genuine_future_ticks",
        "stale_ticks",
        "opposite_direction_blocks",
    ]:
        state["counters"].setdefault(key, 0)
    state["run_id"] = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{os.getpid()}"
    state["run_started_at"] = utc_now()
    state["run_counters"] = {key: 0 for key in state["counters"]}
    state["run_seen_candidate_signal_ids"] = []
    state.setdefault("last_broker_clock_diagnostic", {})

    # Backfill full edge metadata from prior execution audit/output rows. This
    # repairs older open positions whose MT5 comments only contain shortened
    # family text and therefore would otherwise be bucketed as edge_id=UNKNOWN.
    try:
        backfill_position_metadata_from_execution_outputs(state)
        repair_position_fill_time_metadata(state)
    except Exception:
        # State loading must stay fail-open; unresolved positions are later
        # handled by get_open_risk_snapshot()/BLOCK_IF_UNKNOWN_OPEN_RISK.
        pass
    return state

def save_state(state: dict[str, Any], *, force_full: bool = False) -> None:
    """Persist crash-sensitive state on change; throttle the large compatibility snapshot."""
    global _LAST_FULL_STATE_SAVE_MONOTONIC

    logger = logging.getLogger("portfolio_execution_bot")
    state["updated_at"] = utc_now()
    state["executor_signal_feed"] = _SIGNAL_FEED.snapshot()
    state["last_critical_state_save"] = _CRITICAL_STATE_STORE.persist_if_changed(
        state,
        logger=logger,
        force=force_full,
    )

    now = time.monotonic()
    full_due = (
        force_full
        or _LAST_FULL_STATE_SAVE_MONOTONIC <= 0.0
        or now - _LAST_FULL_STATE_SAVE_MONOTONIC >= EXECUTOR_FULL_STATE_PERSIST_SECONDS
    )
    if not full_due:
        return

    state["last_state_integrity_save"] = atomic_write_state(
        STATE_PATH,
        state,
        backup_path=STATE_BACKUP_PATH,
        backup_interval_seconds=300.0,
        logger=logger,
    )
    _LAST_FULL_STATE_SAVE_MONOTONIC = now


def mark_terminal(state: dict[str, Any], signal_id: str, status: str, reason: str, extra: dict[str, Any] | None = None) -> None:
    state.setdefault("terminal_signal_ids", {})[str(signal_id)] = {
        "status": status,
        "reason": reason,
        "timestamp": utc_now(),
        **(extra or {}),
    }


def is_terminal(state: dict[str, Any], signal_id: str) -> bool:
    return str(signal_id) in state.get("terminal_signal_ids", {})


def load_live_bar_policy() -> dict[str, Any]:
    if not LIVE_BAR_POLICY_PATH.exists():
        raise FileNotFoundError(f"Missing live bar policy: {LIVE_BAR_POLICY_PATH}. Run portfolio_live/04_freeze_live_bar_thresholds.py first.")
    payload = read_json_object(LIVE_BAR_POLICY_PATH)
    if payload.get("manifest_type") != "v2_frozen_live_bar_policy":
        raise ValueError(f"Invalid live bar policy manifest_type: {payload.get('manifest_type')}")
    return payload


def selected_edge_lookup(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    edges = manifest.get("selected_edges", [])
    if not isinstance(edges, list) or not edges:
        raise ValueError("live_manifest.selected_edges must be a non-empty list")
    out: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_id = str(edge.get("edge_id", "")).strip()
        if not edge_id:
            raise ValueError("selected edge missing edge_id")
        if edge_id in out:
            raise ValueError(f"duplicate selected edge_id: {edge_id}")
        out[edge_id] = edge
    return out


def universe_symbols(manifest: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for edge in manifest.get("selected_edges", []):
        asset = str(edge.get("asset", "")).strip()
        if asset:
            out[settings.MT5_SYMBOL_MAP.get(asset, asset)] = asset
    return out


def policy_structurally_matches_manifest(manifest: dict[str, Any], policy: dict[str, Any]) -> bool:
    manifest_edges = sorted(str(edge.get("edge_id")) for edge in (manifest.get("selected_edges") or []) if edge.get("edge_id"))
    policy_edges = sorted(str(x) for x in (policy.get("source_selected_edge_ids") or []))
    manifest_assets = sorted({str(edge.get("asset")) for edge in (manifest.get("selected_edges") or []) if edge.get("asset")})
    policy_assets = sorted(str(x) for x in ((policy.get("bar_policies") or {}).keys()))
    return policy.get("source_manifest_created_at") == manifest.get("created_at") and policy_edges == manifest_edges and policy_assets == manifest_assets


def real_order_send_armed(manifest: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    local_stage = str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "")).strip().lower()
    manifest_stage = str(manifest.get("approved_account_stage") or manifest.get("account_stage") or "").strip().lower()
    if EXECUTION_MODE != "live": blockers.append(f"PORTFOLIO_EXECUTION_MODE={EXECUTION_MODE}")
    if not ENABLE_ORDER_SEND: blockers.append("ENABLE_ORDER_SEND=False")
    if str(getattr(settings, "LIVE_MODE", "DRY_RUN_ONLY")) != "REAL_LIVE": blockers.append(f"LIVE_MODE={getattr(settings, 'LIVE_MODE', None)}")
    if not bool(getattr(settings, "ALLOW_REAL_LIVE_TRADING_EXPORT", False)): blockers.append("ALLOW_REAL_LIVE_TRADING_EXPORT=False")
    if manifest.get("deployment_status") != "LIVE_APPROVED": blockers.append(f"manifest_deployment_status={manifest.get('deployment_status')}")
    if manifest.get("live_ready") is not True: blockers.append("manifest.live_ready=False")
    if manifest.get("live_trade_enabled") is not True: blockers.append("manifest.live_trade_enabled=False")
    if manifest.get("stress_validation_status") != "STRESS_PASS": blockers.append(f"stress_validation_status={manifest.get('stress_validation_status')}")
    if manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION: blockers.append("risk_contract_version_mismatch")
    manifest_profile = str(manifest.get("account_rule_profile") or "").strip()
    if manifest_stage != EXPECTED_ACCOUNT_STAGE: blockers.append(f"account_stage_mismatch:manifest={manifest_stage},active_rules={EXPECTED_ACCOUNT_STAGE}")
    if local_stage and local_stage != manifest_stage: blockers.append(f"account_stage_mismatch:local={local_stage},manifest={manifest_stage}")
    if manifest_profile != EXPECTED_ACCOUNT_PROFILE: blockers.append("account_profile_mismatch")
    if not structurally_equal(manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT): blockers.append("active_account_rules_structural_mismatch")
    if str(manifest.get("live_approval_scope") or "") != EXPECTED_APPROVAL_SCOPE: blockers.append("live_approval_scope_mismatch")
    if bool(manifest.get("daily_symbol_risk_rule_enabled", False)) != EXPECTED_SYMBOL_RULE_ENABLED: blockers.append("daily_symbol_risk_enabled_state_mismatch")
    if str(manifest.get("daily_symbol_risk_enforcement_mode") or "") != EXPECTED_SYMBOL_RULE_MODE: blockers.append("daily_symbol_risk_mode_mismatch")
    if HASH_VALIDATION_ENABLED:
        current_manifest_hash = hash_or_none(settings.LIVE_MANIFEST_PATH)
        if policy.get("source_live_manifest_sha256") != current_manifest_hash: blockers.append("bar_policy_source_manifest_hash_mismatch")
    elif not policy_structurally_matches_manifest(manifest, policy):
        blockers.append("bar_policy_structural_manifest_identity_mismatch")
    if policy.get("approved_account_stage") != manifest_stage: blockers.append("bar_policy_account_stage_mismatch")
    if policy.get("live_policy_ready") is not True: blockers.append(f"live_policy_ready={policy.get('live_policy_ready')}")
    if policy.get("order_execution_allowed_by_policy") is not True: blockers.append(f"order_execution_allowed_by_policy={policy.get('order_execution_allowed_by_policy')}")
    return not blockers, blockers


def apply_operational_arming_blockers(
    live_armed: bool,
    blockers: list[str],
    *,
    release_identity: Mapping[str, Any],
    state: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """Add runtime state-integrity blockers after manifest-policy arming.

    Git/release identity is observability only and can never disarm real orders.
    The legacy immutable-release blocker prefix is stripped if encountered in
    inherited blocker state, while genuine execution-state and closed-ledger
    integrity failures remain fail-closed.
    """
    operational_prefixes = (
        "immutable_git_release_required:",
        "execution_state_integrity_operator_recovery_required",
        "closed_trade_ledger_invariant_failed",
    )
    resolved = [
        blocker for blocker in blockers
        if not str(blocker).startswith(operational_prefixes)
    ]
    # Release identity remains recorded in state/logs for operational provenance,
    # but it is deliberately not an execution or order-send authority.
    if bool(state.get("execution_state_integrity_blocked", False)):
        resolved.append("execution_state_integrity_operator_recovery_required")
    if bool(state.get("closed_trade_ledger_integrity_blocked", False)):
        resolved.append("closed_trade_ledger_invariant_failed")
    return bool(live_armed and not resolved), list(dict.fromkeys(resolved))


def validate_live_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("manifest_type") != "v2_allocator_selected_live_manifest":
        raise ValueError(f"Invalid live_manifest manifest_type: {manifest.get('manifest_type')}")
    if manifest.get("threshold_probability_column") != PROBABILITY_COLUMN:
        raise ValueError("live_manifest.threshold_probability_column must be calibrated_probability")
    if manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION:
        raise ValueError(f"live_manifest.risk_contract_version must be {RISK_CONTRACT_VERSION}")
    if manifest.get("live_payoff_contract_version") != LIVE_PAYOFF_CONTRACT_VERSION:
        raise ValueError(
            f"live_manifest.live_payoff_contract_version must be {LIVE_PAYOFF_CONTRACT_VERSION}"
        )
    for selected_edge in manifest.get("selected_edges", []) or []:
        validate_live_payoff_edge(selected_edge)
    manifest_stage = str(manifest.get("approved_account_stage") or manifest.get("account_stage") or "").strip().lower()
    local_stage = str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "") or "").strip().lower()
    manifest_profile = str(manifest.get("account_rule_profile") or "").strip()
    if manifest_stage != EXPECTED_ACCOUNT_STAGE:
        raise ValueError(f"manifest/active-rules account stage mismatch: {manifest_stage!r}!={EXPECTED_ACCOUNT_STAGE!r}")
    if local_stage and manifest_stage != local_stage:
        raise ValueError(f"local/manifest account stage mismatch: {local_stage!r}!={manifest_stage!r}")
    if manifest_profile != EXPECTED_ACCOUNT_PROFILE:
        raise ValueError(f"manifest/active-rules account profile mismatch: {manifest_profile!r}!={EXPECTED_ACCOUNT_PROFILE!r}")
    if not structurally_equal(manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT):
        raise ValueError("live_manifest.active_account_rules differs from active_account_rules.json")
    if str(manifest.get("live_approval_scope") or "") != EXPECTED_APPROVAL_SCOPE:
        raise ValueError("live manifest approval scope mismatch")
    if bool(manifest.get("daily_symbol_risk_rule_enabled", False)) != EXPECTED_SYMBOL_RULE_ENABLED:
        raise ValueError("live manifest daily-symbol-risk enabled state mismatch")
    if str(manifest.get("daily_symbol_risk_enforcement_mode") or "") != EXPECTED_SYMBOL_RULE_MODE:
        raise ValueError("live manifest daily-symbol-risk enforcement mode mismatch")
    if manifest.get("sotd_contracts", {}).get("execution_bot_must_consume_approved_signal_schema_only") is not True:
        raise ValueError("live_manifest SOTD contract does not require approved signal schema consumption")
    if not manifest.get("selected_edges"):
        raise ValueError("live_manifest selected_edges is empty")


def select_symbols(mt5: Any, manifest: dict[str, Any], logger: Any) -> None:
    for symbol, asset in universe_symbols(manifest).items():
        ok = bool(mt5.symbol_select(symbol, True))
        info = mt5.symbol_info(symbol)
        if not ok or info is None:
            raise RuntimeError(f"Failed to select/read MT5 symbol for asset={asset} symbol={symbol}")
        logger.info(
            "SYMBOL_READY asset=%s symbol=%s digits=%s point=%s vol_min=%s vol_step=%s filling_mode=%s",
            asset,
            symbol,
            getattr(info, "digits", None),
            getattr(info, "point", None),
            getattr(info, "volume_min", None),
            getattr(info, "volume_step", None),
            getattr(info, "filling_mode", None),
        )


def read_approved_signal_rows(logger: Any) -> pd.DataFrame:
    # JSONL is the live IPC authority. RecentJsonlSignalFeed performs a metadata-
    # only idle poll and reads only appended bytes when new signals arrive.
    # It deliberately returns the recent live window each poll so the existing
    # transient retry scheduler continues to see deferred non-terminal signals.
    try:
        df = _SIGNAL_FEED.read(logger)
    except Exception as exc:
        logger.warning(
            "SIGNAL_INCREMENTAL_READ_WARN jsonl=%s csv_fallback=%s error=%s:%s",
            APPROVED_SIGNALS_JSONL,
            APPROVED_SIGNALS_CSV,
            type(exc).__name__,
            exc,
        )
        df = pd.DataFrame()
    if df.empty:
        return df
    required = [
        "schema_version", "signal_id", "signal_status", "edge_id", "asset", "mt5_symbol",
        "side_value", "event_time", "calibrated_probability", "afml_bet_size",
        "payoff_context_id", "final_live_threshold", "threshold_probability_column", "approved_signal",
        "portfolio_weight", "risk_contract_version", "account_stage", "approved_account_stage",
        "live_approval_scope", "daily_symbol_risk_rule_enabled",
        "bar_gap_healed", "bar_executable", "bar_sequence_number",
        "bar_time_contract_version", "signal_price", "execution_plan_ready", "execution_plan_json",
    ]
    if HASH_VALIDATION_ENABLED:
        required.extend(["live_manifest_hash", "edge_manifest_hash", "bar_policy_hash", "execution_plan_hash"])
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("SIGNAL_CONTRACT_MISSING_COLUMNS missing=%s", missing)
        return pd.DataFrame()
    df = df.copy()
    df["_event_ts"] = to_datetime_iso(df["event_time"], utc=True, errors="coerce")
    df = df.dropna(subset=["_event_ts"])
    df = df.sort_values(["_event_ts", "signal_id"]).reset_index(drop=True)
    return df


def row_to_signal(row: pd.Series) -> dict[str, Any]:
    d = row.to_dict()
    for col in [
        "approved_signal", "probability_passes_final_threshold", "execution_allowed",
        "live_trade_enabled", "live_policy_ready", "bar_gap_healed", "bar_executable", "execution_plan_ready",
        "daily_symbol_risk_rule_enabled", "calendar_time",
    ]:
        if col in d:
            d[col] = safe_bool(d[col], False)
    for col in ["side_value", "bar_ticks", "bar_threshold", "vertical_horizon_bars", "bar_sequence_number"]:
        if col in d:
            d[col] = safe_int(d[col], None)
    for col in [
        "raw_probability", "calibrated_probability", "afml_bet_size", "final_live_threshold",
        "portfolio_weight", "signal_price", "planned_sl_distance_price", "planned_tp_distance_price",
        "planned_stop_loss", "planned_take_profit", "vertical_horizon_hours",
    ]:
        if col in d:
            d[col] = safe_float(d[col], None)
    return d



def _plan_reason_family(reason: Any) -> str:
    text = str(reason or "unknown").strip()
    for separator in (":", ";", "="):
        if separator in text:
            text = text.split(separator, 1)[0]
    return text or "unknown"


def _plan_retry_delay(attempt: int) -> float:
    schedule = [1.0, 2.0, 5.0, 10.0, 30.0]
    value = schedule[min(max(int(attempt), 1) - 1, len(schedule) - 1)]
    return min(max(value, PLAN_WAIT_RETRY_INITIAL_SECONDS), PLAN_WAIT_RETRY_MAX_SECONDS)


def clear_transient_plan_retry(state: dict[str, Any], signal_id: str) -> None:
    state.setdefault("transient_plan_retry_by_signal", {}).pop(str(signal_id), None)


def record_transient_plan_retry(
    state: dict[str, Any],
    signal: dict[str, Any],
    *,
    stage: str,
    reason: str,
) -> dict[str, Any]:
    signal_id = str(signal.get("signal_id") or "")
    now = utc_now_ts()
    mapping = state.setdefault("transient_plan_retry_by_signal", {})
    previous = mapping.get(signal_id) or {}
    family = _plan_reason_family(reason)
    same_family = str(previous.get("reason_family") or "") == family
    attempt = int(previous.get("attempt_count", 0) or 0) + 1 if same_family else 1
    delay = _plan_retry_delay(attempt)
    event_time = ensure_utc_timestamp(signal.get("event_time")) or now
    expires_at = event_time + pd.Timedelta(seconds=MAX_SIGNAL_AGE_SECONDS)
    last_audit = ensure_utc_timestamp(previous.get("last_audit_at"))
    audit_due = (
        not previous
        or not same_family
        or str(previous.get("last_reason") or "") != str(reason)
        or last_audit is None
        or (now - last_audit).total_seconds() >= PLAN_WAIT_AUDIT_HEARTBEAT_SECONDS
    )
    record = {
        "signal_id": signal_id,
        "stage": str(stage),
        "reason_family": family,
        "last_reason": str(reason),
        "attempt_count": attempt,
        "first_seen_at": previous.get("first_seen_at") or utc_now(),
        "last_checked_at": utc_now(),
        "next_retry_at": (now + pd.Timedelta(seconds=delay)).isoformat(),
        "retry_delay_seconds": delay,
        "expires_at": expires_at.isoformat(),
        "audit_due": bool(audit_due),
        "last_audit_at": utc_now() if audit_due else previous.get("last_audit_at"),
    }
    mapping[signal_id] = record
    return record


def transient_plan_retry_expired(state: dict[str, Any], signal: dict[str, Any]) -> bool:
    record = (state.get("transient_plan_retry_by_signal") or {}).get(str(signal.get("signal_id") or "")) or {}
    expires = ensure_utc_timestamp(record.get("expires_at"))
    return bool(expires is not None and utc_now_ts() >= expires)


def write_transient_wait_if_due(
    *,
    state: dict[str, Any],
    signal: dict[str, Any],
    stage: str,
    reason: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Register one bounded plan-stage wait and terminalise it at expiry.

    The execution loop may revisit a signal for spread, drift, account, prop-risk
    or open-risk reasons.  This helper rate-limits audit output and guarantees
    that every such wait has one explicit stale terminal state rather than an
    unbounded one-second retry.
    """
    retry = record_transient_plan_retry(state, signal, stage=stage, reason=reason)
    now = utc_now_ts()
    expires = ensure_utc_timestamp(retry.get("expires_at"))
    if expires is not None and now >= expires:
        family = _plan_reason_family(reason)
        terminal_reason = f"transient_plan_wait_expired:{stage}:{family}"
        terminal_row = dict(row)
        terminal_row.update({
            "created_at": utc_now(),
            "status": "stale",
            "reason": terminal_reason,
            "plan_retry_json": json.dumps(retry, default=str, sort_keys=True),
        })
        append_rows_csv(EXECUTION_AUDIT_CSV, [terminal_row])
        append_rows_csv(
            REJECTED_SIGNALS_CSV, [terminal_row], dedup_cols=["signal_id", "reason"]
        )
        signal_id = str(signal.get("signal_id") or "")
        mark_terminal(state, signal_id, "stale", terminal_reason)
        clear_transient_plan_retry(state, signal_id)
        clear_transient_order_retry(state, signal_id)
        increment_counter(state, "blocked")
        return {**retry, "expired": True, "terminal_reason": terminal_reason}
    if retry.get("audit_due"):
        row["plan_retry_json"] = json.dumps(retry, default=str, sort_keys=True)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        increment_counter(state, "transient_waits")
    return {**retry, "expired": False}


def candidate_rows(df: pd.DataFrame, state: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if df.empty:
        return out
    for _, row in df.iterrows():
        sig = row_to_signal(row)
        sid = str(sig.get("signal_id", "")).strip()
        if not sid or is_terminal(state, sid):
            continue
        if str(sig.get("signal_status", "")) != "approved_signal":
            continue
        if not safe_bool(sig.get("approved_signal"), False):
            continue
        if safe_bool(sig.get("bar_gap_healed"), False):
            continue
        if not safe_bool(sig.get("bar_executable"), False):
            continue
        if not safe_bool(sig.get("execution_plan_ready"), False):
            continue
        retry = (state.get("transient_order_retry_by_signal") or {}).get(sid) or {}
        plan_retry = (state.get("transient_plan_retry_by_signal") or {}).get(sid) or {}
        retry_times = [
            ensure_utc_timestamp(retry.get("next_retry_at")),
            ensure_utc_timestamp(plan_retry.get("next_retry_at")),
        ]
        future_retries = [value for value in retry_times if value is not None and utc_now_ts() < value]
        if future_retries:
            continue
        out.append(sig)
    return out


def parse_execution_plan(signal: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    raw = signal.get("execution_plan_json")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)) or str(raw).strip() == "":
        return None, "missing_execution_plan_json"
    try:
        if isinstance(raw, str):
            compact = raw
            plan = json.loads(raw)
        else:
            plan = raw
            compact = json.dumps(plan, sort_keys=True, default=str, allow_nan=True)
    except Exception as exc:
        return None, f"invalid_execution_plan_json:{exc}"
    if not isinstance(plan, dict):
        return None, "execution_plan_json_not_object"
    expected_hash = str(signal.get("execution_plan_hash") or "").strip()
    if HASH_VALIDATION_ENABLED:
        actual_hash = hashlib.sha256(compact.encode("utf-8")).hexdigest()
        if not expected_hash:
            return None, "missing_execution_plan_hash"
        if actual_hash != expected_hash:
            return None, "execution_plan_hash_mismatch"
    if safe_bool(plan.get("execution_plan_ready"), safe_bool(signal.get("execution_plan_ready"), False)) is not True:
        return None, f"execution_plan_not_ready:{plan.get('execution_plan_blocker') or signal.get('execution_plan_blocker')}"
    plan["execution_plan_hash"] = expected_hash
    plan["execution_plan_json"] = compact
    return plan, "ok"



def validate_signal_contract(
    signal: dict[str, Any],
    edge_lookup: dict[str, dict[str, Any]],
    current_hashes: dict[str, str | None],
    require_execution_allowed_for_live: bool,
) -> tuple[bool, str, dict[str, Any] | None]:
    sid = str(signal.get("signal_id", "")).strip()
    if not sid:
        return False, "missing_signal_id", None
    schema_version = str(signal.get("schema_version", "")).strip()
    if schema_version not in ACCEPTED_SIGNAL_SCHEMA_VERSIONS:
        return False, f"wrong_schema_version:{signal.get('schema_version')}", None
    if str(signal.get("signal_status", "")) != "approved_signal" or not safe_bool(signal.get("approved_signal"), False):
        return False, "not_approved_signal", None
    if str(signal.get("threshold_probability_column", "calibrated_probability")) != PROBABILITY_COLUMN:
        return False, "threshold_probability_column_not_calibrated_probability", None
    if safe_bool(signal.get("bar_gap_healed"), False):
        return False, "gap_healed_bar_not_executable", None
    if not safe_bool(signal.get("bar_executable"), False):
        return False, "bar_not_executable", None
    sequence = safe_int(signal.get("bar_sequence_number"), None)
    if sequence is None or sequence <= 0:
        return False, "missing_or_invalid_bar_sequence_number", None
    if str(signal.get("bar_time_contract_version") or "") != BAR_TIME_CONTRACT_VERSION:
        return False, "bar_time_contract_version_mismatch", None
    if not safe_bool(signal.get("execution_plan_ready"), False):
        return False, f"execution_plan_not_ready:{signal.get('execution_plan_blocker')}", None
    if require_execution_allowed_for_live and not safe_bool(signal.get("execution_allowed"), False):
        return False, f"execution_not_allowed_by_signal:{signal.get('execution_blocker')}", None

    edge_id = str(signal.get("edge_id", "")).strip()
    if edge_id not in edge_lookup:
        return False, f"edge_not_in_live_manifest:{edge_id}", None
    edge = edge_lookup[edge_id]
    if HASH_VALIDATION_ENABLED:
        if str(signal.get("live_manifest_hash", "")) != str(current_hashes.get("live_manifest_hash")):
            return False, "live_manifest_hash_mismatch", None
        if str(signal.get("bar_policy_hash", "")) != str(current_hashes.get("bar_policy_hash")):
            return False, "bar_policy_hash_mismatch", None
        if str(signal.get("edge_manifest_hash", "")) != str(edge.get("edge_manifest_hash", "")):
            return False, "edge_manifest_hash_mismatch", None
    if str(signal.get("risk_contract_version", "")) != RISK_CONTRACT_VERSION:
        return False, "signal_risk_contract_version_mismatch", None
    manifest_stage = str(edge.get("account_stage") or signal.get("approved_account_stage") or signal.get("account_stage") or "").strip().lower()
    signal_stage = str(signal.get("approved_account_stage") or signal.get("account_stage") or "").strip().lower()
    local_stage = str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "") or "").strip().lower()
    if signal_stage != EXPECTED_ACCOUNT_STAGE or manifest_stage != EXPECTED_ACCOUNT_STAGE:
        return False, "signal_account_stage_mismatch", None
    if local_stage and signal_stage != local_stage:
        return False, "signal_local_account_stage_mismatch", None
    if safe_bool(signal.get("daily_symbol_risk_rule_enabled"), False) != EXPECTED_SYMBOL_RULE_ENABLED:
        return False, "signal_daily_symbol_risk_state_mismatch", None
    cp = safe_float(signal.get("calibrated_probability"), None)
    thr = safe_float(signal.get("final_live_threshold"), None)
    if cp is None or thr is None:
        return False, "missing_probability_or_threshold", None
    manifest_thr = safe_float(edge.get("final_live_threshold"), None)
    if manifest_thr is None or not math.isclose(float(thr), float(manifest_thr), rel_tol=0.0, abs_tol=1e-12):
        return False, f"live_manifest_threshold_mismatch:signal={thr}:manifest={manifest_thr}", None
    if cp + 1e-12 < manifest_thr:
        return False, f"calibrated_probability_below_threshold:{cp:.8f}<{manifest_thr:.8f}", None
    if not safe_bool(signal.get("probability_passes_final_threshold"), True):
        return False, "probability_passes_final_threshold_false", None
    afml = safe_float(signal.get("afml_bet_size"), None)
    if afml is None or afml <= 0:
        return False, f"non_positive_afml_bet_size:{afml}", None
    payoff_context_id = str(signal.get("payoff_context_id", "")).strip()
    if not payoff_context_id:
        return False, "missing_payoff_context_id", None
    try:
        verify_frozen_live_afml(
            float(cp),
            float(afml),
            edge,
            claimed_payoff_context_id=payoff_context_id,
        )
    except Exception as exc:
        return False, f"frozen_live_afml_verification_failed:{type(exc).__name__}:{exc}", None
    side_value = safe_int(signal.get("side_value"), None)
    if side_value not in {-1, 1}:
        return False, f"invalid_side_value:{side_value}", None
    event_ts = ensure_utc_timestamp(signal.get("event_time"))
    if event_ts is None:
        return False, "invalid_event_time", None
    age = (utc_now_ts() - event_ts).total_seconds()
    if age < -FUTURE_SIGNAL_TOLERANCE_SECONDS:
        return False, f"future_event_time:{age:.1f}s", None
    if age > MAX_SIGNAL_AGE_SECONDS:
        return False, f"stale_signal:{age:.1f}s>{MAX_SIGNAL_AGE_SECONDS}s", None

    exec_plan, plan_reason = parse_execution_plan(signal)
    if exec_plan is None:
        return False, plan_reason, None

    # Execution-plan identity and edge-specific geometry are fail-closed.
    if str(exec_plan.get("edge_id", "")) != edge_id:
        return False, "execution_plan_edge_id_mismatch", None
    if str(exec_plan.get("asset", "")) != str(edge.get("asset", "")):
        return False, "execution_plan_asset_mismatch", None
    if safe_int(exec_plan.get("side_value"), None) != side_value:
        return False, "execution_plan_side_mismatch", None
    if HASH_VALIDATION_ENABLED and str(exec_plan.get("edge_manifest_hash", "")) != str(edge.get("edge_manifest_hash", "")):
        return False, "execution_plan_edge_manifest_hash_mismatch", None

    geometry = edge.get("trade_geometry") if isinstance(edge.get("trade_geometry"), dict) else edge
    if HASH_VALIDATION_ENABLED:
        expected_geometry_hash = str(geometry.get("geometry_hash") or edge.get("geometry_hash") or "").strip()
        if not expected_geometry_hash:
            return False, "live_manifest_missing_geometry_hash", None
        if str(exec_plan.get("geometry_hash") or "") != expected_geometry_hash:
            return False, "execution_plan_geometry_hash_mismatch", None
    expected_pt = safe_float(geometry.get("pt_multiplier"), None)
    expected_sl = safe_float(geometry.get("sl_multiplier"), None)
    expected_vh = safe_int(geometry.get("vertical_horizon_bars"), None)
    expected_calendar_time = bool(geometry.get("calendar_time", False))
    expected_vh_hours = safe_float(geometry.get("vertical_horizon_hours"), None)
    plan_pt = safe_float(exec_plan.get("pt_multiplier"), None)
    plan_sl = safe_float(exec_plan.get("sl_multiplier"), None)
    plan_vh = safe_int(exec_plan.get("vertical_horizon_bars"), None)
    plan_calendar_time = bool(exec_plan.get("calendar_time", False))
    plan_vh_hours = safe_float(exec_plan.get("vertical_horizon_hours"), None)
    if expected_pt is None or expected_sl is None or expected_vh is None:
        return False, "live_manifest_incomplete_trade_geometry", None
    if expected_calendar_time and (expected_vh_hours is None or expected_vh_hours <= 0):
        return False, "live_manifest_incomplete_calendar_time_trade_geometry", None
    if plan_pt is None or not math.isclose(plan_pt, expected_pt, rel_tol=0.0, abs_tol=1e-10):
        return False, "execution_plan_pt_multiplier_mismatch", None
    if plan_sl is None or not math.isclose(plan_sl, expected_sl, rel_tol=0.0, abs_tol=1e-10):
        return False, "execution_plan_sl_multiplier_mismatch", None
    if plan_vh != expected_vh:
        return False, "execution_plan_vertical_horizon_mismatch", None
    if plan_calendar_time != expected_calendar_time:
        return False, "execution_plan_calendar_time_mismatch", None
    if expected_calendar_time and (
        plan_vh_hours is None
        or not math.isclose(plan_vh_hours, expected_vh_hours, rel_tol=0.0, abs_tol=1e-6)
    ):
        return False, "execution_plan_vertical_horizon_hours_mismatch", None
    dsl = safe_float(exec_plan.get("planned_sl_distance_price"), None)
    dtp = safe_float(exec_plan.get("planned_tp_distance_price"), None)
    if dsl is None or dtp is None or dsl <= 0 or dtp <= 0:
        return False, "execution_plan_missing_positive_distances", None
    if not math.isclose(dtp / dsl, expected_pt / expected_sl, rel_tol=1e-9, abs_tol=1e-9):
        return False, "execution_plan_reward_risk_ratio_mismatch", None
    return True, "ok", exec_plan



def order_type_for_side(mt5: Any, side_value: int) -> int:
    return mt5.ORDER_TYPE_BUY if int(side_value) == 1 else mt5.ORDER_TYPE_SELL


def side_name_from_value(side_value: int) -> str:
    return "long" if int(side_value) == 1 else "short"


def position_order_type(mt5: Any, position_type: int) -> int | None:
    if int(position_type) == int(mt5.POSITION_TYPE_BUY):
        return mt5.ORDER_TYPE_BUY
    if int(position_type) == int(mt5.POSITION_TYPE_SELL):
        return mt5.ORDER_TYPE_SELL
    return None


def parse_comment_metadata(comment: str) -> dict[str, Any]:
    out = {"asset": None, "family": "UNKNOWN", "edge_id": "UNKNOWN", "metadata_source": "comment_parse"}
    try:
        parts = str(comment or "").split("|")
        if len(parts) >= 3 and parts[0] == "V2P":
            out["asset"] = parts[1]
            out["family"] = parts[2]
    except Exception:
        pass
    return out


def metadata_key(value: Any) -> str:
    """Normalize MT5/order/deal/comment keys for persistent metadata maps."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    try:
        val = float(text)
        if math.isfinite(val) and abs(val - round(val)) < 1e-9:
            return str(int(round(val)))
    except Exception:
        pass
    return text


def compact_family_code(family: str) -> str:
    cleaned = "".join(ch for ch in str(family or "edge").upper() if ch.isalnum())
    return (cleaned[:6] or "EDGE")


def compact_asset_code(asset: str) -> str:
    cleaned = "".join(ch for ch in str(asset or "").upper() if ch.isalnum())
    if cleaned == "NASDAQ":
        return "NAS"
    if cleaned == "XAUUSD":
        return "XAU"
    if cleaned == "BTCUSD":
        return "BTC"
    return (cleaned[:6] or "ASSET")


def signal_hash_suffix(signal_id: str, length: int = 6) -> str:
    raw = str(signal_id or "").strip()
    if not raw:
        raw = utc_now()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def make_position_metadata(plan: "OrderPlan", signal: dict[str, Any], result: Any = None) -> dict[str, Any]:
    fill_time_utc = utc_now()
    return {
        "signal_id": plan.signal_id,
        "edge_id": plan.edge_id,
        "asset": plan.asset,
        "symbol": plan.symbol,
        "family": plan.family,
        "side_name": plan.side_name,
        "side_value": plan.side_value,
        "event_time": signal.get("event_time"),
        "model_target_risk_pct": plan.model_target_risk_pct,
        "model_target_risk_usd": plan.model_target_risk_usd,
        "target_risk_pct": plan.target_risk_pct,
        "target_risk_usd": plan.target_risk_usd,
        "actual_risk_usd_at_entry": plan.actual_risk_usd,
        "actual_risk_pct_at_entry": plan.actual_risk_pct,
        "volume": plan.volume,
        "entry_price": plan.entry_price,
        "stop_loss": plan.stop_loss,
        "take_profit": plan.take_profit,
        "geometry_hash": plan.geometry_hash,
        "pt_multiplier": plan.pt_multiplier,
        "sl_multiplier": plan.sl_multiplier,
        "reward_risk_ratio": plan.reward_risk_ratio,
        "vertical_horizon_bars": plan.vertical_horizon_bars,
        "calendar_time": plan.calendar_time,
        "vertical_horizon_hours": plan.vertical_horizon_hours,
        # Research/live barrier anchor. Bar-count edges anchor on the event's
        # bar sequence; calendar-time edges anchor on the event's wall-clock
        # timestamp only (entry_event_bar_sequence is unused in that mode).
        "entry_event_time": signal.get("event_time"),
        "entry_event_bar_sequence": safe_int(signal.get("bar_sequence_number"), None),
        "bar_sequence_contract_version": BAR_SEQUENCE_CONTRACT_VERSION,
        "bar_time_contract_version": signal.get("bar_time_contract_version"),
        # Exact UTC time captured by this process immediately after the broker
        # reports a successful fill. This is authoritative for vertical-bar
        # causality; MT5 position.time/time_msc may expose broker-local wall time.
        "order_send_completed_at": fill_time_utc,
        "execution_fill_time_utc": fill_time_utc,
        "position_open_time": fill_time_utc,
        "broker_reported_position_open_time": None,
        "risk_contract_hash": plan.risk_contract_hash,
        "order": getattr(result, "order", None) if result is not None else None,
        "deal": getattr(result, "deal", None) if result is not None else None,
        "comment": plan.comment,
        "execution_plan_hash": plan.execution_plan_hash,
        "live_manifest_hash": plan.live_manifest_hash,
        "edge_manifest_hash": plan.edge_manifest_hash,
        "bar_policy_hash": plan.bar_policy_hash,
        "metadata_source": "execution_bot_order_plan",
        "created_at": utc_now(),
    }


def _metadata_edge_id(meta: dict[str, Any] | None) -> str:
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("edge_id") or "").strip()


def _register_metadata_map(
    state: dict[str, Any],
    map_name: str,
    key: Any,
    metadata: dict[str, Any],
    *,
    ambiguity_map_name: str | None = None,
) -> None:
    k = metadata_key(key)
    if not k:
        return
    mapping = state.setdefault(map_name, {})
    existing = mapping.get(k)
    new_edge = _metadata_edge_id(metadata)
    old_edge = _metadata_edge_id(existing)
    if existing and old_edge and new_edge and old_edge != new_edge:
        if ambiguity_map_name:
            state.setdefault(ambiguity_map_name, {})[k] = {
                "created_at": utc_now(),
                "reason": "same_metadata_key_multiple_edge_ids",
                "old_edge_id": old_edge,
                "new_edge_id": new_edge,
                "metadata_key": k,
                "map_name": map_name,
            }
            mapping.pop(k, None)
        return
    if existing and old_edge == new_edge:
        candidate = dict(metadata)
        for field in (
            "entry_event_bar_sequence",
            "bar_sequence_contract_version",
            "bar_time_contract_version",
        ):
            if candidate.get(field) in (None, "") and existing.get(field) not in (None, ""):
                candidate[field] = existing.get(field)
        metadata = candidate
    mapping[k] = metadata


def register_position_metadata(
    state: dict[str, Any],
    metadata: dict[str, Any],
    *,
    ticket: Any = None,
    comment: Any = None,
    order: Any = None,
    deal: Any = None,
    signal_id: Any = None,
) -> None:
    """Persist full edge attribution under every key MT5/audits may expose."""
    if not isinstance(metadata, dict) or not _metadata_edge_id(metadata):
        return
    _register_metadata_map(state, "open_position_metadata_by_ticket", ticket, metadata)
    _register_metadata_map(
        state,
        "open_position_metadata_by_comment",
        comment or metadata.get("comment"),
        metadata,
        ambiguity_map_name="ambiguous_position_metadata_comments",
    )
    _register_metadata_map(state, "open_position_metadata_by_order", order or metadata.get("order"), metadata)
    _register_metadata_map(state, "open_position_metadata_by_deal", deal or metadata.get("deal"), metadata)
    _register_metadata_map(state, "open_position_metadata_by_signal_id", signal_id or metadata.get("signal_id"), metadata)


def metadata_from_execution_output_row(row: dict[str, Any]) -> dict[str, Any] | None:
    edge_id = str(row.get("plan_edge_id") or row.get("edge_id") or "").strip()
    signal_id = str(row.get("plan_signal_id") or row.get("signal_id") or "").strip()
    comment = str(row.get("plan_comment") or row.get("comment") or "").strip()
    if not edge_id or edge_id.upper() == "UNKNOWN":
        return None
    return {
        "signal_id": signal_id,
        "edge_id": edge_id,
        "asset": row.get("plan_asset") or row.get("asset"),
        "symbol": row.get("plan_symbol") or row.get("symbol"),
        "family": row.get("plan_family") or row.get("family") or "UNKNOWN",
        "side_name": row.get("plan_side_name"),
        "side_value": row.get("plan_side_value") or row.get("side_value"),
        "event_time": row.get("event_time"),
        "model_target_risk_pct": row.get("plan_model_target_risk_pct") or row.get("model_target_risk_pct"),
        "model_target_risk_usd": row.get("plan_model_target_risk_usd") or row.get("model_target_risk_usd"),
        "target_risk_pct": row.get("plan_target_risk_pct"),
        "target_risk_usd": row.get("plan_target_risk_usd"),
        "actual_risk_usd_at_entry": row.get("plan_actual_risk_usd"),
        "actual_risk_pct_at_entry": row.get("plan_actual_risk_pct"),
        "volume": row.get("plan_volume"),
        "entry_price": row.get("plan_entry_price"),
        "stop_loss": row.get("plan_stop_loss"),
        "take_profit": row.get("plan_take_profit"),
        "vertical_horizon_bars": row.get("plan_vertical_horizon_bars"),
        "calendar_time": safe_bool(row.get("plan_calendar_time"), False),
        "vertical_horizon_hours": row.get("plan_vertical_horizon_hours"),
        "entry_event_time": row.get("event_time"),
        "entry_event_bar_sequence": row.get("entry_event_bar_sequence") or row.get("bar_sequence_number"),
        "bar_sequence_contract_version": row.get("bar_sequence_contract_version") or BAR_SEQUENCE_CONTRACT_VERSION,
        "bar_time_contract_version": row.get("bar_time_contract_version"),
        "order_send_completed_at": (
            row.get("order_send_completed_at") or row.get("created_at")
        ),
        "execution_fill_time_utc": (
            row.get("execution_fill_time_utc")
            or row.get("position_open_time")
            or row.get("order_send_completed_at")
            or row.get("created_at")
        ),
        "position_open_time": (
            row.get("execution_fill_time_utc")
            or row.get("position_open_time")
            or row.get("order_send_completed_at")
            or row.get("created_at")
        ),
        "broker_reported_position_open_time": row.get(
            "broker_reported_position_open_time"
        ),
        "order": row.get("order"),
        "deal": row.get("deal"),
        "comment": comment,
        "execution_plan_hash": row.get("plan_execution_plan_hash") or row.get("execution_plan_hash"),
        "live_manifest_hash": row.get("plan_live_manifest_hash") or row.get("live_manifest_hash"),
        "edge_manifest_hash": row.get("plan_edge_manifest_hash") or row.get("edge_manifest_hash"),
        "bar_policy_hash": row.get("plan_bar_policy_hash") or row.get("bar_policy_hash"),
        "metadata_source": "execution_output_backfill",
        "created_at": utc_now(),
    }


def backfill_position_metadata_from_execution_outputs(state: dict[str, Any]) -> None:
    """Recover metadata maps from prior execution CSV/JSONL outputs.

    This is intentionally best-effort. It is used to recover full edge_id
    attribution after restarts and for older positions whose MT5 comment could
    not encode the full edge id.
    """
    rows: list[dict[str, Any]] = []

    if EXECUTED_TRADES_CSV.exists():
        try:
            df = pd.read_csv(EXECUTED_TRADES_CSV)
            if not df.empty:
                rows.extend(df.to_dict("records"))
        except Exception:
            pass

    jsonl_path = BOT_OUTPUT_DIR / "portfolio_executed_trades.jsonl"
    if jsonl_path.exists():
        try:
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        payload = json.loads(line)
                        if isinstance(payload, dict):
                            rows.append(payload)
        except Exception:
            pass

    for row in rows[-5000:]:
        metadata = metadata_from_execution_output_row(row)
        if not metadata:
            continue
        register_position_metadata(
            state,
            metadata,
            ticket=row.get("position") or row.get("ticket"),
            comment=metadata.get("comment"),
            order=metadata.get("order"),
            deal=metadata.get("deal"),
            signal_id=metadata.get("signal_id"),
        )


def repair_position_fill_time_metadata(state: dict[str, Any]) -> int:
    """Migrate persisted metadata to an authoritative process-captured UTC fill time.

    Old releases overwrote position_open_time with MT5 position.time_msc, which
    can represent broker-local wall time. The order-send completion timestamp is
    captured by this Python process in UTC and is therefore the safe causal
    reference for post-fill bar checks.
    """
    repaired = 0
    map_names = (
        "open_position_metadata_by_ticket",
        "open_position_metadata_by_comment",
        "open_position_metadata_by_order",
        "open_position_metadata_by_deal",
        "open_position_metadata_by_signal_id",
    )
    for map_name in map_names:
        mapping = state.setdefault(map_name, {})
        for key, raw in list(mapping.items()):
            if not isinstance(raw, dict):
                continue
            meta = dict(raw)
            authoritative = (
                ensure_utc_timestamp(meta.get("execution_fill_time_utc"))
                or ensure_utc_timestamp(meta.get("order_send_completed_at"))
                or ensure_utc_timestamp(meta.get("created_at"))
            )
            if authoritative is None:
                continue
            old_open = ensure_utc_timestamp(meta.get("position_open_time"))
            if (
                old_open is not None
                and abs(float((old_open - authoritative).total_seconds())) > 5.0
                and not meta.get("broker_reported_position_open_time")
            ):
                meta["broker_reported_position_open_time"] = old_open.isoformat()
            authoritative_iso = authoritative.isoformat()
            changed = (
                meta.get("execution_fill_time_utc") != authoritative_iso
                or meta.get("position_open_time") != authoritative_iso
            )
            meta["execution_fill_time_utc"] = authoritative_iso
            meta["position_open_time"] = authoritative_iso
            meta["fill_time_authority"] = "python_process_utc_order_send_completion"
            if changed:
                meta["fill_time_repaired_at"] = utc_now()
                repaired += 1
            mapping[key] = meta
    if repaired:
        state.setdefault("position_fill_time_migration_history", []).append(
            {
                "created_at": utc_now(),
                "records_repaired": repaired,
                "reason": "replace_broker_local_position_time_with_process_utc_fill_time",
            }
        )
        state["position_fill_time_migration_history"] = state[
            "position_fill_time_migration_history"
        ][-50:]
    return repaired


def resolve_position_metadata(position: Any, state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Resolve only exact durable identifiers; never guess from a truncated comment."""
    ticket = metadata_key(getattr(position, "ticket", None))
    identifier = metadata_key(getattr(position, "identifier", None))
    comment = metadata_key(getattr(position, "comment", None))
    ambiguous_comments = state.setdefault("ambiguous_position_metadata_comments", {})
    lookup_order = [
        ("ticket", state.setdefault("open_position_metadata_by_ticket", {}).get(ticket)),
        ("identifier_as_ticket", state.setdefault("open_position_metadata_by_ticket", {}).get(identifier)),
        ("identifier_as_order", state.setdefault("open_position_metadata_by_order", {}).get(identifier)),
        ("ticket_as_order", state.setdefault("open_position_metadata_by_order", {}).get(ticket)),
        ("identifier_as_deal", state.setdefault("open_position_metadata_by_deal", {}).get(identifier)),
        ("ticket_as_deal", state.setdefault("open_position_metadata_by_deal", {}).get(ticket)),
    ]
    if comment and comment not in ambiguous_comments:
        lookup_order.append(("unique_comment", state.setdefault("open_position_metadata_by_comment", {}).get(comment)))
    for source, meta in lookup_order:
        if isinstance(meta, dict) and _metadata_edge_id(meta) and _metadata_edge_id(meta).upper() != "UNKNOWN":
            out = dict(meta)
            out["metadata_source"] = source
            register_position_metadata(
                state, out, ticket=ticket, comment=comment,
                order=out.get("order"), deal=out.get("deal"), signal_id=out.get("signal_id"),
            )
            state.setdefault("unresolved_position_metadata_by_ticket", {}).pop(ticket, None)
            return out, source
    unresolved = {
        "asset": None,
        "family": "UNKNOWN",
        "edge_id": "UNKNOWN",
        "signal_id": None,
        "comment": comment,
        "metadata_source": "unresolved_exact_metadata",
    }
    state.setdefault("unresolved_position_metadata_by_ticket", {})[ticket or identifier or comment or "UNKNOWN"] = {
        "created_at": utc_now(),
        "ticket": ticket,
        "identifier": identifier,
        "comment": comment,
        "reason": "no_exact_ticket_order_deal_signal_or_unique_comment_mapping",
    }
    return unresolved, "unresolved_exact_metadata"


def estimate_position_risk_usd(mt5: Any, position: Any, logger: Any) -> tuple[float | None, str]:
    symbol = str(position.symbol)
    volume = float(position.volume)
    entry = float(position.price_open)
    sl = safe_float(getattr(position, "sl", None), 0.0) or 0.0
    if sl <= 0:
        return None, "missing_stop_loss"
    otype = position_order_type(mt5, int(position.type))
    if otype is None:
        return None, "unknown_position_type"
    profit_at_sl = mt5.order_calc_profit(otype, symbol, volume, entry, sl)
    if profit_at_sl is None:
        logger.warning("OPEN_RISK_CALC_WARN ticket=%s symbol=%s error=%s", getattr(position, "ticket", ""), symbol, mt5.last_error())
        return None, "order_calc_profit_failed"
    return max(0.0, -float(profit_at_sl)), "ok"


def pending_order_direction_type(mt5: Any, order_type: int) -> int | None:
    buy_names = ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_BUY_STOP", "ORDER_TYPE_BUY_STOP_LIMIT")
    sell_names = ("ORDER_TYPE_SELL_LIMIT", "ORDER_TYPE_SELL_STOP", "ORDER_TYPE_SELL_STOP_LIMIT")
    if int(order_type) in {int(getattr(mt5, name)) for name in buy_names if hasattr(mt5, name)}:
        return int(mt5.ORDER_TYPE_BUY)
    if int(order_type) in {int(getattr(mt5, name)) for name in sell_names if hasattr(mt5, name)}:
        return int(mt5.ORDER_TYPE_SELL)
    return None


def estimate_pending_order_risk_usd(mt5: Any, order: Any, logger: Any) -> tuple[float | None, str]:
    symbol = str(getattr(order, "symbol", "") or "")
    volume = safe_float(getattr(order, "volume_current", None), None)
    if volume is None or volume <= 0:
        volume = safe_float(getattr(order, "volume_initial", None), None)
    entry = safe_float(getattr(order, "price_open", None), None)
    sl = safe_float(getattr(order, "sl", None), None)
    direction = pending_order_direction_type(mt5, int(getattr(order, "type", -999)))
    if not symbol or volume is None or volume <= 0 or entry is None or entry <= 0:
        return None, "invalid_pending_order_contract"
    if sl is None or sl <= 0:
        return None, "pending_order_missing_stop_loss"
    if direction is None:
        return None, "unknown_pending_order_type"
    profit_at_sl = mt5.order_calc_profit(direction, symbol, float(volume), float(entry), float(sl))
    if profit_at_sl is None:
        logger.warning("PENDING_RISK_CALC_WARN ticket=%s symbol=%s error=%s", getattr(order, "ticket", ""), symbol, mt5.last_error())
        return None, "pending_order_calc_profit_failed"
    return max(0.0, -float(profit_at_sl)), "ok"


def get_open_risk_snapshot(mt5: Any, manifest: dict[str, Any], state: dict[str, Any], logger: Any) -> dict[str, Any]:
    """Measure stop risk across the entire MT5 account, not only this portfolio."""
    symbol_to_asset = universe_symbols(manifest)
    positions = list(mt5.positions_get() or [])
    pending_orders = list(mt5.orders_get() or []) if INCLUDE_PENDING_ORDERS_IN_RISK else []
    # QUANTARA_OPPOSITE_DIRECTION_SAME_SYMBOL_GUARD_V1
    # Reuse the SAME whole-account MT5 positions snapshot already used for risk.
    # Unmanaged positions count too because the broker/account policy concerns
    # actual symbol exposure, not Quantara metadata attribution.
    open_position_directions_by_symbol: dict[str, dict[str, Any]] = {}
    _buy_type = safe_int(getattr(mt5, "POSITION_TYPE_BUY", 0), 0)
    _sell_type = safe_int(getattr(mt5, "POSITION_TYPE_SELL", 1), 1)
    for _pos in positions:
        _sym = str(getattr(_pos, "symbol", "") or "").strip()
        if not _sym:
            continue
        _ptype = safe_int(getattr(_pos, "type", None), None)
        _ticket = metadata_key(getattr(_pos, "ticket", None))
        _bucket = open_position_directions_by_symbol.setdefault(
            _sym,
            {"long_count": 0, "short_count": 0, "long_tickets": [], "short_tickets": []},
        )
        if _ptype == _buy_type:
            _bucket["long_count"] = int(_bucket["long_count"]) + 1
            if _ticket:
                _bucket["long_tickets"].append(_ticket)
        elif _ptype == _sell_type:
            _bucket["short_count"] = int(_bucket["short_count"]) + 1
            if _ticket:
                _bucket["short_tickets"].append(_ticket)
    try:
        backfill_position_metadata_from_execution_outputs(state)
    except Exception as exc:
        logger.warning("POSITION_METADATA_BACKFILL_WARN error=%s", exc)

    risk_by_asset: dict[str, float] = {}
    risk_by_family: dict[str, float] = {}
    risk_by_edge: dict[str, float] = {}
    count_by_asset: dict[str, int] = {}
    count_by_edge: dict[str, int] = {}
    unknown_risk_positions: list[dict[str, Any]] = []
    unknown_pending_orders: list[dict[str, Any]] = []
    total = 0.0
    managed_risk = 0.0
    unattributed_portfolio_risk = 0.0
    external_position_risk = 0.0
    pending_order_risk = 0.0
    unresolved_edge_metadata_count = 0

    def add_bucket(asset: str | None, family: str | None, edge_id: str | None, risk: float, *, count: bool = True) -> None:
        nonlocal total
        total += float(risk)
        if asset:
            risk_by_asset[asset] = risk_by_asset.get(asset, 0.0) + float(risk)
            if count:
                count_by_asset[asset] = count_by_asset.get(asset, 0) + 1
        if family and family.upper() != "UNKNOWN":
            risk_by_family[family] = risk_by_family.get(family, 0.0) + float(risk)
        if edge_id and edge_id.upper() != "UNKNOWN":
            risk_by_edge[edge_id] = risk_by_edge.get(edge_id, 0.0) + float(risk)
            if count:
                count_by_edge[edge_id] = count_by_edge.get(edge_id, 0) + 1

    for pos in positions:
        symbol = str(getattr(pos, "symbol", "") or "")
        selected_asset = symbol_to_asset.get(symbol)
        if selected_asset is None and not INCLUDE_UNMANAGED_POSITIONS_IN_RISK:
            continue
        ticket = metadata_key(getattr(pos, "ticket", None))
        comment = str(getattr(pos, "comment", "") or "")
        meta, metadata_source = resolve_position_metadata(pos, state)
        family = str(meta.get("family") or "UNKNOWN")
        edge_id = str(meta.get("edge_id") or "UNKNOWN")
        risk, reason = estimate_position_risk_usd(mt5, pos, logger)
        if risk is None:
            unknown_risk_positions.append({
                "ticket": ticket, "symbol": symbol, "asset": selected_asset,
                "volume": float(getattr(pos, "volume", 0.0) or 0.0), "comment": comment,
                "reason": reason, "metadata_source": metadata_source,
                "edge_id": edge_id, "family": family,
            })
            continue
        if selected_asset is None:
            add_bucket(None, None, None, risk)
            external_position_risk += risk
            continue
        if edge_id.upper() == "UNKNOWN":
            add_bucket(selected_asset, None, None, risk)
            unattributed_portfolio_risk += risk
            unresolved_edge_metadata_count += 1
            unknown_risk_positions.append({
                "ticket": ticket, "symbol": symbol, "asset": selected_asset,
                "volume": float(getattr(pos, "volume", 0.0) or 0.0), "comment": comment,
                "risk_usd": risk, "reason": "unresolved_edge_metadata",
                "metadata_source": metadata_source, "edge_id": edge_id, "family": family,
            })
            continue
        add_bucket(selected_asset, family, edge_id, risk)
        managed_risk += risk

    for order in pending_orders:
        symbol = str(getattr(order, "symbol", "") or "")
        selected_asset = symbol_to_asset.get(symbol)
        ticket = metadata_key(getattr(order, "ticket", None))
        risk, reason = estimate_pending_order_risk_usd(mt5, order, logger)
        meta, metadata_source = resolve_position_metadata(order, state)
        family = str(meta.get("family") or "UNKNOWN")
        edge_id = str(meta.get("edge_id") or "UNKNOWN")
        if risk is None:
            unknown_pending_orders.append({
                "ticket": ticket, "symbol": symbol, "asset": selected_asset,
                "volume": safe_float(getattr(order, "volume_current", None), safe_float(getattr(order, "volume_initial", None), 0.0)),
                "reason": reason, "metadata_source": metadata_source,
                "edge_id": edge_id, "family": family,
            })
            continue
        pending_order_risk += risk
        if selected_asset is None:
            add_bucket(None, None, None, risk)
            external_position_risk += risk
        elif edge_id.upper() == "UNKNOWN":
            add_bucket(selected_asset, None, None, risk)
            unattributed_portfolio_risk += risk
            unresolved_edge_metadata_count += 1
            unknown_pending_orders.append({
                "ticket": ticket, "symbol": symbol, "asset": selected_asset,
                "risk_usd": risk, "reason": "unresolved_pending_order_metadata",
                "metadata_source": metadata_source, "edge_id": edge_id, "family": family,
            })
        else:
            add_bucket(selected_asset, family, edge_id, risk)
            managed_risk += risk

    return {
        "total_open_risk_usd": float(total),
        "managed_portfolio_risk_usd": float(managed_risk),
        "unattributed_portfolio_risk_usd": float(unattributed_portfolio_risk),
        "external_position_risk_usd": float(external_position_risk),
        "pending_order_contingent_risk_usd": float(pending_order_risk),
        "open_position_directions_by_symbol": open_position_directions_by_symbol,
        "risk_by_asset": risk_by_asset,
        "risk_by_family": risk_by_family,
        "risk_by_edge": risk_by_edge,
        "count_by_asset": count_by_asset,
        "count_by_edge": count_by_edge,
        "unknown_risk_positions": unknown_risk_positions,
        "unknown_pending_orders": unknown_pending_orders,
        "unknown_risk_blocks_new_entries": bool(
            (BLOCK_IF_UNKNOWN_OPEN_RISK and unknown_risk_positions)
            or (BLOCK_IF_UNKNOWN_PENDING_RISK and unknown_pending_orders)
        ),
        "unresolved_edge_metadata_count": unresolved_edge_metadata_count,
        "position_count_considered": len(positions),
        "pending_order_count_considered": len(pending_orders),
        "metadata_map_counts": {
            "ticket": len(state.get("open_position_metadata_by_ticket", {})),
            "comment": len(state.get("open_position_metadata_by_comment", {})),
            "order": len(state.get("open_position_metadata_by_order", {})),
            "deal": len(state.get("open_position_metadata_by_deal", {})),
            "signal_id": len(state.get("open_position_metadata_by_signal_id", {})),
            "ambiguous_comments": len(state.get("ambiguous_position_metadata_comments", {})),
        },
    }


# QUANTARA_OPPOSITE_DIRECTION_SAME_SYMBOL_GUARD_V1
def opposite_direction_same_symbol_policy_diagnostic(
    signal: dict[str, Any],
    open_risk: dict[str, Any],
) -> dict[str, Any]:
    """Pure account-policy decision using the already-captured broker snapshot."""
    asset = str(signal.get("asset") or "").strip()
    symbol = str(
        signal.get("mt5_symbol")
        or settings.MT5_SYMBOL_MAP.get(asset, asset)
        or ""
    ).strip()
    side_value = safe_int(signal.get("side_value"), None)
    requested = "LONG" if side_value == 1 else "SHORT" if side_value == -1 else "UNKNOWN"

    mapping = open_risk.get("open_position_directions_by_symbol") or {}
    bucket = mapping.get(symbol) if isinstance(mapping, dict) else {}
    if not isinstance(bucket, dict):
        bucket = {}
    long_count = max(int(safe_int(bucket.get("long_count"), 0) or 0), 0)
    short_count = max(int(safe_int(bucket.get("short_count"), 0) or 0), 0)
    long_tickets = [str(x) for x in (bucket.get("long_tickets") or []) if str(x)]
    short_tickets = [str(x) for x in (bucket.get("short_tickets") or []) if str(x)]

    if side_value == 1:
        opposite_side, opposite_count, opposite_tickets = "SHORT", short_count, short_tickets
    elif side_value == -1:
        opposite_side, opposite_count, opposite_tickets = "LONG", long_count, long_tickets
    else:
        opposite_side, opposite_count, opposite_tickets = None, 0, []

    enabled = bool(BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL)
    blocked = bool(enabled and side_value in {-1, 1} and symbol and opposite_count > 0)
    return {
        "policy": "block_opposite_direction_same_symbol",
        "policy_enabled": enabled,
        "blocked": blocked,
        "asset": asset,
        "symbol": symbol,
        "requested_side": requested,
        "requested_side_value": side_value,
        "existing_opposite_side": opposite_side,
        "existing_opposite_count": int(opposite_count),
        "existing_opposite_tickets": opposite_tickets,
        "existing_long_count": int(long_count),
        "existing_short_count": int(short_count),
        "snapshot_position_count_considered": int(
            safe_int(open_risk.get("position_count_considered"), 0) or 0
        ),
    }


def get_fill_type(mt5: Any, symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    mode = int(getattr(info, "filling_mode", mt5.ORDER_FILLING_FOK))
    if mode == getattr(mt5, "ORDER_FILLING_IOC", -1):
        return mt5.ORDER_FILLING_IOC
    if mode == getattr(mt5, "ORDER_FILLING_FOK", -1):
        return mt5.ORDER_FILLING_FOK
    if hasattr(mt5, "ORDER_FILLING_RETURN") and mode == getattr(mt5, "ORDER_FILLING_RETURN"):
        return mt5.ORDER_FILLING_RETURN
    try:
        if mode & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if mode & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
    except Exception:
        pass
    if hasattr(mt5, "ORDER_FILLING_RETURN"):
        return mt5.ORDER_FILLING_RETURN
    return mt5.ORDER_FILLING_FOK


def cached_filling_mode(state: dict[str, Any] | None, symbol: str) -> int | None:
    if not isinstance(state, dict):
        return None
    return safe_int((state.get("preferred_filling_mode_by_symbol") or {}).get(str(symbol)), None)


def remember_filling_mode(state: dict[str, Any] | None, symbol: str, mode: Any) -> None:
    if not isinstance(state, dict):
        return
    parsed = safe_int(mode, None)
    if parsed is None:
        return
    state.setdefault("preferred_filling_mode_by_symbol", {})[str(symbol)] = int(parsed)


def filling_mode_candidates(
    mt5: Any,
    symbol: str,
    preferred: int | None = None,
    state: dict[str, Any] | None = None,
) -> list[int]:
    """Return safe filling modes, preferring the last broker-accepted mode."""
    candidates: list[int] = []

    def _add(value: Any) -> None:
        try:
            mode = int(value)
        except Exception:
            return
        if mode not in candidates:
            candidates.append(mode)

    _add(cached_filling_mode(state, symbol))
    if preferred is not None:
        _add(preferred)

    try:
        _add(get_fill_type(mt5, symbol))
    except Exception:
        pass

    for name in ["ORDER_FILLING_IOC", "ORDER_FILLING_FOK", "ORDER_FILLING_RETURN"]:
        if hasattr(mt5, name):
            _add(getattr(mt5, name))

    return candidates


def is_invalid_fill_result(mt5: Any, result: Any) -> bool:
    """True only for broker rejection caused by unsupported filling mode."""
    if result is None:
        return False
    retcode = getattr(result, "retcode", None)
    try:
        if int(retcode) == int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL")):
            return True
    except Exception:
        pass
    comment = str(getattr(result, "comment", "") or "").lower()
    return "unsupported filling" in comment or "invalid fill" in comment


def transient_order_result_reason(mt5: Any, result: Any) -> str | None:
    """Classify explicit no-execution broker conditions that are safe to retry."""
    if result is None:
        return None

    retcode = safe_int(getattr(result, "retcode", None), None)
    retname = get_retcode_name(mt5, retcode)
    comment = str(getattr(result, "comment", "") or "").strip().lower()

    retryable_names = {
        "TRADE_RETCODE_CONNECTION",
        "TRADE_RETCODE_TIMEOUT",
        "TRADE_RETCODE_PRICE_OFF",
        "TRADE_RETCODE_TOO_MANY_REQUESTS",
        "TRADE_RETCODE_LOCKED",
    }
    if retname in retryable_names:
        return f"{retname}:{comment or 'temporary_broker_condition'}"

    explicit_no_connection = [
        "absence of network connection",
        "no connection",
        "not connected",
        "connection lost",
        "trade server unavailable",
        "server unavailable",
        "temporarily unavailable",
    ]
    if any(token in comment for token in explicit_no_connection):
        if not getattr(result, "order", None) and not getattr(result, "deal", None):
            return f"{retname or retcode}:{comment}"
    return None


def is_market_closed_result(mt5: Any, result: Any) -> bool:
    if result is None:
        return False
    retcode = safe_int(getattr(result, "retcode", None), None)
    market_closed = safe_int(getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", None), None)
    if retcode is not None and market_closed is not None and retcode == market_closed:
        return True
    comment = str(getattr(result, "comment", "") or "").strip().lower()
    return "market closed" in comment or "market is closed" in comment


def _clock_minutes(value: Any) -> int:
    text = str(value or "").strip()
    hour_text, minute_text = (text.split(":", 1) + ["0"])[:2]
    hour = min(max(int(hour_text), 0), 23)
    minute = min(max(int(minute_text), 0), 59)
    return hour * 60 + minute


def _inside_utc_blackout(asset: str, now: pd.Timestamp) -> str | None:
    current = int(now.hour) * 60 + int(now.minute)
    for index, record in enumerate(ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET.get(asset, []) or []):
        if not isinstance(record, dict):
            continue
        weekdays = {int(value) for value in record.get("weekdays", range(7))}
        if int(now.weekday()) not in weekdays:
            continue
        try:
            start = _clock_minutes(record.get("start"))
            end = _clock_minutes(record.get("end"))
        except Exception:
            return f"invalid_entry_blackout_window:{asset}:{index}"
        inside = start <= current < end if start <= end else (current >= start or current < end)
        if inside:
            return f"configured_utc_session_blackout:{asset}:{record.get('start')}:{record.get('end')}"
    return None


def market_entry_guard(
    mt5: Any,
    *,
    asset: str,
    symbol: str,
    side_value: int,
    info: Any,
    tick: Any,
    state: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Fail closed before order_send when the broker/session is not executable."""
    now = utc_now_ts()
    if state is not None:
        cooldown = (state.get("market_closed_cooldown_by_symbol") or {}).get(symbol) or {}
        until = ensure_utc_timestamp(cooldown.get("until"))
        if until is not None and now < until:
            return False, f"market_closed_cooldown:{symbol}:until={until.isoformat()}"
        if until is not None and now >= until:
            state.setdefault("market_closed_cooldown_by_symbol", {}).pop(symbol, None)

    weekend = WEEKEND_ENTRY_GUARD_BY_ASSET.get(asset) or {}
    if isinstance(weekend, dict) and weekend:
        friday = float(weekend.get("friday_block_hour_utc", 24.0))
        monday = float(weekend.get("monday_resume_hour_utc", 0.0))
        hour = float(now.hour) + float(now.minute) / 60.0
        weekday = int(now.weekday())
        if (
            (weekday == 4 and hour >= friday)
            or weekday in {5, 6}
            or (weekday == 0 and hour < monday)
        ):
            return False, f"weekend_gap_entry_guard:{asset}"

    blackout = _inside_utc_blackout(asset, now)
    if blackout:
        return False, blackout

    trade_mode = safe_int(getattr(info, "trade_mode", None), None)
    disabled_modes = {
        safe_int(getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", None), None),
        safe_int(getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", None), None),
    }
    disabled_modes.discard(None)
    if trade_mode in disabled_modes:
        return False, f"symbol_trade_mode_disallows_entry:{symbol}:{trade_mode}"
    long_only = safe_int(getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", None), None)
    short_only = safe_int(getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", None), None)
    if side_value < 0 and long_only is not None and trade_mode == long_only:
        return False, f"symbol_trade_mode_long_only:{symbol}"
    if side_value > 0 and short_only is not None and trade_mode == short_only:
        return False, f"symbol_trade_mode_short_only:{symbol}"

    raw_timestamp = raw_tick_timestamp(tick)
    if raw_timestamp is None:
        return False, f"missing_broker_tick_timestamp:{symbol}"
    try:
        contract = read_broker_clock_contract(BROKER_CLOCK_CONTRACT_JSON)
        validated_clock = validate_broker_clock_contract(
            contract,
            now=now,
            required_symbol=symbol,
            max_published_age_seconds=CLOCK_CONTRACT_MAX_AGE_SECONDS,
            max_measurement_age_seconds=CLOCK_CONTRACT_MAX_AGE_SECONDS,
            future_tolerance_seconds=FUTURE_SIGNAL_TOLERANCE_SECONDS,
        )
        tick_timestamp = normalise_broker_timestamp(raw_timestamp, validated_clock)
    except BrokerClockContractError as exc:
        reason_class = classify_contract_error(exc)
        diagnostic = {
            "status": "CLOCK_CONTRACT_BLOCKED",
            "reason_class": reason_class,
            "symbol": symbol,
            "raw_broker_timestamp": raw_timestamp.isoformat(),
            "checked_at": now.isoformat(),
            "contract_path": str(BROKER_CLOCK_CONTRACT_JSON),
            "error": str(exc),
        }
        if state is not None:
            state["last_broker_clock_diagnostic"] = diagnostic
        return False, f"broker_clock_contract_unavailable:{symbol}:{exc}"
    tick_age = float((now - tick_timestamp).total_seconds())
    diagnostic = {
        "status": "PASS",
        "reason_class": "PASS",
        "symbol": symbol,
        "raw_broker_timestamp": raw_timestamp.isoformat(),
        "normalised_tick_timestamp": tick_timestamp.isoformat(),
        "offset_seconds": int(validated_clock["offset_seconds"]),
        "contract_generation_id": validated_clock["generation_id"],
        "contract_published_age_seconds": validated_clock["published_age_seconds"],
        "contract_measurement_age_seconds": validated_clock["measurement_age_seconds"],
        "tick_age_seconds": tick_age,
        "checked_at": now.isoformat(),
    }
    if tick_age < -FUTURE_SIGNAL_TOLERANCE_SECONDS:
        diagnostic.update({
            "status": "BLOCKED",
            "reason_class": GENUINE_FUTURE_TICK,
        })
        if state is not None:
            state["last_broker_clock_diagnostic"] = diagnostic
        return False, f"future_broker_tick_normalised:{symbol}:{tick_age:.3f}"
    maximum_age = float(MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET.get(asset, 30.0))
    if tick_age > maximum_age:
        diagnostic.update({
            "status": "BLOCKED",
            "reason_class": STALE_TICK,
        })
        if state is not None:
            state["last_broker_clock_diagnostic"] = diagnostic
        return False, f"stale_broker_tick_normalised:{symbol}:{tick_age:.3f}>{maximum_age:.3f}"
    if state is not None:
        state["last_broker_clock_diagnostic"] = diagnostic
    return True, "market_entry_guard_pass"


def record_market_closed_cooldown(
    state: dict[str, Any],
    *,
    symbol: str,
    result: Any,
) -> dict[str, Any]:
    record = {
        "symbol": symbol,
        "created_at": utc_now(),
        "until": (utc_now_ts() + pd.Timedelta(seconds=MARKET_CLOSED_COOLDOWN_SECONDS)).isoformat(),
        "retcode": getattr(result, "retcode", None) if result is not None else None,
        "comment": redact_sensitive_text(getattr(result, "comment", "") if result is not None else ""),
    }
    state.setdefault("market_closed_cooldown_by_symbol", {})[symbol] = record
    return record


def reconcile_prior_transient_order(
    mt5: Any,
    plan: OrderPlan,
    state: dict[str, Any],
    logger: Any,
) -> Any | None:
    """Detect a fill that may have completed despite a prior timeout response.

    Exact comment, symbol and magic matching prevents a timeout retry from
    duplicating an already-opened order. Connection/no-price failures normally
    find nothing and proceed to the normal retry path.
    """
    retry = (state.get("transient_order_retry_by_signal") or {}).get(plan.signal_id)
    if not isinstance(retry, dict) or not retry:
        return None

    expected_comment = str(retry.get("comment") or plan.comment or "")[:31]
    expected_symbol = str(retry.get("symbol") or plan.symbol or "")
    expected_magic = int(plan.magic)

    try:
        positions = mt5.positions_get(symbol=expected_symbol)
        if positions is None:
            positions = mt5.positions_get() or []
        for position in positions or []:
            if str(getattr(position, "symbol", "") or "") != expected_symbol:
                continue
            if int(getattr(position, "magic", -1) or -1) != expected_magic:
                continue
            if str(getattr(position, "comment", "") or "")[:31] != expected_comment:
                continue
            ticket = int(getattr(position, "ticket", 0) or 0)
            logger.warning(
                "TRANSIENT_ORDER_RECONCILED signal_id=%s symbol=%s source=open_position ticket=%s",
                plan.signal_id,
                expected_symbol,
                ticket,
            )
            return SimpleNamespace(
                retcode=int(mt5.TRADE_RETCODE_DONE),
                order=ticket,
                deal=0,
                comment="reconciled_existing_position_after_transient_result",
            )
    except Exception as exc:
        logger.warning(
            "TRANSIENT_ORDER_RECONCILE_POSITION_WARN signal_id=%s symbol=%s error=%s",
            plan.signal_id,
            expected_symbol,
            exc,
        )

    last_attempt = ensure_utc_timestamp(retry.get("last_attempt_at"))
    if last_attempt is None:
        return None
    history_start = (last_attempt - pd.Timedelta(seconds=10)).to_pydatetime()
    history_end = datetime.now(timezone.utc)
    try:
        deals = mt5.history_deals_get(history_start, history_end) or []
        for deal in deals:
            if str(getattr(deal, "symbol", "") or "") != expected_symbol:
                continue
            if int(getattr(deal, "magic", -1) or -1) != expected_magic:
                continue
            if str(getattr(deal, "comment", "") or "")[:31] != expected_comment:
                continue
            order_ticket = int(getattr(deal, "order", 0) or 0)
            deal_ticket = int(getattr(deal, "ticket", 0) or 0)
            logger.warning(
                "TRANSIENT_ORDER_RECONCILED signal_id=%s symbol=%s source=deal_history order=%s deal=%s",
                plan.signal_id,
                expected_symbol,
                order_ticket,
                deal_ticket,
            )
            return SimpleNamespace(
                retcode=int(mt5.TRADE_RETCODE_DONE),
                order=order_ticket,
                deal=deal_ticket,
                comment="reconciled_deal_after_transient_result",
            )
    except Exception as exc:
        logger.warning(
            "TRANSIENT_ORDER_RECONCILE_HISTORY_WARN signal_id=%s symbol=%s error=%s",
            plan.signal_id,
            expected_symbol,
            exc,
        )
    return None


def record_transient_order_retry(
    state: dict[str, Any],
    plan: OrderPlan,
    result: Any,
    reason: str,
) -> dict[str, Any]:
    retries = state.setdefault("transient_order_retry_by_signal", {})
    prior = retries.get(plan.signal_id) or {}
    attempt_count = int(prior.get("attempt_count", 0) or 0) + 1
    delay = min(
        TRANSIENT_ORDER_RETRY_MAX_SECONDS,
        TRANSIENT_ORDER_RETRY_INITIAL_SECONDS * (2 ** max(attempt_count - 1, 0)),
    )
    next_retry = utc_now_ts() + pd.Timedelta(seconds=float(delay))
    record = {
        "signal_id": plan.signal_id,
        "symbol": plan.symbol,
        "comment": plan.comment,
        "attempt_count": attempt_count,
        "last_reason": reason,
        "last_retcode": getattr(result, "retcode", None) if result is not None else None,
        "last_attempt_at": utc_now(),
        "next_retry_at": next_retry.isoformat(),
        "retry_delay_seconds": float(delay),
    }
    retries[plan.signal_id] = record
    return record


def clear_transient_order_retry(state: dict[str, Any], signal_id: str) -> None:
    state.setdefault("transient_order_retry_by_signal", {}).pop(str(signal_id), None)


def summarize_order_attempt(mt5: Any, result: Any, type_filling: int) -> dict[str, Any]:
    retcode = getattr(result, "retcode", None) if result is not None else None
    return {
        "type_filling": int(type_filling),
        "retcode": retcode,
        "retcode_name": get_retcode_name(mt5, retcode),
        "comment": getattr(result, "comment", "order_send returned None") if result is not None else "order_send returned None",
        "order": getattr(result, "order", None) if result is not None else None,
        "deal": getattr(result, "deal", None) if result is not None else None,
    }



def stable_json_hash(value: Any) -> str:
    compact = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def resolve_manifest_source_path(manifest: dict[str, Any]) -> Path:
    raw = str(manifest.get("source") or "").strip()
    if not raw:
        raise RuntimeError("live_manifest_missing_allocator_source")
    path = Path(raw)
    if not path.is_absolute():
        path = V2_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"allocator_source_not_found:{path}")
    if HASH_VALIDATION_ENABLED:
        expected = str((manifest.get("source_hash") or {}).get("sha256") or "").strip()
        if not expected:
            raise RuntimeError("live_manifest_missing_allocator_source_hash")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"allocator_source_hash_mismatch:{actual}!={expected}")
    return path


def _same_tiers(a: Any, b: Any) -> bool:
    def norm(rows: Any) -> list[list[float]]:
        out: list[list[float]] = []
        for item in rows or []:
            if isinstance(item, dict):
                floor = safe_float(item.get("floor") or item.get("balance_floor") or item.get("threshold"), None)
                mult = safe_float(item.get("multiplier") or item.get("mult"), None)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                floor = safe_float(item[0], None); mult = safe_float(item[1], None)
            else:
                continue
            if floor is not None and mult is not None:
                out.append([float(floor), float(mult)])
        return out
    return norm(a) == norm(b)


def load_prop_risk_contract(manifest: dict[str, Any]) -> dict[str, Any]:
    """Load one hashless, structurally verified allocator/live risk contract."""
    source_path = resolve_manifest_source_path(manifest)
    source = read_json_object(source_path)
    variant = source.get("selected_variant") or {}
    if not isinstance(variant, dict) or not variant:
        raise RuntimeError("allocator_source_missing_selected_variant")

    source_rules = source.get("active_account_rules")
    manifest_rules = manifest.get("active_account_rules")
    if not structurally_equal(source_rules, ACCOUNT_RULE_CONTRACT):
        raise RuntimeError("allocator_source_active_account_rules_mismatch")
    if not structurally_equal(manifest_rules, ACCOUNT_RULE_CONTRACT):
        raise RuntimeError("live_manifest_active_account_rules_mismatch")

    account_stage = str(manifest.get("approved_account_stage") or manifest.get("account_stage") or "").strip().lower()
    account_profile = str(manifest.get("account_rule_profile") or "").strip()
    if account_stage != EXPECTED_ACCOUNT_STAGE:
        raise RuntimeError(f"active_account_stage_mismatch:{account_stage}!={EXPECTED_ACCOUNT_STAGE}")
    if account_profile != EXPECTED_ACCOUNT_PROFILE:
        raise RuntimeError(f"active_account_profile_mismatch:{account_profile}!={EXPECTED_ACCOUNT_PROFILE}")
    if str(manifest.get("live_approval_scope") or "") != EXPECTED_APPROVAL_SCOPE:
        raise RuntimeError("active_account_live_approval_scope_mismatch")

    cap_keys = [
        "base_portfolio_risk_budget_pct",
        "max_single_trade_risk_pct",
        "max_total_open_risk_pct",
        "max_asset_open_risk_pct",
        "max_family_open_risk_pct",
        "max_edge_open_risk_pct",
        "max_open_trades_per_edge",
        "max_open_trades_per_asset",
    ]
    root_caps = manifest.get("risk_caps") or {}
    for key in cap_keys:
        if key not in variant or key not in root_caps:
            raise RuntimeError(f"risk_contract_missing_cap:{key}")
        if not math.isclose(float(variant[key]), float(root_caps[key]), rel_tol=0.0, abs_tol=1e-12):
            raise RuntimeError(f"risk_contract_cap_drift:{key}:{root_caps[key]}!={variant[key]}")

    variant_minlot = safe_float(variant.get("max_min_lot_risk_inflation"), None)
    root_minlot = safe_float(root_caps.get("max_min_lot_risk_inflation"), None)
    if (variant_minlot is None) != (root_minlot is None) or (
        variant_minlot is not None
        and not math.isclose(float(variant_minlot), float(root_minlot), rel_tol=0.0, abs_tol=1e-12)
    ):
        raise RuntimeError(
            f"risk_contract_cap_drift:max_min_lot_risk_inflation:{root_minlot}!={variant_minlot}"
        )

    m_throttle = manifest.get("throttle_config") or {}
    if not _same_tiers(m_throttle.get("throttle_tiers"), variant.get("throttle_tiers")):
        raise RuntimeError("risk_contract_throttle_tiers_drift")
    if not math.isclose(
        float(m_throttle.get("pause_below_balance", 0.0) or 0.0),
        float(variant.get("pause_below_balance", 0.0) or 0.0),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise RuntimeError("risk_contract_pause_floor_drift")

    start = float(ACCOUNT_RULES["initial_balance"])
    if EXPECTED_STARTING_CAPITAL is not None and not math.isclose(
        start, float(EXPECTED_STARTING_CAPITAL), rel_tol=0.0, abs_tol=0.01
    ):
        raise RuntimeError(f"unexpected_contract_starting_capital:{start}!={EXPECTED_STARTING_CAPITAL}")

    overall = ACCOUNT_RULES["overall_drawdown"]
    daily_cfg = ACCOUNT_RULES["daily_drawdown"]
    target_cfg = ACCOUNT_RULES["profit_target"]
    symbol_cfg = ACCOUNT_RULES["daily_symbol_risk"]
    floating_cfg = ACCOUNT_RULES["floating_loss_guard"]
    max_trade_cfg = ACCOUNT_RULES["maximum_risk_per_trade"]

    symbol_rule_enabled = bool(symbol_cfg["enabled"])
    if bool(manifest.get("daily_symbol_risk_rule_enabled", False)) != symbol_rule_enabled:
        raise RuntimeError("manifest_daily_symbol_risk_enabled_state_mismatch")
    if str(manifest.get("daily_symbol_risk_enforcement_mode") or "") != str(symbol_cfg["enforcement_mode"]):
        raise RuntimeError("manifest_daily_symbol_risk_mode_mismatch")

    stress_multiplier = max(
        1.0,
        safe_float(variant.get("symbol_risk_stress_multiplier"), DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER)
        or DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER,
    )
    legal_symbol = safe_float(symbol_cfg.get("max_amount"), None)
    internal_symbol = safe_float(symbol_cfg.get("internal_operating_amount"), legal_symbol)
    if symbol_rule_enabled and (legal_symbol is None or internal_symbol is None):
        raise RuntimeError("active_account_rules_missing_symbol_risk_limits")
    live_internal_symbol = (
        min(float(internal_symbol), float(legal_symbol) / float(stress_multiplier))
        if symbol_rule_enabled
        else None
    )

    reserve = safe_float(
        variant.get("trailing_buffer_fixed_reserve_amount"),
        safe_float(variant.get("trailing_buffer_reserve_amount"), 0.0),
    ) or 0.0
    trailing_allowance = float(variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0)
    daily_enabled = bool(daily_cfg["enabled"])
    daily_buffer_admission_enabled = safe_bool(
        variant.get("daily_buffer_admission_enabled"),
        daily_enabled and bool(variant.get("trailing_buffer_admission_enabled", False)),
    )
    daily_buffer_reserve_mode = str(
        variant.get("daily_buffer_reserve_mode")
        or variant.get("trailing_buffer_reserve_mode")
        or "off"
    )
    daily_buffer_reserve_amount = safe_float(
        variant.get("daily_buffer_fixed_reserve_amount"),
        reserve if daily_buffer_admission_enabled else 0.0,
    ) or 0.0
    daily_buffer_cost_allowance_r = safe_float(
        variant.get("daily_buffer_cost_allowance_r"),
        trailing_allowance if daily_buffer_admission_enabled else 0.0,
    ) or 0.0

    normalized = {
        "source_path": str(source_path),
        "artifact_policy": "hashless_structural_account_rules",
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "account_rule_profile": EXPECTED_ACCOUNT_PROFILE,
        "account_stage": EXPECTED_ACCOUNT_STAGE,
        "live_approval_scope": EXPECTED_APPROVAL_SCOPE,
        "daily_symbol_risk_rule_enabled": symbol_rule_enabled,
        "daily_symbol_risk_enforcement_mode": str(symbol_cfg["enforcement_mode"]),
        "starting_capital": start,
        "current_balance_reference": float(ACCOUNT_RULES["current_balance"]),
        "current_high_watermark_reference": float(ACCOUNT_RULES["current_high_watermark"]),
        "profit_target_enabled": bool(target_cfg["enabled"]),
        "profit_target_balance": safe_float(target_cfg.get("target_balance"), None),
        "minimum_trading_days": ACCOUNT_RULES["minimum_trading_days"],
        "drawdown_type": str(overall["type"]),
        "overall_drawdown_enabled": bool(overall["enabled"]),
        "overall_drawdown_breach_basis": str(overall["breach_basis"]),
        "overall_high_watermark_basis": str(overall["high_watermark_basis"]),
        "max_overall_loss_pct": float(overall.get("pct") or 0.0),
        "max_overall_loss_amount": float(overall.get("amount") or 0.0),
        "max_overall_loss_amount_at_plan_size": float(overall.get("amount") or 0.0),
        "initial_floor": float(overall_drawdown_floor(ACCOUNT_RULES, start)),
        "stop_trailing_at_balance": (
            start if bool(overall.get("trailing_stops_at_initial_balance")) else None
        ),
        "daily_drawdown_enabled": daily_enabled,
        "daily_drawdown_pct": float(daily_cfg.get("pct") or 0.0),
        "daily_drawdown_amount": float(
            daily_drawdown_amount(
                ACCOUNT_RULES,
                day_start_balance=start,
                day_start_equity=start,
            )
        ),
        "daily_drawdown_amount_at_plan_size": float(
            daily_drawdown_amount(
                ACCOUNT_RULES,
                day_start_balance=start,
                day_start_equity=start,
            )
        ),
        "daily_drawdown_basis": str(daily_cfg["mode"]),
        "daily_drawdown_breach_basis": str(daily_cfg["breach_basis"]),
        "daily_reset_timezone": str(daily_cfg["reset_timezone"]),
        "daily_reset_offset_hours": float(daily_cfg["bucket_offset_hours"]),
        "maximum_risk_per_trade_enabled": bool(max_trade_cfg["enabled"]),
        "maximum_risk_per_trade_amount": safe_float(max_trade_cfg.get("amount"), None),
        "floating_loss_guard_enabled": bool(floating_cfg["enabled"]),
        "floating_loss_guard_amount": safe_float(floating_cfg.get("amount"), None),
        "floating_loss_guard_internal_amount": safe_float(
            floating_cfg.get("internal_operating_amount"),
            safe_float(floating_cfg.get("amount"), None),
        ),
        "floating_loss_guard_action": str(floating_cfg.get("action") or "block_new_trades"),
        "internal_symbol_risk_amount": float(internal_symbol) if internal_symbol is not None else None,
        "live_internal_symbol_risk_amount": float(live_internal_symbol) if live_internal_symbol is not None else None,
        "legal_symbol_risk_amount": float(legal_symbol) if legal_symbol is not None else None,
        "symbol_risk_stress_multiplier": float(stress_multiplier),
        "symbol_risk_mode": "closed_losses_plus_floating_stop_risk",
        "trailing_buffer_admission_enabled": bool(variant.get("trailing_buffer_admission_enabled", False)),
        "trailing_buffer_reserve_mode": str(variant.get("trailing_buffer_reserve_mode") or "off"),
        "trailing_buffer_reserve_amount": float(reserve),
        "trailing_buffer_cost_allowance_r": float(trailing_allowance),
        "daily_buffer_admission_enabled": bool(daily_buffer_admission_enabled),
        "daily_buffer_reserve_mode": daily_buffer_reserve_mode,
        "daily_buffer_reserve_amount": float(daily_buffer_reserve_amount),
        "daily_buffer_cost_allowance_r": float(daily_buffer_cost_allowance_r),
        "throttle_basis": str(variant.get("throttle_basis") or "account_balance"),
        "throttle_tiers": variant.get("throttle_tiers") or [],
        "pause_below_balance": float(variant.get("pause_below_balance", 0.0) or 0.0),
        "target_lock_tiers": variant.get("target_lock_tiers") or [],
        "max_min_lot_risk_inflation": root_minlot,
        "selected_variant_name": str(variant.get("name") or ""),
        # Kept as nullable compatibility fields.  They are never execution gates.
        "risk_contract_hash": None,
        "selected_variant_contract_hash": None,
    }
    return normalized

def risk_day_window_utc(now: datetime, offset_hours: float) -> tuple[datetime, datetime, str]:
    """Return the UTC window for one broker-server calendar day.

    ``offset_hours`` is broker server time minus UTC. For example, UTC+3 uses
    ``+3`` and therefore rolls over at 21:00 UTC.
    """
    shifted = now + timedelta(hours=float(offset_hours))
    shifted_start = datetime(shifted.year, shifted.month, shifted.day, tzinfo=timezone.utc)
    start = shifted_start - timedelta(hours=float(offset_hours))
    return start, now, shifted_start.date().isoformat()


def dashboard_broker_offset_hours(manifest: dict[str, Any]) -> tuple[float | None, str]:
    """Read the dashboard's one locked account-level broker-server clock."""
    payload: dict[str, Any] | None = None
    source_name: str | None = None
    for path, label in (
        (DASHBOARD_BAR_ACK_JSON, "dashboard_bar_ack"),
        (DASHBOARD_STATUS_JSON, "dashboard_status"),
    ):
        if not path.exists():
            continue
        try:
            payload = read_json_object(path)
            source_name = label
            break
        except Exception:
            continue
    if payload is None:
        return None, "dashboard_clock_payload_missing_or_unreadable"

    account_seconds = safe_float(
        payload.get("account_broker_time_offset_seconds"),
        None,
    )
    account_locked = payload.get("account_broker_time_offset_locked")
    if account_seconds is not None and account_locked is True:
        return (
            float(account_seconds) / 3600.0,
            f"{source_name}_locked_account_broker_time_offset",
        )

    # Backward-compatible fallback for an older dashboard status. It remains
    # strict: every selected symbol must agree.
    offsets = payload.get("broker_time_offset_seconds_by_symbol") or {}
    locked = payload.get("broker_time_offset_locked_by_symbol") or {}
    if not isinstance(offsets, dict) or not isinstance(locked, dict):
        return None, f"{source_name}_offset_contract_missing"
    selected_symbols = list(universe_symbols(manifest).keys())
    values: list[float] = []
    for symbol in selected_symbols:
        if locked.get(symbol) is not True:
            continue
        seconds = safe_float(offsets.get(symbol), None)
        if seconds is not None:
            values.append(float(seconds))
    if not values:
        return None, f"{source_name}_no_locked_selected_symbol_offset"
    if max(values) - min(values) > 1.0:
        return None, f"{source_name}_inconsistent_offsets:{values}"
    return (
        (sum(values) / len(values)) / 3600.0,
        f"{source_name}_locked_per_symbol_broker_time_offset_legacy",
    )


def resolve_daily_reset_offset_hours(
    manifest: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[float, str]:
    explicit = safe_float(DAILY_RESET_OFFSET_HOURS_OVERRIDE, None)
    if explicit is not None:
        return float(explicit), "settings.PORTFOLIO_DAILY_RESET_OFFSET_HOURS"
    # The prop-account reset boundary belongs to active_account_rules.json.
    # Broker clock detection is not allowed to silently replace a non-midnight
    # prop reset such as 17:00 New York.
    return float(contract.get("daily_reset_offset_hours", 0.0) or 0.0), "active_account_rules.daily_drawdown"


def mt5_daily_trade_snapshot(
    mt5: Any,
    manifest: dict[str, Any],
    contract: dict[str, Any],
    now: datetime | None = None,
    *,
    effective_reset_offset_hours: float | None = None,
    reset_offset_source: str | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    offset = (
        float(effective_reset_offset_hours)
        if effective_reset_offset_hours is not None
        else float(contract.get("daily_reset_offset_hours", 0.0) or 0.0)
    )
    start, end, risk_day = risk_day_window_utc(now, offset)
    deals = mt5.history_deals_get(start, end)
    if deals is None:
        raise RuntimeError(f"mt5_history_deals_unavailable:{mt5.last_error()}")

    buy_type = int(getattr(mt5, "DEAL_TYPE_BUY", 0))
    sell_type = int(getattr(mt5, "DEAL_TYPE_SELL", 1))
    trading_adjustment_types = {
        int(value)
        for name in (
            "DEAL_TYPE_COMMISSION", "DEAL_TYPE_COMMISSION_DAILY",
            "DEAL_TYPE_COMMISSION_MONTHLY", "DEAL_TYPE_COMMISSION_AGENT_DAILY",
            "DEAL_TYPE_COMMISSION_AGENT_MONTHLY", "DEAL_TYPE_CHARGE",
            "DEAL_TYPE_INTEREST", "DEAL_TYPE_TAX",
        )
        if (value := getattr(mt5, name, None)) is not None
    }
    symbol_to_asset = universe_symbols(manifest)
    net_realized_trading_pnl = 0.0
    net_balance_change_since_day_start = 0.0
    separate_trading_adjustments = 0.0
    nontrading_balance_adjustments = 0.0
    closed_losses_by_asset: dict[str, float] = {}
    considered = 0
    trading_deals_considered = 0

    for deal in deals:
        deal_type = int(getattr(deal, "type", -999))
        net = sum(float(getattr(deal, key, 0.0) or 0.0) for key in ("profit", "commission", "swap", "fee"))
        # Every financial deal contributes to the actual balance movement used
        # to reconstruct the broker-day opening balance.
        net_balance_change_since_day_start += net
        considered += 1

        symbol = str(getattr(deal, "symbol", "") or "")
        if deal_type in {buy_type, sell_type}:
            net_realized_trading_pnl += net
            trading_deals_considered += 1
            if symbol and net < 0:
                asset = symbol_to_asset.get(symbol, symbol)
                closed_losses_by_asset[asset] = closed_losses_by_asset.get(asset, 0.0) + abs(net)
        elif deal_type in trading_adjustment_types:
            net_realized_trading_pnl += net
            separate_trading_adjustments += net
        else:
            nontrading_balance_adjustments += net

    return {
        # Keep legacy names for downstream symbol-risk code.
        "utc_day": risk_day,
        "utc_day_start": start.isoformat(),
        "risk_day": risk_day,
        "risk_day_start_utc": start.isoformat(),
        "risk_day_end_utc": end.isoformat(),
        "daily_reset_offset_hours": offset,
        "daily_reset_offset_source": reset_offset_source or "contract",
        "deals_considered": considered,
        "trading_deals_considered": trading_deals_considered,
        "net_realized_trading_pnl": float(net_realized_trading_pnl),
        "net_balance_change_since_day_start": float(net_balance_change_since_day_start),
        "separate_trading_adjustments": float(separate_trading_adjustments),
        "nontrading_balance_adjustments": float(nontrading_balance_adjustments),
        "closed_losses_by_asset": closed_losses_by_asset,
    }


def attach_prop_risk_context(
    mt5: Any,
    manifest: dict[str, Any],
    account: Any,
    open_risk: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Attach the live risk state produced from the generic active rules contract."""
    contract = load_prop_risk_contract(manifest)
    effective_offset, offset_source = resolve_daily_reset_offset_hours(manifest, contract)
    daily = mt5_daily_trade_snapshot(
        mt5,
        manifest,
        contract,
        effective_reset_offset_hours=effective_offset,
        reset_offset_source=offset_source,
    )

    login = str(getattr(account, "login", ""))
    server = str(getattr(account, "server", ""))
    risk_state = state.setdefault("prop_risk_state", {})
    old_login = str(risk_state.get("account_login") or "")
    old_server = str(risk_state.get("account_server") or "")
    if old_login and old_login != login:
        raise RuntimeError(f"prop_risk_state_account_login_changed:{old_login}!={login}")
    if old_server and old_server != server:
        raise RuntimeError(f"prop_risk_state_account_server_changed:{old_server}!={server}")
    stored_rules = risk_state.get("active_account_rules")
    if stored_rules is not None and not structurally_equal(stored_rules, ACCOUNT_RULE_CONTRACT):
        raise RuntimeError("prop_account_rules_changed_state_reset_required")

    balance = float(account.balance)
    broker_equity = float(account.equity)
    total_open = float(open_risk.get("total_open_risk_usd", 0.0))
    starting_capital = float(contract["starting_capital"])
    projected_equity_proxy = balance - total_open

    configured_seed = safe_float(INITIAL_EQUITY_HWM_SEED, None)
    prior_hwm = safe_float(
        risk_state.get("overall_high_watermark"),
        safe_float(
            risk_state.get("equity_high_watermark"),
            safe_float(risk_state.get("realized_balance_high_watermark"), None),
        ),
    )
    observed_hwm_value = overall_high_watermark_value(
        ACCOUNT_RULES,
        balance=balance,
        equity=broker_equity,
    )
    hwm_candidates = [
        starting_capital,
        float(ACCOUNT_RULES["current_high_watermark"]),
        observed_hwm_value,
    ]
    if configured_seed is not None:
        hwm_candidates.append(float(configured_seed))
    if prior_hwm is not None:
        hwm_candidates.append(float(prior_hwm))
    overall_hwm = max(hwm_candidates)
    hwm_source = (
        "new_live_high_watermark"
        if prior_hwm is not None and observed_hwm_value > float(prior_hwm) + 1e-9
        else "persisted_or_configured_high_watermark"
    )

    floor = float(overall_drawdown_floor(ACCOUNT_RULES, overall_hwm))
    actual_overall_breach_value = float(
        overall_breach_value(ACCOUNT_RULES, balance=balance, equity=broker_equity)
    )
    projected_overall_breach_value = float(
        overall_breach_value(
            ACCOUNT_RULES,
            balance=balance,
            equity=projected_equity_proxy,
        )
    )
    actual_overall_buffer = actual_overall_breach_value - floor
    projected_overall_buffer = projected_overall_breach_value - floor

    current_day = str(daily["risk_day"])
    anchored_day = str(risk_state.get("daily_anchor_utc_day") or "")
    anchored_balance = safe_float(risk_state.get("daily_anchor_start_balance"), None)
    anchored_equity = safe_float(risk_state.get("daily_anchor_start_equity"), None)
    anchored_offset = safe_float(risk_state.get("daily_anchor_reset_offset_hours"), None)
    offset_matches = anchored_offset is not None and math.isclose(
        float(anchored_offset), float(effective_offset), rel_tol=0.0, abs_tol=1e-9
    )
    if (
        anchored_day == current_day
        and anchored_balance is not None
        and anchored_equity is not None
        and offset_matches
    ):
        daily_start_balance = float(anchored_balance)
        daily_start_equity = float(anchored_equity)
        daily_start_source = "persisted_prop_risk_day_anchor"
    else:
        daily_start_balance = balance - float(daily["net_balance_change_since_day_start"])
        # MT5 history reconstructs opening balance exactly, but not the opening
        # floating P/L.  On the first startup inside a risk day, use the higher
        # of reconstructed balance and current equity as the conservative anchor.
        daily_start_equity = max(daily_start_balance, broker_equity)
        daily_start_source = "reconstructed_balance_conservative_equity_anchor"
        risk_state.update(
            {
                "daily_anchor_utc_day": current_day,
                "daily_anchor_start_balance": daily_start_balance,
                "daily_anchor_start_equity": daily_start_equity,
                "daily_anchor_created_at": utc_now(),
                "daily_anchor_reset_offset_hours": float(effective_offset),
            }
        )

    daily_enabled = bool(contract.get("daily_drawdown_enabled"))
    daily_limit_amount = float(
        daily_drawdown_amount(
            ACCOUNT_RULES,
            day_start_balance=daily_start_balance,
            day_start_equity=daily_start_equity,
        )
    )
    daily_floor_value = float(
        daily_drawdown_floor(
            ACCOUNT_RULES,
            day_start_balance=daily_start_balance,
            day_start_equity=daily_start_equity,
        )
    )
    actual_daily_breach_value = float(
        daily_breach_value(ACCOUNT_RULES, balance=balance, equity=broker_equity)
    )
    projected_daily_breach_value = float(
        daily_breach_value(
            ACCOUNT_RULES,
            balance=balance,
            equity=projected_equity_proxy,
        )
    )
    actual_daily_loss_remaining = (
        actual_daily_breach_value - daily_floor_value if daily_enabled else float("inf")
    )
    projected_daily_loss_remaining = (
        projected_daily_breach_value - daily_floor_value if daily_enabled else float("inf")
    )
    daily_realized_balance_change = balance - daily_start_balance
    daily_floating_pnl = broker_equity - balance
    daily_total_pnl = broker_equity - daily_start_balance
    projected_daily_total_pnl = projected_equity_proxy - daily_start_balance

    minimum_days_cfg = ACCOUNT_RULES["minimum_trading_days"]
    qualifying_days = risk_state.setdefault("qualifying_trading_days", {})
    if bool(minimum_days_cfg.get("enabled")):
        day_mode = str(minimum_days_cfg.get("day_count_mode") or "minimum_profit")
        if day_mode == "any_trade":
            qualifies = int(daily.get("trading_deals_considered", 0)) > 0
        else:
            qualifies = (
                int(daily.get("trading_deals_considered", 0)) > 0
                and float(daily.get("net_realized_trading_pnl", 0.0))
                >= float(minimum_days_cfg.get("minimum_day_profit_amount", 0.0)) - 1e-9
            )
        qualifying_days[current_day] = bool(qualifies)
    else:
        qualifying_days.clear()
    qualifying_day_count = sum(1 for value in qualifying_days.values() if value is True)
    target_eligible = target_satisfied(
        ACCOUNT_RULES,
        balance=balance,
        qualifying_day_count=qualifying_day_count,
    )

    lockouts = risk_state.setdefault("symbol_risk_lockout_utc_day_by_asset", {})
    if bool(contract.get("daily_symbol_risk_rule_enabled")):
        closed_by_asset = daily.get("closed_losses_by_asset", {}) or {}
        open_by_asset = open_risk.get("risk_by_asset", {}) or {}
        stress_mult = float(
            contract.get("symbol_risk_stress_multiplier", DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER)
        )
        legal_cap = float(contract.get("legal_symbol_risk_amount"))
        for asset in sorted(set(closed_by_asset) | set(open_by_asset)):
            closed_loss = float(closed_by_asset.get(asset, 0.0))
            open_stop = float(open_by_asset.get(asset, 0.0))
            if (
                closed_loss + open_stop >= legal_cap - 1e-9
                or closed_loss + stress_mult * open_stop >= legal_cap - 1e-9
            ):
                lockouts[str(asset)] = daily["utc_day"]
    else:
        lockouts.clear()

    actual_floating_loss = max(0.0, balance - broker_equity)
    floating_guard_enabled = bool(contract.get("floating_loss_guard_enabled"))
    floating_guard_legal = safe_float(contract.get("floating_loss_guard_amount"), None)
    floating_guard_internal = safe_float(
        contract.get("floating_loss_guard_internal_amount"), floating_guard_legal
    )
    floating_guard_actual_remaining = (
        float(floating_guard_legal) - actual_floating_loss
        if floating_guard_enabled and floating_guard_legal is not None
        else float("inf")
    )
    floating_guard_projected_remaining = (
        float(floating_guard_internal) - total_open
        if floating_guard_enabled and floating_guard_internal is not None
        else float("inf")
    )

    daily = {
        **daily,
        "daily_start_balance": daily_start_balance,
        "daily_start_equity": daily_start_equity,
        "daily_start_balance_source": daily_start_source,
        "daily_drawdown_pct": float(contract.get("daily_drawdown_pct", 0.0) or 0.0),
        "daily_limit_amount": daily_limit_amount,
        "daily_floor": daily_floor_value,
        "daily_breach_basis": ACCOUNT_RULES["daily_drawdown"]["breach_basis"],
        "daily_actual_breach_value": actual_daily_breach_value,
        "daily_projected_breach_value": projected_daily_breach_value,
        "daily_realized_balance_change": daily_realized_balance_change,
        "daily_floating_pnl": daily_floating_pnl,
        "daily_total_pnl": daily_total_pnl,
        "daily_pnl_reconciliation_error": daily_total_pnl
        - (daily_realized_balance_change + daily_floating_pnl),
        "actual_daily_loss_remaining": actual_daily_loss_remaining,
        "projected_daily_loss_remaining_after_open_stops": projected_daily_loss_remaining,
    }

    context = {
        **contract,
        "account_login": login,
        "account_server": server,
        "balance": balance,
        "broker_equity": broker_equity,
        "total_open_stop_risk": total_open,
        "realized_balance_high_watermark": overall_hwm,
        "equity_high_watermark": overall_hwm,
        "overall_high_watermark": overall_hwm,
        "equity_high_watermark_source": hwm_source,
        # Generic names used by new status/audit consumers.
        "overall_drawdown_type": str(contract["drawdown_type"]),
        "overall_drawdown_limit_amount": float(contract["max_overall_loss_amount_at_plan_size"]),
        "overall_floor": floor,
        "actual_overall_buffer": actual_overall_buffer,
        "projected_overall_buffer_after_open_stops": projected_overall_buffer,
        "additional_operator_floor_buffer_amount": float(STATIC_FLOOR_BUFFER_USD),
        # Legacy aliases retained for compatibility with old audit consumers.
        "fixed_trailing_distance_amount": float(contract["max_overall_loss_amount_at_plan_size"]),
        "trailing_distance_amount_at_hwm": float(contract["max_overall_loss_amount_at_plan_size"]),
        "raw_fixed_distance_floor_before_stop_cap": floor,
        "percentage_trailing_floor_before_stop_cap": (
            floor if contract["drawdown_type"] == "trailing_pct_high_watermark" else None
        ),
        "live_trailing_floor": floor,
        "actual_overall_breach_value": actual_overall_breach_value,
        "projected_overall_breach_value": projected_overall_breach_value,
        "actual_trailing_buffer": actual_overall_buffer,
        "projected_trailing_equity_proxy": projected_equity_proxy,
        "projected_trailing_buffer_after_open_stops": projected_overall_buffer,
        "equity_proxy": projected_equity_proxy,
        "remaining_trailing_buffer": projected_overall_buffer,
        "effective_daily_reset_offset_hours": float(effective_offset),
        "effective_daily_reset_offset_source": offset_source,
        "daily_start_balance": daily_start_balance,
        "daily_start_equity": daily_start_equity,
        "daily_start_balance_source": daily_start_source,
        "daily_drawdown_pct": float(contract.get("daily_drawdown_pct", 0.0) or 0.0),
        "daily_limit_amount": daily_limit_amount,
        "daily_floor": daily_floor_value,
        "daily_realized_balance_change": daily_realized_balance_change,
        "daily_realized_trading_pnl": float(daily.get("net_realized_trading_pnl", 0.0)),
        "daily_floating_pnl": daily_floating_pnl,
        "daily_total_pnl": daily_total_pnl,
        "daily_pnl_reconciliation_error": daily["daily_pnl_reconciliation_error"],
        "daily_actual_equity": broker_equity,
        "actual_daily_loss_remaining": actual_daily_loss_remaining,
        "daily_loss_remaining": actual_daily_loss_remaining,
        "daily_equity_proxy": projected_equity_proxy,
        "projected_daily_equity_proxy": projected_equity_proxy,
        "projected_daily_total_pnl": projected_daily_total_pnl,
        "projected_daily_loss_remaining": projected_daily_loss_remaining,
        "daily_snapshot": daily,
        "qualifying_trading_days": dict(qualifying_days),
        "qualifying_trading_day_count": qualifying_day_count,
        "target_eligible": bool(target_eligible),
        "symbol_risk_lockout_utc_day_by_asset": dict(lockouts),
        "actual_floating_loss": actual_floating_loss,
        "floating_loss_guard_actual_remaining": floating_guard_actual_remaining,
        "floating_loss_guard_projected_remaining": floating_guard_projected_remaining,
    }

    risk_state.update(
        {
            "account_login": login,
            "account_server": server,
            "active_account_rules": ACCOUNT_RULE_CONTRACT,
            "risk_contract_derivation_version": RISK_CONTRACT_DERIVATION_VERSION,
            "realized_balance_high_watermark": overall_hwm,
            "equity_high_watermark": overall_hwm,
            "overall_high_watermark": overall_hwm,
            "equity_high_watermark_source": hwm_source,
            "current_utc_day": daily["utc_day"],
            "current_risk_day": daily["risk_day"],
            "effective_daily_reset_offset_hours": float(effective_offset),
            "effective_daily_reset_offset_source": offset_source,
            "daily_start_balance": daily_start_balance,
            "daily_start_equity": daily_start_equity,
            "daily_start_balance_source": daily_start_source,
            "last_daily_drawdown_pct": float(contract.get("daily_drawdown_pct", 0.0) or 0.0),
            "last_daily_limit_amount": daily_limit_amount,
            "last_daily_floor": daily_floor_value,
            "last_daily_actual_equity": broker_equity,
            "last_actual_daily_loss_remaining": actual_daily_loss_remaining,
            "last_projected_daily_loss_remaining": projected_daily_loss_remaining,
            "last_max_overall_loss_pct": float(contract["max_overall_loss_pct"]),
            "last_fixed_trailing_distance_amount": float(contract["max_overall_loss_amount_at_plan_size"]),
            "last_trailing_floor": floor,
            "last_actual_trailing_buffer": actual_overall_buffer,
            "last_projected_trailing_buffer": projected_overall_buffer,
            "last_remaining_buffer": projected_overall_buffer,
            "last_actual_floating_loss": actual_floating_loss,
            "last_history_sync_utc": utc_now(),
        }
    )
    open_risk["prop_risk_contract"] = contract
    open_risk["prop_risk_context"] = context
    return context

def tier_multiplier(tiers: Any, reference: float, default: float = 1.0) -> float:
    if not tiers:
        return default
    parsed: list[tuple[float, float]] = []
    try:
        for item in tiers:
            if isinstance(item, dict):
                floor = safe_float(item.get("floor") or item.get("balance_floor") or item.get("threshold"), None)
                mult = safe_float(item.get("multiplier") or item.get("mult"), None)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                floor = safe_float(item[0], None)
                mult = safe_float(item[1], None)
            else:
                continue
            if floor is not None and mult is not None:
                parsed.append((floor, mult))
    except Exception:
        return default
    if not parsed:
        return default
    multiplier = default
    for floor, mult in sorted(parsed, key=lambda x: x[0], reverse=True):
        if reference < floor:
            multiplier = mult
    return float(multiplier)


def throttle_multiplier(account: Any, manifest: dict[str, Any], open_risk: dict[str, Any] | None = None) -> tuple[float, str]:
    cfg = manifest.get("throttle_config", {}) or {}
    context = (open_risk or {}).get("prop_risk_context") or {}
    balance = float(account.balance)
    if context:
        actual_buffer = safe_float(context.get("actual_trailing_buffer"), None)
        projected_buffer = safe_float(
            context.get("projected_trailing_buffer_after_open_stops"),
            safe_float(context.get("remaining_trailing_buffer"), None),
        )
        if actual_buffer is None or projected_buffer is None:
            return 0.0, "missing_dynamic_trailing_buffer_for_pause_gate"
        if actual_buffer <= 0:
            return 0.0, f"actual_trailing_drawdown_breach:{actual_buffer:.2f}<=0"
        if projected_buffer <= 0:
            return 0.0, f"projected_trailing_stop_risk_limit_reached:{projected_buffer:.2f}<=0"
    else:
        # Legacy/static fallback only when allocator-derived dynamic context is unavailable.
        pause_below = safe_float(cfg.get("pause_below_balance"), None)
        if pause_below is not None and balance < pause_below:
            return 0.0, f"paused_below_balance_floor:{balance:.2f}<{pause_below:.2f}"
    basis = str(context.get("throttle_basis") or "").strip().lower()
    overall_buffer_bases = {
        # Current generic Script 09 percentage-buffer contract.
        "remaining_overall_dd_buffer_account_currency_equity_proxy",
        # Backward-compatible legacy name.
        "remaining_trailing_dd_buffer_usd_equity_proxy",
    }
    if basis in overall_buffer_bases:
        # Script 09 throttles the conservative buffer after existing open stop
        # risk, not the absolute account balance.
        reference = safe_float(
            context.get("projected_trailing_buffer_after_open_stops"),
            safe_float(context.get("remaining_trailing_buffer"), None),
        )
        if reference is None:
            return 0.0, "missing_remaining_overall_buffer_for_throttle"
        tiers = context.get("throttle_tiers") or cfg.get("throttle_tiers")
        return tier_multiplier(tiers, float(reference), default=1.0), "ok"

    # Fail closed when an allocator-derived context declares an unknown basis.
    # Falling back to balance would silently disable percentage-buffer throttles.
    if context and basis:
        return 0.0, f"unsupported_throttle_basis:{basis}"

    # Legacy fallback only when no allocator-derived risk context exists.
    reference = min(float(account.balance), float(account.equity))
    return tier_multiplier(cfg.get("throttle_tiers"), reference, default=1.0), "legacy_balance_fallback"



def target_lock_multiplier(account: Any, manifest: dict[str, Any]) -> tuple[float, str]:
    cfg = manifest.get("target_lock_config", {}) or {}
    reference = max(float(account.balance), float(account.equity))
    return tier_multiplier(cfg.get("target_lock_tiers"), reference, default=1.0), "ok"


def compute_candidate_sizing(
    signal: dict[str, Any],
    manifest: dict[str, Any],
    edge: dict[str, Any],
    account: Any,
    open_risk: dict[str, Any],
    decommission_allocation: float = 1.0,
) -> CandidateSizing:
    root_caps = manifest.get("risk_caps", {}) or {}
    edge_caps = edge.get("risk_caps", {}) or {}
    base_pct = float(edge_caps.get("base_portfolio_risk_budget_pct", root_caps.get("base_portfolio_risk_budget_pct", 0.0)))
    max_single_pct = float(edge_caps.get("max_single_trade_risk_pct", root_caps.get("max_single_trade_risk_pct", base_pct)))
    portfolio_weight = safe_float(signal.get("portfolio_weight"), safe_float(edge.get("portfolio_weight"), None))
    if portfolio_weight is None or portfolio_weight <= 0:
        raise RuntimeError("invalid_portfolio_weight")
    afml = safe_float(signal.get("afml_bet_size"), None)
    if afml is None or afml <= 0:
        raise RuntimeError("invalid_afml_bet_size")
    throttle, throttle_reason = throttle_multiplier(account, manifest, open_risk)
    if throttle <= 0:
        raise RuntimeError(throttle_reason)
    target_lock, lock_reason = target_lock_multiplier(account, manifest)
    if target_lock <= 0:
        raise RuntimeError(lock_reason)
    target_before_cap = base_pct * float(portfolio_weight) * float(afml) * float(throttle) * float(target_lock)
    # AFML Ch.10.2/10.4 concurrency-aware sizing (2026-10-17): the allocator-selected manifest may carry a per-edge
    # divisor (q-quantile of the edge's concurrent signals). Absent / 1.0 -> unchanged sizing.
    afml_divisor = safe_float(edge_caps.get("afml_concurrency_divisor"), 1.0)
    if afml_divisor is None or not math.isfinite(float(afml_divisor)) or float(afml_divisor) < 1.0:
        raise RuntimeError(f"invalid_afml_concurrency_divisor:{edge_caps.get('afml_concurrency_divisor')}")
    if float(afml_divisor) != 1.0:
        target_before_cap = target_before_cap / float(afml_divisor)
    # Decommission/shrink allocation (AFML Ch.1 p.9; portfolio_live/decommission_rule.py): 1.0/0.8/0.6/0.4/0.2.
    if not (0.0 < float(decommission_allocation) <= 1.0):
        raise RuntimeError(f"invalid_decommission_allocation:{decommission_allocation}")
    target_before_cap = target_before_cap * float(decommission_allocation)
    target_pct = min(target_before_cap, max_single_pct)
    # Steps 08-11 size target and overlap caps from realized balance.
    risk_base = float(account.balance)
    target_usd = risk_base * target_pct
    max_single_usd = risk_base * max_single_pct

    max_total = risk_base * float(edge_caps.get("max_total_open_risk_pct", root_caps.get("max_total_open_risk_pct", 0.0)))
    max_asset = risk_base * float(edge_caps.get("max_asset_open_risk_pct", root_caps.get("max_asset_open_risk_pct", 0.0)))
    max_family = risk_base * float(edge_caps.get("max_family_open_risk_pct", root_caps.get("max_family_open_risk_pct", 0.0)))
    max_edge = risk_base * float(edge_caps.get("max_edge_open_risk_pct", root_caps.get("max_edge_open_risk_pct", 0.0)))

    asset = str(signal.get("asset"))
    family = str(signal.get("family"))
    edge_id = str(signal.get("edge_id"))
    remaining_total = max(0.0, max_total - float(open_risk.get("total_open_risk_usd", 0.0)))
    remaining_asset = max(0.0, max_asset - float(open_risk.get("risk_by_asset", {}).get(asset, 0.0)))
    remaining_family = max(0.0, max_family - float(open_risk.get("risk_by_family", {}).get(family, 0.0)))
    remaining_edge = max(0.0, max_edge - float(open_risk.get("risk_by_edge", {}).get(edge_id, 0.0)))

    return CandidateSizing(
        base_portfolio_risk_budget_pct=base_pct,
        portfolio_weight=float(portfolio_weight),
        afml_bet_size=float(afml),
        throttle_multiplier=float(throttle),
        target_lock_multiplier=float(target_lock),
        target_risk_pct_before_cap=float(target_before_cap),
        target_risk_pct=float(target_pct),
        target_risk_usd=float(target_usd),
        max_single_risk_usd=float(max_single_usd),
        remaining_total_cap_usd=float(remaining_total),
        remaining_asset_cap_usd=float(remaining_asset),
        remaining_family_cap_usd=float(remaining_family),
        remaining_edge_cap_usd=float(remaining_edge),
        decommission_allocation=float(decommission_allocation),
    )


def get_signal_price(signal: dict[str, Any], exec_plan: dict[str, Any]) -> float | None:
    for key in ["signal_price", "entry_price", "bar_close"]:
        val = safe_float(signal.get(key), None)
        if val is not None and val > 0:
            return val
    for key in ["signal_price", "entry_price"]:
        val = safe_float(exec_plan.get(key), None)
        if val is not None and val > 0:
            return val
    return None


def planned_distances(signal: dict[str, Any], exec_plan: dict[str, Any], side_value: int) -> tuple[float | None, float | None, str]:
    for src in [exec_plan, signal]:
        dsl = safe_float(src.get("planned_sl_distance_price"), None)
        dtp = safe_float(src.get("planned_tp_distance_price"), None)
        if dsl is not None and dtp is not None and dsl > 0 and dtp > 0:
            return dsl, dtp, "planned_distance_contract"
    signal_price = get_signal_price(signal, exec_plan)
    sl = safe_float(exec_plan.get("planned_stop_loss"), safe_float(signal.get("planned_stop_loss"), None))
    tp = safe_float(exec_plan.get("planned_take_profit"), safe_float(signal.get("planned_take_profit"), None))
    if signal_price is not None and sl is not None and tp is not None:
        if int(side_value) == 1:
            dsl = signal_price - sl
            dtp = tp - signal_price
        else:
            dsl = sl - signal_price
            dtp = signal_price - tp
        if dsl > 0 and dtp > 0:
            return float(dsl), float(dtp), "derived_from_planned_levels"
    return None, None, "missing_or_invalid_planned_distance"


def allowed_entry_drift_price(asset: str, static_limit: float, sl_distance: float | None) -> float:
    """Return the live entry-drift allowance for a signal.

    The default remains the static per-asset drift limit. For assets configured
    in ``PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET``, the bot may allow a
    slightly wider drift proportional to the planned SL distance, capped by
    ``PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET``. This preserves the
    existing guard while avoiding unnecessary misses on wide-barrier XAUUSD
    signals.
    """
    try:
        allowed = float(static_limit)
    except Exception:
        allowed = 999999.0

    fraction = safe_float(ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET.get(str(asset)), 0.0) or 0.0
    if fraction > 0 and sl_distance is not None and sl_distance > 0:
        allowed = max(allowed, float(sl_distance) * float(fraction))

    cap = safe_float(ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET.get(str(asset)), None)
    if cap is not None and cap > 0 and math.isfinite(cap):
        allowed = min(allowed, float(cap))

    return float(allowed)


def round_volume(volume: float, step: float, min_vol: float, max_vol: float, cap: float) -> float:
    """Return the normal ceil-to-step candidate volume.

    ``estimate_lot_and_risk`` preserves this ceil-first policy, then backs off
    only when the candidate's recalculated MT5 stop risk breaches the effective
    risk cap. This function itself does not floor normal orders.
    """
    volume = max(0.0, min(float(volume), float(max_vol), float(cap)))
    if step <= 0:
        step = 0.01

    decimals = 0
    s = f"{step:.10f}".rstrip("0")
    if "." in s:
        decimals = len(s.split(".")[-1])

    stepped = math.ceil((volume - 1e-12) / step) * step
    stepped = round(stepped, decimals)

    if stepped < min_vol:
        return 0.0
    if stepped > max_vol + 1e-12 or stepped > cap + 1e-12:
        return 0.0
    return stepped


def estimate_lot_and_risk(
    mt5: Any,
    symbol: str,
    side_value: int,
    entry_price: float,
    stop_loss: float,
    target_risk_usd: float,
    max_lot_cap: float,
    max_risk_usd: float | None = None,
) -> tuple[float, float, bool, str]:
    """Size with the existing minimum-lot fallback and ceil-first policy.

    When the normal ceil-to-step volume breaches ``max_risk_usd``, reduce the
    volume one broker step at a time until the largest safe executable volume is
    found. Reject only when no broker-valid volume fits, including the case where
    the broker minimum lot itself exceeds the effective risk cap.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0, 0.0, False, "missing_symbol_info"
    otype = order_type_for_side(mt5, side_value)
    per_lot_profit = mt5.order_calc_profit(
        otype, symbol, 1.0, float(entry_price), float(stop_loss)
    )
    if per_lot_profit is None:
        return 0.0, 0.0, False, f"order_calc_profit_failed:{mt5.last_error()}"
    per_lot_loss = max(0.0, -float(per_lot_profit))
    if per_lot_loss <= 0:
        return 0.0, 0.0, False, "non_positive_per_lot_loss"

    min_vol = float(getattr(info, "volume_min", 0.01) or 0.01)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    max_vol = float(getattr(info, "volume_max", 100.0) or 100.0)
    effective_cap = (
        float(target_risk_usd)
        if max_risk_usd is None
        else max(0.0, float(max_risk_usd))
    )
    if effective_cap <= 0:
        return 0.0, 0.0, False, "non_positive_effective_risk_cap"

    raw_lot = float(target_risk_usd) / per_lot_loss
    used_min_lot = False
    if raw_lot < min_vol:
        if not ALLOW_MIN_LOT_FALLBACK:
            return 0.0, 0.0, False, "raw_lot_below_min_and_fallback_disabled"
        volume = min_vol
        used_min_lot = True
        if volume > max_lot_cap or volume > max_vol:
            return 0.0, 0.0, True, "min_lot_above_lot_cap"
    else:
        volume = round_volume(raw_lot, step, min_vol, max_vol, max_lot_cap)
    if volume <= 0:
        return 0.0, 0.0, used_min_lot, "rounded_volume_zero"

    decimals = 0
    step_text = f"{step:.10f}".rstrip("0")
    if "." in step_text:
        decimals = len(step_text.split(".")[-1])

    while True:
        actual_profit = mt5.order_calc_profit(
            otype,
            symbol,
            float(volume),
            float(entry_price),
            float(stop_loss),
        )
        if actual_profit is None:
            return 0.0, 0.0, used_min_lot, f"actual_risk_calc_failed:{mt5.last_error()}"
        actual_risk = max(0.0, -float(actual_profit))
        if actual_risk <= 0:
            return 0.0, 0.0, used_min_lot, "non_positive_actual_risk"
        if actual_risk <= effective_cap + 1e-9:
            return float(volume), float(actual_risk), used_min_lot, "ok"

        # The minimum-lot fallback is intentionally preserved. If that smallest
        # executable lot is still above the hard cap, there is no safe trade.
        if volume <= min_vol + 1e-12:
            return (
                0.0,
                float(actual_risk),
                used_min_lot,
                f"broker_minimum_lot_exceeds_effective_risk_cap:{actual_risk:.2f}>{effective_cap:.2f}",
            )

        next_volume = round(float(volume) - step, decimals)
        if next_volume < min_vol - 1e-12:
            return (
                0.0,
                float(actual_risk),
                used_min_lot,
                f"no_broker_valid_volume_fits_effective_risk_cap:{actual_risk:.2f}>{effective_cap:.2f}",
            )
        volume = max(min_vol, next_volume)


def make_comment(asset: str, family: str, side_value: int, signal_id: str) -> str:
    """Build a short MT5-safe comment with a stable hash suffix.

    MT5 comments are too short to carry full edge_id. The full attribution is
    persisted in state maps keyed by this comment/order/deal/ticket. The hash
    suffix makes comments unique across signals, avoiding the old threshold-like
    suffix collision that could leave open positions bucketed as UNKNOWN.
    """
    side_letter = "L" if int(side_value) == 1 else "S"
    asset_code = compact_asset_code(asset)
    fam_code = compact_family_code(family)
    suffix = signal_hash_suffix(signal_id, length=6)
    return f"V2P|{asset_code}|{fam_code}|{side_letter}|{suffix}"[:31]


def hard_risk_cap_usd(sizing: CandidateSizing) -> float:
    """Smallest hard safety cap, excluding the model's desired target.

    Keeping this separate from ``target_risk_usd`` preserves the established
    broker-minimum fallback: a small model target may execute at the minimum lot
    when that lot remains below every true safety cap.
    """
    caps = [
        float(sizing.max_single_risk_usd),
        float(sizing.remaining_total_cap_usd),
        float(sizing.remaining_asset_cap_usd),
        float(sizing.remaining_family_cap_usd),
        float(sizing.remaining_edge_cap_usd),
    ]
    valid = [c for c in caps if math.isfinite(c)]
    if not valid:
        return 0.0
    return max(0.0, min(valid))


def cap_constrained_risk_target_usd(
    sizing: CandidateSizing,
    hard_cap_usd: float | None = None,
) -> float:
    """Desired target clipped by hard portfolio capacity, without redefining it."""
    hard_cap = hard_risk_cap_usd(sizing) if hard_cap_usd is None else float(hard_cap_usd)
    return max(0.0, min(float(sizing.target_risk_usd), hard_cap))


def _cap_breach_reason(
    *,
    name: str,
    actual_risk_usd: float,
    remaining_cap_usd: float,
    current_open_risk_usd: float,
) -> str | None:
    """Return absolute-vs-remaining cap breach reason.

    "Absolute" means the trade cannot fit even when the bucket has zero existing
    exposure. "Remaining" means it could fit the bucket's absolute cap, but
    current open positions have consumed too much of that bucket.
    """
    eps = 1e-9
    actual = float(actual_risk_usd)
    remaining = float(remaining_cap_usd)
    current = max(0.0, float(current_open_risk_usd))
    absolute = max(0.0, remaining + current)

    if actual <= remaining + eps:
        return None
    if current <= eps or actual > absolute + eps:
        return f"actual_risk_exceeds_absolute_{name}_cap:{actual:.2f}>{absolute:.2f}"
    return f"actual_risk_exceeds_remaining_{name}_cap:{actual:.2f}>{remaining:.2f}"


def record_spread_observation(
    state: dict[str, Any] | None,
    *,
    asset: str,
    symbol: str,
    spread_points: float,
    max_spread_points: float,
) -> None:
    if state is None:
        return
    records = state.setdefault("spread_observations_by_asset", {})
    rec = dict(records.get(asset) or {})
    count = int(rec.get("count", 0) or 0) + 1
    above = int(rec.get("above_limit_count", 0) or 0) + int(
        spread_points > max_spread_points
    )
    total = float(rec.get("sum_spread_points", 0.0) or 0.0) + float(
        spread_points
    )
    recent = list(rec.get("recent_spread_points") or [])
    recent.append(float(spread_points))
    recent = recent[-200:]
    rec.update(
        {
            "asset": asset,
            "symbol": symbol,
            "count": count,
            "above_limit_count": above,
            "above_limit_fraction": float(above / count),
            "sum_spread_points": total,
            "mean_spread_points": float(total / count),
            "min_spread_points": min(
                float(rec.get("min_spread_points", spread_points)),
                float(spread_points),
            ),
            "max_spread_points_observed": max(
                float(rec.get("max_spread_points_observed", spread_points)),
                float(spread_points),
            ),
            "configured_max_spread_points": float(max_spread_points),
            "last_spread_points": float(spread_points),
            "last_observed_at": utc_now(),
            "recent_spread_points": recent,
            "recent_median_spread_points": float(pd.Series(recent).median()),
            "recent_p95_spread_points": float(
                pd.Series(recent).quantile(0.95)
            ),
        }
    )
    records[asset] = rec


def spread_parity_guard(
    *,
    asset: str,
    spread_bps_live: float,
    state: dict[str, Any] | None,
) -> tuple[bool, str, dict[str, Any]]:
    """Research/live spread-parity guard (2026-09-18).

    Gate A/B/C/D's PASS verdicts were earned against Dukascopy's historical
    bid/ask spread (see live_settings.RESEARCH_COST_BPS_BY_ASSET, read
    verbatim from each asset's own config/<asset>_edge_search_ledger.json).
    A live prop broker's real spread can differ from Dukascopy's. This does
    not verify that -- MAX_SPREAD_POINTS_BY_ASSET is a deliberately generous
    catastrophic-blowout ceiling, unrelated to the research assumption.

    This guard compares the CURRENT live tick's spread, expressed in bps
    against that tick's own mid price (so no static bps<->points conversion
    is needed -- points would go stale as price moves; recomputing from the
    live mid price every check is the equivalent of doing that conversion
    fresh each time), against the researched cost_bps for this asset times a
    tolerance multiplier (research cost_bps is already a p95-p99 band, so
    this multiplier is on top of an already-conservative number).

    It keeps a small, BOUNDED rolling window of recent live spread_bps
    readings per asset in live state (RESEARCH_SPREAD_PARITY_WINDOW entries,
    default 20) -- never raw tick data, never unbounded, never written to a
    growing file -- and blocks NEW entries (open positions are untouched)
    while the rolling median is running materially above the researched
    threshold. It recovers automatically the next time this is called once
    the rolling median falls back under the threshold; there is no separate
    "unblock" step.

    Assets with no researched cost_bps on record are never blocked by this
    guard (there is nothing to compare against) but are flagged in the
    returned diagnostic so that gap is visible.
    """
    research_bps = RESEARCH_COST_BPS_BY_ASSET.get(asset)
    diagnostic: dict[str, Any] = {
        "asset": asset,
        "spread_bps_live": float(spread_bps_live),
        "research_cost_bps": research_bps,
    }
    if research_bps is None:
        diagnostic["status"] = "NO_RESEARCH_REFERENCE"
        return True, "spread_parity_no_research_reference", diagnostic

    multiplier = float(RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET.get(asset, RESEARCH_SPREAD_PARITY_DEFAULT_MULTIPLIER))
    parity_threshold_bps = float(research_bps) * multiplier

    window: list[float] = []
    if state is not None:
        rec = state.setdefault("spread_parity_by_asset", {}).setdefault(asset, {"recent_bps": []})
        recent = list(rec.get("recent_bps") or [])
        recent.append(float(spread_bps_live))
        recent = recent[-RESEARCH_SPREAD_PARITY_WINDOW:]
        rec["recent_bps"] = recent
        window = recent

    if len(window) >= RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS:
        effective_bps = float(pd.Series(window).median())
        basis = "rolling_median"
    else:
        effective_bps = float(spread_bps_live)
        basis = "instantaneous_cold_start"

    blocking = effective_bps > parity_threshold_bps
    diagnostic.update({
        "status": "OK",
        "multiplier": multiplier,
        "parity_threshold_bps": parity_threshold_bps,
        "effective_bps": effective_bps,
        "basis": basis,
        "window_size": len(window),
        "blocking": blocking,
    })
    if state is not None:
        state["spread_parity_by_asset"][asset].update({
            "last_effective_bps": effective_bps,
            "last_basis": basis,
            "last_checked_at": utc_now(),
            "parity_threshold_bps": parity_threshold_bps,
            "research_cost_bps": research_bps,
            "blocking": blocking,
        })

    if blocking:
        return (
            False,
            f"spread_parity_drift:{effective_bps:.2f}bps({basis})>threshold:{parity_threshold_bps:.2f}bps"
            f"(research={research_bps:.2f}bpsx{multiplier:.2f})",
            diagnostic,
        )
    return True, "spread_parity_ok", diagnostic


def per_trade_cost_guard(
    *,
    edge: dict[str, Any],
    spread_price: float,
    sl_dist: float,
) -> tuple[bool, str, dict[str, Any]]:
    """RUNBOOK v5.1 per-trade cost guard (ADDED 2026-10-16).

    Uses the edge's `per_trade_cost_guard` block written by portfolio_live/00 ONLY when the allocator was run on the
    current firm's re-priced trades (portfolio_allocator/00d). expected_net_r is the allocator's expectancy at this
    edge's selected threshold, already net of the firm's modelled costs (incl. the modelled spread modelled_spread_r).
    This trade's spread cost in R is spread / stop distance; if the EXTRA cost over what was modelled exceeds the edge's
    expected net R, the trade's expected value is <= min_net_edge_r and the entry is skipped. Absent block -> no-op.
    Open positions are never touched."""
    cfg = edge.get("per_trade_cost_guard") if isinstance(edge, dict) else None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return True, "per_trade_cost_guard_not_configured", {}
    if sl_dist <= 0 or not math.isfinite(float(spread_price)):
        return True, "per_trade_cost_guard_skipped_invalid_inputs", {}
    spread_r = float(spread_price) / float(sl_dist)
    extra_r = spread_r - float(cfg.get("modelled_spread_r", 0.0))
    headroom_r = float(cfg.get("expected_net_r", 0.0)) - float(cfg.get("min_net_edge_r", 0.0))
    diag = {"spread_r": round(spread_r, 6), "modelled_spread_r": cfg.get("modelled_spread_r"), "extra_r": round(extra_r, 6),
            "expected_net_r": cfg.get("expected_net_r"), "headroom_r": round(headroom_r, 6), "firm": cfg.get("firm")}
    if extra_r > headroom_r:
        return False, f"per_trade_cost_guard:extra_spread_cost {extra_r:.4f}R > expected_net {headroom_r:.4f}R", diag
    return True, "per_trade_cost_guard_ok", diag


def edge_decommission_allocation(signal: dict[str, Any]) -> tuple[float, str]:
    """Allocation multiplier for the signal's edge from the decommission rule.

    Returns (allocation, reason): (1.0..0.2, "ok") to trade, (0.0, "decommission_off") for an
    edge the rule switched off (its signal becomes a shadow bet), (0.0, other) to fail closed.
    """
    try:
        import decommission_rule

        allocations = decommission_rule.live_allocations()
    except Exception as exc:  # no current or last-good verdicts -> fail closed
        return 0.0, f"decommission_rule_unavailable:{type(exc).__name__}"
    record = allocations.get(str(signal.get("edge_id")))
    if record is None:
        return 0.0, "decommission_rule_no_verdict_for_edge"
    if record.get("verdict") == "off":
        return 0.0, "decommission_off"
    if record.get("verdict") == "blocked":
        return 0.0, "decommission_rule_blocked"
    allocation = safe_float(record.get("allocation"), None)
    if allocation is None or not (0.0 < allocation <= 1.0):
        return 0.0, f"decommission_rule_invalid_allocation:{record.get('allocation')}"
    return float(allocation), "ok"


def build_order_plan(
    mt5: Any,
    signal: dict[str, Any],
    exec_plan: dict[str, Any],
    manifest: dict[str, Any],
    edge: dict[str, Any],
    account: Any,
    open_risk: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> tuple[OrderPlan | None, str]:
    asset = str(signal.get("asset"))
    # AFML Ch.1 p.9 re-allocation / decommission, applied automatically (D021, D022).
    decommission_allocation, decommission_reason = edge_decommission_allocation(signal)
    if decommission_allocation <= 0.0:
        if decommission_reason == "decommission_off":
            import shadow_bets

            shadow_bets.record_shadow_signal(signal, decommission_reason)
            return None, "decommission_off_shadow_tracking"
        return None, decommission_reason
    if REQUIRE_EXPLICIT_EXECUTION_GUARDS:
        missing_guard_sets = [
            name for name, mapping in (
                ("max_spread", MAX_SPREAD_POINTS_BY_ASSET),
                ("entry_drift", ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET),
                ("entry_drift_cap", ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET),
                ("deviation", DEVIATION_POINTS_BY_ASSET),
                ("max_lot", MAX_LOT_CAP_BY_ASSET),
            ) if asset not in mapping
        ]
        if missing_guard_sets:
            return None, f"missing_explicit_execution_guards:{asset}:{','.join(missing_guard_sets)}"
    symbol = str(signal.get("mt5_symbol") or settings.MT5_SYMBOL_MAP.get(asset, asset))
    family = str(signal.get("family") or edge.get("family") or "UNKNOWN")
    edge_id = str(signal.get("edge_id"))
    side_value = int(signal.get("side_value"))
    side_name = side_name_from_value(side_value)

    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if tick is None or info is None:
        return None, "missing_tick_or_symbol_info"
    market_ok, market_reason = market_entry_guard(
        mt5,
        asset=asset,
        symbol=symbol,
        side_value=side_value,
        info=info,
        tick=tick,
        state=state,
    )
    if not market_ok:
        return None, market_reason
    point = float(getattr(info, "point", 0.0) or 0.0)
    if point <= 0:
        return None, "invalid_symbol_point"

    spread_points = float((tick.ask - tick.bid) / point)
    max_spread = float(MAX_SPREAD_POINTS_BY_ASSET.get(asset, 999999.0))
    record_spread_observation(
        state,
        asset=asset,
        symbol=symbol,
        spread_points=spread_points,
        max_spread_points=max_spread,
    )
    if spread_points > max_spread:
        return None, f"spread_too_high:{spread_points:.1f}>{max_spread:.1f}"

    mid_price = (float(tick.ask) + float(tick.bid)) / 2.0
    if mid_price > 0:
        spread_bps_live = (float(tick.ask) - float(tick.bid)) / mid_price * 10000.0
        parity_ok, parity_reason, parity_diagnostic = spread_parity_guard(
            asset=asset, spread_bps_live=spread_bps_live, state=state,
        )
        if state is not None:
            state["last_spread_parity_diagnostic"] = parity_diagnostic
        if not parity_ok:
            return None, parity_reason

    signal_price = get_signal_price(signal, exec_plan)
    if signal_price is None:
        return None, "missing_signal_price"

    sl_dist, tp_dist, dist_source = planned_distances(signal, exec_plan, side_value)
    if sl_dist is None or tp_dist is None or sl_dist <= 0 or tp_dist <= 0:
        return None, dist_source

    cost_ok, cost_reason, cost_diag = per_trade_cost_guard(
        edge=edge, spread_price=float(tick.ask) - float(tick.bid), sl_dist=float(sl_dist),
    )
    if cost_diag and state is not None:
        state["last_per_trade_cost_guard_diagnostic"] = {**cost_diag, "edge_id": edge_id, "reason": cost_reason}
    if not cost_ok:
        return None, cost_reason

    entry_price = float(tick.ask if side_value == 1 else tick.bid)
    drift = abs(entry_price - signal_price)
    static_drift_limit = float(ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET.get(asset, 999999.0))
    drift_limit = allowed_entry_drift_price(asset, static_drift_limit, sl_dist)
    if drift > drift_limit:
        return None, f"entry_drift_too_large:{drift:.5f}>{drift_limit:.5f}"

    stops_level_points = float(getattr(info, "trade_stops_level", 0) or 0)
    min_stop_distance = stops_level_points * point
    if ENFORCE_BROKER_MIN_STOP_LEVEL and min_stop_distance > 0 and sl_dist < min_stop_distance:
        if ALLOW_WIDEN_TO_BROKER_MIN_STOP:
            sl_dist = min_stop_distance
        else:
            return None, f"planned_stop_inside_broker_min_stop:{sl_dist:.5f}<{min_stop_distance:.5f}"

    if side_value == 1:
        stop_loss = entry_price - sl_dist
        take_profit = entry_price + tp_dist
    else:
        stop_loss = entry_price + sl_dist
        take_profit = entry_price - tp_dist
    digits = int(getattr(info, "digits", 2) or 2)
    entry_price = round(float(entry_price), digits)
    stop_loss = round(float(stop_loss), digits)
    take_profit = round(float(take_profit), digits)

    sizing = compute_candidate_sizing(signal, manifest, edge, account, open_risk, decommission_allocation)
    if sizing.target_risk_usd <= 0:
        return None, "non_positive_target_risk_usd"

    risk_base = float(account.balance)
    effective_hard_risk_cap_usd = hard_risk_cap_usd(sizing)
    cap_target_risk_usd = cap_constrained_risk_target_usd(
        sizing,
        effective_hard_risk_cap_usd,
    )
    risk_context = open_risk.get("prop_risk_context") or {}
    contract = open_risk.get("prop_risk_contract") or {}
    if bool(contract.get("maximum_risk_per_trade_enabled")):
        prop_trade_cap = safe_float(contract.get("maximum_risk_per_trade_amount"), None)
        if prop_trade_cap is None or prop_trade_cap <= 0:
            return None, "invalid_active_account_maximum_risk_per_trade"
        effective_hard_risk_cap_usd = min(effective_hard_risk_cap_usd, float(prop_trade_cap))
    if risk_context:
        actual_trailing_buffer = float(risk_context.get("actual_trailing_buffer", 0.0))
        projected_trailing_buffer_before = float(
            risk_context.get(
                "projected_trailing_buffer_after_open_stops",
                risk_context.get("remaining_trailing_buffer", 0.0),
            )
        )
        if actual_trailing_buffer <= 0:
            return None, "actual_max_trailing_drawdown_breach"
        if projected_trailing_buffer_before <= 0:
            return None, "projected_trailing_stop_risk_limit_reached"
        if bool(contract.get("daily_drawdown_enabled")):
            if float(risk_context.get("actual_daily_loss_remaining", risk_context.get("daily_loss_remaining", 0.0))) <= 0:
                return None, "actual_daily_loss_limit_reached"
            if float(risk_context.get("projected_daily_loss_remaining", 0.0)) <= 0:
                return None, "projected_daily_stop_risk_limit_reached"
        if bool(contract.get("floating_loss_guard_enabled")):
            if float(risk_context.get("floating_loss_guard_actual_remaining", 0.0)) <= 0:
                return None, "actual_floating_loss_guard_reached"
            projected_guard_remaining = float(
                risk_context.get("floating_loss_guard_projected_remaining", 0.0)
            )
            if projected_guard_remaining <= 0:
                return None, "projected_floating_loss_guard_reached"
            effective_hard_risk_cap_usd = min(
                effective_hard_risk_cap_usd, projected_guard_remaining
            )
        closed_losses = float((risk_context.get("daily_snapshot") or {}).get("closed_losses_by_asset", {}).get(asset, 0.0))
        current_symbol_open = float(open_risk.get("risk_by_asset", {}).get(asset, 0.0))
        current_symbol_usage = closed_losses + current_symbol_open
        if bool(contract.get("daily_symbol_risk_rule_enabled")):
            current_day = str((risk_context.get("daily_snapshot") or {}).get("utc_day") or "")
            lockout_day = str((risk_context.get("symbol_risk_lockout_utc_day_by_asset") or {}).get(asset) or "")
            if current_day and lockout_day == current_day: return None, f"legal_daily_symbol_risk_lockout:{asset}:{current_day}"
            stress_mult = float(contract.get("symbol_risk_stress_multiplier", DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER))
            current_stressed_symbol_usage = closed_losses + stress_mult * current_symbol_open
            legal_symbol_cap = float(contract.get("legal_symbol_risk_amount")); live_internal_cap = float(contract.get("live_internal_symbol_risk_amount"))
            if current_symbol_usage >= legal_symbol_cap - 1e-9 or current_stressed_symbol_usage >= legal_symbol_cap - 1e-9:
                return None, f"legal_daily_symbol_risk_already_reached:raw={current_symbol_usage:.2f},stressed={current_stressed_symbol_usage:.2f}>={legal_symbol_cap:.2f}"
            if current_symbol_usage >= live_internal_cap - 1e-9: return None, f"internal_daily_symbol_risk_already_reached:{current_symbol_usage:.2f}>={live_internal_cap:.2f}"
        if bool(contract.get("trailing_buffer_admission_enabled")):
            remaining_buffer = float(
                risk_context.get(
                    "projected_trailing_buffer_after_open_stops",
                    risk_context.get("remaining_trailing_buffer", 0.0),
                )
            )
            fixed_reserve = float(contract.get("trailing_buffer_reserve_amount", 0.0))
            allowance = max(0.0, float(contract.get("trailing_buffer_cost_allowance_r", 0.0)))
            current_open = float(open_risk.get("total_open_risk_usd", 0.0))
            reserve_ceiling = (remaining_buffer - fixed_reserve - allowance * current_open) / (1.0 + allowance)
            effective_hard_risk_cap_usd = min(
                effective_hard_risk_cap_usd,
                max(0.0, reserve_ceiling),
            )
        if bool(contract.get("daily_drawdown_enabled")):
            projected_daily_remaining = max(
                0.0,
                float(
                    risk_context.get(
                        "projected_daily_loss_remaining",
                        risk_context.get("daily_loss_remaining", 0.0),
                    )
                ),
            )
            daily_ceiling = projected_daily_remaining
            if bool(contract.get("daily_buffer_admission_enabled")):
                daily_fixed = float(contract.get("daily_buffer_reserve_amount", 0.0))
                daily_allowance = max(
                    0.0,
                    float(contract.get("daily_buffer_cost_allowance_r", 0.0)),
                )
                current_open = float(open_risk.get("total_open_risk_usd", 0.0))
                daily_ceiling = (
                    projected_daily_remaining
                    - daily_fixed
                    - daily_allowance * current_open
                ) / (1.0 + daily_allowance)
            effective_hard_risk_cap_usd = min(
                effective_hard_risk_cap_usd,
                max(0.0, daily_ceiling),
            )
        if bool(contract.get("daily_symbol_risk_rule_enabled")):
            raw_symbol_ceiling = live_internal_cap - current_symbol_usage
            stressed_legal_ceiling = ((legal_symbol_cap - closed_losses) / stress_mult) - current_symbol_open
            effective_hard_risk_cap_usd = min(
                effective_hard_risk_cap_usd,
                max(0.0, min(raw_symbol_ceiling, stressed_legal_ceiling)),
            )
    if effective_hard_risk_cap_usd <= 0:
        return None, "no_remaining_risk_capacity"
    cap_target_risk_usd = min(
        cap_target_risk_usd,
        effective_hard_risk_cap_usd,
    )
    if cap_target_risk_usd <= 0:
        return None, "non_positive_cap_constrained_target_risk"
    cap_target_risk_pct = cap_target_risk_usd / risk_base if risk_base > 0 else 0.0

    max_lot_cap = float(MAX_LOT_CAP_BY_ASSET.get(asset, 100.0))
    volume, actual_risk_usd, used_min_lot, volume_reason = estimate_lot_and_risk(
        mt5=mt5,
        symbol=symbol,
        side_value=side_value,
        entry_price=entry_price,
        stop_loss=stop_loss,
        target_risk_usd=cap_target_risk_usd,
        max_lot_cap=max_lot_cap,
        max_risk_usd=effective_hard_risk_cap_usd,
    )
    if volume_reason != "ok":
        return None, volume_reason
    max_min_lot_inflation = safe_float(contract.get("max_min_lot_risk_inflation"), None)
    observed_inflation = (actual_risk_usd / cap_target_risk_usd) if used_min_lot and cap_target_risk_usd > 0 else None
    if used_min_lot and max_min_lot_inflation is not None and observed_inflation is not None:
        if observed_inflation > max_min_lot_inflation + 1e-12:
            return None, (
                "minimum_lot_risk_inflation_guard:"
                f"{observed_inflation:.6f}>{max_min_lot_inflation:.6f}"
            )

    actual_risk_pct = actual_risk_usd / risk_base if risk_base > 0 else 0.0
    if actual_risk_usd > sizing.max_single_risk_usd + 1e-9:
        return None, f"actual_risk_exceeds_max_single:{actual_risk_usd:.2f}>{sizing.max_single_risk_usd:.2f}"

    current_total_risk = float(open_risk.get("total_open_risk_usd", 0.0))
    current_asset_risk = float(open_risk.get("risk_by_asset", {}).get(asset, 0.0))
    current_family_risk = float(open_risk.get("risk_by_family", {}).get(family, 0.0))
    current_edge_risk = float(open_risk.get("risk_by_edge", {}).get(edge_id, 0.0))

    breach = _cap_breach_reason(
        name="total",
        actual_risk_usd=actual_risk_usd,
        remaining_cap_usd=sizing.remaining_total_cap_usd,
        current_open_risk_usd=current_total_risk,
    )
    if breach:
        return None, breach
    breach = _cap_breach_reason(
        name="asset",
        actual_risk_usd=actual_risk_usd,
        remaining_cap_usd=sizing.remaining_asset_cap_usd,
        current_open_risk_usd=current_asset_risk,
    )
    if breach:
        return None, breach
    breach = _cap_breach_reason(
        name="family",
        actual_risk_usd=actual_risk_usd,
        remaining_cap_usd=sizing.remaining_family_cap_usd,
        current_open_risk_usd=current_family_risk,
    )
    if breach:
        return None, breach
    breach = _cap_breach_reason(
        name="edge",
        actual_risk_usd=actual_risk_usd,
        remaining_cap_usd=sizing.remaining_edge_cap_usd,
        current_open_risk_usd=current_edge_risk,
    )
    if breach:
        return None, breach

    actual_trailing_buffer_before = None
    projected_trailing_buffer_before = None
    projected_buffer = None
    required_reserve = None
    projected_daily_equity_proxy = None
    daily_loss_remaining_before = None
    actual_daily_loss_remaining_before = None
    projected_daily_loss_remaining_before = None
    symbol_closed_losses = None
    projected_symbol_usage = None
    projected_stressed_symbol_usage = None
    symbol_risk_stress_multiplier = None
    live_internal_symbol_risk_amount = None
    legal_symbol_risk_amount = None
    if risk_context:
        fixed_reserve = float(contract.get("trailing_buffer_reserve_amount", 0.0))
        allowance = max(0.0, float(contract.get("trailing_buffer_cost_allowance_r", 0.0)))
        projected_open_risk = float(open_risk.get("total_open_risk_usd", 0.0)) + actual_risk_usd
        required_reserve = fixed_reserve + allowance * projected_open_risk
        actual_trailing_buffer_before = float(risk_context.get("actual_trailing_buffer", 0.0))
        projected_trailing_buffer_before = float(
            risk_context.get(
                "projected_trailing_buffer_after_open_stops",
                risk_context.get("remaining_trailing_buffer", 0.0),
            )
        )
        projected_buffer = projected_trailing_buffer_before - actual_risk_usd
        if bool(contract.get("trailing_buffer_admission_enabled")) and projected_buffer < required_reserve - 1e-9:
            return None, f"projected_trailing_buffer_below_reserve:{projected_buffer:.2f}<{required_reserve:.2f}"
        actual_daily_loss_remaining_before = float(
            risk_context.get("actual_daily_loss_remaining", risk_context.get("daily_loss_remaining", 0.0))
        )
        projected_daily_loss_remaining_before = float(risk_context.get("projected_daily_loss_remaining", 0.0))
        # Backward-compatible alias retained for existing audit consumers.
        daily_loss_remaining_before = projected_daily_loss_remaining_before
        projected_daily_equity_proxy = float(risk_context.get("daily_equity_proxy", 0.0)) - actual_risk_usd
        daily_floor_value = float(risk_context.get("daily_floor", 0.0))
        projected_daily_buffer = projected_daily_equity_proxy - daily_floor_value
        if bool(contract.get("daily_buffer_admission_enabled")):
            daily_fixed = float(contract.get("daily_buffer_reserve_amount", 0.0))
            daily_allowance = max(
                0.0,
                float(contract.get("daily_buffer_cost_allowance_r", 0.0)),
            )
            required_daily_reserve = daily_fixed + daily_allowance * projected_open_risk
            if projected_daily_buffer < required_daily_reserve - 1e-9:
                return None, (
                    "projected_daily_buffer_below_reserve:"
                    f"{projected_daily_buffer:.2f}<{required_daily_reserve:.2f}"
                )
        elif bool(contract.get("daily_drawdown_enabled")) and projected_daily_equity_proxy < daily_floor_value - 1e-9:
            return None, f"projected_daily_stop_risk_breach:{projected_daily_equity_proxy:.2f}<{daily_floor_value:.2f}"
        symbol_closed_losses = float((risk_context.get("daily_snapshot") or {}).get("closed_losses_by_asset", {}).get(asset, 0.0))
        projected_symbol_usage = symbol_closed_losses + current_asset_risk + actual_risk_usd
        if bool(contract.get("daily_symbol_risk_rule_enabled")):
            symbol_risk_stress_multiplier = float(contract.get("symbol_risk_stress_multiplier", DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER))
            legal_symbol_risk_amount = float(contract.get("legal_symbol_risk_amount")); live_internal_symbol_risk_amount = float(contract.get("live_internal_symbol_risk_amount"))
            projected_stressed_symbol_usage = symbol_closed_losses + symbol_risk_stress_multiplier * (current_asset_risk + actual_risk_usd)
            if projected_symbol_usage > legal_symbol_risk_amount + 1e-9 or projected_stressed_symbol_usage > legal_symbol_risk_amount + 1e-9:
                return None, f"projected_legal_daily_symbol_risk_breach:raw={projected_symbol_usage:.2f},stressed={projected_stressed_symbol_usage:.2f}>{legal_symbol_risk_amount:.2f}"
            if projected_symbol_usage > live_internal_symbol_risk_amount + 1e-9: return None, f"projected_internal_daily_symbol_risk_cap:{projected_symbol_usage:.2f}>{live_internal_symbol_risk_amount:.2f}"

    root_caps = manifest.get("risk_caps", {}) or {}
    edge_caps = edge.get("risk_caps", {}) or {}
    count_asset = int(open_risk.get("count_by_asset", {}).get(asset, 0))
    count_edge = int(open_risk.get("count_by_edge", {}).get(edge_id, 0))
    max_asset_trades = int(edge_caps.get("max_open_trades_per_asset", root_caps.get("max_open_trades_per_asset", 999999)))
    # Position-count authority remains with the selected live manifest produced
    # by the allocator.  The executor enforces that contract; it does not add a
    # second, stricter portfolio policy of its own.
    max_edge_trades = int(
        edge_caps.get(
            "max_open_trades_per_edge",
            root_caps.get("max_open_trades_per_edge", 999999),
        )
    )
    if count_asset >= max_asset_trades:
        return None, f"max_open_trades_per_asset_reached:{asset}:{count_asset}>={max_asset_trades}"
    if count_edge >= max_edge_trades:
        return None, f"max_open_trades_per_edge_reached:{edge_id}:{count_edge}>={max_edge_trades}"

    floor = safe_float(risk_context.get("live_trailing_floor"), None) if risk_context else None
    target_balance = safe_float(contract.get("profit_target_balance"), None) if risk_context else None
    if floor is not None and math.isfinite(float(floor)):
        worst_case_balance = float(account.balance) - actual_risk_usd
        worst_case_equity = (
            float(account.balance)
            - float(open_risk.get("total_open_risk_usd", 0.0))
            - actual_risk_usd
        )
        worst_case_breach_value = overall_breach_value(
            ACCOUNT_RULES, balance=worst_case_balance, equity=worst_case_equity
        )
        if worst_case_breach_value <= float(floor) + STATIC_FLOOR_BUFFER_USD:
            return None, (
                "worst_case_account_value_too_close_to_floor:"
                f"{worst_case_breach_value:.2f}<={float(floor) + STATIC_FLOOR_BUFFER_USD:.2f}"
            )
    if BLOCK_NEW_TRADES_AT_OR_ABOVE_TARGET and target_balance is not None:
        if bool(risk_context.get("target_eligible", False)):
            return None, (
                f"target_balance_and_minimum_days_reached:{target_balance:.2f}:"
                f"qualifying_days={risk_context.get('qualifying_trading_day_count', 0)}"
            )

    otype = order_type_for_side(mt5, side_value)
    return OrderPlan(
        signal_id=str(signal.get("signal_id")),
        edge_id=edge_id,
        asset=asset,
        symbol=symbol,
        family=family,
        side_name=side_name,
        side_value=side_value,
        order_type_name="BUY" if side_value == 1 else "SELL",
        order_type=otype,
        entry_price=entry_price,
        signal_price=float(signal_price),
        stop_loss=stop_loss,
        take_profit=take_profit,
        sl_distance=float(sl_dist),
        tp_distance=float(tp_dist),
        model_target_risk_pct=sizing.target_risk_pct,
        model_target_risk_usd=sizing.target_risk_usd,
        target_risk_pct=cap_target_risk_pct,
        target_risk_usd=cap_target_risk_usd,
        actual_risk_usd=actual_risk_usd,
        actual_risk_pct=actual_risk_pct,
        volume=volume,
        used_min_lot_fallback=used_min_lot,
        max_min_lot_risk_inflation=max_min_lot_inflation,
        observed_min_lot_risk_inflation=observed_inflation,
        spread_points=spread_points,
        entry_drift_price=drift,
        deviation_points=int(DEVIATION_POINTS_BY_ASSET.get(asset, 100)),
        magic=MAGIC_NUMBER,
        comment=make_comment(asset, family, side_value, str(signal.get("signal_id"))),
        type_filling=get_fill_type(mt5, symbol),
        geometry_hash=str(exec_plan.get("geometry_hash") or "") or None,
        pt_multiplier=float(exec_plan.get("pt_multiplier")),
        sl_multiplier=float(exec_plan.get("sl_multiplier")),
        reward_risk_ratio=float(exec_plan.get("reward_risk_ratio") or (float(exec_plan.get("pt_multiplier")) / float(exec_plan.get("sl_multiplier")))),
        vertical_horizon_bars=safe_int(exec_plan.get("vertical_horizon_bars"), None),
        calendar_time=bool(exec_plan.get("calendar_time", False)),
        vertical_horizon_hours=safe_float(exec_plan.get("vertical_horizon_hours"), None),
        risk_contract_hash=str(contract.get("risk_contract_hash") or "") or None,
        trailing_floor=safe_float(risk_context.get("live_trailing_floor"), None),
        actual_trailing_buffer_before=actual_trailing_buffer_before,
        remaining_trailing_buffer_before=projected_trailing_buffer_before,
        projected_trailing_buffer_before=projected_trailing_buffer_before,
        projected_trailing_buffer_after=projected_buffer,
        required_trailing_buffer_reserve=required_reserve,
        daily_loss_remaining_before=daily_loss_remaining_before,
        actual_daily_loss_remaining_before=actual_daily_loss_remaining_before,
        projected_daily_loss_remaining_before=projected_daily_loss_remaining_before,
        projected_daily_equity_proxy=projected_daily_equity_proxy,
        symbol_closed_losses_usd=symbol_closed_losses,
        projected_symbol_risk_usd=projected_symbol_usage,
        symbol_risk_stress_multiplier=symbol_risk_stress_multiplier,
        projected_stressed_symbol_risk_usd=projected_stressed_symbol_usage,
        live_internal_symbol_risk_amount=live_internal_symbol_risk_amount,
        legal_symbol_risk_amount=legal_symbol_risk_amount,
        execution_plan_hash=str(signal.get("execution_plan_hash") or exec_plan.get("execution_plan_hash") or "") or None,
        live_manifest_hash=str(signal.get("live_manifest_hash") or "") or None,
        edge_manifest_hash=str(signal.get("edge_manifest_hash") or "") or None,
        bar_policy_hash=str(signal.get("bar_policy_hash") or "") or None,
    ), "ok"


def audit_row(
    stage: str,
    status: str,
    reason: str,
    signal: dict[str, Any] | None = None,
    plan: OrderPlan | None = None,
    account: Any = None,
    open_risk: dict[str, Any] | None = None,
    result: Any = None,
    error: str | None = None,
    live_armed: bool | None = None,
    live_blockers: list[str] | None = None,
) -> dict[str, Any]:
    sig = signal or {}
    clock = dict(sig.get("_broker_clock_diagnostic") or {})
    row = {
        "created_at": utc_now(),
        "bot_version": BOT_VERSION,
        "execution_mode": EXECUTION_MODE,
        "enable_order_send": ENABLE_ORDER_SEND,
        "live_armed": live_armed,
        "live_blockers": "; ".join(live_blockers or []),
        "stage": stage,
        "status": status,
        "reason": reason,
        "signal_id": sig.get("signal_id"),
        "event_time": sig.get("event_time"),
        "edge_id": sig.get("edge_id"),
        "asset": sig.get("asset"),
        "symbol": sig.get("mt5_symbol"),
        "family": sig.get("family"),
        "side_value": sig.get("side_value"),
        "calibrated_probability": sig.get("calibrated_probability"),
        "final_live_threshold": sig.get("final_live_threshold"),
        "afml_bet_size": sig.get("afml_bet_size"),
        "portfolio_weight": sig.get("portfolio_weight"),
        "signal_price": sig.get("signal_price"),
        "execution_plan_ready": sig.get("execution_plan_ready"),
        "execution_plan_hash": sig.get("execution_plan_hash"),
        "live_manifest_hash": sig.get("live_manifest_hash"),
        "edge_manifest_hash": sig.get("edge_manifest_hash"),
        "bar_policy_hash": sig.get("bar_policy_hash"),
        "broker_clock_status": clock.get("status"),
        "broker_clock_reason_class": clock.get("reason_class"),
        "broker_clock_raw_timestamp": clock.get("raw_broker_timestamp"),
        "broker_clock_normalised_timestamp": clock.get("normalised_tick_timestamp"),
        "broker_clock_offset_seconds": clock.get("offset_seconds"),
        "broker_clock_generation_id": clock.get("contract_generation_id"),
        "broker_clock_tick_age_seconds": clock.get("tick_age_seconds"),
        "broker_clock_error": clock.get("error"),
        "account_balance": getattr(account, "balance", None) if account is not None else None,
        "account_equity": getattr(account, "equity", None) if account is not None else None,
        "open_risk_total_usd": (open_risk or {}).get("total_open_risk_usd"),
        "unknown_open_risk_count": len((open_risk or {}).get("unknown_risk_positions", [])),
        "unknown_pending_risk_count": len((open_risk or {}).get("unknown_pending_orders", [])),
        "managed_portfolio_risk_usd": (open_risk or {}).get("managed_portfolio_risk_usd"),
        "external_position_risk_usd": (open_risk or {}).get("external_position_risk_usd"),
        "pending_order_contingent_risk_usd": (open_risk or {}).get("pending_order_contingent_risk_usd"),
        "risk_contract_hash": ((open_risk or {}).get("prop_risk_context") or {}).get("risk_contract_hash"),
        "account_rule_profile": ((open_risk or {}).get("prop_risk_context") or {}).get("account_rule_profile"),
        "realized_balance_high_watermark": ((open_risk or {}).get("prop_risk_context") or {}).get("realized_balance_high_watermark"),
        "equity_high_watermark": ((open_risk or {}).get("prop_risk_context") or {}).get("equity_high_watermark"),
        "max_overall_loss_pct": ((open_risk or {}).get("prop_risk_context") or {}).get("max_overall_loss_pct"),
        "trailing_distance_amount_at_hwm": ((open_risk or {}).get("prop_risk_context") or {}).get("trailing_distance_amount_at_hwm"),
        "live_trailing_floor": ((open_risk or {}).get("prop_risk_context") or {}).get("live_trailing_floor"),
        "actual_trailing_buffer": ((open_risk or {}).get("prop_risk_context") or {}).get("actual_trailing_buffer"),
        "projected_trailing_equity_proxy": ((open_risk or {}).get("prop_risk_context") or {}).get("projected_trailing_equity_proxy"),
        "projected_trailing_buffer_after_open_stops": ((open_risk or {}).get("prop_risk_context") or {}).get("projected_trailing_buffer_after_open_stops"),
        "remaining_trailing_buffer": ((open_risk or {}).get("prop_risk_context") or {}).get("remaining_trailing_buffer"),
        "daily_start_balance": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_start_balance"),
        "daily_start_balance_source": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_start_balance_source"),
        "daily_drawdown_pct": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_drawdown_pct"),
        "daily_limit_amount": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_limit_amount"),
        "daily_floor": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_floor"),
        "daily_realized_balance_change": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_realized_balance_change"),
        "daily_realized_trading_pnl": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_realized_trading_pnl"),
        "daily_floating_pnl": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_floating_pnl"),
        "daily_total_pnl": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_total_pnl"),
        "daily_actual_equity": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_actual_equity"),
        "actual_daily_loss_remaining": ((open_risk or {}).get("prop_risk_context") or {}).get("actual_daily_loss_remaining"),
        "daily_loss_remaining": ((open_risk or {}).get("prop_risk_context") or {}).get("daily_loss_remaining"),
        "projected_daily_equity_proxy": ((open_risk or {}).get("prop_risk_context") or {}).get("projected_daily_equity_proxy"),
        "projected_daily_loss_remaining": ((open_risk or {}).get("prop_risk_context") or {}).get("projected_daily_loss_remaining"),
        "retcode": getattr(result, "retcode", None) if result is not None else None,
        "broker_comment": getattr(result, "comment", None) if result is not None else None,
        "order": getattr(result, "order", None) if result is not None else None,
        "deal": getattr(result, "deal", None) if result is not None else None,
        "error": error,
    }
    if plan is not None:
        row.update({f"plan_{k}": v for k, v in asdict(plan).items()})
    return row


def write_status(
    state: dict[str, Any],
    account: Any,
    open_risk: dict[str, Any],
    live_armed: bool,
    live_blockers: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "updated_at": utc_now(),
        "bot_version": BOT_VERSION,
        "execution_mode": EXECUTION_MODE,
        "enable_order_send": ENABLE_ORDER_SEND,
        "real_order_send_armed": live_armed,
        "live_blockers": live_blockers,
        "approved_signals_csv": str(APPROVED_SIGNALS_CSV),
        "approved_signals_jsonl": str(APPROVED_SIGNALS_JSONL),
        "signal_transport": _SIGNAL_FEED.snapshot(),
        "account": {
            "login": getattr(account, "login", None) if account is not None else None,
            "balance": getattr(account, "balance", None) if account is not None else None,
            "equity": getattr(account, "equity", None) if account is not None else None,
            "margin_free": getattr(account, "margin_free", None) if account is not None else None,
        },
        "open_risk": open_risk,
        "vertical_barrier_health": {
            "deferred_count_by_asset": state.get(
                "vertical_bar_read_deferred_count_by_asset", {}
            ),
            "last_progress_by_ticket": state.get(
                "vertical_barrier_last_progress_by_ticket", {}
            ),
            "deferred_history_tail": state.get(
                "vertical_barrier_deferred_history", []
            )[-20:],
        },
        "position_fill_time_migration_history": state.get(
            "position_fill_time_migration_history", []
        )[-20:],
        "spread_observations_by_asset": state.get(
            "spread_observations_by_asset", {}
        ),
        "state_counters": state.get("counters", {}),
        "run_id": state.get("run_id"),
        "run_started_at": state.get("run_started_at"),
        "run_counters": state.get("run_counters", {}),
        "audit_rejected_status_parity": execution_output_parity(state),
        "broker_clock": state.get("last_broker_clock_diagnostic", {}),
        "telegram": {
            **telegram_config_snapshot(),
            "last_send": state.get("last_telegram_send"),
        },
        **(extra or {}),
    }
    write_json(LATEST_STATUS_JSON, payload)


def clear_terminal() -> None:
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def print_heartbeat(
    account: Any,
    open_risk: dict[str, Any],
    pending_count: int,
    last_action: str,
    live_armed: bool,
    live_blockers: list[str],
    *,
    state: dict[str, Any] | None = None,
) -> None:
    state = state or {}
    clear_terminal()
    print("=" * 100)
    print(f"V2 PORTFOLIO EXECUTION BOT | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} | mode={EXECUTION_MODE} | order_send={live_armed}")
    print("=" * 100)
    if account is not None:
        print(f"ACCOUNT: balance={account.balance:.2f} equity={account.equity:.2f} free_margin={account.margin_free:.2f}")
    print(
        f"ACCOUNT RISK: total=${open_risk.get('total_open_risk_usd', 0.0):.2f} | "
        f"managed=${open_risk.get('managed_portfolio_risk_usd', 0.0):.2f} | "
        f"external=${open_risk.get('external_position_risk_usd', 0.0):.2f} | "
        f"pending=${open_risk.get('pending_order_contingent_risk_usd', 0.0):.2f} | "
        f"unknown_positions={len(open_risk.get('unknown_risk_positions', []))} | "
        f"unknown_pending={len(open_risk.get('unknown_pending_orders', []))}"
    )
    ctx = open_risk.get("prop_risk_context") or {}
    if ctx:
        print(
            "PROP RISK: "
            f"dd_type={ctx.get('overall_drawdown_type', ctx.get('drawdown_type', 'unknown'))} "
            f"overall_limit=${ctx.get('overall_drawdown_limit_amount', ctx.get('fixed_trailing_distance_amount', 0.0)):.2f} "
            f"hwm_reference=${ctx.get('overall_high_watermark', ctx.get('equity_high_watermark', 0.0)):.2f} "
            f"floor=${ctx.get('overall_floor', ctx.get('live_trailing_floor', 0.0)):.2f} "
            f"actual_buffer=${ctx.get('actual_overall_buffer', ctx.get('actual_trailing_buffer', 0.0)):.2f} "
            f"after_open_stops=${ctx.get('projected_overall_buffer_after_open_stops', ctx.get('projected_trailing_buffer_after_open_stops', ctx.get('remaining_trailing_buffer', 0.0))):.2f} "
            f"extra_operator_reserve=${ctx.get('additional_operator_floor_buffer_amount', 0.0):.2f} "
            f"daily_pct={100.0 * ctx.get('daily_drawdown_pct', 0.0):.2f}% "
            f"daily_allowance=${ctx.get('daily_limit_amount', 0.0):.2f} "
            f"daily_pnl=${ctx.get('daily_total_pnl', 0.0):.2f} "
            f"daily_actual_remaining=${ctx.get('actual_daily_loss_remaining', ctx.get('daily_loss_remaining', 0.0)):.2f} "
            f"daily_after_open_stops=${ctx.get('projected_daily_loss_remaining', 0.0):.2f}"
        )
    if live_armed:
        print("!!! REAL MT5 ORDER SEND IS ARMED — APPROVED SIGNALS MAY PLACE REAL ORDERS !!!")
    print(f"PENDING APPROVED SIGNALS: {pending_count}")
    run_counts = state.get("run_counters") or {}
    lifetime_counts = state.get("counters") or {}
    parity = execution_output_parity(state)
    print(
        "CANDIDATE FUNNEL: "
        f"run_seen={run_counts.get('unique_candidates_seen', 0)} "
        f"run_blocked={run_counts.get('blocked', 0)} "
        f"run_live={run_counts.get('live_trades', 0)} "
        f"run_errors={run_counts.get('errors', 0)} | "
        f"lifetime_seen={lifetime_counts.get('unique_candidates_seen', 0)} "
        f"lifetime_blocked={lifetime_counts.get('blocked', 0)}"
    )
    print(
        "AUDIT/REJECTED/STATUS PARITY: "
        f"{parity.get('parity_passed')} | "
        f"audit_rows={parity.get('audit_rows_appended')} "
        f"rejected_rows={parity.get('rejected_rows_appended')} "
        f"negative_terminal={parity.get('run_negative_terminal_count')}"
    )
    feed = _SIGNAL_FEED.snapshot()
    print(
        "APPROVED SIGNAL FEED: "
        f"jsonl={APPROVED_SIGNALS_JSONL} recent={feed.get('recent_rows')} "
        f"offset={feed.get('offset')} idle_polls={feed.get('idle_polls')}"
    )
    print(f"LAST ACTION: {last_action}")
    if live_blockers:
        print("LIVE BLOCKERS: " + "; ".join(live_blockers[:4]))
    print("=" * 100)


def risk_inflation(plan: OrderPlan) -> float:
    if plan.target_risk_usd > 0:
        return float(plan.actual_risk_usd) / float(plan.target_risk_usd)
    return float("inf")


def risk_inflation_display(plan: OrderPlan) -> str:
    value = risk_inflation(plan)
    if math.isfinite(value):
        return f"{value:.2f}x"
    return "inf"


def submit_order(mt5: Any, plan: OrderPlan, type_filling: int | None = None):
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": plan.symbol,
        "volume": float(plan.volume),
        "type": int(plan.order_type),
        "price": float(plan.entry_price),
        "sl": float(plan.stop_loss),
        "tp": float(plan.take_profit),
        "deviation": int(plan.deviation_points),
        "magic": int(plan.magic),
        "comment": plan.comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": int(plan.type_filling if type_filling is None else type_filling),
    }
    return mt5.order_send(request)


def submit_order_with_filling_retry(
    mt5: Any,
    plan: OrderPlan,
    logger: Any,
    state: dict[str, Any],
) -> tuple[Any, list[dict[str, Any]]]:
    """Submit order and retry alternate filling modes only after invalid fill.

    This keeps the execution plan, risk, lot, entry, SL/TP, deviation, magic, and
    comment unchanged. Only the MT5 type_filling is retried when the broker
    explicitly rejects the previous attempt for unsupported/invalid filling mode.
    """
    attempts: list[dict[str, Any]] = []
    last_result: Any = None

    reconciled = reconcile_prior_transient_order(mt5, plan, state, logger)
    if reconciled is not None:
        attempts.append({
            "attempt": 0,
            "type_filling": int(plan.type_filling),
            "retcode": int(getattr(reconciled, "retcode", mt5.TRADE_RETCODE_DONE)),
            "retcode_name": "TRADE_RETCODE_DONE",
            "comment": getattr(reconciled, "comment", "reconciled_after_transient_result"),
            "order": getattr(reconciled, "order", None),
            "deal": getattr(reconciled, "deal", None),
            "reconciled": True,
        })
        return reconciled, attempts

    for idx, type_filling in enumerate(filling_mode_candidates(mt5, plan.symbol, plan.type_filling, state), start=1):
        result = submit_order(mt5, plan, type_filling=type_filling)
        last_result = result
        attempt = summarize_order_attempt(mt5, result, type_filling)
        attempt["attempt"] = idx
        attempts.append(attempt)

        logger.info(
            "ORDER_SEND_ATTEMPT signal_id=%s symbol=%s type_filling=%s retcode=%s retcode_name=%s comment=%s attempt=%s",
            plan.signal_id,
            plan.symbol,
            type_filling,
            attempt.get("retcode"),
            attempt.get("retcode_name"),
            attempt.get("comment"),
            idx,
        )

        if result is not None and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE):
            plan.type_filling = int(type_filling)
            remember_filling_mode(state, plan.symbol, type_filling)
            return result, attempts

        if not is_invalid_fill_result(mt5, result):
            return result, attempts

    if attempts:
        plan.type_filling = int(attempts[-1]["type_filling"])
    return last_result, attempts


def position_open_time_utc(position: Any) -> pd.Timestamp | None:
    """Return the raw broker-reported position timestamp.

    This value is diagnostic only because some MT5 bridges expose broker-local
    wall time. Vertical exits use authoritative_position_open_time_utc().
    """
    time_msc = safe_int(getattr(position, "time_msc", None), None)
    if time_msc is not None and time_msc > 0:
        try:
            return pd.Timestamp(time_msc, unit="ms", tz="UTC")
        except Exception:
            pass
    time_seconds = safe_int(getattr(position, "time", None), None)
    if time_seconds is not None and time_seconds > 0:
        try:
            return pd.Timestamp(time_seconds, unit="s", tz="UTC")
        except Exception:
            pass
    return None


def authoritative_position_open_time_utc(
    position: Any,
    metadata: dict[str, Any] | None,
) -> tuple[pd.Timestamp | None, str]:
    meta = metadata if isinstance(metadata, dict) else {}
    for field in (
        "execution_fill_time_utc",
        "position_open_time",
        "order_send_completed_at",
        "created_at",
    ):
        stamp = ensure_utc_timestamp(meta.get(field))
        if stamp is not None:
            return stamp, f"metadata:{field}"
    broker = position_open_time_utc(position)
    return broker, "broker_reported_fallback" if broker is not None else "missing"


def update_position_metadata_after_order(mt5: Any, state: dict[str, Any], plan: OrderPlan, signal: dict[str, Any], result: Any) -> None:
    """Persist exact order/deal/signal mapping and bind only a unique recent fill."""
    metadata = make_position_metadata(plan, signal, result)
    send_completed = ensure_utc_timestamp(metadata.get("order_send_completed_at")) or utc_now_ts()
    result_order = metadata_key(getattr(result, "order", None))
    result_deal = metadata_key(getattr(result, "deal", None))
    register_position_metadata(
        state, metadata, comment=plan.comment, order=result_order,
        deal=result_deal, signal_id=plan.signal_id,
    )
    retries = max(int(getattr(settings, "PORTFOLIO_POSITION_METADATA_CAPTURE_RETRIES", 6)), 1)
    sleep_seconds = max(float(getattr(settings, "PORTFOLIO_POSITION_METADATA_CAPTURE_SLEEP_SECONDS", 0.25)), 0.0)
    for attempt in range(retries):
        try:
            positions = list(mt5.positions_get(symbol=plan.symbol) or [])
            exact: list[Any] = []
            for pos in positions:
                identifier = metadata_key(getattr(pos, "identifier", None))
                comment = str(getattr(pos, "comment", "") or "")
                if (result_order and identifier == result_order) or comment == plan.comment:
                    exact.append(pos)
            if not exact:
                candidates: list[Any] = []
                for pos in positions:
                    opened = position_open_time_utc(pos)
                    if opened is None or opened < send_completed - pd.Timedelta(seconds=5):
                        continue
                    try:
                        same_magic = int(getattr(pos, "magic", 0)) == int(plan.magic)
                        same_volume = abs(float(getattr(pos, "volume", 0.0)) - float(plan.volume)) <= 1e-9
                        same_side = position_order_type(mt5, int(getattr(pos, "type", -999))) == int(plan.order_type)
                        same_entry = abs(float(getattr(pos, "price_open", 0.0)) - float(plan.entry_price)) <= max(1e-6, abs(plan.entry_price) * 1e-5)
                        if same_magic and same_volume and same_side and same_entry:
                            candidates.append(pos)
                    except Exception:
                        continue
                if len(candidates) == 1:
                    exact = candidates
            if len(exact) == 1:
                pos = exact[0]
                enriched = dict(metadata)
                opened = position_open_time_utc(pos)
                if opened is not None:
                    enriched["broker_reported_position_open_time"] = opened.isoformat()
                # Never overwrite the process-captured UTC fill time with
                # broker-local position.time/time_msc.
                enriched["execution_fill_time_utc"] = send_completed.isoformat()
                enriched["position_open_time"] = send_completed.isoformat()
                enriched["fill_time_authority"] = "python_process_utc_order_send_completion"
                enriched["position_ticket"] = metadata_key(getattr(pos, "ticket", None))
                enriched["position_identifier"] = metadata_key(getattr(pos, "identifier", None))
                enriched["metadata_captured_at"] = utc_now()
                register_position_metadata(
                    state, enriched,
                    ticket=getattr(pos, "ticket", None),
                    comment=getattr(pos, "comment", None) or plan.comment,
                    order=result_order or getattr(pos, "identifier", None),
                    deal=result_deal,
                    signal_id=plan.signal_id,
                )
                return
            if len(exact) > 1:
                state.setdefault("unresolved_position_metadata_by_ticket", {})[f"order:{result_order or plan.signal_id}"] = {
                    "created_at": utc_now(), "reason": "multiple_recent_positions_match_filled_order",
                    "candidate_tickets": [metadata_key(getattr(pos, "ticket", None)) for pos in exact],
                    "signal_id": plan.signal_id,
                }
                return
        except Exception as exc:
            state.setdefault("unresolved_position_metadata_by_ticket", {})[f"order:{result_order or plan.signal_id}"] = {
                "created_at": utc_now(), "reason": f"metadata_capture_error:{type(exc).__name__}:{exc}",
                "signal_id": plan.signal_id,
            }
            return
        if sleep_seconds > 0 and attempt < retries - 1:
            time.sleep(sleep_seconds)



def live_bars_path_for_asset(asset: str) -> Path:
    return settings.LIVE_BARS_DIR / f"{asset}_live_standard_quote_tick_bars.csv"



def pending_live_bar_asset_dir(asset: str) -> Path:
    return PENDING_LIVE_BAR_DIR / str(asset).upper()


def _read_pending_live_bars_for_executor(
    asset: str,
    logger: Any,
) -> tuple[pd.DataFrame, str | None]:
    directory = pending_live_bar_asset_dir(asset)
    if not directory.exists():
        return pd.DataFrame(), None
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("schema_version") or "") != "v2_pending_live_bar_v1":
                return pd.DataFrame(), f"unsupported_pending_bar_schema:{path.name}"
            record = payload.get("record")
            if not isinstance(record, dict):
                return pd.DataFrame(), f"invalid_pending_bar_record:{path.name}"
            records.append(record)
        except FileNotFoundError:
            # Compaction may delete a journal between directory enumeration and
            # read. The canonical CSV then already contains that row.
            continue
        except Exception as exc:
            logger.warning(
                "VERTICAL_EXIT_PENDING_BAR_READ_DEFERRED asset=%s path=%s error=%s:%s",
                asset,
                path,
                type(exc).__name__,
                exc,
            )
            return pd.DataFrame(), f"{type(exc).__name__}:{exc}"
    return pd.DataFrame(records), None


def _merge_executor_live_bar_sources(
    base: pd.DataFrame,
    pending: pd.DataFrame,
) -> pd.DataFrame:
    frames = [
        frame.copy()
        for frame in (base, pending)
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    if "Datetime" not in out.columns:
        return out
    parsed = _parse_mixed_utc(out["Datetime"])
    out = out.loc[parsed.notna()].copy()
    out["_merge_datetime_utc"] = parsed.loc[parsed.notna()].values
    out = (
        out.sort_values("_merge_datetime_utc", kind="mergesort")
        .drop_duplicates(subset=["_merge_datetime_utc"], keep="last")
        .drop(columns=["_merge_datetime_utc"])
        .reset_index(drop=True)
    )
    return out



def _read_live_bars_with_retry(asset: str, logger: Any) -> tuple[pd.DataFrame | None, str | None]:
    path = live_bars_path_for_asset(asset)
    last_error: Exception | None = None
    base = pd.DataFrame()

    if path.exists():
        for attempt in range(1, LIVE_BAR_READ_RETRIES + 1):
            try:
                base = pd.read_csv(path)
                last_error = None
                break
            except (
                pd.errors.EmptyDataError,
                pd.errors.ParserError,
                FileNotFoundError,
                PermissionError,
                OSError,
            ) as exc:
                last_error = exc
                if attempt < LIVE_BAR_READ_RETRIES:
                    time.sleep(LIVE_BAR_READ_RETRY_SECONDS * attempt)
        if last_error is not None:
            error_text = f"{type(last_error).__name__}:{last_error}"
            logger.warning(
                "VERTICAL_EXIT_BAR_READ_DEFERRED asset=%s path=%s attempts=%s error=%s",
                asset,
                path,
                LIVE_BAR_READ_RETRIES,
                error_text,
            )
            return None, error_text

    pending, pending_error = _read_pending_live_bars_for_executor(asset, logger)
    if pending_error:
        return None, f"pending_live_bar_read_failed:{pending_error}"
    return _merge_executor_live_bar_sources(base, pending), None


def _read_dashboard_bar_ack(asset: str, logger: Any) -> dict[str, Any]:
    """Read the small sequence handoff; never fall back to full dashboard status."""
    key = str(asset).upper()
    last_error = "missing_dashboard_bar_ack"
    for attempt in range(1, VERTICAL_BARRIER_STATUS_READ_RETRIES + 1):
        try:
            payload = read_json_object(DASHBOARD_BAR_ACK_JSON)
            schema = str(payload.get("schema_version") or "")
            if schema != DASHBOARD_BAR_ACK_SCHEMA_VERSION:
                raise ValueError(f"unsupported_dashboard_bar_ack_schema:{schema}")
            if str(payload.get("bar_sequence_contract_version") or "") != BAR_SEQUENCE_CONTRACT_VERSION:
                raise ValueError("bar_sequence_contract_version_mismatch")
            if str(payload.get("bar_time_contract_version") or "") != BAR_TIME_CONTRACT_VERSION:
                raise ValueError("bar_time_contract_version_mismatch")
            continuity = (payload.get("asset_continuity") or {}).get(key) or {}
            policy_ok = (
                payload.get("live_policy_ready") is True
                and str(payload.get("policy_gate_status") or "").upper()
                == "POLICY_PASS"
            )
            if not policy_ok:
                return {
                    "ok": False,
                    "reason": "dashboard_policy_not_ready",
                    "ack_source": "dashboard_bar_ack",
                }
            if str(continuity.get("status") or "") not in {
                "PASS_CONTINUOUS", "PASS_HEALED_CONTINUOUS", "PASS_NO_LIVE_BARS_YET"
            }:
                return {"ok": False, "reason": f"dashboard_continuity_not_ready:{continuity.get('status')}", "ack_source": "dashboard_bar_ack"}
            item = (payload.get("latest_acknowledged_bar_by_asset") or {}).get(key)
            if not isinstance(item, dict):
                raise ValueError(f"missing_asset_sequence_ack:{key}")
            sequence = safe_int(item.get("bar_sequence_number"), None)
            if sequence is None or sequence < 0:
                raise ValueError(f"invalid_bar_sequence_number:{item.get('bar_sequence_number')}")
            bar_datetime = ensure_utc_timestamp(item.get("Datetime"))
            closed_at = ensure_utc_timestamp(item.get("Closed_At"))
            if sequence > 0 and bar_datetime is None:
                raise ValueError("positive_sequence_missing_Datetime")
            if sequence > 0 and closed_at is None:
                raise ValueError("positive_sequence_missing_Closed_At")
            if str(item.get("bar_sequence_contract_version") or "") != BAR_SEQUENCE_CONTRACT_VERSION:
                raise ValueError("asset_bar_sequence_contract_version_mismatch")
            if str(item.get("bar_time_contract_version") or "") != BAR_TIME_CONTRACT_VERSION:
                raise ValueError("asset_bar_time_contract_version_mismatch")
            return {
                "ok": True, "reason": "ok", "ack_source": "dashboard_bar_ack",
                "ack_schema": schema, "updated_at": payload.get("updated_at"),
                "bar_sequence_number": int(sequence), "bar_datetime": bar_datetime,
                "bar_closed_at": closed_at, "latest_live": bar_datetime,
                "bar_sequence_contract_version": BAR_SEQUENCE_CONTRACT_VERSION,
                "bar_time_contract_version": BAR_TIME_CONTRACT_VERSION,
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}:{exc}"
            if attempt < VERTICAL_BARRIER_STATUS_READ_RETRIES:
                time.sleep(LIVE_BAR_READ_RETRY_SECONDS)
    logger.warning("VERTICAL_BAR_ACK_READ_DEFER asset=%s path=%s error=%s", asset, DASHBOARD_BAR_ACK_JSON, last_error)
    return {"ok": False, "reason": f"dashboard_ack_read_failed:{last_error}", "ack_source": "dashboard_bar_ack"}


def _retry_dashboard_ack_until(
    asset: str,
    target_timestamp: pd.Timestamp,
    logger: Any,
) -> dict[str, Any]:
    """Give the dashboard's immediate bar-ack write a brief chance to catch up."""
    latest: dict[str, Any] = {
        "ok": False,
        "reason": "dashboard_ack_catchup_not_attempted",
        "latest_live": None,
    }
    for attempt in range(1, VERTICAL_BARRIER_ACK_CATCHUP_RETRIES + 1):
        latest = _read_dashboard_bar_ack(asset, logger)
        acknowledged = latest.get("latest_live")
        if (
            latest.get("ok")
            and isinstance(acknowledged, pd.Timestamp)
            and acknowledged >= target_timestamp
        ):
            latest["catchup_attempts"] = attempt
            return latest
        if attempt < VERTICAL_BARRIER_ACK_CATCHUP_RETRIES:
            time.sleep(VERTICAL_BARRIER_ACK_CATCHUP_SLEEP_SECONDS)
    latest["catchup_attempts"] = VERTICAL_BARRIER_ACK_CATCHUP_RETRIES
    return latest



def _parse_mixed_utc(values: Any) -> pd.Series:
    raw = values if isinstance(values, pd.Series) else pd.Series(values)
    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
        # Older pandas releases may treat "mixed" as a literal format and
        # silently coerce every valid ISO value to NaT instead of raising.
        if bool(raw.notna().any()) and not bool(parsed.notna().any()):
            return to_datetime_iso(raw, utc=True, errors="coerce")
        return parsed
    except (TypeError, ValueError):
        return to_datetime_iso(raw, utc=True, errors="coerce")


def _build_startup_sequence_index(
    asset: str,
    ack: dict[str, Any],
    logger: Any,
) -> tuple[dict[str, int] | None, str]:
    """One-time migration oracle for positions opened before Stage 4."""
    frame, error = _read_live_bars_with_retry(asset, logger)
    if frame is None:
        return None, f"legacy_sequence_history_read_failed:{error}"
    if frame.empty:
        return {}, "ok_empty"
    if "Datetime" not in frame.columns:
        return None, "legacy_sequence_history_missing_Datetime"
    work = frame.copy()
    work["Datetime"] = _parse_mixed_utc(work["Datetime"])
    work = work.dropna(subset=["Datetime"])
    executable = work.get("Executable", False)
    gap_healed = work.get("Gap_Healed", False)
    if not isinstance(executable, pd.Series):
        executable = pd.Series([executable] * len(work), index=work.index)
    if not isinstance(gap_healed, pd.Series):
        gap_healed = pd.Series([gap_healed] * len(work), index=work.index)
    work = work[
        executable.apply(lambda value: safe_bool(value, False))
        & ~gap_healed.apply(lambda value: safe_bool(value, False))
    ].copy()
    work = work.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last")
    ack_datetime = ack.get("bar_datetime")
    if ack_datetime is not None:
        work = work[work["Datetime"] <= ack_datetime].copy()
    expected_count = safe_int(ack.get("bar_sequence_number"), None)
    if expected_count is None or int(len(work)) != int(expected_count):
        return None, f"legacy_sequence_history_ack_mismatch:history={len(work)} ack={expected_count}"
    return {
        ensure_utc_timestamp(stamp).isoformat(): index
        for index, stamp in enumerate(work["Datetime"].tolist(), start=1)
    }, "ok"


def migrate_open_position_vertical_sequences(
    mt5: Any,
    state: dict[str, Any],
    logger: Any,
) -> dict[str, Any]:
    """Bind legacy open positions to event sequences during Stage-4 startup.

    A missing/new-schema dashboard ACK is treated as a startup race: migration
    stays pending and retries the small ACK without reading bar history. Once
    the ACK is ready, each affected asset history is scanned once. Persistent
    history/event mismatches fail closed and are not automatically rescanned.
    """
    positions = list(mt5.positions_get() or [])
    previous = state.get("vertical_barrier_sequence_startup_migration") or {}
    attempt_count = int(previous.get("attempt_count", 0) or 0) + 1

    pending: list[tuple[Any, dict[str, Any], str]] = []
    for position in positions:
        meta, source = resolve_position_metadata(position, state)
        if safe_bool(meta.get("calendar_time"), False):
            # Calendar-time positions close on wall-clock elapsed hours from
            # the entry event, computed directly in vertical_barrier_progress
            # -- no bar-sequence anchor is needed, so there is nothing to
            # migrate/bind for these positions.
            continue
        horizon = safe_int(meta.get("vertical_horizon_bars"), None)
        event_time = meta.get("entry_event_time") or meta.get("event_time")
        asset = str(meta.get("asset") or "")
        if horizon is None or horizon <= 0 or not event_time or not asset:
            continue
        if safe_int(meta.get("entry_event_bar_sequence"), None) is not None:
            continue
        pending.append((position, meta, source))

    report = {
        "created_at": utc_now(),
        "attempt_count": attempt_count,
        "positions_examined": len(positions),
        "legacy_positions_needing_sequence": len(pending),
        "migrated": 0,
        "failed": 0,
        "waiting_for_sequence_ack": 0,
        "waiting_assets": [],
        "waiting_positions": [],
        "assets_scanned": [],
        "failures": [],
        "next_retry_at": None,
        "status": "not_needed" if not pending else "running",
    }
    if not pending:
        state["vertical_barrier_sequence_startup_migration"] = report
        return report

    indexes: dict[str, dict[str, int] | None] = {}
    errors: dict[str, str] = {}
    waiting_assets: set[str] = set()
    for asset in sorted({str(meta.get("asset")) for _, meta, _ in pending}):
        ack = _read_dashboard_bar_ack(asset, logger)
        if not ack.get("ok"):
            indexes[asset] = None
            errors[asset] = str(
                ack.get("reason") or "dashboard_sequence_ack_unavailable"
            )
            waiting_assets.add(asset)
            continue
        index, reason = _build_startup_sequence_index(asset, ack, logger)
        indexes[asset] = index
        errors[asset] = reason
        report["assets_scanned"].append(asset)

    report["waiting_assets"] = sorted(waiting_assets)

    for position, meta, source in pending:
        asset = str(meta.get("asset") or "")
        ticket = metadata_key(getattr(position, "ticket", None))
        event_time = ensure_utc_timestamp(
            meta.get("entry_event_time") or meta.get("event_time")
        )

        if asset in waiting_assets:
            report["waiting_for_sequence_ack"] += 1
            report["waiting_positions"].append(
                {
                    "ticket": ticket,
                    "asset": asset,
                    "event_time": (
                        None if event_time is None else event_time.isoformat()
                    ),
                    "reason": errors.get(
                        asset, "dashboard_sequence_ack_unavailable"
                    ),
                    "metadata_source": source,
                }
            )
            continue

        index = indexes.get(asset)
        sequence = (
            None
            if event_time is None or index is None
            else index.get(event_time.isoformat())
        )
        if sequence is None:
            report["failed"] += 1
            reason = (
                errors.get(asset, "legacy_sequence_history_unavailable")
                if index is None
                else "event_time_not_in_sequence_index"
            )
            failure = {
                "ticket": ticket,
                "asset": asset,
                "event_time": (
                    None if event_time is None else event_time.isoformat()
                ),
                "reason": reason,
                "metadata_source": source,
            }
            report["failures"].append(failure)
            state.setdefault("unresolved_position_metadata_by_ticket", {})[
                ticket or "UNKNOWN"
            ] = {
                "created_at": utc_now(),
                "reason": "stage4_vertical_sequence_migration_failed",
                **failure,
            }
            continue

        enriched = dict(meta)
        enriched["entry_event_bar_sequence"] = int(sequence)
        enriched["bar_sequence_contract_version"] = BAR_SEQUENCE_CONTRACT_VERSION
        enriched["bar_time_contract_version"] = BAR_TIME_CONTRACT_VERSION
        enriched["metadata_source"] = (
            f"{source}+stage4_vertical_sequence_migration"
        )
        register_position_metadata(
            state,
            enriched,
            ticket=getattr(position, "ticket", None),
            comment=getattr(position, "comment", None),
            signal_id=enriched.get("signal_id"),
        )
        unresolved = state.setdefault(
            "unresolved_position_metadata_by_ticket", {}
        )
        prior_unresolved = unresolved.get(ticket or "UNKNOWN") or {}
        if (
            str(prior_unresolved.get("reason") or "")
            == "stage4_vertical_sequence_migration_failed"
        ):
            unresolved.pop(ticket or "UNKNOWN", None)
        report["migrated"] += 1

    if report["failed"] > 0:
        report["status"] = "partial_fail_closed"
    elif report["waiting_for_sequence_ack"] > 0:
        report["status"] = "waiting_for_sequence_ack"
        report["next_retry_at"] = (
            utc_now_ts()
            + pd.Timedelta(seconds=VERTICAL_SEQUENCE_MIGRATION_RETRY_SECONDS)
        ).isoformat()
    else:
        report["status"] = "pass"

    state["vertical_barrier_sequence_startup_migration"] = report
    logger.info(
        "VERTICAL_SEQUENCE_STARTUP_MIGRATION "
        "status=%s attempt=%s needed=%s migrated=%s failed=%s "
        "waiting=%s assets_scanned=%s waiting_assets=%s",
        report["status"],
        report["attempt_count"],
        report["legacy_positions_needing_sequence"],
        report["migrated"],
        report["failed"],
        report["waiting_for_sequence_ack"],
        report["assets_scanned"],
        report["waiting_assets"],
    )
    return report


def retry_pending_vertical_sequence_migration_if_due(
    mt5: Any,
    state: dict[str, Any],
    logger: Any,
) -> dict[str, Any]:
    """Retry only a pure ACK-wait startup race.

    Persistent history/event mismatches remain fail-closed until explicit
    process restart/operator recovery, preventing repeated full-history scans.
    """
    report = state.get("vertical_barrier_sequence_startup_migration") or {}
    if str(report.get("status") or "") != "waiting_for_sequence_ack":
        return {"retry_attempted": False, **dict(report)}

    retry_at = ensure_utc_timestamp(report.get("next_retry_at"))
    if retry_at is not None and utc_now_ts() < retry_at:
        return {"retry_attempted": False, **dict(report)}

    updated = migrate_open_position_vertical_sequences(mt5, state, logger)
    return {"retry_attempted": True, **dict(updated)}


def vertical_barrier_progress(
    asset: str,
    event_time: Any,
    logger: Any,
    *,
    event_sequence: Any = None,
    position_open_time: Any = None,
    minimum_ack_sequence: Any = None,
    ack: dict[str, Any] | None = None,
    calendar_time: bool = False,
) -> dict[str, Any]:
    """O(1) vertical progress from dashboard executable-bar sequence authority.

    calendar_time=True switches the deadline arithmetic from bar-count
    (current_dashboard_sequence - event_sequence) to wall-clock hours-
    elapsed (dashboard_latest_live - event_time), mirroring shared/labels/
    calendar_time_barriers.py's calendar_time_vertical_deadline_positions
    on the research side. event_sequence is optional and purely
    informational in this mode -- it is never required to reach "ready",
    since the hours-based deadline needs no bar-sequence anchor at all.
    """
    start = ensure_utc_timestamp(event_time)
    event_seq = safe_int(event_sequence, None)
    open_ts = ensure_utc_timestamp(position_open_time)
    base = {
        "asset": asset, "event_time": start.isoformat() if start is not None else str(event_time),
        "event_sequence": event_seq,
        "position_open_time": open_ts.isoformat() if open_ts is not None else None,
        "status": "defer", "reason": "unknown", "count": 0,
        "elapsed_hours": None, "calendar_time": bool(calendar_time),
        "bars_after_position_open": 0, "first_counted_bar": None,
        "last_counted_bar": None, "first_post_fill_bar": None,
        "dashboard_latest_live": None, "dashboard_bar_sequence": None,
        "dashboard_bar_closed_at": None, "ack_source": None,
        "future_rows": 0, "unacknowledged_rows": 0, "invalid_datetime_rows": 0,
        "invalid_closed_at_rows": 0, "duplicate_rows": 0,
        "conflicting_duplicate_rows": 0,
        "sequence_contract": BAR_SEQUENCE_CONTRACT_VERSION,
        "bar_time_contract": BAR_TIME_CONTRACT_VERSION,
        "progress_mode": "calendar_time_wall_clock_arithmetic" if calendar_time else "dashboard_sequence_arithmetic",
    }
    if start is None:
        base["reason"] = "invalid_event_time"
        return base
    if not calendar_time and (event_seq is None or event_seq <= 0):
        base["reason"] = "missing_or_invalid_event_sequence"
        return base
    ack = dict(ack or _read_dashboard_bar_ack(asset, logger))
    base["ack_source"] = ack.get("ack_source")
    if not ack.get("ok"):
        base["reason"] = str(ack.get("reason") or "dashboard_ack_unavailable")
        return base
    current = safe_int(ack.get("bar_sequence_number"), None)
    latest = ensure_utc_timestamp(ack.get("bar_datetime"))
    minimum = safe_int(minimum_ack_sequence, None)
    base["dashboard_bar_sequence"] = current
    base["dashboard_latest_live"] = latest.isoformat() if latest is not None else None
    closed_at = ensure_utc_timestamp(ack.get("bar_closed_at"))
    base["dashboard_bar_closed_at"] = closed_at.isoformat() if closed_at is not None else None
    if current is None or current < 0:
        base["reason"] = "invalid_dashboard_bar_sequence"
        return base
    if minimum is not None and current < minimum:
        base["reason"] = "dashboard_bar_sequence_regressed_across_polls"
        return base
    if not calendar_time and current < event_seq:
        base["reason"] = "dashboard_bar_sequence_precedes_event_sequence"
        return base
    if current > 0 and latest is None:
        base["reason"] = "dashboard_sequence_missing_Datetime"
        return base
    if latest is not None and latest > utc_now_ts() + pd.Timedelta(seconds=VERTICAL_BARRIER_FUTURE_TOLERANCE_SECONDS):
        base["reason"] = "future_acknowledged_bar_datetime"
        return base
    if latest is not None and latest < start:
        base["reason"] = "dashboard_sequence_datetime_precedes_event_time"
        return base
    if calendar_time:
        if latest is not None:
            base["elapsed_hours"] = float((latest - start).total_seconds() / 3600.0)
            base["last_counted_bar"] = latest.isoformat()
    else:
        count = int(current - event_seq)
        base["count"] = count
        if count > 0:
            base["last_counted_bar"] = latest.isoformat() if latest is not None else None
    if open_ts is not None and latest is not None and latest > open_ts:
        base["bars_after_position_open"] = 1
        base["first_post_fill_bar"] = latest.isoformat()
    base["status"] = "ready"
    base["reason"] = "ok"
    return base


def executable_bars_since(asset: str, event_time: Any, logger: Any, *, event_sequence: Any = None) -> int | None:
    progress = vertical_barrier_progress(asset, event_time, logger, event_sequence=event_sequence)
    return int(progress["count"]) if progress.get("status") == "ready" else None


def submit_position_close_with_filling_retry(
    mt5: Any,
    position: Any,
    asset: str,
    magic: int,
    comment: str,
    logger: Any,
    state: dict[str, Any],
    volume_override: float | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    symbol = str(getattr(position, "symbol"))
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None, [{"reason": "missing_symbol_tick"}]
    is_buy = int(getattr(position, "type")) == int(getattr(mt5, "POSITION_TYPE_BUY", 0))
    order_type = int(mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY)
    price = float(tick.bid if is_buy else tick.ask)
    attempts: list[dict[str, Any]] = []
    last = None
    for idx, fill in enumerate(filling_mode_candidates(mt5, symbol, None, state), start=1):
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": float(volume_override if volume_override is not None else getattr(position, "volume")), "type": order_type,
            "position": int(getattr(position, "ticket")), "price": price,
            "deviation": int(DEVIATION_POINTS_BY_ASSET.get(asset, 100)),
            "magic": int(magic), "comment": str(comment)[:31],
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": int(fill),
        }
        last = mt5.order_send(request)
        rec = summarize_order_attempt(mt5, last, int(fill)); rec["attempt"] = idx
        attempts.append(rec)
        logger.info("VERTICAL_EXIT_ATTEMPT ticket=%s symbol=%s attempt=%s fill=%s retcode=%s comment=%s", getattr(position, "ticket", None), symbol, idx, fill, rec.get("retcode"), rec.get("comment"))
        if last is not None and int(getattr(last, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE):
            remember_filling_mode(state, symbol, fill)
            return last, attempts
        if not is_invalid_fill_result(mt5, last):
            return last, attempts
    return last, attempts



def enforce_floating_loss_guard(
    mt5: Any,
    manifest: dict[str, Any],
    state: dict[str, Any],
    open_risk: dict[str, Any],
    live_armed: bool,
    logger: Any,
) -> list[str]:
    """Enforce an optional account-wide floating-loss guard every executor loop."""
    contract = open_risk.get("prop_risk_contract") or {}
    context = open_risk.get("prop_risk_context") or {}
    if not bool(contract.get("floating_loss_guard_enabled")):
        return []
    remaining = safe_float(context.get("floating_loss_guard_actual_remaining"), None)
    if remaining is None or remaining > 1e-9:
        return []

    action = str(contract.get("floating_loss_guard_action") or "block_new_trades").strip().lower()
    guard_state = state.setdefault("floating_loss_guard", {})
    guard_state.update(
        {
            "last_triggered_at": utc_now(),
            "last_actual_floating_loss": context.get("actual_floating_loss"),
            "last_legal_amount": contract.get("floating_loss_guard_amount"),
            "last_action": action,
        }
    )
    logger.error(
        "FLOATING_LOSS_GUARD_TRIGGERED actual_loss=%s legal_amount=%s action=%s live_armed=%s",
        context.get("actual_floating_loss"),
        contract.get("floating_loss_guard_amount"),
        action,
        live_armed,
    )
    if action != "force_close_all":
        return ["floating_loss_guard_block_new_trades"]
    if not live_armed:
        return ["floating_loss_guard_force_close_required_but_live_not_armed"]

    symbol_to_asset = universe_symbols(manifest)
    actions: list[str] = []
    positions = list(mt5.positions_get() or [])
    for position in positions:
        symbol = str(getattr(position, "symbol", "") or "")
        asset = symbol_to_asset.get(symbol, symbol or "UNKNOWN")
        result, attempts = submit_position_close_with_filling_retry(
            mt5,
            position,
            asset,
            MAGIC_NUMBER,
            f"V2FG|{signal_hash_suffix(str(getattr(position, 'ticket', '')))}",
            logger,
            state,
        )
        success = result is not None and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE)
        actions.append(
            f"floating_guard_close:{getattr(position, 'ticket', None)}:{'done' if success else 'failed'}"
        )
        append_rows_csv(
            EXECUTION_AUDIT_CSV,
            [
                {
                    "created_at": utc_now(),
                    "bot_version": BOT_VERSION,
                    "stage": "floating_loss_guard",
                    "status": "closed" if success else "close_failed",
                    "reason": "active_account_floating_loss_guard",
                    "asset": asset,
                    "symbol": symbol,
                    "position_ticket": getattr(position, "ticket", None),
                    "actual_floating_loss": context.get("actual_floating_loss"),
                    "floating_loss_guard_amount": contract.get("floating_loss_guard_amount"),
                    "order_send_attempts_json": json.dumps(attempts, default=str, sort_keys=True),
                }
            ],
        )

    # Remove pending orders so they cannot reopen exposure after the guard fires.
    for order in list(mt5.orders_get() or []):
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(getattr(order, "ticket")),
            "symbol": str(getattr(order, "symbol", "") or ""),
            "magic": int(MAGIC_NUMBER),
            "comment": "V2FG_CANCEL",
        }
        result = mt5.order_send(request)
        success = result is not None and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE)
        actions.append(
            f"floating_guard_cancel:{getattr(order, 'ticket', None)}:{'done' if success else 'failed'}"
        )
    return actions

def should_log_vertical_defer(
    state: dict[str, Any],
    ticket: str,
    reason: str,
    dashboard_latest_live: Any,
) -> bool:
    now = utc_now_ts()
    key = str(ticket)
    prior = (state.setdefault("vertical_barrier_last_defer_log_by_ticket", {}).get(key) or {})
    prior_at = ensure_utc_timestamp(prior.get("logged_at"))
    same_reason = str(prior.get("reason")) == str(reason)
    same_latest = str(prior.get("dashboard_latest_live")) == str(dashboard_latest_live)
    elapsed = float((now - prior_at).total_seconds()) if prior_at is not None else float("inf")
    should = not (same_reason and same_latest and elapsed < VERTICAL_DEFER_LOG_COOLDOWN_SECONDS)
    if should:
        state["vertical_barrier_last_defer_log_by_ticket"][key] = {
            "logged_at": now.isoformat(), "reason": str(reason),
            "dashboard_latest_live": dashboard_latest_live,
        }
    return should


def enforce_vertical_horizon_exits(mt5: Any, manifest: dict[str, Any], state: dict[str, Any], live_armed: bool, logger: Any) -> list[str]:
    """Close managed live positions when their verified vertical barrier expires."""
    if not live_armed:
        return []
    positions = mt5.positions_get() or []
    actions: list[str] = []
    for position in positions:
        meta, source = resolve_position_metadata(position, state)
        calendar_time = safe_bool(meta.get("calendar_time"), False)
        horizon = safe_int(meta.get("vertical_horizon_bars"), None)
        horizon_hours = safe_float(meta.get("vertical_horizon_hours"), None)
        event_time = meta.get("entry_event_time") or meta.get("event_time")
        event_sequence = safe_int(meta.get("entry_event_bar_sequence"), None)
        asset = str(meta.get("asset") or "")
        edge_id = str(meta.get("edge_id") or "")
        if not event_time or not asset or not edge_id or edge_id == "UNKNOWN":
            continue
        if calendar_time:
            if horizon_hours is None or horizon_hours <= 0:
                continue
        elif horizon is None or horizon <= 0:
            continue

        ticket = str(getattr(position, "ticket", ""))
        authoritative_open_ts, fill_time_source = authoritative_position_open_time_utc(
            position,
            meta,
        )
        progress = vertical_barrier_progress(
            asset, event_time, logger, event_sequence=event_sequence,
            position_open_time=authoritative_open_ts,
            minimum_ack_sequence=(state.get("vertical_barrier_last_ack_sequence_by_asset", {}) or {}).get(asset),
            calendar_time=calendar_time,
        )
        observed_sequence = safe_int(progress.get("dashboard_bar_sequence"), None)
        if observed_sequence is not None and progress.get("reason") != "dashboard_bar_sequence_regressed_across_polls":
            state.setdefault("vertical_barrier_last_ack_sequence_by_asset", {})[asset] = int(observed_sequence)
        progress_record = {
            **progress,
            "ticket": ticket,
            "edge_id": edge_id,
            "vertical_horizon_bars": horizon,
            "vertical_horizon_hours": horizon_hours,
            "metadata_source": source,
            "fill_time_source": fill_time_source,
        }
        state.setdefault("vertical_barrier_last_progress_by_ticket", {})[ticket] = progress_record

        if progress.get("status") != "ready":
            counts = state.setdefault("vertical_bar_read_deferred_count_by_asset", {})
            counts[asset] = int(counts.get(asset, 0) or 0) + 1
            deferred = {
                "created_at": utc_now(),
                **progress_record,
            }
            history = state.setdefault("vertical_barrier_deferred_history", [])
            # Avoid adding the identical asset/ticket/reason every one-second poll.
            last = history[-1] if history else {}
            same_as_last = (
                str(last.get("ticket")) == ticket
                and str(last.get("reason")) == str(progress.get("reason"))
                and str(last.get("dashboard_latest_live")) == str(progress.get("dashboard_latest_live"))
            )
            if should_log_vertical_defer(state, ticket, str(progress.get("reason")), progress.get("dashboard_latest_live")):
                history.append(deferred)
                state["vertical_barrier_deferred_history"] = history[-200:]
                logger.warning(
                    "VERTICAL_EXIT_DEFERRED ticket=%s asset=%s edge=%s horizon=%s calendar_time=%s reason=%s "
                    "event_time=%s position_open_time=%s dashboard_latest_live=%s "
                    "future_rows=%s unacknowledged_rows=%s invalid_datetime_rows=%s "
                    "duplicate_rows=%s conflicting_duplicates=%s",
                    ticket,
                    asset,
                    edge_id,
                    horizon_hours if calendar_time else horizon,
                    calendar_time,
                    progress.get("reason"),
                    progress.get("event_time"),
                    progress.get("position_open_time"),
                    progress.get("dashboard_latest_live"),
                    progress.get("future_rows"),
                    progress.get("unacknowledged_rows"),
                    progress.get("invalid_datetime_rows"),
                    progress.get("duplicate_datetime_rows"),
                    progress.get("conflicting_duplicate_rows"),
                )
            continue

        if calendar_time:
            elapsed = safe_float(progress.get("elapsed_hours"), None)
            if elapsed is None or elapsed < horizon_hours:
                continue
        else:
            elapsed = int(progress.get("count") or 0)
            if elapsed < horizon:
                continue

        # Preserve the event-time horizon anchor, but never submit an immediate
        # close based solely on bars that predate the actual broker fill.
        if (
            VERTICAL_BARRIER_REQUIRE_POST_FILL_BAR
            and authoritative_open_ts is not None
            and int(progress.get("bars_after_position_open") or 0) <= 0
        ):
            counts = state.setdefault("vertical_bar_read_deferred_count_by_asset", {})
            counts[asset] = int(counts.get(asset, 0) or 0) + 1
            deferred = {
                "created_at": utc_now(),
                **progress_record,
                "reason": "no_post_fill_bar",
            }
            history = state.setdefault("vertical_barrier_deferred_history", [])
            last = history[-1] if history else {}
            same_as_last = (
                str(last.get("ticket")) == ticket
                and str(last.get("reason")) == "no_post_fill_bar"
                and str(last.get("dashboard_latest_live")) == str(progress.get("dashboard_latest_live"))
            )
            if should_log_vertical_defer(state, ticket, "no_post_fill_bar", progress.get("dashboard_latest_live")):
                history.append(deferred)
                state["vertical_barrier_deferred_history"] = history[-200:]
                logger.warning(
                    "VERTICAL_EXIT_DEFERRED ticket=%s asset=%s edge=%s horizon=%s calendar_time=%s "
                    "reason=no_post_fill_bar event_time=%s position_open_time=%s "
                    "elapsed=%s dashboard_latest_live=%s",
                    ticket,
                    asset,
                    edge_id,
                    horizon_hours if calendar_time else horizon,
                    calendar_time,
                    progress.get("event_time"),
                    progress.get("position_open_time"),
                    elapsed,
                    progress.get("dashboard_latest_live"),
                )
            continue

        key = f"{ticket}|{edge_id}|{horizon_hours if calendar_time else horizon}"
        if key in state.setdefault("vertical_exit_notification_keys", []):
            continue
        result, attempts = submit_position_close_with_filling_retry(
            mt5,
            position,
            asset,
            MAGIC_NUMBER,
            f"V2VH|{asset}|{signal_hash_suffix(edge_id)}",
            logger,
            state,
        )
        success = (
            result is not None
            and int(getattr(result, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE)
        )
        reason = (
            "vertical_barrier_exit_executed"
            if success
            else (
                "vertical_barrier_exit_rejected:"
                f"{getattr(result, 'retcode', None)}:{getattr(result, 'comment', None)}"
            )
        )
        row = {
            "created_at": utc_now(),
            "bot_version": BOT_VERSION,
            "stage": "vertical_exit",
            "status": "executed" if success else "order_rejected",
            "reason": reason,
            "ticket": ticket,
            "edge_id": edge_id,
            "asset": asset,
            "calendar_time": calendar_time,
            "vertical_horizon_bars": horizon,
            "vertical_horizon_hours": horizon_hours,
            "executable_bars_elapsed": elapsed,
            "event_time": progress.get("event_time"),
            "position_open_time": progress.get("position_open_time"),
            "dashboard_latest_live": progress.get("dashboard_latest_live"),
            "first_eligible_bar": progress.get("first_eligible_bar"),
            "latest_eligible_bar": progress.get("latest_eligible_bar"),
            "first_post_fill_bar": progress.get("first_post_fill_bar"),
            "bars_after_position_open": progress.get("bars_after_position_open"),
            "future_rows": progress.get("future_rows"),
            "unacknowledged_rows": progress.get("unacknowledged_rows"),
            "duplicate_datetime_rows": progress.get("duplicate_datetime_rows"),
            "metadata_source": source,
            "order_send_attempts_json": json.dumps(
                attempts, default=str, sort_keys=True
            ),
            "retcode": getattr(result, "retcode", None) if result is not None else None,
            "broker_comment": (
                getattr(result, "comment", None) if result is not None else None
            ),
        }
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        if success:
            state["vertical_exit_notification_keys"].append(key)
            state["vertical_exit_notification_keys"] = state[
                "vertical_exit_notification_keys"
            ][-1000:]
            if SEND_EXECUTION_TELEGRAM:
                send_telegram_logged(
                    logger,
                    state,
                    "vertical_exit",
                    (
                        "⏱️ V2 VERTICAL BARRIER EXIT\n"
                        f"{asset} | {edge_id}\n"
                        f"ticket={ticket} bars={elapsed}/{horizon}\n"
                        f"event={progress.get('event_time')}\n"
                        f"latest_bar={progress.get('latest_eligible_bar')}"
                    ),
                )
        actions.append(reason)
    return actions


def find_position_for_filled_plan(mt5: Any, state: dict[str, Any], plan: OrderPlan, result: Any) -> Any | None:
    """Locate the broker position created/expanded by this order without changing entry flow."""
    result_order = str(getattr(result, "order", "") or "")
    result_deal = str(getattr(result, "deal", "") or "")
    for attempt in range(max(int(POST_FILL_POSITION_LOOKUP_RETRIES), 1)):
        positions = mt5.positions_get(symbol=plan.symbol) or []
        exact: list[Any] = []
        fallback: list[Any] = []
        for pos in positions:
            try:
                meta, _ = resolve_position_metadata(pos, state)
                if str(meta.get("signal_id") or "") == plan.signal_id:
                    exact.append(pos); continue
                identifier = str(getattr(pos, "identifier", "") or "")
                ticket = str(getattr(pos, "ticket", "") or "")
                if result_order and result_order in {identifier, ticket}:
                    exact.append(pos); continue
                if result_deal and result_deal in {identifier, ticket}:
                    exact.append(pos); continue
                same_magic = int(getattr(pos, "magic", 0)) == int(plan.magic)
                same_side = position_order_type(mt5, int(getattr(pos, "type", -999))) == int(plan.order_type)
                if same_magic and same_side:
                    fallback.append(pos)
            except Exception:
                continue
        candidates = exact or fallback
        if candidates:
            return max(candidates, key=lambda p: (int(getattr(p, "time_msc", 0) or 0), int(getattr(p, "time", 0) or 0), int(getattr(p, "ticket", 0) or 0)))
        if attempt < POST_FILL_POSITION_LOOKUP_RETRIES - 1:
            time.sleep(max(float(POST_FILL_POSITION_LOOKUP_SLEEP_SECONDS), 0.0))
    return None


def symbol_risk_protection_snapshot(asset: str, open_risk: dict[str, Any]) -> dict[str, Any]:
    context = open_risk.get("prop_risk_context") or {}; contract = open_risk.get("prop_risk_contract") or {}
    closed = float((context.get("daily_snapshot") or {}).get("closed_losses_by_asset", {}).get(asset, 0.0)); open_stop = float(open_risk.get("risk_by_asset", {}).get(asset, 0.0))
    enabled = bool(contract.get("daily_symbol_risk_rule_enabled")); multiplier = float(contract.get("symbol_risk_stress_multiplier", DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER))
    return {"enabled": enabled, "closed_losses_usd": closed, "open_stop_risk_usd": open_stop, "raw_usage_usd": closed + open_stop, "stressed_usage_usd": closed + multiplier * open_stop, "stress_multiplier": multiplier, "live_internal_cap_usd": contract.get("live_internal_symbol_risk_amount"), "legal_cap_usd": contract.get("legal_symbol_risk_amount")}


def post_fill_risk_guard(mt5: Any, manifest: dict[str, Any], state: dict[str, Any], plan: OrderPlan, signal: dict[str, Any], result: Any, logger: Any) -> dict[str, Any]:
    """Recheck actual MT5 exposure after fill and reduce only the newly-filled volume on breach."""
    if not POST_FILL_RISK_CHECK_ENABLED: return {"status": "disabled", "breach": False}
    account = mt5.account_info()
    if account is None:
        # A successful fill that cannot be verified is treated fail-closed. We can
        # still locate the newly-created/expanded position without account_info()
        # and reduce only the volume introduced by this plan.
        record = {
            "status": "account_info_unavailable",
            "breach": True,
            "breach_reasons": ["post_fill_account_info_unavailable"],
        }
        position = find_position_for_filled_plan(mt5, state, plan, result)
        if position is None or not POST_FILL_EMERGENCY_REDUCE_ENABLED:
            record.update({
                "close_attempted": False,
                "close_success": False,
                "reason": "post_fill_position_not_found_or_emergency_reduce_disabled",
            })
            return record
        reduce_volume = min(float(plan.volume), float(getattr(position, "volume", plan.volume)))
        result_close, attempts = submit_position_close_with_filling_retry(
            mt5, position, plan.asset, MAGIC_NUMBER,
            f"V2PF|{plan.asset}|{signal_hash_suffix(plan.signal_id)}", logger, state,
            volume_override=reduce_volume,
        )
        success = (
            result_close is not None
            and int(getattr(result_close, "retcode", -1)) == int(mt5.TRADE_RETCODE_DONE)
        )
        record.update({
            "close_attempted": True,
            "close_success": success,
            "close_retcode": getattr(result_close, "retcode", None) if result_close is not None else None,
            "close_comment": getattr(result_close, "comment", None) if result_close is not None else None,
            "close_attempts": attempts,
            "position_ticket": getattr(position, "ticket", None),
            "reduced_volume": reduce_volume,
        })
        append_rows_csv(EXECUTION_AUDIT_CSV, [{
            "created_at": utc_now(),
            "bot_version": BOT_VERSION,
            "stage": "post_fill_risk_guard",
            "status": "emergency_reduced" if success else "emergency_reduce_failed",
            "reason": "post_fill_account_info_unavailable",
            "signal_id": plan.signal_id,
            "edge_id": plan.edge_id,
            "asset": plan.asset,
            "symbol": plan.symbol,
            "close_retcode": record.get("close_retcode"),
            "close_comment": record.get("close_comment"),
            "position_ticket": record.get("position_ticket"),
            "reduced_volume": record.get("reduced_volume"),
            "order_send_attempts_json": json.dumps(attempts, default=str, sort_keys=True),
        }])
        if SEND_REJECTION_TELEGRAM:
            send_telegram_logged(
                logger, state, "post_fill_risk_guard",
                f"🚨 V2 POST-FILL VERIFICATION UNAVAILABLE\n{plan.asset} | {plan.edge_id}\n"
                f"emergency_reduce_success={success}",
            )
        return record
    fresh_open = get_open_risk_snapshot(mt5, manifest, state, logger)
    context = attach_prop_risk_context(mt5, manifest, account, fresh_open, state)
    contract = fresh_open.get("prop_risk_contract") or {}

    root_caps = manifest.get("risk_caps") or {}
    edge_lookup = selected_edge_lookup(manifest)
    selected_edge = edge_lookup.get(plan.edge_id) or {}
    edge_caps = selected_edge.get("risk_caps") or {}

    def _effective_cap(name: str, default: float = 1.0) -> float:
        return float(edge_caps.get(name, root_caps.get(name, default)))

    balance = float(account.balance)
    total = float(fresh_open.get("total_open_risk_usd", 0.0))
    asset_risk = float(fresh_open.get("risk_by_asset", {}).get(plan.asset, 0.0))
    family_risk = float(fresh_open.get("risk_by_family", {}).get(plan.family, 0.0))
    edge_risk = float(fresh_open.get("risk_by_edge", {}).get(plan.edge_id, 0.0))
    count_asset = int(fresh_open.get("count_by_asset", {}).get(plan.asset, 0))
    count_edge = int(fresh_open.get("count_by_edge", {}).get(plan.edge_id, 0))

    total_cap_usd = balance * _effective_cap("max_total_open_risk_pct")
    asset_cap_usd = balance * _effective_cap("max_asset_open_risk_pct")
    family_cap_usd = balance * _effective_cap("max_family_open_risk_pct")
    edge_cap_usd = balance * _effective_cap("max_edge_open_risk_pct")
    max_asset_trades = int(edge_caps.get(
        "max_open_trades_per_asset",
        root_caps.get("max_open_trades_per_asset", 999999),
    ))
    max_edge_trades = int(edge_caps.get(
        "max_open_trades_per_edge",
        root_caps.get("max_open_trades_per_edge", 999999),
    ))

    projected_open = total
    required_reserve = (
        float(contract.get("trailing_buffer_reserve_amount", 0.0))
        + float(contract.get("trailing_buffer_cost_allowance_r", 0.0)) * projected_open
    )
    actual_trailing_buffer = float(context.get("actual_trailing_buffer", 0.0))
    projected_trailing_buffer = float(
        context.get(
            "projected_trailing_buffer_after_open_stops",
            context.get("remaining_trailing_buffer", 0.0),
        )
    )

    reasons = []
    if total > total_cap_usd + 1e-9:
        reasons.append("total_open_risk_cap")
    if asset_risk > asset_cap_usd + 1e-9:
        reasons.append("asset_open_risk_cap")
    if family_risk > family_cap_usd + 1e-9:
        reasons.append("family_open_risk_cap")
    if edge_risk > edge_cap_usd + 1e-9:
        reasons.append("edge_open_risk_cap")
    if count_asset > max_asset_trades:
        reasons.append("max_open_trades_per_asset")
    if count_edge > max_edge_trades:
        reasons.append("max_open_trades_per_edge")
    if actual_trailing_buffer <= 1e-9:
        reasons.append("actual_max_trailing_drawdown_breach")
    if (
        bool(contract.get("trailing_buffer_admission_enabled"))
        and projected_trailing_buffer < required_reserve - 1e-9
    ):
        reasons.append("projected_trailing_buffer_reserve")
    if bool(contract.get("daily_drawdown_enabled")):
        if float(context.get("actual_daily_loss_remaining", context.get("daily_loss_remaining", 0.0))) <= 1e-9:
            reasons.append("actual_daily_drawdown_breach")
        if float(context.get("projected_daily_loss_remaining", 0.0)) <= 1e-9:
            reasons.append("projected_daily_stop_risk_breach")
    if bool(contract.get("floating_loss_guard_enabled")):
        if float(context.get("floating_loss_guard_actual_remaining", 0.0)) <= 1e-9:
            reasons.append("actual_floating_loss_guard_breach")
        if float(context.get("floating_loss_guard_projected_remaining", 0.0)) <= 1e-9:
            reasons.append("projected_floating_loss_guard_breach")
    symbol = symbol_risk_protection_snapshot(plan.asset, fresh_open)
    if symbol["enabled"]:
        live_cap=float(symbol["live_internal_cap_usd"]); legal=float(symbol["legal_cap_usd"])
        if symbol["raw_usage_usd"] > live_cap + 1e-9 or symbol["raw_usage_usd"] > legal + 1e-9 or symbol["stressed_usage_usd"] > legal + 1e-9: reasons.append("daily_symbol_risk")
    breach=bool(reasons)
    record = {
        "status": "breach" if breach else "pass",
        "breach": breach,
        "breach_reasons": reasons,
        "total_open_risk_usd": total,
        "asset_open_risk_usd": asset_risk,
        "family_open_risk_usd": family_risk,
        "edge_open_risk_usd": edge_risk,
        "total_open_risk_cap_usd": total_cap_usd,
        "asset_open_risk_cap_usd": asset_cap_usd,
        "family_open_risk_cap_usd": family_cap_usd,
        "edge_open_risk_cap_usd": edge_cap_usd,
        "count_asset": count_asset,
        "count_edge": count_edge,
        "max_open_trades_per_asset": max_asset_trades,
        "max_open_trades_per_edge": max_edge_trades,
        "edge_specific_caps_applied": bool(edge_caps),
        "equity_high_watermark": context.get("equity_high_watermark"),
        "max_overall_loss_pct": context.get("max_overall_loss_pct"),
        "live_trailing_floor": context.get("live_trailing_floor"),
        "actual_trailing_buffer": actual_trailing_buffer,
        "projected_trailing_equity_proxy": context.get("projected_trailing_equity_proxy"),
        "projected_trailing_buffer_after_open_stops": projected_trailing_buffer,
        "remaining_trailing_buffer": context.get("remaining_trailing_buffer"),
        "required_trailing_buffer_reserve": required_reserve,
        "daily_drawdown_pct": context.get("daily_drawdown_pct"),
        "daily_limit_amount": context.get("daily_limit_amount"),
        "daily_total_pnl": context.get("daily_total_pnl"),
        "actual_daily_loss_remaining": context.get("actual_daily_loss_remaining"),
        "projected_daily_loss_remaining": context.get("projected_daily_loss_remaining"),
        "daily_equity_proxy": context.get("daily_equity_proxy"),
        "daily_floor": context.get("daily_floor"),
        "actual_floating_loss": context.get("actual_floating_loss"),
        "floating_loss_guard_actual_remaining": context.get("floating_loss_guard_actual_remaining"),
        "floating_loss_guard_projected_remaining": context.get("floating_loss_guard_projected_remaining"),
        "symbol_risk": symbol,
    }
    if not breach: return record
    if symbol["enabled"] and "daily_symbol_risk" in reasons:
        day=str((context.get("daily_snapshot") or {}).get("utc_day") or "")
        if day: state.setdefault("prop_risk_state", {}).setdefault("symbol_risk_lockout_utc_day_by_asset", {})[plan.asset]=day
    position=find_position_for_filled_plan(mt5,state,plan,result)
    if position is None or not POST_FILL_EMERGENCY_REDUCE_ENABLED:
        record.update({"close_attempted":False,"close_success":False,"reason":"post_fill_position_not_found_or_emergency_reduce_disabled"}); return record
    reduce_volume=min(float(plan.volume),float(getattr(position,"volume",plan.volume)))
    result_close,attempts=submit_position_close_with_filling_retry(mt5,position,plan.asset,MAGIC_NUMBER,f"V2PF|{plan.asset}|{signal_hash_suffix(plan.signal_id)}",logger,state,volume_override=reduce_volume)
    success=result_close is not None and int(getattr(result_close,"retcode",-1))==int(mt5.TRADE_RETCODE_DONE)
    record.update({"close_attempted":True,"close_success":success,"close_retcode":getattr(result_close,"retcode",None) if result_close is not None else None,"close_comment":getattr(result_close,"comment",None) if result_close is not None else None,"close_attempts":attempts,"position_ticket":getattr(position,"ticket",None),"reduced_volume":reduce_volume})
    append_rows_csv(EXECUTION_AUDIT_CSV,[{"created_at":utc_now(),"bot_version":BOT_VERSION,"stage":"post_fill_risk_guard","status":"emergency_reduced" if success else "emergency_reduce_failed","reason":"post_fill_risk_breach:"+",".join(reasons),"signal_id":plan.signal_id,"edge_id":plan.edge_id,"asset":plan.asset,"symbol":plan.symbol,**{k:v for k,v in record.items() if k not in {"close_attempts","symbol_risk"}},"symbol_risk_json":json.dumps(symbol,default=str,sort_keys=True),"order_send_attempts_json":json.dumps(attempts,default=str,sort_keys=True)}])
    if SEND_REJECTION_TELEGRAM: send_telegram_logged(logger,state,"post_fill_risk_guard",f"🚨 V2 POST-FILL RISK BREACH\n{plan.asset} | {plan.edge_id}\nreasons={','.join(reasons)} emergency_reduce_success={success}")
    return record


def process_signal(
    mt5: Any,
    manifest: dict[str, Any],
    policy: dict[str, Any],
    edge_lookup: dict[str, dict[str, Any]],
    current_hashes: dict[str, str | None],
    state: dict[str, Any],
    signal: dict[str, Any],
    live_armed: bool,
    live_blockers: list[str],
    logger: Any,
) -> str:
    account = mt5.account_info()
    if account is None:
        reason = "missing_account_info"
        row = audit_row("account", "transient_wait", reason, signal=signal, live_armed=live_armed, live_blockers=live_blockers)
        retry = write_transient_wait_if_due(state=state, signal=signal, stage="account", reason=reason, row=row)
        if retry.get("expired"):
            return str(retry.get("terminal_reason"))
        send_signal_issue_telegram_once(
            logger,
            state,
            signal=signal,
            stage="account",
            status="transient_wait",
            reason=reason,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        return reason

    ok, reason, exec_plan = validate_signal_contract(signal, edge_lookup, current_hashes, require_execution_allowed_for_live=live_armed)
    if not ok:
        mark_terminal(state, str(signal.get("signal_id")), "blocked", reason)
        clear_transient_order_retry(state, str(signal.get("signal_id")))
        clear_transient_plan_retry(state, str(signal.get("signal_id")))
        row = audit_row("validate", "blocked", reason, signal=signal, account=account, live_armed=live_armed, live_blockers=live_blockers)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        append_rows_csv(REJECTED_SIGNALS_CSV, [row], dedup_cols=["signal_id", "reason"])
        increment_counter(state, "blocked")
        if SEND_REJECTION_TELEGRAM:
            send_telegram_logged(
                logger,
                state,
                "validation_block",
                f"❌ V2 signal blocked\n{signal.get('edge_id')} {signal.get('asset')}\n{reason}",
            )
        return reason
    assert exec_plan is not None
    edge = edge_lookup[str(signal.get("edge_id"))]
    open_risk = get_open_risk_snapshot(mt5, manifest, state, logger)
    try:
        attach_prop_risk_context(mt5, manifest, account, open_risk, state)
    except Exception as exc:
        reason = f"prop_risk_context_unavailable:{type(exc).__name__}:{exc}"
        row = audit_row("prop_risk", "transient_wait", reason, signal=signal, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
        retry = write_transient_wait_if_due(state=state, signal=signal, stage="prop_risk", reason=reason, row=row)
        if retry.get("expired"):
            return str(retry.get("terminal_reason"))
        send_signal_issue_telegram_once(logger, state, signal=signal, stage="prop_risk", status="transient_wait", reason=reason, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
        return reason
    unknown_positions = list(open_risk.get("unknown_risk_positions") or [])
    unknown_pending = list(open_risk.get("unknown_pending_orders") or [])
    if (BLOCK_IF_UNKNOWN_OPEN_RISK and unknown_positions) or (BLOCK_IF_UNKNOWN_PENDING_RISK and unknown_pending):
        reason = (
            f"unknown_account_risk:positions={len(unknown_positions)},pending={len(unknown_pending)}"
        )
        row = audit_row("open_risk", "transient_wait", reason, signal=signal, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
        retry = write_transient_wait_if_due(state=state, signal=signal, stage="open_risk", reason=reason, row=row)
        if retry.get("expired"):
            return str(retry.get("terminal_reason"))
        send_signal_issue_telegram_once(
            logger,
            state,
            signal=signal,
            stage="open_risk",
            status="transient_wait",
            reason=reason,
            account=account,
            open_risk=open_risk,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        return reason


    # QUANTARA_OPPOSITE_DIRECTION_SAME_SYMBOL_GUARD_V1
    direction_policy = opposite_direction_same_symbol_policy_diagnostic(signal, open_risk)
    state["last_opposite_direction_same_symbol_policy"] = dict(direction_policy)
    if bool(direction_policy.get("blocked")):
        reason = (
            "opposite_direction_same_symbol_block:"
            f"{direction_policy.get('symbol')}:"
            f"requested={direction_policy.get('requested_side')}:"
            f"existing={direction_policy.get('existing_opposite_side')}:"
            f"count={direction_policy.get('existing_opposite_count')}"
        )
        signal_id = str(signal.get("signal_id") or "")
        mark_terminal(
            state,
            signal_id,
            "blocked",
            reason,
            {"opposite_direction_policy": dict(direction_policy)},
        )
        clear_transient_order_retry(state, signal_id)
        clear_transient_plan_retry(state, signal_id)
        row = audit_row(
            "account_policy",
            "blocked",
            reason,
            signal=signal,
            account=account,
            open_risk=open_risk,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        row.update(
            {
                "opposite_direction_policy_enabled": True,
                "requested_position_side": direction_policy.get("requested_side"),
                "existing_opposite_position_side": direction_policy.get("existing_opposite_side"),
                "existing_opposite_position_count": direction_policy.get("existing_opposite_count"),
                "existing_opposite_position_tickets_json": json.dumps(
                    direction_policy.get("existing_opposite_tickets") or [], sort_keys=True
                ),
                "opposite_direction_policy_json": json.dumps(
                    direction_policy, default=str, sort_keys=True
                ),
            }
        )
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        append_rows_csv(REJECTED_SIGNALS_CSV, [row], dedup_cols=["signal_id", "reason"])
        history = state.setdefault("opposite_direction_same_symbol_block_history", [])
        history.append({"created_at": utc_now(), "signal_id": signal_id, "reason": reason, **dict(direction_policy)})
        state["opposite_direction_same_symbol_block_history"] = history[-200:]
        increment_counter(state, "blocked")
        increment_counter(state, "opposite_direction_blocks")
        logger.warning(
            "OPPOSITE_DIRECTION_SAME_SYMBOL_BLOCK signal_id=%s asset=%s symbol=%s "
            "requested=%s existing=%s existing_count=%s tickets=%s",
            signal_id,
            direction_policy.get("asset"),
            direction_policy.get("symbol"),
            direction_policy.get("requested_side"),
            direction_policy.get("existing_opposite_side"),
            direction_policy.get("existing_opposite_count"),
            direction_policy.get("existing_opposite_tickets"),
        )
        if SEND_REJECTION_TELEGRAM:
            send_telegram_logged(
                logger,
                state,
                "opposite_direction_policy_block",
                "V2 OPPOSITE-DIRECTION ENTRY BLOCKED\n"
                f"{direction_policy.get('asset')} {direction_policy.get('requested_side')} | {signal.get('edge_id')}\n"
                f"existing {direction_policy.get('existing_opposite_side')} positions="
                f"{direction_policy.get('existing_opposite_count')}",
            )
        return reason


    state["last_broker_clock_diagnostic"] = {}
    plan, plan_reason = build_order_plan(
        mt5,
        signal,
        exec_plan,
        manifest,
        edge,
        account,
        open_risk,
        state,
    )
    signal["_broker_clock_diagnostic"] = dict(
        state.get("last_broker_clock_diagnostic") or {}
    )
    if plan is None:
        transient_prefixes = [
            "spread_too_high", "entry_drift_too_large", "max_open_trades",
            "actual_risk_exceeds_remaining", "worst_case_equity_too_close_to_floor",
            "target_balance_reached", "paused_below_balance_floor", "missing_tick_or_symbol_info",
            "trailing_drawdown_buffer_exhausted", "actual_daily_loss_limit_reached",
            "projected_daily_stop_risk_limit_reached",
            "legal_daily_symbol_risk_already_reached", "internal_daily_symbol_risk_already_reached",
            "legal_daily_symbol_risk_lockout",
            "projected_trailing_buffer_below_reserve", "projected_daily_stop_risk_breach",
            "projected_legal_daily_symbol_risk_breach", "projected_internal_daily_symbol_risk_cap",
            "broker_clock_contract_unavailable", "missing_broker_tick_timestamp",
            "future_broker_tick_normalised", "stale_broker_tick_normalised",
            "decommission_rule_unavailable",
        ]
        is_transient = any(str(plan_reason).startswith(p) for p in transient_prefixes)
        if str(plan_reason).startswith("broker_clock_contract_unavailable"):
            increment_counter(state, "broker_clock_contract_waits")
        clock_reason_class = str(
            (signal.get("_broker_clock_diagnostic") or {}).get("reason_class") or ""
        )
        if clock_reason_class == CLOCK_CONTRACT_RETRY:
            increment_counter(state, "clock_contract_retries")
        elif clock_reason_class == CLOCK_CONTRACT_INVALID:
            increment_counter(state, "clock_contract_invalid")
        elif clock_reason_class == GENUINE_FUTURE_TICK:
            increment_counter(state, "genuine_future_ticks")
        elif clock_reason_class == STALE_TICK:
            increment_counter(state, "stale_ticks")
        status = "transient_wait" if is_transient else "blocked"
        row = audit_row("plan", status, plan_reason, signal=signal, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
        if is_transient:
            retry = write_transient_wait_if_due(
                state=state, signal=signal, stage="plan", reason=plan_reason, row=row
            )
            if retry.get("expired"):
                return str(retry.get("terminal_reason"))
        else:
            append_rows_csv(EXECUTION_AUDIT_CSV, [row])
            mark_terminal(state, str(signal.get("signal_id")), "blocked", plan_reason)
            clear_transient_plan_retry(state, str(signal.get("signal_id")))
            clear_transient_order_retry(state, str(signal.get("signal_id")))
            append_rows_csv(REJECTED_SIGNALS_CSV, [row], dedup_cols=["signal_id", "reason"])
            increment_counter(state, "blocked")
        send_signal_issue_telegram_once(
            logger,
            state,
            signal=signal,
            stage="plan",
            status=status,
            reason=plan_reason,
            account=account,
            open_risk=open_risk,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        return plan_reason

    if not live_armed:
        reason = "paper_mode_or_live_not_armed_no_order_send"
        row = audit_row("paper_execute", "paper_executed", reason, signal=signal, plan=plan, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        append_rows_csv(EXECUTED_TRADES_CSV, [row], dedup_cols=["signal_id"])
        mark_terminal(state, plan.signal_id, "paper_executed", reason, {"plan": asdict(plan)})
        clear_transient_order_retry(state, plan.signal_id)
        clear_transient_plan_retry(state, plan.signal_id)
        increment_counter(state, "paper_trades")
        if SEND_EXECUTION_TELEGRAM:
            send_telegram_logged(
                logger,
                state,
                "paper_trade",
                f"🧪 V2 PAPER PORTFOLIO TRADE\n"
                f"{plan.asset} {plan.side_name.upper()} | {plan.edge_id}\n"
                f"p={safe_float(signal.get('calibrated_probability'), 0.0):.4f} thr={safe_float(signal.get('final_live_threshold'), 0.0):.4f}\n"
                f"risk model_target=${plan.model_target_risk_usd:.2f} sizing_target=${plan.target_risk_usd:.2f} broker_actual=${plan.actual_risk_usd:.2f}\n"
                f"actual_pct={plan.actual_risk_pct:.4%} inflation={risk_inflation_display(plan)} min_lot={plan.used_min_lot_fallback}\n"
                f"vol={plan.volume} entry={plan.entry_price} SL={plan.stop_loss} TP={plan.take_profit}",
            )
        return reason

    row = audit_row("submit", "submitting", "sending_order", signal=signal, plan=plan, account=account, open_risk=open_risk, live_armed=live_armed, live_blockers=live_blockers)
    append_rows_csv(EXECUTION_AUDIT_CSV, [row])
    result, order_attempts = submit_order_with_filling_retry(mt5, plan, logger, state)
    if result is not None and int(result.retcode) == int(mt5.TRADE_RETCODE_DONE):
        reason = "order_executed"
        update_position_metadata_after_order(mt5, state, plan, signal, result)
        post_fill = post_fill_risk_guard(mt5, manifest, state, plan, signal, result, logger)
        if post_fill.get("breach"):
            reason = (
                "order_executed_post_fill_risk_breach_emergency_reduced"
                if post_fill.get("close_success")
                else "order_executed_post_fill_risk_breach_reduce_failed"
            )
        row = audit_row("result", "executed", reason, signal=signal, plan=plan, account=account, open_risk=open_risk, result=result, live_armed=live_armed, live_blockers=live_blockers)
        row["post_fill_risk_guard_json"] = json.dumps(post_fill, default=str, sort_keys=True)
        row["order_send_attempts_json"] = json.dumps(order_attempts, default=str, sort_keys=True)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        append_rows_csv(EXECUTED_TRADES_CSV, [row], dedup_cols=["signal_id"])
        mark_terminal(state, plan.signal_id, "executed", reason, {
            "plan": asdict(plan), "retcode": int(result.retcode),
            "order_send_attempts": order_attempts, "post_fill_risk_guard": post_fill,
        })
        clear_transient_order_retry(state, plan.signal_id)
        clear_transient_plan_retry(state, plan.signal_id)
        increment_counter(state, "live_trades")
        if SEND_EXECUTION_TELEGRAM:
            send_telegram_logged(
                logger,
                state,
                "live_trade",
                f"✅ V2 PORTFOLIO TRADE EXECUTED\n"
                f"{plan.asset} {plan.side_name.upper()} | {plan.edge_id}\n"
                f"risk model_target=${plan.model_target_risk_usd:.2f} sizing_target=${plan.target_risk_usd:.2f} broker_actual=${plan.actual_risk_usd:.2f}\n"
                f"actual_pct={plan.actual_risk_pct:.4%} inflation={risk_inflation_display(plan)} min_lot={plan.used_min_lot_fallback}\n"
                f"vol={plan.volume} entry={plan.entry_price} SL={plan.stop_loss} TP={plan.take_profit}\n"
                f"order={getattr(result, 'order', '')} deal={getattr(result, 'deal', '')}",
            )
        return reason

    retcode = getattr(result, "retcode", None) if result is not None else None
    retname = get_retcode_name(mt5, retcode)
    broker_comment = getattr(result, "comment", "order_send returned None") if result is not None else "order_send returned None"
    attempts_text = json.dumps(order_attempts, default=str, sort_keys=True)

    if is_market_closed_result(mt5, result):
        cooldown = record_market_closed_cooldown(
            state,
            symbol=plan.symbol,
            result=result,
        )
        reason = (
            f"market_session_closed:{plan.symbol}:{retcode}:{retname}:"
            f"cooldown_until={cooldown['until']}"
        )
        row = audit_row(
            "result",
            "market_session_closed",
            reason,
            signal=signal,
            plan=plan,
            account=account,
            open_risk=open_risk,
            result=result,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        row["order_send_attempts_json"] = attempts_text
        row["market_closed_cooldown_json"] = json.dumps(cooldown, default=str, sort_keys=True)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        append_rows_csv(REJECTED_SIGNALS_CSV, [row], dedup_cols=["signal_id", "reason"])
        mark_terminal(
            state,
            plan.signal_id,
            "market_session_closed",
            reason,
            {
                "plan": asdict(plan),
                "retcode": retcode,
                "order_send_attempts": order_attempts,
                "market_closed_cooldown": cooldown,
            },
        )
        clear_transient_order_retry(state, plan.signal_id)
        clear_transient_plan_retry(state, plan.signal_id)
        increment_counter(state, "blocked")
        logger.warning(
            "MARKET_SESSION_CLOSED signal_id=%s symbol=%s retcode=%s cooldown_until=%s",
            plan.signal_id,
            plan.symbol,
            retcode,
            cooldown["until"],
        )
        return reason

    transient_reason = transient_order_result_reason(mt5, result)
    if transient_reason is not None:
        retry = record_transient_order_retry(state, plan, result, transient_reason)
        reason = (
            f"transient_order_retry:{transient_reason}; "
            f"attempt_count={retry['attempt_count']}; "
            f"next_retry_at={retry['next_retry_at']}; "
            f"filling_attempts={attempts_text}"
        )
        row = audit_row(
            "result",
            "transient_wait",
            reason,
            signal=signal,
            plan=plan,
            account=account,
            open_risk=open_risk,
            result=result,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        row["order_send_attempts_json"] = attempts_text
        row["transient_retry_json"] = json.dumps(retry, default=str, sort_keys=True)
        append_rows_csv(EXECUTION_AUDIT_CSV, [row])
        increment_counter(state, "transient_waits")
        send_signal_issue_telegram_once(
            logger,
            state,
            signal=signal,
            stage="order_send",
            status="transient_wait",
            reason=reason,
            account=account,
            open_risk=open_risk,
            live_armed=live_armed,
            live_blockers=live_blockers,
        )
        return reason

    reason = f"order_rejected:{retcode}:{retname}:{broker_comment}; filling_attempts={attempts_text}"
    row = audit_row("result", "order_rejected", reason, signal=signal, plan=plan, account=account, open_risk=open_risk, result=result, live_armed=live_armed, live_blockers=live_blockers)
    row["order_send_attempts_json"] = attempts_text
    append_rows_csv(EXECUTION_AUDIT_CSV, [row])
    append_rows_csv(REJECTED_SIGNALS_CSV, [row], dedup_cols=["signal_id", "reason"])
    mark_terminal(state, plan.signal_id, "order_rejected", reason, {"plan": asdict(plan), "retcode": retcode, "order_send_attempts": order_attempts})
    clear_transient_order_retry(state, plan.signal_id)
    clear_transient_plan_retry(state, plan.signal_id)
    increment_counter(state, "blocked")
    if SEND_REJECTION_TELEGRAM:
        send_telegram_logged(
            logger,
            state,
            "order_rejected",
            f"❌ V2 PORTFOLIO ORDER REJECTED\n{plan.asset} {plan.side_name.upper()} {plan.edge_id}\n{reason}",
        )
    return reason


def validate_expected_account_identity(account: Any) -> None:
    login = safe_int(getattr(account, "login", None), None)
    server = str(getattr(account, "server", "") or "")
    currency = str(getattr(account, "currency", "") or "").upper()
    if EXPECTED_ACCOUNT_LOGIN is not None and login != safe_int(EXPECTED_ACCOUNT_LOGIN, None):
        raise RuntimeError(f"unexpected_mt5_login:{login}!={EXPECTED_ACCOUNT_LOGIN}")
    if EXPECTED_ACCOUNT_SERVER and server != EXPECTED_ACCOUNT_SERVER:
        raise RuntimeError(f"unexpected_mt5_server:{server}!={EXPECTED_ACCOUNT_SERVER}")
    if EXPECTED_ACCOUNT_CURRENCY and currency != EXPECTED_ACCOUNT_CURRENCY:
        raise RuntimeError(f"unexpected_mt5_account_currency:{currency}!={EXPECTED_ACCOUNT_CURRENCY}")
    if EXPECTED_STARTING_CAPITAL is not None and float(EXPECTED_STARTING_CAPITAL) <= 0:
        raise RuntimeError("invalid_expected_starting_capital")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one polling cycle, then exit.")
    parser.add_argument("--max-loops", type=int, default=None, help="Stop after N loops.")
    args = parser.parse_args()

    ensure_runtime_dirs()
    logger = setup_logger("portfolio_execution_bot", LOG_PATH)
    logger.info("=" * 100)
    logger.info("V2 PORTFOLIO EXECUTION BOT version=%s mode=%s enable_order_send=%s", BOT_VERSION, EXECUTION_MODE, ENABLE_ORDER_SEND)
    logger.info(
        "ACTIVE_RULE_POST_FILL_GUARD account_stage=%s symbol_stress_multiplier=%s post_fill_check=%s emergency_reduce=%s",
        EXPECTED_ACCOUNT_STAGE,
        DEFAULT_SYMBOL_RISK_STRESS_MULTIPLIER, POST_FILL_RISK_CHECK_ENABLED, POST_FILL_EMERGENCY_REDUCE_ENABLED,
    )
    logger.info("=" * 100)
    logger.info(
        "OPPOSITE_DIRECTION_SAME_SYMBOL_POLICY enabled=%s "
        "setting=PORTFOLIO_BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL",
        BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL,
    )
    tg_config = telegram_config_snapshot()
    logger.info(
        "TELEGRAM_CONFIG token_present=%s token_length=%s chat_ids_count=%s masked_chat_ids=%s "
        "requests_available=%s startup_enabled=%s execution_enabled=%s rejection_enabled=%s",
        tg_config["token_present"],
        tg_config["token_length"],
        tg_config["chat_ids_count"],
        tg_config["masked_chat_ids"],
        tg_config["requests_available"],
        tg_config["send_startup_enabled"],
        tg_config["send_execution_enabled"],
        tg_config["send_rejection_enabled"],
    )

    if EXECUTION_MODE not in {"paper", "live"}:
        raise RuntimeError(f"Invalid PORTFOLIO_EXECUTION_MODE={EXECUTION_MODE}. Use 'paper' or 'live'.")

    manifest = load_live_manifest()
    policy = load_live_bar_policy()
    validate_live_manifest(manifest)
    stale_temp_recovery = recover_stale_temp_files(
        roots=[BOT_OUTPUT_DIR, settings.LIVE_STATE_DIR],
        archive_root=BOT_OUTPUT_DIR / "recovered_temp_files",
        min_age_seconds=60.0,
    )
    release_identity = build_release_identity(
        process_name="portfolio_execution_bot",
        repo_root=REPO_ROOT,
        v2_root=V2_ROOT,
        output_dir=RELEASE_IDENTITY_DIR,
        manifest=manifest,
        critical_files=[
            THIS_FILE,
            LIVE_DIR / "executor_runtime_io.py",
            LIVE_DIR / "common.py",
            LIVE_DIR / "live_settings.py",
            LIVE_DIR / "portfolio_closed_trade_ledger.py",
            LIVE_DIR / "production_observability.py",
            LIVE_DIR / "production_release_identity.py",
            LIVE_DIR / "production_state_integrity.py",
            LIVE_DIR / "broker_clock_contract.py",
            V2_ROOT / "shared" / "prop_account_rules.py",
            settings.LIVE_MANIFEST_PATH,
            LIVE_BAR_POLICY_PATH,
            settings.ACTIVE_ACCOUNT_RULES_PATH,
        ],
    )
    runtime_metrics = RuntimeMetricsSampler(
        process_name="portfolio_execution_bot",
        output_root=BOT_OUTPUT_DIR,
    )
    logger.info(
        "PRODUCTION_RELEASE_IDENTITY release_id=%s git_commit=%s stale_temp_count=%s recovered_rows=%s",
        release_identity.get("release_id"), release_identity.get("git_commit"),
        stale_temp_recovery.get("stale_temp_count"), stale_temp_recovery.get("recovered_row_count"),
    )
    edge_lookup = selected_edge_lookup(manifest)
    current_hashes = {
        "live_manifest_hash": hash_or_none(settings.LIVE_MANIFEST_PATH),
        "bar_policy_hash": hash_or_none(LIVE_BAR_POLICY_PATH),
    }
    live_armed, live_blockers = real_order_send_armed(manifest, policy)

    state = load_state()
    state["production_release_identity"] = release_identity
    state["stale_temp_recovery"] = stale_temp_recovery
    state["last_manifest_hash"] = current_hashes.get("live_manifest_hash")
    state["last_bar_policy_hash"] = current_hashes.get("bar_policy_hash")
    live_armed, live_blockers = apply_operational_arming_blockers(
        live_armed,
        live_blockers,
        release_identity=release_identity,
        state=state,
    )
    if EXECUTION_MODE == "live" and not live_armed:
        logger.warning("LIVE_MODE_REQUESTED_BUT_NOT_ARMED — bot will NOT send real orders. blockers=%s", live_blockers)

    global _TELEGRAM_WORKER
    _TELEGRAM_WORKER = AsyncNotificationWorker(
        sender=send_telegram,
        name="quantara_executor_telegram",
        max_queue_size=TELEGRAM_ASYNC_QUEUE_MAX_SIZE,
    )
    _TELEGRAM_WORKER.start()
    logger.info("TELEGRAM_ASYNC_WORKER_STARTED max_queue_size=%s", TELEGRAM_ASYNC_QUEUE_MAX_SIZE)

    mt5 = None
    last_action = "starting"
    last_heartbeat = 0.0
    loops = 0
    shutdown_event = threading.Event()
    previous_signal_handlers: dict[int, Any] = {}

    def _request_shutdown(signum: int, _frame: Any) -> None:
        if not shutdown_event.is_set():
            logger.info(
                "Shutdown requested by signal=%s. Current operation will finish before state is saved.",
                signum,
            )
        shutdown_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            previous_signal_handlers[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):
            pass

    try:
        mt5, account = init_mt5()
        logger.info(
            "MT5_CONNECTED login=%s balance=%.2f equity=%.2f server=%s",
            getattr(account, "login", ""),
            float(getattr(account, "balance", 0.0)),
            float(getattr(account, "equity", 0.0)),
            getattr(account, "server", ""),
        )
        validate_expected_account_identity(account)
        select_symbols(mt5, manifest, logger)
        migration = migrate_open_position_vertical_sequences(mt5, state, logger)
        if migration.get("migrated") or migration.get("failed"):
            save_state(state, force_full=True)
        if SEND_STARTUP_TELEGRAM:
            send_telegram_logged(
                logger,
                state,
                "startup",
                f"🤖 V2 PORTFOLIO EXECUTION BOT ONLINE\nmode={EXECUTION_MODE} order_send={live_armed}\nversion={BOT_VERSION}",
            )

        startup_full_audit_pending = True
        while not shutdown_event.is_set():
            loops += 1
            try:
                drain_telegram_completions(logger, state)
                # Reload manifest/policy hashes every loop so stale signals are blocked after config changes.
                manifest = load_live_manifest()
                policy = load_live_bar_policy()
                validate_live_manifest(manifest)
                edge_lookup = selected_edge_lookup(manifest)
                current_hashes = {
                    "live_manifest_hash": hash_or_none(settings.LIVE_MANIFEST_PATH),
                    "bar_policy_hash": hash_or_none(LIVE_BAR_POLICY_PATH),
                }
                live_armed, live_blockers = real_order_send_armed(manifest, policy)
                live_armed, live_blockers = apply_operational_arming_blockers(
                    live_armed,
                    live_blockers,
                    release_identity=release_identity,
                    state=state,
                )

                account = mt5.account_info()
                migration_retry = retry_pending_vertical_sequence_migration_if_due(
                    mt5, state, logger
                )
                if (
                    migration_retry.get("retry_attempted")
                    and (
                        int(migration_retry.get("migrated", 0) or 0) > 0
                        or int(migration_retry.get("failed", 0) or 0) > 0
                    )
                ):
                    save_state(state, force_full=True)
                open_risk = get_open_risk_snapshot(mt5, manifest, state, logger)
                attach_prop_risk_context(mt5, manifest, account, open_risk, state)
                floating_guard_actions = enforce_floating_loss_guard(
                    mt5, manifest, state, open_risk, live_armed, logger
                )
                if floating_guard_actions:
                    last_action = floating_guard_actions[-1]
                    account = mt5.account_info() or account
                    open_risk = get_open_risk_snapshot(mt5, manifest, state, logger)
                    attach_prop_risk_context(mt5, manifest, account, open_risk, state)
                vertical_actions = enforce_vertical_horizon_exits(mt5, manifest, state, live_armed, logger)
                if vertical_actions:
                    last_action = vertical_actions[-1]
                    account = mt5.account_info() or account
                    open_risk = get_open_risk_snapshot(mt5, manifest, state, logger)
                    attach_prop_risk_context(mt5, manifest, account, open_risk, state)
                closed_ledger_result = reconcile_closed_trades(
                    mt5=mt5,
                    state=state,
                    live_bars_dir=settings.LIVE_BARS_DIR,
                    append_rows=append_rows_csv,
                    csv_path=CLOSED_TRADES_CSV,
                    logger=logger,
                    minimum_interval_seconds=CLOSED_TRADE_RECONCILE_SECONDS,
                    history_overlap_days=CLOSED_TRADE_HISTORY_OVERLAP_DAYS,
                    history_lookback_days=CLOSED_TRADE_HISTORY_LOOKBACK_DAYS,
                    full_audit_interval_seconds=CLOSED_TRADE_FULL_AUDIT_SECONDS,
                    force_full_audit=startup_full_audit_pending,
                    account_balance=safe_float(getattr(account, "balance", None), None),
                    expected_starting_capital=safe_float(EXPECTED_STARTING_CAPITAL, None),
                )
                ledger_status = str(closed_ledger_result.get("status") or "").strip()
                if (
                    startup_full_audit_pending
                    and closed_ledger_result.get("reconcile_mode") == "full_audit"
                    and ledger_status != "history_query_failed"
                ):
                    startup_full_audit_pending = False
                state["closed_trade_ledger_integrity_blocked"] = ledger_status != "PASS"
                live_armed, live_blockers = real_order_send_armed(manifest, policy)
                live_armed, live_blockers = apply_operational_arming_blockers(
                    live_armed,
                    live_blockers,
                    release_identity=release_identity,
                    state=state,
                )
                state["runtime_observability"] = runtime_metrics.snapshot(
                    extra={
                        "closed_trade_ledger": closed_ledger_result,
                        "pending_plan_retries": len(state.get("transient_plan_retry_by_signal", {})),
                        "pending_order_retries": len(state.get("transient_order_retry_by_signal", {})),
                        "signal_transport": _SIGNAL_FEED.snapshot(),
                        "telegram_async": (
                            _TELEGRAM_WORKER.snapshot()
                            if _TELEGRAM_WORKER is not None
                            else {"status": "inactive"}
                        ),
                    }
                )
                df = read_approved_signal_rows(logger)
                rows = candidate_rows(df, state)
                increment_counter(state, "seen_candidates", len(rows))
                increment_counter(state, "candidate_poll_observations", len(rows))
                seen_signal_ids = list(state.get("seen_candidate_signal_ids") or [])
                seen_signal_id_set = set(str(value) for value in seen_signal_ids)
                for candidate in rows:
                    signal_id = str(candidate.get("signal_id") or "").strip()
                    if signal_id and signal_id not in seen_signal_id_set:
                        seen_signal_ids.append(signal_id)
                        seen_signal_id_set.add(signal_id)
                state["seen_candidate_signal_ids"] = seen_signal_ids[-10000:]
                state["counters"]["unique_candidates_seen"] = len(
                    state["seen_candidate_signal_ids"]
                )
                run_seen = list(state.get("run_seen_candidate_signal_ids") or [])
                run_seen_set = set(str(value) for value in run_seen)
                for candidate in rows:
                    signal_id = str(candidate.get("signal_id") or "").strip()
                    if signal_id and signal_id not in run_seen_set:
                        run_seen.append(signal_id)
                        run_seen_set.add(signal_id)
                state["run_seen_candidate_signal_ids"] = run_seen[-10000:]
                state["run_counters"]["unique_candidates_seen"] = len(
                    state["run_seen_candidate_signal_ids"]
                )

                heartbeat_due = time.time() - last_heartbeat >= HEARTBEAT_SECONDS
                if heartbeat_due:
                    print_heartbeat(
                        account,
                        open_risk,
                        len(rows),
                        last_action,
                        live_armed,
                        live_blockers,
                        state=state,
                    )
                    write_status(state, account, open_risk, live_armed, live_blockers, {
                        "pending_candidate_count": len(rows),
                        "last_action": last_action,
                        "loop": loops,
                        "live_manifest_hash": current_hashes.get("live_manifest_hash"),
                        "bar_policy_hash": current_hashes.get("bar_policy_hash"),
                        "release_id": release_identity.get("release_id"),
                        "runtime_observability": state.get("runtime_observability", {}),
                        "closed_trade_ledger": state.get("closed_trade_ledger", {}),
                        "append_only_outputs": True,
                        "signal_transport": _SIGNAL_FEED.snapshot(),
                        "full_state_persist_seconds": EXECUTOR_FULL_STATE_PERSIST_SECONDS,
                    })
                    save_state(state)
                    last_heartbeat = time.time()

                if rows and heartbeat_due:
                    logger.info(
                        "CANDIDATES_PENDING count=%s plan_retries=%s order_retries=%s",
                        len(rows),
                        len(state.get("transient_plan_retry_by_signal", {})),
                        len(state.get("transient_order_retry_by_signal", {})),
                    )
                for signal_row in rows:
                    if shutdown_event.is_set():
                        break
                    last_action = process_signal(
                        mt5,
                        manifest,
                        policy,
                        edge_lookup,
                        current_hashes,
                        state,
                        signal_row,
                        live_armed,
                        live_blockers,
                        logger,
                    )
                    save_state(state)

                drain_telegram_completions(logger, state)

                if args.once or (args.max_loops is not None and loops >= args.max_loops):
                    break
                shutdown_event.wait(POLL_SECONDS)
            except KeyboardInterrupt:
                logger.info("KeyboardInterrupt received. Finishing shutdown.")
                shutdown_event.set()
                save_state(state, force_full=True)
                break
            except Exception as exc:
                increment_counter(state, "errors")
                last_action = f"loop_error:{type(exc).__name__}:{exc}"
                logger.error("LOOP_ERROR %s", last_action)
                logger.error(traceback.format_exc())
                save_state(state, force_full=True)
                if args.once:
                    raise
                shutdown_event.wait(3)
    finally:
        shutdown_event.set()
        if _TELEGRAM_WORKER is not None:
            telegram_shutdown = _TELEGRAM_WORKER.stop(
                drain=True,
                timeout_seconds=TELEGRAM_ASYNC_SHUTDOWN_DRAIN_SECONDS,
            )
            drain_telegram_completions(logger, state)
            state["telegram_async_shutdown"] = telegram_shutdown
            logger.info(
                "TELEGRAM_ASYNC_WORKER_STOPPED shutdown_clean=%s submitted=%s completed=%s pending_messages=%s pending_results=%s",
                telegram_shutdown.get("shutdown_clean"),
                telegram_shutdown.get("submitted"),
                telegram_shutdown.get("completed"),
                telegram_shutdown.get("pending_messages"),
                telegram_shutdown.get("pending_results"),
            )
            _TELEGRAM_WORKER = None
        save_state(state, force_full=True)
        if mt5 is not None:
            shutdown_mt5(mt5)
        for sig_value, previous in previous_signal_handlers.items():
            try:
                signal.signal(sig_value, previous)
            except (ValueError, OSError):
                pass
        save_state(state, force_full=True)
        logger.info(
            "Execution bot stopped cleanly. State saved: %s pending_order_retries=%s pending_plan_retries=%s",
            STATE_PATH,
            len(state.get("transient_order_retry_by_signal", {})),
            len(state.get("transient_plan_retry_by_signal", {})),
        )


# QUANTARA_PHASE_E_EXECUTOR_CLEANUP_E1_E2_E3_PY39_V1
if __name__ == "__main__":
    main()
