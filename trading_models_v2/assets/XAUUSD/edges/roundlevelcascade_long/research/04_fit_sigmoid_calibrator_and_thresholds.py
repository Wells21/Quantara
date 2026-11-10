"""V2 Step 04 wrapper: sigmoid calibration + calibrated threshold sweep for XAUUSD/trend_short.

SOTD rule: Step 04 implementation lives in
shared/calibration/fit_sigmoid_calibrator_and_thresholds.py.

This edge-local file exists only for command clarity and Colab/local workflow
compatibility. It must stay a thin wrapper.

This edge is research_in_progress. Running this script is valid only after Step
03 and Step 03B artifacts exist for this specific edge.

Run from trading_models_v2:

    python ./assets/XAUUSD/edges/trend_short/research/04_fit_sigmoid_calibrator_and_thresholds.py
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

from shared.calibration.fit_sigmoid_calibrator_and_thresholds import CalibrationError, run_step04  # noqa: E402


def main() -> None:
    run_step04(EDGE_ROOT)


if __name__ == "__main__":
    try:
        main()
    except CalibrationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
