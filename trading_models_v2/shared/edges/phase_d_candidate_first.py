"""Phase D.2 candidate-first narrow projection primitives.

This module does NOT install production routing.

Purpose
-------
Phase D.1 proved that selected candidate detectors consume a small subset of the
C.3 certified edge projection. D.2 uses that frozen source-derived subset to
run the *unchanged* runtime candidate generator before allocating/copying the
full model projection.

The full C.3 projection remains authoritative whenever an edge actually fires.
No candidate math, primary-side logic, model input, model, calibration, AFML,
threshold, geometry, allocator, or executor rule is changed here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

D2_SCHEMA_VERSION = "phase_d2_candidate_first_narrow_projection_v1"
D2_ENGINE_VERSION = "phase_d2_candidate_first_narrow_projection_v1"
RUNTIME_ROUTING_ENABLED = False

IDENTITY_COLUMNS = ("edge_id", "side")


class PhaseD2ContractError(RuntimeError):
    """Fail-closed Phase D.2 contract error."""


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PhaseD2ContractError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _relative(root: Path, path: Path) -> str:
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PhaseD2ContractError(f"Path escapes V2 root: {path}") from exc


def load_phase_d1_contract(
    v2_root: Path,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    root = Path(v2_root).resolve()
    contract_path = (
        Path(path).resolve()
        if path is not None
        else (
            root
            / "portfolio_live"
            / "config"
            / "phase_d"
            / "candidate_contract.json"
        ).resolve()
    )
    _relative(root, contract_path)
    if not contract_path.is_file():
        raise PhaseD2ContractError(f"Phase D.1 contract missing: {contract_path}")
    payload = _read_json(contract_path)
    if payload.get("status") != "PASS":
        raise PhaseD2ContractError("Phase D.1 contract is not PASS")
    if payload.get("runtime_routing_enabled") is not False:
        raise PhaseD2ContractError("Phase D.1 unexpectedly enables runtime routing")
    if payload.get("phase_d_stage") != "D1_SOURCE_INVENTORY_ONLY":
        raise PhaseD2ContractError(
            f"Unexpected Phase D.1 stage: {payload.get('phase_d_stage')!r}"
        )
    return payload


def edge_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("edges")
    if not isinstance(rows, list):
        raise PhaseD2ContractError("Contract edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            continue
        if edge_id in out:
            raise PhaseD2ContractError(f"Duplicate edge_id: {edge_id}")
        out[edge_id] = dict(raw)
    return out


def manifest_edge_map(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("selected_edges")
    if not isinstance(rows, list):
        raise PhaseD2ContractError("live_manifest.selected_edges must be a list")
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        edge_id = str(raw.get("edge_id") or "").strip()
        if not edge_id:
            raise PhaseD2ContractError("live manifest contains blank edge_id")
        if edge_id in out:
            raise PhaseD2ContractError(f"Duplicate manifest edge_id: {edge_id}")
        out[edge_id] = dict(raw)
    return out


def source_guard(
    *,
    v2_root: Path,
    d1_contract: Mapping[str, Any],
    c3_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Current-source structural guard; historical SHA equality is not authority."""
    root = Path(v2_root).resolve()
    failures: list[str] = []
    edges = edge_map(d1_contract)
    c3_edges = edge_map(c3_contract) if c3_contract is not None else {}

    if c3_contract is not None and set(edges) != set(c3_edges):
        failures.append("D.1/C.3 selected-edge universe differs")

    checked = 0
    for edge_id, row in edges.items():
        candidate = row.get("candidate") or {}
        primary = row.get("primary_side") or {}
        runtime = row.get("runtime_candidate_wrapper") or {}
        candidate_path = (root / str(candidate.get("path") or "")).resolve()
        runtime_path = (root / str(runtime.get("path") or "")).resolve()
        primary_path = (
            (root / str(primary.get("path") or "")).resolve()
            if primary.get("present") and primary.get("path")
            else None
        )
        try:
            _relative(root, candidate_path)
            _relative(root, runtime_path)
            if primary_path is not None:
                _relative(root, primary_path)
        except Exception as exc:
            failures.append(f"{edge_id}: {exc}")
            continue

        if not candidate_path.is_file():
            failures.append(f"{edge_id}: candidate source missing")
            continue
        if not runtime_path.is_file():
            failures.append(f"{edge_id}: runtime source missing")
            continue
        if primary_path is not None and not primary_path.is_file():
            failures.append(f"{edge_id}: primary-side source missing")
            continue

        if c3_contract is not None:
            c3_edge = c3_edges.get(edge_id) or {}
            projection = set(
                str(x)
                for x in c3_edge.get("canonical_projection_columns") or []
            )
            required = set(
                str(x)
                for x in candidate.get(
                    "candidate_input_columns_from_certified_projection"
                )
                or []
            )
            if not required:
                failures.append(f"{edge_id}: D.1 candidate input projection is empty")
            elif not required.issubset(projection):
                failures.append(
                    f"{edge_id}: D.1 candidate inputs are outside current C.3 projection"
                )
        checked += 1

    return {
        "status": "PASS" if not failures else "FAIL",
        "checked_edges": checked,
        "failed_count": len(failures),
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
    }



def _side_value(
    edge: Mapping[str, Any],
    d1_edge: Mapping[str, Any],
) -> int:
    value = edge.get("side_value")
    if value is None:
        value = d1_edge.get("side_value")
    if value is None:
        text = str(edge.get("side") or edge.get("side_name") or "").lower()
        if text == "long":
            value = 1
        elif text == "short":
            value = -1
    try:
        number = int(value)
    except Exception as exc:
        raise PhaseD2ContractError(
            f"{edge.get('edge_id')}: cannot resolve side_value"
        ) from exc
    if number not in {-1, 1}:
        raise PhaseD2ContractError(
            f"{edge.get('edge_id')}: side_value must be +/-1, got {number}"
        )
    return number


def candidate_projection_columns(
    d1_edge: Mapping[str, Any],
) -> list[str]:
    candidate = d1_edge.get("candidate") or {}
    columns = [
        str(x)
        for x in candidate.get(
            "candidate_input_columns_from_certified_projection"
        )
        or []
        if str(x)
    ]
    if not columns:
        raise PhaseD2ContractError(
            f"{d1_edge.get('edge_id')}: D.1 candidate projection is empty"
        )
    return list(dict.fromkeys(columns))


def full_projection_columns(
    c3_edge: Mapping[str, Any],
) -> list[str]:
    columns = [
        str(x)
        for x in c3_edge.get("canonical_projection_columns") or []
        if str(x)
    ]
    if not columns:
        raise PhaseD2ContractError(
            f"{c3_edge.get('edge_id')}: C.3 projection is empty"
        )
    return list(dict.fromkeys(columns))


def project_candidate_features(
    *,
    ring: pd.DataFrame,
    edge: Mapping[str, Any],
    d1_edge: Mapping[str, Any],
) -> pd.DataFrame:
    """Copy only candidate-required columns from the canonical ring.

    This deliberately does not perform ``ring.copy(deep=True)`` before column
    selection. The wide C.3 model projection is allocated only after a current
    candidate has been found by the caller.
    """
    if not isinstance(ring, pd.DataFrame) or ring.empty:
        raise PhaseD2ContractError("Canonical feature ring is empty")
    edge_id = str(edge.get("edge_id") or d1_edge.get("edge_id") or "")
    required = candidate_projection_columns(d1_edge)
    missing = [name for name in required if name not in ring.columns]
    if missing:
        raise PhaseD2ContractError(
            f"{edge_id}: canonical ring missing candidate columns: {missing[:30]}"
        )

    # Narrow first, then copy. This is the core D.2 optimization.
    projected = ring.loc[:, required].copy()
    projected["edge_id"] = edge_id
    projected["side"] = _side_value(edge, d1_edge)
    projected.attrs.update(ring.attrs)
    projected.attrs["phase_d2_role"] = "candidate_first_narrow_projection"
    projected.attrs["phase_d2_edge_id"] = edge_id
    projected.attrs["phase_d2_candidate_input_columns"] = tuple(required)
    projected.attrs["phase_d2_full_projection_deferred"] = True
    return projected


def project_full_c3_features(
    *,
    ring: pd.DataFrame,
    edge: Mapping[str, Any],
    d1_edge: Mapping[str, Any],
    c3_edge: Mapping[str, Any],
) -> pd.DataFrame:
    """Build the current C.3 full projection for certification/fired edges."""
    if not isinstance(ring, pd.DataFrame) or ring.empty:
        raise PhaseD2ContractError("Canonical feature ring is empty")
    edge_id = str(edge.get("edge_id") or d1_edge.get("edge_id") or "")
    required = full_projection_columns(c3_edge)
    base_required = [
        name for name in required if name not in IDENTITY_COLUMNS
    ]
    missing = [name for name in base_required if name not in ring.columns]
    if missing:
        raise PhaseD2ContractError(
            f"{edge_id}: canonical ring missing C.3 columns: {missing[:30]}"
        )

    projected = ring.loc[:, base_required].copy()
    projected["edge_id"] = edge_id
    projected["side"] = _side_value(edge, d1_edge)
    # Restore exact C.3 column order.
    projected = projected.loc[:, required].copy()
    projected.attrs.update(ring.attrs)
    projected.attrs["phase_d2_role"] = "full_c3_projection_after_candidate"
    projected.attrs["phase_d2_edge_id"] = edge_id
    return projected


def projection_reduction_record(
    *,
    d1_edge: Mapping[str, Any],
    c3_edge: Mapping[str, Any],
) -> dict[str, Any]:
    narrow = len(candidate_projection_columns(d1_edge)) + len(IDENTITY_COLUMNS)
    full = len(full_projection_columns(c3_edge))
    if full <= 0:
        raise PhaseD2ContractError("Full projection column count is zero")
    return {
        "edge_id": str(d1_edge.get("edge_id") or ""),
        "candidate_projection_columns_including_identity": narrow,
        "full_c3_projection_columns": full,
        "column_reduction_fraction": 1.0 - (float(narrow) / float(full)),
        "full_to_candidate_column_ratio": float(full) / float(narrow),
    }


__all__ = [
    "D2_SCHEMA_VERSION",
    "D2_ENGINE_VERSION",
    "RUNTIME_ROUTING_ENABLED",
    "PhaseD2ContractError",
    "load_phase_d1_contract",
    "edge_map",
    "manifest_edge_map",
    "source_guard",
    "candidate_projection_columns",
    "full_projection_columns",
    "project_candidate_features",
    "project_full_c3_features",
    "projection_reduction_record",
]
