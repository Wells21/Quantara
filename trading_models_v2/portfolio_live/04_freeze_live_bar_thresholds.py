from __future__ import annotations

"""V2 Portfolio Live 04 — freeze live bar thresholds/policies.

This step freezes the adaptive live bar policy produced by Step 03. It does not
connect to MT5, score edges, engineer features, or place orders.

The freeze is mode-agnostic and scalable:
- Step 03 PASS -> POLICY_PASS
- Step 03 WARN_REVIEW_REQUIRED -> POLICY_REVIEW_REQUIRED
- Step 03 FAIL or missing policy -> fail closed

Order permission is never hardcoded here. The execution bot must later require:
1. live_manifest.live_trade_enabled=true;
2. frozen bar policy live_policy_ready=true;
3. all broker/risk/hash gates pass.
"""

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for path in (LIVE_DIR, V2_ROOT, REPO_ROOT):
    text = str(path)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)

import live_settings as settings  # type: ignore  # noqa: E402

SCHEMA_VERSION = "v2.1"
STEP_NAME = "V2 Portfolio Live 04 — freeze live bar thresholds"

OUTPUT_DIR = Path(getattr(settings, "OUTPUT_DIR", LIVE_DIR / "outputs")).resolve() / "threshold_diagnostics"
CONFIG_DIR = Path(getattr(settings, "LIVE_CONFIG_DIR", LIVE_DIR / "config")).resolve()
DIAGNOSTIC_MANIFEST = OUTPUT_DIR / "threshold_diagnostic_manifest.json"
FROZEN_POLICY_JSON = CONFIG_DIR / "live_bar_policy.json"
FREEZE_MANIFEST_JSON = OUTPUT_DIR / "live_bar_policy_freeze_manifest.json"
LIVE_MANIFEST_PATH = Path(getattr(settings, "LIVE_MANIFEST_PATH", LIVE_DIR / "live_manifest.json")).resolve()

ALLOWED_DIAGNOSTIC_STATUSES = {"PASS", "WARN_REVIEW_REQUIRED"}
HASH_VALIDATION_ENABLED = bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False))
RESEARCH_TARGET_BARS_PER_DAY = int(getattr(settings, "THRESHOLD_TARGET_BARS_PER_DAY", 50))
RESEARCH_EWMA_SPAN_DAYS = int(getattr(settings, "DAILY_TICK_EWMA_SPAN_DAYS", 20))
RESEARCH_MIN_WARMUP_DAYS = int(getattr(settings, "MIN_WARMUP_DAYS", 20))
CADENCE_WARNING_TOLERANCE = float(getattr(settings, "LIVE_BAR_CADENCE_WARNING_TOLERANCE", 0.35))
CADENCE_BLOCK_TOLERANCE = float(getattr(settings, "LIVE_BAR_CADENCE_BLOCK_TOLERANCE", 0.75))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing JSON file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def validate_policy(asset: str, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = [
        "bar_mode",
        "threshold_unit",
        "threshold_mode",
        "target_bars_per_day",
        "daily_tick_ewma_span_days",
        "min_warmup_days",
        "threshold_frozen_at_bar_start",
        "uses_future_data",
        "fallback_initial_threshold_ticks",
    ]
    for key in required:
        if key not in policy:
            errors.append(f"{asset}: missing policy field {key}")
    if str(policy.get("bar_mode")) != "standard_quote_tick":
        errors.append(f"{asset}: bar_mode must be standard_quote_tick")
    if str(policy.get("threshold_unit")) != "ticks":
        errors.append(f"{asset}: threshold_unit must be ticks")
    if str(policy.get("threshold_mode")) != "causal_daily_ewma_tick_count":
        errors.append(f"{asset}: threshold_mode must be causal_daily_ewma_tick_count")
    if policy.get("threshold_frozen_at_bar_start") is not True:
        errors.append(f"{asset}: threshold_frozen_at_bar_start must be true")
    if policy.get("uses_future_data") is not False:
        errors.append(f"{asset}: uses_future_data must be false")
    try:
        if int(policy.get("fallback_initial_threshold_ticks")) <= 0:
            errors.append(f"{asset}: fallback_initial_threshold_ticks must be > 0")
    except Exception:
        errors.append(f"{asset}: fallback_initial_threshold_ticks must be an integer")
    try:
        target = float(policy.get("target_bars_per_day"))
        if target <= 0:
            errors.append(f"{asset}: target_bars_per_day must be > 0")
        elif not math.isclose(target, float(RESEARCH_TARGET_BARS_PER_DAY), rel_tol=0.0, abs_tol=1e-12):
            errors.append(
                f"{asset}: target_bars_per_day={target} differs from research target={RESEARCH_TARGET_BARS_PER_DAY}"
            )
    except Exception:
        errors.append(f"{asset}: target_bars_per_day must be numeric")
    try:
        span = int(policy.get("daily_tick_ewma_span_days"))
        if span != RESEARCH_EWMA_SPAN_DAYS:
            errors.append(
                f"{asset}: daily_tick_ewma_span_days={span} differs from research span={RESEARCH_EWMA_SPAN_DAYS}"
            )
    except Exception:
        errors.append(f"{asset}: daily_tick_ewma_span_days must be an integer")
    try:
        warmup = int(policy.get("min_warmup_days"))
        if warmup < RESEARCH_MIN_WARMUP_DAYS:
            errors.append(
                f"{asset}: min_warmup_days={warmup} is below research minimum={RESEARCH_MIN_WARMUP_DAYS}"
            )
    except Exception:
        errors.append(f"{asset}: min_warmup_days must be an integer")
    return errors


def derive_policy_gate(diagnostic_status: str, require_pass: bool) -> tuple[str, bool, bool]:
    if diagnostic_status == "PASS":
        return "POLICY_PASS", True, False
    if diagnostic_status == "WARN_REVIEW_REQUIRED":
        if require_pass:
            raise RuntimeError("--require-pass was used but Step 03 returned WARN_REVIEW_REQUIRED")
        return "POLICY_REVIEW_REQUIRED", False, True
    raise RuntimeError(f"Step 03 diagnostic_status is not acceptable for freeze: {diagnostic_status}")


def build_freeze(require_pass: bool = False, write: bool = True) -> dict[str, Any]:
    diagnostic = read_json(DIAGNOSTIC_MANIFEST)
    live_manifest = read_json(LIVE_MANIFEST_PATH) if LIVE_MANIFEST_PATH.exists() else {}
    current_manifest_hash = sha256_file(LIVE_MANIFEST_PATH)
    if HASH_VALIDATION_ENABLED and diagnostic.get("source_live_manifest_sha256") != current_manifest_hash:
        raise RuntimeError("Step 03 diagnostic was not produced from the current live manifest.")
    if not HASH_VALIDATION_ENABLED:
        current_edges = sorted(str(edge.get("edge_id")) for edge in (live_manifest.get("selected_edges") or []) if edge.get("edge_id"))
        diagnostic_edges = sorted(str(x) for x in (diagnostic.get("source_selected_edge_ids") or []))
        if diagnostic.get("source_manifest_created_at") != live_manifest.get("created_at") or diagnostic_edges != current_edges:
            raise RuntimeError("Step 03 diagnostic does not structurally match the current live manifest.")
    selected_assets = {str(edge.get("asset")) for edge in live_manifest.get("selected_edges", []) if edge.get("asset")}
    diagnostic_status = str(diagnostic.get("diagnostic_status", ""))
    if diagnostic_status not in ALLOWED_DIAGNOSTIC_STATUSES:
        raise RuntimeError(f"Step 03 diagnostic_status is not acceptable for freeze: {diagnostic_status}")

    policy_gate_status, live_policy_ready, policy_review_required = derive_policy_gate(diagnostic_status, require_pass)
    live_trade_enabled_by_manifest = bool(live_manifest.get("live_trade_enabled", False))
    order_execution_allowed_by_policy = bool(live_policy_ready and live_trade_enabled_by_manifest)

    policies = diagnostic.get("adaptive_threshold_policies", {})
    if not isinstance(policies, dict) or not policies:
        raise RuntimeError("Step 03 diagnostic manifest has no adaptive_threshold_policies")
    if set(map(str, policies.keys())) != selected_assets:
        raise RuntimeError(
            f"Step 03 policy assets do not exactly match current selected assets: "
            f"policy={sorted(policies)} selected={sorted(selected_assets)}"
        )

    errors: list[str] = []
    warnings: list[str] = []
    normalized: dict[str, Any] = {}
    diagnostics = diagnostic.get("diagnostics", [])
    diagnostic_by_asset = {str(row.get("asset")): row for row in diagnostics if isinstance(row, dict)} if isinstance(diagnostics, list) else {}

    for asset, policy in policies.items():
        if not isinstance(policy, dict):
            errors.append(f"{asset}: policy must be an object")
            continue
        errors.extend(validate_policy(str(asset), policy))
        row = diagnostic_by_asset.get(str(asset), {})
        if str(row.get("status")) == "warn":
            warnings.append(f"{asset}: Step 03 policy warning retained: {row.get('reason')}")
        asset_policy_ready = bool(live_policy_ready and str(row.get("status")) == "pass")
        normalized[str(asset)] = {
            **policy,
            "asset": str(asset),
            "policy_gate_status": "POLICY_PASS" if asset_policy_ready else "POLICY_REVIEW_REQUIRED",
            "live_policy_ready": asset_policy_ready,
            "policy_review_required": not asset_policy_ready,
            "order_execution_allowed_by_policy": bool(asset_policy_ready and live_trade_enabled_by_manifest),
            "source_step03_asset_status": row.get("status"),
            "source_step03_reason": row.get("reason"),
            "diagnostic_cache_reused": bool(row.get("cache_reused", False)),
            "diagnostic_bars_per_day": row.get("estimated_bars_per_day_fallback_threshold"),
            "diagnostic_cadence_deviation_fraction": row.get("fallback_cadence_deviation_fraction"),
            "cadence_warning_tolerance": CADENCE_WARNING_TOLERANCE,
            "cadence_block_tolerance": CADENCE_BLOCK_TOLERANCE,
            "cadence_monitoring_required": True,
        }

    if errors:
        raise RuntimeError("Live bar policy freeze failed:\n- " + "\n- ".join(errors))

    frozen_policy = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": "v2_frozen_live_bar_policy",
        "created_at": now_utc(),
        "policy_gate_status": policy_gate_status,
        "live_policy_ready": live_policy_ready,
        "policy_review_required": policy_review_required,
        "live_trade_enabled_by_manifest": live_trade_enabled_by_manifest,
        "order_execution_allowed_by_policy": order_execution_allowed_by_policy,
        "source_live_manifest": rel(LIVE_MANIFEST_PATH),
        "source_live_manifest_sha256": current_manifest_hash,  # audit only
        "source_manifest_created_at": live_manifest.get("created_at"),
        "source_selected_edge_ids": sorted(str(edge.get("edge_id")) for edge in (live_manifest.get("selected_edges") or []) if edge.get("edge_id")),
        "artifact_hash_validation_enabled": HASH_VALIDATION_ENABLED,
        "source_diagnostic_manifest": rel(DIAGNOSTIC_MANIFEST),
        "source_diagnostic_manifest_sha256": sha256_file(DIAGNOSTIC_MANIFEST),
        "approved_account_stage": live_manifest.get("approved_account_stage"),
        "risk_contract_version": live_manifest.get("risk_contract_version"),
        "bar_policies": normalized,
        "sotd_contracts": {
            "bar_mode": "standard_quote_tick",
            "threshold_unit": "ticks",
            "threshold_mode": "causal_daily_ewma_tick_count",
            "threshold_frozen_at_bar_start": True,
            "uses_future_data": False,
            "proxy_value_is_ticks_only": True,
            "dashboard_must_use_frozen_policy": True,
            "dashboard_may_engineer_edge_features": False,
            "execution_bot_must_check_policy_gate": True,
            "research_target_bars_per_day": RESEARCH_TARGET_BARS_PER_DAY,
            "research_daily_tick_ewma_span_days": RESEARCH_EWMA_SPAN_DAYS,
            "research_min_warmup_days": RESEARCH_MIN_WARMUP_DAYS,
            "valid_quote_filter": "bid>0_and_ask>0_and_ask>=bid",
            "quote_tick_multiplicity_preserved": True,
            "sustained_live_cadence_monitoring_required": True,
            "cadence_warning_tolerance_fraction": CADENCE_WARNING_TOLERANCE,
            "cadence_block_tolerance_fraction": CADENCE_BLOCK_TOLERANCE,
        },
        "notes": [
            "This artifact freezes the live bar policy for dashboard and live orchestration.",
            "fallback_initial_threshold_ticks is only an initial fallback; the bar policy remains adaptive from completed daily tick counts.",
            "This artifact does not independently arm orders; execution requires both live_manifest.live_trade_enabled and live_policy_ready.",
            "Live cadence is monitored on rolling completed-day windows; 50 bars/day is an adaptive average target, not an exact daily quota.",
        ],
    }

    freeze_manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "step": "04_freeze_live_bar_thresholds.py",
        "policy_gate_status": policy_gate_status,
        "live_policy_ready": live_policy_ready,
        "policy_review_required": policy_review_required,
        "source_diagnostic_status": diagnostic_status,
        "live_trade_enabled_by_manifest": live_trade_enabled_by_manifest,
        "order_execution_allowed_by_policy": order_execution_allowed_by_policy,
        "warnings": warnings,
        "frozen_policy_path": rel(FROZEN_POLICY_JSON),
        "frozen_policy_sha256": None,
        "selected_assets": sorted(normalized.keys()),
        "next_ordered_step_if_policy_frozen": "python ./portfolio_live/portfolio_live_dashboard_orchestrator.py --once",
    }

    if write:
        write_json(FROZEN_POLICY_JSON, frozen_policy)
        freeze_manifest["frozen_policy_sha256"] = sha256_file(FROZEN_POLICY_JSON)
        write_json(FREEZE_MANIFEST_JSON, freeze_manifest)
    return {"frozen_policy": frozen_policy, "freeze_manifest": freeze_manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-pass", action="store_true", help="Require Step 03 PASS. Use before real-live arming.")
    args = parser.parse_args()
    result = build_freeze(require_pass=args.require_pass, write=not args.dry_run)
    manifest = result["freeze_manifest"]
    print(STEP_NAME)
    print(f"Policy gate status: {manifest['policy_gate_status']}")
    print(f"Live policy ready: {manifest['live_policy_ready']}")
    print(f"Policy review required: {manifest['policy_review_required']}")
    print(f"Source diagnostic status: {manifest['source_diagnostic_status']}")
    print(f"Live trade enabled by manifest: {manifest['live_trade_enabled_by_manifest']}")
    print(f"Order execution allowed by policy: {manifest['order_execution_allowed_by_policy']}")
    print(f"Assets: {len(manifest['selected_assets'])}")
    for asset in manifest["selected_assets"]:
        policy = result["frozen_policy"]["bar_policies"][asset]
        print(
            f"- {asset} mode={policy['threshold_mode']} "
            f"fallback={policy['fallback_initial_threshold_ticks']} "
            f"next={policy.get('computed_next_session_threshold_ticks')} "
            f"policy_ready={policy['live_policy_ready']}"
        )
    if manifest.get("warnings"):
        print("Warnings:")
        for warning in manifest["warnings"]:
            print(f"- {warning}")
    print("Output: portfolio_live/config/live_bar_policy.json")


if __name__ == "__main__":
    main()
