"""Post-install, non-trading health check for the rolling-window latency fix."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CERT = HERE / "native_one_row_certification.json"
ACK = HERE / "outputs" / "live_dashboard" / "latest_dashboard_bar_ack.json"
DASH_LOG = HERE / "live_logs" / "portfolio_live_dashboard_orchestrator.log"
EXEC_LOG = HERE / "live_logs" / "portfolio_execution_bot.log"
VERSION_TOKEN = "rolling_window_continuation_v1"


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload


def latest_run_segment(text: str, token: str) -> str:
    pos = text.rfind(token)
    if pos < 0:
        return ""
    line_start = text.rfind("\n", 0, pos)
    return text[max(0, line_start):]


def main() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    report: dict[str, Any] = {
        "schema_version": "rolling_window_latency_runtime_validation_v1",
        "orders_or_mt5_used": False,
    }

    try:
        cert = read_json(CERT)
    except Exception as exc:
        cert = {}
        failures.append(f"certification_unreadable:{type(exc).__name__}:{exc}")
    report["certification_schema"] = cert.get("schema_version")
    report["certification_status"] = cert.get("status")
    if cert.get("schema_version") != "native_one_row_certification_v5":
        failures.append("native_one_row_certification_v5_required")
    if cert.get("status") != "PASS":
        failures.append("native_one_row_certification_not_pass")

    assets = cert.get("assets") or {}
    missing_asset_proofs = sorted(
        asset for asset, row in assets.items()
        if not isinstance(row, dict)
        or row.get("rolling_window_feature_parity_passed") is not True
    )
    native_edges = {
        edge_id: row for edge_id, row in (cert.get("edges") or {}).items()
        if isinstance(row, dict)
        and row.get("event_execution_mode") == "native_one_row_certified"
    }
    missing_event_proofs = sorted(
        edge_id for edge_id, row in native_edges.items()
        if row.get("rolling_window_event_parity_passed") is not True
    )
    report["missing_asset_rolling_proofs"] = missing_asset_proofs
    report["missing_native_event_rolling_proofs"] = missing_event_proofs
    if missing_asset_proofs:
        failures.append("asset_rolling_window_proof_missing")
    if missing_event_proofs:
        failures.append("native_event_rolling_window_proof_missing")

    try:
        ack = read_json(ACK)
    except Exception as exc:
        ack = {}
        failures.append(f"dashboard_bar_ack_unreadable:{type(exc).__name__}:{exc}")
    report["bar_ack_schema"] = ack.get("schema_version")
    report["bar_ack_updated_at"] = ack.get("updated_at")
    if ack.get("schema_version") != "v2_dashboard_bar_ack_v1":
        failures.append("dashboard_bar_ack_v1_required")
    if ack and (
        ack.get("live_policy_ready") is not True
        or str(ack.get("policy_gate_status") or "").upper() != "POLICY_PASS"
    ):
        failures.append("dashboard_bar_ack_policy_not_ready")

    dashboard_segment = ""
    if DASH_LOG.exists():
        dashboard_segment = latest_run_segment(
            DASH_LOG.read_text(encoding="utf-8", errors="replace"),
            VERSION_TOKEN,
        )
    if not dashboard_segment:
        warnings.append("new_dashboard_run_not_found")

    counters = {
        "shared_feature_accept": dashboard_segment.count("LIVE_STATEFUL_SHARED_FEATURES_ACCEPT"),
        "shared_feature_reject": dashboard_segment.count("LIVE_STATEFUL_SHARED_FEATURES_REJECT"),
        "old_nonappend_false_positive": dashboard_segment.count(
            "runtime_stateful_update_attempted_nonappend_historical_insertion"
        ),
        "native_event_used": dashboard_segment.count("LIVE_EVENT_ONLY_API_USED"),
        "native_event_failed": dashboard_segment.count("LIVE_EVENT_ONLY_API_FAILED"),
        "runtime_state_discarded": dashboard_segment.count("LIVE_RUNTIME_STATE_DISCARDED"),
        "scoring_jobs_failed": dashboard_segment.count("LIVE_SCORING_QUEUE_JOB_FAILED"),
    }
    elapsed = [
        float(value) / 1000.0
        for value in re.findall(r"LIVE_SCORING_QUEUE_JOB_DONE .*?elapsed_ms=([0-9.]+)", dashboard_segment)
    ]
    report["runtime_counters"] = counters
    report["scoring_jobs"] = {
        "count": len(elapsed),
        "median_seconds": statistics.median(elapsed) if elapsed else None,
        "max_seconds": max(elapsed) if elapsed else None,
    }
    if counters["old_nonappend_false_positive"]:
        failures.append("rolling_window_still_misclassified_as_historical_insertion")
    if counters["shared_feature_reject"]:
        failures.append("shared_feature_continuation_rejected")
    if counters["native_event_failed"]:
        failures.append("native_event_continuation_failed")
    if counters["scoring_jobs_failed"]:
        failures.append("scoring_job_failed")
    if elapsed and statistics.median(elapsed) > 60:
        warnings.append("median_scoring_job_still_above_60_seconds")
    if not elapsed:
        warnings.append("no_completed_scoring_job_in_new_run_yet")

    executor_segment = ""
    if EXEC_LOG.exists() and ack.get("updated_at"):
        # Conservative: inspect the current executor log for handoff errors.
        executor_segment = EXEC_LOG.read_text(encoding="utf-8", errors="replace")
    executor_errors = {
        "dashboard_status_json_decode_errors": executor_segment.count(
            "dashboard_status_read_failed:JSONDecodeError"
        ),
        "dashboard_ack_read_failures": executor_segment.count(
            "dashboard_ack_read_failed:"
        ),
    }
    report["executor_handoff_errors_whole_log"] = executor_errors
    # Old status errors may remain in a cumulative log, so report rather than
    # fail unless the new ack itself is unreadable.

    report["warnings"] = warnings
    report["failures"] = failures
    report["status"] = "PASS" if not failures else "FAIL"
    print(json.dumps(report, indent=2, default=str))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
