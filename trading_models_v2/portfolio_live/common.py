from __future__ import annotations

import importlib
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from . import live_settings as settings
except ImportError:  # pragma: no cover
    import live_settings as settings

from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    for path in [settings.OUTPUT_DIR, settings.LIVE_BARS_DIR, settings.LIVE_LOGS_DIR, settings.LIVE_STATE_DIR, settings.LIVE_CONFIG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def setup_logger(name: str, log_file: Path | None = None) -> logging.Logger:
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
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON: {path}")
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return payload


def write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str, allow_nan=True)
        f.write("\n")


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing CSV: {path}")
    return pd.read_csv(path)


def resolve_path(path_value: Any, root: Path | None = None) -> Path:
    if path_value is None:
        return Path("")
    s = str(path_value).strip()
    if not s or s.lower() == "nan":
        return Path("")
    p = Path(s)
    if p.is_absolute() and p.exists():
        return p
    roots = [root, settings.V2_ROOT, settings.REPO_ROOT, settings.PORTFOLIO_ALLOCATOR_DIR, settings.PORTFOLIO_LIVE_DIR]
    for candidate_root in roots:
        if candidate_root is None:
            continue
        candidate = candidate_root / s
        if candidate.exists():
            return candidate
    return p


def live_manifest_path() -> Path:
    return settings.LIVE_MANIFEST_PATH


def load_live_manifest() -> dict[str, Any]:
    path = live_manifest_path()
    if not path.exists():
        raise FileNotFoundError(f"Live manifest not found: {path}\nRun python ./portfolio_live/00_build_live_deployment_manifest.py first.")
    return read_json(path)


def selected_edges(manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if manifest is None:
        manifest = load_live_manifest()
    edges = manifest.get("selected_edges", [])
    if not isinstance(edges, list):
        raise TypeError("live_manifest.selected_edges must be a list")
    return edges


def broker_symbol_for_asset(asset: str) -> str:
    return settings.MT5_SYMBOL_MAP.get(asset, asset)


def safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None or pd.isna(x):
            return default
        out = float(x)
        if out != out:
            return default
        return out
    except Exception:
        return default


def safe_int(x: Any, default: int | None = None) -> int | None:
    try:
        if x is None or pd.isna(x):
            return default
        return int(x)
    except Exception:
        return default


def import_edge_runtime(edge: dict[str, Any]):
    """Load the unchanged authoritative edge runtime.

    Native one-row/stateful continuation is intentionally retired. The dashboard
    calls the edge-owned full build_features(), generate_candidate_events(),
    build_model_rows() and score() functions on the authoritative bar snapshot.
    """
    runtime = load_edge_runtime_from_manifest_entry(edge, v2_root=settings.V2_ROOT)

    # PHASE_A_LIVE_GENERATION_CONSUMER_BINDING_V1
    #
    # Live scoring must consume FFD/HMM from the exact asset dependency
    # generation already certified by the snapshot coordinator. Edge-local
    # feature modules remain authoritative for feature math, but their shared
    # dependency loaders must not independently re-read another generation.
    # Keep this binding live-only: the generic research runtime loader is
    # intentionally unchanged.
    from shared.live_streaming.hotpath import patch_runtime_shared_loaders

    patch_report = patch_runtime_shared_loaders(runtime)
    setattr(runtime, "_quantara_live_shared_loader_patch", dict(patch_report))

    required = ("build_features", "generate_candidate_events", "build_model_rows", "score")
    missing = [name for name in required if not callable(getattr(runtime, name, None))]
    if missing:
        raise RuntimeError(
            f"Authoritative runtime {edge.get('edge_id')} is missing required functions: {missing}"
        )
    return runtime


def init_mt5():
    mt5 = importlib.import_module("MetaTrader5")
    kwargs: dict[str, Any] = {"timeout": int(settings.MT5_TIMEOUT_MS)}
    if settings.MT5_PATH:
        kwargs["path"] = settings.MT5_PATH
    if settings.MT5_LOGIN is not None:
        kwargs["login"] = int(settings.MT5_LOGIN)
    if settings.MT5_PASSWORD:
        kwargs["password"] = settings.MT5_PASSWORD
    if settings.MT5_SERVER:
        kwargs["server"] = settings.MT5_SERVER
    ok = bool(mt5.initialize(**kwargs))
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        try:
            err = mt5.last_error()
        except Exception:
            err = "unknown"
        raise RuntimeError(f"MT5 account_info failed: {err}")
    return mt5, account


def shutdown_mt5(mt5: Any) -> None:
    try:
        mt5.shutdown()
    except Exception:
        pass


def assert_no_order_send_unless_armed(manifest: dict[str, Any] | None = None) -> None:
    if manifest is None:
        manifest = load_live_manifest()
    settings.assert_order_send_armed_by_manifest(manifest)


def validate_signal_contract(signal: dict[str, Any], manifest: dict[str, Any] | None = None) -> None:
    if manifest is None:
        manifest = load_live_manifest()
    required = ["schema_version", "edge_id", "asset", "side_value", "event_time", "raw_probability", "calibrated_probability", "afml_bet_size", "approved_signal", "final_live_threshold", "portfolio_weight", "risk_contract_version", "account_stage", "approved_account_stage", "daily_symbol_risk_rule_enabled"]
    if bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False)):
        required.extend(["edge_manifest_hash", "live_manifest_hash"])
    missing = [field for field in required if field not in signal]
    if missing:
        raise ValueError(f"Approved signal missing required fields: {missing}")
    if signal.get("threshold_probability_column", "calibrated_probability") != "calibrated_probability":
        raise ValueError("Signal threshold_probability_column must be calibrated_probability")
    if signal.get("risk_contract_version") != manifest.get("risk_contract_version"):
        raise ValueError("Signal risk_contract_version does not match live_manifest.")
    if str(signal.get("approved_account_stage") or signal.get("account_stage") or "").lower() != str(manifest.get("approved_account_stage") or manifest.get("account_stage") or "").lower():
        raise ValueError("Signal account stage does not match live_manifest.")
    if bool(signal.get("daily_symbol_risk_rule_enabled")) != bool(manifest.get("daily_symbol_risk_rule_enabled")):
        raise ValueError("Signal symbol-risk enablement does not match live_manifest.")
    if signal.get("approved_signal") is True:
        afml = safe_float(signal.get("afml_bet_size"), None)
        if afml is None or afml <= 0.0:
            raise ValueError("Approved signal must have a finite positive afml_bet_size.")
        if manifest.get("live_trade_enabled") is not True:
            raise ValueError("Signal is approved but live_manifest.live_trade_enabled is false; execution must remain dry-run/paper.")
