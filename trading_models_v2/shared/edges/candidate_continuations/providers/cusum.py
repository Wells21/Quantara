from __future__ import annotations

"""Exact raw symmetric-CUSUM continuation canary.

The unchanged candidate_events.py files remain research authority. This provider
implements only their recursive raw candidate computation. It does not implement
primary-side filtering, model rows, scoring, risk, allocator logic, or routing.
"""

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Mapping, Union

import numpy as np
import pandas as pd

from shared.edges.native_one_row_feature_engine import (
    _ewm_var_state,
    _ewm_var_update,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from ..base import (
    CandidateContinuationError,
    CandidateSourceIdentity,
    extract_candidate_source_identity,
)

POSITIVE_CUSUM_SIGNATURE_DAX_NASDAQ = (
    "0ae152aa617aa0abf2cb8dd5b6275a3a535656a48530fb829fcc408b501c162a"
)
POSITIVE_CUSUM_SIGNATURE_XAUUSD = (
    "48c438d37a71e0a90bd27587a401e8d3bba809fbb10bae155292733e84b88a32"
)
POSITIVE_CUSUM_SIGNATURE = POSITIVE_CUSUM_SIGNATURE_DAX_NASDAQ
NEGATIVE_CUSUM_SIGNATURE = (
    "4e85d64e02d3594c3d063f3981e8e2207f644ff078799fe7b8c32ef462601ac6"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_raw_symmetric_cusum",
    "provider_version": "1",
    "supported_signatures": {
        POSITIVE_CUSUM_SIGNATURE_DAX_NASDAQ: {"emit_direction": "positive"},
        POSITIVE_CUSUM_SIGNATURE_XAUUSD: {"emit_direction": "positive"},
        NEGATIVE_CUSUM_SIGNATURE: {"emit_direction": "negative"},
    },
}

EVENT_COLUMNS = [
    "Datetime",
    "Bar_Start",
    "edge_id",
    "side",
    "Event_Vol",
    "event_id",
    "candidate_event_generator",
    "cusum_direction",
    "event_known_after_close",
    "uses_future_information",
    "cusum_k",
    "cusum_threshold",
    "bar_index",
    "Close",
]


@dataclass
class _State:
    ewm_var: dict[str, Any]
    close_history: list[float] = field(default_factory=list)
    last_close: float = math.nan
    s_pos: float = 0.0
    s_neg: float = 0.0
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None


class RawCusumCandidateContinuation:
    def __init__(
        self,
        *,
        identity: CandidateSourceIdentity,
        emit_direction: str,
        eps: float,
        cusum_k: float,
        vol_lookback_bars: int,
        vol_ewm_span: int,
        min_event_vol: float,
        authority_window_rows: int = 6000,
    ) -> None:
        if emit_direction not in {"positive", "negative"}:
            raise CandidateContinuationError("invalid CUSUM emit direction")
        if (
            eps <= 0
            or cusum_k <= 0
            or vol_lookback_bars <= 0
            or vol_ewm_span <= 0
            or min_event_vol <= 0
            or authority_window_rows <= 0
        ):
            raise CandidateContinuationError("invalid CUSUM provider configuration")

        self.identity = identity
        self.emit_direction = emit_direction
        self.eps = float(eps)
        self.cusum_k = float(cusum_k)
        self.vol_lookback_bars = int(vol_lookback_bars)
        self.vol_ewm_span = int(vol_ewm_span)
        self.min_event_vol = float(min_event_vol)
        self.authority_window_rows = int(authority_window_rows)
        self._state = self._new_state()

    def _new_state(self) -> _State:
        return _State(
            ewm_var=_ewm_var_state(
                np.asarray([], dtype="float64"),
                self.vol_ewm_span,
            )
        )

    def reset(self) -> None:
        self._state = self._new_state()

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(features, pd.DataFrame):
            raise CandidateContinuationError("features must be DataFrame")
        required = ["Datetime", "Bar_Start", "Close"]
        missing = [name for name in required if name not in features.columns]
        if missing:
            raise CandidateContinuationError(
                f"features missing required columns: {missing}"
            )
        frame = features[required].copy()
        frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
        frame["Bar_Start"] = to_datetime_iso(frame["Bar_Start"], utc=True, errors="coerce")
        frame["Close"] = pd.to_numeric(frame["Close"], errors="coerce")
        frame = frame.dropna(subset=required).copy()
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise CandidateContinuationError("features empty after normalization")
        return frame

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        result = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(result):
            raise CandidateContinuationError(f"invalid timestamp: {value!r}")
        return pd.Timestamp(result)

    @staticmethod
    def _close(value: Any) -> float:
        result = pd.to_numeric(pd.Series([value]), errors="coerce").iat[0]
        if pd.isna(result):
            raise CandidateContinuationError(f"invalid Close: {value!r}")
        return float(result)

    def _event_vol(self, close: float) -> float:
        history = self._state.close_history
        if len(history) >= self.vol_lookback_bars:
            lag_close = float(history[-self.vol_lookback_bars])
            with np.errstate(divide="ignore", invalid="ignore"):
                long_return = float(
                    np.log((close + self.eps) / (lag_close + self.eps))
                )
        else:
            long_return = math.nan

        variance = float(_ewm_var_update(self._state.ewm_var, long_return))
        if not np.isfinite(variance) or variance < 0.0:
            return math.nan
        return max(math.sqrt(variance), self.min_event_vol)

    def _append_close(self, close: float) -> None:
        history = self._state.close_history
        history.append(float(close))
        if len(history) > self.vol_lookback_bars:
            del history[: len(history) - self.vol_lookback_bars]

    def _event(
        self,
        *,
        timestamp: pd.Timestamp,
        bar_start: pd.Timestamp,
        close: float,
        event_vol: float,
        threshold: float,
        source_index: int,
        direction: str,
    ) -> dict[str, Any]:
        return {
            "Datetime": timestamp,
            "Bar_Start": bar_start,
            "edge_id": self.identity.edge_id,
            "side": int(self.identity.side_value),
            "Event_Vol": float(event_vol),
            "event_id": (
                f"{self.identity.edge_id}|{timestamp.isoformat()}|"
                f"{direction}_cusum"
            ),
            "candidate_event_generator": self.identity.candidate_event_generator,
            "cusum_direction": direction,
            "event_known_after_close": True,
            "uses_future_information": False,
            "cusum_k": float(self.cusum_k),
            "cusum_threshold": float(threshold),
            "bar_index": min(
                int(source_index),
                self.authority_window_rows - 1,
            ),
            "Close": float(close),
        }

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any] | None:
        state = self._state
        timestamp = self._timestamp(row["Datetime"])
        bar_start = self._timestamp(row["Bar_Start"])
        close = self._close(row["Close"])

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and timestamp <= state.last_timestamp
        ):
            raise CandidateContinuationError(
                "update_one requires strictly increasing Datetime"
            )

        source_index = int(state.processed_rows)
        event_vol = self._event_vol(close)
        if source_index == 0 or not np.isfinite(state.last_close):
            diff = math.nan
        else:
            with np.errstate(divide="ignore", invalid="ignore"):
                diff = float(
                    np.log(close + self.eps)
                    - np.log(float(state.last_close) + self.eps)
                )

        emitted = None
        if np.isfinite(diff) and np.isfinite(event_vol):
            threshold = float(self.cusum_k * event_vol)
            if threshold > 0.0 and np.isfinite(threshold):
                state.s_pos = max(0.0, state.s_pos + float(diff))
                state.s_neg = min(0.0, state.s_neg + float(diff))

                if state.s_pos > threshold:
                    if self.emit_direction == "positive":
                        emitted = self._event(
                            timestamp=timestamp,
                            bar_start=bar_start,
                            close=close,
                            event_vol=event_vol,
                            threshold=threshold,
                            source_index=source_index,
                            direction="positive",
                        )
                    state.s_pos = 0.0

                if state.s_neg < -threshold:
                    if self.emit_direction == "negative":
                        emitted = self._event(
                            timestamp=timestamp,
                            bar_start=bar_start,
                            close=close,
                            event_vol=event_vol,
                            threshold=threshold,
                            source_index=source_index,
                            direction="negative",
                        )
                    state.s_neg = 0.0

        self._append_close(close)
        state.last_close = close
        state.last_timestamp = timestamp
        state.processed_rows += 1
        return emitted

    def bootstrap(self, features: pd.DataFrame) -> dict[str, Any]:
        frame = self._normalize(features)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(row, enforce_append_only=False)
        return self.state_summary()

    def update_one(
        self,
        row: Union[Mapping[str, Any], pd.Series],
    ) -> dict[str, Any] | None:
        """Consume one new row; no history read and no batch fallback."""

        payload = row.to_dict() if isinstance(row, pd.Series) else dict(row)
        missing = [
            name
            for name in ("Datetime", "Bar_Start", "Close")
            if name not in payload
        ]
        if missing:
            raise CandidateContinuationError(
                f"update_one missing fields: {missing}"
            )
        return self._advance(payload, enforce_append_only=True)

    def replay(self, features: pd.DataFrame) -> pd.DataFrame:
        """Offline certification helper; not a production normal-path API."""

        frame = self._normalize(features)
        self.reset()
        events = []
        for row in frame.to_dict(orient="records"):
            event = self._advance(row, enforce_append_only=False)
            if event is not None:
                events.append(event)
        return pd.DataFrame(events, columns=EVENT_COLUMNS)

    def state_summary(self) -> dict[str, Any]:
        state = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "emit_direction": self.emit_direction,
            "processed_rows": int(state.processed_rows),
            "last_timestamp": (
                state.last_timestamp.isoformat()
                if state.last_timestamp is not None
                else None
            ),
            "close_history_rows": len(state.close_history),
            "authority_window_rows": self.authority_window_rows,
            "normal_path_history_disk_reads": False,
            "normal_path_full_candidate_fallback": False,
        }


def _constant(constants: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in constants:
            return constants[name]
    raise CandidateContinuationError(
        f"missing D.1 CUSUM semantic constant; expected one of {names}"
    )


def create_provider(
    *,
    candidate_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root: Union[str, Path],
) -> RawCusumCandidateContinuation:
    signature = str(candidate_logic_signature or "").strip()
    mode = PROVIDER_SPEC["supported_signatures"].get(signature)
    if not isinstance(mode, Mapping):
        raise CandidateContinuationError(
            f"CUSUM provider does not support {signature}"
        )

    candidate = d1_edge.get("candidate") or {}
    if str(candidate.get("candidate_logic_signature") or "") != signature:
        raise CandidateContinuationError("D.1/provider signature mismatch")
    constants = candidate.get("semantic_constants") or {}
    if not isinstance(constants, Mapping):
        raise CandidateContinuationError("D.1 semantic_constants missing")

    identity = extract_candidate_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    return RawCusumCandidateContinuation(
        identity=identity,
        emit_direction=str(mode["emit_direction"]),
        eps=float(_constant(constants, "EPS")),
        cusum_k=float(_constant(constants, "CUSUM_K")),
        vol_lookback_bars=int(
            _constant(
                constants,
                "EVENT_VOL_LOOKBACK_BARS",
                "EVENT_VOL_LOOKBACK",
            )
        ),
        vol_ewm_span=int(_constant(constants, "EVENT_VOL_EWM_SPAN")),
        min_event_vol=float(
            _constant(constants, "MIN_EVENT_VOL", "MIN_VOL")
        ),
        authority_window_rows=6000,
    )
