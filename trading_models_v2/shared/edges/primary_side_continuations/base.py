from __future__ import annotations

"""Common contracts for Phase D.4 primary-side continuation providers."""

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Union


class PrimarySideContinuationError(RuntimeError):
    """Raised when an incremental primary-side continuation cannot be trusted."""


@dataclass(frozen=True)
class PrimarySourceIdentity:
    edge_id: str
    side_value: int
    primary_source_path: str


@dataclass(frozen=True)
class PrimaryProviderDescriptor:
    provider_id: str
    provider_version: str
    module_name: str
    supported_signatures: tuple[str, ...]


def _literal_module_constants(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
    except Exception as exc:
        raise PrimarySideContinuationError(
            f"Could not parse primary-side source {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            targets = node.targets
            value_node = node.value
        else:
            targets = [node.target]
            value_node = node.value
        if value_node is None:
            continue
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name)
        ]
        if len(names) != 1:
            continue
        try:
            out[str(names[0])] = ast.literal_eval(value_node)
        except Exception:
            continue
    return out


def extract_primary_source_identity(
    *,
    v2_root: Union[str, Path],
    d1_edge: Mapping[str, Any],
) -> PrimarySourceIdentity:
    """Parse identity literals from the unchanged primary-side research source."""

    root = Path(v2_root).resolve()
    edge_id = str(d1_edge.get("edge_id") or "").strip()
    primary = d1_edge.get("primary_side") or {}
    if not isinstance(primary, Mapping) or primary.get("present") is not True:
        raise PrimarySideContinuationError(
            f"{edge_id}: D.1 primary-side record missing"
        )

    relative = str(primary.get("path") or "").strip()
    if not relative:
        raise PrimarySideContinuationError(
            f"{edge_id}: D.1 primary-side path missing"
        )

    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PrimarySideContinuationError(
            f"{edge_id}: primary-side source escapes V2 root"
        ) from exc

    if not path.is_file():
        raise PrimarySideContinuationError(
            f"{edge_id}: primary-side source missing: {path}"
        )

    constants = _literal_module_constants(path)
    source_edge_id = str(constants.get("EDGE_ID") or "").strip()
    try:
        side_value = int(constants.get("SIDE_VALUE"))
    except Exception as exc:
        raise PrimarySideContinuationError(
            f"{edge_id}: primary-side SIDE_VALUE literal missing/invalid"
        ) from exc

    if source_edge_id != edge_id:
        raise PrimarySideContinuationError(
            f"{edge_id}: primary-side EDGE_ID mismatch: {source_edge_id!r}"
        )
    if d1_edge.get("side_value") is not None:
        if int(d1_edge["side_value"]) != side_value:
            raise PrimarySideContinuationError(
                f"{edge_id}: D.1/primary-side source side mismatch"
            )

    return PrimarySourceIdentity(
        edge_id=edge_id,
        side_value=side_value,
        primary_source_path=relative,
    )
