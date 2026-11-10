from __future__ import annotations

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

from ..base import PrimarySideContinuationError, extract_primary_source_identity

RANGESTRUCTURE_SHORT_PRIMARY_SIGNATURE = "469ff1bb84a092529b356632f47d25426f7fa92f58a633855720f844365d09fc"

PROVIDER_SPEC = {
    "provider_id": "phase_d4_rangestructure_short_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        RANGESTRUCTURE_SHORT_PRIMARY_SIGNATURE: {"side_value": -1},
    },
}

@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None

class RangeStructureShortPrimarySideContinuation:
    def __init__(self, *, identity) -> None:
        if int(identity.side_value) != -1:
            raise PrimarySideContinuationError(
                "rangestructure_short primary requires side -1"
            )
        self.identity = identity
        self.side_value = -1
        self._state = _State()

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise PrimarySideContinuationError(f"invalid Datetime: {value!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _finite(value: Any, name: str) -> float:
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise PrimarySideContinuationError(f"invalid {name}: {value!r}")
        return float(x)

    def reset(self) -> None:
        self._state = _State()

    def _advance(self, row: Mapping[str, Any], *, enforce_append_only: bool) -> None:
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
        frame = features[["Datetime"]].copy()
        frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
        frame = frame.dropna().sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise PrimarySideContinuationError("bootstrap requires Datetime")
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(self, row: Union[Mapping[str, Any], pd.Series]) -> dict[str, Any]:
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        self._advance(payload, enforce_append_only=True)
        return {"Datetime": self._state.last_timestamp, "primary_side": -1}

    def filter_event(self, event: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
        if event is None:
            return None
        payload = dict(event)
        required = (
            "Datetime", "edge_id", "side", "Event_Vol",
            "Range_Structure_Event_Type", "Structure_Bias_TStat",
        )
        missing = [name for name in required if name not in payload]
        if missing:
            raise PrimarySideContinuationError(
                f"range-structure-short primary missing fields: {missing}"
            )
        if self._state.last_timestamp is None:
            raise PrimarySideContinuationError("primary provider not initialized")
        if self._timestamp(payload["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError("event/current timestamp mismatch")
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError("edge_id mismatch")

        raw_side = int(self._finite(payload["side"], "side"))
        event_type = int(
            self._finite(
                payload["Range_Structure_Event_Type"],
                "Range_Structure_Event_Type",
            )
        )
        tstat = self._finite(payload["Structure_Bias_TStat"], "Structure_Bias_TStat")
        self._finite(payload["Event_Vol"], "Event_Vol")
        if raw_side not in {-1, 1} or event_type not in {-1, 1}:
            raise PrimarySideContinuationError(
                "range-structure raw side/event type must be +/-1"
            )
        if tstat >= 0:
            return None

        out = dict(payload)
        out["raw_side_from_structure_bias"] = int(np.sign(tstat))
        out["primary_side_rule"] = "Structure_Bias_TStat < 0 -> side=-1 short"
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
            "processed_rows": self._state.processed_rows,
            "last_timestamp": (
                self._state.last_timestamp.isoformat()
                if self._state.last_timestamp is not None else None
            ),
            "normal_path_ffd_disk_reads": False,
            "normal_path_history_disk_reads": False,
            "normal_path_full_primary_fallback": False,
        }

def create_provider(*, primary_logic_signature: str, d1_edge: Mapping[str, Any], v2_root):
    signature = str(primary_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise PrimarySideContinuationError(
            f"range-structure-short primary does not support {signature}"
        )
    primary = d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError("D.1/provider primary signature mismatch")
    identity = extract_primary_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return RangeStructureShortPrimarySideContinuation(identity=identity)
