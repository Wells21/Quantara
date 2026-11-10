from __future__ import annotations

"""Exact trend primary-side continuation canary.

The unchanged primary_side.py files remain research authority. This provider
implements only their current-bar primary-side context and event filtering.

It does NOT implement:
- raw candidate sampling,
- model-row construction,
- model scoring,
- probability calibration,
- AFML sizing,
- threshold decisions,
- signal IDs,
- allocator/risk logic,
- production routing.

State advances on every closed bar. Candidate events, when present, are filtered
against the already-computed current primary-side context.
"""

import ast
from dataclasses import dataclass, field
import math
from pathlib import Path
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
    PrimarySourceIdentity,
    extract_primary_source_identity,
)


TREND_LONG_PRIMARY_SIGNATURE = (
    "e67893a36f3807ee4deaec8e1e165573883264a977d746793f63b278e72d6e59"
)
TREND_SHORT_PRIMARY_SIGNATURE = (
    "b215f89bed951065ab80401eea331d0ae13c40fb0935fa2d661cd790f2ddc05a"
)

PROVIDER_SPEC = {
    "provider_id": "phase_d4_trend_primary_side",
    "provider_version": "1",
    "supported_signatures": {
        TREND_LONG_PRIMARY_SIGNATURE: {"side_value": 1},
        TREND_SHORT_PRIMARY_SIGNATURE: {"side_value": -1},
    },
}

CONTEXT_COLUMNS = [
    "Datetime",
    "Close_FFD",
    "ffd_ma",
    "trend_signal",
    "Kalman_Close_Local",
    "Dist_Kalman_Local",
    "Trend_Return_5",
    "trend_core_side",
    "regime_ok",
    "kalman_ok",
    "return_ok",
    "primary_side",
]


@dataclass
class _State:
    ffd_history: list[float] = field(default_factory=list)
    close_history: list[float] = field(default_factory=list)
    kalman_mean: float = math.nan
    kalman_cov: float = math.nan
    kalman_initialized: bool = False
    processed_rows: int = 0
    last_timestamp: Optional[pd.Timestamp] = None
    current_context: Optional[dict[str, Any]] = None


def _source_tree(path: Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise PrimarySideContinuationError(
            f"Could not parse trend primary source {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _module_literal_constants(path: Path) -> dict[str, Any]:
    tree = _source_tree(path)
    out: dict[str, Any] = {}

    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(
                node.targets[0],
                ast.Name,
            ):
                continue
            name = node.targets[0].id
            value_node = node.value
        else:
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            name = node.target.id
            value_node = node.value

        try:
            out[name] = ast.literal_eval(value_node)
        except Exception:
            continue

    return out


def _function_literal_assignments(
    path: Path,
    function_name: str,
) -> dict[str, Any]:
    tree = _source_tree(path)
    function = None

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            function = node
            break

    if function is None:
        raise PrimarySideContinuationError(
            f"{path}: function {function_name!r} missing"
        )

    out: dict[str, Any] = {}
    for node in function.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(
                node.targets[0],
                ast.Name,
            ):
                continue
            name = node.targets[0].id
            value_node = node.value
        else:
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            name = node.target.id
            value_node = node.value

        try:
            out[name] = ast.literal_eval(value_node)
        except Exception:
            continue

    return out


class TrendPrimarySideContinuation:
    def __init__(
        self,
        *,
        identity: PrimarySourceIdentity,
        eps: float,
        trend_window: int,
        trend_threshold: float,
        use_regime_filter: bool,
        use_kalman_confirm: bool,
        kalman_dist_threshold: float,
        use_return_confirm: bool,
        return_lookback: int,
        process_variance: float,
        measurement_variance: float,
    ) -> None:
        if eps <= 0:
            raise PrimarySideContinuationError("EPS must be > 0")
        if trend_window <= 0:
            raise PrimarySideContinuationError("TREND_WINDOW must be > 0")
        if return_lookback <= 0:
            raise PrimarySideContinuationError("RETURN_LOOKBACK must be > 0")
        if process_variance <= 0 or measurement_variance <= 0:
            raise PrimarySideContinuationError(
                "Kalman variances must be > 0"
            )

        # The current signed trend source has regime filtering disabled and
        # hard-wires regime_ok=1. A future signature with different semantics
        # must get its own certified provider behavior.
        if use_regime_filter:
            raise PrimarySideContinuationError(
                "Current trend primary signature unexpectedly enables regime filter"
            )

        self.identity = identity
        self.side_value = int(identity.side_value)
        self.eps = float(eps)
        self.trend_window = int(trend_window)
        self.trend_threshold = float(trend_threshold)
        self.use_regime_filter = bool(use_regime_filter)
        self.use_kalman_confirm = bool(use_kalman_confirm)
        self.kalman_dist_threshold = float(kalman_dist_threshold)
        self.use_return_confirm = bool(use_return_confirm)
        self.return_lookback = int(return_lookback)
        self.process_variance = float(process_variance)
        self.measurement_variance = float(measurement_variance)
        self._state = _State()

    def reset(self) -> None:
        self._state = _State()

    @staticmethod
    def _timestamp(value: Any) -> pd.Timestamp:
        result = to_datetime_iso(value, utc=True, errors="coerce")
        if pd.isna(result):
            raise PrimarySideContinuationError(
                f"invalid Datetime: {value!r}"
            )
        return pd.Timestamp(result)

    @staticmethod
    def _finite_close(value: Any) -> float:
        result = pd.to_numeric(
            pd.Series([value]),
            errors="coerce",
        ).iat[0]
        if pd.isna(result) or not np.isfinite(float(result)):
            raise PrimarySideContinuationError(
                f"invalid Close: {value!r}"
            )
        return float(result)

    @staticmethod
    def _ffd_value(value: Any) -> float:
        result = pd.to_numeric(
            pd.Series([value]),
            errors="coerce",
        ).iat[0]
        return math.nan if pd.isna(result) else float(result)

    def _normalize(self, features: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(features, pd.DataFrame):
            raise PrimarySideContinuationError(
                "primary-side features must be DataFrame"
            )

        required = ["Datetime", "Close", "Close_FFD"]
        missing = [
            name for name in required
            if name not in features.columns
        ]
        if missing:
            raise PrimarySideContinuationError(
                f"primary-side features missing required columns: {missing}"
            )

        frame = features[required].copy()
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"],
            utc=True,
            errors="coerce",
        )
        frame["Close"] = pd.to_numeric(
            frame["Close"],
            errors="coerce",
        )
        frame["Close_FFD"] = pd.to_numeric(
            frame["Close_FFD"],
            errors="coerce",
        )

        # Mirrors source build_primary_side_context: required source fields are
        # Datetime and Close. Close_FFD may remain NaN and naturally fail the
        # rolling trend condition until sufficient valid context exists.
        frame = frame.dropna(subset=["Datetime", "Close"]).copy()
        frame = frame.sort_values("Datetime").reset_index(drop=True)
        if frame.empty:
            raise PrimarySideContinuationError(
                "primary-side features empty after normalization"
            )
        return frame

    def _advance_kalman(self, close: float) -> float:
        state = self._state
        if not state.kalman_initialized:
            state.kalman_mean = float(close)
            state.kalman_cov = 1.0
            state.kalman_initialized = True
            return float(state.kalman_mean)

        prior_mean = float(state.kalman_mean)
        prior_cov = float(state.kalman_cov) + self.process_variance
        gain = prior_cov / (
            prior_cov + self.measurement_variance
        )
        mean = prior_mean + gain * (float(close) - prior_mean)
        cov = (1.0 - gain) * prior_cov

        state.kalman_mean = float(mean)
        state.kalman_cov = float(cov)
        return float(mean)

    def _rolling_ffd_mean(self, close_ffd: float) -> float:
        history = self._state.ffd_history
        history.append(float(close_ffd))
        if len(history) > self.trend_window:
            del history[: len(history) - self.trend_window]

        if len(history) < self.trend_window:
            return math.nan
        if not all(np.isfinite(value) for value in history):
            return math.nan
        return float(
            math.fsum(float(value) for value in history)
            / float(self.trend_window)
        )

    def _return_context(self, close: float) -> float:
        history = self._state.close_history
        if len(history) >= self.return_lookback:
            lag_close = float(history[-self.return_lookback])
            with np.errstate(divide="ignore", invalid="ignore"):
                value = float(
                    np.log(
                        (float(close) + self.eps)
                        / (lag_close + self.eps)
                    )
                )
        else:
            value = math.nan

        history.append(float(close))
        if len(history) > self.return_lookback:
            del history[: len(history) - self.return_lookback]
        return value

    def _advance(
        self,
        row: Mapping[str, Any],
        *,
        enforce_append_only: bool,
    ) -> dict[str, Any]:
        state = self._state
        timestamp = self._timestamp(row["Datetime"])
        close = self._finite_close(row["Close"])
        close_ffd = self._ffd_value(row["Close_FFD"])

        if (
            enforce_append_only
            and state.last_timestamp is not None
            and timestamp <= state.last_timestamp
        ):
            raise PrimarySideContinuationError(
                "update_one requires strictly increasing Datetime"
            )

        ffd_ma = self._rolling_ffd_mean(close_ffd)
        if np.isfinite(close_ffd) and np.isfinite(ffd_ma):
            trend_signal = float(close_ffd - ffd_ma)
        else:
            trend_signal = math.nan

        kalman_close = self._advance_kalman(close)
        dist_kalman = float(
            (close - kalman_close)
            / (kalman_close + self.eps)
        )
        trend_return = self._return_context(close)

        trend_core_side = 0
        if (
            np.isfinite(trend_signal)
            and trend_signal > self.trend_threshold
        ):
            trend_core_side = 1
        elif (
            np.isfinite(trend_signal)
            and trend_signal < -self.trend_threshold
        ):
            trend_core_side = -1

        regime_ok = 1

        if self.use_kalman_confirm:
            if trend_core_side == 1:
                kalman_ok = int(
                    dist_kalman > self.kalman_dist_threshold
                )
            elif trend_core_side == -1:
                kalman_ok = int(
                    dist_kalman < -self.kalman_dist_threshold
                )
            else:
                kalman_ok = 0
        else:
            kalman_ok = 1

        if self.use_return_confirm:
            if trend_core_side == 1:
                return_ok = int(
                    np.isfinite(trend_return)
                    and trend_return > 0.0
                )
            elif trend_core_side == -1:
                return_ok = int(
                    np.isfinite(trend_return)
                    and trend_return < 0.0
                )
            else:
                return_ok = 0
        else:
            return_ok = 1

        primary_side = (
            self.side_value
            if (
                trend_core_side == self.side_value
                and regime_ok == 1
                and kalman_ok == 1
                and return_ok == 1
            )
            else 0
        )

        context = {
            "Datetime": timestamp,
            "Close_FFD": float(close_ffd),
            "ffd_ma": float(ffd_ma),
            "trend_signal": float(trend_signal),
            "Kalman_Close_Local": float(kalman_close),
            "Dist_Kalman_Local": float(dist_kalman),
            "Trend_Return_5": float(trend_return),
            "trend_core_side": int(trend_core_side),
            "regime_ok": int(regime_ok),
            "kalman_ok": int(kalman_ok),
            "return_ok": int(return_ok),
            "primary_side": int(primary_side),
        }

        state.processed_rows += 1
        state.last_timestamp = timestamp
        state.current_context = dict(context)
        return context

    def bootstrap(
        self,
        features: pd.DataFrame,
    ) -> dict[str, Any]:
        frame = self._normalize(features)
        self.reset()
        for row in frame.to_dict(orient="records"):
            self._advance(
                row,
                enforce_append_only=False,
            )
        return self.state_summary()

    def update_one(
        self,
        row: Union[Mapping[str, Any], pd.Series],
    ) -> dict[str, Any]:
        """Consume one new row; no FFD/history disk read and no batch fallback."""

        payload = (
            row.to_dict()
            if isinstance(row, pd.Series)
            else dict(row)
        )
        missing = [
            name
            for name in ("Datetime", "Close", "Close_FFD")
            if name not in payload
        ]
        if missing:
            raise PrimarySideContinuationError(
                f"update_one missing fields: {missing}"
            )
        return self._advance(
            payload,
            enforce_append_only=True,
        )

    def replay(
        self,
        features: pd.DataFrame,
    ) -> pd.DataFrame:
        """Offline certification helper; not a production normal-path API."""

        frame = self._normalize(features)
        self.reset()
        rows = []
        for row in frame.to_dict(orient="records"):
            rows.append(
                self._advance(
                    row,
                    enforce_append_only=False,
                )
            )
        return pd.DataFrame(
            rows,
            columns=CONTEXT_COLUMNS,
        )

    def filter_event(
        self,
        event: Optional[Mapping[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Apply current primary context to one already-generated raw event."""

        if event is None:
            return None

        context = self._state.current_context
        if context is None:
            raise PrimarySideContinuationError(
                "filter_event requires initialized current primary context"
            )

        payload = dict(event)
        if "Datetime" not in payload:
            raise PrimarySideContinuationError(
                "candidate event missing Datetime"
            )

        event_time = self._timestamp(payload["Datetime"])
        context_time = self._timestamp(context["Datetime"])
        if event_time != context_time:
            raise PrimarySideContinuationError(
                "candidate event timestamp does not match current primary context"
            )

        if "side" in payload:
            try:
                event_side = int(payload["side"])
            except Exception as exc:
                raise PrimarySideContinuationError(
                    "candidate event side is invalid"
                ) from exc
            if event_side != self.side_value:
                raise PrimarySideContinuationError(
                    "candidate event side conflicts with primary-side provider"
                )

        if int(context["primary_side"]) != self.side_value:
            return None

        out = dict(payload)
        for name in CONTEXT_COLUMNS:
            if name == "Datetime":
                continue
            out[name] = context[name]

        out["primary_side_filter"] = 1
        out["side"] = self.side_value
        return out

    def state_summary(self) -> dict[str, Any]:
        state = self._state
        return {
            "provider_id": PROVIDER_SPEC["provider_id"],
            "provider_version": PROVIDER_SPEC["provider_version"],
            "edge_id": self.identity.edge_id,
            "side_value": self.side_value,
            "processed_rows": int(state.processed_rows),
            "last_timestamp": (
                state.last_timestamp.isoformat()
                if state.last_timestamp is not None
                else None
            ),
            "ffd_history_rows": len(state.ffd_history),
            "close_history_rows": len(state.close_history),
            "trend_window": self.trend_window,
            "return_lookback": self.return_lookback,
            "kalman_initialized": bool(
                state.kalman_initialized
            ),
            "normal_path_ffd_disk_reads": False,
            "normal_path_history_disk_reads": False,
            "normal_path_full_primary_fallback": False,
        }


def _require_source_constant(
    constants: Mapping[str, Any],
    name: str,
) -> Any:
    if name not in constants:
        raise PrimarySideContinuationError(
            f"trend primary source constant missing: {name}"
        )
    return constants[name]


def create_provider(
    *,
    primary_logic_signature: str,
    d1_edge: Mapping[str, Any],
    v2_root: Union[str, Path],
) -> TrendPrimarySideContinuation:
    signature = str(primary_logic_signature or "").strip()
    mode = PROVIDER_SPEC["supported_signatures"].get(signature)
    if not isinstance(mode, Mapping):
        raise PrimarySideContinuationError(
            f"Trend primary provider does not support {signature}"
        )

    primary = d1_edge.get("primary_side") or {}
    if (
        str(primary.get("primary_logic_signature") or "")
        != signature
    ):
        raise PrimarySideContinuationError(
            "D.1/provider primary signature mismatch"
        )

    identity = extract_primary_source_identity(
        v2_root=v2_root,
        d1_edge=d1_edge,
    )
    expected_side = int(mode["side_value"])
    if int(identity.side_value) != expected_side:
        raise PrimarySideContinuationError(
            "Primary signature/source side mismatch"
        )

    root = Path(v2_root).resolve()
    source_path = (
        root / identity.primary_source_path
    ).resolve()

    source_constants = _module_literal_constants(
        source_path
    )
    kalman_constants = _function_literal_assignments(
        source_path,
        "_compute_local_kalman",
    )

    required = (
        "EPS",
        "TREND_WINDOW",
        "TREND_THRESHOLD",
        "USE_REGIME_FILTER",
        "USE_KALMAN_CONFIRM",
        "KALMAN_DIST_THRESHOLD",
        "USE_RETURN_CONFIRM",
        "RETURN_LOOKBACK",
    )
    for name in required:
        _require_source_constant(source_constants, name)

    for name in (
        "process_variance",
        "measurement_variance",
    ):
        if name not in kalman_constants:
            raise PrimarySideContinuationError(
                f"trend primary Kalman literal missing: {name}"
            )

    # D.1 stores the source-derived semantic subset. Whenever it carries a
    # current constant, construction fails closed if source and D.1 disagree.
    d1_constants = primary.get("semantic_constants") or {}
    if not isinstance(d1_constants, Mapping):
        raise PrimarySideContinuationError(
            "D.1 primary semantic_constants missing"
        )

    for name, d1_value in d1_constants.items():
        if name not in source_constants:
            continue

        source_value = source_constants[name]

        # D.1 is JSON, so source sets are serialized as arrays/lists.
        # Preserve ordinary ordered-sequence equality; only normalize when
        # the unchanged Python source authority is explicitly a set/frozenset.
        if isinstance(source_value, (set, frozenset)):
            if not isinstance(
                d1_value,
                (list, tuple, set, frozenset),
            ):
                semantic_equal = False
            else:
                try:
                    semantic_equal = set(source_value) == set(d1_value)
                except TypeError:
                    semantic_equal = False
        else:
            semantic_equal = source_value == d1_value

        if not semantic_equal:
            raise PrimarySideContinuationError(
                f"D.1/source primary constant mismatch {name}: "
                f"{d1_value!r} != {source_value!r}"
            )

    provider = TrendPrimarySideContinuation(
        identity=identity,
        eps=float(source_constants["EPS"]),
        trend_window=int(
            source_constants["TREND_WINDOW"]
        ),
        trend_threshold=float(
            source_constants["TREND_THRESHOLD"]
        ),
        use_regime_filter=bool(
            source_constants["USE_REGIME_FILTER"]
        ),
        use_kalman_confirm=bool(
            source_constants["USE_KALMAN_CONFIRM"]
        ),
        kalman_dist_threshold=float(
            source_constants["KALMAN_DIST_THRESHOLD"]
        ),
        use_return_confirm=bool(
            source_constants["USE_RETURN_CONFIRM"]
        ),
        return_lookback=int(
            source_constants["RETURN_LOOKBACK"]
        ),
        process_variance=float(
            kalman_constants["process_variance"]
        ),
        measurement_variance=float(
            kalman_constants["measurement_variance"]
        ),
    )

    if provider.return_lookback != 5:
        raise PrimarySideContinuationError(
            "Current trend primary signature no longer uses 5-bar return"
        )

    return provider
