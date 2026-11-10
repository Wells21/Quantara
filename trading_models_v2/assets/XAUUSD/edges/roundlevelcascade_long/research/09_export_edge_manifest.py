"""V2 Step 09 wrapper: export edge_manifest.json for XAUUSD/trend_short.

SOTD rule: Step 09 implementation lives in shared/edges/export_edge_manifest.py.
This edge-local file exists only for command clarity and must stay a thin wrapper.

This edge is research_in_progress. This script must fail closed until Step 05,
Step 07, and Step 08 have all passed for this specific edge.

Run from trading_models_v2:

    python ./assets/XAUUSD/edges/trend_short/research/09_export_edge_manifest.py
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

from shared.edges.export_edge_manifest import (  # noqa: E402
    EdgeManifestExportError,
    export_edge_manifest,
    print_manifest_summary,
)
from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402


def main() -> None:
    layout = get_edge_layout_from_edge_root(EDGE_ROOT)
    print("=" * 80, flush=True)
    print(f"V2 STEP 09: EXPORT EDGE MANIFEST — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    manifest = export_edge_manifest(EDGE_ROOT, write=True)
    print_manifest_summary(layout, manifest)


if __name__ == "__main__":
    try:
        main()
    except EdgeManifestExportError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
