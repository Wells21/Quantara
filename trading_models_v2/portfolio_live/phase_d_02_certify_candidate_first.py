from __future__ import annotations

"""Phase D.2 offline candidate-first narrow-projection certification.

This script does NOT install production routing.

For each selected asset it reconstructs one exact 6,000-row canonical Phase C
feature ring, then for every selected edge compares the unchanged runtime
candidate generator on:

    current full C.3 projection
        versus
    Phase D.1 source-derived narrow candidate projection

Candidate/event parity must be exact within the same strict numeric tolerance
used by Phase C. Candidate timing and projection timing are reported but do not
change the trading contract.
"""

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
for path in (V2_ROOT, V2_ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.phase_c_consumer_adapter import strip_selected_feature_collisions
from shared.edges.phase_c_incremental_feature_engine import bootstrap_compact_state
from shared.edges.phase_d_candidate_first import (
    D2_ENGINE_VERSION,
    edge_map,
    load_phase_d1_contract,
    manifest_edge_map,
    project_candidate_features,
    project_full_c3_features,
    projection_reduction_record,
    source_guard,
)
from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry

import phase_c_03_certify_consumer_contract as c3cert


DEFAULT_D1 = HERE / "config" / "phase_d" / "candidate_contract.json"
DEFAULT_C1 = HERE / "config" / "phase_c" / "feature_contract.json"
DEFAULT_C2 = HERE / "config" / "phase_c" / "incremental_feature_certification.json"
DEFAULT_C3 = HERE / "config" / "phase_c" / "consumer_contract.json"
DEFAULT_MANIFEST = HERE / "live_manifest.json"
DEFAULT_OUTPUT = HERE / "config" / "phase_d" / "candidate_first_certification.json"

CERT_SCHEMA_VERSION = "phase_d2_candidate_first_certification_v1"
DEFAULT_ROWS = 6000
DEFAULT_ATOL = 5e-10
DEFAULT_RTOL = 5e-10


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    return c3cert._normalize(frame)


def _sanitize(
    events: pd.DataFrame,
    *,
    selected_features: list[str],
) -> pd.DataFrame:
    sanitized, _ = strip_selected_feature_collisions(
        events=events,
        selected_features=selected_features,
    )
    return _normalize(sanitized)


def _timed_candidate(
    runtime: Any,
    features: pd.DataFrame,
    *,
    selected_features: list[str],
) -> tuple[pd.DataFrame, float]:
    before = c3cert._frame_digest(features)
    started = time.perf_counter()
    events = runtime.generate_candidate_events(features=features)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    after = c3cert._frame_digest(features)
    if before != after:
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: candidate generator mutated input"
        )
    if not isinstance(events, pd.DataFrame):
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: candidate generator did not return DataFrame"
        )
    return _sanitize(events, selected_features=selected_features), elapsed_ms


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.quantile(np.asarray(values, dtype="float64"), q))


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean_ms": float(statistics.mean(values)),
        "p50_ms": _quantile(values, 0.50),
        "p95_ms": _quantile(values, 0.95),
        "max_ms": float(max(values)),
        "sum_ms": float(sum(values)),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=V2_ROOT)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--atol", type=float, default=DEFAULT_ATOL)
    parser.add_argument("--rtol", type=float, default=DEFAULT_RTOL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(args.v2_root).resolve()
    rows = int(args.rows)
    if rows != DEFAULT_ROWS:
        raise RuntimeError(
            f"Phase D.2 certification requires exact {DEFAULT_ROWS}-row C.3 authority"
        )
    if args.atol > DEFAULT_ATOL or args.rtol > DEFAULT_RTOL:
        raise RuntimeError("Phase D.2 tolerance may not be weaker than 5e-10")

    paths = {
        "d1": root / "portfolio_live/config/phase_d/candidate_contract.json",
        "c1": root / "portfolio_live/config/phase_c/feature_contract.json",
        "c2": root / "portfolio_live/config/phase_c/incremental_feature_certification.json",
        "c3": root / "portfolio_live/config/phase_c/consumer_contract.json",
        "manifest": root / "portfolio_live/live_manifest.json",
        "d2_engine": root / "shared/edges/phase_d_candidate_first.py",
        "d1_compiler": root / "shared/edges/phase_d_candidate_contract.py",
        "c3_certifier": root / "portfolio_live/phase_c_03_certify_consumer_contract.py",
    }
    for path in paths.values():
        if not path.is_file():
            raise RuntimeError(f"Required file missing: {path}")

    d1 = load_phase_d1_contract(root, path=paths["d1"])
    c1 = _read_json(paths["c1"])
    c2 = _read_json(paths["c2"])
    c3 = _read_json(paths["c3"])
    manifest = _read_json(paths["manifest"])

    d1_edges = edge_map(d1)
    c3_edges = edge_map(c3)
    manifest_edges = manifest_edge_map(manifest)

    if set(d1_edges) != set(c3_edges) or set(d1_edges) != set(manifest_edges):
        raise RuntimeError("D.1/C.3/live-manifest selected-edge universe differs")
    if c1.get("status") != "PASS" or c2.get("status") != "PASS" or c3.get("status") != "PASS":
        raise RuntimeError("Phase C frozen authorities must all be PASS")
    # SINGLE_OPERATOR_AUTHORITY: historical SHA equality is audit-only.

    source = source_guard(
        v2_root=root,
        d1_contract=d1,
        c3_contract=c3,
    )
    if source.get("status") != "PASS":
        raise RuntimeError(
            "Phase D.2 source guard failed: "
            + " | ".join(str(x) for x in source.get("failures") or [])
        )

    assets = sorted(
        {
            str(row.get("asset") or "").upper()
            for row in manifest_edges.values()
            if str(row.get("asset") or "")
        }
    )

    results: dict[str, Any] = {}
    asset_builds: list[dict[str, Any]] = []
    fatal_errors: list[str] = []
    full_projection_times: list[float] = []
    narrow_projection_times: list[float] = []
    full_candidate_times: list[float] = []
    narrow_candidate_times: list[float] = []
    reduction_records: list[dict[str, Any]] = []

    print("=" * 112)
    print("PHASE D.2 — CANDIDATE-FIRST NARROW PROJECTION CERTIFICATION")
    print("=" * 112)
    print(f"V2 root:       {root}")
    print(f"Assets:        {len(assets)}")
    print(f"Edges:         {len(manifest_edges)}")
    print(f"Rows:          {rows} exact C.3 authority")
    print(f"Tolerance:     atol={args.atol} rtol={args.rtol}")
    print("Routing live:  False")
    print("Dependency authority sandbox: ON")

    with c3cert._isolated_dependency_authority(root, assets) as sandbox:
        for asset in assets:
            asset_edges = [
                edge_id
                for edge_id in sorted(manifest_edges)
                if str(manifest_edges[edge_id].get("asset") or "").upper() == asset
            ]
            c2_asset = (c2.get("assets") or {}).get(asset) or {}
            representative_id = str(
                c2_asset.get("canonical_core_representative_edge_id") or ""
            )
            if representative_id not in manifest_edges:
                fatal_errors.append(
                    f"{asset}: C.2 canonical representative unavailable: {representative_id!r}"
                )
                continue

            rep_runtime = load_edge_runtime_from_manifest_entry(
                manifest_edges[representative_id],
                v2_root=root,
            )
            bars_all = c3cert._load_bars(rep_runtime, v2_root=root)
            if len(bars_all) < rows:
                fatal_errors.append(
                    f"{asset}: need >= {rows} verified bars, have {len(bars_all)}"
                )
                continue
            bars = bars_all.tail(rows).reset_index(drop=True)

            build_start = time.perf_counter()
            try:
                authority = rep_runtime.build_features(
                    bars=bars.copy(),
                    drop_warmup_rows=False,
                )
                state = bootstrap_compact_state(
                    asset=asset,
                    bars=bars,
                    authoritative_features=authority,
                    phase_c1_contract=c1,
                    feature_context_rows=rows,
                )
                ring = _normalize(state["feature_tail"]).tail(rows).reset_index(drop=True)
            except Exception as exc:
                fatal_errors.append(
                    f"{asset}: canonical ring build failed: {type(exc).__name__}: {exc}"
                )
                continue
            build_ms = (time.perf_counter() - build_start) * 1000.0
            asset_builds.append(
                {
                    "asset": asset,
                    "representative_edge_id": representative_id,
                    "rows": len(ring),
                    "build_ms": build_ms,
                }
            )

            print(f"\n--- {asset} ---")
            print(
                f"canonical_ring rep={representative_id} rows={len(ring)} "
                f"build_ms={build_ms:.1f}"
            )

            for index, edge_id in enumerate(asset_edges):
                edge = manifest_edges[edge_id]
                d1_edge = d1_edges[edge_id]
                c3_edge = c3_edges[edge_id]
                runtime = load_edge_runtime_from_manifest_entry(edge, v2_root=root)
                selected_features = [
                    str(x) for x in c3_edge.get("selected_features") or []
                ]

                try:
                    t0 = time.perf_counter()
                    full = project_full_c3_features(
                        ring=ring,
                        edge={
                            **edge,
                            "side_value": getattr(
                                runtime, "SIDE_VALUE", edge.get("side_value")
                            ),
                        },
                        d1_edge=d1_edge,
                        c3_edge=c3_edge,
                    )
                    full_projection_ms = (time.perf_counter() - t0) * 1000.0

                    t0 = time.perf_counter()
                    narrow = project_candidate_features(
                        ring=ring,
                        edge={
                            **edge,
                            "side_value": getattr(
                                runtime, "SIDE_VALUE", edge.get("side_value")
                            ),
                        },
                        d1_edge=d1_edge,
                    )
                    narrow_projection_ms = (time.perf_counter() - t0) * 1000.0

                    # Alternate order to avoid a systematic warm-cache advantage.
                    if index % 2 == 0:
                        full_events, full_candidate_ms = _timed_candidate(
                            runtime,
                            full,
                            selected_features=selected_features,
                        )
                        narrow_events, narrow_candidate_ms = _timed_candidate(
                            runtime,
                            narrow,
                            selected_features=selected_features,
                        )
                    else:
                        narrow_events, narrow_candidate_ms = _timed_candidate(
                            runtime,
                            narrow,
                            selected_features=selected_features,
                        )
                        full_events, full_candidate_ms = _timed_candidate(
                            runtime,
                            full,
                            selected_features=selected_features,
                        )

                    parity = c3cert._compare_frames(
                        full_events,
                        narrow_events,
                        atol=float(args.atol),
                        rtol=float(args.rtol),
                    )
                    reduction = projection_reduction_record(
                        d1_edge=d1_edge,
                        c3_edge=c3_edge,
                    )
                    reduction_records.append(reduction)

                    result = {
                        "status": "PASS" if parity.get("ok") else "FAIL",
                        "parity": parity,
                        "full_event_rows": int(len(full_events)),
                        "narrow_event_rows": int(len(narrow_events)),
                        "full_projection_ms": full_projection_ms,
                        "narrow_projection_ms": narrow_projection_ms,
                        "full_candidate_ms": full_candidate_ms,
                        "narrow_candidate_ms": narrow_candidate_ms,
                        "full_total_ms": full_projection_ms + full_candidate_ms,
                        "narrow_total_ms": narrow_projection_ms + narrow_candidate_ms,
                        "projection_reduction": reduction,
                    }
                    results[edge_id] = result

                    full_projection_times.append(full_projection_ms)
                    narrow_projection_times.append(narrow_projection_ms)
                    full_candidate_times.append(full_candidate_ms)
                    narrow_candidate_times.append(narrow_candidate_ms)

                    print(
                        f"{edge_id:<52} {result['status']} "
                        f"cols={reduction['candidate_projection_columns_including_identity']}/"
                        f"{reduction['full_c3_projection_columns']} "
                        f"events={len(full_events)} "
                        f"full_ms={result['full_total_ms']:.1f} "
                        f"narrow_ms={result['narrow_total_ms']:.1f}"
                    )
                except Exception as exc:
                    results[edge_id] = {
                        "status": "FAIL",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    fatal_errors.append(
                        f"{edge_id}: {type(exc).__name__}: {exc}"
                    )
                    print(f"{edge_id:<52} FAIL {type(exc).__name__}: {exc}")

        sandbox_meta = {
            "enabled": True,
            "seeded_file_count": int(sandbox["seeded_file_count"]),
            "production_continuation_mutated": False,
        }

    failed_edges = sorted(
        edge_id
        for edge_id, row in results.items()
        if row.get("status") != "PASS"
    )
    passed_edges = sorted(
        edge_id
        for edge_id, row in results.items()
        if row.get("status") == "PASS"
    )

    full_total = [
        float(row["full_total_ms"])
        for row in results.values()
        if row.get("status") == "PASS"
    ]
    narrow_total = [
        float(row["narrow_total_ms"])
        for row in results.values()
        if row.get("status") == "PASS"
    ]
    full_sum = sum(full_total)
    narrow_sum = sum(narrow_total)

    report = {
        "schema_version": CERT_SCHEMA_VERSION,
        "engine_version": D2_ENGINE_VERSION,
        "status": (
            "PASS"
            if not fatal_errors
            and not failed_edges
            and len(passed_edges) == len(manifest_edges)
            else "FAIL"
        ),
        "runtime_routing_enabled": False,
        "rows": rows,
        "strict_atol": float(args.atol),
        "strict_rtol": float(args.rtol),
        "selected_assets": assets,
        "selected_edge_count": len(manifest_edges),
        "edges_checked": len(results),
        "edges_passed": len(passed_edges),
        "edges_failed": failed_edges,
        "phase_d1_contract_sha256": d1.get("contract_sha256"),
        "phase_c1_contract_sha256": c1.get("contract_sha256"),
        "phase_c2_engine_certification_status": c2.get("status"),
        "phase_c3_contract_sha256": c3.get("contract_sha256"),
        "implementation_fingerprints": {
            name: _sha256(path)
            for name, path in paths.items()
            if name in {"d2_engine", "d1_compiler", "c3_certifier"}
        },
        "source_guard": source,
        "dependency_authority_sandbox": sandbox_meta,
        "asset_canonical_ring_builds": asset_builds,
        "projection_column_reduction": {
            "records": reduction_records,
            "mean_full_to_candidate_ratio": (
                float(statistics.mean(
                    row["full_to_candidate_column_ratio"]
                    for row in reduction_records
                ))
                if reduction_records else None
            ),
            "median_full_to_candidate_ratio": (
                float(statistics.median(
                    row["full_to_candidate_column_ratio"]
                    for row in reduction_records
                ))
                if reduction_records else None
            ),
        },
        "timing": {
            "full_projection": _summary(full_projection_times),
            "narrow_projection": _summary(narrow_projection_times),
            "full_candidate": _summary(full_candidate_times),
            "narrow_candidate": _summary(narrow_candidate_times),
            "full_total": _summary(full_total),
            "narrow_total": _summary(narrow_total),
            "aggregate_full_to_narrow_speedup_ratio": (
                float(full_sum / narrow_sum) if narrow_sum > 0 else None
            ),
        },
        "edges": results,
        "fatal_errors": fatal_errors,
        "certification_statement": (
            "PASS means the unchanged candidate generator produced the same "
            "sanitized candidate/event table from the D.1 narrow projection "
            "as from the current full C.3 projection for every selected edge."
        ),
        "next_stage": {
            "name": "D3_PRODUCTION_CANDIDATE_FIRST_ROUTING",
            "allowed_only_if_status_pass": True,
            "production_routing_enabled_here": False,
        },
    }

    output = Path(args.output)
    if not output.is_absolute():
        output = (root / output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print("\n" + "=" * 112)
    print("PHASE D.2 SUMMARY")
    print("=" * 112)
    print(f"Status:                    {report['status']}")
    print(f"Edges passed:              {len(passed_edges)}/{len(manifest_edges)}")
    print(
        "Median column ratio:     "
        f"{report['projection_column_reduction']['median_full_to_candidate_ratio']}"
    )
    print(
        "Aggregate speedup ratio: "
        f"{report['timing']['aggregate_full_to_narrow_speedup_ratio']}"
    )
    print(f"Output:                    {output}")
    print("Production routing enabled: False")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
