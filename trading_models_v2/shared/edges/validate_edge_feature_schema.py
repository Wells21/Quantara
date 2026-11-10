"""
Validate one V2 edge feature schema.

Blueprint purpose
-----------------
Every candidate and selected feature must be declared before model use. This
validator checks the edge-local feature_schema.json before build_features() is
implemented or trusted.

Run from ``trading_models_v2``:

    python ./shared/edges/validate_edge_feature_schema.py --asset XAUUSD --family trend --side long
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import V2_CONTRACT_VERSION  # noqa: E402
from shared.edges.edge_layout import EdgeLayout, get_edge_layout  # noqa: E402
from shared.schemas.json_schema_loader import (  # noqa: E402
    SchemaLoadError,
    SchemaValidationError,
    load_json,
    validate_feature_schema_document,
)


class EdgeFeatureSchemaValidationError(RuntimeError):
    """Raised when an edge feature schema violates V2 edge-level rules."""


def _expected_runtime_build_features_path(layout: EdgeLayout) -> str:
    try:
        runtime_rel = layout.runtime_py.resolve().relative_to(_V2_ROOT.resolve()).as_posix()
    except ValueError:
        runtime_rel = layout.runtime_py.as_posix()
    return f"{runtime_rel}::build_features"


def _allowed_computed_by_paths(layout: EdgeLayout) -> set[str]:
    """build_features, plus the edge's own feature_expansion.py, which build_features calls.

    Convention since 2026-09-11 (commit 4b58127f): features added by
    add_v1_feature_expansion name that function in computed_by.
    """
    runtime_path = _expected_runtime_build_features_path(layout)
    allowed = {runtime_path}
    runtime_source = layout.runtime_py.read_text(encoding="utf-8") if layout.runtime_py.exists() else ""
    if "add_v1_feature_expansion(" in runtime_source:
        edge_rel = runtime_path.rsplit("/", 1)[0] if "/" in runtime_path else ""
        prefix = f"{edge_rel}/" if edge_rel else ""
        allowed.add(f"{prefix}feature_expansion.py::add_v1_feature_expansion")
    return allowed


def _require_edge_schema_rules(data: Mapping[str, Any], layout: EdgeLayout) -> None:
    expected_edge_id = layout.edge_id
    allowed_computed_by = _allowed_computed_by_paths(layout)

    schema_version = data.get("schema_version")
    if schema_version != V2_CONTRACT_VERSION:
        raise EdgeFeatureSchemaValidationError(
            f"feature_schema.schema_version expected {V2_CONTRACT_VERSION!r}, got {schema_version!r}"
        )

    edge_id = data.get("edge_id")
    if edge_id != expected_edge_id:
        raise EdgeFeatureSchemaValidationError(
            f"feature_schema.edge_id expected {expected_edge_id!r}, got {edge_id!r}"
        )

    features = data.get("features")
    if not isinstance(features, list):
        raise EdgeFeatureSchemaValidationError("feature_schema.features must be a list")

    for idx, row in enumerate(features):
        if not isinstance(row, Mapping):
            raise EdgeFeatureSchemaValidationError(f"features[{idx}] must be an object")

        name = row.get("feature_name")
        live_available = row.get("live_available")
        uses_future = row.get("uses_future_information")
        allowed = row.get("allowed_for_model_input")
        tier = row.get("feature_tier")

        if tier not in {1, 2, 3}:
            raise EdgeFeatureSchemaValidationError(f"Feature {name!r} must declare feature_tier as 1, 2, or 3")

        if allowed is True:
            if live_available is not True:
                raise EdgeFeatureSchemaValidationError(
                    f"Feature {name!r} is allowed_for_model_input but live_available is not true"
                )
            if uses_future is not False:
                raise EdgeFeatureSchemaValidationError(
                    f"Feature {name!r} is allowed_for_model_input but uses_future_information is not false"
                )

        if row.get("computed_by") not in allowed_computed_by:
            raise EdgeFeatureSchemaValidationError(
                f"Feature {name!r} must be computed by one of {sorted(allowed_computed_by)!r}, got {row.get('computed_by')!r}"
            )


def validate_edge_feature_schema(asset: str, family: str, side: str) -> dict[str, Any]:
    layout = get_edge_layout(asset, family, side)
    path = layout.contracts_dir / "feature_schema.json"
    doc = load_json(path)
    validate_feature_schema_document(doc)
    data = doc.require_mapping()
    _require_edge_schema_rules(data, layout)

    features = data["features"]
    allowed = [row["feature_name"] for row in features if row.get("allowed_for_model_input") is True]
    deferred = [row["feature_name"] for row in features if row.get("feature_tier") == 2]
    rejected = [row["feature_name"] for row in features if row.get("feature_tier") == 3]

    return {
        "edge_id": layout.edge_id,
        "schema_path": path.as_posix(),
        "feature_count": len(features),
        "allowed_for_model_input_count": len(allowed),
        "deferred_tier2_count": len(deferred),
        "rejected_tier3_count": len(rejected),
        "expected_computed_by": _expected_runtime_build_features_path(layout),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a V2 edge feature schema.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--family", required=True)
    parser.add_argument("--side", required=True, choices=["long", "short"])
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = validate_edge_feature_schema(args.asset, args.family, args.side)
    print("✅ V2 edge feature schema validation passed.")
    print(f"Edge: {result['edge_id']}")
    print(f"Feature schema: {result['schema_path']}")
    print(f"Features declared: {result['feature_count']}")
    print(f"Allowed model-input candidates: {result['allowed_for_model_input_count']}")
    print(f"Deferred Tier 2 features: {result['deferred_tier2_count']}")
    print(f"Rejected Tier 3 features: {result['rejected_tier3_count']}")
    print(f"Computed by: {result['expected_computed_by']}")


if __name__ == "__main__":
    try:
        _run()
    except (SchemaLoadError, SchemaValidationError, EdgeFeatureSchemaValidationError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
