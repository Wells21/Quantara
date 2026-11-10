from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.io.artifact_hash import sha256_file  # noqa: E402
from shared.prop_account_rules import (  # noqa: E402
    active_rule_contract,
    load_active_account_rules,
    structurally_equal,
)
from shared.live_payoff_contract import (  # noqa: E402
    LIVE_PAYOFF_CONTRACT_VERSION,
    validate_live_payoff_edge,
)
import live_settings as settings  # type: ignore  # noqa: E402

# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1

SCHEMA_VERSION = "v2.3"
PROBABILITY_COLUMN = "calibrated_probability"
RISK_CONTRACT = "direct_weight_calibrated_afml_broker_executable"
VALID_MANIFEST_TYPES = {"v2_initial_live_manifest", "v2_allocator_selected_live_manifest"}
NON_LIVE_STATUSES = {
    "BOOTSTRAP_NOT_LIVE_APPROVED",
    "LIVE_READY_NOT_ARMED",
    "LIVE_REVIEW_REQUIRED_NOT_ARMED",
    "DRY_RUN_ONLY_STRESS_PASS_NOT_ARMED",
    "DRY_RUN_ONLY_STRESS_REVIEW_REQUIRED",
}
LIVE_APPROVED_STATUS = "LIVE_APPROVED"
ACCOUNT_RULES = load_active_account_rules()
ACCOUNT_RULE_CONTRACT = active_rule_contract(ACCOUNT_RULES)
EXPECTED_ACCOUNT_PROFILE = str(ACCOUNT_RULES["profile_label"])
EXPECTED_ACCOUNT_STAGE = str(ACCOUNT_RULES["account_stage"])
EXPECTED_APPROVAL_SCOPE = (
    f"active_account_rules:{EXPECTED_ACCOUNT_PROFILE}:{EXPECTED_ACCOUNT_STAGE}"
)
EXPECTED_LIVE_APPROVAL_STATUS = (
    "APPROVED_FOR_ACTIVE_ACCOUNT_RULES_LIVE_MANIFEST_EXPORT"
)
EXPECTED_SYMBOL_MODE = str(
    ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"]
)
EXPECTED_SYMBOL_ENABLED = bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"])
HASH_VALIDATION_ENABLED = bool(
    getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False)
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()


def finding(items: list[dict[str, Any]], severity: str, code: str, message: str) -> None:
    items.append({"severity": severity, "code": code, "message": message})


def check_hash(path_text: str, expected_sha: str) -> tuple[bool, str]:
    path = V2_ROOT / path_text
    if not path.exists():
        return False, f"Missing file: {path_text}"
    if not HASH_VALIDATION_ENABLED:
        return True, f"Structural mode: current file exists; digest ignored for {path_text}"
    actual = sha256_file(path)
    if actual != expected_sha:
        return False, f"Hash mismatch for {path_text}: expected {expected_sha}, got {actual}"
    return True, f"Hash verified for {path_text}"


def check_hash_record(rows: list[dict[str, Any]], record: Any, code: str, label: str) -> None:
    if not HASH_VALIDATION_ENABLED:
        path_text = str(record.get("path") or "") if isinstance(record, dict) else ""
        if path_text:
            ok = (V2_ROOT / path_text).exists()
            finding(rows, "PASS" if ok else "FAIL", code, f"{label}: structural path exists={ok}: {path_text}")
        else:
            finding(rows, "PASS", code, f"{label}: digest record not required in structural mode")
        return
    if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
        finding(rows, "FAIL", f"{code}_MISSING", f"{label} hash record must contain path and sha256")
        return
    ok, msg = check_hash(str(record["path"]), str(record["sha256"]))
    finding(rows, "PASS" if ok else "FAIL", code, f"{label}: {msg}")


def validate_root_contracts(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    manifest_type = manifest.get("manifest_type")
    finding(rows, "PASS" if manifest.get("schema_version") == SCHEMA_VERSION else "FAIL", "SCHEMA_VERSION", f"schema_version={manifest.get('schema_version')}")
    finding(rows, "PASS" if manifest_type in VALID_MANIFEST_TYPES else "FAIL", "MANIFEST_TYPE", f"manifest_type={manifest_type}")
    finding(rows, "PASS" if manifest.get("threshold_probability_column") == PROBABILITY_COLUMN else "FAIL", "THRESHOLD_PROBABILITY_COLUMN", f"threshold_probability_column={manifest.get('threshold_probability_column')}")
    finding(rows, "PASS" if manifest.get("risk_contract_version") == RISK_CONTRACT else "FAIL", "RISK_CONTRACT", f"risk_contract_version={manifest.get('risk_contract_version')}")
    finding(
        rows,
        "PASS" if manifest.get("live_payoff_contract_version") == LIVE_PAYOFF_CONTRACT_VERSION else "FAIL",
        "LIVE_PAYOFF_CONTRACT",
        f"live_payoff_contract_version={manifest.get('live_payoff_contract_version')!r}",
    )

    selected_edges = manifest.get("selected_edges", [])
    finding(rows, "PASS" if isinstance(selected_edges, list) and selected_edges else "FAIL", "SELECTED_EDGES_PRESENT", f"selected_edges={len(selected_edges) if isinstance(selected_edges, list) else 'invalid'}")

    weights = manifest.get("portfolio_weights", {})
    if isinstance(weights, dict) and isinstance(selected_edges, list) and selected_edges:
        total_weight = sum(float(weights.get(edge.get("edge_id"), 0.0)) for edge in selected_edges)
        finding(rows, "PASS" if abs(total_weight - 1.0) <= 1e-9 else "FAIL", "PORTFOLIO_WEIGHTS_SUM_TO_ONE", f"total_weight={total_weight:.12f}")
    else:
        finding(rows, "FAIL", "PORTFOLIO_WEIGHTS_MISSING", "portfolio_weights must be an object")

    risk_caps = manifest.get("risk_caps", {})
    required_caps = ["base_portfolio_risk_budget_pct", "max_single_trade_risk_pct", "max_total_open_risk_pct", "max_asset_open_risk_pct", "max_family_open_risk_pct", "max_edge_open_risk_pct", "max_open_trades_per_asset", "max_open_trades_per_edge", "max_min_lot_risk_inflation"]
    missing_caps = [c for c in required_caps if c not in risk_caps]
    finding(rows, "PASS" if not missing_caps else "FAIL", "ROOT_RISK_CAPS", f"missing_caps={missing_caps}")

    source_hash = manifest.get("source_hash", {})
    if not HASH_VALIDATION_ENABLED:
        source_path = str(manifest.get("source") or (source_hash.get("path") if isinstance(source_hash, dict) else "") or "")
        ok = bool(source_path) and (V2_ROOT / source_path).exists()
        finding(rows, "PASS" if ok else "FAIL", "SOURCE_STRUCTURAL_PATH", f"source_path={source_path!r} exists={ok}")
    elif isinstance(source_hash, dict) and source_hash.get("path") and source_hash.get("sha256"):
        ok, msg = check_hash(str(source_hash["path"]), str(source_hash["sha256"]))
        finding(rows, "PASS" if ok else "FAIL", "SOURCE_HASH", msg)
    else:
        finding(rows, "FAIL", "SOURCE_HASH_MISSING", "source_hash must contain path and sha256")


def validate_active_account_contract(
    rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> None:
    checks = [
        (manifest.get("account_rule_profile") == EXPECTED_ACCOUNT_PROFILE, "ACCOUNT_RULE_PROFILE", f"account_rule_profile={manifest.get('account_rule_profile')!r}"),
        (manifest.get("account_stage") == EXPECTED_ACCOUNT_STAGE, "ACCOUNT_STAGE", f"account_stage={manifest.get('account_stage')!r}"),
        (manifest.get("approved_account_stage") == EXPECTED_ACCOUNT_STAGE, "APPROVED_ACCOUNT_STAGE", f"approved_account_stage={manifest.get('approved_account_stage')!r}"),
        (manifest.get("live_approval_scope") == EXPECTED_APPROVAL_SCOPE, "LIVE_APPROVAL_SCOPE", f"live_approval_scope={manifest.get('live_approval_scope')!r}"),
        (manifest.get("live_approval_status") == EXPECTED_LIVE_APPROVAL_STATUS, "LIVE_APPROVAL_STATUS", f"live_approval_status={manifest.get('live_approval_status')!r}"),
        (bool(manifest.get("daily_symbol_risk_rule_enabled", False)) == EXPECTED_SYMBOL_ENABLED, "DAILY_SYMBOL_RULE_ENABLED", f"daily_symbol_risk_rule_enabled={manifest.get('daily_symbol_risk_rule_enabled')!r}"),
        (manifest.get("daily_symbol_risk_enforcement_mode") == EXPECTED_SYMBOL_MODE, "SYMBOL_RISK_MODE", f"daily_symbol_risk_enforcement_mode={manifest.get('daily_symbol_risk_enforcement_mode')!r}"),
        (structurally_equal(manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT), "ACTIVE_ACCOUNT_RULES", "manifest active_account_rules structurally match the current JSON"),
    ]
    for ok, code, message in checks:
        finding(rows, "PASS" if ok else "FAIL", code, message)

    projected = manifest.get("projected_buffer_config") or {}
    required = [
        "trailing_buffer_admission_enabled",
        "trailing_buffer_reserve_mode",
        "trailing_buffer_fixed_reserve_amount",
        "trailing_buffer_cost_allowance_r",
    ]
    missing = [key for key in required if key not in projected]
    finding(
        rows,
        "PASS" if not missing else "FAIL",
        "PROJECTED_BUFFER_CONFIG",
        f"missing={missing}",
    )

    runtime_contract = manifest.get("live_runtime_contract") or {}
    runtime_ok = (
        runtime_contract.get("runtime_mode")
        == "authoritative_batch_shared_cache"
        and runtime_contract.get("native_one_row_certification_required") is False
        and runtime_contract.get("native_one_row_continuation_allowed") is False
        and runtime_contract.get("stateful_incremental_continuation_allowed") is False
    )
    finding(
        rows,
        "PASS" if runtime_ok else "FAIL",
        "AUTHORITATIVE_LIVE_RUNTIME_CONTRACT",
        f"live_runtime_contract={runtime_contract}",
    )

    live_rule = manifest.get("live_prop_rule_interpretation") or {}
    rule_ok = all(
        structurally_equal(live_rule.get(key), ACCOUNT_RULES.get(key))
        for key in [
            "overall_drawdown",
            "daily_drawdown",
            "minimum_trading_days",
            "maximum_risk_per_trade",
            "daily_symbol_risk",
            "floating_loss_guard",
            "operational_permissions",
        ]
    )
    finding(
        rows,
        "PASS" if rule_ok else "FAIL",
        "LIVE_PROP_RULE_INTERPRETATION",
        "live prop-rule interpretation matches active_account_rules.json",
    )


def validate_deployment_status(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    manifest_type = manifest.get("manifest_type")
    deployment_status = str(manifest.get("deployment_status", ""))
    live_ready = manifest.get("live_ready")
    live_trade_enabled = manifest.get("live_trade_enabled")
    stress_status = str(manifest.get("stress_validation_status", ""))

    if manifest_type == "v2_initial_live_manifest":
        finding(rows, "PASS" if deployment_status == "BOOTSTRAP_NOT_LIVE_APPROVED" else "FAIL", "BOOTSTRAP_STATUS", f"deployment_status={deployment_status}")
        finding(rows, "PASS" if live_ready is False else "FAIL", "BOOTSTRAP_NOT_LIVE_READY", "initial bootstrap manifest must keep live_ready=false")
        finding(rows, "PASS" if live_trade_enabled is False else "FAIL", "BOOTSTRAP_TRADING_DISABLED", "initial bootstrap manifest must keep live_trade_enabled=false")
        return

    if manifest_type == "v2_allocator_selected_live_manifest":
        if deployment_status == LIVE_APPROVED_STATUS:
            finding(rows, "PASS" if stress_status == "STRESS_PASS" else "FAIL", "LIVE_APPROVED_REQUIRES_STRESS_PASS", f"stress_validation_status={stress_status}")
            finding(rows, "PASS" if live_ready is True else "FAIL", "LIVE_APPROVED_READY", f"live_ready={live_ready}")
            finding(rows, "PASS" if live_trade_enabled is True else "FAIL", "LIVE_APPROVED_TRADING_ENABLED", f"live_trade_enabled={live_trade_enabled}")
        elif deployment_status in NON_LIVE_STATUSES:
            finding(rows, "PASS", "NON_LIVE_STATUS", f"deployment_status={deployment_status}")
            finding(rows, "PASS" if live_ready is False else "FAIL", "NON_LIVE_NOT_READY", f"live_ready={live_ready}")
            finding(rows, "PASS" if live_trade_enabled is False else "FAIL", "NON_LIVE_TRADING_DISABLED", f"live_trade_enabled={live_trade_enabled}")
            if stress_status != "STRESS_PASS":
                finding(rows, "PASS", "STRESS_REVIEW_LOCKS_LIVE_TRADING", f"stress_validation_status={stress_status}; live trading remains disabled")
        else:
            finding(rows, "FAIL", "UNKNOWN_DEPLOYMENT_STATUS", f"deployment_status={deployment_status}")

        blockers = manifest.get("live_blockers", [])
        if live_trade_enabled is False:
            finding(rows, "PASS" if isinstance(blockers, list) and blockers else "FAIL", "LIVE_BLOCKERS_PRESENT", f"blockers={len(blockers) if isinstance(blockers, list) else 'invalid'}")


def validate_sotd_contracts(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    contracts = manifest.get("sotd_contracts", {})
    checks = {
        "allocator_consumes_only_pass_edge_manifests": True,
        "dashboard_must_call_edge_runtime_score": True,
        "dashboard_may_engineer_edge_features": False,
        "execution_bot_must_consume_approved_signal_schema_only": True,
        "native_one_row_runtime_allowed": False,
    }
    for key, expected in checks.items():
        finding(rows, "PASS" if contracts.get(key) is expected else "FAIL", f"SOTD_{key.upper()}", f"{key}={contracts.get(key)} expected={expected}")
    if "raw_probability_may_not_select_final_threshold" in contracts:
        finding(rows, "PASS" if contracts.get("raw_probability_may_not_select_final_threshold") is True else "FAIL", "SOTD_RAW_PROBABILITY_NOT_FINAL_THRESHOLD", f"raw_probability_may_not_select_final_threshold={contracts.get('raw_probability_may_not_select_final_threshold')}")


def validate_edge(rows: list[dict[str, Any]], edge: dict[str, Any], root_status: str, root_live_trade_enabled: bool) -> None:
    edge_id = str(edge.get("edge_id", ""))
    finding(rows, "PASS" if edge_id else "FAIL", "EDGE_ID_PRESENT", f"edge_id={edge_id}")
    finding(rows, "PASS" if edge.get("threshold_probability_column") == PROBABILITY_COLUMN else "FAIL", "EDGE_THRESHOLD_COLUMN", f"{edge_id}: {edge.get('threshold_probability_column')}")
    finding(rows, "PASS" if edge.get("risk_contract_version") == RISK_CONTRACT else "FAIL", "EDGE_RISK_CONTRACT", f"{edge_id}: {edge.get('risk_contract_version')}")
    try:
        validated_payoff = validate_live_payoff_edge(edge)
        finding(
            rows, "PASS", "EDGE_LIVE_PAYOFF_CONTEXT",
            f"{edge_id}: payoff_context_id={validated_payoff['payoff_context_id']}",
        )
    except Exception as exc:
        finding(rows, "FAIL", "EDGE_LIVE_PAYOFF_CONTEXT", f"{edge_id}: {type(exc).__name__}: {exc}")

    try:
        threshold_ok = 0.0 <= float(edge.get("final_live_threshold")) <= 1.0
    except Exception:
        threshold_ok = False
    finding(rows, "PASS" if threshold_ok else "FAIL", "EDGE_FINAL_THRESHOLD", f"{edge_id}: final_live_threshold={edge.get('final_live_threshold')}")

    try:
        weight_ok = float(edge.get("portfolio_weight")) > 0
    except Exception:
        weight_ok = False
    finding(rows, "PASS" if weight_ok else "FAIL", "EDGE_PORTFOLIO_WEIGHT", f"{edge_id}: portfolio_weight={edge.get('portfolio_weight')}")

    finding(rows, "PASS" if edge.get("dashboard_may_engineer_edge_features") is not True else "FAIL", "DASHBOARD_FEATURE_ENGINEERING_FORBIDDEN", f"{edge_id}: dashboard feature engineering forbidden")

    runtime_contract = edge.get("dashboard_runtime_contract", {})
    if runtime_contract:
        required_runtime_flags = ["must_import_runtime_module", "must_call_build_features", "must_call_generate_candidate_events", "must_call_build_model_rows", "must_call_score"]
        missing = [flag for flag in required_runtime_flags if runtime_contract.get(flag) is not True]
        finding(rows, "PASS" if not missing else "FAIL", "EDGE_DASHBOARD_RUNTIME_CONTRACT", f"{edge_id}: missing_runtime_flags={missing}")
        authoritative_ok = (
            runtime_contract.get("runtime_mode") == "authoritative_batch_shared_cache"
            and runtime_contract.get("native_one_row_continuation_allowed") is False
            and runtime_contract.get("stateful_incremental_continuation_allowed") is False
        )
        finding(rows, "PASS" if authoritative_ok else "FAIL", "EDGE_AUTHORITATIVE_RUNTIME_MODE", f"{edge_id}: runtime_contract={runtime_contract}")

    if root_status in NON_LIVE_STATUSES:
        finding(rows, "PASS" if edge.get("live_trade_enabled") is False else "FAIL", "EDGE_NON_LIVE_TRADING_DISABLED", f"{edge_id}: live_trade_enabled={edge.get('live_trade_enabled')}")
    elif root_status == LIVE_APPROVED_STATUS:
        finding(rows, "PASS" if edge.get("live_trade_enabled") is root_live_trade_enabled else "FAIL", "EDGE_LIVE_APPROVED_TRADING_CONSISTENT", f"{edge_id}: edge_live={edge.get('live_trade_enabled')} root_live={root_live_trade_enabled}")

    manifest_file = edge.get("edge_manifest_path")
    manifest_sha = edge.get("edge_manifest_hash")
    if not HASH_VALIDATION_ENABLED:
        ok = bool(manifest_file) and (V2_ROOT / str(manifest_file)).exists()
        finding(rows, "PASS" if ok else "FAIL", "EDGE_MANIFEST_PATH", f"{edge_id}: path={manifest_file!r} exists={ok}")
        finding(rows, "PASS", "EDGE_ARTIFACT_DIGESTS_NOT_REQUIRED", f"{edge_id}: structural-current-files mode")
    elif manifest_file and manifest_sha:
        ok, msg = check_hash(str(manifest_file), str(manifest_sha))
        finding(rows, "PASS" if ok else "FAIL", "EDGE_MANIFEST_HASH", f"{edge_id}: {msg}")
    else:
        finding(rows, "FAIL", "EDGE_MANIFEST_HASH_MISSING", f"{edge_id}: edge manifest hash missing")

    artifact_hashes = edge.get("artifact_hashes")
    if HASH_VALIDATION_ENABLED:
        finding(rows, "PASS" if isinstance(artifact_hashes, dict) and bool(artifact_hashes) else "FAIL", "EDGE_ARTIFACT_HASHES_PRESENT", f"{edge_id}: artifact_hashes present={bool(artifact_hashes)}")

    edge_risk_caps = edge.get("risk_caps")
    if edge_risk_caps is not None:
        required_edge_caps = ["base_portfolio_risk_budget_pct", "max_single_trade_risk_pct", "max_total_open_risk_pct", "max_asset_open_risk_pct", "max_family_open_risk_pct", "max_edge_open_risk_pct", "max_open_trades_per_edge", "max_open_trades_per_asset", "max_min_lot_risk_inflation"]
        missing = [c for c in required_edge_caps if c not in edge_risk_caps]
        finding(rows, "PASS" if not missing else "FAIL", "EDGE_RISK_CAPS", f"{edge_id}: missing_caps={missing}")


def validate_allocator_source_hashes(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    source_hashes = manifest.get("allocator_source_hashes", {})
    required = {"active_edge_universe", "monte_carlo_stress_manifest", "stress_gate_results"}
    if not isinstance(source_hashes, dict):
        finding(rows, "FAIL", "ALLOCATOR_SOURCE_HASHES_MISSING", "allocator_source_hashes must be an object")
        return
    for key in sorted(required):
        record = source_hashes.get(key)
        check_hash_record(rows, record, f"ALLOCATOR_SOURCE_HASH_{key.upper()}", key)


def validate_source_contract_copy(
    rows: list[dict[str, Any]], manifest: dict[str, Any]
) -> None:
    source_text = str(manifest.get("source") or "").strip()
    if not source_text:
        finding(rows, "FAIL", "SOURCE_CONTRACT_PATH", "manifest source path is missing")
        return
    source_path = V2_ROOT / source_text
    try:
        source = load_json(source_path)
    except Exception as exc:
        finding(rows, "FAIL", "SOURCE_CONTRACT_READ", str(exc))
        return
    keys = [
        "account_rule_profile",
        "account_stage",
        "approved_account_stage",
        "live_approval_scope",
        "daily_reset_timezone",
        "daily_reset_offset_hours",
        "daily_symbol_risk_rule_enabled",
        "daily_symbol_risk_enforcement_mode",
        "risk_contract_version",
    ]
    for key in keys:
        finding(
            rows,
            "PASS" if manifest.get(key) == source.get(key) else "FAIL",
            f"SOURCE_COPY_{key.upper()}",
            f"manifest={manifest.get(key)!r} source={source.get(key)!r}",
        )
    finding(
        rows,
        "PASS"
        if structurally_equal(source.get("active_account_rules"), ACCOUNT_RULE_CONTRACT)
        and structurally_equal(manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT)
        else "FAIL",
        "SOURCE_COPY_ACTIVE_ACCOUNT_RULES",
        "source and live manifest structurally match active_account_rules.json",
    )


def validate_live_manifest(write_report: bool = True) -> dict[str, Any]:
    manifest_path = V2_ROOT / "portfolio_live" / "live_manifest.json"
    report_path = V2_ROOT / "portfolio_live" / "live_manifest_validation_report.json"
    rows: list[dict[str, Any]] = []

    try:
        manifest = load_json(manifest_path)
    except Exception as exc:
        report = {"schema_version": SCHEMA_VERSION, "created_at": now_utc(), "validation_status": "FAIL", "findings": [{"severity": "FAIL", "code": "LIVE_MANIFEST_LOAD_FAILED", "message": str(exc)}]}
        if write_report:
            write_json(report_path, report)
        return report

    validate_root_contracts(rows, manifest)
    validate_deployment_status(rows, manifest)
    if manifest.get("manifest_type") == "v2_allocator_selected_live_manifest":
        validate_active_account_contract(rows, manifest)
        validate_source_contract_copy(rows, manifest)
    validate_sotd_contracts(rows, manifest)
    if HASH_VALIDATION_ENABLED:
        validate_allocator_source_hashes(rows, manifest)
    else:
        finding(rows, "PASS", "ALLOCATOR_SOURCE_DIGESTS_SKIPPED", "Structural-current-files mode")

    selected_edges = manifest.get("selected_edges", [])
    if not isinstance(selected_edges, list):
        selected_edges = []

    seen: set[str] = set()
    for edge in selected_edges:
        edge_id = str(edge.get("edge_id", ""))
        if edge_id in seen:
            finding(rows, "FAIL", "DUPLICATE_EDGE", f"Duplicate selected edge: {edge_id}")
        seen.add(edge_id)
        validate_edge(rows, edge, str(manifest.get("deployment_status", "")), bool(manifest.get("live_trade_enabled")))

    live_execution = manifest.get("live_execution_contract", {})
    if live_execution:
        finding(rows, "PASS" if live_execution.get("execution_bot_must_validate_live_manifest_hash") is HASH_VALIDATION_ENABLED else "FAIL", "EXEC_CONTRACT_VALIDATE_LIVE_MANIFEST_HASH", f"value={live_execution.get('execution_bot_must_validate_live_manifest_hash')}")
        finding(rows, "PASS" if live_execution.get("execution_bot_must_validate_edge_manifest_hashes") is HASH_VALIDATION_ENABLED else "FAIL", "EXEC_CONTRACT_VALIDATE_EDGE_HASHES", f"value={live_execution.get('execution_bot_must_validate_edge_manifest_hashes')}")
        finding(rows, "PASS" if live_execution.get("execution_bot_must_consume_runtime_score_outputs_only") is True else "FAIL", "EXEC_CONTRACT_RUNTIME_OUTPUTS_ONLY", f"value={live_execution.get('execution_bot_must_consume_runtime_score_outputs_only')}")
        finding(rows, "PASS" if live_execution.get("execution_bot_must_validate_account_stage") is True else "FAIL", "EXEC_CONTRACT_ACCOUNT_STAGE", f"value={live_execution.get('execution_bot_must_validate_account_stage')}")
        finding(rows, "PASS" if live_execution.get("execution_bot_must_validate_risk_contract_version") is True else "FAIL", "EXEC_CONTRACT_RISK_VERSION", f"value={live_execution.get('execution_bot_must_validate_risk_contract_version')}")
        finding(rows, "PASS" if live_execution.get("execution_bot_must_validate_frozen_policy_manifest_hash") is HASH_VALIDATION_ENABLED else "FAIL", "EXEC_CONTRACT_POLICY_HASH", f"value={live_execution.get('execution_bot_must_validate_frozen_policy_manifest_hash')}")
        if manifest.get("live_trade_enabled") is False:
            finding(rows, "PASS" if live_execution.get("orders_allowed") is False else "FAIL", "EXEC_CONTRACT_NON_LIVE_ORDERS_DISABLED", f"orders_allowed={live_execution.get('orders_allowed')}")

    fail_count = sum(1 for row in rows if row["severity"] == "FAIL")
    warn_count = sum(1 for row in rows if row["severity"] == "WARN")
    pass_count = sum(1 for row in rows if row["severity"] == "PASS")
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "validation_status": "FAIL" if fail_count else "PASS",
        "manifest_path": rel(manifest_path),
        "live_manifest_sha256": sha256_file(manifest_path),  # audit only; never a gate when disabled
        "manifest_created_at": manifest.get("created_at"),
        "selected_edge_ids": sorted(str(edge.get("edge_id")) for edge in (manifest.get("selected_edges") or []) if edge.get("edge_id")),
        "artifact_hash_validation_enabled": HASH_VALIDATION_ENABLED,
        "manifest_type": manifest.get("manifest_type"),
        "approved_account_stage": manifest.get("approved_account_stage"),
        "risk_contract_version": manifest.get("risk_contract_version"),
        "deployment_status": manifest.get("deployment_status"),
        "live_ready": manifest.get("live_ready"),
        "live_trade_enabled": manifest.get("live_trade_enabled"),
        "summary": {"pass_count": pass_count, "warn_count": warn_count, "fail_count": fail_count},
        "findings": rows,
        "next_ordered_step_if_pass": "python ./portfolio_live/02_validate_live_environment.py",
    }
    if write_report:
        write_json(report_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = validate_live_manifest(write_report=not args.dry_run)
    summary = report.get("summary", {})
    print("V2 Portfolio Live 01 — validate live manifest")
    print(f"Status: {report['validation_status']}")
    print(f"Manifest type: {report.get('manifest_type')}")
    print(f"Deployment status: {report.get('deployment_status')}")
    print(f"Live ready: {report.get('live_ready')}")
    print(f"Live trade enabled: {report.get('live_trade_enabled')}")
    print(f"Pass: {summary.get('pass_count', 0)} | Warn: {summary.get('warn_count', 0)} | Fail: {summary.get('fail_count', 0)}")
    for row in report.get("findings", []):
        if row["severity"] != "PASS":
            print(f"{row['severity']} {row['code']}: {row['message']}")
    if report["validation_status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
