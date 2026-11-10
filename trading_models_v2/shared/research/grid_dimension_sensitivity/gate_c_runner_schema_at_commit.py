"""gate_c_runner.py with its lab calls routed through lab_with_schema_at_commit.py (EWM span test, 2026-10-31).
Same arguments as shared/research/gate_c_runner.py."""
from __future__ import annotations

import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(V2))
import shared.research.gate_c_runner as runner  # noqa: E402

runner.LAB = Path(__file__).resolve().parent / "lab_with_schema_at_commit.py"

if __name__ == "__main__":
    sys.exit(runner.main())
