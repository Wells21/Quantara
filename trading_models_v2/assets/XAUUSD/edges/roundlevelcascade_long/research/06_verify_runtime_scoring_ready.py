"""Step 06 wrapper: verify runtime scoring readiness for this edge.

SOTD rule: Step 06 is a verifier only. It must never patch runtime.py.
This wrapper is intentionally edge-root derived and contains no alpha/scoring
logic so it can be copied safely to future edge packages.
"""

from __future__ import annotations

import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
EDGE_ROOT = THIS_FILE.parents[1]
V2_ROOT = THIS_FILE.parents[5]
REPO_ROOT = THIS_FILE.parents[6]
for path in (V2_ROOT, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.verify_runtime_scoring_ready import verify_runtime_scoring_ready  # noqa: E402


def main() -> None:
    layout = get_edge_layout_from_edge_root(EDGE_ROOT)
    print("=" * 80, flush=True)
    print(f"V2 STEP 06: VERIFY RUNTIME SCORING READY — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    report = verify_runtime_scoring_ready(layout)
    print(f"Status: {report['verification_status']}", flush=True)
    print(f"Pass: {report['summary']['pass_count']} | Fail: {report['summary']['fail_count']}", flush=True)
    print(f"Report: {layout.runtime_scoring_ready_report_json}", flush=True)
    if report["verification_status"] != "PASS":
        print("\nFailed findings:", flush=True)
        for finding in report["findings"]:
            if finding["severity"] == "FAIL":
                print(f"  - {finding['code']}: {finding['message']}", flush=True)
        raise SystemExit(1)
    print("Runtime scoring readiness verified. No runtime patching was performed.", flush=True)


if __name__ == "__main__":
    main()
