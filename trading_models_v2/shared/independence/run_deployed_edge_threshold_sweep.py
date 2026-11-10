"""
Run the live_calibrated_probability threshold-robustness sweep across all
currently-deployed, cross-asset edges.

Instead of testing independence at one sparse, non-authoritative per-edge
selected_threshold (in the wrong -- CPCV-fold-averaged -- probability
space, see config/mldp_deviation_register.json D015), this sweeps several
per-edge population quantiles of live_calibrated_probability (the column
live scoring and the portfolio_allocator threshold grid actually use) and
reports, per pair, whether the independence verdict is stable across the
sweep or threshold-sensitive.

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
    main(["--sweep"] + sys.argv[1:])
