from __future__ import annotations

"""Common contracts for Phase D.4 candidate continuation providers."""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Union


class CandidateContinuationError(RuntimeError):
    """Raised when an incremental candidate continuation cannot be trusted."""


@dataclass(frozen=True)
class CandidateSourceIdentity:
    edge_id: str
    side_value: int
    candidate_event_generator: str
    candidate_source_path: str


@dataclass(frozen=True)
class CandidateProviderDescriptor:
    provider_id: str
    provider_version: str
    module_name: str
    supported_signatures: tuple[str, ...]


def _literal_module_constants(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
    except Exception as exc:
        raise CandidateContinuationError(
            f"Could not parse candidate source {path}: {type(exc).__name__}: {exc}"
        ) from exc

    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            targets, value_node = node.targets, node.value
        else:
            targets, value_node = [node.target], node.value
        if value_node is None:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if len(names) != 1:
            continue
        try:
            out[str(names[0])] = ast.literal_eval(value_node)
        except Exception:
            continue
    return out


def extract_candidate_source_identity(
    *,
    v2_root: Union[str, Path],
    d1_edge: Mapping[str, Any],
) -> CandidateSourceIdentity:
    """Parse identity literals from the unchanged candidate research source."""

    root = Path(v2_root).resolve()
    edge_id = str(d1_edge.get("edge_id") or "").strip()
    candidate = d1_edge.get("candidate") or {}
    if not isinstance(candidate, Mapping):
        raise CandidateContinuationError(f"{edge_id}: D.1 candidate record missing")

    relative = str(candidate.get("path") or "").strip()
    if not relative:
        raise CandidateContinuationError(f"{edge_id}: D.1 candidate path missing")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CandidateContinuationError(
            f"{edge_id}: candidate source escapes V2 root"
        ) from exc
    if not path.is_file():
        raise CandidateContinuationError(
            f"{edge_id}: candidate source missing: {path}"
        )

    constants = _literal_module_constants(path)
    source_edge_id = str(constants.get("EDGE_ID") or "").strip()
    generator = str(constants.get("CANDIDATE_EVENT_GENERATOR") or "").strip()
    try:
        side_value = int(constants.get("SIDE_VALUE"))
    except Exception as exc:
        raise CandidateContinuationError(
            f"{edge_id}: candidate source SIDE_VALUE literal missing/invalid"
        ) from exc

    if source_edge_id != edge_id:
        raise CandidateContinuationError(
            f"{edge_id}: source EDGE_ID mismatch: {source_edge_id!r}"
        )
    if not generator:
        raise CandidateContinuationError(
            f"{edge_id}: CANDIDATE_EVENT_GENERATOR literal missing"
        )
    if d1_edge.get("side_value") is not None:
        if int(d1_edge["side_value"]) != side_value:
            raise CandidateContinuationError(
                f"{edge_id}: D.1/source side mismatch"
            )

    return CandidateSourceIdentity(
        edge_id=edge_id,
        side_value=side_value,
        candidate_event_generator=generator,
        candidate_source_path=relative,
    )
