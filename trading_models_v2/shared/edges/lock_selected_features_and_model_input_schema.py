"""Shared Step 04B selected-feature and model-input-schema locking.

SOT rule: Step 04B is the only official path that promotes feature-surgery
output into canonical artifacts/selected_features.json and
contracts/model_input_schema.json. Do not manually edit these artifacts.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from shared.validation.edge_contracts import edge_identity_from_root
from shared.validation.feature_schema import (
    FeatureSchemaValidationError,
    load_json,
    normalize_selected_features,
    validate_selected_features,
)


class LockSelectedFeaturesError(RuntimeError):
    """Raised when Step 04B must fail closed."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.resolve().as_posix()


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")


def _v2_root_from_edge_root(edge_root: Path) -> Path:
    try:
        return edge_root.resolve().parents[3]
    except IndexError as exc:
        raise LockSelectedFeaturesError(f"Could not resolve V2 root from edge_root: {edge_root}") from exc


def _validate_feature_surgery_manifest(path: Path, expected_edge_id: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = load_json(path)
    if not isinstance(payload, Mapping):
        raise LockSelectedFeaturesError(f"Feature surgery manifest must be an object: {path}")
    manifest_edge_id = payload.get("edge_id")
    if manifest_edge_id and manifest_edge_id != expected_edge_id:
        raise LockSelectedFeaturesError(
            f"Feature surgery manifest edge_id mismatch: expected {expected_edge_id}, got {manifest_edge_id!r}"
        )
    return dict(payload)


def lock_selected_features_and_model_input_schema(edge_root: str | Path) -> dict[str, Any]:
    edge_path = Path(edge_root).resolve()
    identity = edge_identity_from_root(edge_path)
    v2_root = _v2_root_from_edge_root(edge_path)
    edge_id = identity["edge_id"]
    edge_stem = identity["edge_stem"]

    source_selected = edge_path / "artifacts" / "feature_surgery" / f"{edge_stem}_selected_features.json"
    surgery_manifest_path = edge_path / "artifacts" / "feature_surgery" / "feature_surgery_manifest.json"
    feature_schema_path = edge_path / "contracts" / "feature_schema.json"
    canonical_selected_path = edge_path / "artifacts" / "selected_features.json"
    model_input_schema_path = edge_path / "contracts" / "model_input_schema.json"

    try:
        selected_features = normalize_selected_features(load_json(source_selected))
        feature_schema = load_json(feature_schema_path)
        if not isinstance(feature_schema, Mapping):
            raise LockSelectedFeaturesError("feature_schema.json must be a JSON object.")
        validation = validate_selected_features(selected_features, feature_schema, expected_edge_id=edge_id)
    except FeatureSchemaValidationError as exc:
        raise LockSelectedFeaturesError(str(exc)) from exc

    _validate_feature_surgery_manifest(surgery_manifest_path, edge_id)

    canonical_payload = {
        "schema_version": "v2.1",
        "edge_id": edge_id,
        "status": "selected_features_locked_not_deployable",
        "feature_order_locked": True,
        "selected_feature_count": len(selected_features),
        "source_artifact": _rel(source_selected, v2_root),
        "locked_at": now_utc(),
        "selected_features": selected_features,
    }
    model_input_payload = {
        "schema_version": "v2.1",
        "edge_id": edge_id,
        "live_must_reproduce_exact_selected_rows": True,
        "model_input_order_locked": True,
        "selected_features_artifact": "artifacts/selected_features.json",
        "model_input_column_count": len(selected_features),
        "model_input_columns": selected_features,
    }
    _save_json(canonical_selected_path, canonical_payload)
    _save_json(model_input_schema_path, model_input_payload)
    return {
        "edge_id": edge_id,
        "edge_stem": edge_stem,
        "selected_feature_count": len(selected_features),
        "validation": validation,
        "source_artifact": _rel(source_selected, v2_root),
        "canonical_selected_features": _rel(canonical_selected_path, v2_root),
        "model_input_schema": _rel(model_input_schema_path, v2_root),
    }


def _run_self_test() -> None:
    parser = argparse.ArgumentParser(description="Run generic Step 04B locking for any edge root.")
    parser.add_argument("--edge-root", default=".", help="Path to an edge root. Defaults to current directory.")
    parser.add_argument("--write", action="store_true", help="Actually lock canonical artifacts. Without this, only identity/path resolution is tested.")
    args = parser.parse_args()
    identity = edge_identity_from_root(Path(args.edge_root))
    print("✅ shared.edges.lock_selected_features_and_model_input_schema identity self-test passed.")
    print(f"Edge: {identity['edge_id']}")
    if args.write:
        result = lock_selected_features_and_model_input_schema(args.edge_root)
        print("✅ Selected features locked into canonical contracts.")
        print(json.dumps(result, indent=2, default=str))
    else:
        print("Dry-run only. Add --write to execute Step 04B locking.")


if __name__ == "__main__":
    _run_self_test()
