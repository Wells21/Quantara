"""
Verify all enabled V2 edges from config/edge_universe.json.

SOTD purpose
------------
The system must scale to many assets and many side-specialized edges without
manual dashboard edits. This verifier checks each registry row against the
folder-derived layout, full edge.yaml contract, runtime API, fail-closed status,
and deployability manifest gates.

Important: research_in_progress/audit_pending edges are allowed to be incomplete
as long as they fail closed. Registry verification must not force future artifact
folders such as artifacts/calibration or artifacts/thresholds to exist before an
edge reaches the relevant lifecycle stage.
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

from shared.contracts import REQUIRED_EDGE_RUNTIME_FUNCTIONS  # noqa: E402
from shared.edges.edge_registry import EdgeRegistryError, enabled_edges, load_edge_registry  # noqa: E402
from shared.edges.edge_yaml_contract import EdgeYamlContractError, validate_edge_yaml_file  # noqa: E402
from shared.edges.verify_edge_skeleton import (  # noqa: E402
    EdgeSkeletonVerificationError,
    verify_contract_files,
    verify_edge_skeleton,
    verify_runtime_functions,
)


class EdgeRegistryVerificationError(RuntimeError):
    """Raised when one or more enabled registry edges fail verification."""


def _load_json(path: Path) -> Mapping[str, Any]:
    if not path.exists() or not path.is_file():
        raise EdgeRegistryVerificationError(f"Missing JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeRegistryVerificationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EdgeRegistryVerificationError(f"JSON document must be an object: {path}")
    return payload


def _require_file(path: Path, edge_id: str) -> None:
    if not path.exists() or not path.is_file():
        raise EdgeRegistryVerificationError(f"{edge_id}: required file missing: {path}")


def _verify_edge_yaml_matches_registry(row: Any) -> dict[str, Any]:
    try:
        result = validate_edge_yaml_file(row.to_layout().edge_root)
    except EdgeYamlContractError as exc:
        raise EdgeRegistryVerificationError(f"{row.edge_id}: edge.yaml contract failed: {exc}") from exc
    if result["edge_id"] != row.edge_id:
        raise EdgeRegistryVerificationError(f"{row.edge_id}: edge.yaml edge_id mismatch: {result['edge_id']}")
    if result["status"] != row.status:
        raise EdgeRegistryVerificationError(
            f"{row.edge_id}: registry status {row.status!r} does not match edge.yaml status {result['status']!r}"
        )
    return result


def _verify_base_edge_package(row: Any) -> None:
    """Verify only the always-required fail-closed edge package surface."""
    layout = row.to_layout()
    edge_id = row.edge_id
    _verify_edge_yaml_matches_registry(row)

    identity_report = layout.validate_identity()
    if identity_report.failed:
        raise EdgeRegistryVerificationError(f"{edge_id}: invalid edge identity: {'; '.join(identity_report.fail_messages())}")

    # Always-required directories. Do not require lifecycle artifact subfolders
    # such as model/oof/calibration/thresholds for research_in_progress edges.
    required_directories = (
        layout.edge_root,
        layout.contracts_dir,
        layout.configs_dir,
        layout.artifacts_dir,
        layout.reports_dir,
        layout.research_dir,
    )
    for directory in required_directories:
        if not directory.exists() or not directory.is_dir():
            raise EdgeRegistryVerificationError(f"{edge_id}: required directory missing: {directory}")

    required_files = (
        layout.edge_yaml,
        layout.runtime_py,
        *layout.expected_contract_files(),
    )
    for file_path in required_files:
        _require_file(file_path, edge_id)

    try:
        verify_runtime_functions(layout)
        verify_contract_files(layout)
    except EdgeSkeletonVerificationError as exc:
        raise EdgeRegistryVerificationError(f"{edge_id}: {exc}") from exc

    runtime_text = layout.runtime_py.read_text(encoding="utf-8")
    missing_functions = [fn for fn in REQUIRED_EDGE_RUNTIME_FUNCTIONS if f"def {fn}" not in runtime_text]
    if missing_functions:
        raise EdgeRegistryVerificationError(f"{edge_id}: runtime.py missing functions: {missing_functions}")
    if "dashboard_may_engineer_edge_features = True" in runtime_text:
        raise EdgeRegistryVerificationError(f"{edge_id}: runtime.py permits dashboard-side feature engineering")


def _verify_non_deployable_edge(row: Any) -> None:
    """Verify a non-PASS edge can exist safely and fail closed."""
    layout = row.to_layout()
    edge_id = row.edge_id
    _verify_base_edge_package(row)

    # Non-deployable edges must not expose a PASS/deployable edge manifest.
    if layout.edge_manifest_json.exists():
        manifest = _load_json(layout.edge_manifest_json)
        if manifest.get("deployment_status") == "PASS" or manifest.get("deployable") is True:
            raise EdgeRegistryVerificationError(f"{edge_id}: non-deployable registry status but edge_manifest declares PASS/deployable")


def _verify_deployability_pass_edge(row: Any) -> None:
    layout = row.to_layout()
    _verify_base_edge_package(row)
    manifest = _load_json(layout.edge_manifest_json)

    expected_edge_id = row.edge_id
    identity = manifest.get("identity", {})
    if not isinstance(identity, dict):
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest.identity must be an object")

    manifest_edge_id = identity.get("edge_id") or f"{identity.get('asset')}/{identity.get('family')}_{identity.get('side_name')}"
    if manifest_edge_id != expected_edge_id:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest identity mismatch: got {manifest_edge_id!r}")

    if manifest.get("deployment_status") != "PASS":
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest deployment_status must be PASS")
    if manifest.get("deployable") is not True:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest deployable must be true")

    contracts = manifest.get("contracts", {})
    if not isinstance(contracts, dict):
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest.contracts must be an object")
    if contracts.get("allocator_may_consume_only_if_deployment_status_pass") is not True:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest must enforce allocator PASS gate")
    if contracts.get("dashboard_may_engineer_edge_features") is not False:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: edge_manifest must forbid dashboard-side feature engineering")

    selected_features = manifest.get("selected_features", {})
    if not isinstance(selected_features, dict):
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: selected_features manifest section must be an object")
    if int(selected_features.get("selected_feature_count", 0)) <= 0:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: selected_feature_count must be positive")
    if selected_features.get("feature_order_locked") is not True:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: selected feature order must be locked")

    live_compatibility = manifest.get("live_compatibility", {})
    if not isinstance(live_compatibility, dict):
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: live_compatibility must be an object")
    required_pass_fields = [
        "selected_row_replay_passed",
        "raw_probability_replay_passed",
        "calibrated_probability_replay_passed",
        "afml_bet_size_replay_passed",
        "threshold_decision_replay_passed",
    ]
    missing_pass = [field for field in required_pass_fields if live_compatibility.get(field) is not True]
    if missing_pass:
        raise EdgeRegistryVerificationError(f"{expected_edge_id}: replay pass fields missing/false in edge_manifest: {missing_pass}")


def verify_enabled_registry_edges() -> tuple[tuple[str, str], ...]:
    rows = enabled_edges(load_edge_registry())
    if not rows:
        raise EdgeRegistryVerificationError("No enabled edges found in config/edge_universe.json")

    verified: list[tuple[str, str]] = []
    failures: list[str] = []

    for row in rows:
        try:
            if row.status == "deployability_pass":
                _verify_deployability_pass_edge(row)
                verified.append((row.edge_id, "edge_manifest_pass"))
            elif row.status == "scaffold_not_deployable":
                _verify_edge_yaml_matches_registry(row)
                verify_edge_skeleton(row.to_layout())
                verified.append((row.edge_id, "scaffold_fail_closed"))
            else:
                _verify_non_deployable_edge(row)
                verified.append((row.edge_id, "non_deployable_fail_closed"))
        except (EdgeSkeletonVerificationError, EdgeRegistryVerificationError) as exc:
            failures.append(f"{row.edge_id}: {exc}")

    if failures:
        joined = "\n".join(f"- {failure}" for failure in failures)
        raise EdgeRegistryVerificationError(f"Enabled edge verification failed:\n{joined}")
    return tuple(verified)


def _run() -> None:
    verified = verify_enabled_registry_edges()
    print("V2 edge registry verification passed")
    print(f"Enabled edges verified: {len(verified)}")
    for edge_id, mode in verified:
        print(f"  - {edge_id} | verification={mode}")


if __name__ == "__main__":
    try:
        _run()
    except (EdgeRegistryError, EdgeRegistryVerificationError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
