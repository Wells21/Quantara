"""Phase D.1 manifest-driven candidate-detector contract compiler.

Phase C proved exact incremental *features*. Phase D.1 does not change trading
logic. It inventories the next hot-path boundary:

    one immutable canonical asset generation
        -> edge candidate detector
        -> primary-side filter (when present)
        -> model rows / scorer only when a current candidate exists

The compiler is deliberately source-driven. It does not contain an asset list,
edge count, family lookup table, or hand-maintained candidate registry.

The generated contract is NOT permission to route incrementally. It exists to
tell Phase D.2 exactly what candidate source/state shapes are present and which
selected edges are genuinely source/logic compatible.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "phase_d1_candidate_detector_contract_v2"
COMPILER_VERSION = "phase_d1_manifest_source_candidate_inventory_v2_full_consumer_columns"

IDENTITY_CONSTANTS = {
    "EDGE_ID",
    "ASSET",
    "FAMILY",
    "SIDE_NAME",
    "SIDE_VALUE",
    "CANDIDATE_EVENT_GENERATOR",
}

HISTORY_METHODS = {
    "rolling",
    "ewm",
    "expanding",
    "shift",
    "diff",
    "pct_change",
    "cummax",
    "cummin",
    "cumsum",
    "cumprod",
    "groupby",
    "resample",
}

LOOKBACK_NAME_TOKENS = (
    "LOOKBACK",
    "WINDOW",
    "PERIOD",
    "SPAN",
    "MIN_OBS",
    "GAP",
    "LAG",
    "HORIZON",
)

RAW_IDENTITY_COLUMNS = {"edge_id", "side"}


class PhaseDCandidateContractError(RuntimeError):
    """Raised when the Phase D.1 source inventory cannot be trusted."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
        raise PhaseDCandidateContractError(
            f"Could not read JSON {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PhaseDCandidateContractError(f"Expected JSON object: {path}")
    return payload


def _relative(root: Path, path: Path) -> str:
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PhaseDCandidateContractError(
            f"Phase D source escapes V2 root: {path}"
        ) from exc


def _parse(path: Path) -> tuple[str, ast.Module]:
    try:
        source = Path(path).read_text(encoding="utf-8-sig")
        return source, ast.parse(source)
    except Exception as exc:
        raise PhaseDCandidateContractError(
            f"Could not parse Python source {path}: {type(exc).__name__}: {exc}"
        ) from exc


def _function_map(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _import_aliases(tree: ast.Module, module_name: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if str(node.module or "") != module_name:
            continue
        for item in node.names:
            aliases[str(item.asname or item.name)] = str(item.name)
    return aliases


def _called_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            out.add(str(func.id))
    return out


def _reachable_functions(
    function_map: Mapping[str, ast.AST],
    entry_names: Iterable[str],
) -> list[str]:
    queue = [str(x) for x in entry_names if str(x) in function_map]
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        node = function_map[name]
        for called in sorted(_called_names(node)):
            if called in function_map and called not in seen:
                queue.append(called)
    return sorted(seen)


def _literal_constants(tree: ast.Module) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets: list[ast.AST] = []
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        else:
            targets = [node.target]
            value_node = node.value
        if value_node is None:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if len(names) != 1:
            continue
        try:
            value = ast.literal_eval(value_node)
        except Exception:
            continue
        if isinstance(value, tuple):
            value = list(value)
        elif isinstance(value, set):
            value = sorted(value, key=str)
        if isinstance(value, (str, int, float, bool, list, dict, type(None))):
            out[str(names[0])] = value
    return out


def _loaded_names(nodes: Iterable[ast.AST]) -> set[str]:
    out: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                out.add(str(child.id))
    return out


def _function_source(source: str, node: ast.AST) -> str:
    lines = source.splitlines()
    start = max(int(getattr(node, "lineno", 1)) - 1, 0)
    end = int(getattr(node, "end_lineno", start + 1))
    segment = "\n".join(lines[start:end]).strip()
    return segment.replace("\r\n", "\n").replace("\r", "\n")


def _logic_signature(
    *,
    source: str,
    function_map: Mapping[str, ast.AST],
    reachable: list[str],
    constants: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    nodes = [function_map[name] for name in reachable if name in function_map]
    referenced_names = _loaded_names(nodes)
    semantic_constants = {
        name: constants[name]
        for name in sorted(referenced_names.intersection(constants))
        if name not in IDENTITY_CONSTANTS
    }
    function_sources = [
        {
            "name": name,
            "source": _function_source(source, function_map[name]),
        }
        for name in reachable
        if name in function_map
    ]
    payload = {
        "functions": function_sources,
        "semantic_constants": semantic_constants,
    }
    return _json_hash(payload), semantic_constants


def _history_profile(
    *,
    nodes: Iterable[ast.AST],
    semantic_constants: Mapping[str, Any],
) -> dict[str, Any]:
    methods: set[str] = set()
    has_for = False
    has_while = False
    has_array_iteration = False
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                method = str(child.func.attr)
                if method in HISTORY_METHODS:
                    methods.add(method)
                if method in {"to_numpy", "tolist", "iterrows", "itertuples"}:
                    has_array_iteration = True
            elif isinstance(child, ast.For):
                has_for = True
            elif isinstance(child, ast.While):
                has_while = True

    lookback_hints: dict[str, Any] = {}
    numeric_hints: list[int] = []
    for name, value in semantic_constants.items():
        upper = str(name).upper()
        if not any(token in upper for token in LOOKBACK_NAME_TOKENS):
            continue
        lookback_hints[name] = value
        if isinstance(value, int) and value >= 0:
            numeric_hints.append(int(value))

    has_ewm = "ewm" in methods
    has_rolling = "rolling" in methods or "expanding" in methods
    has_shift = bool({"shift", "diff", "pct_change"}.intersection(methods))
    has_spacing_state = any(
        "GAP" in str(name).upper()
        for name in semantic_constants
    ) or has_for or has_while

    return {
        "history_methods": sorted(methods),
        "has_python_for_loop": has_for,
        "has_python_while_loop": has_while,
        "has_array_iteration_hint": has_array_iteration,
        "has_rolling_state": has_rolling,
        "has_ewm_state": has_ewm,
        "has_shift_or_difference_state": has_shift,
        "has_spacing_or_sequence_state": has_spacing_state,
        "declared_lookback_hints": lookback_hints,
        "max_declared_integer_lookback_hint": max(numeric_hints) if numeric_hints else None,
        "requires_historical_context": bool(methods or has_for or has_while),
    }


def _resolve_joined_string(
    node: ast.JoinedStr,
    constants: Mapping[str, Any],
) -> str | None:
    """Resolve source f-strings whose formatted values are frozen literals."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(str(value.value))
            continue
        if not isinstance(value, ast.FormattedValue):
            return None
        expr = value.value
        if isinstance(expr, ast.Name) and expr.id in constants:
            resolved = constants[expr.id]
        elif isinstance(expr, ast.Constant):
            resolved = expr.value
        else:
            return None
        if not isinstance(resolved, (str, int, float, bool)):
            return None
        parts.append(str(resolved))
    return "".join(parts)


def _source_strings(
    nodes: Iterable[ast.AST],
    constants: Mapping[str, Any],
) -> set[str]:
    """Collect literal and source-resolvable dynamic strings."""
    out: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                out.add(str(child.value))
            elif isinstance(child, ast.JoinedStr):
                resolved = _resolve_joined_string(child, constants)
                if resolved:
                    out.add(resolved)

    for value in constants.values():
        if isinstance(value, list):
            out.update(str(x) for x in value if isinstance(x, str))
        elif isinstance(value, str):
            out.add(value)
    return out


def _manifest_edge_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = manifest.get("selected_edges")
    if not isinstance(rows, list):
        raise PhaseDCandidateContractError("live_manifest.selected_edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseDCandidateContractError("live_manifest selected edge is not an object")
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            raise PhaseDCandidateContractError("live_manifest selected edge has blank edge_id")
        if edge_id in out:
            raise PhaseDCandidateContractError(f"duplicate live edge_id: {edge_id}")
        out[edge_id] = dict(raw)
    return out


def _c3_edge_map(c3: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = c3.get("edges")
    if not isinstance(rows, list):
        raise PhaseDCandidateContractError("C.3 edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        edge_id = str(raw.get("edge_id") or "").strip()
        if edge_id:
            out[edge_id] = dict(raw)
    return out


def _edge_root(root: Path, edge: Mapping[str, Any]) -> Path:
    text = str(edge.get("edge_root") or "").strip()
    if text:
        path = (root / text).resolve()
    else:
        runtime_module = str(edge.get("runtime_module") or "").strip()
        if not runtime_module.endswith("runtime.py"):
            raise PhaseDCandidateContractError(
                f"{edge.get('edge_id')}: manifest has no edge_root/runtime.py"
            )
        path = (root / runtime_module).resolve().parent
    _relative(root, path)
    return path


def _runtime_candidate_entries(
    runtime_tree: ast.Module,
) -> tuple[list[str], list[str], list[str]]:
    functions = _function_map(runtime_tree)
    runtime_generate = functions.get("generate_candidate_events")
    if runtime_generate is None:
        raise PhaseDCandidateContractError(
            "runtime.py does not define generate_candidate_events"
        )

    candidate_aliases = _import_aliases(runtime_tree, "candidate_events")
    primary_aliases = _import_aliases(runtime_tree, "primary_side")
    calls = _called_names(runtime_generate)

    candidate_entries = sorted(
        candidate_aliases[name]
        for name in calls
        if name in candidate_aliases
    )
    primary_entries = sorted(
        primary_aliases[name]
        for name in calls
        if name in primary_aliases
    )
    return candidate_entries, primary_entries, sorted(calls)


def _compile_edge(
    *,
    root: Path,
    edge: Mapping[str, Any],
    c3_edge: Mapping[str, Any],
) -> dict[str, Any]:
    edge_id = str(edge.get("edge_id") or "")
    asset = str(edge.get("asset") or "").upper()
    family = str(edge.get("family") or "")
    edge_root = _edge_root(root, edge)

    runtime_path = edge_root / "runtime.py"
    candidate_path = edge_root / "candidate_events.py"
    primary_path = edge_root / "primary_side.py"

    for path in (runtime_path, candidate_path):
        if not path.is_file():
            raise PhaseDCandidateContractError(
                f"{edge_id}: required source missing: {_relative(root, path)}"
            )

    runtime_source, runtime_tree = _parse(runtime_path)
    candidate_source, candidate_tree = _parse(candidate_path)

    candidate_entries, primary_entries, runtime_calls = _runtime_candidate_entries(
        runtime_tree
    )
    if not candidate_entries:
        raise PhaseDCandidateContractError(
            f"{edge_id}: runtime generate_candidate_events does not call a "
            "function imported from candidate_events"
        )

    candidate_functions = _function_map(candidate_tree)
    missing_entries = [
        name for name in candidate_entries if name not in candidate_functions
    ]
    if missing_entries:
        raise PhaseDCandidateContractError(
            f"{edge_id}: candidate entry functions missing: {missing_entries}"
        )

    reachable = _reachable_functions(candidate_functions, candidate_entries)
    candidate_nodes = [candidate_functions[name] for name in reachable]
    constants = _literal_constants(candidate_tree)
    logic_sha, semantic_constants = _logic_signature(
        source=candidate_source,
        function_map=candidate_functions,
        reachable=reachable,
        constants=constants,
    )
    history = _history_profile(
        nodes=candidate_nodes,
        semantic_constants=semantic_constants,
    )

    canonical_columns = [
        str(x) for x in c3_edge.get("canonical_projection_columns") or []
    ]
    certified_input_space = set(canonical_columns).difference(RAW_IDENTITY_COLUMNS)

    candidate_source_strings = _source_strings(candidate_nodes, constants)
    candidate_source_input_columns = sorted(
        certified_input_space.intersection(candidate_source_strings)
    )

    primary_source = ""
    primary_tree: ast.Module | None = None
    primary_functions: dict[str, ast.AST] = {}
    primary_reachable: list[str] = []
    primary_nodes: list[ast.AST] = []
    primary_constants: dict[str, Any] = {}
    primary_semantic_constants: dict[str, Any] = {}
    primary_logic_sha: str | None = None
    primary_source_input_columns: list[str] = []

    if primary_entries and primary_path.is_file():
        primary_source, primary_tree = _parse(primary_path)
        primary_functions = _function_map(primary_tree)
        missing_primary_entries = [
            name for name in primary_entries if name not in primary_functions
        ]
        if missing_primary_entries:
            raise PhaseDCandidateContractError(
                f"{edge_id}: primary-side entry functions missing: "
                f"{missing_primary_entries}"
            )
        primary_reachable = _reachable_functions(
            primary_functions,
            primary_entries,
        )
        primary_nodes = [
            primary_functions[name]
            for name in primary_reachable
            if name in primary_functions
        ]
        primary_constants = _literal_constants(primary_tree)
        primary_logic_sha, primary_semantic_constants = _logic_signature(
            source=primary_source,
            function_map=primary_functions,
            reachable=primary_reachable,
            constants=primary_constants,
        )
        primary_source_strings = _source_strings(
            primary_nodes,
            primary_constants,
        )
        primary_source_input_columns = sorted(
            certified_input_space.intersection(primary_source_strings)
        )

    candidate_input_columns = sorted(
        set(candidate_source_input_columns).union(
            primary_source_input_columns
        )
    )

    consumer_source = c3_edge.get("consumer_source") or {}
    frozen_candidate_sha = str(
        consumer_source.get("candidate_events_file_sha256") or ""
    )
    current_candidate_sha = _file_sha256(candidate_path)
    frozen_runtime_sha = str(
        consumer_source.get("runtime_file_sha256") or ""
    )
    current_runtime_sha = _file_sha256(runtime_path)

    failures: list[str] = []
    # SINGLE_OPERATOR_AUTHORITY: historical SHA equality is audit-only.
    # SINGLE_OPERATOR_AUTHORITY: historical SHA equality is audit-only.

    primary: dict[str, Any] = {
        "present": primary_path.is_file(),
        "runtime_called_functions": primary_entries,
        "path": _relative(root, primary_path) if primary_path.is_file() else None,
        "sha256": _file_sha256(primary_path) if primary_path.is_file() else None,
        "reachable_primary_functions": primary_reachable,
        "primary_logic_signature": primary_logic_sha,
        "semantic_constants": primary_semantic_constants,
        "feature_input_columns_from_certified_projection": primary_source_input_columns,
        "feature_input_column_count": len(primary_source_input_columns),
    }
    if primary_entries and not primary_path.is_file():
        failures.append("runtime calls primary_side functions but primary_side.py is missing")

    runtime_functions = _function_map(runtime_tree)
    runtime_generate = runtime_functions["generate_candidate_events"]
    runtime_generate_source = _function_source(runtime_source, runtime_generate)

    return {
        "edge_id": edge_id,
        "asset": asset,
        "family": family,
        "side_value": edge.get("side_value"),
        "edge_root": _relative(root, edge_root),
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "candidate": {
            "path": _relative(root, candidate_path),
            "file_sha256": current_candidate_sha,
            "frozen_c3_file_sha256": frozen_candidate_sha or None,
            "runtime_entry_functions": candidate_entries,
            "reachable_candidate_functions": reachable,
            "candidate_logic_signature": logic_sha,
            "semantic_constants": semantic_constants,
            "history_profile": history,
            "candidate_source_input_columns_from_certified_projection": candidate_source_input_columns,
            "primary_side_source_input_columns_from_certified_projection": primary_source_input_columns,
            "candidate_input_columns_from_certified_projection": candidate_input_columns,
            "certified_projection_column_count": len(canonical_columns),
            "candidate_input_column_count": len(candidate_input_columns),
        },
        "primary_side": primary,
        "runtime_candidate_wrapper": {
            "path": _relative(root, runtime_path),
            "file_sha256": current_runtime_sha,
            "frozen_c3_file_sha256": frozen_runtime_sha or None,
            "generate_candidate_events_source_sha256": hashlib.sha256(
                runtime_generate_source.encode("utf-8")
            ).hexdigest(),
            "called_names": runtime_calls,
        },
        "phase_d_routing": {
            "incremental_candidate_routing_enabled": False,
            "resident_scorer_routing_enabled": False,
            "normal_full_candidate_fallback_allowed_after_d2_activation": False,
            "current_role": "inventory_and_certification_planning_only",
        },
    }


def compile_candidate_contract(
    *,
    v2_root: Path,
    live_manifest_path: Path | None = None,
    c3_contract_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(v2_root).resolve()
    manifest_path = (
        Path(live_manifest_path).resolve()
        if live_manifest_path is not None
        else (root / "portfolio_live" / "live_manifest.json").resolve()
    )
    c3_path = (
        Path(c3_contract_path).resolve()
        if c3_contract_path is not None
        else (
            root
            / "portfolio_live"
            / "config"
            / "phase_c"
            / "consumer_contract.json"
        ).resolve()
    )

    for path in (manifest_path, c3_path):
        _relative(root, path)
        if not path.is_file():
            raise PhaseDCandidateContractError(
                f"Required Phase D.1 input missing: {path}"
            )

    manifest = _read_json(manifest_path)
    c3 = _read_json(c3_path)
    if c3.get("status") != "PASS":
        raise PhaseDCandidateContractError("C.3 contract must be PASS")
    if c3.get("runtime_routing_enabled") is not False:
        raise PhaseDCandidateContractError(
            "Frozen C.3 contract must retain runtime_routing_enabled=false"
        )

    manifest_edges = _manifest_edge_map(manifest)
    c3_edges = _c3_edge_map(c3)
    if set(manifest_edges) != set(c3_edges):
        raise PhaseDCandidateContractError(
            "live manifest and frozen C.3 selected-edge universe differ"
        )

    compiled: list[dict[str, Any]] = []
    fatal_errors: list[str] = []
    for edge_id in sorted(manifest_edges):
        try:
            row = _compile_edge(
                root=root,
                edge=manifest_edges[edge_id],
                c3_edge=c3_edges[edge_id],
            )
            compiled.append(row)
            if row["status"] != "PASS":
                fatal_errors.extend(
                    f"{edge_id}: {message}"
                    for message in row.get("failures") or []
                )
        except Exception as exc:
            fatal_errors.append(
                f"{edge_id}: {type(exc).__name__}: {exc}"
            )

    selected_assets = sorted(
        {
            str(row.get("asset") or "").upper()
            for row in manifest_edges.values()
            if str(row.get("asset") or "")
        }
    )

    groups: dict[str, list[str]] = defaultdict(list)
    history_shapes: Counter[str] = Counter()
    entry_function_counts: Counter[str] = Counter()
    for row in compiled:
        signature = str(
            (row.get("candidate") or {}).get("candidate_logic_signature") or ""
        )
        if signature:
            groups[signature].append(str(row["edge_id"]))
        profile = (row.get("candidate") or {}).get("history_profile") or {}
        shape = "|".join(
            [
                f"rolling={bool(profile.get('has_rolling_state'))}",
                f"ewm={bool(profile.get('has_ewm_state'))}",
                f"shift={bool(profile.get('has_shift_or_difference_state'))}",
                f"spacing={bool(profile.get('has_spacing_or_sequence_state'))}",
                f"for={bool(profile.get('has_python_for_loop'))}",
            ]
        )
        history_shapes[shape] += 1
        for name in (row.get("candidate") or {}).get("runtime_entry_functions") or []:
            entry_function_counts[str(name)] += 1

    group_rows = [
        {
            "candidate_logic_signature": signature,
            "edge_count": len(edge_ids),
            "edge_ids": sorted(edge_ids),
        }
        for signature, edge_ids in groups.items()
    ]
    group_rows.sort(
        key=lambda row: (-int(row["edge_count"]), str(row["candidate_logic_signature"]))
    )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "created_at": _now_utc(),
        "status": "PASS" if not fatal_errors else "FAIL",
        "runtime_routing_enabled": False,
        "phase_d_stage": "D1_SOURCE_INVENTORY_ONLY",
        "v2_root": str(root),
        "python_version": sys.version.split()[0],
        "source_live_manifest_sha256": _file_sha256(manifest_path),
        "source_c3_contract_sha256": str(c3.get("contract_sha256") or ""),
        "source_c3_contract_file_sha256": _file_sha256(c3_path),
        "selected_asset_count": len(selected_assets),
        "selected_assets": selected_assets,
        "selected_edge_count": len(manifest_edges),
        "compiled_edge_count": len(compiled),
        "failed_edge_count": len(
            [row for row in compiled if row.get("status") != "PASS"]
        ),
        "candidate_logic_group_count": len(group_rows),
        "multi_edge_logic_group_count": len(
            [row for row in group_rows if int(row["edge_count"]) > 1]
        ),
        "candidate_logic_groups": group_rows,
        "history_shape_counts": dict(sorted(history_shapes.items())),
        "candidate_entry_function_counts": dict(
            sorted(entry_function_counts.items())
        ),
        "edges": compiled,
        "fatal_errors": fatal_errors,
        "phase_d_invariants": [
            "Phase A/B remain sole dependency-generation owners",
            "Phase C.4 canonical feature math and generation identity remain unchanged",
            "D.1 performs no production routing",
            "candidate + primary-side consumer authority is source-derived from current C.3-selected edge packages",
            "no fixed asset list or selected-edge count is encoded",
            "candidate logic groups are source-derived and advisory until D.2 numeric certification",
            "a non-firing edge must eventually avoid model-row/model inference work",
            "D.2 normal path will not hide a full historical candidate fallback",
        ],
        "next_stage": {
            "name": "D2_CANDIDATE_FIRST_NARROW_CERTIFICATION",
            "routing_enabled": False,
            "requires_numeric_authority": True,
            "requires_restart_parity": True,
            "requires_full_candidate_timestamp_parity": True,
        },
    }
    unsigned = dict(report)
    report["contract_sha256"] = _json_hash(unsigned)
    return report


def contract_without_nondeterministic_fields(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    out = json.loads(json.dumps(dict(payload), default=str))
    out.pop("created_at", None)
    out.pop("contract_sha256", None)
    out.pop("v2_root", None)
    return out


__all__ = [
    "SCHEMA_VERSION",
    "COMPILER_VERSION",
    "PhaseDCandidateContractError",
    "compile_candidate_contract",
    "contract_without_nondeterministic_fields",
]
