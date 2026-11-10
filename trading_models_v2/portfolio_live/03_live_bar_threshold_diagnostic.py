from __future__ import annotations

"""V2 Portfolio Live 03 — live bar threshold diagnostic.

This script diagnoses the live MT5 tick stream and writes the live bar policy
that the dashboard must use later. It does not score, trade, engineer edge
features, or place orders.

SOTD contract: research/live bars use standard_quote_tick bars, threshold_unit
is ticks, and the live policy is adaptive/causal by completed daily tick counts.
The scalar threshold printed by this script is only the initial/fallback value;
it is not the whole live policy.
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import hashlib
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

SCHEMA_VERSION = "v2.1"
DIAGNOSTIC_LOGIC_VERSION = "v2_causal_active_day_cadence_review"
STEP_NAME = "V2 Portfolio Live 03 — live bar threshold diagnostic"

LIVE_MANIFEST_PATH = Path(getattr(settings, "LIVE_MANIFEST_PATH", LIVE_DIR / "live_manifest.json")).resolve()
LOCAL_SETTINGS_PATH = Path(getattr(settings, "LOCAL_LIVE_SETTINGS_PATH", LIVE_DIR / "local_live_settings.json")).resolve()
OUTPUT_DIR = Path(getattr(settings, "OUTPUT_DIR", LIVE_DIR / "outputs")).resolve() / "threshold_diagnostics"
LOG_DIR = Path(getattr(settings, "LIVE_LOGS_DIR", LIVE_DIR / "live_logs")).resolve()
SUMMARY_CSV = OUTPUT_DIR / "threshold_diagnostic_summary.csv"
MANIFEST_JSON = OUTPUT_DIR / "threshold_diagnostic_manifest.json"
MANIFEST_VALIDATION_REPORT = Path(getattr(settings, "LIVE_MANIFEST_VALIDATION_REPORT_PATH", LIVE_DIR / "live_manifest_validation_report.json")).resolve()
ENVIRONMENT_VALIDATION_REPORT = Path(getattr(settings, "LIVE_ENVIRONMENT_VALIDATION_REPORT_PATH", LIVE_DIR / "live_environment_validation_report.json")).resolve()

TARGET_BARS_PER_DAY = int(getattr(settings, "THRESHOLD_TARGET_BARS_PER_DAY", 50))
EWMA_SPAN_DAYS = int(getattr(settings, "DAILY_TICK_EWMA_SPAN_DAYS", 20))
MIN_WARMUP_DAYS = int(getattr(settings, "MIN_WARMUP_DAYS", 20))
WARMUP_DAYS_BY_ASSET = dict(
    getattr(settings, "LIVE_THRESHOLD_MIN_WARMUP_DAYS_BY_ASSET", {})
)
LOOKBACK_HOURS = int(getattr(settings, "THRESHOLD_DIAGNOSTIC_LOOKBACK_HOURS", 720))
FETCH_CHUNK_HOURS = int(getattr(settings, "THRESHOLD_FETCH_CHUNK_HOURS", 6))
MAX_TICKS_PER_ASSET = int(getattr(settings, "THRESHOLD_MAX_TICKS_PER_ASSET", 750_000))
MIN_THRESHOLD_TICKS = int(getattr(settings, "FREEZE_THRESHOLD_MIN_TICKS", 10_000))
MIN_SIM_BARS = int(getattr(settings, "THRESHOLD_MIN_SIM_BARS_FOR_CONFIDENCE", 10))
ASSET_WORKERS = max(int(getattr(settings, "THRESHOLD_DIAGNOSTIC_ASSET_WORKERS", 1)), 1)
RESUME_CACHE_ENABLED = bool(getattr(settings, "THRESHOLD_DIAGNOSTIC_RESUME_CACHE_ENABLED", True))
RESUME_CACHE_MAX_AGE_HOURS = float(getattr(settings, "THRESHOLD_DIAGNOSTIC_RESUME_CACHE_MAX_AGE_HOURS", 24.0))
ASSET_CACHE_DIR = OUTPUT_DIR / "asset_cache"

DEFAULT_SYMBOL_MAP = dict(getattr(settings, "MT5_SYMBOL_MAP", {}))
HASH_VALIDATION_ENABLED = bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require_current_validation_reports(live_manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    current_hash = sha256_file(LIVE_MANIFEST_PATH)
    manifest_report = read_json(MANIFEST_VALIDATION_REPORT)
    environment_report = read_json(ENVIRONMENT_VALIDATION_REPORT)
    expected_edges = sorted(str(edge.get("edge_id")) for edge in (live_manifest.get("selected_edges") or []) if edge.get("edge_id"))
    if HASH_VALIDATION_ENABLED:
        manifest_current = manifest_report.get("live_manifest_sha256") == current_hash
        environment_current = environment_report.get("live_manifest_sha256") == current_hash
    else:
        manifest_current = manifest_report.get("manifest_created_at") == live_manifest.get("created_at") and sorted(str(x) for x in (manifest_report.get("selected_edge_ids") or [])) == expected_edges
        environment_current = environment_report.get("manifest_created_at") == live_manifest.get("created_at") and sorted(str(x) for x in (environment_report.get("selected_edge_ids") or [])) == expected_edges
    if manifest_report.get("validation_status") != "PASS" or not manifest_current:
        raise RuntimeError("Step 01 validation report is missing, failed, or stale for the current live manifest.")
    if environment_report.get("validation_status") != "PASS" or not environment_current:
        raise RuntimeError("Step 02 environment report is missing, failed, or stale for the current live manifest.")
    if environment_report.get("approved_account_stage") != live_manifest.get("approved_account_stage"):
        raise RuntimeError("Step 02 environment account stage does not match the current live manifest.")
    return manifest_report, environment_report, current_hash


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def is_standard_quote_tick(mode: Any, unit: Any) -> bool:
    return norm(mode) in {"standard_quote_tick", "standard_quote_tick_bars", "standard_tick_bars"} and norm(unit) in {"ticks", "tick", "tick_count", "quote_ticks"}


def setup_logger() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("v2_live_bar_threshold_diagnostic")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = RotatingFileHandler(
        LOG_DIR / "03_live_bar_threshold_diagnostic.log",
        maxBytes=max(int(getattr(settings, "LIVE_LOG_ROTATION_MAX_BYTES", 25_000_000)), 1_000_000),
        backupCount=max(int(getattr(settings, "LIVE_LOG_ROTATION_BACKUP_COUNT", 8)), 1),
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def local_settings() -> dict[str, Any]:
    return read_json(LOCAL_SETTINGS_PATH) if LOCAL_SETTINGS_PATH.exists() else {}


def symbol_map() -> dict[str, str]:
    out = dict(DEFAULT_SYMBOL_MAP)
    global_map = getattr(settings, "MT5_SYMBOL_MAP", {})
    if isinstance(global_map, dict):
        out.update({str(k): str(v) for k, v in global_map.items()})
    local_map = local_settings().get("mt5_symbol_map", {})
    if isinstance(local_map, dict):
        out.update({str(k): str(v) for k, v in local_map.items()})
    return out


def init_mt5():
    mt5 = importlib.import_module("MetaTrader5")
    local = local_settings()
    kwargs: dict[str, Any] = {"timeout": int(local.get("mt5_timeout_ms", getattr(settings, "MT5_TIMEOUT_MS", 60_000)))}
    terminal_path = local.get("mt5_path", getattr(settings, "MT5_PATH", None))
    login = local.get("mt5_login", getattr(settings, "MT5_LOGIN", None))
    password = local.get("mt5_password", getattr(settings, "MT5_PASSWORD", ""))
    server = local.get("mt5_server", getattr(settings, "MT5_SERVER", ""))
    if terminal_path:
        kwargs["path"] = str(terminal_path)
    if login is not None:
        kwargs["login"] = int(login)
    if password:
        kwargs["password"] = str(password)
    if server:
        kwargs["server"] = str(server)
    if not bool(mt5.initialize(**kwargs)):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
    return mt5, account


def shutdown_mt5(mt5: Any) -> None:
    try:
        mt5.shutdown()
    except Exception:
        pass


def selected_assets(live_manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    edges = live_manifest.get("selected_edges", [])
    if not isinstance(edges, list):
        raise TypeError("live_manifest.selected_edges must be a list")
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        asset = str(edge.get("asset", "")).strip()
        if asset:
            out.setdefault(asset, []).append(edge)
    if not out:
        raise RuntimeError("No selected assets in live_manifest.json")
    return out


def edge_manifest(edge: dict[str, Any]) -> dict[str, Any]:
    path_text = str(edge.get("edge_manifest_path", "")).strip()
    path = V2_ROOT / path_text
    return read_json(path) if path_text and path.exists() else {}


def bar_contract(edge: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts", {}) if isinstance(manifest.get("artifacts"), dict) else {}
    contracts = manifest.get("contracts", {}) if isinstance(manifest.get("contracts"), dict) else {}
    candidates: list[Path] = []
    if artifacts.get("bar_contract"):
        candidates.append(V2_ROOT / str(artifacts["bar_contract"]))
    if edge.get("edge_root"):
        candidates.append(V2_ROOT / str(edge["edge_root"]) / "contracts" / "bar_contract.json")
    for path in candidates:
        if path.exists():
            return read_json(path)
    return {
        "bar_mode": contracts.get("bar_mode"),
        "threshold_unit": contracts.get("threshold_unit"),
        "required_source": None,
        "dashboard_may_engineer_edge_features": contracts.get("dashboard_may_engineer_edge_features"),
    }


def asset_contract(asset: str, edges: list[dict[str, Any]]) -> dict[str, Any]:
    contracts = []
    for edge in edges:
        contracts.append(bar_contract(edge, edge_manifest(edge)))
    modes = {str(c.get("bar_mode", "")) for c in contracts}
    units = {str(c.get("threshold_unit", "")) for c in contracts}
    sources = sorted({str(c.get("required_source", "")) for c in contracts if c.get("required_source")})
    return {
        "asset": asset,
        "bar_mode": sorted(modes)[0] if len(modes) == 1 else "MIXED",
        "threshold_unit": sorted(units)[0] if len(units) == 1 else "MIXED",
        "required_sources": sources,
        "required_source": sources[0] if sources else None,
    }


def parse_research_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = read_json(path)
    except Exception:
        return None
    bc = payload.get("bar_construction", {}) if isinstance(payload.get("bar_construction"), dict) else {}
    tc = payload.get("threshold_config", {}) if isinstance(payload.get("threshold_config"), dict) else {}
    diag = payload.get("diagnostics_all_period", {}) if isinstance(payload.get("diagnostics_all_period"), dict) else {}
    stats = payload.get("forge_stats", {}) if isinstance(payload.get("forge_stats"), dict) else {}
    dc = payload.get("data_contract", {}) if isinstance(payload.get("data_contract"), dict) else {}
    mode = payload.get("selected_bar_mode") or bc.get("bar_type") or tc.get("bar_type") or payload.get("bar_mode")
    unit = tc.get("threshold_unit") or payload.get("threshold_unit")
    if mode and ("quote_tick" in norm(mode) or "standard_tick" in norm(mode)):
        unit = unit or "ticks"
    if "proxy_value is set equal to ticks" in str(dc.get("proxy_value_definition", "")).lower():
        mode = mode or "standard_quote_tick"
        unit = unit or "ticks"
    if not is_standard_quote_tick(mode, unit):
        return None
    return {
        "path": rel(path),
        "threshold_mode": str(tc.get("threshold_mode") or bc.get("threshold_mode") or "causal_daily_ewma_tick_count"),
        "target_bars_per_day": safe_float(bc.get("target_bars_per_day"), safe_float(tc.get("target_bars_per_day"), float(TARGET_BARS_PER_DAY))),
        "avg_bars_per_day": safe_float(diag.get("avg_bars_per_day")),
        "median_threshold_ticks": safe_float(tc.get("median_ticks_per_bar_threshold"), safe_float(diag.get("median_ticks_per_bar"), safe_float(stats.get("median_ticks_per_bar")))),
        "ewma_span_days": int(safe_float(tc.get("daily_tick_ewma_span_days"), float(EWMA_SPAN_DAYS)) or EWMA_SPAN_DAYS),
        "min_warmup_days": int(safe_float(tc.get("min_warmup_days"), float(MIN_WARMUP_DAYS)) or MIN_WARMUP_DAYS),
        "threshold_frozen_at_bar_start": bool(bc.get("threshold_frozen_at_bar_start", True)),
        "uses_future_data": bool(tc.get("uses_future_data", bc.get("uses_future_data", False))),
    }


def research_policy(asset: str, contract: dict[str, Any]) -> dict[str, Any]:
    roots: list[Path] = []
    for source in contract.get("required_sources", []):
        path = V2_ROOT / str(source)
        if path.exists():
            roots.append(path)
    fallback = V2_ROOT / "assets" / asset / "bars" / "standard_quote_tick"
    if not roots and fallback.exists():
        roots.append(fallback)
    found = []
    for root in roots:
        for path in root.rglob("*.json"):
            parsed = parse_research_manifest(path)
            if parsed:
                found.append(parsed)
    if not found:
        return {
            "candidate_count": 0,
            "source": None,
            "threshold_mode": "causal_daily_ewma_tick_count",
            "target_bars_per_day": float(TARGET_BARS_PER_DAY),
            "median_threshold_ticks": None,
            "ewma_span_days": EWMA_SPAN_DAYS,
            "min_warmup_days": MIN_WARMUP_DAYS,
            "threshold_frozen_at_bar_start": True,
            "uses_future_data": False,
        }
    def med(key: str, default: float | None = None) -> float | None:
        values = [safe_float(item.get(key)) for item in found]
        values = [v for v in values if v is not None]
        return float(np.median(values)) if values else default
    return {
        "candidate_count": len(found),
        "source": found[0]["path"],
        "threshold_mode": found[0].get("threshold_mode", "causal_daily_ewma_tick_count"),
        "target_bars_per_day": med("target_bars_per_day", float(TARGET_BARS_PER_DAY)),
        "median_threshold_ticks": med("median_threshold_ticks"),
        "ewma_span_days": int(med("ewma_span_days", float(EWMA_SPAN_DAYS)) or EWMA_SPAN_DAYS),
        "min_warmup_days": int(med("min_warmup_days", float(MIN_WARMUP_DAYS)) or MIN_WARMUP_DAYS),
        "threshold_frozen_at_bar_start": bool(found[0].get("threshold_frozen_at_bar_start", True)),
        "uses_future_data": bool(found[0].get("uses_future_data", False)),
    }


def normalize_ticks(raw: Any) -> pd.DataFrame:
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    if "time_msc" in df.columns:
        df["timestamp"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
    elif "time" in df.columns:
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["time_msc"] = (df["timestamp"].astype("int64") // 1_000_000).astype("int64")
    else:
        raise ValueError("MT5 ticks missing time/time_msc")
    df["bid"] = pd.to_numeric(df.get("bid"), errors="coerce")
    df["ask"] = pd.to_numeric(df.get("ask"), errors="coerce")
    df = df.dropna(subset=["timestamp", "bid", "ask"])
    df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"])].copy()
    if df.empty:
        return df
    df["mid"] = (df["bid"] + df["ask"]) / 2.0
    df["date"] = df["timestamp"].dt.date.astype(str)
    return df.sort_values(["timestamp", "time_msc"], kind="mergesort").reset_index(drop=True)


def fetch_ticks(mt5: Any, symbol: str) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=LOOKBACK_HOURS)
    chunks = []
    total = 0
    cursor = end
    while cursor > start:
        prev = max(cursor - timedelta(hours=FETCH_CHUNK_HOURS), start)
        ticks = mt5.copy_ticks_range(symbol, prev, cursor, mt5.COPY_TICKS_ALL)
        if ticks is not None and len(ticks) > 0:
            part = pd.DataFrame(ticks)
            chunks.append(part)
            total += len(part)
            if total >= MAX_TICKS_PER_ASSET:
                break
        cursor = prev
    if not chunks:
        return pd.DataFrame()
    raw = pd.concat(chunks, ignore_index=True)
    if len(raw) > MAX_TICKS_PER_ASSET:
        key = "time_msc" if "time_msc" in raw.columns else "time"
        raw = raw.sort_values(key).tail(MAX_TICKS_PER_ASSET)
    return normalize_ticks(raw)


def completed_daily_profile(ticks: pd.DataFrame) -> list[dict[str, Any]]:
    if ticks.empty:
        return []
    current_date = datetime.now(timezone.utc).date().isoformat()
    counts = ticks.groupby("date").size().reset_index(name="tick_count")
    counts = counts[counts["date"].astype(str) < current_date]
    return [{"date": str(row.date), "tick_count": int(row.tick_count)} for row in counts.itertuples(index=False)]


def ewma_threshold(profile: list[dict[str, Any]], target_bpd: float, span: int, min_days: int) -> int | None:
    counts = [float(row["tick_count"]) for row in profile]
    if len(counts) < min_days:
        return None
    ewma_daily_count = float(pd.Series(counts).ewm(span=span, adjust=False).mean().iloc[-1])
    return max(1, int(round(ewma_daily_count / max(float(target_bpd), 1.0))))


def static_sim(ticks: pd.DataFrame, threshold: float | None) -> dict[str, Any]:
    """Legacy all-period scalar audit only; never used as the live readiness gate.

    Calendar elapsed time includes weekends and closed sessions for exchange-traded
    indices, so this metric is retained for observability but is not suitable for
    deciding whether the causal adaptive policy targets 50 active-session bars/day.
    """
    if ticks.empty or threshold is None or threshold <= 0:
        return {"bars": 0, "estimated_bars_per_day": None}
    observed_days = max((ticks["timestamp"].max() - ticks["timestamp"].min()).total_seconds(), 1.0) / 86400.0
    bars = int(len(ticks) // max(int(round(threshold)), 1))
    return {"bars": bars, "estimated_bars_per_day": float(bars / observed_days)}


def causal_profile_sim(
    profile: list[dict[str, Any]],
    target_bpd: float,
    span: int,
    min_days: int,
) -> dict[str, Any]:
    """Replay the research threshold rule across completed active UTC days.

    For each completed day after warm-up, compute that day's threshold from only
    earlier completed days, then estimate how many standard quote-tick bars that
    day's tick count would form. This mirrors the causal research/live contract
    and avoids treating weekends/closed sessions as zero-bar trading days.
    """
    counts = [float(row["tick_count"]) for row in profile if safe_float(row.get("tick_count"), None) is not None and float(row["tick_count"]) > 0]
    if len(counts) <= int(min_days):
        return {
            "simulated_days": 0,
            "estimated_bars_per_active_day": None,
            "median_bars_per_active_day": None,
            "deviation_fraction": None,
            "threshold_min": None,
            "threshold_max": None,
        }

    estimated: list[float] = []
    thresholds: list[int] = []
    for index in range(int(min_days), len(counts)):
        prior = pd.Series(counts[:index], dtype="float64")
        ewma_daily_count = float(prior.ewm(span=int(span), adjust=False).mean().iloc[-1])
        threshold = max(1, int(round(ewma_daily_count / max(float(target_bpd), 1.0))))
        thresholds.append(threshold)
        estimated.append(float(counts[index]) / float(threshold))

    average_bpd = float(np.mean(estimated)) if estimated else None
    median_bpd = float(np.median(estimated)) if estimated else None
    deviation = (
        abs(float(average_bpd) - float(target_bpd)) / max(float(target_bpd), 1.0)
        if average_bpd is not None else None
    )
    return {
        "simulated_days": len(estimated),
        "estimated_bars_per_active_day": average_bpd,
        "median_bars_per_active_day": median_bpd,
        "deviation_fraction": deviation,
        "threshold_min": min(thresholds) if thresholds else None,
        "threshold_max": max(thresholds) if thresholds else None,
    }


def reassess_diagnostic_result(result: dict[str, Any]) -> dict[str, Any]:
    """Apply the current readiness rule to a fresh or cached asset result."""
    out = dict(result)
    policy = out.get("adaptive_threshold_policy")
    research = out.get("research_policy") or {}
    if not isinstance(policy, dict):
        return out

    target_bpd = float(policy.get("target_bars_per_day") or TARGET_BARS_PER_DAY)
    span = int(policy.get("daily_tick_ewma_span_days") or EWMA_SPAN_DAYS)
    min_days = int(policy.get("min_warmup_days") or MIN_WARMUP_DAYS)
    profile = list(policy.get("completed_daily_tick_profile") or [])
    causal = causal_profile_sim(profile, target_bpd, span, min_days)
    tolerance = float(getattr(settings, "FREEZE_RESEARCH_CADENCE_TOLERANCE", 0.35))

    active_threshold = policy.get("computed_next_session_threshold_ticks")
    active_source = "computed_next_session_threshold_ticks"
    if active_threshold is None:
        active_threshold = policy.get("fallback_initial_threshold_ticks")
        active_source = "fallback_initial_threshold_ticks"

    out.update({
        "diagnostic_logic_version": DIAGNOSTIC_LOGIC_VERSION,
        "active_threshold_ticks": active_threshold,
        "active_threshold_source": active_source,
        "causal_profile_simulated_days": causal.get("simulated_days"),
        "causal_estimated_bars_per_active_day": causal.get("estimated_bars_per_active_day"),
        "causal_median_bars_per_active_day": causal.get("median_bars_per_active_day"),
        "causal_cadence_deviation_fraction": causal.get("deviation_fraction"),
        "causal_threshold_min_ticks": causal.get("threshold_min"),
        "causal_threshold_max_ticks": causal.get("threshold_max"),
        "cadence_review_basis": "causal_completed_active_days_prior_history_only",
    })

    if int(research.get("candidate_count", 0) or 0) <= 0:
        out["status"] = "warn"
        out["reason"] = "research_bar_manifest_not_found_using_default_policy"
    elif not bool(policy.get("warmup_ready_from_live_history")):
        out["status"] = "warn"
        out["reason"] = "adaptive_policy_ready_but_live_history_warmup_incomplete"
    elif int(causal.get("simulated_days") or 0) <= 0:
        out["status"] = "warn"
        out["reason"] = "too_few_causal_active_days_for_confidence"
    elif causal.get("deviation_fraction") is not None and float(causal["deviation_fraction"]) > tolerance:
        out["status"] = "warn"
        out["reason"] = "causal_adaptive_cadence_outside_review_tolerance"
    else:
        out["status"] = "pass"
        out["reason"] = "adaptive_policy_ready"
    return out


def diagnose_asset(mt5: Any, asset: str, edges: list[dict[str, Any]], logger: logging.Logger) -> dict[str, Any]:
    contract = asset_contract(asset, edges)
    edge_ids = [str(edge.get("edge_id")) for edge in edges]
    symbol = symbol_map().get(asset, asset)
    research = research_policy(asset, contract)
    base = {
        "asset": asset,
        "mt5_symbol": symbol,
        "selected_edges": edge_ids,
        "bar_mode": contract["bar_mode"],
        "threshold_unit": contract["threshold_unit"],
        "research_policy": research,
    }
    if not is_standard_quote_tick(contract["bar_mode"], contract["threshold_unit"]):
        return {**base, "status": "fail", "reason": "bar_contract_not_standard_quote_tick_ticks", "adaptive_threshold_policy": None}
    if norm(research["threshold_mode"]) != "causal_daily_ewma_tick_count" or research.get("uses_future_data") is True:
        return {**base, "status": "fail", "reason": "unsupported_or_noncausal_research_bar_policy", "adaptive_threshold_policy": None}
    if mt5.symbol_info(symbol) is None:
        return {**base, "status": "fail", "reason": "mt5_symbol_not_found", "adaptive_threshold_policy": None}
    mt5.symbol_select(symbol, True)
    logger.info("Fetching ticks asset=%s symbol=%s lookback_hours=%s", asset, symbol, LOOKBACK_HOURS)
    ticks = fetch_ticks(mt5, symbol)
    if ticks.empty:
        return {**base, "status": "warn", "reason": "no_ticks_available", "adaptive_threshold_policy": None}
    first = ticks["timestamp"].min()
    last = ticks["timestamp"].max()
    observed_days = max((last - first).total_seconds(), 1.0) / 86400.0
    ticks_per_day = float(len(ticks) / observed_days)
    target_bpd = float(research.get("target_bars_per_day") or TARGET_BARS_PER_DAY)
    span = int(research.get("ewma_span_days") or EWMA_SPAN_DAYS)
    research_min_days = int(
        research.get("min_warmup_days") or MIN_WARMUP_DAYS
    )

    min_days = max(
        int(MIN_WARMUP_DAYS),
        int(research_min_days),
        int(WARMUP_DAYS_BY_ASSET.get(asset, research_min_days)),
    )
    profile = completed_daily_profile(ticks)
    next_threshold = ewma_threshold(profile, target_bpd, span, min_days)
    diagnostic_static = max(float(ticks_per_day / max(target_bpd, 1.0)), float(MIN_THRESHOLD_TICKS))
    fallback = int(round(max(float(next_threshold or 0), diagnostic_static, float(research.get("median_threshold_ticks") or 0), float(MIN_THRESHOLD_TICKS))))
    policy = {
        "asset": asset,
        "mt5_symbol": symbol,
        "bar_mode": "standard_quote_tick",
        "threshold_unit": "ticks",
        "threshold_mode": "causal_daily_ewma_tick_count",
        "target_bars_per_day": target_bpd,
        "daily_tick_ewma_span_days": span,
        "research_min_warmup_days": research_min_days,
        "min_warmup_days": min_days,
        "live_min_warmup_override_applied": min_days != research_min_days,
        "threshold_frozen_at_bar_start": True,
        "uses_future_data": False,
        "use_completed_days_only": True,
        "exclude_current_incomplete_day": True,
        "fallback_initial_threshold_ticks": fallback,
        "computed_next_session_threshold_ticks": next_threshold,
        "completed_daily_profile_days": len(profile),
        "warmup_ready_from_live_history": len(profile) >= min_days,
        "completed_daily_tick_profile": profile,
        "research_manifest_source": research.get("source"),
        "research_manifest_candidate_count": research.get("candidate_count"),
        "research_median_threshold_ticks": research.get("median_threshold_ticks"),
        "live_diagnostic_static_threshold_ticks": diagnostic_static,
    }
    # Retain the scalar fallback simulation for audit only. Readiness is judged
    # by a causal replay over completed active days, because calendar elapsed days
    # incorrectly penalize exchange-traded symbols for weekends and closed sessions.
    sim = static_sim(ticks, fallback)
    cadence_target = float(target_bpd)
    fallback_estimated_bpd = safe_float(sim.get("estimated_bars_per_day"), None)
    fallback_cadence_deviation = (
        abs(float(fallback_estimated_bpd) - cadence_target) / max(cadence_target, 1.0)
        if fallback_estimated_bpd is not None else None
    )
    research_avg_bpd = safe_float(research.get("avg_bars_per_day"), None)
    research_cadence_deviation = (
        abs(float(research_avg_bpd) - cadence_target) / max(cadence_target, 1.0)
        if research_avg_bpd is not None else None
    )
    result = {
        **base,
        "status": "pass",
        "reason": "adaptive_policy_ready",
        "ticks": int(len(ticks)),
        "first_tick": first.isoformat(),
        "last_tick": last.isoformat(),
        "observed_days": float(observed_days),
        "live_ticks_per_day": ticks_per_day,
        "fallback_initial_threshold_ticks": fallback,
        "computed_next_session_threshold_ticks": next_threshold,
        "simulated_bars_fallback_threshold": sim["bars"],
        "estimated_bars_per_day_fallback_threshold": sim["estimated_bars_per_day"],
        "cadence_target_bars_per_day": cadence_target,
        "fallback_cadence_deviation_fraction": fallback_cadence_deviation,
        "fallback_cadence_is_audit_only": True,
        "research_avg_bars_per_day": research_avg_bpd,
        "research_cadence_deviation_fraction": research_cadence_deviation,
        "adaptive_threshold_policy": policy,
    }
    return reassess_diagnostic_result(result)


def _asset_cache_signature(asset: str, edges: list[dict[str, Any]], live_manifest_sha256: str) -> str:
    payload = {
        "asset": asset,
        "edge_ids": sorted(str(edge.get("edge_id")) for edge in edges),
        "symbol": symbol_map().get(asset, asset),
        "live_manifest_sha256": live_manifest_sha256,
        "lookback_hours": LOOKBACK_HOURS,
        "fetch_chunk_hours": FETCH_CHUNK_HOURS,
        "max_ticks": MAX_TICKS_PER_ASSET,
        "target_bars_per_day": TARGET_BARS_PER_DAY,
        "ewma_span_days": EWMA_SPAN_DAYS,
        "min_warmup_days": MIN_WARMUP_DAYS,
        "min_threshold_ticks": MIN_THRESHOLD_TICKS,
    }
    compact = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _asset_cache_path(asset: str) -> Path:
    safe = "".join(ch for ch in str(asset) if ch.isalnum() or ch in {"_", "-"})
    return ASSET_CACHE_DIR / f"{safe}_threshold_diagnostic.json"


def _load_cached_asset(asset: str, edges: list[dict[str, Any]], live_manifest_sha256: str) -> dict[str, Any] | None:
    if not RESUME_CACHE_ENABLED:
        return None
    path = _asset_cache_path(asset)
    if not path.exists():
        return None
    try:
        payload = read_json(path)
        if payload.get("cache_signature") != _asset_cache_signature(asset, edges, live_manifest_sha256):
            return None
        created = pd.Timestamp(payload.get("cached_at"))
        if created.tzinfo is None:
            created = created.tz_localize("UTC")
        age_hours = (pd.Timestamp.now(tz="UTC") - created.tz_convert("UTC")).total_seconds() / 3600.0
        if age_hours > RESUME_CACHE_MAX_AGE_HOURS:
            return None
        result = payload.get("result")
        if isinstance(result, dict):
            # Reassess cached raw/profile evidence under the current diagnostic
            # logic without redownloading millions of MT5 ticks.
            result = reassess_diagnostic_result(dict(result))
            result["cache_reused"] = True
            result["cache_age_hours"] = float(age_hours)
            return result
    except Exception:
        return None
    return None


def _save_cached_asset(asset: str, edges: list[dict[str, Any]], live_manifest_sha256: str, result: dict[str, Any]) -> None:
    ASSET_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    write_json(
        _asset_cache_path(asset),
        {
            "schema_version": SCHEMA_VERSION,
            "cached_at": now_utc(),
            "cache_signature": _asset_cache_signature(asset, edges, live_manifest_sha256),
            "result": result,
        },
    )


def _worker_logger(asset: str) -> logging.Logger:
    logger = logging.getLogger(f"v2_threshold_worker_{asset}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    return logger


def _diagnose_asset_worker(payload: tuple[str, list[dict[str, Any]]]) -> dict[str, Any]:
    asset, edges = payload
    mt5, _ = init_mt5()
    try:
        return diagnose_asset(mt5, asset, edges, _worker_logger(asset))
    finally:
        shutdown_mt5(mt5)


def build(write: bool = True, *, fresh: bool = False) -> dict[str, Any]:
    logger = setup_logger()
    live_manifest = read_json(LIVE_MANIFEST_PATH)
    manifest_validation_report, environment_validation_report, live_manifest_sha256 = require_current_validation_reports(live_manifest)
    assets = selected_assets(live_manifest)
    diagnostics_by_asset: dict[str, dict[str, Any]] = {}

    if not fresh:
        for asset, edges in assets.items():
            cached = _load_cached_asset(asset, edges, live_manifest_sha256)
            if cached is not None:
                diagnostics_by_asset[asset] = cached
                logger.info("Threshold diagnostic cache hit asset=%s age_hours=%s", asset, cached.get("cache_age_hours"))

    pending = [(asset, edges) for asset, edges in assets.items() if asset not in diagnostics_by_asset]
    account_login = None
    account_server = None
    if pending and ASSET_WORKERS > 1 and len(pending) > 1:
        workers = min(ASSET_WORKERS, len(pending))
        logger.info("Parallel threshold diagnostic start profile=%s workers=%s assets=%s", getattr(settings, "PORTFOLIO_COMPUTE_PROFILE", "local_safe"), workers, [a for a, _ in pending])
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_diagnose_asset_worker, item): item[0] for item in pending}
            for future in as_completed(futures):
                asset = futures[future]
                result = future.result()
                diagnostics_by_asset[asset] = result
                _save_cached_asset(asset, assets[asset], live_manifest_sha256, result)
                logger.info("Threshold diagnostic asset complete asset=%s status=%s", asset, result.get("status"))
        # Read account identity once in the parent for the manifest.
        mt5, account = init_mt5()
        try:
            account_login = getattr(account, "login", None)
            account_server = getattr(account, "server", None)
        finally:
            shutdown_mt5(mt5)
    elif pending:
        mt5, account = init_mt5()
        account_login = getattr(account, "login", None)
        account_server = getattr(account, "server", None)
        try:
            for asset, edges in pending:
                logger.info("Diagnosing asset=%s selected_edges=%s", asset, [edge.get("edge_id") for edge in edges])
                result = diagnose_asset(mt5, asset, edges, logger)
                diagnostics_by_asset[asset] = result
                _save_cached_asset(asset, edges, live_manifest_sha256, result)
        finally:
            shutdown_mt5(mt5)
    else:
        local = local_settings()
        account_login = local.get("mt5_login")
        account_server = local.get("mt5_server")

    diagnostics = [diagnostics_by_asset[asset] for asset in sorted(assets)]
    statuses = [str(row.get("status")) for row in diagnostics]
    diagnostic_status = "PASS" if statuses and all(s == "pass" for s in statuses) else "WARN_REVIEW_REQUIRED"
    if any(s == "fail" for s in statuses):
        diagnostic_status = "FAIL"
    policies = {row["asset"]: row["adaptive_threshold_policy"] for row in diagnostics if row.get("adaptive_threshold_policy")}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "step": "03_live_bar_threshold_diagnostic.py",
        "diagnostic_status": diagnostic_status,
        "compute_profile": str(getattr(settings, "PORTFOLIO_COMPUTE_PROFILE", "local_safe")),
        "diagnostic_logic_version": DIAGNOSTIC_LOGIC_VERSION,
        "asset_workers": int(ASSET_WORKERS),
        "resume_cache_enabled": bool(RESUME_CACHE_ENABLED),
        "fresh_run_requested": bool(fresh),
        "live_manifest_path": rel(LIVE_MANIFEST_PATH),
        "source_live_manifest": rel(LIVE_MANIFEST_PATH),
        "source_live_manifest_sha256": live_manifest_sha256,  # audit only
        "source_manifest_created_at": live_manifest.get("created_at"),
        "source_selected_edge_ids": sorted(str(edge.get("edge_id")) for edge in (live_manifest.get("selected_edges") or []) if edge.get("edge_id")),
        "artifact_hash_validation_enabled": HASH_VALIDATION_ENABLED,
        "source_manifest_validation_report": rel(MANIFEST_VALIDATION_REPORT),
        "source_manifest_validation_report_sha256": sha256_file(MANIFEST_VALIDATION_REPORT),
        "source_environment_validation_report": rel(ENVIRONMENT_VALIDATION_REPORT),
        "source_environment_validation_report_sha256": sha256_file(ENVIRONMENT_VALIDATION_REPORT),
        "approved_account_stage": live_manifest.get("approved_account_stage"),
        "risk_contract_version": live_manifest.get("risk_contract_version"),
        "account_login": account_login,
        "account_server": account_server,
        "selected_assets": sorted(assets.keys()),
        "summary_csv": rel(SUMMARY_CSV),
        "diagnostics": diagnostics,
        "adaptive_threshold_policies": policies,
        "sotd_contracts": {
            "bar_mode": "standard_quote_tick",
            "threshold_unit": "ticks",
            "threshold_mode": "causal_daily_ewma_tick_count",
            "threshold_frozen_at_bar_start": True,
            "uses_future_data": False,
            "positive_non_crossed_quotes_only": True,
            "quote_tick_multiplicity_preserved": True,
            "research_min_warmup_days_enforced": int(MIN_WARMUP_DAYS),
            "dashboard_must_use_frozen_policy": True,
        },
        "next_ordered_step_if_pass_or_warn_review_accepted": "python ./portfolio_live/04_freeze_live_bar_thresholds.py --require-pass",
    }
    if write:
        flat_rows = [{k: v for k, v in row.items() if k != "adaptive_threshold_policy"} for row in diagnostics]
        pd.DataFrame(flat_rows).to_csv(SUMMARY_CSV, index=False)
        write_json(MANIFEST_JSON, manifest)
    return manifest


def print_summary(manifest: dict[str, Any]) -> None:
    print(STEP_NAME)
    print(f"Status: {manifest['diagnostic_status']}")
    print(f"Assets: {len(manifest['selected_assets'])}")
    for row in manifest["diagnostics"]:
        policy = row.get("adaptive_threshold_policy") or {}
        print(
            f"- {row['asset']} symbol={row['mt5_symbol']} status={row['status']} "
            f"ticks={row.get('ticks')} fallback={policy.get('fallback_initial_threshold_ticks')} "
            f"next={policy.get('computed_next_session_threshold_ticks')} reason={row['reason']}"
        )
    print("Output: portfolio_live/outputs/threshold_diagnostics/threshold_diagnostic_manifest.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fresh", action="store_true", help="Ignore resumable per-asset diagnostic cache.")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run, fresh=args.fresh)
    print_summary(manifest)
    if manifest["diagnostic_status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
