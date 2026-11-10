"""
Shared SOTD edge runtime environment checks.

Every edge runtime must validate its environment before research/live uses it.
This module keeps that logic scalable so each side-specialized edge does not
reimplement contract loading, asset-bar verification, full edge.yaml validation,
and fail-closed checks.

SOTD identity contract
----------------------
Edge identity is derived from folder layout:

    assets/<ASSET>/edges/<family>_<side>/

edge.yaml confirms that derived identity through the full SOTD contract. It is
not parsed as the old flat scaffold YAML anymore.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.bars.verify_asset_bar_manifest import verify_asset_bar_manifest  # noqa: E402
from shared.contracts import REQUIRED_EDGE_RUNTIME_FUNCTIONS  # noqa: E402
from shared.edges.edge_layout import EdgeLayout, get_edge_layout  # noqa: E402
from shared.edges.edge_yaml_contract import (  # noqa: E402
    EdgeYamlContractError,
    load_edge_yaml,
    validate_edge_yaml_payload,
)


class EdgeRuntimeEnvironmentError(RuntimeError):
    """Raised when an edge runtime environment must fail closed."""


STATEFUL_RUNTIME_API_VERSION = "v2_stateful_runtime_adapter_v2_native_fail_closed"
STATEFUL_RUNTIME_FUNCTIONS = (
    "build_features_live_incremental",
    "generate_candidate_events_live",
)


def validate_stateful_runtime_module(runtime: Any) -> dict[str, Any]:
    """Validate the dashboard continuation API after the shared adapter is installed.

    Existing research/live SOTD functions remain authoritative and mandatory. These
    two live-only adapters are additional fail-closed entry points; they may not
    replace ``build_features`` or ``generate_candidate_events``.
    """
    missing_standard = [
        name for name in ("build_features", "generate_candidate_events", "score")
        if not callable(getattr(runtime, name, None))
    ]
    if missing_standard:
        raise EdgeRuntimeEnvironmentError(
            f"Runtime is missing authoritative SOTD functions: {missing_standard}"
        )
    missing_stateful = [
        name for name in STATEFUL_RUNTIME_FUNCTIONS
        if not callable(getattr(runtime, name, None))
    ]
    if missing_stateful:
        raise EdgeRuntimeEnvironmentError(
            f"Runtime is missing dashboard stateful functions: {missing_stateful}"
        )
    actual_version = str(getattr(runtime, "STATEFUL_RUNTIME_API_VERSION", ""))
    if actual_version != STATEFUL_RUNTIME_API_VERSION:
        raise EdgeRuntimeEnvironmentError(
            f"Stateful runtime API version mismatch: {actual_version!r} != {STATEFUL_RUNTIME_API_VERSION!r}"
        )
    contract_hash = str(getattr(runtime, "SHARED_FEATURE_CONTRACT_HASH", "")).strip()
    if not contract_hash:
        raise EdgeRuntimeEnvironmentError("Runtime is missing SHARED_FEATURE_CONTRACT_HASH")
    return {
        "stateful_runtime_api_status": "PASS",
        "stateful_runtime_api_version": actual_version,
        "stateful_runtime_functions": list(STATEFUL_RUNTIME_FUNCTIONS),
        "shared_feature_contract_hash": contract_hash,
        "authoritative_functions_unchanged": True,
        "dashboard_may_engineer_edge_features": False,
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EdgeRuntimeEnvironmentError(f"Missing JSON contract: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeRuntimeEnvironmentError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EdgeRuntimeEnvironmentError(f"JSON contract must be an object: {path}")
    return data


def _require_bool(obj: Mapping[str, Any], key: str, expected: bool, context: str) -> None:
    actual = obj.get(key)
    if actual is not expected:
        raise EdgeRuntimeEnvironmentError(f"{context}.{key} expected {expected!r}, got {actual!r}")


def load_edge_contract_bundle(layout: EdgeLayout) -> dict[str, Any]:
    """Load full edge.yaml and required JSON contracts for one edge layout."""

    try:
        edge_yaml = load_edge_yaml(layout.edge_yaml)
        validate_edge_yaml_payload(edge_yaml, layout)
    except EdgeYamlContractError as exc:
        raise EdgeRuntimeEnvironmentError(f"edge.yaml SOTD contract failed for {layout.edge_id}: {exc}") from exc

    contracts = {
        "bar_contract": _load_json(layout.contracts_dir / "bar_contract.json"),
        "feature_schema": _load_json(layout.contracts_dir / "feature_schema.json"),
        "event_schema": _load_json(layout.contracts_dir / "event_schema.json"),
        "label_schema": _load_json(layout.contracts_dir / "label_schema.json"),
        "calibration_schema": _load_json(layout.contracts_dir / "calibration_schema.json"),
        "live_runtime_contract": _load_json(layout.contracts_dir / "live_runtime_contract.json"),
    }

    # model_input_schema.json is produced by Step 04B after feature surgery.
    # It must not be required for fresh research/scaffold edges, otherwise a new
    # copied edge cannot run runtime.build_features/events/model_rows before
    # training. Once selected_features.json exists, or once the edge is marked
    # deployability_pass, the schema becomes mandatory again and scoring must
    # fail closed if it is missing.
    model_input_schema_path = layout.contracts_dir / "model_input_schema.json"
    selected_features_path = layout.edge_root / "artifacts" / "selected_features.json"
    edge_status = str(edge_yaml.get("status", ""))

    if model_input_schema_path.exists():
        contracts["model_input_schema"] = _load_json(model_input_schema_path)
    elif selected_features_path.exists() or edge_status == "deployability_pass":
        raise EdgeRuntimeEnvironmentError(
            f"Missing model_input_schema.json for feature-locked/scoring-ready edge: {model_input_schema_path}"
        )

    live_runtime_contract = contracts["live_runtime_contract"]
    required = live_runtime_contract.get("required_runtime_functions")
    if required != list(REQUIRED_EDGE_RUNTIME_FUNCTIONS):
        raise EdgeRuntimeEnvironmentError(
            "live_runtime_contract.required_runtime_functions does not match SOTD required runtime functions."
        )
    if live_runtime_contract.get("research_live_same_runtime_functions") is not True:
        raise EdgeRuntimeEnvironmentError("live_runtime_contract must require research/live same runtime functions.")
    if live_runtime_contract.get("dashboard_may_engineer_edge_features") is not False:
        raise EdgeRuntimeEnvironmentError("live_runtime_contract must forbid dashboard-side feature engineering.")

    bar_contract = contracts["bar_contract"]
    if bar_contract.get("dashboard_may_engineer_edge_features") is not False:
        raise EdgeRuntimeEnvironmentError("bar_contract must forbid dashboard-side feature engineering.")
    if bar_contract.get("asset_bar_scope") != "asset_level_shared_bars":
        raise EdgeRuntimeEnvironmentError("bar_contract must require asset_level_shared_bars.")

    runtime_contract = edge_yaml.get("runtime_contract")
    if not isinstance(runtime_contract, Mapping):
        raise EdgeRuntimeEnvironmentError("edge.yaml.runtime_contract must be a mapping.")
    _require_bool(runtime_contract, "dashboard_must_not_compute_features", True, "edge.yaml.runtime_contract")

    return {
        "edge_yaml": edge_yaml,
        "contracts": contracts,
    }


def validate_edge_runtime_environment(asset: str, family: str, side_name: str) -> dict[str, Any]:
    """Validate one edge runtime's environment.

    This returns PASS only for runtime environment foundations. It does not imply
    the edge is deployable.
    """

    layout = get_edge_layout(asset, family, side_name)
    identity_report = layout.validate_identity()
    if identity_report.failed:
        raise EdgeRuntimeEnvironmentError("Invalid edge identity: " + "; ".join(identity_report.fail_messages()))

    contract_bundle = load_edge_contract_bundle(layout)
    bar_verification = verify_asset_bar_manifest(layout.asset_layout.asset)

    return {
        "edge_id": layout.edge_id,
        "asset": layout.asset_layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
        "runtime_environment_status": "PASS",
        "deployable": False,
        "deployability_status": "ENVIRONMENT_PASS_NOT_DEPLOYABLE",
        "reason_not_deployable": (
            "Environment and asset bars are valid, but deployability still requires Step 05, Step 06, "
            "Step 07, Step 08 replay audit PASS, and Step 09 edge_manifest PASS."
        ),
        "edge_yaml_status": contract_bundle["edge_yaml"].get("status"),
        "bar_verification": bar_verification,
        "loaded_contracts": sorted(contract_bundle["contracts"].keys()),
        "required_runtime_functions": list(REQUIRED_EDGE_RUNTIME_FUNCTIONS),
        "dashboard_may_engineer_edge_features": False,
        "dashboard_stateful_runtime_api_version": STATEFUL_RUNTIME_API_VERSION,
        "dashboard_stateful_runtime_functions": list(STATEFUL_RUNTIME_FUNCTIONS),
        "stateful_runtime_api_installed_by": "shared.edges.live_stateful_runtime",
    }


def _run_self_test() -> None:
    try:
        result = validate_edge_runtime_environment("XAUUSD", "trend", "long")
    except Exception as exc:  # noqa: BLE001 - self-test should display the fail-closed reason.
        raise SystemExit(f"shared.edges.runtime_environment self-test failed: {exc}") from exc

    print("shared.edges.runtime_environment self-test passed")
    print(f"Edge: {result['edge_id']}")
    print(f"Runtime environment: {result['runtime_environment_status']}")
    print(f"Edge YAML status: {result['edge_yaml_status']}")
    print(f"Deployable: {result['deployable']}")
    print(f"Bar contract: {'PASS' if result['bar_verification']['bar_contract_passed'] else 'FAIL'}")


if __name__ == "__main__":
    _run_self_test()
