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

VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE = "8c870e16fe2e1818b52f1672f8237c28872db77d8e000adfc70233a5cf0de26f"
PROVIDER_SPEC = {
    "provider_id": "phase_d4_volatilityregime_long_primary_side",
    "provider_version": "1",
    "supported_signatures": {VOLATILITYREGIME_LONG_PRIMARY_SIGNATURE: {}},
}
PRIMARY_SIDE_RULE = "Vol_Regime_Event=1 bullish expansion candle -> side=+1 long"

@dataclass
class _State:
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None

class VolatilityRegimeLongPrimarySideContinuation:
    def __init__(self, *, identity, min_body_share, long_close_location):
        self.identity = identity
        self.side_value = 1
        self.min_body_share = float(min_body_share)
        self.long_close_location = float(long_close_location)
        self._state = _State()

    @staticmethod
    def _ts(value):
        ts = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(ts):
            raise PrimarySideContinuationError(f"invalid Datetime: {value!r}")
        return pd.Timestamp(ts)

    @staticmethod
    def _finite(value, name):
        x = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(x) or not np.isfinite(float(x)):
            raise PrimarySideContinuationError(f"invalid {name}: {value!r}")
        return float(x)

    def reset(self):
        self._state = _State()

    def _advance(self, row, *, enforce_append_only):
        ts = self._ts(row["Datetime"])
        if enforce_append_only and self._state.last_timestamp is not None and ts <= self._state.last_timestamp:
            raise PrimarySideContinuationError("update_one requires increasing Datetime")
        self._state.last_timestamp = ts
        self._state.processed_rows += 1

    def bootstrap(self, features):
        if not isinstance(features, pd.DataFrame) or features.empty or "Datetime" not in features.columns:
            raise PrimarySideContinuationError("bootstrap requires Datetime")
        frame = features[["Datetime"]].copy()
        frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
        frame = frame.dropna().sort_values("Datetime").reset_index(drop=True)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(self, row: Union[Mapping[str, Any], pd.Series]):
        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        self._advance(payload, enforce_append_only=True)
        return {"Datetime": self._state.last_timestamp, "primary_side": 1}

    def filter_event(self, event):
        if event is None:
            return None
        if self._state.last_timestamp is None:
            raise PrimarySideContinuationError("provider not initialized")
        payload = dict(event)
        required = (
            "Datetime","edge_id","side","Event_Vol","Vol_Regime_Event",
            "Current_Vol_Ratio","Was_Compressed","Vol_Expansion",
            "Body_Direction","Body_Share","Close_Location",
        )
        missing = [c for c in required if c not in payload]
        if missing:
            raise PrimarySideContinuationError(f"missing event fields: {missing}")
        if self._ts(payload["Datetime"]) != self._state.last_timestamp:
            raise PrimarySideContinuationError("event/current timestamp mismatch")
        if str(payload["edge_id"]) != self.identity.edge_id:
            raise PrimarySideContinuationError("edge_id mismatch")
        side = int(self._finite(payload["side"], "side"))
        vol_event = int(self._finite(payload["Vol_Regime_Event"], "Vol_Regime_Event"))
        compressed = int(self._finite(payload["Was_Compressed"], "Was_Compressed"))
        expansion = int(self._finite(payload["Vol_Expansion"], "Vol_Expansion"))
        self._finite(payload["Current_Vol_Ratio"], "Current_Vol_Ratio")
        self._finite(payload["Event_Vol"], "Event_Vol")
        body_direction = self._finite(payload["Body_Direction"], "Body_Direction")
        body_share = self._finite(payload["Body_Share"], "Body_Share")
        close_location = self._finite(payload["Close_Location"], "Close_Location")
        if side != 0 or vol_event != 1 or compressed != 1 or expansion != 1:
            raise PrimarySideContinuationError("invalid raw volatility-regime event")
        if not (
            body_direction > 0
            and body_share >= self.min_body_share
            and close_location >= self.long_close_location
        ):
            return None
        out = dict(payload)
        out["primary_side_rule"] = PRIMARY_SIDE_RULE
        out["primary_side"] = 1
        out["primary_side_filter"] = 1
        out["side"] = 1
        out["Volatility_Regime_Event_Type"] = 1
        return out

    def state_summary(self):
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "side_value": 1,
            "processed_rows": self._state.processed_rows,
            "last_timestamp": self._state.last_timestamp.isoformat() if self._state.last_timestamp is not None else None,
            "normal_path_ffd_disk_reads": False,
            "normal_path_history_disk_reads": False,
            "normal_path_full_primary_fallback": False,
        }

def create_provider(*, primary_logic_signature, d1_edge, v2_root):
    signature = str(primary_logic_signature or "").strip()
    if signature not in PROVIDER_SPEC["supported_signatures"]:
        raise PrimarySideContinuationError(f"unsupported signature: {signature}")
    primary = d1_edge.get("primary_side") or {}
    if str(primary.get("primary_logic_signature") or "") != signature:
        raise PrimarySideContinuationError("D.1/provider signature mismatch")
    constants = primary.get("semantic_constants") or {}
    if "MIN_BODY_SHARE" not in constants or "LONG_CLOSE_LOCATION" not in constants:
        raise PrimarySideContinuationError("missing D.1 primary constants")
    identity = extract_primary_source_identity(v2_root=v2_root, d1_edge=d1_edge)
    return VolatilityRegimeLongPrimarySideContinuation(
        identity=identity,
        min_body_share=float(constants["MIN_BODY_SHARE"]),
        long_close_location=float(constants["LONG_CLOSE_LOCATION"]),
    )
