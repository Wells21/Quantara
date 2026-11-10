"""
V2 PORTFOLIO LIVE SETTINGS — SAFE DEFAULTS ONLY

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
import os
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
ACTIVE_ACCOUNT_RULES_PATH = PORTFOLIO_ALLOCATOR_DIR / "config" / "active_account_rules.json"

# -----------------------------------------------------------------------------
# Broker credentials — intentionally blank in git.
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
    "NASDAQ": "NAS100",
    "DAX": "GER40",
    "GBPJPY": "GBPJPY",
    "HK": "HK50",
    "JPN": "Nikkei225",
    "US500": "SP500",
    "USDMXN": "USDMXN",
    "USOIL": "USOUSD",
}

# -----------------------------------------------------------------------------
# Live mode and execution arming.
# -----------------------------------------------------------------------------
LIVE_MODE = "DRY_RUN_ONLY"  # DRY_RUN_ONLY | REAL_LIVE
ALLOW_REAL_LIVE_TRADING_EXPORT = False
ENABLE_ORDER_SEND = False
PORTFOLIO_EXECUTION_MODE = "paper"  # paper | live
PORTFOLIO_MAGIC_NUMBER = 260512
PORTFOLIO_ACCOUNT_STAGE = ""  # optional local cross-check; active rules remain authoritative

# Manifest contract names.
LIVE_ALLOCATOR_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"
REQUIRED_THRESHOLD_PROBABILITY_COLUMN = "calibrated_probability"
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"

# Live deployment identity is structural, not digest-gated. Historical upstream
# JSON files may retain old hashes after legitimate edge/artifact regeneration;
# those digests must never block the current live build. Internal ephemeral cache
# fingerprints may still be used for in-process cache keys, but no artifact hash
# is an execution or deployment authority.
LIVE_ARTIFACT_HASH_VALIDATION_ENABLED = False
LIVE_CONTRACT_IDENTITY_MODE = "structural_current_files_no_hash_gate"

# -----------------------------------------------------------------------------
# Tick stream / dashboard controls preserved from V1.
# -----------------------------------------------------------------------------
TICK_POLL_SECONDS = 1.0
TICK_BATCH_LOOKBACK_SECONDS = 30
SAVE_STATE_EVERY_N_CLOSED_BARS = 1
MAX_CLOSED_BARS_PER_ASSET = None
HEARTBEAT_EVERY_N_LOOPS = 30

PORTFOLIO_DASHBOARD_RUN_MODE = "signal_only"
# Preserve the allocator/live contract's existing signal-validity policy.
# Latency targets and warnings are observability concerns; they must not
# silently shorten the window in which an otherwise valid signal may trade.
# QUANTARA_MLDP_TASK1_FOUNDATION_V1
PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 120
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

# Compute profile. Local and VPS modes share one code path and differ only in
# bounded scheduling. Edge math, runtime functions, thresholds and artifacts are
# identical in every profile.
PORTFOLIO_COMPUTE_PROFILE = "vps_parallel"  # local_safe | vps_parallel | custom
_CPU_COUNT = max(int(os.cpu_count() or 1), 1)
_COMPUTE_PROFILES = {
    "local_safe": {
        "LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED": False,
        "LIVE_DASHBOARD_ASSET_SCORING_WORKERS": 1,
        "LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED": False,
        "LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET": 1,
        "LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS": 1,
        "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS": 1,
        "LIVE_DASHBOARD_SLOW_LANE_SCORING_ENABLED": False,
        "THRESHOLD_DIAGNOSTIC_ASSET_WORKERS": 1,
        "PORTFOLIO_NUMERIC_LIBRARY_THREADS": 1,
    },
    "vps_parallel": {
        # Asset-level parallelism is the safest high-value parallelism because
        # each asset has an independent bar stream, feature cache and cursor set.
        "LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED": True,
        "LIVE_DASHBOARD_ASSET_SCORING_WORKERS": max(4, min(8, _CPU_COUNT // 2 or 1)),
        # Keep edges sequential inside each asset until benchmarked. This avoids
        # nested oversubscription and runtime-module cache races.
        "LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED": False,
        "LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET": 1,
        "LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS": max(4, min(8, _CPU_COUNT // 2 or 1)),
        "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS": 1,
        "LIVE_DASHBOARD_SLOW_LANE_SCORING_ENABLED": False,
        "THRESHOLD_DIAGNOSTIC_ASSET_WORKERS": max(2, min(6, _CPU_COUNT // 2 or 1)),
        "PORTFOLIO_NUMERIC_LIBRARY_THREADS": 1,
    },
}

# Defaults are filled by _apply_compute_profile() after local settings are read.
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
LIVE_DASHBOARD_RECOVERY_COOLDOWN_SECONDS = 300.0
THRESHOLD_DIAGNOSTIC_ASSET_WORKERS = 1
THRESHOLD_DIAGNOSTIC_RESUME_CACHE_ENABLED = True
THRESHOLD_DIAGNOSTIC_RESUME_CACHE_MAX_AGE_HOURS = 24.0
PORTFOLIO_NUMERIC_LIBRARY_THREADS = 1
LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_ENABLED = True
LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_EVERY_SECONDS = 86_400.0
LIVE_DASHBOARD_LATENCY_STAGE_LOGGING_ENABLED = False
LIVE_PRODUCTION_VERBOSE_LOGGING_ENABLED = False

# Authoritative runtime mode. Native one-row edge-feature approximations remain
# retired. Shared FFD/HMM dependencies advance incrementally once per immutable
# asset snapshot; unchanged full edge runtimes consume that READY snapshot.
LIVE_DASHBOARD_RUNTIME_MODE = "authoritative_batch_shared_cache"
LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_ENABLED = True
LIVE_DASHBOARD_SHARED_FEATURE_CACHE_ENABLED = True
LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_CACHE_ENABLED = True
LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED = True
LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR = False
LIVE_DASHBOARD_AUTHORITATIVE_STARTUP_SMOKE_TEST_ENABLED = True
LIVE_DASHBOARD_AUTHORITATIVE_STARTUP_SMOKE_MAX_EDGES_PER_ASSET = 1
# Deprecated compatibility flags are hard-disabled and ignored by the updated
# orchestrator. They remain only so stale private settings cannot re-enable them.
LIVE_DASHBOARD_INCREMENTAL_SHARED_FEATURES_ENABLED = False
LIVE_DASHBOARD_RUNTIME_STATEFUL_CONTINUATION_ENABLED = False
LIVE_DASHBOARD_RUNTIME_STATE_PERSISTENCE_ENABLED = False
LIVE_DASHBOARD_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED = False
LIVE_DASHBOARD_LIVE_EVENT_ONLY_RUNTIME_API_ENABLED = False
LIVE_DASHBOARD_RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED = False
LIVE_DASHBOARD_CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME = False

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
    asset: 20 for asset in MT5_SYMBOL_MAP
}

FREEZE_THRESHOLD_MIN_TICKS = 1
FREEZE_THRESHOLD_MIN_LIVE_SIM_BARS = 20
FREEZE_RESEARCH_CADENCE_TOLERANCE = 0.35
FREEZE_ALLOW_FALLBACK_1_VOLUME = False
LIVE_BAR_CADENCE_MONITORING_ENABLED = True
LIVE_BAR_CADENCE_TARGET_BARS_PER_DAY = 50.0
LIVE_BAR_CADENCE_WARNING_TOLERANCE = 0.35
LIVE_BAR_CADENCE_BLOCK_TOLERANCE = 0.75
LIVE_BAR_CADENCE_WARNING_MIN_COMPLETED_DAYS = 5
LIVE_BAR_CADENCE_BLOCK_MIN_COMPLETED_DAYS = 10
LIVE_BAR_CADENCE_BLOCK_ASSET_ON_SUSTAINED_DRIFT = True
LIVE_BAR_CADENCE_SUSTAINED_BLOCK_ENABLED = LIVE_BAR_CADENCE_BLOCK_ASSET_ON_SUSTAINED_DRIFT

# -----------------------------------------------------------------------------
# Execution-bot controls preserved from V1 but kept unarmed by default.
# -----------------------------------------------------------------------------
PORTFOLIO_EXECUTION_POLL_SECONDS = 1.0
PORTFOLIO_EXECUTION_HEARTBEAT_SECONDS = 5.0
PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 120
PORTFOLIO_EXECUTION_FUTURE_SIGNAL_TOLERANCE_SECONDS = 5.0
PORTFOLIO_INCLUDE_UNMANAGED_POSITIONS_IN_RISK = True
PORTFOLIO_BLOCK_IF_UNKNOWN_OPEN_RISK = True
PORTFOLIO_ENFORCE_BROKER_MIN_STOP_LEVEL = True
PORTFOLIO_ALLOW_WIDEN_TO_BROKER_MIN_STOP = False
PORTFOLIO_STATIC_FLOOR_BUFFER_USD = 0.0
PORTFOLIO_BLOCK_AT_TARGET_BALANCE = True
PORTFOLIO_ALLOW_MIN_LOT_FALLBACK = True
PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARD_PER_ASSET = True
PORTFOLIO_INCLUDE_ALL_PENDING_ORDERS_IN_RISK = True
PORTFOLIO_INCLUDE_PENDING_ORDERS_IN_RISK = PORTFOLIO_INCLUDE_ALL_PENDING_ORDERS_IN_RISK
PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARDS = PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARD_PER_ASSET
PORTFOLIO_BLOCK_IF_UNKNOWN_PENDING_RISK = True
# QUANTARA_OPPOSITE_DIRECTION_SAME_SYMBOL_GUARD_V1
# Account-policy overlay. True blocks a new signal when the exact broker
# symbol already has open exposure in the opposite direction. Same-side
# stacking remains governed by the normal allocator/live risk contract.
# Set False for accounts that permit simultaneous opposite exposure.
PORTFOLIO_BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL = True
PORTFOLIO_INITIAL_EQUITY_HIGH_WATERMARK: float | None = None
PORTFOLIO_EXPECTED_ACCOUNT_LOGIN: int | None = None
PORTFOLIO_EXPECTED_ACCOUNT_SERVER: str = ""
PORTFOLIO_EXPECTED_ACCOUNT_CURRENCY: str = "USD"
PORTFOLIO_EXPECTED_STARTING_CAPITAL: float | None = None
PORTFOLIO_VERTICAL_DEFER_LOG_COOLDOWN_SECONDS = 300.0
PORTFOLIO_MARKET_CLOSED_COOLDOWN_SECONDS = 900.0
PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET = {
    asset: (30.0 if asset != "BTCUSD" else 15.0) for asset in MT5_SYMBOL_MAP
}
# Broker-specific UTC maintenance windows may be supplied locally.  The live
# executor also blocks on broker trade-mode, stale-tick, and market-closed
# cooldown signals, so an empty default does not cause blind order submission.
PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET: dict[str, list[dict[str, Any]]] = {}
PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET = {
    asset: {
        "friday_block_hour_utc": 20.0,
        "monday_resume_hour_utc": 0.0,
    }
    for asset in MT5_SYMBOL_MAP
    if asset != "BTCUSD"
}
# The observed weekend stop-gap clusters warrant an earlier no-new-entry
# cutoff for these two markets. Existing positions remain broker-SL protected.
for _weekend_gap_asset in ("GBPJPY", "USOIL"):
    PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET[_weekend_gap_asset][
        "friday_block_hour_utc"
    ] = 18.0
del _weekend_gap_asset
PORTFOLIO_CLOSED_TRADE_HISTORY_OVERLAP_DAYS = 7
PORTFOLIO_CLOSED_TRADE_HISTORY_LOOKBACK_DAYS = 120
# Deprecated compatibility flag only.
# Git/release cleanliness is operational provenance, not execution authority.
# Real-live arming is governed by the manifest, account/risk, bar policy,
# execution-state integrity and broker/runtime safety gates.
LIVE_REQUIRE_IMMUTABLE_GIT_RELEASE = False

LIVE_LOG_ROTATION_MAX_BYTES = 25_000_000
LIVE_LOG_ROTATION_BACKUP_COUNT = 8

PORTFOLIO_EXECUTION_GUARDS_BY_ASSET = {
    "XAUUSD": {"max_spread_points": 90.0, "entry_drift_limit_price": 6.0, "deviation_points": 100, "max_lot_cap": 10.0},
    "BTCUSD": {"max_spread_points": 9000.0, "entry_drift_limit_price": 300.0, "deviation_points": 2000, "max_lot_cap": 10.0},
    "NASDAQ": {"max_spread_points": 350.0, "entry_drift_limit_price": 55.0, "deviation_points": 220, "max_lot_cap": 10.0},
    "DAX": {"max_spread_points": 350.0, "entry_drift_limit_price": 55.0, "deviation_points": 220, "max_lot_cap": 10.0},
    "US500": {"max_spread_points": 180.0, "entry_drift_limit_price": 18.0, "deviation_points": 150, "max_lot_cap": 10.0},
    "HK": {"max_spread_points": 500.0, "entry_drift_limit_price": 100.0, "deviation_points": 300, "max_lot_cap": 10.0},
    "JPN": {"max_spread_points": 500.0, "entry_drift_limit_price": 120.0, "deviation_points": 300, "max_lot_cap": 10.0},
    "USOIL": {"max_spread_points": 120.0, "entry_drift_limit_price": 1.5, "deviation_points": 120, "max_lot_cap": 10.0},
    "USDMXN": {"max_spread_points": 500.0, "entry_drift_limit_price": 0.08, "deviation_points": 200, "max_lot_cap": 10.0},
    "GBPJPY": {"max_spread_points": 120.0, "entry_drift_limit_price": 0.6, "deviation_points": 120, "max_lot_cap": 10.0},
}
PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET = {asset: cfg["max_spread_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
PORTFOLIO_ENTRY_DRIFT_LIMIT_PRICE_BY_ASSET = {asset: cfg["entry_drift_limit_price"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
# Optional adaptive entry drift. Static drift remains the base guard; assets
# with a positive fraction may use max(static_limit, planned_SL_distance *
# fraction), capped by the corresponding max-cap value. Only XAUUSD is widened
# by default because its recent missed signals had large planned SL distances.
PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET = {
    asset: 0.15 for asset in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET
}
PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET["XAUUSD"] = 0.18
PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET = {
    asset: float(cfg["entry_drift_limit_price"]) * 1.75
    for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()
}
PORTFOLIO_DEVIATION_POINTS_BY_ASSET = {asset: cfg["deviation_points"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}
PORTFOLIO_MAX_LOT_CAP_BY_ASSET = {asset: cfg["max_lot_cap"] for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()}

# Research/live spread-parity guard (2026-09-18). PORTFOLIO_MAX_SPREAD_POINTS_BY_ASSET
# above is a catastrophic-blowout ceiling only, deliberately generous and
# disconnected from research -- it does not verify that live spread actually
# resembles the Dukascopy historical spread each edge's Gate A/B/C/D verdict
# was earned under (see config/mldp_deviation_register.json and the
# 2026-09-18 research/live spread parity conversation for the full
# derivation).
#
# REVISED 2026-09-24: RESEARCH_COST_BPS_BY_ASSET moved from a p95-p99 tail
# band to the Dukascopy MEDIAN bid/ask spread, computed from the FULL tick
# history per asset (every weekly file, no sampling -- see
# shared/research/compute_full_history_spread_bps.py and
# config/us500_edge_search_ledger.json's
# per_asset_empirical_spread_bps_2026-09-24_full_history entry for the exact
# median/p95/p99/n_ticks per asset). Reasoning: the tail-risk role the old
# p95-p99 assumption was playing is already covered by
# RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET below (a REAL-TIME check against
# live spread), so padding the RESEARCH assumption itself with the same tail
# margin was double-charging every edge's cost model and was confirmed
# (2026-09-24) to be silently killing legitimate short-barrier/high-turnover
# candidates in Gate B ranking that had strong gross_R before cost (e.g.
# DAX/trend_long's 5-bar lookback: gross_mean_R=+0.101, killed to
# net_mean_R=-0.072 purely by the old 3.83bps p99-adjacent assumption on a
# ~22bps stop). This is the SAME single number used for Gate B's --cost-bps
# ranking AND this live enforcement constant -- there is deliberately no
# second "tail" number anywhere; an edge tested/ranked at cost X is enforced
# live at cost X, with the multiplier below acting only as a real-time
# circuit-breaker, never as an alternate cost assumption.
# spread_parity_guard() in portfolio_execution_bot.py compares live spread
# (computed fresh each signal from the live tick's own bid/ask/mid -- never
# persisted as raw tick history, just a small bounded rolling window) against
# this reference in bps space directly (no bps<->points static conversion,
# which would go stale as price moves -- the live tick's own current mid
# price is used every check instead, which is the equivalent of doing that
# conversion fresh each time). It blocks NEW entries only (open positions are
# untouched) while live spread runs materially above the researched
# assumption, and recovers automatically once it normalises.
RESEARCH_COST_BPS_BY_ASSET = {
    "XAUUSD": 1.72,
    "JPN": 1.84,
    "US500": 1.03,
    "NASDAQ": 1.12,
    "DAX": 1.01,
    "HK": 4.66,
    "GBPJPY": 1.17,
    "USDMXN": 2.77,
    "USOIL": 6.01,
    "BTCUSD": 17.17,
}
# How many times the researched cost_bps a rolling live spread reading may
# run before the parity guard blocks new entries. Research cost_bps is now
# the Dukascopy MEDIAN (not a pre-padded tail band, see note above), so this
# multiplier is the ONLY tail-risk margin in the system -- it is what catches
# a genuine broker-spread-regime mismatch (or a live spread blowout around
# news/illiquidity), not ordinary tick-to-tick noise around the median.
RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET = {asset: 1.5 for asset in RESEARCH_COST_BPS_BY_ASSET}
# Rolling window of recent live spread_bps readings kept per asset in live
# state -- bounded, never grows without limit, and never written to disk as
# raw tick data (see module docstring above this block).
RESEARCH_SPREAD_PARITY_WINDOW = 20
# Below this many observations for an asset, use the instantaneous live
# spread reading instead of a rolling median (cold-start fallback).
RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS = 5

# Telegram defaults — override locally.
TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_IDS: list[str] = []
PORTFOLIO_SEND_STARTUP_TELEGRAM = False
PORTFOLIO_SEND_EXECUTION_TELEGRAM = False
PORTFOLIO_SEND_REJECTION_TELEGRAM = False

# Backward-compatible aliases for ported V1 live scripts.
LIVE_DASHBOARD_RUN_MODE = PORTFOLIO_DASHBOARD_RUN_MODE
LIVE_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS
LIVE_DASHBOARD_FAIL_IF_GAP_NOT_FULLY_HEALED = PORTFOLIO_DASHBOARD_FAIL_IF_PARTIAL_GAP_HEAL


def _apply_compute_profile(profile_name: str) -> None:
    name = str(profile_name or "local_safe").strip().lower()
    if name == "custom":
        globals()["PORTFOLIO_COMPUTE_PROFILE"] = name
        return
    if name not in _COMPUTE_PROFILES:
        raise ValueError(
            f"Unsupported portfolio_compute_profile={profile_name!r}; "
            f"expected one of {sorted([*_COMPUTE_PROFILES, 'custom'])}"
        )
    globals()["PORTFOLIO_COMPUTE_PROFILE"] = name
    for key, value in _COMPUTE_PROFILES[name].items():
        globals()[key] = value


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
    requested_profile = local.get("portfolio_compute_profile", local.get("PORTFOLIO_COMPUTE_PROFILE", PORTFOLIO_COMPUTE_PROFILE))
    _apply_compute_profile(str(requested_profile))
    simple_keys = [
        "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "MT5_TIMEOUT_MS",
        "LIVE_MODE", "ALLOW_REAL_LIVE_TRADING_EXPORT", "ENABLE_ORDER_SEND",
        "PORTFOLIO_EXECUTION_MODE", "PORTFOLIO_MAGIC_NUMBER", "PORTFOLIO_ACCOUNT_STAGE",
        "STRICT_RECENT_TICK_CHECK", "FAIL_IF_MANIFEST_NOT_LIVE_READY",
        "FAIL_IF_SYMBOL_NOT_TRADEABLE", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_IDS",
        "PORTFOLIO_SEND_STARTUP_TELEGRAM", "PORTFOLIO_SEND_EXECUTION_TELEGRAM",
        "PORTFOLIO_SEND_REJECTION_TELEGRAM",
        "LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED", "LIVE_DASHBOARD_ASSET_SCORING_WORKERS",
        "LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED", "LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET",
        "LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS",
        "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_ENABLED", "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS",
        "LIVE_DASHBOARD_RECOVERY_COOLDOWN_SECONDS",
        "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_ENABLED", "LIVE_DASHBOARD_SHARED_FEATURE_CACHE_ENABLED",
        "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_CACHE_ENABLED", "LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED",
        "LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR", "THRESHOLD_DIAGNOSTIC_ASSET_WORKERS",
        "LIVE_RESEARCH_FFD_WINDOW_BARS", "LIVE_RESEARCH_FFD_LIVE_BUFFER_BARS",
        "LIVE_RESEARCH_HMM_TRAIN_WINDOW_ROWS", "LIVE_RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS",
        "LIVE_RESEARCH_HMM_REFIT_FREQUENCY_BARS", "LIVE_RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS",
        "LIVE_DASHBOARD_RESEARCH_RECOVERY_RAW_BARS",
        "LIVE_DASHBOARD_REQUIRE_POSITIVE_AFML_FOR_APPROVAL",
        "LIVE_DASHBOARD_STRICT_SHARED_FEATURE_CONTRACT_ENABLED",
        "LIVE_DASHBOARD_STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED",
        "LIVE_DASHBOARD_RAW_TICK_DAILY_PROFILE_ENABLED", "LIVE_DASHBOARD_RAW_TICK_CURSOR_SIGNATURE_LIMIT",
        "PORTFOLIO_INITIAL_EQUITY_HIGH_WATERMARK", "PORTFOLIO_EXPECTED_ACCOUNT_LOGIN",
        "PORTFOLIO_EXPECTED_ACCOUNT_SERVER", "PORTFOLIO_EXPECTED_ACCOUNT_CURRENCY",
        "PORTFOLIO_EXPECTED_STARTING_CAPITAL", "PORTFOLIO_INCLUDE_ALL_PENDING_ORDERS_IN_RISK",
        "PORTFOLIO_BLOCK_IF_UNKNOWN_PENDING_RISK", "PORTFOLIO_BLOCK_OPPOSITE_DIRECTION_SAME_SYMBOL", "PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARD_PER_ASSET",
        "PORTFOLIO_VERTICAL_DEFER_LOG_COOLDOWN_SECONDS",
        "PORTFOLIO_MARKET_CLOSED_COOLDOWN_SECONDS",
        "PORTFOLIO_CLOSED_TRADE_HISTORY_OVERLAP_DAYS", "PORTFOLIO_CLOSED_TRADE_HISTORY_LOOKBACK_DAYS",
        "LIVE_BAR_CADENCE_MONITORING_ENABLED", "LIVE_BAR_CADENCE_WARNING_TOLERANCE",
        "LIVE_BAR_CADENCE_BLOCK_TOLERANCE", "LIVE_BAR_CADENCE_WARNING_MIN_COMPLETED_DAYS",
        "LIVE_BAR_CADENCE_BLOCK_MIN_COMPLETED_DAYS", "LIVE_BAR_CADENCE_BLOCK_ASSET_ON_SUSTAINED_DRIFT",
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
        globals_map["PORTFOLIO_ENTRY_DRIFT_MAX_CAP_PRICE_BY_ASSET"] = {
            asset: cfg.get("entry_drift_max_cap_price", max(float(cfg["entry_drift_limit_price"]), 1e-9) * 1.75)
            for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()
        }
        globals_map["PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET"] = {
            asset: cfg.get("entry_drift_fraction_of_sl", PORTFOLIO_ENTRY_DRIFT_FRACTION_OF_SL_BY_ASSET.get(asset, 0.15))
            for asset, cfg in PORTFOLIO_EXECUTION_GUARDS_BY_ASSET.items()
        }
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
    tick_ages = local.get("portfolio_max_broker_tick_age_seconds_by_asset") or local.get("PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET")
    if isinstance(tick_ages, dict):
        PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET.update({
            str(k): float(v) for k, v in tick_ages.items()
        })
    research_cost_bps = local.get("research_cost_bps_by_asset") or local.get("RESEARCH_COST_BPS_BY_ASSET")
    if isinstance(research_cost_bps, dict):
        RESEARCH_COST_BPS_BY_ASSET.update({str(k): float(v) for k, v in research_cost_bps.items()})
    parity_multipliers = local.get("research_spread_parity_multiplier_by_asset") or local.get("RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET")
    if isinstance(parity_multipliers, dict):
        RESEARCH_SPREAD_PARITY_MULTIPLIER_BY_ASSET.update({str(k): float(v) for k, v in parity_multipliers.items()})
    parity_window = local.get("research_spread_parity_window") or local.get("RESEARCH_SPREAD_PARITY_WINDOW")
    if parity_window is not None:
        globals_map["RESEARCH_SPREAD_PARITY_WINDOW"] = int(parity_window)
    parity_min_obs = local.get("research_spread_parity_min_observations") or local.get("RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS")
    if parity_min_obs is not None:
        globals_map["RESEARCH_SPREAD_PARITY_MIN_OBSERVATIONS"] = int(parity_min_obs)
    blackout_windows = local.get("portfolio_entry_blackout_windows_utc_by_asset") or local.get("PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET")
    if isinstance(blackout_windows, dict):
        globals_map["PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET"] = blackout_windows
    weekend_guards = local.get("portfolio_weekend_entry_guard_by_asset") or local.get("PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET")
    if isinstance(weekend_guards, dict):
        globals_map["PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET"] = weekend_guards
    # Local settings may not reactivate retired native/incremental paths.
    globals_map["LIVE_DASHBOARD_INCREMENTAL_SHARED_FEATURES_ENABLED"] = False
    globals_map["LIVE_DASHBOARD_RUNTIME_STATEFUL_CONTINUATION_ENABLED"] = False
    globals_map["LIVE_DASHBOARD_RUNTIME_STATE_PERSISTENCE_ENABLED"] = False
    globals_map["LIVE_DASHBOARD_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED"] = False
    globals_map["LIVE_DASHBOARD_LIVE_EVENT_ONLY_RUNTIME_API_ENABLED"] = False
    globals_map["LIVE_DASHBOARD_RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED"] = False
    globals_map["LIVE_DASHBOARD_CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME"] = False
    globals_map["PORTFOLIO_INCLUDE_PENDING_ORDERS_IN_RISK"] = bool(globals_map.get("PORTFOLIO_INCLUDE_ALL_PENDING_ORDERS_IN_RISK", True))
    globals_map["PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARDS"] = bool(globals_map.get("PORTFOLIO_REQUIRE_EXPLICIT_EXECUTION_GUARD_PER_ASSET", True))
    globals_map["LIVE_BAR_CADENCE_SUSTAINED_BLOCK_ENABLED"] = bool(globals_map.get("LIVE_BAR_CADENCE_BLOCK_ASSET_ON_SUSTAINED_DRIFT", True))
    globals_map["LIVE_DASHBOARD_RUN_MODE"] = PORTFOLIO_DASHBOARD_RUN_MODE
    globals_map["LIVE_DASHBOARD_MAX_SIGNAL_AGE_SECONDS"] = PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS
    globals_map["LIVE_DASHBOARD_FAIL_IF_GAP_NOT_FULLY_HEALED"] = PORTFOLIO_DASHBOARD_FAIL_IF_PARTIAL_GAP_HEAL


_apply_compute_profile(PORTFOLIO_COMPUTE_PROFILE)
_apply_local_settings()


def _apply_active_account_authority() -> None:
    """Make the single active-account JSON authoritative over stale local duplicates."""
    if not ACTIVE_ACCOUNT_RULES_PATH.exists():
        raise FileNotFoundError(
            f"Missing active account rules: {ACTIVE_ACCOUNT_RULES_PATH}"
        )
    payload = json.loads(ACTIVE_ACCOUNT_RULES_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("active_account_rules.json must contain a JSON object")
    stage = str(payload.get("account_stage") or "").strip().lower()
    starting_capital = payload.get("initial_balance")
    current_balance = payload.get("current_balance", starting_capital)
    current_high_watermark = payload.get("current_high_watermark")
    if not stage:
        raise ValueError("active_account_rules.json account_stage cannot be empty")
    try:
        starting_capital = float(starting_capital)
        current_balance = float(current_balance)
        current_high_watermark = (
            max(starting_capital, current_balance)
            if current_high_watermark is None
            else float(current_high_watermark)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "active_account_rules.json initial_balance/current_balance/current_high_watermark must be numeric"
        ) from exc
    if starting_capital <= 0:
        raise ValueError("active_account_rules.json initial_balance must be positive")
    if current_balance < 0:
        raise ValueError("active_account_rules.json current_balance cannot be negative")
    current_high_watermark = max(
        starting_capital,
        current_balance,
        current_high_watermark,
    )
    globals()["PORTFOLIO_ACCOUNT_STAGE"] = stage
    globals()["PORTFOLIO_EXPECTED_STARTING_CAPITAL"] = starting_capital
    # The same active JSON supplies the live HWM seed.  A stale value left in
    # local_live_settings.json from another account or phase may not override it.
    globals()["PORTFOLIO_INITIAL_EQUITY_HIGH_WATERMARK"] = current_high_watermark


_apply_active_account_authority()


def assert_order_send_armed_by_manifest(live_manifest: dict[str, Any]) -> None:
    """Fail closed unless local arming and the generic live contract agree."""
    if not ENABLE_ORDER_SEND:
        raise RuntimeError("ENABLE_ORDER_SEND=False. Real order_send is not armed.")
    if PORTFOLIO_EXECUTION_MODE != "live":
        raise RuntimeError(
            f"PORTFOLIO_EXECUTION_MODE={PORTFOLIO_EXECUTION_MODE!r}. Must be 'live' for real orders."
        )
    if LIVE_MODE != "REAL_LIVE" or not ALLOW_REAL_LIVE_TRADING_EXPORT:
        raise RuntimeError(
            "LIVE_MODE/ALLOW_REAL_LIVE_TRADING_EXPORT do not arm real live trading."
        )
    if live_manifest.get("deployment_status") != "LIVE_APPROVED":
        raise RuntimeError(
            "live_manifest deployment_status is not LIVE_APPROVED: "
            f"{live_manifest.get('deployment_status')}"
        )
    if (
        live_manifest.get("live_ready") is not True
        or live_manifest.get("live_trade_enabled") is not True
    ):
        raise RuntimeError("live_manifest is not live_ready/live_trade_enabled.")
    if live_manifest.get("stress_validation_status") != "STRESS_PASS":
        raise RuntimeError(
            "live_manifest stress_validation_status must be STRESS_PASS for real orders."
        )
    if live_manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION:
        raise RuntimeError(
            f"live_manifest risk_contract_version={live_manifest.get('risk_contract_version')!r}; "
            f"expected {RISK_CONTRACT_VERSION!r}."
        )

    account_rules = live_manifest.get("active_account_rules") or {}
    if not isinstance(account_rules, dict) or not account_rules:
        raise RuntimeError("live_manifest is missing active_account_rules.")
    manifest_profile = str(live_manifest.get("account_rule_profile") or "").strip()
    manifest_stage = str(
        live_manifest.get("approved_account_stage")
        or live_manifest.get("account_stage")
        or ""
    ).strip().lower()
    rules_profile = str(account_rules.get("profile_label") or "").strip()
    rules_stage = str(account_rules.get("account_stage") or "").strip().lower()
    if not manifest_profile or manifest_profile != rules_profile:
        raise RuntimeError(
            f"Manifest/account-rules profile mismatch: {manifest_profile!r} != {rules_profile!r}."
        )
    if not manifest_stage or manifest_stage != rules_stage:
        raise RuntimeError(
            f"Manifest/account-rules stage mismatch: {manifest_stage!r} != {rules_stage!r}."
        )

    expected_scope = f"active_account_rules:{manifest_profile}:{manifest_stage}"
    if str(live_manifest.get("live_approval_scope")) != expected_scope:
        raise RuntimeError(
            "live_manifest live_approval_scope does not match the active generic account contract."
        )

    local_stage = str(PORTFOLIO_ACCOUNT_STAGE or "").strip().lower()
    if local_stage and manifest_stage != local_stage:
        raise RuntimeError(
            f"Local account stage {local_stage!r} does not match live_manifest stage {manifest_stage!r}."
        )

    symbol_rules = account_rules.get("daily_symbol_risk") or {}
    if bool(live_manifest.get("daily_symbol_risk_rule_enabled", False)) != bool(
        symbol_rules.get("enabled", False)
    ):
        raise RuntimeError(
            "live_manifest daily-symbol-risk state differs from active_account_rules."
        )
