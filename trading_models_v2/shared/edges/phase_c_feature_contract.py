"""Phase C.1 manifest-driven feature inventory and frozen DAG contract compiler.

This module is deliberately deployment/offline-only. It does not alter live
routing, edge mathematics, dependency generation, model scoring, or execution.

The compiler discovers the current selected edge universe from
``portfolio_live/live_manifest.json``. It never assumes a fixed number of
assets or edges. Adding an asset/edge therefore requires regeneration and
later numerical certification, not source-code edits to this compiler.

C.1 is structural inventory, not numerical equivalence authority:
* identical schema definitions become *canonical candidates* only;
* unique/subset features become addon candidates;
* conflicting definitions are preserved as separate variants;
* C.2/C.3 must prove exact batch/incremental parity before production routing.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "phase_c_feature_contract_v1"
COMPILER_VERSION = "phase_c1_manifest_driven_feature_dag_v1"

KNOWN_RAW_BAR_INPUTS = frozenset(
    {
        "Datetime",
        "Date",
        "Bar_Start",
        "Open",
        "High",
        "Low",
        "Close",
        "Ticks",
        "Threshold",
        "Volume",
        "Tick_Volume",
        "Spread",
        "Real_Volume",
    }
)
PHASE_AB_DEPENDENCY_INPUTS = frozenset(
    {
        "Open_FFD",
        "High_FFD",
        "Low_FFD",
        "Close_FFD",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
    }
)
PROB_STATE_RE = re.compile(r"^Prob_State_\d+$")

PHASE_AB_INVARIANTS = (
    "one coherent FFD+HMM dependency generation per asset/bar",
    "dependency generation must be READY before feature consumption",
    "normal live dependency frames remain bounded and disk-free",
    "normal feature consumers may not mutate dependency generation",
    "startup/recovery retains full authoritative history and fail-closed validation",
    "the 6000-bar research authority is not reduced by Phase C",
    "one immutable feature generation will later be shared by all edge consumers",
)


class PhaseCFeatureContractError(RuntimeError):
    """Raised for malformed compiler inputs or frozen-contract validation."""


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PhaseCFeatureContractError(
            f"Could not read JSON {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PhaseCFeatureContractError(f"Expected JSON object: {path}")
    return payload


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _relative(v2_root: Path, path: Path) -> str:
    root = Path(v2_root).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise PhaseCFeatureContractError(
            f"Phase C source path escapes V2 root: {resolved}"
        ) from exc


def _resolve(v2_root: Path, value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise PhaseCFeatureContractError("Required repository path is blank")
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = Path(v2_root) / candidate
    resolved = candidate.resolve()
    _relative(Path(v2_root), resolved)
    return resolved


def _classify_leaf(name: str) -> str:
    if name in PHASE_AB_DEPENDENCY_INPUTS or PROB_STATE_RE.match(name):
        return "phase_ab_dependency"
    if name in KNOWN_RAW_BAR_INPUTS:
        return "raw_bar"
    return "external_source"


def _semantic_feature_payload(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Return path-independent structural semantics for one declared feature."""
    required = feature.get("required_inputs") or []
    if not isinstance(required, list):
        required = list(required) if isinstance(required, tuple) else [required]
    return {
        "feature_name": str(feature.get("feature_name") or "").strip(),
        "dtype": str(feature.get("dtype") or "").strip(),
        "formula_description": _norm_text(feature.get("formula_description")),
        "required_inputs": [str(value).strip() for value in required if str(value).strip()],
        "owner_stage": str(feature.get("owner_stage") or "").strip(),
        "live_available": bool(feature.get("live_available", False)),
        "uses_future_information": bool(feature.get("uses_future_information", False)),
        "allowed_for_model_input": bool(feature.get("allowed_for_model_input", False)),
        "missing_value_policy": str(feature.get("missing_value_policy") or "").strip(),
        "feature_tier": feature.get("feature_tier"),
        "feature_group": str(feature.get("feature_group") or "").strip(),
    }


def _runtime_build_features_ast_hash(runtime_path: Path) -> str | None:
    """Hash build_features syntax without importing runtime code."""
    try:
        tree = ast.parse(Path(runtime_path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "build_features"
        ),
        None,
    )
    if node is None:
        return None
    node.name = "authoritative_build_features"
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
    """Conservative source fingerprint for one selected edge."""
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
    return {
        "files": rows,
        "bundle_sha256": _json_hash(rows),
    }


def _topological_feature_order(
    feature_names: Iterable[str],
    dependencies: Mapping[str, Iterable[str]],
) -> tuple[list[str], list[str]]:
    names = sorted(set(str(value) for value in feature_names))
    name_set = set(names)
    deps = {
        name: {str(dep) for dep in dependencies.get(name, ()) if str(dep) in name_set}
        for name in names
    }
    reverse: dict[str, set[str]] = {name: set() for name in names}
    indegree = {name: len(deps[name]) for name in names}
    for name, required in deps.items():
        for dep in required:
            reverse.setdefault(dep, set()).add(name)
    ready = sorted(name for name, count in indegree.items() if count == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for consumer in sorted(reverse.get(name, ())):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
                ready.sort()
    cyclic = sorted(name for name, count in indegree.items() if count > 0)
    return order, cyclic


def _edge_paths(v2_root: Path, edge: Mapping[str, Any]) -> dict[str, Path]:
    artifacts = edge.get("artifacts") or {}
    if not isinstance(artifacts, Mapping):
        raise PhaseCFeatureContractError(
            f"{edge.get('edge_id')}: artifacts must be a mapping"
        )
    edge_root_text = str(edge.get("edge_root") or "").strip()
    runtime_text = str(edge.get("runtime_module") or "").strip()
    if edge_root_text:
        edge_root = _resolve(v2_root, edge_root_text)
    elif runtime_text:
        edge_root = _resolve(v2_root, runtime_text).parent
    else:
        raise PhaseCFeatureContractError(
            f"{edge.get('edge_id')}: edge_root/runtime_module missing"
        )
    runtime_path = (
        _resolve(v2_root, runtime_text)
        if runtime_text
        else (edge_root / "runtime.py").resolve()
    )
    feature_schema = _resolve(v2_root, artifacts.get("feature_schema"))
    selected_features = _resolve(v2_root, artifacts.get("selected_features"))
    return {
        "edge_root": edge_root,
        "runtime": runtime_path,
        "feature_schema": feature_schema,
        "selected_features": selected_features,
    }


def _compile_edge(
    *,
    v2_root: Path,
    edge: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    edge_id = str(edge.get("edge_id") or "").strip()
    asset = str(edge.get("asset") or "").strip().upper()
    family = str(edge.get("family") or "").strip().lower()
    if not edge_id or not asset:
        raise PhaseCFeatureContractError(
            f"Selected edge is missing edge_id/asset: {dict(edge)}"
        )
    if "/" in edge_id and edge_id.split("/", 1)[0].upper() != asset:
        raise PhaseCFeatureContractError(
            f"{edge_id}: edge_id asset prefix does not match asset={asset}"
        )

    paths = _edge_paths(v2_root, edge)
    for label, path in paths.items():
        if label == "edge_root":
            if not path.is_dir():
                raise PhaseCFeatureContractError(
                    f"{edge_id}: edge root missing: {_relative(v2_root, path)}"
                )
        elif not path.is_file():
            raise PhaseCFeatureContractError(
                f"{edge_id}: {label} missing: {_relative(v2_root, path)}"
            )

    schema = _read_json(paths["feature_schema"])
    selected = _read_json(paths["selected_features"])
    if str(schema.get("edge_id") or edge_id) != edge_id:
        raise PhaseCFeatureContractError(
            f"{edge_id}: feature schema edge_id mismatch={schema.get('edge_id')}"
        )
    if str(selected.get("edge_id") or edge_id) != edge_id:
        raise PhaseCFeatureContractError(
            f"{edge_id}: selected_features edge_id mismatch={selected.get('edge_id')}"
        )

    raw_features = schema.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise PhaseCFeatureContractError(
            f"{edge_id}: feature_schema.features must be a non-empty list"
        )

    features: dict[str, dict[str, Any]] = {}
    for raw in raw_features:
        if not isinstance(raw, Mapping):
            raise PhaseCFeatureContractError(
                f"{edge_id}: feature_schema contains a non-object feature row"
            )
        normalized = _semantic_feature_payload(raw)
        name = normalized["feature_name"]
        if not name:
            raise PhaseCFeatureContractError(
                f"{edge_id}: feature_schema contains blank feature_name"
            )
        if name in features:
            raise PhaseCFeatureContractError(
                f"{edge_id}: duplicate feature declaration: {name}"
            )
        features[name] = normalized

    selected_list = selected.get("selected_features")
    if not isinstance(selected_list, list) or not selected_list:
        raise PhaseCFeatureContractError(
            f"{edge_id}: selected_features.selected_features must be non-empty"
        )
    selected_names = [str(value).strip() for value in selected_list if str(value).strip()]
    if len(selected_names) != len(set(selected_names)):
        raise PhaseCFeatureContractError(
            f"{edge_id}: selected feature list contains duplicates"
        )

    missing_selected = sorted(set(selected_names).difference(features))
    if missing_selected:
        raise PhaseCFeatureContractError(
            f"{edge_id}: selected features absent from feature schema: {missing_selected[:20]}"
        )

    invalid_selected: list[str] = []
    for name in selected_names:
        row = features[name]
        if row["uses_future_information"]:
            invalid_selected.append(f"{name}:uses_future_information")
        if not row["live_available"]:
            invalid_selected.append(f"{name}:not_live_available")
        if not row["allowed_for_model_input"]:
            invalid_selected.append(f"{name}:not_allowed_for_model_input")
    if invalid_selected:
        raise PhaseCFeatureContractError(
            f"{edge_id}: selected feature contract invalid: {invalid_selected[:20]}"
        )

    feature_names = set(features)
    feature_rows: list[dict[str, Any]] = []
    source_leaves: dict[str, set[str]] = defaultdict(set)
    for name in sorted(features):
        row = dict(features[name])
        semantic_hash = _json_hash(row)
        internal_inputs: list[str] = []
        leaf_inputs: list[dict[str, str]] = []
        for input_name in row["required_inputs"]:
            if input_name in feature_names:
                internal_inputs.append(input_name)
            else:
                kind = _classify_leaf(input_name)
                source_leaves[kind].add(input_name)
                leaf_inputs.append({"name": input_name, "kind": kind})
        feature_rows.append(
            {
                **row,
                "semantic_hash": semantic_hash,
                "internal_feature_inputs": internal_inputs,
                "source_inputs": leaf_inputs,
                "selected_for_model": name in selected_names,
            }
        )

    feature_schema_semantic_hash = _json_hash(
        [
            {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "semantic_hash",
                    "internal_feature_inputs",
                    "source_inputs",
                    "selected_for_model",
                }
            }
            for row in feature_rows
        ]
    )
    bundle = _edge_python_bundle(paths["edge_root"], v2_root)
    runtime_hash = _file_sha256(paths["runtime"])
    build_features_hash = _runtime_build_features_ast_hash(paths["runtime"])
    edge_contract_fingerprint = _json_hash(
        {
            "edge_id": edge_id,
            "asset": asset,
            "family": family,
            "feature_schema_semantic_hash": feature_schema_semantic_hash,
            "selected_features": selected_names,
            "runtime_sha256": runtime_hash,
            "build_features_ast_hash": build_features_hash,
            "edge_python_bundle_sha256": bundle["bundle_sha256"],
        }
    )

    review: list[str] = []
    if build_features_hash is None:
        review.append(
            f"{edge_id}: build_features is not a top-level function in runtime.py; "
            "C.2 must resolve its implementation source before numerical certification"
        )

    return (
        {
            "edge_id": edge_id,
            "asset": asset,
            "family": family,
            "edge_root": _relative(v2_root, paths["edge_root"]),
            "runtime_module": _relative(v2_root, paths["runtime"]),
            "feature_schema_path": _relative(v2_root, paths["feature_schema"]),
            "selected_features_path": _relative(v2_root, paths["selected_features"]),
            "feature_schema_file_sha256": _file_sha256(paths["feature_schema"]),
            "selected_features_file_sha256": _file_sha256(paths["selected_features"]),
            "runtime_file_sha256": runtime_hash,
            "build_features_ast_sha256": build_features_hash,
            "edge_python_bundle_sha256": bundle["bundle_sha256"],
            "edge_python_files": bundle["files"],
            "feature_schema_semantic_sha256": feature_schema_semantic_hash,
            "selected_feature_order_sha256": _json_hash(selected_names),
            "edge_feature_contract_fingerprint": edge_contract_fingerprint,
            "candidate_scope": str(schema.get("candidate_scope") or ""),
            "feature_count": len(feature_rows),
            "selected_feature_count": len(selected_names),
            "selected_features": selected_names,
            "features": feature_rows,
            "source_inputs": {
                kind: sorted(values) for kind, values in sorted(source_leaves.items())
            },
        },
        review,
    )


def _compile_asset(asset: str, edge_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered_edges = sorted(edge_rows, key=lambda row: str(row["edge_id"]))
    edge_ids = [str(row["edge_id"]) for row in ordered_edges]
    families = sorted({str(row["family"]) for row in ordered_edges})

    variants: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    feature_dependencies: dict[str, set[str]] = defaultdict(set)
    source_inputs: dict[str, set[str]] = defaultdict(set)

    for edge in ordered_edges:
        edge_id = str(edge["edge_id"])
        family = str(edge["family"])
        selected_set = set(edge.get("selected_features") or [])
        for feature in edge.get("features") or []:
            name = str(feature["feature_name"])
            semantic_hash = str(feature["semantic_hash"])
            bucket = variants[name].setdefault(
                semantic_hash,
                {
                    "semantic_hash": semantic_hash,
                    "definition": {
                        key: feature[key]
                        for key in (
                            "feature_name",
                            "dtype",
                            "formula_description",
                            "required_inputs",
                            "owner_stage",
                            "live_available",
                            "uses_future_information",
                            "allowed_for_model_input",
                            "missing_value_policy",
                            "feature_tier",
                            "feature_group",
                        )
                    },
                    "schema_consumer_edges": [],
                    "selected_model_consumer_edges": [],
                    "families": [],
                    "producer_edge_contract_fingerprints": [],
                    "internal_feature_inputs": [],
                    "source_inputs": [],
                },
            )
            bucket["schema_consumer_edges"].append(edge_id)
            if name in selected_set:
                bucket["selected_model_consumer_edges"].append(edge_id)
            bucket["families"].append(family)
            bucket["producer_edge_contract_fingerprints"].append(
                str(edge["edge_feature_contract_fingerprint"])
            )
            internal = list(feature.get("internal_feature_inputs") or [])
            feature_dependencies[name].update(internal)
            bucket["internal_feature_inputs"].extend(internal)
            for leaf in feature.get("source_inputs") or []:
                leaf_name = str(leaf["name"])
                leaf_kind = str(leaf["kind"])
                source_inputs[leaf_kind].add(leaf_name)
                bucket["source_inputs"].append(
                    {
                        "name": leaf_name,
                        "kind": leaf_kind,
                        "edge_id": edge_id,
                    }
                )

    feature_names = sorted(variants)
    order, cyclic = _topological_feature_order(feature_names, feature_dependencies)

    node_rows: list[dict[str, Any]] = []
    shared_core: list[str] = []
    addons: list[str] = []
    conflicts: list[str] = []
    for name in feature_names:
        variant_rows = []
        all_consumers: set[str] = set()
        for _semantic_hash, raw_bucket in sorted(variants[name].items()):
            bucket = dict(raw_bucket)
            for key in (
                "schema_consumer_edges",
                "selected_model_consumer_edges",
                "families",
                "producer_edge_contract_fingerprints",
                "internal_feature_inputs",
            ):
                bucket[key] = sorted(set(bucket[key]))
            source_triplets = sorted(
                {
                    (item["name"], item["kind"], item["edge_id"])
                    for item in bucket["source_inputs"]
                }
            )
            bucket["source_inputs"] = [
                {"name": name_, "kind": kind, "edge_id": edge_id}
                for name_, kind, edge_id in source_triplets
            ]
            all_consumers.update(bucket["schema_consumer_edges"])
            variant_rows.append(bucket)

        if len(variant_rows) > 1:
            classification = "variant_conflict_requires_numeric_certification"
            conflicts.append(name)
        elif set(edge_ids) == all_consumers:
            classification = "shared_core_candidate"
            shared_core.append(name)
        else:
            classification = "edge_or_family_addon_candidate"
            addons.append(name)

        node_rows.append(
            {
                "feature_name": name,
                "classification": classification,
                "variant_count": len(variant_rows),
                "schema_consumer_edge_count": len(all_consumers),
                "schema_consumer_edges": sorted(all_consumers),
                "internal_feature_dependencies": sorted(feature_dependencies.get(name, set())),
                "variants": variant_rows,
                "numerical_certification_required": True,
            }
        )

    contract_groups: dict[str, list[str]] = defaultdict(list)
    for edge in ordered_edges:
        group_key = _json_hash(
            {
                "feature_schema_semantic_sha256": edge["feature_schema_semantic_sha256"],
                "build_features_ast_sha256": edge["build_features_ast_sha256"],
            }
        )
        contract_groups[group_key].append(str(edge["edge_id"]))

    return {
        "asset": asset,
        "edge_count": len(edge_ids),
        "edge_ids": edge_ids,
        "families": families,
        "declared_feature_name_count": len(feature_names),
        "structural_feature_variant_count": sum(len(value) for value in variants.values()),
        "shared_core_candidate_count": len(shared_core),
        "addon_candidate_count": len(addons),
        "variant_conflict_count": len(conflicts),
        "shared_core_candidate_features": shared_core,
        "addon_candidate_features": addons,
        "variant_conflict_features": conflicts,
        "source_inputs": {
            kind: sorted(values) for kind, values in sorted(source_inputs.items())
        },
        "topological_feature_order": order,
        "cyclic_feature_dependencies": cyclic,
        "feature_nodes": node_rows,
        "structural_contract_groups": [
            {
                "group_id": group_id,
                "edge_count": len(sorted(set(group_edges))),
                "edge_ids": sorted(set(group_edges)),
            }
            for group_id, group_edges in sorted(contract_groups.items())
        ],
    }


def compile_feature_contract(
    *,
    v2_root: Path,
    live_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Compile the current manifest-driven Phase C.1 structural contract."""
    root = Path(v2_root).resolve()
    manifest_path = (
        Path(live_manifest_path).resolve()
        if live_manifest_path is not None
        else (root / "portfolio_live" / "live_manifest.json").resolve()
    )
    _relative(root, manifest_path)
    manifest = _read_json(manifest_path)
    selected_edges = manifest.get("selected_edges")
    if not isinstance(selected_edges, list):
        raise PhaseCFeatureContractError(
            "live_manifest.selected_edges must be a list"
        )

    fatal_errors: list[str] = []
    review_items: list[str] = []
    manifest_count = manifest.get("selected_edge_count")
    if manifest_count is not None:
        try:
            if int(manifest_count) != len(selected_edges):
                fatal_errors.append(
                    "live_manifest selected_edge_count mismatch: "
                    f"declared={manifest_count} actual={len(selected_edges)}"
                )
        except Exception:
            fatal_errors.append(
                f"live_manifest selected_edge_count is not an integer: {manifest_count!r}"
            )

    seen_ids: set[str] = set()
    compiled_edges: list[dict[str, Any]] = []
    object_edges = [edge for edge in selected_edges if isinstance(edge, Mapping)]
    for raw_edge in sorted(object_edges, key=lambda edge: str(edge.get("edge_id") or "")):
        edge_id = str(raw_edge.get("edge_id") or "").strip()
        if edge_id in seen_ids:
            fatal_errors.append(f"duplicate selected edge_id: {edge_id}")
            continue
        seen_ids.add(edge_id)
        try:
            compiled, edge_review = _compile_edge(v2_root=root, edge=raw_edge)
            compiled_edges.append(compiled)
            review_items.extend(edge_review)
        except Exception as exc:
            fatal_errors.append(
                f"{edge_id or '<unknown>'}: {type(exc).__name__}: {exc}"
            )

    non_mapping_count = sum(1 for edge in selected_edges if not isinstance(edge, Mapping))
    if non_mapping_count:
        fatal_errors.append(
            f"live_manifest selected_edges contains {non_mapping_count} non-object rows"
        )

    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in compiled_edges:
        by_asset[str(edge["asset"])].append(edge)

    assets: dict[str, dict[str, Any]] = {}
    for asset, rows in sorted(by_asset.items()):
        asset_contract = _compile_asset(asset, rows)
        if asset_contract["cyclic_feature_dependencies"]:
            fatal_errors.append(
                f"{asset}: feature dependency cycle detected: "
                f"{asset_contract['cyclic_feature_dependencies'][:20]}"
            )
        if asset_contract["variant_conflict_features"]:
            review_items.append(
                f"{asset}: {len(asset_contract['variant_conflict_features'])} feature names "
                "have multiple structural definitions; preserve variants until C.2 "
                "numerical certification decides canonical/addon ownership"
            )
        assets[asset] = asset_contract

    feature_names_global = sorted(
        {
            node["feature_name"]
            for asset in assets.values()
            for node in asset["feature_nodes"]
        }
    )
    structural_variant_count = sum(
        asset["structural_feature_variant_count"] for asset in assets.values()
    )
    shared_core_count = sum(
        asset["shared_core_candidate_count"] for asset in assets.values()
    )
    addon_count = sum(asset["addon_candidate_count"] for asset in assets.values())
    conflict_count = sum(asset["variant_conflict_count"] for asset in assets.values())

    if len(compiled_edges) != len(selected_edges):
        fatal_errors.append(
            "compiled selected edge count does not equal live manifest selected edge count: "
            f"compiled={len(compiled_edges)} manifest={len(selected_edges)}"
        )

    # Keep the frozen artifact compact: per-feature definitions live once in
    # the asset DAG. Edge records retain source fingerprints and model-selected
    # feature order but do not duplicate their entire feature schema.
    public_edges = [
        {key: value for key, value in edge.items() if key != "features"}
        for edge in compiled_edges
    ]

    deterministic_payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "status": "PASS" if not fatal_errors else "FAIL",
        "runtime_routing_enabled": False,
        "scope": "deployment_time_structural_inventory_only",
        "source_live_manifest_path": _relative(root, manifest_path),
        "source_live_manifest_sha256": _file_sha256(manifest_path),
        "selected_edge_count": len(selected_edges),
        "compiled_edge_count": len(compiled_edges),
        "selected_asset_count": len(assets),
        "selected_assets": sorted(assets),
        "global_declared_feature_name_count": len(feature_names_global),
        "global_structural_feature_variant_count": structural_variant_count,
        "asset_shared_core_candidate_count_sum": shared_core_count,
        "asset_addon_candidate_count_sum": addon_count,
        "asset_variant_conflict_count_sum": conflict_count,
        "all_declared_feature_names": feature_names_global,
        "phase_ab_invariants": list(PHASE_AB_INVARIANTS),
        "scalability_contract": {
            "asset_discovery": "derived_from_live_manifest_selected_edges",
            "edge_discovery": "derived_from_live_manifest_selected_edges",
            "fixed_asset_list": False,
            "fixed_edge_count": False,
            "new_edge_policy": "regenerate_contract_and_recertify_before_phase_c_runtime_use",
            "new_asset_policy": "regenerate_contract_and_recertify_before_phase_c_runtime_use",
        },
        "feature_scope_policy": {
            "inventory_basis": "union_of_all_features_declared_by_each_selected_edge_feature_schema",
            "why_full_schema_not_model_only": (
                "candidate/event logic may depend on declared features that are not selected "
                "model inputs; C.1 conservatively retains the full declared live feature universe"
            ),
            "structural_identity_is_not_numerical_equivalence": True,
            "numerical_equivalence_authority": "future_phase_c_certification_against_unchanged_batch_runtime",
        },
        "fatal_errors": fatal_errors,
        "review_items": sorted(set(review_items)),
        "edges": public_edges,
        "assets": assets,
    }
    contract_hash = _json_hash(deterministic_payload)
    return {**deterministic_payload, "contract_sha256": contract_hash}


def contract_summary(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": contract.get("status"),
        "schema_version": contract.get("schema_version"),
        "compiler_version": contract.get("compiler_version"),
        "runtime_routing_enabled": contract.get("runtime_routing_enabled"),
        "selected_asset_count": contract.get("selected_asset_count"),
        "selected_edge_count": contract.get("selected_edge_count"),
        "compiled_edge_count": contract.get("compiled_edge_count"),
        "global_declared_feature_name_count": contract.get("global_declared_feature_name_count"),
        "global_structural_feature_variant_count": contract.get("global_structural_feature_variant_count"),
        "asset_shared_core_candidate_count_sum": contract.get("asset_shared_core_candidate_count_sum"),
        "asset_addon_candidate_count_sum": contract.get("asset_addon_candidate_count_sum"),
        "asset_variant_conflict_count_sum": contract.get("asset_variant_conflict_count_sum"),
        "fatal_error_count": len(contract.get("fatal_errors") or []),
        "review_item_count": len(contract.get("review_items") or []),
        "contract_sha256": contract.get("contract_sha256"),
    }


def validate_frozen_contract(
    *,
    v2_root: Path,
    contract_path: Path,
    live_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Validate current structural feature authority without provenance hash gates."""
    frozen = _read_json(Path(contract_path))
    current = compile_feature_contract(
        v2_root=Path(v2_root),
        live_manifest_path=live_manifest_path,
    )
    failures: list[str] = []

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                text = str(key).lower()
                if (
                    "sha256" in text
                    or "fingerprint" in text
                    or text.endswith("_hash")
                    or text.endswith("_ast_hash")
                    or key in {"created_at", "generated_at", "v2_root"}
                ):
                    continue
                out[str(key)] = clean(item)
            return out
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    if frozen.get("schema_version") != SCHEMA_VERSION:
        failures.append(
            f"schema_version mismatch frozen={frozen.get('schema_version')} expected={SCHEMA_VERSION}"
        )
    if frozen.get("status") != "PASS":
        failures.append(f"frozen contract status is {frozen.get('status')}")
    if current.get("status") != "PASS":
        failures.append(
            f"current recompilation status is {current.get('status')}: "
            f"{(current.get('fatal_errors') or [])[:5]}"
        )

    frozen_edges = {
        str(row.get("edge_id")): clean(row)
        for row in frozen.get("edges") or []
        if isinstance(row, Mapping) and row.get("edge_id")
    }
    current_edges = {
        str(row.get("edge_id")): clean(row)
        for row in current.get("edges") or []
        if isinstance(row, Mapping) and row.get("edge_id")
    }
    if frozen_edges != current_edges:
        failures.append(
            "feature contract structural edge/schema/selected-feature state changed; "
            "regenerate and recertify Phase C"
        )

    if clean(frozen.get("assets") or {}) != clean(current.get("assets") or {}):
        failures.append(
            "feature contract structural asset DAG state changed; regenerate and recertify Phase C"
        )

    if sorted(str(x).upper() for x in frozen.get("selected_assets") or []) != sorted(
        str(x).upper() for x in current.get("selected_assets") or []
    ):
        failures.append("feature contract selected-asset universe changed")
    if int(frozen.get("selected_edge_count") or -1) != int(
        current.get("selected_edge_count") or -1
    ):
        failures.append("feature contract selected-edge count changed")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "frozen": contract_summary(frozen),
        "current": contract_summary(current),
    }



__all__ = [
    "SCHEMA_VERSION",
    "COMPILER_VERSION",
    "KNOWN_RAW_BAR_INPUTS",
    "PHASE_AB_DEPENDENCY_INPUTS",
    "PHASE_AB_INVARIANTS",
    "PhaseCFeatureContractError",
    "compile_feature_contract",
    "contract_summary",
    "validate_frozen_contract",
]
