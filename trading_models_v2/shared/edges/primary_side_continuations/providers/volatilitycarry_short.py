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

VOLATILITYCARRY_SHORT_PRIMARY_SIGNATURE = "75aeb3a94e639b1527e63b5d76000c26bb8f714859da8450a27e8a0ddaa4dc7c"

PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilitycarry_short_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        VOLATILITYCARRY_SHORT_PRIMARY_SIGNATURE: {"side_value": -1},
    },
}

@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None

class VolatilityCarryShortPrimarySideContinuation:
    def __init__(self, *, identity, event_type: int) -> None:
        if int(identity.side_value) != -1 or int(event_type) != -1:
            raise PrimarySideContinuationError(
                "volatilitycarry_short requires side/event type -1"
            )
        self.identity = identity
        self.side_value = -1
        self.event_type = -1
        self._state = _State()

    @staticmethod
    def _ts(value: Any) -> pd.Timestamp:
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
        ts = self._ts(row["Datetime"])
        if enforce_append_only and self._state.last_timestamp is not None and ts <= self._state.last_timestamp:
            raise PrimarySideContinuationError("update_one requires increasing Datetime")
        self._state.last_timestamp = ts
        self._state.processed_rows += 1

    def bootstrap(self, features: pd.DataFrame) -> dict[str, Any]:
        frame=features[["Datetime"]].copy()
        frame["Datetime"]=to_datetime_iso(frame["Datetime"],utc=True,errors="coerce")
        frame=frame.dropna().sort_values("Datetime")
        if frame.empty:
            raise PrimarySideContinuationError("bootstrap requires Datetime")
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row,enforce_append_only=False)
        return self.state_summary()

    def update_one(self,row: Union[Mapping[str,Any],pd.Series])->dict[str,Any]:
        payload=row.to_dict() if isinstance(row,pd.Series) else dict(row)
        self._advance(payload,enforce_append_only=True)
        return {"Datetime":self._state.last_timestamp,"primary_side":-1}

    def filter_event(self,event: Optional[Mapping[str,Any]])->Optional[dict[str,Any]]:
        if event is None:
            return None
        if self._state.last_timestamp is None:
            raise PrimarySideContinuationError("provider not initialized")
        payload=dict(event)
        required=("Datetime","edge_id","side","Event_Vol","Volatility_Carry_Event_Type","Drift_TStat","Volatility_Ratio","Is_Calm_Vol")
        missing=[c for c in required if c not in payload]
        if missing:
            raise PrimarySideContinuationError(f"missing event fields: {missing}")
        if self._ts(payload["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError("event/current timestamp mismatch")
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError("edge_id mismatch")
        raw_side=int(self._finite(payload["side"],"side"))
        event_type=int(self._finite(payload["Volatility_Carry_Event_Type"],"Volatility_Carry_Event_Type"))
        drift=self._finite(payload["Drift_TStat"],"Drift_TStat")
        self._finite(payload["Volatility_Ratio"],"Volatility_Ratio")
        self._finite(payload["Event_Vol"],"Event_Vol")
        if raw_side not in {-1,1} or event_type not in {-1,1}:
            raise PrimarySideContinuationError("raw side/event type must be +/-1")
        if not bool(payload["Is_Calm_Vol"]):
            raise PrimarySideContinuationError("non-calm event reached primary")
        if event_type != -1:
            return None
        if drift >= 0:
            raise PrimarySideContinuationError("accepted short carry must have negative Drift_TStat")
        out=dict(payload)
        out["primary_side_rule"]="Volatility_Carry_Event_Type=-1 -> side=-1 short"
        out["primary_side"]=-1
        out["primary_side_filter"]=1
        out["side"]=-1
        return out

    def state_summary(self)->dict[str,Any]:
        return {
            "provider_id":PROVIDER_SPEC["provider_id"],
            "provider_version":PROVIDER_SPEC["provider_version"],
            "edge_id":self.identity.edge_id,
            "side_value":-1,
            "processed_rows":self._state.processed_rows,
            "last_timestamp":self._state.last_timestamp.isoformat() if self._state.last_timestamp is not None else None,
            "normal_path_ffd_disk_reads":False,
            "normal_path_history_disk_reads":False,
            "normal_path_full_primary_fallback":False,
        }

def create_provider(*,primary_logic_signature: str,d1_edge: Mapping[str,Any],v2_root):
    signature=str(primary_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise PrimarySideContinuationError(f"unsupported signature: {signature}")
    primary=d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError("D.1/provider primary signature mismatch")
    constants=primary.get("semantic_constants") or {}
    event_type=int(constants.get("VOLATILITY_CARRY_EVENT_TYPE",0))
    if event_type != -1:
        raise PrimarySideContinuationError("D.1 VOLATILITY_CARRY_EVENT_TYPE changed")
    identity=extract_primary_source_identity(v2_root=v2_root,d1_edge=d1_edge)
    return VolatilityCarryShortPrimarySideContinuation(identity=identity,event_type=event_type)
