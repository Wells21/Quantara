"""Phase C.3 manifest-driven edge consumer contract compiler.

C.3 does not change production routing.  C.2 already certified the compact
canonical feature engine.  C.3 freezes the *consumer* boundary:

    canonical asset feature contract
        -> edge-required addon projection
        -> candidate/event consumer
        -> live-safe model/scoring rows

The compiler is deliberately dynamic:
* assets/edges come from the current live manifest and frozen C.1 contract;
* addon ownership comes from C.1 feature-node consumer sets;
* selected model features come from each frozen edge record;
* consumer source fingerprints come from the actual edge package/runtime.

Any universe, selected-feature, edge-source, C.1, C.2, or manifest change
invalidates the frozen C.3 contract and requires regeneration/re-certification.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "phase_c3_edge_consumer_contract_v1_2"
COMPILER_VERSION = "phase_c3_manifest_driven_consumer_contract_v1_2_canonical_model_input_ownership"

REQUIRED_RUNTIME_CONSUMER_FUNCTIONS = (
    "generate_candidate_events",
    "build_model_rows",
    "_build_scoring_rows",
    "_build_live_candidate_model_rows",
)

RAW_CONSUMER_COLUMNS = (
    "Datetime",
    "Bar_Start",
    "Open",
    "High",
    "Low",
    "Close",
    "Ticks",
    "Threshold",
    "edge_id",
    "side",
)

# These are private canonical aliases already produced by the certified C.2
# mathematical kernel and retained in its bounded feature_tail.  They are NOT
# public model-schema features.  C.3 discovers which selected candidate sources
# actually reference them, freezes that per-edge control projection, and then
# numerically certifies both candidate parity and C.2 continuation parity.
PRIVATE_CANDIDATE_CONTROL_COLUMNS = (
    "ATR_14",
    "Bar_Volatility_Bps",
)

CONSUMER_ADAPTER_RELATIVE_PATH = "shared/edges/phase_c_consumer_adapter.py"

PHASE_C3_INVARIANTS = (
    "Phase A/B remain sole owners of FFD/HMM dependency generation",
    "Phase C.2 compact feature math remains unchanged",
    "production routing remains disabled throughout C.3",
    "selected model features are sourced from the certified feature table",
    "candidate/event context may consume separately frozen private canonical controls but cannot replace selected model features",
    "selected model-feature collisions are stripped from candidate-event context only after candidate detection and before live model-row assembly",
    "private candidate controls remain outside the public model feature schema and are sourced from C.2 bounded internal state",
    "edge addon projection is derived from the frozen manifest/C.1 contract, never from a fixed asset list",
    "edge source or selected-feature changes invalidate the C.3 contract",
)


class PhaseCConsumerContractError(RuntimeError):
    """Raised when the frozen consumer contract cannot be trusted."""


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


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PhaseCConsumerContractError(
            f"Could not read JSON {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PhaseCConsumerContractError(f"Expected JSON object: {path}")
    return payload


def _relative(v2_root: Path, path: Path) -> str:
    root = Path(v2_root).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PhaseCConsumerContractError(
            f"Consumer-contract source escapes V2 root: {resolved}"
        ) from exc


def _resolve(v2_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(v2_root) / path
    path = path.resolve()
    _relative(Path(v2_root), path)
    return path


def _function_ast_hash(runtime_path: Path, function_name: str) -> str | None:
    try:
        tree = ast.parse(Path(runtime_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == function_name
        ),
        None,
    )
    if node is None:
        return None
    node.name = "phase_c3_consumer_function"
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(getattr(node.body[0], "value", None), ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return hashlib.sha256(
        ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8")
    ).hexdigest()


def _edge_python_bundle(edge_root: Path, v2_root: Path) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    for path in sorted(Path(edge_root).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rows.append(
            {
                "path": _relative(v2_root, path),
                "sha256": _file_sha256(path),
            }
        )
    return {"files": rows, "bundle_sha256": _json_hash(rows)}


def _candidate_private_controls(
    edge_root: Path,
    *,
    v2_root: Path,
) -> tuple[Path, list[str], str]:
    """Discover private canonical control references from candidate source.

    This is intentionally source-driven, not an edge/family lookup table.
    Adding a new edge that references a new private canonical control therefore
    fails closed until that control is deliberately added to the certified
    canonical-private-control universe and C.3 is rerun.
    """
    candidate_path = (Path(edge_root) / "candidate_events.py").resolve()
    _relative(v2_root, candidate_path)
    if not candidate_path.is_file():
        raise PhaseCConsumerContractError(
            f"candidate_events.py missing: {_relative(v2_root, candidate_path)}"
        )
    try:
        tree = ast.parse(candidate_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PhaseCConsumerContractError(
            f"Could not parse candidate source {candidate_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    literal_strings = {
        str(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    controls = sorted(
        set(PRIVATE_CANDIDATE_CONTROL_COLUMNS).intersection(literal_strings)
    )
    return candidate_path, controls, _file_sha256(candidate_path)


def _manifest_edge_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = manifest.get("selected_edges")
    if not isinstance(rows, list):
        raise PhaseCConsumerContractError("live_manifest.selected_edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseCConsumerContractError("live_manifest contains non-object selected edge")
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            raise PhaseCConsumerContractError("live_manifest selected edge has blank edge_id")
        if edge_id in out:
            raise PhaseCConsumerContractError(f"duplicate live_manifest edge_id: {edge_id}")
        out[edge_id] = dict(raw)
    return out


def _c1_edge_map(c1: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = c1.get("edges")
    if not isinstance(rows, list):
        raise PhaseCConsumerContractError("C.1 contract edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseCConsumerContractError("C.1 contract contains non-object edge")
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            raise PhaseCConsumerContractError("C.1 contract edge has blank edge_id")
        if edge_id in out:
            raise PhaseCConsumerContractError(f"duplicate C.1 edge_id: {edge_id}")
        out[edge_id] = dict(raw)
    return out


def _asset_contract(c1: Mapping[str, Any], asset: str) -> dict[str, Any]:
    row = (c1.get("assets") or {}).get(str(asset).upper())
    if not isinstance(row, Mapping):
        raise PhaseCConsumerContractError(f"{asset}: missing C.1 asset contract")
    return dict(row)


def _edge_schema_features(asset_contract: Mapping[str, Any], edge_id: str) -> list[str]:
    names: list[str] = []
    for node in asset_contract.get("feature_nodes") or []:
        if not isinstance(node, Mapping):
            continue
        consumers = {str(x) for x in node.get("schema_consumer_edges") or []}
        if edge_id in consumers:
            names.append(str(node.get("feature_name")))
    return sorted(set(names))


def _edge_required_addons(asset_contract: Mapping[str, Any], edge_id: str) -> list[str]:
    addon_set = {str(x) for x in asset_contract.get("addon_candidate_features") or []}
    return [name for name in _edge_schema_features(asset_contract, edge_id) if name in addon_set]


def _validate_c1_c2(
    *,
    v2_root: Path,
    manifest_path: Path,
    c1_path: Path,
    c2_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate current C1/C2 authority structurally; provenance hashes are audit-only."""
    manifest = _read_json(manifest_path)
    c1 = _read_json(c1_path)
    c2 = _read_json(c2_path)

    if c1.get("status") != "PASS" or c1.get("runtime_routing_enabled") is not False:
        raise PhaseCConsumerContractError("C.1 contract must be PASS with routing disabled")
    if c2.get("status") != "PASS" or c2.get("runtime_routing_enabled") is not False:
        raise PhaseCConsumerContractError("C.2 certification must be PASS with routing disabled")
    if int(c2.get("bootstrap_rows") or -1) < 6000:
        raise PhaseCConsumerContractError("C.2 certification is below 6000-row authority")
    if int(c2.get("sequential_steps") or -1) < 128:
        raise PhaseCConsumerContractError("C.2 certification is below 128 sequential steps")
    return manifest, c1, c2



def compile_consumer_contract(
    *,
    v2_root: Path,
    live_manifest_path: Path | None = None,
    phase_c1_contract_path: Path | None = None,
    phase_c2_certification_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(v2_root).resolve()
    manifest_path = (
        Path(live_manifest_path).resolve()
        if live_manifest_path is not None
        else (root / "portfolio_live" / "live_manifest.json").resolve()
    )
    c1_path = (
        Path(phase_c1_contract_path).resolve()
        if phase_c1_contract_path is not None
        else (root / "portfolio_live" / "config" / "phase_c" / "feature_contract.json").resolve()
    )
    c2_path = (
        Path(phase_c2_certification_path).resolve()
        if phase_c2_certification_path is not None
        else (
            root
            / "portfolio_live"
            / "config"
            / "phase_c"
            / "incremental_feature_certification.json"
        ).resolve()
    )
    for path in (manifest_path, c1_path, c2_path):
        _relative(root, path)
        if not path.is_file():
            raise PhaseCConsumerContractError(f"Required C.3 input missing: {path}")

    manifest, c1, c2 = _validate_c1_c2(
        v2_root=root,
        manifest_path=manifest_path,
        c1_path=c1_path,
        c2_path=c2_path,
    )
    manifest_edges = _manifest_edge_map(manifest)
    c1_edges = _c1_edge_map(c1)
    if set(manifest_edges) != set(c1_edges):
        missing = sorted(set(manifest_edges).difference(c1_edges))
        extra = sorted(set(c1_edges).difference(manifest_edges))
        raise PhaseCConsumerContractError(
            f"C.1/live-manifest universe mismatch missing_in_c1={missing[:10]} extra_in_c1={extra[:10]}"
        )

    selected_assets = sorted({str(row.get("asset") or "").upper() for row in manifest_edges.values()})
    if int(c2.get("selected_edge_count") or -1) != len(manifest_edges):
        raise PhaseCConsumerContractError("C.2 selected-edge count differs from live manifest")
    if sorted(str(x).upper() for x in c2.get("selected_assets") or []) != selected_assets:
        raise PhaseCConsumerContractError("C.2 selected-asset universe differs from live manifest")

    fatal_errors: list[str] = []
    compiled_edges: list[dict[str, Any]] = []
    addon_registry: dict[str, dict[str, list[str]]] = {}

    for edge_id in sorted(manifest_edges):
        c1_edge = c1_edges[edge_id]
        manifest_edge = manifest_edges[edge_id]
        asset = str(c1_edge.get("asset") or manifest_edge.get("asset") or "").upper()
        family = str(c1_edge.get("family") or manifest_edge.get("family") or "").lower()
        asset_row = _asset_contract(c1, asset)

        if asset_row.get("variant_conflict_features"):
            fatal_errors.append(
                f"{asset}: unresolved C.1 feature variants remain: "
                f"{asset_row.get('variant_conflict_features')[:10]}"
            )
            continue

        schema_features = _edge_schema_features(asset_row, edge_id)
        core_candidates = {str(x) for x in asset_row.get("shared_core_candidate_features") or []}
        addon_candidates = {str(x) for x in asset_row.get("addon_candidate_features") or []}
        core_features = [name for name in schema_features if name in core_candidates]
        addon_features = [name for name in schema_features if name in addon_candidates]
        unresolved = sorted(set(schema_features).difference(core_candidates).difference(addon_candidates))
        if unresolved:
            fatal_errors.append(f"{edge_id}: schema features outside core/addon contract: {unresolved[:20]}")
            continue

        selected_features = [str(x) for x in c1_edge.get("selected_features") or []]
        missing_selected = sorted(set(selected_features).difference(schema_features))
        if missing_selected:
            fatal_errors.append(
                f"{edge_id}: selected features outside edge schema contract: {missing_selected[:20]}"
            )
            continue

        edge_root = _resolve(root, str(c1_edge.get("edge_root") or ""))
        runtime_path = _resolve(root, str(c1_edge.get("runtime_module") or ""))
        if not edge_root.is_dir() or not runtime_path.is_file():
            fatal_errors.append(f"{edge_id}: edge root/runtime missing")
            continue

        try:
            candidate_path, candidate_controls, candidate_source_sha256 = (
                _candidate_private_controls(edge_root, v2_root=root)
            )
        except Exception as exc:
            fatal_errors.append(
                f"{edge_id}: candidate-control discovery failed: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        bundle = _edge_python_bundle(edge_root, root)
        # SINGLE_OPERATOR_AUTHORITY: historical SHA equality is audit-only.
        # SINGLE_OPERATOR_AUTHORITY: historical SHA equality is audit-only.

        function_hashes = {
            name: _function_ast_hash(runtime_path, name)
            for name in (*REQUIRED_RUNTIME_CONSUMER_FUNCTIONS, "score")
        }
        missing_functions = [
            name for name in REQUIRED_RUNTIME_CONSUMER_FUNCTIONS if not function_hashes.get(name)
        ]
        if missing_functions:
            fatal_errors.append(
                f"{edge_id}: runtime missing required C.3 consumer functions: {missing_functions}"
            )
            continue

        consumer_source_payload = {
            "edge_python_bundle_sha256": bundle["bundle_sha256"],
            "runtime_file_sha256": _file_sha256(runtime_path),
            "candidate_events_file_sha256": candidate_source_sha256,
            "runtime_consumer_function_ast_sha256": function_hashes,
        }
        consumer_contract_payload = {
            "edge_id": edge_id,
            "asset": asset,
            "family": family,
            "schema_features": schema_features,
            "core_features": core_features,
            "required_addon_features": addon_features,
            "candidate_control_columns": candidate_controls,
            "selected_features": selected_features,
            "consumer_source": consumer_source_payload,
        }

        compiled_edges.append(
            {
                **consumer_contract_payload,
                "candidate_scope": str(c1_edge.get("candidate_scope") or ""),
                "edge_root": _relative(root, edge_root),
                "runtime_module": _relative(root, runtime_path),
                "candidate_events_module": _relative(root, candidate_path),
                "schema_feature_count": len(schema_features),
                "core_feature_count": len(core_features),
                "required_addon_feature_count": len(addon_features),
                "candidate_control_count": len(candidate_controls),
                "selected_feature_count": len(selected_features),
                "consumer_contract_fingerprint": _json_hash(consumer_contract_payload),
                "selected_feature_source_policy": "certified_feature_table_only",
                "selected_feature_event_collision_policy": "strip_after_candidate_detection_before_live_model_row_build",
                "event_context_policy": "may_add_control_context_but_may_not_override_selected_features",
                "canonical_projection_columns": list(
                    dict.fromkeys(
                        [
                            *RAW_CONSUMER_COLUMNS,
                            *schema_features,
                            *candidate_controls,
                        ]
                    )
                ),
            }
        )
        addon_registry.setdefault(asset, {})[edge_id] = addon_features

    if len(compiled_edges) != len(manifest_edges):
        fatal_errors.append(
            f"compiled edge count mismatch compiled={len(compiled_edges)} manifest={len(manifest_edges)}"
        )

    deterministic: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "status": "PASS" if not fatal_errors else "FAIL",
        "runtime_routing_enabled": False,
        "scope": "phase_c3_consumer_contract_offline_only",
        "selected_asset_count": len(selected_assets),
        "selected_assets": selected_assets,
        "selected_edge_count": len(manifest_edges),
        "compiled_edge_count": len(compiled_edges),
        "source_live_manifest_path": _relative(root, manifest_path),
        "source_live_manifest_sha256": _file_sha256(manifest_path),
        "phase_c1_contract_path": _relative(root, c1_path),
        "phase_c1_contract_file_sha256": _file_sha256(c1_path),
        "phase_c1_contract_sha256": str(c1.get("contract_sha256") or ""),
        "phase_c2_certification_path": _relative(root, c2_path),
        "phase_c2_certification_file_sha256": _file_sha256(c2_path),
        "phase_c2_engine_version": str(c2.get("engine_version") or ""),
        "phase_c2_implementation_fingerprint": dict(c2.get("implementation_fingerprint") or {}),
        "raw_consumer_columns": list(RAW_CONSUMER_COLUMNS),
        "private_candidate_control_universe": list(PRIVATE_CANDIDATE_CONTROL_COLUMNS),
        "required_runtime_consumer_functions": list(REQUIRED_RUNTIME_CONSUMER_FUNCTIONS),
        "consumer_adapter_path": CONSUMER_ADAPTER_RELATIVE_PATH,
        "consumer_adapter_file_sha256": _file_sha256(root / CONSUMER_ADAPTER_RELATIVE_PATH),
        "phase_c3_invariants": list(PHASE_C3_INVARIANTS),
        "scalability_contract": {
            "asset_discovery": "derived_from_live_manifest_and_frozen_c1",
            "edge_discovery": "derived_from_live_manifest_and_frozen_c1",
            "addon_registry": "derived_per_edge_from_c1_feature_node_schema_consumer_sets",
            "candidate_control_registry": "derived_per_edge_from_candidate_source_literal_references_to_certified_private_control_universe",
            "fixed_asset_list": False,
            "fixed_edge_count": False,
            "new_asset_or_edge_policy": "regenerate_c1_c2_c3_and_recertify_before_runtime_use",
        },
        "consumer_policy": {
            "canonical_projection": "raw_identity_plus_exact_edge_schema_features_plus_source_discovered_private_candidate_controls",
            "candidate_event_control_fields": "separate_private_canonical_surface_outside_model_schema",
            "selected_model_feature_source": "canonical_feature_table_only",
            "selected_feature_event_collision_policy": "strip_after_candidate_detection_before_live_model_row_build",
            "research_or_future_columns_in_live_rows": "forbidden",
            "hidden_edge_build_extras": "all_extras_beyond_schema_and_frozen_candidate_controls_must_be_proven_non_required_by_c3_numeric_certification",
        },
        "addon_registry": addon_registry,
        "edges": compiled_edges,
        "fatal_errors": fatal_errors,
    }
    deterministic["contract_sha256"] = _json_hash(deterministic)
    return deterministic


def summarize_consumer_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    edges = list(contract.get("edges") or [])
    addon_counts = [int(row.get("required_addon_feature_count") or 0) for row in edges]
    control_counts = [int(row.get("candidate_control_count") or 0) for row in edges]
    control_consumers = {
        name: sum(
            1 for row in edges if name in set(row.get("candidate_control_columns") or [])
        )
        for name in PRIVATE_CANDIDATE_CONTROL_COLUMNS
    }
    return {
        "schema_version": contract.get("schema_version"),
        "compiler_version": contract.get("compiler_version"),
        "status": contract.get("status"),
        "runtime_routing_enabled": contract.get("runtime_routing_enabled"),
        "selected_asset_count": contract.get("selected_asset_count"),
        "selected_edge_count": contract.get("selected_edge_count"),
        "compiled_edge_count": contract.get("compiled_edge_count"),
        "edge_with_addons_count": sum(1 for value in addon_counts if value > 0),
        "edge_without_addons_count": sum(1 for value in addon_counts if value == 0),
        "max_required_addon_feature_count": max(addon_counts) if addon_counts else 0,
        "edge_with_candidate_controls_count": sum(1 for value in control_counts if value > 0),
        "edge_without_candidate_controls_count": sum(1 for value in control_counts if value == 0),
        "max_candidate_control_count": max(control_counts) if control_counts else 0,
        "candidate_control_consumer_counts": control_consumers,
        "fatal_error_count": len(contract.get("fatal_errors") or []),
        "contract_sha256": contract.get("contract_sha256"),
    }


def validate_frozen_consumer_contract(
    *,
    v2_root: Path,
    frozen_path: Path,
    live_manifest_path: Path | None = None,
    phase_c1_contract_path: Path | None = None,
    phase_c2_certification_path: Path | None = None,
) -> dict[str, Any]:
    """Validate C3 structural consumer equivalence without file/contract SHA gates."""
    frozen = _read_json(Path(frozen_path))
    current = compile_consumer_contract(
        v2_root=Path(v2_root),
        live_manifest_path=live_manifest_path,
        phase_c1_contract_path=phase_c1_contract_path,
        phase_c2_certification_path=phase_c2_certification_path,
    )
    failures: list[str] = []

    def signature(row: Mapping[str, Any]) -> dict[str, Any]:
        keys = (
            "edge_id", "asset", "family", "schema_features", "core_features",
            "required_addon_features", "candidate_control_columns", "selected_features",
            "candidate_scope", "edge_root", "runtime_module", "candidate_events_module",
            "canonical_projection_columns", "selected_feature_source_policy",
            "selected_feature_event_collision_policy", "event_context_policy",
        )
        return {key: row.get(key) for key in keys}

    if frozen.get("status") != "PASS":
        failures.append(f"frozen contract status={frozen.get('status')!r}")
    if frozen.get("runtime_routing_enabled") is not False:
        failures.append("frozen contract unexpectedly enables runtime routing")
    if current.get("status") != "PASS":
        failures.append(
            "current C.3 compiler is not PASS: "
            + " | ".join(str(x) for x in current.get("fatal_errors") or [])
        )

    frozen_edges = {
        str(row.get("edge_id")): signature(row)
        for row in frozen.get("edges") or []
        if isinstance(row, Mapping) and row.get("edge_id")
    }
    current_edges = {
        str(row.get("edge_id")): signature(row)
        for row in current.get("edges") or []
        if isinstance(row, Mapping) and row.get("edge_id")
    }
    if frozen_edges != current_edges:
        failures.append("frozen/current C.3 structural consumer projections differ")

    if sorted(str(x).upper() for x in frozen.get("selected_assets") or []) != sorted(
        str(x).upper() for x in current.get("selected_assets") or []
    ):
        failures.append("frozen/current C.3 selected assets differ")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "frozen": summarize_consumer_contract(frozen),
        "current": summarize_consumer_contract(current),
    }
