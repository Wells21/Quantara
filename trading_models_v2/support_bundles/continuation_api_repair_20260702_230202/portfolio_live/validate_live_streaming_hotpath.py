"""Offline validation for the V2 live streaming hot path.

This command imports every selected live edge, verifies that the process-local
hot-path hooks are installed, and times cold/warm FFD/HMM verification. It does
not initialize MT5, submit orders, or start the dashboard loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _name in (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ.setdefault(_name, "1")

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for _path in (LIVE_DIR, V2_ROOT, REPO_ROOT):
    _text = str(_path)
    if _text in sys.path:
        sys.path.remove(_text)
    sys.path.insert(0, _text)

import live_settings as settings  # type: ignore  # noqa: E402
from common import load_live_manifest, selected_edges, write_json  # type: ignore  # noqa: E402
import portfolio_live_dashboard_orchestrator as dashboard  # type: ignore  # noqa: E402
from shared.edges.native_one_row_contract import (  # noqa: E402
    FULL_AUTHORITY_EVENT_MODE,
    NATIVE_EVENT_MODE,
    SCHEMA_VERSION as NATIVE_CERTIFICATION_SCHEMA_VERSION,
    event_certification,
    event_execution_classification,
    feature_certified,
    load_certification,
)
from shared.live_streaming.hotpath import (  # noqa: E402
    HOTPATH_VERSION,
    cache_status,
    install_hotpath_verifiers,
    patch_runtime_shared_loaders,
    verify_asset_ffd_manifest_hot,
    verify_asset_hmm_regime_manifest_hot,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timed_call(fn, asset: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = fn(asset)
    return result, float(time.perf_counter() - started)




def _validate_event_execution_certifications(
    edges: list[dict[str, Any]],
    native_certification_report: dict[str, Any],
    failures: list[str],
) -> None:
    """Validate native and mandatory-full-authority event classifications.

    ``event_certification`` is intentionally native-only.  Overall validation
    uses ``event_execution_classification`` so a cumulative/path-dependent edge
    can be certified safe only by remaining outside the bounded native engine.
    """
    for edge in edges:
        edge_id = str(edge.get("edge_id", ""))
        classification = event_execution_classification(edge_id)
        if not classification:
            native_certification_report["missing_event_edges"].append(edge_id)
            failures.append(
                f"{edge_id}: safe event execution certification/classification is missing"
            )
            continue

        mode = str(classification.get("event_execution_mode") or "")
        native_certification_report["event_execution_modes"][edge_id] = mode

        if mode == NATIVE_EVENT_MODE:
            if not event_certification(edge_id):
                native_certification_report["missing_event_edges"].append(edge_id)
                failures.append(
                    f"{edge_id}: classified native but native event continuation proof is missing"
                )
                continue
            native_certification_report["event_edges_passed"].append(edge_id)
            native_certification_report["native_event_edges_passed"].append(edge_id)
        elif mode == FULL_AUTHORITY_EVENT_MODE:
            # Native exposure here would violate the safety classification.
            if event_certification(edge_id):
                native_certification_report["missing_event_edges"].append(edge_id)
                failures.append(
                    f"{edge_id}: full-authority edge unexpectedly exposed a native event certificate"
                )
                continue
            if classification.get("native_event_eligible") is not False:
                native_certification_report["missing_event_edges"].append(edge_id)
                failures.append(
                    f"{edge_id}: full-authority classification must set native_event_eligible=False"
                )
                continue
            if classification.get("full_authority_required") is not True:
                native_certification_report["missing_event_edges"].append(edge_id)
                failures.append(
                    f"{edge_id}: full-authority classification must set full_authority_required=True"
                )
                continue
            native_certification_report["event_edges_passed"].append(edge_id)
            native_certification_report["full_authority_event_edges_passed"].append(edge_id)
        else:
            native_certification_report["missing_event_edges"].append(edge_id)
            failures.append(f"{edge_id}: unsupported certified event execution mode={mode!r}")

    native_certification_report["native_event_edge_count"] = len(
        native_certification_report["native_event_edges_passed"]
    )
    native_certification_report["full_authority_event_edge_count"] = len(
        native_certification_report["full_authority_event_edges_passed"]
    )
    native_certification_report["safe_event_edge_count"] = len(
        native_certification_report["event_edges_passed"]
    )

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-authoritative-verification",
        action="store_true",
        help="Only verify runtime hook coverage; do not execute FFD/HMM verifiers.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.OUTPUT_DIR / "live_streaming_hotpath_validation.json",
    )
    args = parser.parse_args()

    install_report = install_hotpath_verifiers()
    manifest = load_live_manifest()
    edges = selected_edges(manifest)
    runtime_reports: list[dict[str, Any]] = []
    failures: list[str] = []
    assets = sorted(
        {str(edge.get("asset", "")).upper() for edge in edges if edge.get("asset")}
    )

    for edge in edges:
        edge_id = str(edge.get("edge_id", ""))
        try:
            runtime = dashboard.import_edge_runtime(edge)
            report = getattr(runtime, "LIVE_STREAMING_HOTPATH_REPORT", None)
            if not isinstance(report, dict):
                report = patch_runtime_shared_loaders(runtime)
            runtime_reports.append({"edge_id": edge_id, **report})
            if str(edge.get("family", "")).lower() == "absorption":
                gate = report.get("absorption_event_first_gate") or {}
                if not gate.get("installed"):
                    failures.append(
                        f"{edge_id}: absorption event-first gate not installed"
                    )
        except Exception as exc:  # validation must report every failed edge
            failures.append(f"{edge_id}: {type(exc).__name__}: {exc}")

    orchestrator_path = LIVE_DIR / "portfolio_live_dashboard_orchestrator.py"
    retired_launcher_path = LIVE_DIR / "portfolio_live_dashboard_hotpath.py"
    entrypoint_report = {
        "orchestrator": str(orchestrator_path),
        "orchestrator_exists": orchestrator_path.exists(),
        "retired_launcher": str(retired_launcher_path),
        "retired_launcher_exists": retired_launcher_path.exists(),
        "single_dashboard_entrypoint": bool(
            orchestrator_path.exists() and not retired_launcher_path.exists()
        ),
        "dashboard_version": dashboard.DASHBOARD_VERSION,
        "integrated_hotpath_version": getattr(dashboard, "HOTPATH_VERSION", None),
    }
    if not entrypoint_report["single_dashboard_entrypoint"]:
        failures.append(
            "dashboard entrypoint consolidation failed: original orchestrator must exist "
            "and portfolio_live_dashboard_hotpath.py must be absent"
        )
    if getattr(dashboard, "HOTPATH_VERSION", None) != HOTPATH_VERSION:
        failures.append("original dashboard does not expose the installed hotpath version")

    native_certification = load_certification()
    native_certification_report: dict[str, Any] = {
        "schema_version": NATIVE_CERTIFICATION_SCHEMA_VERSION,
        "loaded": bool(native_certification),
        "status": native_certification.get("status") if native_certification else None,
        "feature_assets_passed": [],
        "event_edges_passed": [],
        "native_event_edges_passed": [],
        "full_authority_event_edges_passed": [],
        "event_execution_modes": {},
        "missing_feature_assets": [],
        "missing_event_edges": [],
    }
    if not native_certification:
        failures.append(
            "native one-row certification is missing, stale, or does not match the current implementation; "
            "run portfolio_live/certify_native_one_row_runtime.py --fresh"
        )
    for asset in assets:
        if feature_certified(asset):
            native_certification_report["feature_assets_passed"].append(asset)
        else:
            native_certification_report["missing_feature_assets"].append(asset)
            failures.append(f"{asset}: native feature continuation certification/proof is missing")
    _validate_event_execution_certifications(
        edges, native_certification_report, failures
    )

    continuation_reports: dict[str, Any] = {}
    if not args.skip_authoritative_verification:
        for asset in assets:
            try:
                ffd_cold, ffd_cold_seconds = _timed_call(
                    verify_asset_ffd_manifest_hot, asset
                )
                hmm_cold, hmm_cold_seconds = _timed_call(
                    verify_asset_hmm_regime_manifest_hot, asset
                )
                ffd_warm, ffd_warm_seconds = _timed_call(
                    verify_asset_ffd_manifest_hot, asset
                )
                hmm_warm, hmm_warm_seconds = _timed_call(
                    verify_asset_hmm_regime_manifest_hot, asset
                )
                flags = {
                    "ffd_streaming_parity_passed": bool(
                        ffd_warm.get("streaming_parity_passed")
                    ),
                    "hmm_streaming_parity_passed": bool(
                        hmm_warm.get("streaming_parity_passed")
                    ),
                    "ffd_append_only_history": bool(
                        ffd_warm.get("append_only_history")
                    ),
                    "hmm_append_only_history": bool(
                        hmm_warm.get("append_only_history")
                    ),
                    "ffd_restart_parity_passed": bool(
                        ffd_warm.get("restart_parity_passed")
                    ),
                    "hmm_restart_parity_passed": bool(
                        hmm_warm.get("restart_parity_passed")
                    ),
                    "hmm_refit_schedule_preserved": bool(
                        hmm_warm.get("hmm_refit_schedule_preserved")
                    ),
                    "deterministic_noise_contract_preserved": bool(
                        hmm_warm.get("deterministic_noise_contract_preserved")
                    ),
                }
                missing = [name for name, passed in flags.items() if not passed]
                if missing:
                    failures.append(
                        f"{asset}: continuation proof flags failed: {missing}"
                    )
                continuation_reports[asset] = {
                    **flags,
                    "ffd_cold_seconds": ffd_cold_seconds,
                    "hmm_cold_seconds": hmm_cold_seconds,
                    "ffd_warm_seconds": ffd_warm_seconds,
                    "hmm_warm_seconds": hmm_warm_seconds,
                    "ffd_features": ffd_cold.get("features"),
                    "hmm_features": hmm_cold.get("features"),
                }
            except Exception as exc:
                failures.append(
                    f"{asset}: continuation verification failed: {type(exc).__name__}: {exc}"
                )

    report = {
        "schema_version": "live_streaming_hotpath_validation_v1",
        "created_at": _utc_now(),
        "hotpath_version": HOTPATH_VERSION,
        "status": "PASS" if not failures else "FAIL",
        "selected_edge_count": len(edges),
        "asset_count": len(assets),
        "install_report": install_report,
        "entrypoint_report": entrypoint_report,
        "runtime_reports": runtime_reports,
        "continuation_reports": continuation_reports,
        "native_one_row_certification": native_certification_report,
        "cache_status": cache_status(),
        "failures": failures,
        "orders_or_mt5_used": False,
    }
    write_json(args.output, report)
    print(json.dumps(report, indent=2, default=str))
    print(f"Validation report: {args.output}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
