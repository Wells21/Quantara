"""Finalized per-edge trade geometry contract utilities.

This module does not change edge research, threshold selection, portfolio weighting,
or execution logic. It normalizes the selected geometry in each edge.yaml so
allocator and live artifacts can carry and verify one immutable contract.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise RuntimeError("PyYAML is required to load finalized edge trade geometry") from exc

GEOMETRY_CONTRACT_VERSION = "trade_geometry_v1"
DEFAULT_BARRIER_REFERENCE = "Event_Vol"
DEFAULT_BARRIER_REFERENCE_UNITS = "return_fraction"
DEFAULT_ENTRY_TIMING = "after_closed_proxy_information_bar"

GEOMETRY_PAYLOAD_COLUMNS: tuple[str, ...] = (
    "geometry_contract_version",
    "pt_multiplier",
    "sl_multiplier",
    "reward_risk_ratio",
    "vertical_horizon_bars",
    "barrier_reference",
    "barrier_reference_units",
    "entry_timing",
    "tie_policy",
    "tick_resolution_price",
    "barrier_contract",
)
GEOMETRY_COLUMNS: tuple[str, ...] = (*GEOMETRY_PAYLOAD_COLUMNS, "geometry_hash")


class TradeGeometryError(ValueError):
    """Raised when a geometry contract is missing, inconsistent, or stale."""


def _positive_float(value: Any, field: str) -> float:
    try:
        out = float(value)
    except Exception as exc:
        raise TradeGeometryError(f"{field} must be numeric, got {value!r}") from exc
    if not (out > 0.0):
        raise TradeGeometryError(f"{field} must be > 0, got {out!r}")
    return out


def _positive_int(value: Any, field: str) -> int:
    try:
        out = int(value)
    except Exception as exc:
        raise TradeGeometryError(f"{field} must be an integer, got {value!r}") from exc
    if out <= 0:
        raise TradeGeometryError(f"{field} must be > 0, got {out!r}")
    return out


def canonical_geometry_hash(payload: Mapping[str, Any]) -> str:
    canonical = {key: payload[key] for key in GEOMETRY_PAYLOAD_COLUMNS}
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_trade_geometry(
    geometry: Mapping[str, Any],
    *,
    expected_side_name: str | None = None,
    expected_edge_id: str | None = None,
) -> dict[str, Any]:
    missing = [key for key in GEOMETRY_COLUMNS if key not in geometry]
    if missing:
        prefix = f"{expected_edge_id}: " if expected_edge_id else ""
        raise TradeGeometryError(prefix + f"missing geometry fields: {missing}")

    normalized: dict[str, Any] = {
        "geometry_contract_version": str(geometry["geometry_contract_version"]).strip(),
        "pt_multiplier": _positive_float(geometry["pt_multiplier"], "pt_multiplier"),
        "sl_multiplier": _positive_float(geometry["sl_multiplier"], "sl_multiplier"),
        "reward_risk_ratio": _positive_float(geometry["reward_risk_ratio"], "reward_risk_ratio"),
        "vertical_horizon_bars": _positive_int(geometry["vertical_horizon_bars"], "vertical_horizon_bars"),
        "barrier_reference": str(geometry["barrier_reference"]).strip(),
        "barrier_reference_units": str(geometry["barrier_reference_units"]).strip(),
        "entry_timing": str(geometry["entry_timing"]).strip(),
        "tie_policy": str(geometry["tie_policy"]).strip(),
        "tick_resolution_price": str(geometry["tick_resolution_price"]).strip(),
        "barrier_contract": str(geometry["barrier_contract"]).strip(),
    }
    if normalized["geometry_contract_version"] != GEOMETRY_CONTRACT_VERSION:
        raise TradeGeometryError(
            f"Unsupported geometry_contract_version={normalized['geometry_contract_version']!r}; "
            f"expected {GEOMETRY_CONTRACT_VERSION!r}"
        )
    for field in (
        "barrier_reference",
        "barrier_reference_units",
        "entry_timing",
        "tie_policy",
        "tick_resolution_price",
        "barrier_contract",
    ):
        if not normalized[field]:
            raise TradeGeometryError(f"{field} must be non-empty")

    computed_rr = normalized["pt_multiplier"] / normalized["sl_multiplier"]
    if abs(normalized["reward_risk_ratio"] - computed_rr) > max(1e-9, abs(computed_rr) * 1e-9):
        raise TradeGeometryError(
            "reward_risk_ratio does not equal pt_multiplier/sl_multiplier: "
            f"declared={normalized['reward_risk_ratio']}, computed={computed_rr}"
        )

    side = str(expected_side_name or "").strip().lower()
    contract = normalized["barrier_contract"].lower()
    if side == "long" and not (
        "pt_price_above_entry" in contract and "sl_price_below_entry" in contract
    ):
        raise TradeGeometryError(f"Long edge has incompatible barrier_contract={normalized['barrier_contract']!r}")
    if side == "short" and not (
        "pt_price_below_entry" in contract and "sl_price_above_entry" in contract
    ):
        raise TradeGeometryError(f"Short edge has incompatible barrier_contract={normalized['barrier_contract']!r}")

    computed_hash = canonical_geometry_hash(normalized)
    declared_hash = str(geometry["geometry_hash"]).strip()
    if declared_hash != computed_hash:
        prefix = f"{expected_edge_id}: " if expected_edge_id else ""
        raise TradeGeometryError(
            prefix + f"geometry_hash mismatch: declared={declared_hash}, computed={computed_hash}"
        )
    normalized["geometry_hash"] = computed_hash
    # REVISED 2026-09-25: additive pass-through, deliberately NOT part of
    # GEOMETRY_PAYLOAD_COLUMNS/the hash -- defaults preserve the exact prior
    # behavior (calendar_time=False) for every geometry dict that doesn't
    # carry these keys (e.g. a mapping reconstructed from an older manifest).
    normalized["calendar_time"] = bool(geometry.get("calendar_time", False))
    normalized["vertical_horizon_hours"] = geometry.get("vertical_horizon_hours")
    return normalized


def load_trade_geometry(
    edge_yaml_path: Path,
    *,
    expected_edge_id: str | None = None,
    expected_side_name: str | None = None,
) -> dict[str, Any]:
    if not edge_yaml_path.exists() or not edge_yaml_path.is_file():
        raise FileNotFoundError(f"edge.yaml does not exist: {edge_yaml_path}")
    raw = yaml.safe_load(edge_yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TradeGeometryError(f"edge.yaml must contain a mapping: {edge_yaml_path}")

    edge_id = str(raw.get("edge_id", "")).strip()
    side_name = str(raw.get("side_name", "")).strip().lower()
    if expected_edge_id and edge_id != expected_edge_id:
        raise TradeGeometryError(f"edge_id mismatch: expected={expected_edge_id}, edge_yaml={edge_id}")
    if expected_side_name and side_name != str(expected_side_name).strip().lower():
        raise TradeGeometryError(
            f"side_name mismatch: expected={expected_side_name}, edge_yaml={side_name}"
        )
    if side_name not in {"long", "short"}:
        raise TradeGeometryError(f"edge.yaml side_name must be long/short, got {side_name!r}")

    labeling = raw.get("labeling")
    if not isinstance(labeling, dict):
        raise TradeGeometryError(f"{edge_id or edge_yaml_path}: missing labeling mapping")

    horizon = labeling.get("vertical_horizon_bars", labeling.get("vertical_horizon"))
    barrier_key = f"{side_name}_barrier_contract"
    barrier_contract = labeling.get("barrier_contract", labeling.get(barrier_key))
    if not barrier_contract:
        raise TradeGeometryError(f"{edge_id}: missing labeling.{barrier_key}")

    pt = _positive_float(labeling.get("pt_multiplier"), "pt_multiplier")
    sl = _positive_float(labeling.get("sl_multiplier"), "sl_multiplier")
    rr = labeling.get("reward_risk_ratio", pt / sl)
    payload: dict[str, Any] = {
        "geometry_contract_version": GEOMETRY_CONTRACT_VERSION,
        "pt_multiplier": pt,
        "sl_multiplier": sl,
        "reward_risk_ratio": _positive_float(rr, "reward_risk_ratio"),
        "vertical_horizon_bars": _positive_int(horizon, "vertical_horizon_bars"),
        "barrier_reference": str(labeling.get("barrier_reference", DEFAULT_BARRIER_REFERENCE)).strip(),
        "barrier_reference_units": str(
            labeling.get("barrier_reference_units", DEFAULT_BARRIER_REFERENCE_UNITS)
        ).strip(),
        "entry_timing": str(labeling.get("entry_timing", DEFAULT_ENTRY_TIMING)).strip(),
        "tie_policy": str(labeling.get("tie_policy", "")).strip(),
        "tick_resolution_price": str(labeling.get("tick_resolution_price", "")).strip(),
        "barrier_contract": str(barrier_contract).strip(),
        # REVISED 2026-09-25 (calendar-time labeling rollout): purely ADDITIVE
        # fields, deliberately kept OUT of GEOMETRY_PAYLOAD_COLUMNS so the
        # geometry_hash formula and every existing consumer (portfolio_live,
        # portfolio_allocator, Step01-03) is byte-for-byte unchanged for every
        # edge that doesn't set labeling.vertical_horizon_hours. When present,
        # oof_exact_execution.py uses this real-time deadline instead of the
        # (still-required, otherwise-unused) vertical_horizon_bars above --
        # see shared/labels/calendar_time_barriers.py for the exact mechanism
        # already used by Gate A/B/C's v3_horizon_lab.py.
        "calendar_time": "vertical_horizon_hours" in labeling,
        "vertical_horizon_hours": (
            _positive_float(labeling["vertical_horizon_hours"], "vertical_horizon_hours")
            if "vertical_horizon_hours" in labeling
            else None
        ),
    }
    payload["geometry_hash"] = canonical_geometry_hash(payload)
    return validate_trade_geometry(
        payload,
        expected_side_name=side_name,
        expected_edge_id=edge_id or expected_edge_id,
    )


def geometry_from_mapping(mapping: Mapping[str, Any], *, expected_side_name: str | None = None) -> dict[str, Any]:
    return validate_trade_geometry(
        {key: mapping[key] for key in GEOMETRY_COLUMNS if key in mapping},
        expected_side_name=expected_side_name,
        expected_edge_id=str(mapping.get("edge_id", "")).strip() or None,
    )


def geometry_columns_present(mapping: Mapping[str, Any]) -> bool:
    return all(key in mapping for key in GEOMETRY_COLUMNS)
