from __future__ import annotations

"""Exact absorption_long primary-side continuation for Phase D."""

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


ABSORPTION_LONG_PRIMARY_SIGNATURE = "23db34cc4ad5b85a4a138d5ae37ac942e8e798dbc5e49c3e109d054c28af8210"

PROVIDER_SPEC = {
    "provider_id": "phase_d4_absorption_long_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        ABSORPTION_LONG_PRIMARY_SIGNATURE: {"side_value": 1},
    },
}


@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class AbsorptionLongPrimarySideContinuation:
    def __init__(
        self,
        *,
        identity,
        absorption_event_type: int,
    ) -> None:
        if int(identity.side_value) != 1:
            raise PrimarySideContinuationError(
                "absorption_long primary requires side +1"
            )
        if int(absorption_event_type) != -1:
            raise PrimarySideContinuationError(
                "absorption_long must map event type -1"
            )
        self.identity = identity
        self.side_value = 1
        self.absorption_event_type = -1
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
    def _finite(value: Any, name: str) -> float:
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
                "filter_event requires initialized state"
            )

        payload = dict(event)
        required = (
            "Datetime",
            "edge_id",
            "side",
            "Absorption_Event_Type",
            "Event_Vol",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise PrimarySideContinuationError(
                f"absorption primary event missing fields: {missing}"
            )

        if self._timestamp(payload["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError(
                "candidate event timestamp does not match primary state"
            )
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError(
                "absorption primary event edge_id mismatch"
            )

        side = int(self._finite(payload["side"], "side"))
        event_type = int(
            self._finite(
                payload["Absorption_Event_Type"],
                "Absorption_Event_Type",
            )
        )
        self._finite(payload["Event_Vol"], "Event_Vol")

        if side != 1:
            raise PrimarySideContinuationError(
                "absorption_long raw candidate must already have side +1"
            )
        if event_type != self.absorption_event_type:
            return None

        out = dict(payload)
        out["primary_side_rule"] = (
            "Absorption_Event_Type=-1 -> side=+1 long"
        )
        out["primary_side"] = 1
        out["primary_side_filter"] = 1
        out["side"] = 1
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
) -> AbsorptionLongPrimarySideContinuation:
    signature = str(primary_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise PrimarySideContinuationError(
            f"absorption primary provider does not support {signature}"
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
    if int(constants.get("ABSORPTION_EVENT_TYPE", 0)) != -1:
        raise PrimarySideContinuationError(
            "D.1 ABSORPTION_EVENT_TYPE changed"
        )

    identity = extract_primary_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    return AbsorptionLongPrimarySideContinuation(
        identity=identity,
        absorption_event_type=-1,
    )
