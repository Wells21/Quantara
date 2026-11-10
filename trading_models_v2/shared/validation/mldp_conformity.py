"""MLDP methodological-conformity primitives for Quantara."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping
from shared.labels.outcome_ontology import ONTOLOGY_VERSION, get_layer

class MLDPConformityError(RuntimeError):
    pass

@dataclass(frozen=True)
class ProbabilityTargetContract:
    outcome_layer: str
    target_contract_version: str
    target_column: str
    return_column: str
    label_end_time_column: str
    probability_column: str
    probability_semantics: str
    ontology_version: str = ONTOLOGY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def probability_target_contract(
    layer_name: str, *, probability_column: str
) -> ProbabilityTargetContract:
    layer = get_layer(layer_name)
    semantics = (
        f"{probability_column} estimates P({layer.label_column}=1 | information "
        f"available at event t0), where {layer.label_column}=1 iff "
        f"{layer.return_column}>0 under {layer.contract_version}."
    )
    return ProbabilityTargetContract(
        outcome_layer=layer.name,
        target_contract_version=layer.contract_version,
        target_column=layer.label_column,
        return_column=layer.return_column,
        label_end_time_column=layer.end_time_column,
        probability_column=str(probability_column),
        probability_semantics=semantics,
    )

def _coerce_contract(value: ProbabilityTargetContract | Mapping[str, Any]) -> dict[str, Any]:
    return value.to_dict() if isinstance(value, ProbabilityTargetContract) else dict(value)

def assert_same_target(
    model_probability: ProbabilityTargetContract | Mapping[str, Any],
    calibration_target: ProbabilityTargetContract | Mapping[str, Any],
) -> dict[str, Any]:
    left = _coerce_contract(model_probability)
    right = _coerce_contract(calibration_target)
    keys = (
        "ontology_version", "outcome_layer", "target_contract_version",
        "target_column", "return_column", "label_end_time_column",
    )
    mismatches = {
        key: {"model": left.get(key), "calibration": right.get(key)}
        for key in keys if left.get(key) != right.get(key)
    }
    if mismatches:
        raise MLDPConformityError(
            "Probability/calibration target mismatch. Calibration cannot hide "
            "a change of estimand. " + json.dumps(mismatches, sort_keys=True)
        )
    return {
        "status": "PASS",
        "same_target": True,
        "outcome_layer": left["outcome_layer"],
    }

def validate_method_registry(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))
    allowed = {
        "MLDP_CANONICAL", "MLDP_LATER_REFINEMENT",
        "QUANTARA_EXTENSION", "DATA_CONSTRAINED_ADAPTATION",
    }
    methods = payload.get("methods")
    if not isinstance(methods, list) or not methods:
        raise MLDPConformityError("Method registry must contain a non-empty methods list.")
    ids: set[str] = set()
    for row in methods:
        method_id = str(row.get("method_id", "")).strip()
        if not method_id or method_id in ids:
            raise MLDPConformityError(f"Invalid/duplicate method_id: {method_id!r}")
        ids.add(method_id)
        if row.get("classification") not in allowed:
            raise MLDPConformityError(
                f"{method_id}: invalid classification {row.get('classification')!r}"
            )
        if not str(row.get("source_reference", "")).strip():
            raise MLDPConformityError(f"{method_id}: source_reference is required.")
    return {
        "status": "PASS",
        "method_count": len(methods),
        "registry_version": payload.get("registry_version"),
    }
