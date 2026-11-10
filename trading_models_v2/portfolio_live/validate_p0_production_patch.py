from __future__ import annotations

"""Static and import-time validation for the P0 dashboard patch."""

import argparse
import importlib
import json
import py_compile
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--no-dashboard-import",
        action="store_true",
        help="Run compile/static checks only.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    live = root / "portfolio_live"
    dashboard = live / "portfolio_live_dashboard_orchestrator.py"
    helper = live / "production_readiness_p0.py"

    checks = {}
    py_compile.compile(str(helper), doraise=True)
    py_compile.compile(str(dashboard), doraise=True)
    checks["python_compile"] = True

    source = dashboard.read_text(encoding="utf-8")
    checks["install_hook_present"] = (
        "# P0_PRODUCTION_HARDENING_INSTALL_V1" in source
    )
    checks["current_v2_architecture_present"] = all(
        marker in source
        for marker in [
            "+hashless_optional_variant_hash_v1",
            "class ScoringQueueManager:",
            "def score_edge(",
        ]
    )

    sys.path.insert(0, str(live))
    sys.path.insert(0, str(root))
    helper_module = importlib.import_module("production_readiness_p0")
    self_test = helper_module.self_test()
    checks["helper_self_test"] = self_test.get("status") == "PASS"

    import_report = None
    if not args.no_dashboard_import:
        dashboard_module = importlib.import_module(
            "portfolio_live_dashboard_orchestrator"
        )
        import_report = getattr(
            dashboard_module,
            "_P0_PRODUCTION_HARDENING_REPORT",
            None,
        )
        checks["dashboard_patch_installed"] = bool(
            getattr(
                dashboard_module,
                "_P0_PRODUCTION_HARDENING_INSTALLED",
                False,
            )
        )
        checks["dashboard_version_marked"] = (
            "p0_hmm_integrity_terminal_tombstone_v3"
            in str(dashboard_module.DASHBOARD_VERSION)
        )
        # The snapshot coordinator is the current owner layered after P0. A
        # successful full-dashboard import must therefore expose coordinator
        # wrappers, not the superseded intermediate P0 queue/cursor wrappers.
        checks["queue_patch_installed"] = (
            dashboard_module.ScoringQueueManager.start.__name__
            == "start_snapshot_manager"
        )
        checks["cursor_patch_installed"] = (
            dashboard_module.mark_incomplete_model_input_candidate_processed.__name__
            == "mark_incomplete_read_only"
        )

    failed = [name for name, passed in checks.items() if not passed]
    report = {
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed": failed,
        "helper_self_test": self_test,
        "dashboard_import_report": import_report,
    }
    print(json.dumps(report, indent=2, default=str))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
