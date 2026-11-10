from __future__ import annotations

"""Offline Phase D.3 production-routing certification.

D.2 already proves 74/74 unchanged candidate-event parity from the corrected
narrow consumer contract. D.3 certifies the remaining integration boundary:

1. current D.1/D.2/C.3/C.4 source authority is still exact;
2. D.3 deferred full projection is byte/numeric equivalent to the currently
   certified C.4 full projection for every selected edge over the exact 6,000
   row canonical authority;
3. current C.5 model/decision parity still passes for every selected edge;
4. D.3 source self-tests pass.

No production routing is enabled by the default run. --seal-production writes
a local activation artifact only after a clean v2 release has a PASS report.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
REPO_ROOT = V2_ROOT.parent
for path in (V2_ROOT, HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import phase_c_04_production_router as c4
import phase_d_03_production_router as d3
from shared.edges.phase_d_candidate_first import (
    edge_map,
    load_phase_d1_contract,
    project_full_c3_features,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry

DEFAULT_OUTPUT = (
    HERE / "config" / "phase_d" / "candidate_first_routing_certification.json"
)
STRICT_ATOL = 5e-10
STRICT_RTOL = 5e-10


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            + (proc.stderr.strip() or proc.stdout.strip())
        )
    return proc.stdout.strip()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "Datetime" in out.columns:
        out["Datetime"] = to_datetime_iso(
            out["Datetime"],
            utc=True,
            errors="coerce",
        )
        out = (
            out.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .reset_index(drop=True)
        )
    return out


def _compare_exact_projection(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    c3cert: Any,
) -> dict[str, Any]:
    if list(left.columns) != list(right.columns):
        return {
            "ok": False,
            "reason": "column_order",
            "left_columns": list(left.columns),
            "right_columns": list(right.columns),
        }
    return c3cert._compare_frames(
        left,
        right,
        atol=STRICT_ATOL,
        rtol=STRICT_RTOL,
        required_columns=list(left.columns),
    )


def _canonical_asset_rings(
    *,
    root: Path,
    manifest_edges: Mapping[str, Mapping[str, Any]],
    c2: Mapping[str, Any],
    c1: Mapping[str, Any],
    assets: list[str],
    c3cert: Any,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, Any]]]:
    rings: dict[str, pd.DataFrame] = {}
    builds: list[dict[str, Any]] = []

    with c3cert._isolated_dependency_authority(root, assets):
        for asset in assets:
            c2_asset = (c2.get("assets") or {}).get(asset) or {}
            representative = str(
                c2_asset.get("canonical_core_representative_edge_id") or ""
            )
            edge = manifest_edges.get(representative)
            if not edge:
                raise RuntimeError(
                    f"{asset}: C.2 representative unavailable: {representative}"
                )
            runtime = load_edge_runtime_from_manifest_entry(
                edge,
                v2_root=root,
            )
            bars = c3cert._load_bars(runtime, v2_root=root)
            if len(bars) < c4.CERTIFIED_CONSUMER_RING_ROWS:
                raise RuntimeError(
                    f"{asset}: insufficient bars for 6000-row authority"
                )
            bars = (
                bars.tail(c4.CERTIFIED_CONSUMER_RING_ROWS)
                .reset_index(drop=True)
            )

            started = time.perf_counter()
            authority = runtime.build_features(
                bars=bars.copy(),
                drop_warmup_rows=False,
            )
            full_state = c3cert.bootstrap_compact_state(
                asset=asset,
                bars=bars,
                authoritative_features=authority,
                phase_c1_contract=c1,
                feature_context_rows=c4.CERTIFIED_CONSUMER_RING_ROWS,
            )
            ring = _normalize(
                full_state["feature_tail"]
            ).tail(c4.CERTIFIED_CONSUMER_RING_ROWS)
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            rings[asset] = ring.reset_index(drop=True)
            builds.append(
                {
                    "asset": asset,
                    "representative_edge_id": representative,
                    "rows": int(len(ring)),
                    "build_ms": elapsed_ms,
                }
            )
            print(
                f"canonical_ring asset={asset} "
                f"rep={representative} rows={len(ring)} "
                f"build_ms={elapsed_ms:.1f}"
            )
    return rings, builds


def _current_c5_model_decision_parity() -> dict[str, Any]:
    c5 = _load_module(
        HERE / "phase_c_05_certify_production_routing.py",
        "phase_d3_c5_parity",
    )
    result = c5._model_decision_parity()
    if not isinstance(result, dict):
        raise RuntimeError("C.5 model-decision parity returned non-object")
    return result


def certify(
    *,
    root: Path,
    output: Path,
) -> dict[str, Any]:
    root = Path(root).resolve()
    static = d3.static_contract_report(root)
    if static.get("status") != "PASS":
        report = {
            "schema_version": d3.D3_CERTIFICATION_SCHEMA_VERSION,
            "status": "FAIL",
            "runtime_routing_enabled": False,
            "production_activation_allowed": False,
            "fatal_errors": list(static.get("failures") or []),
            "static_contract": static,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return report

    d1 = load_phase_d1_contract(root)
    d2 = _read_json(
        root / "portfolio_live/config/phase_d/candidate_first_certification.json"
    )
    c1 = _read_json(
        root / "portfolio_live/config/phase_c/feature_contract.json"
    )
    c2 = _read_json(
        root / "portfolio_live/config/phase_c/incremental_feature_certification.json"
    )
    c3 = _read_json(
        root / "portfolio_live/config/phase_c/consumer_contract.json"
    )
    manifest = _read_json(root / "portfolio_live/live_manifest.json")

    manifest_edges = {
        str(row["edge_id"]): dict(row)
        for row in manifest.get("selected_edges") or []
    }
    d1_edges = edge_map(d1)
    c3_edges = edge_map(c3)
    assets = list(static.get("selected_assets") or [])

    c3cert = _load_module(
        HERE / "phase_c_03_certify_consumer_contract.py",
        "phase_d3_c3_helpers",
    )

    fatal_errors: list[str] = []
    projection_results: dict[str, Any] = {}
    asset_builds: list[dict[str, Any]] = []

    try:
        rings, asset_builds = _canonical_asset_rings(
            root=root,
            manifest_edges=manifest_edges,
            c2=c2,
            c1=c1,
            assets=assets,
            c3cert=c3cert,
        )

        for edge_id in sorted(manifest_edges):
            try:
                edge = manifest_edges[edge_id]
                asset = str(edge.get("asset") or "").upper()
                ring = rings[asset]
                runtime = load_edge_runtime_from_manifest_entry(
                    edge,
                    v2_root=root,
                )
                projected_edge = dict(edge)
                projected_edge["side_value"] = getattr(
                    runtime,
                    "SIDE_VALUE",
                    edge.get("side_value"),
                )

                current_c4 = c4._project_edge_features(
                    ring=ring,
                    edge=projected_edge,
                    edge_contract=c3_edges[edge_id],
                )
                deferred_d3 = project_full_c3_features(
                    ring=ring,
                    edge=projected_edge,
                    d1_edge=d1_edges[edge_id],
                    c3_edge=c3_edges[edge_id],
                )
                parity = _compare_exact_projection(
                    current_c4,
                    deferred_d3,
                    c3cert=c3cert,
                )
                projection_results[edge_id] = {
                    "status": "PASS" if parity.get("ok") else "FAIL",
                    "parity": parity,
                    "rows": int(len(current_c4)),
                    "columns": int(len(current_c4.columns)),
                }
                print(
                    f"{edge_id:<52} "
                    f"{projection_results[edge_id]['status']} "
                    f"rows={len(current_c4)} cols={len(current_c4.columns)}"
                )
            except Exception as exc:
                projection_results[edge_id] = {
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                fatal_errors.append(
                    f"{edge_id}: {type(exc).__name__}: {exc}"
                )
                print(
                    f"{edge_id:<52} FAIL "
                    f"{type(exc).__name__}: {exc}"
                )
    except Exception as exc:
        fatal_errors.append(
            f"canonical_authority: {type(exc).__name__}: {exc}"
        )

    failed_projection_edges = sorted(
        edge_id
        for edge_id, row in projection_results.items()
        if row.get("status") != "PASS"
    )

    c5_parity: dict[str, Any]
    try:
        print("\n--- CURRENT C.5 MODEL / DECISION PARITY ---")
        c5_parity = _current_c5_model_decision_parity()
    except Exception as exc:
        c5_parity = {
            "status": "FAIL",
            "fatal_error": f"{type(exc).__name__}: {exc}",
        }
        fatal_errors.append(
            f"c5_model_decision_parity: {type(exc).__name__}: {exc}"
        )

    self_test = d3.self_test()
    if self_test.get("status") != "PASS":
        fatal_errors.extend(
            "self_test:" + str(x)
            for x in self_test.get("failed") or []
        )

    d2_candidate_authority_ok = (
        d2.get("status") == "PASS"
        and int(d2.get("edges_passed") or -1)
        == len(manifest_edges)
        and not list(d2.get("edges_failed") or [])
        and not list(d2.get("fatal_errors") or [])
    )
    if not d2_candidate_authority_ok:
        fatal_errors.append(
            "D.2 candidate-event authority is not all-edge PASS"
        )

    c5_status = str(c5_parity.get("status") or "")
    if c5_status != "PASS":
        fatal_errors.append(
            "current C.5 model/decision parity is not PASS"
        )

    passed_projection_edges = sorted(
        edge_id
        for edge_id, row in projection_results.items()
        if row.get("status") == "PASS"
    )

    status = (
        "PASS"
        if not fatal_errors
        and not failed_projection_edges
        and len(passed_projection_edges) == len(manifest_edges)
        and d2_candidate_authority_ok
        and c5_status == "PASS"
        else "FAIL"
    )

    report = {
        "schema_version": d3.D3_CERTIFICATION_SCHEMA_VERSION,
        "status": status,
        "runtime_routing_enabled": False,
        "production_activation_allowed": status == "PASS",
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "router_version": d3.D3_ROUTER_VERSION,
        "d1_contract_sha256": d1.get("contract_sha256"),
        "d2_certification_file_sha256": _sha256(
            root
            / "portfolio_live/config/phase_d/candidate_first_certification.json"
        ),
        "d2_candidate_authority": {
            "status": d2.get("status"),
            "edges_checked": d2.get("edges_checked"),
            "edges_passed": d2.get("edges_passed"),
            "edges_failed": d2.get("edges_failed"),
            "aggregate_full_to_narrow_speedup_ratio": (
                (d2.get("timing") or {}).get(
                    "aggregate_full_to_narrow_speedup_ratio"
                )
            ),
        },
        "c3_contract_sha256": c3.get("contract_sha256"),
        "selected_assets": assets,
        "selected_edge_count": len(manifest_edges),
        "deferred_projection_edges_checked": len(projection_results),
        "deferred_projection_edges_passed": len(
            passed_projection_edges
        ),
        "deferred_projection_edges_failed": failed_projection_edges,
        "deferred_projection_results": projection_results,
        "asset_canonical_ring_builds": asset_builds,
        "c5_model_decision_parity": c5_parity,
        "d3_self_test": self_test,
        "static_contract": static,
        "implementation_fingerprints": {
            "d3_router": _sha256(
                root / "portfolio_live/phase_d_03_production_router.py"
            ),
            "d2_engine": _sha256(
                root / "shared/edges/phase_d_candidate_first.py"
            ),
            "c4_router": _sha256(
                root / "portfolio_live/phase_c_04_production_router.py"
            ),
            "dashboard": _sha256(
                root
                / "portfolio_live/portfolio_live_dashboard_orchestrator.py"
            ),
            "d3_certifier": _sha256(Path(__file__).resolve()),
        },
        "fatal_errors": fatal_errors,
        "certification_statement": (
            "PASS composes three exact authorities: D.2 unchanged candidate "
            "parity for every selected edge; D.3 deferred full projection "
            "equals the currently certified C.4 full projection for every "
            "selected edge over the exact 6000-row authority; and current "
            "C.5 model/decision parity remains PASS."
        ),
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
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--seal-production",
        action="store_true",
        help=(
            "After PASS, write the local D.3 activation artifact. "
            "Git branch/HEAD/clean-tree SHA identity is not a gate."
        ),
    )
    args = parser.parse_args()

    root = Path(args.v2_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = (root / output).resolve()

    print("=" * 112)
    print("PHASE D.3 — CANDIDATE-FIRST CURRENT-STATE ROUTING CERTIFICATION")
    print("=" * 112)
    print(f"V2 root: {root}")
    print(f"Output:  {output}")
    print("Identity mode: structural current state; Git/file SHA equality is NOT a gate")
    print("Production routing enabled during certification: False")

    report = certify(root=root, output=output)
    report["authority_identity_mode"] = (
        "structural_current_state_no_git_or_file_sha_gate"
    )
    report.pop("release_head_sha", None)
    report.pop("implementation_fingerprints", None)
    report.pop("d1_contract_sha256", None)
    report.pop("d2_certification_file_sha256", None)
    report.pop("c3_contract_sha256", None)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print("\n" + "=" * 112)
    print("PHASE D.3 SUMMARY")
    print("=" * 112)
    print("Status:                         ", report.get("status"))
    print(
        "Deferred projection edges:      ",
        f"{report.get('deferred_projection_edges_passed')}/"
        f"{report.get('selected_edge_count')}",
    )
    print(
        "D.2 candidate authority:         ",
        (report.get("d2_candidate_authority") or {}).get("status"),
    )
    print(
        "C.5 model/decision parity:       ",
        (report.get("c5_model_decision_parity") or {}).get("status"),
    )
    print("Fatal errors:                    ", len(report.get("fatal_errors") or []))
    print("Production routing enabled:      False")

    if report.get("status") != "PASS":
        return 1

    if args.seal_production:
        activation = d3.build_activation_payload(
            v2_root=root,
            certification_path=output,
        )
        d3.D3_ACTIVATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        d3.D3_ACTIVATION_PATH.write_text(
            json.dumps(
                activation,
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        print("D.3 activation written:", d3.D3_ACTIVATION_PATH)
        print("Authority identity mode: structural current state")

    return 0



if __name__ == "__main__":
    raise SystemExit(main())
