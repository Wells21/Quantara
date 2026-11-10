"""Shared Step 06 runtime scoring readiness verifier.

SOT rule: Step 06 verifies runtime scoring readiness. It must never patch
runtime.py or write scoring logic into an edge package.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.edges.edge_layout import EdgeLayout, get_edge_layout, get_edge_layout_from_edge_root
from shared.scoring.edge_scorer import EdgeScorer, EdgeScoringNotReady
from shared.validation.edge_contracts import RuntimeContractError, validate_edge_contracts


class RuntimeScoringReadyError(RuntimeError):
    """Raised when Step 06 runtime scoring readiness fails."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def add_finding(findings: list[dict[str, Any]], severity: str, code: str, message: str, **extra: Any) -> None:
    row = {"severity": severity, "code": code, "message": message}
    row.update(extra)
    findings.append(row)


def verify_runtime_scoring_ready(layout: EdgeLayout) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    root = layout.v2_root

    try:
        contract_result = validate_edge_contracts(layout.edge_root, require_scoring_artifacts=False)
        add_finding(findings, "PASS", "EDGE_CONTRACTS_VALID", "Edge identity, runtime API, and fail-closed contracts are valid.", details=contract_result)
    except RuntimeContractError as exc:
        add_finding(findings, "FAIL", "EDGE_CONTRACTS_INVALID", str(exc))

    scorer = EdgeScorer.from_edge_root(layout.edge_root)
    try:
        artifact_result = scorer.validate_artifacts()
        add_finding(findings, "PASS", "SCORING_ARTIFACTS_READY", "Canonical scoring artifacts are present and internally consistent.", details=artifact_result)
    except EdgeScoringNotReady as exc:
        add_finding(findings, "FAIL", "SCORING_ARTIFACTS_NOT_READY", str(exc))
    except Exception as exc:
        add_finding(findings, "FAIL", "SCORING_ARTIFACT_CONTRACT_INVALID", str(exc))

    required_paths = {
        "selected_features": layout.selected_features_json,
        "model_input_schema": layout.model_input_schema_json,
        "model": layout.model_pkl,
        "calibrator": layout.calibrator_json,
        "selected_threshold": layout.selected_threshold_json,
        "runtime_py": layout.runtime_py,
    }
    for key, path in required_paths.items():
        if path.exists():
            add_finding(findings, "PASS", f"{key.upper()}_EXISTS", f"Found {key}: {rel(path, root)}")
        else:
            add_finding(findings, "FAIL", f"{key.upper()}_MISSING", f"Missing {key}: {rel(path, root)}")

    fails = [finding for finding in findings if finding["severity"] == "FAIL"]
    report = {
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "script_stage": "06_verify_runtime_scoring_ready",
        "verification_status": "FAIL" if fails else "PASS",
        "deployable": False,
        "runtime_patch_applied": False,
        "sot_rule": "Step 06 verifies runtime scoring readiness and never patches runtime.py.",
        "summary": {
            "pass_count": sum(1 for finding in findings if finding["severity"] == "PASS"),
            "fail_count": len(fails),
        },
        "findings": findings,
        "next_ordered_step_if_pass": "07_runtime_score_smoke_test.py",
        "next_ordered_step_if_fail": "fix failed contracts/artifacts before smoke test",
    }
    save_json(layout.runtime_scoring_ready_report_json, report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify SOT Step 06 runtime scoring readiness.")
    parser.add_argument("--asset")
    parser.add_argument("--family")
    parser.add_argument("--side", choices=["long", "short"])
    parser.add_argument("--edge-root")
    return parser


def _layout_from_args(args: argparse.Namespace) -> EdgeLayout:
    if args.edge_root:
        return get_edge_layout_from_edge_root(args.edge_root)
    if not (args.asset and args.family and args.side):
        raise RuntimeScoringReadyError("Provide either --edge-root or --asset --family --side.")
    return get_edge_layout(args.asset, args.family, args.side)


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    layout = _layout_from_args(args)
    print("=" * 80, flush=True)
    print(f"V2 STEP 06: VERIFY RUNTIME SCORING READY — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    report = verify_runtime_scoring_ready(layout)
    print(f"Status: {report['verification_status']}", flush=True)
    print(f"Pass: {report['summary']['pass_count']} | Fail: {report['summary']['fail_count']}", flush=True)
    print(f"Report: {layout.runtime_scoring_ready_report_json}", flush=True)
    if report["verification_status"] != "PASS":
        print("\nFailed findings:", flush=True)
        for finding in report["findings"]:
            if finding["severity"] == "FAIL":
                print(f"  - {finding['code']}: {finding['message']}", flush=True)
        raise SystemExit(1)
    print("✅ Runtime scoring readiness verified. No runtime patching was performed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeScoringReadyError as exc:
        raise SystemExit(f"❌ {exc}") from exc
