"""Generic SOTD Step 07 runtime score smoke test.

Step 07 proves that runtime.score(latest_only=True) can produce a live-safe
scored row through shared EdgeScorer. It is edge-agnostic and derives identity
from assets/<ASSET>/edges/<family>_<side>/.

Edge-local research/07_runtime_score_smoke_test.py files should be thin wrappers
around this module.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import numpy as np

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402
from shared.labels.leakage_columns import LEAKAGE_COLS, SCORE_OUTPUT_OVERLAP  # noqa: E402

REQUIRED_SCORE_COLUMNS = [
    "Datetime",
    "event_id",
    "edge_id",
    "side",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "selected_threshold",
    "approved_signal",
    "signal_id",
]
# Canonical leakage-column list (see shared/labels/leakage_columns.py); "label"
# and "target" are generic aliases kept in addition to the canonical set.
# SCORE_OUTPUT_OVERLAP is subtracted because a live score row legitimately
# carries some of those same column names as intended output, not leakage.
FORBIDDEN_LABEL_COLUMNS = (LEAKAGE_COLS | {"label", "target"}) - SCORE_OUTPUT_OVERLAP


class RuntimeScoreSmokeTestError(RuntimeError):
    """Raised when Step 07 fails."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def finite_float(value: Any) -> float:
    out = float(value)
    if not np.isfinite(out):
        raise RuntimeScoreSmokeTestError(f"Expected finite numeric value, got {value!r}")
    return out


def import_runtime(layout: EdgeLayout) -> ModuleType:
    runtime_path = layout.runtime_py
    if not runtime_path.exists():
        raise RuntimeScoreSmokeTestError(f"Missing runtime.py: {runtime_path}")
    edge_root_str = str(layout.edge_root)
    if edge_root_str not in sys.path:
        sys.path.insert(0, edge_root_str)
    spec = importlib.util.spec_from_file_location(f"v2_runtime_{layout.edge_stem}_step07", runtime_path)
    if spec is None or spec.loader is None:
        raise RuntimeScoreSmokeTestError(f"Could not import runtime.py: {runtime_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def add_finding(findings: list[dict[str, Any]], severity: str, code: str, message: str, **extra: Any) -> None:
    row = {"severity": severity, "code": code, "message": message}
    row.update(extra)
    findings.append(row)


def run_runtime_score_smoke_test(edge_root: str | Path, *, write_report: bool = True) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    runtime = import_runtime(layout)
    findings: list[dict[str, Any]] = []

    try:
        scored, manifest = runtime.score(latest_only=True)
    except Exception as exc:
        report = {
            "created_at": now_utc(),
            "edge_id": layout.edge_id,
            "script": "shared/edges/runtime_score_smoke_test.py",
            "smoke_test_status": "FAIL",
            "deployable": False,
            "next_ordered_step_if_pass": "08_probability_calibration_afml_replay_audit.py",
            "summary": {"pass_count": 0, "fail_count": 1, "rows_scored": 0, "approved_signals": None},
            "latest_signal": None,
            "runtime_manifest": None,
            "findings": [{"severity": "FAIL", "code": "RUNTIME_SCORE_FAILED_CLOSED", "message": str(exc)}],
        }
        if write_report:
            save_json(layout.runtime_score_smoke_test_report_json, report)
        return report

    missing = [col for col in REQUIRED_SCORE_COLUMNS if col not in scored.columns]
    if missing:
        add_finding(findings, "FAIL", "SCORE_COLUMNS_MISSING", f"Missing required score columns: {missing}")
    else:
        add_finding(findings, "PASS", "SCORE_COLUMNS_PRESENT", "All required score columns are present.")

    if len(scored) == 1:
        add_finding(findings, "PASS", "LATEST_ONLY_ONE_ROW", "latest_only=True returned exactly one scored row.")
    else:
        add_finding(findings, "FAIL", "LATEST_ONLY_ROW_COUNT_INVALID", f"Expected one row, got {len(scored)}.")

    forbidden_present = sorted(FORBIDDEN_LABEL_COLUMNS.intersection(scored.columns))
    if forbidden_present:
        add_finding(findings, "FAIL", "FORBIDDEN_LABEL_COLUMNS_PRESENT", f"Score output contains forbidden research columns: {forbidden_present}")
    else:
        add_finding(findings, "PASS", "NO_FORBIDDEN_LABEL_COLUMNS", "Score output contains no research label columns.")

    latest_signal: dict[str, Any] | None = None
    if not scored.empty and not missing:
        row = scored.iloc[-1].to_dict()
        raw_probability = finite_float(row["raw_probability"])
        calibrated_probability = finite_float(row["calibrated_probability"])
        afml_bet_size = finite_float(row["afml_bet_size"])
        selected_threshold = finite_float(row["selected_threshold"])
        approved_signal = bool(row["approved_signal"])
        expected_approval = calibrated_probability >= selected_threshold

        add_finding(findings, "PASS" if 0.0 <= raw_probability <= 1.0 else "FAIL", "RAW_PROBABILITY_RANGE", f"raw_probability={raw_probability}")
        add_finding(findings, "PASS" if 0.0 <= calibrated_probability <= 1.0 else "FAIL", "CALIBRATED_PROBABILITY_RANGE", f"calibrated_probability={calibrated_probability}")
        add_finding(findings, "PASS" if 0.0 <= afml_bet_size <= 1.0 else "FAIL", "AFML_BET_SIZE_RANGE", f"afml_bet_size={afml_bet_size}")
        add_finding(
            findings,
            "PASS" if approved_signal == expected_approval else "FAIL",
            "THRESHOLD_DECISION_MATCHES",
            "approved_signal matches calibrated_probability >= selected_threshold." if approved_signal == expected_approval else "approved_signal does not match calibrated threshold decision.",
        )
        latest_signal = {
            "Datetime": str(row.get("Datetime")),
            "event_id": row.get("event_id"),
            "signal_id": row.get("signal_id"),
            "side": int(row.get("side")) if row.get("side") is not None else None,
            "raw_probability": raw_probability,
            "calibrated_probability": calibrated_probability,
            "afml_bet_size": afml_bet_size,
            "selected_threshold": selected_threshold,
            "approved_signal": approved_signal,
        }

    fail_count = sum(1 for row in findings if row["severity"] == "FAIL")
    pass_count = sum(1 for row in findings if row["severity"] == "PASS")
    approved_signals = int(scored["approved_signal"].sum()) if "approved_signal" in scored.columns else None
    report = {
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "script": "shared/edges/runtime_score_smoke_test.py",
        "smoke_test_status": "FAIL" if fail_count else "PASS",
        "deployable": False,
        "next_ordered_step_if_pass": "08_probability_calibration_afml_replay_audit.py",
        "summary": {
            "pass_count": pass_count,
            "fail_count": fail_count,
            "rows_scored": int(len(scored)),
            "approved_signals": approved_signals,
        },
        "latest_signal": latest_signal,
        "runtime_manifest": manifest,
        "findings": findings,
    }
    if write_report:
        save_json(layout.runtime_score_smoke_test_report_json, report)
    return report


def print_summary(layout: EdgeLayout, report: Mapping[str, Any]) -> None:
    summary = report.get("summary", {})
    latest = report.get("latest_signal") or {}
    print("=" * 80, flush=True)
    print(f"V2 STEP 07: RUNTIME SCORE SMOKE TEST — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    print(f"Status: {report.get('smoke_test_status')}", flush=True)
    print(f"Rows scored: {summary.get('rows_scored')}", flush=True)
    print(f"Approved signals: {summary.get('approved_signals')}", flush=True)
    if latest:
        print("Latest signal:", flush=True)
        print(f"  Datetime: {latest.get('Datetime')}", flush=True)
        print(f"  raw_probability: {float(latest.get('raw_probability')):.8f}", flush=True)
        print(f"  calibrated_probability: {float(latest.get('calibrated_probability')):.8f}", flush=True)
        print(f"  afml_bet_size: {float(latest.get('afml_bet_size')):.8f}", flush=True)
        print(f"  selected_threshold: {float(latest.get('selected_threshold')):.8f}", flush=True)
        print(f"  approved_signal: {latest.get('approved_signal')}", flush=True)
    print(f"Report: {layout.runtime_score_smoke_test_report_json}", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic SOTD Step 07 runtime score smoke test.")
    parser.add_argument("--edge-root", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    layout = get_edge_layout_from_edge_root(args.edge_root)
    report = run_runtime_score_smoke_test(layout.edge_root, write_report=True)
    print_summary(layout, report)
    if report.get("smoke_test_status") != "PASS":
        failures = [row for row in report.get("findings", []) if row.get("severity") == "FAIL"]
        if failures:
            print("\nFailed findings:", flush=True)
            for row in failures:
                print(f"  - {row.get('code')}: {row.get('message')}", flush=True)
        raise SystemExit(1)
    print("✅ Runtime scoring smoke test passed. Edge is still non-deployable until replay audit passes.", flush=True)


if __name__ == "__main__":
    main()
