"""
Shared V2 contract models for the MLDP/AFML research-to-live rebuild.

This module is the first reusable foundation layer under ``trading_models_v2/shared``.
It is intentionally dependency-light and does not import anything from V1.

It encodes the non-negotiable Blueprint contracts used by later edge packages,
allocator scripts, dashboard orchestration, execution preflight, and audits.

Run from ``trading_models_v2``:

    python ./shared/contracts.py

or from the repository root:

    python trading_models_v2/shared/contracts.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence


# =============================================================================
# Blueprint constants
# =============================================================================

V2_CONTRACT_VERSION = "v2.1"
LIVE_MANIFEST_CONTRACT_VERSION = "direct_weight_calibrated_afml_v2"

BAR_MODE_STANDARD_QUOTE_TICK = "standard_quote_tick"
THRESHOLD_UNIT_TICKS = "ticks"
PROXY_VALUE_MEANING_TICK_COUNT = "tick_count_backward_compatibility_only"

PROBABILITY_SPACE_RAW = "raw_probability"
PROBABILITY_SPACE_CALIBRATED = "calibrated_probability"

DEPLOYMENT_PASS = "PASS"
DEPLOYMENT_FAIL = "FAIL"
DEPLOYMENT_WARN = "WARN"

SIDE_LONG = "long"
SIDE_SHORT = "short"
SIDE_VALUE_LONG = 1
SIDE_VALUE_SHORT = -1

REQUIRED_EDGE_RUNTIME_FUNCTIONS: tuple[str, ...] = (
    "load_contract",
    "validate_environment",
    "build_features",
    "generate_candidate_events",
    "build_model_rows",
    "load_model",
    "load_calibrator",
    "score",
    "explain_missing_features",
)

REQUIRED_BAR_COLUMNS: tuple[str, ...] = (
    "Datetime",
    "Bar_Start",
    "Open",
    "High",
    "Low",
    "Close",
    "Proxy_Value",
    "Ticks",
    "Threshold",
    "Threshold_Unit",
    "Research_Bar_Mode",
    "Executable",
    "Gap_Healed",
    "Source",
)

REQUIRED_EDGE_MANIFEST_SECTIONS: tuple[str, ...] = (
    "identity",
    "contracts",
    "artifacts",
    "runtime",
    "metrics",
    "threshold",
    "live_compatibility",
)

REQUIRED_LIVE_MANIFEST_FIELDS: tuple[str, ...] = (
    "contract_version",
    "created_at",
    "selected_allocator_scenario",
    "selected_variant",
    "risk_formula",
    "prop_challenge_rules",
    "alpha_legs",
    "frozen_bar_thresholds",
    "validation_summary",
)

FORBIDDEN_SELECTED_FEATURE_TOKENS: tuple[str, ...] = (
    "future",
    "label",
    "target",
    "outcome",
    "realized_r",
    "t1",
    "barrier_hit",
    "post_entry",
    "drawdown_after_entry",
    "live_signal_metadata",
)


# =============================================================================
# Audit primitives
# =============================================================================

class AuditSeverity(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class AuditFinding:
    severity: AuditSeverity
    code: str
    message: str


@dataclass(frozen=True)
class AuditReport:
    name: str
    findings: tuple[AuditFinding, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not any(f.severity == AuditSeverity.FAIL for f in self.findings)

    @property
    def failed(self) -> bool:
        return not self.passed

    @property
    def warnings(self) -> tuple[AuditFinding, ...]:
        return tuple(f for f in self.findings if f.severity == AuditSeverity.WARN)

    @property
    def failures(self) -> tuple[AuditFinding, ...]:
        return tuple(f for f in self.findings if f.severity == AuditSeverity.FAIL)

    def fail_messages(self) -> list[str]:
        return [f"{f.code}: {f.message}" for f in self.failures]

    def print_summary(self) -> None:
        status = "PASS" if self.passed else "FAIL"
        print(f"[{status}] {self.name}")
        for finding in self.findings:
            print(f"  - {finding.severity.value}: {finding.code}: {finding.message}")


class ContractValidationError(RuntimeError):
    """Raised when a V2 contract must fail closed."""


def require_no_failures(reports: Iterable[AuditReport]) -> None:
    failures: list[str] = []
    for report in reports:
        failures.extend([f"[{report.name}] {msg}" for msg in report.fail_messages()])

    if failures:
        joined = "\n".join(f"- {msg}" for msg in failures)
        raise ContractValidationError(f"V2 contract validation failed:\n{joined}")


# =============================================================================
# Bar contract
# =============================================================================

@dataclass(frozen=True)
class BarContract:
    bar_mode: str = BAR_MODE_STANDARD_QUOTE_TICK
    threshold_unit: str = THRESHOLD_UNIT_TICKS
    proxy_value_meaning: str = PROXY_VALUE_MEANING_TICK_COUNT
    required_columns: tuple[str, ...] = REQUIRED_BAR_COLUMNS

    def validate(self) -> AuditReport:
        findings: list[AuditFinding] = []

        if self.bar_mode != BAR_MODE_STANDARD_QUOTE_TICK:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_MODE_NOT_SUPPORTED",
                    f"bar_mode must be {BAR_MODE_STANDARD_QUOTE_TICK!r}, got {self.bar_mode!r}.",
                )
            )

        if self.threshold_unit != THRESHOLD_UNIT_TICKS:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "THRESHOLD_UNIT_NOT_TICKS",
                    f"threshold_unit must be {THRESHOLD_UNIT_TICKS!r}, got {self.threshold_unit!r}.",
                )
            )

        if self.proxy_value_meaning != PROXY_VALUE_MEANING_TICK_COUNT:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "PROXY_VALUE_MEANING_INVALID",
                    "Proxy_Value may only mean backward-compatible tick count in the V2 production path.",
                )
            )

        missing = [c for c in REQUIRED_BAR_COLUMNS if c not in self.required_columns]
        if missing:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "BAR_COLUMNS_MISSING",
                    f"Missing required bar columns: {missing}",
                )
            )

        if not findings:
            findings.append(AuditFinding(AuditSeverity.PASS, "BAR_CONTRACT_OK", "Bar contract is V2-compliant."))

        return AuditReport(name="bar_contract", findings=tuple(findings))


# =============================================================================
# Feature contract
# =============================================================================

@dataclass(frozen=True)
class FeatureContractRow:
    feature_name: str
    owner_stage: str
    dtype: str
    formula_description: str
    required_inputs: tuple[str, ...]
    live_available: bool
    uses_future_information: bool
    allowed_for_model_input: bool
    missing_value_policy: str
    training_fit_required: bool = False
    artifact_dependency: str | None = None
    version_added: str = V2_CONTRACT_VERSION

    def validate_for_selected_model_input(self) -> AuditReport:
        findings: list[AuditFinding] = []
        lower_name = self.feature_name.lower()

        if not self.feature_name.strip():
            findings.append(
                AuditFinding(AuditSeverity.FAIL, "FEATURE_NAME_EMPTY", "Feature name must be non-empty.")
            )

        if not self.live_available:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "FEATURE_NOT_LIVE_AVAILABLE",
                    f"Selected feature {self.feature_name!r} is not live_available.",
                )
            )

        if self.uses_future_information:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "FEATURE_USES_FUTURE_INFORMATION",
                    f"Selected feature {self.feature_name!r} uses future information.",
                )
            )

        if not self.allowed_for_model_input:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "FEATURE_NOT_ALLOWED_FOR_MODEL_INPUT",
                    f"Selected feature {self.feature_name!r} is not allowed_for_model_input.",
                )
            )

        bad_tokens = [tok for tok in FORBIDDEN_SELECTED_FEATURE_TOKENS if tok in lower_name]
        if bad_tokens:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "FEATURE_NAME_HAS_FORBIDDEN_TOKEN",
                    f"Selected feature {self.feature_name!r} contains forbidden token(s): {bad_tokens}",
                )
            )

        if self.training_fit_required and not self.artifact_dependency:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "FEATURE_FIT_STATE_MISSING",
                    f"Feature {self.feature_name!r} requires fitted state but has no artifact_dependency.",
                )
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "FEATURE_SELECTED_INPUT_OK",
                    f"Feature {self.feature_name!r} is valid for selected model input.",
                )
            )

        return AuditReport(name=f"feature::{self.feature_name}", findings=tuple(findings))


def validate_selected_feature_rows(rows: Sequence[FeatureContractRow]) -> AuditReport:
    findings: list[AuditFinding] = []
    seen: set[str] = set()

    for row in rows:
        if row.feature_name in seen:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "DUPLICATE_FEATURE_NAME",
                    f"Duplicate feature schema row: {row.feature_name!r}",
                )
            )
        seen.add(row.feature_name)
        findings.extend(row.validate_for_selected_model_input().findings)

    if rows and not any(f.severity == AuditSeverity.FAIL for f in findings):
        findings.append(
            AuditFinding(
                AuditSeverity.PASS,
                "SELECTED_FEATURE_SCHEMA_OK",
                "All selected feature schema rows are live-safe and model-input-safe.",
            )
        )

    if not rows:
        findings.append(
            AuditFinding(
                AuditSeverity.WARN,
                "NO_FEATURE_ROWS_YET",
                "No feature rows supplied yet. This is acceptable only before the first pilot edge.",
            )
        )

    return AuditReport(name="selected_feature_schema", findings=tuple(findings))


# =============================================================================
# Edge/runtime contract
# =============================================================================

@dataclass(frozen=True)
class EdgeIdentity:
    asset: str
    family: str
    side_name: str
    side_value: int

    @property
    def edge_id(self) -> str:
        return f"{self.asset}/{self.family}_{self.side_name}"

    def validate(self) -> AuditReport:
        findings: list[AuditFinding] = []

        if self.side_name not in {SIDE_LONG, SIDE_SHORT}:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "EDGE_SIDE_NAME_INVALID",
                    f"side_name must be 'long' or 'short', got {self.side_name!r}.",
                )
            )

        expected_value = SIDE_VALUE_LONG if self.side_name == SIDE_LONG else SIDE_VALUE_SHORT
        if self.side_name in {SIDE_LONG, SIDE_SHORT} and self.side_value != expected_value:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "EDGE_SIDE_VALUE_MISMATCH",
                    f"{self.side_name!r} edge must have side_value={expected_value}, got {self.side_value}.",
                )
            )

        if not self.asset.strip() or not self.family.strip():
            findings.append(
                AuditFinding(AuditSeverity.FAIL, "EDGE_IDENTITY_INCOMPLETE", "asset and family must be non-empty.")
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "EDGE_IDENTITY_OK",
                    f"Edge identity {self.edge_id!r} is side-specialized and valid.",
                )
            )

        return AuditReport(name="edge_identity", findings=tuple(findings))


@dataclass(frozen=True)
class EdgeRuntimeContract:
    required_functions: tuple[str, ...] = REQUIRED_EDGE_RUNTIME_FUNCTIONS
    live_compatible: bool = False

    def validate_runtime_namespace(self, runtime_namespace: Mapping[str, object]) -> AuditReport:
        findings: list[AuditFinding] = []
        missing = [fn for fn in self.required_functions if fn not in runtime_namespace]
        not_callable = [fn for fn in self.required_functions if fn in runtime_namespace and not callable(runtime_namespace[fn])]

        if missing:
            findings.append(
                AuditFinding(AuditSeverity.FAIL, "RUNTIME_FUNCTIONS_MISSING", f"Missing functions: {missing}")
            )

        if not_callable:
            findings.append(
                AuditFinding(AuditSeverity.FAIL, "RUNTIME_FUNCTIONS_NOT_CALLABLE", f"Not callable: {not_callable}")
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "RUNTIME_CONTRACT_OK",
                    "All required edge runtime functions are present and callable.",
                )
            )

        return AuditReport(name="edge_runtime_contract", findings=tuple(findings))


# =============================================================================
# Probability, edge manifest, and live manifest contracts
# =============================================================================

@dataclass(frozen=True)
class ProbabilityFlowContract:
    raw_probability_name: str = PROBABILITY_SPACE_RAW
    calibrated_probability_name: str = PROBABILITY_SPACE_CALIBRATED
    threshold_probability_space: str = PROBABILITY_SPACE_CALIBRATED

    def validate(self) -> AuditReport:
        findings: list[AuditFinding] = []

        if self.threshold_probability_space != PROBABILITY_SPACE_CALIBRATED:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "THRESHOLD_NOT_CALIBRATED_PROBABILITY",
                    "Threshold decisions must use calibrated_probability, not raw_probability.",
                )
            )

        if self.raw_probability_name == self.calibrated_probability_name:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "PROBABILITY_NAMES_COLLIDE",
                    "raw_probability and calibrated_probability must be separate fields.",
                )
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "PROBABILITY_FLOW_OK",
                    "Probability flow uses raw -> sigmoid calibrated -> AFML -> calibrated threshold decision.",
                )
            )

        return AuditReport(name="probability_flow_contract", findings=tuple(findings))


@dataclass(frozen=True)
class EdgeManifestContract:
    required_sections: tuple[str, ...] = REQUIRED_EDGE_MANIFEST_SECTIONS

    def validate_manifest_dict(self, manifest: Mapping[str, object]) -> AuditReport:
        findings: list[AuditFinding] = []
        missing = [section for section in self.required_sections if section not in manifest]

        if missing:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "EDGE_MANIFEST_SECTIONS_MISSING",
                    f"Missing required edge manifest sections: {missing}",
                )
            )

        live_compatibility = manifest.get("live_compatibility")
        if isinstance(live_compatibility, Mapping):
            status = live_compatibility.get("deployment_status")
            if status != DEPLOYMENT_PASS:
                findings.append(
                    AuditFinding(
                        AuditSeverity.FAIL,
                        "EDGE_MANIFEST_NOT_DEPLOYABLE",
                        f"Edge deployment_status must be PASS before allocator consumption, got {status!r}.",
                    )
                )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "EDGE_MANIFEST_CONTRACT_OK",
                    "Edge manifest has required sections and deployment_status=PASS.",
                )
            )

        return AuditReport(name="edge_manifest_contract", findings=tuple(findings))


@dataclass(frozen=True)
class LiveManifestContract:
    contract_version: str = LIVE_MANIFEST_CONTRACT_VERSION
    required_fields: tuple[str, ...] = REQUIRED_LIVE_MANIFEST_FIELDS

    def validate_manifest_dict(self, manifest: Mapping[str, object]) -> AuditReport:
        findings: list[AuditFinding] = []
        missing = [field for field in self.required_fields if field not in manifest]

        if missing:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "LIVE_MANIFEST_FIELDS_MISSING",
                    f"Missing required live manifest fields: {missing}",
                )
            )

        version = manifest.get("contract_version")
        if version != self.contract_version:
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "LIVE_MANIFEST_CONTRACT_VERSION_INVALID",
                    f"contract_version must be {self.contract_version!r}, got {version!r}.",
                )
            )

        alpha_legs = manifest.get("alpha_legs")
        if alpha_legs is not None and not isinstance(alpha_legs, list):
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "LIVE_MANIFEST_ALPHA_LEGS_NOT_LIST",
                    "alpha_legs must be a list.",
                )
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "LIVE_MANIFEST_CONTRACT_OK",
                    "Live manifest satisfies the V2 direct-weight calibrated-AFML contract shape.",
                )
            )

        return AuditReport(name="live_manifest_contract", findings=tuple(findings))


# =============================================================================
# Self-test
# =============================================================================

def _example_features() -> tuple[FeatureContractRow, ...]:
    return (
        FeatureContractRow(
            feature_name="Close_Return_1",
            owner_stage="full_feature_factory",
            dtype="float64",
            formula_description="One-bar close-to-close return computed only from historical OHLC bars.",
            required_inputs=("Close",),
            live_available=True,
            uses_future_information=False,
            allowed_for_model_input=True,
            missing_value_policy="drop_until_available",
        ),
        FeatureContractRow(
            feature_name="Time_Bucket_Code",
            owner_stage="model_row",
            dtype="int16",
            formula_description="Deterministic UTC time bucket derived from the event timestamp.",
            required_inputs=("Datetime",),
            live_available=True,
            uses_future_information=False,
            allowed_for_model_input=True,
            missing_value_policy="not_null",
        ),
    )


def run_self_test() -> None:
    reports = (
        BarContract().validate(),
        EdgeIdentity(asset="XAUUSD", family="trend", side_name="long", side_value=1).validate(),
        ProbabilityFlowContract().validate(),
        validate_selected_feature_rows(_example_features()),
        EdgeManifestContract().validate_manifest_dict(
            {
                "identity": {},
                "contracts": {},
                "artifacts": {},
                "runtime": {},
                "metrics": {},
                "threshold": {},
                "live_compatibility": {"deployment_status": "PASS"},
            }
        ),
        LiveManifestContract().validate_manifest_dict(
            {
                "contract_version": LIVE_MANIFEST_CONTRACT_VERSION,
                "created_at": "self_test",
                "selected_allocator_scenario": "self_test",
                "selected_variant": "self_test",
                "risk_formula": "direct_weight_calibrated_afml",
                "prop_challenge_rules": {},
                "alpha_legs": [],
                "frozen_bar_thresholds": {},
                "validation_summary": {},
            }
        ),
    )

    require_no_failures(reports)

    print("✅ shared.contracts self-test passed.")
    print(f"Contract version: {V2_CONTRACT_VERSION}")
    print(f"Live manifest contract: {LIVE_MANIFEST_CONTRACT_VERSION}")
    print(f"Required edge runtime functions: {', '.join(REQUIRED_EDGE_RUNTIME_FUNCTIONS)}")
    print(f"Required bar columns: {', '.join(REQUIRED_BAR_COLUMNS)}")


if __name__ == "__main__":
    try:
        run_self_test()
    except ContractValidationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
