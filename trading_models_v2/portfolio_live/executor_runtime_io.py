from __future__ import annotations

"""Executor hot-path I/O primitives for Quantara V2.

Production contract
-------------------
- JSONL is the executor IPC transport; CSV is operator/audit fallback only.
- Idle signal polls perform metadata checks only and read zero payload bytes.
- The recent non-terminal signal window remains available on every poll so the
  existing transient plan/order retry semantics are preserved.
- Crash-sensitive lifecycle/risk state is checkpointed only when materially
  changed; the large compatibility state may be persisted at a slower cadence.
- This module does not alter signal validation, risk sizing, order planning,
  order submission, or any research/live trading mathematics.
"""

import copy
import hashlib
import json
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso


RUNTIME_IO_VERSION = "2026-08-13_executor_runtime_io_v1"


def _utc_now_ts() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _utc(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except Exception:
        return None


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _path_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


class RecentJsonlSignalFeed:
    """Incremental append-aware reader for dashboard-approved signals.

    The feed intentionally returns the entire *recent executable window* on
    every poll rather than only newly appended rows.  The executor's existing
    candidate_rows() function owns terminal de-duplication and retry scheduling,
    so a transiently deferred signal must remain visible until it executes,
    terminalises, or expires.
    """

    def __init__(
        self,
        *,
        jsonl_path: Path,
        csv_fallback_path: Path,
        max_age_seconds: float,
        bootstrap_max_bytes: int = 64 * 1024 * 1024,
        bootstrap_margin_seconds: float = 120.0,
    ) -> None:
        self.jsonl_path = Path(jsonl_path)
        self.csv_fallback_path = Path(csv_fallback_path)
        self.max_age_seconds = max(float(max_age_seconds), 1.0)
        self.bootstrap_max_bytes = max(int(bootstrap_max_bytes), 1024 * 1024)
        self.bootstrap_margin_seconds = max(float(bootstrap_margin_seconds), 0.0)

        self._lock = threading.RLock()
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._mtime_ns = 0
        self._anchor_digest: str | None = None
        self._rows: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._csv_identity: tuple[int, int, int, int] | None = None
        self._csv_cache = pd.DataFrame()
        self._bootstrapped = False

        self.stats: dict[str, int] = {
            "polls": 0,
            "idle_polls": 0,
            "jsonl_bootstraps": 0,
            "jsonl_append_reads": 0,
            "jsonl_bytes_read": 0,
            "jsonl_rows_parsed": 0,
            "csv_fallback_reads": 0,
            "parse_errors": 0,
            "rotations_or_truncations": 0,
        }

    @staticmethod
    def _signal_id(row: Mapping[str, Any]) -> str:
        value = str(row.get("signal_id") or "").strip()
        if value:
            return value
        payload = _stable_json(dict(row))
        return "missing_signal_id:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_time(row: Mapping[str, Any]) -> pd.Timestamp | None:
        for key in ("event_time", "Datetime", "created_at", "exported_at"):
            stamp = _utc(row.get(key))
            if stamp is not None:
                return stamp
        return None

    def _cutoff(self) -> pd.Timestamp:
        return _utc_now_ts() - pd.Timedelta(
            seconds=self.max_age_seconds + self.bootstrap_margin_seconds
        )

    def _keep(self, row: Mapping[str, Any]) -> bool:
        stamp = self._event_time(row)
        # Malformed timestamps stay visible so downstream validation can fail
        # closed instead of transport silently deleting the record.
        return stamp is None or stamp >= self._cutoff()

    def _parse_lines(self, payload: bytes) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for raw in payload.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except Exception:
                self.stats["parse_errors"] += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
                self.stats["jsonl_rows_parsed"] += 1
            else:
                self.stats["parse_errors"] += 1
        return rows

    def _replace_recent(self, rows: Iterable[Mapping[str, Any]]) -> None:
        self._rows.clear()
        for row in rows:
            if self._keep(row):
                self._rows[self._signal_id(row)] = dict(row)
        self._evict_old()

    def _merge_recent(self, rows: Iterable[Mapping[str, Any]]) -> None:
        for row in rows:
            if not self._keep(row):
                continue
            sid = self._signal_id(row)
            self._rows.pop(sid, None)
            self._rows[sid] = dict(row)
        self._evict_old()

    def _evict_old(self) -> None:
        cutoff = self._cutoff()
        for sid, row in list(self._rows.items()):
            stamp = self._event_time(row)
            if stamp is not None and stamp < cutoff:
                self._rows.pop(sid, None)

    def _full_recent_scan(self) -> tuple[list[dict[str, Any]], int]:
        rows: list[dict[str, Any]] = []
        bytes_read = 0
        with self.jsonl_path.open("rb") as handle:
            for raw in handle:
                bytes_read += len(raw)
                for row in self._parse_lines(raw):
                    if self._keep(row):
                        rows.append(row)
        return rows, bytes_read

    def _capture_anchor(self) -> None:
        if self._offset <= 0 or not self.jsonl_path.exists():
            self._anchor_digest = None
            return
        width = min(self._offset, 512)
        with self.jsonl_path.open("rb") as handle:
            handle.seek(self._offset - width)
            payload = handle.read(width)
        self._anchor_digest = hashlib.sha256(payload).hexdigest()

    def _anchor_matches(self) -> bool:
        if self._anchor_digest is None or self._offset <= 0:
            return True
        width = min(self._offset, 512)
        try:
            with self.jsonl_path.open("rb") as handle:
                handle.seek(self._offset - width)
                payload = handle.read(width)
        except Exception:
            return False
        return hashlib.sha256(payload).hexdigest() == self._anchor_digest

    def _bootstrap_jsonl(self, logger: Any) -> None:
        stat = self.jsonl_path.stat()
        size = int(stat.st_size)
        identity = (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))

        if size <= 0:
            self._identity = identity
            self._offset = 0
            self._mtime_ns = int(stat.st_mtime_ns)
            self._anchor_digest = None
            self._replace_recent([])
            self._bootstrapped = True
            return

        start = max(0, size - self.bootstrap_max_bytes)
        with self.jsonl_path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(size - start)
        self.stats["jsonl_bytes_read"] += len(payload)

        if start > 0:
            newline = payload.find(b"\n")
            payload = payload[newline + 1 :] if newline >= 0 else b""

        last_newline = payload.rfind(b"\n")
        complete = payload[: last_newline + 1] if last_newline >= 0 else b""
        rows = self._parse_lines(complete)

        # If the bounded tail contains only records newer than our cutoff, the
        # tail may have omitted still-live retry rows. Correctness wins once at
        # startup: stream the full JSONL and retain only the recent window.
        cutoff = self._cutoff()
        finite_times = [
            stamp
            for row in rows
            if (stamp := self._event_time(row)) is not None
        ]
        if start > 0 and (not finite_times or min(finite_times) > cutoff):
            rows, extra_bytes = self._full_recent_scan()
            self.stats["jsonl_bytes_read"] += int(extra_bytes)

        self._replace_recent(rows)
        self._identity = identity

        # Dashboard JSONL should be newline terminated.  If a partial final line
        # exists, leave it unconsumed so the next poll can retry it safely.
        with self.jsonl_path.open("rb") as handle:
            handle.seek(max(0, size - 1))
            newline_terminated = handle.read(1) == b"\n"
        if newline_terminated:
            self._offset = size
        else:
            with self.jsonl_path.open("rb") as handle:
                back = min(size, 1024 * 1024)
                handle.seek(size - back)
                tail = handle.read(back)
            index = tail.rfind(b"\n")
            self._offset = (size - back + index + 1) if index >= 0 else 0

        self._mtime_ns = int(stat.st_mtime_ns)
        self._capture_anchor()
        self.stats["jsonl_bootstraps"] += 1
        self._bootstrapped = True

        if logger is not None:
            logger.info(
                "EXECUTOR_SIGNAL_JSONL_BOOTSTRAP path=%s size=%s offset=%s recent_rows=%s",
                self.jsonl_path,
                size,
                self._offset,
                len(self._rows),
            )

    def _read_jsonl(self, logger: Any) -> pd.DataFrame:
        stat = self.jsonl_path.stat()
        identity = (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))
        size = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)

        changed_behind_cursor = (
            self._bootstrapped
            and mtime_ns != self._mtime_ns
            and not self._anchor_matches()
        )
        needs_rebootstrap = (
            not self._bootstrapped
            or self._identity != identity
            or size < self._offset
            or changed_behind_cursor
            or (size == self._offset and mtime_ns != self._mtime_ns)
        )
        if needs_rebootstrap:
            if self._bootstrapped:
                self.stats["rotations_or_truncations"] += 1
            self._bootstrap_jsonl(logger)
            return pd.DataFrame(list(self._rows.values()))

        if size == self._offset:
            self.stats["idle_polls"] += 1
            self._evict_old()
            return pd.DataFrame(list(self._rows.values()))

        with self.jsonl_path.open("rb") as handle:
            handle.seek(self._offset)
            payload = handle.read(size - self._offset)
        self.stats["jsonl_bytes_read"] += len(payload)

        last_newline = payload.rfind(b"\n")
        if last_newline < 0:
            return pd.DataFrame(list(self._rows.values()))

        complete = payload[: last_newline + 1]
        rows = self._parse_lines(complete)
        self._merge_recent(rows)
        self._offset += len(complete)
        self._mtime_ns = mtime_ns
        self._capture_anchor()
        self.stats["jsonl_append_reads"] += 1

        if logger is not None and rows:
            logger.info(
                "EXECUTOR_SIGNAL_JSONL_APPEND bytes=%s rows=%s recent_rows=%s offset=%s",
                len(complete),
                len(rows),
                len(self._rows),
                self._offset,
            )
        return pd.DataFrame(list(self._rows.values()))

    def _read_csv_fallback(self, logger: Any) -> pd.DataFrame:
        signature = _path_identity(self.csv_fallback_path)
        if signature is None:
            return pd.DataFrame()
        if signature != self._csv_identity:
            self._csv_cache = pd.read_csv(self.csv_fallback_path)
            self._csv_identity = signature
            self.stats["csv_fallback_reads"] += 1
            if logger is not None:
                logger.warning(
                    "EXECUTOR_SIGNAL_CSV_FALLBACK path=%s rows=%s; restore JSONL transport",
                    self.csv_fallback_path,
                    len(self._csv_cache),
                )
        if self._csv_cache.empty:
            return self._csv_cache.copy()

        work = self._csv_cache.copy()
        for column in ("event_time", "Datetime", "created_at", "exported_at"):
            if column in work.columns:
                times = to_datetime_iso(work[column], utc=True, errors="coerce")
                keep = times.isna() | (times >= self._cutoff())
                return work.loc[keep].reset_index(drop=True)
        return work

    def read(self, logger: Any = None) -> pd.DataFrame:
        with self._lock:
            self.stats["polls"] += 1
            if self.jsonl_path.exists():
                return self._read_jsonl(logger)
            return self._read_csv_fallback(logger)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": RUNTIME_IO_VERSION,
                "transport": "jsonl_incremental_cursor",
                "jsonl_path": str(self.jsonl_path),
                "csv_fallback_path": str(self.csv_fallback_path),
                "offset": int(self._offset),
                "recent_rows": int(len(self._rows)),
                **dict(self.stats),
            }


class AsyncNotificationWorker:
    """Bounded single worker for observability-only network notifications.

    The executor submits and immediately continues. Network delivery runs on
    this worker thread. Completion records are drained by the executor main
    loop so mutable executor state remains main-thread-owned.
    """

    VERSION = "2026-08-20_async_notification_worker_v1"

    def __init__(self, *, sender: Callable[[str], Any], name: str = "quantara_async_notification", max_queue_size: int = 256) -> None:
        self.sender = sender
        self.name = str(name)
        self.max_queue_size = max(int(max_queue_size), 1)
        self._queue = queue.Queue(maxsize=self.max_queue_size)
        self._results = queue.Queue()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._accepting = True
        self.stats = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "queue_full": 0,
            "results_drained": 0,
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._accepting:
                raise RuntimeError("async_notification_worker_already_stopped")
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def submit(self, *, purpose: str, message: str) -> dict[str, Any]:
        self.start()
        item = {
            "purpose": str(purpose),
            "message": str(message),
            "submitted_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self.stats["queue_full"] += 1
            return {"queued": False, "reason": "queue_full", "purpose": str(purpose)}
        with self._lock:
            self.stats["submitted"] += 1
        return {"queued": True, "reason": "queued", "purpose": str(purpose)}

    def _run(self) -> None:
        while (not self._stop_event.is_set()) or (not self._queue.empty()):
            try:
                item = self._queue.get(timeout=0.10)
            except queue.Empty:
                continue
            started = time.perf_counter()
            result = None
            error = None
            try:
                result = self.sender(str(item.get("message") or ""))
            except Exception as exc:
                error = "%s:%s" % (type(exc).__name__, exc)
                with self._lock:
                    self.stats["failed"] += 1
            finally:
                self._results.put({
                    "purpose": str(item.get("purpose") or ""),
                    "submitted_at": item.get("submitted_at"),
                    "completed_at": pd.Timestamp.now(tz="UTC").isoformat(),
                    "message_length": len(str(item.get("message") or "")),
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                    "result": result,
                    "error": error,
                })
                with self._lock:
                    self.stats["completed"] += 1
                self._queue.task_done()

    def drain_results(self, max_items: int = 1000) -> list[dict[str, Any]]:
        output = []
        limit = max(int(max_items), 1)
        while len(output) < limit:
            try:
                output.append(self._results.get_nowait())
            except queue.Empty:
                break
        if output:
            with self._lock:
                self.stats["results_drained"] += len(output)
        return output

    def stop(self, *, drain: bool = True, timeout_seconds: float = 20.0) -> dict[str, Any]:
        with self._lock:
            self._accepting = False
            self._stop_event.set()
            thread = self._thread
        if not drain:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        if thread is not None:
            thread.join(timeout=max(float(timeout_seconds), 0.0))
        snapshot = self.snapshot()
        snapshot["drain_requested"] = bool(drain)
        snapshot["shutdown_timeout_seconds"] = float(timeout_seconds)
        snapshot["shutdown_clean"] = not bool(snapshot.get("thread_alive"))
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.VERSION,
                "name": self.name,
                "max_queue_size": self.max_queue_size,
                "thread_alive": bool(self._thread is not None and self._thread.is_alive()),
                "accepting": bool(self._accepting),
                "pending_messages": int(self._queue.qsize()),
                "pending_results": int(self._results.qsize()),
                **dict(self.stats),
            }


class ExecutorCriticalStateStore:
    """Crash-safe compact checkpoint for executor state that cannot wait.

    The large compatibility state remains available for operators and legacy
    tooling, but it does not need to be rewritten on every heartbeat.  This
    checkpoint persists only lifecycle/risk fields whose loss could alter
    de-duplication, retries, attribution, or prop-risk safety after a crash.
    """

    SCHEMA_VERSION = "quantara_executor_critical_state_v1"

    def __init__(
        self,
        *,
        path: Path,
        backup_path: Path,
        atomic_writer: Callable[..., Any],
    ) -> None:
        self.path = Path(path)
        self.backup_path = Path(backup_path)
        self.atomic_writer = atomic_writer
        self._lock = threading.RLock()
        self._last_hash: str | None = None

    @staticmethod
    def _compact_terminal_map(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for sid, value in (state.get("terminal_signal_ids") or {}).items():
            record = dict(value) if isinstance(value, Mapping) else {}
            compact: dict[str, Any] = {
                "status": record.get("status"),
                "reason": record.get("reason"),
                "timestamp": record.get("timestamp"),
            }
            for key in ("retcode", "order", "deal", "ticket"):
                scalar = record.get(key)
                if scalar is not None and not isinstance(scalar, (dict, list, tuple, set)):
                    compact[key] = scalar
            output[str(sid)] = compact
        return output

    @staticmethod
    def _position_records(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        mappings = (
            ("open_position_metadata_by_ticket", "position_ticket"),
            ("open_position_metadata_by_signal_id", "signal_id"),
            ("open_position_metadata_by_order", "order"),
            ("open_position_metadata_by_deal", "deal"),
            ("open_position_metadata_by_comment", "comment"),
        )
        for mapping_name, source_key in mappings:
            mapping = state.get(mapping_name) or {}
            if not isinstance(mapping, Mapping):
                continue
            for map_key, raw in mapping.items():
                if not isinstance(raw, Mapping):
                    continue
                value = copy.deepcopy(dict(raw))
                value.setdefault(source_key, map_key)
                stable = str(value.get("signal_id") or "").strip()
                if not stable:
                    stable = "|".join(
                        str(value.get(name) or "")
                        for name in ("asset", "comment", "position_ticket", "order", "deal")
                    )
                prior = records.get(stable)
                if prior is None or len(_stable_json(value)) > len(_stable_json(prior)):
                    records[stable] = value
        return list(records.values())

    @staticmethod
    def _rebuild_position_indexes(
        state: dict[str, Any], records: Iterable[Mapping[str, Any]]
    ) -> None:
        by_ticket: dict[str, dict[str, Any]] = {}
        by_signal: dict[str, dict[str, Any]] = {}
        by_order: dict[str, dict[str, Any]] = {}
        by_deal: dict[str, dict[str, Any]] = {}
        by_comment: dict[str, dict[str, Any]] = {}

        for raw in records:
            meta = copy.deepcopy(dict(raw))
            for key, target in (
                ("position_ticket", by_ticket),
                ("signal_id", by_signal),
                ("order", by_order),
                ("deal", by_deal),
                ("comment", by_comment),
            ):
                value = meta.get(key)
                if value not in (None, ""):
                    target[str(value)] = meta

        state["open_position_metadata_by_ticket"] = by_ticket
        state["open_position_metadata_by_signal_id"] = by_signal
        state["open_position_metadata_by_order"] = by_order
        state["open_position_metadata_by_deal"] = by_deal
        state["open_position_metadata_by_comment"] = by_comment

    @staticmethod
    def _critical_prop_risk_state(state: Mapping[str, Any]) -> dict[str, Any]:
        raw = state.get("prop_risk_state") or {}
        if not isinstance(raw, Mapping):
            return {}
        keys = (
            "daily_anchor_utc_day",
            "daily_anchor_start_balance",
            "daily_anchor_start_equity",
            "daily_anchor_created_at",
            "daily_anchor_reset_offset_hours",
            "qualifying_trading_days",
            "symbol_risk_lockout_utc_day_by_asset",
            "account_login",
            "account_server",
            "active_account_rules",
            "risk_contract_derivation_version",
            "realized_balance_high_watermark",
            "equity_high_watermark",
            "overall_high_watermark",
            "equity_high_watermark_source",
            "current_utc_day",
            "current_risk_day",
            "effective_daily_reset_offset_hours",
            "effective_daily_reset_offset_source",
            "daily_start_balance",
            "daily_start_equity",
            "daily_start_balance_source",
        )
        return {key: copy.deepcopy(raw.get(key)) for key in keys if key in raw}

    def _payload(self, state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "runtime_io_version": RUNTIME_IO_VERSION,
            "updated_at": state.get("updated_at"),
            "terminal_signal_ids": self._compact_terminal_map(state),
            "position_metadata_records": self._position_records(state),
            "ambiguous_position_metadata_comments": copy.deepcopy(
                state.get("ambiguous_position_metadata_comments") or {}
            ),
            "unresolved_position_metadata_by_ticket": copy.deepcopy(
                state.get("unresolved_position_metadata_by_ticket") or {}
            ),
            "transient_order_retry_by_signal": copy.deepcopy(
                state.get("transient_order_retry_by_signal") or {}
            ),
            "transient_plan_retry_by_signal": copy.deepcopy(
                state.get("transient_plan_retry_by_signal") or {}
            ),
            "market_closed_cooldown_by_symbol": copy.deepcopy(
                state.get("market_closed_cooldown_by_symbol") or {}
            ),
            "preferred_filling_mode_by_symbol": copy.deepcopy(
                state.get("preferred_filling_mode_by_symbol") or {}
            ),
            "prop_risk_state": self._critical_prop_risk_state(state),
            "equity_high_watermark_migration": copy.deepcopy(
                state.get("equity_high_watermark_migration") or {}
            ),
            "execution_state_integrity_blocked": bool(
                state.get("execution_state_integrity_blocked", False)
            ),
            "closed_trade_ledger_integrity_blocked": bool(
                state.get("closed_trade_ledger_integrity_blocked", False)
            ),
            "last_manifest_hash": state.get("last_manifest_hash"),
            "last_bar_policy_hash": state.get("last_bar_policy_hash"),
        }

    @staticmethod
    def _payload_hash(payload: Mapping[str, Any]) -> str:
        stable = {key: value for key, value in payload.items() if key != "updated_at"}
        return _digest(stable)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def _read_best(self) -> dict[str, Any] | None:
        candidates = [
            value
            for path in (self.path, self.backup_path)
            if path.exists() and (value := self._read_json(path)) is not None
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: _utc(item.get("updated_at")) or pd.Timestamp(0, tz="UTC"),
            reverse=True,
        )
        return candidates[0]

    def restore_into(self, state: dict[str, Any], logger: Any = None) -> dict[str, Any]:
        with self._lock:
            checkpoint = self._read_best()
            if not checkpoint:
                self._last_hash = self._payload_hash(self._payload(state))
                return {"status": "no_critical_checkpoint"}

            if checkpoint.get("schema_version") != self.SCHEMA_VERSION:
                return {
                    "status": "ignored_unknown_schema",
                    "schema_version": checkpoint.get("schema_version"),
                }

            checkpoint_at = _utc(checkpoint.get("updated_at"))
            full_at = _utc(state.get("updated_at"))
            if checkpoint_at is not None and (full_at is None or checkpoint_at >= full_at):
                state["terminal_signal_ids"] = copy.deepcopy(
                    checkpoint.get("terminal_signal_ids") or {}
                )
                self._rebuild_position_indexes(
                    state, checkpoint.get("position_metadata_records") or []
                )
                for key in (
                    "ambiguous_position_metadata_comments",
                    "unresolved_position_metadata_by_ticket",
                    "transient_order_retry_by_signal",
                    "transient_plan_retry_by_signal",
                    "market_closed_cooldown_by_symbol",
                    "preferred_filling_mode_by_symbol",
                    "prop_risk_state",
                    "equity_high_watermark_migration",
                ):
                    state[key] = copy.deepcopy(checkpoint.get(key) or {})
                for key in (
                    "execution_state_integrity_blocked",
                    "closed_trade_ledger_integrity_blocked",
                ):
                    state[key] = bool(checkpoint.get(key, False))
                for key in ("last_manifest_hash", "last_bar_policy_hash"):
                    if key in checkpoint:
                        state[key] = checkpoint.get(key)
                status = "restored_newer_or_equal_critical_checkpoint"
            else:
                status = "full_state_newer_than_critical_checkpoint"

            self._last_hash = self._payload_hash(self._payload(state))
            if logger is not None:
                logger.info(
                    "EXECUTOR_CRITICAL_STATE_RECOVERY status=%s checkpoint_at=%s full_at=%s",
                    status,
                    checkpoint.get("updated_at"),
                    state.get("updated_at"),
                )
            return {
                "status": status,
                "checkpoint_updated_at": checkpoint.get("updated_at"),
            }

    def persist_if_changed(
        self,
        state: Mapping[str, Any],
        *,
        logger: Any = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self._payload(state)
            digest = self._payload_hash(payload)
            if not force and self._last_hash == digest:
                return {
                    "written": False,
                    "reason": "unchanged",
                    "sha256": digest,
                }

            self.path.parent.mkdir(parents=True, exist_ok=True)
            result = self.atomic_writer(
                self.path,
                payload,
                backup_path=self.backup_path,
                backup_interval_seconds=60.0,
                logger=logger,
            )
            self._last_hash = digest
            return {
                "written": True,
                "reason": "forced" if force else "material_change",
                "sha256": digest,
                "writer_result": result,
            }
