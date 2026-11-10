# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
"""Generic Step 09 edge manifest exporter.

SOTD purpose
------------
Step 09 must not be copied and hard-coded for every edge. This shared module
exports the allocator-facing edge_manifest.json for any side-specialized edge
whose Step 05, Step 07, and Step 08 gates have passed.

The edge-local research/09_export_edge_manifest.py scripts should be thin
wrappers around this module so future assets/edges can be added without manual
manifest-export rewrites.
"""

from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A1_STEP01_PROVENANCE_V1

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.edge_yaml_contract import validate_edge_yaml_file  # noqa: E402
from shared.io.artifact_hash import hash_artifacts  # noqa: E402
from shared.validation.v3_semantic_contract import validate_research_generation_chain  # noqa: E402

SCHEMA_VERSION = "v2.1"
DEPLOYMENT_STATUS_PASS = "PASS"
RISK_CONTRACT_VERSION = "direct_weight_calibrated_afml_v2"
PROBABILITY_FLOW = "raw_probability -> sigmoid calibrated_probability -> AFML bet size -> calibrated-threshold decision"


class EdgeManifestExportError(RuntimeError):
    """Raised when Step 09 must fail closed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EdgeManifestExportError(f"Missing JSON artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeManifestExportError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EdgeManifestExportError(f"JSON artifact must be an object: {path}")
    return payload


def save_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def require_file(path: Path, layout: EdgeLayout) -> None:
    if not path.exists():
        raise EdgeManifestExportError(f"Missing required artifact before edge manifest export: {rel(path, layout.v2_root)}")
    if not path.is_file():
        raise EdgeManifestExportError(f"Required artifact is not a file: {rel(path, layout.v2_root)}")


def require_report_status(path: Path, status_key: str, expected: str = DEPLOYMENT_STATUS_PASS) -> dict[str, Any]:
    report = load_json(path)
    actual = report.get(status_key)
    if actual != expected:
        raise EdgeManifestExportError(f"{path} expected {status_key}={expected!r}, got {actual!r}")
    fail_count = int(report.get("summary", {}).get("fail_count", 0))
    if fail_count != 0:
        raise EdgeManifestExportError(f"{path} has non-zero fail_count={fail_count}")
    return report


TASK2A_MIN_COVERAGE_RATE = 0.99


def require_task2a_execution(path: Path) -> dict[str, Any]:
    """Task 2A (shared/execution/executable_target_dataset.py) is audit-only
    and reports coverage_rate/execution_status_counts rather than a PASS/FAIL
    status field, unlike the retired Step03A manifest. Gate on coverage_rate
    instead, matching the >=0.99 convention already used to rank finalists in
    shared/research/v3_horizon_lab.py."""
    manifest = load_json(path)
    coverage = manifest.get("coverage_rate")
    try:
        coverage_value = float(coverage)
    except (TypeError, ValueError):
        raise EdgeManifestExportError(f"{path} missing/invalid coverage_rate: {coverage!r}")
    if coverage_value < TASK2A_MIN_COVERAGE_RATE:
        raise EdgeManifestExportError(
            f"{path} coverage_rate={coverage_value!r} is below required {TASK2A_MIN_COVERAGE_RATE}"
        )
    return manifest


def required_artifacts(layout: EdgeLayout) -> list[Path]:
    return [
        layout.selected_features_json,
        layout.feature_surgery_manifest_json,
        layout.model_input_schema_json,
        layout.feature_schema_json,
        layout.bar_contract_json,
        layout.event_schema_json,
        layout.label_schema_json,
        layout.calibration_schema_json,
        layout.live_runtime_contract_json,
        layout.edge_yaml,
        layout.runtime_py,
        layout.model_pkl,
        layout.model_best_params_json,
        layout.model_training_manifest_json,
        layout.oof_manifest_json,
        layout.oof_dir / f"{layout.edge_stem}_oof_raw_threshold_sweep_manifest.json",
        layout.resolution_csv,
        layout.exact_target_manifest_json,
        layout.calibrator_json,
        layout.calibrator_pkl,
        layout.calibration_report_json,
        layout.calibration_manifest_json,
        layout.calibrated_oof_predictions_csv,
        layout.selected_threshold_json,
        layout.threshold_manifest_json,
        layout.calibrated_threshold_sweep_csv,
        layout.training_artifact_verification_report_json,
        layout.runtime_score_smoke_test_report_json,
        layout.replay_audit_report_json,
    ]


def selected_features_payload(layout: EdgeLayout) -> tuple[dict[str, Any], list[str]]:
    payload = load_json(layout.selected_features_json)
    if payload.get("edge_id") not in {None, layout.edge_id}:
        raise EdgeManifestExportError(
            f"selected_features edge_id mismatch: expected {layout.edge_id}, got {payload.get('edge_id')!r}"
        )
    features = payload.get("selected_features")
    if not isinstance(features, list) or not features:
        raise EdgeManifestExportError("selected_features.json must contain a non-empty selected_features list.")
    if payload.get("feature_order_locked") is not True:
        raise EdgeManifestExportError("selected_features.json must set feature_order_locked=true.")
    return payload, [str(feature) for feature in features]


def threshold_payload(layout: EdgeLayout) -> tuple[dict[str, Any], dict[str, Any], float]:
    payload = load_json(layout.selected_threshold_json)
    if payload.get("edge_id") not in {None, layout.edge_id}:
        raise EdgeManifestExportError(
            f"threshold edge_id mismatch: expected {layout.edge_id}, got {payload.get('edge_id')!r}"
        )
    if payload.get("threshold_probability_column") != "calibrated_probability":
        raise EdgeManifestExportError("Selected threshold artifact must use calibrated_probability.")
    selected = payload.get("selected")
    if not isinstance(selected, dict):
        selected = {}
    value = selected.get("selected_threshold", selected.get("threshold", payload.get("selected_threshold", payload.get("threshold"))))
    if value is None:
        raise EdgeManifestExportError("Selected threshold artifact must contain selected_threshold/threshold.")
    threshold = float(value)
    if not 0.0 < threshold < 1.0:
        raise EdgeManifestExportError(f"Selected threshold must be in (0,1), got {threshold!r}")
    return payload, selected, threshold


def _report_metric(report: Mapping[str, Any], *names: str) -> Any:
    metrics = report.get("metrics", {})
    if isinstance(metrics, Mapping):
        for name in names:
            if name in metrics:
                return metrics[name]
    for name in names:
        if name in report:
            return report[name]
    return None


def _approved_rate(report: Mapping[str, Any]) -> Any:
    value = _report_metric(report, "approved_rate", "approved_rate_at_edge_default_threshold")
    if value is not None:
        return value
    approved = _report_metric(report, "approved_signals", "approved_signals_at_edge_default_threshold")
    rows = _report_metric(report, "runtime_rows_scored")
    try:
        return float(approved) / float(rows) if rows else None
    except Exception:
        return None


def build_edge_manifest(edge_root: str | Path) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    validate_edge_yaml_file(layout.edge_root)
    semantic_chain = validate_research_generation_chain(layout)

    artifacts = required_artifacts(layout)
    for artifact in artifacts:
        require_file(artifact, layout)

    training_report = require_report_status(layout.training_artifact_verification_report_json, "verification_status")
    smoke_report = require_report_status(layout.runtime_score_smoke_test_report_json, "smoke_test_status")
    replay_report = require_report_status(layout.replay_audit_report_json, "replay_audit_status")
    execution_report = require_task2a_execution(layout.exact_target_manifest_json)

    selected_payload, features = selected_features_payload(layout)
    threshold_full, threshold_selected, edge_default_threshold = threshold_payload(layout)
    calibration_report = load_json(layout.calibration_report_json)
    calibrator_json = load_json(layout.calibrator_json)
    model_training = load_json(layout.model_training_manifest_json)
    oof_manifest = load_json(layout.oof_manifest_json)

    artifact_hashes = hash_artifacts(artifacts, root=layout.v2_root)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "edge_manifest_type": "side_specialized_alpha_edge_manifest",
        "deployment_status": DEPLOYMENT_STATUS_PASS,
        "deployable": True,
        "identity": {
            "edge_id": layout.edge_id,
            "asset": layout.asset,
            "family": layout.family,
            "side_name": layout.side_name,
            "side_value": layout.side_value,
            "edge_root": rel(layout.edge_root, layout.v2_root),
        },
        "contracts": {
            "schema_version": SCHEMA_VERSION,
            "risk_contract_version": RISK_CONTRACT_VERSION,
            "bar_mode": "standard_quote_tick",
            "threshold_unit": "ticks",
            "proxy_value_meaning": "tick_count_backward_compatibility_only",
            "probability_flow": PROBABILITY_FLOW,
            "gross_label_contract_version": semantic_chain["gross_label_contract_version"],
            "execution_contract_version": semantic_chain["execution_contract_version"],
            "executed_target_contract_version": semantic_chain["executed_target_contract_version"],
            "payoff_profile_version": semantic_chain["payoff_profile_version"],
            "payoff_context_version": semantic_chain["payoff_context_version"],
            "afml_sizing_method": semantic_chain["afml_sizing_method"],
            "threshold_probability_column": "calibrated_probability",
            "threshold_contract": {
                "edge_manifest_threshold_is_final_live_threshold": False,
                "edge_manifest_threshold_role": "edge_default_calibrated_candidate_threshold",
                "allocator_selects_final_representative_threshold": True,
                "live_manifest_must_contain_final_threshold": True,
            },
            "dashboard_may_engineer_edge_features": False,
            "runtime_owns_feature_engineering": True,
            "allocator_may_consume_only_if_deployment_status_pass": True,
        },
        "runtime": {
            "runtime_module": rel(layout.runtime_py, layout.v2_root),
            "required_functions": [
                "load_contract",
                "validate_environment",
                "build_features",
                "generate_candidate_events",
                "build_model_rows",
                "load_model",
                "load_calibrator",
                "score",
                "explain_missing_features",
            ],
            "scoring_status": "SCORING_IMPLEMENTED_REPLAY_AUDIT_PASSED",
            "live_scoring_must_not_call_build_model_rows": True,
        },
        "artifacts": {
            "selected_features": rel(layout.selected_features_json, layout.v2_root),
            "model_input_schema": rel(layout.model_input_schema_json, layout.v2_root),
            "feature_schema": rel(layout.feature_schema_json, layout.v2_root),
            "model": rel(layout.model_pkl, layout.v2_root),
            "calibrator_json": rel(layout.calibrator_json, layout.v2_root),
            "calibrator_pkl": rel(layout.calibrator_pkl, layout.v2_root),
            "calibrated_oof_predictions": rel(layout.calibrated_oof_predictions_csv, layout.v2_root),
            "task2a_execution_resolution": rel(layout.resolution_csv, layout.v2_root),
            "task2a_execution_manifest": rel(layout.exact_target_manifest_json, layout.v2_root),
            "edge_default_threshold_artifact": rel(layout.selected_threshold_json, layout.v2_root),
            "threshold_sweep": rel(layout.calibrated_threshold_sweep_csv, layout.v2_root),
            "training_verification_report": rel(layout.training_artifact_verification_report_json, layout.v2_root),
            "runtime_score_smoke_test_report": rel(layout.runtime_score_smoke_test_report_json, layout.v2_root),
            "replay_audit_report": rel(layout.replay_audit_report_json, layout.v2_root),
        },
        "selected_features": {
            "selected_feature_count": len(features),
            "feature_order_locked": True,
            "source_artifact": selected_payload.get("source_artifact"),
            "features": features,
        },
        "model": {
            "model_type": model_training.get("model_type"),
            "model_output_type": model_training.get("model_output_type"),
            "rows_used": model_training.get("rows_used"),
            "date_start": model_training.get("date_start"),
            "date_end": model_training.get("date_end"),
            "target": model_training.get("target"),
            "target_mean": model_training.get("target_mean"),
            "best_cpcv_f1": model_training.get("scoring", {}).get("best_cpcv_f1") if isinstance(model_training.get("scoring"), Mapping) else None,
            "oof_metrics": oof_manifest.get("oof_metrics", {}),
        },
        "calibration": {
            "method": calibrator_json.get("method", calibration_report.get("method")),
            "calibration_report": rel(layout.calibration_report_json, layout.v2_root),
            "summary": calibration_report.get("summary", {}),
            "live_sigmoid_calibrator": calibration_report.get("live_sigmoid_calibrator", calibrator_json),
        },
        "threshold": {
            "threshold_probability_column": threshold_full.get("threshold_probability_column"),
            "edge_default_calibrated_threshold": edge_default_threshold,
            "edge_default_threshold_source": "Step 04 calibrated OOF threshold sweep; used for edge replay/smoke testing and allocator candidate-bank ingestion, not final portfolio/live threshold selection.",
            "allocator_must_select_or_confirm_final_threshold": True,
            "allocator_threshold_override_allowed": True,
            "final_live_threshold_source": "V2 allocator representative-threshold + overlap-aware simulation live manifest",
            "selected_threshold_metrics": threshold_selected,
            "payoff_context_id": semantic_chain["payoff_context_id"],
            "payoff_context": semantic_chain["payoff_context"],
        },
        "live_compatibility": {
            "deployment_status": DEPLOYMENT_STATUS_PASS,
            "training_artifact_verification_status": training_report.get("verification_status"),
            "runtime_score_smoke_test_status": smoke_report.get("smoke_test_status"),
            "replay_audit_status": replay_report.get("replay_audit_status"),
            "task2a_execution_coverage_rate": execution_report.get("coverage_rate"),
            "task2a_execution_contract_version": execution_report.get("execution_contract_version"),
            "selected_row_replay_passed": True,
            "raw_probability_replay_passed": True,
            "calibrated_probability_replay_passed": True,
            "afml_bet_size_replay_passed": True,
            "threshold_decision_replay_passed": True,
            "dashboard_side_feature_engineering_allowed": False,
            "notes": [
                "Runtime score output contains no research label columns.",
                "OOF probabilities are not compared to final-model runtime probabilities because OOF uses fold models.",
                "Extra runtime rows after research label horizon are acceptable for live scoring.",
                "Edge-level threshold is not final portfolio/live threshold; allocator/live manifest must select or confirm final representative threshold.",
            ],
        },
        "metrics": {
            "runtime_rows_scored": _report_metric(replay_report, "runtime_rows_scored"),
            "research_model_rows": _report_metric(replay_report, "research_model_rows"),
            "overlap_rows": _report_metric(replay_report, "overlap_rows", "research_overlap_rows"),
            "approved_signals_at_edge_default_threshold": _report_metric(replay_report, "approved_signals", "approved_signals_at_edge_default_threshold"),
            "approved_rate_at_edge_default_threshold": _approved_rate(replay_report),
            "raw_replay_max_abs_diff": _report_metric(replay_report, "raw_replay_max_abs_diff", "raw_replay_max_diff"),
            "calibrated_replay_max_abs_diff": _report_metric(replay_report, "calibrated_replay_max_abs_diff", "calibrated_replay_max_diff"),
            "afml_replay_max_abs_diff": _report_metric(replay_report, "afml_replay_max_abs_diff", "afml_replay_max_diff"),
            "threshold_decision_mismatches": _report_metric(replay_report, "threshold_decision_mismatches", "threshold_mismatches"),
        },
        "semantic_generation": semantic_chain,
        "artifact_hashes": artifact_hashes,
        "warnings": [
            {
                "code": "XGBOOST_PICKLE_FORMAT_WARNING",
                "severity": "WARN",
                "message": "Runtime model currently loads from pickle/joblib. Future hardening should export an XGBoost native model artifact with Booster.save_model.",
            },
            {
                "code": "EDGE_THRESHOLD_NOT_FINAL_LIVE_THRESHOLD",
                "severity": "WARN",
                "message": "The edge default calibrated threshold is for replay/candidate-bank ingestion only. Final live threshold must come from allocator representative-threshold and overlap-aware simulation pipeline.",
            },
        ],
        "next_ordered_step": "build_v2_allocator_candidate_bank_from_deployability_pass_edge_manifests",
    }
    return manifest


def export_edge_manifest(edge_root: str | Path, *, write: bool = True) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    manifest = build_edge_manifest(layout.edge_root)
    if write:
        save_json(layout.edge_manifest_json, manifest)
    return manifest


def print_manifest_summary(layout: EdgeLayout, manifest: Mapping[str, Any]) -> None:
    metrics = manifest.get("metrics", {}) if isinstance(manifest.get("metrics"), Mapping) else {}
    threshold = manifest.get("threshold", {}) if isinstance(manifest.get("threshold"), Mapping) else {}
    selected = manifest.get("selected_features", {}) if isinstance(manifest.get("selected_features"), Mapping) else {}
    approved_rate = metrics.get("approved_rate_at_edge_default_threshold")
    print("Status: PASS", flush=True)
    print(f"Deployment status: {manifest.get('deployment_status')}", flush=True)
    print(f"Deployable: {manifest.get('deployable')}", flush=True)
    print(f"Selected features: {selected.get('selected_feature_count')}", flush=True)
    print(f"Edge default calibrated threshold: {threshold.get('edge_default_calibrated_threshold')}", flush=True)
    print("Final live threshold source: allocator/live manifest", flush=True)
    print(f"Runtime rows scored: {metrics.get('runtime_rows_scored')}", flush=True)
    if approved_rate is None:
        print("Approved rate at edge default threshold: unavailable", flush=True)
    else:
        print(f"Approved rate at edge default threshold: {float(approved_rate):.2%}", flush=True)
    print(f"Manifest: {layout.edge_manifest_json}", flush=True)
    print("✅ Edge manifest exported with deployment_status=PASS. Run promote_edge_status.py next if registry/edge.yaml are not already deployability_pass.", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic SOTD Step 09 edge manifest exporter.")
    parser.add_argument("--edge-root", required=True, help="Path to assets/<ASSET>/edges/<family>_<side>.")
    parser.add_argument("--dry-run", action="store_true", help="Build and validate manifest but do not write edge_manifest.json.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    layout = get_edge_layout_from_edge_root(args.edge_root)
    print("=" * 80, flush=True)
    print(f"V2 STEP 09: EXPORT EDGE MANIFEST — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    manifest = export_edge_manifest(layout.edge_root, write=not args.dry_run)
    print_manifest_summary(layout, manifest)
    if args.dry_run:
        print("Dry-run only. No edge_manifest.json was written.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except EdgeManifestExportError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
