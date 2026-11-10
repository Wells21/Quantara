from __future__ import annotations

"""Phase D.4 structural contract for exact candidate continuation providers.

This module is inert. It does not route production and it does not contain
trading/event logic.

D.1 is the source-derived inventory authority. D.4 groups selected edges only by
D.1's candidate_logic_signature and declares the continuation-provider
requirements that must be certified before routing can change.

Scalability contract
--------------------
* no asset registry;
* no fixed selected-edge count;
* no family-name dispatch;
* no candidate-function -> implementation recipe table;
* a future source-equivalent edge automatically joins its D.1 logic group;
* genuinely new candidate logic creates a new unbound logic group and therefore
  fails closed until a provider is implemented and certified.

Authority identity
------------------
Git/file SHA values are audit metadata, not execution authority. Structural
current-state identity plus fresh numerical/behavioral certification is the
gate.
"""

import copy
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "phase_d4_incremental_candidate_contract_v2"
COMPILER_VERSION = "phase_d4_d1_logic_signature_provider_requirements_v2"
AUTHORITY_IDENTITY_MODE = "structural_current_state_no_git_or_file_sha_gate"
BOOTSTRAP_AUTHORITY_ROWS = 6000
PROVIDER_BINDING_MODE = "d1_candidate_logic_signature"
PROVIDER_DIRECTORY = "shared/edges/candidate_continuations/providers"


class PhaseD4ContractError(RuntimeError):
    """Raised when the current D.1 inventory cannot define a safe D.4 plan."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PhaseD4ContractError(f"Expected JSON object: {path}")
    return payload


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _strip_nonstructural(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_nonstructural(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        lower = str(key).lower()
        if (
            lower in {"compiled_at", "contract_sha256", "file_sha256", "sha256"}
            or lower.endswith("_sha256")
            or "fingerprint" in lower
            or lower.startswith("release_")
            or lower.startswith("git_")
        ):
            continue
        out[str(key)] = _strip_nonstructural(item)
    return out


def _d1_edge_map(d1: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = d1.get("edges")
    if not isinstance(rows, list) or not rows:
        raise PhaseD4ContractError("D.1 edges must be a non-empty list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseD4ContractError("D.1 edge is not an object")
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id or edge_id in out:
            raise PhaseD4ContractError(f"Invalid/duplicate D.1 edge_id: {edge_id!r}")
        out[edge_id] = dict(raw)
    return out


def _manifest_ids(manifest: Mapping[str, Any]) -> list[str]:
    rows = manifest.get("selected_edges")
    if not isinstance(rows, list) or not rows:
        raise PhaseD4ContractError("live_manifest selected_edges must be non-empty")
    ids: list[str] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseD4ContractError("live_manifest selected edge is not an object")
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            raise PhaseD4ContractError("live_manifest selected edge has blank edge_id")
        ids.append(edge_id)
    if len(ids) != len(set(ids)):
        raise PhaseD4ContractError("live_manifest contains duplicate edge_id values")
    return ids


def _candidate_row(edge: Mapping[str, Any]) -> dict[str, Any]:
    candidate = edge.get("candidate")
    if not isinstance(candidate, Mapping):
        raise PhaseD4ContractError(
            f"{edge.get('edge_id')}: D.1 candidate record is missing"
        )
    return dict(candidate)


def _logic_group(
    signature: str,
    rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    edge_ids = sorted(str(row.get("edge_id") or "") for row in rows)
    candidate_rows = [_candidate_row(row) for row in rows]

    entries = sorted(
        {
            str(entry)
            for candidate in candidate_rows
            for entry in (candidate.get("runtime_entry_functions") or [])
            if str(entry).strip()
        }
    )
    if not entries:
        failures.append(f"{signature}: no runtime candidate entry function")

    source_paths = sorted(
        {
            str(candidate.get("path") or "")
            for candidate in candidate_rows
            if str(candidate.get("path") or "").strip()
        }
    )
    if not source_paths:
        failures.append(f"{signature}: no candidate source path")

    history_profiles = [
        copy.deepcopy(candidate.get("history_profile") or {})
        for candidate in candidate_rows
    ]
    semantic_constants = [
        copy.deepcopy(candidate.get("semantic_constants") or {})
        for candidate in candidate_rows
    ]
    if any(profile != history_profiles[0] for profile in history_profiles[1:]):
        failures.append(
            f"{signature}: source-equivalent logic group has differing history_profile"
        )
    if any(constants != semantic_constants[0] for constants in semantic_constants[1:]):
        failures.append(
            f"{signature}: source-equivalent logic group has differing semantic_constants"
        )

    required_by_edge: dict[str, list[str]] = {}
    union_columns: set[str] = set()
    for edge, candidate in zip(rows, candidate_rows):
        edge_id = str(edge.get("edge_id") or "")
        columns = [
            str(value)
            for value in (
                candidate.get("candidate_input_columns_from_certified_projection")
                or []
            )
        ]
        if not columns:
            failures.append(f"{edge_id}: empty candidate input column set")
        required_by_edge[edge_id] = columns
        union_columns.update(columns)

    primary_signatures = sorted(
        {
            str((row.get("primary_side") or {}).get("primary_logic_signature") or "")
            for row in rows
            if str((row.get("primary_side") or {}).get("primary_logic_signature") or "")
        }
    )

    return (
        {
            "candidate_logic_signature": signature,
            "edge_count": len(edge_ids),
            "edge_ids": edge_ids,
            "runtime_entry_functions": entries,
            "candidate_source_paths": source_paths,
            "history_profile": history_profiles[0],
            "semantic_constants": semantic_constants[0],
            "candidate_required_columns_union": sorted(union_columns),
            "candidate_required_columns_by_edge": required_by_edge,
            "primary_logic_signatures": primary_signatures,
            "provider_binding_key": signature,
            "provider_binding_mode": PROVIDER_BINDING_MODE,
            "provider_status": "UNBOUND",
            "provider_module": None,
            "provider_certification_status": "NOT_RUN",
            "normal_path": {
                "input": "one_new_closed_c4_canonical_row",
                "candidate_history_disk_reads_allowed": False,
                "full_candidate_generator_allowed": False,
                "full_candidate_fallback_allowed": False,
                "full_canonical_ring_copy_allowed": False,
                "model_row_build_allowed_when_candidate_false": False,
                "model_predict_allowed_when_candidate_false": False,
            },
            "bootstrap": {
                "authority": "current_c4_canonical_ring",
                "rows": BOOTSTRAP_AUTHORITY_ROWS,
                "startup_or_dedicated_recovery_only": True,
            },
        },
        failures,
    )


def compile_incremental_candidate_contract(
    v2_root: str | Path,
) -> dict[str, Any]:
    root = Path(v2_root).resolve()
    d1 = _read_json(
        root / "portfolio_live/config/phase_d/candidate_contract.json"
    )
    manifest = _read_json(root / "portfolio_live/live_manifest.json")

    failures: list[str] = []
    if d1.get("status") != "PASS":
        failures.append("D.1 candidate contract is not PASS")

    d1_edges = _d1_edge_map(d1)
    manifest_ids = _manifest_ids(manifest)
    if set(manifest_ids) != set(d1_edges):
        failures.append("D.1 selected edge set differs from current live_manifest")

    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    compiled_edges: list[dict[str, Any]] = []

    for edge_id in manifest_ids:
        edge = d1_edges.get(edge_id)
        if edge is None:
            continue
        candidate = _candidate_row(edge)
        signature = str(candidate.get("candidate_logic_signature") or "").strip()
        if not signature:
            failures.append(f"{edge_id}: missing candidate_logic_signature")
            continue
        grouped[signature].append(edge)
        compiled_edges.append(
            {
                "edge_id": edge_id,
                "asset": str(edge.get("asset") or "").upper(),
                "side_value": edge.get("side_value"),
                "candidate_logic_signature": signature,
                "provider_binding_key": signature,
            }
        )

    groups: list[dict[str, Any]] = []
    for signature, rows in sorted(grouped.items()):
        group, group_failures = _logic_group(signature, rows)
        groups.append(group)
        failures.extend(group_failures)

    d1_group_count = int(d1.get("candidate_logic_group_count") or 0)
    if d1_group_count and len(groups) != d1_group_count:
        failures.append(
            f"D.1 logic-group count differs: d1={d1_group_count} d4={len(groups)}"
        )

    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "compiled_at": _now_utc(),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": AUTHORITY_IDENTITY_MODE,
        "runtime_routing_enabled": False,
        "production_activation_allowed": False,
        "provider_binding_mode": PROVIDER_BINDING_MODE,
        "provider_directory": PROVIDER_DIRECTORY,
        "selected_asset_count": len(
            {str(row.get("asset") or "") for row in compiled_edges}
        ),
        "selected_edge_count": len(manifest_ids),
        "compiled_edge_count": len(compiled_edges),
        "candidate_logic_group_count": len(groups),
        "bound_provider_count": 0,
        "unbound_logic_group_count": len(groups),
        "bootstrap_authority_rows": BOOTSTRAP_AUTHORITY_ROWS,
        "normal_path_contract": {
            "one_closed_canonical_row_per_asset_generation": True,
            "candidate_history_disk_reads_allowed": False,
            "full_candidate_generator_allowed": False,
            "full_candidate_fallback_allowed": False,
            "full_canonical_ring_copy_allowed": False,
            "startup_rebuild_from_c4_ring_allowed": True,
            "dedicated_recovery_rebuild_from_c4_ring_allowed": True,
            "provider_binding_must_be_source_derived": True,
        },
        "certification_required_before_activation": {
            "latest_candidate_matches_full_authoritative_generator": True,
            "candidate_sequence_matches_full_authority": True,
            "candidate_restart_matches_uninterrupted_sequence": True,
            "nonfiring_edge_never_builds_model_rows": True,
            "nonfiring_edge_never_invokes_model_predict": True,
            "current_model_row_parity_all_firing_edges": True,
            "current_decision_parity_all_selected_edges": True,
            "signal_id_parity_all_firing_edges": True,
            "no_normal_path_full_candidate_fallback": True,
        },
        "candidate_logic_groups": groups,
        "edges": compiled_edges,
    }
    contract["contract_sha256"] = _json_hash(_strip_nonstructural(contract))
    return contract


def validate_current_contract(
    frozen: Mapping[str, Any],
    *,
    v2_root: str | Path,
) -> dict[str, Any]:
    current = compile_incremental_candidate_contract(v2_root)
    failures: list[str] = []
    if frozen.get("status") != "PASS":
        failures.append("frozen D.4 contract is not PASS")
    if current.get("status") != "PASS":
        failures.extend(
            f"current:{item}" for item in (current.get("failures") or [])
        )
    if _strip_nonstructural(frozen) != _strip_nonstructural(current):
        failures.append(
            "frozen D.4 structural provider-requirements contract differs "
            "from current state"
        )
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": AUTHORITY_IDENTITY_MODE,
        "provider_binding_mode": PROVIDER_BINDING_MODE,
        "frozen": {
            "selected_asset_count": frozen.get("selected_asset_count"),
            "selected_edge_count": frozen.get("selected_edge_count"),
            "candidate_logic_group_count": frozen.get(
                "candidate_logic_group_count"
            ),
            "contract_sha256": frozen.get("contract_sha256"),
        },
        "current": {
            "selected_asset_count": current.get("selected_asset_count"),
            "selected_edge_count": current.get("selected_edge_count"),
            "candidate_logic_group_count": current.get(
                "candidate_logic_group_count"
            ),
            "contract_sha256": current.get("contract_sha256"),
        },
    }


def self_test() -> dict[str, Any]:
    tests = {
        "provider_binding_is_d1_logic_signature":
            PROVIDER_BINDING_MODE == "d1_candidate_logic_signature",
        "bootstrap_authority_positive": BOOTSTRAP_AUTHORITY_ROWS > 0,
        "provider_directory_declared": bool(PROVIDER_DIRECTORY),
    }
    failed = [name for name, ok in tests.items() if not ok]
    return {
        "status": "PASS" if not failed else "FAIL",
        "tests": tests,
        "failed": failed,
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "authority_identity_mode": AUTHORITY_IDENTITY_MODE,
    }


__all__ = [
    "AUTHORITY_IDENTITY_MODE",
    "BOOTSTRAP_AUTHORITY_ROWS",
    "COMPILER_VERSION",
    "PROVIDER_BINDING_MODE",
    "PROVIDER_DIRECTORY",
    "PhaseD4ContractError",
    "SCHEMA_VERSION",
    "compile_incremental_candidate_contract",
    "self_test",
    "validate_current_contract",
]
