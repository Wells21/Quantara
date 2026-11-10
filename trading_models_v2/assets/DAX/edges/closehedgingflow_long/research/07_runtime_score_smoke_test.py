"""V2 Step 07 wrapper: runtime score smoke test for DAX/trend_short.

SOTD rule: Step 07 implementation lives in
shared/edges/runtime_score_smoke_test.py.

This edge-local file exists only for command clarity and must stay a thin wrapper.

This edge is research_in_progress. The script should fail closed until runtime
scoring artifacts exist and EdgeScorer validates them.

Run from trading_models_v2:

    python ./assets/DAX/edges/trend_short/research/07_runtime_score_smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
EDGE_ROOT = THIS_FILE.parents[1]
V2_ROOT = THIS_FILE.parents[5]
REPO_ROOT = THIS_FILE.parents[6]
for path in (EDGE_ROOT, V2_ROOT, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.runtime_score_smoke_test import print_summary, run_runtime_score_smoke_test  # noqa: E402


def main() -> None:
    layout = get_edge_layout_from_edge_root(EDGE_ROOT)
    report = run_runtime_score_smoke_test(EDGE_ROOT, write_report=True)
    print_summary(layout, report)
    if report.get("smoke_test_status") != "PASS":
        failures = [row for row in report.get("findings", []) if row.get("severity") == "FAIL"]
        if failures:
            print("\nFailed findings:", flush=True)
            for row in failures:
                print(f"  - {row.get('code')}: {row.get('message')}", flush=True)
        raise SystemExit(1)
    print("✅ Runtime scoring smoke test passed. Edge is still non-deployable until replay audit passes.", flush=True)


if __name__ == "__main__":
    main()
