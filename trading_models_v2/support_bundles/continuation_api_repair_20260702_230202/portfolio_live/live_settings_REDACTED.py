"""
V2 PORTFOLIO LIVE SETTINGS â€” SAFE DEFAULTS ONLY

Purpose
-------
This file preserves the V1 portfolio_live settings role while enforcing the
V2/SOTD source-of-truth contracts.

Rules
-----
- Do not commit real MT5 credentials here.
- Put real broker credentials and broker-specific symbol overrides in:
    trading_models_v2/portfolio_live/local_live_settings.json
  which is ignored by git.
- Allocator/live_manifest.json remains the source of truth for selected edges,
  thresholds, portfolio weights, risk caps, throttle tiers, target-lock tiers,
  and live/dry-run arming status.
- This file must not override live_manifest risk or threshold decisions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
V2_ROOT = THIS_DIR.parent
REPO_ROOT = V2_ROOT.parent
PORTFOLIO_ALLOCATOR_DIR = V2_ROOT / "portfolio_allocator"
PORTFOLIO_LIVE_DIR = THIS_DIR

OUTPUT_DIR = THIS_DIR / "outputs"
LIVE_BARS_DIR = THIS_DIR / "live_bars"
LIVE_LOGS_DIR = THIS_DIR / "live_logs"
LIVE_STATE_DIR = THIS_DIR / "live_state"
LIVE_CONFIG_DIR = THIS_DIR / "config"

LIVE_MANIFEST_PATH = THIS_DIR / "live_manifest.json"
LIVE_DEPLOYMENT_MANIFEST_PATH = THIS_DIR / "live_deployment_manifest.json"
LIVE_MANIFEST_VALIDATION_REPORT_PATH = THIS_DIR / "live_manifest_validation_report.json"
LIVE_ENVIRONMENT_VALIDATION_REPORT_PATH = THIS_DIR / "live_environment_validation_report.json"

LOCAL_LIVE_SETTINGS_PATH = THIS_DIR / "local_live_settings.json"

# -----------------------------------------------------------------------------
# Broker credentials â€” intentionally blank in git.
# -----------------------------------------------------------------------------
MT5_LOGIN: int | None = None
MT5_PASSWORD: str = ""
MT5_SERVER: str = ""
MT5_PATH: str | None = None
MT5_TIMEOUT_MS: int = 60_000

# Broker symbol defaults. Override locally if broker suffixes differ.
MT5_SYMBOL_MAP = {
    "XAUUSD": "XAUUSD",
    "BTCUSD": "BTCUSD",
    "NASDAQ": "US100",
    "US30": "US30",
    "US500": "US500",
    "EURUSD": "EURUSD",
    "AUDUSD": "AUDUSD",
    "USDJPY": "USDJPY",
}

# -----------------------------------------------------------------------------
# Live mode and execution arming.
# -----------------------------------------------------------------------------
LIVE_MODE = "REAL_LIVE"  # DRY_RUN_ONLY | REAL_LIVE
ALLOW_REAL_LIVE_TRADING_EXPORT = True
ENABLE_ORDER_SEND = True
PORTFOLIO_EXECUTION_MODE = "live"  # paper | live
PORTFOLIO_MAGIC_NUMBER = 260512
PORTFOLIO_ACCOUNT_STAGE = "challenge"  # challenge | funded

# Manifest contract names.
LIVE_ALLOCATOR_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"
REQUIRED_THRESHOLD_PROBABILITY_COLUMN = "calibrated_probability"
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"

# -----------------------------------------------------------------------------
# Tick stream / dashboard controls preserved from V1.
# -----------------------------------------------------------------------------
TICK_POLL_SECONDS = 1.0
TICK_BATCH_LOOKBACK_SECONDS = 30
SAVE_STATE_EVERY_N_CLOSED_BARS = 1
MAX_CLOSED_BARS_PER_ASSET = None
HEARTBEAT_EVERY_N_LOOPS = 30

PORTFOLIO_DASHBOARD_RUN_MODE = "signal_only"
PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 600
PORTFOLIO_DASHBOARD_FAIL_IF_PARTIAL_GAP_HEAL = True
STRICT_RECENT_TICK_CHECK = False
FAIL_IF_MANIFEST_NOT_LIVE_READY = False
FAIL_IF_SYMBOL_NOT_TRADEABLE = True

# Gap healing / broker time runtime controls.
LIVE_DASHBOARD_GAP_HEALING_ENABLED = True
LIVE_DASHBOARD_GAP_MAX_LOOKBACK_HOURS = 2000
LIVE_DASHBOARD_GAP_FETCH_CHUNK_HOURS = 6
LIVE_DASHBOARD_MIN_GAP_SECONDS_TO_HEAL = 5.0
LIVE_DASHBOARD_MAX_TICKS_PER_GAP_ASSET = 25_000_000
LIVE_DASHBOARD_BROKER_TIME_AUTO_DETECT = True
LIVE_DASHBOARD_BROKER_TIME_OFFSET_HOURS = None
LIVE_DASHBOARD_BROKER_TIME_FUTURE_TOLERANCE_SECONDS = 900.0
LIVE_DASHBOARD_BROKER_TIME_MAX_REASONABLE_OFFSET_HOURS = 12
LIVE_DASHBOARD_FUTURE_EVENT_BLOCK_TOLERANCE_SECONDS = 5.0

# Research-derived live state contract. These values mirror the shared research
# pipeline and replace the retired arbitrary 8,000-bar safety total.
LIVE_RESEARCH_FFD_WINDOW_BARS = 1000
LIVE_RESEARCH_FFD_LIVE_BUFFER_BARS = 1010
LIVE_RESEARCH_HMM_TRAIN_WINDOW_ROWS = 5000
LIVE_RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS = 1000
LIVE_RESEARCH_HMM_REFIT_FREQUENCY_BARS = 1000
LIVE_RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS = 200
LIVE_DASHBOARD_RESEARCH_RECOVERY_RAW_BARS = 6000

LIVE_FEATURE_STATE_RECENT_ROWS_TO_SAVE = 300
# Backward-compatible alias for old scripts only. The updated dashboard uses the
# explicit recovery setting above and never lowers it below the research minimum.
LIVE_FEATURE_STATE_MIN_MEMORY_KEEP_BARS = 6000

# Bounded concurrency and isolated slow-lane scheduling.
LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED = False
LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET = 1
LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED = False
LIVE_DASHBOARD_ASSET_SCORING_WORKERS = 1
LIVE_DASHBOARD_SLOW_LANE_SCORING_ENABLED = False
LIVE_DASHBOARD_SLOW_LANE_EDGE_FAMILIES = ["absorption"]
LIVE_DASHBOARD_ASYNC_SLOW_LANE_MAX_PENDING_JOBS = 1
LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_ENABLED = True
LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS = 1

LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS = 1
LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_ENABLED = True
LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_EVERY_SECONDS = 86_400.0

# Shared asset continuation and runtime-owned state APIs.
LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_ENABLED = True
LIVE_DASHBOARD_SHARED_FEATURE_CACHE_ENABLED = True
LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_CACHE_ENABLED = True
LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED = True
LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR = False
LIVE_DASHBOARD_INCREMENTAL_SHARED_FEATURES_ENABLED = True
LIVE_DASHBOARD_INCREMENTAL_PARITY_REQUIRED = True
LIVE_DASHBOARD_RUNTIME_STATEFUL_CONTINUATION_ENABLED = True
LIVE_DASHBOARD_RUNTIME_STATE_PERSISTENCE_ENABLED = True
LIVE_DASHBOARD_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED = True
LIVE_DASHBOARD_LIVE_EVENT_ONLY_RUNTIME_API_ENABLED = True
LIVE_DASHBOARD_RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED = True
# Retired tail-search values are intentionally empty. Full authority rebuilds are
# bootstrap/recovery/audit only and align to the HMM refit cadence.
LIVE_DASHBOARD_INCREMENTAL_TAIL_CANDIDATES = []
LIVE_DASHBOARD_INCREMENTAL_OVERLAP_CHECK_ROWS = 200
LIVE_DASHBOARD_INCREMENTAL_FULL_REBUILD_EVERY_N_BARS = 1000
LIVE_DASHBOARD_INCREMENTAL_EXCLUDED_FAMILIES = ["absorption"]
LIVE_DASHBOARD_INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE = True

# Verified suffix-continuation controls. Empty row lists use conservative
# family-specific defaults from shared/edges/incremental_runtime_primitives.py.
# The complete available frame is always attempted last as a fail-closed fallback.
LIVE_DASHBOARD_NATIVE_FEATURE_REPLAY_ROWS = []
LIVE_DASHBOARD_NATIVE_EVENT_REPLAY_ROWS = []
LIVE_DASHBOARD_NATIVE_EVENT_DECISION_HISTORY_ROWS = 700
LIVE_DASHBOARD_NATIVE_MIN_FEATURE_OVERLAP_ROWS = 160
LIVE_DASHBOARD_NATIVE_MIN_EVENT_DECISION_OVERLAP_ROWS = 160
# Every actual candidate is re-evaluated through the unchanged full edge runtime
# before model scoring. No order decision is made from suffix replay alone.
LIVE_DASHBOARD_CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME = True

# Overlap parity tolerances.  Exact fields (Datetime, booleans, integers,
# identifiers and categoricals) are never relaxed by these values.
# Feature tolerance covers tiny FFD/rolling floating-point reconstruction noise.
LIVE_DASHBOARD_INCREMENTAL_NUMERIC_ATOL = 1e-9  # legacy generic alias
LIVE_DASHBOARD_INCREMENTAL_NUMERIC_RTOL = 1e-8  # legacy generic alias
LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_ATOL = 5e-8
LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_RTOL = 1e-7
LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_ATOL = 1e-6
LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_RTOL = 1e-6
# Narrow scale-aware allowance for Datetime-aligned rolling event descriptors
# such as Event_Vol / ATR / range geometry.  Material changes still fail closed.
LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_ATOL = 1e-3
LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_RTOL = 2e-4

# Strict dashboard-owned contract gates.
LIVE_DASHBOARD_REQUIRE_POSITIVE_AFML_FOR_APPROVAL = True
LIVE_DASHBOARD_STRICT_SHARED_FEATURE_CONTRACT_ENABLED = True
LIVE_DASHBOARD_STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED = True
LIVE_DASHBOARD_RAW_TICK_DAILY_PROFILE_ENABLED = True
LIVE_DASHBOARD_RAW_TICK_CURSOR_SIGNATURE_LIMIT = 4096

# -----------------------------------------------------------------------------
# Quote-tick/live bar threshold diagnostic settings preserved from V1.
# -----------------------------------------------------------------------------
ASSET_LIVE_BAR_THRESHOLDS = {}
MIN_DISCOVERED_TICK_THRESHOLD = 1000.0
ALLOW_ADAPTIVE_THRESHOLD_FALLBACK = False
THRESHOLD_TARGET_BARS_PER_DAY = 50
THRESHOLD_DIAGNOSTIC_LOOKBACK_HOURS = 2000
THRESHOLD_FETCH_CHUNK_HOURS = 6
THRESHOLD_MAX_TICKS_PER_ASSET = 25_000_000
THRESHOLD_SAVE_SIMULATED_BARS = True
THRESHOLD_MIN_SIM_BARS_FOR_CONFIDENCE = 10

LIVE_THRESHOLD_MIN_WARMUP_DAYS_BY_ASSET = {
    "XAUUSD": 6,
    "NASDAQ": 13,
    "BTCUSD": 20,
}

FREEZE_THRESHOLD_MIN_TICKS = 1_000
FREEZE_THRESHOLD_MIN_LIVE_SIM_BARS = 20
FREEZE_RESEARCH_CADENCE_TOLERANCE = 0.35
FREEZE_ALLOW_FALLBACK_1_VOLUME = False

# -----------------------------------------------------------------------------
# Execution-bot controls preserved from V1 but kept unarmed by default.
# -----------------------------------------------------------------------------
PORTFOLIO_EXECUTION_POLL_SECONDS = 1.0
PORTFOLIO_EXECUTION_HEARTBEAT_SECONDS = 5.0
PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 600
PORTFOLIO_EXECUTION_FUTURE_SIGNAL_TOLERANCE_SECONDS = 5.0
PORTFOLIO_INCLUDE_UNMANAGED_POSITIONS_IN_RISK = True
PORTFOLIO_BLOCK_IF_UNKNOWN_OPEN_RISK = True
PORTFOLIO_ENFORCE_BROKER_MIN_STOP_LEVEL = True
PORTFOLIO_ALLOW_WIDEN_TO_BROKER_MIN_STOP = False
PORTFOLIO_STATIC_FLOOR_BUFFER_USD = 25.0
PORTFOLIO_BLOCK_AT_TARGET_BALANCE = True
PORTFOLIO_ALLOW_MIN_LOT_FALLBACK = True

LIVE_LOG_ROTATION_MAX_BYTES = 25_000_000
LIVE_LOG_ROTATION_BACKUP_COUNT = 8

PORTFOLIO_EXECUTION_GUARDS_BY_ASSET = {
    "XAUUSD": {"max_spread_points": 90.0, "entry_drift_limit_price": 4.0, "deviation_points": 80, "max_lot_cap": 5.00},
    "BTCUSD": {"max_spread_points": 9000.0, "entry_drift_limit_price": 175.0, "deviation_points": 1500, "max_lot_cap": 5.00},
    "NASDAQ": {"max_spread_points": 350.0, "entry_drift_limit_price": 35.0, "deviation_points": 150, "max_lot_cap": 5.00},
}
PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET = {asset: cfg["max_spread_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET = {asset: cfg["entry_drift_limit_price"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
# Optional adaptive entry drift. Static drift remains the base guard; assets
# with a positive fraction may use max(static_limit, planned_SL_distance *
# fraction), capped by the corresponding max-cap value. Only XAUUSD is widened
# by default because its recent missed signals had large planned SL distances.
PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET = {
    "XAUUSD": 0.12,
    "BTCUSD": 0.0,
    "NASDAQ": 0.0,
}
PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET = {
    "XAUUSD": 8.0,
    "BTCUSD": PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET.get("BTCUSD", 175.0),
    "NASDAQ": PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET.get("NASDAQ", 35.0),
}
PORTFOLIO_DEVIATION_POINTS_BY_ASSET = {asset: cfg["deviation_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
PORTFOLIO_MAX_LOT_CAP_BY_ASSET = {asset: cfg["max_lot_cap"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}

# Telegram defaults â€” override locally.
TELEGRAM_TOKEN = "<REDACTED>"
TELEGRAM_CHAT_IDS: list[str] = []
PORTFOLIO_SEND_STARTUP_TELEGRAM = False
PORTFOLIO_SEND_EXECUTION_TELEGRAM = False
PORTFOLIO_SEND_REJECTION_TELEGRAM = False

# Backward-compatible aliases for ported V1 live scripts.
LIVE_DASHBOARD_RUN_MODE = PORTFOLIO_DASHBOARD_RUN_MODE
LIVE_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS
LIVE_DASHBOARD_FAIL_IF_GAP_NOT_FULLY_HEALED = PORTFOLIO_DASHBOARD_FAIL_IF_PARTIAL_GAP_HEAL


def _load_local_settings() -> dict[str, Any]:
    if not LOCAL_LIVE_SETTINGS_PATH.exists():
        return {}
    with LOCAL_LIVE_SETTINGS_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise TypeError(f"local_live_settings.json must be a JSON object: {LOCAL_LIVE_SETTINGS_PATH}")
    return payload


def _apply_local_settings() -> None:
    local = _load_local_settings()
    if not local:
        return
    globals_map = globals()
    simple_keys = [
        "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "MT5_TIMEOUT_MS",
        "LIVE_MODE", "ALLOW_REAL_LIVE_TRADING_EXPORT", "ENABLE_ORDER_SEND",
        "PORTFOLIO_EXECUTION_MODE", "PORTFOLIO_MAGIC_NUMBER", "PORTFOLIO_ACCOUNT_STAGE",
        "STRICT_RECENT_TICK_CHECK", "FAIL_IF_MANIFEST_NOT_LIVE_READY",
        "FAIL_IF_SYMBOL_NOT_TRADEABLE", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_IDS",
        "PORTFOLIO_SEND_STARTUP_TELEGRAM", "PORTFOLIO_SEND_EXECUTION_TELEGRAM",
        "PORTFOLIO_SEND_REJECTION_TELEGRAM", "LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED", 
        "LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET", "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_ENABLED",
        "LIVE_DASHBOARD_SHARED_FEATURE_CACHE_ENABLED", "LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED",
        "LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR",
        "LIVE_DASHBOARD_INCREMENTAL_SHARED_FEATURES_ENABLED",
        "LIVE_DASHBOARD_INCREMENTAL_PARITY_REQUIRED",
        "LIVE_DASHBOARD_INCREMENTAL_TAIL_CANDIDATES",
        "LIVE_DASHBOARD_INCREMENTAL_OVERLAP_CHECK_ROWS",
        "LIVE_DASHBOARD_INCREMENTAL_FULL_REBUILD_EVERY_N_BARS",
        "LIVE_DASHBOARD_INCREMENTAL_EXCLUDED_FAMILIES",
        "LIVE_DASHBOARD_INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE",
        "LIVE_DASHBOARD_NATIVE_FEATURE_REPLAY_ROWS",
        "LIVE_DASHBOARD_NATIVE_EVENT_REPLAY_ROWS",
        "LIVE_DASHBOARD_NATIVE_EVENT_DECISION_HISTORY_ROWS",
        "LIVE_DASHBOARD_NATIVE_MIN_FEATURE_OVERLAP_ROWS",
        "LIVE_DASHBOARD_NATIVE_MIN_EVENT_DECISION_OVERLAP_ROWS",
        "LIVE_DASHBOARD_CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME",
        "LIVE_DASHBOARD_INCREMENTAL_NUMERIC_ATOL",
        "LIVE_DASHBOARD_INCREMENTAL_NUMERIC_RTOL",
        "LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_ATOL",
        "LIVE_DASHBOARD_INCREMENTAL_FEATURE_NUMERIC_RTOL",
        "LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_ATOL",
        "LIVE_DASHBOARD_INCREMENTAL_EVENT_NUMERIC_RTOL",
        "LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_ATOL",
        "LIVE_DASHBOARD_INCREMENTAL_ROLLING_EVENT_NUMERIC_RTOL",
        "LIVE_RESEARCH_FFD_WINDOW_BARS",
        "LIVE_RESEARCH_FFD_LIVE_BUFFER_BARS",
        "LIVE_RESEARCH_HMM_TRAIN_WINDOW_ROWS",
        "LIVE_RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS",
        "LIVE_RESEARCH_HMM_REFIT_FREQUENCY_BARS",
        "LIVE_RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS",
        "LIVE_DASHBOARD_RESEARCH_RECOVERY_RAW_BARS",
        "LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED",
        "LIVE_DASHBOARD_ASSET_SCORING_WORKERS",
        "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_ENABLED",
        "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS",
        "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_CACHE_ENABLED",
        "LIVE_DASHBOARD_RUNTIME_STATEFUL_CONTINUATION_ENABLED",
        "LIVE_DASHBOARD_RUNTIME_STATE_PERSISTENCE_ENABLED",
        "LIVE_DASHBOARD_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED",
        "LIVE_DASHBOARD_LIVE_EVENT_ONLY_RUNTIME_API_ENABLED",
        "LIVE_DASHBOARD_RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED",
        "LIVE_DASHBOARD_REQUIRE_POSITIVE_AFML_FOR_APPROVAL",
        "LIVE_DASHBOARD_STRICT_SHARED_FEATURE_CONTRACT_ENABLED",
        "LIVE_DASHBOARD_STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED",
        "LIVE_DASHBOARD_RAW_TICK_DAILY_PROFILE_ENABLED",
        "LIVE_DASHBOARD_RAW_TICK_CURSOR_SIGNATURE_LIMIT",
        "PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET",
        "PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET",
    ]
    for key in simple_keys:
        if key.lower() in local:
            globals_map[key] = local[key.lower()]
        elif key in local:
            globals_map[key] = local[key]
    symbol_map = local.get("mt5_symbol_map") or local.get("MT5_SYMBOL_MAP")
    if isinstance(symbol_map, dict):
        MT5_SYMBOL_MAP.update({str(k): str(v) for k, v in symbol_map.items() if str(k).strip() and str(v).strip()})
    guards = local.get("portfolio_execution_guards_by_asset") or local.get("PORTFOLIO_EXECUTION_GUARDS_BY_ASSET")
    if isinstance(guards, dict):
        PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.update(guards)
        globals_map["PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET"] = {asset: cfg["max_spread_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
        globals_map["PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET"] = {asset: cfg["entry_drift_limit_price"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
        globals_map["PORTFOLIO_DEVIATION_POINTS_BY_ASSET"] = {asset: cfg["deviation_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
        globals_map["PORTFOLIO_MAX_LOT_CAP_BY_ASSET"] = {asset: cfg["max_lot_cap"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
    drift_fractions = local.get("portfolio_entry_drift_fraction_of_sl_by_asset") or local.get("PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET")
    if isinstance(drift_fractions, dict):
        PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET.update({
            str(k): float(v) for k, v in drift_fractions.items()
        })
    drift_caps = local.get("portfolio_entry_drift_max_cap_price_by_asset") or local.get("PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET")
    if isinstance(drift_caps, dict):
        PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET.update({
            str(k): float(v) for k, v in drift_caps.items()
        })


_apply_local_settings()


def assert_order_send_armed_by_manifest(live_manifest: dict[str, Any]) -> None:
    """Fail closed unless both local settings and live manifest allow real orders."""
    if not ENABLE_ORDER_SEND:
        raise RuntimeError("ENABLE_ORDER_SEND=False. Real order_send is not armed.")
    if PORTFOLIO_EXECUTION_MODE != "live":
        raise RuntimeError(f"PORTFOLIO_EXECUTION_MODE={PORTFOLIO_EXECUTION_MODE!r}. Must be 'live' for real orders.")
    if LIVE_MODE != "REAL_LIVE" or not ALLOW_REAL_LIVE_TRADING_EXPORT:
        raise RuntimeError("LIVE_MODE/ALLOW_REAL_LIVE_TRADING_EXPORT do not arm real live trading.")
    if live_manifest.get("deployment_status") != "LIVE_APPROVED":
        raise RuntimeError(f"live_manifest deployment_status is not LIVE_APPROVED: {live_manifest.get('deployment_status')}")
    if live_manifest.get("live_ready") is not True or live_manifest.get("live_trade_enabled") is not True:
        raise RuntimeError("live_manifest is not live_ready/live_trade_enabled.")
    if live_manifest.get("stress_validation_status") != "STRESS_PASS":
        raise RuntimeError("live_manifest stress_validation_status must be STRESS_PASS for real orders.")
    manifest_stage = str(live_manifest.get("approved_account_stage") or live_manifest.get("account_stage") or "").strip().lower()
    local_stage = str(PORTFOLIO_ACCOUNT_STAGE or "").strip().lower()
    if local_stage not in {"challenge", "funded"}:
        raise RuntimeError(f"PORTFOLIO_ACCOUNT_STAGE={PORTFOLIO_ACCOUNT_STAGE!r}. Must be 'challenge' or 'funded'.")
    if manifest_stage != local_stage:
        raise RuntimeError(f"Local account stage {local_stage!r} does not match live_manifest approved stage {manifest_stage!r}.")
    if live_manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION:
        raise RuntimeError(
            f"live_manifest risk_contract_version={live_manifest.get('risk_contract_version')!r}; "
            f"expected {RISK_CONTRACT_VERSION!r}."
        )
    if local_stage == "challenge":
        if live_manifest.get("live_approval_scope") != "atmos_nova_100k_challenge_only":
            raise RuntimeError("Challenge orders require live_approval_scope=atmos_nova_100k_challenge_only.")
        if live_manifest.get("daily_symbol_risk_rule_enabled") is not False:
            raise RuntimeError("Challenge manifest must keep the funded daily symbol-risk rule disabled.")
    elif live_manifest.get("funded_stage_live_approval_status") != "APPROVED_FOR_FUNDED_LIVE_MANIFEST_EXPORT":
        raise RuntimeError("Funded orders are not approved by the current allocator stress-validation contract.")

