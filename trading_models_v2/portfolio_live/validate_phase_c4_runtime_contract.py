from __future__ import annotations

"""Lightweight Phase C.4 source/contract validator.

This validator intentionally does NOT run the expensive C.2/C.3 numerical
certifications and does NOT create the production activation file. It is safe
for the local workstation after applying the patch.
"""

import argparse
import inspect
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
for _path in (V2_ROOT, HERE):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import phase_c_04_production_router as c4


def _source_checks(v2_root: Path) -> dict[str, bool]:
    live = v2_root / "portfolio_live"
    dashboard = (live / "portfolio_live_dashboard_orchestrator.py").read_text(
        encoding="utf-8"
    )
    snapshot = (live / "production_snapshot_pipeline.py").read_text(
        encoding="utf-8"
    )
    advance = inspect.getsource(c4._advance_one)
    score_wrapper_source = inspect.getsource(c4.install_phase_c4_production_router)
    return {
        "dashboard_installs_c4_after_snapshot_pipeline": (
            "_SNAPSHOT_PIPELINE_REPORT = _install_snapshot_pipeline(globals())"
            in dashboard
            and "_PHASE_C4_PRODUCTION_ROUTER_REPORT = _install_phase_c4_production_router(globals())"
            in dashboard
            and dashboard.index("_SNAPSHOT_PIPELINE_REPORT = _install_snapshot_pipeline(globals())")
            < dashboard.index("_PHASE_C4_PRODUCTION_ROUTER_REPORT = _install_phase_c4_production_router(globals())")
        ),
        "snapshot_recovery_resets_phase_c4_state": "_PHASE_C4_RECOVERY_RESET" in snapshot,
        "snapshot_classifies_phase_c4_contract_failures": "phase_c4_feature_contract" in snapshot,
        "normal_incremental_update_has_no_build_features": "build_features" not in advance,
        "normal_incremental_update_has_no_dependency_disk_reads": (
            "read_csv" not in advance
            and "load_verified_ffd" not in advance
            and "load_verified_hmm" not in advance
            and "refresh_verified_feature_dependencies_hot" not in advance
        ),
        "edge_normal_path_installs_batch_build_guard": (
            "normal_path_runtime_build_features_forbidden" in score_wrapper_source
        ),
        "router_has_no_fixed_current_asset_names": all(
            token not in (live / "phase_c_04_production_router.py").read_text(encoding="utf-8")
            for token in (
                '"BTCUSD"', '"DAX"', '"GBPJPY"', '"JPN"',
                '"NASDAQ"', '"US500"', '"USOIL"', '"XAUUSD"',
            )
        ),
        "router_has_no_fixed_selected_edge_count_gate": (
            "selected_edge_count == 74"
            not in (live / "phase_c_04_production_router.py").read_text(encoding="utf-8")
            and "selected_edge_count != 74"
            not in (live / "phase_c_04_production_router.py").read_text(encoding="utf-8")
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--v2-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    root = args.v2_root.resolve()

    static = c4.static_contract_report(root)
    self_test = c4.self_test()
    source_checks = _source_checks(root)
    failed_source = [name for name, passed in source_checks.items() if not passed]

    status = (
        "PASS"
        if static.get("status") == "PASS"
        and self_test.get("status") == "PASS"
        and not failed_source
        else "FAIL"
    )
    report = {
        "status": status,
        "router_version": c4.ROUTER_VERSION,
        "static_contract": static,
        "router_self_test": self_test,
        "source_checks": source_checks,
        "failed_source_checks": failed_source,
        "activation_present": (
            root / "portfolio_live" / "config" / "phase_c" / "production_routing_activation.json"
        ).is_file(),
        "activation_written_by_validator": False,
    }

    print("=" * 112)
    print("PHASE C.4 — LIGHTWEIGHT LOCAL RUNTIME CONTRACT VALIDATOR")
    print("=" * 112)
    print(f"V2 root:       {root}")
    print(f"Router:        {c4.ROUTER_VERSION}")
    print(f"Static:        {static.get('status')}")
    c3_recompile = static.get("c3_recompile_validation") or {}
    print(f"C.3 recompile: {c3_recompile.get('mode', 'N/A')}")
    if c3_recompile.get("ast_function_hash_difference_count") is not None:
        print(
            "C.3 AST variance: "
            f"{c3_recompile.get('ast_function_hash_difference_count')} function fingerprints "
            f"(Python {c3_recompile.get('python_version', 'unknown')})"
        )
    print(f"Self-test:     {self_test.get('status')}")
    print(f"Source checks: {sum(source_checks.values())}/{len(source_checks)}")
    print(f"Activation:    {'PRESENT' if report['activation_present'] else 'ABSENT (safe staged state)'}")
    if static.get("failures"):
        print("\nSTATIC CONTRACT FAILURES:")
        for value in static["failures"]:
            print(f"  - {value}")
    if self_test.get("failed"):
        print("\nSELF-TEST FAILURES:")
        for value in self_test["failed"]:
            print(f"  - {value}")
    if failed_source:
        print("\nSOURCE CHECK FAILURES:")
        for value in failed_source:
            print(f"  - {value}")
    print(f"\nSTATUS: {status}")
    print("This validator never writes the Phase C.4 activation file.")

    if args.json_output is not None:
        target = args.json_output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"Report: {target}")
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
