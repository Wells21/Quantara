from __future__ import annotations

# QUANTARA_V3_PHASE3C_SEMANTIC_GENERATION_CLOSURE_V1
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (LIVE_DIR, V2_ROOT, REPO_ROOT):
    s = str(p)
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)

import live_settings as settings  # type: ignore  # noqa: E402
from shared.edges.trade_geometry import (  # noqa: E402
    GEOMETRY_COLUMNS,
    geometry_from_mapping,
    load_trade_geometry,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.io.artifact_hash import hash_artifact  # noqa: E402
from shared.prop_account_rules import (  # noqa: E402
    active_rule_contract,
    load_active_account_rules,
    structurally_equal,
)
from shared.live_payoff_contract import (  # noqa: E402
    LIVE_PAYOFF_CONTRACT_VERSION,
    freeze_live_payoff_from_representative,
)
from shared.validation.v3_semantic_contract import PAYOFF_CONTEXT_VERSION  # noqa: E402
if str(V2_ROOT / "portfolio_allocator") not in sys.path:
    sys.path.insert(0, str(V2_ROOT / "portfolio_allocator"))
import afml_concurrency_sizing as AFMLC  # noqa: E402  (AFML Ch.10.2/10.4 concurrency-aware sizing, 2026-10-17)

# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1
SCHEMA_VERSION = "v2.3"
PROBABILITY_COLUMN = "calibrated_probability"
RISK_CONTRACT = "direct_weight_calibrated_afml_broker_executable"
LIVE_MODE = str(getattr(settings, "LIVE_MODE", "DRY_RUN_ONLY"))
ALLOW_REAL_LIVE_TRADING_EXPORT = bool(getattr(settings, "ALLOW_REAL_LIVE_TRADING_EXPORT", False))
HASH_VALIDATION_ENABLED = bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False))
CONTRACT_IDENTITY_MODE = str(getattr(settings, "LIVE_CONTRACT_IDENTITY_MODE", "structural_current_files_no_hash_gate"))

INPUT_ACTIVE_EDGE_UNIVERSE = V2_ROOT / "portfolio_allocator" / "outputs" / "active_edge_universe.json"
INPUT_REPRESENTATIVE_CANDIDATES = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_threshold_candidates_all.csv"
INPUT_STRESS_CONFIG = V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs" / "candidate_stress_validated_live_config.json"
INPUT_STRESS_MANIFEST = V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs" / "monte_carlo_stress_manifest.json"
INPUT_STRESS_GATE_REPORT = V2_ROOT / "portfolio_allocator" / "outputs" / "monte_carlo_stress_outputs" / "stress_gate_results.csv"
OUTPUT_LIVE_MANIFEST = V2_ROOT / "portfolio_live" / "live_manifest.json"
OUTPUT_DEPLOYMENT_MANIFEST = V2_ROOT / "portfolio_live" / "live_deployment_manifest.json"
OUTPUT_ALPHA_SUMMARY = V2_ROOT / "portfolio_live" / "live_manifest_alpha_summary.csv"

NON_LIVE_STRESS_PASS_STATUS = "LIVE_READY_NOT_ARMED"
NON_LIVE_REVIEW_STATUS = "LIVE_REVIEW_REQUIRED_NOT_ARMED"
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
LOCAL_ACCOUNT_STAGE = str(
    getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "") or ""
).strip().lower()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns = [
        "edge_id", "asset", "family", "side_name", "side_value", "representative_id",
        "final_live_threshold", "portfolio_weight", "base_portfolio_risk_budget_pct",
        "max_single_trade_risk_pct", "live_trade_enabled", "deployment_status",
        "edge_manifest_hash", "runtime_module", *GEOMETRY_COLUMNS,
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def rel(path: Path) -> str:
    return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()


def artifact_record(path: Path) -> dict[str, Any]:
    return hash_artifact(path, root=V2_ROOT).to_dict()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def optional_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    out = float(value)
    if out <= 0:
        raise ValueError(f"Expected positive optional float, got {value!r}")
    return out


def current_geometry_for_active_edge(active_edge: dict[str, Any]) -> dict[str, Any]:
    edge_id = str(active_edge.get("edge_id", ""))
    side_name = str(active_edge.get("side_name", ""))
    embedded = geometry_from_mapping(active_edge, expected_side_name=side_name)
    edge_yaml_rel = str(active_edge.get("edge_yaml_path", "")).strip()
    require(edge_yaml_rel, f"{edge_id}: active edge is missing edge_yaml_path")
    edge_yaml = V2_ROOT / edge_yaml_rel
    current = load_trade_geometry(
        edge_yaml,
        expected_edge_id=edge_id,
        expected_side_name=side_name,
    )
    if HASH_VALIDATION_ENABLED:
        require(
            embedded["geometry_hash"] == current["geometry_hash"],
            f"{edge_id}: allocator-ingested geometry no longer matches current edge.yaml",
        )
    else:
        # The current edge.yaml is authoritative in structural mode. Explicit
        # geometry values are copied from it and validated downstream.
        _ = embedded
    expected_yaml_hash = str(active_edge.get("edge_yaml_hash", "")).strip()
    if HASH_VALIDATION_ENABLED and expected_yaml_hash:
        require(
            artifact_record(edge_yaml)["sha256"] == expected_yaml_hash,
            f"{edge_id}: current edge.yaml hash differs from allocator-ingested hash",
        )
    return current


def load_active_edges() -> dict[str, dict[str, Any]]:
    payload = load_json(INPUT_ACTIVE_EDGE_UNIVERSE)
    require(payload.get("ingestion_status") == "PASS", "active_edge_universe ingestion_status must be PASS")
    active_edges = payload.get("active_edges", [])
    require(isinstance(active_edges, list) and active_edges, "active_edge_universe must contain active_edges")
    out: dict[str, dict[str, Any]] = {}
    for edge in active_edges:
        edge_id = str(edge.get("edge_id", ""))
        require(edge_id, "active edge missing edge_id")
        require(edge_id not in out, f"Duplicate active edge_id: {edge_id}")
        current_geometry_for_active_edge(edge)
        out[edge_id] = edge
    return out


def load_representative_candidates() -> dict[str, dict[str, Any]]:
    require(
        INPUT_REPRESENTATIVE_CANDIDATES.exists(),
        f"Missing allocator 04C representative file: {INPUT_REPRESENTATIVE_CANDIDATES}",
    )
    with INPUT_REPRESENTATIVE_CANDIDATES.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    require(bool(rows), "Allocator 04C representative file is empty.")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        rep_id = str(row.get("representative_id", "")).strip()
        edge_id = str(row.get("edge_id", "")).strip()
        require(rep_id, "04C representative row missing representative_id")
        require(edge_id, f"{rep_id}: 04C representative row missing edge_id")
        require(rep_id not in out, f"Duplicate 04C representative_id: {rep_id}")
        require(str(row.get("payoff_context_id", "")).strip(), f"{rep_id}: missing payoff_context_id")
        out[rep_id] = row
    return out


def normalize_weight_rows(stress_config: dict[str, Any]) -> list[dict[str, Any]]:
    weights = stress_config.get("selected_hrp_weights", [])
    require(isinstance(weights, list) and weights, "candidate_stress_validated_live_config.selected_hrp_weights must be non-empty")
    total = sum(float(row.get("weight", 0.0)) for row in weights)
    require(total > 0, "selected HRP weights must sum to > 0")
    rows = []
    seen: set[str] = set()
    for row in weights:
        edge_id = str(row.get("edge_id", ""))
        require(edge_id, "selected HRP weight row missing edge_id")
        require(edge_id not in seen, f"Duplicate selected edge in HRP weights: {edge_id}")
        seen.add(edge_id)
        threshold = row.get("threshold")
        require(threshold is not None, f"{edge_id}: selected HRP weight missing threshold")
        payoff_context_id = str(row.get("payoff_context_id", "")).strip()
        payoff_context_version = str(row.get("payoff_context_version", "")).strip()
        require(payoff_context_id, f"{edge_id}: selected HRP weight missing payoff_context_id")
        require(
            payoff_context_version == PAYOFF_CONTEXT_VERSION,
            f"{edge_id}: selected HRP weight payoff_context_version is stale",
        )
        rows.append({
            **row,
            "weight": float(row.get("weight", 0.0)) / total,
            "threshold": float(threshold),
            "payoff_context_id": payoff_context_id,
            "payoff_context_version": payoff_context_version,
        })
    return rows


def validate_stress_gate_csv(path: Path) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    require(bool(rows), "stress_gate_results.csv is empty")
    mandatory = [row for row in rows if str(row.get("validation_class", "")).strip().lower() == "mandatory"]
    require(bool(mandatory), "stress_gate_results.csv contains no mandatory gate rows")
    failed = [row.get("scenario_name", "unknown") for row in mandatory if str(row.get("gate_passed", "")).strip().lower() not in {"true", "1", "yes"}]
    require(not failed, f"Mandatory Script 11 stress gates failed: {failed}")


def deployment_status_from_gates(
    stress_config: dict[str, Any],
) -> tuple[str, bool, bool, list[str]]:
    stress_status = str(stress_config.get("stress_validation_status", "")).upper()
    blockers: list[str] = []
    stress_rules = stress_config.get("active_account_rules")
    checks = [
        (stress_status == "STRESS_PASS", f"Monte Carlo stress validation is not STRESS_PASS: {stress_status}"),
        (str(stress_config.get("live_approval_status")) == EXPECTED_LIVE_APPROVAL_STATUS, f"live_approval_status={stress_config.get('live_approval_status')!r}"),
        (str(stress_config.get("account_rule_profile")) == EXPECTED_ACCOUNT_PROFILE, f"account_rule_profile={stress_config.get('account_rule_profile')!r}"),
        (str(stress_config.get("account_stage")) == EXPECTED_ACCOUNT_STAGE, f"account_stage={stress_config.get('account_stage')!r}"),
        (str(stress_config.get("approved_account_stage")) == EXPECTED_ACCOUNT_STAGE, f"approved_account_stage={stress_config.get('approved_account_stage')!r}"),
        (str(stress_config.get("live_approval_scope")) == EXPECTED_APPROVAL_SCOPE, f"live_approval_scope={stress_config.get('live_approval_scope')!r}"),
        (structurally_equal(stress_rules, ACCOUNT_RULE_CONTRACT), "stress output active_account_rules differs from active_account_rules.json"),
        (bool(stress_config.get("daily_symbol_risk_rule_enabled", False)) == EXPECTED_SYMBOL_ENABLED, f"daily_symbol_risk_rule_enabled={stress_config.get('daily_symbol_risk_rule_enabled')!r}"),
        (str(stress_config.get("daily_symbol_risk_enforcement_mode")) == EXPECTED_SYMBOL_MODE, f"daily_symbol_risk_enforcement_mode={stress_config.get('daily_symbol_risk_enforcement_mode')!r}"),
        (str(stress_config.get("risk_contract_version")) == RISK_CONTRACT, f"risk_contract_version={stress_config.get('risk_contract_version')!r}"),
        (not LOCAL_ACCOUNT_STAGE or LOCAL_ACCOUNT_STAGE == EXPECTED_ACCOUNT_STAGE, f"PORTFOLIO_ACCOUNT_STAGE={LOCAL_ACCOUNT_STAGE!r}; expected {EXPECTED_ACCOUNT_STAGE!r}"),
    ]
    blockers.extend(message for ok, message in checks if not ok)
    if LIVE_MODE != "REAL_LIVE":
        blockers.append(f"LIVE_MODE={LIVE_MODE}; real-live export is not armed")
    if not ALLOW_REAL_LIVE_TRADING_EXPORT:
        blockers.append("ALLOW_REAL_LIVE_TRADING_EXPORT=False")
    live_ready = not blockers
    if live_ready:
        return LIVE_APPROVED_STATUS, True, True, []
    if stress_status == "STRESS_PASS":
        return NON_LIVE_STRESS_PASS_STATUS, False, False, blockers
    return NON_LIVE_REVIEW_STATUS, False, False, blockers


BROKER_REPRICING_CONFIG = V2_ROOT / "portfolio_allocator" / "config" / "broker_repricing.json"


def per_trade_cost_guard_block(edge_id: str, representative_row: dict[str, Any]) -> dict[str, Any] | None:
    """RUNBOOK v5.1 (ADDED 2026-10-16): live per-trade cost guard inputs, ONLY when the allocator ran with the current
    firm's re-priced trades (portfolio_allocator/config/broker_repricing.json enabled + 00d summary for this edge).
    expected_net_r = the allocator's expectancy at the selected threshold (net of the firm's modelled costs);
    modelled_spread_r = mean round-trip spread cost per trade in R that this expectancy already paid (00d).
    Live: skip a trade when (current spread / its stop distance) - modelled_spread_r exceeds expected_net_r.
    Returns None otherwise -> the bot's guard is a no-op and the manifest is unchanged."""
    if not BROKER_REPRICING_CONFIG.exists():
        return None
    cfg = load_json(BROKER_REPRICING_CONFIG)
    if not cfg.get("enabled"):
        return None
    firm = str(cfg["firm"])
    summary_path = V2_ROOT / "portfolio_allocator" / "outputs" / "broker_repricing" / firm / "reprice_summary.json"
    require(summary_path.exists(), f"broker re-pricing enabled but 00d summary missing: {summary_path}")
    rec = load_json(summary_path).get("edges", {}).get(edge_id)
    require(isinstance(rec, dict) and "error" not in rec and rec.get("mean_spread_R") is not None,
            f"{edge_id}: no usable 00d re-pricing record for firm {firm!r}")
    expected = representative_row.get("expectancy_r")
    require(expected not in (None, ""), f"{edge_id}: representative expectancy_r missing")
    return {"enabled": True, "firm": firm, "expected_net_r": float(expected), "modelled_spread_r": float(rec["mean_spread_R"]),
            "min_net_edge_r": 0.0, "source": rel(summary_path),
            "rule": "skip entry if (live_spread_price / stop_distance_price) - modelled_spread_r > expected_net_r - min_net_edge_r"}


INPUT_REPRESENTATIVE_LEDGER = V2_ROOT / "portfolio_allocator" / "outputs" / "v2_representative_trade_ledger_core_plus_watchlist.csv"
_AFML_DIVISOR_CACHE: dict[float, dict[str, float]] = {}


def afml_concurrency_divisor(edge_id: str, variant: dict[str, Any]) -> tuple[float, float]:
    """(quantile, divisor) for one edge. AFML Ch.10.2/10.4: divisor = q-quantile of the edge's concurrent active signals,
    derived from the SAME representative ledger and filters (exit > entry, afml_bet_size > 0) that Steps 08-11 simulate,
    so live sizing reproduces the allocator exactly. Quantile 0 -> (0.0, 1.0): sizing unchanged."""
    q = AFMLC.validate_quantile(variant.get("afml_concurrency_quantile"))
    if not AFMLC.is_enabled(q):
        return 0.0, 1.0
    if q not in _AFML_DIVISOR_CACHE:
        import pandas as pd
        require(INPUT_REPRESENTATIVE_LEDGER.exists(), f"Missing {INPUT_REPRESENTATIVE_LEDGER} for AFML concurrency divisors")
        led = pd.read_csv(INPUT_REPRESENTATIVE_LEDGER, usecols=["edge_id", "entry_time", "exit_time", "afml_bet_size"], low_memory=False)
        led["entry_time"] = to_datetime_iso(led["entry_time"], utc=True, errors="coerce")
        led["exit_time"] = to_datetime_iso(led["exit_time"], utc=True, errors="coerce")
        led["afml_bet_size"] = pd.to_numeric(led["afml_bet_size"], errors="coerce").clip(lower=0.0, upper=1.0)
        led = led.dropna(subset=["entry_time", "exit_time", "afml_bet_size"])
        led = led[(led["exit_time"] > led["entry_time"]) & (led["afml_bet_size"] > 0.0)]
        _AFML_DIVISOR_CACHE[q] = AFMLC.divisors_by_edge_id(
            led["edge_id"].astype(str).to_numpy(), led["entry_time"].astype("int64").to_numpy(),
            led["exit_time"].astype("int64").to_numpy(), q)
    div = _AFML_DIVISOR_CACHE[q].get(edge_id)
    require(div is not None and div >= 1.0, f"{edge_id}: no AFML concurrency divisor in the representative ledger")
    return q, float(div)


def _optional_cost_guard(edge_id: str, representative_row: dict[str, Any]) -> dict[str, Any]:
    block = per_trade_cost_guard_block(edge_id, representative_row)
    return {"per_trade_cost_guard": block} if block else {}


def build_live_edge(weight_row: dict[str, Any], active_edge: dict[str, Any], representative_row: dict[str, Any], stress_config: dict[str, Any], deployment_status: str, live_trade_enabled: bool, live_blockers: list[str]) -> dict[str, Any]:
    variant = stress_config["selected_variant"]
    edge_id = str(weight_row["edge_id"])
    final_threshold = float(weight_row["threshold"])
    portfolio_weight = float(weight_row["weight"])
    geometry = current_geometry_for_active_edge(active_edge)
    live_payoff = freeze_live_payoff_from_representative(
        representative_row,
        edge_id=edge_id,
        threshold=final_threshold,
        geometry_hash=str(geometry["geometry_hash"]),
    )

    # Current active edge + current edge.yaml are authoritative. Upstream HRP/
    # stress digests are historical audit metadata and cannot block deployment.
    row_geometry_hash = str(weight_row.get("geometry_hash", "")).strip()
    if HASH_VALIDATION_ENABLED and row_geometry_hash:
        require(
            row_geometry_hash == geometry["geometry_hash"],
            f"{edge_id}: stress/HRP geometry hash differs from finalized active-edge geometry",
        )
    row_manifest_hash = str(weight_row.get("edge_manifest_hash", "")).strip()
    if HASH_VALIDATION_ENABLED and row_manifest_hash:
        require(
            row_manifest_hash == str(active_edge.get("edge_manifest_hash", "")),
            f"{edge_id}: stress/HRP edge manifest hash differs from active PASS edge manifest",
        )

    trade_barrier_config = {
        "geometry_contract_version": geometry["geometry_contract_version"],
        "geometry_hash": geometry["geometry_hash"],
        "pt_multiplier": geometry["pt_multiplier"],
        "sl_multiplier": geometry["sl_multiplier"],
        "reward_risk_ratio": geometry["reward_risk_ratio"],
        "vertical_horizon_bars": geometry["vertical_horizon_bars"],
        # REVISED 2026-09-25 (calendar-time labeling rollout): additive
        # pass-through, see shared/edges/trade_geometry.py. Defaults to
        # False/None for every edge that doesn't set
        # labeling.vertical_horizon_hours in edge.yaml -- zero behavior
        # change for edges not part of this rollout.
        "calendar_time": geometry.get("calendar_time", False),
        "vertical_horizon_hours": geometry.get("vertical_horizon_hours"),
        "entry_timing": geometry["entry_timing"],
        "barrier_reference": geometry["barrier_reference"],
        "barrier_reference_units": geometry["barrier_reference_units"],
        "tie_policy": geometry["tie_policy"],
        "tick_resolution_price": geometry["tick_resolution_price"],
        "barrier_contract": geometry["barrier_contract"],
    }
    return {
        "edge_id": edge_id,
        "asset": active_edge["asset"],
        "family": active_edge["family"],
        "side_name": active_edge["side_name"],
        "side_value": active_edge["side_value"],
        "edge_root": active_edge["edge_root"],
        "runtime_module": active_edge["runtime_module"],
        "representative_id": weight_row.get("representative_id", ""),
        **geometry,
        "trade_geometry": geometry,
        "trade_barrier_config": trade_barrier_config,
        "final_live_threshold": final_threshold,
        "live_payoff_contract_version": LIVE_PAYOFF_CONTRACT_VERSION,
        "payoff_context_version": live_payoff["payoff_context_version"],
        "payoff_context_id": live_payoff["payoff_context_id"],
        "live_afml_sizing_method": live_payoff["afml_sizing_method"],
        "live_payoff_profile": live_payoff["payoff_profile"],
        "live_payoff_context": live_payoff["payoff_context"],
        "live_payoff_source": rel(INPUT_REPRESENTATIVE_CANDIDATES),
        "threshold_probability_column": PROBABILITY_COLUMN,
        "threshold_source": "allocator_hrp_selected_representative_threshold",
        "threshold_probability_contract": {
            "approved_probability_column": PROBABILITY_COLUMN,
            "raw_probability_allowed_for_final_decision": False,
            "decision_rule": "approved_signal = calibrated_probability >= final_live_threshold",
        },
        "portfolio_weight": portfolio_weight,
        "risk_contract_version": RISK_CONTRACT,
        "account_stage": stress_config.get("account_stage"),
        "approved_account_stage": stress_config.get("approved_account_stage"),
        "live_approval_scope": stress_config.get("live_approval_scope"),
        "daily_symbol_risk_rule_enabled": bool(stress_config.get("daily_symbol_risk_rule_enabled", False)),
        "risk_sizing_formula": "trade_risk_pct = min(base_portfolio_risk_budget_pct * portfolio_weight * calibrated_afml_bet_size * throttle_multiplier * target_lock_multiplier / afml_concurrency_divisor, max_single_trade_risk_pct)",
        "risk_caps": {
            # AFML Ch.10.2/10.4 concurrency-aware sizing (1.0 = off).
            "afml_concurrency_quantile": afml_concurrency_divisor(edge_id, variant)[0],
            "afml_concurrency_divisor": afml_concurrency_divisor(edge_id, variant)[1],
            "base_portfolio_risk_budget_pct": float(variant["base_portfolio_risk_budget_pct"]),
            "max_single_trade_risk_pct": float(variant["max_single_trade_risk_pct"]),
            "max_total_open_risk_pct": float(variant["max_total_open_risk_pct"]),
            "max_asset_open_risk_pct": float(variant["max_asset_open_risk_pct"]),
            "max_family_open_risk_pct": float(variant["max_family_open_risk_pct"]),
            "max_edge_open_risk_pct": float(variant["max_edge_open_risk_pct"]),
            "max_open_trades_per_edge": int(variant["max_open_trades_per_edge"]),
            "max_open_trades_per_asset": int(variant["max_open_trades_per_asset"]),
            "max_min_lot_risk_inflation": optional_positive_float(variant.get("max_min_lot_risk_inflation")),
        },
        "throttle_config": {
            "name": variant.get("throttle_name", ""),
            "throttle_tiers": variant.get("throttle_tiers", []),
            "pause_below_balance": float(variant["pause_below_balance"]),
        },
        "target_lock_config": {
            "name": variant.get("target_lock_name", ""),
            "target_lock_tiers": variant.get("target_lock_tiers", []),
        },
        "projected_buffer_config": {
            "trailing_buffer_admission_enabled": bool(variant.get("trailing_buffer_admission_enabled", False)),
            "trailing_buffer_reserve_mode": str(variant.get("trailing_buffer_reserve_mode", "off")),
            "trailing_buffer_fixed_reserve_amount": float(variant.get("trailing_buffer_fixed_reserve_amount", 0.0) or 0.0),
            "trailing_buffer_cost_allowance_r": float(variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0),
            "trailing_buffer_admission_policy": str(variant.get("trailing_buffer_admission_policy", "off")),
        },
        "edge_manifest_path": active_edge["edge_manifest_path"],
        "edge_manifest_hash": active_edge["edge_manifest_hash"],
        "edge_manifest_hash_record": active_edge.get("edge_manifest_hash_record", {}),
        "edge_yaml_path": active_edge.get("edge_yaml_path", ""),
        "edge_yaml_hash": active_edge.get("edge_yaml_hash", ""),
        "artifact_hashes": active_edge.get("artifact_hashes", {}),
        "artifacts": active_edge.get("artifacts", {}),
        "selected_for_live": True,
        "deployment_status": deployment_status,
        "live_trade_enabled": live_trade_enabled,
        "live_trade_blocker": "" if live_trade_enabled else "; ".join(live_blockers),
        **_optional_cost_guard(edge_id, representative_row),
        "dashboard_may_engineer_edge_features": False,
        "dashboard_runtime_contract": {
            "must_import_runtime_module": True,
            "must_call_build_features": True,
            "must_call_generate_candidate_events": True,
            "must_call_build_model_rows": True,
            "must_call_score": True,
            "manual_edge_feature_engineering_allowed": False,
            "runtime_mode": "authoritative_batch_shared_cache",
            "native_one_row_continuation_allowed": False,
            "stateful_incremental_continuation_allowed": False,
        },
    }


def build_live_manifest(write: bool = True) -> dict[str, Any]:
    active = load_active_edges()
    stress_config = load_json(INPUT_STRESS_CONFIG)
    stress_manifest = load_json(INPUT_STRESS_MANIFEST)
    require(INPUT_STRESS_GATE_REPORT.exists(), f"Missing mandatory stress gate report: {INPUT_STRESS_GATE_REPORT}")
    validate_stress_gate_csv(INPUT_STRESS_GATE_REPORT)
    require("selected_variant" in stress_config, "stress config missing selected_variant")
    require(stress_manifest.get("stress_validation_status") == stress_config.get("stress_validation_status"), "stress config/manifest status mismatch")
    require(stress_manifest.get("selected_variant_label") == stress_config.get("selected_variant_label"), "stress config/manifest selected label mismatch")
    require(stress_manifest.get("approved_account_stage") == stress_config.get("approved_account_stage"), "stress config/manifest approved stage mismatch")
    require(stress_manifest.get("live_approval_scope") == stress_config.get("live_approval_scope"), "stress config/manifest approval scope mismatch")
    require(stress_manifest.get("live_approval_status") == stress_config.get("live_approval_status"), "stress config/manifest live approval mismatch")
    require("source_scenario" in stress_config, "stress config missing source_scenario")
    require(
        structurally_equal(stress_config.get("active_account_rules"), ACCOUNT_RULE_CONTRACT),
        "stress config active_account_rules differs from active_account_rules.json",
    )
    require(
        structurally_equal(stress_manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT),
        "stress manifest active_account_rules differs from active_account_rules.json",
    )
    weight_rows = normalize_weight_rows(stress_config)
    representative_lookup = load_representative_candidates()
    deployment_status, live_ready, live_trade_enabled, blockers = deployment_status_from_gates(stress_config)
    selected_edges = []
    for row in weight_rows:
        edge_id = str(row["edge_id"])
        require(edge_id in active, f"Selected edge is not in active PASS edge universe: {edge_id}")
        representative_id = str(row.get("representative_id", "")).strip()
        require(representative_id, f"{edge_id}: stress/HRP row missing representative_id")
        require(
            representative_id in representative_lookup,
            f"{edge_id}: selected representative_id not found in allocator 04C output: {representative_id}",
        )
        representative_row = representative_lookup[representative_id]
        require(
            str(representative_row.get("edge_id", "")).strip() == edge_id,
            f"{edge_id}: 04C representative belongs to another edge",
        )
        require(
            str(representative_row.get("payoff_context_id", "")).strip()
            == str(row.get("payoff_context_id", "")).strip(),
            f"{edge_id}: stress-tested payoff_context_id differs from current 04C representative",
        )
        require(
            str(representative_row.get("payoff_context_version", "")).strip()
            == str(row.get("payoff_context_version", "")).strip()
            == PAYOFF_CONTEXT_VERSION,
            f"{edge_id}: stress-tested payoff_context_version differs from current 04C representative",
        )
        selected_edges.append(build_live_edge(
            row, active[edge_id], representative_row, stress_config,
            deployment_status, live_trade_enabled, blockers,
        ))
    total_weight = sum(float(edge["portfolio_weight"]) for edge in selected_edges)
    require(abs(total_weight - 1.0) <= 1e-9, f"Selected live edge weights must sum to 1.0, got {total_weight}")
    variant = stress_config["selected_variant"]
    source_scenario = stress_config["source_scenario"]
    root_risk_caps = {
        "afml_concurrency_quantile": AFMLC.validate_quantile(variant.get("afml_concurrency_quantile")),
        "base_portfolio_risk_budget_pct": float(variant["base_portfolio_risk_budget_pct"]),
        "max_single_trade_risk_pct": float(variant["max_single_trade_risk_pct"]),
        "max_total_open_risk_pct": float(variant["max_total_open_risk_pct"]),
        "max_asset_open_risk_pct": float(variant["max_asset_open_risk_pct"]),
        "max_family_open_risk_pct": float(variant["max_family_open_risk_pct"]),
        "max_edge_open_risk_pct": float(variant["max_edge_open_risk_pct"]),
        "max_open_trades_per_edge": int(variant["max_open_trades_per_edge"]),
        "max_open_trades_per_asset": int(variant["max_open_trades_per_asset"]),
        "max_min_lot_risk_inflation": optional_positive_float(variant.get("max_min_lot_risk_inflation")),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": "v2_allocator_selected_live_manifest",
        "created_at": now_utc(),
        "deployment_status": deployment_status,
        "live_mode": LIVE_MODE,
        "allow_real_live_trading_export": ALLOW_REAL_LIVE_TRADING_EXPORT,
        "live_ready": live_ready,
        "live_trade_enabled": live_trade_enabled,
        "stress_validation_status": stress_config.get("stress_validation_status"),
        "live_approval_status": stress_config.get("live_approval_status"),
        "artifact_policy": "hashless_structural_account_rules",
        "account_rule_profile": EXPECTED_ACCOUNT_PROFILE,
        "account_stage": EXPECTED_ACCOUNT_STAGE,
        "approved_account_stage": EXPECTED_ACCOUNT_STAGE,
        "live_approval_scope": EXPECTED_APPROVAL_SCOPE,
        "active_account_rules": ACCOUNT_RULE_CONTRACT,
        "active_account_rule_contract": stress_config.get("active_account_rule_contract", {}),
        "daily_reset_timezone": stress_config.get("daily_reset_timezone", "UTC"),
        "daily_reset_offset_hours": float(stress_config.get("daily_reset_offset_hours", 0.0) or 0.0),
        "daily_symbol_risk_rule_enabled": EXPECTED_SYMBOL_ENABLED,
        "daily_symbol_risk_enforcement_mode": EXPECTED_SYMBOL_MODE,
        "source": rel(INPUT_STRESS_CONFIG),
        "allocator_sources": {
            "active_edge_universe": rel(INPUT_ACTIVE_EDGE_UNIVERSE),
            "representative_threshold_candidates_04c": rel(INPUT_REPRESENTATIVE_CANDIDATES),
            "monte_carlo_stress_manifest": rel(INPUT_STRESS_MANIFEST),
            "stress_gate_results": rel(INPUT_STRESS_GATE_REPORT),
        },
        "contract_identity_mode": CONTRACT_IDENTITY_MODE,
        "artifact_hash_validation_enabled": HASH_VALIDATION_ENABLED,
        "geometry_contract_version": "trade_geometry_v1",
        "geometry_source": "allocator_active_edge_universe_verified_against_current_edge_yaml",
        "risk_contract_version": RISK_CONTRACT,
        "risk_sizing_mode": RISK_CONTRACT,
        "risk_sizing_formula": "target_trade_risk_pct = min(base_portfolio_risk_budget_pct * portfolio_weight * calibrated_afml_bet_size * throttle_multiplier * target_lock_multiplier / afml_concurrency_divisor[edge], max_single_trade_risk_pct); target risk is clipped by remaining portfolio/drawdown/reserve capacity and converted to broker-executable lot risk before admission",
        "live_runtime_contract": {
            "runtime_mode": "authoritative_batch_shared_cache",
            "authoritative_raw_bar_rows": 6000,
            "asset_level_parallelism_allowed": True,
            "edge_level_parallelism_default": False,
            "native_one_row_certification_required": False,
            "native_one_row_continuation_allowed": False,
            "stateful_incremental_continuation_allowed": False,
            "shared_feature_cache_requires_exact_bar_snapshot_and_contract_match": True,
        },
        "live_prop_rule_interpretation": {
            "overall_drawdown": ACCOUNT_RULES["overall_drawdown"],
            "daily_drawdown": ACCOUNT_RULES["daily_drawdown"],
            "minimum_trading_days": ACCOUNT_RULES["minimum_trading_days"],
            "maximum_risk_per_trade": ACCOUNT_RULES["maximum_risk_per_trade"],
            "daily_symbol_risk": ACCOUNT_RULES["daily_symbol_risk"],
            "floating_loss_guard": ACCOUNT_RULES["floating_loss_guard"],
            "operational_permissions": ACCOUNT_RULES["operational_permissions"],
        },
        "threshold_probability_column": PROBABILITY_COLUMN,
        "live_payoff_contract_version": LIVE_PAYOFF_CONTRACT_VERSION,
        "selected_edge_count": len(selected_edges),
        "selected_edges": selected_edges,
        "portfolio_weights": {edge["edge_id"]: edge["portfolio_weight"] for edge in selected_edges},
        "risk_caps": root_risk_caps,
        "throttle_config": {
            "name": variant.get("throttle_name", ""),
            "throttle_tiers": variant.get("throttle_tiers", []),
            "pause_below_balance": float(variant["pause_below_balance"]),
        },
        "target_lock_config": {
            "name": variant.get("target_lock_name", ""),
            "target_lock_tiers": variant.get("target_lock_tiers", []),
        },
        "projected_buffer_config": {
            "trailing_buffer_admission_enabled": bool(variant.get("trailing_buffer_admission_enabled", False)),
            "trailing_buffer_reserve_mode": str(variant.get("trailing_buffer_reserve_mode", "off")),
            "trailing_buffer_fixed_reserve_amount": float(variant.get("trailing_buffer_fixed_reserve_amount", 0.0) or 0.0),
            "trailing_buffer_cost_allowance_r": float(variant.get("trailing_buffer_cost_allowance_r", 0.0) or 0.0),
            "trailing_buffer_admission_policy": str(variant.get("trailing_buffer_admission_policy", "off")),
        },
        "final_allocator_selection": {
            "selected_variant_label": stress_config.get("selected_variant_label", ""),
            "source_scenario_id": source_scenario.get("scenario_id", ""),
            "return_mode": source_scenario.get("return_mode", ""),
            "universe": source_scenario.get("universe", ""),
            "profile_role": source_scenario.get("profile_role", ""),
            "linkage_method": source_scenario.get("linkage_method", ""),
            "stress_validation_status": stress_config.get("stress_validation_status"),
            "approved_account_stage": stress_config.get("approved_account_stage"),
            "live_approval_scope": stress_config.get("live_approval_scope"),
            "selected_variant_contract": (stress_config.get("stress_integrity") or {}).get("selected_variant_contract"),
            "selected_source_contract": (stress_config.get("stress_integrity") or {}).get("selected_source_contract"),
        },
        "validation_summary": {
            "edge_universe_pass_only": True,
            "threshold_probability_column_is_calibrated_probability": True,
            "dashboard_manual_feature_engineering_allowed": False,
            "finalized_geometry_present_for_every_selected_edge": True,
            "geometry_verified_against_current_edge_yaml": True,
            "global_default_trade_geometry_allowed": False,
            "stress_gate_required_for_real_live": True,
            "stress_gate_passed": stress_config.get("stress_validation_status") == "STRESS_PASS",
            "real_live_trading_armed": live_trade_enabled,
            "native_one_row_runtime_retired": True,
            "authoritative_batch_runtime_required": True,
            "max_min_lot_risk_inflation_carried": True,
        },
        "live_execution_contract": {
            "signal_schema": "v2_approved_signal_schema",
            "execution_bot_must_validate_live_manifest_hash": HASH_VALIDATION_ENABLED,
            "execution_bot_must_validate_edge_manifest_hashes": HASH_VALIDATION_ENABLED,
            "execution_bot_must_validate_geometry_hashes": HASH_VALIDATION_ENABLED,
            "execution_bot_must_consume_runtime_score_outputs_only": True,
            "execution_bot_must_honor_vertical_horizon_bars": True,
            "execution_bot_must_validate_account_stage": True,
            "execution_bot_must_validate_risk_contract_version": True,
            "execution_bot_must_recompute_and_verify_frozen_live_afml": True,
            "execution_bot_must_validate_payoff_context_id": True,
            "execution_bot_must_validate_frozen_policy_manifest_hash": HASH_VALIDATION_ENABLED,
            "execution_bot_must_count_all_account_positions_in_total_risk": True,
            "execution_bot_must_count_pending_order_contingent_risk": True,
            "execution_bot_must_use_active_account_rules": True,
            "orders_allowed": live_trade_enabled,
        },
        "sotd_contracts": {
            "allocator_consumes_only_pass_edge_manifests": True,
            "dashboard_must_call_edge_runtime_score": True,
            "dashboard_may_engineer_edge_features": False,
            "execution_bot_must_consume_approved_signal_schema_only": True,
            "raw_probability_may_not_select_final_threshold": True,
            "final_threshold_probability_column": PROBABILITY_COLUMN,
            "final_trade_geometry_source": "edge_yaml_labeling_verified_at_allocator_and_live_build",
            "live_runtime_mode": "authoritative_batch_shared_cache",
            "native_one_row_runtime_allowed": False,
        },
        "live_blockers": blockers,
        "stress_manifest_summary": {
            "monte_carlo_stress_status": stress_manifest.get("monte_carlo_stress_status"),
            "stress_validation_status": stress_manifest.get("stress_validation_status"),
            "counts": stress_manifest.get("counts", {}),
        },
        "next_ordered_steps": [
            "python ./portfolio_live/01_validate_live_manifest.py",
            "python ./portfolio_live/02_validate_live_environment.py",
            "python ./portfolio_live/03_live_bar_threshold_diagnostic.py",
            "python ./portfolio_live/04_freeze_live_bar_thresholds.py --require-pass",
            "python ./portfolio_live/portfolio_live_dashboard_orchestrator.py --once",
            "python ./portfolio_live/portfolio_execution_bot.py --once",
        ],
    }
    if write:
        write_json(OUTPUT_LIVE_MANIFEST, manifest)
        write_json(OUTPUT_DEPLOYMENT_MANIFEST, manifest)
        write_csv(OUTPUT_ALPHA_SUMMARY, selected_edges)
        # Record which version of each edge this manifest runs, so live oversight
        # (decommission rule, drift dashboard) judges only bets of the current version.
        sys.path.insert(0, str(V2_ROOT / "portfolio_live"))
        import edge_versions

        edge_versions.register_current_manifest(OUTPUT_LIVE_MANIFEST)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build_live_manifest(write=not args.dry_run)
    print("V2 Portfolio Live 00 — build live deployment manifest")
    print(f"Deployment status: {manifest['deployment_status']}")
    print(f"Stress validation: {manifest.get('stress_validation_status')}")
    print(f"Live mode: {manifest.get('live_mode')}")
    print(f"Allow real live export: {manifest.get('allow_real_live_trading_export')}")
    print(f"Live ready: {manifest['live_ready']}")
    print(f"Live trade enabled: {manifest['live_trade_enabled']}")
    print(f"Selected edges: {manifest['selected_edge_count']}")
    for edge in manifest["selected_edges"]:
        print(
            f"- {edge['edge_id']} threshold={edge['final_live_threshold']} "
            f"weight={edge['portfolio_weight']:.6f} "
            f"geometry={edge['pt_multiplier']}:{edge['sl_multiplier']}@{edge['vertical_horizon_bars']} "
            f"live={edge['live_trade_enabled']}"
        )
    if manifest.get("live_blockers"):
        print("Live blockers:")
        for item in manifest["live_blockers"]:
            print(f"- {item}")
    print("Output: portfolio_live/live_manifest.json")


if __name__ == "__main__":
    main()
