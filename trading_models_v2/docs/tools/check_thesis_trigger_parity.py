"""Parity: shared/edges/thesis_triggers.entries == gate0_tool's EDGE event positions, same bars.

usage (venv312, repo root on PYTHONPATH): python docs/tools/check_thesis_trigger_parity.py T20:XAUUSD:1 T17:BTCUSD:-1 ...
exit 0 = every pair identical.
"""
import sys

import numpy as np

from shared.edges import thesis_triggers as tt
from shared.research.gate0 import gate0_tool as g

bad = 0
for spec in sys.argv[1:]:
    th, asset, side = spec.split(":")
    side = int(side)
    conf = g.THESES[th]
    sets, lab = conf["build"](asset, conf["cell"])
    ref = np.sort(sets["LONG" if side == 1 else "SHORT"]["edge"].pos.to_numpy(dtype=int))
    got = np.sort(tt.entries(th, asset, side, lab.ts, lab.cl, high=lab.hi, low=lab.lo).pos.to_numpy(dtype=int))
    same = len(ref) == len(got) and bool((ref == got).all())
    bad += not same
    print(spec, "gate0", len(ref), "trigger", len(got), "IDENTICAL" if same else
          f"DIFF only_gate0={len(np.setdiff1d(ref, got))} only_trigger={len(np.setdiff1d(got, ref))}")
sys.exit(1 if bad else 0)
