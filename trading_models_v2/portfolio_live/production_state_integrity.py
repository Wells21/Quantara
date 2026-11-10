from __future__ import annotations

"""Crash-safe persisted-state IO for the V2 live processes.

This module is operational only. It does not alter bars, feature math, events,
models, thresholds, AFML, geometry, portfolio weights, or broker execution.

The state contract is:
- serialize and validate the complete JSON object before replacing the target;
- replace within the same directory so ``os.replace`` is atomic on Windows;
- retain a throttled last-known-good backup;
- quarantine malformed primary files rather than deleting them;
- restore a valid backup when available;
- otherwise return a caller-supplied fresh state so the dashboard's existing
  new-state cursor guard can skip already-existing live bars safely.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

STATE_INTEGRITY_VERSION = "2026-08-13_v2_atomic_state_recovery_v2_windows_replace_retry"

_LOCKS_GUARD = threading.RLock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_LAST_BACKUP_MONOTONIC_BY_PATH: dict[str, float] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _validate_json_object_bytes(payload: bytes, *, source: str) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded)
    except Exception as exc:
        raise ValueError(f"Invalid JSON state at {source}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Expected JSON object at {source}, got {type(value).__name__}")
    return value


def read_json_object(path: Path) -> dict[str, Any]:
    return _validate_json_object_bytes(path.read_bytes(), source=str(path))


def _write_bytes_fsync(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


ATOMIC_REPLACE_RETRY_BUDGET_SECONDS = 2.0
ATOMIC_REPLACE_RETRY_INITIAL_SLEEP_SECONDS = 0.002
ATOMIC_REPLACE_RETRY_MAX_SLEEP_SECONDS = 0.05
_TRANSIENT_WINDOWS_REPLACE_WINERRORS = {5, 32, 33}


def _is_transient_atomic_replace_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    return bool(os.name == "nt" and winerror in _TRANSIENT_WINDOWS_REPLACE_WINERRORS)


def _replace_with_retry(source: Path, destination: Path) -> None:
    deadline = time.monotonic() + ATOMIC_REPLACE_RETRY_BUDGET_SECONDS
    sleep_seconds = ATOMIC_REPLACE_RETRY_INITIAL_SLEEP_SECONDS
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if not _is_transient_atomic_replace_error(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(sleep_seconds, remaining))
            sleep_seconds = min(
                sleep_seconds * 2.0,
                ATOMIC_REPLACE_RETRY_MAX_SLEEP_SECONDS,
            )


def _atomic_write_bytes(path: Path, payload: bytes, *, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.{marker}.tmp"
    )
    try:
        _write_bytes_fsync(tmp, payload)
        _replace_with_retry(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _serialize_state(state: Mapping[str, Any]) -> bytes:
    text = json.dumps(
        dict(state),
        indent=2,
        default=str,
        allow_nan=True,
        sort_keys=False,
    ) + "\n"
    payload = text.encode("utf-8")
    _validate_json_object_bytes(payload, source="in_memory_serialized_state")
    return payload


def _archive_corrupt_file(path: Path, corrupt_dir: Path) -> Path | None:
    if not path.exists():
        return None
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    archive = corrupt_dir / f"{path.stem}.{_utc_stamp()}.corrupt{path.suffix}"
    try:
        os.replace(path, archive)
        return archive
    except Exception:
        try:
            shutil.copy2(path, archive)
            path.unlink()
            return archive
        except Exception:
            return None


def atomic_write_state(
    path: Path,
    state: Mapping[str, Any],
    *,
    backup_path: Path | None = None,
    backup_interval_seconds: float = 300.0,
    logger: Any = None,
) -> dict[str, Any]:
    """Atomically replace a JSON state file and retain a valid prior backup."""
    path = Path(path)
    backup_path = Path(backup_path) if backup_path is not None else path.with_suffix(path.suffix + ".last_good")
    payload = _serialize_state(state)
    key = str(path.resolve())
    backup_written = False

    with _path_lock(path):
        now_mono = time.monotonic()
        last_backup = _LAST_BACKUP_MONOTONIC_BY_PATH.get(key, float("-inf"))
        backup_due = (
            not backup_path.exists()
            or (now_mono - last_backup) >= max(float(backup_interval_seconds), 0.0)
        )
        if path.exists() and backup_due:
            try:
                prior_payload = path.read_bytes()
                _validate_json_object_bytes(prior_payload, source=str(path))
                _atomic_write_bytes(backup_path, prior_payload, marker="backup")
                _LAST_BACKUP_MONOTONIC_BY_PATH[key] = now_mono
                backup_written = True
            except Exception as exc:
                if logger is not None:
                    logger.warning(
                        "STATE_BACKUP_SKIPPED path=%s reason=%s:%s",
                        path,
                        type(exc).__name__,
                        exc,
                    )

        _atomic_write_bytes(path, payload, marker="state")
        # Verify the final file, not just the temporary payload.
        read_json_object(path)

    return {
        "version": STATE_INTEGRITY_VERSION,
        "status": "saved",
        "path": str(path),
        "backup_path": str(backup_path),
        "backup_written": bool(backup_written),
        "bytes_written": int(len(payload)),
    }


def load_state_with_recovery(
    path: Path,
    *,
    fresh_state_factory: Callable[[], dict[str, Any]],
    backup_path: Path | None = None,
    corrupt_dir: Path | None = None,
    logger: Any = None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Load state, recover a backup, or return a safe fresh-state contract."""
    path = Path(path)
    backup_path = Path(backup_path) if backup_path is not None else path.with_suffix(path.suffix + ".last_good")
    corrupt_dir = Path(corrupt_dir) if corrupt_dir is not None else path.parent / "corrupt_state_archives"
    primary_error: str | None = None
    archived_path: Path | None = None

    with _path_lock(path):
        if path.exists():
            try:
                state = read_json_object(path)
                return state, False, {
                    "version": STATE_INTEGRITY_VERSION,
                    "status": "loaded_primary",
                    "path": str(path),
                    "backup_path": str(backup_path),
                    "created_fresh_state": False,
                }
            except Exception as exc:
                primary_error = f"{type(exc).__name__}: {exc}"
                archived_path = _archive_corrupt_file(path, corrupt_dir)
                if logger is not None:
                    logger.error(
                        "STATE_PRIMARY_CORRUPT path=%s archived=%s error=%s",
                        path,
                        archived_path,
                        primary_error,
                    )

        if backup_path.exists():
            try:
                state = read_json_object(backup_path)
                restored_payload = _serialize_state(state)
                _atomic_write_bytes(path, restored_payload, marker="restore")
                report = {
                    "version": STATE_INTEGRITY_VERSION,
                    "status": "recovered_from_last_good_backup",
                    "path": str(path),
                    "backup_path": str(backup_path),
                    "corrupt_archive_path": str(archived_path) if archived_path else None,
                    "primary_error": primary_error,
                    "created_fresh_state": False,
                }
                if logger is not None:
                    logger.warning(
                        "STATE_RECOVERED_FROM_BACKUP primary=%s backup=%s archived=%s",
                        path,
                        backup_path,
                        archived_path,
                    )
                state["last_state_integrity_recovery"] = report
                return state, False, report
            except Exception as exc:
                backup_error = f"{type(exc).__name__}: {exc}"
                backup_archive = _archive_corrupt_file(backup_path, corrupt_dir)
                if logger is not None:
                    logger.error(
                        "STATE_BACKUP_CORRUPT path=%s archived=%s error=%s",
                        backup_path,
                        backup_archive,
                        backup_error,
                    )
            else:  # pragma: no cover - retained for structural clarity
                backup_error = None
        else:
            backup_error = None

        state = fresh_state_factory()
        report = {
            "version": STATE_INTEGRITY_VERSION,
            "status": (
                "initialized_fresh_after_corrupt_state"
                if primary_error
                else "initialized_fresh_no_existing_state"
            ),
            "path": str(path),
            "backup_path": str(backup_path),
            "corrupt_archive_path": str(archived_path) if archived_path else None,
            "primary_error": primary_error,
            "backup_error": backup_error,
            "created_fresh_state": True,
            "safety_contract": "dashboard_new_state_cursor_guard_skips_existing_live_bars",
        }
        state["last_state_integrity_recovery"] = report
        if logger is not None:
            logger.warning(
                "STATE_INITIALIZED_FRESH status=%s primary=%s archived=%s",
                report["status"],
                path,
                archived_path,
            )
        return state, True, report


__all__ = [
    "STATE_INTEGRITY_VERSION",
    "atomic_write_state",
    "load_state_with_recovery",
    "read_json_object",
]
