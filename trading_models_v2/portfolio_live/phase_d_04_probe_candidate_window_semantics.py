from __future__ import annotations

"""D4.2 authority-semantics probe for candidate continuation design.

This is a read-only offline probe. It does NOT implement a continuation provider
and does NOT install production routing.

Question answered
-----------------
The current live D3 path evaluates candidate logic on the current 6,000-row C4
canonical ring. A true one-row continuation naturally behaves as if the
bootstrap 6,000 rows were followed by additional rows without discarding the
oldest candidate state.

For each selected edge (or one representative per D.1 candidate-logic group),
this probe compares at each new endpoint:

    growing authority:
        candidate generator on bootstrap-6000 + all new rows so far

    current live-style authority:
        candidate generator on the latest 6000 rows only

Both sides call the UNCHANGED research runtime candidate generator. Existing
candidate_events.py/runtime.py files are never modified.

The output classifies which candidate logic is empirically compatible with
persistent bootstrap state and which logic is sensitive to the sliding-window
boundary. This classification guides provider implementation; it is not a
production certificate.
"""

import argparse
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
for path in (HERE, V2_ROOT, V2_ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

import phase_c_03_certify_consumer_contract as c3cert  # noqa: E402
import phase_d_02_certify_candidate_first as d2cert  # noqa: E402
from shared.edges.phase_c_incremental_feature_engine import (  # noqa: E402
    bootstrap_compact_state,
)
from shared.edges.phase_d_candidate_first import (  # noqa: E402
    edge_map,
    load_phase_d1_contract,
    manifest_edge_map,
    project_candidate_features,
    source_guard,
)
from shared.edges.phase_d_incremental_candidate_contract import (  # noqa: E402
    BOOTSTRAP_AUTHORITY_ROWS,
)
from shared.edges.runtime_loader import (  # noqa: E402
    load_edge_runtime_from_manifest_entry,
)

PROBE_SCHEMA_VERSION = "phase_d4_candidate_window_semantics_probe_v1"
DEFAULT_STEPS = 8
MAX_STEPS = 256
STRICT_ATOL = 5e-10
STRICT_RTOL = 5e-10
NON_SEMANTIC_POSITION_COLUMNS = {"bar_index"}

DEFAULT_OUTPUT = (
    HERE
    / "config/phase_d/candidate_window_semantics_probe.json"
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return payload


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    return c3cert._normalize(frame)


def _current_event(events: pd.DataFrame, timestamp: Any) -> pd.DataFrame:
    frame = _normalize(events)
    if "Datetime" not in frame.columns:
        return frame.iloc[[]].copy()
    target = pd.Timestamp(timestamp)
    target = (
        target.tz_localize("UTC")
        if target.tzinfo is None
        else target.tz_convert("UTC")
    )
    return frame.loc[frame["Datetime"].eq(target)].reset_index(drop=True)


def _compare_current(
    growing: pd.DataFrame,
    sliding: pd.DataFrame,
    *,
    selected_features: list[str],
    atol: float = STRICT_ATOL,
    rtol: float = STRICT_RTOL,
) -> dict[str, Any]:
    if len(growing) != len(sliding):
        return {
            "identity_ok": False,
            "context_ok": False,
            "reason": "current_event_row_count",
            "growing_rows": int(len(growing)),
            "sliding_rows": int(len(sliding)),
        }
    if growing.empty:
        return {
            "identity_ok": True,
            "context_ok": True,
            "reason": "no_current_event_both",
            "ignored_position_columns": [],
        }

    identity_columns = [
        name
        for name in (
            "Datetime",
            "Bar_Start",
            "edge_id",
            "side",
            "event_id",
            "candidate_event_generator",
        )
        if name in growing.columns or name in sliding.columns
    ]
    identity = c3cert._compare_frames(
        growing,
        sliding,
        atol=atol,
        rtol=rtol,
        required_columns=identity_columns,
    )

    ignored = sorted(
        name
        for name in NON_SEMANTIC_POSITION_COLUMNS
        if (
            name in growing.columns
            and name in sliding.columns
            and name not in selected_features
        )
    )
    common = sorted(set(growing.columns).intersection(sliding.columns))
    context_columns = [name for name in common if name not in ignored]
    context = c3cert._compare_frames(
        growing,
        sliding,
        atol=atol,
        rtol=rtol,
        required_columns=context_columns,
    )
    return {
        "identity_ok": bool(identity.get("ok")),
        "context_ok": bool(context.get("ok")),
        "reason": (
            "exact"
            if identity.get("ok") and context.get("ok")
            else "identity_mismatch"
            if not identity.get("ok")
            else "context_mismatch"
        ),
        "identity": identity,
        "context": context,
        "ignored_position_columns": ignored,
    }


def _group_representatives(
    d1_edges: Mapping[str, Mapping[str, Any]],
    manifest_edges: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for edge_id in sorted(manifest_edges):
        candidate = (d1_edges[edge_id].get("candidate") or {})
        signature = str(candidate.get("candidate_logic_signature") or "").strip()
        if not signature:
            raise RuntimeError(f"{edge_id}: missing candidate_logic_signature")
        grouped[signature].append(edge_id)
    return [sorted(edge_ids)[0] for _, edge_ids in sorted(grouped.items())]


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    array = np.asarray(values, dtype="float64")
    return {
        "count": int(len(values)),
        "mean_ms": float(statistics.mean(values)),
        "p50_ms": float(np.quantile(array, 0.50)),
        "p95_ms": float(np.quantile(array, 0.95)),
        "max_ms": float(np.max(array)),
        "sum_ms": float(np.sum(array)),
    }


def run_probe(
    *,
    root: Path,
    steps: int,
    scope: str,
    output: Path,
) -> dict[str, Any]:
    if steps <= 0 or steps > MAX_STEPS:
        raise RuntimeError(f"steps must be within 1..{MAX_STEPS}")
    if scope not in {"group-representatives", "all-edges"}:
        raise RuntimeError("scope must be group-representatives or all-edges")

    paths = {
        "d1": root / "portfolio_live/config/phase_d/candidate_contract.json",
        "c1": root / "portfolio_live/config/phase_c/feature_contract.json",
        "c2": root / "portfolio_live/config/phase_c/incremental_feature_certification.json",
        "c3": root / "portfolio_live/config/phase_c/consumer_contract.json",
        "manifest": root / "portfolio_live/live_manifest.json",
    }
    for path in paths.values():
        if not path.is_file():
            raise RuntimeError(f"Required authority missing: {path}")

    d1 = load_phase_d1_contract(root, path=paths["d1"])
    c1 = _read_json(paths["c1"])
    c2 = _read_json(paths["c2"])
    c3 = _read_json(paths["c3"])
    manifest = _read_json(paths["manifest"])

    if any(payload.get("status") != "PASS" for payload in (d1, c1, c2, c3)):
        raise RuntimeError("D.1/C.1/C.2/C.3 authorities must all be PASS")

    d1_edges = edge_map(d1)
    c3_edges = edge_map(c3)
    manifest_edges = manifest_edge_map(manifest)
    if set(d1_edges) != set(c3_edges) or set(d1_edges) != set(manifest_edges):
        raise RuntimeError("D.1/C.3/live-manifest edge universe differs")

    guard = source_guard(
        v2_root=root,
        d1_contract=d1,
        c3_contract=c3,
    )
    if guard.get("status") != "PASS":
        raise RuntimeError(
            "D.4.2 source guard failed: "
            + " | ".join(str(x) for x in guard.get("failures") or [])
        )

    selected_edge_ids = (
        sorted(manifest_edges)
        if scope == "all-edges"
        else _group_representatives(d1_edges, manifest_edges)
    )
    selected_assets = sorted(
        {
            str(manifest_edges[edge_id].get("asset") or "").upper()
            for edge_id in selected_edge_ids
        }
    )
    total_rows = int(BOOTSTRAP_AUTHORITY_ROWS + steps)

    asset_frames: dict[str, pd.DataFrame] = {}
    asset_builds: list[dict[str, Any]] = []
    fatal_errors: list[str] = []
    results: dict[str, Any] = {}
    candidate_call_times: list[float] = []

    started_all = time.perf_counter()

    with c3cert._isolated_dependency_authority(root, selected_assets) as sandbox:
        for asset in selected_assets:
            c2_asset = (c2.get("assets") or {}).get(asset) or {}
            representative_id = str(
                c2_asset.get("canonical_core_representative_edge_id") or ""
            )
            if representative_id not in manifest_edges:
                fatal_errors.append(
                    f"{asset}: canonical representative unavailable: "
                    f"{representative_id!r}"
                )
                continue

            runtime = load_edge_runtime_from_manifest_entry(
                manifest_edges[representative_id],
                v2_root=root,
            )
            bars_all = c3cert._load_bars(runtime, v2_root=root)
            if len(bars_all) < total_rows:
                fatal_errors.append(
                    f"{asset}: need >= {total_rows} verified bars, "
                    f"have {len(bars_all)}"
                )
                continue
            bars = bars_all.tail(total_rows).reset_index(drop=True)

            t0 = time.perf_counter()
            try:
                authority = runtime.build_features(
                    bars=bars.copy(),
                    drop_warmup_rows=False,
                )
                state = bootstrap_compact_state(
                    asset=asset,
                    bars=bars,
                    authoritative_features=authority,
                    phase_c1_contract=c1,
                    feature_context_rows=total_rows,
                )
                frame = _normalize(state["feature_tail"]).tail(
                    total_rows
                ).reset_index(drop=True)
            except Exception as exc:
                fatal_errors.append(
                    f"{asset}: extended canonical frame build failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            build_ms = (time.perf_counter() - t0) * 1000.0
            if len(frame) != total_rows:
                fatal_errors.append(
                    f"{asset}: extended canonical frame rows "
                    f"{len(frame)} != {total_rows}"
                )
                continue
            asset_frames[asset] = frame
            asset_builds.append(
                {
                    "asset": asset,
                    "representative_edge_id": representative_id,
                    "rows": int(len(frame)),
                    "build_ms": build_ms,
                }
            )

        for edge_id in selected_edge_ids:
            edge = manifest_edges[edge_id]
            d1_edge = d1_edges[edge_id]
            c3_edge = c3_edges[edge_id]
            asset = str(edge.get("asset") or "").upper()
            if asset not in asset_frames:
                continue

            runtime = load_edge_runtime_from_manifest_entry(
                edge,
                v2_root=root,
            )
            selected_features = [
                str(value) for value in c3_edge.get("selected_features") or []
            ]
            candidate = d1_edge.get("candidate") or {}
            signature = str(candidate.get("candidate_logic_signature") or "")

            try:
                extended = project_candidate_features(
                    ring=asset_frames[asset],
                    edge={
                        **edge,
                        "side_value": getattr(
                            runtime,
                            "SIDE_VALUE",
                            edge.get("side_value"),
                        ),
                    },
                    d1_edge=d1_edge,
                )
                step_rows: list[dict[str, Any]] = []
                identity_mismatches = 0
                context_mismatches = 0

                for step in range(1, steps + 1):
                    endpoint = BOOTSTRAP_AUTHORITY_ROWS + step
                    growing = extended.iloc[:endpoint].reset_index(drop=True)
                    sliding = extended.iloc[
                        endpoint - BOOTSTRAP_AUTHORITY_ROWS : endpoint
                    ].reset_index(drop=True)
                    current_ts = pd.Timestamp(extended.iloc[endpoint - 1]["Datetime"])

                    growing_events, growing_ms = d2cert._timed_candidate(
                        runtime,
                        growing,
                        selected_features=selected_features,
                    )
                    sliding_events, sliding_ms = d2cert._timed_candidate(
                        runtime,
                        sliding,
                        selected_features=selected_features,
                    )
                    candidate_call_times.extend([growing_ms, sliding_ms])

                    growing_current = _current_event(
                        growing_events,
                        current_ts,
                    )
                    sliding_current = _current_event(
                        sliding_events,
                        current_ts,
                    )
                    parity = _compare_current(
                        growing_current,
                        sliding_current,
                        selected_features=selected_features,
                    )
                    if not parity["identity_ok"]:
                        identity_mismatches += 1
                    elif not parity["context_ok"]:
                        context_mismatches += 1

                    step_rows.append(
                        {
                            "step": step,
                            "timestamp": current_ts.isoformat(),
                            "growing_rows": int(len(growing)),
                            "sliding_rows": int(len(sliding)),
                            "growing_current_event": bool(
                                not growing_current.empty
                            ),
                            "sliding_current_event": bool(
                                not sliding_current.empty
                            ),
                            "growing_candidate_ms": growing_ms,
                            "sliding_candidate_ms": sliding_ms,
                            "parity": parity,
                        }
                    )

                if identity_mismatches:
                    classification = "CURRENT_CANDIDATE_IDENTITY_DIVERGES"
                elif context_mismatches:
                    classification = "CURRENT_EVENT_CONTEXT_DIVERGES_ONLY"
                else:
                    classification = (
                        "PERSISTENT_BOOTSTRAP_COMPATIBLE_ON_PROBE"
                    )

                results[edge_id] = {
                    "status": "PASS",
                    "asset": asset,
                    "candidate_logic_signature": signature,
                    "classification": classification,
                    "steps_checked": steps,
                    "identity_mismatch_steps": identity_mismatches,
                    "context_mismatch_steps": context_mismatches,
                    "step_results": step_rows,
                }
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                results[edge_id] = {
                    "status": "FAIL",
                    "asset": asset,
                    "candidate_logic_signature": signature,
                    "error": message,
                }
                fatal_errors.append(f"{edge_id}: {message}")

        sandbox_meta = {
            "enabled": True,
            "seeded_file_count": int(sandbox["seeded_file_count"]),
            "production_continuation_mutated": False,
        }

    group_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge_id, row in results.items():
        signature = str(row.get("candidate_logic_signature") or "")
        if signature:
            group_rows[signature].append({"edge_id": edge_id, **row})

    group_summary: list[dict[str, Any]] = []
    for signature, rows in sorted(group_rows.items()):
        classifications = sorted(
            {str(row.get("classification") or "ERROR") for row in rows}
        )
        if any(row.get("status") != "PASS" for row in rows):
            group_classification = "PROBE_ERROR"
        elif "CURRENT_CANDIDATE_IDENTITY_DIVERGES" in classifications:
            group_classification = "SLIDING_WINDOW_SENSITIVE"
        elif "CURRENT_EVENT_CONTEXT_DIVERGES_ONLY" in classifications:
            group_classification = "EVENT_CONTEXT_WINDOW_SENSITIVE"
        else:
            group_classification = "PERSISTENT_BOOTSTRAP_COMPATIBLE_ON_PROBE"
        group_summary.append(
            {
                "candidate_logic_signature": signature,
                "edge_ids_checked": sorted(row["edge_id"] for row in rows),
                "edge_count_checked": len(rows),
                "classification": group_classification,
                "edge_classifications": classifications,
            }
        )

    report = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": "PASS" if not fatal_errors else "FAIL",
        "production_routing_enabled": False,
        "production_activation_allowed": False,
        "probe_role": "architecture_classification_not_production_certificate",
        "scope": scope,
        "bootstrap_authority_rows": BOOTSTRAP_AUTHORITY_ROWS,
        "steps": steps,
        "selected_edge_count": len(selected_edge_ids),
        "selected_asset_count": len(selected_assets),
        "selected_edge_ids": selected_edge_ids,
        "asset_builds": asset_builds,
        "results": results,
        "candidate_logic_group_summary": group_summary,
        "candidate_call_timing": _summary(candidate_call_times),
        "dependency_sandbox": sandbox_meta,
        "fatal_errors": fatal_errors,
        "elapsed_seconds": time.perf_counter() - started_all,
        "interpretation": {
            "persistent_bootstrap_compatible": (
                "No current-event identity/context divergence was observed "
                "between growing bootstrap history and the current sliding "
                "6000-row live-style oracle across the probed steps."
            ),
            "sliding_window_sensitive": (
                "At least one current candidate identity diverged. A provider "
                "for this logic cannot assume unbounded persistent state; it "
                "needs an exact bounded/sliding formulation or another proven "
                "authority-preserving design."
            ),
            "not_a_certificate": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=V2_ROOT)
    parser.add_argument("--steps", type=int, default=DEFAULT_STEPS)
    parser.add_argument(
        "--scope",
        choices=("group-representatives", "all-edges"),
        default="group-representatives",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    root = Path(args.v2_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = (root / output).resolve()

    print("=" * 112)
    print("PHASE D.4.2 — CANDIDATE WINDOW-SEMANTICS AUTHORITY PROBE")
    print("=" * 112)
    print("V2 root:                  ", root)
    print("Scope:                    ", args.scope)
    print("Bootstrap/live window:    ", BOOTSTRAP_AUTHORITY_ROWS)
    print("Sequential steps:         ", args.steps)
    print("Research source modified: NO")
    print("Dashboard modified:       NO")
    print("Production routing:       NO")
    print("Activation allowed:       NO")
    print()

    report = run_probe(
        root=root,
        steps=int(args.steps),
        scope=str(args.scope),
        output=output,
    )

    print()
    print("--- LOGIC GROUP CLASSIFICATION ---")
    for row in report.get("candidate_logic_group_summary") or []:
        print(
            f"{row['candidate_logic_signature'][:12]} "
            f"edges={row['edge_count_checked']:2d} "
            f"{row['classification']}"
        )

    print()
    print("--- TIMING ---")
    for key, value in (report.get("candidate_call_timing") or {}).items():
        print(f"{key}: {value}")

    print()
    print("--- FATAL ERRORS ---")
    errors = report.get("fatal_errors") or []
    if errors:
        for error in errors:
            print(" -", error)
    else:
        print(" none")

    print()
    print("Status:", report.get("status"))
    print("Output:", output)
    print()
    print(
        "This is an architecture probe only. It does not certify or enable a "
        "continuation provider."
    )
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
