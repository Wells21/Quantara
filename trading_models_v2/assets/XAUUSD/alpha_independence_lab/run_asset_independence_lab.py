"""
Run the V2 alpha-independence lab for XAUUSD.

Default mode:
- selected_trades: deployability-PASS edges only, using calibrated OOF predictions
  and each edge's selected calibrated-probability threshold artifact.

Research/candidate mode:
- research_events: exported research model rows, optionally including non-PASS edges.
  This is for early overlap diagnostics only and is not deployability evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[3]
_REPO_ROOT = _V2_ROOT.parent
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.independence.asset_independence_lab import main, run_asset_independence_lab  # noqa: E402,F401


if __name__ == "__main__":
    main()
