"""V2 Step 05 wrapper: training artifact verifier for XAUUSD/trend_short.

SOTD rule: Step 05 implementation lives in
shared/edges/training_artifact_verifier.py.

This edge-local file exists only for command clarity and must stay a thin wrapper.

This edge is research_in_progress. The script should fail closed until all
canonical Step 01-04 artifacts exist for this specific edge.

Run from trading_models_v2:

    python ./assets/XAUUSD/edges/trend_short/research/05_training_artifact_verifier.py
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
from shared.edges.training_artifact_verifier import print_summary, verify_training_artifacts  # noqa: E402


def main() -> None:
    layout = get_edge_layout_from_edge_root(EDGE_ROOT)
    report = verify_training_artifacts(EDGE_ROOT, write_report=True)
    print_summary(layout, report)
    if report.get("verification_status") != "PASS":
        raise SystemExit(1)
    print("✅ Training artifacts are internally consistent. Edge is still not deployable until runtime score readiness and replay audit pass.", flush=True)


if __name__ == "__main__":
    main()
