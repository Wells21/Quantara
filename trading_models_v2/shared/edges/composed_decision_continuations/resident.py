from __future__ import annotations

"""Resident Phase D.4 scorer using the unchanged shared EdgeScorer mathematics."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from shared.calibration.sigmoid_afml import (
    APPROVED_SIGNAL_COL,
    CALIBRATED_PROBABILITY_COL,
    apply_probability_flow,
)
from shared.scoring.edge_scorer import EdgeScorer, OUTPUT_COLUMNS_TO_REPLACE

from .base import ComposedDecisionContinuationError


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResidentEdgeScorer:
    """Load and validate scorer-owned artifacts once, then score from memory."""

    def __init__(self, edge_root: Path) -> None:
        self.scorer = EdgeScorer.from_edge_root(Path(edge_root))
        self.edge_id = str(self.scorer.edge_id)

        # All filesystem/artifact work is intentionally startup-only.
        self.validation = self.scorer.validate_artifacts()
        self.selected_features = self.scorer.load_selected_features()
        self.model_input_schema = self.scorer.load_model_input_schema()
        self.model = self.scorer.load_model()
        self.calibrator = self.scorer.load_calibrator()
        self.threshold = float(self.scorer.load_threshold())
        self.geometry = self.scorer.load_trade_geometry()
        self.artifact_hashes = {
            key: str(record["sha256"])
            for key, record in self.scorer._artifact_hashes().items()
        }

        if self.model_input_schema.get("model_input_columns") != self.selected_features:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: resident selected-feature/schema ordering mismatch"
            )
        if not self.selected_features:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: resident selected-feature list is empty"
            )

    def score_current(self, model_row: pd.DataFrame) -> pd.DataFrame:
        """Score exactly one already-approved current model row without file I/O."""

        if model_row is None or not isinstance(model_row, pd.DataFrame):
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: model_row must be a DataFrame"
            )
        if len(model_row) != 1:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: resident scorer requires exactly one current row"
            )

        rows = model_row.copy()
        missing = [
            feature
            for feature in self.selected_features
            if feature not in rows.columns
        ]
        if missing:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: current model row missing selected features: "
                f"{missing[:30]}"
            )

        x_data = (
            rows[self.selected_features]
            .apply(pd.to_numeric, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
        )
        keep_mask = x_data.notna().all(axis=1)
        rows = rows.loc[keep_mask].copy()
        x_data = x_data.loc[keep_mask].copy()
        if x_data.empty:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: current model row dropped after selected-feature "
                "numeric/NaN filtering"
            )

        rows = rows.drop(
            columns=[
                column
                for column in OUTPUT_COLUMNS_TO_REPLACE
                if column in rows.columns
            ],
            errors="ignore",
        )

        raw_probability = self.scorer._positive_class_probability(
            self.model,
            x_data,
        )
        probability_flow = apply_probability_flow(
            raw_probability,
            self.calibrator,
            self.threshold,
            reward_risk_ratio=float(self.geometry["reward_risk_ratio"]),
        )

        scored = pd.concat(
            [rows.reset_index(drop=True), probability_flow.reset_index(drop=True)],
            axis=1,
        )
        scored.insert(0, "score_timestamp", _now_utc())
        scored.insert(0, "edge_id", self.edge_id)

        for column, sha256 in self.artifact_hashes.items():
            scored[column] = sha256

        scored["threshold_probability_column"] = CALIBRATED_PROBABILITY_COL
        scored["probability_flow"] = (
            "raw_probability -> sigmoid calibrated_probability -> "
            "payoff-aware AFML bet size -> calibrated-threshold decision"
        )
        scored[APPROVED_SIGNAL_COL] = scored[APPROVED_SIGNAL_COL].astype(bool)
        return scored.reset_index(drop=True)

    def state_summary(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "resident": True,
            "objects_loaded_once_at_startup": True,
            "selected_feature_count": int(len(self.selected_features)),
            "model_class": type(self.model).__name__,
            "calibrator_method": self.calibrator.get("method"),
            "selected_threshold": float(self.threshold),
            "reward_risk_ratio": float(self.geometry["reward_risk_ratio"]),
            "geometry_hash": str(self.geometry.get("geometry_hash")),
            "artifact_hashes": dict(self.artifact_hashes),
            "normal_path_artifact_reads": False,
        }
