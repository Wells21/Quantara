"""Certification contract for the guarded native one-row runtime.

Native mode is never enabled merely because code exists.  A local certification
file, produced against the machine's canonical bars and verified FFD/HMM
artifacts, must match the implementation hashes.  Otherwise the existing
verified-suffix/full-authority path remains active.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CERTIFICATION_PATH = ROOT / "portfolio_live" / "native_one_row_certification.json"
SCHEMA_VERSION = "native_one_row_certification_v5"
_CERTIFICATION_CACHE_LOCK = threading.RLock()
_CERTIFICATION_CACHE: dict[str, Any] | None = None
_CERTIFICATION_CACHE_PATH: str | None = None


def clear_certification_cache() -> None:
    global _CERTIFICATION_CACHE, _CERTIFICATION_CACHE_PATH
    with _CERTIFICATION_CACHE_LOCK:
        _CERTIFICATION_CACHE = None
        _CERTIFICATION_CACHE_PATH = None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def implementation_hashes() -> dict[str, str]:
    paths = {
        "shared/edges/native_one_row_contract.py": Path(__file__),
        "shared/edges/native_one_row_feature_engine.py": Path(__file__).with_name("native_one_row_feature_engine.py"),
        "shared/edges/native_one_row_event_engine.py": Path(__file__).with_name("native_one_row_event_engine.py"),
        "shared/edges/live_stateful_runtime.py": Path(__file__).with_name("live_stateful_runtime.py"),
        "shared/edges/incremental_runtime_primitives.py": Path(__file__).with_name("incremental_runtime_primitives.py"),
        "portfolio_live/portfolio_live_dashboard_orchestrator.py": ROOT / "portfolio_live" / "portfolio_live_dashboard_orchestrator.py",
        "portfolio_live/certify_native_one_row_runtime.py": ROOT / "portfolio_live" / "certify_native_one_row_runtime.py",
    }
    return {name: file_sha256(path) if path.exists() else "MISSING" for name, path in paths.items()}


def runtime_environment() -> dict[str, str]:
    versions: dict[str, str] = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": platform.platform(),
    }
    modules = {
        "numpy": "numpy",
        "pandas": "pandas",
        "pandas_ta_classic": "pandas_ta_classic",
        "pywt": "pywt",
    }
    for key, module_name in modules.items():
        try:
            module = __import__(module_name)
            versions[key] = str(getattr(module, "__version__", "UNKNOWN"))
        except Exception as exc:
            versions[key] = f"MISSING:{type(exc).__name__}"
    return versions


def load_certification(path: Path | None = None) -> dict[str, Any]:
    global _CERTIFICATION_CACHE, _CERTIFICATION_CACHE_PATH
    target = Path(path or CERTIFICATION_PATH).resolve()
    cacheable = path is None
    target_key = str(target)
    if cacheable:
        with _CERTIFICATION_CACHE_LOCK:
            if _CERTIFICATION_CACHE_PATH == target_key and _CERTIFICATION_CACHE is not None:
                return dict(_CERTIFICATION_CACHE)

    payload: dict[str, Any] = {}
    if target.exists():
        try:
            candidate = json.loads(target.read_text(encoding="utf-8-sig"))
        except Exception:
            candidate = {}
        if (
            isinstance(candidate, dict)
            and candidate.get("schema_version") == SCHEMA_VERSION
            and candidate.get("status") == "PASS"
            and int(candidate.get("selected_edge_count") or 0) == 33
            and candidate.get("implementation_hashes") == implementation_hashes()
            and candidate.get("runtime_environment") == runtime_environment()
        ):
            payload = candidate

    if cacheable:
        with _CERTIFICATION_CACHE_LOCK:
            _CERTIFICATION_CACHE_PATH = target_key
            _CERTIFICATION_CACHE = dict(payload)
    return dict(payload)


def feature_certified(asset: str, path: Path | None = None) -> bool:
    payload = load_certification(path)
    row = (payload.get("assets") or {}).get(str(asset).upper()) or {}
    required = (
        "feature_parity_passed",
        "exact_schema_parity_passed",
        "append_only_history_passed",
        "restart_parity_passed",
        "rolling_window_feature_parity_passed",
    )
    return all(row.get(name) is True for name in required)


NATIVE_EVENT_MODE = "native_one_row_certified"
FULL_AUTHORITY_EVENT_MODE = "authoritative_full_history_required"


def edge_certification_passed(row: Any) -> bool:
    """Return True only for an explicitly safe event execution classification."""
    if not isinstance(row, dict) or row.get("feature_parity_passed") is not True:
        return False
    mode = str(row.get("event_execution_mode") or "")
    if mode == NATIVE_EVENT_MODE:
        required = (
            "event_parity_passed",
            "append_only_history_passed",
            "restart_parity_passed",
            "event_state_continuity_passed",
            "historical_revision_rejected",
            "rolling_window_event_parity_passed",
        )
        return row.get("native_event_eligible") is True and all(
            row.get(name) is True for name in required
        )
    if mode == FULL_AUTHORITY_EVENT_MODE:
        required = (
            "event_parity_passed",
            "full_authority_determinism_passed",
            "force_full_event_replay",
        )
        return (
            row.get("native_event_eligible") is False
            and row.get("full_authority_required") is True
            and all(row.get(name) is True for name in required)
        )
    return False


def event_execution_classification(edge_id: str, path: Path | None = None) -> dict[str, Any]:
    """Return the certified event execution policy for diagnostics and routing."""
    payload = load_certification(path)
    row = dict((payload.get("edges") or {}).get(str(edge_id)) or {})
    if not edge_certification_passed(row):
        return {}
    return {
        "edge_id": str(edge_id),
        "event_execution_mode": str(row.get("event_execution_mode")),
        "native_event_eligible": bool(row.get("native_event_eligible")),
        "full_authority_required": bool(row.get("full_authority_required")),
        "force_full_event_replay": bool(row.get("force_full_event_replay")),
        "certification_reason": str(row.get("certification_reason") or ""),
    }


def event_certification(edge_id: str, path: Path | None = None) -> dict[str, Any]:
    """Return only certifications eligible for the native one-row event engine.

    A full-authority edge can be safely certified overall while deliberately
    returning an empty native certification here.  This prevents the installer
    from ever routing a cumulative/path-dependent edge into the bounded native
    event engine merely because the global certification status is PASS.
    """
    payload = load_certification(path)
    row = dict((payload.get("edges") or {}).get(str(edge_id)) or {})
    if (
        edge_certification_passed(row)
        and row.get("event_execution_mode") == NATIVE_EVENT_MODE
        and row.get("native_event_eligible") is True
    ):
        return row
    return {}


__all__ = [
    "ROOT", "CERTIFICATION_PATH", "SCHEMA_VERSION",
    "NATIVE_EVENT_MODE", "FULL_AUTHORITY_EVENT_MODE",
    "implementation_hashes", "runtime_environment",
    "clear_certification_cache", "load_certification", "feature_certified",
    "edge_certification_passed", "event_execution_classification",
    "event_certification",
]
