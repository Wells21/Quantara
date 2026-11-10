# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
"""Generic edge scoring adapter for V2 SOT runtime packages.

EdgeScorer owns only artifact loading, selected-feature ordering, probability
calibration, AFML bet sizing, threshold decisions, and audit metadata. It must
never know edge alpha logic. Edge runtimes own feature/event/model-row creation.
"""

from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import joblib
except ImportError as exc:  # pragma: no cover
    raise ImportError("EdgeScorer requires joblib to load model/calibrator pickle artifacts.") from exc

from shared.calibration.sigmoid_afml import (
    AFML_BET_SIZE_COL,
    AFML_ECONOMIC_STRENGTH_COL,
    AFML_EFFECTIVE_PROBABILITY_COL,
    AFML_SIZING_METHOD,
    AFML_SIZING_METHOD_COL,
    APPROVED_SIGNAL_COL,
    BREAK_EVEN_PROBABILITY_COL,
    CALIBRATED_PROBABILITY_COL,
    REWARD_RISK_RATIO_COL,
    apply_probability_flow,
    EXECUTED_TARGET_CONTRACT_VERSION,
    load_calibrator_json,
    payoff_profile_from_threshold_payload,
    validate_threshold_payload,
)
from shared.edges.edge_layout import EdgeLayout, get_edge_layout, get_edge_layout_from_edge_root
from shared.edges.trade_geometry import load_trade_geometry as load_trade_geometry_contract
from shared.io.artifact_hash import hash_artifact
from shared.models.sequential_bootstrap_bagging import from_portable_payload, is_portable_payload
from shared.validation.feature_schema import FORBIDDEN_MODEL_INPUT_COLUMNS
from shared.validation.v3_semantic_contract import (
    EXECUTION_CONTRACT_VERSION,
    GROSS_LABEL_CONTRACT_VERSION,
    TARGET_COLUMN,
    validate_payoff_context_mapping,
)


class EdgeScoringNotReady(RuntimeError):
    """Raised when an edge must fail closed because scoring artifacts are incomplete."""


class EdgeScoringContractError(RuntimeError):
    """Raised when canonical scoring contracts are inconsistent."""


OUTPUT_COLUMNS_TO_REPLACE: tuple[str, ...] = (
    "edge_id",
    "score_timestamp",
    "raw_probability",
    "calibrated_probability",
    "reward_risk_ratio",
    "break_even_probability",
    "afml_economic_strength",
    "afml_effective_probability",
    "afml_bet_size",
    "afml_sizing_method",
    "selected_threshold",
    "threshold_probability_column",
    "approved_signal",
    "probability_flow",
    "model_artifact_hash",
    "calibrator_artifact_hash",
    "threshold_artifact_hash",
    "selected_features_hash",
    "model_input_schema_hash",
    "gross_label_contract_version",
    "execution_contract_version",
    "executed_target_contract_version",
    "payoff_context_version",
    "payoff_context_id",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_ARTIFACT_CACHE_LOCK = threading.RLock()
_ARTIFACT_CACHE: dict[tuple[Any, ...], Any] = {}
_ARTIFACT_CACHE_STATS: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "loads": 0,
}


def _artifact_signature(path: Path) -> tuple[str, int, int]:
    """Return a conservative cache signature for a filesystem artifact."""
    if not path.exists():
        raise EdgeScoringNotReady(f"Missing required scoring artifact: {path}")
    stat = path.stat()
    return (str(path.resolve()), int(stat.st_size), int(stat.st_mtime_ns))


def _cached_artifact(kind: str, path: Path, loader: Any, *, copy_result: bool = True, extra_key: Any = None) -> Any:
    """Load an artifact once per unchanged file signature.

    This is a live-speed cache only. It does not weaken any scoring contract:
    missing files still fail closed, JSON/schema/model validation still runs, and
    the cache automatically misses when the file size or mtime changes.
    """
    signature = _artifact_signature(path)
    key = (kind, signature, extra_key)
    with _ARTIFACT_CACHE_LOCK:
        if key in _ARTIFACT_CACHE:
            _ARTIFACT_CACHE_STATS["hits"] = int(_ARTIFACT_CACHE_STATS.get("hits", 0)) + 1
            value = _ARTIFACT_CACHE[key]
            return copy.deepcopy(value) if copy_result else value

        _ARTIFACT_CACHE_STATS["misses"] = int(_ARTIFACT_CACHE_STATS.get("misses", 0)) + 1
        value = loader()
        _ARTIFACT_CACHE[key] = value
        _ARTIFACT_CACHE_STATS["loads"] = int(_ARTIFACT_CACHE_STATS.get("loads", 0)) + 1
        return copy.deepcopy(value) if copy_result else value


def edge_scorer_cache_snapshot() -> dict[str, Any]:
    """Return cache observability for dashboard/status logs."""
    with _ARTIFACT_CACHE_LOCK:
        by_kind: dict[str, int] = {}
        for key in _ARTIFACT_CACHE:
            kind = str(key[0]) if key else "unknown"
            by_kind[kind] = by_kind.get(kind, 0) + 1
        return {
            "enabled": True,
            "cache_entries": int(len(_ARTIFACT_CACHE)),
            "entries_by_kind": by_kind,
            "stats": dict(_ARTIFACT_CACHE_STATS),
        }


def clear_edge_scorer_artifact_cache() -> None:
    """Clear cached scorer artifacts. Intended for manual diagnostics/tests only."""
    with _ARTIFACT_CACHE_LOCK:
        _ARTIFACT_CACHE.clear()
        _ARTIFACT_CACHE_STATS.update({"hits": 0, "misses": 0, "loads": 0})


def _load_json_uncached(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EdgeScoringNotReady(f"Missing required scoring artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeScoringContractError(f"Invalid JSON artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EdgeScoringContractError(f"JSON artifact must be an object: {path}")
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    return _cached_artifact("json", path, lambda: _load_json_uncached(path), copy_result=True)


class EdgeScorer:
    """Shared SOT scoring adapter for any side-specialized edge."""

    def __init__(self, edge_root: Path) -> None:
        self.layout: EdgeLayout = get_edge_layout_from_edge_root(edge_root)
        self.edge_root = self.layout.edge_root.resolve()
        self.asset = self.layout.asset
        self.family = self.layout.family
        self.side_name = self.layout.side_name
        self.side_value = self.layout.side_value
        self.edge_id = self.layout.edge_id
        self.edge_stem = self.layout.edge_stem
        self.selected_features_path = self.layout.selected_features_json
        self.model_input_schema_path = self.layout.model_input_schema_json
        self.model_path = self.layout.model_pkl
        self.calibrator_json_path = self.layout.calibrator_json
        self.calibrator_pkl_path = self.layout.calibrator_pkl
        self.threshold_path = self.layout.selected_threshold_json

    @classmethod
    def from_edge_root(cls, edge_root: Path) -> "EdgeScorer":
        return cls(edge_root=edge_root)

    def validate_artifacts(self) -> dict[str, Any]:
        missing = [str(path) for path in self.layout.canonical_scoring_artifacts() if not path.exists()]
        if missing:
            raise EdgeScoringNotReady(f"{self.edge_id} scoring artifacts missing: {missing}")
        selected_features = self.load_selected_features()
        model_input_schema = self.load_model_input_schema()
        threshold = self.load_threshold()
        calibrator = self.load_calibrator()
        profile = self.load_payoff_profile()
        geometry = self.load_trade_geometry()
        if str(calibrator.get("target_contract_version", "")) != EXECUTED_TARGET_CONTRACT_VERSION:
            raise EdgeScoringContractError(
                f"Calibrator target contract is stale for {self.edge_id}; rerun Step 04."
            )
        if str(calibrator.get("target_column", "")) != TARGET_COLUMN:
            raise EdgeScoringContractError(f"Calibrator target_column must be {TARGET_COLUMN}.")
        if str(calibrator.get("gross_label_contract_version", "")) != GROSS_LABEL_CONTRACT_VERSION:
            raise EdgeScoringContractError(f"Calibrator gross label contract is stale for {self.edge_id}.")
        if str(calibrator.get("execution_contract_version", "")) != EXECUTION_CONTRACT_VERSION:
            raise EdgeScoringContractError(f"Calibrator execution contract is stale for {self.edge_id}.")
        threshold_payload = self.load_threshold_payload()
        payoff_context = validate_payoff_context_mapping(
            threshold_payload.get("payoff_context") or {},
            expected_edge_id=self.edge_id,
            expected_threshold=threshold,
            expected_geometry_hash=str(geometry["geometry_hash"]),
        )
        return {
            "edge_id": self.edge_id,
            "status": "PASS",
            "selected_feature_count": len(selected_features),
            "model_input_column_count": len(model_input_schema["model_input_columns"]),
            "threshold": threshold,
            "threshold_probability_column": CALIBRATED_PROBABILITY_COL,
            "calibrator_method": calibrator.get("method"),
            "reward_risk_ratio": float(profile["empirical_reward_risk_ratio"]),
            "break_even_probability": float(profile["empirical_break_even_probability"]),
            "afml_sizing_method": AFML_SIZING_METHOD,
            "payoff_profile_version": profile["payoff_profile_version"],
            "geometry_hash": str(geometry["geometry_hash"]),
            "payoff_context_id": payoff_context["payoff_context_id"],
            "payoff_context_version": payoff_context["payoff_context_version"],
        }

    def load_selected_features(self) -> list[str]:
        payload = _load_json(self.selected_features_path)
        if payload.get("edge_id") not in {None, self.edge_id}:
            raise EdgeScoringContractError(
                f"selected_features edge_id mismatch: expected {self.edge_id}, got {payload.get('edge_id')!r}"
            )
        if payload.get("feature_order_locked") is not True:
            raise EdgeScoringContractError("selected_features.json must set feature_order_locked=true.")
        features = payload.get("selected_features")
        if not isinstance(features, list) or not features:
            raise EdgeScoringContractError("selected_features.json must contain a non-empty selected_features list.")
        features = [str(feature) for feature in features]
        duplicates = sorted({feature for feature in features if features.count(feature) > 1})
        if duplicates:
            raise EdgeScoringContractError(f"Duplicate selected features: {duplicates}")
        forbidden = [feature for feature in features if feature in FORBIDDEN_MODEL_INPUT_COLUMNS]
        if forbidden:
            raise EdgeScoringContractError(f"Selected features contain forbidden columns: {forbidden}")
        return features

    def load_model_input_schema(self) -> dict[str, Any]:
        payload = _load_json(self.model_input_schema_path)
        if payload.get("edge_id") not in {None, self.edge_id}:
            raise EdgeScoringContractError(
                f"model_input_schema edge_id mismatch: expected {self.edge_id}, got {payload.get('edge_id')!r}"
            )
        columns = payload.get("model_input_columns")
        if not isinstance(columns, list) or not columns:
            raise EdgeScoringContractError("model_input_schema.json must contain non-empty model_input_columns.")
        columns = [str(column) for column in columns]
        selected = self.load_selected_features()
        if columns != selected:
            raise EdgeScoringContractError("model_input_schema.json must match artifacts/selected_features.json exactly and in order.")
        if payload.get("model_input_order_locked") is not True:
            raise EdgeScoringContractError("model_input_schema.json must set model_input_order_locked=true.")
        payload["model_input_columns"] = columns
        return payload

    def load_model(self) -> Any:
        if not self.model_path.exists():
            raise EdgeScoringNotReady(f"Missing model artifact: {self.model_path}")
        try:
            def _load() -> Any:
                raw = joblib.load(self.model_path)
                # Step02 persists the AFML Ch.6 sequential-bootstrap-bagging
                # ensemble as a plain portable payload, not the wrapper class
                # instance directly -- see shared/models/sequential_bootstrap_
                # bagging.py's to_portable_payload docstring for why (pickle's
                # strict class-identity check makes the class itself
                # unpicklable across the Colab-inline-definition boundary).
                # Reconstruct the real class here, the one place that already
                # imports the canonical module.
                if is_portable_payload(raw):
                    return from_portable_payload(raw)
                return raw
            return _cached_artifact("joblib_model", self.model_path, _load, copy_result=False)
        except Exception as exc:
            raise EdgeScoringContractError(f"Could not load model artifact {self.model_path}: {exc}") from exc

    def load_calibrator(self) -> dict[str, Any]:
        calibrator = _cached_artifact(
            "calibrator_json",
            self.calibrator_json_path,
            lambda: load_calibrator_json(self.calibrator_json_path),
            copy_result=True,
        )
        if calibrator.get("edge_id") not in {None, self.edge_id}:
            raise EdgeScoringContractError(
                f"calibrator edge_id mismatch: expected {self.edge_id}, got {calibrator.get('edge_id')!r}"
            )
        return calibrator

    def load_trade_geometry(self) -> dict[str, Any]:
        return _cached_artifact(
            "trade_geometry",
            self.edge_root / "edge.yaml",
            lambda: load_trade_geometry_contract(
                self.edge_root / "edge.yaml",
                expected_edge_id=self.edge_id,
                expected_side_name=self.side_name,
            ),
            copy_result=True,
        )

    def load_threshold_payload(self) -> dict[str, Any]:
        payload = _load_json(self.threshold_path)
        if payload.get("edge_id") not in {None, self.edge_id}:
            raise EdgeScoringContractError(
                f"threshold edge_id mismatch: expected {self.edge_id}, got {payload.get('edge_id')!r}"
            )
        validate_threshold_payload(payload)
        return payload

    def load_payoff_profile(self) -> dict[str, Any]:
        try:
            return payoff_profile_from_threshold_payload(self.load_threshold_payload())
        except Exception as exc:
            raise EdgeScoringContractError(
                f"{self.edge_id}: selected threshold payoff profile is stale/invalid: {exc}"
            ) from exc

    def load_threshold(self) -> float:
        return validate_threshold_payload(self.load_threshold_payload())

    def explain_missing_features(self, model_rows: pd.DataFrame) -> dict[str, Any]:
        selected = self.load_selected_features()
        missing = [feature for feature in selected if feature not in model_rows.columns]
        forbidden_present = sorted(set(model_rows.columns).intersection(FORBIDDEN_MODEL_INPUT_COLUMNS))
        dtype_issues = [
            feature
            for feature in selected
            if feature in model_rows.columns and not pd.api.types.is_numeric_dtype(model_rows[feature])
        ]
        nan_summary = {
            feature: int(model_rows[feature].isna().sum())
            for feature in selected
            if feature in model_rows.columns and int(model_rows[feature].isna().sum()) > 0
        }
        return {
            "edge_id": self.edge_id,
            "selected_feature_count": len(selected),
            "missing_selected_features": missing,
            "forbidden_columns_present": forbidden_present,
            "non_numeric_selected_features": dtype_issues,
            "nan_summary": nan_summary,
            "ready": not missing and not dtype_issues,
        }

    def _positive_class_probability(self, model: Any, x_data: pd.DataFrame) -> np.ndarray:
        if not hasattr(model, "predict_proba"):
            raise EdgeScoringContractError("Model artifact must expose predict_proba.")
        proba = np.asarray(model.predict_proba(x_data))
        if proba.ndim != 2:
            raise EdgeScoringContractError("Model predict_proba must return a 2D array.")
        classes = getattr(model, "classes_", None)
        if proba.shape[1] == 1:
            cls = int(classes[0]) if classes is not None and len(classes) else 1
            return np.ones(len(x_data), dtype="float64") if cls == 1 else np.zeros(len(x_data), dtype="float64")
        if classes is None:
            return proba[:, -1].astype("float64")
        class_to_col = {int(cls): i for i, cls in enumerate(classes)}
        if 1 not in class_to_col:
            raise EdgeScoringContractError("Model classes_ does not contain positive class 1.")
        return proba[:, class_to_col[1]].astype("float64")

    def _artifact_hashes(self) -> dict[str, dict[str, Any]]:
        paths = {
            "model_artifact_hash": self.model_path,
            "calibrator_artifact_hash": self.calibrator_json_path,
            "threshold_artifact_hash": self.threshold_path,
            "selected_features_hash": self.selected_features_path,
            "model_input_schema_hash": self.model_input_schema_path,
        }
        out: dict[str, dict[str, Any]] = {}
        for key, path in paths.items():
            out[key] = _cached_artifact(
                "artifact_hash",
                path,
                lambda p=path: hash_artifact(p, root=self.edge_root).to_dict(),
                copy_result=True,
                extra_key=str(self.edge_root),
            )
        return out

    def score(self, model_rows: pd.DataFrame, latest_only: bool = False) -> pd.DataFrame:
        if model_rows is None or model_rows.empty:
            raise EdgeScoringContractError("model_rows must be a non-empty DataFrame.")
        self.validate_artifacts()
        rows = model_rows.tail(1).copy() if latest_only else model_rows.copy()
        selected = self.load_selected_features()
        missing_report = self.explain_missing_features(rows)
        if missing_report["missing_selected_features"]:
            raise EdgeScoringContractError(f"Missing selected features: {missing_report['missing_selected_features']}")
        if missing_report["non_numeric_selected_features"]:
            raise EdgeScoringContractError(f"Selected features must be numeric: {missing_report['non_numeric_selected_features']}")
        x_data = rows[selected].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
        before = len(x_data)
        keep_mask = x_data.notna().all(axis=1)
        rows = rows.loc[keep_mask].copy()
        x_data = x_data.loc[keep_mask].copy()
        if x_data.empty:
            raise EdgeScoringContractError(f"All rows dropped after selected-feature NaN filtering. before={before}")
        rows = rows.drop(columns=[c for c in OUTPUT_COLUMNS_TO_REPLACE if c in rows.columns], errors="ignore")
        raw_probability = self._positive_class_probability(self.load_model(), x_data)
        probability_flow = apply_probability_flow(
            raw_probability,
            self.load_calibrator(),
            self.load_threshold(),
            payoff_profile=self.load_payoff_profile(),
        )
        scored = pd.concat([rows.reset_index(drop=True), probability_flow.reset_index(drop=True)], axis=1)
        scored.insert(0, "score_timestamp", _now_utc())
        scored.insert(0, "edge_id", self.edge_id)
        for column, record in self._artifact_hashes().items():
            scored[column] = record["sha256"]
        threshold_payload = self.load_threshold_payload()
        payoff_context = validate_payoff_context_mapping(
            threshold_payload.get("payoff_context") or {},
            expected_edge_id=self.edge_id,
            expected_threshold=self.load_threshold(),
            expected_geometry_hash=str(self.load_trade_geometry()["geometry_hash"]),
        )
        scored["gross_label_contract_version"] = payoff_context["gross_label_contract_version"]
        scored["execution_contract_version"] = payoff_context["execution_contract_version"]
        scored["executed_target_contract_version"] = payoff_context["executed_target_contract_version"]
        scored["payoff_context_version"] = payoff_context["payoff_context_version"]
        scored["payoff_context_id"] = payoff_context["payoff_context_id"]
        scored["threshold_probability_column"] = CALIBRATED_PROBABILITY_COL
        scored["probability_flow"] = "raw_probability -> sigmoid calibrated_probability -> payoff-aware AFML bet size -> calibrated-threshold decision"
        scored[APPROVED_SIGNAL_COL] = scored[APPROVED_SIGNAL_COL].astype(bool)
        return scored


def preload_edge_scorer_artifacts(edge_root: Path, *, validate: bool = True) -> dict[str, Any]:
    """Warm all scorer-owned artifacts for one edge into the in-process cache.

    This is Phase 9 startup warmup. It loads the same artifacts that score()
    already uses, validates the same contracts, and returns diagnostics. It does
    not change probabilities, thresholds, model inputs, or runtime edge logic.
    """
    start = datetime.now(timezone.utc)
    scorer = EdgeScorer.from_edge_root(edge_root)
    if validate:
        validation = scorer.validate_artifacts()
    else:
        validation = {"edge_id": scorer.edge_id, "status": "SKIPPED_VALIDATION"}
    model = scorer.load_model()
    hashes = scorer._artifact_hashes()
    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
    return {
        "edge_id": scorer.edge_id,
        "asset": scorer.asset,
        "family": scorer.family,
        "side_name": scorer.side_name,
        "status": "PASS",
        "elapsed_ms": float(elapsed),
        "selected_feature_count": validation.get("selected_feature_count"),
        "model_input_column_count": validation.get("model_input_column_count"),
        "threshold": validation.get("threshold"),
        "threshold_probability_column": validation.get("threshold_probability_column"),
        "calibrator_method": validation.get("calibrator_method"),
        "reward_risk_ratio": validation.get("reward_risk_ratio"),
        "break_even_probability": validation.get("break_even_probability"),
        "afml_sizing_method": validation.get("afml_sizing_method"),
        "geometry_hash": validation.get("geometry_hash"),
        "model_class": type(model).__name__,
        "artifact_hash_keys": sorted(hashes.keys()),
        "cache_snapshot": edge_scorer_cache_snapshot(),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EdgeScorer identity/path self-test for any edge.")
    parser.add_argument("--edge-root", default=".", help="Path to an edge root. Defaults to current directory.")
    return parser.parse_args()


def _run_self_test() -> None:
    args = _parse_args()
    scorer = EdgeScorer.from_edge_root(Path(args.edge_root))
    print("✅ shared.scoring.edge_scorer self-test passed.")
    print(f"Edge: {scorer.edge_id}")
    print(f"Edge stem: {scorer.edge_stem}")
    print(f"Model: {scorer.model_path.as_posix()}")
    print(f"Calibrator: {scorer.calibrator_json_path.as_posix()}")
    print(f"Threshold: {scorer.threshold_path.as_posix()}")


if __name__ == "__main__":
    _run_self_test()
