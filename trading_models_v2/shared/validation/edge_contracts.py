"""Shared edge contract validation for SOTD.

This module centralizes runtime API, full edge.yaml contract validation,
model-input schema parity, selected-feature validation, and fail-closed scoring
artifact checks. It must remain scalable for any future asset/edge folder.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.contracts import REQUIRED_EDGE_RUNTIME_FUNCTIONS
from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root
from shared.edges.edge_yaml_contract import EdgeYamlContractError, validate_edge_yaml_file
from shared.edges.runtime_loader import EdgeRuntimeLoadError, load_edge_runtime
from shared.scoring.edge_scorer import EdgeScorer, EdgeScoringNotReady
from shared.validation.feature_schema import FeatureSchemaValidationError, normalize_selected_features, validate_selected_features


class RuntimeContractError(RuntimeError):
    """Raised when a runtime/edge contract must fail closed."""


def load_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise RuntimeContractError(f"Missing JSON file: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeContractError(f"Invalid JSON in {file_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError(f"JSON must be an object: {file_path}")
    return payload


def layout_from_edge_root(edge_root: str | Path) -> EdgeLayout:
    try:
        return get_edge_layout_from_edge_root(edge_root)
    except Exception as exc:
        raise RuntimeContractError(str(exc)) from exc


def edge_identity_from_root(edge_root: str | Path) -> dict[str, Any]:
    layout = layout_from_edge_root(edge_root)
    return {
        "asset": layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
        "edge_id": layout.edge_id,
        "edge_stem": layout.edge_stem,
    }


def validate_edge_yaml(edge_root: str | Path) -> dict[str, Any]:
    try:
        return validate_edge_yaml_file(edge_root)
    except EdgeYamlContractError as exc:
        raise RuntimeContractError(str(exc)) from exc


def load_runtime_module(edge_root: str | Path) -> ModuleType:
    try:
        return load_edge_runtime(edge_root)
    except EdgeRuntimeLoadError as exc:
        raise RuntimeContractError(str(exc)) from exc


def validate_runtime_required_api(edge_root: str | Path, *, import_runtime: bool = False) -> dict[str, Any]:
    layout = layout_from_edge_root(edge_root)
    runtime_path = layout.runtime_py
    if not runtime_path.exists():
        raise RuntimeContractError(f"Missing runtime.py: {runtime_path}")
    text = runtime_path.read_text(encoding="utf-8")
    missing_by_text = [fn for fn in REQUIRED_EDGE_RUNTIME_FUNCTIONS if f"def {fn}" not in text]
    if missing_by_text:
        raise RuntimeContractError(f"runtime.py missing required function definitions: {missing_by_text}")
    if "dashboard_may_engineer_edge_features = True" in text:
        raise RuntimeContractError("runtime.py permits dashboard-side edge feature engineering.")
    if import_runtime:
        module = load_runtime_module(layout.edge_root)
        missing = [fn for fn in REQUIRED_EDGE_RUNTIME_FUNCTIONS if not callable(getattr(module, fn, None))]
        if missing:
            raise RuntimeContractError(f"runtime.py missing callable required functions: {missing}")
    return {"runtime_py": str(runtime_path), "required_functions": list(REQUIRED_EDGE_RUNTIME_FUNCTIONS)}


def validate_model_input_schema_matches_selected(edge_root: str | Path) -> dict[str, Any]:
    layout = layout_from_edge_root(edge_root)
    selected_payload = load_json(layout.selected_features_json)
    model_input_payload = load_json(layout.model_input_schema_json)
    feature_schema_payload = load_json(layout.feature_schema_json)
    selected = normalize_selected_features(selected_payload)
    columns = [str(x) for x in model_input_payload.get("model_input_columns", [])]
    if selected != columns:
        raise RuntimeContractError("model_input_schema.json must match artifacts/selected_features.json exactly and in order.")
    if selected_payload.get("feature_order_locked") is not True:
        raise RuntimeContractError("artifacts/selected_features.json must set feature_order_locked=true.")
    if model_input_payload.get("model_input_order_locked") is not True:
        raise RuntimeContractError("contracts/model_input_schema.json must set model_input_order_locked=true.")
    try:
        validate_selected_features(selected, feature_schema_payload, expected_edge_id=layout.edge_id)
    except FeatureSchemaValidationError as exc:
        raise RuntimeContractError(str(exc)) from exc
    return {"edge_id": layout.edge_id, "selected_feature_count": len(selected), "model_input_order_locked": True}


def validate_scoring_artifacts(edge_root: str | Path) -> dict[str, Any]:
    try:
        return EdgeScorer.from_edge_root(Path(edge_root)).validate_artifacts()
    except EdgeScoringNotReady:
        raise
    except Exception as exc:
        raise RuntimeContractError(str(exc)) from exc


def validate_fail_closed_contract(edge_root: str | Path) -> dict[str, Any]:
    layout = layout_from_edge_root(edge_root)
    edge_yaml = validate_edge_yaml(layout.edge_root)
    validate_runtime_required_api(layout.edge_root, import_runtime=False)
    status = str(edge_yaml.get("status", ""))
    manifest = layout.edge_manifest_json
    if status != "deployability_pass" and manifest.exists():
        payload = load_json(manifest)
        if payload.get("deployment_status") == "PASS" or payload.get("deployable") is True:
            raise RuntimeContractError(f"{layout.edge_id} is not deployability_pass but edge_manifest declares PASS/deployable.")
    return {"edge_id": layout.edge_id, "status": status, "fail_closed": True}


def validate_edge_contracts(edge_root: str | Path, *, require_scoring_artifacts: bool = False) -> dict[str, Any]:
    layout = layout_from_edge_root(edge_root)
    result = {
        "edge_yaml": validate_edge_yaml(layout.edge_root),
        "runtime_api": validate_runtime_required_api(layout.edge_root, import_runtime=False),
        "fail_closed": validate_fail_closed_contract(layout.edge_root),
    }
    if layout.selected_features_json.exists() and layout.model_input_schema_json.exists():
        result["model_input_schema"] = validate_model_input_schema_matches_selected(layout.edge_root)
    if require_scoring_artifacts:
        result["scoring_artifacts"] = validate_scoring_artifacts(layout.edge_root)
    return result


def _run_self_test() -> None:
    parser = argparse.ArgumentParser(description="Validate generic SOTD edge contracts for any edge root.")
    parser.add_argument("--edge-root", default=".")
    args = parser.parse_args()
    result = validate_edge_contracts(Path(args.edge_root), require_scoring_artifacts=False)
    print("shared.validation.edge_contracts self-test passed")
    print(json.dumps(result["edge_yaml"], indent=2, default=str))


if __name__ == "__main__":
    _run_self_test()
