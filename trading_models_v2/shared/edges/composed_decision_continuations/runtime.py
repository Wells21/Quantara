from __future__ import annotations

"""Inert alpha-agnostic Phase D candidate -> row -> resident scoring path."""

from pathlib import Path
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.primary_side_continuations import create_primary_side_provider

from .base import (
    ComposedDecisionContinuationError,
    RuntimeCompositionIdentity,
    extract_runtime_composition_identity,
)
from .resident import ResidentEdgeScorer


FORBIDDEN_LIVE_COLUMNS = frozenset(
    {
        "meta_label", "label", "target", "t1", "barrier_hit",
        "resolution_source", "entry_price", "pt_price", "sl_price",
        "realized_r", "raw_probability", "calibrated_probability",
        "reward_risk_ratio", "break_even_probability",
        "afml_economic_strength", "afml_effective_probability",
        "afml_bet_size", "afml_sizing_method", "selected_threshold",
        "threshold_probability_column", "approved_signal", "probability_flow",
        "model_artifact_hash", "calibrator_artifact_hash",
        "threshold_artifact_hash", "selected_features_hash",
        "model_input_schema_hash", "score_timestamp", "signal_id",
    }
)



def _timestamp(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def build_current_live_model_row(
    *,
    current_feature: pd.Series,
    composed_event: Mapping[str, Any],
    selected_features: list[str],
) -> pd.DataFrame:
    """Build one alpha-agnostic row from live event context + current canonical data."""
    if not isinstance(current_feature, pd.Series):
        raise ComposedDecisionContinuationError(
            "current_feature must be a pandas Series"
        )

    event = dict(composed_event)
    if "Datetime" not in event or "Datetime" not in current_feature.index:
        raise ComposedDecisionContinuationError(
            "Current feature/event Datetime is required"
        )

    forbidden_in_event = sorted(FORBIDDEN_LIVE_COLUMNS.intersection(event))
    if forbidden_in_event:
        raise ComposedDecisionContinuationError(
            f"Accepted event contains forbidden research/scoring columns: "
            f"{forbidden_in_event}"
        )

    event_time = _timestamp(event["Datetime"])
    feature_time = _timestamp(current_feature["Datetime"])
    if event_time != feature_time:
        raise ComposedDecisionContinuationError(
            "Current feature/event Datetime mismatch"
        )

    # Preserve every live-safe event field. No CUSUM/absorption/range/etc schema.
    payload: dict[str, Any] = dict(event)
    payload["Datetime"] = event_time

    missing: list[str] = []
    for feature in selected_features:
        if feature in payload:
            continue
        if feature in current_feature.index:
            payload[feature] = current_feature[feature]
        else:
            missing.append(feature)

    if missing:
        raise ComposedDecisionContinuationError(
            f"Selected features missing from accepted event/current canonical row: "
            f"{missing[:30]}"
        )

    row = pd.DataFrame([payload])
    forbidden = sorted(FORBIDDEN_LIVE_COLUMNS.intersection(row.columns))
    if forbidden:
        raise ComposedDecisionContinuationError(
            f"Live current model row contains forbidden columns: {forbidden}"
        )

    for feature in selected_features:
        row[feature] = pd.to_numeric(row[feature], errors="coerce")
    row = row.replace([np.inf, -np.inf], np.nan)
    row = row.dropna(subset=selected_features).reset_index(drop=True)
    if len(row) != 1:
        raise ComposedDecisionContinuationError(
            "Current model row dropped after selected-feature numeric/NaN filtering"
        )
    return row


class ComposedDecisionContinuation:
    """Stateful inert D4.2D decision path for one exact edge stream."""

    def __init__(
        self,
        *,
        edge_id: str,
        identity: RuntimeCompositionIdentity,
        candidate_provider: Any,
        primary_provider: Any,
        resident_scorer: Any,
    ) -> None:
        self.edge_id = str(edge_id)
        self.identity = identity
        self.candidate_provider = candidate_provider
        self.primary_provider = primary_provider
        self.resident_scorer = resident_scorer
        self.selected_features = list(resident_scorer.selected_features)

        if self.identity.edge_id != self.edge_id:
            raise ComposedDecisionContinuationError(
                "Runtime composition identity edge_id mismatch"
            )
        if str(resident_scorer.edge_id) != self.edge_id:
            raise ComposedDecisionContinuationError(
                "Resident scorer edge_id mismatch"
            )

        self.processed_rows = 0
        self.raw_candidate_count = 0
        self.primary_accepted_count = 0
        self.primary_rejected_count = 0
        self.model_row_call_count = 0
        self.score_call_count = 0
        self.last_timestamp: Optional[pd.Timestamp] = None

    def bootstrap(self, candidate_projection: pd.DataFrame) -> None:
        if candidate_projection is None or candidate_projection.empty:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: bootstrap projection is empty"
            )
        self.candidate_provider.bootstrap(candidate_projection.copy())
        self.primary_provider.bootstrap(candidate_projection.copy())
        last = _timestamp(candidate_projection.iloc[-1]["Datetime"])
        self.last_timestamp = last
        self.processed_rows = int(len(candidate_projection))

    def update_one(
        self,
        *,
        candidate_row: pd.Series,
        current_canonical_row: pd.Series,
    ) -> Optional[pd.DataFrame]:
        """Advance all state every bar; build/score only after both gates accept."""

        if not isinstance(candidate_row, pd.Series):
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: candidate_row must be a Series"
            )
        if not isinstance(current_canonical_row, pd.Series):
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: current_canonical_row must be a Series"
            )

        candidate_ts = _timestamp(candidate_row["Datetime"])
        canonical_ts = _timestamp(current_canonical_row["Datetime"])
        if candidate_ts != canonical_ts:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: candidate/canonical current timestamp mismatch"
            )
        if self.last_timestamp is not None and candidate_ts <= self.last_timestamp:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: non-monotonic current timestamp"
            )

        raw_event = self.candidate_provider.update_one(candidate_row)
        self.primary_provider.update_one(candidate_row)

        self.processed_rows += 1
        self.last_timestamp = candidate_ts

        if raw_event is None:
            return None

        self.raw_candidate_count += 1
        accepted_event = self.primary_provider.filter_event(raw_event)
        if accepted_event is None:
            self.primary_rejected_count += 1
            return None

        self.primary_accepted_count += 1
        composed_event = dict(accepted_event)
        composed_event["candidate_event_generator"] = (
            self.identity.composed_candidate_event_generator
        )

        model_row = build_current_live_model_row(
            current_feature=current_canonical_row,
            composed_event=composed_event,
            selected_features=self.selected_features,
        )
        self.model_row_call_count += 1

        scored = self.resident_scorer.score_current(model_row)
        self.score_call_count += 1

        if len(scored) != 1:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: resident scorer did not return exactly one row"
            )
        if "Datetime" not in scored.columns or "selected_threshold" not in scored.columns:
            raise ComposedDecisionContinuationError(
                f"{self.edge_id}: scored row lacks signal_id inputs"
            )

        scored["signal_id"] = [
            f"{self.edge_id}|{pd.Timestamp(ts).isoformat()}|thr_{float(thr):.4f}"
            for ts, thr in zip(scored["Datetime"], scored["selected_threshold"])
        ]
        return scored.reset_index(drop=True)

    def state_summary(self) -> dict[str, Any]:
        candidate_summary = (
            self.candidate_provider.state_summary()
            if callable(getattr(self.candidate_provider, "state_summary", None))
            else {}
        )
        primary_summary = (
            self.primary_provider.state_summary()
            if callable(getattr(self.primary_provider, "state_summary", None))
            else {}
        )
        resident_summary = (
            self.resident_scorer.state_summary()
            if callable(getattr(self.resident_scorer, "state_summary", None))
            else {}
        )
        return {
            "edge_id": self.edge_id,
            "processed_rows": int(self.processed_rows),
            "last_timestamp": (
                self.last_timestamp.isoformat()
                if self.last_timestamp is not None
                else None
            ),
            "raw_candidate_count": int(self.raw_candidate_count),
            "primary_accepted_count": int(self.primary_accepted_count),
            "primary_rejected_count": int(self.primary_rejected_count),
            "model_row_call_count": int(self.model_row_call_count),
            "score_call_count": int(self.score_call_count),
            "runtime_composed_candidate_event_generator": (
                self.identity.composed_candidate_event_generator
            ),
            "candidate_provider": candidate_summary,
            "primary_provider": primary_summary,
            "resident_scorer": resident_summary,
            "normal_path_runtime_generate_candidate_events_calls": 0,
            "normal_path_runtime_live_model_row_builder_calls": 0,
            "normal_path_artifact_reads": False,
            "production_routing_enabled": False,
            "activation": "NONE",
        }


def create_composed_decision_continuation(
    *,
    d1_edge: Mapping[str, Any],
    manifest_edge: Mapping[str, Any],
    v2_root: Union[str, Path],
) -> ComposedDecisionContinuation:
    """Create one inert composed continuation from current D.1/manifest identity."""

    root = Path(v2_root).resolve()
    edge_id = str(d1_edge.get("edge_id") or "").strip()
    manifest_edge_id = str(manifest_edge.get("edge_id") or "").strip()
    if not edge_id or edge_id != manifest_edge_id:
        raise ComposedDecisionContinuationError(
            f"D.1/manifest edge identity mismatch: {edge_id!r} vs {manifest_edge_id!r}"
        )

    candidate = d1_edge.get("candidate") or {}
    primary = d1_edge.get("primary_side") or {}
    candidate_signature = str(
        candidate.get("candidate_logic_signature") or ""
    ).strip()
    primary_signature = str(
        primary.get("primary_logic_signature") or ""
    ).strip()
    if not candidate_signature:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: candidate_logic_signature missing"
        )
    if not primary_signature:
        raise ComposedDecisionContinuationError(
            f"{edge_id}: primary_logic_signature missing"
        )

    identity = extract_runtime_composition_identity(
        v2_root=root,
        d1_edge=d1_edge,
    )
    edge_root = (root / identity.runtime_source_path).resolve().parent

    candidate_provider = create_candidate_provider(
        candidate_signature,
        d1_edge=d1_edge,
        v2_root=str(root),
    )
    primary_provider = create_primary_side_provider(
        primary_signature,
        d1_edge=d1_edge,
        v2_root=str(root),
    )
    resident_scorer = ResidentEdgeScorer(edge_root)

    return ComposedDecisionContinuation(
        edge_id=edge_id,
        identity=identity,
        candidate_provider=candidate_provider,
        primary_provider=primary_provider,
        resident_scorer=resident_scorer,
    )
