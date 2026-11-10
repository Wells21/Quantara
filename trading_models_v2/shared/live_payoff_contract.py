# QUANTARA_V3_PHASE3B_ALLOCATOR_FROZEN_LIVE_PAYOFF_V1
"""Shared fail-closed live payoff/AFML ownership contract for Quantara V3."""
from __future__ import annotations

import math
from typing import Any, Mapping

from shared.calibration.sigmoid_afml import (
    AFML_BET_SIZE_COL,
    AFML_ECONOMIC_STRENGTH_COL,
    AFML_EFFECTIVE_PROBABILITY_COL,
    AFML_SIZING_METHOD,
    BREAK_EVEN_PROBABILITY_COL,
    payoff_profile_afml_components,
)
from shared.validation.v3_semantic_contract import (
    AFML_SIZING_METHOD as SEMANTIC_AFML_SIZING_METHOD,
    PAYOFF_CONTEXT_VERSION,
    build_payoff_context,
    profile_from_mapping,
    validate_payoff_context_mapping,
)

LIVE_PAYOFF_CONTRACT_VERSION = "quantara_v3_allocator_frozen_live_payoff_v1"


class LivePayoffContractError(RuntimeError):
    """Raised when live threshold/payoff/AFML ownership is stale or inconsistent."""


def _finite_float(value: Any, name: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise LivePayoffContractError(f"{name} must be numeric, got {value!r}") from exc
    if not math.isfinite(out):
        raise LivePayoffContractError(f"{name} must be finite, got {value!r}")
    return out


def _same_float(left: Any, right: Any, *, atol: float = 1e-12) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=atol)
    except Exception:
        return False


def freeze_live_payoff_from_representative(
    representative: Mapping[str, Any],
    *,
    edge_id: str,
    threshold: float,
    geometry_hash: str,
) -> dict[str, Any]:
    if not isinstance(representative, Mapping):
        raise LivePayoffContractError("04C representative must be a mapping.")
    rep_edge = str(representative.get("edge_id", "")).strip()
    if rep_edge != str(edge_id):
        raise LivePayoffContractError(
            f"04C representative edge mismatch: expected {edge_id!r}, got {rep_edge!r}"
        )
    rep_threshold = _finite_float(representative.get("threshold"), "04C threshold")
    if not _same_float(rep_threshold, threshold):
        raise LivePayoffContractError(
            f"04C threshold mismatch: stress/live={threshold!r}, representative={rep_threshold!r}"
        )

    profile = profile_from_mapping(representative)
    context = validate_payoff_context_mapping(
        representative,
        expected_edge_id=str(edge_id),
        expected_threshold=float(threshold),
        expected_geometry_hash=str(geometry_hash),
    )
    rebuilt = build_payoff_context(
        edge_id=str(edge_id),
        threshold=float(threshold),
        geometry_hash=str(geometry_hash),
        payoff_profile=profile,
    )
    if rebuilt["payoff_context_id"] != context["payoff_context_id"]:
        raise LivePayoffContractError("04C payoff profile/context identity mismatch.")

    return {
        "live_payoff_contract_version": LIVE_PAYOFF_CONTRACT_VERSION,
        "payoff_context_version": PAYOFF_CONTEXT_VERSION,
        "payoff_context_id": context["payoff_context_id"],
        "afml_sizing_method": AFML_SIZING_METHOD,
        "payoff_profile": profile,
        "payoff_context": context,
    }


def validate_live_payoff_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(edge, Mapping):
        raise LivePayoffContractError("Live edge must be a mapping.")
    edge_id = str(edge.get("edge_id", "")).strip()
    if not edge_id:
        raise LivePayoffContractError("Live edge missing edge_id.")
    if str(edge.get("live_payoff_contract_version", "")) != LIVE_PAYOFF_CONTRACT_VERSION:
        raise LivePayoffContractError(f"{edge_id}: live payoff contract is missing/stale.")
    threshold = _finite_float(edge.get("final_live_threshold"), "final_live_threshold")
    if not 0.0 < threshold < 1.0:
        raise LivePayoffContractError(
            f"{edge_id}: final_live_threshold must be in (0,1), got {threshold}"
        )
    geometry_hash = str(edge.get("geometry_hash", "")).strip()
    if not geometry_hash:
        raise LivePayoffContractError(f"{edge_id}: missing geometry_hash.")

    profile_raw = edge.get("live_payoff_profile")
    context_raw = edge.get("live_payoff_context")
    if not isinstance(profile_raw, Mapping):
        raise LivePayoffContractError(f"{edge_id}: missing live_payoff_profile.")
    if not isinstance(context_raw, Mapping):
        raise LivePayoffContractError(f"{edge_id}: missing live_payoff_context.")

    profile = profile_from_mapping(profile_raw)
    context = validate_payoff_context_mapping(
        context_raw,
        expected_edge_id=edge_id,
        expected_threshold=threshold,
        expected_geometry_hash=geometry_hash,
    )
    rebuilt = build_payoff_context(
        edge_id=edge_id,
        threshold=threshold,
        geometry_hash=geometry_hash,
        payoff_profile=profile,
    )
    supplied_id = str(edge.get("payoff_context_id", "")).strip()
    if not supplied_id:
        raise LivePayoffContractError(f"{edge_id}: missing payoff_context_id.")
    if supplied_id != context["payoff_context_id"] or supplied_id != rebuilt["payoff_context_id"]:
        raise LivePayoffContractError(f"{edge_id}: live threshold/payoff context identity mismatch.")
    if str(edge.get("payoff_context_version", "")) != PAYOFF_CONTEXT_VERSION:
        raise LivePayoffContractError(f"{edge_id}: payoff_context_version mismatch.")
    live_method = str(edge.get("live_afml_sizing_method", "")).strip()
    if live_method != AFML_SIZING_METHOD or live_method != SEMANTIC_AFML_SIZING_METHOD:
        raise LivePayoffContractError(f"{edge_id}: live AFML sizing method mismatch.")

    return {
        "edge_id": edge_id,
        "threshold": threshold,
        "geometry_hash": geometry_hash,
        "payoff_context_id": supplied_id,
        "payoff_context": context,
        "payoff_profile": profile,
        "afml_sizing_method": live_method,
    }


def compute_frozen_live_afml(
    calibrated_probability: float,
    edge: Mapping[str, Any],
) -> dict[str, float | str]:
    validated = validate_live_payoff_edge(edge)
    p = _finite_float(calibrated_probability, "calibrated_probability")
    if not 0.0 <= p <= 1.0:
        raise LivePayoffContractError(f"calibrated_probability outside [0,1]: {p}")
    components = payoff_profile_afml_components([p], validated["payoff_profile"])
    return {
        "payoff_context_id": str(validated["payoff_context_id"]),
        "afml_bet_size": float(components[AFML_BET_SIZE_COL][0]),
        "break_even_probability": float(components[BREAK_EVEN_PROBABILITY_COL][0]),
        "afml_economic_strength": float(components[AFML_ECONOMIC_STRENGTH_COL][0]),
        "afml_effective_probability": float(components[AFML_EFFECTIVE_PROBABILITY_COL][0]),
    }


def verify_frozen_live_afml(
    calibrated_probability: float,
    claimed_afml_bet_size: float,
    edge: Mapping[str, Any],
    *,
    claimed_payoff_context_id: str,
    atol: float = 1e-12,
) -> dict[str, float | str]:
    result = compute_frozen_live_afml(calibrated_probability, edge)
    if str(claimed_payoff_context_id).strip() != str(result["payoff_context_id"]):
        raise LivePayoffContractError("Signal payoff_context_id does not match live manifest.")
    claimed = _finite_float(claimed_afml_bet_size, "claimed_afml_bet_size")
    expected = float(result["afml_bet_size"])
    if not math.isclose(claimed, expected, rel_tol=0.0, abs_tol=float(atol)):
        raise LivePayoffContractError(
            f"Signal AFML mismatch: claimed={claimed:.17g} expected={expected:.17g}"
        )
    return result
