from __future__ import annotations

"""Low-overhead production observability for the V2 live processes.

The module is intentionally operational only. It does not alter bars, features,
models, thresholds, AFML, portfolio sizing, execution geometry, or order flow.
"""

import copy
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # Optional on a fresh VPS; the fallback remains functional.
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


OBSERVABILITY_VERSION = "2026-07-30_v2_production_observability_v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SemaphoreStats:
    name: str
    capacity: int
    acquire_count: int = 0
    contended_count: int = 0
    total_wait_seconds: float = 0.0
    max_wait_seconds: float = 0.0
    active_count: int = 0
    max_active_count: int = 0


class ObservedSemaphore:
    """Drop-in context-manager wrapper around a bounded semaphore."""

    def __init__(self, capacity: int, *, name: str) -> None:
        self._semaphore = threading.BoundedSemaphore(max(int(capacity), 1))
        self._stats = SemaphoreStats(name=str(name), capacity=max(int(capacity), 1))
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        started = time.perf_counter()
        if timeout is None:
            acquired = self._semaphore.acquire(blocking)
        else:
            acquired = self._semaphore.acquire(blocking, timeout)
        elapsed = max(time.perf_counter() - started, 0.0)
        if acquired:
            with self._lock:
                self._stats.acquire_count += 1
                self._stats.total_wait_seconds += elapsed
                self._stats.max_wait_seconds = max(self._stats.max_wait_seconds, elapsed)
                if elapsed >= 0.001:
                    self._stats.contended_count += 1
                self._stats.active_count += 1
                self._stats.max_active_count = max(
                    self._stats.max_active_count,
                    self._stats.active_count,
                )
        return acquired

    def release(self) -> None:
        self._semaphore.release()
        with self._lock:
            self._stats.active_count = max(self._stats.active_count - 1, 0)

    def __enter__(self) -> "ObservedSemaphore":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = copy.deepcopy(self._stats.__dict__)
        count = int(payload.get("acquire_count") or 0)
        total = float(payload.get("total_wait_seconds") or 0.0)
        payload["mean_wait_seconds"] = total / count if count else 0.0
        payload["version"] = OBSERVABILITY_VERSION
        return payload


class RuntimeMetricsSampler:
    """Collect process/system metrics without depending on trading code."""

    def __init__(
        self,
        *,
        process_name: str,
        output_root: Path | None = None,
        persist_interval_seconds: float = 30.0,
    ) -> None:
        self.process_name = str(process_name)
        self.output_root = Path(output_root) if output_root is not None else None
        self.persist_interval_seconds = max(float(persist_interval_seconds), 1.0)
        self.metrics_jsonl = (
            self.output_root / "runtime_metrics.jsonl"
            if self.output_root is not None
            else None
        )
        self._lock = threading.RLock()
        self._last_disk: dict[str, int] | None = None
        self._last_at: float | None = None
        self._last_payload: dict[str, Any] = {}
        self._last_persist_at: float | None = None
        self._process = psutil.Process(os.getpid()) if psutil is not None else None
        if self._process is not None:
            try:
                self._process.cpu_percent(interval=None)
            except Exception:
                pass

    def snapshot(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        payload: dict[str, Any] = {
            "version": OBSERVABILITY_VERSION,
            "created_at": utc_now(),
            "process_name": self.process_name,
            "pid": os.getpid(),
            "python_thread_count": threading.active_count(),
            "psutil_available": psutil is not None,
            "psutil_install_hint": (
                None if psutil is not None
                else "python -m pip install -r requirements_production_observability.txt"
            ),
        }
        if self._process is not None:
            try:
                mem = self._process.memory_info()
                payload.update(
                    {
                        "process_cpu_percent": float(self._process.cpu_percent(interval=None)),
                        "process_rss_bytes": int(mem.rss),
                        "process_vms_bytes": int(mem.vms),
                        "process_native_thread_count": int(self._process.num_threads()),
                    }
                )
            except Exception as exc:
                payload["process_metrics_error"] = f"{type(exc).__name__}:{exc}"
            try:
                vm = psutil.virtual_memory()
                payload.update(
                    {
                        "system_cpu_percent": float(psutil.cpu_percent(interval=None)),
                        "system_memory_total_bytes": int(vm.total),
                        "system_memory_available_bytes": int(vm.available),
                        "system_memory_percent": float(vm.percent),
                    }
                )
            except Exception as exc:
                payload["system_metrics_error"] = f"{type(exc).__name__}:{exc}"
            try:
                io = self._process.io_counters()
                current = {
                    "read_bytes": int(getattr(io, "read_bytes", 0) or 0),
                    "write_bytes": int(getattr(io, "write_bytes", 0) or 0),
                    "read_count": int(getattr(io, "read_count", 0) or 0),
                    "write_count": int(getattr(io, "write_count", 0) or 0),
                }
                payload["process_io_totals"] = current
                with self._lock:
                    if self._last_disk is not None and self._last_at is not None:
                        elapsed = max(now - self._last_at, 1e-9)
                        payload["process_io_interval_seconds"] = elapsed
                        payload["process_read_bytes_per_second"] = max(
                            current["read_bytes"] - self._last_disk["read_bytes"], 0
                        ) / elapsed
                        payload["process_write_bytes_per_second"] = max(
                            current["write_bytes"] - self._last_disk["write_bytes"], 0
                        ) / elapsed
                    self._last_disk = current
                    self._last_at = now
            except Exception as exc:
                payload["io_metrics_error"] = f"{type(exc).__name__}:{exc}"
        else:
            try:
                load = os.getloadavg()
                payload["system_load_1m"] = float(load[0])
                payload["system_load_5m"] = float(load[1])
                payload["system_load_15m"] = float(load[2])
            except Exception:
                pass

        if self.output_root is not None:
            try:
                usage = shutil.disk_usage(self.output_root)
                payload.update(
                    {
                        "output_disk_total_bytes": int(usage.total),
                        "output_disk_used_bytes": int(usage.used),
                        "output_disk_free_bytes": int(usage.free),
                    }
                )
            except Exception as exc:
                payload["output_disk_metrics_error"] = f"{type(exc).__name__}:{exc}"
        if extra:
            payload.update(copy.deepcopy(extra))
        if self.metrics_jsonl is not None:
            payload["metrics_jsonl"] = str(self.metrics_jsonl)
        with self._lock:
            self._last_payload = copy.deepcopy(payload)
            persist_due = (
                self.metrics_jsonl is not None
                and (
                    self._last_persist_at is None
                    or now - self._last_persist_at >= self.persist_interval_seconds
                )
            )
            if persist_due:
                try:
                    assert self.metrics_jsonl is not None
                    self.metrics_jsonl.parent.mkdir(parents=True, exist_ok=True)
                    with self.metrics_jsonl.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
                    self._last_persist_at = now
                except Exception as exc:
                    payload["metrics_persist_error"] = f"{type(exc).__name__}:{exc}"
                    self._last_payload = copy.deepcopy(payload)
        return payload

    def last_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._last_payload)


class ProductionNoiseFilter(logging.Filter):
    """Suppress or rate-limit high-volume diagnostics while keeping incidents."""

    DEFAULT_SUPPRESSED_PREFIXES = (
        "LIVE_RUNTIME_STAGE ",
        "LIVE_LATENCY_STAGE ",
        "LIVE_LATENCY_ASSET_SNAPSHOT ",
        "LIVE_EDGE_SCORING_ORDER ",
        "LIVE_PARALLEL_EDGE_SCORING_BYPASS ",
        "P0_ASSET_QUEUE_DEDUPED ",
        "P0_ASSET_QUEUE_RERUN_MARKED ",
        "RUNTIME_STAGE_DIAGNOSTICS ",
        "RUNTIME_SCORE_START ",
        "RUNTIME_FEATURE_EVENT_READY ",
        "RUNTIME_EVENT_SCORE_FILTER ",
        "P0_ASSET_READINESS_RETRY_COALESCED ",
    )

    def __init__(
        self,
        *,
        verbose: bool = False,
        repeat_interval_seconds: float = 300.0,
        suppressed_prefixes: Iterable[str] | None = None,
    ) -> None:
        super().__init__()
        self.verbose = bool(verbose)
        self.repeat_interval_seconds = max(float(repeat_interval_seconds), 1.0)
        self.suppressed_prefixes = tuple(
            suppressed_prefixes or self.DEFAULT_SUPPRESSED_PREFIXES
        )
        self._lock = threading.RLock()
        self._last_by_signature: dict[str, float] = {}
        self._suppressed_counts: dict[str, int] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        if self.verbose or record.levelno >= logging.WARNING:
            return True
        message = record.getMessage()
        if not message.startswith(self.suppressed_prefixes):
            return True
        signature = message.split(" ", 1)[0]
        now = time.monotonic()
        with self._lock:
            last = self._last_by_signature.get(signature)
            if last is None or now - last >= self.repeat_interval_seconds:
                suppressed = self._suppressed_counts.pop(signature, 0)
                self._last_by_signature[signature] = now
                if suppressed:
                    record.msg = f"{message} suppressed_since_last={suppressed}"
                    record.args = ()
                return True
            self._suppressed_counts[signature] = (
                int(self._suppressed_counts.get(signature, 0)) + 1
            )
            return False


def install_production_log_filter(
    logger: logging.Logger,
    *,
    verbose: bool = False,
    repeat_interval_seconds: float = 300.0,
) -> ProductionNoiseFilter:
    filt = ProductionNoiseFilter(
        verbose=verbose,
        repeat_interval_seconds=repeat_interval_seconds,
    )
    for handler in logger.handlers:
        handler.addFilter(filt)
    return filt


__all__ = [
    "OBSERVABILITY_VERSION",
    "ObservedSemaphore",
    "ProductionNoiseFilter",
    "RuntimeMetricsSampler",
    "install_production_log_filter",
]
