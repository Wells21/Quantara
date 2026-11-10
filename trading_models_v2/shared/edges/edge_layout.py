# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
"""Scalable V2 edge layout resolver.

SOT rule: no shared script should hard-code current assets or edges. Edge
identity is derived from the folder contract:

    assets/<ASSET>/edges/<family>_<side>/

The edge folder name gives edge_name. The text before the final underscore is
family. The text after the final underscore is side_name. edge.yaml confirms
this identity but does not replace the folder contract.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.assets.asset_layout import AssetLayout, get_asset_layout  # noqa: E402
from shared.contracts import (  # noqa: E402
    AuditFinding,
    AuditReport,
    AuditSeverity,
    EdgeIdentity,
    REQUIRED_EDGE_RUNTIME_FUNCTIONS,
    SIDE_LONG,
    SIDE_SHORT,
    SIDE_VALUE_LONG,
    SIDE_VALUE_SHORT,
)


class EdgeLayoutError(RuntimeError):
    """Raised when an edge path violates the scalable V2 layout contract."""


CONTRACT_FILE_NAMES: tuple[str, ...] = (
    "bar_contract.json",
    "feature_schema.json",
    "event_schema.json",
    "label_schema.json",
    "model_input_schema.json",
    "calibration_schema.json",
    "live_runtime_contract.json",
)
CONFIG_FILE_NAMES: tuple[str, ...] = ("research.yaml", "labeling.yaml", "model.yaml", "calibration.yaml")
ARTIFACT_FILE_NAMES: tuple[str, ...] = (
    "selected_features.json",
    "edge_manifest.json",
)
REPORT_FILE_NAMES: tuple[str, ...] = (
    "training_artifact_verification_report.json",
    "runtime_scoring_ready_report.json",
    "runtime_score_smoke_test_report.json",
    "probability_calibration_afml_replay_audit_report.json",
)


@dataclass(frozen=True)
class EdgeLayout:
    """Resolved layout for one side-specialized edge."""

    asset_layout: AssetLayout
    family: str
    side_name: str

    @property
    def v2_root(self) -> Path:
        return self.asset_layout.v2_root

    @property
    def asset(self) -> str:
        return self.asset_layout.asset

    @property
    def side_value(self) -> int:
        if self.side_name == SIDE_LONG:
            return SIDE_VALUE_LONG
        if self.side_name == SIDE_SHORT:
            return SIDE_VALUE_SHORT
        raise EdgeLayoutError(f"side_name must be long or short, got {self.side_name!r}")

    @property
    def identity(self) -> EdgeIdentity:
        return EdgeIdentity(asset=self.asset, family=self.family, side_name=self.side_name, side_value=self.side_value)

    @property
    def edge_name(self) -> str:
        return f"{self.family}_{self.side_name}"

    @property
    def edge_id(self) -> str:
        return self.identity.edge_id

    @property
    def edge_stem(self) -> str:
        return f"{self.asset.lower()}_{self.edge_name}"

    @property
    def edge_root(self) -> Path:
        return self.asset_layout.edges_root / self.edge_name

    @property
    def contracts_dir(self) -> Path:
        return self.edge_root / "contracts"

    @property
    def configs_dir(self) -> Path:
        return self.edge_root / "configs"

    @property
    def artifacts_dir(self) -> Path:
        return self.edge_root / "artifacts"

    @property
    def reports_dir(self) -> Path:
        return self.edge_root / "reports"

    @property
    def plots_dir(self) -> Path:
        return self.reports_dir / "plots"

    @property
    def research_dir(self) -> Path:
        return self.edge_root / "research"

    @property
    def research_dataset_dir(self) -> Path:
        return self.artifacts_dir / "research_dataset"

    @property
    def feature_surgery_dir(self) -> Path:
        return self.artifacts_dir / "feature_surgery"

    @property
    def model_dir(self) -> Path:
        return self.artifacts_dir / "model"

    @property
    def oof_dir(self) -> Path:
        return self.artifacts_dir / "oof"

    @property
    def execution_dir(self) -> Path:
        return self.artifacts_dir / "execution"

    @property
    def calibration_dir(self) -> Path:
        return self.artifacts_dir / "calibration"

    @property
    def thresholds_dir(self) -> Path:
        return self.artifacts_dir / "thresholds"

    @property
    def edge_yaml(self) -> Path:
        return self.edge_root / "edge.yaml"

    @property
    def runtime_py(self) -> Path:
        return self.edge_root / "runtime.py"

    @property
    def research_pipeline_py(self) -> Path:
        return self.edge_root / "research_pipeline.py"

    @property
    def edge_manifest_json(self) -> Path:
        return self.artifacts_dir / "edge_manifest.json"

    @property
    def selected_features_json(self) -> Path:
        return self.artifacts_dir / "selected_features.json"

    @property
    def feature_surgery_selected_features_json(self) -> Path:
        return self.feature_surgery_dir / f"{self.edge_stem}_selected_features.json"

    @property
    def feature_surgery_manifest_json(self) -> Path:
        return self.feature_surgery_dir / "feature_surgery_manifest.json"

    @property
    def resolution_csv(self) -> Path:
        """Task 2A's execution-resolution CSV -- the canonical dataset for
        Step 01/02/03 under the MLDP executable-target architecture
        (superset of the legacy <edge_stem>_model_rows.csv)."""
        return self.research_dataset_dir / f"{self.edge_stem}_mldp_execution_resolution.csv"

    @property
    def model_pkl(self) -> Path:
        return self.model_dir / f"{self.edge_stem}_meta_model.pkl"

    @property
    def legacy_model_pkl(self) -> Path:
        return self.artifacts_dir / "model.pkl"

    @property
    def model_best_params_json(self) -> Path:
        return self.model_dir / f"{self.edge_stem}_meta_model_best_params.json"

    @property
    def model_training_manifest_json(self) -> Path:
        return self.model_dir / "model_training_manifest.json"

    @property
    def oof_predictions_csv(self) -> Path:
        return self.oof_dir / f"{self.edge_stem}_oof_predictions.csv"

    @property
    def oof_manifest_json(self) -> Path:
        return self.oof_dir / "oof_predictions_manifest.json"

    @property
    def exact_oof_execution_csv(self) -> Path:
        """Legacy Step03A output path. Step03A is retired; nothing writes here
        anymore. Kept only so old on-disk artifacts remain addressable."""
        return self.execution_dir / f"{self.edge_stem}_oof_exact_execution.csv"

    @property
    def exact_oof_execution_manifest_json(self) -> Path:
        """Legacy Step03A output path. Step03A is retired; nothing writes here
        anymore. Kept only so old on-disk artifacts remain addressable."""
        return self.execution_dir / "oof_exact_execution_manifest.json"

    @property
    def exact_target_manifest_json(self) -> Path:
        """Task 2A's executable-target manifest -- the current source of
        exact Bid/Ask execution provenance (coverage_rate, execution_status_counts,
        execution_contract_version), superseding the retired Step03A manifest."""
        return self.research_dataset_dir / f"{self.edge_stem}_mldp_executable_target_manifest.json"

    @property
    def calibrator_json(self) -> Path:
        return self.calibration_dir / f"probability_calibrator_sigmoid_side_{self.side_name}.json"

    @property
    def legacy_calibrator_json(self) -> Path:
        return self.artifacts_dir / "calibrator.json"

    @property
    def calibrator_pkl(self) -> Path:
        return self.calibration_dir / f"{self.edge_stem}_sigmoid_calibrator.pkl"

    @property
    def calibrated_oof_predictions_csv(self) -> Path:
        return self.calibration_dir / f"{self.edge_stem}_calibrated_oof_predictions.csv"

    @property
    def calibration_report_json(self) -> Path:
        return self.calibration_dir / f"{self.edge_stem}_calibration_report.json"

    @property
    def calibration_manifest_json(self) -> Path:
        return self.calibration_dir / f"{self.edge_stem}_calibration_manifest.json"

    @property
    def selected_threshold_json(self) -> Path:
        return self.thresholds_dir / f"{self.edge_stem}_selected_threshold.json"

    @property
    def calibrated_threshold_sweep_csv(self) -> Path:
        return self.thresholds_dir / f"{self.edge_stem}_calibrated_threshold_sweep.csv"

    @property
    def threshold_manifest_json(self) -> Path:
        return self.thresholds_dir / f"{self.edge_stem}_threshold_manifest.json"

    @property
    def feature_schema_json(self) -> Path:
        return self.contracts_dir / "feature_schema.json"

    @property
    def model_input_schema_json(self) -> Path:
        return self.contracts_dir / "model_input_schema.json"

    @property
    def bar_contract_json(self) -> Path:
        return self.contracts_dir / "bar_contract.json"

    @property
    def event_schema_json(self) -> Path:
        return self.contracts_dir / "event_schema.json"

    @property
    def label_schema_json(self) -> Path:
        return self.contracts_dir / "label_schema.json"

    @property
    def calibration_schema_json(self) -> Path:
        return self.contracts_dir / "calibration_schema.json"

    @property
    def live_runtime_contract_json(self) -> Path:
        return self.contracts_dir / "live_runtime_contract.json"

    @property
    def training_artifact_verification_report_json(self) -> Path:
        return self.reports_dir / "training_artifact_verification_report.json"

    @property
    def runtime_scoring_ready_report_json(self) -> Path:
        return self.reports_dir / "runtime_scoring_ready_report.json"

    @property
    def runtime_score_smoke_test_report_json(self) -> Path:
        return self.reports_dir / "runtime_score_smoke_test_report.json"

    @property
    def replay_audit_report_json(self) -> Path:
        return self.reports_dir / "probability_calibration_afml_replay_audit_report.json"

    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.edge_root,
            self.contracts_dir,
            self.configs_dir,
            self.artifacts_dir,
            self.research_dataset_dir,
            self.feature_surgery_dir,
            self.model_dir,
            self.oof_dir,
            self.calibration_dir,
            self.thresholds_dir,
            self.reports_dir,
            self.plots_dir,
        )

    def expected_contract_files(self) -> tuple[Path, ...]:
        return tuple(self.contracts_dir / name for name in CONTRACT_FILE_NAMES)

    def expected_config_files(self) -> tuple[Path, ...]:
        return tuple(self.configs_dir / name for name in CONFIG_FILE_NAMES)

    def expected_artifact_files(self) -> tuple[Path, ...]:
        return tuple(self.artifacts_dir / name for name in ARTIFACT_FILE_NAMES)

    def expected_report_files(self) -> tuple[Path, ...]:
        return tuple(self.reports_dir / name for name in REPORT_FILE_NAMES)

    def canonical_scoring_artifacts(self) -> tuple[Path, ...]:
        return (self.selected_features_json, self.model_input_schema_json, self.model_pkl, self.calibrator_json, self.selected_threshold_json)

    def create_directories(self) -> None:
        for path in self.required_directories():
            path.mkdir(parents=True, exist_ok=True)

    def validate_identity(self) -> AuditReport:
        return self.identity.validate()

    def validate_existing_skeleton(self, require_files: bool = False) -> AuditReport:
        findings: list[AuditFinding] = []
        findings.extend(self.validate_identity().findings)
        for directory in self.required_directories():
            if not directory.exists():
                findings.append(AuditFinding(AuditSeverity.FAIL, "EDGE_REQUIRED_DIRECTORY_MISSING", f"Missing edge directory: {directory}"))
        if require_files:
            required_files = (self.edge_yaml, self.runtime_py, *self.expected_contract_files())
            for file_path in required_files:
                if not file_path.exists():
                    findings.append(AuditFinding(AuditSeverity.FAIL, "EDGE_REQUIRED_FILE_MISSING", f"Missing edge skeleton file: {file_path}"))
        if not any(f.severity == AuditSeverity.FAIL for f in findings):
            findings.append(AuditFinding(AuditSeverity.PASS, "EDGE_LAYOUT_OK", f"Edge layout is valid for {self.edge_id}."))
        return AuditReport(name=f"edge_layout::{self.edge_id}", findings=tuple(findings))


def normalize_family(family: str) -> str:
    normalized = family.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise EdgeLayoutError("family must be non-empty")
    if normalized in {SIDE_LONG, SIDE_SHORT}:
        raise EdgeLayoutError("family cannot be only a side name")
    return normalized


def normalize_side(side_name: str) -> str:
    side = side_name.strip().lower()
    if side not in {SIDE_LONG, SIDE_SHORT}:
        raise EdgeLayoutError(f"side_name must be long or short, got {side_name!r}")
    return side


def split_edge_name(edge_name: str) -> tuple[str, str]:
    if "_" not in edge_name:
        raise EdgeLayoutError(f"edge folder must be <family>_<side>, got {edge_name!r}")
    family, side_name = edge_name.rsplit("_", 1)
    return normalize_family(family), normalize_side(side_name)


def get_edge_layout(asset: str, family: str, side_name: str, v2_root: str | Path | None = None) -> EdgeLayout:
    asset_layout = get_asset_layout(asset, v2_root=v2_root)
    return EdgeLayout(asset_layout=asset_layout, family=normalize_family(family), side_name=normalize_side(side_name))


def get_edge_layout_from_edge_root(edge_root: str | Path) -> EdgeLayout:
    root = Path(edge_root).resolve()
    family, side_name = split_edge_name(root.name)
    if root.parent.name != "edges":
        raise EdgeLayoutError(f"edge_root parent must be 'edges', got {root.parent.name!r}: {root}")
    asset = root.parent.parent.name
    try:
        v2_root = root.parents[3]
    except IndexError as exc:
        raise EdgeLayoutError(f"Could not resolve trading_models_v2 root from {root}") from exc
    return get_edge_layout(asset=asset, family=family, side_name=side_name, v2_root=v2_root)


def describe_edge_layout(layout: EdgeLayout) -> list[str]:
    return [
        f"edge_id={layout.edge_id}",
        f"edge_stem={layout.edge_stem}",
        f"edge_root={layout.edge_root.as_posix()}",
        f"runtime_py={layout.runtime_py.as_posix()}",
        f"model={layout.model_pkl.as_posix()}",
        f"calibrator={layout.calibrator_json.as_posix()}",
        f"selected_threshold={layout.selected_threshold_json.as_posix()}",
        f"edge_manifest={layout.edge_manifest_json.as_posix()}",
        f"required_runtime_functions={', '.join(REQUIRED_EDGE_RUNTIME_FUNCTIONS)}",
    ]


def _run_self_test() -> None:
    parser = argparse.ArgumentParser(description="Resolve a scalable V2 edge layout from folder structure.")
    parser.add_argument("--edge-root", default=".", help="Path to assets/<ASSET>/edges/<family>_<side>.")
    args = parser.parse_args()
    layout = get_edge_layout_from_edge_root(args.edge_root)
    expected_stem = f"{layout.asset.lower()}_{layout.edge_name}"
    if layout.edge_stem != expected_stem:
        raise SystemExit(f"edge_stem mismatch: expected {expected_stem}, got {layout.edge_stem}")
    if layout.model_pkl.name != f"{layout.edge_stem}_meta_model.pkl":
        raise SystemExit(f"model path mismatch: {layout.model_pkl}")
    if layout.calibrator_json.name != f"probability_calibrator_sigmoid_side_{layout.side_name}.json":
        raise SystemExit(f"calibrator path mismatch: {layout.calibrator_json}")
    if layout.selected_threshold_json.name != f"{layout.edge_stem}_selected_threshold.json":
        raise SystemExit(f"threshold path mismatch: {layout.selected_threshold_json}")
    report = layout.validate_identity()
    if report.failed:
        raise SystemExit("Edge identity failed: " + "; ".join(report.fail_messages()))
    print("shared.edges.edge_layout self-test passed")
    print("SOT scalability rule: asset and edge identity are derived from folder names")
    for line in describe_edge_layout(layout):
        print(f"  - {line}")


if __name__ == "__main__":
    _run_self_test()
