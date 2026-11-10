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
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.edges.edge_layout import get_edge_layout  # noqa: E402
from shared.edges.trade_geometry import GEOMETRY_COLUMNS, load_trade_geometry  # noqa: E402
from shared.validation.v3_semantic_contract import (  # noqa: E402
    validate_research_generation_chain,
    validate_semantic_generation_snapshot,
)

SCHEMA_VERSION = "v2.3"
ARTIFACT_POLICY = "hashless_path_and_contract_validation"
PROBABILITY_COLUMN = "calibrated_probability"
BROKER_PROFILE_PATH = (
    V2_ROOT
    / "portfolio_allocator"
    / "config"
    / "broker_profiles"
    / "mt5_broker_profile.json"
)

REPLAY_FLAGS = [
    "selected_row_replay_passed",
    "raw_probability_replay_passed",
    "calibrated_probability_replay_passed",
    "afml_bet_size_replay_passed",
    "threshold_decision_replay_passed",
]


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


def must(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_declared_artifact(edge_id: str, manifest: dict[str, Any], key: str) -> str:
    """Require the declared artifact path to exist without consuming file hashes."""
    artifacts = manifest.get("artifacts", {})
    must(isinstance(artifacts, dict), f"{edge_id}: artifacts must be a mapping")
    value = artifacts.get(key)
    must(isinstance(value, str) and value.strip(), f"{edge_id}: missing artifacts.{key}")
    path = V2_ROOT / value
    must(path.exists() and path.is_file(), f"{edge_id}: artifacts.{key} path does not exist: {value}")
    return value




def load_broker_availability() -> dict[str, Any]:
    if not BROKER_PROFILE_PATH.exists():
        raise FileNotFoundError(
            "Missing MT5 broker profile. Run "
            "portfolio_allocator/00a_capture_mt5_broker_profile.py first: "
            f"{BROKER_PROFILE_PATH}"
        )
    profile = load_json(BROKER_PROFILE_PATH)
    summary = profile.get("summary") if isinstance(profile.get("summary"), dict) else {}
    status = str(summary.get("status") or "").upper()
    must(status == "PASS", f"MT5 broker profile status must be PASS, got {status!r}")
    contracts = profile.get("contracts") if isinstance(profile.get("contracts"), dict) else {}
    available = {
        str(asset).strip().upper()
        for asset, row in contracts.items()
        if isinstance(row, dict) and str(row.get("status") or "captured").lower() == "captured"
    }
    must(bool(available), "MT5 broker profile contains no captured assets")
    inputs = profile.get("inputs") if isinstance(profile.get("inputs"), dict) else {}
    requested = [
        str(x).strip().upper()
        for x in (inputs.get("assets") or sorted(available | set(summary.get("missing_assets") or [])))
    ]
    missing = [str(x).strip().upper() for x in (summary.get("missing_assets") or [])]
    return {
        "profile_path": rel(BROKER_PROFILE_PATH),
        "account_currency": str((profile.get("account") or {}).get("currency") or "").upper(),
        "server": (profile.get("account") or {}).get("server"),
        "available_assets": sorted(available),
        "missing_assets": sorted(set(missing)),
        "requested_assets": requested,
        "availability_status": str(summary.get("availability_status") or ("FULL" if not missing else "PARTIAL")),
    }

def validate_pass_manifest(edge_id: str, manifest: dict[str, Any]) -> None:
    contracts = manifest.get("contracts", {})
    live = manifest.get("live_compatibility", {})
    threshold = manifest.get("threshold", {})
    t_contract = contracts.get("threshold_contract", {})
    must(manifest.get("deployment_status") == "PASS", f"{edge_id}: deployment_status is not PASS")
    must(manifest.get("deployable") is True, f"{edge_id}: deployable is not true")
    must(contracts.get("allocator_may_consume_only_if_deployment_status_pass") is True, f"{edge_id}: allocator gate missing")
    must(contracts.get("dashboard_may_engineer_edge_features") is False, f"{edge_id}: dashboard-side feature engineering allowed")
    must(contracts.get("threshold_probability_column") == PROBABILITY_COLUMN, f"{edge_id}: threshold column must be calibrated_probability")
    must(t_contract.get("edge_manifest_threshold_is_final_live_threshold") is False, f"{edge_id}: edge threshold cannot be final live threshold")
    must(t_contract.get("live_manifest_must_contain_final_threshold") is True, f"{edge_id}: live manifest threshold contract missing")
    must(threshold.get("edge_default_calibrated_threshold") is not None, f"{edge_id}: missing edge default threshold")
    must(threshold.get("threshold_probability_column") == PROBABILITY_COLUMN, f"{edge_id}: threshold section must use calibrated_probability")
    for flag in REPLAY_FLAGS:
        must(live.get(flag) is True, f"{edge_id}: replay flag is not true: {flag}")
    require_declared_artifact(edge_id, manifest, "calibrated_oof_predictions")
    require_declared_artifact(edge_id, manifest, "threshold_sweep")
    require_declared_artifact(edge_id, manifest, "replay_audit_report")


def flatten_row(entry: dict[str, Any], manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    layout = get_edge_layout(entry["asset"], entry["family"], entry["side"], v2_root=V2_ROOT)
    threshold = manifest["threshold"]
    metrics = manifest.get("metrics", {})
    runtime = manifest.get("runtime", {})
    legacy_hashes = manifest.get("artifact_hashes")
    legacy_hashes = legacy_hashes if isinstance(legacy_hashes, dict) else {}
    geometry = load_trade_geometry(
        layout.edge_yaml,
        expected_edge_id=layout.edge_id,
        expected_side_name=layout.side_name,
    )
    return {
        "edge_id": layout.edge_id,
        "asset": layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
        "edge_root": rel(layout.edge_root),
        "runtime_module": runtime.get("runtime_module") or rel(layout.runtime_py),
        "edge_manifest_path": rel(manifest_path),
        # Compatibility columns remain present but are never populated or consumed.
        "edge_manifest_hash": "",
        "edge_manifest_hash_record": {},
        "edge_yaml_path": rel(layout.edge_yaml),
        "edge_yaml_hash": "",
        "edge_yaml_hash_record": {},
        "trade_geometry": geometry,
        **geometry,
        "final_live_threshold": float(threshold["edge_default_calibrated_threshold"]),
        "threshold_probability_column": PROBABILITY_COLUMN,
        "threshold_source": "edge_manifest_default_calibrated_candidate_threshold",
        "threshold_metrics": threshold.get("selected_threshold_metrics", {}),
        "runtime_rows_scored": int(metrics.get("runtime_rows_scored", 0)),
        "research_model_rows": int(metrics.get("research_model_rows", 0)),
        "artifact_policy": ARTIFACT_POLICY,
        "legacy_artifact_hash_metadata_present": bool(legacy_hashes),
        "legacy_artifact_hash_count": int(len(legacy_hashes)),
        "source_manifest_schema_version": manifest.get("schema_version", ""),
        "source_manifest_created_at": manifest.get("created_at", ""),
        "artifacts": manifest.get("artifacts", {}),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "edge_id", "asset", "family", "side_name", "side_value", "edge_root", "runtime_module",
        "edge_manifest_path", "edge_manifest_hash", "edge_yaml_path", "edge_yaml_hash",
        "artifact_policy", "legacy_artifact_hash_metadata_present", "legacy_artifact_hash_count",
        *GEOMETRY_COLUMNS,
        "final_live_threshold", "threshold_probability_column", "threshold_source",
        "runtime_rows_scored", "research_model_rows",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ingest(write: bool = True) -> dict[str, Any]:
    universe = load_json(V2_ROOT / "config" / "edge_universe.json")
    broker_availability = load_broker_availability()
    available_assets = set(broker_availability["available_assets"])
    active: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for entry in universe.get("edges", []):
        edge_id = f"{entry.get('asset')}/{entry.get('family')}_{entry.get('side')}"
        if entry.get("enabled") is not True or entry.get("status") != "deployability_pass":
            skipped.append({"edge_id": edge_id, "status": str(entry.get("status")), "reason": "not_enabled_or_not_deployability_pass"})
            continue
        asset = str(entry.get("asset") or "").strip().upper()
        if asset not in available_assets:
            skipped.append({
                "edge_id": edge_id,
                "status": str(entry.get("status")),
                "reason": "broker_symbol_unavailable",
                "asset": asset,
            })
            continue
        try:
            layout = get_edge_layout(entry["asset"], entry["family"], entry["side"], v2_root=V2_ROOT)
            manifest = load_json(layout.edge_manifest_json)
            validate_pass_manifest(layout.edge_id, manifest)
            current_generation = validate_research_generation_chain(layout)
            validate_semantic_generation_snapshot(
                manifest.get("semantic_generation"), current_generation,
                context=f"{layout.edge_id} edge_manifest.semantic_generation",
            )
            active.append(flatten_row(entry, manifest, layout.edge_manifest_json))
        except Exception as exc:
            failures.append({"edge_id": edge_id, "error": str(exc)})
    active.sort(key=lambda row: row["edge_id"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "ingestion_status": "FAIL" if failures else "PASS",
        "active_edge_count": len(active),
        "active_assets": sorted({str(row["asset"]) for row in active}),
        "broker_availability": broker_availability,
        "skipped_edge_count": len(skipped),
        "broker_unavailable_edge_count": sum(1 for row in skipped if row.get("reason") == "broker_symbol_unavailable"),
        "failure_count": len(failures),
        "geometry_contract_version": "trade_geometry_v1",
        "artifact_policy": ARTIFACT_POLICY,
        "file_hash_validation": "disabled",
        "legacy_hash_metadata_consumed": False,
        "active_edges": active,
        "skipped_edges": skipped,
        "failures": failures,
        "next_ordered_step_if_pass": "portfolio_allocator/02_build_edge_candidate_bank.py",
        "live_handoff_after_allocator_completion": "portfolio_live/00_build_live_deployment_manifest.py",
    }
    if write:
        out = V2_ROOT / "portfolio_allocator" / "outputs"
        write_json(out / "active_edge_universe.json", payload)
        write_json(out / "pass_edge_manifest_ingestion_report.json", payload)
        write_csv(out / "active_edge_universe.csv", active)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    payload = ingest(write=not args.dry_run)
    print("V2 Phase B1.01 PASS-edge ingestion")
    print(f"Status: {payload['ingestion_status']}")
    print(f"Active PASS edges: {payload['active_edge_count']}")
    print(f"Skipped edges: {payload['skipped_edge_count']}")
    print(f"Failures: {payload['failure_count']}")
    availability = payload.get("broker_availability", {})
    print(f"Broker assets available: {', '.join(availability.get('available_assets', []))}")
    if availability.get("missing_assets"):
        print(f"Broker assets excluded: {', '.join(availability.get('missing_assets', []))}")
    for row in payload["active_edges"]:
        print(
            f"- {row['edge_id']} threshold={row['final_live_threshold']} "
            f"geometry={row['pt_multiplier']}:{row['sl_multiplier']}@{row['vertical_horizon_bars']} "
            f"artifact_policy={row['artifact_policy']}"
        )
    for row in payload["failures"]:
        print(f"FAIL {row['edge_id']}: {row['error']}")
    if payload["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
