"""Exact absorption event-first gate for live candidate bars."""

from __future__ import annotations

import logging
import pickle
from types import ModuleType
from typing import Any, Mapping

import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

from . import state as s
from .continuation import (
    verify_asset_ffd_manifest_hot,
    verify_asset_hmm_regime_manifest_hot,
)

LOG = logging.getLogger("v2_live_streaming_hotpath")


def _times(values: Any) -> list[pd.Timestamp]:
    output: list[pd.Timestamp] = []
    for value in values or []:
        try:
            output.append(s.utc_timestamp(value))
        except Exception:
            continue
    return sorted(set(output))


def _unwrap(state: Mapping[str, Any] | None) -> tuple[Any, set[pd.Timestamp]]:
    if not isinstance(state, Mapping) or state.get("schema_version") != s.PROBE_SCHEMA:
        return state, set()
    return state.get("authoritative_state"), set(
        _times(state.get("confirmed_no_event_times"))
    )


def _wrap(
    edge_id: str,
    asset: str,
    authority: Any,
    confirmed: set[pd.Timestamp],
    last: pd.Timestamp | None,
) -> dict[str, Any]:
    state = {
        "schema_version": s.PROBE_SCHEMA,
        "edge_id": edge_id,
        "asset": asset,
        "authoritative_state": authority,
        "confirmed_no_event_times": [
            stamp.isoformat() for stamp in sorted(confirmed)[-500:]
        ],
        "last_probe_timestamp": last.isoformat() if last is not None else None,
    }
    pickle.loads(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    return state


def _probe(
    runtime: ModuleType,
    bars: pd.DataFrame,
    candidates: list[pd.Timestamp],
    confirmed: set[pd.Timestamp],
):
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    missing = [column for column in required if column not in bars.columns]
    if missing:
        raise RuntimeError(f"probe bars missing required columns: {missing}")
    frame = bars.copy()
    frame["Datetime"] = to_datetime_iso(frame["Datetime"], utc=True, errors="coerce")
    frame["Bar_Start"] = to_datetime_iso(frame["Bar_Start"], utc=True, errors="coerce")
    for column in ["Open", "High", "Low", "Close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=required).sort_values("Datetime", kind="mergesort")
    frame = frame.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    atr = runtime.ta.atr(
        frame["High"].astype("float64"),
        frame["Low"].astype("float64"),
        frame["Close"].astype("float64"),
        length=14,
    )
    if atr is None:
        raise RuntimeError("absorption probe ATR returned None")
    keep = [
        column
        for column in [*required, "Ticks", "Threshold"]
        if column in frame.columns
    ]
    probe = frame[keep].copy()
    probe["ATR_14"] = pd.to_numeric(atr, errors="coerce")
    probe["edge_id"] = str(runtime.EDGE_ID)
    probe["side"] = int(runtime.SIDE_VALUE)
    pending = [stamp for stamp in candidates if stamp not in confirmed]
    if not pending:
        return (
            probe,
            pd.DataFrame(columns=["Datetime"]),
            {
                "reason": "all_candidate_times_previously_confirmed_no_event",
                "candidate_times": len(candidates),
                "candidate_events": 0,
            },
        )
    events = runtime.generate_candidate_events(probe)
    if not isinstance(events, pd.DataFrame):
        raise RuntimeError("candidate event generator did not return DataFrame")
    if events.empty or "Datetime" not in events.columns:
        current = pd.DataFrame(columns=["Datetime"])
    else:
        events = events.copy()
        events["Datetime"] = to_datetime_iso(
            events["Datetime"], utc=True, errors="coerce"
        )
        current = (
            events[events["Datetime"].isin(set(pending))].copy().reset_index(drop=True)
        )
    return (
        probe,
        current,
        {
            "reason": "candidate_event_detected"
            if not current.empty
            else "candidate_times_exactly_confirmed_no_event",
            "candidate_times": len(candidates),
            "candidate_events": int(len(current)),
        },
    )


def _wrap_result(
    result: Any,
    edge_id: str,
    asset: str,
    confirmed: set[pd.Timestamp],
    last: pd.Timestamp | None,
) -> Any:
    if not isinstance(result, dict):
        return result
    output = dict(result)
    authority = None
    for key in ("state", "runtime_state", "next_state"):
        if key in output and output[key] is not None:
            authority = output[key]
            break
    output["state"] = _wrap(edge_id, asset, authority, confirmed, last)
    metadata = dict(output.get("metadata") or output.get("diagnostics") or {})
    metadata["absorption_event_first_gate"] = True
    output["metadata"] = metadata
    return output


def install_absorption_event_gate(runtime: ModuleType) -> dict[str, Any]:
    if str(getattr(runtime, "FAMILY", "")).strip().lower() != "absorption":
        return {"installed": False, "reason": "not_absorption"}
    if bool(getattr(runtime, "ABSORPTION_EVENT_FIRST_GATE_INSTALLED", False)):
        return {"installed": True, "already_installed": True}
    original_features = getattr(runtime, "build_features_live_incremental", None)
    original_events = getattr(runtime, "generate_candidate_events_live", None)
    if not callable(original_features) or not callable(original_events):
        return {"installed": False, "reason": "stateful_runtime_api_missing"}
    edge_id = str(runtime.EDGE_ID)
    asset = s.asset_key(runtime.ASSET)

    def features_api(
        bars: pd.DataFrame,
        candidate_times=None,
        last_processed=None,
        drop_warmup_rows: bool = False,
        state=None,
        research_contract=None,
        mode: str = "live",
    ):
        authority, confirmed = _unwrap(state)
        candidates = _times(candidate_times)
        last = max(candidates) if candidates else None
        try:
            probe, current, diagnostics = _probe(runtime, bars, candidates, confirmed)
        except Exception as exc:
            LOG.warning(
                "ABSORPTION_EVENT_PROBE_FALLBACK edge=%s error=%s:%s",
                edge_id,
                type(exc).__name__,
                exc,
            )
            result = original_features(
                bars=bars,
                candidate_times=candidate_times,
                last_processed=last_processed,
                drop_warmup_rows=drop_warmup_rows,
                state=authority,
                research_contract=research_contract,
                mode=mode,
            )
            return _wrap_result(result, edge_id, asset, confirmed, last)
        if not current.empty:
            event_times = set(
                to_datetime_iso(current["Datetime"], utc=True, errors="coerce").dropna()
            )
            confirmed.update(set(candidates).difference(event_times))
            result = original_features(
                bars=bars,
                candidate_times=candidate_times,
                last_processed=last_processed,
                drop_warmup_rows=drop_warmup_rows,
                state=authority,
                research_contract=research_contract,
                mode=mode,
            )
            output = _wrap_result(result, edge_id, asset, confirmed, last)
            output["metadata"]["absorption_probe"] = diagnostics
            return output
        confirmed.update(candidates)
        probe[s.PROBE_MARKER] = 1
        ffd = verify_asset_ffd_manifest_hot(asset)
        hmm = verify_asset_hmm_regime_manifest_hot(asset)
        flags = {
            "streaming_parity_passed": bool(
                ffd.get("streaming_parity_passed")
                and hmm.get("streaming_parity_passed")
            ),
            "append_only_history": bool(
                ffd.get("append_only_history") and hmm.get("append_only_history")
            ),
            "restart_parity_passed": bool(
                ffd.get("restart_parity_passed") and hmm.get("restart_parity_passed")
            ),
            "hmm_refit_schedule_preserved": bool(
                hmm.get("hmm_refit_schedule_preserved")
            ),
            "deterministic_noise_contract_preserved": bool(
                hmm.get("deterministic_noise_contract_preserved")
            ),
        }
        if not all(flags.values()):
            raise RuntimeError(f"shared continuation parity failed: {flags}")
        LOG.info(
            "ABSORPTION_EVENT_PROBE_SKIP edge=%s reason=%s",
            edge_id,
            diagnostics["reason"],
        )
        return {
            "features": probe,
            "state": _wrap(edge_id, asset, authority, confirmed, last),
            "metadata": {
                **flags,
                "event_state_continuity_preserved": True,
                "edge_local_incremental_optimization": True,
                "edge_local_authority_rebuilt": False,
                "absorption_probe": diagnostics,
            },
        }

    def events_api(
        features: pd.DataFrame,
        candidate_times=None,
        last_processed=None,
        state=None,
        research_contract=None,
        mode: str = "live",
    ):
        authority, confirmed = _unwrap(state)
        candidates = _times(candidate_times)
        last = max(candidates) if candidates else None
        if isinstance(features, pd.DataFrame) and s.PROBE_MARKER in features.columns:
            confirmed.update(candidates)
            return {
                "events": pd.DataFrame(columns=["Datetime"]),
                "state": _wrap(edge_id, asset, authority, confirmed, last),
                "metadata": {
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "event_state_continuity_preserved": True,
                },
            }
        result = original_events(
            features=features,
            candidate_times=candidate_times,
            last_processed=last_processed,
            state=authority,
            research_contract=research_contract,
            mode=mode,
        )
        return _wrap_result(result, edge_id, asset, confirmed, last)

    runtime.build_features_live_incremental = features_api
    runtime.generate_candidate_events_live = events_api
    runtime.ABSORPTION_EVENT_FIRST_GATE_INSTALLED = True
    return {
        "installed": True,
        "edge_id": edge_id,
        "asset": asset,
        "mode": "exact_event_probe_then_authoritative_full_build_only_on_candidate",
    }
