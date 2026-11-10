#!/usr/bin/env python3
"""Post-install, non-trading health check for the production latency/I/O hardening patch.

This script reads local certification, dashboard status, journals, and logs only.
It never imports MetaTrader5, never sends orders, and never changes live state.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUT_DIR = HERE / "outputs"
DASHBOARD_OUTPUT_DIR = OUTPUT_DIR / "live_dashboard"
PENDING_DIR = HERE / "live_state" / "pending_live_bars"
REPORT_PATH = OUTPUT_DIR / "production_latency_io_runtime_validation.json"

REQUIRED_VERSION_TOKENS = (
    "durable_live_bar_journal_v1",
    "direct_full_authority_routing_v1",
    "bounded_parallel_scoring_v1",
    "continuation_api_repair_v2",
    "v2_live_streaming_hotpath_v2_inmemory_dependency_refresh",
)

ASSET_BATCH_RE = re.compile(
    r"LIVE_LATENCY_ASSET_BATCH\s+asset=(?P<asset>\S+).*?elapsed_ms=(?P<elapsed>[0-9.]+)"
)
JOB_DONE_RE = re.compile(
    r"LIVE_SCORING_QUEUE_JOB_(?:DONE|COMPLETED).*?elapsed(?:_ms)?=(?P<elapsed>[0-9.]+)(?P<unit>ms|s)?"
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # validation must report malformed/missing inputs, not crash
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(value, dict):
        return None, f"expected JSON object, got {type(value).__name__}"
    return value, None


def _candidate_logs(stem: str) -> list[Path]:
    found: list[Path] = []
    for directory in (HERE / "live_logs", HERE):
        if not directory.exists():
            continue
        found.extend(p for p in directory.glob(f"{stem}*.log*") if p.is_file())
    return sorted(set(found), key=lambda p: (p.stat().st_mtime_ns, p.name))


def _combined_log(stem: str) -> tuple[str, list[str]]:
    paths = _candidate_logs(stem)
    return "\n".join(_read_text(p) for p in paths), [str(p) for p in paths]


def _latest_dashboard_session(text: str) -> str:
    marker = "V2 PORTFOLIO LIVE DASHBOARD ORCHESTRATOR version="
    starts = [m.start() for m in re.finditer(re.escape(marker), text)]
    if not starts:
        return text
    # Prefer the newest session containing this patch. If logs end with an older session,
    # return the last session and let the version checks fail transparently.
    patched = [
        pos
        for pos in starts
        if all(token in text[pos : pos + 700] for token in REQUIRED_VERSION_TOKENS)
    ]
    pos = patched[-1] if patched else starts[-1]
    return text[pos:]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * q
    low = math.floor(idx)
    high = math.ceil(idx)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (idx - low)


def _summary_ms(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "median_ms": round(statistics.median(values), 3) if values else None,
        "p95_ms": round(_percentile(values, 0.95), 3) if values else None,
        "max_ms": round(max(values), 3) if values else None,
    }


def _journal_health() -> dict[str, Any]:
    result: dict[str, Any] = {
        "directory": str(PENDING_DIR),
        "assets": {},
        "total_pending": 0,
        "malformed_files": [],
    }
    if not PENDING_DIR.exists():
        return result
    now = datetime.now(timezone.utc).timestamp()
    for asset_dir in sorted(p for p in PENDING_DIR.iterdir() if p.is_dir()):
        files = sorted(asset_dir.glob("*.json"))
        oldest_age: float | None = None
        valid = 0
        for path in files:
            payload, error = _read_json(path)
            if error is not None:
                result["malformed_files"].append({"path": str(path), "error": error})
                continue
            valid += 1
            try:
                age = max(0.0, now - path.stat().st_mtime)
                oldest_age = age if oldest_age is None else max(oldest_age, age)
            except OSError:
                pass
        result["assets"][asset_dir.name.upper()] = {
            "pending_files": len(files),
            "valid_files": valid,
            "oldest_age_seconds": round(oldest_age, 3) if oldest_age is not None else None,
        }
        result["total_pending"] += len(files)
    return result


def _count(text: str, token: str) -> int:
    return text.count(token)


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []

    certification_candidates = [
        HERE / "native_one_row_certification.json",
        OUTPUT_DIR / "native_one_row_certification.json",
    ]
    certification_path = next((p for p in certification_candidates if p.exists()), certification_candidates[0])
    certification, cert_error = _read_json(certification_path)
    cert_summary: dict[str, Any] = {
        "path": str(certification_path),
        "loaded": certification is not None,
        "error": cert_error,
    }
    if certification is None:
        failures.append(f"native certification missing or unreadable: {cert_error}")
    else:
        cert_summary.update(
            {
                "schema_version": certification.get("schema_version"),
                "status": certification.get("status"),
                "asset_count": int(certification.get("passed_asset_count") or 0),
                "native_event_edge_count": certification.get("native_event_edge_count"),
                "full_authority_event_edge_count": certification.get("full_authority_event_edge_count"),
            }
        )
        if certification.get("status") != "PASS":
            failures.append(f"native certification status is {certification.get('status')!r}, expected PASS")
        if certification.get("schema_version") != "native_one_row_certification_v7_fast_long_sequential":
            failures.append(
                f"native certification schema is {certification.get('schema_version')!r}, expected native_one_row_certification_v7_fast_long_sequential"
            )

    status_path = DASHBOARD_OUTPUT_DIR / "latest_dashboard_status.json"
    status, status_error = _read_json(status_path)
    status_summary: dict[str, Any] = {"path": str(status_path), "loaded": status is not None, "error": status_error}
    if status is None:
        warnings.append(f"latest dashboard status is unavailable: {status_error}")
    else:
        version = str(status.get("dashboard_version", ""))
        persistence = status.get("bar_persistence_status_by_asset", {}) or {}
        status_summary.update(
            {
                "dashboard_version": version,
                "persistence_contract": status.get("bar_persistence_contract_version"),
                "bar_persistence_status_by_asset": persistence,
                "pending_bar_count_by_asset": status.get("pending_bar_count_by_asset", {}) or {},
                "asset_io_paused_by_asset": status.get("asset_io_paused_by_asset", {}) or {},
                "last_bar_write_error_by_asset": status.get("last_bar_write_error_by_asset", {}) or {},
            }
        )
        for token in REQUIRED_VERSION_TOKENS:
            if token not in version:
                failures.append(f"dashboard status version is missing required token: {token}")
        paused = [asset for asset, value in (status.get("asset_io_paused_by_asset", {}) or {}).items() if bool(value)]
        if paused:
            failures.append(f"asset I/O is currently paused for: {', '.join(sorted(paused))}")

    dashboard_text, dashboard_log_paths = _combined_log("portfolio_live_dashboard_orchestrator")
    dashboard_session = _latest_dashboard_session(dashboard_text)
    session_version_line = next(
        (line for line in dashboard_session.splitlines() if "V2 PORTFOLIO LIVE DASHBOARD ORCHESTRATOR version=" in line),
        "",
    )
    for token in REQUIRED_VERSION_TOKENS:
        if token not in session_version_line:
            failures.append(f"latest analyzed dashboard session is missing version token: {token}")

    counts = {
        "traceback": _count(dashboard_session, "Traceback (most recent call last):"),
        "permission_error": _count(dashboard_session, "PermissionError:"),
        "scoring_job_failed": _count(dashboard_session, "LIVE_SCORING_QUEUE_JOB_FAILED"),
        "native_event_failed": _count(dashboard_session, "LIVE_EVENT_ONLY_API_FAILED"),
        "runtime_state_discarded": _count(dashboard_session, "LIVE_RUNTIME_STATE_DISCARDED"),
        "drawdown_direct_full_authority": _count(dashboard_session, "LIVE_EVENT_FULL_AUTHORITY_DIRECT edge=BTCUSD/drawdownhazardcontinuation_short"),
        "drawdown_native_failure": sum(
            1
            for line in dashboard_session.splitlines()
            if "LIVE_EVENT_ONLY_API_FAILED" in line and "BTCUSD/drawdownhazardcontinuation_short" in line
        ),
        "parallel_edge_start": _count(dashboard_session, "LIVE_PARALLEL_EDGE_SCORING_START"),
        "parallel_asset_start": _count(dashboard_session, "LIVE_PARALLEL_ASSET_SCORING_START"),
        "bar_compaction_deferred": _count(dashboard_session, "LIVE_BAR_COMPACTION_DEFERRED"),
        "asset_io_deferred": _count(dashboard_session, "LIVE_BAR_ASSET_IO_DEFERRED"),
        "journal_read_failure": _count(dashboard_session, "LIVE_BAR_JOURNAL_READ_FAILED"),
    }
    if counts["traceback"]:
        failures.append(f"latest patched dashboard session contains {counts['traceback']} traceback(s)")
    if counts["scoring_job_failed"]:
        failures.append(f"latest patched dashboard session contains {counts['scoring_job_failed']} failed scoring job(s)")
    if counts["drawdown_native_failure"]:
        failures.append(
            f"drawdown edge attempted/failed native continuation {counts['drawdown_native_failure']} time(s); it must route directly to full authority"
        )
    if counts["journal_read_failure"]:
        failures.append(f"pending live-bar journal read failed {counts['journal_read_failure']} time(s)")
    if counts["drawdown_direct_full_authority"] == 0:
        warnings.append("no direct full-authority drawdown log was observed yet; a BTCUSD bar may not have been scored in this session")
    if counts["parallel_edge_start"] == 0:
        warnings.append("no parallel edge-scoring start was observed yet")
    if counts["parallel_asset_start"] == 0:
        warnings.append("no parallel asset-scoring start was observed; simultaneous asset jobs may not have occurred")
    if counts["bar_compaction_deferred"]:
        warnings.append(
            f"canonical live-bar CSV compaction was deferred {counts['bar_compaction_deferred']} time(s); this is safe while journals remain healthy"
        )
    if counts["asset_io_deferred"]:
        warnings.append(
            f"durable journal write failed and per-asset state was rolled back {counts['asset_io_deferred']} time(s); investigate disk/permissions"
        )

    per_asset: dict[str, list[float]] = {}
    for match in ASSET_BATCH_RE.finditer(dashboard_session):
        per_asset.setdefault(match.group("asset").upper(), []).append(float(match.group("elapsed")))
    latency = {asset: _summary_ms(values) for asset, values in sorted(per_asset.items())}
    all_asset_ms = [value for values in per_asset.values() for value in values]
    latency["ALL_ASSETS"] = _summary_ms(all_asset_ms)
    if all_asset_ms:
        median_ms = statistics.median(all_asset_ms)
        if median_ms > 90_000:
            warnings.append(f"median asset scoring remains high at {median_ms / 1000:.1f}s")
    else:
        warnings.append("no post-patch asset scoring batches were found; latency has not yet been measured")

    journal = _journal_health()
    if journal["malformed_files"]:
        failures.append(f"{len(journal['malformed_files'])} pending live-bar journal file(s) are malformed")
    if journal["total_pending"]:
        warnings.append(
            f"{journal['total_pending']} durable live-bar journal record(s) await canonical CSV compaction"
        )

    executor_text, executor_log_paths = _combined_log("portfolio_execution_bot")
    executor_counts = {
        "json_decode_errors": _count(executor_text, "JSONDecodeError"),
        "dashboard_status_read_failed": _count(executor_text, "dashboard_status_read_failed"),
        "bar_rows_ahead_of_ack": sum(
            1
            for line in executor_text.splitlines()
            if "VERTICAL_EXIT_DEFERRED" in line and "bar_rows_ahead_of_dashboard_ack" in line
        ),
        "loop_errors": _count(executor_text, "LOOP_ERROR"),
    }
    if executor_counts["json_decode_errors"] or executor_counts["dashboard_status_read_failed"]:
        warnings.append("executor still observed dashboard JSON read failures; inspect whether logs include pre-patch sessions")
    if executor_counts["bar_rows_ahead_of_ack"]:
        warnings.append(
            f"executor recorded {executor_counts['bar_rows_ahead_of_ack']} bar-ahead-of-ack deferral(s); bounded retry did not eliminate all races"
        )

    result_status = "FAIL" if failures else ("WARN" if warnings else "PASS")
    report = {
        "schema_version": "production_latency_io_runtime_validation_v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": result_status,
        "certification": cert_summary,
        "dashboard_status": status_summary,
        "dashboard_logs": dashboard_log_paths,
        "latest_dashboard_session_version_line": session_version_line,
        "dashboard_counts": counts,
        "latency": latency,
        "pending_live_bar_journal": journal,
        "executor_logs": executor_log_paths,
        "executor_counts": executor_counts,
        "failures": failures,
        "warnings": warnings,
        "orders_or_mt5_used": False,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Validation report: {REPORT_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
