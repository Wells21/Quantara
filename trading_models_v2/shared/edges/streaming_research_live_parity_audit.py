"""Fail-closed streaming research/live parity audit for selected V2 edges.

The audit does not train or modify an edge. It compares the new dashboard APIs
against each edge's existing authoritative full runtime, including a serialized
restart checkpoint. Shared FFD/HMM verification is executed by the runtime adapter.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[2]
REPO_ROOT = THIS_FILE.parents[3]
for path in (V2_ROOT, REPO_ROOT, V2_ROOT / "portfolio_live"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from portfolio_live.common import import_edge_runtime, load_live_manifest  # noqa: E402

REPORT_DIR = V2_ROOT / "portfolio_live" / "outputs" / "streaming_parity_audits"


class StreamingParityAuditError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "Datetime" not in out.columns:
        raise StreamingParityAuditError("Frame is missing Datetime")
    try:
        out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    return out.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def _compare(authority: pd.DataFrame, candidate: pd.DataFrame, *, label: str, atol: float, rtol: float) -> dict[str, Any]:
    left = _normalize(authority).set_index("Datetime")
    right = _normalize(candidate).set_index("Datetime")
    common_index = left.index.intersection(right.index)
    if common_index.empty:
        raise StreamingParityAuditError(f"{label}: no common timestamps")
    common_cols = [col for col in left.columns if col in right.columns]
    if not common_cols:
        raise StreamingParityAuditError(f"{label}: no common columns")
    left = left.loc[common_index, common_cols]
    right = right.loc[common_index, common_cols]
    mismatches: list[str] = []
    max_abs = 0.0
    for col in common_cols:
        lnum = pd.to_numeric(left[col], errors="coerce")
        rnum = pd.to_numeric(right[col], errors="coerce")
        numeric = lnum.notna() | rnum.notna()
        if numeric.any():
            lv = lnum.to_numpy(dtype=float)
            rv = rnum.to_numpy(dtype=float)
            both_nan = np.isnan(lv) & np.isnan(rv)
            finite = np.isfinite(lv) & np.isfinite(rv)
            if finite.any():
                max_abs = max(max_abs, float(np.max(np.abs(lv[finite] - rv[finite]))))
            equal = both_nan | (finite & np.isclose(lv, rv, atol=atol, rtol=rtol))
            if not bool(np.all(equal)):
                mismatches.append(col)
        elif not left[col].fillna("<NA>").astype(str).equals(right[col].fillna("<NA>").astype(str)):
            mismatches.append(col)
    if mismatches:
        raise StreamingParityAuditError(f"{label}: mismatch columns={mismatches[:30]} max_abs_diff={max_abs}")
    return {
        "status": "PASS",
        "rows_compared": int(len(common_index)),
        "columns_compared": int(len(common_cols)),
        "max_abs_diff": float(max_abs),
    }


def _load_bars(runtime: Any) -> pd.DataFrame:
    env = runtime.validate_environment()
    rel = str(env["bar_verification"]["bars"])
    path = V2_ROOT / rel
    bars = pd.read_csv(path)
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    bars["Bar_Start"] = to_datetime_iso(bars["Bar_Start"], utc=True, errors="coerce")
    return bars.dropna(subset=["Datetime", "Bar_Start"]).sort_values("Datetime").reset_index(drop=True)


def audit_edge(
    edge: dict[str, Any],
    *,
    stream_rows: int,
    step_size: int,
    restart_after_rows: int,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    runtime = import_edge_runtime(edge)
    bars = _load_bars(runtime)
    if len(bars) <= stream_rows + 6000:
        raise StreamingParityAuditError(
            f"{edge['edge_id']}: need > {stream_rows + 6000} bars, have {len(bars)}"
        )
    split = len(bars) - int(stream_rows)
    prefix = bars.iloc[:split].copy()

    authority_started = datetime.now(timezone.utc)
    authority_features = runtime.build_features(bars=bars, drop_warmup_rows=False)
    authority_events = runtime.generate_candidate_events(authority_features)
    authority_elapsed_seconds = (
        datetime.now(timezone.utc) - authority_started
    ).total_seconds()

    feature_result = runtime.build_features_live_incremental(
        bars=prefix,
        candidate_times=[prefix["Datetime"].iloc[-1]],
        state={},
        mode="live",
    )
    feature_state = feature_result["state"]
    streamed_features = feature_result["features"]

    event_result = runtime.generate_candidate_events_live(
        features=streamed_features,
        candidate_times=[prefix["Datetime"].iloc[-1]],
        state={},
        mode="live",
    )
    event_state = event_result["state"]
    streamed_event_parts: list[pd.DataFrame] = []
    initial_events = event_result.get("events")
    if isinstance(initial_events, pd.DataFrame) and not initial_events.empty:
        # Prefix output is used only to initialise state; events before the
        # streaming split are excluded from the final comparison below.
        initial = _normalize(initial_events)
        initial = initial[initial["Datetime"] >= bars["Datetime"].iloc[split]]
        if not initial.empty:
            streamed_event_parts.append(initial)

    processed = 0
    restart_done = False
    feature_modes: dict[str, int] = {}
    event_modes: dict[str, int] = {}
    feature_elapsed_ms = 0.0
    event_elapsed_ms = 0.0
    feature_replay_rows: list[int] = []
    event_replay_rows: list[int] = []

    while split + processed < len(bars):
        take = min(step_size, len(bars) - (split + processed))
        chunk_start = split + processed
        processed += take
        current = bars.iloc[: split + processed].copy()
        chunk_times = list(bars.iloc[chunk_start : split + processed]["Datetime"])

        feature_started = datetime.now(timezone.utc)
        feature_result = runtime.build_features_live_incremental(
            bars=current,
            candidate_times=chunk_times,
            state=feature_state,
            mode="live",
        )
        feature_elapsed_ms += (
            datetime.now(timezone.utc) - feature_started
        ).total_seconds() * 1000.0
        feature_state = feature_result["state"]
        streamed_features = feature_result["features"]
        feature_meta = dict(feature_result.get("metadata") or {})
        feature_mode = str(feature_meta.get("continuation_mode") or "unknown")
        feature_modes[feature_mode] = feature_modes.get(feature_mode, 0) + 1
        if feature_meta.get("replay_rows_used") is not None:
            feature_replay_rows.append(int(feature_meta["replay_rows_used"]))

        event_started = datetime.now(timezone.utc)
        event_result = runtime.generate_candidate_events_live(
            features=streamed_features,
            candidate_times=chunk_times,
            state=event_state,
            mode="live",
        )
        event_elapsed_ms += (
            datetime.now(timezone.utc) - event_started
        ).total_seconds() * 1000.0
        event_state = event_result["state"]
        event_meta = dict(event_result.get("metadata") or {})
        event_mode = str(event_meta.get("continuation_mode") or "unknown")
        event_modes[event_mode] = event_modes.get(event_mode, 0) + 1
        if event_meta.get("replay_rows_used") is not None:
            event_replay_rows.append(int(event_meta["replay_rows_used"]))
        new_events = event_result.get("events")
        if isinstance(new_events, pd.DataFrame) and not new_events.empty:
            streamed_event_parts.append(_normalize(new_events))

        if not restart_done and processed >= restart_after_rows:
            feature_state = pickle.loads(
                pickle.dumps(feature_state, protocol=pickle.HIGHEST_PROTOCOL)
            )
            event_state = pickle.loads(
                pickle.dumps(event_state, protocol=pickle.HIGHEST_PROTOCOL)
            )
            restart_done = True

    feature_comparison = _compare(
        authority_features,
        streamed_features,
        label=f"{edge['edge_id']}:features",
        atol=atol,
        rtol=rtol,
    )

    authority_stream_events = _normalize(authority_events)
    allowed = set(to_datetime_iso(bars.iloc[split:]["Datetime"], utc=True))
    authority_stream_events = authority_stream_events[
        authority_stream_events["Datetime"].isin(allowed)
    ].reset_index(drop=True)

    if streamed_event_parts:
        candidate_events = _normalize(
            pd.concat(streamed_event_parts, ignore_index=True, sort=False)
        )
        candidate_events = candidate_events[
            candidate_events["Datetime"].isin(allowed)
        ].reset_index(drop=True)
    else:
        candidate_events = pd.DataFrame(columns=authority_stream_events.columns)

    authority_event_times = list(authority_stream_events.get("Datetime", []))
    candidate_event_times = list(candidate_events.get("Datetime", []))
    if authority_event_times != candidate_event_times:
        raise StreamingParityAuditError(
            f"{edge['edge_id']}: event timestamp sequence mismatch "
            f"authority={len(authority_event_times)} candidate={len(candidate_event_times)}"
        )

    if authority_stream_events.empty and candidate_events.empty:
        event_comparison = {
            "status": "PASS",
            "rows_compared": 0,
            "columns_compared": 0,
            "max_abs_diff": 0.0,
        }
    else:
        event_comparison = _compare(
            authority_stream_events,
            candidate_events,
            label=f"{edge['edge_id']}:events",
            atol=atol,
            rtol=rtol,
        )

    return {
        "edge_id": edge["edge_id"],
        "asset": edge["asset"],
        "status": "PASS",
        "bars_total": int(len(bars)),
        "stream_rows": int(stream_rows),
        "step_size": int(step_size),
        "restart_after_rows": int(restart_after_rows),
        "restart_exercised": bool(restart_done),
        "feature_comparison": feature_comparison,
        "event_comparison": event_comparison,
        "feature_metadata": feature_result.get("metadata", {}),
        "event_metadata": event_result.get("metadata", {}),
        "performance": {
            "authority_full_feature_and_event_seconds": float(authority_elapsed_seconds),
            "stream_feature_total_ms": float(feature_elapsed_ms),
            "stream_event_total_ms": float(event_elapsed_ms),
            "stream_updates": int(max(1, (processed + step_size - 1) // step_size)),
            "feature_continuation_modes": feature_modes,
            "event_continuation_modes": event_modes,
            "feature_replay_rows_min": min(feature_replay_rows) if feature_replay_rows else None,
            "feature_replay_rows_max": max(feature_replay_rows) if feature_replay_rows else None,
            "event_replay_rows_min": min(event_replay_rows) if event_replay_rows else None,
            "event_replay_rows_max": max(event_replay_rows) if event_replay_rows else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edge-id", action="append", default=[])
    parser.add_argument("--stream-rows", type=int, default=1200)
    parser.add_argument("--step-size", type=int, default=1)
    parser.add_argument("--restart-after-rows", type=int, default=600)
    parser.add_argument("--atol", type=float, default=1e-9)
    parser.add_argument("--rtol", type=float, default=1e-8)
    args = parser.parse_args()

    manifest = load_live_manifest()
    selected = manifest.get("selected_edges") or []
    wanted = set(args.edge_id)
    edges = [edge for edge in selected if not wanted or str(edge.get("edge_id")) in wanted]
    if not edges:
        raise SystemExit("No selected live edges matched --edge-id")

    reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for edge in edges:
        try:
            reports.append(
                audit_edge(
                    edge,
                    stream_rows=max(args.stream_rows, 1),
                    step_size=max(args.step_size, 1),
                    restart_after_rows=max(args.restart_after_rows, 1),
                    atol=max(args.atol, 0.0),
                    rtol=max(args.rtol, 0.0),
                )
            )
        except Exception as exc:
            failures.append({"edge_id": str(edge.get("edge_id")), "error": f"{type(exc).__name__}: {exc}"})

    payload = {
        "schema_version": "v2.1",
        "created_at": _utc_now(),
        "status": "PASS" if not failures else "FAIL",
        "edges_tested": int(len(reports) + len(failures)),
        "edges_passed": int(len(reports)),
        "edges_failed": int(len(failures)),
        "reports": reports,
        "failures": failures,
        "training_or_artifact_mutation": False,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / "streaming_research_live_parity_audit_report.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(report_path), "failures": failures}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
