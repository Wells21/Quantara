from __future__ import annotations

"""Exact drawupreversalhazard_short primary-side continuation for Phase D.4."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[4]))
    from shared.io.timeparse import to_datetime_iso

from ..base import (
    PrimarySideContinuationError,
    extract_primary_source_identity,
)


DRAWUP_REVERSAL_HAZARD_SHORT_PRIMARY_SIGNATURE = (
    "bf383b5a816848405f7792eebc84ad238a911406c4908eab24586e4cf4ccf3e2"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_drawupreversalhazard_short_primary",
    "provider_version": "1",
    "supported_signatures": {
        DRAWUP_REVERSAL_HAZARD_SHORT_PRIMARY_SIGNATURE: {
            "side_value": -1
        },
    },
}

PRIMARY_SIDE_RULE = (
    "negative completed-bar impulses after a sufficiently severe and positively "
    "concentrated drawup has begun a limited pullback, with a causal resolved-history "
    "reversal hazard above threshold, map to side=-1 short; both initial state entries "
    "and spaced renewed impulses are valid"
)


@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class DrawupReversalHazardShortPrimarySideContinuation:
    def __init__(
        self,
        *,
        identity,
        min_drawup_severity_percentile: float,
        min_positive_tail_concentration_percentile: float,
        min_pullback_fraction: float,
        max_pullback_fraction: float,
        min_reversal_hazard_probability: float,
        min_hazard_cell_observations: int,
    ) -> None:
        if int(identity.side_value) != -1:
            raise PrimarySideContinuationError(
                "drawupreversalhazard_short primary requires side -1"
            )
        self.identity = identity
        self.side_value = -1
        self.min_drawup_severity_percentile = float(
            min_drawup_severity_percentile
        )
        self.min_positive_tail_concentration_percentile = float(
            min_positive_tail_concentration_percentile
        )
        self.min_pullback_fraction = float(min_pullback_fraction)
        self.max_pullback_fraction = float(max_pullback_fraction)
        self.min_reversal_hazard_probability = float(
            min_reversal_hazard_probability
        )
        self.min_hazard_cell_observations = int(
            min_hazard_cell_observations
        )
        self._state = _State()

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise PrimarySideContinuationError(
                f"invalid Datetime: {value!r}"
            )
        return pd.Timestamp(ts)

    @staticmethod
    def _number(value: Any, name: str) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(result) or not np.isfinite(float(result)):
            raise PrimarySideContinuationError(
                f"invalid {name}: {value!r}"
            )
        return float(result)

    def reset(self) -> None:
        self._state = _State()

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> None:
        if "Datetime" not in row:
            raise PrimarySideContinuationError(
                "primary update missing Datetime"
            )
        timestamp = self._timestamp(row["Datetime"])
        if (
            enforce_append_only
            and self._state.last_timestamp is not None
            and timestamp <= self._state.last_timestamp
        ):
            raise PrimarySideContinuationError(
                "update_one requires strictly increasing Datetime"
            )
        self._state.last_timestamp = timestamp
        self._state.processed_rows += 1

    def bootstrap(self, features: pd.DataFrame) -> dict[str, Any]:
        if (
            not isinstance(features, pd.DataFrame)
            or features.empty
            or "Datetime" not in features.columns
        ):
            raise PrimarySideContinuationError(
                "bootstrap requires nonempty Datetime"
            )
        frame = features[["Datetime"]].copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        frame = frame.dropna(subset=["Datetime"])
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise PrimarySideContinuationError(
                "bootstrap empty after normalization"
            )
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(
        self,
        row: Union[Mapping[str, Any], pd.Series],
    ) -> dict[str, Any]:
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        self._advance(payload, enforce_append_only=True)
        return {
            "Datetime": self._state.last_timestamp,
            "primary_side": -1,
        }

    def filter_event(
        self,
        event: Optional[Mapping[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if event is None:
            return None
        if self._state.last_timestamp is None:
            raise PrimarySideContinuationError(
                "filter_event requires initialized state"
            )

        payload = dict(event)
        required = (
            "Datetime",
            "edge_id",
            "side",
            "Event_Vol",
            "Drawup_Reversal_Hazard_Event",
            "Drawup_Reversal_Hazard_Active_State",
            "Drawup_Reversal_Hazard_State_Entry",
            "Drawup_Reversal_Hazard_Renewed_Impulse",
            "Negative_Event_Bar",
            "Drawup_Active",
            "Log_Return_1",
            "Drawup_Severity_Percentile_100_Over_1000",
            "Positive_Tail_Concentration_Percentile_20_Over_500",
            "Pullback_Fraction_Of_Drawup",
            "Drawup_Reversal_Hazard_Probability",
            "Drawup_Reversal_Hazard_Cell_Count",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise PrimarySideContinuationError(
                f"drawup hazard primary event missing fields: {missing}"
            )

        if self._timestamp(payload["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError(
                "candidate event timestamp does not match primary state"
            )
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError(
                "drawup hazard primary edge_id mismatch"
            )

        side = int(self._number(payload["side"], "side"))
        if side != 0:
            raise PrimarySideContinuationError(
                "drawup hazard raw candidate side must remain zero"
            )

        event_flag = int(
            self._number(
                payload["Drawup_Reversal_Hazard_Event"],
                "Drawup_Reversal_Hazard_Event",
            )
        )
        active = int(
            self._number(
                payload["Drawup_Reversal_Hazard_Active_State"],
                "Drawup_Reversal_Hazard_Active_State",
            )
        )
        state_entry = int(
            self._number(
                payload["Drawup_Reversal_Hazard_State_Entry"],
                "Drawup_Reversal_Hazard_State_Entry",
            )
        )
        renewed = int(
            self._number(
                payload["Drawup_Reversal_Hazard_Renewed_Impulse"],
                "Drawup_Reversal_Hazard_Renewed_Impulse",
            )
        )
        negative = int(
            self._number(payload["Negative_Event_Bar"], "Negative_Event_Bar")
        )
        drawup_active = int(
            self._number(payload["Drawup_Active"], "Drawup_Active")
        )

        if event_flag != 1 or active != 1 or negative != 1 or drawup_active != 1:
            raise PrimarySideContinuationError(
                "drawup hazard event-state contract failed"
            )
        if state_entry not in {0, 1} or renewed not in {0, 1}:
            raise PrimarySideContinuationError(
                "drawup hazard entry/renewed flags must be binary"
            )
        if state_entry + renewed != 1:
            raise PrimarySideContinuationError(
                "drawup hazard event must be exactly entry or renewed"
            )

        event_vol = self._number(payload["Event_Vol"], "Event_Vol")
        log_return = self._number(payload["Log_Return_1"], "Log_Return_1")
        severity = self._number(
            payload["Drawup_Severity_Percentile_100_Over_1000"],
            "Drawup_Severity_Percentile_100_Over_1000",
        )
        concentration = self._number(
            payload[
                "Positive_Tail_Concentration_Percentile_20_Over_500"
            ],
            "Positive_Tail_Concentration_Percentile_20_Over_500",
        )
        pullback = self._number(
            payload["Pullback_Fraction_Of_Drawup"],
            "Pullback_Fraction_Of_Drawup",
        )
        probability = self._number(
            payload["Drawup_Reversal_Hazard_Probability"],
            "Drawup_Reversal_Hazard_Probability",
        )
        cell_count = self._number(
            payload["Drawup_Reversal_Hazard_Cell_Count"],
            "Drawup_Reversal_Hazard_Cell_Count",
        )

        if event_vol <= 0.0:
            raise PrimarySideContinuationError(
                "drawup hazard Event_Vol must be positive"
            )
        if log_return >= 0.0:
            raise PrimarySideContinuationError(
                "drawup hazard accepted impulse must have negative return"
            )
        if severity < self.min_drawup_severity_percentile:
            raise PrimarySideContinuationError(
                "drawup hazard severity contract failed"
            )
        if (
            concentration
            < self.min_positive_tail_concentration_percentile
        ):
            raise PrimarySideContinuationError(
                "drawup hazard concentration contract failed"
            )
        if not (
            self.min_pullback_fraction
            <= pullback
            <= self.max_pullback_fraction
        ):
            raise PrimarySideContinuationError(
                "drawup hazard pullback contract failed"
            )
        if probability < self.min_reversal_hazard_probability:
            raise PrimarySideContinuationError(
                "drawup hazard probability contract failed"
            )
        if cell_count < self.min_hazard_cell_observations:
            raise PrimarySideContinuationError(
                "drawup hazard cell-count contract failed"
            )

        out = dict(payload)
        out["primary_side_rule"] = PRIMARY_SIDE_RULE
        out["primary_side"] = -1
        out["primary_side_filter"] = 1
        out["side"] = -1
        return out

    def state_summary(self) -> dict[str, Any]:
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "side_value": -1,
            "processed_rows": int(self._state.processed_rows),
            "last_timestamp": (
                self._state.last_timestamp.isoformat()
                if self._state.last_timestamp is not None
                else None
            ),
            "normal_path_ffd_disk_reads": False,
            "normal_path_history_disk_reads": False,
            "normal_path_full_primary_fallback": False,
        }


def _candidate_constant(
    d1_edge: Mapping[str, Any],
    name: str,
) -> Any:
    candidate = d1_edge.get("candidate") or {}
    constants = candidate.get("semantic_constants") or {}
    if not isinstance(constants, Mapping) or name not in constants:
        raise PrimarySideContinuationError(
            f"drawup hazard D.1 candidate constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    primary_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> DrawupReversalHazardShortPrimarySideContinuation:
    signature = str(primary_logic_signature or "").strip()
    if signature != DRAWUP_REVERSAL_HAZARD_SHORT_PRIMARY_SIGNATURE:
        raise PrimarySideContinuationError(
            f"drawup hazard primary provider does not support {signature}"
        )

    primary = d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError(
            "D.1/provider primary signature mismatch"
        )

    identity = extract_primary_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )

    return DrawupReversalHazardShortPrimarySideContinuation(
        identity=identity,
        min_drawup_severity_percentile=float(
            _candidate_constant(
                d1_edge,
                "MIN_DRAWUP_SEVERITY_PERCENTILE",
            )
        ),
        min_positive_tail_concentration_percentile=float(
            _candidate_constant(
                d1_edge,
                "MIN_POSITIVE_TAIL_CONCENTRATION_PERCENTILE",
            )
        ),
        min_pullback_fraction=float(
            _candidate_constant(d1_edge, "MIN_PULLBACK_FRACTION")
        ),
        max_pullback_fraction=float(
            _candidate_constant(d1_edge, "MAX_PULLBACK_FRACTION")
        ),
        min_reversal_hazard_probability=float(
            _candidate_constant(
                d1_edge,
                "MIN_REVERSAL_HAZARD_PROBABILITY",
            )
        ),
        min_hazard_cell_observations=int(
            _candidate_constant(
                d1_edge,
                "MIN_HAZARD_CELL_OBSERVATIONS",
            )
        ),
    )
