from __future__ import annotations

"""V2 portfolio live dashboard orchestrator.

SOTD role
---------
This process is the live signal dashboard/orchestrator. It may run continuously
beside the execution bot, but it never sends orders.

Preserved from V1:
- MT5 tick polling;
- broker/server time normalization;
- quote-tick bar construction;
- persistent state/cursors;
- startup gap healing as memory-only bars;
- duplicate/stale/future signal gates;
- audit + approved-signal exports;
- research-seed -> live-bar continuity for stateful features.

Changed for V2/SOTD:
- verified live-streaming FFD/HMM hotpath is integrated directly into this sole dashboard entrypoint;
- selected edges come only from portfolio_live/live_manifest.json;
- bar policy comes only from portfolio_live/config/live_bar_policy.json;
- edge-specific features/events/scoring are owned by each edge runtime.py;
- dashboard never discovers feature factories, selected features, models, or calibrators;
- final live approval uses allocator-selected final_live_threshold from live_manifest.
"""

import argparse
import ast
import copy
import errno
from collections import Counter
import hashlib
import inspect
import json
import logging
import math
import os
import pickle
import queue
import random
import sys
import threading
import time
import traceback
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any

# Prevent nested BLAS/OpenMP/XGBoost oversubscription on the live host.
# Explicit operator environment values remain authoritative.
for _thread_env_name in (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_thread_env_name, "1")

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for path in (LIVE_DIR, V2_ROOT, REPO_ROOT):
    text = str(path)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)

import live_settings as settings  # type: ignore  # noqa: E402

try:
    from shared.edges.native_one_row_event_engine import is_expected_empty_event_exception
except Exception:
    def is_expected_empty_event_exception(exc: BaseException) -> bool:
        return False

from common import (  # type: ignore  # noqa: E402
    broker_symbol_for_asset,
    ensure_dirs,
    import_edge_runtime as _base_import_edge_runtime,
    init_mt5,
    load_live_manifest,
    read_json,
    setup_logger,
    shutdown_mt5,
    write_json,
)
from shared.live_streaming.hotpath import (  # noqa: E402
    HOTPATH_VERSION,
    cache_status as live_streaming_cache_status,
    install_hotpath_verifiers,
    patch_runtime_shared_loaders,
    verify_asset_ffd_manifest_hot,
    verify_asset_hmm_regime_manifest_hot,
)

_HOTPATH_INSTALL_REPORT = install_hotpath_verifiers()


def import_edge_runtime(edge: dict[str, Any]):
    """Import an edge runtime and attach the verified shared-loader hotpath."""
    runtime = _base_import_edge_runtime(edge)
    report = patch_runtime_shared_loaders(runtime)
    setattr(runtime, "LIVE_STREAMING_HOTPATH_REPORT", report)
    return runtime


SCHEMA_VERSION = "v2.1"
DASHBOARD_VERSION = (
    "2026-07-02_v2_native_continuation_latency_fix+rolling_window_continuation_v1"
    "+interval_relative_gap_lookback_v1+monotonic_live_tick_cursor_v1"
    "+atomic_dashboard_ack_v1+durable_live_bar_journal_v1"
    "+direct_full_authority_routing_v1+sequential_scoring_stability_v1"
    "+candidate_model_input_guard_v1"
    f"+{HOTPATH_VERSION}+unified_dashboard"
)
EXECUTION_SIGNAL_CONTRACT_VERSION = "v2_dashboard_to_execution_bot_calibrated_afml_broker_executable"
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_broker_executable"
EXPECTED_ACCOUNT_STAGE = "challenge"
EXPECTED_APPROVAL_SCOPE = "atmos_nova_100k_challenge_only"

OUTPUT_DIR = settings.OUTPUT_DIR / "live_dashboard"
RUNTIME_SCORE_ROWS_DIR = OUTPUT_DIR / "runtime_score_rows"
AUDIT_CSV = OUTPUT_DIR / "live_signal_audit.csv"
APPROVED_SIGNALS_CSV = OUTPUT_DIR / "live_approved_signals.csv"
APPROVED_SIGNALS_JSONL = OUTPUT_DIR / "live_approved_signals.jsonl"
STATUS_JSON = OUTPUT_DIR / "latest_dashboard_status.json"
BAR_ACK_JSON = OUTPUT_DIR / "latest_dashboard_bar_ack.json"
GAP_REPORT_JSON = OUTPUT_DIR / "gap_healing_report.json"
GAP_REPORT_CSV = OUTPUT_DIR / "gap_healing_report.csv"
LOG_PATH = settings.LIVE_LOGS_DIR / "portfolio_live_dashboard_orchestrator.log"
STATE_PATH = settings.LIVE_STATE_DIR / "portfolio_live_dashboard_orchestrator_state.json"
LIVE_BAR_POLICY_PATH = settings.LIVE_CONFIG_DIR / "live_bar_policy.json"
RUNTIME_CONTINUATION_STATE_DIR = settings.LIVE_STATE_DIR / "runtime_continuation"
PENDING_LIVE_BAR_DIR = settings.LIVE_STATE_DIR / "pending_live_bars"

MAX_SIGNAL_AGE_SECONDS = int(getattr(settings, "PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS", getattr(settings, "LIVE_DASHBOARD_MAX_SIGNAL_AGE_SECONDS", 300)))
FUTURE_EVENT_BLOCK_TOLERANCE_SECONDS = float(getattr(settings, "LIVE_DASHBOARD_FUTURE_EVENT_BLOCK_TOLERANCE_SECONDS", 5.0))
TICK_POLL_SECONDS = float(getattr(settings, "TICK_POLL_SECONDS", 1.0))
TICK_BATCH_LOOKBACK_SECONDS = int(getattr(settings, "TICK_BATCH_LOOKBACK_SECONDS", 30))
LIVE_TICK_RANGE_FALLBACK_STALE_SECONDS = float(
    getattr(settings, "LIVE_DASHBOARD_TICK_RANGE_FALLBACK_STALE_SECONDS", max(TICK_BATCH_LOOKBACK_SECONDS, 30))
)
LIVE_TICK_RANGE_FALLBACK_MAX_LOOKBACK_HOURS = float(
    getattr(settings, "LIVE_DASHBOARD_TICK_RANGE_FALLBACK_MAX_LOOKBACK_HOURS", 24.0)
)
LIVE_TICK_RANGE_FALLBACK_END_LEEWAY_SECONDS = float(
    getattr(settings, "LIVE_DASHBOARD_TICK_RANGE_FALLBACK_END_LEEWAY_SECONDS", 5.0)
)
LIVE_TICK_RANGE_RECOVERY_COOLDOWN_SECONDS = float(
    getattr(settings, "LIVE_DASHBOARD_TICK_RANGE_RECOVERY_COOLDOWN_SECONDS", 15.0)
)
HEARTBEAT_EVERY_N_LOOPS = int(getattr(settings, "HEARTBEAT_EVERY_N_LOOPS", 30))
SAVE_STATE_EVERY_N_CLOSED_BARS = int(getattr(settings, "SAVE_STATE_EVERY_N_CLOSED_BARS", 1))
MIN_LIVE_BARS_FOR_RUNTIME = int(getattr(settings, "LIVE_DASHBOARD_MIN_LIVE_BARS", 1))

# Research-derived state contract. These are the constants used by the shared
# research pipeline across XAUUSD, USTECH and BTCUSD. They are deliberately
# separated because they represent different kinds of memory; 8,000 was only an
# old live safety total, while 1,000 alone cannot reproduce a 5,000-row HMM refit.
RESEARCH_FFD_WINDOW_BARS = int(getattr(settings, "LIVE_RESEARCH_FFD_WINDOW_BARS", 1000))
RESEARCH_FFD_LIVE_BUFFER_BARS = int(getattr(settings, "LIVE_RESEARCH_FFD_LIVE_BUFFER_BARS", RESEARCH_FFD_WINDOW_BARS + 10))
RESEARCH_HMM_TRAIN_WINDOW_ROWS = int(getattr(settings, "LIVE_RESEARCH_HMM_TRAIN_WINDOW_ROWS", 5000))
RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS = int(getattr(settings, "LIVE_RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS", 1000))
RESEARCH_HMM_REFIT_FREQUENCY_BARS = int(getattr(settings, "LIVE_RESEARCH_HMM_REFIT_FREQUENCY_BARS", 1000))
RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS = int(getattr(settings, "LIVE_RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS", 200))
# A cold rebuild needs 5,000 post-FFD rows plus the current observation:
# raw_rows - (FFD_WINDOW - 1) >= HMM_TRAIN_WINDOW + 1.
RESEARCH_COLD_BOOTSTRAP_RAW_BARS = max(
    RESEARCH_FFD_WINDOW_BARS + RESEARCH_HMM_TRAIN_WINDOW_ROWS,
    RESEARCH_FFD_LIVE_BUFFER_BARS,
)
RESEARCH_HMM_INFERENCE_RAW_BARS = RESEARCH_FFD_WINDOW_BARS + RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS - 1
RESEARCH_LIVE_PARITY_CONTRACT = {
    "priority": "research_live_parity_before_latency",
    "feature_math_owner": "edge_runtime",
    "ffd_window_bars": RESEARCH_FFD_WINDOW_BARS,
    "ffd_live_buffer_bars": RESEARCH_FFD_LIVE_BUFFER_BARS,
    "hmm_train_window_rows": RESEARCH_HMM_TRAIN_WINDOW_ROWS,
    "hmm_inference_lookback_rows": RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS,
    "hmm_refit_frequency_bars": RESEARCH_HMM_REFIT_FREQUENCY_BARS,
    "max_finite_feature_lookback_bars": RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS,
    "cold_bootstrap_raw_bars": RESEARCH_COLD_BOOTSTRAP_RAW_BARS,
    "normal_hot_path": "runtime_owned_stateful_continuation",
    "raw_history_rebuild_role": "bootstrap_recovery_and_periodic_parity_audit_only",
    "legacy_arbitrary_tail_search_enabled": False,
}
# Old LIVE_FEATURE_STATE_MIN_MEMORY_KEEP_BARS / MAX_COMBINED defaults of 8,000
# are intentionally not inherited: they encoded the retired additive safety
# estimate. A new explicit recovery override may raise (never lower) the
# evidence-based 6,000-bar cold-bootstrap requirement.
_configured_runtime_memory_bars = int(
    getattr(settings, "LIVE_DASHBOARD_RESEARCH_RECOVERY_RAW_BARS", RESEARCH_COLD_BOOTSTRAP_RAW_BARS)
)
DEFAULT_RUNTIME_MEMORY_BARS = max(_configured_runtime_memory_bars, RESEARCH_COLD_BOOTSTRAP_RAW_BARS)
MAX_COMBINED_BARS_FOR_RUNTIME = DEFAULT_RUNTIME_MEMORY_BARS
MAX_EVENTS_TO_SCORE_PER_EDGE = max(int(getattr(settings, "LIVE_DASHBOARD_MAX_EVENTS_TO_SCORE_PER_EDGE", 10)), 1)
GAP_HEALING_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_GAP_HEALING_ENABLED", True))
GAP_MAX_LOOKBACK_HOURS = float(getattr(settings, "LIVE_DASHBOARD_GAP_MAX_LOOKBACK_HOURS", 2160))
GAP_FETCH_CHUNK_HOURS = float(getattr(settings, "LIVE_DASHBOARD_GAP_FETCH_CHUNK_HOURS", 6))
MIN_GAP_SECONDS_TO_HEAL = float(getattr(settings, "LIVE_DASHBOARD_MIN_GAP_SECONDS_TO_HEAL", 5.0))
MAX_TICKS_PER_GAP_ASSET = int(getattr(settings, "LIVE_DASHBOARD_MAX_TICKS_PER_GAP_ASSET", 20_000_000))
FAIL_IF_GAP_NOT_FULLY_HEALED = bool(getattr(settings, "LIVE_DASHBOARD_FAIL_IF_GAP_NOT_FULLY_HEALED", True))
BROKER_TIME_AUTO_DETECT = bool(getattr(settings, "LIVE_DASHBOARD_BROKER_TIME_AUTO_DETECT", True))
BROKER_TIME_OFFSET_HOURS = getattr(settings, "LIVE_DASHBOARD_BROKER_TIME_OFFSET_HOURS", None)
BROKER_TIME_FUTURE_TOLERANCE_SECONDS = float(getattr(settings, "LIVE_DASHBOARD_BROKER_TIME_FUTURE_TOLERANCE_SECONDS", 900.0))
BROKER_TIME_MAX_REASONABLE_OFFSET_HOURS = int(getattr(settings, "LIVE_DASHBOARD_BROKER_TIME_MAX_REASONABLE_OFFSET_HOURS", 12))

# Adaptive live bar threshold controls. These mirror the research bar-forging
# policy: causal completed daily tick counts -> EWMA daily ticks -> target bars/day.
ADAPTIVE_THRESHOLD_REFRESH_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_ENABLED", True))
ADAPTIVE_THRESHOLD_REFRESH_EVERY_SECONDS = float(getattr(settings, "LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_REFRESH_EVERY_SECONDS", 300.0))
ADAPTIVE_THRESHOLD_TARGET_BARS_PER_DAY = int(getattr(settings, "THRESHOLD_TARGET_BARS_PER_DAY", 50))
ADAPTIVE_THRESHOLD_EWMA_SPAN_DAYS = int(getattr(settings, "DAILY_TICK_EWMA_SPAN_DAYS", 20))
ADAPTIVE_THRESHOLD_MIN_WARMUP_DAYS = int(getattr(settings, "MIN_WARMUP_DAYS", 20))
# No research/parity floor is imposed here. The old FREEZE_THRESHOLD_MIN_TICKS
# default of 10,000 could silently cap adaptive threshold behavior. Keep only
# a positive-tick safety guard because a threshold of 0 cannot form bars.
ADAPTIVE_THRESHOLD_MIN_TICKS = 1
ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS = int(getattr(settings, "LIVE_DASHBOARD_ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS", 120))
ADAPTIVE_THRESHOLD_WRITE_POLICY_UPDATES = bool(getattr(settings, "LIVE_DASHBOARD_WRITE_ADAPTIVE_THRESHOLD_UPDATES", True))

REQUIRED_BAR_COLUMNS = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
SCORE_COLUMNS = ["Datetime", "raw_probability", "calibrated_probability", "afml_bet_size"]
PASS_CONTINUITY_STATUSES = {"PASS_CONTINUOUS", "PASS_HEALED_CONTINUOUS", "PASS_NO_LIVE_BARS_YET"}
# "target" is intentionally not forbidden: in MLDP triple-barrier systems it can be
# the *dynamic current-event volatility target* used for SL/PT distance, not a
# future label. Future/label columns remain forbidden.
FORBIDDEN_RUNTIME_OUTPUT_COLUMNS = {"meta_label", "label", "y", "t1", "barrier_hit", "executed_r", "realized_r", "future_return", "future_r"}
EXECUTION_PLAN_VERSION = "v2_mldp_dynamic_barrier_plan_v1"
GAP_HEALED_BAR_LOG_EVERY_N = int(getattr(settings, "LIVE_DASHBOARD_GAP_HEALED_BAR_LOG_EVERY_N", 25))
LATENCY_STAGE_LOGGING_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_LATENCY_STAGE_LOGGING_ENABLED", True))
PARALLEL_EDGE_SCORING_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED", True))
EDGE_WORKERS_PER_ASSET = max(int(getattr(settings, "LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET", 4)), 1)
SHARED_FEATURE_PREWARM_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_ENABLED", True))
SHARED_FEATURE_CACHE_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_SHARED_FEATURE_CACHE_ENABLED", True))
SHARED_FEATURE_PREWARM_CACHE_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_SHARED_FEATURE_PREWARM_CACHE_ENABLED", True))
LIVE_EVENT_ONLY_RUNTIME_API_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_LIVE_EVENT_ONLY_RUNTIME_API_ENABLED", True))
LIVE_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED", True))
PRELOAD_EDGE_ARTIFACTS_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED", True))
FAIL_ON_PRELOAD_ERROR = bool(getattr(settings, "LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR", False))

# Dashboard-owned strict-parity gates. These do not change any edge feature,
# event, model, calibration, threshold, AFML, or geometry math. They only refuse
# unsafe cache reuse / approval and bound concurrency around the existing paths.
REQUIRE_POSITIVE_AFML_FOR_APPROVAL = bool(
    getattr(settings, "LIVE_DASHBOARD_REQUIRE_POSITIVE_AFML_FOR_APPROVAL", True)
)
STRICT_SHARED_FEATURE_CONTRACT_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_STRICT_SHARED_FEATURE_CONTRACT_ENABLED", True)
)
STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED", True)
)
SELECTED_FEATURE_NAN_DROP_ERROR_FRAGMENT = (
    "All candidate rows dropped after selected-feature NaN filtering."
)
INCOMPLETE_MODEL_INPUT_RETRY_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_INCOMPLETE_MODEL_INPUT_RETRY_ENABLED", True)
)
ASSET_SCORING_PARALLEL_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_ASSET_SCORING_PARALLEL_ENABLED", True)
)
ASSET_SCORING_WORKERS = max(
    int(getattr(settings, "LIVE_DASHBOARD_ASSET_SCORING_WORKERS", 2)),
    1,
)
RAW_TICK_DAILY_PROFILE_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_RAW_TICK_DAILY_PROFILE_ENABLED", True)
)
RAW_TICK_CURSOR_SIGNATURE_LIMIT = max(
    int(getattr(settings, "LIVE_DASHBOARD_RAW_TICK_CURSOR_SIGNATURE_LIMIT", 4096)),
    64,
)
LIVE_TICK_CURSOR_CONTRACT_VERSION = "v2_monotonic_live_cursor_startup_cutoff_v1"
LIVE_TICK_STARTUP_GUARD_ENABLED = bool(
    getattr(settings, "LIVE_DASHBOARD_TICK_STARTUP_GUARD_ENABLED", True)
)
LIVE_TICK_HISTORICAL_BATCH_WARN_SECONDS = max(
    float(getattr(settings, "LIVE_DASHBOARD_TICK_HISTORICAL_BATCH_WARN_SECONDS", 60.0)),
    1.0,
)
RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED = bool(
    getattr(settings, "LIVE_DASHBOARD_RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED", True)
)
RUNTIME_STATEFUL_REQUIRED_PROOF_FLAGS = (
    "streaming_parity_passed",
    "append_only_history",
    "restart_parity_passed",
    "hmm_refit_schedule_preserved",
    "deterministic_noise_contract_preserved",
)
RUNTIME_EVENT_REQUIRED_PROOF_FLAGS = (
    "streaming_parity_passed",
    "append_only_history",
    "restart_parity_passed",
    "event_state_continuity_preserved",
)

# Research-parity incremental shared-feature cache. The dashboard never computes
# FFD/HMM/EMA/Kalman/CUSUM itself. It asks a runtime-owned stateful API to replay
# the unchanged edge functions on a conservative suffix, accepts the update only
# after overlap parity, and retains full raw-history builds for cold bootstrap,
# recovery, actual-candidate confirmation, and periodic HMM-aligned audits.
INCREMENTAL_SHARED_FEATURES_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_SHARED_FEATURES_ENABLED", True))
INCREMENTAL_PARITY_REQUIRED = bool(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_PARITY_REQUIRED", True))
RUNTIME_STATEFUL_CONTINUATION_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_RUNTIME_STATEFUL_CONTINUATION_ENABLED", True))
RUNTIME_STATE_PERSISTENCE_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_RUNTIME_STATE_PERSISTENCE_ENABLED", True))
INCREMENTAL_TAIL_CANDIDATES: tuple[int, ...] = tuple()  # compatibility/status only; intentionally unused
INCREMENTAL_OVERLAP_CHECK_ROWS = max(
    int(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_OVERLAP_CHECK_ROWS", RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS)),
    RESEARCH_MAX_FINITE_FEATURE_LOOKBACK_BARS,
)
INCREMENTAL_FULL_REBUILD_EVERY_N_BARS = max(
    int(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_FULL_REBUILD_EVERY_N_BARS", RESEARCH_HMM_REFIT_FREQUENCY_BARS)),
    RESEARCH_HMM_REFIT_FREQUENCY_BARS,
)
_INCREMENTAL_EXCLUDED_RAW = getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_EXCLUDED_FAMILIES", ["absorption"])
if isinstance(_INCREMENTAL_EXCLUDED_RAW, str):
    INCREMENTAL_EXCLUDED_FAMILIES = {part.strip().lower() for part in _INCREMENTAL_EXCLUDED_RAW.split(",") if part.strip()}
else:
    INCREMENTAL_EXCLUDED_FAMILIES = {str(part).strip().lower() for part in (_INCREMENTAL_EXCLUDED_RAW or []) if str(part).strip()}
if not INCREMENTAL_EXCLUDED_FAMILIES:
    INCREMENTAL_EXCLUDED_FAMILIES = {"absorption"}
INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE = bool(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE", True))
CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME = bool(
    getattr(settings, "LIVE_DASHBOARD_CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME", True)
)
INCREMENTAL_NUMERIC_ATOL = max(float(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_NUMERIC_ATOL", 1e-9)), 0.0)
INCREMENTAL_NUMERIC_RTOL = max(float(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_NUMERIC_RTOL", 1e-8)), 0.0)
# STC uses two recursively rounded stochastic stages.  The pandas-ta batch
# implementation and the scalar continuation can differ by a few 1e-7 across
# pandas/numpy builds solely from floating operation order.  This override is
# intentionally isolated to STC; every other feature keeps the strict global
# incremental tolerance above.
INCREMENTAL_STC_NUMERIC_ATOL = max(
    float(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_STC_NUMERIC_ATOL", 5e-7)),
    INCREMENTAL_NUMERIC_ATOL,
)
INCREMENTAL_STC_NUMERIC_RTOL = max(
    float(getattr(settings, "LIVE_DASHBOARD_INCREMENTAL_STC_NUMERIC_RTOL", 0.0)),
    0.0,
)


def incremental_feature_numeric_tolerance(column: str) -> tuple[float, float, str]:
    if str(column) == "STC":
        return (
            INCREMENTAL_STC_NUMERIC_ATOL,
            INCREMENTAL_STC_NUMERIC_RTOL,
            "stc_recursive_roundoff_absolute_only",
        )
    return INCREMENTAL_NUMERIC_ATOL, INCREMENTAL_NUMERIC_RTOL, "default_strict"
_RUNTIME_CACHE: dict[str, Any] = {}
_RUNTIME_CACHE_LOCK = threading.RLock()
_RUNTIME_CONTINUATION_STATE_CACHE: dict[str, Any] = {}
_RUNTIME_CONTINUATION_STATE_CACHE_LOCK = threading.RLock()
_SEED_BAR_CACHE: dict[str, pd.DataFrame] = {}
_SHARED_FEATURE_PREWARM_CACHE: dict[str, dict[str, Any]] = {}
# Exact-snapshot cache shared by fast/slow scoring lanes. The cache stores only
# runtime-owned feature tables and is reused solely when the complete asset-bar
# signature is unchanged; per-edge schema checks still decide whether reuse is
# safe, otherwise the existing edge-local runtime path remains authoritative.
_SHARED_ASSET_RUNTIME_FEATURE_CACHE: dict[str, dict[str, Any]] = {}
_SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK = threading.RLock()
_LIVE_BAR_FILE_LOCKS: dict[str, threading.RLock] = {}
_LIVE_BAR_FILE_LOCKS_GUARD = threading.RLock()
_OUTPUT_WRITE_LOCK = threading.RLock()
_APPROVED_SIGNAL_JSONL_IDS: set[str] | None = None
_DASHBOARD_SHUTDOWN_EVENT = threading.Event()
# Global bound across fast/slow lanes. At most ASSET_SCORING_WORKERS asset jobs
# can execute concurrently, even when both scheduler lanes are active.
_ASSET_SCORING_SEMAPHORE = threading.BoundedSemaphore(ASSET_SCORING_WORKERS)

ASYNC_SCORING_QUEUE_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_ENABLED", True))
ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS = max(int(getattr(settings, "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS", 3)), 1)
ASYNC_SCORING_QUEUE_JOIN_TIMEOUT_SECONDS = float(getattr(settings, "LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_JOIN_TIMEOUT_SECONDS", 180.0))
MAX_CONCURRENT_RUNTIME_CALLS = max(
    int(getattr(settings, "LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS", 4)),
    1,
)
LIVE_BAR_REPLACE_MAX_WAIT_SECONDS = max(
    float(getattr(settings, "LIVE_DASHBOARD_BAR_REPLACE_MAX_WAIT_SECONDS", 8.0)),
    0.25,
)
LIVE_BAR_COMPACTION_MAX_WAIT_SECONDS = max(
    float(getattr(settings, "LIVE_DASHBOARD_BAR_COMPACTION_MAX_WAIT_SECONDS", 0.75)),
    0.05,
)
LIVE_BAR_COMPACT_PENDING_THRESHOLD = max(
    int(getattr(settings, "LIVE_DASHBOARD_BAR_COMPACT_PENDING_THRESHOLD", 8)),
    1,
)
LIVE_BAR_COMPACT_MAX_AGE_SECONDS = max(
    float(getattr(settings, "LIVE_DASHBOARD_BAR_COMPACT_MAX_AGE_SECONDS", 60.0)),
    1.0,
)
_RUNTIME_CALL_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_RUNTIME_CALLS)

# Stage 11: keep expensive custom-feature edges off the fast scoring lane.
# This is scheduling only: edge runtimes, model rows, thresholds, AFML, and
# execution handoff remain unchanged. Absorption is slow-lane by default because
# it is the current edge family that may require edge-local fallback features.
SLOW_LANE_SCORING_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_SLOW_LANE_SCORING_ENABLED", True))
_SLOW_LANE_FAMILIES_RAW = getattr(settings, "LIVE_DASHBOARD_SLOW_LANE_EDGE_FAMILIES", ["absorption"])
if isinstance(_SLOW_LANE_FAMILIES_RAW, str):
    SLOW_LANE_EDGE_FAMILIES = {item.strip().lower() for item in _SLOW_LANE_FAMILIES_RAW.split(",") if item.strip()}
else:
    SLOW_LANE_EDGE_FAMILIES = {str(item).strip().lower() for item in (_SLOW_LANE_FAMILIES_RAW or []) if str(item).strip()}
if not SLOW_LANE_EDGE_FAMILIES:
    SLOW_LANE_EDGE_FAMILIES = {"absorption"}
ASYNC_SLOW_LANE_QUEUE_MAX_PENDING_JOBS = max(int(getattr(settings, "LIVE_DASHBOARD_ASYNC_SLOW_LANE_MAX_PENDING_JOBS", 1)), 1)

# Phase 10: keep non-critical observability/detailed score-file I/O off the
# scoring hot path. Approved-signal outputs remain synchronous because the
# execution bot consumes them directly.
ASYNC_NONCRITICAL_IO_ENABLED = bool(getattr(settings, "LIVE_DASHBOARD_ASYNC_NONCRITICAL_IO_ENABLED", True))
ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS = max(int(getattr(settings, "LIVE_DASHBOARD_ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS", 1000)), 1)
ASYNC_NONCRITICAL_IO_JOIN_TIMEOUT_SECONDS = float(getattr(settings, "LIVE_DASHBOARD_ASYNC_NONCRITICAL_IO_JOIN_TIMEOUT_SECONDS", 30.0))
_NONCRITICAL_IO_MANAGER: NonCriticalIOManager | None = None


def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """Create the existing console/file logger with bounded file rotation.

    This intentionally preserves the logger name, format and call sites. Only the
    file handler changes from an unbounded FileHandler to RotatingFileHandler.
    """
    ensure_dirs()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = max(int(getattr(settings, "LIVE_LOG_ROTATION_MAX_BYTES", 25_000_000)), 1_000_000)
        backup_count = max(int(getattr(settings, "LIVE_LOG_ROTATION_BACKUP_COUNT", 8)), 1)
        fh = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


@dataclass
class CurrentBar:
    asset: str
    mt5_symbol: str
    threshold_ticks: int
    bar_mode: str = "standard_quote_tick"
    threshold_unit: str = "ticks"
    threshold_mode: str = "causal_daily_ewma_tick_count"
    threshold_source: str = "live_bar_policy_unknown"
    bar_start: str | None = None
    datetime: str | None = None
    broker_bar_start: str | None = None
    broker_datetime: str | None = None
    broker_time_offset_seconds: int = 0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    ticks: int = 0

    def is_empty(self) -> bool:
        return self.ticks <= 0 or self.open is None

    def update(self, tick_time_utc: pd.Timestamp, broker_tick_time: pd.Timestamp, mid: float, offset_seconds: int) -> None:
        ts = ensure_utc_timestamp(tick_time_utc).isoformat()
        broker_ts = ensure_utc_timestamp(broker_tick_time).isoformat()
        self.broker_time_offset_seconds = int(offset_seconds)
        if self.is_empty():
            self.bar_start = ts
            self.broker_bar_start = broker_ts
            self.open = float(mid)
            self.high = float(mid)
            self.low = float(mid)
            self.close = float(mid)
        else:
            self.high = max(float(self.high), float(mid))
            self.low = min(float(self.low), float(mid))
            self.close = float(mid)
        self.datetime = ts
        self.broker_datetime = broker_ts
        self.ticks += 1

    def should_close(self) -> bool:
        return self.ticks >= int(self.threshold_ticks) and self.ticks > 0

    def close_record(self, *, gap_healed: bool, executable: bool, threshold_source: str) -> dict[str, Any]:
        return {
            "Datetime": self.datetime,
            "Bar_Start": self.bar_start,
            "Broker_Datetime": self.broker_datetime,
            "Broker_Bar_Start": self.broker_bar_start,
            "Broker_Time_Offset_Seconds": int(self.broker_time_offset_seconds or 0),
            "Open": self.open,
            "High": self.high,
            "Low": self.low,
            "Close": self.close,
            "Proxy_Value": int(self.ticks),
            "Ticks": int(self.ticks),
            "Threshold": int(self.threshold_ticks),
            "Threshold_Source": threshold_source,
            "Research_Bar_Mode": self.bar_mode,
            "Threshold_Unit": self.threshold_unit,
            "Threshold_Mode": self.threshold_mode,
            "Closed_At": utc_now(),
            "Gap_Healed": bool(gap_healed),
            "Executable": bool(executable),
        }

    def reset(self) -> None:
        self.bar_start = self.datetime = self.broker_bar_start = self.broker_datetime = None
        self.open = self.high = self.low = self.close = None
        self.ticks = 0


@dataclass
class DashboardSignalRow:
    schema_version: str
    dashboard_version: str
    created_at: str
    signal_id: str
    signal_status: str
    reason: str
    edge_id: str
    asset: str
    mt5_symbol: str
    family: str
    side_name: str
    side_value: int
    event_time: str | None
    bar_start: str | None
    broker_event_time: str | None
    broker_bar_start: str | None
    event_age_seconds: float | None
    raw_probability: float | None
    calibrated_probability: float | None
    afml_bet_size: float | None
    final_live_threshold: float
    runtime_selected_threshold: float | None
    threshold_probability_column: str
    probability_passes_final_threshold: bool
    approved_signal: bool
    execution_allowed: bool
    execution_blocker: str
    portfolio_weight: float
    risk_contract_version: str
    account_stage: str
    approved_account_stage: str
    live_approval_scope: str
    daily_symbol_risk_rule_enabled: bool
    selected_variant_contract_hash: str | None
    risk_caps: dict[str, Any]
    live_manifest_hash: str
    edge_manifest_hash: str
    bar_policy_hash: str
    runtime_manifest: dict[str, Any]
    runtime_score_rows_csv: str | None
    live_trade_enabled: bool
    live_policy_ready: bool
    policy_gate_status: str
    bar_open: float | None
    bar_high: float | None
    bar_low: float | None
    bar_close: float | None
    bar_ticks: int | None
    bar_threshold: int | None
    bar_threshold_unit: str | None
    bar_research_bar_mode: str | None
    bar_threshold_mode: str | None
    bar_gap_healed: bool | None
    bar_executable: bool | None
    signal_price: float | None
    signal_price_source: str | None
    risk_source: str
    order_owner: str
    continuity_status: str | None
    entry_timing: str | None = None
    barrier_reference: str | None = None
    barrier_value: float | None = None
    barrier_value_source: str | None = None
    barrier_reference_units: str | None = None
    barrier_price_distance_formula: str | None = None
    pt_multiplier: float | None = None
    sl_multiplier: float | None = None
    vertical_horizon_bars: int | None = None
    geometry_hash: str | None = None
    reward_risk_ratio: float | None = None
    planned_sl_distance_price: float | None = None
    planned_tp_distance_price: float | None = None
    planned_stop_loss: float | None = None
    planned_take_profit: float | None = None
    execution_plan_hash: str | None = None
    execution_plan_source: str | None = None
    execution_plan_ready: bool = False
    execution_plan_blocker: str | None = None
    execution_plan_json: str | None = None
    memory_only_blocked_rows_for_edge_batch: int = 0
    error: str | None = None



def live_signal_contract_fields(manifest: dict[str, Any], edge: dict[str, Any]) -> dict[str, Any]:
    """Return the challenge-contract fields required by every dashboard row.

    The live manifest is the source of truth. Edge-level values are fallback
    compatibility only, while the selected allocator variant hash is sourced
    from final_allocator_selection in the current manifest format.
    """
    final_selection = manifest.get("final_allocator_selection") or {}
    if not isinstance(final_selection, dict):
        final_selection = {}

    account_stage = str(manifest.get("account_stage") or edge.get("account_stage") or "").strip()
    approved_account_stage = str(
        manifest.get("approved_account_stage")
        or edge.get("approved_account_stage")
        or account_stage
    ).strip()
    live_approval_scope = str(
        manifest.get("live_approval_scope")
        or final_selection.get("live_approval_scope")
        or edge.get("live_approval_scope")
        or ""
    ).strip()

    daily_symbol_risk_raw = manifest.get("daily_symbol_risk_rule_enabled")
    if daily_symbol_risk_raw is None:
        daily_symbol_risk_raw = edge.get("daily_symbol_risk_rule_enabled")

    selected_variant_contract_hash = (
        final_selection.get("selected_variant_contract_hash")
        or manifest.get("selected_variant_contract_hash")
        or edge.get("selected_variant_contract_hash")
    )
    selected_variant_contract_hash = (
        str(selected_variant_contract_hash).strip()
        if selected_variant_contract_hash is not None
        else None
    )

    missing: list[str] = []
    if not account_stage:
        missing.append("account_stage")
    if not approved_account_stage:
        missing.append("approved_account_stage")
    if not live_approval_scope:
        missing.append("live_approval_scope")
    if daily_symbol_risk_raw is None:
        missing.append("daily_symbol_risk_rule_enabled")
    if not selected_variant_contract_hash:
        missing.append("selected_variant_contract_hash")
    if missing:
        raise RuntimeError(
            "live manifest cannot populate DashboardSignalRow challenge contract; "
            f"missing={missing} edge={edge.get('edge_id')}"
        )

    return {
        "account_stage": account_stage,
        "approved_account_stage": approved_account_stage,
        "live_approval_scope": live_approval_scope,
        "daily_symbol_risk_rule_enabled": safe_bool(daily_symbol_risk_raw, False),
        "selected_variant_contract_hash": selected_variant_contract_hash,
    }


def make_dashboard_signal_row(
    *,
    manifest: dict[str, Any],
    edge: dict[str, Any],
    **row_fields: Any,
) -> DashboardSignalRow:
    """Build one row with authoritative contract fields on every code path."""
    payload = dict(row_fields)
    # Contract values are intentionally applied last so a caller cannot
    # accidentally override the current live-manifest approval contract.
    payload.update(live_signal_contract_fields(manifest, edge))
    return DashboardSignalRow(**payload)


def validate_dashboard_signal_row_factory(manifest: dict[str, Any]) -> None:
    """Fail at startup if the signal-row factory and dataclass drift apart."""
    edges = manifest.get("selected_edges") or []
    if not edges:
        raise RuntimeError("DashboardSignalRow smoke test requires at least one selected edge")
    edge = edges[0]
    contract = live_signal_contract_fields(manifest, edge)
    row = make_dashboard_signal_row(
        manifest=manifest,
        edge=edge,
        schema_version=SCHEMA_VERSION,
        dashboard_version=DASHBOARD_VERSION,
        created_at="1970-01-01T00:00:00+00:00",
        signal_id="SCHEMA_SMOKE_TEST",
        signal_status="blocked_schema_smoke_test",
        reason="startup_schema_factory_validation",
        edge_id=str(edge.get("edge_id") or "SCHEMA_SMOKE_TEST"),
        asset=str(edge.get("asset") or "SCHEMA_SMOKE_TEST"),
        mt5_symbol="SCHEMA_SMOKE_TEST",
        family=str(edge.get("family") or "schema_smoke_test"),
        side_name=str(edge.get("side_name") or "schema_smoke_test"),
        side_value=int(edge.get("side_value", 0) or 0),
        event_time=None,
        bar_start=None,
        broker_event_time=None,
        broker_bar_start=None,
        event_age_seconds=None,
        raw_probability=None,
        calibrated_probability=None,
        afml_bet_size=None,
        final_live_threshold=float(edge.get("final_live_threshold", 0.0) or 0.0),
        runtime_selected_threshold=None,
        threshold_probability_column="calibrated_probability",
        probability_passes_final_threshold=False,
        approved_signal=False,
        execution_allowed=False,
        execution_blocker="schema_smoke_test",
        portfolio_weight=float(edge.get("portfolio_weight", 0.0) or 0.0),
        risk_contract_version=str(manifest.get("risk_contract_version", RISK_CONTRACT_VERSION)),
        risk_caps=dict(edge.get("risk_caps") or manifest.get("risk_caps") or {}),
        live_manifest_hash="SCHEMA_SMOKE_TEST",
        edge_manifest_hash=str(edge.get("edge_manifest_hash") or "SCHEMA_SMOKE_TEST"),
        bar_policy_hash="SCHEMA_SMOKE_TEST",
        runtime_manifest={},
        runtime_score_rows_csv=None,
        live_trade_enabled=bool(manifest.get("live_trade_enabled", False)),
        live_policy_ready=False,
        policy_gate_status="SCHEMA_SMOKE_TEST",
        bar_open=None,
        bar_high=None,
        bar_low=None,
        bar_close=None,
        bar_ticks=None,
        bar_threshold=None,
        bar_threshold_unit=None,
        bar_research_bar_mode=None,
        bar_threshold_mode=None,
        bar_gap_healed=None,
        bar_executable=None,
        signal_price=None,
        signal_price_source=None,
        risk_source="schema_smoke_test",
        order_owner="portfolio_execution_bot.py",
        continuity_status=None,
    )
    for name, expected in contract.items():
        if getattr(row, name) != expected:
            raise RuntimeError(
                f"DashboardSignalRow smoke test contract mismatch field={name} "
                f"actual={getattr(row, name)!r} expected={expected!r}"
            )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def ensure_utc_timestamp(ts: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(ts)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")



def parse_utc_datetime_series(values: Any) -> pd.Series:
    """Parse mixed ISO timestamp formats into UTC timestamps.

    Pandas can infer one strict timestamp shape for an entire Series. Live bar
    files can contain both microsecond and non-microsecond ISO strings, so this
    helper first uses pandas' mixed-format parser when available and then falls
    back to row-wise Timestamp parsing for any values that remain unparsed.
    """
    if isinstance(values, pd.Series):
        raw = values.copy()
    else:
        raw = pd.Series(values)

    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")

    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        reparsed = []
        for value in raw.loc[missed]:
            try:
                reparsed.append(ensure_utc_timestamp(value))
            except Exception:
                reparsed.append(pd.NaT)
        parsed.loc[missed] = reparsed
    return parsed


def normalize_utc_datetime_column(df: pd.DataFrame, column: str) -> pd.DataFrame:
    """Normalize an existing datetime-like column to stable UTC ISO strings."""
    if column not in df.columns:
        return df
    out = df.copy()
    parsed = parse_utc_datetime_series(out[column])
    out[column] = parsed.apply(lambda x: x.isoformat() if pd.notna(x) else None)
    return out

def safe_iso(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        return ensure_utc_timestamp(ts).isoformat()
    except Exception:
        return str(ts)


def elapsed_ms(start_perf: float) -> float:
    """Milliseconds since a perf-counter timestamp; diagnostics only."""
    return float((time.perf_counter() - float(start_perf)) * 1000.0)


def rows_len(obj: Any) -> int:
    """Best-effort row count for diagnostic logging only."""
    try:
        if obj is None:
            return 0
        return int(len(obj))
    except Exception:
        return 0


def latest_live_age_seconds(live_event_times: set[pd.Timestamp] | None) -> float | None:
    """Return the age of the newest executable live timestamp; diagnostics only."""
    try:
        if not live_event_times:
            return None
        latest = max(ensure_utc_timestamp(ts) for ts in live_event_times)
        if pd.isna(latest):
            return None
        return float((utc_now_ts() - latest).total_seconds())
    except Exception:
        return None


def live_timestamps_after_cursor(
    live_event_times: set[pd.Timestamp] | None,
    last_processed: pd.Timestamp | None,
) -> list[pd.Timestamp]:
    """Sorted executable live timestamps that this edge has not processed yet."""
    out: list[pd.Timestamp] = []
    for raw_ts in live_event_times or set():
        try:
            ts = ensure_utc_timestamp(raw_ts)
        except Exception:
            continue
        if pd.isna(ts):
            continue
        if last_processed is not None and ts <= last_processed:
            continue
        out.append(ts)
    return sorted(out)


def pre_runtime_live_event_guard(
    *,
    edge_id: str,
    asset: str,
    live_event_times: set[pd.Timestamp] | None,
    last_processed: pd.Timestamp | None,
    state: dict[str, Any],
    logger,
) -> tuple[bool, dict[str, Any]]:
    """Skip expensive runtime work when no still-tradable live timestamp exists.

    This is a fail-closed latency guard. It does not change feature/event/model
    logic and it does not approve anything. It only avoids running heavy
    runtime.build_features()/generate_candidate_events()/score() when the edge
    has no pending executable live timestamps, or when every pending executable
    live timestamp is already older than the dashboard stale limit and would be
    rejected later by the existing stale gate.
    """
    pending = live_timestamps_after_cursor(live_event_times, last_processed)
    now = utc_now_ts()
    latest_pending = pending[-1] if pending else None
    oldest_pending = pending[0] if pending else None
    latest_age = None if latest_pending is None else float((now - latest_pending).total_seconds())
    oldest_age = None if oldest_pending is None else float((now - oldest_pending).total_seconds())
    diag: dict[str, Any] = {
        "pending_live_timestamps_count": int(len(pending)),
        "oldest_pending_live_timestamp": safe_iso(oldest_pending) if oldest_pending is not None else None,
        "latest_pending_live_timestamp": safe_iso(latest_pending) if latest_pending is not None else None,
        "oldest_pending_live_age_seconds": oldest_age,
        "latest_pending_live_age_seconds": latest_age,
        "max_signal_age_seconds": int(MAX_SIGNAL_AGE_SECONDS),
        "last_processed_timestamp": safe_iso(last_processed) if last_processed is not None else None,
        "skip": False,
        "cursor_advanced": False,
        "cursor_advanced_to": None,
        "reason": "pending_live_fresh_enough",
    }

    if not pending:
        diag["skip"] = True
        diag["reason"] = "no_pending_executable_live_timestamp_after_cursor"
        logger.info(
            "LIVE_PRE_RUNTIME_SKIP edge=%s asset=%s reason=%s last_processed=%s executable_live_timestamp_count=%s",
            edge_id,
            asset,
            diag["reason"],
            diag["last_processed_timestamp"],
            len(live_event_times or set()),
        )
        return True, diag

    # If the newest pending live bar is already too old, all earlier pending bars
    # are older too. Building features for this edge cannot lead to an approved
    # signal because the existing stale gate would reject every possible event.
    if latest_age is not None and latest_age > float(MAX_SIGNAL_AGE_SECONDS):
        cursor_to = latest_pending
        cursor_iso = cursor_to.isoformat()
        state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = cursor_iso
        diag["skip"] = True
        diag["cursor_advanced"] = True
        diag["cursor_advanced_to"] = cursor_iso
        diag["reason"] = "all_pending_live_timestamps_already_stale_before_runtime"

        counts = state.setdefault("stale_pre_runtime_skip_count_by_edge", {})
        counts[edge_id] = int(counts.get(edge_id, 0)) + 1
        rec = {
            "created_at": utc_now(),
            "edge_id": edge_id,
            "asset": asset,
            **diag,
        }
        state.setdefault("last_stale_pre_runtime_skip_by_edge", {})[edge_id] = rec
        state.setdefault("stale_pre_runtime_skip_history", []).append(rec)
        state["stale_pre_runtime_skip_history"] = state["stale_pre_runtime_skip_history"][-500:]

        logger.warning(
            "LIVE_STALE_PRE_RUNTIME_SKIP edge=%s asset=%s reason=%s pending_live_timestamps=%s "
            "oldest_pending=%s oldest_age_seconds=%s latest_pending=%s latest_age_seconds=%s "
            "max_signal_age_seconds=%s last_processed=%s cursor_advanced_to=%s",
            edge_id,
            asset,
            diag["reason"],
            diag["pending_live_timestamps_count"],
            diag["oldest_pending_live_timestamp"],
            diag["oldest_pending_live_age_seconds"],
            diag["latest_pending_live_timestamp"],
            diag["latest_pending_live_age_seconds"],
            diag["max_signal_age_seconds"],
            diag["last_processed_timestamp"],
            diag["cursor_advanced_to"],
        )
        return True, diag

    return False, diag


def log_latency_stage(
    logger,
    *,
    edge_id: str,
    asset: str,
    stage: str,
    start_perf: float,
    edge_start_perf: float,
    live_event_times: set[pd.Timestamp] | None,
    rows: Any = None,
    extra: str = "",
) -> None:
    """Emit hot-path timing diagnostics without changing trading behavior."""
    if not LATENCY_STAGE_LOGGING_ENABLED:
        return
    logger.info(
        "LIVE_LATENCY_STAGE edge=%s asset=%s stage=%s elapsed_ms=%.3f edge_elapsed_ms=%.3f "
        "latest_live_age_seconds=%s rows=%s%s",
        edge_id,
        asset,
        stage,
        elapsed_ms(start_perf),
        elapsed_ms(edge_start_perf),
        latest_live_age_seconds(live_event_times),
        rows_len(rows),
        f" {extra}" if extra else "",
    )



def shared_feature_prewarm_cache_signature(asset: str, bars_snapshot: pd.DataFrame | None = None) -> dict[str, Any]:
    """Create a conservative signature for the asset feature-continuation inputs.

    This is used only to avoid repeating expensive FFD/HMM verifier prewarm when
    the asset bar snapshot has not changed. It does not change feature math,
    runtime scoring, event generation, or signal gates.
    """
    signature: dict[str, Any] = {
        "asset": asset,
        "combined_bar_rows": 0,
        "latest_combined_bar_timestamp": None,
        "live_bar_file_exists": False,
        "live_bar_file_size": None,
        "live_bar_file_mtime_ns": None,
    }

    try:
        if bars_snapshot is not None and not bars_snapshot.empty and "Datetime" in bars_snapshot.columns:
            dt = parse_utc_datetime_series(bars_snapshot["Datetime"]).dropna()
            signature["combined_bar_rows"] = int(len(bars_snapshot))
            signature["latest_combined_bar_timestamp"] = safe_iso(dt.max()) if not dt.empty else None
    except Exception:
        signature["combined_bar_rows"] = rows_len(bars_snapshot)
        signature["latest_combined_bar_timestamp"] = None

    try:
        path = live_bars_path(asset)
        if path.exists():
            stat = path.stat()
            signature["live_bar_file_exists"] = True
            signature["live_bar_file_size"] = int(stat.st_size)
            signature["live_bar_file_mtime_ns"] = int(stat.st_mtime_ns)
    except Exception:
        # Cache miss is safer than trusting a broken signature.
        signature["live_bar_file_exists"] = False
        signature["live_bar_file_size"] = None
        signature["live_bar_file_mtime_ns"] = None

    return signature


def shared_feature_prewarm_cache_matches(cached: dict[str, Any] | None, signature: dict[str, Any]) -> bool:
    """Return True only when the previous successful prewarm used this snapshot."""
    if not cached or cached.get("success") is not True:
        return False
    old_signature = cached.get("signature")
    if not isinstance(old_signature, dict):
        return False
    keys = [
        "asset",
        "combined_bar_rows",
        "latest_combined_bar_timestamp",
        "live_bar_file_exists",
        "live_bar_file_size",
        "live_bar_file_mtime_ns",
    ]
    return all(old_signature.get(key) == signature.get(key) for key in keys)



def _nested_contract_values(payload: Any, keys: set[str], *, depth: int = 0) -> list[Any]:
    """Collect exact-name contract values from verifier output."""
    if depth > 8:
        return []
    out: list[Any] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in keys:
                out.append(value)
            if isinstance(value, (dict, list, tuple)):
                out.extend(_nested_contract_values(value, keys, depth=depth + 1))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            if isinstance(value, (dict, list, tuple)):
                out.extend(_nested_contract_values(value, keys, depth=depth + 1))
    return out


def validate_shared_research_state_contract(
    asset: str,
    ffd_verification: dict[str, Any],
    hmm_verification: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed if verifier-exposed FFD/HMM constants differ from research."""
    checks = [
        ("ffd_window_bars", ffd_verification, {"ffd_window", "ffd_window_bars"}, RESEARCH_FFD_WINDOW_BARS),
        ("ffd_live_buffer_bars", ffd_verification, {"live_buffer_bars", "ffd_live_buffer_bars"}, RESEARCH_FFD_LIVE_BUFFER_BARS),
        ("hmm_train_window_rows", hmm_verification, {"train_window", "train_window_rows", "hmm_train_window_rows"}, RESEARCH_HMM_TRAIN_WINDOW_ROWS),
        ("hmm_inference_lookback_rows", hmm_verification, {"inference_lookback", "hmm_inference_lookback_rows"}, RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS),
        ("hmm_refit_frequency_bars", hmm_verification, {"refit_frequency", "hmm_refit_frequency_bars"}, RESEARCH_HMM_REFIT_FREQUENCY_BARS),
    ]
    audit: dict[str, Any] = {
        "asset": asset,
        "status": "pass",
        "expected": dict(RESEARCH_LIVE_PARITY_CONTRACT),
        "checks": {},
        "missing_exposed_fields": [],
    }
    mismatches: list[str] = []
    for label, payload, keys, expected in checks:
        raw_values = _nested_contract_values(payload, keys)
        numeric_values: list[int] = []
        for value in raw_values:
            try:
                numeric_values.append(int(float(value)))
            except Exception:
                continue
        numeric_values = sorted(set(numeric_values))
        audit["checks"][label] = {"expected": int(expected), "observed": numeric_values}
        if not numeric_values:
            audit["missing_exposed_fields"].append(label)
            continue
        if any(value != int(expected) for value in numeric_values):
            mismatches.append(f"{label}: expected={expected} observed={numeric_values}")
    if mismatches:
        audit["status"] = "fail"
        audit["mismatches"] = mismatches
        raise RuntimeError(
            f"{asset}: shared FFD/HMM verifier contract differs from research: " + "; ".join(mismatches)
        )
    if audit["missing_exposed_fields"]:
        audit["status"] = "pass_with_unexposed_fields"
    return audit

def prewarm_shared_live_feature_continuation(
    asset: str,
    logger,
    *,
    bars_snapshot: pd.DataFrame | None = None,
    state: dict[str, Any] | None = None,
) -> bool:
    """Materialize shared FFD/HMM live continuation files before edge workers run.

    Stage 5.1 safety: edge runtimes may all need the same asset-level FFD/HMM
    continuation files. Prewarming in the main dashboard thread avoids multiple
    workers trying to build/write the same CSV at the same time. The verifier
    modules still own the actual FFD/HMM math and contract checks.

    Stage 6.3 optimization: if this exact asset bar snapshot has already passed
    prewarm in the current dashboard process, skip the verifier calls and reuse
    the already-materialized continuation files. A cache hit never changes
    trading logic; it only avoids repeated verification for unchanged inputs.
    """
    if not SHARED_FEATURE_PREWARM_ENABLED:
        logger.info("LIVE_SHARED_FEATURE_PREWARM_BYPASS asset=%s enabled=False", asset)
        return True

    prewarm_start = time.perf_counter()
    signature = shared_feature_prewarm_cache_signature(asset, bars_snapshot)

    if SHARED_FEATURE_PREWARM_CACHE_ENABLED:
        cached = _SHARED_FEATURE_PREWARM_CACHE.get(asset)
        if shared_feature_prewarm_cache_matches(cached, signature):
            cache_hit_record = {
                "asset": asset,
                "created_at": utc_now(),
                "cache_hit": True,
                "signature": signature,
                "cached_at": cached.get("created_at"),
                "cached_elapsed_ms": cached.get("elapsed_ms"),
                "reason": "unchanged_asset_bar_snapshot",
            }
            if state is not None:
                state.setdefault("last_shared_feature_prewarm_cache_by_asset", {})[asset] = cache_hit_record
            logger.info(
                "LIVE_SHARED_FEATURE_PREWARM_CACHE_HIT asset=%s elapsed_ms=%.3f latest_bar=%s rows=%s file_size=%s file_mtime_ns=%s cached_at=%s cached_elapsed_ms=%s reason=unchanged_asset_bar_snapshot",
                asset,
                elapsed_ms(prewarm_start),
                signature.get("latest_combined_bar_timestamp"),
                signature.get("combined_bar_rows"),
                signature.get("live_bar_file_size"),
                signature.get("live_bar_file_mtime_ns"),
                cached.get("created_at"),
                cached.get("elapsed_ms"),
            )
            return True

        logger.info(
            "LIVE_SHARED_FEATURE_PREWARM_CACHE_MISS asset=%s latest_bar=%s rows=%s file_size=%s file_mtime_ns=%s reason=%s",
            asset,
            signature.get("latest_combined_bar_timestamp"),
            signature.get("combined_bar_rows"),
            signature.get("live_bar_file_size"),
            signature.get("live_bar_file_mtime_ns"),
            "no_previous_successful_prewarm" if cached is None else "asset_bar_snapshot_changed",
        )
    else:
        logger.info("LIVE_SHARED_FEATURE_PREWARM_CACHE_BYPASS asset=%s enabled=False", asset)

    try:
        from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: WPS433
        from shared.regime.verify_asset_hmm_regime_manifest import verify_asset_hmm_regime_manifest  # noqa: WPS433

        ffd_perf = time.perf_counter()
        ffd_verification = verify_asset_ffd_manifest(asset)
        ffd_ms = elapsed_ms(ffd_perf)

        hmm_perf = time.perf_counter()
        hmm_verification = verify_asset_hmm_regime_manifest(asset)
        hmm_ms = elapsed_ms(hmm_perf)

        contract_audit = validate_shared_research_state_contract(
            asset,
            ffd_verification,
            hmm_verification,
        )
        elapsed = elapsed_ms(prewarm_start)
        cache_record = {
            "asset": asset,
            "created_at": utc_now(),
            "success": True,
            "elapsed_ms": elapsed,
            "ffd_ms": ffd_ms,
            "hmm_ms": hmm_ms,
            "signature": signature,
            "ffd_features": ffd_verification.get("features"),
            "ffd_live_continuation": ffd_verification.get("live_continuation_applied"),
            "hmm_features": hmm_verification.get("features"),
            "hmm_live_continuation": hmm_verification.get("live_continuation_applied"),
            "research_state_contract_audit": contract_audit,
        }
        if SHARED_FEATURE_PREWARM_CACHE_ENABLED:
            _SHARED_FEATURE_PREWARM_CACHE[asset] = cache_record
        if state is not None:
            state.setdefault("last_shared_feature_prewarm_cache_by_asset", {})[asset] = {
                **cache_record,
                "cache_hit": False,
                "reason": "prewarm_completed_and_cache_updated",
            }

        logger.info(
            "LIVE_SHARED_FEATURE_PREWARM_DONE asset=%s elapsed_ms=%.3f ffd_ms=%.3f hmm_ms=%.3f "
            "ffd_features=%s ffd_live_continuation=%s hmm_features=%s hmm_live_continuation=%s contract_audit_status=%s cache_updated=%s latest_bar=%s rows=%s",
            asset,
            elapsed,
            ffd_ms,
            hmm_ms,
            ffd_verification.get("features"),
            ffd_verification.get("live_continuation_applied"),
            hmm_verification.get("features"),
            hmm_verification.get("live_continuation_applied"),
            contract_audit.get("status"),
            SHARED_FEATURE_PREWARM_CACHE_ENABLED,
            signature.get("latest_combined_bar_timestamp"),
            signature.get("combined_bar_rows"),
        )
        return True
    except Exception as exc:
        if state is not None:
            state.setdefault("last_shared_feature_prewarm_cache_by_asset", {})[asset] = {
                "asset": asset,
                "created_at": utc_now(),
                "success": False,
                "cache_hit": False,
                "signature": signature,
                "error": f"{type(exc).__name__}: {exc}",
                "reason": "prewarm_failed_cache_not_updated",
            }
        logger.error("LIVE_SHARED_FEATURE_PREWARM_FAILED asset=%s elapsed_ms=%.3f error=%s", asset, elapsed_ms(prewarm_start), exc)
        logger.error(traceback.format_exc())
        return False



def safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None or pd.isna(x):
            return default
        out = float(x)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def safe_int(x: Any, default: int | None = None) -> int | None:
    try:
        if x is None or pd.isna(x):
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
    text = str(x).strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return default


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def ensure_runtime_dirs() -> None:
    ensure_dirs()
    for path in [
        OUTPUT_DIR,
        RUNTIME_SCORE_ROWS_DIR,
        settings.LIVE_BARS_DIR,
        settings.LIVE_LOGS_DIR,
        settings.LIVE_STATE_DIR,
        settings.LIVE_CONFIG_DIR,
        RUNTIME_CONTINUATION_STATE_DIR,
        PENDING_LIVE_BAR_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)


class LiveBarJournalError(RuntimeError):
    """Raised only when a durable per-bar journal entry cannot be created."""


def _is_windows_sharing_violation(exc: BaseException) -> bool:
    winerror = getattr(exc, "winerror", None)
    err_no = getattr(exc, "errno", None)
    return bool(
        isinstance(exc, PermissionError)
        or winerror in {5, 32, 33}
        or err_no in {errno.EACCES, errno.EPERM}
    )


def _replace_with_backoff(
    source: Path,
    destination: Path,
    *,
    max_wait_seconds: float,
) -> dict[str, Any]:
    """Replace ``destination`` while tolerating transient Windows sharing locks."""
    started = time.monotonic()
    attempts = 0
    last_error: BaseException | None = None
    while True:
        attempts += 1
        try:
            os.replace(source, destination)
            return {
                "attempts": attempts,
                "elapsed_seconds": time.monotonic() - started,
            }
        except OSError as exc:
            last_error = exc
            if not _is_windows_sharing_violation(exc):
                raise
            elapsed = time.monotonic() - started
            if elapsed >= max_wait_seconds:
                raise
            delay = min(0.05 * (2 ** min(attempts - 1, 7)), 1.0)
            delay += random.uniform(0.0, min(0.05, delay * 0.15))
            time.sleep(min(delay, max(0.0, max_wait_seconds - elapsed)))
    if last_error is not None:  # pragma: no cover - loop exits by return/raise
        raise last_error


def atomic_write_dataframe_csv(
    path: Path,
    df: pd.DataFrame,
    *,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Atomically replace one CSV with bounded Windows-lock retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            df.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        return _replace_with_backoff(
            tmp,
            path,
            max_wait_seconds=(
                LIVE_BAR_REPLACE_MAX_WAIT_SECONDS
                if max_wait_seconds is None
                else max(float(max_wait_seconds), 0.01)
            ),
        )
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_csv_append(path: Path, rows: list[dict[str, Any]], dedup_cols: list[str] | None = None) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_csv(path)
        df = pd.concat([old, new_df], ignore_index=True, sort=False)
    else:
        df = new_df
    if dedup_cols:
        cols = [c for c in dedup_cols if c in df.columns]
        if cols:
            df = df.drop_duplicates(subset=cols, keep="last")
    sort_cols = [c for c in ["event_time", "Datetime", "edge_id", "signal_id"] if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols)
    atomic_write_dataframe_csv(path, df)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str, allow_nan=True) + "\n")


def _load_approved_jsonl_signal_ids() -> set[str]:
    ids: set[str] = set()
    if not APPROVED_SIGNALS_JSONL.exists():
        return ids
    try:
        with APPROVED_SIGNALS_JSONL.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    signal_id = str(payload.get("signal_id") or "").strip()
                    if signal_id:
                        ids.add(signal_id)
    except Exception:
        return set()
    return ids


def write_approved_signal_handoff(
    rows: list[DashboardSignalRow],
    logger,
) -> int:
    """Synchronously export each approved edge result as soon as it finishes."""
    if not rows or _DASHBOARD_SHUTDOWN_EVENT.is_set():
        if rows and _DASHBOARD_SHUTDOWN_EVENT.is_set():
            logger.warning(
                "LIVE_APPROVED_SIGNAL_EXPORT_SKIPPED reason=dashboard_shutdown_requested rows=%s",
                len(rows),
            )
        return 0

    approved = [asdict(row) for row in rows if getattr(row, "approved_signal", False)]
    if not approved:
        return 0

    global _APPROVED_SIGNAL_JSONL_IDS
    with _OUTPUT_WRITE_LOCK:
        write_csv_append(APPROVED_SIGNALS_CSV, approved, dedup_cols=["signal_id"])
        if _APPROVED_SIGNAL_JSONL_IDS is None:
            _APPROVED_SIGNAL_JSONL_IDS = _load_approved_jsonl_signal_ids()

        new_jsonl_rows: list[dict[str, Any]] = []
        for row in approved:
            signal_id = str(row.get("signal_id") or "").strip()
            if signal_id and signal_id in _APPROVED_SIGNAL_JSONL_IDS:
                continue
            new_jsonl_rows.append(row)
            if signal_id:
                _APPROVED_SIGNAL_JSONL_IDS.add(signal_id)
        append_jsonl(APPROVED_SIGNALS_JSONL, new_jsonl_rows)

    logger.info(
        "LIVE_APPROVED_SIGNAL_EXPORTED_IMMEDIATE count=%s signal_ids=%s",
        len(approved),
        [row.get("signal_id") for row in approved],
    )
    return len(approved)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish a complete JSON snapshot for cross-process readers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=False, default=str, allow_nan=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                os.replace(temporary, path)
                return
            except (PermissionError, OSError) as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.025 * attempt)
        if last_error is not None:
            raise last_error
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def dashboard_bar_ack_payload(
    state: dict[str, Any],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
) -> dict[str, Any]:
    """Build the small execution handoff updated immediately after bar closure."""
    return {
        "schema_version": "v2_dashboard_bar_ack_v1",
        "updated_at": utc_now(),
        "dashboard_version": DASHBOARD_VERSION,
        "live_manifest_hash": sha256_file(settings.LIVE_MANIFEST_PATH)
        if settings.LIVE_MANIFEST_PATH.exists() else None,
        "bar_policy_hash": sha256_file(LIVE_BAR_POLICY_PATH)
        if LIVE_BAR_POLICY_PATH.exists() else None,
        "policy_gate_status": bar_policy.get("policy_gate_status"),
        "live_policy_ready": bar_policy.get("live_policy_ready"),
        "live_trade_enabled": manifest.get("live_trade_enabled"),
        "asset_continuity": copy.deepcopy(state.get("asset_continuity", {})),
        "closed_bar_counts": copy.deepcopy(state.get("closed_bar_counts", {})),
        "live_tick_cursor_contract_version": LIVE_TICK_CURSOR_CONTRACT_VERSION,
    }


def write_dashboard_bar_ack_best_effort(
    state: dict[str, Any],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    logger=None,
) -> bool:
    try:
        _atomic_write_json(
            BAR_ACK_JSON,
            dashboard_bar_ack_payload(state, manifest, bar_policy),
        )
        state.pop("last_dashboard_bar_ack_write_error", None)
        return True
    except Exception as exc:
        state["last_dashboard_bar_ack_write_error"] = {
            "created_at": utc_now(),
            "path": rel(BAR_ACK_JSON),
            "error": f"{type(exc).__name__}:{exc}",
        }
        if logger is not None:
            logger.warning(
                "LIVE_DASHBOARD_BAR_ACK_WRITE_FAILED path=%s error=%s:%s",
                BAR_ACK_JSON,
                type(exc).__name__,
                exc,
            )
        return False


def write_dashboard_status_json_best_effort(status: dict[str, Any], state: dict[str, Any]) -> bool:
    """Write the dashboard status snapshot without risking the live loop.

    latest_dashboard_status.json is an operator/observability snapshot. Approved
    signal CSV/JSONL writes above must still fail loudly if they fail, but this
    status snapshot must never kill live tick polling/scoring because Windows or
    another process briefly locked the JSON file.
    """
    errors: list[str] = []

    for attempt in (1, 2):
        try:
            _atomic_write_json(STATUS_JSON, status)
            state.pop("last_dashboard_status_json_write_warning", None)
            state.pop("last_dashboard_status_json_write_error", None)
            return True
        except Exception as exc:
            errors.append(f"primary_attempt_{attempt}:{type(exc).__name__}:{exc}")
            if attempt == 1:
                time.sleep(0.05)

    fallback_name = f"{STATUS_JSON.stem}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{STATUS_JSON.suffix}"
    fallback_path = STATUS_JSON.with_name(fallback_name)
    try:
        _atomic_write_json(fallback_path, status)
        state["last_dashboard_status_json_write_warning"] = {
            "created_at": utc_now(),
            "primary_status_json": rel(STATUS_JSON),
            "fallback_status_json": rel(fallback_path),
            "errors": errors[-5:],
            "reason": "primary_status_json_write_failed_fallback_succeeded",
        }
        state.pop("last_dashboard_status_json_write_error", None)
        return True
    except Exception as exc:
        errors.append(f"fallback:{type(exc).__name__}:{exc}")
        state["last_dashboard_status_json_write_error"] = {
            "created_at": utc_now(),
            "primary_status_json": rel(STATUS_JSON),
            "fallback_status_json": rel(fallback_path),
            "errors": errors[-5:],
            "reason": "status_json_write_failed_but_live_loop_continues",
        }
        return False


def write_dashboard_status_payload_best_effort(status: dict[str, Any], logger=None) -> bool:
    """Write status JSON without mutating live state.

    Used by the Phase 10 non-critical I/O worker. Status JSON is an operator
    snapshot, not an execution handoff. If Windows or another process locks the
    file, this must not stop scoring or tick polling.
    """
    errors: list[str] = []
    for attempt in (1, 2):
        try:
            _atomic_write_json(STATUS_JSON, status)
            return True
        except Exception as exc:
            errors.append(f"primary_attempt_{attempt}:{type(exc).__name__}:{exc}")
            if attempt == 1:
                time.sleep(0.05)

    fallback_name = f"{STATUS_JSON.stem}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{STATUS_JSON.suffix}"
    fallback_path = STATUS_JSON.with_name(fallback_name)
    try:
        _atomic_write_json(fallback_path, status)
        if logger is not None:
            logger.warning(
                "LIVE_NONCRITICAL_IO_STATUS_JSON_FALLBACK primary=%s fallback=%s errors=%s",
                rel(STATUS_JSON),
                rel(fallback_path),
                errors[-5:],
            )
        return True
    except Exception as exc:
        errors.append(f"fallback:{type(exc).__name__}:{exc}")
        if logger is not None:
            logger.warning(
                "LIVE_NONCRITICAL_IO_STATUS_JSON_DROPPED primary=%s fallback=%s errors=%s",
                rel(STATUS_JSON),
                rel(fallback_path),
                errors[-5:],
            )
        return False


class NonCriticalIOManager:
    """Single background writer for non-critical dashboard artifacts.

    Critical approved-signal CSV/JSONL writes stay synchronous. This worker is
    only for detailed audit/runtime-score/status outputs that are useful for
    observability but should not extend the scoring hot path.
    """

    def __init__(self, logger) -> None:
        self.logger = logger
        self.enabled = bool(ASYNC_NONCRITICAL_IO_ENABLED)
        self.tasks: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.total_enqueued = 0
        self.total_completed = 0
        self.total_failed = 0
        self.total_fallback_sync = 0
        self.total_dropped_status = 0
        self.last_task_summary: dict[str, Any] = {}
        self.active_task: dict[str, Any] | None = None
        self.lock = threading.RLock()

    def start(self) -> None:
        if not self.enabled:
            self.logger.info("LIVE_NONCRITICAL_IO_BYPASS enabled=False")
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="live_noncritical_io_writer", daemon=True)
        self.thread.start()
        self.logger.info(
            "LIVE_NONCRITICAL_IO_START enabled=True max_pending_tasks=%s mode=single_background_writer",
            ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS,
        )

    def status_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": bool(self.enabled),
                "pending_tasks": int(self.tasks.qsize()) if self.enabled else 0,
                "max_pending_tasks": int(ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS),
                "active_task_type": (self.active_task or {}).get("type"),
                "active_task_label": (self.active_task or {}).get("label"),
                "total_enqueued": int(self.total_enqueued),
                "total_completed": int(self.total_completed),
                "total_failed": int(self.total_failed),
                "total_fallback_sync": int(self.total_fallback_sync),
                "total_dropped_status": int(self.total_dropped_status),
                "last_task": dict(self.last_task_summary or {}),
            }

    def enqueue_csv_append(self, *, path: Path, rows: list[dict[str, Any]], dedup_cols: list[str] | None = None, label: str = "csv_append") -> bool:
        if not rows:
            return True
        task = {
            "type": "csv_append",
            "label": label,
            "path": str(path),
            "rows": rows,
            "dedup_cols": dedup_cols,
            "created_at": utc_now(),
            "row_count": int(len(rows)),
        }
        return self._enqueue_or_sync(task, critical=False)

    def enqueue_status_json(self, *, status: dict[str, Any], label: str = "status_json") -> bool:
        task = {
            "type": "status_json",
            "label": label,
            "status": status,
            "created_at": utc_now(),
            "row_count": 0,
        }
        return self._enqueue_or_sync(task, critical=False)

    def _enqueue_or_sync(self, task: dict[str, Any], *, critical: bool = False) -> bool:
        if not self.enabled:
            return self._execute_task_sync(task)
        try:
            self.tasks.put_nowait(task)
            with self.lock:
                self.total_enqueued += 1
            return True
        except queue.Full:
            # Status snapshots are lossy by design: keep the live path moving.
            if task.get("type") == "status_json" and not critical:
                with self.lock:
                    self.total_dropped_status += 1
                    self.last_task_summary = {
                        "type": task.get("type"),
                        "label": task.get("label"),
                        "created_at": task.get("created_at"),
                        "finished_at": utc_now(),
                        "status": "dropped_queue_full",
                    }
                self.logger.warning(
                    "LIVE_NONCRITICAL_IO_DROP type=%s label=%s reason=queue_full queue_size=%s",
                    task.get("type"),
                    task.get("label"),
                    self.tasks.qsize(),
                )
                return False

            # For audit/runtime rows, prefer a synchronous fallback over data loss.
            with self.lock:
                self.total_fallback_sync += 1
            self.logger.warning(
                "LIVE_NONCRITICAL_IO_SYNC_FALLBACK type=%s label=%s rows=%s reason=queue_full queue_size=%s",
                task.get("type"),
                task.get("label"),
                task.get("row_count"),
                self.tasks.qsize(),
            )
            return self._execute_task_sync(task)

    def _execute_task_sync(self, task: dict[str, Any]) -> bool:
        start = time.perf_counter()
        try:
            self._execute_task(task)
            elapsed = elapsed_ms(start)
            with self.lock:
                self.last_task_summary = {
                    "type": task.get("type"),
                    "label": task.get("label"),
                    "created_at": task.get("created_at"),
                    "finished_at": utc_now(),
                    "elapsed_ms": elapsed,
                    "rows": task.get("row_count", 0),
                    "status": "done_sync",
                }
            return True
        except Exception as exc:
            elapsed = elapsed_ms(start)
            with self.lock:
                self.total_failed += 1
                self.last_task_summary = {
                    "type": task.get("type"),
                    "label": task.get("label"),
                    "created_at": task.get("created_at"),
                    "finished_at": utc_now(),
                    "elapsed_ms": elapsed,
                    "rows": task.get("row_count", 0),
                    "status": "failed_sync",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.logger.error("LIVE_NONCRITICAL_IO_SYNC_FAILED type=%s label=%s error=%s", task.get("type"), task.get("label"), exc)
            self.logger.error(traceback.format_exc())
            return False

    def _run(self) -> None:
        while not self.stop_event.is_set() or not self.tasks.empty():
            try:
                task = self.tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                self.tasks.task_done()
                break
            start = time.perf_counter()
            self.active_task = task
            try:
                self._execute_task(task)
                elapsed = elapsed_ms(start)
                with self.lock:
                    self.total_completed += 1
                    self.last_task_summary = {
                        "type": task.get("type"),
                        "label": task.get("label"),
                        "created_at": task.get("created_at"),
                        "finished_at": utc_now(),
                        "elapsed_ms": elapsed,
                        "rows": task.get("row_count", 0),
                        "status": "done",
                    }
                if elapsed > 1000.0:
                    self.logger.info(
                        "LIVE_NONCRITICAL_IO_TASK_DONE type=%s label=%s rows=%s elapsed_ms=%.3f queue_size=%s",
                        task.get("type"),
                        task.get("label"),
                        task.get("row_count"),
                        elapsed,
                        self.tasks.qsize(),
                    )
            except Exception as exc:
                elapsed = elapsed_ms(start)
                with self.lock:
                    self.total_failed += 1
                    self.last_task_summary = {
                        "type": task.get("type"),
                        "label": task.get("label"),
                        "created_at": task.get("created_at"),
                        "finished_at": utc_now(),
                        "elapsed_ms": elapsed,
                        "rows": task.get("row_count", 0),
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                self.logger.error("LIVE_NONCRITICAL_IO_TASK_FAILED type=%s label=%s error=%s", task.get("type"), task.get("label"), exc)
                self.logger.error(traceback.format_exc())
            finally:
                self.active_task = None
                self.tasks.task_done()

    def _execute_task(self, task: dict[str, Any]) -> None:
        kind = str(task.get("type"))
        if kind == "csv_append":
            write_csv_append(
                Path(str(task["path"])),
                list(task.get("rows") or []),
                dedup_cols=task.get("dedup_cols"),
            )
            return
        if kind == "status_json":
            write_dashboard_status_payload_best_effort(dict(task.get("status") or {}), logger=self.logger)
            return
        raise ValueError(f"Unknown non-critical I/O task type: {kind}")

    def stop(self, *, wait: bool = True) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        try:
            self.tasks.put_nowait(None)
        except queue.Full:
            pass
        if wait and self.thread is not None:
            self.thread.join(timeout=ASYNC_NONCRITICAL_IO_JOIN_TIMEOUT_SECONDS)
            if self.thread.is_alive():
                self.logger.warning(
                    "LIVE_NONCRITICAL_IO_STOP_WAITING active_task=%s queue_size=%s timeout_seconds=%s",
                    self.active_task,
                    self.tasks.qsize(),
                    ASYNC_NONCRITICAL_IO_JOIN_TIMEOUT_SECONDS,
                )
                self.thread.join()
            self.logger.info("LIVE_NONCRITICAL_IO_STOPPED queue_size=%s", self.tasks.qsize())


def start_noncritical_io_writer(logger) -> None:
    global _NONCRITICAL_IO_MANAGER
    if _NONCRITICAL_IO_MANAGER is None:
        _NONCRITICAL_IO_MANAGER = NonCriticalIOManager(logger)
    _NONCRITICAL_IO_MANAGER.start()


def stop_noncritical_io_writer(logger, *, wait: bool = True) -> None:
    global _NONCRITICAL_IO_MANAGER
    if _NONCRITICAL_IO_MANAGER is None:
        return
    try:
        _NONCRITICAL_IO_MANAGER.stop(wait=wait)
    except Exception as exc:
        logger.warning("LIVE_NONCRITICAL_IO_STOP_WARN error=%s", exc)


def noncritical_io_status_snapshot() -> dict[str, Any]:
    if _NONCRITICAL_IO_MANAGER is None:
        return {
            "enabled": bool(ASYNC_NONCRITICAL_IO_ENABLED),
            "started": False,
            "pending_tasks": 0,
            "max_pending_tasks": int(ASYNC_NONCRITICAL_IO_MAX_PENDING_TASKS),
        }
    out = _NONCRITICAL_IO_MANAGER.status_snapshot()
    out["started"] = _NONCRITICAL_IO_MANAGER.thread is not None
    return out


def enqueue_noncritical_csv_append(
    *,
    path: Path,
    rows: list[dict[str, Any]],
    dedup_cols: list[str] | None = None,
    label: str = "csv_append",
) -> bool:
    if not rows:
        return True
    if _NONCRITICAL_IO_MANAGER is None:
        return write_csv_append(path, rows, dedup_cols=dedup_cols) is None
    return _NONCRITICAL_IO_MANAGER.enqueue_csv_append(path=path, rows=rows, dedup_cols=dedup_cols, label=label)


def enqueue_noncritical_status_json(*, status: dict[str, Any], label: str = "status_json") -> bool:
    if _NONCRITICAL_IO_MANAGER is None:
        return write_dashboard_status_payload_best_effort(status)
    return _NONCRITICAL_IO_MANAGER.enqueue_status_json(status=status, label=label)


def load_bar_policy() -> dict[str, Any]:
    if not LIVE_BAR_POLICY_PATH.exists():
        raise FileNotFoundError("Run Step 03 and Step 04 before dashboard: live_bar_policy.json missing")
    payload = read_json(LIVE_BAR_POLICY_PATH)
    if payload.get("manifest_type") != "v2_frozen_live_bar_policy":
        raise RuntimeError(f"Unexpected live bar policy manifest_type={payload.get('manifest_type')}")
    policies = payload.get("bar_policies")
    if not isinstance(policies, dict) or not policies:
        raise RuntimeError("live_bar_policy.json must contain non-empty bar_policies")
    for asset, policy in policies.items():
        if policy.get("bar_mode") != "standard_quote_tick" or policy.get("threshold_unit") != "ticks":
            raise RuntimeError(f"{asset}: V2 dashboard requires standard_quote_tick/ticks policy")
        if policy.get("uses_future_data") is not False:
            raise RuntimeError(f"{asset}: live bar policy cannot use future data")
        if active_threshold_ticks(policy) <= 0:
            raise RuntimeError(f"{asset}: active live threshold must be positive")
    return payload


def active_threshold_source(policy: dict[str, Any]) -> tuple[int, str]:
    """Return the active quote-tick threshold and the exact source field.

    The preferred source is the causal adaptive threshold computed from completed
    daily tick counts. The fallback is allowed for bootstrap/review mode only and
    is logged loudly by the dashboard so it cannot be mistaken for a matured
    adaptive policy.
    """
    value = policy.get("computed_next_session_threshold_ticks")
    source = "computed_next_session_threshold_ticks"
    if value is None or (isinstance(value, str) and not str(value).strip()):
        value = policy.get("fallback_initial_threshold_ticks")
        source = "fallback_initial_threshold_ticks"
    return max(int(round(float(value))), 1), source


def active_threshold_ticks(policy: dict[str, Any]) -> int:
    return active_threshold_source(policy)[0]


def policy_threshold_audit(asset: str, policy: dict[str, Any]) -> dict[str, Any]:
    threshold, source = active_threshold_source(policy)
    return {
        "asset": asset,
        "active_threshold_ticks": int(threshold),
        "active_threshold_source": source,
        "threshold_mode": policy.get("threshold_mode"),
        "target_bars_per_day": policy.get("target_bars_per_day"),
        "computed_next_session_threshold_ticks": policy.get("computed_next_session_threshold_ticks"),
        "fallback_initial_threshold_ticks": policy.get("fallback_initial_threshold_ticks"),
        "warmup_ready_from_live_history": policy.get("warmup_ready_from_live_history"),
        "completed_daily_profile_days": policy.get("completed_daily_profile_days"),
        "last_dashboard_threshold_refresh_at": policy.get("last_dashboard_threshold_refresh_at"),
        "last_dashboard_threshold_refresh_reason": policy.get("last_dashboard_threshold_refresh_reason"),
    }


def selected_edges_by_asset(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        asset = str(edge.get("asset", "")).strip()
        if asset:
            out.setdefault(asset, []).append(edge)
    return out


def edge_family_name(edge: dict[str, Any]) -> str:
    """Return the normalized edge family name for scheduler classification."""
    family = str(edge.get("family") or "").strip()
    if not family:
        edge_id = str(edge.get("edge_id") or "").strip()
        edge_name = edge_id.split("/", 1)[-1]
        if "_" in edge_name:
            family = edge_name.rsplit("_", 1)[0]
        else:
            family = edge_name
    return family.lower()


def is_slow_lane_edge(edge: dict[str, Any]) -> bool:
    """True for edges that should never block fast/cache-hit edge jobs."""
    return bool(SLOW_LANE_SCORING_ENABLED and edge_family_name(edge) in SLOW_LANE_EDGE_FAMILIES)


def filter_edges_for_scoring_lane(
    asset_edges: list[dict[str, Any]],
    *,
    scoring_lane: str = "all",
    allowed_edge_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter selected edges by scheduler lane without changing edge logic."""
    lane = str(scoring_lane or "all").strip().lower()
    out: list[dict[str, Any]] = []
    for edge in asset_edges:
        edge_id = str(edge.get("edge_id") or "").strip()
        if allowed_edge_ids is not None and edge_id not in allowed_edge_ids:
            continue
        slow = is_slow_lane_edge(edge)
        if lane == "fast" and slow:
            continue
        if lane == "slow" and not slow:
            continue
        out.append(edge)
    return out


def assets_with_lane_edges(manifest: dict[str, Any], assets: list[str], *, scoring_lane: str) -> list[str]:
    """Return assets that have at least one selected edge in a scoring lane."""
    edges_by_asset = selected_edges_by_asset(manifest.get("selected_edges", []))
    lane_assets: list[str] = []
    for asset in sorted({str(asset) for asset in assets if asset}):
        if filter_edges_for_scoring_lane(edges_by_asset.get(asset, []), scoring_lane=scoring_lane):
            lane_assets.append(asset)
    return lane_assets


def active_assets_from_manifest(manifest: dict[str, Any], bar_policy: dict[str, Any]) -> list[str]:
    assets = sorted({str(edge.get("asset")) for edge in manifest.get("selected_edges", []) if edge.get("asset")})
    return [asset for asset in assets if asset in bar_policy.get("bar_policies", {})]


def validate_live_inputs(manifest: dict[str, Any], bar_policy: dict[str, Any]) -> None:
    if manifest.get("manifest_type") != "v2_allocator_selected_live_manifest":
        raise RuntimeError(f"Unexpected live manifest type: {manifest.get('manifest_type')}")
    if manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION:
        raise RuntimeError(
            f"live_manifest risk_contract_version={manifest.get('risk_contract_version')!r}; "
            f"expected {RISK_CONTRACT_VERSION!r}"
        )
    local_stage = str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "")).strip().lower()
    manifest_stage = str(manifest.get("approved_account_stage") or manifest.get("account_stage") or "").strip().lower()
    if local_stage != EXPECTED_ACCOUNT_STAGE or manifest_stage != EXPECTED_ACCOUNT_STAGE:
        raise RuntimeError(f"Challenge dashboard requires local/manifest stage=challenge; local={local_stage!r} manifest={manifest_stage!r}")
    if manifest.get("live_approval_scope") != EXPECTED_APPROVAL_SCOPE:
        raise RuntimeError(f"Unexpected live_approval_scope={manifest.get('live_approval_scope')!r}")
    if manifest.get("daily_symbol_risk_rule_enabled") is not False:
        raise RuntimeError("Challenge dashboard requires funded daily symbol-risk rule disabled.")
    if bar_policy.get("source_live_manifest_sha256") != sha256_file(settings.LIVE_MANIFEST_PATH):
        raise RuntimeError("Frozen live bar policy was not produced from the current live manifest.")
    if bar_policy.get("approved_account_stage") != manifest_stage:
        raise RuntimeError("Frozen live bar policy account stage does not match live manifest.")
    edges = manifest.get("selected_edges", [])
    if not isinstance(edges, list) or not edges:
        raise RuntimeError("live_manifest.selected_edges must be non-empty")
    policies = bar_policy.get("bar_policies", {})
    selected_assets = {str(edge.get("asset")) for edge in edges if edge.get("asset")}
    if set(map(str, policies.keys())) != selected_assets:
        raise RuntimeError(
            f"Frozen policy assets do not exactly match selected assets: policy={sorted(policies)} selected={sorted(selected_assets)}"
        )
    for edge in edges:
        edge_id = str(edge.get("edge_id", ""))
        asset = str(edge.get("asset", ""))
        if asset not in policies:
            raise RuntimeError(f"{edge_id}: no live bar policy for asset={asset}")
        if edge.get("threshold_probability_column") != "calibrated_probability":
            raise RuntimeError(f"{edge_id}: threshold_probability_column must be calibrated_probability")
        if edge.get("risk_contract_version") != RISK_CONTRACT_VERSION:
            raise RuntimeError(f"{edge_id}: edge risk contract mismatch")
        if edge.get("dashboard_may_engineer_edge_features") is not False:
            raise RuntimeError(f"{edge_id}: dashboard-side feature engineering must be forbidden")
        threshold = float(edge.get("final_live_threshold"))
        if threshold < 0 or threshold > 1:
            raise RuntimeError(f"{edge_id}: invalid final_live_threshold={threshold}")


def initial_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dashboard_version": DASHBOARD_VERSION,
        "created_at": utc_now(),
        "updated_at": None,
        "last_tick_time_msc": {},
        "last_tick_cursor_by_symbol": {},
        "last_normalized_tick_time_utc": {},
        "raw_daily_tick_counts_by_asset": {},
        "raw_daily_tick_profile_audit_by_asset": {},
        "broker_time_offset_seconds_by_symbol": {},
        "broker_time_offset_locked_by_symbol": {},
        "broker_time_offset_source_by_symbol": {},
        "broker_time_offset_locked_at_by_symbol": {},
        "current_bars": {},
        "closed_bar_counts": {},
        "last_processed_timestamp_by_edge": {},
        "emitted_signal_ids": [],
        "asset_continuity": {},
        "total_closed_bars_since_state_created": 0,
        "total_signals_emitted": 0,
        "last_gap_healing_report": [],
        "startup_cursor_initialized_at": None,
        "last_cursor_advance": [],
        "blocked_memory_only_bar_count_by_asset": {},
        "blocked_gap_healed_event_count_by_asset": {},
        "latest_memory_only_bar_time_by_asset": {},
        "last_gap_healing_summary_by_asset": {},
        "last_ticks_fetched_by_asset": {},
        "last_tick_age_seconds_by_asset": {},
        "last_ticks_fetched_raw_by_asset": {},
        "live_tick_startup_guard_by_asset": {},
        "live_tick_guard_history": [],
        "live_tick_guard_drop_count_by_asset": {},
        "live_tick_cursor_rejections": [],
        "gap_healing_last_tick_by_symbol": {},
        "cursor_repair_history": [],
        "live_tick_range_recovery_by_asset": {},
        "live_tick_range_recovery_history": [],
        "last_threshold_refresh_at_by_asset": {},
        "last_threshold_refresh_date_by_asset": {},
        "threshold_refresh_history": [],
        "gap_threshold_reconstruction_history": [],
        "gap_healed_partial_bar_resets": [],
        "stale_pre_runtime_skip_count_by_edge": {},
        "last_stale_pre_runtime_skip_by_edge": {},
        "stale_pre_runtime_skip_history": [],
        "last_shared_feature_prewarm_cache_by_asset": {},
        "runtime_continuation_metadata_by_edge": {},
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
        "last_edge_artifact_preload": {},
        "last_noncritical_io_status": {},
        "last_async_scoring_queue_status": {},
        "bar_persistence_status_by_asset": {},
        "pending_bar_count_by_asset": {},
        "oldest_pending_bar_age_seconds_by_asset": {},
        "last_bar_write_error_by_asset": {},
        "bar_write_retry_count_by_asset": {},
        "asset_io_paused_by_asset": {},
    }


def load_state() -> tuple[dict[str, Any], bool]:
    if STATE_PATH.exists():
        state = read_json(STATE_PATH)
        for k, v in initial_state().items():
            state.setdefault(k, v)
        state["dashboard_version"] = DASHBOARD_VERSION
        state["research_live_parity_contract"] = dict(RESEARCH_LIVE_PARITY_CONTRACT)
        return state, False
    return initial_state(), True


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    write_json(STATE_PATH, state)


def is_broker_offset_locked(symbol: str, state: dict[str, Any]) -> bool:
    return bool(state.setdefault("broker_time_offset_locked_by_symbol", {}).get(symbol, False))


def get_stored_broker_offset_seconds(symbol: str, state: dict[str, Any]) -> int:
    return int(state.setdefault("broker_time_offset_seconds_by_symbol", {}).get(symbol, 0) or 0)


def set_broker_time_offset_seconds(symbol: str, state: dict[str, Any], offset_seconds: int, *, source: str, lock: bool = False) -> int:
    offset = int(offset_seconds)
    state.setdefault("broker_time_offset_seconds_by_symbol", {})[symbol] = offset
    state.setdefault("broker_time_offset_source_by_symbol", {})[symbol] = str(source)
    if lock:
        state.setdefault("broker_time_offset_locked_by_symbol", {})[symbol] = True
        state.setdefault("broker_time_offset_locked_at_by_symbol", {})[symbol] = utc_now()
    return offset


def lock_broker_time_offset(symbol: str, state: dict[str, Any], logger, *, source: str) -> int:
    offset = get_stored_broker_offset_seconds(symbol, state)
    set_broker_time_offset_seconds(symbol, state, offset, source=source, lock=True)
    logger.info("BROKER_TIME_OFFSET_LOCKED symbol=%s offset_seconds=%s source=%s", symbol, offset, source)
    return offset

def get_manual_broker_offset_seconds(symbol: str, asset: str | None = None) -> int | None:
    raw = getattr(settings, "LIVE_DASHBOARD_BROKER_TIME_OFFSET_HOURS", BROKER_TIME_OFFSET_HOURS)
    if raw is None:
        return None
    try:
        if isinstance(raw, dict):
            for key in [symbol, asset]:
                if key and key in raw:
                    return int(round(float(raw[key]) * 3600))
            return None
        return int(round(float(raw) * 3600))
    except Exception:
        return None


def infer_broker_time_offset_seconds(
    raw_broker_timestamps: pd.Series,
    symbol: str,
    state: dict[str, Any],
    logger,
    asset: str | None = None,
    *,
    allow_update: bool = True,
    lock_after_infer: bool = False,
    source: str = "live_recent_tick_inference",
) -> int:
    """Infer broker offset from RECENT broker ticks only.

    Critical V1/V2 live safety rule: historical gap-healing chunks must never
    re-infer or overwrite broker time offset. Gap healing uses the already
    primed/locked offset so old April ticks are not compared to current May UTC.
    """
    if is_broker_offset_locked(symbol, state):
        return get_stored_broker_offset_seconds(symbol, state)

    manual = get_manual_broker_offset_seconds(symbol, asset)
    if manual is not None:
        previous = state.setdefault("broker_time_offset_seconds_by_symbol", {}).get(symbol)
        set_broker_time_offset_seconds(symbol, state, int(manual), source="manual_settings", lock=True)
        if previous != manual:
            logger.warning("BROKER_TIME_OFFSET_SET asset=%s symbol=%s offset_seconds=%s source=manual_settings", asset, symbol, manual)
        return int(manual)

    if not BROKER_TIME_AUTO_DETECT or not allow_update:
        return get_stored_broker_offset_seconds(symbol, state)

    try:
        ts = parse_utc_datetime_series(raw_broker_timestamps).dropna()
        if ts.empty:
            raise ValueError("no valid raw broker timestamps")
        latest_raw = ensure_utc_timestamp(ts.max())
        delta_seconds = float((latest_raw - utc_now_ts()).total_seconds())
        inferred = 0 if abs(delta_seconds) <= BROKER_TIME_FUTURE_TOLERANCE_SECONDS else int(round(delta_seconds / 3600.0) * 3600)
        max_offset = int(BROKER_TIME_MAX_REASONABLE_OFFSET_HOURS * 3600)
        if abs(inferred) > max_offset:
            stored = get_stored_broker_offset_seconds(symbol, state)
            logger.warning(
                "BROKER_TIME_OFFSET_UNREASONABLE symbol=%s inferred=%s stored=%s; keeping stored offset",
                symbol,
                inferred,
                stored,
            )
            return stored
        previous = state.setdefault("broker_time_offset_seconds_by_symbol", {}).get(symbol)
        set_broker_time_offset_seconds(symbol, state, int(inferred), source=source, lock=lock_after_infer)
        if previous != inferred:
            logger.warning("BROKER_TIME_OFFSET_SET asset=%s symbol=%s offset_seconds=%s source=%s", asset, symbol, inferred, source)
        if lock_after_infer:
            logger.info("BROKER_TIME_OFFSET_LOCKED symbol=%s offset_seconds=%s source=%s", symbol, inferred, source)
        return int(inferred)
    except Exception as exc:
        logger.warning("BROKER_TIME_OFFSET_INFER_WARN symbol=%s error=%s; using stored/0", symbol, exc)
        return get_stored_broker_offset_seconds(symbol, state)


def true_utc_to_broker_time(ts: Any, symbol: str, state: dict[str, Any]) -> pd.Timestamp:
    offset = get_stored_broker_offset_seconds(symbol, state)
    return ensure_utc_timestamp(ts) + pd.Timedelta(seconds=offset)


def prime_broker_time_offsets(mt5: Any, assets: list[str], state: dict[str, Any], logger) -> None:
    for asset in assets:
        symbol = broker_symbol_for_asset(asset)
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                logger.warning("BROKER_TIME_PRIME_SKIPPED asset=%s symbol=%s reason=no_symbol_info_tick", asset, symbol)
                continue
            raw = getattr(tick, "time_msc", None)
            ts = pd.to_datetime(int(raw), unit="ms", utc=True) if raw is not None else pd.to_datetime(int(tick.time), unit="s", utc=True)
            offset = infer_broker_time_offset_seconds(
                pd.Series([ts]),
                symbol=symbol,
                state=state,
                logger=logger,
                asset=asset,
                allow_update=True,
                lock_after_infer=True,
                source="startup_symbol_info_tick",
            )
            logger.info("BROKER_TIME_PRIMED asset=%s symbol=%s offset_seconds=%s broker_tick=%s", asset, symbol, offset, ts.isoformat())
        except Exception as exc:
            logger.warning("BROKER_TIME_PRIME_WARN asset=%s symbol=%s error=%s", asset, symbol, exc)


def _tick_signature_from_values(
    bid: Any,
    ask: Any,
    last: Any = 0.0,
    volume: Any = 0.0,
    flags: Any = 0,
    volume_real: Any = 0.0,
) -> str:
    """Stable MT5 tick identity within one millisecond.

    MT5 exposes no universal sequence number. Persisting multiplicities of the
    complete quote payload at the last millisecond prevents the old strict
    timestamp filter from dropping distinct same-millisecond ticks while still
    suppressing overlap rows returned again by copy_ticks_from().
    """
    def fhex(value: Any) -> str:
        try:
            return float(value).hex()
        except Exception:
            return "0x0.0p+0"

    try:
        flag_text = str(int(float(flags)))
    except Exception:
        flag_text = "0"
    return "|".join(
        [fhex(bid), fhex(ask), fhex(last), fhex(volume), flag_text, fhex(volume_real)]
    )


def _tick_signature_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="object")
    return pd.Series(
        (
            _tick_signature_from_values(
                row.bid,
                row.ask,
                getattr(row, "last", 0.0),
                getattr(row, "volume", 0.0),
                getattr(row, "flags", 0),
                getattr(row, "volume_real", 0.0),
            )
            for row in df.itertuples(index=False)
        ),
        index=df.index,
        dtype="object",
    )


def daily_tick_counts_from_ticks_df(ticks_df: pd.DataFrame) -> dict[str, int]:
    if ticks_df is None or ticks_df.empty or "timestamp" not in ticks_df.columns:
        return {}
    ts = parse_utc_datetime_series(ticks_df["timestamp"])
    valid = ts.dropna()
    if valid.empty:
        return {}
    dates = valid.dt.date.astype(str)
    counts = dates.value_counts(sort=False)
    return {str(day): int(count) for day, count in counts.items()}


def record_raw_tick_daily_counts(
    asset: str,
    ticks_df: pd.DataFrame,
    state: dict[str, Any],
    *,
    source: str,
) -> None:
    """Persist direct UTC-day quote-tick counts after cursor de-duplication.

    Research profiles raw valid quote ticks directly. This continuation record
    therefore counts ticks, not completed bars, and is later preferred over the
    older bar-close-date approximation whenever a day is available here.
    """
    if not RAW_TICK_DAILY_PROFILE_ENABLED:
        return
    counts = daily_tick_counts_from_ticks_df(ticks_df)
    if not counts:
        return
    target = state.setdefault("raw_daily_tick_counts_by_asset", {}).setdefault(asset, {})
    for day, count in counts.items():
        target[day] = int(target.get(day, 0) or 0) + int(count)

    if ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS > 0:
        min_day = (
            utc_now_ts() - pd.Timedelta(days=ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS + 2)
        ).date().isoformat()
        for day in list(target):
            if str(day) < min_day:
                target.pop(day, None)

    state.setdefault("raw_daily_tick_profile_audit_by_asset", {})[asset] = {
        "updated_at": utc_now(),
        "source": source,
        "days": int(len(target)),
        "first_day": min(target) if target else None,
        "last_day": max(target) if target else None,
        "last_batch_rows": int(len(ticks_df)),
        "last_batch_counts": counts,
        "contract": "direct_valid_quote_ticks_grouped_by_true_utc_day",
    }


def normalize_ticks_df(
    raw: pd.DataFrame,
    asset: str,
    symbol: str,
    state: dict[str, Any],
    logger,
    *,
    allow_offset_inference: bool = True,
    offset_source: str = "live_tick_batch",
) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    if "time_msc" in df.columns:
        df["time_msc"] = pd.to_numeric(df["time_msc"], errors="coerce")
        df["broker_timestamp"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True, errors="coerce")
    elif "time" in df.columns:
        df["broker_timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True, errors="coerce")
        df["time_msc"] = (df["broker_timestamp"].astype("int64") // 1_000_000).astype("Int64")
    else:
        raise ValueError("MT5 ticks missing time_msc/time column")

    for column, default in [
        ("bid", np.nan),
        ("ask", np.nan),
        ("last", 0.0),
        ("volume", 0.0),
        ("flags", 0),
        ("volume_real", 0.0),
    ]:
        if column not in df.columns:
            df[column] = default
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["broker_timestamp", "time_msc", "bid", "ask"])
    # Match the research normalizer exactly: positive quotes and no crossed market.
    df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])].copy()
    if df.empty:
        return pd.DataFrame()
    df["time_msc"] = df["time_msc"].astype("int64")

    if allow_offset_inference:
        offset = infer_broker_time_offset_seconds(
            df["broker_timestamp"],
            symbol=symbol,
            state=state,
            logger=logger,
            asset=asset,
            allow_update=True,
            lock_after_infer=False,
            source=offset_source,
        )
    else:
        offset = get_stored_broker_offset_seconds(symbol, state)

    df["timestamp"] = df["broker_timestamp"] - pd.to_timedelta(offset, unit="s")
    df["broker_time_offset_seconds"] = int(offset)
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    columns = [
        "time_msc",
        "timestamp",
        "broker_timestamp",
        "broker_time_offset_seconds",
        "bid",
        "ask",
        "last",
        "volume",
        "flags",
        "volume_real",
        "mid",
    ]
    return df[columns].sort_values(
        ["time_msc", "bid", "ask", "flags"], kind="mergesort"
    ).reset_index(drop=True)


def _filter_ticks_after_cursor(
    df: pd.DataFrame,
    *,
    cursor: dict[str, Any],
    last_msc: int | None,
) -> pd.DataFrame:
    """Return only unseen ticks while preserving same-millisecond multiplicity."""
    if df is None or df.empty or last_msc is None:
        return pd.DataFrame() if df is None else df.reset_index(drop=True)

    later = df["time_msc"] > int(last_msc)
    same = df["time_msc"] == int(last_msc)
    keep = later.copy()
    if same.any():
        prior_counts = Counter(
            {
                str(key): int(value)
                for key, value in dict(cursor.get("signature_counts") or {}).items()
                if int(value) > 0
            }
        )
        same_df = df.loc[same].copy()
        same_df["_tick_signature"] = _tick_signature_series(same_df)
        same_df["_signature_occurrence"] = (
            same_df.groupby("_tick_signature", sort=False).cumcount() + 1
        )
        same_keep = (
            same_df["_signature_occurrence"]
            > same_df["_tick_signature"].map(prior_counts).fillna(0)
        )
        keep.loc[same_df.index] = same_keep.to_numpy(dtype=bool)
    return df.loc[keep].reset_index(drop=True)


def fetch_ticks(mt5: Any, asset: str, symbol: str, state: dict[str, Any], logger) -> pd.DataFrame:
    """Fetch unseen live ticks with a broker-time-aware range recovery path.

    The normal low-cost path remains ``copy_ticks_from``. Some MT5 terminals can
    leave a symbol's local tick-history cache stale even while ``symbol_info_tick``
    is current. When the cursor path produces no unseen ticks and the normalized
    cursor is stale, a bounded ``copy_ticks_range`` request forces synchronization.
    Both request boundaries are converted from true UTC to the already locked
    broker clock, and normal cursor de-duplication remains authoritative.
    """
    cursor = state.setdefault("last_tick_cursor_by_symbol", {}).get(symbol) or {}
    legacy_last = state.setdefault("last_tick_time_msc", {}).get(symbol)
    last_msc = safe_int(cursor.get("time_msc"), safe_int(legacy_last, None))
    last_norm_raw = state.setdefault("last_normalized_tick_time_utc", {}).get(symbol)
    last_norm = None
    if last_norm_raw:
        try:
            last_norm = ensure_utc_timestamp(last_norm_raw)
        except Exception:
            last_norm = None

    now_true = utc_now_ts()
    if last_msc is None:
        true_start = now_true - pd.Timedelta(seconds=TICK_BATCH_LOOKBACK_SECONDS)
        start = true_utc_to_broker_time(true_start, symbol, state).to_pydatetime()
    else:
        # Raw time_msc is stored on the broker clock. Reusing it directly is the
        # cheapest cursor request and preserves exact same-millisecond identity.
        start = (
            pd.to_datetime(int(last_msc), unit="ms", utc=True).to_pydatetime()
            - timedelta(seconds=2)
        )

    ticks = mt5.copy_ticks_from(symbol, start, 200000, mt5.COPY_TICKS_ALL)
    primary = normalize_ticks_df(
        pd.DataFrame(ticks) if ticks is not None else pd.DataFrame(),
        asset=asset,
        symbol=symbol,
        state=state,
        logger=logger,
        allow_offset_inference=True,
        offset_source="live_poll_tick_batch",
    )
    unseen = _filter_ticks_after_cursor(primary, cursor=cursor, last_msc=last_msc)
    if not unseen.empty:
        return unseen

    cursor_age_seconds = None
    if last_norm is not None:
        cursor_age_seconds = float((now_true - last_norm).total_seconds())

    snapshot_msc = None
    try:
        snapshot = mt5.symbol_info_tick(symbol)
        raw_snapshot_msc = getattr(snapshot, "time_msc", None) if snapshot is not None else None
        if raw_snapshot_msc is not None:
            snapshot_msc = int(raw_snapshot_msc)
        elif snapshot is not None and getattr(snapshot, "time", None) is not None:
            snapshot_msc = int(snapshot.time) * 1000
    except Exception as exc:
        logger.warning(
            "LIVE_TICK_RANGE_RECOVERY_SNAPSHOT_WARN asset=%s symbol=%s error=%s",
            asset,
            symbol,
            exc,
        )

    snapshot_ahead = bool(
        snapshot_msc is not None
        and (last_msc is None or int(snapshot_msc) > int(last_msc))
    )
    cursor_stale = bool(
        last_norm is None
        or cursor_age_seconds is None
        or cursor_age_seconds >= LIVE_TICK_RANGE_FALLBACK_STALE_SECONDS
    )
    if not (snapshot_ahead and cursor_stale):
        return unseen

    previous = state.setdefault("live_tick_range_recovery_by_asset", {}).get(asset) or {}
    previous_at = previous.get("created_at")
    if previous_at:
        try:
            previous_ts = ensure_utc_timestamp(previous_at)
            if (now_true - previous_ts).total_seconds() < LIVE_TICK_RANGE_RECOVERY_COOLDOWN_SECONDS:
                return unseen
        except Exception:
            pass

    max_lookback_start = now_true - pd.Timedelta(
        hours=LIVE_TICK_RANGE_FALLBACK_MAX_LOOKBACK_HOURS
    )
    recovery_true_start = (
        max(last_norm - pd.Timedelta(seconds=2), max_lookback_start)
        if last_norm is not None
        else now_true - pd.Timedelta(seconds=TICK_BATCH_LOOKBACK_SECONDS)
    )
    recovery_true_end = now_true + pd.Timedelta(
        seconds=LIVE_TICK_RANGE_FALLBACK_END_LEEWAY_SECONDS
    )
    api_start = true_utc_to_broker_time(recovery_true_start, symbol, state)
    api_end = true_utc_to_broker_time(recovery_true_end, symbol, state)
    fallback_ticks = mt5.copy_ticks_range(
        symbol,
        api_start.to_pydatetime(),
        api_end.to_pydatetime(),
        mt5.COPY_TICKS_ALL,
    )
    fallback = normalize_ticks_df(
        pd.DataFrame(fallback_ticks) if fallback_ticks is not None else pd.DataFrame(),
        asset=asset,
        symbol=symbol,
        state=state,
        logger=logger,
        allow_offset_inference=False,
        offset_source="live_poll_broker_time_range_recovery",
    )
    unseen = _filter_ticks_after_cursor(fallback, cursor=cursor, last_msc=last_msc)

    rec = {
        "created_at": utc_now(),
        "asset": asset,
        "symbol": symbol,
        "cursor_age_seconds_before_recovery": cursor_age_seconds,
        "snapshot_time_msc": snapshot_msc,
        "cursor_time_msc": last_msc,
        "snapshot_ahead_of_cursor": snapshot_ahead,
        "snapshot_lead_seconds": (
            None
            if snapshot_msc is None or last_msc is None
            else float((int(snapshot_msc) - int(last_msc)) / 1000.0)
        ),
        "true_start": safe_iso(recovery_true_start),
        "true_end": safe_iso(recovery_true_end),
        "api_start_broker_time": safe_iso(api_start),
        "api_end_broker_time": safe_iso(api_end),
        "raw_rows": int(0 if fallback_ticks is None else len(fallback_ticks)),
        "unseen_rows": int(len(unseen)),
        "mt5_last_error": mt5.last_error(),
    }
    state.setdefault("live_tick_range_recovery_by_asset", {})[asset] = rec
    state.setdefault("live_tick_range_recovery_history", []).append(rec)
    state["live_tick_range_recovery_history"] = state[
        "live_tick_range_recovery_history"
    ][-200:]
    logger.warning(
        "LIVE_TICK_RANGE_RECOVERY asset=%s symbol=%s cursor_age_seconds=%s "
        "snapshot_lead_seconds=%s true_start=%s true_end=%s api_start=%s "
        "api_end=%s raw_rows=%s unseen_rows=%s last_error=%s",
        asset,
        symbol,
        cursor_age_seconds,
        rec["snapshot_lead_seconds"],
        safe_iso(recovery_true_start),
        safe_iso(recovery_true_end),
        safe_iso(api_start),
        safe_iso(api_end),
        rec["raw_rows"],
        rec["unseen_rows"],
        rec["mt5_last_error"],
    )
    return unseen


def _record_live_tick_cursor_rejection(
    *,
    symbol: str,
    state: dict[str, Any],
    source: str,
    reason: str,
    previous_time_msc: int | None,
    proposed_time_msc: int | None,
    previous_normalized_utc: Any = None,
    proposed_normalized_utc: Any = None,
) -> None:
    rec = {
        "created_at": utc_now(),
        "symbol": symbol,
        "source": source,
        "reason": reason,
        "previous_time_msc": previous_time_msc,
        "proposed_time_msc": proposed_time_msc,
        "previous_normalized_utc": safe_iso(previous_normalized_utc),
        "proposed_normalized_utc": safe_iso(proposed_normalized_utc),
        "cursor_contract": LIVE_TICK_CURSOR_CONTRACT_VERSION,
    }
    state.setdefault("live_tick_cursor_rejections", []).append(rec)
    state["live_tick_cursor_rejections"] = state["live_tick_cursor_rejections"][-200:]


def update_last_tick(
    symbol: str,
    ticks_df: pd.DataFrame,
    state: dict[str, Any],
    *,
    source: str = "live_poll",
    logger=None,
) -> bool:
    """Advance the executable-live cursor monotonically.

    Historical gap-healing must never call this function. The cursor may remain
    at the same millisecond to preserve quote multiplicity, but it may never move
    backward in raw broker time or normalized UTC time.
    """
    if ticks_df is None or ticks_df.empty:
        return False
    max_msc = int(pd.to_numeric(ticks_df["time_msc"], errors="raise").max())
    latest = ticks_df[pd.to_numeric(ticks_df["time_msc"], errors="coerce") == max_msc].copy()
    current_counts = Counter(_tick_signature_series(latest).tolist())

    cursor_map = state.setdefault("last_tick_cursor_by_symbol", {})
    previous = cursor_map.get(symbol) or {}
    previous_msc = safe_int(previous.get("time_msc"), safe_int(
        state.setdefault("last_tick_time_msc", {}).get(symbol), None
    ))
    previous_norm_raw = state.setdefault("last_normalized_tick_time_utc", {}).get(symbol)
    previous_norm = None
    if previous_norm_raw:
        try:
            previous_norm = ensure_utc_timestamp(previous_norm_raw)
        except Exception:
            previous_norm = None

    max_rows = ticks_df[pd.to_numeric(ticks_df["time_msc"], errors="coerce") == max_msc]
    norm_ts = ensure_utc_timestamp(max_rows["timestamp"].iloc[-1])

    if previous_msc is not None and max_msc < int(previous_msc):
        _record_live_tick_cursor_rejection(
            symbol=symbol, state=state, source=source, reason="raw_time_msc_regression",
            previous_time_msc=int(previous_msc), proposed_time_msc=max_msc,
            previous_normalized_utc=previous_norm, proposed_normalized_utc=norm_ts,
        )
        if logger is not None:
            logger.error(
                "LIVE_TICK_CURSOR_REGRESSION_REJECTED symbol=%s source=%s previous_time_msc=%s proposed_time_msc=%s previous_normalized=%s proposed_normalized=%s",
                symbol, source, previous_msc, max_msc, safe_iso(previous_norm), safe_iso(norm_ts),
            )
        return False
    if previous_norm is not None and norm_ts < previous_norm:
        _record_live_tick_cursor_rejection(
            symbol=symbol, state=state, source=source, reason="normalized_utc_regression",
            previous_time_msc=previous_msc, proposed_time_msc=max_msc,
            previous_normalized_utc=previous_norm, proposed_normalized_utc=norm_ts,
        )
        if logger is not None:
            logger.error(
                "LIVE_TICK_CURSOR_REGRESSION_REJECTED symbol=%s source=%s previous_time_msc=%s proposed_time_msc=%s previous_normalized=%s proposed_normalized=%s",
                symbol, source, previous_msc, max_msc, safe_iso(previous_norm), safe_iso(norm_ts),
            )
        return False

    if previous_msc is not None and int(previous_msc) == max_msc:
        prior_counts = Counter(
            {
                str(key): int(value)
                for key, value in dict(previous.get("signature_counts") or {}).items()
                if int(value) > 0
            }
        )
        current_counts = prior_counts + current_counts

    if len(current_counts) > RAW_TICK_CURSOR_SIGNATURE_LIMIT:
        current_counts = Counter(dict(current_counts.most_common(RAW_TICK_CURSOR_SIGNATURE_LIMIT)))

    cursor_map[symbol] = {
        "time_msc": max_msc,
        "signature_counts": dict(current_counts),
        "updated_at": utc_now(),
        "source": source,
        "contract": "time_msc_plus_full_quote_payload_multiplicity_monotonic",
        "cursor_contract_version": LIVE_TICK_CURSOR_CONTRACT_VERSION,
    }
    state.setdefault("last_tick_time_msc", {})[symbol] = max_msc
    state.setdefault("last_normalized_tick_time_utc", {})[symbol] = norm_ts.isoformat()
    return True


def record_gap_healing_last_tick(
    symbol: str,
    ticks_df: pd.DataFrame,
    state: dict[str, Any],
    *,
    label: str,
) -> None:
    """Audit historical recovery without mutating the executable-live cursor."""
    if ticks_df is None or ticks_df.empty:
        return
    max_msc = int(pd.to_numeric(ticks_df["time_msc"], errors="raise").max())
    max_rows = ticks_df[pd.to_numeric(ticks_df["time_msc"], errors="coerce") == max_msc]
    norm_ts = ensure_utc_timestamp(max_rows["timestamp"].iloc[-1])
    state.setdefault("gap_healing_last_tick_by_symbol", {})[symbol] = {
        "created_at": utc_now(),
        "label": label,
        "time_msc": max_msc,
        "normalized_timestamp_utc": norm_ts.isoformat(),
        "rows": int(len(ticks_df)),
        "mutated_live_cursor": False,
    }


def _live_tick_guard_record(asset: str, state: dict[str, Any]) -> dict[str, Any]:
    record = state.setdefault("live_tick_startup_guard_by_asset", {}).get(asset)
    return dict(record) if isinstance(record, dict) else {}


def apply_live_tick_startup_guard(
    asset: str,
    symbol: str,
    ticks_df: pd.DataFrame,
    state: dict[str, Any],
    logger,
) -> pd.DataFrame:
    """Drop every tick at/before the post-gap-healing executable cutoff.

    This is an independent second line of defence behind the raw cursor. Even if
    MT5 returns an archived batch or persisted cursor state is damaged, historical
    ticks cannot enter executable bar construction.
    """
    if ticks_df is None or ticks_df.empty or not LIVE_TICK_STARTUP_GUARD_ENABLED:
        return pd.DataFrame() if ticks_df is None else ticks_df.reset_index(drop=True)
    guard = _live_tick_guard_record(asset, state)
    cutoff_raw = guard.get("cutoff_timestamp_utc")
    cutoff_msc = safe_int(guard.get("cutoff_time_msc"), None)
    if not cutoff_raw or cutoff_msc is None:
        raise RuntimeError(
            f"{asset}: executable live tick guard is missing after startup gap healing"
        )
    cutoff = ensure_utc_timestamp(cutoff_raw)
    frame = ticks_df.copy()
    times = parse_utc_datetime_series(frame["timestamp"])
    raw_msc = pd.to_numeric(frame["time_msc"], errors="coerce")
    keep = times.gt(cutoff) & raw_msc.gt(int(cutoff_msc))
    dropped = int((~keep).sum())
    retained = frame.loc[keep].reset_index(drop=True)
    if dropped:
        oldest = times.loc[~keep].min()
        newest = times.loc[~keep].max()
        lag_seconds = (cutoff - oldest).total_seconds() if pd.notna(oldest) else None
        state.setdefault("live_tick_guard_drop_count_by_asset", {})[asset] = int(
            state.setdefault("live_tick_guard_drop_count_by_asset", {}).get(asset, 0)
        ) + dropped
        rec = {
            "created_at": utc_now(),
            "asset": asset,
            "symbol": symbol,
            "cutoff_timestamp_utc": cutoff.isoformat(),
            "cutoff_time_msc": int(cutoff_msc),
            "input_rows": int(len(frame)),
            "dropped_rows": dropped,
            "retained_rows": int(len(retained)),
            "oldest_dropped_timestamp_utc": safe_iso(oldest),
            "newest_dropped_timestamp_utc": safe_iso(newest),
            "oldest_drop_lag_seconds": lag_seconds,
            "cursor_contract": LIVE_TICK_CURSOR_CONTRACT_VERSION,
        }
        state.setdefault("live_tick_guard_history", []).append(rec)
        state["live_tick_guard_history"] = state["live_tick_guard_history"][-200:]
        level = logger.error if lag_seconds is not None and lag_seconds >= LIVE_TICK_HISTORICAL_BATCH_WARN_SECONDS else logger.warning
        level(
            "LIVE_TICK_STARTUP_GUARD_DROP asset=%s symbol=%s input_rows=%s dropped_rows=%s retained_rows=%s cutoff=%s oldest_dropped=%s newest_dropped=%s lag_seconds=%s",
            asset, symbol, len(frame), dropped, len(retained), cutoff.isoformat(),
            safe_iso(oldest), safe_iso(newest), lag_seconds,
        )
        # Repair a stale raw cursor floor immediately so the same archived batch
        # is not requested again on every poll. Signature counts are intentionally
        # empty because all ticks at the cutoff millisecond are non-executable.
        current = state.setdefault("last_tick_cursor_by_symbol", {}).get(symbol) or {}
        current_msc = safe_int(current.get("time_msc"), None)
        if current_msc is None or current_msc < int(cutoff_msc):
            state.setdefault("last_tick_cursor_by_symbol", {})[symbol] = {
                "time_msc": int(cutoff_msc),
                "signature_counts": {},
                "updated_at": utc_now(),
                "source": "startup_guard_floor_repair",
                "contract": "time_msc_plus_full_quote_payload_multiplicity_monotonic",
                "cursor_contract_version": LIVE_TICK_CURSOR_CONTRACT_VERSION,
            }
            state.setdefault("last_tick_time_msc", {})[symbol] = int(cutoff_msc)
            state.setdefault("last_normalized_tick_time_utc", {})[symbol] = cutoff.isoformat()
    return retained


def _snapshot_live_tick_cursors(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_tick_cursor_by_symbol": copy.deepcopy(state.get("last_tick_cursor_by_symbol") or {}),
        "last_tick_time_msc": copy.deepcopy(state.get("last_tick_time_msc") or {}),
        "last_normalized_tick_time_utc": copy.deepcopy(state.get("last_normalized_tick_time_utc") or {}),
    }


def prime_live_tick_cursors_after_gap_healing(
    mt5: Any,
    assets: list[str],
    state: dict[str, Any],
    logger,
    *,
    pre_gap_snapshot: dict[str, Any] | None = None,
) -> None:
    """Create a fresh executable-live cutoff after all historical work is done."""
    pre_gap_snapshot = pre_gap_snapshot or {}
    prime_now = utc_now_ts()
    for asset in assets:
        symbol = broker_symbol_for_asset(asset)
        latest_live = latest_live_bar_timestamp(asset)
        previous_norm_raw = (pre_gap_snapshot.get("last_normalized_tick_time_utc") or {}).get(symbol)
        previous_norm = None
        if previous_norm_raw:
            try:
                previous_norm = ensure_utc_timestamp(previous_norm_raw)
            except Exception:
                previous_norm = None
        previous_cursor = (pre_gap_snapshot.get("last_tick_cursor_by_symbol") or {}).get(symbol) or {}
        previous_msc = safe_int(
            previous_cursor.get("time_msc"),
            safe_int((pre_gap_snapshot.get("last_tick_time_msc") or {}).get(symbol), None),
        )

        snapshot_msc = None
        snapshot_norm = None
        try:
            snapshot = mt5.symbol_info_tick(symbol)
            raw_msc = getattr(snapshot, "time_msc", None) if snapshot is not None else None
            if raw_msc is None and snapshot is not None and getattr(snapshot, "time", None) is not None:
                raw_msc = int(snapshot.time) * 1000
            if raw_msc is not None:
                snapshot_msc = int(raw_msc)
                broker_ts = pd.to_datetime(snapshot_msc, unit="ms", utc=True)
                snapshot_norm = broker_ts - pd.Timedelta(
                    seconds=get_stored_broker_offset_seconds(symbol, state)
                )
        except Exception as exc:
            logger.warning(
                "LIVE_TICK_CURSOR_PRIME_SNAPSHOT_WARN asset=%s symbol=%s error=%s:%s",
                asset, symbol, type(exc).__name__, exc,
            )

        cutoff_candidates = [prime_now]
        for candidate in (latest_live, previous_norm, snapshot_norm):
            if candidate is not None:
                cutoff_candidates.append(ensure_utc_timestamp(candidate))
        cutoff = max(cutoff_candidates)
        broker_cutoff = true_utc_to_broker_time(cutoff, symbol, state)
        cutoff_msc = int(broker_cutoff.value // 1_000_000)
        if previous_msc is not None:
            cutoff_msc = max(cutoff_msc, int(previous_msc))
        if snapshot_msc is not None:
            cutoff_msc = max(cutoff_msc, int(snapshot_msc))

        state.setdefault("last_tick_cursor_by_symbol", {})[symbol] = {
            "time_msc": cutoff_msc,
            "signature_counts": {},
            "updated_at": utc_now(),
            "source": "post_gap_healing_live_cursor_prime",
            "contract": "time_msc_plus_full_quote_payload_multiplicity_monotonic",
            "cursor_contract_version": LIVE_TICK_CURSOR_CONTRACT_VERSION,
        }
        state.setdefault("last_tick_time_msc", {})[symbol] = cutoff_msc
        state.setdefault("last_normalized_tick_time_utc", {})[symbol] = cutoff.isoformat()
        guard = {
            "created_at": utc_now(),
            "asset": asset,
            "symbol": symbol,
            "cutoff_timestamp_utc": cutoff.isoformat(),
            "cutoff_time_msc": cutoff_msc,
            "latest_live_bar_timestamp_utc": safe_iso(latest_live),
            "pre_gap_cursor_time_msc": previous_msc,
            "pre_gap_normalized_timestamp_utc": safe_iso(previous_norm),
            "snapshot_time_msc": snapshot_msc,
            "snapshot_normalized_timestamp_utc": safe_iso(snapshot_norm),
            "historical_gap_healing_mutates_live_cursor": False,
            "strictly_newer_ticks_required": True,
            "cursor_contract": LIVE_TICK_CURSOR_CONTRACT_VERSION,
        }
        state.setdefault("live_tick_startup_guard_by_asset", {})[asset] = guard
        logger.info(
            "LIVE_TICK_CURSOR_PRIMED asset=%s symbol=%s cutoff=%s cutoff_time_msc=%s latest_live=%s pre_gap_cursor=%s snapshot_time_msc=%s contract=%s",
            asset, symbol, cutoff.isoformat(), cutoff_msc, safe_iso(latest_live),
            previous_msc, snapshot_msc, LIVE_TICK_CURSOR_CONTRACT_VERSION,
        )


def reset_stale_current_bar_before_live_ticks(
    asset: str,
    symbol: str,
    policy: dict[str, Any],
    state: dict[str, Any],
    logger,
) -> None:
    guard = _live_tick_guard_record(asset, state)
    cutoff_raw = guard.get("cutoff_timestamp_utc")
    raw = state.setdefault("current_bars", {}).get(asset)
    if not cutoff_raw or not isinstance(raw, dict) or int(raw.get("ticks", 0) or 0) <= 0:
        return
    cutoff = ensure_utc_timestamp(cutoff_raw)
    raw_time = raw.get("datetime") or raw.get("bar_start")
    try:
        bar_time = ensure_utc_timestamp(raw_time)
    except Exception:
        bar_time = None
    if bar_time is not None and bar_time > cutoff:
        return
    logger.error(
        "LIVE_STALE_PARTIAL_BAR_RESET asset=%s symbol=%s bar_time=%s cutoff=%s ticks=%s reason=partial_bar_not_strictly_after_live_startup_cutoff",
        asset, symbol, safe_iso(bar_time), cutoff.isoformat(), raw.get("ticks"),
    )
    state.setdefault("live_tick_guard_history", []).append({
        "created_at": utc_now(),
        "asset": asset,
        "symbol": symbol,
        "reason": "stale_partial_bar_reset",
        "bar_time": safe_iso(bar_time),
        "cutoff_timestamp_utc": cutoff.isoformat(),
        "discarded_ticks": int(raw.get("ticks", 0) or 0),
    })
    state["live_tick_guard_history"] = state["live_tick_guard_history"][-200:]
    reset_current_bar(asset, state)


def assert_executable_bar_after_startup_cutoff(
    asset: str,
    rec: dict[str, Any],
    state: dict[str, Any],
) -> None:
    guard = _live_tick_guard_record(asset, state)
    cutoff_raw = guard.get("cutoff_timestamp_utc")
    if not cutoff_raw:
        raise RuntimeError(f"{asset}: executable bar cutoff is missing")
    cutoff = ensure_utc_timestamp(cutoff_raw)
    event_ts = ensure_utc_timestamp(rec.get("Datetime"))
    bar_start = ensure_utc_timestamp(rec.get("Bar_Start"))
    if event_ts <= cutoff or bar_start <= cutoff:
        raise RuntimeError(
            f"{asset}: executable bar is not strictly after startup cutoff: "
            f"bar_start={bar_start.isoformat()} event={event_ts.isoformat()} cutoff={cutoff.isoformat()}"
        )

def live_bars_path(asset: str) -> Path:
    return settings.LIVE_BARS_DIR / f"{asset}_live_standard_quote_tick_bars.csv"


def live_bar_file_lock(asset: str) -> threading.RLock:
    """Return an in-process lock for this asset's live-bar CSV.

    Stage 8 runs scoring in a background worker while the main thread keeps
    polling MT5 and appending bars. These locks prevent the worker from reading
    a live-bar CSV while the main thread is rewriting it, without blocking the
    whole scoring cycle.
    """
    key = str(asset)
    with _LIVE_BAR_FILE_LOCKS_GUARD:
        lock = _LIVE_BAR_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LIVE_BAR_FILE_LOCKS[key] = lock
        return lock


def standardize_bars(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
    out = df.copy()
    if "Datetime" not in out.columns:
        if out.index.name:
            out = out.reset_index().rename(columns={out.index.name: "Datetime"})
        else:
            raise ValueError(f"{source} missing Datetime column")
    out["Datetime"] = parse_utc_datetime_series(out["Datetime"])
    out["Bar_Start"] = parse_utc_datetime_series(out.get("Bar_Start", out["Datetime"]))
    for col in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        if col not in out.columns:
            raise ValueError(f"{source} missing required bar column {col}")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "Proxy_Value" not in out.columns:
        out["Proxy_Value"] = out["Ticks"]
    out["Proxy_Value"] = pd.to_numeric(out["Proxy_Value"], errors="coerce")
    out["Gap_Healed"] = out.get("Gap_Healed", False)
    out["Gap_Healed"] = out["Gap_Healed"].apply(lambda x: safe_bool(x, False))
    out["Executable"] = out.get("Executable", False)
    out["Executable"] = out["Executable"].apply(lambda x: safe_bool(x, False))
    if "Threshold_Unit" not in out.columns:
        out["Threshold_Unit"] = "ticks"
    if "Research_Bar_Mode" not in out.columns:
        out["Research_Bar_Mode"] = "standard_quote_tick"
    if "Threshold_Mode" not in out.columns:
        out["Threshold_Mode"] = "causal_daily_ewma_tick_count"
    out = out.dropna(subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"])
    out = out[(out[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return out.sort_values("Datetime", kind="mergesort").drop_duplicates(subset=["Datetime"], keep="last").reset_index(drop=True)



def pending_live_bar_asset_dir(asset: str) -> Path:
    return PENDING_LIVE_BAR_DIR / str(asset).upper()


def _pending_live_bar_record_path(asset: str, record: dict[str, Any]) -> Path:
    stamp = ensure_utc_timestamp(record.get("Datetime"))
    if stamp is None:
        raise LiveBarJournalError(f"{asset}: pending live bar is missing a valid Datetime")
    identity = hashlib.sha256(
        f"{str(asset).upper()}|{stamp.isoformat()}".encode("utf-8")
    ).hexdigest()[:24]
    return pending_live_bar_asset_dir(asset) / f"{identity}.json"


def _canonical_bar_record(record: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    datetime_names = {
        "Datetime", "Bar_Start", "Broker_Datetime",
        "Broker_Bar_Start", "Closed_At",
    }
    for name, raw_value in dict(record).items():
        value = raw_value.item() if isinstance(raw_value, np.generic) else raw_value
        if name in datetime_names and value not in (None, ""):
            stamp = ensure_utc_timestamp(value)
            value = stamp.isoformat() if stamp is not None else str(value)
        else:
            try:
                if pd.isna(value):
                    value = None
            except Exception:
                pass
        payload[name] = value
    return payload


def journal_live_bar_record(
    asset: str,
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    logger=None,
) -> Path:
    """Durably journal one live bar before state/cursor acknowledgement.

    Each bar owns a deterministic file, so no other process needs to replace or
    truncate a shared destination. Replaying the same tick batch is idempotent.
    """
    directory = pending_live_bar_asset_dir(asset)
    directory.mkdir(parents=True, exist_ok=True)
    path = _pending_live_bar_record_path(asset, record)
    payload = {
        "schema_version": "v2_pending_live_bar_v1",
        "created_at": utc_now(),
        "asset": str(asset).upper(),
        "record": _canonical_bar_record(record),
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if _canonical_bar_record(existing.get("record") or {}) != payload["record"]:
                raise LiveBarJournalError(
                    f"{asset}: conflicting pending bar journal for {payload['record'].get('Datetime')}"
                )
            return path
        except LiveBarJournalError:
            raise
        except Exception as exc:
            raise LiveBarJournalError(
                f"{asset}: unreadable pending bar journal {path}: {type(exc).__name__}: {exc}"
            ) from exc

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, default=str, allow_nan=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_backoff(temporary, path, max_wait_seconds=2.0)
    except Exception as exc:
        raise LiveBarJournalError(
            f"{asset}: durable live-bar journal write failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass

    if state is not None:
        state.setdefault("bar_persistence_status_by_asset", {})[str(asset).upper()] = {
            "status": "journaled_pending_compaction",
            "updated_at": utc_now(),
            "last_journal_path": rel(path),
        }
        state.setdefault("asset_io_paused_by_asset", {})[str(asset).upper()] = False
    if logger is not None:
        logger.info(
            "LIVE_BAR_JOURNALED asset=%s time=%s path=%s",
            asset,
            payload["record"].get("Datetime"),
            rel(path),
        )
    return path


def read_pending_live_bar_records(asset: str, logger=None) -> tuple[pd.DataFrame, list[Path]]:
    directory = pending_live_bar_asset_dir(asset)
    if not directory.exists():
        return pd.DataFrame(columns=REQUIRED_BAR_COLUMNS), []
    records: list[dict[str, Any]] = []
    paths: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("schema_version") or "") != "v2_pending_live_bar_v1":
                raise ValueError("unsupported pending-live-bar schema")
            record = payload.get("record")
            if not isinstance(record, dict):
                raise ValueError("pending-live-bar record is not an object")
            records.append(record)
            paths.append(path)
        except Exception as exc:
            if logger is not None:
                logger.error(
                    "LIVE_BAR_JOURNAL_READ_FAILED asset=%s path=%s error=%s:%s",
                    asset,
                    path,
                    type(exc).__name__,
                    exc,
                )
            raise LiveBarJournalError(
                f"{asset}: pending live-bar journal is unreadable: {path}"
            ) from exc
    if not records:
        return pd.DataFrame(columns=REQUIRED_BAR_COLUMNS), []
    return standardize_bars(pd.DataFrame(records), f"pending live bars for {asset}"), paths


def merge_live_bar_frames(
    base: pd.DataFrame | None,
    pending: pd.DataFrame | None,
    *,
    source: str,
) -> pd.DataFrame:
    frames = [
        frame.copy()
        for frame in (base, pending)
        if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if not frames:
        return pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
    return standardize_bars(
        pd.concat(frames, ignore_index=True, sort=False),
        source,
    )


def _refresh_bar_persistence_health(
    asset: str,
    state: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    retry_count: int | None = None,
) -> None:
    pending_dir = pending_live_bar_asset_dir(asset)
    paths = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    oldest_age = None
    if paths:
        try:
            oldest_age = max(
                0.0,
                time.time() - min(path.stat().st_mtime for path in paths),
            )
        except OSError:
            oldest_age = None
    key = str(asset).upper()
    state.setdefault("pending_bar_count_by_asset", {})[key] = int(len(paths))
    state.setdefault("oldest_pending_bar_age_seconds_by_asset", {})[key] = oldest_age
    state.setdefault("bar_persistence_status_by_asset", {})[key] = {
        "status": status,
        "updated_at": utc_now(),
        "pending_bar_count": int(len(paths)),
        "oldest_pending_bar_age_seconds": oldest_age,
    }
    if retry_count is not None:
        state.setdefault("bar_write_retry_count_by_asset", {})[key] = int(retry_count)
    if error:
        state.setdefault("last_bar_write_error_by_asset", {})[key] = {
            "created_at": utc_now(),
            "error": str(error),
        }
    elif status == "compact":
        state.setdefault("last_bar_write_error_by_asset", {}).pop(key, None)
    state.setdefault("asset_io_paused_by_asset", {})[key] = status == "journal_write_failed"


def compact_pending_live_bars(
    asset: str,
    state: dict[str, Any],
    logger,
    *,
    force: bool = False,
) -> bool:
    """Best-effort merge of durable per-bar journals into the canonical CSV.

    Failure to replace the canonical CSV never terminates live polling. Readers
    merge the same journal files, so the logical bar stream remains complete.
    """
    pending, journal_paths = read_pending_live_bar_records(asset, logger)
    if not journal_paths:
        _refresh_bar_persistence_health(asset, state, status="compact")
        return True

    oldest_age = max(
        0.0,
        time.time() - min(path.stat().st_mtime for path in journal_paths),
    )
    if (
        not force
        and len(journal_paths) < LIVE_BAR_COMPACT_PENDING_THRESHOLD
        and oldest_age < LIVE_BAR_COMPACT_MAX_AGE_SECONDS
    ):
        _refresh_bar_persistence_health(
            asset,
            state,
            status="journaled_pending_compaction",
        )
        return False

    path = live_bars_path(asset)
    with live_bar_file_lock(asset):
        try:
            if path.exists():
                raw = pd.read_csv(path)
                validate_live_bar_file_contract(asset, raw, path)
                base = standardize_bars(raw, str(path))
            else:
                base = pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
            out = merge_live_bar_frames(
                base,
                pending,
                source=f"compacted live bars for {asset}",
            )
            replace_diag = atomic_write_dataframe_csv(
                path,
                out,
                max_wait_seconds=LIVE_BAR_COMPACTION_MAX_WAIT_SECONDS,
            )
        except Exception as exc:
            if not (
                isinstance(exc, OSError)
                and _is_windows_sharing_violation(exc)
            ):
                raise
            error = f"{type(exc).__name__}:{exc}"
            _refresh_bar_persistence_health(
                asset,
                state,
                status="canonical_csv_locked_journal_authoritative",
                error=error,
            )
            logger.warning(
                "LIVE_BAR_COMPACTION_DEFERRED asset=%s pending=%s path=%s error=%s",
                asset,
                len(journal_paths),
                path,
                error,
            )
            return False

        removed = 0
        for journal_path in journal_paths:
            try:
                journal_path.unlink()
                removed += 1
            except FileNotFoundError:
                removed += 1
            except OSError as exc:
                logger.warning(
                    "LIVE_BAR_JOURNAL_DELETE_DEFERRED asset=%s path=%s error=%s:%s",
                    asset,
                    journal_path,
                    type(exc).__name__,
                    exc,
                )
        _refresh_bar_persistence_health(
            asset,
            state,
            status="compact" if removed == len(journal_paths) else "compacted_cleanup_pending",
            retry_count=int(replace_diag.get("attempts", 1) or 1),
        )
        logger.info(
            "LIVE_BAR_COMPACTION_DONE asset=%s merged=%s removed_journals=%s rows=%s replace_attempts=%s elapsed_seconds=%.3f",
            asset,
            len(journal_paths),
            removed,
            len(out),
            replace_diag.get("attempts"),
            float(replace_diag.get("elapsed_seconds", 0.0) or 0.0),
        )
        return True


def compact_all_pending_live_bars(
    assets: list[str],
    state: dict[str, Any],
    logger,
    *,
    force: bool = False,
) -> None:
    for asset in assets:
        try:
            compact_pending_live_bars(asset, state, logger, force=force)
        except LiveBarJournalError as exc:
            _refresh_bar_persistence_health(
                asset,
                state,
                status="journal_read_failed",
                error=f"{type(exc).__name__}:{exc}",
            )
            logger.error(
                "LIVE_BAR_COMPACTION_BLOCKED asset=%s error=%s",
                asset,
                exc,
            )



def validate_live_bar_file_contract(asset: str, raw: pd.DataFrame, path: Path) -> None:
    """Fail closed if an existing live bar file is not a V2 standard quote-tick stream.

    Old/legacy bars without metadata are dangerous because stateful live features
    can silently mix incompatible bar definitions. Archive/delete old bars and
    let the dashboard rebuild them instead of auto-assuming compatibility.
    """
    missing = [c for c in ["Threshold_Unit", "Research_Bar_Mode"] if c not in raw.columns]
    if missing:
        raise RuntimeError(
            f"Existing live bars for {asset} are missing metadata {missing}: {path}. "
            "Archive/delete old live_bars and restart dashboard."
        )
    units = {str(x).strip().lower() for x in raw["Threshold_Unit"].dropna().unique().tolist()}
    modes = {str(x).strip().lower() for x in raw["Research_Bar_Mode"].dropna().unique().tolist()}
    if units != {"ticks"} or modes != {"standard_quote_tick"}:
        raise RuntimeError(
            f"Existing live bars for {asset} have incompatible metadata units={units} modes={modes}: {path}. "
            "Archive/delete old live_bars and restart dashboard."
        )


def live_bar_file_needs_normalization(raw: pd.DataFrame, normalized: pd.DataFrame) -> bool:
    """Return True when a live-bar CSV should be rewritten in canonical time order."""
    if raw is None or raw.empty:
        return False
    if normalized is None or normalized.empty:
        return len(raw) > 0
    if len(raw) != len(normalized):
        return True
    if "Datetime" not in raw.columns:
        return True
    raw_dt = parse_utc_datetime_series(raw["Datetime"])
    if raw_dt.isna().any():
        return True
    if not raw_dt.is_monotonic_increasing:
        return True
    normalized_iso = normalized["Datetime"].apply(lambda x: ensure_utc_timestamp(x).isoformat()).reset_index(drop=True)
    raw_iso = raw_dt.apply(lambda x: x.isoformat() if pd.notna(x) else None).reset_index(drop=True)
    return not raw_iso.equals(normalized_iso)


def load_live_bars(asset: str) -> pd.DataFrame:
    """Return the canonical CSV plus any durable, not-yet-compacted live bars."""
    path = live_bars_path(asset)
    with live_bar_file_lock(asset):
        if path.exists():
            raw = pd.read_csv(path)
            validate_live_bar_file_contract(asset, raw, path)
            base = standardize_bars(raw, str(path))
            if live_bar_file_needs_normalization(raw, base):
                # Hygiene is best effort. The pending journal remains the logical
                # source of truth even when Windows temporarily locks the CSV.
                try:
                    atomic_write_dataframe_csv(
                        path,
                        base,
                        max_wait_seconds=LIVE_BAR_COMPACTION_MAX_WAIT_SECONDS,
                    )
                except OSError:
                    pass
        else:
            base = pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
        pending, _ = read_pending_live_bar_records(asset)
        return merge_live_bar_frames(
            base,
            pending,
            source=f"logical live bars for {asset}",
        )


def append_live_bar(
    asset: str,
    record: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    logger=None,
) -> None:
    """Durably publish one bar without making canonical CSV locks fatal.

    Every completed bar is first committed to a deterministic per-bar journal.
    The canonical CSV is merely a compacted projection. This applies to live and
    gap-healed rows, so a Windows lock can never terminate startup or live
    polling after the durable journal commit succeeds.
    """
    journal_live_bar_record(
        asset,
        record,
        state=state,
        logger=logger,
    )
    if state is not None and logger is not None:
        compact_pending_live_bars(
            asset,
            state,
            logger,
            force=False,
        )


def current_bar_from_state(asset: str, symbol: str, policy: dict[str, Any], state: dict[str, Any]) -> CurrentBar:
    threshold, threshold_source = active_threshold_source(policy)
    raw = state.setdefault("current_bars", {}).get(asset)
    if isinstance(raw, dict):
        try:
            if str(raw.get("threshold_unit", "ticks")) == "ticks" and str(raw.get("bar_mode", "standard_quote_tick")) == "standard_quote_tick":
                raw["mt5_symbol"] = symbol
                # Freeze the threshold for an already-open bar. If the saved bar
                # is empty, adopt the latest adaptive policy before the next tick.
                if int(raw.get("ticks", 0) or 0) <= 0:
                    raw["threshold_ticks"] = int(threshold)
                    raw["threshold_source"] = threshold_source
                else:
                    raw["threshold_ticks"] = int(raw.get("threshold_ticks", threshold))
                    raw.setdefault("threshold_source", "existing_open_bar_frozen_threshold")
                return CurrentBar(**raw)
        except Exception:
            pass
    return CurrentBar(asset=asset, mt5_symbol=symbol, threshold_ticks=threshold, threshold_source=threshold_source)


def store_current_bar(state: dict[str, Any], bar: CurrentBar) -> None:
    state.setdefault("current_bars", {})[bar.asset] = asdict(bar)


def reset_current_bar(asset: str, state: dict[str, Any]) -> None:
    state.setdefault("current_bars", {}).pop(asset, None)


def reset_gap_healed_partial_bars_before_live(
    assets: list[str],
    state: dict[str, Any],
    logger,
) -> None:
    """Discard incomplete gap-healed bridge bars before executable live mode.

    Gap-healed bars are memory-only by contract. If a historical gap-healing
    interval ends with a partial/open bar, that partial bar contains historical
    bridge ticks and must never be completed later by fresh executable live
    ticks. Resetting it here prevents a mixed memory/live bar from becoming
    executable while preserving all fully closed gap-healed bars in the live
    memory file as Executable=False.
    """
    for asset in assets:
        raw = state.setdefault("current_bars", {}).get(asset)
        if not isinstance(raw, dict):
            continue

        ticks = safe_int(raw.get("ticks"), 0) or 0
        threshold_source = str(raw.get("threshold_source") or "")
        if ticks <= 0 or not threshold_source.startswith("gap_heal_"):
            continue

        rec = {
            "asset": asset,
            "created_at": utc_now(),
            "discarded_ticks": int(ticks),
            "threshold_ticks": safe_int(raw.get("threshold_ticks")),
            "threshold_source": threshold_source,
            "bar_start": raw.get("bar_start"),
            "datetime": raw.get("datetime"),
            "reason": "gap_healed_partial_bar_must_not_become_executable",
        }
        state.setdefault("gap_healed_partial_bar_resets", []).append(rec)
        state["gap_healed_partial_bar_resets"] = state["gap_healed_partial_bar_resets"][-200:]

        logger.warning(
            "GAP_HEALED_PARTIAL_BAR_RESET asset=%s ticks=%s threshold=%s source=%s bar_start=%s datetime=%s reason=memory_only_partial_cannot_enter_live_executable_phase",
            asset,
            ticks,
            raw.get("threshold_ticks"),
            threshold_source,
            raw.get("bar_start"),
            raw.get("datetime"),
        )
        reset_current_bar(asset, state)




def refresh_asset_continuity_latest_live_from_closed_bar(
    asset: str,
    rec: dict[str, Any],
    state: dict[str, Any],
    logger,
) -> None:
    """Keep live continuity/status fresh after each executable live bar closes.

    Startup gap healing verifies research -> live continuity, but once the
    dashboard is running, heartbeat/status should not keep reporting the
    startup latest_live forever. This updates only the latest live timestamp
    audit fields. It does not change the continuity gate/status, does not
    convert memory-only bars into executable bars, and does not affect runtime
    scoring or signal approval.
    """
    try:
        event_ts = ensure_utc_timestamp(rec.get("Datetime"))
    except Exception:
        logger.warning("CONTINUITY_LATEST_LIVE_REFRESH_SKIPPED asset=%s reason=invalid_bar_datetime datetime=%s", asset, rec.get("Datetime"))
        return

    continuity_by_asset = state.setdefault("asset_continuity", {})
    continuity = continuity_by_asset.setdefault(asset, {"asset": asset})
    previous_latest_raw = continuity.get("latest_live")
    previous_latest = None
    if previous_latest_raw:
        try:
            previous_latest = ensure_utc_timestamp(previous_latest_raw)
        except Exception:
            previous_latest = None

    if previous_latest is not None and event_ts <= previous_latest:
        return

    continuity["latest_live"] = event_ts.isoformat()
    continuity["latest_live_refreshed_at"] = utc_now()
    continuity["latest_live_refresh_reason"] = "closed_executable_live_bar"
    continuity["latest_live_refresh_bar_threshold"] = safe_int(rec.get("Threshold"))
    continuity["latest_live_refresh_bar_threshold_source"] = rec.get("Threshold_Source")
    continuity_by_asset[asset] = continuity

    logger.info(
        "CONTINUITY_LATEST_LIVE_REFRESH asset=%s previous_latest=%s latest_live=%s reason=closed_executable_live_bar",
        asset,
        safe_iso(previous_latest) if previous_latest is not None else previous_latest_raw,
        event_ts.isoformat(),
    )

def process_ticks_to_bars(
    asset: str,
    symbol: str,
    policy: dict[str, Any],
    ticks_df: pd.DataFrame,
    state: dict[str, Any],
    logger,
    *,
    gap_healed: bool,
    executable: bool,
    gap_threshold_cache: dict[str, Any] | None = None,
) -> int:
    if ticks_df is None or ticks_df.empty:
        return 0
    if (not gap_healed) and executable:
        ticks_df = apply_live_tick_startup_guard(asset, symbol, ticks_df, state, logger)
        if ticks_df.empty:
            return 0
        reset_stale_current_bar_before_live_ticks(asset, symbol, policy, state, logger)
    record_raw_tick_daily_counts(
        asset,
        ticks_df,
        state,
        source="gap_healing_tick_batch" if gap_healed else "live_poll_tick_batch",
    )
    bar = current_bar_from_state(asset, symbol, policy, state)
    closed = 0
    first_closed_time: str | None = None
    last_closed_time: str | None = None
    for row in ticks_df.itertuples(index=False):
        # For historical bridge bars, choose the threshold at BAR START from the
        # target tick's UTC date, using only completed prior days. Once a bar has
        # started, its threshold remains frozen until it closes.
        if gap_healed and gap_threshold_cache is not None and bar.is_empty():
            threshold_info = gap_threshold_for_tick(asset, policy, row.timestamp, gap_threshold_cache)
            bar.threshold_ticks = int(threshold_info["threshold_ticks"])
            bar.threshold_source = str(threshold_info["threshold_source"])
            bar.threshold_mode = str(threshold_info.get("threshold_mode") or "causal_daily_ewma_tick_count")
            bar.threshold_unit = "ticks"
            bar.bar_mode = "standard_quote_tick"

        bar.update(row.timestamp, row.broker_timestamp, float(row.mid), int(row.broker_time_offset_seconds or 0))
        if bar.should_close():
            if gap_healed:
                src = str(bar.threshold_source or "unknown")
                close_threshold_source = src if src.startswith("gap_heal_") else f"gap_heal_{src}"
            else:
                close_threshold_source = str(bar.threshold_source or "live_bar_policy_unknown")
            rec = bar.close_record(gap_healed=gap_healed, executable=executable, threshold_source=close_threshold_source)
            if (not gap_healed) and executable:
                assert_executable_bar_after_startup_cutoff(asset, rec, state)
            append_live_bar(asset, rec, state=state, logger=logger)
            if (not gap_healed) and executable:
                refresh_asset_continuity_latest_live_from_closed_bar(asset, rec, state, logger)
            if gap_healed and gap_threshold_cache is not None:
                record_gap_closed_bar_in_threshold_cache(rec, gap_threshold_cache)
            closed += 1
            first_closed_time = first_closed_time or str(rec.get("Datetime"))
            last_closed_time = str(rec.get("Datetime"))
            state.setdefault("closed_bar_counts", {})[asset] = int(state.setdefault("closed_bar_counts", {}).get(asset, 0)) + 1
            state["total_closed_bars_since_state_created"] = int(state.get("total_closed_bars_since_state_created", 0)) + 1
            if (not gap_healed) or closed <= 5 or closed % max(GAP_HEALED_BAR_LOG_EVERY_N, 1) == 0:
                logger.info(
                    "%s asset=%s symbol=%s time=%s ticks=%s threshold=%s source=%s close=%.5f executable=%s",
                    "GAP_HEALED_BAR" if gap_healed else "CLOSED_BAR",
                    asset,
                    symbol,
                    rec.get("Datetime"),
                    rec.get("Ticks"),
                    rec.get("Threshold"),
                    rec.get("Threshold_Source"),
                    float(rec.get("Close")),
                    executable,
                )
            bar.reset()
            # The next bar may start after a daily adaptive refresh, so adopt the
            # latest active policy only after the previous bar is closed. For
            # gap-healing, the next tick will set a date-specific causal threshold.
            if not (gap_healed and gap_threshold_cache is not None):
                next_threshold, next_threshold_source = active_threshold_source(policy)
                bar.threshold_ticks = int(next_threshold)
                bar.threshold_source = next_threshold_source
    if gap_healed and closed > 5:
        extra = ""
        if gap_threshold_cache is not None:
            summary = serializable_gap_threshold_summary(gap_threshold_cache)
            extra = (
                f" adaptive_days={summary.get('adaptive_threshold_days_used')} "
                f"fallback_days={summary.get('fallback_threshold_days_used')} "
                f"threshold_first={summary.get('threshold_date_first')} "
                f"threshold_last={summary.get('threshold_date_last')}"
            )
        logger.info(
            "GAP_HEALED_BAR_BATCH_SUMMARY asset=%s symbol=%s bars_created=%s first_bar=%s last_bar=%s executable=False%s",
            asset,
            symbol,
            closed,
            first_closed_time,
            last_closed_time,
            extra,
        )
    store_current_bar(state, bar)
    return closed

def load_seed_bars_for_asset(asset: str, policy: dict[str, Any]) -> pd.DataFrame:
    if asset in _SEED_BAR_CACHE:
        return _SEED_BAR_CACHE[asset].copy()
    source = str(policy.get("research_manifest_source") or f"assets/{asset}/bars/standard_quote_tick/bar_manifest.json")
    manifest_path = V2_ROOT / source
    if not manifest_path.exists():
        _SEED_BAR_CACHE[asset] = pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
        return _SEED_BAR_CACHE[asset].copy()
    obj = read_json(manifest_path)
    bars_rel = (obj.get("outputs") or {}).get("bars") or ((obj.get("artifact_hashes") or {}).get("bars") or {}).get("path")
    bars_path = V2_ROOT / str(bars_rel) if bars_rel else None
    seed = standardize_bars(pd.read_csv(bars_path), str(bars_path)) if bars_path and bars_path.exists() else pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
    _SEED_BAR_CACHE[asset] = seed
    return seed.copy()


def combined_bars_for_asset(
    asset: str,
    policy: dict[str, Any],
    *,
    max_rows: int | None = MAX_COMBINED_BARS_FOR_RUNTIME,
) -> pd.DataFrame:
    """Return research-seed + live bars with an explicit consumer-specific limit.

    Runtime recovery uses the evidence-based cold-bootstrap size (at least 6,000
    raw bars for the 1,000-bar FFD + 5,000-row HMM contract). Daily adaptive
    threshold reconstruction requests ``max_rows=None`` so risk/bar-policy
    history is not accidentally shortened by a runtime-memory setting.
    """
    seed = load_seed_bars_for_asset(asset, policy)
    live = load_live_bars(asset)
    if seed.empty and live.empty:
        return pd.DataFrame(columns=REQUIRED_BAR_COLUMNS)
    out = standardize_bars(pd.concat([seed, live], ignore_index=True, sort=False), f"combined_bars:{asset}")
    if max_rows is not None:
        out = out.tail(max(int(max_rows), 1))
    return out.reset_index(drop=True)


def latest_live_bar_timestamp(asset: str) -> pd.Timestamp | None:
    live = load_live_bars(asset)
    if live.empty:
        return None
    return ensure_utc_timestamp(live["Datetime"].max())


def completed_daily_tick_profile_from_bars(
    asset: str,
    policy: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return causal completed UTC-day tick counts for threshold continuation.

    Priority is direct raw quote-tick counts captured by this dashboard, then the
    Step-03 policy profile, then the legacy completed-bar approximation. This
    keeps backward compatibility while removing bar-close-date attribution for
    every day for which direct tick counts are available.
    """
    merged: dict[str, int] = {}
    sources: dict[str, str] = {}

    for row in list(policy.get("completed_daily_tick_profile") or []) + list(
        policy.get("completed_daily_tick_profile_tail") or []
    ):
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or "").strip()
        count = safe_int(row.get("tick_count"), None)
        if day and count is not None and count > 0:
            merged[day] = int(count)
            sources[day] = "live_bar_policy_raw_tick_profile"

    bars = combined_bars_for_asset(asset, policy, max_rows=None)
    if not bars.empty:
        bars = bars.copy()
        bars["Datetime"] = parse_utc_datetime_series(bars["Datetime"])
        bars["Ticks"] = pd.to_numeric(bars.get("Ticks"), errors="coerce")
        bars = bars.dropna(subset=["Datetime", "Ticks"])
        bars = bars[bars["Ticks"] > 0].copy()
        if not bars.empty:
            bars["date"] = bars["Datetime"].dt.date.astype(str)
            profile = bars.groupby("date", as_index=False)["Ticks"].sum()
            for row in profile.itertuples(index=False):
                day = str(row.date)
                if day not in merged:
                    merged[day] = int(round(float(row.Ticks)))
                    sources[day] = "completed_bar_tick_sum_fallback"

    if state is not None and RAW_TICK_DAILY_PROFILE_ENABLED:
        raw_counts = (
            state.get("raw_daily_tick_counts_by_asset", {}).get(asset, {}) or {}
        )
        for day, count_raw in raw_counts.items():
            count = safe_int(count_raw, None)
            if count is not None and count > 0:
                merged[str(day)] = int(count)
                sources[str(day)] = "dashboard_direct_raw_quote_tick_count"

    current_date = utc_now_ts().date().isoformat()
    min_date = None
    if ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS > 0:
        min_date = (
            utc_now_ts() - pd.Timedelta(days=ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS)
        ).date().isoformat()
    days = [
        day
        for day in sorted(merged)
        if day < current_date and (min_date is None or day >= min_date)
    ]
    if state is not None:
        state.setdefault("raw_daily_tick_profile_audit_by_asset", {}).setdefault(asset, {}).update(
            {
                "threshold_profile_built_at": utc_now(),
                "threshold_profile_days": len(days),
                "threshold_profile_source_counts": dict(Counter(sources[d] for d in days)),
                "threshold_profile_first_day": days[0] if days else None,
                "threshold_profile_last_day": days[-1] if days else None,
            }
        )
    return [
        {"date": day, "tick_count": int(merged[day]), "source": sources.get(day)}
        for day in days
    ]

def compute_adaptive_threshold_from_completed_bars(
    asset: str,
    policy: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_bpd = float(policy.get("target_bars_per_day") or ADAPTIVE_THRESHOLD_TARGET_BARS_PER_DAY)
    span = int(policy.get("daily_tick_ewma_span_days") or ADAPTIVE_THRESHOLD_EWMA_SPAN_DAYS)
    min_days = int(policy.get("min_warmup_days") or ADAPTIVE_THRESHOLD_MIN_WARMUP_DAYS)
    profile = completed_daily_tick_profile_from_bars(asset, policy, state)
    values = [float(row["tick_count"]) for row in profile]
    threshold = None
    ewma_daily_tick_count = None
    if len(values) >= min_days:
        ewma_daily_tick_count = float(pd.Series(values).ewm(span=span, adjust=False).mean().iloc[-1])
        threshold = max(int(round(ewma_daily_tick_count / max(target_bpd, 1.0))), int(ADAPTIVE_THRESHOLD_MIN_TICKS))
    return {
        "asset": asset,
        "computed_next_session_threshold_ticks": threshold,
        "completed_daily_profile_days": len(profile),
        "completed_daily_tick_profile_tail": profile[-60:],
        "warmup_ready_from_live_history": len(values) >= min_days,
        "target_bars_per_day": target_bpd,
        "daily_tick_ewma_span_days": span,
        "min_warmup_days": min_days,
        "ewma_daily_tick_count": ewma_daily_tick_count,
        "uses_future_data": False,
        "use_completed_days_only": True,
        "exclude_current_incomplete_day": True,
    }


def _date_str_from_any(ts_or_date: Any) -> str:
    """Normalize a timestamp/date-like object to YYYY-MM-DD in UTC.

    Used by gap healing so historical bridge thresholds are keyed by the UTC
    trading day that was actually being reconstructed.
    """
    try:
        return ensure_utc_timestamp(ts_or_date).date().isoformat()
    except Exception:
        return pd.Timestamp(ts_or_date).date().isoformat()


def completed_daily_tick_counts_from_bars(
    asset: str,
    policy: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Return all known daily tick counts for causal gap reconstruction."""
    out: dict[str, int] = {}
    bars = combined_bars_for_asset(asset, policy, max_rows=None)
    if not bars.empty:
        bars = bars.copy()
        bars["Datetime"] = parse_utc_datetime_series(bars["Datetime"])
        bars["Ticks"] = pd.to_numeric(bars.get("Ticks"), errors="coerce")
        bars = bars.dropna(subset=["Datetime", "Ticks"])
        bars = bars[bars["Ticks"] > 0].copy()
        if not bars.empty:
            bars["date"] = bars["Datetime"].dt.date.astype(str)
            profile = bars.groupby("date", as_index=False)["Ticks"].sum().rename(columns={"Ticks": "tick_count"})
            out.update({str(row.date): int(round(float(row.tick_count))) for row in profile.itertuples(index=False)})

    for row in list(policy.get("completed_daily_tick_profile") or []) + list(policy.get("completed_daily_tick_profile_tail") or []):
        if not isinstance(row, dict):
            continue
        day = str(row.get("date") or "").strip()
        count = safe_int(row.get("tick_count"), None)
        if day and count is not None and count > 0:
            out[day] = int(count)
    if state is not None and RAW_TICK_DAILY_PROFILE_ENABLED:
        for day, count_raw in (state.get("raw_daily_tick_counts_by_asset", {}).get(asset, {}) or {}).items():
            count = safe_int(count_raw, None)
            if count is not None and count > 0:
                out[str(day)] = int(count)
    return out

def compute_gap_heal_threshold_for_date(
    asset: str,
    policy: dict[str, Any],
    daily_tick_counts: dict[str, int],
    target_date: str,
) -> dict[str, Any]:
    """Compute the threshold that would have been known before target_date.

    This is the critical no-leakage rule for historical gap healing:
    - use only completed UTC dates strictly before target_date;
    - optionally cap lookback age relative to target_date, not relative to now;
    - freeze the returned threshold for any bar opened on target_date;
    - fall back only when prior-day warmup is incomplete.
    """
    target_date = str(target_date)
    target_dt = pd.Timestamp(target_date).date()
    min_date: str | None = None
    if ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS > 0:
        min_date = (pd.Timestamp(target_dt) - pd.Timedelta(days=ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS)).date().isoformat()

    eligible_dates = []
    for date_key in daily_tick_counts.keys():
        if str(date_key) >= target_date:
            continue
        if min_date is not None and str(date_key) < min_date:
            continue
        eligible_dates.append(str(date_key))
    eligible_dates = sorted(eligible_dates)
    values = [float(daily_tick_counts[d]) for d in eligible_dates]

    target_bpd = float(policy.get("target_bars_per_day") or ADAPTIVE_THRESHOLD_TARGET_BARS_PER_DAY)
    span = int(policy.get("daily_tick_ewma_span_days") or ADAPTIVE_THRESHOLD_EWMA_SPAN_DAYS)
    min_days = int(policy.get("min_warmup_days") or ADAPTIVE_THRESHOLD_MIN_WARMUP_DAYS)
    fallback_raw = policy.get("fallback_initial_threshold_ticks")
    fallback = int(round(float(fallback_raw))) if fallback_raw is not None and str(fallback_raw).strip() else int(ADAPTIVE_THRESHOLD_MIN_TICKS)

    ewma_daily_tick_count = None
    warmup_ready = len(values) >= min_days
    if warmup_ready:
        ewma_daily_tick_count = float(pd.Series(values).ewm(span=span, adjust=False).mean().iloc[-1])
        threshold = max(int(round(ewma_daily_tick_count / max(target_bpd, 1.0))), int(ADAPTIVE_THRESHOLD_MIN_TICKS))
        source = "gap_heal_causal_daily_ewma_tick_count"
    else:
        threshold = max(int(fallback), 1)
        source = "gap_heal_fallback_initial_threshold_ticks_warmup_incomplete"

    return {
        "asset": asset,
        "target_date": target_date,
        "threshold_ticks": int(threshold),
        "threshold_source": source,
        "threshold_mode": "causal_daily_ewma_tick_count",
        "target_bars_per_day": target_bpd,
        "daily_tick_ewma_span_days": span,
        "min_warmup_days": min_days,
        "completed_prior_days": len(values),
        "warmup_ready": bool(warmup_ready),
        "ewma_daily_tick_count": ewma_daily_tick_count,
        "uses_future_data": False,
        "use_completed_prior_days_only": True,
        "exclude_target_incomplete_day": True,
        "profile_first_date": eligible_dates[0] if eligible_dates else None,
        "profile_last_date": eligible_dates[-1] if eligible_dates else None,
    }


def make_gap_threshold_cache(
    asset: str,
    policy: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build mutable cache used during one gap-healing interval.

    As memory-only bars are closed, their tick counts are added to
    daily_tick_counts. Later reconstructed days can therefore use earlier
    reconstructed days, but never bars from their own date or future dates.
    """
    return {
        "asset": asset,
        "daily_tick_counts": completed_daily_tick_counts_from_bars(asset, policy, state),
        "raw_tick_counts_preloaded": False,
        "threshold_info_by_date": {},
        "adaptive_days_used": set(),
        "fallback_days_used": set(),
        "bars_by_threshold_source": {},
    }


def gap_threshold_for_tick(
    asset: str,
    policy: dict[str, Any],
    tick_timestamp: Any,
    gap_threshold_cache: dict[str, Any],
) -> dict[str, Any]:
    target_date = _date_str_from_any(tick_timestamp)
    cache = gap_threshold_cache.setdefault("threshold_info_by_date", {})
    if target_date not in cache:
        info = compute_gap_heal_threshold_for_date(
            asset=asset,
            policy=policy,
            daily_tick_counts=gap_threshold_cache.setdefault("daily_tick_counts", {}),
            target_date=target_date,
        )
        cache[target_date] = info
        if str(info.get("threshold_source")) == "gap_heal_causal_daily_ewma_tick_count":
            gap_threshold_cache.setdefault("adaptive_days_used", set()).add(target_date)
        else:
            gap_threshold_cache.setdefault("fallback_days_used", set()).add(target_date)
    return cache[target_date]


def record_gap_closed_bar_in_threshold_cache(rec: dict[str, Any], gap_threshold_cache: dict[str, Any]) -> None:
    """Update the causal daily count cache after a gap bar closes."""
    try:
        date_key = _date_str_from_any(rec.get("Datetime"))
        ticks = int(round(float(rec.get("Ticks", 0) or 0)))
        if ticks > 0 and not bool(gap_threshold_cache.get("raw_tick_counts_preloaded", False)):
            counts = gap_threshold_cache.setdefault("daily_tick_counts", {})
            counts[date_key] = int(counts.get(date_key, 0)) + ticks
        source = str(rec.get("Threshold_Source") or "unknown")
        by_source = gap_threshold_cache.setdefault("bars_by_threshold_source", {})
        by_source[source] = int(by_source.get(source, 0)) + 1
    except Exception:
        return


def serializable_gap_threshold_summary(gap_threshold_cache: dict[str, Any]) -> dict[str, Any]:
    info_by_date = gap_threshold_cache.get("threshold_info_by_date", {}) or {}
    ordered_dates = sorted(str(k) for k in info_by_date.keys())
    return {
        "gap_threshold_mode": "causal_daily_ewma_tick_count",
        "gap_threshold_uses_future_data": False,
        "gap_threshold_reconstruction": "day_by_day_completed_prior_daily_tick_counts",
        "adaptive_threshold_days_used": len(gap_threshold_cache.get("adaptive_days_used", set()) or []),
        "fallback_threshold_days_used": len(gap_threshold_cache.get("fallback_days_used", set()) or []),
        "threshold_days_total": len(ordered_dates),
        "threshold_date_first": ordered_dates[0] if ordered_dates else None,
        "threshold_date_last": ordered_dates[-1] if ordered_dates else None,
        "bars_by_threshold_source": dict(gap_threshold_cache.get("bars_by_threshold_source", {}) or {}),
        "threshold_audit_tail": [info_by_date[d] for d in ordered_dates[-20:]],
    }


def refresh_asset_threshold_policy(asset: str, policy: dict[str, Any], state: dict[str, Any], logger, *, reason: str) -> bool:
    """Refresh the adaptive threshold for one asset from completed daily bars.

    Returns True if the in-memory policy changed. The active threshold is only
    applied to new/empty bars; already-open bars keep their stored threshold.
    """
    if not ADAPTIVE_THRESHOLD_REFRESH_ENABLED:
        return False
    if str(policy.get("threshold_mode", "causal_daily_ewma_tick_count")) != "causal_daily_ewma_tick_count":
        logger.warning("THRESHOLD_REFRESH_SKIPPED asset=%s reason=unsupported_threshold_mode mode=%s", asset, policy.get("threshold_mode"))
        return False

    calc = compute_adaptive_threshold_from_completed_bars(asset, policy, state)
    old_threshold, old_source = active_threshold_source(policy)
    new_threshold = calc.get("computed_next_session_threshold_ticks")
    changed = False
    if new_threshold is not None:
        if safe_int(policy.get("computed_next_session_threshold_ticks")) != int(new_threshold):
            policy["computed_next_session_threshold_ticks"] = int(new_threshold)
            changed = True
    # If warmup is not ready, do not delete an existing computed threshold. The
    # fallback remains explicit and auditable if no computed value exists.
    policy["completed_daily_profile_days"] = int(calc["completed_daily_profile_days"])
    policy["warmup_ready_from_live_history"] = bool(calc["warmup_ready_from_live_history"])
    policy["completed_daily_tick_profile_tail"] = calc["completed_daily_tick_profile_tail"]
    policy["dashboard_threshold_refresh_method"] = "direct_raw_quote_tick_count_preferred_then_policy_then_completed_bar_fallback_ewma"
    policy["last_dashboard_threshold_refresh_at"] = utc_now()
    policy["last_dashboard_threshold_refresh_reason"] = reason
    policy["dashboard_threshold_refresh_uses_future_data"] = False
    if calc.get("ewma_daily_tick_count") is not None:
        policy["dashboard_ewma_daily_tick_count"] = calc["ewma_daily_tick_count"]

    new_active, new_source = active_threshold_source(policy)
    rec = {
        "asset": asset,
        "created_at": utc_now(),
        "reason": reason,
        "old_threshold_ticks": int(old_threshold),
        "old_threshold_source": old_source,
        "new_threshold_ticks": int(new_active),
        "new_threshold_source": new_source,
        "computed_next_session_threshold_ticks": policy.get("computed_next_session_threshold_ticks"),
        "fallback_initial_threshold_ticks": policy.get("fallback_initial_threshold_ticks"),
        "warmup_ready_from_live_history": policy.get("warmup_ready_from_live_history"),
        "completed_daily_profile_days": policy.get("completed_daily_profile_days"),
    }
    state.setdefault("last_threshold_refresh_at_by_asset", {})[asset] = rec["created_at"]
    state.setdefault("last_threshold_refresh_date_by_asset", {})[asset] = utc_now_ts().date().isoformat()
    state.setdefault("threshold_refresh_history", []).append(rec)
    state["threshold_refresh_history"] = state["threshold_refresh_history"][-500:]

    if new_source == "fallback_initial_threshold_ticks":
        logger.warning(
            "THRESHOLD_FALLBACK_ACTIVE asset=%s threshold=%s reason=adaptive_warmup_incomplete_or_no_computed_threshold completed_days=%s min_days=%s",
            asset, new_active, calc["completed_daily_profile_days"], calc["min_warmup_days"],
        )
    logger.info(
        "THRESHOLD_POLICY_ACTIVE asset=%s threshold=%s source=%s target_bars_per_day=%s completed_days=%s warmup_ready=%s changed=%s reason=%s",
        asset, new_active, new_source, calc["target_bars_per_day"], calc["completed_daily_profile_days"], calc["warmup_ready_from_live_history"], changed, reason,
    )
    return changed


def refresh_all_threshold_policies(assets: list[str], bar_policy: dict[str, Any], state: dict[str, Any], logger, *, reason: str, force: bool = False) -> bool:
    if not ADAPTIVE_THRESHOLD_REFRESH_ENABLED:
        return False
    now = utc_now_ts()
    changed_any = False
    refreshed_any = False
    for asset in assets:
        policy = (bar_policy.get("bar_policies") or {}).get(asset)
        if not isinstance(policy, dict):
            continue
        last_raw = state.setdefault("last_threshold_refresh_at_by_asset", {}).get(asset)
        should_refresh = force or not last_raw
        if not should_refresh:
            try:
                last_ts = ensure_utc_timestamp(last_raw)
                should_refresh = float((now - last_ts).total_seconds()) >= ADAPTIVE_THRESHOLD_REFRESH_EVERY_SECONDS
            except Exception:
                should_refresh = True
        # Always refresh on a new UTC date so the threshold can adapt daily.
        last_date = state.setdefault("last_threshold_refresh_date_by_asset", {}).get(asset)
        if last_date != now.date().isoformat():
            should_refresh = True
        if should_refresh:
            refreshed_any = True
            changed_any = refresh_asset_threshold_policy(asset, policy, state, logger, reason=reason) or changed_any
    if (changed_any or (force and refreshed_any)) and ADAPTIVE_THRESHOLD_WRITE_POLICY_UPDATES:
        bar_policy["updated_at"] = utc_now()
        bar_policy["dashboard_adaptive_threshold_refresh_enabled"] = True
        bar_policy["dashboard_adaptive_threshold_refresh_method"] = "completed_daily_bar_tick_sum_ewma"
        bar_policy["dashboard_adaptive_threshold_refresh_uses_future_data"] = False
        write_json(LIVE_BAR_POLICY_PATH, bar_policy)
    return changed_any


def partial_bar_progress_by_asset(assets: list[str], bar_policy: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for asset in assets:
        policy = (bar_policy.get("bar_policies") or {}).get(asset, {})
        threshold, source = active_threshold_source(policy) if isinstance(policy, dict) else (None, None)
        raw = state.setdefault("current_bars", {}).get(asset, {})
        if isinstance(raw, dict):
            ticks = int(raw.get("ticks", 0) or 0)
            bar_threshold = int(raw.get("threshold_ticks", threshold or 0) or 0)
            out[asset] = {
                "current_bar_ticks": ticks,
                "bar_threshold_ticks": bar_threshold,
                "ticks_to_next_bar": max(bar_threshold - ticks, 0) if bar_threshold else None,
                "bar_threshold_source": raw.get("threshold_source", source),
                "active_policy_threshold_ticks": threshold,
                "active_policy_threshold_source": source,
                "bar_start": raw.get("bar_start"),
                "latest_bar_time": (state.get("asset_continuity") or {}).get(asset, {}).get("latest_live"),
            }
        else:
            out[asset] = {
                "current_bar_ticks": 0,
                "bar_threshold_ticks": threshold,
                "ticks_to_next_bar": threshold,
                "bar_threshold_source": source,
                "active_policy_threshold_ticks": threshold,
                "active_policy_threshold_source": source,
                "bar_start": None,
                "latest_bar_time": (state.get("asset_continuity") or {}).get(asset, {}).get("latest_live"),
            }
    return out


def latest_live_executable_timestamps(asset: str) -> set[pd.Timestamp]:
    live = load_live_bars(asset)
    if live.empty:
        return set()
    live = live[(live["Executable"].apply(lambda x: safe_bool(x, False))) & (~live["Gap_Healed"].apply(lambda x: safe_bool(x, False)))].copy()
    return set(parse_utc_datetime_series(live["Datetime"]).dropna())


def continuity_ok(asset: str, state: dict[str, Any]) -> bool:
    status = ((state.get("asset_continuity") or {}).get(asset) or {}).get("status")
    return str(status) in PASS_CONTINUITY_STATUSES


def get_runtime(edge: dict[str, Any]) -> Any:
    edge_id = str(edge.get("edge_id"))
    with _RUNTIME_CACHE_LOCK:
        if edge_id not in _RUNTIME_CACHE:
            _RUNTIME_CACHE[edge_id] = import_edge_runtime(edge)
        return _RUNTIME_CACHE[edge_id]


def edge_root_from_manifest_edge(edge: dict[str, Any]) -> Path:
    """Resolve an edge root from manifest metadata using repo layout fallback."""
    for key in ("edge_root", "edge_dir", "edge_path", "runtime_root"):
        raw = edge.get(key)
        if raw:
            path = Path(str(raw))
            return path if path.is_absolute() else (V2_ROOT / path).resolve()
    runtime_path = edge.get("runtime_path") or edge.get("runtime_py")
    if runtime_path:
        path = Path(str(runtime_path))
        path = path if path.is_absolute() else (V2_ROOT / path)
        return path.parent.resolve()
    edge_id = str(edge.get("edge_id") or "")
    asset = str(edge.get("asset") or "")
    edge_name = str(edge.get("edge_name") or "")
    if "/" in edge_id:
        asset_from_id, edge_from_id = edge_id.split("/", 1)
        asset = asset or asset_from_id
        edge_name = edge_name or edge_from_id
    if not asset or not edge_name:
        raise ValueError(f"Cannot resolve edge root from manifest edge: {edge}")
    return (V2_ROOT / "assets" / asset / "edges" / edge_name).resolve()


def preload_live_edge_artifacts(manifest: dict[str, Any], state: dict[str, Any], logger) -> dict[str, Any]:
    """Warm selected edge runtimes and scorer-owned artifacts at startup.

    Phase 9 speed/stability only. This does not change edge feature logic, event
    generation, model inputs, probabilities, thresholds, stale gates, signal
    approval, or execution handoff. Any failed warmup falls back to normal lazy
    loading unless LIVE_DASHBOARD_FAIL_ON_PRELOAD_ERROR=True.
    """
    start = time.perf_counter()
    selected_edges = manifest.get("selected_edges", []) or []
    if not PRELOAD_EDGE_ARTIFACTS_ENABLED:
        summary = {
            "created_at": utc_now(),
            "enabled": False,
            "status": "BYPASS",
            "reason": "LIVE_DASHBOARD_PRELOAD_EDGE_ARTIFACTS_ENABLED=False",
            "selected_edge_count": len(selected_edges),
        }
        state["last_edge_artifact_preload"] = summary
        logger.info("LIVE_EDGE_ARTIFACT_PRELOAD_BYPASS enabled=False edges=%s", len(selected_edges))
        return summary

    try:
        from shared.scoring.edge_scorer import edge_scorer_cache_snapshot, preload_edge_scorer_artifacts  # noqa: WPS433
    except Exception as exc:
        summary = {
            "created_at": utc_now(),
            "enabled": True,
            "status": "FAILED_IMPORT",
            "selected_edge_count": len(selected_edges),
            "elapsed_ms": elapsed_ms(start),
            "error": f"{type(exc).__name__}: {exc}",
        }
        state["last_edge_artifact_preload"] = summary
        logger.warning("LIVE_EDGE_ARTIFACT_PRELOAD_IMPORT_FAILED error=%s", exc)
        if FAIL_ON_PRELOAD_ERROR:
            raise
        return summary

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    logger.info(
        "LIVE_EDGE_ARTIFACT_PRELOAD_START edges=%s fail_on_error=%s mode=runtime_import_plus_edge_scorer_cache_warmup",
        len(selected_edges),
        FAIL_ON_PRELOAD_ERROR,
    )
    for edge in selected_edges:
        edge_id = str(edge.get("edge_id"))
        edge_start = time.perf_counter()
        try:
            runtime = get_runtime(edge)
            edge_root = edge_root_from_manifest_edge(edge)
            preload = preload_edge_scorer_artifacts(edge_root=edge_root, validate=True)
            record = {
                "edge_id": edge_id,
                "asset": edge.get("asset"),
                "status": "PASS",
                "elapsed_ms": elapsed_ms(edge_start),
                "runtime_module": getattr(runtime, "__name__", type(runtime).__name__),
                "edge_root": rel(edge_root),
                "selected_feature_count": preload.get("selected_feature_count"),
                "model_input_column_count": preload.get("model_input_column_count"),
                "threshold": preload.get("threshold"),
                "calibrator_method": preload.get("calibrator_method"),
                "model_class": preload.get("model_class"),
            }
            records.append(record)
            logger.info(
                "LIVE_EDGE_ARTIFACT_PRELOAD_EDGE_DONE edge=%s asset=%s elapsed_ms=%.3f selected_features=%s model_class=%s threshold=%s",
                edge_id,
                edge.get("asset"),
                record["elapsed_ms"],
                record.get("selected_feature_count"),
                record.get("model_class"),
                record.get("threshold"),
            )
        except Exception as exc:
            record = {
                "edge_id": edge_id,
                "asset": edge.get("asset"),
                "status": "FAILED",
                "elapsed_ms": elapsed_ms(edge_start),
                "error": f"{type(exc).__name__}: {exc}",
            }
            records.append(record)
            failures.append(record)
            logger.warning("LIVE_EDGE_ARTIFACT_PRELOAD_EDGE_FAILED edge=%s asset=%s error=%s", edge_id, edge.get("asset"), exc)
            logger.warning(traceback.format_exc())
            if FAIL_ON_PRELOAD_ERROR:
                raise

    cache_snapshot = edge_scorer_cache_snapshot()
    summary = {
        "created_at": utc_now(),
        "enabled": True,
        "status": "PASS" if not failures else "PARTIAL_PASS",
        "selected_edge_count": len(selected_edges),
        "preloaded_edges": int(sum(1 for rec in records if rec.get("status") == "PASS")),
        "failed_edges": int(len(failures)),
        "elapsed_ms": elapsed_ms(start),
        "cache_snapshot": cache_snapshot,
        "records_tail": records[-50:],
        "failures": failures[-20:],
    }
    state["last_edge_artifact_preload"] = summary
    logger.info(
        "LIVE_EDGE_ARTIFACT_PRELOAD_DONE status=%s edges=%s preloaded=%s failed=%s elapsed_ms=%.3f cache_entries=%s cache_stats=%s",
        summary["status"],
        summary["selected_edge_count"],
        summary["preloaded_edges"],
        summary["failed_edges"],
        summary["elapsed_ms"],
        cache_snapshot.get("cache_entries"),
        cache_snapshot.get("stats"),
    )
    return summary


def fetch_ticks_range_chunked(mt5: Any, asset: str, symbol: str, start_ts: pd.Timestamp, end_ts: pd.Timestamp, state: dict[str, Any], logger) -> pd.DataFrame:
    start_ts = ensure_utc_timestamp(start_ts)
    end_ts = ensure_utc_timestamp(end_ts)
    if start_ts >= end_ts:
        out = pd.DataFrame()
        out.attrs["hit_tick_cap"] = False
        return out

    if not is_broker_offset_locked(symbol, state):
        lock_broker_time_offset(symbol, state, logger, source="pre_gap_healing_existing_offset")

    offset_before = get_stored_broker_offset_seconds(symbol, state)
    api_start = true_utc_to_broker_time(start_ts, symbol, state)
    api_end = true_utc_to_broker_time(end_ts, symbol, state)
    chunks: list[pd.DataFrame] = []
    cursor = api_start
    tick_count = 0
    hit_cap = False

    while cursor < api_end:
        chunk_end = min(cursor + pd.Timedelta(hours=GAP_FETCH_CHUNK_HOURS), api_end)
        chunk_true_start = ensure_utc_timestamp(cursor) - pd.Timedelta(seconds=offset_before)
        chunk_true_end = ensure_utc_timestamp(chunk_end) - pd.Timedelta(seconds=offset_before)
        is_last_chunk = bool(chunk_end >= api_end)
        ticks = mt5.copy_ticks_range(symbol, cursor.to_pydatetime(), chunk_end.to_pydatetime(), mt5.COPY_TICKS_ALL)
        if ticks is not None and len(ticks) > 0:
            df = normalize_ticks_df(
                pd.DataFrame(ticks),
                asset=asset,
                symbol=symbol,
                state=state,
                logger=logger,
                allow_offset_inference=False,
                offset_source="historical_gap_healing_locked_offset",
            )
            offset_after = get_stored_broker_offset_seconds(symbol, state)
            if offset_after != offset_before:
                raise RuntimeError(
                    f"{asset}/{symbol}: broker offset changed during gap healing "
                    f"from {offset_before} to {offset_after}; refusing to build mixed-time bars"
                )
            if not df.empty:
                # Use half-open chunk boundaries to avoid API-inclusive boundary
                # duplicates without collapsing legitimate identical ticks that
                # share the same millisecond and quote payload.
                right_mask = (
                    df["timestamp"] <= min(chunk_true_end, end_ts)
                    if is_last_chunk
                    else df["timestamp"] < min(chunk_true_end, end_ts)
                )
                df = df[
                    (df["timestamp"] >= max(chunk_true_start, start_ts))
                    & right_mask
                ].copy()
            if not df.empty:
                chunks.append(df)
                tick_count += len(df)
                if tick_count >= MAX_TICKS_PER_GAP_ASSET:
                    hit_cap = True
                    logger.warning("GAP_FETCH_TICK_CAP asset=%s symbol=%s max=%s", asset, symbol, MAX_TICKS_PER_GAP_ASSET)
                    break
        cursor = chunk_end

    if not chunks:
        out = pd.DataFrame()
    else:
        out = pd.concat(chunks, ignore_index=True, sort=False).sort_values(
            ["time_msc", "bid", "ask", "flags"], kind="mergesort"
        ).reset_index(drop=True)
    out.attrs["hit_tick_cap"] = hit_cap
    out.attrs["offset_seconds"] = offset_before
    out.attrs["last_tick_timestamp_utc"] = safe_iso(out["timestamp"].iloc[-1]) if not out.empty else None
    return out

def heal_interval(mt5: Any, asset: str, symbol: str, policy: dict[str, Any], start: pd.Timestamp, end: pd.Timestamp, state: dict[str, Any], logger, label: str, *, reset_partial: bool = False) -> dict[str, Any]:
    start = ensure_utc_timestamp(start)
    end = ensure_utc_timestamp(end)
    requested_gap_seconds = float((end - start).total_seconds())
    if requested_gap_seconds <= MIN_GAP_SECONDS_TO_HEAL:
        return {
            "interval": label,
            "status": "pass",
            "reason": "no_material_gap",
            "requested_start": safe_iso(start),
            "actual_start": safe_iso(start),
            "end": safe_iso(end),
            "requested_gap_seconds": requested_gap_seconds,
            "requested_gap_hours": requested_gap_seconds / 3600.0,
            "ticks_fetched": 0,
            "bridge_bars_created": 0,
            "fully_healed": True,
            "why_partial": None,
            "suggested_action": None,
            "gap_threshold_mode": "causal_daily_ewma_tick_count",
            "gap_threshold_uses_future_data": False,
            "gap_threshold_reconstruction": "not_needed_no_material_gap",
        }

    # Bound the duration of the interval being healed, not its age relative to
    # wall-clock time. A short, already-established research-to-live bridge
    # may be old without being a large recovery request. Anchoring this limit
    # to utc_now_ts() made such intervals expire merely because another day
    # passed. For latest_live_to_now, end is approximately now, so the normal
    # outage lookback protection is unchanged.
    max_start = end - pd.Timedelta(hours=GAP_MAX_LOOKBACK_HOURS)
    actual_start = max(start, max_start)
    if reset_partial:
        reset_current_bar(asset, state)

    ticks = fetch_ticks_range_chunked(mt5, asset, symbol, actual_start, end, state, logger)
    hit_cap = bool(ticks.attrs.get("hit_tick_cap", False)) if ticks is not None else False
    history_limited = bool(actual_start > start)
    fully_healed = bool(not history_limited and not hit_cap)

    gap_threshold_cache = make_gap_threshold_cache(asset, policy, state)
    direct_gap_counts = daily_tick_counts_from_ticks_df(ticks)
    if direct_gap_counts:
        counts = gap_threshold_cache.setdefault("daily_tick_counts", {})
        for day, count in direct_gap_counts.items():
            counts[str(day)] = int(count)
        gap_threshold_cache["raw_tick_counts_preloaded"] = True
        gap_threshold_cache["raw_tick_counts_preloaded_days"] = sorted(direct_gap_counts)
    closed = process_ticks_to_bars(
        asset,
        symbol,
        policy,
        ticks,
        state,
        logger,
        gap_healed=True,
        executable=False,
        gap_threshold_cache=gap_threshold_cache,
    )
    record_gap_healing_last_tick(symbol, ticks, state, label=label)
    threshold_summary = serializable_gap_threshold_summary(gap_threshold_cache)

    interval_threshold_audit = {
        "asset": asset,
        "symbol": symbol,
        "interval": label,
        "created_at": utc_now(),
        "requested_start": safe_iso(start),
        "actual_start": safe_iso(actual_start),
        "end": safe_iso(end),
        **threshold_summary,
    }
    state.setdefault("gap_threshold_reconstruction_history", []).append(interval_threshold_audit)
    state["gap_threshold_reconstruction_history"] = state["gap_threshold_reconstruction_history"][-200:]

    if hit_cap:
        why_partial = "tick_cap"
        suggested_action = "increase LIVE_DASHBOARD_MAX_TICKS_PER_GAP_ASSET or pre-seed live bars from offline tick rebuild"
    elif history_limited:
        why_partial = "history_limit"
        suggested_action = "increase LIVE_DASHBOARD_GAP_MAX_LOOKBACK_HOURS or rebuild bridge bars offline from broker/tick archive"
    elif ticks is None or ticks.empty:
        why_partial = "broker_returned_no_ticks"
        suggested_action = "confirm broker tick history availability or rebuild bridge bars from offline tick archive"
    else:
        why_partial = None
        suggested_action = None

    reason = "gap_healed_memory_only" if fully_healed else f"partial_gap_healed_{why_partial or 'unknown'}"
    logger.info(
        "GAP_HEALING_INTERVAL_SUMMARY asset=%s symbol=%s interval=%s status=%s requested_start=%s actual_start=%s end=%s ticks=%s bars=%s fully_healed=%s why_partial=%s adaptive_days=%s fallback_days=%s threshold_first=%s threshold_last=%s",
        asset,
        symbol,
        label,
        "pass" if fully_healed else ("fail" if FAIL_IF_GAP_NOT_FULLY_HEALED else "warn"),
        safe_iso(start),
        safe_iso(actual_start),
        safe_iso(end),
        int(len(ticks)) if ticks is not None else 0,
        int(closed),
        fully_healed,
        why_partial,
        threshold_summary.get("adaptive_threshold_days_used"),
        threshold_summary.get("fallback_threshold_days_used"),
        threshold_summary.get("threshold_date_first"),
        threshold_summary.get("threshold_date_last"),
    )

    return {
        "interval": label,
        "status": "pass" if fully_healed else ("fail" if FAIL_IF_GAP_NOT_FULLY_HEALED else "warn"),
        "reason": reason,
        "requested_start": safe_iso(start),
        "actual_start": safe_iso(actual_start),
        "end": safe_iso(end),
        "requested_gap_seconds": requested_gap_seconds,
        "requested_gap_hours": requested_gap_seconds / 3600.0,
        "missing_interval_start": safe_iso(start),
        "missing_interval_end": safe_iso(end),
        "ticks_fetched": int(len(ticks)) if ticks is not None else 0,
        "bridge_bars_created": int(closed),
        "fully_healed": fully_healed,
        "hit_tick_cap": hit_cap,
        "history_limited": history_limited,
        "why_partial": why_partial,
        "suggested_action": suggested_action,
        "offset_seconds": ticks.attrs.get("offset_seconds") if ticks is not None else get_stored_broker_offset_seconds(symbol, state),
        "last_tick_timestamp_utc": ticks.attrs.get("last_tick_timestamp_utc") if ticks is not None else None,
        **threshold_summary,
    }

def advance_edge_cursors_to_latest_live_bar(asset: str, manifest: dict[str, Any], state: dict[str, Any], reason: str, logger) -> str | None:
    latest = latest_live_bar_timestamp(asset)
    if latest is None:
        return None
    latest_iso = latest.isoformat()
    advanced: list[str] = []
    for edge in selected_edges_by_asset(manifest.get("selected_edges", [])).get(asset, []):
        edge_id = str(edge.get("edge_id"))
        state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = latest_iso
        advanced.append(edge_id)
    rec = {"asset": asset, "latest_bar": latest_iso, "edges": advanced, "reason": reason, "created_at": utc_now()}
    state.setdefault("last_cursor_advance", []).append(rec)
    state["last_cursor_advance"] = state["last_cursor_advance"][-100:]
    logger.info("EDGE_CURSOR_ADVANCE asset=%s latest_bar=%s edges=%d reason=%s", asset, latest_iso, len(advanced), reason)
    return latest_iso


def initialize_edge_cursors_on_new_state(manifest: dict[str, Any], state: dict[str, Any], logger) -> None:
    """V1 protection: when state is new/reset, never treat old live bars as new signals."""
    if state.get("startup_cursor_initialized_at"):
        return
    for asset in selected_edges_by_asset(manifest.get("selected_edges", [])).keys():
        if latest_live_bar_timestamp(asset) is not None:
            advance_edge_cursors_to_latest_live_bar(asset, manifest, state, "new_state_skip_existing_live_bars", logger)
    state["startup_cursor_initialized_at"] = utc_now()


def verify_asset_continuity(asset: str, policy: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    seed = load_seed_bars_for_asset(asset, policy)
    live = load_live_bars(asset)
    seed_latest = ensure_utc_timestamp(seed["Datetime"].max()) if not seed.empty else None
    if live.empty:
        return {
            "asset": asset,
            "status": "PASS_NO_LIVE_BARS_YET",
            "seed_latest": safe_iso(seed_latest),
            "first_live": None,
            "latest_live": None,
            "gap_seconds_seed_to_first_live": None,
            "gap_hours_seed_to_first_live": None,
            "missing_interval_start": None,
            "missing_interval_end": None,
            "reason": "no_live_bars_yet",
            "suggested_action": None,
        }

    first_live = ensure_utc_timestamp(live["Datetime"].min())
    latest_live = ensure_utc_timestamp(live["Datetime"].max())
    if seed_latest is None:
        return {
            "asset": asset,
            "status": "WARN_NO_RESEARCH_SEED",
            "seed_latest": None,
            "first_live": safe_iso(first_live),
            "latest_live": safe_iso(latest_live),
            "gap_seconds_seed_to_first_live": None,
            "gap_hours_seed_to_first_live": None,
            "missing_interval_start": None,
            "missing_interval_end": None,
            "reason": "cannot_verify_research_to_live_continuity_without_seed",
            "suggested_action": "confirm bar_manifest.json contains the research seed bars path",
        }

    asset_rows = [r for r in state.get("last_gap_healing_report", []) if str(r.get("asset")) == asset]
    failed = [r for r in asset_rows if str(r.get("status")).lower() == "fail"]
    partial = [r for r in asset_rows if r.get("fully_healed") is False]
    gap_seconds = float((first_live - seed_latest).total_seconds())
    gap_hours = gap_seconds / 3600.0
    missing_start = safe_iso(seed_latest + pd.Timedelta(milliseconds=1)) if gap_seconds > MIN_GAP_SECONDS_TO_HEAL else None
    missing_end = safe_iso(first_live - pd.Timedelta(milliseconds=1)) if gap_seconds > MIN_GAP_SECONDS_TO_HEAL else None

    if failed:
        return {
            "asset": asset,
            "status": "FAIL_GAP_HEALING_FAILED",
            "seed_latest": safe_iso(seed_latest),
            "first_live": safe_iso(first_live),
            "latest_live": safe_iso(latest_live),
            "gap_seconds_seed_to_first_live": gap_seconds,
            "gap_hours_seed_to_first_live": gap_hours,
            "missing_interval_start": missing_start,
            "missing_interval_end": missing_end,
            "reason": ";".join(str(r.get("reason")) for r in failed[:3]),
            "why_partial": ";".join(str(r.get("why_partial")) for r in failed[:3]),
            "suggested_action": "; ".join(str(r.get("suggested_action")) for r in failed[:3] if r.get("suggested_action")),
            "gap_report_rows": len(asset_rows),
        }
    if partial:
        return {
            "asset": asset,
            "status": "WARN_GAP_HEALING_PARTIAL",
            "seed_latest": safe_iso(seed_latest),
            "first_live": safe_iso(first_live),
            "latest_live": safe_iso(latest_live),
            "gap_seconds_seed_to_first_live": gap_seconds,
            "gap_hours_seed_to_first_live": gap_hours,
            "missing_interval_start": missing_start,
            "missing_interval_end": missing_end,
            "reason": ";".join(str(r.get("reason")) for r in partial[:3]),
            "why_partial": ";".join(str(r.get("why_partial")) for r in partial[:3]),
            "suggested_action": "; ".join(str(r.get("suggested_action")) for r in partial[:3] if r.get("suggested_action")),
            "gap_report_rows": len(asset_rows),
        }

    if not asset_rows:
        if gap_seconds <= MIN_GAP_SECONDS_TO_HEAL:
            status = "PASS_CONTINUOUS"
            reason = "first_live_bar_continues_research_seed"
        else:
            status = "WARN_RESEARCH_TO_LIVE_GAP_REMAINS"
            reason = "no_gap_healing_report_available_for_seed_to_live_gap"
    else:
        status = "PASS_HEALED_CONTINUOUS"
        reason = "gap_healing_report_fully_healed_or_previously_continuous"

    return {
        "asset": asset,
        "status": status,
        "seed_latest": safe_iso(seed_latest),
        "first_live": safe_iso(first_live),
        "latest_live": safe_iso(latest_live),
        "gap_seconds_seed_to_first_live": gap_seconds,
        "gap_hours_seed_to_first_live": gap_hours,
        "missing_interval_start": missing_start,
        "missing_interval_end": missing_end,
        "reason": reason,
        "why_partial": None,
        "suggested_action": None,
        "gap_report_rows": len(asset_rows),
    }

def enforce_continuity_or_fail(assets: list[str], state: dict[str, Any]) -> None:
    if not FAIL_IF_GAP_NOT_FULLY_HEALED:
        return
    bad: list[str] = []
    for asset in assets:
        rec = (state.get("asset_continuity") or {}).get(asset) or {}
        if str(rec.get("status")) not in PASS_CONTINUITY_STATUSES:
            bad.append(f"{asset}: {rec.get('status')} ({rec.get('reason')})")
    if bad:
        raise RuntimeError("Startup research/live continuity failed closed before scoring.\n" + "\n".join(f"- {x}" for x in bad))


def run_startup_gap_healing(mt5: Any, manifest: dict[str, Any], bar_policy: dict[str, Any], state: dict[str, Any], logger) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not GAP_HEALING_ENABLED:
        state["last_gap_healing_report"] = [{"status": "skipped", "reason": "gap_healing_disabled", "fully_healed": False}]
        return state["last_gap_healing_report"]

    policies = bar_policy.get("bar_policies", {})
    now_ts = utc_now_ts()
    for asset in selected_edges_by_asset(manifest.get("selected_edges", [])).keys():
        policy = policies.get(asset)
        if not policy:
            continue
        symbol = broker_symbol_for_asset(asset)
        try:
            if not is_broker_offset_locked(symbol, state):
                lock_broker_time_offset(symbol, state, logger, source="gap_healing_startup_lock")

            seed = load_seed_bars_for_asset(asset, policy)
            live = load_live_bars(asset)
            seed_latest = ensure_utc_timestamp(seed["Datetime"].max()) if not seed.empty else None
            first_live = ensure_utc_timestamp(live["Datetime"].min()) if not live.empty else None
            latest_live = ensure_utc_timestamp(live["Datetime"].max()) if not live.empty else None

            if seed_latest is None and latest_live is None:
                rows.append({"asset": asset, "symbol": symbol, "status": "warn", "reason": "no_seed_or_live_bars_available", "fully_healed": False})
                continue

            asset_interval_rows: list[dict[str, Any]] = []
            if seed_latest is not None and first_live is not None and float((first_live - seed_latest).total_seconds()) > MIN_GAP_SECONDS_TO_HEAL:
                asset_interval_rows.append({"asset": asset, "symbol": symbol, **heal_interval(mt5, asset, symbol, policy, seed_latest + pd.Timedelta(milliseconds=1), first_live - pd.Timedelta(milliseconds=1), state, logger, "research_seed_to_first_live", reset_partial=True)})

            latest_after_bridge = latest_live_bar_timestamp(asset) or seed_latest
            if latest_after_bridge is not None:
                asset_interval_rows.append({"asset": asset, "symbol": symbol, **heal_interval(mt5, asset, symbol, policy, latest_after_bridge + pd.Timedelta(milliseconds=1), now_ts, state, logger, "latest_live_to_now")})

            rows.extend(asset_interval_rows)
            advance_edge_cursors_to_latest_live_bar(asset, manifest, state, "gap_bootstrap_memory_only", logger)
            state["last_gap_healing_report"] = rows

            continuity = verify_asset_continuity(asset, policy, state)
            state.setdefault("asset_continuity", {})[asset] = continuity
            rows.append({"asset": asset, "symbol": symbol, "interval": "continuity_verification", **continuity})

            total_ticks = sum(int(r.get("ticks_fetched", 0) or 0) for r in asset_interval_rows)
            total_bars = sum(int(r.get("bridge_bars_created", 0) or 0) for r in asset_interval_rows)
            fully_healed = all(bool(r.get("fully_healed")) for r in asset_interval_rows) if asset_interval_rows else True
            summary = {
                "asset": asset,
                "symbol": symbol,
                "interval_count": len(asset_interval_rows),
                "ticks_fetched": total_ticks,
                "bridge_bars_created": total_bars,
                "fully_healed": fully_healed,
                "continuity_status": continuity.get("status"),
                "continuity_reason": continuity.get("reason"),
                "suggested_action": continuity.get("suggested_action"),
                "offset_seconds": get_stored_broker_offset_seconds(symbol, state),
                "offset_locked": is_broker_offset_locked(symbol, state),
            }
            state.setdefault("last_gap_healing_summary_by_asset", {})[asset] = summary
            logger.info("GAP_HEALING_ASSET_SUMMARY asset=%s symbol=%s ticks=%s bars=%s fully_healed=%s continuity=%s offset=%s locked=%s", asset, symbol, total_ticks, total_bars, fully_healed, continuity.get("status"), summary["offset_seconds"], summary["offset_locked"])
            logger.info("CONTINUITY asset=%s status=%s reason=%s seed_latest=%s first_live=%s latest_live=%s gap_hours=%s suggested_action=%s", asset, continuity.get("status"), continuity.get("reason"), continuity.get("seed_latest"), continuity.get("first_live"), continuity.get("latest_live"), continuity.get("gap_hours_seed_to_first_live"), continuity.get("suggested_action"))
        except Exception as exc:
            logger.warning("GAP_HEAL_WARN asset=%s symbol=%s error=%s", asset, symbol, exc)
            continuity = {"asset": asset, "status": "FAIL_GAP_HEAL_EXCEPTION" if FAIL_IF_GAP_NOT_FULLY_HEALED else "WARN_GAP_HEAL_EXCEPTION", "reason": f"{type(exc).__name__}:{exc}", "suggested_action": "fix gap-healing exception before runtime scoring"}
            state.setdefault("asset_continuity", {})[asset] = continuity
            rows.append({"asset": asset, "symbol": symbol, "status": "fail" if FAIL_IF_GAP_NOT_FULLY_HEALED else "warn", "reason": f"gap_heal_exception:{type(exc).__name__}:{exc}", "fully_healed": False, "suggested_action": continuity["suggested_action"]})

    state["last_gap_healing_report"] = rows
    write_json(GAP_REPORT_JSON, {"schema_version": SCHEMA_VERSION, "created_at": utc_now(), "rows": rows, "summaries": state.get("last_gap_healing_summary_by_asset", {}), "fail_if_gap_not_fully_healed": FAIL_IF_GAP_NOT_FULLY_HEALED})
    if rows:
        write_csv_append(GAP_REPORT_CSV, rows)
    return rows

def get_bar_context(asset: str, event_ts: pd.Timestamp) -> dict[str, Any]:
    live = load_live_bars(asset)
    if live.empty:
        return {}
    live["Datetime"] = parse_utc_datetime_series(live["Datetime"])
    match = live[live["Datetime"] == ensure_utc_timestamp(event_ts)]
    if match.empty:
        return {}
    row = match.iloc[-1]
    return {
        "bar_start": safe_iso(row.get("Bar_Start")),
        "broker_event_time": safe_iso(row.get("Broker_Datetime")),
        "broker_bar_start": safe_iso(row.get("Broker_Bar_Start")),
        "bar_open": safe_float(row.get("Open")),
        "bar_high": safe_float(row.get("High")),
        "bar_low": safe_float(row.get("Low")),
        "bar_close": safe_float(row.get("Close")),
        "bar_ticks": safe_int(row.get("Ticks")),
        "bar_threshold": safe_int(row.get("Threshold")),
        "bar_threshold_unit": str(row.get("Threshold_Unit") or "ticks"),
        "bar_research_bar_mode": str(row.get("Research_Bar_Mode") or "standard_quote_tick"),
        "bar_threshold_mode": str(row.get("Threshold_Mode") or "causal_daily_ewma_tick_count"),
        "bar_gap_healed": safe_bool(row.get("Gap_Healed"), False),
        "bar_executable": safe_bool(row.get("Executable"), False),
    }

def live_execution_allowed(manifest: dict[str, Any], bar_policy: dict[str, Any]) -> tuple[bool, str]:
    blockers: list[str] = []
    if manifest.get("live_trade_enabled") is not True:
        blockers.append("live_manifest.live_trade_enabled is false")
    if manifest.get("risk_contract_version") != RISK_CONTRACT_VERSION:
        blockers.append("live_manifest.risk_contract_version mismatch")
    if str(manifest.get("approved_account_stage") or "").lower() != str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "")).lower():
        blockers.append("local/manifest account stage mismatch")
    if manifest.get("daily_symbol_risk_rule_enabled") is not False:
        blockers.append("challenge manifest unexpectedly enables funded symbol-risk rule")
    if bar_policy.get("live_policy_ready") is not True:
        blockers.append("live_bar_policy.live_policy_ready is false")
    if bar_policy.get("order_execution_allowed_by_policy") is not True:
        blockers.append("live_bar_policy.order_execution_allowed_by_policy is false")
    if bar_policy.get("source_live_manifest_sha256") != sha256_file(settings.LIVE_MANIFEST_PATH):
        blockers.append("live_bar_policy source manifest hash mismatch")
    return (False, "; ".join(blockers)) if blockers else (True, "")


def validate_runtime_score_output(edge_id: str, scored: pd.DataFrame) -> None:
    missing_cols = [col for col in SCORE_COLUMNS if col not in scored.columns]
    if missing_cols:
        raise RuntimeError(f"{edge_id}: runtime.score output missing required columns: {missing_cols}")
    forbidden = [c for c in scored.columns if str(c).strip().lower() in FORBIDDEN_RUNTIME_OUTPUT_COLUMNS]
    if forbidden:
        raise RuntimeError(f"{edge_id}: runtime.score output contains forbidden label/future columns: {forbidden}")


def runtime_score_rows_path(edge: dict[str, Any]) -> Path:
    safe_edge = str(edge.get("edge_id")).replace("/", "__").replace("\\", "__")
    return RUNTIME_SCORE_ROWS_DIR / str(edge.get("asset")) / f"{safe_edge}_runtime_score_rows.csv"


def export_runtime_score_rows(edge: dict[str, Any], scored: pd.DataFrame, live_hash: str, policy_hash: str, runtime_manifest: dict[str, Any]) -> Path | None:
    if scored is None or scored.empty:
        return None
    out = scored.copy()
    out["edge_id"] = str(edge.get("edge_id"))
    out["asset"] = str(edge.get("asset"))
    out["family"] = str(edge.get("family", ""))
    out["side_name"] = str(edge.get("side_name", ""))
    out["side_value"] = int(edge.get("side_value"))
    out["final_live_threshold"] = float(edge.get("final_live_threshold"))
    out["threshold_probability_column"] = "calibrated_probability"
    out["portfolio_weight"] = float(edge.get("portfolio_weight", 0.0))
    out["live_manifest_hash"] = live_hash
    out["edge_manifest_hash"] = str(edge.get("edge_manifest_hash", ""))
    out["bar_policy_hash"] = policy_hash
    out["runtime_manifest_json"] = json.dumps(runtime_manifest, default=str, sort_keys=True)
    out["exported_at"] = utc_now()
    path = runtime_score_rows_path(edge)
    enqueue_noncritical_csv_append(
        path=path,
        rows=out.to_dict("records"),
        dedup_cols=["Datetime", "edge_id", "final_live_threshold"],
        label=f"runtime_score_rows:{edge.get('edge_id')}",
    )
    return path


def nested_get(obj: Any, *paths: str, default: Any = None) -> Any:
    """Read a value from nested dict/list structures using dotted paths."""
    for path in paths:
        cur = obj
        ok = True
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def load_edge_manifest_for_signal(edge: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Path] = []
    for key in ["edge_manifest_path", "manifest_path", "edge_manifest"]:
        value = edge.get(key)
        if value:
            path = Path(str(value))
            candidates.append(path if path.is_absolute() else V2_ROOT / path)
    edge_id = str(edge.get("edge_id", ""))
    if "/" in edge_id:
        asset, edge_name = edge_id.split("/", 1)
        candidates.append(V2_ROOT / "assets" / asset / "edges" / edge_name / "artifacts" / "edge_manifest.json")
    for path in candidates:
        try:
            if path.exists():
                return read_json(path)
        except Exception:
            continue
    return {}


def first_numeric(row: Any, names: list[str]) -> tuple[float | None, str | None]:
    for name in names:
        try:
            if isinstance(row, pd.Series):
                value = row.get(name)
            elif isinstance(row, dict):
                value = row.get(name)
            else:
                value = None
            out = safe_float(value)
            if out is not None and out > 0:
                return out, name
        except Exception:
            continue
    return None, None


def first_numeric_nested(*objects: dict[str, Any], names: list[str]) -> tuple[float | None, str | None]:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for name in names:
            value = nested_get(obj, name)
            out = safe_float(value)
            if out is not None and out > 0:
                return out, name
    return None, None


def first_int_nested(*objects: dict[str, Any], names: list[str]) -> tuple[int | None, str | None]:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for name in names:
            value = nested_get(obj, name)
            out = safe_int(value)
            if out is not None and out > 0:
                return out, name
    return None, None


def infer_barrier_units(value: float, edge_manifest: dict[str, Any], runtime_manifest: dict[str, Any]) -> str:
    explicit = nested_get(
        edge_manifest,
        "barrier_reference_units",
        "labeling.barrier_reference_units",
        "labeling.target_unit",
        "model_rows.barrier_reference_units",
        default=None,
    ) or nested_get(
        runtime_manifest,
        "barrier_reference_units",
        "labeling.barrier_reference_units",
        "target_unit",
        default=None,
    )
    if explicit:
        text = str(explicit).strip().lower()
        if text in {"return", "returns", "return_fraction", "fraction", "pct", "percent", "percentage"}:
            return "return_fraction"
        if text in {"price", "price_distance", "points", "ticks"}:
            return "price_distance"
    # In MLDP triple-barrier pipelines the target is normally a return/vol
    # fraction. Values larger than 0.5 are treated as price-distance units.
    return "return_fraction" if value <= 0.5 else "price_distance"


def build_dynamic_execution_plan(
    *,
    edge: dict[str, Any],
    scored_row: pd.Series,
    runtime_manifest: dict[str, Any],
    bar_context: dict[str, Any],
    signal_price: float | None,
) -> dict[str, Any]:
    """Materialize one edge's dynamic triple-barrier execution plan.

    The dynamic event barrier value comes from the runtime score output. The
    PT/SL multipliers, reward/risk ratio, geometry hash and vertical horizon come
    only from the allocator-verified selected edge in live_manifest.json. Runtime
    or edge-manifest geometry is used only as an integrity cross-check. Missing
    or inconsistent geometry fails closed; no 1R fallback is permitted.
    """
    edge_manifest = load_edge_manifest_for_signal(edge)
    side = int(edge.get("side_value", 0) or 0)
    source_parts: list[str] = ["dashboard_dynamic_event_barrier_plus_live_manifest_edge_geometry"]
    blocker: list[str] = []

    edge_geometry = edge.get("trade_geometry") if isinstance(edge.get("trade_geometry"), dict) else edge
    pt_multiplier = safe_float(edge_geometry.get("pt_multiplier"), None)
    sl_multiplier = safe_float(edge_geometry.get("sl_multiplier"), None)
    vertical_horizon_bars = safe_int(edge_geometry.get("vertical_horizon_bars"), None)
    geometry_hash = str(edge_geometry.get("geometry_hash") or edge.get("geometry_hash") or "").strip()
    reward_risk_ratio = safe_float(edge_geometry.get("reward_risk_ratio"), None)
    declared_barrier_reference = str(
        edge_geometry.get("barrier_reference")
        or edge.get("barrier_reference")
        or nested_get(edge_manifest, "barrier_reference", "trade_geometry.barrier_reference", "labeling.barrier_reference", default="")
        or ""
    ).strip()
    units_explicit = str(
        edge_geometry.get("barrier_reference_units")
        or edge.get("barrier_reference_units")
        or nested_get(edge_manifest, "barrier_reference_units", "trade_geometry.barrier_reference_units", "labeling.barrier_reference_units", default="")
        or ""
    ).strip().lower()

    barrier_value: float | None = None
    barrier_source: str | None = None
    if STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED:
        if not declared_barrier_reference:
            blocker.append("missing_declared_live_manifest_barrier_reference")
        else:
            barrier_value, barrier_source = first_numeric(scored_row, [declared_barrier_reference])
            if barrier_value is None:
                blocker.append(
                    f"declared_barrier_reference_missing_from_runtime_score:{declared_barrier_reference}"
                )
    else:
        barrier_value, barrier_source = first_numeric(
            scored_row,
            [
                "Event_Vol", "event_vol", "barrier_value", "event_target",
                "event_trgt", "trgt", "target_volatility", "volatility_target",
                "event_volatility", "daily_vol", "barrier_target", "target",
                "Bar_Volatility", "bar_volatility", "volatility",
            ],
        )
        if barrier_value is None:
            barrier_value, nested_source = first_numeric_nested(
                runtime_manifest,
                names=[
                    "Event_Vol", "event_vol", "candidate_row_manifest.Event_Vol",
                    "barrier_value", "labeling.barrier_value", "labeling.target",
                    "labeling.trgt", "model_rows.target", "model_rows.trgt",
                    "training.labeling.target",
                ],
            )
            barrier_source = nested_source

    if pt_multiplier is None or pt_multiplier <= 0:
        blocker.append("missing_positive_live_manifest_pt_multiplier")
    if sl_multiplier is None or sl_multiplier <= 0:
        blocker.append("missing_positive_live_manifest_sl_multiplier")
    if vertical_horizon_bars is None or vertical_horizon_bars <= 0:
        blocker.append("missing_positive_live_manifest_vertical_horizon_bars")
    if not geometry_hash:
        blocker.append("missing_live_manifest_geometry_hash")
    if pt_multiplier is not None and sl_multiplier is not None and sl_multiplier > 0:
        calculated_rr = float(pt_multiplier) / float(sl_multiplier)
        if reward_risk_ratio is None:
            reward_risk_ratio = calculated_rr
        elif not math.isclose(float(reward_risk_ratio), calculated_rr, rel_tol=0.0, abs_tol=1e-10):
            blocker.append("live_manifest_reward_risk_ratio_mismatch")

    # Runtime score rows may repeat geometry for audit, but may never override it.
    row_pt, _ = first_numeric(scored_row, ["pt_multiplier", "pt_mult", "profit_taking_multiplier", "profit_taking_multiple"])
    row_sl, _ = first_numeric(scored_row, ["sl_multiplier", "sl_mult", "stop_loss_multiplier", "stop_loss_multiple"])
    if row_pt is not None and pt_multiplier is not None and not math.isclose(row_pt, pt_multiplier, rel_tol=0.0, abs_tol=1e-10):
        blocker.append("runtime_pt_multiplier_differs_from_live_manifest")
    if row_sl is not None and sl_multiplier is not None and not math.isclose(row_sl, sl_multiplier, rel_tol=0.0, abs_tol=1e-10):
        blocker.append("runtime_sl_multiplier_differs_from_live_manifest")

    # Cross-check the current edge manifest when it exposes geometry fields.
    manifest_pt, _ = first_numeric_nested(
        edge_manifest,
        names=["pt_multiplier", "labeling.pt_multiplier", "labeling.pt_mult", "trade_geometry.pt_multiplier"],
    )
    manifest_sl, _ = first_numeric_nested(
        edge_manifest,
        names=["sl_multiplier", "labeling.sl_multiplier", "labeling.sl_mult", "trade_geometry.sl_multiplier"],
    )
    manifest_vh, _ = first_int_nested(
        edge_manifest,
        names=["vertical_horizon_bars", "labeling.vertical_horizon_bars", "trade_geometry.vertical_horizon_bars"],
    )
    manifest_geometry_hash = str(
        nested_get(edge_manifest, "geometry_hash", "trade_geometry.geometry_hash", "labeling.geometry_hash", default="") or ""
    ).strip()
    if manifest_pt is not None and pt_multiplier is not None and not math.isclose(manifest_pt, pt_multiplier, rel_tol=0.0, abs_tol=1e-10):
        blocker.append("edge_manifest_pt_multiplier_mismatch")
    if manifest_sl is not None and sl_multiplier is not None and not math.isclose(manifest_sl, sl_multiplier, rel_tol=0.0, abs_tol=1e-10):
        blocker.append("edge_manifest_sl_multiplier_mismatch")
    if manifest_vh is not None and vertical_horizon_bars is not None and int(manifest_vh) != int(vertical_horizon_bars):
        blocker.append("edge_manifest_vertical_horizon_mismatch")
    if manifest_geometry_hash and geometry_hash and manifest_geometry_hash != geometry_hash:
        blocker.append("edge_manifest_geometry_hash_mismatch")

    if signal_price is None or signal_price <= 0:
        blocker.append("missing_positive_signal_price")
    if side not in {-1, 1}:
        blocker.append("invalid_side_value")
    if barrier_value is None or barrier_value <= 0:
        blocker.append("missing_dynamic_barrier_value_from_runtime")

    if units_explicit in {"return", "returns", "return_fraction", "fraction", "pct", "percent", "percentage"}:
        units = "return_fraction"
    elif units_explicit in {"price", "price_distance", "points", "ticks"}:
        units = "price_distance"
    elif STRICT_BARRIER_REFERENCE_CONTRACT_ENABLED:
        units = None
        blocker.append("missing_or_unsupported_explicit_barrier_reference_units")
    else:
        units = infer_barrier_units(float(barrier_value), edge_manifest, runtime_manifest) if barrier_value is not None else None

    formula = None
    sl_distance = None
    tp_distance = None
    planned_sl = None
    planned_tp = None
    if not blocker and signal_price is not None and barrier_value is not None and pt_multiplier is not None and sl_multiplier is not None:
        if units == "return_fraction":
            base_distance = abs(float(signal_price) * float(barrier_value))
            formula = "price_distance = signal_price * dynamic_event_target * edge_multiplier"
        else:
            base_distance = abs(float(barrier_value))
            formula = "price_distance = dynamic_event_price_distance * edge_multiplier"
        sl_distance = base_distance * float(sl_multiplier)
        tp_distance = base_distance * float(pt_multiplier)
        if side > 0:
            planned_sl = float(signal_price) - sl_distance
            planned_tp = float(signal_price) + tp_distance
        else:
            planned_sl = float(signal_price) + sl_distance
            planned_tp = float(signal_price) - tp_distance
        if planned_sl <= 0 or planned_tp <= 0:
            blocker.append("computed_non_positive_sl_or_tp")
        if sl_distance <= 0 or tp_distance <= 0:
            blocker.append("computed_zero_distance_sl_or_tp")

    if barrier_source:
        source_parts.append(f"barrier={barrier_source}")
    source_parts.append("geometry=live_manifest_selected_edge")

    ready = len(blocker) == 0
    plan = {
        "execution_plan_version": EXECUTION_PLAN_VERSION,
        "execution_plan_ready": ready,
        "execution_plan_blocker": "; ".join(blocker) if blocker else "",
        "entry_timing": "immediate_on_closed_executable_standard_quote_tick_bar",
        "entry_price": signal_price,
        "signal_price": signal_price,
        "signal_price_source": "closed_standard_quote_tick_bar_close" if signal_price is not None else None,
        "side_value": side,
        "barrier_reference": declared_barrier_reference or "mldp_dynamic_Event_Vol_triple_barrier_target",
        "barrier_value": barrier_value,
        "barrier_value_source": barrier_source,
        "barrier_reference_units": units,
        "barrier_price_distance_formula": formula,
        "pt_multiplier": float(pt_multiplier) if pt_multiplier is not None else None,
        "sl_multiplier": float(sl_multiplier) if sl_multiplier is not None else None,
        "reward_risk_ratio": float(reward_risk_ratio) if reward_risk_ratio is not None else None,
        "vertical_horizon_bars": vertical_horizon_bars,
        "geometry_hash": geometry_hash or None,
        "planned_sl_distance_price": sl_distance,
        "planned_tp_distance_price": tp_distance,
        "planned_stop_loss": planned_sl,
        "planned_take_profit": planned_tp,
        "edge_id": edge.get("edge_id"),
        "asset": edge.get("asset"),
        "mt5_symbol": broker_symbol_for_asset(str(edge.get("asset"))),
        "edge_manifest_hash": edge.get("edge_manifest_hash", ""),
        "source": " + ".join(source_parts),
        "audit_bar": {
            "bar_open": bar_context.get("bar_open"),
            "bar_high": bar_context.get("bar_high"),
            "bar_low": bar_context.get("bar_low"),
            "bar_close": bar_context.get("bar_close"),
            "bar_ticks": bar_context.get("bar_ticks"),
            "bar_threshold": bar_context.get("bar_threshold"),
        },
    }
    compact = json.dumps(plan, sort_keys=True, default=str, allow_nan=True)
    plan["execution_plan_hash"] = hashlib.sha256(compact.encode("utf-8")).hexdigest()
    plan["execution_plan_json"] = compact
    return plan



def note_memory_only_blocked(asset: str, scored_candidates: pd.DataFrame, state: dict[str, Any]) -> int:
    if scored_candidates is None or scored_candidates.empty:
        return 0
    live = load_live_bars(asset)
    if live.empty:
        return 0
    live["Datetime"] = parse_utc_datetime_series(live["Datetime"])
    memory = live[(live["Gap_Healed"].apply(lambda x: safe_bool(x, False))) | (~live["Executable"].apply(lambda x: safe_bool(x, False)))].copy()
    if memory.empty:
        return 0
    memory_times = set(memory["Datetime"].dropna())
    count = int(scored_candidates["Datetime"].isin(memory_times).sum()) if "Datetime" in scored_candidates.columns else 0
    if count:
        state.setdefault("blocked_memory_only_bar_count_by_asset", {})[asset] = int(state.setdefault("blocked_memory_only_bar_count_by_asset", {}).get(asset, 0)) + count
        gap_count = int(memory[memory["Datetime"].isin(set(scored_candidates["Datetime"].dropna()))]["Gap_Healed"].apply(lambda x: safe_bool(x, False)).sum())
        state.setdefault("blocked_gap_healed_event_count_by_asset", {})[asset] = int(state.setdefault("blocked_gap_healed_event_count_by_asset", {}).get(asset, 0)) + gap_count
        latest = memory[memory["Datetime"].isin(set(scored_candidates["Datetime"].dropna()))]["Datetime"].max()
        if pd.notna(latest):
            state.setdefault("latest_memory_only_bar_time_by_asset", {})[asset] = ensure_utc_timestamp(latest).isoformat()
    return count


def clear_runtime_batch_caches() -> None:
    # Runtime modules stay cached for speed. Large seed-bar copies are reloadable.
    _SEED_BAR_CACHE.clear()


def _timestamp_alignment_summary(scored: pd.DataFrame | None, live_times: pd.Series) -> dict[str, Any]:
    """Return timestamp summary for one scored-row stage.

    Diagnostic-only helper. It never changes the scored rows or trading gate.
    """
    out: dict[str, Any] = {
        "row_count": int(0 if scored is None else len(scored)),
        "earliest_scored_timestamp": None,
        "latest_scored_timestamp": None,
        "nearest_scored_timestamp": None,
        "nearest_executable_live_timestamp": None,
        "nearest_scored_to_live_diff_seconds": None,
        "possible_timestamp_precision_mismatch": False,
    }

    if scored is None or scored.empty or "Datetime" not in scored.columns:
        return out

    scored_times = parse_utc_datetime_series(scored["Datetime"]).dropna().sort_values().reset_index(drop=True)
    if scored_times.empty:
        out["row_count"] = 0
        return out

    out["row_count"] = int(len(scored_times))
    out["earliest_scored_timestamp"] = safe_iso(scored_times.iloc[0])
    out["latest_scored_timestamp"] = safe_iso(scored_times.iloc[-1])

    if live_times.empty:
        return out

    # Efficient nearest-neighbor scan against sorted executable live timestamps.
    live_ns = live_times.astype("int64").to_numpy()
    best_diff_ns: int | None = None
    best_scored_ts: pd.Timestamp | None = None
    best_live_ts: pd.Timestamp | None = None

    for scored_ts in scored_times:
        scored_ns = int(pd.Timestamp(scored_ts).value)
        pos = int(live_ns.searchsorted(scored_ns))
        candidate_positions = []
        if pos < len(live_ns):
            candidate_positions.append(pos)
        if pos > 0:
            candidate_positions.append(pos - 1)

        for candidate_pos in candidate_positions:
            diff_ns = abs(scored_ns - int(live_ns[candidate_pos]))
            if best_diff_ns is None or diff_ns < best_diff_ns:
                best_diff_ns = int(diff_ns)
                best_scored_ts = ensure_utc_timestamp(scored_ts)
                best_live_ts = ensure_utc_timestamp(live_times.iloc[candidate_pos])

    if best_diff_ns is not None:
        diff_seconds = float(best_diff_ns / 1_000_000_000.0)
        out["nearest_scored_timestamp"] = safe_iso(best_scored_ts)
        out["nearest_executable_live_timestamp"] = safe_iso(best_live_ts)
        out["nearest_scored_to_live_diff_seconds"] = diff_seconds
        out["possible_timestamp_precision_mismatch"] = bool(0.0 < diff_seconds <= 5.0)

    return out


def runtime_timestamp_alignment_diagnostics(
    asset: str,
    scored_after_cursor: pd.DataFrame,
    live_event_times: set[pd.Timestamp],
    memory_only_blocked_count: int,
    *,
    scored_before_cursor: pd.DataFrame | None = None,
    last_processed: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Summarize why scored rows did not intersect executable live bar timestamps.

    Diagnostic-only helper. It must not loosen or change the live-trading gate.

    Two stages are reported:
    - runtime-scored rows after Datetime normalization but before cursor filtering;
    - rows remaining after the edge last_processed cursor filter and before the
      executable-live timestamp gate.
    """
    try:
        live_times = pd.Series(list(live_event_times or []), dtype="datetime64[ns, UTC]").dropna().sort_values().reset_index(drop=True)
    except Exception:
        live_times = pd.Series(dtype="datetime64[ns, UTC]")

    before = _timestamp_alignment_summary(scored_before_cursor, live_times)
    after = _timestamp_alignment_summary(scored_after_cursor, live_times)

    runtime_count = int(before.get("row_count", 0))
    after_cursor_count = int(after.get("row_count", 0))

    diagnostics: dict[str, Any] = {
        "asset": asset,
        "runtime_scored_rows_normalized": runtime_count,
        "scored_rows_after_cursor_filter": after_cursor_count,
        "scored_rows_before_live_filter": after_cursor_count,
        "cursor_filter_removed_rows": max(runtime_count - after_cursor_count, 0),
        "cursor_filter_removed_all": bool(runtime_count > 0 and after_cursor_count == 0),
        "last_processed_timestamp": safe_iso(last_processed) if last_processed is not None else None,
        "executable_live_timestamp_count": int(len(live_times)),
        "memory_only_blocked_count": int(memory_only_blocked_count),
        "latest_executable_live_timestamp": safe_iso(live_times.iloc[-1]) if not live_times.empty else None,

        # After-cursor stage: this is the exact input to the executable-live gate.
        "earliest_scored_timestamp": after.get("earliest_scored_timestamp"),
        "latest_scored_timestamp": after.get("latest_scored_timestamp"),
        "nearest_scored_timestamp": after.get("nearest_scored_timestamp"),
        "nearest_executable_live_timestamp": after.get("nearest_executable_live_timestamp"),
        "nearest_scored_to_live_diff_seconds": after.get("nearest_scored_to_live_diff_seconds"),
        "possible_timestamp_precision_mismatch": bool(after.get("possible_timestamp_precision_mismatch")),

        # Before-cursor stage: useful when the cursor filter removes all scored rows.
        "earliest_runtime_scored_timestamp": before.get("earliest_scored_timestamp"),
        "latest_runtime_scored_timestamp": before.get("latest_scored_timestamp"),
        "nearest_runtime_scored_timestamp": before.get("nearest_scored_timestamp"),
        "nearest_runtime_executable_live_timestamp": before.get("nearest_executable_live_timestamp"),
        "nearest_runtime_to_live_diff_seconds": before.get("nearest_scored_to_live_diff_seconds"),
        "runtime_possible_timestamp_precision_mismatch": bool(before.get("possible_timestamp_precision_mismatch")),
    }

    return diagnostics


def runtime_stage_datetime_diagnostics(
    stage_name: str,
    frame: Any,
    live_event_times: set[pd.Timestamp],
    *,
    last_processed: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Summarize Datetime coverage for one runtime stage.

    Diagnostic-only helper. It does not alter features, events, scoring rows,
    cursor handling, or executable-live timestamp gates. It is used to answer:
    did this runtime stage actually produce rows near the fresh live bar, or are
    all rows still ending around the research/memory period?
    """
    row_count = 0
    has_datetime = False
    try:
        row_count = int(0 if frame is None else len(frame))
        has_datetime = bool(hasattr(frame, "columns") and "Datetime" in frame.columns)
    except Exception:
        row_count = 0
        has_datetime = False

    out: dict[str, Any] = {
        "stage": stage_name,
        "row_count": row_count,
        "has_datetime": has_datetime,
        "datetime_valid_count": 0,
        "earliest_timestamp": None,
        "latest_timestamp": None,
        "rows_after_last_processed": None,
        "rows_on_executable_live_timestamps": None,
        "latest_executable_live_timestamp": None,
        "nearest_stage_timestamp": None,
        "nearest_executable_live_timestamp": None,
        "nearest_stage_to_live_diff_seconds": None,
        "possible_timestamp_precision_mismatch": False,
        "last_processed_timestamp": safe_iso(last_processed) if last_processed is not None else None,
    }

    try:
        live_times = pd.Series(list(live_event_times or []), dtype="datetime64[ns, UTC]").dropna().sort_values().reset_index(drop=True)
    except Exception:
        live_times = pd.Series(dtype="datetime64[ns, UTC]")

    if not live_times.empty:
        out["latest_executable_live_timestamp"] = safe_iso(live_times.iloc[-1])

    if not has_datetime or frame is None:
        return out

    try:
        times = parse_utc_datetime_series(frame["Datetime"]).dropna().sort_values().reset_index(drop=True)
    except Exception:
        return out

    if times.empty:
        return out

    out["datetime_valid_count"] = int(len(times))
    out["earliest_timestamp"] = safe_iso(times.iloc[0])
    out["latest_timestamp"] = safe_iso(times.iloc[-1])

    if last_processed is not None:
        try:
            out["rows_after_last_processed"] = int((times > last_processed).sum())
        except Exception:
            out["rows_after_last_processed"] = None

    if not live_times.empty:
        try:
            live_set = set(live_times.tolist())
            out["rows_on_executable_live_timestamps"] = int(times.isin(live_set).sum())
        except Exception:
            out["rows_on_executable_live_timestamps"] = None

        live_ns = live_times.astype("int64").to_numpy()
        best_diff_ns: int | None = None
        best_stage_ts: pd.Timestamp | None = None
        best_live_ts: pd.Timestamp | None = None

        for stage_ts in times:
            stage_ns = int(pd.Timestamp(stage_ts).value)
            pos = int(live_ns.searchsorted(stage_ns))
            candidate_positions = []
            if pos < len(live_ns):
                candidate_positions.append(pos)
            if pos > 0:
                candidate_positions.append(pos - 1)

            for candidate_pos in candidate_positions:
                diff_ns = abs(stage_ns - int(live_ns[candidate_pos]))
                if best_diff_ns is None or diff_ns < best_diff_ns:
                    best_diff_ns = int(diff_ns)
                    best_stage_ts = ensure_utc_timestamp(stage_ts)
                    best_live_ts = ensure_utc_timestamp(live_times.iloc[candidate_pos])

        if best_diff_ns is not None:
            diff_seconds = float(best_diff_ns / 1_000_000_000.0)
            out["nearest_stage_timestamp"] = safe_iso(best_stage_ts)
            out["nearest_executable_live_timestamp"] = safe_iso(best_live_ts)
            out["nearest_stage_to_live_diff_seconds"] = diff_seconds
            out["possible_timestamp_precision_mismatch"] = bool(0.0 < diff_seconds <= 5.0)

    return out


def log_runtime_stage_datetime_diagnostics(
    logger,
    edge_id: str,
    stage_name: str,
    frame: Any,
    live_event_times: set[pd.Timestamp],
    *,
    last_processed: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Log Datetime coverage for a runtime stage without changing any logic."""
    diag = runtime_stage_datetime_diagnostics(
        stage_name,
        frame,
        live_event_times,
        last_processed=last_processed,
    )
    logger.info(
        "RUNTIME_STAGE_DIAGNOSTICS edge=%s stage=%s rows=%s has_datetime=%s datetime_valid=%s "
        "earliest=%s latest=%s rows_after_last_processed=%s rows_on_executable_live=%s "
        "latest_executable_live_timestamp=%s nearest_stage_timestamp=%s nearest_executable_live_timestamp=%s "
        "nearest_stage_to_live_diff_seconds=%s possible_timestamp_precision_mismatch=%s last_processed=%s",
        edge_id,
        diag.get("stage"),
        diag.get("row_count"),
        diag.get("has_datetime"),
        diag.get("datetime_valid_count"),
        diag.get("earliest_timestamp"),
        diag.get("latest_timestamp"),
        diag.get("rows_after_last_processed"),
        diag.get("rows_on_executable_live_timestamps"),
        diag.get("latest_executable_live_timestamp"),
        diag.get("nearest_stage_timestamp"),
        diag.get("nearest_executable_live_timestamp"),
        diag.get("nearest_stage_to_live_diff_seconds"),
        diag.get("possible_timestamp_precision_mismatch"),
        diag.get("last_processed_timestamp"),
    )
    return diag



def candidate_live_times_for_runtime(
    live_event_times: set[pd.Timestamp] | None,
    last_processed: pd.Timestamp | None,
) -> list[pd.Timestamp]:
    """Fresh executable live timestamps this edge is allowed to consider now.

    This is a scheduling/API helper only. The normal stale/timestamp/future gates
    still run after scoring. It gives optional runtime live-event APIs a narrow
    candidate-time list instead of asking them to scan historical events.
    """
    now = utc_now_ts()
    out: list[pd.Timestamp] = []
    for ts in live_timestamps_after_cursor(live_event_times, last_processed):
        try:
            age = float((now - ts).total_seconds())
        except Exception:
            continue
        if age <= float(MAX_SIGNAL_AGE_SECONDS):
            out.append(ts)
    return out[-MAX_EVENTS_TO_SCORE_PER_EDGE:]


def _runtime_continuation_fingerprint(runtime: Any) -> str:
    """Fingerprint the runtime module that owns continuation state."""
    h = hashlib.sha256()
    module_file = getattr(runtime, "__file__", None)
    if module_file:
        try:
            path = Path(str(module_file))
            if path.exists():
                h.update(path.read_bytes())
        except Exception:
            h.update(str(module_file).encode("utf-8", errors="replace"))
    for name in (
        "build_features",
        "build_features_live_incremental",
        "build_features_for_live_events",
        "build_live_features_for_events",
        "generate_candidate_events",
        "generate_candidate_events_live",
        "generate_live_candidate_events",
        "generate_candidate_events_for_live_times",
    ):
        fn = getattr(runtime, name, None)
        if not callable(fn):
            continue
        h.update(name.encode("utf-8"))
        code = getattr(fn, "__code__", None)
        if code is not None:
            h.update(code.co_code)
            h.update(repr(code.co_consts).encode("utf-8", errors="replace"))
        try:
            h.update(inspect.getsource(fn).encode("utf-8", errors="replace"))
        except Exception:
            h.update(repr(fn).encode("utf-8", errors="replace"))
    return h.hexdigest()


def _runtime_continuation_cache_key(edge_id: str, channel: str) -> str:
    return f"{edge_id}::{channel}"


def _runtime_continuation_state_path(edge_id: str, channel: str) -> Path:
    digest = hashlib.sha256(f"{edge_id}::{channel}".encode("utf-8")).hexdigest()[:24]
    return RUNTIME_CONTINUATION_STATE_DIR / f"{digest}.pkl"


def _load_runtime_owned_continuation_state(
    *,
    edge_id: str,
    channel: str,
    runtime_fingerprint: str,
    logger,
) -> tuple[Any, str]:
    """Load opaque runtime-owned state without interpreting its contents."""
    key = _runtime_continuation_cache_key(edge_id, channel)
    with _RUNTIME_CONTINUATION_STATE_CACHE_LOCK:
        if key in _RUNTIME_CONTINUATION_STATE_CACHE:
            try:
                return copy.deepcopy(_RUNTIME_CONTINUATION_STATE_CACHE[key]), "memory_cache"
            except Exception:
                return _RUNTIME_CONTINUATION_STATE_CACHE[key], "memory_cache_no_deepcopy"

    if not RUNTIME_STATE_PERSISTENCE_ENABLED:
        return {}, "persistence_disabled"
    path = _runtime_continuation_state_path(edge_id, channel)
    if not path.exists():
        return {}, "no_persisted_state"
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
        if not isinstance(payload, dict):
            raise TypeError(f"unexpected persisted payload type={type(payload).__name__}")
        if str(payload.get("edge_id")) != edge_id or str(payload.get("channel")) != channel:
            raise ValueError("persisted runtime-state identity mismatch")
        if str(payload.get("runtime_fingerprint")) != runtime_fingerprint:
            logger.warning(
                "LIVE_RUNTIME_STATE_IGNORED edge=%s channel=%s reason=runtime_fingerprint_changed path=%s",
                edge_id,
                channel,
                rel(path),
            )
            return {}, "persisted_state_runtime_fingerprint_changed"
        runtime_state = payload.get("runtime_state", {})
        with _RUNTIME_CONTINUATION_STATE_CACHE_LOCK:
            _RUNTIME_CONTINUATION_STATE_CACHE[key] = runtime_state
        try:
            return copy.deepcopy(runtime_state), "persisted_state"
        except Exception:
            return runtime_state, "persisted_state_no_deepcopy"
    except Exception as exc:
        logger.warning(
            "LIVE_RUNTIME_STATE_LOAD_FAILED edge=%s channel=%s path=%s error=%s fallback=empty_state",
            edge_id,
            channel,
            rel(path),
            exc,
        )
        return {}, f"persisted_state_load_failed:{type(exc).__name__}"


def _persist_runtime_owned_continuation_state(
    *,
    edge_id: str,
    channel: str,
    runtime_fingerprint: str,
    runtime_state: Any,
    logger,
) -> tuple[bool, str | None]:
    """Persist an opaque runtime state atomically; feature math stays runtime-owned."""
    key = _runtime_continuation_cache_key(edge_id, channel)
    with _RUNTIME_CONTINUATION_STATE_CACHE_LOCK:
        _RUNTIME_CONTINUATION_STATE_CACHE[key] = runtime_state
    if not RUNTIME_STATE_PERSISTENCE_ENABLED:
        return False, None
    path = _runtime_continuation_state_path(edge_id, channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    payload = {
        "schema_version": "runtime_owned_continuation_state_v1",
        "edge_id": edge_id,
        "channel": channel,
        "runtime_fingerprint": runtime_fingerprint,
        "updated_at": utc_now(),
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
        "runtime_state": runtime_state,
    }
    try:
        with tmp.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True, rel(path)
    except Exception as exc:
        logger.warning(
            "LIVE_RUNTIME_STATE_PERSIST_FAILED edge=%s channel=%s path=%s error=%s in_memory_state_retained=True",
            edge_id,
            channel,
            rel(path),
            exc,
        )
        return False, rel(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass



def _discard_runtime_owned_continuation_state(
    *,
    edge_id: str,
    channel: str,
    logger,
    reason: str,
) -> None:
    """Delete an untrusted runtime checkpoint after a parity failure."""
    key = _runtime_continuation_cache_key(edge_id, channel)
    with _RUNTIME_CONTINUATION_STATE_CACHE_LOCK:
        _RUNTIME_CONTINUATION_STATE_CACHE.pop(key, None)
    path = _runtime_continuation_state_path(edge_id, channel)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning(
            "LIVE_RUNTIME_STATE_DISCARD_WARN edge=%s channel=%s path=%s reason=%s error=%s",
            edge_id,
            channel,
            rel(path),
            reason,
            exc,
        )
    logger.error(
        "LIVE_RUNTIME_STATE_DISCARDED edge=%s channel=%s reason=%s path=%s",
        edge_id,
        channel,
        reason,
        rel(path),
    )

def _runtime_api_call_kwargs(
    fn: Any,
    *,
    base_kwargs: dict[str, Any],
    runtime_state: Any,
) -> dict[str, Any]:
    """Pass only parameters explicitly supported by a runtime-owned API."""
    try:
        signature = inspect.signature(fn)
        params = signature.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except Exception:
        params = {}
        has_var_kw = True

    kwargs: dict[str, Any] = {}
    for key, value in base_kwargs.items():
        if has_var_kw or key in params:
            kwargs[key] = value

    if "state" in params:
        kwargs["state"] = runtime_state
    elif "runtime_state" in params:
        kwargs["runtime_state"] = runtime_state
    elif has_var_kw:
        kwargs["state"] = runtime_state

    for contract_name in ("research_contract", "parity_contract", "live_parity_contract"):
        if contract_name in params:
            kwargs[contract_name] = dict(RESEARCH_LIVE_PARITY_CONTRACT)
            break
    else:
        if has_var_kw:
            kwargs["research_contract"] = dict(RESEARCH_LIVE_PARITY_CONTRACT)

    if "mode" in params or has_var_kw:
        kwargs.setdefault("mode", "live")
    return kwargs


def _normalize_runtime_owned_api_result(result: Any) -> tuple[pd.DataFrame | None, Any, dict[str, Any]]:
    """Normalize DataFrame, (DataFrame, state), or mapping runtime API results."""
    metadata: dict[str, Any] = {}
    runtime_state: Any = None
    features: pd.DataFrame | None = None
    if isinstance(result, pd.DataFrame):
        features = result
    elif isinstance(result, tuple):
        if len(result) >= 1 and isinstance(result[0], pd.DataFrame):
            features = result[0]
        if len(result) >= 2:
            runtime_state = result[1]
        if len(result) >= 3 and isinstance(result[2], dict):
            metadata = dict(result[2])
    elif isinstance(result, dict):
        for key in ("features", "feature_rows", "events", "event_rows", "dataframe", "frame", "rows"):
            value = result.get(key)
            if isinstance(value, pd.DataFrame):
                features = value
                break
        for key in ("state", "runtime_state", "continuation_state", "next_state"):
            if key in result:
                runtime_state = result.get(key)
                break
        meta = result.get("metadata") or result.get("diagnostics")
        if isinstance(meta, dict):
            metadata = dict(meta)
    return features, runtime_state, metadata



def _json_safe_runtime_metadata(value: Any, *, depth: int = 0) -> Any:
    """Bound arbitrary runtime diagnostics before storing them in JSON state."""
    if depth > 6:
        return f"<{type(value).__name__}:max_depth>"
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, np.generic):
        try:
            return value.item()
        except Exception:
            return str(value)
    if isinstance(value, (pd.Timestamp, datetime, Path)):
        return str(value)
    if isinstance(value, pd.DataFrame):
        return {
            "type": "DataFrame",
            "rows": int(len(value)),
            "columns": [str(c) for c in list(value.columns)[:50]],
        }
    if isinstance(value, pd.Series):
        return {"type": "Series", "rows": int(len(value)), "name": str(value.name)}
    if isinstance(value, dict):
        items = list(value.items())[:100]
        return {
            str(k): _json_safe_runtime_metadata(v, depth=depth + 1)
            for k, v in items
        }
    if isinstance(value, (list, tuple, set)):
        return [
            _json_safe_runtime_metadata(v, depth=depth + 1)
            for v in list(value)[:100]
        ]
    return str(value)

def _record_runtime_continuation_metadata(
    dashboard_state: dict[str, Any],
    *,
    edge_id: str,
    channel: str,
    record: dict[str, Any],
) -> None:
    by_edge = dashboard_state.setdefault("runtime_continuation_metadata_by_edge", {})
    edge_rec = dict(by_edge.get(edge_id) or {})
    safe_record = _json_safe_runtime_metadata(record)
    edge_rec[channel] = {"updated_at": utc_now(), **dict(safe_record or {})}
    edge_rec["research_live_parity_contract"] = dict(RESEARCH_LIVE_PARITY_CONTRACT)
    by_edge[edge_id] = edge_rec


def validate_runtime_stateful_parity_proof(metadata: dict[str, Any]) -> tuple[bool, list[str]]:
    """Require a runtime-owned continuation API to certify critical invariants.

    The dashboard cannot safely implement HMM/FFD/Kalman/CUSUM mathematics. It
    therefore accepts a stateful fast path only when the owning runtime reports
    that its streaming replay, append-only history, restart replay, scheduled HMM
    refits, and deterministic-noise contract have passed.
    """
    missing: list[str] = []
    for flag in RUNTIME_STATEFUL_REQUIRED_PROOF_FLAGS:
        value = nested_get(metadata, flag, f"parity.{flag}", f"contract.{flag}", default=None)
        if value is not True:
            missing.append(flag)
    return len(missing) == 0, missing


def validate_runtime_event_parity_proof(metadata: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for flag in RUNTIME_EVENT_REQUIRED_PROOF_FLAGS:
        value = nested_get(metadata, flag, f"parity.{flag}", f"contract.{flag}", default=None)
        if value is not True:
            missing.append(flag)
    return len(missing) == 0, missing


def call_runtime_incremental_feature_api(
    *,
    runtime: Any,
    edge_id: str,
    asset: str,
    bars: pd.DataFrame,
    live_event_times: set[pd.Timestamp],
    last_processed: pd.Timestamp | None,
    dashboard_state: dict[str, Any],
    logger,
    authoritative_features: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Use runtime-owned stateful feature continuation when the edge provides it.

    The dashboard passes the research contract and opaque prior runtime state,
    then stores the returned/mutated state atomically. It never rebuilds EMA,
    Kalman, FFD or HMM state itself. If no API exists, the caller uses the
    evidence-sized cold/recovery build.
    """
    diag: dict[str, Any] = {
        "api_used": False,
        "api_name": None,
        "candidate_times": 0,
        "reason": "stateful_incremental_feature_api_not_available",
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
    }
    if not LIVE_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED or not RUNTIME_STATEFUL_CONTINUATION_ENABLED:
        diag["reason"] = "disabled_by_settings"
        return None, diag

    candidate_times = candidate_live_times_for_runtime(live_event_times, last_processed)
    diag["candidate_times"] = int(len(candidate_times))
    if not candidate_times:
        diag["reason"] = "no_fresh_candidate_times"
        return None, diag

    runtime_fingerprint = _runtime_continuation_fingerprint(runtime)
    prior_state, state_source = _load_runtime_owned_continuation_state(
        edge_id=edge_id,
        channel="features",
        runtime_fingerprint=runtime_fingerprint,
        logger=logger,
    )
    diag["runtime_state_source"] = state_source

    for api_name in (
        "build_features_live_incremental",
        "build_features_for_live_events",
        "build_live_features_for_events",
    ):
        fn = getattr(runtime, api_name, None)
        if not callable(fn):
            continue
        api_start = time.perf_counter()
        try:
            kwargs = _runtime_api_call_kwargs(
                fn,
                base_kwargs={
                    "bars": bars,
                    "candidate_times": candidate_times,
                    "last_processed": last_processed,
                    "drop_warmup_rows": False,
                    "authoritative_features": authoritative_features,
                },
                runtime_state=prior_state,
            )
            result = fn(**kwargs)
            features, returned_state, api_metadata = _normalize_runtime_owned_api_result(result)
        except Exception as exc:
            logger.warning(
                "LIVE_INCREMENTAL_FEATURE_API_FAILED edge=%s asset=%s api=%s elapsed_ms=%.3f error=%s fallback=evidence_sized_recovery_build",
                edge_id,
                asset,
                api_name,
                elapsed_ms(api_start),
                exc,
            )
            logger.warning(traceback.format_exc())
            diag["reason"] = f"{api_name}_failed:{type(exc).__name__}"
            _discard_runtime_owned_continuation_state(
                edge_id=edge_id,
                channel="features",
                logger=logger,
                reason=diag["reason"],
            )
            _record_runtime_continuation_metadata(
                dashboard_state,
                edge_id=edge_id,
                channel="features",
                record={**diag, "error": f"{type(exc).__name__}: {exc}"},
            )
            return None, diag

        if features is None or features.empty:
            logger.warning(
                "LIVE_INCREMENTAL_FEATURE_API_EMPTY edge=%s asset=%s api=%s elapsed_ms=%.3f fallback=evidence_sized_recovery_build",
                edge_id,
                asset,
                api_name,
                elapsed_ms(api_start),
            )
            diag["reason"] = f"{api_name}_empty"
            _discard_runtime_owned_continuation_state(
                edge_id=edge_id,
                channel="features",
                logger=logger,
                reason=diag["reason"],
            )
            return None, diag

        proof_ok, missing_proof_flags = validate_runtime_stateful_parity_proof(api_metadata)
        if RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED and not proof_ok:
            diag.update({
                "api_used": False,
                "api_name": api_name,
                "reason": "runtime_stateful_parity_proof_missing",
                "missing_parity_proof_flags": missing_proof_flags,
                "api_metadata": api_metadata,
            })
            _discard_runtime_owned_continuation_state(
                edge_id=edge_id,
                channel="features",
                logger=logger,
                reason=diag["reason"],
            )
            _record_runtime_continuation_metadata(
                dashboard_state,
                edge_id=edge_id,
                channel="features",
                record=diag,
            )
            logger.error(
                "LIVE_INCREMENTAL_FEATURE_API_REJECTED edge=%s asset=%s api=%s reason=%s missing_proof_flags=%s fallback=evidence_sized_recovery_build",
                edge_id,
                asset,
                api_name,
                diag["reason"],
                missing_proof_flags,
            )
            return None, diag

        next_state = returned_state if returned_state is not None else prior_state
        persisted, state_path = _persist_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="features",
            runtime_fingerprint=runtime_fingerprint,
            runtime_state=next_state,
            logger=logger,
        )
        diag.update({
            "api_used": True,
            "api_name": api_name,
            "reason": "runtime_owned_stateful_feature_continuation_used",
            "runtime_state_persisted": bool(persisted),
            "runtime_state_path": state_path,
            "api_metadata": api_metadata,
            "output_rows": rows_len(features),
        })
        _record_runtime_continuation_metadata(
            dashboard_state,
            edge_id=edge_id,
            channel="features",
            record=diag,
        )
        logger.info(
            "LIVE_INCREMENTAL_FEATURE_API_USED edge=%s asset=%s api=%s candidate_times=%s rows=%s elapsed_ms=%.3f state_source=%s state_persisted=%s",
            edge_id,
            asset,
            api_name,
            len(candidate_times),
            rows_len(features),
            elapsed_ms(api_start),
            state_source,
            persisted,
        )
        return features, diag

    _record_runtime_continuation_metadata(
        dashboard_state,
        edge_id=edge_id,
        channel="features",
        record=diag,
    )
    return None, diag


def call_runtime_live_event_api(
    *,
    runtime: Any,
    edge_id: str,
    asset: str,
    features: pd.DataFrame,
    live_event_times: set[pd.Timestamp],
    last_processed: pd.Timestamp | None,
    dashboard_state: dict[str, Any],
    logger,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Use a runtime-owned stateful live-event API when available.

    CUSUM accumulators, event-volatility EWM and edge-local Kalman state are
    path-dependent. Their opaque state is therefore loaded/passed/persisted in
    exactly the same way as feature continuation; the dashboard never resets or
    recreates those calculations itself.
    """
    diag: dict[str, Any] = {
        "api_used": False,
        "api_name": None,
        "candidate_times": 0,
        "reason": "stateful_live_event_api_not_available",
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
    }
    if not LIVE_EVENT_ONLY_RUNTIME_API_ENABLED or not RUNTIME_STATEFUL_CONTINUATION_ENABLED:
        diag["reason"] = "disabled_by_settings"
        return None, diag

    candidate_times = candidate_live_times_for_runtime(live_event_times, last_processed)
    diag["candidate_times"] = int(len(candidate_times))
    if not candidate_times:
        diag["reason"] = "no_fresh_candidate_times"
        return pd.DataFrame(), diag

    runtime_fingerprint = _runtime_continuation_fingerprint(runtime)
    prior_state, state_source = _load_runtime_owned_continuation_state(
        edge_id=edge_id,
        channel="events",
        runtime_fingerprint=runtime_fingerprint,
        logger=logger,
    )
    diag["runtime_state_source"] = state_source

    for api_name in (
        "generate_candidate_events_live",
        "generate_live_candidate_events",
        "generate_candidate_events_for_live_times",
    ):
        fn = getattr(runtime, api_name, None)
        if not callable(fn):
            continue
        api_start = time.perf_counter()
        try:
            kwargs = _runtime_api_call_kwargs(
                fn,
                base_kwargs={
                    "features": features,
                    "candidate_times": candidate_times,
                    "last_processed": last_processed,
                },
                runtime_state=prior_state,
            )
            result = fn(**kwargs)
            events, returned_state, api_metadata = _normalize_runtime_owned_api_result(result)
        except Exception as exc:
            logger.warning(
                "LIVE_EVENT_ONLY_API_FAILED edge=%s asset=%s api=%s elapsed_ms=%.3f error=%s fallback=full_candidate_event_generation",
                edge_id,
                asset,
                api_name,
                elapsed_ms(api_start),
                exc,
            )
            logger.warning(traceback.format_exc())
            diag["reason"] = f"{api_name}_failed:{type(exc).__name__}"
            _discard_runtime_owned_continuation_state(
                edge_id=edge_id,
                channel="events",
                logger=logger,
                reason=diag["reason"],
            )
            _record_runtime_continuation_metadata(
                dashboard_state,
                edge_id=edge_id,
                channel="events",
                record={**diag, "error": f"{type(exc).__name__}: {exc}"},
            )
            return None, diag

        if events is None:
            events = pd.DataFrame()
        proof_ok, missing_proof_flags = validate_runtime_event_parity_proof(api_metadata)
        if RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED and not proof_ok:
            diag.update({
                "api_used": False,
                "api_name": api_name,
                "reason": "runtime_event_stateful_parity_proof_missing",
                "missing_parity_proof_flags": missing_proof_flags,
                "api_metadata": api_metadata,
            })
            _discard_runtime_owned_continuation_state(
                edge_id=edge_id,
                channel="events",
                logger=logger,
                reason=diag["reason"],
            )
            _record_runtime_continuation_metadata(
                dashboard_state,
                edge_id=edge_id,
                channel="events",
                record=diag,
            )
            logger.error(
                "LIVE_EVENT_ONLY_API_REJECTED edge=%s asset=%s api=%s reason=%s missing_proof_flags=%s fallback=full_candidate_event_generation",
                edge_id,
                asset,
                api_name,
                diag["reason"],
                missing_proof_flags,
            )
            return None, diag
        next_state = returned_state if returned_state is not None else prior_state
        persisted, state_path = _persist_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="events",
            runtime_fingerprint=runtime_fingerprint,
            runtime_state=next_state,
            logger=logger,
        )
        diag.update({
            "api_used": True,
            "api_name": api_name,
            "reason": "runtime_owned_stateful_live_event_api_used",
            "runtime_state_persisted": bool(persisted),
            "runtime_state_path": state_path,
            "api_metadata": api_metadata,
            "output_rows": rows_len(events),
        })
        _record_runtime_continuation_metadata(
            dashboard_state,
            edge_id=edge_id,
            channel="events",
            record=diag,
        )
        logger.info(
            "LIVE_EVENT_ONLY_API_USED edge=%s asset=%s api=%s candidate_times=%s events=%s elapsed_ms=%.3f state_source=%s state_persisted=%s",
            edge_id,
            asset,
            api_name,
            len(candidate_times),
            rows_len(events),
            elapsed_ms(api_start),
            state_source,
            persisted,
        )
        return events, diag

    _record_runtime_continuation_metadata(
        dashboard_state,
        edge_id=edge_id,
        channel="events",
        record=diag,
    )
    return None, diag


def generate_candidate_events_with_live_api_fallback(
    *,
    runtime: Any,
    edge_id: str,
    asset: str,
    features: pd.DataFrame,
    live_event_times: set[pd.Timestamp],
    last_processed: pd.Timestamp | None,
    dashboard_state: dict[str, Any],
    logger,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Generate events through the certified execution class.

    Cumulative/path-dependent edges are routed directly to their mandatory full
    authority. They must never spend time attempting or invalidating a bounded
    native checkpoint first.
    """
    if bool(getattr(runtime, "EVENT_FULL_AUTHORITY_REQUIRED", False)):
        started = time.perf_counter()
        try:
            events = runtime.generate_candidate_events(features)
        except Exception as exc:
            if not is_expected_empty_event_exception(exc):
                raise
            events = pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
        diag = {
            "api_used": False,
            "api_name": "generate_candidate_events",
            "candidate_times": int(
                len(candidate_live_times_for_runtime(live_event_times, last_processed))
            ),
            "reason": "certified_authoritative_full_history_direct",
            "event_execution_mode": "authoritative_full_history_required",
            "full_authority_required": True,
            "elapsed_ms": elapsed_ms(started),
        }
        _record_runtime_continuation_metadata(
            dashboard_state,
            edge_id=edge_id,
            channel="events",
            record=diag,
        )
        logger.info(
            "LIVE_EVENT_FULL_AUTHORITY_DIRECT edge=%s asset=%s elapsed_ms=%.3f events=%s",
            edge_id,
            asset,
            float(diag["elapsed_ms"]),
            rows_len(events),
        )
        return events, diag

    events, diag = call_runtime_live_event_api(
        runtime=runtime,
        edge_id=edge_id,
        asset=asset,
        features=features,
        live_event_times=live_event_times,
        last_processed=last_processed,
        dashboard_state=dashboard_state,
        logger=logger,
    )
    if events is not None:
        return events, diag

    # Recovery compatibility only. The runtime remains the owner of event math,
    # but without a stateful API it must replay its full event path on the
    # evidence-sized recovery history instead of an arbitrary short tail.
    try:
        events = runtime.generate_candidate_events(features)
    except Exception as exc:
        if not is_expected_empty_event_exception(exc):
            raise
        events = pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
        diag["reason"] = "fallback_full_runtime_event_replay_valid_no_candidate"
        return events, diag
    diag["reason"] = "fallback_full_runtime_event_replay_no_stateful_api"
    return events, diag



def _candidate_identity_rows(
    events: pd.DataFrame,
    allowed_times: set[pd.Timestamp],
) -> list[tuple[Any, ...]]:
    if events is None or not isinstance(events, pd.DataFrame) or events.empty or "Datetime" not in events.columns:
        return []
    work = events.copy()
    work["Datetime"] = parse_utc_datetime_series(work["Datetime"])
    work = work.dropna(subset=["Datetime"])
    if allowed_times:
        work = work[work["Datetime"].isin(allowed_times)]
    columns = [
        name
        for name in ("Datetime", "event_id", "side", "candidate_event_generator", "cusum_direction")
        if name in work.columns
    ]
    rows: list[tuple[Any, ...]] = []
    for _, row in work[columns].sort_values("Datetime").iterrows():
        values: list[Any] = []
        for name in columns:
            value = row[name]
            if name == "Datetime":
                values.append(safe_iso(value))
            elif pd.isna(value):
                values.append(None)
            elif isinstance(value, np.generic):
                values.append(value.item())
            else:
                values.append(value)
        rows.append(tuple(values))
    return rows


def confirm_incremental_candidate_with_full_runtime(
    *,
    runtime: Any,
    edge_id: str,
    asset: str,
    bars: pd.DataFrame,
    fast_events: pd.DataFrame,
    live_event_times: set[pd.Timestamp],
    last_processed: pd.Timestamp | None,
    logger,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Confirm every actual candidate with the unchanged full edge runtime.

    Verified suffix continuation is allowed to accelerate no-event bars.  A
    model-scoring decision, however, is always supplied with full authoritative
    features/events.  This preserves the existing research/live decision path
    while keeping the common no-event path fast.
    """
    started = time.perf_counter()
    allowed = set(candidate_live_times_for_runtime(live_event_times, last_processed))
    fast_identity = _candidate_identity_rows(fast_events, allowed)
    full_features = runtime.build_features(bars=bars, drop_warmup_rows=False)
    full_events = runtime.generate_candidate_events(full_features)
    full_identity = _candidate_identity_rows(full_events, allowed)
    match = fast_identity == full_identity
    diagnostics = {
        "enabled": True,
        "confirmed": True,
        "identity_match": bool(match),
        "fast_candidate_rows": int(len(fast_identity)),
        "full_candidate_rows": int(len(full_identity)),
        "allowed_candidate_times": int(len(allowed)),
        "elapsed_ms": elapsed_ms(started),
    }
    if not match:
        diagnostics["fast_identity"] = fast_identity[:10]
        diagnostics["full_identity"] = full_identity[:10]
        _discard_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="features",
            logger=logger,
            reason="candidate_full_authority_identity_mismatch",
        )
        _discard_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="events",
            logger=logger,
            reason="candidate_full_authority_identity_mismatch",
        )
        logger.error(
            "LIVE_INCREMENTAL_CANDIDATE_CONFIRM_MISMATCH edge=%s asset=%s fast=%s full=%s action=use_full_authority_and_discard_checkpoints",
            edge_id,
            asset,
            fast_identity[:10],
            full_identity[:10],
        )
    else:
        logger.info(
            "LIVE_INCREMENTAL_CANDIDATE_CONFIRMED edge=%s asset=%s candidate_rows=%s elapsed_ms=%.3f source=unchanged_full_runtime",
            edge_id,
            asset,
            len(full_identity),
            diagnostics["elapsed_ms"],
        )
    return full_features, full_events, diagnostics


def filter_recent_events_for_runtime_score(
    edge_id: str,
    events: Any,
    live_event_times: set[pd.Timestamp],
    last_processed: pd.Timestamp | None,
    logger,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Keep runtime feature/event memory intact, but cap the event rows sent to scoring.

    The dashboard still builds features and generates events from the full combined
    memory window. This helper only reduces the candidate event table passed into
    runtime.score(), preventing repeated scoring of hundreds of historical event
    rows on every new bar. It does not loosen timestamp gates, thresholds, stale
    checks, or execution rules.
    """
    diagnostics: dict[str, Any] = {
        "input_events": int(0 if events is None else len(events)),
        "events_with_valid_datetime": 0,
        "events_after_cursor": 0,
        "events_on_executable_live_after_cursor": 0,
        "events_sent_to_runtime_score": 0,
        "events_trimmed_by_limit": 0,
        "max_events_to_score": int(MAX_EVENTS_TO_SCORE_PER_EDGE),
        "last_processed_timestamp": safe_iso(last_processed) if last_processed is not None else None,
        "latest_executable_live_timestamp": None,
        "filter_reason": "no_events",
    }

    if events is None or not hasattr(events, "empty") or events.empty:
        return pd.DataFrame(), diagnostics
    if "Datetime" not in getattr(events, "columns", []):
        diagnostics["filter_reason"] = "events_missing_datetime"
        logger.warning("RUNTIME_EVENT_SCORE_FILTER edge=%s reason=events_missing_datetime input_events=%s", edge_id, diagnostics["input_events"])
        return pd.DataFrame(), diagnostics

    try:
        live_times = pd.Series(list(live_event_times or []), dtype="datetime64[ns, UTC]").dropna().sort_values().reset_index(drop=True)
    except Exception:
        live_times = pd.Series(dtype="datetime64[ns, UTC]")
    if not live_times.empty:
        diagnostics["latest_executable_live_timestamp"] = safe_iso(live_times.iloc[-1])

    work = events.copy()
    work["_dashboard_event_time"] = parse_utc_datetime_series(work["Datetime"])
    work = work.dropna(subset=["_dashboard_event_time"]).sort_values("_dashboard_event_time")
    diagnostics["events_with_valid_datetime"] = int(len(work))
    if work.empty:
        diagnostics["filter_reason"] = "no_valid_event_datetime"
        return pd.DataFrame(), diagnostics

    if last_processed is not None:
        recent = work[work["_dashboard_event_time"] > last_processed].copy()
        diagnostics["filter_reason"] = "post_cursor_recent_events"
    else:
        # On a brand-new state, avoid scoring the whole historical memory window.
        # Only executable live timestamps are eligible for trading anyway.
        live_set = set(live_times.tolist())
        recent = work[work["_dashboard_event_time"].isin(live_set)].copy() if live_set else work.tail(MAX_EVENTS_TO_SCORE_PER_EDGE).copy()
        diagnostics["filter_reason"] = "no_cursor_live_timestamp_bootstrap"

    diagnostics["events_after_cursor"] = int(len(recent))
    if recent.empty:
        return pd.DataFrame(), diagnostics

    if not live_times.empty:
        live_set = set(live_times.tolist())
        diagnostics["events_on_executable_live_after_cursor"] = int(recent["_dashboard_event_time"].isin(live_set).sum())

    before_limit = int(len(recent))
    if before_limit > MAX_EVENTS_TO_SCORE_PER_EDGE:
        recent = recent.tail(MAX_EVENTS_TO_SCORE_PER_EDGE).copy()
    diagnostics["events_sent_to_runtime_score"] = int(len(recent))
    diagnostics["events_trimmed_by_limit"] = max(before_limit - int(len(recent)), 0)

    return recent.drop(columns=["_dashboard_event_time"]), diagnostics


def edge_has_fresh_pending_live_timestamp(
    edge: dict[str, Any],
    live_event_times: set[pd.Timestamp] | None,
    state: dict[str, Any],
) -> bool:
    """Return True when this edge still has at least one not-yet-stale live bar.

    Diagnostic/scheduling helper only. It does not mutate cursors or approve
    signals. The existing per-edge pre-runtime guard remains the authority for
    cursor advancement when all pending live timestamps are stale.
    """
    edge_id = str(edge.get("edge_id", ""))
    last_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
    try:
        last_processed = ensure_utc_timestamp(last_raw) if last_raw else None
        if last_processed is not None and pd.isna(last_processed):
            last_processed = None
    except Exception:
        last_processed = None
    pending = live_timestamps_after_cursor(live_event_times, last_processed)
    if not pending:
        return False
    latest_pending = pending[-1]
    try:
        age = float((utc_now_ts() - latest_pending).total_seconds())
    except Exception:
        return False
    return age <= float(MAX_SIGNAL_AGE_SECONDS)


def runtime_expected_feature_names(runtime: Any) -> list[str]:
    """Best-effort access to a runtime's schema feature list for cache safety."""
    getter = getattr(runtime, "_schema_feature_names", None)
    if callable(getter):
        try:
            return [str(x) for x in getter()]
        except Exception:
            return []
    return []


def runtime_selected_feature_names(runtime: Any) -> list[str]:
    """Best-effort access to the runtime's locked model-input feature list."""
    getter = getattr(runtime, "_load_selected_features", None)
    if callable(getter):
        try:
            return [str(x) for x in getter()]
        except Exception:
            pass

    scorer_getter = getattr(runtime, "_edge_scorer", None)
    if callable(scorer_getter):
        try:
            scorer = scorer_getter()
            load_selected = getattr(scorer, "load_selected_features", None)
            if callable(load_selected):
                return [str(x) for x in load_selected()]
        except Exception:
            pass
    return []


def is_selected_feature_nan_drop_exception(exc: BaseException) -> bool:
    """Identify the runtime's fail-closed 'all candidate inputs are NaN' error."""
    try:
        return SELECTED_FEATURE_NAN_DROP_ERROR_FRAGMENT.lower() in str(exc).lower()
    except Exception:
        return False


def candidate_selected_feature_diagnostics(
    *,
    runtime: Any,
    features: pd.DataFrame,
    events: pd.DataFrame,
) -> dict[str, Any]:
    """Describe non-finite locked model inputs at candidate timestamps.

    This mirrors the runtime's feature-first lookup closely enough for diagnosis
    only. It does not engineer, impute, or alter any model input.
    """
    selected = runtime_selected_feature_names(runtime)
    diag: dict[str, Any] = {
        "selected_feature_count": len(selected),
        "candidate_rows": 0,
        "invalid_candidate_rows": 0,
        "missing_selected_features": [],
        "invalid_features_by_timestamp": {},
        "invalid_feature_sample": [],
    }
    if not selected or features is None or features.empty or events is None or events.empty:
        return diag

    feature_table = features.copy()
    event_table = events.copy()
    if "Datetime" not in feature_table.columns or "Datetime" not in event_table.columns:
        return diag

    feature_table["Datetime"] = pd.to_datetime(
        feature_table["Datetime"], utc=True, errors="coerce"
    )
    event_table["Datetime"] = pd.to_datetime(
        event_table["Datetime"], utc=True, errors="coerce"
    )
    feature_table = feature_table.dropna(subset=["Datetime"]).sort_values("Datetime")
    event_table = event_table.dropna(subset=["Datetime"]).sort_values("Datetime")
    if event_table.empty:
        return diag

    missing = [
        name
        for name in selected
        if name not in feature_table.columns and name not in event_table.columns
    ]
    diag["missing_selected_features"] = missing

    # The scoring batch is intentionally tiny (normally one row), so explicit
    # timestamp lookup is clearer and safer than a broad merge with collisions.
    feature_groups = {
        ts: group.iloc[-1]
        for ts, group in feature_table.groupby("Datetime", sort=False)
    }
    event_groups = {
        ts: group.iloc[-1]
        for ts, group in event_table.groupby("Datetime", sort=False)
    }

    invalid_union: set[str] = set()
    for ts in sorted(event_groups):
        diag["candidate_rows"] += 1
        feature_row = feature_groups.get(ts)
        event_row = event_groups.get(ts)
        invalid: list[str] = []
        for name in selected:
            if feature_row is not None and name in feature_table.columns:
                value = feature_row.get(name)
            elif event_row is not None and name in event_table.columns:
                value = event_row.get(name)
            else:
                invalid.append(name)
                continue
            numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
            try:
                finite = bool(pd.notna(numeric) and np.isfinite(float(numeric)))
            except Exception:
                finite = False
            if not finite:
                invalid.append(name)
        if invalid:
            diag["invalid_candidate_rows"] += 1
            invalid_union.update(invalid)
            diag["invalid_features_by_timestamp"][ts.isoformat()] = invalid[:25]

    diag["invalid_feature_sample"] = sorted(invalid_union)[:25]
    return diag


def mark_incomplete_model_input_candidate_processed(
    *,
    edge_id: str,
    asset: str,
    runtime: Any,
    features: pd.DataFrame,
    events: pd.DataFrame,
    state: dict[str, Any],
    logger,
    source: str,
) -> None:
    """Fail closed without turning a deterministic non-scorable row into an error."""
    diag = candidate_selected_feature_diagnostics(
        runtime=runtime,
        features=features,
        events=events,
    )
    timestamps: list[pd.Timestamp] = []
    if events is not None and not events.empty and "Datetime" in events.columns:
        parsed = pd.to_datetime(events["Datetime"], utc=True, errors="coerce").dropna()
        timestamps = sorted(set(parsed.tolist()))
    if timestamps:
        current_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
        current = ensure_utc_timestamp(current_raw) if current_raw else None
        latest = timestamps[-1]
        if current is None or latest > current:
            state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = latest.isoformat()

    counts = state.setdefault("blocked_incomplete_model_input_count_by_edge", {})
    counts[edge_id] = int(counts.get(edge_id, 0) or 0) + max(
        int(diag.get("invalid_candidate_rows", 0) or 0),
        len(timestamps),
        1,
    )
    record = {
        "created_at": utc_now(),
        "edge_id": edge_id,
        "asset": asset,
        "source": source,
        "candidate_timestamps": [ts.isoformat() for ts in timestamps],
        **diag,
    }
    state.setdefault("last_incomplete_model_input_by_edge", {})[edge_id] = record
    history = state.setdefault("incomplete_model_input_history", [])
    history.append(record)
    state["incomplete_model_input_history"] = history[-200:]
    logger.warning(
        "LIVE_CANDIDATE_MODEL_INPUT_BLOCKED edge=%s asset=%s source=%s "
        "candidate_rows=%s invalid_candidate_rows=%s missing_selected=%s "
        "invalid_feature_sample=%s action=cursor_advanced_fail_closed_no_signal",
        edge_id,
        asset,
        source,
        diag.get("candidate_rows"),
        diag.get("invalid_candidate_rows"),
        diag.get("missing_selected_features"),
        diag.get("invalid_feature_sample"),
    )



def prepare_shared_features_for_edge(
    *,
    edge: dict[str, Any],
    runtime: Any,
    shared_features_snapshot: pd.DataFrame | None,
    logger,
) -> tuple[pd.DataFrame | None, str]:
    """Return an edge-specific copy of a shared asset feature snapshot if safe.

    Reuse now requires both schema coverage and a shared-feature contract match.
    If no explicit contract hash exists, exact build_features() implementation
    identity is required. Any mismatch preserves the original edge-local path.
    """
    edge_id = str(edge.get("edge_id", ""))
    asset = str(edge.get("asset") or (edge_id.split("/", 1)[0] if "/" in edge_id else ""))
    family = str(edge.get("family", "")).strip().lower()
    if family in INCREMENTAL_EXCLUDED_FAMILIES:
        logger.info(
            "LIVE_SHARED_FEATURE_CACHE_EDGE_BYPASS edge=%s family=%s reason=excluded_family",
            edge_id,
            family,
        )
        return None, "excluded_family"
    if shared_features_snapshot is None or shared_features_snapshot.empty:
        return None, "shared_feature_snapshot_empty"

    if STRICT_SHARED_FEATURE_CONTRACT_ENABLED:
        with _SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK:
            cache_record = dict(_SHARED_ASSET_RUNTIME_FEATURE_CACHE.get(asset) or {})
        builder_identity = str(cache_record.get("builder_contract_identity") or cache_record.get("builder_fingerprint") or "")
        builder_identity_source = str(cache_record.get("builder_contract_identity_source") or "legacy_builder_fingerprint")
        target_source, target_identity = shared_feature_contract_identity(edge, runtime)
        if not builder_identity:
            logger.warning(
                "LIVE_SHARED_FEATURE_CACHE_EDGE_BYPASS edge=%s reason=missing_builder_feature_contract_identity",
                edge_id,
            )
            return None, "missing_builder_feature_contract_identity"
        if target_identity != builder_identity:
            logger.warning(
                "LIVE_SHARED_FEATURE_CACHE_EDGE_BYPASS edge=%s reason=shared_feature_contract_mismatch "
                "builder_source=%s builder_identity=%s target_source=%s target_identity=%s",
                edge_id,
                builder_identity_source,
                builder_identity,
                target_source,
                target_identity,
            )
            return None, "shared_feature_contract_mismatch"

    expected = runtime_expected_feature_names(runtime)
    if expected:
        missing = [name for name in expected if name not in shared_features_snapshot.columns]
        if missing:
            logger.warning(
                "LIVE_SHARED_FEATURE_CACHE_EDGE_BYPASS edge=%s reason=missing_schema_features missing_count=%s sample_missing=%s",
                edge_id,
                len(missing),
                missing[:10],
            )
            return None, "missing_schema_features"
    out = shared_features_snapshot.copy()
    if "edge_id" in out.columns:
        out["edge_id"] = edge_id
    side_value = edge.get("side_value")
    if "side" in out.columns and side_value is not None:
        try:
            out["side"] = int(side_value)
        except Exception:
            out["side"] = side_value
    return out, "shared_feature_snapshot_contract_verified"

def shared_feature_builder_priority(edge: dict[str, Any]) -> tuple[int, str]:
    """Prefer runtimes that usually emit the widest shared feature table.

    This is only a scheduling/latency choice for the shared feature cache. It
    does not alter event logic, model inputs, thresholds, or signal gates. The
    selected runtime still owns build_features(); if the shared table is not
    compatible for another edge, that edge falls back to its own runtime path.
    """
    edge_id = str(edge.get("edge_id", "")).lower()
    edge_name = edge_id.split("/", 1)[-1]
    family = str(edge.get("family", "")).lower()
    haystack = f"{edge_id} {edge_name} {family}"
    priorities = [
        ("volatilitycarry", 0),
        ("volatilityregime", 1),
        ("timeregime", 2),
        ("trend", 3),
        ("rangestructure", 4),
        ("compressionbreakoutcontinuation", 5),
        ("absorption", 6),
        ("reversal", 7),
    ]
    for token, priority in priorities:
        if token in haystack:
            return priority, edge_id
    return 50, edge_id


def ordered_shared_feature_builder_edges(
    *,
    asset_edges: list[dict[str, Any]],
    live_event_times_snapshot: set[pd.Timestamp] | None,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return fresh builder candidates ordered by expected feature breadth."""
    fresh = [
        edge
        for edge in asset_edges
        if edge_has_fresh_pending_live_timestamp(edge, live_event_times_snapshot, state)
    ]
    return sorted(fresh, key=shared_feature_builder_priority)



def edge_family_is_incremental_excluded(edge: dict[str, Any]) -> bool:
    return str(edge.get("family", "")).strip().lower() in INCREMENTAL_EXCLUDED_FAMILIES


def runtime_build_features_fingerprint(runtime: Any) -> str:
    """Stable best-effort fingerprint of the canonical runtime feature builder."""
    fn = getattr(runtime, "build_features", None)
    h = hashlib.sha256()
    h.update(str(getattr(fn, "__module__", "")).encode("utf-8"))
    h.update(str(getattr(fn, "__qualname__", getattr(fn, "__name__", ""))).encode("utf-8"))
    code = getattr(fn, "__code__", None)
    if code is not None:
        h.update(code.co_code)
        h.update(repr(code.co_consts).encode("utf-8", errors="replace"))
        h.update(repr(code.co_names).encode("utf-8", errors="replace"))
    try:
        h.update(inspect.getsource(fn).encode("utf-8", errors="replace"))
    except Exception:
        h.update(repr(fn).encode("utf-8", errors="replace"))
    return h.hexdigest()


def runtime_build_features_semantic_fingerprint(runtime: Any) -> str:
    """Fingerprint build_features semantics without edge/module identity noise."""
    fn = getattr(runtime, "build_features", None)
    h = hashlib.sha256()
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
        node = next((item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if node is None:
            raise ValueError("build_features function AST not found")
        node.name = "build_features"
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:]
        h.update(ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8"))
    except Exception:
        code = getattr(fn, "__code__", None)
        if code is not None:
            h.update(code.co_code)
            # co_names captures helper dependencies while deliberately excluding
            # __module__/__qualname__, which differ for isolated edge imports.
            h.update(repr(code.co_names).encode("utf-8", errors="replace"))
        else:
            h.update(repr(fn).encode("utf-8", errors="replace"))
    return h.hexdigest()


def shared_feature_contract_identity(
    edge: dict[str, Any],
    runtime: Any,
) -> tuple[str, str]:
    """Return an explicit shared-feature contract hash or a strict code fingerprint.

    Column presence alone is not mathematical parity. When an edge does not
    declare an asset-level shared_feature_contract_hash, exact build_features()
    implementation identity is required before it may consume another runtime's
    feature table. A mismatch falls back to the edge's own runtime path.
    """
    edge_manifest = load_edge_manifest_for_signal(edge)
    candidates = [
        nested_get(edge, "shared_feature_contract_hash", "feature_contract_hash", "contracts.shared_feature_contract_hash", "contracts.shared_asset_feature_contract_hash", default=None),
        nested_get(edge_manifest, "shared_feature_contract_hash", "feature_contract_hash", "contracts.shared_feature_contract_hash", "contracts.shared_asset_feature_contract_hash", default=None),
    ]
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return "declared_shared_feature_contract_hash", text
    return "runtime_build_features_semantic_fingerprint", runtime_build_features_semantic_fingerprint(runtime)


def _normalized_datetime_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted, unique Datetime frame without changing caller data."""
    if frame is None or frame.empty or "Datetime" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["Datetime"] = parse_utc_datetime_series(out["Datetime"])
    out = out.dropna(subset=["Datetime"]).sort_values("Datetime")
    out = out.drop_duplicates(subset=["Datetime"], keep="last").reset_index(drop=True)
    return out


def _stable_frame_hash(frame: pd.DataFrame, *, exclude_columns: set[str] | None = None) -> str:
    """Content hash used only to detect unchanged dashboard snapshots."""
    if frame is None or frame.empty:
        return hashlib.sha256(b"EMPTY").hexdigest()
    exclude = set(exclude_columns or set())
    normalized = _normalized_datetime_frame(frame)
    if normalized.empty:
        normalized = frame.copy().reset_index(drop=True)
    columns = [str(c) for c in normalized.columns if str(c) not in exclude]
    normalized = normalized[columns].copy()
    for column in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[column]):
            normalized[column] = normalized[column].astype("int64")
        elif normalized[column].dtype == object:
            normalized[column] = normalized[column].map(
                lambda value: "<NA>" if pd.isna(value) else str(value)
            )
    hashed = pd.util.hash_pandas_object(normalized, index=False).values.tobytes()
    h = hashlib.sha256()
    h.update("|".join(columns).encode("utf-8"))
    h.update(hashed)
    return h.hexdigest()


def _raw_bar_overlap_diagnostics(
    cached_bars: pd.DataFrame,
    current_bars: pd.DataFrame,
) -> dict[str, Any]:
    """Prove that common raw bars were not revised between cache snapshots."""
    old = _normalized_datetime_frame(cached_bars)
    new = _normalized_datetime_frame(current_bars)
    diag: dict[str, Any] = {
        "ok": False,
        "reason": "missing_or_empty_bars",
        "cached_rows": rows_len(old),
        "current_rows": rows_len(new),
        "common_rows": 0,
        "new_rows": 0,
        "dropped_rows": 0,
        "latest_cached_bar": None,
        "latest_current_bar": None,
    }
    if old.empty or new.empty:
        return diag
    old_idx = old.set_index("Datetime", drop=False)
    new_idx = new.set_index("Datetime", drop=False)
    common = old_idx.index.intersection(new_idx.index).sort_values()
    new_only = new_idx.index.difference(old_idx.index)
    old_only = old_idx.index.difference(new_idx.index)
    diag.update({
        "common_rows": int(len(common)),
        "new_rows": int(len(new_only)),
        "dropped_rows": int(len(old_only)),
        "latest_cached_bar": safe_iso(old_idx.index.max()),
        "latest_current_bar": safe_iso(new_idx.index.max()),
    })
    if len(common) < max(1, min(INCREMENTAL_OVERLAP_CHECK_ROWS, len(old_idx) - 1, len(new_idx) - 1)):
        diag["reason"] = "insufficient_raw_bar_overlap"
        return diag
    if len(new_only) <= 0:
        diag["reason"] = "no_new_bar_timestamp"
        return diag
    if new_idx.index.max() <= old_idx.index.max():
        diag["reason"] = "current_snapshot_does_not_advance"
        return diag

    compare_columns = [
        column for column in old.columns
        if column in new.columns and column not in {"Closed_At"}
    ]
    old_common = old_idx.loc[common, compare_columns].reset_index(drop=True)
    new_common = new_idx.loc[common, compare_columns].reset_index(drop=True)
    if _stable_frame_hash(old_common) != _stable_frame_hash(new_common):
        diag["reason"] = "historical_raw_bar_overlap_changed"
        return diag
    diag["ok"] = True
    diag["reason"] = "append_or_rolling_window_verified"
    return diag


def _feature_parity_diagnostics(
    trusted_features: pd.DataFrame,
    recomputed_features: pd.DataFrame,
    *,
    overlap_rows: int,
) -> dict[str, Any]:
    """Strict timestamp/schema/NaN/value parity on recent trusted overlap rows."""
    trusted = _normalized_datetime_frame(trusted_features)
    recomputed = _normalized_datetime_frame(recomputed_features)
    diag: dict[str, Any] = {
        "ok": False,
        "reason": "empty_feature_frame",
        "trusted_rows": rows_len(trusted),
        "recomputed_rows": rows_len(recomputed),
        "overlap_rows_available": 0,
        "overlap_rows_checked": 0,
        "columns_checked": 0,
        "max_abs_diff": 0.0,
        "max_rel_diff": 0.0,
        "mismatch_column": None,
    }
    if trusted.empty or recomputed.empty:
        return diag
    trusted_columns = list(trusted.columns)
    recomputed_columns = list(recomputed.columns)
    if set(trusted_columns) != set(recomputed_columns):
        diag["reason"] = "feature_schema_mismatch"
        diag["missing_from_recomputed"] = sorted(set(trusted_columns) - set(recomputed_columns))[:20]
        diag["extra_in_recomputed"] = sorted(set(recomputed_columns) - set(trusted_columns))[:20]
        return diag

    trusted_idx = trusted.set_index("Datetime", drop=False)
    recomputed_idx = recomputed.set_index("Datetime", drop=False)
    common = trusted_idx.index.intersection(recomputed_idx.index).sort_values()
    diag["overlap_rows_available"] = int(len(common))
    required = min(max(int(overlap_rows), 1), len(trusted_idx))
    if len(common) < required:
        diag["reason"] = "insufficient_feature_overlap"
        diag["required_overlap_rows"] = int(required)
        return diag
    check_times = common[-required:]
    left = trusted_idx.loc[check_times, trusted_columns]
    right = recomputed_idx.loc[check_times, trusted_columns]
    diag["overlap_rows_checked"] = int(len(check_times))

    for column in trusted_columns:
        if column == "Datetime":
            continue
        lser = left[column]
        rser = right[column]
        lna = lser.isna().to_numpy()
        rna = rser.isna().to_numpy()
        if not np.array_equal(lna, rna):
            diag.update({"reason": "nan_mask_mismatch", "mismatch_column": str(column)})
            return diag
        valid = ~lna
        if not valid.any():
            continue
        if pd.api.types.is_numeric_dtype(lser) and pd.api.types.is_numeric_dtype(rser):
            lv = pd.to_numeric(lser[valid], errors="coerce").to_numpy(dtype=float)
            rv = pd.to_numeric(rser[valid], errors="coerce").to_numpy(dtype=float)
            finite = np.isfinite(lv) & np.isfinite(rv)
            if not finite.all():
                if not np.array_equal(np.isfinite(lv), np.isfinite(rv)):
                    diag.update({"reason": "finite_mask_mismatch", "mismatch_column": str(column)})
                    return diag
            if finite.any():
                abs_diff = np.abs(lv[finite] - rv[finite])
                denom = np.maximum(np.maximum(np.abs(lv[finite]), np.abs(rv[finite])), 1.0)
                rel_diff = abs_diff / denom
                diag["max_abs_diff"] = max(float(diag["max_abs_diff"]), float(abs_diff.max(initial=0.0)))
                diag["max_rel_diff"] = max(float(diag["max_rel_diff"]), float(rel_diff.max(initial=0.0)))
                column_atol, column_rtol, tolerance_policy = incremental_feature_numeric_tolerance(str(column))
                if not np.allclose(
                    lv[finite],
                    rv[finite],
                    rtol=column_rtol,
                    atol=column_atol,
                    equal_nan=True,
                ):
                    diag.update({
                        "reason": "numeric_value_mismatch",
                        "mismatch_column": str(column),
                        "numeric_atol": float(column_atol),
                        "numeric_rtol": float(column_rtol),
                        "numeric_tolerance_policy": tolerance_policy,
                    })
                    return diag
        else:
            lv = lser[valid].map(str).to_numpy()
            rv = rser[valid].map(str).to_numpy()
            if not np.array_equal(lv, rv):
                diag.update({"reason": "nonnumeric_value_mismatch", "mismatch_column": str(column)})
                return diag
        diag["columns_checked"] = int(diag["columns_checked"]) + 1

    diag["ok"] = True
    diag["reason"] = "strict_overlap_parity_pass"
    return diag


def _record_incremental_feature_state(
    state: dict[str, Any],
    asset: str,
    record: dict[str, Any],
) -> None:
    safe_record = _json_safe_runtime_metadata(record)
    payload = {"created_at": utc_now(), "asset": asset, **dict(safe_record or {})}
    state.setdefault("incremental_shared_feature_cache_by_asset", {})[asset] = payload
    history = state.setdefault("incremental_shared_feature_cache_history", [])
    history.append(payload)
    state["incremental_shared_feature_cache_history"] = history[-300:]



def _synchronize_runtime_feature_state_after_full_build(
    *,
    asset: str,
    builder_edge: dict[str, Any],
    runtime: Any,
    bars_snapshot: pd.DataFrame,
    full_features: pd.DataFrame,
    live_event_times_snapshot: set[pd.Timestamp] | None,
    state: dict[str, Any],
    logger,
) -> dict[str, Any]:
    """Advance and verify the opaque runtime checkpoint to the full-build row."""
    edge_id = str(builder_edge.get("edge_id"))
    diag: dict[str, Any] = {
        "ok": False,
        "reason": "runtime_stateful_feature_api_not_available",
        "api_used": False,
        "parity": None,
    }
    last_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
    try:
        last_processed = ensure_utc_timestamp(last_raw) if last_raw else None
    except Exception:
        last_processed = None
    update, api_diag = call_runtime_incremental_feature_api(
        runtime=runtime,
        edge_id=edge_id,
        asset=asset,
        bars=bars_snapshot,
        live_event_times=set(live_event_times_snapshot or set()),
        last_processed=last_processed,
        dashboard_state=state,
        logger=logger,
        authoritative_features=full_features,
    )
    diag["runtime_api"] = api_diag
    if update is None:
        diag["reason"] = str(api_diag.get("reason") or diag["reason"])
        return diag

    full_norm = _normalized_datetime_frame(full_features)
    update_norm = _normalized_datetime_frame(update)
    if full_norm.empty or update_norm.empty:
        diag["reason"] = "state_sync_empty_full_or_runtime_update"
        _discard_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="features",
            logger=logger,
            reason=diag["reason"],
        )
        return diag
    common = set(full_norm["Datetime"]).intersection(set(update_norm["Datetime"]))
    if not common:
        diag["reason"] = "state_sync_runtime_update_has_no_full_build_timestamp_overlap"
        _discard_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="features",
            logger=logger,
            reason=diag["reason"],
        )
        return diag
    parity = _feature_parity_diagnostics(
        full_norm,
        update_norm,
        overlap_rows=min(INCREMENTAL_OVERLAP_CHECK_ROWS, len(common)),
    )
    diag.update({
        "api_used": True,
        "parity": parity,
        "ok": bool(parity.get("ok")),
        "reason": "full_build_runtime_state_checkpoint_verified" if parity.get("ok") else f"state_sync_{parity.get('reason')}",
    })
    if not diag["ok"]:
        _discard_runtime_owned_continuation_state(
            edge_id=edge_id,
            channel="features",
            logger=logger,
            reason=str(diag["reason"]),
        )
        logger.error(
            "LIVE_FULL_BUILD_STATE_SYNC_MISMATCH asset=%s edge=%s reason=%s mismatch_column=%s max_abs_diff=%s max_rel_diff=%s",
            asset,
            edge_id,
            diag["reason"],
            parity.get("mismatch_column"),
            parity.get("max_abs_diff"),
            parity.get("max_rel_diff"),
        )
    else:
        logger.info(
            "LIVE_FULL_BUILD_STATE_SYNC_VERIFIED asset=%s edge=%s api=%s overlap_rows=%s",
            asset,
            edge_id,
            api_diag.get("api_name"),
            parity.get("overlap_rows_checked"),
        )
    return diag

def _full_shared_feature_build(
    *,
    asset: str,
    builder_edge: dict[str, Any],
    runtime: Any,
    builder_fingerprint: str,
    bars_snapshot: pd.DataFrame,
    live_event_times_snapshot: set[pd.Timestamp] | None,
    cache_signature: dict[str, Any],
    state: dict[str, Any],
    logger,
    reason: str,
    prior_cache: dict[str, Any] | None = None,
    disable_incremental: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    builder_edge_id = str(builder_edge.get("edge_id"))
    builder_contract_source, builder_contract_identity = shared_feature_contract_identity(builder_edge, runtime)
    prior_incremental_disabled = bool((prior_cache or {}).get("incremental_disabled", False))
    prior_incremental_disabled_reason = str((prior_cache or {}).get("incremental_disabled_reason") or "")
    if prior_incremental_disabled:
        disable_incremental = True
    start = time.perf_counter()
    features = runtime.build_features(bars=bars_snapshot.copy(), drop_warmup_rows=False)
    if features is None or features.empty:
        raise RuntimeError("builder_returned_empty")

    audit_diag: dict[str, Any] | None = None
    if prior_cache and isinstance(prior_cache.get("features"), pd.DataFrame):
        audit_diag = _feature_parity_diagnostics(
            prior_cache["features"],
            features,
            overlap_rows=INCREMENTAL_OVERLAP_CHECK_ROWS,
        )
        if reason == "scheduled_research_hmm_refit_boundary_full_audit" and not audit_diag.get("ok"):
            disable_incremental = bool(INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE)
            logger.error(
                "LIVE_INCREMENTAL_FULL_AUDIT_MISMATCH asset=%s builder_edge=%s reason=%s mismatch_column=%s max_abs_diff=%s max_rel_diff=%s disable_incremental=%s",
                asset,
                builder_edge_id,
                audit_diag.get("reason"),
                audit_diag.get("mismatch_column"),
                audit_diag.get("max_abs_diff"),
                audit_diag.get("max_rel_diff"),
                disable_incremental,
            )

    state_sync_diag = _synchronize_runtime_feature_state_after_full_build(
        asset=asset,
        builder_edge=builder_edge,
        runtime=runtime,
        bars_snapshot=bars_snapshot,
        full_features=features,
        live_event_times_snapshot=live_event_times_snapshot,
        state=state,
        logger=logger,
    )
    if state_sync_diag.get("api_used") and not state_sync_diag.get("ok"):
        disable_incremental = bool(INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE)
    if disable_incremental and state_sync_diag.get("api_used") and not state_sync_diag.get("ok"):
        effective_disabled_reason = str(state_sync_diag.get("reason"))
    elif prior_incremental_disabled and prior_incremental_disabled_reason:
        effective_disabled_reason = prior_incremental_disabled_reason
    else:
        effective_disabled_reason = reason

    cache_record = {
        "success": True,
        "asset": asset,
        "created_at": utc_now(),
        "signature": cache_signature,
        "bar_content_hash": _stable_frame_hash(bars_snapshot, exclude_columns={"Closed_At"}),
        "builder_edge_id": builder_edge_id,
        "builder_fingerprint": builder_fingerprint,
        "builder_contract_identity_source": builder_contract_source,
        "builder_contract_identity": builder_contract_identity,
        "features": features.copy(),
        "bars": bars_snapshot.copy(),
        "bars_since_full_rebuild": 0,
        "full_rebuild_count": int((prior_cache or {}).get("full_rebuild_count", 0) or 0) + 1,
        "incremental_update_count": int((prior_cache or {}).get("incremental_update_count", 0) or 0),
        "stateful_continuation_count": int((prior_cache or {}).get("stateful_continuation_count", 0) or 0),
        "incremental_disabled": bool(disable_incremental),
        "incremental_disabled_reason": effective_disabled_reason if disable_incremental else None,
        "last_build_mode": "bootstrap_recovery_or_parity_audit_full",
        "last_build_reason": reason,
        "last_tail_rows": None,
        "last_parity": audit_diag,
        "last_state_checkpoint_sync": state_sync_diag,
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
    }
    _record_incremental_feature_state(
        state,
        asset,
        {
            "mode": "bootstrap_recovery_or_parity_audit_full",
            "reason": reason,
            "builder_edge_id": builder_edge_id,
            "builder_fingerprint": builder_fingerprint,
            "builder_contract_identity_source": builder_contract_source,
            "builder_contract_identity": builder_contract_identity,
            "rows": rows_len(features),
            "columns": len(features.columns),
            "elapsed_ms": elapsed_ms(start),
            "incremental_disabled": bool(disable_incremental),
            "full_audit": audit_diag,
            "state_checkpoint_sync": state_sync_diag,
            "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
        },
    )
    logger.info(
        "LIVE_SHARED_FEATURE_RECOVERY_BUILD_DONE asset=%s builder_edge=%s rows=%s columns=%s elapsed_ms=%.3f reason=%s stateful_continuation_disabled=%s cold_bootstrap_raw_bars=%s",
        asset,
        builder_edge_id,
        rows_len(features),
        len(features.columns),
        elapsed_ms(start),
        reason,
        disable_incremental,
        RESEARCH_COLD_BOOTSTRAP_RAW_BARS,
    )
    return features, cache_record


def _merge_runtime_stateful_feature_update(
    *,
    trusted_features: pd.DataFrame,
    runtime_update: pd.DataFrame,
    current_bars: pd.DataFrame,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Merge one runtime-owned update across an append or fixed-width window.

    The live runtime intentionally scores a moving raw-bar window.  Native
    feature checkpoints may retain a slightly longer restart tail than the
    dashboard's current 6,000-bar authority.  Rows that have already fallen off
    the *current bar window* are therefore neither historical insertions nor
    revisions and must be excluded before append/parity classification.

    Safety remains timestamp based: all overlapping rows inside the current
    authority window must match, and every genuinely new in-window timestamp
    must append strictly after the latest retained trusted timestamp.
    """
    trusted_all = _normalized_datetime_frame(trusted_features)
    update_all = _normalized_datetime_frame(runtime_update)
    bars = _normalized_datetime_frame(current_bars)
    diag: dict[str, Any] = {
        "ok": False,
        "reason": "empty_runtime_stateful_update",
        "trusted_rows": rows_len(trusted_all),
        "update_rows": rows_len(update_all),
        "current_bar_rows": rows_len(bars),
        "common_rows": 0,
        "new_rows": 0,
        "rolling_window_evicted_trusted_rows": 0,
        "ignored_runtime_history_rows": 0,
        "overlap_parity": None,
    }
    if trusted_all.empty or update_all.empty or bars.empty:
        return None, diag
    if set(trusted_all.columns) != set(update_all.columns):
        diag.update({
            "reason": "runtime_stateful_feature_schema_mismatch",
            "missing_from_update": sorted(set(trusted_all.columns) - set(update_all.columns))[:20],
            "extra_in_update": sorted(set(update_all.columns) - set(trusted_all.columns))[:20],
        })
        return None, diag

    current_times = set(bars["Datetime"].tolist())
    trusted = trusted_all[trusted_all["Datetime"].isin(current_times)].reset_index(drop=True)
    update = update_all[update_all["Datetime"].isin(current_times)].reset_index(drop=True)
    diag["rolling_window_evicted_trusted_rows"] = int(len(trusted_all) - len(trusted))
    diag["ignored_runtime_history_rows"] = int(len(update_all) - len(update))
    diag["trusted_rows_in_current_window"] = rows_len(trusted)
    diag["update_rows_in_current_window"] = rows_len(update)
    if update.empty:
        diag["reason"] = "runtime_stateful_update_has_no_current_window_rows"
        return None, diag

    trusted_idx = trusted.set_index("Datetime", drop=False)
    update_idx = update.set_index("Datetime", drop=False)
    common = trusted_idx.index.intersection(update_idx.index).sort_values()
    new_times = update_idx.index.difference(trusted_idx.index).sort_values()
    diag["common_rows"] = int(len(common))
    diag["new_rows"] = int(len(new_times))

    if len(common) > 0:
        overlap_rows = min(INCREMENTAL_OVERLAP_CHECK_ROWS, len(common))
        parity = _feature_parity_diagnostics(trusted, update, overlap_rows=overlap_rows)
        diag["overlap_parity"] = parity
        if INCREMENTAL_PARITY_REQUIRED and not parity.get("ok"):
            diag.update({
                "reason": f"runtime_stateful_overlap_{parity.get('reason')}",
                "mismatch_column": parity.get("mismatch_column"),
                "max_abs_diff": parity.get("max_abs_diff"),
                "max_rel_diff": parity.get("max_rel_diff"),
            })
            return None, diag

    if len(new_times) <= 0:
        diag["reason"] = "runtime_stateful_update_contains_no_new_timestamp"
        return None, diag
    latest_trusted = trusted_idx.index.max() if not trusted_idx.empty else None
    if latest_trusted is not None and any(ts <= latest_trusted for ts in new_times):
        diag["reason"] = "runtime_stateful_update_attempted_nonappend_historical_insertion"
        diag["nonappend_timestamps"] = [safe_iso(ts) for ts in new_times if ts <= latest_trusted][:10]
        return None, diag

    new_rows = update_idx.loc[new_times].reset_index(drop=True)
    merged = pd.concat([trusted, new_rows], ignore_index=True, sort=False)
    merged = _normalized_datetime_frame(merged)
    merged = merged[merged["Datetime"].isin(current_times)].reset_index(drop=True)
    if merged.empty:
        diag["reason"] = "runtime_stateful_merge_empty"
        return None, diag
    latest_bar = bars["Datetime"].max()
    merged_times = set(merged["Datetime"].tolist())
    if latest_bar not in merged_times:
        diag["reason"] = "newest_bar_missing_from_runtime_stateful_features"
        return None, diag

    update_current_times = set(update["Datetime"].tolist())
    missing_runtime_times = sorted(current_times.difference(update_current_times))
    # Warm-up rows may legitimately have no engineered feature output.  Missing
    # rows are accepted only when they are older than the earliest runtime row.
    earliest_runtime = update["Datetime"].min()
    material_missing = [ts for ts in missing_runtime_times if ts >= earliest_runtime]
    if material_missing:
        diag["reason"] = "runtime_stateful_update_missing_current_window_timestamps"
        diag["missing_current_window_timestamps"] = [safe_iso(ts) for ts in material_missing[:10]]
        return None, diag

    diag.update({
        "ok": True,
        "reason": "runtime_owned_stateful_rolling_append_verified",
        "merged_rows": rows_len(merged),
        "latest_feature_timestamp": safe_iso(merged["Datetime"].max()),
        "fixed_width_eviction_accepted": bool(
            diag["rolling_window_evicted_trusted_rows"]
            or diag["ignored_runtime_history_rows"]
        ),
    })
    return merged, diag


def _try_verified_incremental_shared_features(
    *,
    asset: str,
    builder_edge: dict[str, Any],
    runtime: Any,
    builder_fingerprint: str,
    bars_snapshot: pd.DataFrame,
    live_event_times_snapshot: set[pd.Timestamp] | None,
    cache_signature: dict[str, Any],
    cached: dict[str, Any],
    state: dict[str, Any],
    logger,
) -> tuple[pd.DataFrame | None, dict[str, Any] | None, str]:
    """Continue shared features through a runtime-owned stateful API only."""
    builder_edge_id = str(builder_edge.get("edge_id"))
    if not INCREMENTAL_SHARED_FEATURES_ENABLED or not RUNTIME_STATEFUL_CONTINUATION_ENABLED:
        return None, None, "runtime_stateful_continuation_disabled_by_settings"
    if cached.get("incremental_disabled"):
        return None, None, f"asset_incremental_disabled:{cached.get('incremental_disabled_reason')}"
    if str(cached.get("builder_edge_id") or "") != builder_edge_id:
        return None, None, "canonical_builder_changed"
    if str(cached.get("builder_fingerprint") or "") != builder_fingerprint:
        return None, None, "canonical_builder_fingerprint_changed"
    cached_bars = cached.get("bars")
    cached_features = cached.get("features")
    if not isinstance(cached_bars, pd.DataFrame) or not isinstance(cached_features, pd.DataFrame):
        return None, None, "trusted_cache_missing_bars_or_features"

    raw_diag = _raw_bar_overlap_diagnostics(cached_bars, bars_snapshot)
    if not raw_diag.get("ok"):
        return None, None, str(raw_diag.get("reason"))
    new_bar_count = int(raw_diag.get("new_rows", 0) or 0)
    bars_since_full = int(cached.get("bars_since_full_rebuild", 0) or 0) + new_bar_count

    # The research HMM refits every 1,000 bars. Audit the complete runtime path at
    # that same evidence-based boundary rather than every arbitrary 25 bars.
    if bars_since_full >= INCREMENTAL_FULL_REBUILD_EVERY_N_BARS:
        return None, None, "scheduled_research_hmm_refit_boundary_full_audit"

    last_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(builder_edge_id)
    try:
        last_processed = ensure_utc_timestamp(last_raw) if last_raw else None
    except Exception:
        last_processed = None
    update, api_diag = call_runtime_incremental_feature_api(
        runtime=runtime,
        edge_id=builder_edge_id,
        asset=asset,
        bars=bars_snapshot,
        live_event_times=set(live_event_times_snapshot or set()),
        last_processed=last_processed,
        dashboard_state=state,
        logger=logger,
    )
    if update is None:
        reason = str(api_diag.get("reason") or "runtime_stateful_feature_api_unavailable")
        _record_incremental_feature_state(
            state,
            asset,
            {
                "mode": "recovery_full_required",
                "reason": reason,
                "builder_edge_id": builder_edge_id,
                "builder_fingerprint": builder_fingerprint,
                "new_bar_count": new_bar_count,
                "runtime_api": api_diag,
                "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
            },
        )
        return None, None, reason

    merged, merge_diag = _merge_runtime_stateful_feature_update(
        trusted_features=cached_features,
        runtime_update=update,
        current_bars=bars_snapshot,
    )
    if merged is None:
        reason = str(merge_diag.get("reason") or "runtime_stateful_merge_failed")
        _discard_runtime_owned_continuation_state(
            edge_id=builder_edge_id,
            channel="features",
            logger=logger,
            reason=reason,
        )
        _record_incremental_feature_state(
            state,
            asset,
            {
                "mode": "recovery_full_required",
                "reason": reason,
                "builder_edge_id": builder_edge_id,
                "builder_fingerprint": builder_fingerprint,
                "new_bar_count": new_bar_count,
                "runtime_api": api_diag,
                "merge_diagnostics": merge_diag,
                "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
            },
        )
        logger.error(
            "LIVE_STATEFUL_SHARED_FEATURES_REJECT asset=%s builder_edge=%s reason=%s mismatch_column=%s fallback=evidence_sized_recovery_build",
            asset,
            builder_edge_id,
            reason,
            merge_diag.get("mismatch_column"),
        )
        return None, None, reason

    builder_contract_source, builder_contract_identity = shared_feature_contract_identity(builder_edge, runtime)
    cache_record = dict(cached)
    cache_record.update({
        "success": True,
        "asset": asset,
        "created_at": utc_now(),
        "signature": cache_signature,
        "bar_content_hash": _stable_frame_hash(bars_snapshot, exclude_columns={"Closed_At"}),
        "builder_edge_id": builder_edge_id,
        "builder_fingerprint": builder_fingerprint,
        "builder_contract_identity_source": builder_contract_source,
        "builder_contract_identity": builder_contract_identity,
        "features": merged.copy(),
        "bars": bars_snapshot.copy(),
        "bars_since_full_rebuild": bars_since_full,
        "incremental_update_count": int(cached.get("incremental_update_count", 0) or 0) + 1,
        "stateful_continuation_count": int(cached.get("stateful_continuation_count", 0) or 0) + 1,
        "last_build_mode": "runtime_owned_stateful_continuation",
        "last_build_reason": "runtime_owned_stateful_append_verified",
        "last_tail_rows": None,
        "last_parity": merge_diag,
        "last_runtime_api": api_diag,
        "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
    })
    _record_incremental_feature_state(
        state,
        asset,
        {
            "mode": "runtime_owned_stateful_continuation",
            "reason": "runtime_owned_stateful_append_verified",
            "builder_edge_id": builder_edge_id,
            "builder_fingerprint": builder_fingerprint,
            "new_bar_count": new_bar_count,
            "bars_since_full_rebuild": bars_since_full,
            "rows": rows_len(merged),
            "columns": len(merged.columns),
            "runtime_api": api_diag,
            "merge_diagnostics": merge_diag,
            "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
        },
    )
    logger.info(
        "LIVE_STATEFUL_SHARED_FEATURES_ACCEPT asset=%s builder_edge=%s new_bars=%s bars_since_full_audit=%s common_rows=%s appended_rows=%s rows=%s api=%s",
        asset,
        builder_edge_id,
        new_bar_count,
        bars_since_full,
        merge_diag.get("common_rows"),
        merge_diag.get("new_rows"),
        rows_len(merged),
        api_diag.get("api_name"),
    )
    return merged, cache_record, "runtime_owned_stateful_append_verified"


def build_shared_asset_runtime_features(
    *,
    asset: str,
    asset_edges: list[dict[str, Any]],
    bars_snapshot: pd.DataFrame,
    live_event_times_snapshot: set[pd.Timestamp] | None,
    state: dict[str, Any],
    logger,
) -> tuple[pd.DataFrame | None, str | None]:
    """Build or incrementally refresh one canonical runtime feature table.

    The normal hot path uses a runtime-owned stateful continuation API and
    persists its opaque state. The dashboard never engineers a feature itself.
    Absorption/custom families remain on their edge-local path. A full raw-history
    runtime build is retained only for bootstrap, recovery and the 1,000-bar HMM
    refit-boundary parity audit.
    """
    if not SHARED_FEATURE_CACHE_ENABLED:
        logger.info("LIVE_SHARED_FEATURE_CACHE_BYPASS asset=%s reason=disabled", asset)
        return None, None
    eligible_edges = [edge for edge in (asset_edges or []) if not edge_family_is_incremental_excluded(edge)]
    if not eligible_edges:
        logger.info(
            "LIVE_SHARED_FEATURE_CACHE_BYPASS asset=%s reason=all_edges_in_excluded_families excluded_families=%s",
            asset,
            sorted(INCREMENTAL_EXCLUDED_FAMILIES),
        )
        return None, None
    if bars_snapshot is None or bars_snapshot.empty or not live_event_times_snapshot:
        logger.info(
            "LIVE_SHARED_FEATURE_CACHE_BYPASS asset=%s reason=no_bars_or_live_times bars=%s live_event_times=%s",
            asset,
            rows_len(bars_snapshot),
            rows_len(list(live_event_times_snapshot or [])),
        )
        return None, None

    cache_signature = shared_feature_prewarm_cache_signature(asset, bars_snapshot)
    bar_content_hash = _stable_frame_hash(bars_snapshot, exclude_columns={"Closed_At"})
    with _SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK:
        cached_runtime_features = _SHARED_ASSET_RUNTIME_FEATURE_CACHE.get(asset)
        if cached_runtime_features:
            cached_features = cached_runtime_features.get("features")
            if (
                isinstance(cached_features, pd.DataFrame)
                and not cached_features.empty
                and cached_runtime_features.get("bar_content_hash") == bar_content_hash
            ):
                cached_runtime_features["signature"] = cache_signature
                builder_edge_id = str(cached_runtime_features.get("builder_edge_id") or "") or None
                logger.info(
                    "LIVE_SHARED_FEATURE_CACHE_CONTENT_HIT asset=%s builder_edge=%s rows=%s columns=%s cached_at=%s reason=identical_bar_content",
                    asset,
                    builder_edge_id,
                    rows_len(cached_features),
                    len(cached_features.columns),
                    cached_runtime_features.get("created_at"),
                )
                return cached_features.copy(), builder_edge_id

    builder_candidates = ordered_shared_feature_builder_edges(
        asset_edges=eligible_edges,
        live_event_times_snapshot=live_event_times_snapshot,
        state=state,
    )
    if not builder_candidates:
        logger.info("LIVE_SHARED_FEATURE_CACHE_BYPASS asset=%s reason=no_eligible_edge_has_fresh_pending_live_timestamp", asset)
        return None, None

    cached_builder_id = str((cached_runtime_features or {}).get("builder_edge_id") or "")
    if cached_builder_id:
        builder_candidates = sorted(
            builder_candidates,
            key=lambda edge: (0 if str(edge.get("edge_id")) == cached_builder_id else 1, shared_feature_builder_priority(edge)),
        )
    logger.info(
        "LIVE_SHARED_FEATURE_CACHE_BUILDER_CANDIDATES asset=%s candidates=%s excluded_families=%s stateful_continuation_enabled=%s legacy_tail_search_enabled=False overlap_rows=%s full_audit_every=%s cold_bootstrap_raw_bars=%s",
        asset,
        [str(edge.get("edge_id")) for edge in builder_candidates],
        sorted(INCREMENTAL_EXCLUDED_FAMILIES),
        bool(INCREMENTAL_SHARED_FEATURES_ENABLED and RUNTIME_STATEFUL_CONTINUATION_ENABLED),
        INCREMENTAL_OVERLAP_CHECK_ROWS,
        INCREMENTAL_FULL_REBUILD_EVERY_N_BARS,
        RESEARCH_COLD_BOOTSTRAP_RAW_BARS,
    )

    last_error: str | None = None
    for builder_edge in builder_candidates:
        builder_edge_id = str(builder_edge.get("edge_id"))
        try:
            runtime = get_runtime(builder_edge)
            builder_fingerprint = runtime_build_features_fingerprint(runtime)
            prior_cache = cached_runtime_features if isinstance(cached_runtime_features, dict) else None

            incremental_features: pd.DataFrame | None = None
            incremental_record: dict[str, Any] | None = None
            incremental_reason = "no_prior_trusted_cache"
            if prior_cache:
                incremental_features, incremental_record, incremental_reason = _try_verified_incremental_shared_features(
                    asset=asset,
                    builder_edge=builder_edge,
                    runtime=runtime,
                    builder_fingerprint=builder_fingerprint,
                    bars_snapshot=bars_snapshot,
                    live_event_times_snapshot=live_event_times_snapshot,
                    cache_signature=cache_signature,
                    cached=prior_cache,
                    state=state,
                    logger=logger,
                )
            if incremental_features is not None and incremental_record is not None:
                with _SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK:
                    _SHARED_ASSET_RUNTIME_FEATURE_CACHE[asset] = incremental_record
                return incremental_features.copy(), builder_edge_id

            parity_failure_reasons = {
                "runtime_stateful_feature_schema_mismatch",
                "runtime_stateful_overlap_nan_mask_mismatch",
                "runtime_stateful_overlap_finite_mask_mismatch",
                "runtime_stateful_overlap_numeric_value_mismatch",
                "runtime_stateful_overlap_nonnumeric_value_mismatch",
                "runtime_stateful_update_attempted_nonappend_historical_insertion",
                "historical_raw_bar_overlap_changed",
            }
            disable_incremental = bool(
                bool((prior_cache or {}).get("incremental_disabled", False))
                or (
                    incremental_reason in parity_failure_reasons
                    and INCREMENTAL_DISABLE_ASSET_ON_PARITY_FAILURE
                )
            )
            full_reason = incremental_reason
            if full_reason == "no_prior_trusted_cache":
                full_reason = "initial_research_state_bootstrap_full_build"
            features, cache_record = _full_shared_feature_build(
                asset=asset,
                builder_edge=builder_edge,
                runtime=runtime,
                builder_fingerprint=builder_fingerprint,
                bars_snapshot=bars_snapshot,
                live_event_times_snapshot=live_event_times_snapshot,
                cache_signature=cache_signature,
                state=state,
                logger=logger,
                reason=full_reason,
                prior_cache=prior_cache,
                disable_incremental=disable_incremental,
            )
            with _SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK:
                _SHARED_ASSET_RUNTIME_FEATURE_CACHE[asset] = cache_record
            return features.copy(), builder_edge_id
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "LIVE_SHARED_FEATURE_CACHE_BUILDER_REJECTED asset=%s builder_edge=%s error=%s trying_next_builder=True",
                asset,
                builder_edge_id,
                exc,
            )
            logger.warning(traceback.format_exc())

    logger.warning(
        "LIVE_SHARED_FEATURE_CACHE_BUILD_FAILED asset=%s builders_tried=%s last_error=%s fallback=edge_local_build_features",
        asset,
        [str(edge.get("edge_id")) for edge in builder_candidates],
        last_error,
    )
    return None, None


def shared_feature_cache_coverage_for_edge(
    *,
    edge: dict[str, Any],
    shared_features_snapshot: pd.DataFrame | None,
    logger,
) -> tuple[bool, str, int, list[str]]:
    """Return whether an edge can safely reuse the shared feature snapshot."""
    edge_id = str(edge.get("edge_id", ""))
    asset = str(edge.get("asset") or (edge_id.split("/", 1)[0] if "/" in edge_id else ""))
    if shared_features_snapshot is None or shared_features_snapshot.empty:
        return False, "shared_feature_snapshot_empty", 0, []

    try:
        runtime = get_runtime(edge)
        if STRICT_SHARED_FEATURE_CONTRACT_ENABLED:
            with _SHARED_ASSET_RUNTIME_FEATURE_CACHE_LOCK:
                cache_record = dict(_SHARED_ASSET_RUNTIME_FEATURE_CACHE.get(asset) or {})
            builder_identity = str(cache_record.get("builder_contract_identity") or cache_record.get("builder_fingerprint") or "")
            _, target_identity = shared_feature_contract_identity(edge, runtime)
            if not builder_identity:
                return False, "missing_builder_feature_contract_identity", 0, []
            if target_identity != builder_identity:
                return False, "shared_feature_contract_mismatch", 0, []
        expected = runtime_expected_feature_names(runtime)
    except Exception as exc:
        logger.warning(
            "LIVE_EDGE_SCORING_ORDER_COVERAGE_WARN edge=%s reason=runtime_schema_or_contract_unavailable error=%s",
            edge_id,
            exc,
        )
        return False, "runtime_schema_or_contract_unavailable", 0, []

    if expected:
        missing = [name for name in expected if name not in shared_features_snapshot.columns]
        if missing:
            return False, "missing_schema_features", len(missing), missing[:10]
    return True, "shared_feature_snapshot_contract_verified", 0, []

def order_edges_for_live_scoring(
    *,
    asset: str,
    asset_edges: list[dict[str, Any]],
    shared_features_snapshot: pd.DataFrame | None,
    shared_features_builder_edge_id: str | None,
    live_event_times_snapshot: set[pd.Timestamp] | None,
    state: dict[str, Any],
    logger,
) -> list[dict[str, Any]]:
    """Score fast shared-cache edges before slow edge-local fallback edges.

    Stage 6.5 latency scheduling only. This does not change feature math, event
    logic, model scoring, thresholds, stale gates, state/cursor semantics, or
    execution handoff. It only changes the order in which edges are attempted
    inside an asset batch so still-fresh cache-hit edges are not delayed behind
    known fallback-heavy runtimes.
    """
    if not asset_edges:
        return []

    decorated: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    cache_hit_edges: list[str] = []
    fallback_edges: list[str] = []
    no_fresh_pending_edges: list[str] = []

    for original_index, edge in enumerate(asset_edges):
        edge_id = str(edge.get("edge_id", ""))
        has_fresh_pending = edge_has_fresh_pending_live_timestamp(edge, live_event_times_snapshot, state)
        cache_ok, cache_reason, missing_count, sample_missing = shared_feature_cache_coverage_for_edge(
            edge=edge,
            shared_features_snapshot=shared_features_snapshot,
            logger=logger,
        )

        if has_fresh_pending and cache_ok:
            group = 0
            cache_hit_edges.append(edge_id)
            category = "fresh_cache_hit"
        elif (not has_fresh_pending) and cache_ok:
            # These should skip quickly via the existing pre-runtime guard; keep
            # them ahead of fallback-heavy edges because they cost milliseconds.
            group = 1
            no_fresh_pending_edges.append(edge_id)
            category = "cache_hit_no_fresh_pending"
        elif not has_fresh_pending:
            group = 2
            no_fresh_pending_edges.append(edge_id)
            category = "no_fresh_pending"
        else:
            group = 3
            fallback_edges.append(edge_id)
            category = "fresh_edge_local_fallback"

        decorated.append(
            (
                group,
                original_index,
                edge,
                {
                    "edge_id": edge_id,
                    "category": category,
                    "cache_ok": cache_ok,
                    "cache_reason": cache_reason,
                    "missing_count": int(missing_count),
                    "sample_missing": sample_missing,
                    "has_fresh_pending": bool(has_fresh_pending),
                },
            )
        )

    decorated.sort(key=lambda item: (item[0], item[1]))
    ordered_edges = [item[2] for item in decorated]
    ordered_edge_ids = [str(edge.get("edge_id", "")) for edge in ordered_edges]
    details = [item[3] for item in decorated]

    logger.info(
        "LIVE_EDGE_SCORING_ORDER asset=%s policy=shared_cache_hit_first_edge_local_fallback_last "
        "builder_edge=%s ordered_edges=%s cache_hit_edges=%s fallback_edges=%s no_fresh_pending_edges=%s details=%s",
        asset,
        shared_features_builder_edge_id,
        ordered_edge_ids,
        cache_hit_edges,
        fallback_edges,
        no_fresh_pending_edges,
        details,
    )
    return ordered_edges

def live_probability_afml_gate(
    calibrated_probability: float,
    final_live_threshold: float,
    afml_bet_size: float,
) -> tuple[bool, bool, bool, str, str]:
    """Apply the allocator probability gate and positive-AFML execution gate."""
    probability_passes = bool(
        math.isfinite(float(calibrated_probability))
        and float(calibrated_probability) >= float(final_live_threshold)
    )
    afml_passes = bool(math.isfinite(float(afml_bet_size)) and float(afml_bet_size) > 0.0)
    approved = bool(
        probability_passes
        and (afml_passes or not REQUIRE_POSITIVE_AFML_FOR_APPROVAL)
    )
    if not probability_passes:
        return approved, probability_passes, afml_passes, "blocked_probability_threshold", "calibrated_probability_below_allocator_final_threshold"
    if REQUIRE_POSITIVE_AFML_FOR_APPROVAL and not afml_passes:
        return approved, probability_passes, afml_passes, "blocked_nonpositive_afml_bet_size", "calibrated_probability_passed_but_afml_bet_size_not_positive"
    return approved, probability_passes, afml_passes, "approved_signal", "calibrated_probability_and_positive_afml_passed_allocator_live_gates"


def score_edge(
    edge: dict[str, Any],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state: dict[str, Any],
    logger,
    *,
    bars_snapshot: pd.DataFrame | None = None,
    live_event_times_snapshot: set[pd.Timestamp] | None = None,
    shared_features_snapshot: pd.DataFrame | None = None,
    shared_features_builder_edge_id: str | None = None,
) -> list[DashboardSignalRow]:
    edge_start_perf = time.perf_counter()
    edge_id = str(edge["edge_id"])
    asset = str(edge["asset"])
    continuity = ((state.get("asset_continuity") or {}).get(asset) or {})
    if not continuity_ok(asset, state):
        logger.warning("RUNTIME_SCORE_BLOCKED_CONTINUITY edge=%s asset=%s status=%s reason=%s", edge_id, asset, continuity.get("status"), continuity.get("reason"))
        return []

    policy = bar_policy["bar_policies"][asset]
    stage_perf = time.perf_counter()
    if bars_snapshot is None:
        bars = combined_bars_for_asset(asset, policy)
        cache_extra = "cache_hit=False"
    else:
        # Stage 3 optimization: reuse the asset-level bars snapshot built once
        # for this scoring batch. Copy per edge so a runtime cannot mutate the
        # shared snapshot for other edges. This preserves the same data and gates
        # while avoiding repeated seed/live CSV reads and standardization.
        bars = bars_snapshot.copy()
        cache_extra = "cache_hit=True"
    log_latency_stage(
        logger,
        edge_id=edge_id,
        asset=asset,
        stage="combined_bars_for_asset",
        start_perf=stage_perf,
        edge_start_perf=edge_start_perf,
        live_event_times=None,
        rows=bars,
        extra=cache_extra,
    )
    if bars.empty or len(bars) < MIN_LIVE_BARS_FOR_RUNTIME:
        logger.info("RUNTIME_SKIP edge=%s asset=%s combined_bars=%s", edge_id, asset, len(bars))
        return []

    stage_perf = time.perf_counter()
    if live_event_times_snapshot is None:
        live_event_times = latest_live_executable_timestamps(asset)
        live_times_extra = "cache_hit=False"
    else:
        # Copy the immutable timestamp set for per-edge diagnostics/gates while
        # preserving the exact executable-live timestamp snapshot for the batch.
        live_event_times = set(live_event_times_snapshot)
        live_times_extra = "cache_hit=True"
    log_latency_stage(
        logger,
        edge_id=edge_id,
        asset=asset,
        stage="latest_live_executable_timestamps",
        start_perf=stage_perf,
        edge_start_perf=edge_start_perf,
        live_event_times=live_event_times,
        rows=list(live_event_times or []),
        extra=live_times_extra,
    )
    if not live_event_times:
        return []

    last_processed_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
    last_processed = ensure_utc_timestamp(last_processed_raw) if last_processed_raw else None
    if last_processed is not None and pd.isna(last_processed):
        last_processed = None

    stage_perf = time.perf_counter()
    skip_pre_runtime, pre_runtime_guard_diag = pre_runtime_live_event_guard(
        edge_id=edge_id,
        asset=asset,
        live_event_times=live_event_times,
        last_processed=last_processed,
        state=state,
        logger=logger,
    )
    log_latency_stage(
        logger,
        edge_id=edge_id,
        asset=asset,
        stage="pre_runtime_live_event_guard",
        start_perf=stage_perf,
        edge_start_perf=edge_start_perf,
        live_event_times=live_event_times,
        rows=list(live_timestamps_after_cursor(live_event_times, last_processed)),
        extra=(
            f"guard_reason={pre_runtime_guard_diag.get('reason')} "
            f"pending_live_timestamps={pre_runtime_guard_diag.get('pending_live_timestamps_count')} "
            f"latest_pending_live_timestamp={pre_runtime_guard_diag.get('latest_pending_live_timestamp')} "
            f"latest_pending_live_age_seconds={pre_runtime_guard_diag.get('latest_pending_live_age_seconds')} "
            f"cursor_advanced={pre_runtime_guard_diag.get('cursor_advanced')} "
            f"cursor_advanced_to={pre_runtime_guard_diag.get('cursor_advanced_to')}"
        ),
    )
    if skip_pre_runtime:
        if LATENCY_STAGE_LOGGING_ENABLED:
            logger.info(
                "LIVE_LATENCY_EDGE_TOTAL edge=%s asset=%s total_ms=%.3f rows=0 latest_live_age_seconds=%s signals=0 reason=%s",
                edge_id,
                asset,
                elapsed_ms(edge_start_perf),
                latest_live_age_seconds(live_event_times),
                pre_runtime_guard_diag.get("reason"),
            )
        return []

    stage_perf = time.perf_counter()
    runtime = get_runtime(edge)
    log_latency_stage(
        logger,
        edge_id=edge_id,
        asset=asset,
        stage="get_runtime",
        start_perf=stage_perf,
        edge_start_perf=edge_start_perf,
        live_event_times=live_event_times,
        rows=0,
    )
    runtime_manifest: dict[str, Any] = {}
    rows: list[DashboardSignalRow] = []
    try:
        logger.info(
            "RUNTIME_SCORE_START edge=%s asset=%s bars=%d recovery_memory_limit=%d hot_path=runtime_owned_stateful_continuation cold_bootstrap_raw_bars=%d",
            edge_id,
            asset,
            len(bars),
            MAX_COMBINED_BARS_FOR_RUNTIME,
            RESEARCH_COLD_BOOTSTRAP_RAW_BARS,
        )
        log_runtime_stage_datetime_diagnostics(logger, edge_id, "combined_bars_input", bars, live_event_times, last_processed=last_processed)

        stage_perf = time.perf_counter()
        features, shared_feature_reason = prepare_shared_features_for_edge(
            edge=edge,
            runtime=runtime,
            shared_features_snapshot=shared_features_snapshot,
            logger=logger,
        )
        shared_feature_cache_used = features is not None
        incremental_feature_diag: dict[str, Any] = {
            "api_used": False,
            "api_name": None,
            "reason": "not_attempted_shared_feature_cache_available" if shared_feature_cache_used else "not_attempted",
        }
        if features is None:
            features, incremental_feature_diag = call_runtime_incremental_feature_api(
                runtime=runtime,
                edge_id=edge_id,
                asset=asset,
                bars=bars,
                live_event_times=live_event_times,
                last_processed=last_processed,
                dashboard_state=state,
                logger=logger,
            )
        incremental_feature_api_used = bool(incremental_feature_diag.get("api_used"))
        feature_cache_extra = (
            f"cache_hit=True builder_edge={shared_features_builder_edge_id} reason={shared_feature_reason}"
            if shared_feature_cache_used
            else (
                f"cache_hit=False incremental_api_used=True api={incremental_feature_diag.get('api_name')} "
                f"reason={incremental_feature_diag.get('reason')}"
                if incremental_feature_api_used
                else f"cache_hit=False builder_edge={shared_features_builder_edge_id} "
                     f"reason={shared_feature_reason} incremental_reason={incremental_feature_diag.get('reason')}"
            )
        )
        if features is None:
            features = runtime.build_features(bars=bars, drop_warmup_rows=False)
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="runtime_build_features",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=features,
            extra=feature_cache_extra,
        )
        log_runtime_stage_datetime_diagnostics(logger, edge_id, "features_after_build", features, live_event_times, last_processed=last_processed)

        stage_perf = time.perf_counter()
        try:
            events, live_event_api_diag = generate_candidate_events_with_live_api_fallback(
                runtime=runtime,
                edge_id=edge_id,
                asset=asset,
                features=features,
                live_event_times=live_event_times,
                last_processed=last_processed,
                dashboard_state=state,
                logger=logger,
            )
            event_cache_extra = (
                f"feature_cache_hit={shared_feature_cache_used} builder_edge={shared_features_builder_edge_id} "
                f"live_event_api_used={live_event_api_diag.get('api_used')} "
                f"live_event_api={live_event_api_diag.get('api_name')} "
                f"live_event_reason={live_event_api_diag.get('reason')} "
                f"candidate_times={live_event_api_diag.get('candidate_times')}"
                if shared_feature_cache_used
                else (
                    f"feature_cache_hit=False live_event_api_used={live_event_api_diag.get('api_used')} "
                    f"live_event_api={live_event_api_diag.get('api_name')} "
                    f"live_event_reason={live_event_api_diag.get('reason')} "
                    f"candidate_times={live_event_api_diag.get('candidate_times')}"
                )
            )
        except Exception as exc:
            if not shared_feature_cache_used:
                raise

            logger.warning(
                "LIVE_SHARED_FEATURE_CACHE_EVENT_FALLBACK edge=%s asset=%s builder_edge=%s "
                "reason=generate_candidate_events_failed_on_shared_features error=%s",
                edge_id,
                asset,
                shared_features_builder_edge_id,
                exc,
            )
            logger.warning(traceback.format_exc())

            retry_feature_perf = time.perf_counter()
            features, incremental_retry_diag = call_runtime_incremental_feature_api(
                runtime=runtime,
                edge_id=edge_id,
                asset=asset,
                bars=bars,
                live_event_times=live_event_times,
                last_processed=last_processed,
                dashboard_state=state,
                logger=logger,
            )
            if features is None:
                features = runtime.build_features(bars=bars, drop_warmup_rows=False)
            log_latency_stage(
                logger,
                edge_id=edge_id,
                asset=asset,
                stage="runtime_build_features_retry_edge_local_after_event_fallback",
                start_perf=retry_feature_perf,
                edge_start_perf=edge_start_perf,
                live_event_times=live_event_times,
                rows=features,
                extra=(
                    f"cache_hit=False fallback_from_shared_builder={shared_features_builder_edge_id} "
                    "reason=event_generation_required_columns_missing_or_incompatible "
                    f"incremental_api_used={incremental_retry_diag.get('api_used')} "
                    f"incremental_api={incremental_retry_diag.get('api_name')} "
                    f"incremental_reason={incremental_retry_diag.get('reason')}"
                ),
            )
            log_runtime_stage_datetime_diagnostics(
                logger,
                edge_id,
                "features_after_shared_cache_event_fallback_rebuild",
                features,
                live_event_times,
                last_processed=last_processed,
            )

            retry_event_perf = time.perf_counter()
            events, live_event_retry_diag = generate_candidate_events_with_live_api_fallback(
                runtime=runtime,
                edge_id=edge_id,
                asset=asset,
                features=features,
                live_event_times=live_event_times,
                last_processed=last_processed,
                dashboard_state=state,
                logger=logger,
            )
            log_latency_stage(
                logger,
                edge_id=edge_id,
                asset=asset,
                stage="runtime_generate_candidate_events_retry_edge_local_after_event_fallback",
                start_perf=retry_event_perf,
                edge_start_perf=edge_start_perf,
                live_event_times=live_event_times,
                rows=events,
                extra=(
                    f"fallback_from_shared_builder={shared_features_builder_edge_id} "
                    "reason=event_generation_required_columns_missing_or_incompatible "
                    f"live_event_api_used={live_event_retry_diag.get('api_used')} "
                    f"live_event_api={live_event_retry_diag.get('api_name')} "
                    f"live_event_reason={live_event_retry_diag.get('reason')} "
                    f"candidate_times={live_event_retry_diag.get('candidate_times')}"
                ),
            )
            event_cache_extra = (
                f"feature_cache_hit=False fallback_from_shared_builder={shared_features_builder_edge_id} "
                "reason=event_generation_required_columns_missing_or_incompatible "
                f"live_event_api_used={live_event_retry_diag.get('api_used')} "
                f"live_event_api={live_event_retry_diag.get('api_name')} "
                f"live_event_reason={live_event_retry_diag.get('reason')} "
                f"candidate_times={live_event_retry_diag.get('candidate_times')}"
            )

        candidate_confirmation_diag: dict[str, Any] = {
            "enabled": bool(CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME),
            "confirmed": False,
            "reason": "no_current_candidate_or_already_full_edge_authority",
        }
        current_candidate_times = set(
            candidate_live_times_for_runtime(live_event_times, last_processed)
        )
        current_fast_identity = _candidate_identity_rows(events, current_candidate_times)
        live_event_api_used_for_confirmation = bool(
            (live_event_api_diag if 'live_event_api_diag' in locals() else {}).get("api_used")
            or (live_event_retry_diag if 'live_event_retry_diag' in locals() else {}).get("api_used")
        )
        candidate_source_used_incremental_or_shared = bool(
            shared_feature_cache_used
            or incremental_feature_api_used
            or live_event_api_used_for_confirmation
        )
        _event_diag_for_confirmation = (
            live_event_retry_diag if 'live_event_retry_diag' in locals() and live_event_retry_diag.get("api_used")
            else live_event_api_diag if 'live_event_api_diag' in locals()
            else {}
        )
        _event_api_metadata = dict((_event_diag_for_confirmation or {}).get("api_metadata") or {})
        native_event_already_full_confirmed = bool(
            _event_api_metadata.get("candidate_full_authority_confirmed") is True
            and int(_event_api_metadata.get("full_candidate_confirmations") or 0) > 0
        )
        if native_event_already_full_confirmed:
            candidate_confirmation_diag = {
                "enabled": True,
                "confirmed": True,
                "identity_match": True,
                "reason": "native_event_engine_already_confirmed_against_full_event_authority",
                "fast_candidate_rows": int(len(current_fast_identity)),
                "full_candidate_rows": int(len(current_fast_identity)),
                "elapsed_ms": 0.0,
            }
        if (
            CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME
            and candidate_source_used_incremental_or_shared
            and current_fast_identity
            and not native_event_already_full_confirmed
        ):
            confirmation_perf = time.perf_counter()
            features, events, candidate_confirmation_diag = confirm_incremental_candidate_with_full_runtime(
                runtime=runtime,
                edge_id=edge_id,
                asset=asset,
                bars=bars,
                fast_events=events,
                live_event_times=live_event_times,
                last_processed=last_processed,
                logger=logger,
            )
            event_cache_extra = (
                f"{event_cache_extra} candidate_full_authority_confirmed=True "
                f"candidate_identity_match={candidate_confirmation_diag.get('identity_match')}"
            )
            log_latency_stage(
                logger,
                edge_id=edge_id,
                asset=asset,
                stage="runtime_confirm_incremental_candidate_full_authority",
                start_perf=confirmation_perf,
                edge_start_perf=edge_start_perf,
                live_event_times=live_event_times,
                rows=events,
                extra=(
                    f"identity_match={candidate_confirmation_diag.get('identity_match')} "
                    f"fast_candidate_rows={candidate_confirmation_diag.get('fast_candidate_rows')} "
                    f"full_candidate_rows={candidate_confirmation_diag.get('full_candidate_rows')}"
                ),
            )

        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="runtime_generate_candidate_events",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=events,
            extra=event_cache_extra,
        )
        logger.info("RUNTIME_FEATURE_EVENT_READY edge=%s features=%d events=%d", edge_id, len(features), len(events))
        log_runtime_stage_datetime_diagnostics(logger, edge_id, "events_after_generate", events, live_event_times, last_processed=last_processed)

        stage_perf = time.perf_counter()
        events_for_scoring, event_filter_diag = filter_recent_events_for_runtime_score(edge_id, events, live_event_times, last_processed, logger)
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="filter_recent_events_for_runtime_score",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=events_for_scoring,
        )
        logger.info(
            "RUNTIME_EVENT_SCORE_FILTER edge=%s input_events=%s valid_datetime_events=%s events_after_cursor=%s "
            "events_on_executable_live_after_cursor=%s events_sent_to_runtime_score=%s events_trimmed_by_limit=%s "
            "max_events_to_score=%s last_processed=%s latest_executable_live_timestamp=%s reason=%s",
            edge_id,
            event_filter_diag.get("input_events"),
            event_filter_diag.get("events_with_valid_datetime"),
            event_filter_diag.get("events_after_cursor"),
            event_filter_diag.get("events_on_executable_live_after_cursor"),
            event_filter_diag.get("events_sent_to_runtime_score"),
            event_filter_diag.get("events_trimmed_by_limit"),
            event_filter_diag.get("max_events_to_score"),
            event_filter_diag.get("last_processed_timestamp"),
            event_filter_diag.get("latest_executable_live_timestamp"),
            event_filter_diag.get("filter_reason"),
        )
        log_runtime_stage_datetime_diagnostics(logger, edge_id, "events_sent_to_runtime_score", events_for_scoring, live_event_times, last_processed=last_processed)
        if events_for_scoring.empty:
            return []

        stage_perf = time.perf_counter()
        try:
            scored, runtime_manifest = runtime.score(
                features=features,
                events=events_for_scoring,
                bars=bars,
                latest_only=False,
            )
        except Exception as score_exc:
            if not is_selected_feature_nan_drop_exception(score_exc):
                raise

            retry_source = "edge_local_authoritative_retry"
            if INCOMPLETE_MODEL_INPUT_RETRY_ENABLED and shared_feature_cache_used:
                logger.warning(
                    "LIVE_SCORE_SHARED_FEATURE_INCOMPLETE_RETRY edge=%s asset=%s "
                    "builder_edge=%s reason=selected_feature_nan_drop "
                    "action=full_edge_local_features_and_events",
                    edge_id,
                    asset,
                    shared_features_builder_edge_id,
                )
                retry_perf = time.perf_counter()
                retry_features = runtime.build_features(
                    bars=bars,
                    drop_warmup_rows=False,
                )
                retry_events = runtime.generate_candidate_events(
                    features=retry_features,
                )
                retry_events_for_scoring, retry_filter_diag = filter_recent_events_for_runtime_score(
                    edge_id,
                    retry_events,
                    live_event_times,
                    last_processed,
                    logger,
                )
                logger.info(
                    "LIVE_SCORE_SHARED_FEATURE_INCOMPLETE_RETRY_READY edge=%s asset=%s "
                    "feature_rows=%s event_rows=%s scoring_rows=%s elapsed_ms=%.3f",
                    edge_id,
                    asset,
                    rows_len(retry_features),
                    rows_len(retry_events),
                    rows_len(retry_events_for_scoring),
                    elapsed_ms(retry_perf),
                )
                if retry_events_for_scoring.empty:
                    mark_incomplete_model_input_candidate_processed(
                        edge_id=edge_id,
                        asset=asset,
                        runtime=runtime,
                        features=features,
                        events=events_for_scoring,
                        state=state,
                        logger=logger,
                        source="shared_candidate_not_reproduced_by_edge_local_authority",
                    )
                    return []
                try:
                    scored, runtime_manifest = runtime.score(
                        features=retry_features,
                        events=retry_events_for_scoring,
                        bars=bars,
                        latest_only=False,
                    )
                    features = retry_features
                    events = retry_events
                    events_for_scoring = retry_events_for_scoring
                    event_filter_diag = retry_filter_diag
                    shared_feature_cache_used = False
                    logger.info(
                        "LIVE_SCORE_SHARED_FEATURE_INCOMPLETE_RETRY_RECOVERED edge=%s asset=%s "
                        "scored_rows=%s",
                        edge_id,
                        asset,
                        rows_len(scored),
                    )
                except Exception as retry_exc:
                    if not is_selected_feature_nan_drop_exception(retry_exc):
                        raise
                    mark_incomplete_model_input_candidate_processed(
                        edge_id=edge_id,
                        asset=asset,
                        runtime=runtime,
                        features=retry_features,
                        events=retry_events_for_scoring,
                        state=state,
                        logger=logger,
                        source=retry_source,
                    )
                    return []
            else:
                mark_incomplete_model_input_candidate_processed(
                    edge_id=edge_id,
                    asset=asset,
                    runtime=runtime,
                    features=features,
                    events=events_for_scoring,
                    state=state,
                    logger=logger,
                    source=(
                        "edge_local_runtime_selected_feature_nan_drop"
                        if not shared_feature_cache_used
                        else "shared_feature_retry_disabled"
                    ),
                )
                return []
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="runtime_score",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=scored,
        )
        if isinstance(runtime_manifest, dict):
            runtime_manifest.setdefault("dashboard_event_score_filter", event_filter_diag)
            runtime_manifest.setdefault("dashboard_incremental_feature_api", incremental_feature_diag)
            runtime_manifest.setdefault("dashboard_live_event_api", live_event_api_diag if 'live_event_api_diag' in locals() else {})
            runtime_manifest.setdefault("dashboard_incremental_candidate_confirmation", candidate_confirmation_diag)
            runtime_manifest.setdefault("dashboard_research_live_parity_contract", dict(RESEARCH_LIVE_PARITY_CONTRACT))
            runtime_manifest.setdefault(
                "dashboard_runtime_continuation_metadata",
                copy.deepcopy((state.get("runtime_continuation_metadata_by_edge") or {}).get(edge_id, {})),
            )
        logger.info("RUNTIME_SCORE_DONE edge=%s scored_rows=%d", edge_id, 0 if scored is None else len(scored))
        log_runtime_stage_datetime_diagnostics(logger, edge_id, "scored_raw_runtime_output", scored, live_event_times, last_processed=last_processed)

        if scored is None or scored.empty:
            return []

        stage_perf = time.perf_counter()
        validate_runtime_score_output(edge_id, scored)
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="validate_runtime_score_output",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=scored,
        )
        scored = scored.copy()
        scored["Datetime"] = parse_utc_datetime_series(scored["Datetime"])
        scored_before_cursor = scored.dropna(subset=["Datetime"]).sort_values("Datetime")
        scored = scored_before_cursor
        if last_processed is not None:
            scored = scored[scored["Datetime"] > last_processed]

        memory_blocked_count = note_memory_only_blocked(asset, scored, state)
        timestamp_diagnostics = runtime_timestamp_alignment_diagnostics(
            asset,
            scored,
            live_event_times,
            memory_blocked_count,
            scored_before_cursor=scored_before_cursor,
            last_processed=last_processed,
        )
        stage_perf = time.perf_counter()
        scored = scored[scored["Datetime"].isin(live_event_times)]
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="timestamp_cursor_live_gate",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=scored,
        )
        if scored.empty:
            logger.info(
                "RUNTIME_SCORE_NO_LIVE_EVENTS edge=%s reason=no_scored_rows_intersect_executable_live_bar_timestamps "
                "runtime_scored_rows_normalized=%s scored_rows_after_cursor_filter=%s cursor_filter_removed_rows=%s cursor_filter_removed_all=%s "
                "last_processed_timestamp=%s scored_rows_before_live_filter=%s executable_live_timestamp_count=%s "
                "latest_executable_live_timestamp=%s earliest_scored_timestamp=%s latest_scored_timestamp=%s "
                "nearest_scored_timestamp=%s nearest_executable_live_timestamp=%s nearest_scored_to_live_diff_seconds=%s "
                "possible_timestamp_precision_mismatch=%s earliest_runtime_scored_timestamp=%s latest_runtime_scored_timestamp=%s "
                "nearest_runtime_scored_timestamp=%s nearest_runtime_executable_live_timestamp=%s nearest_runtime_to_live_diff_seconds=%s "
                "runtime_possible_timestamp_precision_mismatch=%s memory_only_blocked=%s",
                edge_id,
                timestamp_diagnostics.get("runtime_scored_rows_normalized"),
                timestamp_diagnostics.get("scored_rows_after_cursor_filter"),
                timestamp_diagnostics.get("cursor_filter_removed_rows"),
                timestamp_diagnostics.get("cursor_filter_removed_all"),
                timestamp_diagnostics.get("last_processed_timestamp"),
                timestamp_diagnostics.get("scored_rows_before_live_filter"),
                timestamp_diagnostics.get("executable_live_timestamp_count"),
                timestamp_diagnostics.get("latest_executable_live_timestamp"),
                timestamp_diagnostics.get("earliest_scored_timestamp"),
                timestamp_diagnostics.get("latest_scored_timestamp"),
                timestamp_diagnostics.get("nearest_scored_timestamp"),
                timestamp_diagnostics.get("nearest_executable_live_timestamp"),
                timestamp_diagnostics.get("nearest_scored_to_live_diff_seconds"),
                timestamp_diagnostics.get("possible_timestamp_precision_mismatch"),
                timestamp_diagnostics.get("earliest_runtime_scored_timestamp"),
                timestamp_diagnostics.get("latest_runtime_scored_timestamp"),
                timestamp_diagnostics.get("nearest_runtime_scored_timestamp"),
                timestamp_diagnostics.get("nearest_runtime_executable_live_timestamp"),
                timestamp_diagnostics.get("nearest_runtime_to_live_diff_seconds"),
                timestamp_diagnostics.get("runtime_possible_timestamp_precision_mismatch"),
                memory_blocked_count,
            )
            return []

        final_threshold = float(edge["final_live_threshold"])
        portfolio_weight = float(edge.get("portfolio_weight", 0.0))
        live_manifest_hash = sha256_file(settings.LIVE_MANIFEST_PATH)
        bar_policy_hash = sha256_file(LIVE_BAR_POLICY_PATH)
        stage_perf = time.perf_counter()
        score_rows_path = export_runtime_score_rows(edge, scored, live_manifest_hash, bar_policy_hash, runtime_manifest)
        log_latency_stage(
            logger,
            edge_id=edge_id,
            asset=asset,
            stage="export_runtime_score_rows",
            start_perf=stage_perf,
            edge_start_perf=edge_start_perf,
            live_event_times=live_event_times,
            rows=scored,
        )
        live_allowed, live_blocker = live_execution_allowed(manifest, bar_policy)
        now_ts = utc_now_ts()
        emitted = set(state.setdefault("emitted_signal_ids", []))
        max_seen_ts: pd.Timestamp | None = None

        for _, scored_row in scored.iterrows():
            event_ts = ensure_utc_timestamp(scored_row["Datetime"])
            max_seen_ts = event_ts if max_seen_ts is None or event_ts > max_seen_ts else max_seen_ts
            raw_p = float(scored_row["raw_probability"])
            cal_p = float(scored_row["calibrated_probability"])
            afml = float(scored_row["afml_bet_size"])
            runtime_thr = safe_float(scored_row.get("selected_threshold"))
            signal_id = f"{edge_id}|{event_ts.isoformat()}|thr_{final_threshold:.4f}"
            age_seconds = float((now_ts - event_ts).total_seconds())
            passes, probability_passes, afml_passes, status, reason = live_probability_afml_gate(
                cal_p,
                final_threshold,
                afml,
            )

            if signal_id in emitted:
                status = "blocked_duplicate"
                reason = "signal_id_already_emitted"
            elif age_seconds < -FUTURE_EVENT_BLOCK_TOLERANCE_SECONDS:
                status = "blocked_future_timestamp_clock_mismatch"
                reason = f"event_age_seconds_{age_seconds:.1f}_lt_negative_tolerance_{FUTURE_EVENT_BLOCK_TOLERANCE_SECONDS}"
            elif age_seconds > MAX_SIGNAL_AGE_SECONDS:
                status = "blocked_stale"
                reason = f"event_age_seconds_{age_seconds:.1f}_gt_{MAX_SIGNAL_AGE_SECONDS}"

            ctx = get_bar_context(asset, event_ts)
            signal_price = ctx.get("bar_close")
            execution_plan = build_dynamic_execution_plan(
                edge=edge,
                scored_row=scored_row,
                runtime_manifest=runtime_manifest,
                bar_context=ctx,
                signal_price=signal_price,
            )
            plan_ready = bool(execution_plan.get("execution_plan_ready"))
            plan_blocker = str(execution_plan.get("execution_plan_blocker") or "")

            approved = status == "approved_signal"
            execution_allowed = bool(approved and live_allowed and plan_ready)
            if execution_allowed:
                execution_blocker = ""
            elif approved and not live_allowed:
                execution_blocker = live_blocker
            elif approved and not plan_ready:
                execution_blocker = f"execution_plan_not_ready: {plan_blocker}"
            else:
                execution_blocker = reason

            if approved:
                emitted.add(signal_id)
                state.setdefault("emitted_signal_ids", []).append(signal_id)
                state["emitted_signal_ids"] = state["emitted_signal_ids"][-5000:]
                state["total_signals_emitted"] = int(state.get("total_signals_emitted", 0)) + 1

            rows.append(make_dashboard_signal_row(
                manifest=manifest,
                edge=edge,
                schema_version=SCHEMA_VERSION,
                dashboard_version=DASHBOARD_VERSION,
                created_at=utc_now(),
                signal_id=signal_id,
                signal_status=status,
                reason=reason,
                edge_id=edge_id,
                asset=asset,
                mt5_symbol=broker_symbol_for_asset(asset),
                family=str(edge.get("family", "")),
                side_name=str(edge.get("side_name", "")),
                side_value=int(edge.get("side_value")),
                event_time=event_ts.isoformat(),
                bar_start=ctx.get("bar_start"),
                broker_event_time=ctx.get("broker_event_time"),
                broker_bar_start=ctx.get("broker_bar_start"),
                event_age_seconds=age_seconds,
                raw_probability=raw_p,
                calibrated_probability=cal_p,
                afml_bet_size=afml,
                final_live_threshold=final_threshold,
                runtime_selected_threshold=runtime_thr,
                threshold_probability_column="calibrated_probability",
                probability_passes_final_threshold=probability_passes,
                approved_signal=approved,
                execution_allowed=execution_allowed,
                execution_blocker=execution_blocker,
                portfolio_weight=portfolio_weight,
                risk_contract_version=str(manifest.get("risk_contract_version", RISK_CONTRACT_VERSION)),
                risk_caps=edge.get("risk_caps", {}),
                live_manifest_hash=live_manifest_hash,
                edge_manifest_hash=str(edge.get("edge_manifest_hash", "")),
                bar_policy_hash=bar_policy_hash,
                runtime_manifest=runtime_manifest,
                runtime_score_rows_csv=rel(score_rows_path) if score_rows_path else None,
                live_trade_enabled=bool(manifest.get("live_trade_enabled", False)),
                live_policy_ready=bool(bar_policy.get("live_policy_ready", False)),
                policy_gate_status=str(bar_policy.get("policy_gate_status", "")),
                bar_open=ctx.get("bar_open"),
                bar_high=ctx.get("bar_high"),
                bar_low=ctx.get("bar_low"),
                bar_close=ctx.get("bar_close"),
                bar_ticks=ctx.get("bar_ticks"),
                bar_threshold=ctx.get("bar_threshold"),
                bar_threshold_unit=ctx.get("bar_threshold_unit"),
                bar_research_bar_mode=ctx.get("bar_research_bar_mode"),
                bar_threshold_mode=ctx.get("bar_threshold_mode"),
                bar_gap_healed=ctx.get("bar_gap_healed"),
                bar_executable=ctx.get("bar_executable"),
                signal_price=signal_price,
                signal_price_source="closed_standard_quote_tick_bar_close" if signal_price is not None else None,
                risk_source="portfolio_live/live_manifest.json portfolio_weight + calibrated_probability + afml_bet_size",
                order_owner="portfolio_execution_bot.py",
                continuity_status=str(continuity.get("status")),
                entry_timing=execution_plan.get("entry_timing"),
                barrier_reference=execution_plan.get("barrier_reference"),
                barrier_value=safe_float(execution_plan.get("barrier_value")),
                barrier_value_source=execution_plan.get("barrier_value_source"),
                barrier_reference_units=execution_plan.get("barrier_reference_units"),
                barrier_price_distance_formula=execution_plan.get("barrier_price_distance_formula"),
                pt_multiplier=safe_float(execution_plan.get("pt_multiplier")),
                sl_multiplier=safe_float(execution_plan.get("sl_multiplier")),
                vertical_horizon_bars=safe_int(execution_plan.get("vertical_horizon_bars")),
                geometry_hash=str(execution_plan.get("geometry_hash") or "") or None,
                reward_risk_ratio=safe_float(execution_plan.get("reward_risk_ratio")),
                planned_sl_distance_price=safe_float(execution_plan.get("planned_sl_distance_price")),
                planned_tp_distance_price=safe_float(execution_plan.get("planned_tp_distance_price")),
                planned_stop_loss=safe_float(execution_plan.get("planned_stop_loss")),
                planned_take_profit=safe_float(execution_plan.get("planned_take_profit")),
                execution_plan_hash=execution_plan.get("execution_plan_hash"),
                execution_plan_source=execution_plan.get("source"),
                execution_plan_ready=plan_ready,
                execution_plan_blocker=plan_blocker,
                execution_plan_json=execution_plan.get("execution_plan_json"),
                memory_only_blocked_rows_for_edge_batch=memory_blocked_count,
            ))
            logger.info(
                "SIGNAL_CHECK edge=%s status=%s time=%s raw=%.6f cal=%.6f threshold=%.4f afml=%.6f execution_allowed=%s plan_ready=%s reason=%s blocker=%s",
                edge_id,
                status,
                event_ts.isoformat(),
                raw_p,
                cal_p,
                final_threshold,
                afml,
                execution_allowed,
                plan_ready,
                reason,
                execution_blocker,
            )

        if max_seen_ts is not None:
            state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = max_seen_ts.isoformat()
        if LATENCY_STAGE_LOGGING_ENABLED:
            logger.info(
                "LIVE_LATENCY_EDGE_TOTAL edge=%s asset=%s total_ms=%.3f rows=%s latest_live_age_seconds=%s signals=%s",
                edge_id,
                asset,
                elapsed_ms(edge_start_perf),
                rows_len(scored),
                latest_live_age_seconds(live_event_times),
                len(rows),
            )
        return rows
    except Exception as exc:
        logger.error("EDGE_RUNTIME_SCORE_FAILED edge=%s error=%s", edge_id, exc)
        logger.error(traceback.format_exc())
        return [make_dashboard_signal_row(
            manifest=manifest,
            edge=edge,
            schema_version=SCHEMA_VERSION,
            dashboard_version=DASHBOARD_VERSION,
            created_at=utc_now(),
            signal_id=f"{edge_id}|ERROR|{utc_now()}",
            signal_status="failed_edge_runtime_exception",
            reason="exception_during_runtime_feature_event_or_score",
            edge_id=edge_id,
            asset=asset,
            mt5_symbol=broker_symbol_for_asset(asset),
            family=str(edge.get("family", "")),
            side_name=str(edge.get("side_name", "")),
            side_value=int(edge.get("side_value", 0)),
            event_time=None,
            bar_start=None,
            broker_event_time=None,
            broker_bar_start=None,
            event_age_seconds=None,
            raw_probability=None,
            calibrated_probability=None,
            afml_bet_size=None,
            final_live_threshold=float(edge.get("final_live_threshold", 0.0)),
            runtime_selected_threshold=None,
            threshold_probability_column="calibrated_probability",
            probability_passes_final_threshold=False,
            approved_signal=False,
            execution_allowed=False,
            execution_blocker="runtime_exception",
            portfolio_weight=float(edge.get("portfolio_weight", 0.0)),
            risk_contract_version=str(manifest.get("risk_contract_version", RISK_CONTRACT_VERSION)),
            risk_caps=edge.get("risk_caps", {}),
            live_manifest_hash=sha256_file(settings.LIVE_MANIFEST_PATH) if settings.LIVE_MANIFEST_PATH.exists() else "",
            edge_manifest_hash=str(edge.get("edge_manifest_hash", "")),
            bar_policy_hash=sha256_file(LIVE_BAR_POLICY_PATH) if LIVE_BAR_POLICY_PATH.exists() else "",
            runtime_manifest=runtime_manifest,
            runtime_score_rows_csv=None,
            live_trade_enabled=bool(manifest.get("live_trade_enabled", False)),
            live_policy_ready=bool(bar_policy.get("live_policy_ready", False)),
            policy_gate_status=str(bar_policy.get("policy_gate_status", "")),
            bar_open=None,
            bar_high=None,
            bar_low=None,
            bar_close=None,
            bar_ticks=None,
            bar_threshold=None,
            bar_threshold_unit=None,
            bar_research_bar_mode=None,
            bar_threshold_mode=None,
            bar_gap_healed=None,
            bar_executable=None,
            signal_price=None,
            signal_price_source=None,
            risk_source="portfolio_live/live_manifest.json portfolio_weight + calibrated_probability + afml_bet_size",
            order_owner="portfolio_execution_bot.py",
            continuity_status=str(continuity.get("status")),
            execution_plan_ready=False,
            execution_plan_blocker="runtime_exception",
            memory_only_blocked_rows_for_edge_batch=0,
            error=f"{type(exc).__name__}: {exc}",
        )]

def write_runtime_outputs(rows: list[DashboardSignalRow], state: dict[str, Any], manifest: dict[str, Any], bar_policy: dict[str, Any]) -> None:
    with _OUTPUT_WRITE_LOCK:
        output_rows = [asdict(row) for row in rows]
        if output_rows:
            # Approved signals are exported immediately when each edge finishes.
            # Batch completion remains responsible only for audit/status output.
            # Non-critical operator audit: queue in the background when enabled.
            enqueue_noncritical_csv_append(
                path=AUDIT_CSV,
                rows=output_rows,
                dedup_cols=["signal_id"],
                label="live_signal_audit",
            )
        status = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now(),
            "dashboard_version": DASHBOARD_VERSION,
            "live_streaming_hotpath": {
                "version": HOTPATH_VERSION,
                "install": _HOTPATH_INSTALL_REPORT,
                "cache": live_streaming_cache_status(),
                "single_dashboard_entrypoint": True,
            },
            "execution_signal_contract_version": EXECUTION_SIGNAL_CONTRACT_VERSION,
            "execution_plan_version": EXECUTION_PLAN_VERSION,
            "live_manifest_path": rel(settings.LIVE_MANIFEST_PATH),
            "live_manifest_hash": sha256_file(settings.LIVE_MANIFEST_PATH) if settings.LIVE_MANIFEST_PATH.exists() else None,
            "bar_policy_path": rel(LIVE_BAR_POLICY_PATH),
            "bar_policy_hash": sha256_file(LIVE_BAR_POLICY_PATH) if LIVE_BAR_POLICY_PATH.exists() else None,
            "deployment_status": manifest.get("deployment_status"),
            "live_trade_enabled": manifest.get("live_trade_enabled"),
            "policy_gate_status": bar_policy.get("policy_gate_status"),
            "live_policy_ready": bar_policy.get("live_policy_ready"),
            "order_execution_allowed_by_policy": bar_policy.get("order_execution_allowed_by_policy"),
            "selected_edge_count": len(manifest.get("selected_edges", [])),
            "active_thresholds_by_asset": {asset: policy_threshold_audit(asset, policy) for asset, policy in (bar_policy.get("bar_policies") or {}).items()},
            "threshold_refresh_history_tail": state.get("threshold_refresh_history", [])[-20:],
            "gap_threshold_reconstruction_history_tail": state.get("gap_threshold_reconstruction_history", [])[-20:],
            "gap_healed_partial_bar_resets_tail": state.get("gap_healed_partial_bar_resets", [])[-20:],
            "closed_bar_counts": state.get("closed_bar_counts", {}),
            "last_tick_time_msc": state.get("last_tick_time_msc", {}),
            "last_normalized_tick_time_utc": state.get("last_normalized_tick_time_utc", {}),
            "live_tick_cursor_contract_version": LIVE_TICK_CURSOR_CONTRACT_VERSION,
            "live_tick_startup_guard_by_asset": state.get("live_tick_startup_guard_by_asset", {}),
            "live_tick_guard_drop_count_by_asset": state.get("live_tick_guard_drop_count_by_asset", {}),
            "live_tick_guard_history_tail": state.get("live_tick_guard_history", [])[-20:],
            "live_tick_cursor_rejections_tail": state.get("live_tick_cursor_rejections", [])[-20:],
            "gap_healing_last_tick_by_symbol": state.get("gap_healing_last_tick_by_symbol", {}),
            "cursor_repair_history_tail": state.get("cursor_repair_history", [])[-20:],
            "live_tick_range_recovery_by_asset": state.get("live_tick_range_recovery_by_asset", {}),
            "live_tick_range_recovery_history_tail": state.get("live_tick_range_recovery_history", [])[-20:],
            "broker_time_offset_seconds_by_symbol": state.get("broker_time_offset_seconds_by_symbol", {}),
            "broker_time_offset_locked_by_symbol": state.get("broker_time_offset_locked_by_symbol", {}),
            "broker_time_offset_source_by_symbol": state.get("broker_time_offset_source_by_symbol", {}),
            "asset_continuity": state.get("asset_continuity", {}),
            "dashboard_bar_ack_path": rel(BAR_ACK_JSON),
            "dashboard_bar_ack_contract": "v2_dashboard_bar_ack_v1",
            "last_dashboard_bar_ack_write_error": state.get("last_dashboard_bar_ack_write_error"),
            "bar_persistence_contract": "v2_durable_per_bar_journal_with_best_effort_csv_compaction",
            "bar_persistence_status_by_asset": state.get("bar_persistence_status_by_asset", {}),
            "pending_bar_count_by_asset": state.get("pending_bar_count_by_asset", {}),
            "oldest_pending_bar_age_seconds_by_asset": state.get("oldest_pending_bar_age_seconds_by_asset", {}),
            "last_bar_write_error_by_asset": state.get("last_bar_write_error_by_asset", {}),
            "bar_write_retry_count_by_asset": state.get("bar_write_retry_count_by_asset", {}),
            "asset_io_paused_by_asset": state.get("asset_io_paused_by_asset", {}),
            "pending_live_bar_directory": rel(PENDING_LIVE_BAR_DIR),
            "last_cursor_advance": state.get("last_cursor_advance", []),
            "last_gap_healing_summary_by_asset": state.get("last_gap_healing_summary_by_asset", {}),
            "blocked_memory_only_bar_count_by_asset": state.get("blocked_memory_only_bar_count_by_asset", {}),
            "blocked_gap_healed_event_count_by_asset": state.get("blocked_gap_healed_event_count_by_asset", {}),
            "latest_memory_only_bar_time_by_asset": state.get("latest_memory_only_bar_time_by_asset", {}),
            "last_shared_feature_prewarm_cache_by_asset": state.get("last_shared_feature_prewarm_cache_by_asset", {}),
            "research_live_parity_contract": dict(RESEARCH_LIVE_PARITY_CONTRACT),
            "runtime_continuation_metadata_by_edge": state.get("runtime_continuation_metadata_by_edge", {}),
            "blocked_incomplete_model_input_count_by_edge": state.get("blocked_incomplete_model_input_count_by_edge", {}),
            "last_incomplete_model_input_by_edge": state.get("last_incomplete_model_input_by_edge", {}),
            "incomplete_model_input_history_tail": state.get("incomplete_model_input_history", [])[-20:],
            "incremental_shared_feature_cache_by_asset": state.get("incremental_shared_feature_cache_by_asset", {}),
            "runtime_recovery_memory_bars": int(MAX_COMBINED_BARS_FOR_RUNTIME),
            "runtime_state_directory": rel(RUNTIME_CONTINUATION_STATE_DIR),
            "edge_artifact_preload": state.get("last_edge_artifact_preload", {}),
            "noncritical_io": noncritical_io_status_snapshot(),
            "async_scoring_queue": state.get("last_async_scoring_queue_status", {}),
            "last_async_scoring_job": state.get("last_async_scoring_job", {}),
            "total_closed_bars_since_state_created": state.get("total_closed_bars_since_state_created", 0),
            "total_signals_emitted": state.get("total_signals_emitted", 0),
            "latest_batch_rows": len(output_rows),
            "latest_batch_approved": sum(1 for row in output_rows if row.get("approved_signal") is True),
            "latest_batch_execution_allowed": sum(1 for row in output_rows if row.get("execution_allowed") is True),
            "latest_batch_execution_plan_ready": sum(1 for row in output_rows if row.get("execution_plan_ready") is True),
            "audit_csv": rel(AUDIT_CSV),
            "approved_signals_csv": rel(APPROVED_SIGNALS_CSV),
            "approved_signals_jsonl": rel(APPROVED_SIGNALS_JSONL),
            "runtime_score_rows_dir": rel(RUNTIME_SCORE_ROWS_DIR),
            "state_path": rel(STATE_PATH),
            "sotd_contracts": {
                "dashboard_may_engineer_edge_features": False,
                "dashboard_calls_edge_runtime_score": True,
                "requires_research_to_live_bar_continuity": True,
                "runtime_owned_stateful_continuation_preferred": True,
                "dashboard_may_interpret_runtime_continuation_state": False,
                "legacy_arbitrary_tail_search_enabled": False,
                "full_raw_history_build_role": "bootstrap_recovery_and_hmm_refit_boundary_parity_audit",
                "fail_closed_if_gap_not_fully_healed": FAIL_IF_GAP_NOT_FULLY_HEALED,
                "broker_time_offset_locked_before_gap_healing": True,
                "gap_healing_may_not_reinfer_broker_offset": True,
                "new_state_skips_existing_live_bars": True,
                "gap_healed_bars_are_memory_only": True,
                "gap_healing_threshold_reconstruction": "causal_day_by_day_completed_prior_daily_tick_counts",
                "gap_healing_threshold_uses_future_data": False,
                "gap_healed_partial_bars_may_not_become_executable": True,
                "historical_gap_healing_may_not_mutate_live_tick_cursor": True,
                "live_tick_cursor_is_monotonic": True,
                "executable_ticks_must_be_strictly_after_startup_cutoff": True,
                "executable_bars_must_be_strictly_after_startup_cutoff": True,
                "final_threshold_source": "portfolio_live/live_manifest.json selected_edges[].final_live_threshold",
                "execution_plan_source": "runtime/edge/live manifest materialized by dashboard for low-latency execution bot",
                "dynamic_mldp_sl_pt": True,
                "orders_sent_by_dashboard": False,
            },
        }
        state["last_noncritical_io_status"] = noncritical_io_status_snapshot()
        enqueue_noncritical_status_json(status=status, label="latest_dashboard_status")

def validate_symbols(mt5: Any, assets: list[str], logger) -> None:
    for asset in assets:
        symbol = broker_symbol_for_asset(asset)
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol not found for asset={asset}: {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Could not select MT5 symbol for asset={asset}: {symbol}")
        logger.info("SYMBOL_READY asset=%s symbol=%s", asset, symbol)


def _edge_state_delta_after_parallel_worker(
    *,
    edge_id: str,
    asset: str,
    base_state: dict[str, Any],
    worker_state: dict[str, Any],
    rows: list[DashboardSignalRow],
) -> dict[str, Any]:
    """Extract only safe per-edge/per-asset state changes from a parallel worker.

    Stage 5 runs expensive edge runtime work in worker threads, but the main
    dashboard remains the owner of live state. Workers receive a deep-copied
    state snapshot. This helper extracts the small, deterministic state changes
    produced by one edge so the main thread can merge them safely.
    """
    base_cursors = base_state.get("last_processed_timestamp_by_edge", {}) or {}
    worker_cursors = worker_state.get("last_processed_timestamp_by_edge", {}) or {}
    base_stale_counts = base_state.get("stale_pre_runtime_skip_count_by_edge", {}) or {}
    worker_stale_counts = worker_state.get("stale_pre_runtime_skip_count_by_edge", {}) or {}
    base_mem_counts = base_state.get("blocked_memory_only_bar_count_by_asset", {}) or {}
    worker_mem_counts = worker_state.get("blocked_memory_only_bar_count_by_asset", {}) or {}
    base_gap_counts = base_state.get("blocked_gap_healed_event_count_by_asset", {}) or {}
    worker_gap_counts = worker_state.get("blocked_gap_healed_event_count_by_asset", {}) or {}

    approved_signal_ids = [row.signal_id for row in rows if getattr(row, "approved_signal", False)]
    delta = {
        "edge_id": edge_id,
        "asset": asset,
        "last_processed_timestamp": worker_cursors.get(edge_id)
        if worker_cursors.get(edge_id) != base_cursors.get(edge_id)
        else None,
        "stale_pre_runtime_skip_count_delta": int(worker_stale_counts.get(edge_id, 0) or 0) - int(base_stale_counts.get(edge_id, 0) or 0),
        "last_stale_pre_runtime_skip": (worker_state.get("last_stale_pre_runtime_skip_by_edge", {}) or {}).get(edge_id),
        "stale_pre_runtime_skip_history_new": [],
        "blocked_memory_only_delta": int(worker_mem_counts.get(asset, 0) or 0) - int(base_mem_counts.get(asset, 0) or 0),
        "blocked_gap_healed_delta": int(worker_gap_counts.get(asset, 0) or 0) - int(base_gap_counts.get(asset, 0) or 0),
        "latest_memory_only_bar_time": None,
        "approved_signal_ids": approved_signal_ids,
        "approved_signal_count": len(approved_signal_ids),
        "runtime_continuation_metadata": copy.deepcopy(
            (worker_state.get("runtime_continuation_metadata_by_edge", {}) or {}).get(edge_id)
        ),
    }

    base_history_len = len(base_state.get("stale_pre_runtime_skip_history", []) or [])
    worker_history = worker_state.get("stale_pre_runtime_skip_history", []) or []
    if len(worker_history) > base_history_len:
        delta["stale_pre_runtime_skip_history_new"] = worker_history[base_history_len:]

    worker_latest_memory = worker_state.get("latest_memory_only_bar_time_by_asset", {}) or {}
    base_latest_memory = base_state.get("latest_memory_only_bar_time_by_asset", {}) or {}
    if worker_latest_memory.get(asset) != base_latest_memory.get(asset):
        delta["latest_memory_only_bar_time"] = worker_latest_memory.get(asset)

    return delta


def _merge_parallel_edge_state_delta(state: dict[str, Any], delta: dict[str, Any]) -> None:
    """Merge a worker's safe state delta back into the main dashboard state."""
    edge_id = str(delta.get("edge_id") or "")
    asset = str(delta.get("asset") or "")

    cursor = delta.get("last_processed_timestamp")
    if edge_id and cursor:
        current_raw = state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
        try:
            current_ts = ensure_utc_timestamp(current_raw) if current_raw else None
            incoming_ts = ensure_utc_timestamp(cursor)
            if current_ts is None or incoming_ts > current_ts:
                state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = incoming_ts.isoformat()
        except Exception:
            state.setdefault("last_processed_timestamp_by_edge", {})[edge_id] = str(cursor)

    stale_delta = int(delta.get("stale_pre_runtime_skip_count_delta") or 0)
    if edge_id and stale_delta > 0:
        counts = state.setdefault("stale_pre_runtime_skip_count_by_edge", {})
        counts[edge_id] = int(counts.get(edge_id, 0) or 0) + stale_delta
        last_rec = delta.get("last_stale_pre_runtime_skip")
        if isinstance(last_rec, dict):
            state.setdefault("last_stale_pre_runtime_skip_by_edge", {})[edge_id] = last_rec
        new_history = delta.get("stale_pre_runtime_skip_history_new") or []
        if new_history:
            state.setdefault("stale_pre_runtime_skip_history", []).extend(new_history)
            state["stale_pre_runtime_skip_history"] = state["stale_pre_runtime_skip_history"][-500:]

    if asset:
        mem_delta = int(delta.get("blocked_memory_only_delta") or 0)
        if mem_delta > 0:
            by_asset = state.setdefault("blocked_memory_only_bar_count_by_asset", {})
            by_asset[asset] = int(by_asset.get(asset, 0) or 0) + mem_delta
        gap_delta = int(delta.get("blocked_gap_healed_delta") or 0)
        if gap_delta > 0:
            by_asset = state.setdefault("blocked_gap_healed_event_count_by_asset", {})
            by_asset[asset] = int(by_asset.get(asset, 0) or 0) + gap_delta
        latest_memory = delta.get("latest_memory_only_bar_time")
        if latest_memory:
            state.setdefault("latest_memory_only_bar_time_by_asset", {})[asset] = latest_memory

    runtime_meta = delta.get("runtime_continuation_metadata")
    if edge_id and isinstance(runtime_meta, dict):
        state.setdefault("runtime_continuation_metadata_by_edge", {})[edge_id] = copy.deepcopy(runtime_meta)

    approved_ids = list(delta.get("approved_signal_ids") or [])
    if approved_ids:
        emitted = state.setdefault("emitted_signal_ids", [])
        known = set(emitted)
        added = 0
        for signal_id in approved_ids:
            if signal_id not in known:
                emitted.append(signal_id)
                known.add(signal_id)
                added += 1
        if added:
            state["emitted_signal_ids"] = emitted[-5000:]
            state["total_signals_emitted"] = int(state.get("total_signals_emitted", 0) or 0) + added

def _scoring_batch_state_delta_after_worker(
    *,
    assets: list[str],
    base_state: dict[str, Any],
    worker_state: dict[str, Any],
    rows: list[DashboardSignalRow],
) -> dict[str, Any]:
    """Extract safe state changes from an asynchronous scoring batch.

    The tick/bar loop owns the real dashboard state. The scoring worker receives
    a deep-copied snapshot, scores without touching MT5, and returns only the
    deterministic cursor/signal/audit counters that should be merged back.
    """
    asset_set = {str(asset) for asset in assets}
    base_cursors = base_state.get("last_processed_timestamp_by_edge", {}) or {}
    worker_cursors = worker_state.get("last_processed_timestamp_by_edge", {}) or {}
    base_stale_counts = base_state.get("stale_pre_runtime_skip_count_by_edge", {}) or {}
    worker_stale_counts = worker_state.get("stale_pre_runtime_skip_count_by_edge", {}) or {}
    base_mem_counts = base_state.get("blocked_memory_only_bar_count_by_asset", {}) or {}
    worker_mem_counts = worker_state.get("blocked_memory_only_bar_count_by_asset", {}) or {}
    base_gap_counts = base_state.get("blocked_gap_healed_event_count_by_asset", {}) or {}
    worker_gap_counts = worker_state.get("blocked_gap_healed_event_count_by_asset", {}) or {}

    changed_edges = set(base_cursors) | set(worker_cursors)
    changed_edges |= {str(getattr(row, "edge_id", "")) for row in rows if getattr(row, "edge_id", "")}
    changed_edges |= set(worker_stale_counts) | set(base_stale_counts)
    base_runtime_meta = base_state.get("runtime_continuation_metadata_by_edge", {}) or {}
    worker_runtime_meta = worker_state.get("runtime_continuation_metadata_by_edge", {}) or {}
    changed_edges |= set(base_runtime_meta) | set(worker_runtime_meta)

    edge_deltas: list[dict[str, Any]] = []
    for edge_id in sorted(edge for edge in changed_edges if edge):
        cursor = None
        if worker_cursors.get(edge_id) != base_cursors.get(edge_id):
            cursor = worker_cursors.get(edge_id)
        stale_delta = int(worker_stale_counts.get(edge_id, 0) or 0) - int(base_stale_counts.get(edge_id, 0) or 0)
        runtime_meta_changed = worker_runtime_meta.get(edge_id) != base_runtime_meta.get(edge_id)
        if cursor or stale_delta > 0 or runtime_meta_changed:
            edge_deltas.append({
                "edge_id": edge_id,
                "asset": edge_id.split("/", 1)[0] if "/" in edge_id else "",
                "last_processed_timestamp": cursor,
                "stale_pre_runtime_skip_count_delta": stale_delta,
                "last_stale_pre_runtime_skip": (worker_state.get("last_stale_pre_runtime_skip_by_edge", {}) or {}).get(edge_id),
                "stale_pre_runtime_skip_history_new": [],
                "blocked_memory_only_delta": 0,
                "blocked_gap_healed_delta": 0,
                "latest_memory_only_bar_time": None,
                "approved_signal_ids": [],
                "approved_signal_count": 0,
                "runtime_continuation_metadata": copy.deepcopy(
                    (worker_state.get("runtime_continuation_metadata_by_edge", {}) or {}).get(edge_id)
                ),
            })

    base_history_len = len(base_state.get("stale_pre_runtime_skip_history", []) or [])
    worker_history = worker_state.get("stale_pre_runtime_skip_history", []) or []
    history_new = worker_history[base_history_len:] if len(worker_history) > base_history_len else []

    asset_deltas: list[dict[str, Any]] = []
    for asset in sorted(asset_set | set(worker_mem_counts) | set(worker_gap_counts)):
        mem_delta = int(worker_mem_counts.get(asset, 0) or 0) - int(base_mem_counts.get(asset, 0) or 0)
        gap_delta = int(worker_gap_counts.get(asset, 0) or 0) - int(base_gap_counts.get(asset, 0) or 0)
        latest_memory = (worker_state.get("latest_memory_only_bar_time_by_asset", {}) or {}).get(asset)
        base_latest_memory = (base_state.get("latest_memory_only_bar_time_by_asset", {}) or {}).get(asset)
        if mem_delta > 0 or gap_delta > 0 or latest_memory != base_latest_memory:
            asset_deltas.append({
                "edge_id": "",
                "asset": asset,
                "last_processed_timestamp": None,
                "stale_pre_runtime_skip_count_delta": 0,
                "last_stale_pre_runtime_skip": None,
                "stale_pre_runtime_skip_history_new": [],
                "blocked_memory_only_delta": max(mem_delta, 0),
                "blocked_gap_healed_delta": max(gap_delta, 0),
                "latest_memory_only_bar_time": latest_memory if latest_memory != base_latest_memory else None,
                "approved_signal_ids": [],
                "approved_signal_count": 0,
                "runtime_continuation_metadata": None,
            })

    approved_signal_ids = [row.signal_id for row in rows if getattr(row, "approved_signal", False)]

    return {
        "assets": sorted(asset_set),
        "edge_deltas": edge_deltas,
        "asset_deltas": asset_deltas,
        "stale_pre_runtime_skip_history_new": history_new,
        "approved_signal_ids": approved_signal_ids,
        "rows": int(len(rows)),
    }


def _merge_scoring_batch_state_delta(state: dict[str, Any], delta: dict[str, Any]) -> None:
    """Merge asynchronous scoring-batch state changes back into main state."""
    for item in delta.get("edge_deltas", []) or []:
        _merge_parallel_edge_state_delta(state, item)

    history_new = delta.get("stale_pre_runtime_skip_history_new") or []
    if history_new:
        state.setdefault("stale_pre_runtime_skip_history", []).extend(history_new)
        state["stale_pre_runtime_skip_history"] = state["stale_pre_runtime_skip_history"][-500:]

    for item in delta.get("asset_deltas", []) or []:
        _merge_parallel_edge_state_delta(state, item)

    approved_ids = list(delta.get("approved_signal_ids") or [])
    if approved_ids:
        emitted = state.setdefault("emitted_signal_ids", [])
        known = set(emitted)
        added = 0
        for signal_id in approved_ids:
            if signal_id not in known:
                emitted.append(signal_id)
                known.add(signal_id)
                added += 1
        if added:
            state["emitted_signal_ids"] = emitted[-5000:]
            state["total_signals_emitted"] = int(state.get("total_signals_emitted", 0) or 0) + added


class ScoringQueueManager:
    """Priority scoring queue so MT5 tick ingestion never waits for scoring.

    Stage 11 adds two scheduler lanes:
    - fast lane: normal/cache-hit edges, always processed first;
    - slow lane: configured heavy fallback families such as absorption.

    This is scheduling only. The worker still calls score_selected_assets(),
    score_edge(), each runtime.py, thresholds, AFML, and execution handoff exactly
    as before. MT5 remains owned by the main thread.
    """

    def __init__(
        self,
        *,
        manifest: dict[str, Any],
        bar_policy: dict[str, Any],
        state: dict[str, Any],
        logger,
        state_lock: threading.RLock,
    ) -> None:
        self.manifest = manifest
        self.bar_policy = bar_policy
        self.state = state
        self.logger = logger
        self.state_lock = state_lock
        self.enabled = bool(ASYNC_SCORING_QUEUE_ENABLED)
        self.fast_jobs: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS)
        self.slow_jobs: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=ASYNC_SLOW_LANE_QUEUE_MAX_PENDING_JOBS)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None  # compatibility alias for fast worker
        self.threads: dict[str, threading.Thread] = {}
        self.active_jobs_by_lane: dict[str, dict[str, Any]] = {}
        self.active_jobs_lock = threading.RLock()
        self.metrics_lock = threading.RLock()
        self.job_seq = 0
        self.active_job_id: str | None = None
        self.active_job_lane: str | None = None
        self.active_job_assets: list[str] = []
        self.active_job_created_at: str | None = None
        self.active_job_started_at: str | None = None
        self.last_job_summary: dict[str, Any] = {}
        self.total_jobs_started = 0
        self.total_jobs_completed = 0
        self.total_jobs_failed = 0
        self.total_slow_jobs_skipped = 0
        self.total_jobs_cancelled_on_shutdown = 0
        # Producers and the worker use the same condition so selecting the fast
        # queue is atomic relative to new enqueues. This makes the documented
        # fast-first policy true instead of allowing a blocking slow get() race.
        self.job_available = threading.Condition(threading.RLock())

    def _queue_for_lane(self, lane: str) -> queue.Queue[dict[str, Any] | None]:
        return self.slow_jobs if str(lane).lower() == "slow" else self.fast_jobs

    def _queue_size_for_lane(self, lane: str) -> int:
        try:
            return int(self._queue_for_lane(lane).qsize())
        except Exception:
            return 0

    def _pending_queue_size(self) -> int:
        return int((self.fast_jobs.qsize() if self.enabled else 0) + (self.slow_jobs.qsize() if self.enabled else 0))

    def _queues_empty(self) -> bool:
        return self.fast_jobs.empty() and self.slow_jobs.empty()

    def status_snapshot(self) -> dict[str, Any]:
        """Return queue observability without mutating trading logic."""
        active_started = self.active_job_started_at
        active_age = None
        if active_started:
            try:
                active_age = float((utc_now_ts() - ensure_utc_timestamp(active_started)).total_seconds())
            except Exception:
                active_age = None
        last_elapsed = None
        last_rows = None
        last_approved = None
        if isinstance(self.last_job_summary, dict) and self.last_job_summary:
            last_elapsed = safe_float(self.last_job_summary.get("elapsed_seconds"))
            last_rows = safe_int(self.last_job_summary.get("rows"))
            last_approved = safe_int(self.last_job_summary.get("approved_signal_count"))
        with self.active_jobs_lock:
            active_jobs_by_lane = copy.deepcopy(self.active_jobs_by_lane)
        return {
            "enabled": bool(self.enabled),
            "slow_lane_enabled": bool(SLOW_LANE_SCORING_ENABLED),
            "slow_lane_families": sorted(SLOW_LANE_EDGE_FAMILIES),
            "worker_mode": "dedicated_fast_and_slow_workers",
            "active_jobs_by_lane": active_jobs_by_lane,
            "active_worker_count": int(len(active_jobs_by_lane)),
            "active_job_id": self.active_job_id,
            "active_job_lane": self.active_job_lane,
            "active_job_assets": list(self.active_job_assets or []),
            "active_job_created_at": self.active_job_created_at,
            "active_job_started_at": self.active_job_started_at,
            "active_job_age_seconds": active_age,
            "pending_queue_size": self._pending_queue_size() if self.enabled else 0,
            "pending_fast_queue_size": int(self.fast_jobs.qsize()) if self.enabled else 0,
            "pending_slow_queue_size": int(self.slow_jobs.qsize()) if self.enabled else 0,
            "max_pending_jobs": int(ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS + ASYNC_SLOW_LANE_QUEUE_MAX_PENDING_JOBS),
            "max_pending_fast_jobs": int(ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS),
            "max_pending_slow_jobs": int(ASYNC_SLOW_LANE_QUEUE_MAX_PENDING_JOBS),
            "last_job": self.last_job_summary,
            "last_job_elapsed_seconds": last_elapsed,
            "last_job_rows": last_rows,
            "last_job_approved": last_approved,
            "total_jobs_started": int(self.total_jobs_started),
            "total_jobs_completed": int(self.total_jobs_completed),
            "total_jobs_failed": int(self.total_jobs_failed),
            "total_slow_jobs_skipped": int(self.total_slow_jobs_skipped),
            "total_jobs_cancelled_on_shutdown": int(self.total_jobs_cancelled_on_shutdown),
            "shutdown_requested": bool(self.stop_event.is_set()),
        }

    def start(self) -> None:
        if not self.enabled:
            self.logger.info("LIVE_SCORING_QUEUE_BYPASS enabled=False reason=setting_disabled")
            return
        lanes = ["fast"]
        if SLOW_LANE_SCORING_ENABLED:
            lanes.append("slow")
        for lane in lanes:
            thread = threading.Thread(
                target=self._run_lane,
                args=(lane,),
                name=f"live_scoring_{lane}_worker",
                daemon=True,
            )
            self.threads[lane] = thread
            thread.start()
        self.thread = self.threads.get("fast")
        self.logger.info(
            "LIVE_SCORING_QUEUE_START enabled=True max_fast_jobs=%s max_slow_jobs=%s mode=dedicated_fast_and_slow_workers workers=%s slow_lane_enabled=%s slow_lane_families=%s global_asset_concurrency_limit=%s",
            ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS,
            ASYNC_SLOW_LANE_QUEUE_MAX_PENDING_JOBS,
            sorted(self.threads),
            SLOW_LANE_SCORING_ENABLED,
            sorted(SLOW_LANE_EDGE_FAMILIES),
            ASSET_SCORING_WORKERS,
        )

    def enqueue(self, assets: list[str], *, reason: str) -> bool:
        if not self.enabled:
            return False
        if self.stop_event.is_set() or _DASHBOARD_SHUTDOWN_EVENT.is_set():
            self.logger.info(
                "LIVE_SCORING_QUEUE_ENQUEUE_SKIP reason=shutdown_requested assets=%s",
                assets,
            )
            return False
        unique_assets = sorted({str(asset) for asset in assets if asset})
        if not unique_assets:
            return True

        if SLOW_LANE_SCORING_ENABLED:
            fast_assets = assets_with_lane_edges(self.manifest, unique_assets, scoring_lane="fast")
            slow_assets = assets_with_lane_edges(self.manifest, unique_assets, scoring_lane="slow")
            ok_fast = self._enqueue_lane(fast_assets, lane="fast", reason=reason)
            ok_slow = self._enqueue_lane(slow_assets, lane="slow", reason=reason)
            return bool(ok_fast and ok_slow)

        return self._enqueue_lane(unique_assets, lane="all", reason=reason)

    def _enqueue_lane(self, assets: list[str], *, lane: str, reason: str) -> bool:
        lane = str(lane or "fast").strip().lower()
        unique_assets = sorted({str(asset) for asset in assets if asset})
        if not unique_assets:
            self.logger.info("LIVE_SCORING_QUEUE_ENQUEUE_SKIP lane=%s reason=no_assets_with_lane_edges", lane)
            return True

        self.job_seq += 1
        job = {
            "job_id": f"score_job_{self.job_seq}_{lane}",
            "assets": unique_assets,
            "lane": lane,
            "created_at": utc_now(),
            "reason": reason,
        }
        q = self._queue_for_lane(lane)
        with self.job_available:
            if self.stop_event.is_set() or _DASHBOARD_SHUTDOWN_EVENT.is_set():
                self.logger.info(
                    "LIVE_SCORING_QUEUE_ENQUEUE_SKIP lane=%s reason=shutdown_requested assets=%s",
                    lane,
                    unique_assets,
                )
                return False
            try:
                q.put_nowait(job)
                self.job_available.notify()
                self.logger.info(
                    "LIVE_SCORING_QUEUE_ENQUEUE job_id=%s lane=%s assets=%s fast_queue_size=%s slow_queue_size=%s reason=%s",
                    job["job_id"],
                    lane,
                    unique_assets,
                    self.fast_jobs.qsize(),
                    self.slow_jobs.qsize(),
                    reason,
                )
                return True
            except queue.Full:
                merged_assets = set(unique_assets)
                drained_jobs = 0
                while True:
                    try:
                        old = q.get_nowait()
                    except queue.Empty:
                        break
                    if old is None:
                        q.task_done()
                        continue
                    merged_assets.update(old.get("assets", []) or [])
                    drained_jobs += 1
                    q.task_done()
                job["assets"] = sorted(merged_assets)
                q.put_nowait(job)
                self.job_available.notify()
                self.logger.warning(
                    "LIVE_SCORING_QUEUE_COALESCE job_id=%s lane=%s assets=%s drained_jobs=%s fast_queue_size=%s slow_queue_size=%s reason=queue_full",
                    job["job_id"],
                    lane,
                    job["assets"],
                    drained_jobs,
                    self.fast_jobs.qsize(),
                    self.slow_jobs.qsize(),
                )
                return True

    def _get_next_job(self) -> tuple[queue.Queue[dict[str, Any] | None] | None, dict[str, Any] | None]:
        # Strict priority: producers enqueue under this same condition, so a slow
        # job cannot be selected while a fast job is already waiting.
        with self.job_available:
            while True:
                if self.stop_event.is_set():
                    return None, None
                try:
                    return self.fast_jobs, self.fast_jobs.get_nowait()
                except queue.Empty:
                    pass
                try:
                    return self.slow_jobs, self.slow_jobs.get_nowait()
                except queue.Empty:
                    pass
                if self.stop_event.is_set() and self._queues_empty():
                    return None, None
                self.job_available.wait(timeout=0.5)

    def _run(self) -> None:
        while True:
            q, job = self._get_next_job()
            if q is None:
                break
            if job is None:
                q.task_done()
                continue
            try:
                self._process_job(job)
            finally:
                q.task_done()

    def _run_lane(self, lane: str) -> None:
        """Dedicated worker for one scheduler lane; slow work cannot occupy fast."""
        lane = str(lane).strip().lower()
        q = self._queue_for_lane(lane)
        while not self.stop_event.is_set() or not q.empty():
            try:
                job = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                q.task_done()
                break
            try:
                self._process_job(job)
            finally:
                q.task_done()

    def _refresh_legacy_active_fields_locked(self) -> None:
        preferred = self.active_jobs_by_lane.get("fast") or self.active_jobs_by_lane.get("slow")
        if preferred is None and self.active_jobs_by_lane:
            preferred = next(iter(self.active_jobs_by_lane.values()))
        preferred = preferred or {}
        self.active_job_id = preferred.get("job_id")
        self.active_job_lane = preferred.get("lane")
        self.active_job_assets = list(preferred.get("assets") or [])
        self.active_job_created_at = preferred.get("created_at")
        self.active_job_started_at = preferred.get("started_at")

    def _set_active_job(
        self,
        *,
        lane: str,
        job_id: str,
        assets: list[str],
        created_at: str | None,
        started_at: str,
    ) -> None:
        with self.active_jobs_lock:
            self.active_jobs_by_lane[lane] = {
                "job_id": job_id,
                "lane": lane,
                "assets": list(assets),
                "created_at": created_at,
                "started_at": started_at,
            }
            self._refresh_legacy_active_fields_locked()

    def _update_active_job_assets(self, *, lane: str, assets: list[str]) -> None:
        with self.active_jobs_lock:
            if lane in self.active_jobs_by_lane:
                self.active_jobs_by_lane[lane]["assets"] = list(assets)
            self._refresh_legacy_active_fields_locked()

    def _clear_active_job(self, lane: str) -> None:
        with self.active_jobs_lock:
            self.active_jobs_by_lane.pop(lane, None)
            self._refresh_legacy_active_fields_locked()

    def _fresh_slow_lane_assets(self, assets: list[str]) -> list[str]:
        """Return slow-lane assets with at least one fresh pending absorption event.

        This pre-check prevents expensive slow-lane feature builds when every
        pending slow-lane timestamp would be stale before runtime work starts.
        It uses the same pre_runtime_live_event_guard as the normal edge path.
        """
        edges_by_asset = selected_edges_by_asset(self.manifest.get("selected_edges", []))
        keep_assets: list[str] = []

        for asset in sorted({str(asset) for asset in assets if asset}):
            live_event_times = latest_live_executable_timestamps(asset)
            slow_edges = filter_edges_for_scoring_lane(edges_by_asset.get(asset, []), scoring_lane="slow")
            keep_asset = False

            for edge in slow_edges:
                edge_id = str(edge.get("edge_id") or "").strip()
                last_raw = self.state.setdefault("last_processed_timestamp_by_edge", {}).get(edge_id)
                try:
                    last_processed = ensure_utc_timestamp(last_raw) if last_raw else None
                except Exception:
                    last_processed = None

                skip, diag = pre_runtime_live_event_guard(
                    edge_id=edge_id,
                    asset=asset,
                    live_event_times=live_event_times,
                    last_processed=last_processed,
                    state=self.state,
                    logger=self.logger,
                )
                if not skip:
                    keep_asset = True
                else:
                    self.logger.info(
                        "LIVE_SLOW_LANE_EDGE_SKIP edge=%s asset=%s reason=%s latest_pending_age_seconds=%s",
                        edge_id,
                        asset,
                        diag.get("reason"),
                        diag.get("latest_pending_live_age_seconds"),
                    )

            if keep_asset:
                keep_assets.append(asset)

        return keep_assets

    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id"))
        lane = str(job.get("lane") or "all").strip().lower()
        assets = sorted({str(asset) for asset in job.get("assets", []) if asset})
        if not assets:
            return

        job_started_at = utc_now()
        self._set_active_job(
            lane=lane,
            job_id=job_id,
            assets=assets,
            created_at=str(job.get("created_at") or ""),
            started_at=job_started_at,
        )
        with self.metrics_lock:
            self.total_jobs_started += 1
        start_perf = time.perf_counter()

        if lane == "slow":
            with self.state_lock:
                fresh_assets = self._fresh_slow_lane_assets(assets)
                self.state["last_async_scoring_queue_status"] = self.status_snapshot()
            if not fresh_assets:
                self.total_slow_jobs_skipped += 1
                elapsed = elapsed_ms(start_perf)
                self.last_job_summary = {
                    "job_id": job_id,
                    "lane": lane,
                    "assets": assets,
                    "created_at": job.get("created_at"),
                    "started_at": job_started_at,
                    "finished_at": utc_now(),
                    "elapsed_ms": elapsed,
                    "elapsed_seconds": elapsed / 1000.0,
                    "rows": 0,
                    "approved_signal_count": 0,
                    "queue_size_after": self._pending_queue_size(),
                    "fast_queue_size_after": self.fast_jobs.qsize(),
                    "slow_queue_size_after": self.slow_jobs.qsize(),
                    "status": "skipped",
                    "reason": "slow_lane_no_fresh_pending_timestamps_before_runtime",
                }
                with self.state_lock:
                    self.state["last_async_scoring_job"] = dict(self.last_job_summary)
                    self.state["last_async_scoring_queue_status"] = self.status_snapshot()
                    write_runtime_outputs([], self.state, self.manifest, self.bar_policy)
                self.logger.warning(
                    "LIVE_SCORING_QUEUE_JOB_SKIP job_id=%s lane=%s assets=%s elapsed_ms=%.3f reason=slow_lane_no_fresh_pending_timestamps_before_runtime fast_queue_size=%s slow_queue_size=%s",
                    job_id,
                    lane,
                    assets,
                    elapsed,
                    self.fast_jobs.qsize(),
                    self.slow_jobs.qsize(),
                )
                return
            assets = fresh_assets
            self._update_active_job_assets(lane=lane, assets=assets)

        with self.state_lock:
            self.state["last_async_scoring_queue_status"] = self.status_snapshot()
        self.logger.info(
            "LIVE_SCORING_QUEUE_JOB_START job_id=%s lane=%s assets=%s fast_queue_size=%s slow_queue_size=%s reason=%s",
            job_id,
            lane,
            assets,
            self.fast_jobs.qsize(),
            self.slow_jobs.qsize(),
            job.get("reason"),
        )
        try:
            with self.state_lock:
                base_state = copy.deepcopy(self.state)
            worker_state = copy.deepcopy(base_state)

            rows = score_selected_assets(
                assets,
                self.manifest,
                self.bar_policy,
                worker_state,
                self.logger,
                scoring_lane=lane,
            )
            delta = _scoring_batch_state_delta_after_worker(
                assets=assets,
                base_state=base_state,
                worker_state=worker_state,
                rows=rows,
            )

            with self.state_lock:
                _merge_scoring_batch_state_delta(self.state, delta)
                elapsed = elapsed_ms(start_perf)
                approved_count = int(sum(1 for row in rows if getattr(row, "approved_signal", False)))
                self.last_job_summary = {
                    "job_id": job_id,
                    "lane": lane,
                    "assets": assets,
                    "created_at": job.get("created_at"),
                    "started_at": job_started_at,
                    "finished_at": utc_now(),
                    "elapsed_ms": elapsed,
                    "elapsed_seconds": elapsed / 1000.0,
                    "rows": int(len(rows)),
                    "approved_signal_count": approved_count,
                    "queue_size_after": self._pending_queue_size(),
                    "fast_queue_size_after": self.fast_jobs.qsize(),
                    "slow_queue_size_after": self.slow_jobs.qsize(),
                    "status": "done",
                }
                self.total_jobs_completed += 1
                self.state["last_async_scoring_job"] = dict(self.last_job_summary)
                self.state["last_async_scoring_queue_status"] = self.status_snapshot()
                write_runtime_outputs([], self.state, self.manifest, self.bar_policy)
            self.logger.info(
                "LIVE_SCORING_QUEUE_JOB_DONE job_id=%s lane=%s assets=%s elapsed_ms=%.3f rows=%s approved=%s fast_queue_size=%s slow_queue_size=%s",
                job_id,
                lane,
                assets,
                elapsed_ms(start_perf),
                len(rows),
                int(sum(1 for row in rows if getattr(row, "approved_signal", False))),
                self.fast_jobs.qsize(),
                self.slow_jobs.qsize(),
            )
        except Exception as exc:
            self.total_jobs_failed += 1
            self.last_job_summary = {
                "job_id": job_id,
                "lane": lane,
                "assets": assets,
                "created_at": job.get("created_at"),
                "started_at": job_started_at,
                "finished_at": utc_now(),
                "elapsed_ms": elapsed_ms(start_perf),
                "elapsed_seconds": elapsed_ms(start_perf) / 1000.0,
                "rows": 0,
                "approved_signal_count": 0,
                "queue_size_after": self._pending_queue_size(),
                "fast_queue_size_after": self.fast_jobs.qsize(),
                "slow_queue_size_after": self.slow_jobs.qsize(),
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            with self.state_lock:
                self.state["last_async_scoring_job"] = dict(self.last_job_summary)
                self.state["last_async_scoring_queue_status"] = self.status_snapshot()
            self.logger.error("LIVE_SCORING_QUEUE_JOB_FAILED job_id=%s lane=%s assets=%s error=%s", job_id, lane, assets, exc)
            self.logger.error(traceback.format_exc())
        finally:
            self._clear_active_job(lane)
            with self.state_lock:
                self.state["last_async_scoring_queue_status"] = self.status_snapshot()
            clear_runtime_batch_caches()

    def stop(self, *, wait: bool = True) -> None:
        if not self.enabled:
            return

        _DASHBOARD_SHUTDOWN_EVENT.set()
        self.stop_event.set()
        cancelled = 0
        with self.job_available:
            for q in (self.fast_jobs, self.slow_jobs):
                while True:
                    try:
                        pending = q.get_nowait()
                    except queue.Empty:
                        break
                    q.task_done()
                    if pending is not None:
                        cancelled += 1
            self.total_jobs_cancelled_on_shutdown += cancelled
            self.job_available.notify_all()

        self.logger.info(
            "LIVE_SCORING_QUEUE_SHUTDOWN_REQUESTED cancelled_pending_jobs=%s active_job_id=%s active_job_lane=%s",
            cancelled,
            self.active_job_id,
            self.active_job_lane,
        )

        if wait and self.threads:
            for lane, thread in list(self.threads.items()):
                thread.join(timeout=ASYNC_SCORING_QUEUE_JOIN_TIMEOUT_SECONDS)
                if thread.is_alive():
                    self.logger.warning(
                        "LIVE_SCORING_QUEUE_STOP_WAITING_FOR_ACTIVE_JOB lane=%s active_jobs=%s timeout_seconds=%s",
                        lane,
                        copy.deepcopy(self.active_jobs_by_lane),
                        ASYNC_SCORING_QUEUE_JOIN_TIMEOUT_SECONDS,
                    )
                    thread.join()

            self.logger.info(
                "LIVE_SCORING_QUEUE_STOPPED cancelled_pending_jobs=%s fast_queue_size=%s slow_queue_size=%s workers=%s",
                cancelled,
                self.fast_jobs.qsize(),
                self.slow_jobs.qsize(),
                sorted(self.threads),
            )





def _score_edge_parallel_worker(
    edge: dict[str, Any],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state_snapshot: dict[str, Any],
    logger,
    *,
    bars_snapshot: pd.DataFrame,
    live_event_times_snapshot: set[pd.Timestamp],
    shared_features_snapshot: pd.DataFrame | None = None,
    shared_features_builder_edge_id: str | None = None,
) -> dict[str, Any]:
    """Run one edge's expensive runtime path using an isolated state copy."""
    edge_id = str(edge.get("edge_id"))
    asset = str(edge.get("asset"))
    worker_state = copy.deepcopy(state_snapshot)
    with _RUNTIME_CALL_SEMAPHORE:
        rows = score_edge(
            edge,
            manifest,
            bar_policy,
            worker_state,
            logger,
            bars_snapshot=bars_snapshot,
            live_event_times_snapshot=live_event_times_snapshot,
            shared_features_snapshot=shared_features_snapshot,
            shared_features_builder_edge_id=shared_features_builder_edge_id,
        )
    return {
        "edge_id": edge_id,
        "asset": asset,
        "rows": rows,
        "state_delta": _edge_state_delta_after_parallel_worker(
            edge_id=edge_id,
            asset=asset,
            base_state=state_snapshot,
            worker_state=worker_state,
            rows=rows,
        ),
    }


def _score_selected_assets_sequential(
    assets_to_score: list[str],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state: dict[str, Any],
    logger,
    *,
    scoring_lane: str = "all",
    allowed_edge_ids: set[str] | None = None,
    _write_outputs: bool = True,
) -> list[DashboardSignalRow]:
    edges_by_asset = selected_edges_by_asset(manifest.get("selected_edges", []))
    signal_rows: list[DashboardSignalRow] = []
    batch_start_perf = time.perf_counter()
    lane = str(scoring_lane or "all").strip().lower()
    for asset in sorted(set(assets_to_score)):
        asset_start_perf = time.perf_counter()
        asset_signal_start = len(signal_rows)
        all_asset_edges = edges_by_asset.get(asset, [])
        asset_edges = filter_edges_for_scoring_lane(
            all_asset_edges,
            scoring_lane=lane,
            allowed_edge_ids=allowed_edge_ids,
        )
        if not asset_edges:
            logger.info(
                "LIVE_SCORING_LANE_ASSET_SKIP asset=%s lane=%s reason=no_edges_in_lane all_asset_edges=%s",
                asset,
                lane,
                len(all_asset_edges),
            )
            continue

        policy = bar_policy["bar_policies"][asset]
        snapshot_perf = time.perf_counter()
        bars_snapshot = combined_bars_for_asset(asset, policy)
        bars_snapshot_ms = elapsed_ms(snapshot_perf)

        live_times_perf = time.perf_counter()
        live_event_times_snapshot = latest_live_executable_timestamps(asset)
        live_times_ms = elapsed_ms(live_times_perf)

        if LATENCY_STAGE_LOGGING_ENABLED:
            logger.info(
                "LIVE_LATENCY_ASSET_SNAPSHOT asset=%s lane=%s combined_bars_ms=%.3f latest_live_timestamps_ms=%.3f bars=%s live_event_times=%s edges=%s all_asset_edges=%s",
                asset,
                lane,
                bars_snapshot_ms,
                live_times_ms,
                rows_len(bars_snapshot),
                rows_len(list(live_event_times_snapshot or [])),
                len(asset_edges),
                len(all_asset_edges),
            )

        shared_feature_prewarm_ok = True
        should_prewarm_shared_features = (
            PARALLEL_EDGE_SCORING_ENABLED
            and EDGE_WORKERS_PER_ASSET > 1
            and len(asset_edges) > 1
            and not bars_snapshot.empty
            and bool(live_event_times_snapshot)
        )
        if should_prewarm_shared_features:
            shared_feature_prewarm_ok = prewarm_shared_live_feature_continuation(asset, logger, bars_snapshot=bars_snapshot, state=state)
        elif asset_edges and not bars_snapshot.empty and bool(live_event_times_snapshot):
            logger.info(
                "LIVE_SHARED_FEATURE_PREWARM_BYPASS asset=%s reason=sequential_scoring_no_worker_file_race parallel_enabled=%s workers=%s edges=%s",
                asset,
                PARALLEL_EDGE_SCORING_ENABLED,
                EDGE_WORKERS_PER_ASSET,
                len(asset_edges),
            )

        shared_features_snapshot: pd.DataFrame | None = None
        shared_features_builder_edge_id: str | None = None
        if shared_feature_prewarm_ok:
            shared_features_snapshot, shared_features_builder_edge_id = build_shared_asset_runtime_features(
                asset=asset,
                asset_edges=asset_edges,
                bars_snapshot=bars_snapshot,
                live_event_times_snapshot=live_event_times_snapshot,
                state=state,
                logger=logger,
            )

        scoring_edges = order_edges_for_live_scoring(
            asset=asset,
            asset_edges=asset_edges,
            shared_features_snapshot=shared_features_snapshot,
            shared_features_builder_edge_id=shared_features_builder_edge_id,
            live_event_times_snapshot=live_event_times_snapshot,
            state=state,
            logger=logger,
        )

        if (
            PARALLEL_EDGE_SCORING_ENABLED
            and len(scoring_edges) > 1
            and EDGE_WORKERS_PER_ASSET > 1
            and not bars_snapshot.empty
            and bool(live_event_times_snapshot)
            and shared_feature_prewarm_ok
        ):
            worker_count = min(int(EDGE_WORKERS_PER_ASSET), len(scoring_edges))
            parallel_start_perf = time.perf_counter()
            logger.info(
                "LIVE_PARALLEL_EDGE_SCORING_START asset=%s edges=%s workers=%s bars=%s live_event_times=%s shared_feature_cache=%s builder_edge=%s mode=threaded_runtime_work_main_thread_state_merge",
                asset,
                len(scoring_edges),
                worker_count,
                rows_len(bars_snapshot),
                rows_len(list(live_event_times_snapshot or [])),
                shared_features_snapshot is not None,
                shared_features_builder_edge_id,
            )

            # Preload runtime modules in the main thread to avoid import/cache races.
            preload_perf = time.perf_counter()
            for edge in scoring_edges:
                get_runtime(edge)
            if LATENCY_STAGE_LOGGING_ENABLED:
                logger.info(
                    "LIVE_LATENCY_RUNTIME_PRELOAD asset=%s elapsed_ms=%.3f edges=%s",
                    asset,
                    elapsed_ms(preload_perf),
                    len(scoring_edges),
                )

            state_snapshot = copy.deepcopy(state)
            results_by_edge: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"live_edge_{asset}") as executor:
                future_to_edge_id = {
                    executor.submit(
                        _score_edge_parallel_worker,
                        edge,
                        manifest,
                        bar_policy,
                        state_snapshot,
                        logger,
                        bars_snapshot=bars_snapshot,
                        live_event_times_snapshot=live_event_times_snapshot,
                        shared_features_snapshot=shared_features_snapshot,
                        shared_features_builder_edge_id=shared_features_builder_edge_id,
                    ): str(edge.get("edge_id"))
                    for edge in scoring_edges
                }
                for future in as_completed(future_to_edge_id):
                    edge_id = future_to_edge_id[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.error("LIVE_PARALLEL_EDGE_SCORING_FAILED edge=%s asset=%s error=%s", edge_id, asset, exc)
                        logger.error(traceback.format_exc())
                        result = {"edge_id": edge_id, "asset": asset, "rows": [], "state_delta": {"edge_id": edge_id, "asset": asset}}
                    write_approved_signal_handoff(list(result.get("rows") or []), logger)
                    results_by_edge[edge_id] = result

            for edge in scoring_edges:
                edge_id = str(edge.get("edge_id"))
                result = results_by_edge.get(edge_id) or {"rows": [], "state_delta": {"edge_id": edge_id, "asset": asset}}
                _merge_parallel_edge_state_delta(state, result.get("state_delta") or {})
                signal_rows.extend(result.get("rows") or [])

            logger.info(
                "LIVE_PARALLEL_EDGE_SCORING_DONE asset=%s edges=%s workers=%s elapsed_ms=%.3f signals=%s",
                asset,
                len(scoring_edges),
                worker_count,
                elapsed_ms(parallel_start_perf),
                len(signal_rows) - asset_signal_start,
            )
        else:
            logger.info(
                "LIVE_PARALLEL_EDGE_SCORING_BYPASS asset=%s enabled=%s edges=%s workers=%s bars_empty=%s live_event_times=%s prewarm_ok=%s reason=sequential_fallback",
                asset,
                PARALLEL_EDGE_SCORING_ENABLED,
                len(scoring_edges),
                EDGE_WORKERS_PER_ASSET,
                bool(bars_snapshot.empty),
                rows_len(list(live_event_times_snapshot or [])),
                shared_feature_prewarm_ok,
            )
            for edge in scoring_edges:
                edge_rows = score_edge(
                    edge,
                    manifest,
                    bar_policy,
                    state,
                    logger,
                    bars_snapshot=bars_snapshot,
                    live_event_times_snapshot=live_event_times_snapshot,
                    shared_features_snapshot=shared_features_snapshot,
                    shared_features_builder_edge_id=shared_features_builder_edge_id,
                )
                write_approved_signal_handoff(edge_rows, logger)
                signal_rows.extend(edge_rows)
        if LATENCY_STAGE_LOGGING_ENABLED:
            logger.info(
                "LIVE_LATENCY_ASSET_BATCH asset=%s lane=%s elapsed_ms=%.3f edges=%s all_asset_edges=%s signals=%s parallel_enabled=%s workers=%s",
                asset,
                lane,
                elapsed_ms(asset_start_perf),
                len(asset_edges),
                len(all_asset_edges),
                len(signal_rows) - asset_signal_start,
                PARALLEL_EDGE_SCORING_ENABLED,
                EDGE_WORKERS_PER_ASSET,
            )
    if LATENCY_STAGE_LOGGING_ENABLED:
        logger.info(
            "LIVE_LATENCY_SCORE_BATCH assets=%s lane=%s elapsed_ms=%.3f signals=%s parallel_enabled=%s workers_per_asset=%s",
            sorted(set(assets_to_score)),
            lane,
            elapsed_ms(batch_start_perf),
            len(signal_rows),
            PARALLEL_EDGE_SCORING_ENABLED,
            EDGE_WORKERS_PER_ASSET,
        )
    if _write_outputs:
        write_runtime_outputs(signal_rows, state, manifest, bar_policy)
    return signal_rows


def _score_one_asset_bounded(
    *,
    asset: str,
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    base_state: dict[str, Any],
    logger,
    scoring_lane: str,
    allowed_edge_ids: set[str] | None,
) -> dict[str, Any]:
    """Score one asset under the global concurrency bound using isolated state."""
    with _ASSET_SCORING_SEMAPHORE:
        worker_state = copy.deepcopy(base_state)
        started = time.perf_counter()
        rows = _score_selected_assets_sequential(
            [asset],
            manifest,
            bar_policy,
            worker_state,
            logger,
            scoring_lane=scoring_lane,
            allowed_edge_ids=allowed_edge_ids,
            _write_outputs=False,
        )
        return {
            "asset": asset,
            "rows": rows,
            "worker_state": worker_state,
            "elapsed_ms": elapsed_ms(started),
        }


def score_selected_assets(
    assets_to_score: list[str],
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state: dict[str, Any],
    logger,
    *,
    scoring_lane: str = "all",
    allowed_edge_ids: set[str] | None = None,
) -> list[DashboardSignalRow]:
    """Score at most two assets concurrently while preserving edge-owned logic.

    Each asset receives an isolated dashboard-state snapshot. Only the existing
    deterministic scoring delta is merged back after completion; MT5 ingestion
    remains in the main thread and approved handoff remains immediate.
    """
    assets = sorted({str(asset) for asset in assets_to_score if asset})
    if (
        not ASSET_SCORING_PARALLEL_ENABLED
        or ASSET_SCORING_WORKERS <= 1
        or len(assets) <= 1
    ):
        with _ASSET_SCORING_SEMAPHORE:
            return _score_selected_assets_sequential(
                assets,
                manifest,
                bar_policy,
                state,
                logger,
                scoring_lane=scoring_lane,
                allowed_edge_ids=allowed_edge_ids,
                _write_outputs=True,
            )

    batch_start = time.perf_counter()
    edges_by_asset = selected_edges_by_asset(manifest.get("selected_edges", []))
    # Import in the caller thread to avoid concurrent module-import side effects.
    for asset in assets:
        for edge in filter_edges_for_scoring_lane(
            edges_by_asset.get(asset, []),
            scoring_lane=str(scoring_lane or "all").strip().lower(),
            allowed_edge_ids=allowed_edge_ids,
        ):
            get_runtime(edge)

    base_state = copy.deepcopy(state)
    rows_by_asset: dict[str, list[DashboardSignalRow]] = {}
    results_by_asset: dict[str, dict[str, Any]] = {}
    worker_count = min(int(ASSET_SCORING_WORKERS), len(assets))
    logger.info(
        "LIVE_PARALLEL_ASSET_SCORING_START lane=%s assets=%s workers=%s global_asset_concurrency_limit=%s",
        scoring_lane,
        assets,
        worker_count,
        ASSET_SCORING_WORKERS,
    )
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="live_asset") as executor:
        future_to_asset = {
            executor.submit(
                _score_one_asset_bounded,
                asset=asset,
                manifest=manifest,
                bar_policy=bar_policy,
                base_state=base_state,
                logger=logger,
                scoring_lane=scoring_lane,
                allowed_edge_ids=allowed_edge_ids,
            ): asset
            for asset in assets
        }
        for future in as_completed(future_to_asset):
            asset = future_to_asset[future]
            try:
                result = future.result()
            except Exception as exc:
                logger.error(
                    "LIVE_PARALLEL_ASSET_SCORING_FAILED asset=%s lane=%s error=%s",
                    asset,
                    scoring_lane,
                    exc,
                )
                logger.error(traceback.format_exc())
                result = {
                    "asset": asset,
                    "rows": [],
                    "worker_state": copy.deepcopy(base_state),
                    "elapsed_ms": 0.0,
                }
            results_by_asset[asset] = result
            rows_by_asset[asset] = list(result.get("rows") or [])
            logger.info(
                "LIVE_PARALLEL_ASSET_SCORING_ASSET_DONE asset=%s lane=%s elapsed_ms=%.3f rows=%s approved=%s",
                asset,
                scoring_lane,
                float(result.get("elapsed_ms", 0.0) or 0.0),
                len(rows_by_asset[asset]),
                sum(1 for row in rows_by_asset[asset] if getattr(row, "approved_signal", False)),
            )

    all_rows: list[DashboardSignalRow] = []
    for asset in assets:
        result = results_by_asset.get(asset) or {
            "worker_state": copy.deepcopy(base_state),
            "rows": [],
        }
        asset_rows = list(result.get("rows") or [])
        delta = _scoring_batch_state_delta_after_worker(
            assets=[asset],
            base_state=base_state,
            worker_state=result.get("worker_state") or copy.deepcopy(base_state),
            rows=asset_rows,
        )
        _merge_scoring_batch_state_delta(state, delta)
        all_rows.extend(asset_rows)

    write_runtime_outputs(all_rows, state, manifest, bar_policy)
    logger.info(
        "LIVE_PARALLEL_ASSET_SCORING_DONE lane=%s assets=%s workers=%s elapsed_ms=%.3f rows=%s approved=%s",
        scoring_lane,
        assets,
        worker_count,
        elapsed_ms(batch_start),
        len(all_rows),
        sum(1 for row in all_rows if getattr(row, "approved_signal", False)),
    )
    return all_rows



_ASSET_TRANSACTION_MAP_KEYS = (
    "current_bars",
    "raw_daily_tick_counts_by_asset",
    "raw_daily_tick_profile_audit_by_asset",
    "closed_bar_counts",
    "asset_continuity",
)


def snapshot_asset_tick_transaction_state(
    asset: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    key = str(asset).upper()
    snapshot: dict[str, Any] = {
        "asset": key,
        "total_closed_bars_since_state_created": int(
            state.get("total_closed_bars_since_state_created", 0) or 0
        ),
    }
    for map_name in _ASSET_TRANSACTION_MAP_KEYS:
        mapping = state.get(map_name)
        exists = isinstance(mapping, dict) and key in mapping
        snapshot[map_name] = {
            "exists": bool(exists),
            "value": copy.deepcopy(mapping.get(key)) if exists else None,
        }
    return snapshot


def restore_asset_tick_transaction_state(
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> None:
    key = str(snapshot.get("asset") or "").upper()
    if not key:
        return
    for map_name in _ASSET_TRANSACTION_MAP_KEYS:
        item = snapshot.get(map_name) or {}
        mapping = state.setdefault(map_name, {})
        if item.get("exists"):
            mapping[key] = copy.deepcopy(item.get("value"))
        else:
            mapping.pop(key, None)
    state["total_closed_bars_since_state_created"] = int(
        snapshot.get("total_closed_bars_since_state_created", 0) or 0
    )



def run_once(
    mt5: Any,
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state: dict[str, Any],
    logger,
    *,
    force_score_existing_live_bars: bool = False,
    scoring_queue: ScoringQueueManager | None = None,
) -> int:
    policies = bar_policy["bar_policies"]
    assets = active_assets_from_manifest(manifest, bar_policy)
    closed_assets: list[str] = []
    for asset in assets:
        symbol = broker_symbol_for_asset(asset)
        # Canonical CSV compaction is optional for correctness because readers
        # merge the durable journal. Retry it opportunistically without blocking
        # tick ingestion or allowing a Windows sharing lock to terminate the app.
        try:
            compact_pending_live_bars(asset, state, logger, force=False)
        except LiveBarJournalError as exc:
            _refresh_bar_persistence_health(
                asset,
                state,
                status="journal_read_failed",
                error=f"{type(exc).__name__}:{exc}",
            )
            logger.error(
                "LIVE_BAR_ASSET_IO_PAUSED asset=%s symbol=%s reason=pending_journal_unreadable error=%s",
                asset,
                symbol,
                exc,
            )
            continue

        ticks_df = fetch_ticks(mt5, asset, symbol, state, logger)
        state.setdefault("last_ticks_fetched_raw_by_asset", {})[asset] = int(0 if ticks_df is None else len(ticks_df))
        ticks_df = apply_live_tick_startup_guard(asset, symbol, ticks_df, state, logger)
        state.setdefault("last_ticks_fetched_by_asset", {})[asset] = int(0 if ticks_df is None else len(ticks_df))
        last_norm = state.setdefault("last_normalized_tick_time_utc", {}).get(symbol)
        if last_norm:
            try:
                state.setdefault("last_tick_age_seconds_by_asset", {})[asset] = float((utc_now_ts() - ensure_utc_timestamp(last_norm)).total_seconds())
            except Exception:
                pass
        if ticks_df.empty:
            continue
        executable = continuity_ok(asset, state)
        if not executable:
            logger.warning("LIVE_BAR_MARKED_NON_EXECUTABLE_CONTINUITY asset=%s continuity=%s", asset, ((state.get("asset_continuity") or {}).get(asset) or {}).get("status"))

        transaction_snapshot = snapshot_asset_tick_transaction_state(asset, state)
        try:
            closed = process_ticks_to_bars(
                asset,
                symbol,
                policies[asset],
                ticks_df,
                state,
                logger,
                gap_healed=False,
                executable=executable,
            )
        except LiveBarJournalError as exc:
            restore_asset_tick_transaction_state(transaction_snapshot, state)
            _refresh_bar_persistence_health(
                asset,
                state,
                status="journal_write_failed",
                error=f"{type(exc).__name__}:{exc}",
            )
            logger.error(
                "LIVE_BAR_ASSET_IO_DEFERRED asset=%s symbol=%s rows=%s cursor_advanced=False state_rolled_back=True error=%s",
                asset,
                symbol,
                len(ticks_df),
                exc,
            )
            # Do not advance this symbol's cursor. The same de-duplicated ticks
            # will be retried, while all other assets and scoring workers remain
            # alive.
            continue

        update_last_tick(symbol, ticks_df, state, source="live_poll", logger=logger)
        state.setdefault("asset_io_paused_by_asset", {})[str(asset).upper()] = False
        if closed and executable:
            closed_assets.extend([asset] * int(closed))
    assets_to_score = sorted(set(closed_assets))
    if force_score_existing_live_bars and not assets_to_score:
        assets_to_score = assets

    if assets_to_score:
        if scoring_queue is not None and scoring_queue.enabled:
            scoring_queue.enqueue(assets_to_score, reason="closed_executable_live_bar_or_forced_score_existing")
        else:
            score_selected_assets(assets_to_score, manifest, bar_policy, state, logger)
    elif scoring_queue is None or not scoring_queue.enabled:
        # Preserve synchronous-mode heartbeat/status behavior without doing any
        # work. Async mode avoids this no-op call so the tick loop stays lean.
        score_selected_assets([], manifest, bar_policy, state, logger)
    return len(closed_assets)



def _operator_format_time(value: Any) -> str:
    """Format timestamps for the human terminal dashboard only."""
    if value in (None, "", "None"):
        return "none"
    return str(value).replace("T", " ").replace("+00:00", " UTC")


def _operator_format_age_seconds(value: Any) -> str:
    """Format tick age for terminal readability only."""
    try:
        if value is None or pd.isna(value):
            return "unknown"
        age = float(value)
        if not math.isfinite(age):
            return "unknown"
        if age < 60.0:
            return f"{age:.1f}s"
        return f"{age / 60.0:.1f}m"
    except Exception:
        return "unknown"


def _operator_int(value: Any, default: int = 0) -> int:
    """Best-effort integer conversion for terminal summaries only."""
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _operator_active_thresholds(assets: list[str], bar_policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return compact active threshold audit records for selected assets."""
    return {
        asset: policy_threshold_audit(asset, bar_policy["bar_policies"][asset])
        for asset in assets
        if asset in bar_policy.get("bar_policies", {})
    }


def format_dashboard_ready_summary(
    *,
    assets: list[str],
    selected_edges_count: int,
    manifest: dict[str, Any],
    bar_policy: dict[str, Any],
    state: dict[str, Any],
    queue_status: dict[str, Any] | None = None,
) -> str:
    """Human-readable startup summary for terminal/log output.

    This function is display-only. It does not mutate state, bars, thresholds,
    runtime scoring, signal approval, or execution contracts.
    """

    continuity = state.get("asset_continuity", {}) or {}
    partial_bars = partial_bar_progress_by_asset(assets, bar_policy, state)
    active_thresholds = _operator_active_thresholds(assets, bar_policy)
    queue_status = queue_status or state.get("last_async_scoring_queue_status", {}) or {}

    lines: list[str] = []
    lines.append("=" * 88)
    lines.append(f"LIVE DASHBOARD READY | {utc_now()}")
    lines.append("=" * 88)
    lines.append(f"Assets: {', '.join(assets)}")
    lines.append(f"Selected edges: {selected_edges_count}")
    preload = state.get("last_edge_artifact_preload", {}) or {}
    if preload:
        lines.append(
            f"Edge artifact preload: {preload.get('status')} | "
            f"preloaded={preload.get('preloaded_edges', 0)}/{preload.get('selected_edge_count', selected_edges_count)} | "
            f"failed={preload.get('failed_edges', 0)}"
        )
    lines.append(f"Manifest live trade enabled: {manifest.get('live_trade_enabled')}")
    lines.append(
        f"Bar policy: {bar_policy.get('policy_gate_status')} | "
        f"live_policy_ready={bar_policy.get('live_policy_ready')}"
    )
    lines.append(
        f"Runtime recovery bars: {MAX_COMBINED_BARS_FOR_RUNTIME} | "
        "hot path=runtime-owned stateful continuation"
    )
    lines.append(
        "Research state: "
        f"FFD={RESEARCH_FFD_WINDOW_BARS}/{RESEARCH_FFD_LIVE_BUFFER_BARS} raw | "
        f"HMM train={RESEARCH_HMM_TRAIN_WINDOW_ROWS}, infer={RESEARCH_HMM_INFERENCE_LOOKBACK_ROWS}, "
        f"refit={RESEARCH_HMM_REFIT_FREQUENCY_BARS} bars"
    )
    lines.append("Dashboard order sending: OFF - signals are for portfolio_execution_bot.py")

    lines.append("")
    lines.append("CONTINUITY")
    for asset in assets:
        rec = continuity.get(asset, {}) or {}
        status = str(rec.get("status", "UNKNOWN"))
        marker = "OK" if status.startswith("PASS") else "CHECK"
        latest_live = _operator_format_time(rec.get("latest_live"))
        lines.append(f"  {asset:<8} {marker:<5} {status:<26} latest_live={latest_live}")

    lines.append("")
    lines.append("BAR PROGRESS AT START")
    lines.append("  Asset    Last closed bar UTC          Current/Threshold     Progress   Ticks left")
    for asset in assets:
        rec = partial_bars.get(asset, {}) or {}
        current = _operator_int(rec.get("current_bar_ticks"))
        threshold = _operator_int(rec.get("bar_threshold_ticks"))
        left = _operator_int(rec.get("ticks_to_next_bar"), max(threshold - current, 0))
        progress = (current / threshold * 100.0) if threshold > 0 else 0.0
        latest = _operator_format_time(rec.get("latest_bar_time"))
        lines.append(f"  {asset:<8} {latest:<28} {current:>5}/{threshold:<8} {progress:>6.1f}%   {left:>6}")

    lines.append("")
    lines.append("ACTIVE THRESHOLDS")
    for asset in assets:
        rec = active_thresholds.get(asset, {}) or {}
        lines.append(
            f"  {asset:<8} threshold={rec.get('active_threshold_ticks')} ticks | "
            f"source={rec.get('active_threshold_source')} | "
            f"warmup={rec.get('warmup_ready_from_live_history')} | "
            f"profile_days={rec.get('completed_daily_profile_days')}"
        )

    lines.append("=" * 88)
    return "\n".join(lines)


def format_human_heartbeat(
    *,
    loop: int,
    assets: list[str],
    state: dict[str, Any],
    bar_policy: dict[str, Any],
    manifest: dict[str, Any],
    queue_status: dict[str, Any] | None = None,
) -> str:
    """Human-readable heartbeat for terminal/log output.

    This function is display-only. It does not mutate state, bars, thresholds,
    runtime scoring, signal approval, or execution contracts.
    """

    continuity = state.get("asset_continuity", {}) or {}
    partial_bars = partial_bar_progress_by_asset(assets, bar_policy, state)
    ticks_fetched = state.get("last_ticks_fetched_by_asset", {}) or {}
    tick_age_seconds = state.get("last_tick_age_seconds_by_asset", {}) or {}
    closed_counts = state.get("closed_bar_counts", {}) or {}
    total_signals = int(state.get("total_signals_emitted", 0) or 0)
    active_thresholds = _operator_active_thresholds(assets, bar_policy)
    queue_status = queue_status or state.get("last_async_scoring_queue_status", {}) or {}

    lines: list[str] = []
    lines.append("=" * 88)
    lines.append(f"LIVE DASHBOARD HEARTBEAT | {utc_now()} | loop={loop}")
    lines.append("=" * 88)

    lines.append("MODE")
    lines.append(f"  Manifest live trade enabled: {manifest.get('live_trade_enabled')}")
    lines.append(
        f"  Bar policy: {bar_policy.get('policy_gate_status')} | "
        f"live_policy_ready={bar_policy.get('live_policy_ready')}"
    )
    lines.append("  Dashboard sends orders: NO - it only builds bars, scores runtimes, and emits approved signals")
    lines.append("  Order owner: portfolio_execution_bot.py")
    preload = state.get("last_edge_artifact_preload", {}) or {}
    if preload:
        cache = preload.get("cache_snapshot", {}) or {}
        stats = cache.get("stats", {}) or {}
        lines.append(
            f"  Edge artifact preload: {preload.get('status')} | "
            f"preloaded={preload.get('preloaded_edges', 0)}/{preload.get('selected_edge_count', 0)} | "
            f"cache_entries={cache.get('cache_entries', 0)} | hits={stats.get('hits', 0)} | loads={stats.get('loads', 0)}"
        )

    lines.append("")
    lines.append("SIGNALS")
    lines.append(f"  Total emitted this state: {total_signals}")
    if total_signals <= 0:
        lines.append("  Status: waiting for a new executable bar that also creates a valid edge event")
    else:
        lines.append("  Status: signal(s) emitted; check live_approved_signals outputs and the execution bot")

    lines.append("")
    lines.append("SCORING QUEUE")
    queue_enabled = bool(queue_status.get("enabled", False))
    active_job_id = queue_status.get("active_job_id")
    active_lane = queue_status.get("active_job_lane") or "none"
    active_assets = queue_status.get("active_job_assets") or []
    active_age = _operator_format_age_seconds(queue_status.get("active_job_age_seconds"))
    pending_size = _operator_int(queue_status.get("pending_queue_size"))
    pending_fast = _operator_int(queue_status.get("pending_fast_queue_size"))
    pending_slow = _operator_int(queue_status.get("pending_slow_queue_size"))
    max_pending = _operator_int(queue_status.get("max_pending_jobs"))
    max_fast = _operator_int(queue_status.get("max_pending_fast_jobs"))
    max_slow = _operator_int(queue_status.get("max_pending_slow_jobs"))
    last_elapsed = _operator_format_age_seconds(queue_status.get("last_job_elapsed_seconds"))
    last_rows = _operator_int(queue_status.get("last_job_rows"))
    last_approved = _operator_int(queue_status.get("last_job_approved"))
    lines.append(f"  Enabled: {queue_enabled} | slow_lane={bool(queue_status.get('slow_lane_enabled', False))} families={queue_status.get('slow_lane_families') or []}")
    lines.append(f"  Active job: {active_job_id or 'none'} | lane={active_lane} | assets={active_assets or []} | age={active_age}")
    lines.append(f"  Pending jobs: {pending_size}/{max_pending} | fast={pending_fast}/{max_fast} | slow={pending_slow}/{max_slow}")
    lines.append(f"  Last job: elapsed={last_elapsed} | rows={last_rows} | approved={last_approved}")
    lines.append(
        f"  Totals: started={_operator_int(queue_status.get('total_jobs_started'))} | "
        f"completed={_operator_int(queue_status.get('total_jobs_completed'))} | "
        f"failed={_operator_int(queue_status.get('total_jobs_failed'))} | "
        f"slow_skipped={_operator_int(queue_status.get('total_slow_jobs_skipped'))}"
    )

    lines.append("")
    lines.append("CONTINUITY")
    for asset in assets:
        rec = continuity.get(asset, {}) or {}
        status = str(rec.get("status", "UNKNOWN"))
        marker = "OK" if status.startswith("PASS") else "CHECK"
        latest_live = _operator_format_time(rec.get("latest_live"))
        lines.append(f"  {asset:<8} {marker:<5} {status:<26} latest_live={latest_live}")

    lines.append("")
    lines.append("LIVE BAR PROGRESS")
    lines.append("  Asset    Last closed bar UTC          Current/Threshold     Progress   Ticks left   Closed bars")
    for asset in assets:
        rec = partial_bars.get(asset, {}) or {}
        current = _operator_int(rec.get("current_bar_ticks"))
        threshold = _operator_int(rec.get("bar_threshold_ticks"))
        left = _operator_int(rec.get("ticks_to_next_bar"), max(threshold - current, 0))
        progress = (current / threshold * 100.0) if threshold > 0 else 0.0
        latest = _operator_format_time(rec.get("latest_bar_time"))
        closed = _operator_int(closed_counts.get(asset))
        lines.append(
            f"  {asset:<8} {latest:<28} {current:>5}/{threshold:<8} "
            f"{progress:>6.1f}%   {left:>6}       {closed:>6}"
        )

    lines.append("")
    lines.append("TICKS")
    for asset in assets:
        fetched = _operator_int(ticks_fetched.get(asset))
        age = _operator_format_age_seconds(tick_age_seconds.get(asset))
        lines.append(f"  {asset:<8} fetched_this_loop={fetched:<5} last_tick_age={age}")

    lines.append("")
    lines.append("BAR PERSISTENCE")
    persistence = state.get("bar_persistence_status_by_asset", {}) or {}
    pending_counts = state.get("pending_bar_count_by_asset", {}) or {}
    pending_ages = state.get("oldest_pending_bar_age_seconds_by_asset", {}) or {}
    io_paused = state.get("asset_io_paused_by_asset", {}) or {}
    for asset in assets:
        rec = persistence.get(asset, {}) or {}
        lines.append(
            f"  {asset:<8} status={rec.get('status', 'unknown')} | "
            f"pending={_operator_int(pending_counts.get(asset))} | "
            f"oldest={_operator_format_age_seconds(pending_ages.get(asset))} | "
            f"io_paused={bool(io_paused.get(asset, False))}"
        )

    lines.append("")
    lines.append("ACTIVE THRESHOLDS")
    for asset in assets:
        rec = active_thresholds.get(asset, {}) or {}
        lines.append(
            f"  {asset:<8} threshold={rec.get('active_threshold_ticks')} ticks | "
            f"source={rec.get('active_threshold_source')} | "
            f"warmup={rec.get('warmup_ready_from_live_history')} | "
            f"profile_days={rec.get('completed_daily_profile_days')}"
        )

    lines.append("")
    lines.append("MEANING")
    lines.append("  OK Broker tick loop is alive if tick ages stay fresh")
    lines.append("  OK Continuity is acceptable when every asset shows PASS_*")
    lines.append("  OK Runtime scoring is triggered only after executable bars close")
    if total_signals <= 0:
        lines.append("  WAIT No approved signal yet; this is normal until an edge event passes its threshold")
    else:
        lines.append("  ACTION Approved signal exists; check execution bot / approved signal files")

    lines.append("=" * 88)
    return "\n".join(lines)

def startup_shared_continuation_warmup(
    manifest: dict[str, Any],
    logger,
) -> dict[str, Any]:
    """Fail closed on the exact FFD/HMM continuation proofs before live polling."""
    enabled = str(os.environ.get("LIVE_STREAMING_STARTUP_WARMUP", "1")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    if not enabled:
        report = {"enabled": False, "assets": {}}
        logger.warning("LIVE_STREAMING_STARTUP_WARMUP disabled=True source=environment")
        return report

    assets = sorted(
        {
            str(edge.get("asset", "")).upper()
            for edge in manifest.get("selected_edges", [])
            if edge.get("asset")
        }
    )
    report: dict[str, Any] = {"enabled": True, "assets": {}}
    for asset in assets:
        started = time.perf_counter()
        ffd = verify_asset_ffd_manifest_hot(asset)
        ffd_seconds = time.perf_counter() - started
        started_hmm = time.perf_counter()
        hmm = verify_asset_hmm_regime_manifest_hot(asset)
        hmm_seconds = time.perf_counter() - started_hmm
        required = {
            "ffd_streaming_parity_passed": bool(ffd.get("streaming_parity_passed")),
            "hmm_streaming_parity_passed": bool(hmm.get("streaming_parity_passed")),
            "ffd_append_only_history": bool(ffd.get("append_only_history")),
            "hmm_append_only_history": bool(hmm.get("append_only_history")),
            "ffd_restart_parity_passed": bool(ffd.get("restart_parity_passed")),
            "hmm_restart_parity_passed": bool(hmm.get("restart_parity_passed")),
            "hmm_refit_schedule_preserved": bool(
                hmm.get("hmm_refit_schedule_preserved")
            ),
            "deterministic_noise_contract_preserved": bool(
                hmm.get("deterministic_noise_contract_preserved")
            ),
        }
        missing = [name for name, passed in required.items() if not passed]
        if missing:
            raise RuntimeError(
                f"{asset}: startup continuation proof failed: {missing}"
            )
        asset_report = {
            **required,
            "ffd_seconds": round(ffd_seconds, 6),
            "hmm_seconds": round(hmm_seconds, 6),
            "total_seconds": round(time.perf_counter() - started, 6),
        }
        report["assets"][asset] = asset_report
        logger.info(
            "LIVE_STREAMING_STARTUP_WARMUP asset=%s status=PASS ffd_seconds=%.6f "
            "hmm_seconds=%.6f total_seconds=%.6f",
            asset,
            asset_report["ffd_seconds"],
            asset_report["hmm_seconds"],
            asset_report["total_seconds"],
        )
    return report



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one polling/scoring cycle and exit.")
    parser.add_argument("--max-loops", type=int, default=None, help="Stop after N loops. Omit for continuous dashboard mode.")
    parser.add_argument("--skip-gap-heal", action="store_true", help="Skip startup gap healing for this run only; runtime scoring remains blocked unless continuity was previously verified.")
    parser.add_argument("--score-existing-live-bars", action="store_true", help="Force runtime scoring of existing executable live bars if continuity is verified.")
    args = parser.parse_args()
    ensure_runtime_dirs()
    logger = setup_logger("portfolio_live_dashboard_orchestrator", LOG_PATH)
    logger.info("=" * 100)
    logger.info("V2 PORTFOLIO LIVE DASHBOARD ORCHESTRATOR version=%s", DASHBOARD_VERSION)
    logger.info("orders_sent_by_dashboard=False runtime_scoring_owner=edge_runtime.py continuity_required=True")
    logger.info("single_dashboard_entrypoint=True launcher=portfolio_live_dashboard_orchestrator.py")
    logger.info("=" * 100)
    start_noncritical_io_writer(logger)
    manifest = load_live_manifest()
    startup_warmup = startup_shared_continuation_warmup(manifest, logger)
    logger.info(
        "LIVE_STREAMING_HOTPATH_ENABLED install=%s startup_warmup=%s cache=%s "
        "single_dashboard_entrypoint=True parallel_edges=%s edge_workers=%s "
        "parallel_assets=%s asset_workers=%s max_runtime_calls=%s "
        "slow_lane_concurrent=%s prewarm_each_bar=%s threshold_refresh_seconds=86400",
        _HOTPATH_INSTALL_REPORT,
        startup_warmup,
        live_streaming_cache_status(),
        PARALLEL_EDGE_SCORING_ENABLED,
        EDGE_WORKERS_PER_ASSET,
        ASSET_SCORING_PARALLEL_ENABLED,
        ASSET_SCORING_WORKERS,
        MAX_CONCURRENT_RUNTIME_CALLS,
        SLOW_LANE_SCORING_ENABLED,
        SHARED_FEATURE_PREWARM_ENABLED,
    )
    bar_policy = load_bar_policy()
    validate_live_inputs(manifest, bar_policy)
    validate_dashboard_signal_row_factory(manifest)
    logger.info("DASHBOARD_SIGNAL_ROW_SCHEMA_SMOKE_TEST status=PASS contract_fields=5")
    selected_edges = manifest.get("selected_edges", [])
    assets = active_assets_from_manifest(manifest, bar_policy)
    state, state_created = load_state()
    preload_live_edge_artifacts(manifest, state, logger)
    state_lock = threading.RLock()
    scoring_queue = ScoringQueueManager(
        manifest=manifest,
        bar_policy=bar_policy,
        state=state,
        logger=logger,
        state_lock=state_lock,
    )
    mt5, account = init_mt5()
    logger.info("MT5_CONNECTED login=%s balance=%s equity=%s server=%s", account.login, account.balance, account.equity, account.server)
    try:
        validate_symbols(mt5, assets, logger)
        prime_broker_time_offsets(mt5, assets, state, logger)
        pre_gap_live_cursor_snapshot = _snapshot_live_tick_cursors(state)
        if state_created:
            initialize_edge_cursors_on_new_state(manifest, state, logger)
        if not args.skip_gap_heal:
            gap_rows = run_startup_gap_healing(mt5, manifest, bar_policy, state, logger)
            logger.info("GAP_HEALING_COMPLETE rows=%d state_created=%s", len(gap_rows), state_created)
            enforce_continuity_or_fail(assets, state)
            save_state(state)
        else:
            for asset in assets:
                if asset not in state.setdefault("asset_continuity", {}):
                    state["asset_continuity"][asset] = verify_asset_continuity(asset, bar_policy["bar_policies"][asset], state)
            logger.warning("GAP_HEALING_SKIPPED runtime scoring will remain blocked for assets without PASS continuity")
        reset_gap_healed_partial_bars_before_live(assets, state, logger)
        prime_live_tick_cursors_after_gap_healing(
            mt5,
            assets,
            state,
            logger,
            pre_gap_snapshot=pre_gap_live_cursor_snapshot,
        )
        compact_all_pending_live_bars(
            assets,
            state,
            logger,
            force=True,
        )
        refresh_all_threshold_policies(assets, bar_policy, state, logger, reason="dashboard_startup_after_gap_heal_or_verified_continuity", force=True)
        logger.info(
            "\n%s",
            format_dashboard_ready_summary(
                assets=assets,
                selected_edges_count=len(selected_edges),
                manifest=manifest,
                bar_policy=bar_policy,
                state=state,
            ),
        )
        state["last_async_scoring_queue_status"] = scoring_queue.status_snapshot()
        write_runtime_outputs([], state, manifest, bar_policy)
        write_dashboard_bar_ack_best_effort(
            state,
            manifest,
            bar_policy,
            logger,
        )
        scoring_queue.start()
        state["last_async_scoring_queue_status"] = scoring_queue.status_snapshot()
        loop = 0
        closed_since_save = 0
        while True:
            loop += 1
            with state_lock:
                refresh_all_threshold_policies(assets, bar_policy, state, logger, reason="dashboard_loop_periodic_or_new_day", force=False)
                closed = run_once(
                    mt5,
                    manifest,
                    bar_policy,
                    state,
                    logger,
                    force_score_existing_live_bars=bool(args.score_existing_live_bars and loop == 1),
                    scoring_queue=scoring_queue,
                )
                closed_since_save += int(closed)
                if int(closed) > 0:
                    write_dashboard_bar_ack_best_effort(
                        state,
                        manifest,
                        bar_policy,
                        logger,
                    )
                if closed_since_save >= SAVE_STATE_EVERY_N_CLOSED_BARS:
                    save_state(state)
                    closed_since_save = 0
                if loop % HEARTBEAT_EVERY_N_LOOPS == 0:
                    queue_status = scoring_queue.status_snapshot()
                    state["last_async_scoring_queue_status"] = queue_status
                    logger.info(
                        "\n%s",
                        format_human_heartbeat(
                            loop=loop,
                            assets=assets,
                            state=state,
                            bar_policy=bar_policy,
                            manifest=manifest,
                            queue_status=queue_status,
                        ),
                    )
                    compact_all_pending_live_bars(
                        assets,
                        state,
                        logger,
                        force=False,
                    )
                    write_runtime_outputs([], state, manifest, bar_policy)
                    save_state(state)
                    if scoring_queue.active_job_id is None:
                        clear_runtime_batch_caches()
            if args.once or (args.max_loops is not None and loop >= int(args.max_loops)):
                break
            time.sleep(TICK_POLL_SECONDS)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Saving state and stopping.")
        _DASHBOARD_SHUTDOWN_EVENT.set()
    finally:
        _DASHBOARD_SHUTDOWN_EVENT.set()
        try:
            scoring_queue.stop(wait=True)
        except Exception as exc:
            logger.warning("LIVE_SCORING_QUEUE_STOP_WARN error=%s", exc)
        try:
            stop_noncritical_io_writer(logger, wait=True)
        except Exception as exc:
            logger.warning("LIVE_NONCRITICAL_IO_STOP_WARN error=%s", exc)
        with state_lock:
            compact_all_pending_live_bars(
                assets,
                state,
                logger,
                force=True,
            )
            save_state(state)
        shutdown_mt5(mt5)
        logger.info("Dashboard stopped. State saved: %s", STATE_PATH)


if __name__ == "__main__":
    main()
