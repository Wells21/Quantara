# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
"""Shared feature-schema validation for V2 SOT contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from shared.contracts import FORBIDDEN_SELECTED_FEATURE_TOKENS, V2_CONTRACT_VERSION


class FeatureSchemaValidationError(RuntimeError):
    """Raised when feature/schema validation must fail closed."""


FORBIDDEN_MODEL_INPUT_COLUMNS: set[str] = {
    "meta_label",
    "label",
    "target",
    "t1",
    "barrier_hit",
    "resolution_source",
    "entry_price",
    "pt_price",
    "sl_price",
    "realized_r",
    "executed_r",
    "executed_meta_label",
    "gross_meta_label",
    "pt_hit_label",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "approved_signal",
    "selected_threshold",
    "portfolio_weight",
    "risk_fraction",
}


def load_json(path: str | Path) -> Any:
    file_path = Path(path)
    if not file_path.exists():
        raise FeatureSchemaValidationError(f"Missing JSON file: {file_path}")
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureSchemaValidationError(f"Invalid JSON in {file_path}: {exc}") from exc


def normalize_selected_features(payload: Any) -> list[str]:
    if isinstance(payload, list):
        features = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("selected_features"), list):
        features = payload["selected_features"]
    elif isinstance(payload, Mapping) and isinstance(payload.get("features"), list):
        features = payload["features"]
    else:
        raise FeatureSchemaValidationError(
            "Selected features must be a list or an object containing selected_features/features."
        )
    out = [str(feature) for feature in features]
    if not out:
        raise FeatureSchemaValidationError("Selected feature list is empty.")
    duplicates = sorted({feature for feature in out if out.count(feature) > 1})
    if duplicates:
        raise FeatureSchemaValidationError(f"Duplicate selected features: {duplicates}")
    return out


def feature_rows_by_name(feature_schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if feature_schema.get("schema_version") != V2_CONTRACT_VERSION:
        raise FeatureSchemaValidationError(
            f"feature_schema.schema_version must be {V2_CONTRACT_VERSION!r}, got {feature_schema.get('schema_version')!r}"
        )
    rows = feature_schema.get("features")
    if not isinstance(rows, list) or not rows:
        raise FeatureSchemaValidationError("feature_schema.features must be a non-empty list.")
    out: dict[str, Mapping[str, Any]] = {}
    for idx, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise FeatureSchemaValidationError(f"feature_schema.features[{idx}] must be an object.")
        name = row.get("feature_name")
        if not isinstance(name, str) or not name.strip():
            raise FeatureSchemaValidationError(f"feature_schema.features[{idx}].feature_name must be a non-empty string.")
        if name in out:
            raise FeatureSchemaValidationError(f"Duplicate feature_schema feature_name: {name}")
        out[name] = row
    return out


def validate_feature_schema_document(feature_schema: Mapping[str, Any], *, expected_edge_id: str | None = None) -> dict[str, Any]:
    if expected_edge_id is not None and feature_schema.get("edge_id") != expected_edge_id:
        raise FeatureSchemaValidationError(
            f"feature_schema.edge_id expected {expected_edge_id!r}, got {feature_schema.get('edge_id')!r}"
        )
    rows_by_name = feature_rows_by_name(feature_schema)
    allowed = []
    for name, row in rows_by_name.items():
        if row.get("allowed_for_model_input") is True:
            allowed.append(name)
            if row.get("live_available") is not True:
                raise FeatureSchemaValidationError(f"{name}: allowed_for_model_input=true but live_available is not true.")
            if row.get("uses_future_information") is not False:
                raise FeatureSchemaValidationError(f"{name}: allowed_for_model_input=true but uses_future_information is not false.")
    return {
        "edge_id": feature_schema.get("edge_id"),
        "feature_count": len(rows_by_name),
        "allowed_for_model_input_count": len(allowed),
    }


def validate_selected_features(
    selected_features: Sequence[str],
    feature_schema: Mapping[str, Any],
    *,
    expected_edge_id: str | None = None,
) -> dict[str, Any]:
    validate_feature_schema_document(feature_schema, expected_edge_id=expected_edge_id)
    rows_by_name = feature_rows_by_name(feature_schema)
    selected = [str(feature) for feature in selected_features]
    if not selected:
        raise FeatureSchemaValidationError("Selected features must not be empty.")
    duplicates = sorted({feature for feature in selected if selected.count(feature) > 1})
    if duplicates:
        raise FeatureSchemaValidationError(f"Duplicate selected features: {duplicates}")
    missing = [feature for feature in selected if feature not in rows_by_name]
    if missing:
        raise FeatureSchemaValidationError(f"Selected features missing from feature_schema.json: {missing}")
    forbidden_exact = [feature for feature in selected if feature in FORBIDDEN_MODEL_INPUT_COLUMNS]
    forbidden_token = [
        feature
        for feature in selected
        if any(token in feature.lower() for token in FORBIDDEN_SELECTED_FEATURE_TOKENS)
    ]
    if forbidden_exact or forbidden_token:
        raise FeatureSchemaValidationError(
            f"Selected features contain forbidden label/probability/risk fields. exact={forbidden_exact}, token_matches={forbidden_token}"
        )
    bad_flags: list[dict[str, Any]] = []
    for feature in selected:
        row = rows_by_name[feature]
        if (
            row.get("live_available") is not True
            or row.get("allowed_for_model_input") is not True
            or row.get("uses_future_information") is not False
        ):
            bad_flags.append(
                {
                    "feature_name": feature,
                    "live_available": row.get("live_available"),
                    "allowed_for_model_input": row.get("allowed_for_model_input"),
                    "uses_future_information": row.get("uses_future_information"),
                }
            )
    if bad_flags:
        raise FeatureSchemaValidationError(f"Selected features are not live-safe/model-input-safe: {bad_flags}")
    return {
        "edge_id": feature_schema.get("edge_id"),
        "selected_feature_count": len(selected),
        "feature_order_locked": True,
        "all_selected_features_in_schema": True,
        "all_selected_features_live_safe": True,
    }


def validate_selected_features_from_paths(
    selected_features_path: str | Path,
    feature_schema_path: str | Path,
    *,
    expected_edge_id: str | None = None,
) -> dict[str, Any]:
    selected = normalize_selected_features(load_json(selected_features_path))
    schema = load_json(feature_schema_path)
    if not isinstance(schema, Mapping):
        raise FeatureSchemaValidationError(f"feature_schema must be a JSON object: {feature_schema_path}")
    result = validate_selected_features(selected, schema, expected_edge_id=expected_edge_id)
    result["selected_features_path"] = str(selected_features_path)
    result["feature_schema_path"] = str(feature_schema_path)
    return result
