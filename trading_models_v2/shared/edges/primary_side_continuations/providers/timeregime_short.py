from __future__ import annotations

"""Exact timeregime_short primary-side continuation for Phase D.4."""

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


TIMEREGIME_SHORT_PRIMARY_SIGNATURE = (
    "f543ef226396906f2110bb2f3489fe2502d15d447b2a69fc1860640015f59f51"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_timeregime_short_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        TIMEREGIME_SHORT_PRIMARY_SIGNATURE: {"side_value": -1},
    },
}

PRIMARY_SIDE_RULE = "Time_Regime_Event_Type=-1 -> side=-1 short"


@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class TimeRegimeShortPrimarySideContinuation:
    def __init__(self, *, identity, time_regime_event_type: int) -> None:
        if int(identity.side_value) != -1:
            raise PrimarySideContinuationError(
                "time-regime short primary provider requires side -1"
            )
        if int(time_regime_event_type) != -1:
            raise PrimarySideContinuationError(
                "TIME_REGIME_EVENT_TYPE must remain -1"
            )
        self.identity = identity
        self.side_value = -1
        self.time_regime_event_type = -1
        self._state = _State()

    def reset(self) -> None:
        self._state = _State()

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise PrimarySideContinuationError(
                f"invalid Datetime: {value!r}"
            )
        return pd.Timestamp(ts)

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
                "primary bootstrap requires Datetime"
            )
        frame = features[["Datetime"]].copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        frame = frame.dropna(subset=["Datetime"])
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise PrimarySideContinuationError(
                "primary bootstrap empty after normalization"
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
            "primary_side": self.side_value,
        }

    def filter_event(
        self,
        event: Optional[Mapping[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if event is None:
            return None
        if self._state.last_timestamp is None:
            raise PrimarySideContinuationError(
                "filter_event requires initialized current timestamp"
            )

        payload = dict(event)
        required = (
            "Datetime",
            "edge_id",
            "side",
            "Event_Vol",
            "Time_Regime_Event_Type",
            "Time_Bias_TStat",
            "Volatility_Ratio",
            "Time_Bucket",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise PrimarySideContinuationError(
                f"primary event missing fields: {missing}"
            )

        event_time = self._timestamp(payload["Datetime"])
        if event_time != self._state.last_timestamp:
            raise PrimarySideContinuationError(
                "candidate event timestamp does not match current primary state"
            )
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError(
                "time-regime primary event edge_id mismatch"
            )

        try:
            raw_side = int(payload["side"])
            event_type = int(payload["Time_Regime_Event_Type"])
        except Exception as exc:
            raise PrimarySideContinuationError(
                "time-regime primary side/event type is invalid"
            ) from exc

        if raw_side not in {-1, 1} or event_type not in {-1, 1}:
            raise PrimarySideContinuationError(
                "time-regime raw side/event type must be +/-1"
            )
        if str(payload["Time_Bucket"]) == "rollover":
            raise PrimarySideContinuationError(
                "rollover event reached time-regime primary mapping"
            )

        values = []
        for name in ("Time_Bias_TStat", "Volatility_Ratio", "Event_Vol"):
            value = pd.to_numeric(
                pd.Series([payload[name]]), errors="coerce"
            ).iat[0]
            if pd.isna(value) or not np.isfinite(float(value)):
                raise PrimarySideContinuationError(
                    "time-regime primary event contains non-finite controls"
                )
            values.append(float(value))
        tstat, _, _ = values

        if event_type != self.time_regime_event_type:
            return None
        if raw_side != -1:
            raise PrimarySideContinuationError(
                "accepted timeregime_short event must have raw side -1"
            )
        if tstat >= 0.0:
            raise PrimarySideContinuationError(
                "accepted timeregime_short event must have negative TStat"
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
            "side_value": self.side_value,
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


def create_provider(
    *,
    primary_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root,
) -> TimeRegimeShortPrimarySideContinuation:
    signature = str(primary_logic_signature or "").strip()
    if signature != TIMEREGIME_SHORT_PRIMARY_SIGNATURE:
        raise PrimarySideContinuationError(
            f"timeregime_short primary provider does not support {signature}"
        )

    primary = d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError(
            "D.1/provider primary signature mismatch"
        )
    constants = primary.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise PrimarySideContinuationError(
            "D.1 primary semantic_constants missing"
        )
    if int(constants.get("TIME_REGIME_EVENT_TYPE", 0)) != -1:
        raise PrimarySideContinuationError(
            "D.1 TIME_REGIME_EVENT_TYPE changed"
        )

    identity = extract_primary_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    return TimeRegimeShortPrimarySideContinuation(
        identity=identity,
        time_regime_event_type=-1,
    )
