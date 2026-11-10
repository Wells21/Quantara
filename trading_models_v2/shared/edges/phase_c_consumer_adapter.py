"""Phase C.3 consumer adapters.

No feature math lives here. C.2 remains the mathematical owner.

This module has two narrow responsibilities:
1. project source-certified private candidate controls from C.2's bounded
   internal ``feature_tail``; and
2. enforce one-way ownership at the candidate->model-row boundary by removing
   duplicate selected model-feature columns from candidate-event context after
   candidate detection, before the unchanged edge row builder runs.

Candidate detection is therefore unchanged. Event metadata/context is preserved.
Only duplicate model-input columns are removed so the certified canonical
feature table is the sole source of selected model values.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from shared.edges.phase_c_consumer_contract import (
    PRIVATE_CANDIDATE_CONTROL_COLUMNS,
    PhaseCConsumerContractError,
)


FORBIDDEN_EVENT_MODEL_OWNERSHIP_COLUMNS = {
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
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "approved_signal",
}


def candidate_control_columns(edge_contract: Mapping[str, Any]) -> list[str]:
    values = [str(x) for x in edge_contract.get("candidate_control_columns") or []]
    unknown = sorted(set(values).difference(PRIVATE_CANDIDATE_CONTROL_COLUMNS))
    if unknown:
        raise PhaseCConsumerContractError(
            f"edge contract requests uncertified private candidate controls: {unknown}"
        )
    if len(values) != len(set(values)):
        raise PhaseCConsumerContractError(
            "edge contract contains duplicate private candidate controls"
        )
    return values


def latest_candidate_control_values(
    *,
    state: Mapping[str, Any],
    edge_contract: Mapping[str, Any],
) -> dict[str, float]:
    """Read latest private controls from C.2 bounded internal state.

    The adapter performs no recomputation and no dependency I/O.
    """
    controls = candidate_control_columns(edge_contract)
    if not controls:
        return {}
    feature_tail = state.get("feature_tail")
    if not isinstance(feature_tail, pd.DataFrame) or feature_tail.empty:
        raise PhaseCConsumerContractError(
            "C.2 feature state has no bounded feature_tail for candidate controls"
        )
    latest = feature_tail.iloc[-1]
    missing = [name for name in controls if name not in latest.index]
    if missing:
        raise PhaseCConsumerContractError(
            f"C.2 internal feature row missing candidate controls: {missing}"
        )
    output: dict[str, float] = {}
    nonfinite: list[str] = []
    for name in controls:
        try:
            value = float(latest[name])
        except Exception:
            nonfinite.append(name)
            continue
        if not np.isfinite(value):
            nonfinite.append(name)
        output[name] = value
    if nonfinite:
        raise PhaseCConsumerContractError(
            f"C.2 internal candidate controls are non-finite: {nonfinite}"
        )
    return output


def merge_candidate_controls(
    *,
    public_feature_row: Mapping[str, Any],
    state: Mapping[str, Any],
    edge_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a consumer row with private controls added without overriding public features."""
    row = dict(public_feature_row)
    controls = latest_candidate_control_values(
        state=state,
        edge_contract=edge_contract,
    )
    overlap = sorted(set(row).intersection(controls))
    if overlap:
        raise PhaseCConsumerContractError(
            f"private candidate controls unexpectedly overlap public projection: {overlap}"
        )
    row.update(controls)
    return row


def strip_selected_feature_collisions(
    *,
    events: pd.DataFrame,
    selected_features: Sequence[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Remove duplicate selected model inputs from event context.

    This runs only *after* candidate events have already been generated. It does
    not alter event detection, event timestamps, sides, private controls, or
    event-specific metadata. It only prevents the event frame from acting as a
    second owner of model inputs that are guaranteed by C.1 to exist in the
    canonical feature table.
    """
    if not isinstance(events, pd.DataFrame) or events.empty:
        raise PhaseCConsumerContractError(
            "candidate event table is empty/invalid"
        )
    selected = [str(x) for x in selected_features]
    if len(selected) != len(set(selected)):
        raise PhaseCConsumerContractError(
            "selected model feature list contains duplicates"
        )
    forbidden = sorted(
        FORBIDDEN_EVENT_MODEL_OWNERSHIP_COLUMNS.intersection(events.columns)
    )
    if forbidden:
        raise PhaseCConsumerContractError(
            f"candidate events contain forbidden future/scoring columns: {forbidden}"
        )
    collisions = sorted(set(selected).intersection(events.columns))
    sanitized = events.drop(columns=collisions, errors="ignore").copy()
    if "Datetime" not in sanitized.columns:
        raise PhaseCConsumerContractError(
            "candidate event table lost required Datetime identity"
        )
    return sanitized, {
        "selected_feature_collision_count": len(collisions),
        "selected_feature_collision_columns": collisions,
        "selected_feature_source_policy": "canonical_feature_table_only",
        "collision_policy": "strip_after_candidate_detection_before_live_model_row_build",
    }


def build_canonical_live_candidate_rows(
    *,
    runtime: Any,
    features: pd.DataFrame,
    events: pd.DataFrame,
    selected_features: Sequence[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build edge live rows while forcing selected model inputs to canonical features.

    The edge's existing row builder remains authoritative for event alignment and
    edge-specific context. We only sanitize duplicate selected-feature columns
    from the already-generated event frame first.
    """
    if not isinstance(features, pd.DataFrame) or features.empty:
        raise PhaseCConsumerContractError(
            "canonical feature table is empty/invalid"
        )
    selected = [str(x) for x in selected_features]
    missing = [name for name in selected if name not in features.columns]
    if missing:
        raise PhaseCConsumerContractError(
            f"canonical feature table missing selected model inputs: {missing}"
        )
    builder = getattr(runtime, "_build_live_candidate_model_rows", None)
    if not callable(builder):
        raise PhaseCConsumerContractError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: missing live candidate row builder"
        )

    sanitized_events, ownership = strip_selected_feature_collisions(
        events=events,
        selected_features=selected,
    )
    rows, edge_manifest = builder(features, sanitized_events, selected)
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        raise PhaseCConsumerContractError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: edge live row builder returned no rows"
        )
    missing_rows = [name for name in selected if name not in rows.columns]
    if missing_rows:
        raise PhaseCConsumerContractError(
            f"live candidate rows missing selected model inputs: {missing_rows}"
        )

    manifest = {
        "adapter": "phase_c3_canonical_model_input_ownership_v1_2",
        **ownership,
        "edge_row_builder": (edge_manifest or {}).get("row_builder"),
        "edge_row_manifest": dict(edge_manifest or {}),
        "rows": int(len(rows)),
    }
    return rows, manifest


__all__ = [
    "candidate_control_columns",
    "latest_candidate_control_values",
    "merge_candidate_controls",
    "strip_selected_feature_collisions",
    "build_canonical_live_candidate_rows",
]
