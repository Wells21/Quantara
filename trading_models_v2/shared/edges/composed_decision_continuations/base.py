from __future__ import annotations

"""Identity guards for Phase D.4 composed candidate/model/scoring continuation."""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Union


class ComposedDecisionContinuationError(RuntimeError):
    """Raised when a composed decision continuation cannot be trusted."""


@dataclass(frozen=True)
class RuntimeCompositionIdentity:
    edge_id: str
    side_value: int
    runtime_source_path: str
    composed_candidate_event_generator: str


def _subscript_key(target: ast.Subscript) -> Any:
    key = target.slice
    return key.value if isinstance(key, ast.Constant) else None


def _runtime_generator_labels(path: Path) -> list[str]:
    """Find explicit runtime relabels without assuming an alpha or variable name."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ComposedDecisionContinuationError(
            f"Could not parse runtime source {path}: {type(exc).__name__}: {exc}"
        ) from exc

    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "generate_candidate_events"
        ),
        None,
    )
    if function is None:
        raise ComposedDecisionContinuationError(
            f"generate_candidate_events missing from runtime source: {path}"
        )

    labels: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and _subscript_key(target) == "candidate_event_generator"
            ):
                labels.append(str(value.value))
    return sorted(set(labels))


def _module_string_constant(path: Path, name: str) -> str | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ComposedDecisionContinuationError(
            f"Could not parse candidate source {path}: {type(exc).__name__}: {exc}"
        ) from exc

    values: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            values.append(str(value.value))

    unique = sorted(set(values))
    if len(unique) > 1:
        raise ComposedDecisionContinuationError(
            f"Multiple conflicting {name} values in {path}: {unique!r}"
        )
    return unique[0] if unique else None


def extract_runtime_composition_identity(
    *,
    v2_root: Union[str, Path],
    d1_edge: Mapping[str, Any],
) -> RuntimeCompositionIdentity:
    """Derive the composed-event identity from the unchanged runtime source."""

    root = Path(v2_root).resolve()
    edge_id = str(d1_edge.get("edge_id") or "").strip()
    if not edge_id:
        raise ComposedDecisionContinuationError("D.1 edge_id missing")

    runtime = d1_edge.get("runtime_candidate_wrapper") or {}
    if not isinstance(runtime, Mapping):
        raise ComposedDecisionContinuationError(
            f"{edge_id}: D.1 runtime_candidate_wrapper missing"
        )
    relative = str(runtime.get("path") or "").strip()
    if not relative:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: D.1 runtime wrapper path missing"
        )

    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: runtime source escapes V2 root"
        ) from exc
    if not path.is_file():
        raise ComposedDecisionContinuationError(
            f"{edge_id}: runtime source missing: {path}"
        )
    if path.name != "runtime.py":
        raise ComposedDecisionContinuationError(
            f"{edge_id}: runtime candidate wrapper is not runtime.py: {relative}"
        )

    try:
        side_value = int(d1_edge.get("side_value"))
    except Exception as exc:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: D.1 side_value missing/invalid"
        ) from exc
    if side_value not in {-1, 1}:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: D.1 side_value must be +/-1"
        )

    labels = _runtime_generator_labels(path)
    if len(labels) > 1:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: multiple runtime candidate generator relabels: {labels!r}"
        )

    if labels:
        composed_label = labels[0]
    else:
        candidate = d1_edge.get("candidate") or {}
        candidate_relative = str(candidate.get("path") or "").strip()
        if not candidate_relative:
            raise ComposedDecisionContinuationError(
                f"{edge_id}: candidate source path missing"
            )
        candidate_path = (root / candidate_relative).resolve()
        try:
            candidate_path.relative_to(root)
        except ValueError as exc:
            raise ComposedDecisionContinuationError(
                f"{edge_id}: candidate source escapes V2 root"
            ) from exc
        if not candidate_path.is_file():
            raise ComposedDecisionContinuationError(
                f"{edge_id}: candidate source missing: {candidate_path}"
            )
        composed_label = _module_string_constant(
            candidate_path, "CANDIDATE_EVENT_GENERATOR"
        )
        if not composed_label:
            raise ComposedDecisionContinuationError(
                f"{edge_id}: no runtime relabel and no "
                "CANDIDATE_EVENT_GENERATOR constant"
            )

    return RuntimeCompositionIdentity(
        edge_id=edge_id,
        side_value=side_value,
        runtime_source_path=relative,
        composed_candidate_event_generator=composed_label,
    )
