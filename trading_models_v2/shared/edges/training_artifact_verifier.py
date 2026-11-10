"""Generic SOTD Step 05 training artifact verifier.

Step 05 verifies canonical training/scoring artifacts before runtime scoring and
replay audit. It is edge-agnostic and derives all paths from the edge folder.

Edge-local research/05_training_artifact_verifier.py files should be thin wrappers
around this module.
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

from shared.calibration.sigmoid_afml import CALIBRATED_PROBABILITY_COL, validate_threshold_payload  # noqa: E402
from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.edge_yaml_contract import validate_edge_yaml_file  # noqa: E402
from shared.labels.leakage_columns import LEAKAGE_COLS  # noqa: E402
from shared.validation.feature_schema import validate_selected_features  # noqa: E402
from shared.validation.v3_semantic_contract import validate_research_generation_chain  # noqa: E402


class TrainingArtifactVerifierError(RuntimeError):
    """Raised when Step 05 fails."""


# Canonical leakage-column list (see shared/labels/leakage_columns.py); "label"
# and "target" are generic aliases kept in addition to the canonical set.
FORBIDDEN_SELECTED_FEATURES = LEAKAGE_COLS | {"label", "target"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def load_json_any(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_object(path: Path) -> dict[str, Any]:
    payload = load_json_any(path)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return payload


def save_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def add_finding(findings: list[dict[str, Any]], severity: str, code: str, message: str, **extra: Any) -> None:
    row = {"severity": severity, "code": code, "message": message}
    row.update(extra)
    findings.append(row)


def require_file(layout: EdgeLayout, findings: list[dict[str, Any]], path: Path, code: str) -> bool:
    if path.exists() and path.is_file():
        add_finding(findings, "PASS", code, f"Found required artifact: {rel(path, layout.v2_root)}")
        return True
    add_finding(findings, "FAIL", code, f"Missing required artifact: {rel(path, layout.v2_root)}")
    return False


def normalize_selected_features(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        features = payload.get("selected_features")
    else:
        features = payload
    if not isinstance(features, list):
        raise TrainingArtifactVerifierError("selected_features payload must contain a selected_features list or be a list.")
    if not features:
        raise TrainingArtifactVerifierError("selected_features list must not be empty.")
    return [str(feature) for feature in features]


def _manifest_feature_list(payload: Mapping[str, Any]) -> list[str] | None:
    for key in ("selected_features", "model_input_columns", "features"):
        value = payload.get(key)
        if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
            return [str(item) for item in value]
    return None


def verify_training_artifacts(edge_root: str | Path, *, write_report: bool = True) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    findings: list[dict[str, Any]] = []

    try:
        validate_edge_yaml_file(layout.edge_root)
        add_finding(findings, "PASS", "EDGE_YAML_CONTRACT_PASS", "edge.yaml full SOTD contract passed.")
    except Exception as exc:
        add_finding(findings, "FAIL", "EDGE_YAML_CONTRACT_FAIL", str(exc))

    raw_sweep_manifest = layout.oof_dir / f"{layout.edge_stem}_oof_raw_threshold_sweep_manifest.json"
    required_files = [
        (layout.selected_features_json, "CANONICAL_SELECTED_FEATURES_EXISTS"),
        (layout.feature_surgery_selected_features_json, "FEATURE_SURGERY_SELECTED_EXISTS"),
        (layout.feature_surgery_manifest_json, "FEATURE_SURGERY_MANIFEST_EXISTS"),
        (layout.feature_schema_json, "FEATURE_SCHEMA_EXISTS"),
        (layout.model_input_schema_json, "MODEL_INPUT_SCHEMA_EXISTS"),
        (layout.model_training_manifest_json, "MODEL_TRAINING_MANIFEST_EXISTS"),
        (layout.model_best_params_json, "MODEL_BEST_PARAMS_EXISTS"),
        (layout.model_pkl, "MODEL_PKL_EXISTS"),
        (layout.oof_manifest_json, "OOF_MANIFEST_EXISTS"),
        (raw_sweep_manifest, "RAW_SWEEP_MANIFEST_EXISTS"),
        (layout.calibration_report_json, "CALIBRATION_REPORT_EXISTS"),
        (layout.calibrator_json, "CALIBRATOR_JSON_EXISTS"),
        (layout.calibrator_pkl, "CALIBRATOR_PKL_EXISTS"),
        (layout.calibrated_oof_predictions_csv, "CALIBRATED_OOF_EXISTS"),
        (layout.calibration_manifest_json, "CALIBRATION_MANIFEST_EXISTS"),
        (layout.selected_threshold_json, "SELECTED_THRESHOLD_EXISTS"),
        (layout.calibrated_threshold_sweep_csv, "THRESHOLD_SWEEP_EXISTS"),
        (layout.threshold_manifest_json, "THRESHOLD_MANIFEST_EXISTS"),
    ]
    all_required_present = True
    for path, code in required_files:
        all_required_present = require_file(layout, findings, path, code) and all_required_present

    if all_required_present:
        try:
            try:
                semantic_chain = validate_research_generation_chain(layout)
                add_finding(
                    findings, "PASS", "V3_PHASE3A_SEMANTIC_GENERATION_CHAIN_PASS",
                    "Gross-label -> OOF -> exact execution -> executable calibration -> threshold/payoff context chain is current.",
                    payoff_context_id=semantic_chain.get("payoff_context_id"),
                )
            except Exception as exc:
                add_finding(findings, "FAIL", "V3_PHASE3A_SEMANTIC_GENERATION_CHAIN_FAIL", str(exc))

            selected_payload = load_json_object(layout.selected_features_json)
            surgery_payload = load_json_any(layout.feature_surgery_selected_features_json)
            model_input_payload = load_json_object(layout.model_input_schema_json)
            feature_schema_payload = load_json_object(layout.feature_schema_json)
            model_manifest = load_json_object(layout.model_training_manifest_json)
            oof_manifest = load_json_object(layout.oof_manifest_json)
            calibration_report = load_json_object(layout.calibration_report_json)
            calibrator_json = load_json_object(layout.calibrator_json)
            threshold_payload = load_json_object(layout.selected_threshold_json)

            selected = normalize_selected_features(selected_payload)
            surgery_selected = normalize_selected_features(surgery_payload)
            model_columns = [str(item) for item in model_input_payload.get("model_input_columns", [])]

            if selected_payload.get("feature_order_locked") is True:
                add_finding(findings, "PASS", "FEATURE_ORDER_LOCKED", "Canonical selected-feature order is locked.")
            else:
                add_finding(findings, "FAIL", "FEATURE_ORDER_NOT_LOCKED", "selected_features.json must set feature_order_locked=true.")

            if model_input_payload.get("model_input_order_locked") is True:
                add_finding(findings, "PASS", "MODEL_INPUT_ORDER_LOCKED", "model_input_schema order is locked.")
            else:
                add_finding(findings, "FAIL", "MODEL_INPUT_ORDER_NOT_LOCKED", "model_input_schema.json must set model_input_order_locked=true.")

            if selected and selected == surgery_selected:
                add_finding(findings, "PASS", "FEATURE_SURGERY_MATCHES_CANONICAL", "Step 01 selected features match canonical selected features exactly.")
            else:
                add_finding(findings, "FAIL", "FEATURE_SURGERY_CANONICAL_MISMATCH", "Step 01 selected features do not match canonical selected features.")

            if selected and selected == model_columns:
                add_finding(findings, "PASS", "MODEL_INPUT_SCHEMA_MATCHES_CANONICAL", "model_input_schema matches canonical selected features exactly and in order.")
            else:
                add_finding(findings, "FAIL", "MODEL_INPUT_SCHEMA_CANONICAL_MISMATCH", "model_input_schema does not match selected feature order.")

            try:
                validate_selected_features(selected, feature_schema_payload, expected_edge_id=layout.edge_id)
                add_finding(findings, "PASS", "FEATURE_SCHEMA_SELECTED_FEATURES_SAFE", "Selected features are schema-declared and live-safe.")
            except Exception as exc:
                add_finding(findings, "FAIL", "FEATURE_SCHEMA_SELECTED_FEATURES_INVALID", str(exc))

            forbidden = sorted(set(selected).intersection(FORBIDDEN_SELECTED_FEATURES))
            if forbidden:
                add_finding(findings, "FAIL", "FORBIDDEN_SELECTED_FEATURES_PRESENT", "Selected features contain forbidden label/probability/risk fields.", features=forbidden)
            else:
                add_finding(findings, "PASS", "NO_FORBIDDEN_SELECTED_FEATURES", "Selected features contain no forbidden label/probability/risk fields.")

            for name, payload in (("model_training_manifest", model_manifest), ("oof_manifest", oof_manifest)):
                manifest_features = _manifest_feature_list(payload)
                if manifest_features is None:
                    add_finding(findings, "WARN", f"{name.upper()}_FEATURE_LIST_MISSING", f"{name} does not expose a comparable feature list.")
                elif manifest_features == selected:
                    add_finding(findings, "PASS", f"{name.upper()}_FEATURES_MATCH", f"{name} feature list matches canonical selected features.")
                else:
                    add_finding(findings, "FAIL", f"{name.upper()}_FEATURES_MISMATCH", f"{name} feature list does not match canonical selected features.")

            if calibrator_json.get("output_column", CALIBRATED_PROBABILITY_COL) == CALIBRATED_PROBABILITY_COL:
                add_finding(findings, "PASS", "CALIBRATOR_OUTPUT_COLUMN_OK", "Calibrator outputs calibrated_probability.")
            else:
                add_finding(findings, "FAIL", "CALIBRATOR_OUTPUT_COLUMN_INVALID", "Calibrator output_column must be calibrated_probability.")

            try:
                threshold = validate_threshold_payload(threshold_payload)
                add_finding(findings, "PASS", "SELECTED_THRESHOLD_CONTRACT_OK", f"Selected threshold uses calibrated_probability: {threshold:.6f}")
            except Exception as exc:
                add_finding(findings, "FAIL", "SELECTED_THRESHOLD_CONTRACT_INVALID", str(exc))

            if calibration_report.get("probability_flow"):
                add_finding(findings, "PASS", "CALIBRATION_REPORT_PROBABILITY_FLOW_PRESENT", "Calibration report records probability flow.")
            else:
                add_finding(findings, "WARN", "CALIBRATION_REPORT_PROBABILITY_FLOW_MISSING", "Calibration report does not record probability flow.")
        except Exception as exc:
            add_finding(findings, "FAIL", "ARTIFACT_CONTENT_VALIDATION_EXCEPTION", str(exc))

    fail_count = sum(1 for row in findings if row["severity"] == "FAIL")
    warn_count = sum(1 for row in findings if row["severity"] == "WARN")
    pass_count = sum(1 for row in findings if row["severity"] == "PASS")
    report = {
        "created_at": now_utc(),
        "edge_id": layout.edge_id,
        "script": "shared/edges/training_artifact_verifier.py",
        "verification_status": "FAIL" if fail_count else "PASS",
        "deployable": False,
        "next_ordered_step_if_pass": "06_verify_runtime_scoring_ready.py",
        "summary": {"pass_count": pass_count, "warn_count": warn_count, "fail_count": fail_count},
        "findings": findings,
    }
    if write_report:
        save_json(layout.training_artifact_verification_report_json, report)
    return report


def print_summary(layout: EdgeLayout, report: Mapping[str, Any]) -> None:
    summary = report.get("summary", {})
    print("=" * 80, flush=True)
    print(f"V2 STEP 05: TRAINING ARTIFACT VERIFIER — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    print(f"Status: {report.get('verification_status')}", flush=True)
    print(f"Pass: {summary.get('pass_count', 0)} | Warn: {summary.get('warn_count', 0)} | Fail: {summary.get('fail_count', 0)}", flush=True)
    print(f"Report: {layout.training_artifact_verification_report_json}", flush=True)
    warnings = [row for row in report.get("findings", []) if row.get("severity") == "WARN"]
    failures = [row for row in report.get("findings", []) if row.get("severity") == "FAIL"]
    if warnings:
        print("\nWarnings:", flush=True)
        for row in warnings:
            print(f"  - {row.get('code')}: {row.get('message')}", flush=True)
    if failures:
        print("\nFailures:", flush=True)
        for row in failures:
            print(f"  - {row.get('code')}: {row.get('message')}", flush=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic SOTD Step 05 training artifact verifier.")
    parser.add_argument("--edge-root", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    layout = get_edge_layout_from_edge_root(args.edge_root)
    report = verify_training_artifacts(layout.edge_root, write_report=True)
    print_summary(layout, report)
    if report.get("verification_status") != "PASS":
        raise SystemExit(1)
    print("✅ Training artifacts are internally consistent. Edge is still not deployable until runtime score readiness and replay audit pass.", flush=True)


if __name__ == "__main__":
    main()
