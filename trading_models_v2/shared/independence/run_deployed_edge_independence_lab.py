"""
Run the cross-asset, deployed-edges-only statistical independence lab.

Scans every asset under assets/, keeps only edges registered as
deployability_pass in config/edge_universe.json, and runs circular-shift
permutation tests (return-correlation + trade-timing overlap) on every
cross-asset pair plus a Marcenko-Pastur diagnostic across the full set.

This is a diagnostic, not a gate -- it reports statistics, it does not
kill/promote/reweight edges.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _V2_ROOT.parent
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.independence.asset_independence_lab import main  # noqa: E402


if __name__ == "__main__":
    main(["--cross-asset"] + sys.argv[1:])
