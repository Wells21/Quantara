"""
Lightweight JSON schema/config loader for V2 edge contracts.

Blueprint purpose
-----------------
Every V2 artifact has an explicit schema. Edge packages must not hide assumptions
inside ad-hoc CSV merges or dashboard-side feature reconstruction. This module is
for loading JSON contract files and validating that required keys exist before a
component is trusted.

This is intentionally not a full JSON Schema implementation yet. It is a small,
standard-library-only validator for the first V2 foundation stage. Later, we can
add richer schema enforcement without changing callers.

Run from ``trading_models_v2``:

    python ./shared/schemas/json_schema_loader.py

or from the repository root:

    python trading_models_v2/shared/schemas/json_schema_loader.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class SchemaLoadError(RuntimeError):
    """Raised when a schema/config file cannot be loaded safely."""


class SchemaValidationError(RuntimeError):
    """Raised when schema/config content fails required-key validation."""


@dataclass(frozen=True)
class JsonDocument:
    """Loaded JSON document with source path metadata."""

    path: Path
    data: Any

    def require_mapping(self) -> Mapping[str, Any]:
        if not isinstance(self.data, Mapping):
            raise SchemaValidationError(f"JSON document must be an object: {self.path}")
        return self.data


def load_json(path: str | Path) -> JsonDocument:
    """Load a JSON file with clear fail-closed errors."""

    json_path = Path(path)
    if not json_path.exists():
        raise SchemaLoadError(f"JSON file does not exist: {json_path}")
    if not json_path.is_file():
        raise SchemaLoadError(f"JSON path is not a file: {json_path}")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaLoadError(f"Invalid JSON in {json_path}: {exc}") from exc

    return JsonDocument(path=json_path, data=data)


def require_keys(mapping: Mapping[str, Any], required_keys: Sequence[str], context: str) -> None:
    """Fail if required top-level keys are missing."""

    missing = [key for key in required_keys if key not in mapping]
    if missing:
        raise SchemaValidationError(f"{context} missing required key(s): {missing}")


def require_nested_keys(mapping: Mapping[str, Any], required_paths: Sequence[Sequence[str]], context: str) -> None:
    """Fail if required nested key paths are missing.

    Example
    -------
    ``require_nested_keys(manifest, [("identity", "edge_id")], "edge_manifest")``
    requires ``manifest["identity"]["edge_id"]``.
    """

    missing: list[str] = []

    for path_parts in required_paths:
        current: Any = mapping
        valid = True
        for part in path_parts:
            if not isinstance(current, Mapping) or part not in current:
                valid = False
                break
            current = current[part]
        if not valid:
            missing.append(".".join(path_parts))

    if missing:
        raise SchemaValidationError(f"{context} missing required path(s): {missing}")


def require_bool(mapping: Mapping[str, Any], key: str, context: str) -> bool:
    """Read a required boolean key."""

    require_keys(mapping, [key], context)
    value = mapping[key]
    if not isinstance(value, bool):
        raise SchemaValidationError(f"{context}.{key} must be a boolean, got {type(value).__name__}")
    return value


def require_str(mapping: Mapping[str, Any], key: str, context: str, allow_empty: bool = False) -> str:
    """Read a required string key."""

    require_keys(mapping, [key], context)
    value = mapping[key]
    if not isinstance(value, str):
        raise SchemaValidationError(f"{context}.{key} must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise SchemaValidationError(f"{context}.{key} must not be empty")
    return value


def require_list(mapping: Mapping[str, Any], key: str, context: str) -> list[Any]:
    """Read a required list key."""

    require_keys(mapping, [key], context)
    value = mapping[key]
    if not isinstance(value, list):
        raise SchemaValidationError(f"{context}.{key} must be a list, got {type(value).__name__}")
    return value


def validate_feature_schema_document(document: JsonDocument) -> None:
    """Validate the minimum V2 feature schema document shape.

    Expected shape for early V2:

    {
      "schema_version": "v2.1",
      "features": [
        {
          "feature_name": "Close_Return_1",
          "owner_stage": "full_feature_factory",
          "dtype": "float64",
          "formula_description": "...",
          "required_inputs": ["Close"],
          "live_available": true,
          "uses_future_information": false,
          "allowed_for_model_input": true,
          "missing_value_policy": "drop_until_available"
        }
      ]
    }
    """

    data = document.require_mapping()
    require_keys(data, ["schema_version", "features"], "feature_schema")
    features = require_list(data, "features", "feature_schema")

    required_feature_keys = [
        "feature_name",
        "owner_stage",
        "dtype",
        "formula_description",
        "required_inputs",
        "live_available",
        "uses_future_information",
        "allowed_for_model_input",
        "missing_value_policy",
    ]

    seen: set[str] = set()
    for idx, row in enumerate(features):
        context = f"feature_schema.features[{idx}]"
        if not isinstance(row, Mapping):
            raise SchemaValidationError(f"{context} must be an object")
        require_keys(row, required_feature_keys, context)
        name = require_str(row, "feature_name", context)
        require_str(row, "owner_stage", context)
        require_str(row, "dtype", context)
        require_str(row, "formula_description", context)
        require_list(row, "required_inputs", context)
        require_bool(row, "live_available", context)
        require_bool(row, "uses_future_information", context)
        require_bool(row, "allowed_for_model_input", context)
        require_str(row, "missing_value_policy", context)

        if name in seen:
            raise SchemaValidationError(f"Duplicate feature_name in feature schema: {name}")
        seen.add(name)


def _run_self_test() -> None:
    import tempfile

    valid_doc = {
        "schema_version": "v2.1",
        "features": [
            {
                "feature_name": "Close_Return_1",
                "owner_stage": "full_feature_factory",
                "dtype": "float64",
                "formula_description": "One-bar close-to-close return.",
                "required_inputs": ["Close"],
                "live_available": True,
                "uses_future_information": False,
                "allowed_for_model_input": True,
                "missing_value_policy": "drop_until_available",
            }
        ],
    }

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "feature_schema.json"
        path.write_text(json.dumps(valid_doc, indent=2), encoding="utf-8")
        loaded = load_json(path)
        validate_feature_schema_document(loaded)

    print("✅ shared.schemas.json_schema_loader self-test passed.")
    print("Validated: feature schema minimum document shape")


if __name__ == "__main__":
    try:
        _run_self_test()
    except (SchemaLoadError, SchemaValidationError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
