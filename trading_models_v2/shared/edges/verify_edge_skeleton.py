"""
Verify a V2 side-specialized edge skeleton.

Blueprint purpose
-----------------
Before any edge research logic is implemented, its package must prove that it
has the correct side-specialized structure and fail-closed scaffold status.

This verifier checks:

1. Asset/family/side identity is valid.
2. Required edge folders exist.
3. Required skeleton files exist.
4. runtime.py exposes all required runtime functions.
5. Scaffold contracts clearly say the edge is not deployable yet.
6. No dashboard-side feature engineering permission is introduced.

Run from ``trading_models_v2``:

    python ./shared/edges/verify_edge_skeleton.py --asset XAUUSD --family trend --side long
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import REQUIRED_EDGE_RUNTIME_FUNCTIONS  # noqa: E402
from shared.edges.edge_layout import EdgeLayout, describe_edge_layout, get_edge_layout  # noqa: E402


class EdgeSkeletonVerificationError(RuntimeError):
    """Raised when an edge skeleton violates the V2 Blueprint scaffold contract."""


def _load_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise EdgeSkeletonVerificationError(f"Missing JSON file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeSkeletonVerificationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EdgeSkeletonVerificationError(f"JSON document must be an object: {path}")
    return data


def _require_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise EdgeSkeletonVerificationError(f"Required file missing: {path}")


def _require_dir(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        raise EdgeSkeletonVerificationError(f"Required directory missing: {path}")


def verify_runtime_functions(layout: EdgeLayout) -> None:
    _require_file(layout.runtime_py)
    text = layout.runtime_py.read_text(encoding="utf-8")
    missing = [fn for fn in REQUIRED_EDGE_RUNTIME_FUNCTIONS if f"def {fn}" not in text]
    if missing:
        raise EdgeSkeletonVerificationError(f"runtime.py missing required runtime function(s): {missing}")

    forbidden_dashboard_phrases = [
        "dashboard_may_engineer_edge_features = True",
        'dashboard_may_engineer_edge_features": true',
        "manual dashboard feature",
    ]
    hits = [phrase for phrase in forbidden_dashboard_phrases if phrase in text]
    if hits:
        raise EdgeSkeletonVerificationError(
            f"runtime.py contains forbidden dashboard-side feature engineering phrase(s): {hits}"
        )


def verify_contract_files(layout: EdgeLayout) -> None:
    bar_contract = _load_json(layout.contracts_dir / "bar_contract.json")
    if bar_contract.get("dashboard_may_engineer_edge_features") is not False:
        raise EdgeSkeletonVerificationError("bar_contract.json must set dashboard_may_engineer_edge_features=false")
    if bar_contract.get("asset_bar_scope") != "asset_level_shared_bars":
        raise EdgeSkeletonVerificationError("bar_contract.json must require asset_level_shared_bars")

    feature_schema = _load_json(layout.contracts_dir / "feature_schema.json")
    if not isinstance(feature_schema.get("features"), list):
        raise EdgeSkeletonVerificationError("feature_schema.json must contain a features list")

    model_input_schema = _load_json(layout.contracts_dir / "model_input_schema.json")
    if model_input_schema.get("live_must_reproduce_exact_selected_rows") is not True:
        raise EdgeSkeletonVerificationError(
            "model_input_schema.json must set live_must_reproduce_exact_selected_rows=true"
        )

    live_runtime_contract = _load_json(layout.contracts_dir / "live_runtime_contract.json")
    required = live_runtime_contract.get("required_runtime_functions")
    if required != list(REQUIRED_EDGE_RUNTIME_FUNCTIONS):
        raise EdgeSkeletonVerificationError(
            "live_runtime_contract.json required_runtime_functions does not match Blueprint runtime functions"
        )
    if live_runtime_contract.get("dashboard_may_engineer_edge_features") is not False:
        raise EdgeSkeletonVerificationError(
            "live_runtime_contract.json must set dashboard_may_engineer_edge_features=false"
        )


def verify_artifact_scaffold_status(layout: EdgeLayout) -> None:
    selected_features = _load_json(layout.selected_features_json)
    if selected_features.get("status") != "empty_scaffold_not_deployable":
        raise EdgeSkeletonVerificationError(
            "selected_features.json must remain empty_scaffold_not_deployable in scaffold stage"
        )

    training_summary = _load_json(layout.artifacts_dir / "training_summary.json")
    if training_summary.get("deployable") is not False:
        raise EdgeSkeletonVerificationError("training_summary.json must set deployable=false in scaffold stage")

    validation_report = _load_json(layout.reports_dir / "validation_report.json")
    if validation_report.get("deployment_status") != "FAIL":
        raise EdgeSkeletonVerificationError("validation_report.json must set deployment_status=FAIL in scaffold stage")


def verify_edge_skeleton(layout: EdgeLayout) -> None:
    identity_report = layout.validate_identity()
    if identity_report.failed:
        raise EdgeSkeletonVerificationError("Invalid edge identity: " + "; ".join(identity_report.fail_messages()))

    for directory in layout.required_directories():
        _require_dir(directory)

    required_files = (
        layout.edge_root / "README.md",
        layout.edge_yaml,
        layout.runtime_py,
        layout.research_pipeline_py,
        *layout.expected_contract_files(),
        *layout.expected_config_files(),
        layout.selected_features_json,
        layout.artifacts_dir / "training_summary.json",
        layout.reports_dir / "validation_report.json",
    )
    for file_path in required_files:
        _require_file(file_path)

    verify_runtime_functions(layout)
    verify_contract_files(layout)
    verify_artifact_scaffold_status(layout)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a V2 edge skeleton package.")
    parser.add_argument("--asset", required=True, help="Asset name, e.g. XAUUSD.")
    parser.add_argument("--family", required=True, help="Edge family, e.g. trend.")
    parser.add_argument("--side", required=True, choices=["long", "short"], help="Edge side.")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    layout = get_edge_layout(args.asset, args.family, args.side)
    verify_edge_skeleton(layout)

    print("✅ V2 edge skeleton verification passed.")
    print("Verified scaffold is side-specialized and fail-closed/not deployable yet.")
    for line in describe_edge_layout(layout):
        print(f"  - {line}")


if __name__ == "__main__":
    try:
        _run()
    except EdgeSkeletonVerificationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
