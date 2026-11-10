"""Regression test for the Gate C PBO/CSCV driver (shared/research/gate_c_pbo.py).

Fixture: the exact daily compound-velocity performance matrix of the 2026-10-17 Gate C re-certification of
JPN/volatilitycarry_long (51 eligible cells x 1480 days). Its recorded verdict was PBO 0.5071484071484071,
Prob[OOS<=0] 0.43286713286713285, winner H8_E100_V168_PT2_SL1 (borderline KILLED_PBO, sent to Gate D by user decision).

1. cscv_pbo + compound_velocity_stat on the fixture must reproduce the recorded numbers.
2. If the run's Gate C artifacts exist locally (they are gitignored CSVs), the driver must rebuild the same matrix.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.research import gate_c_pbo as G  # noqa: E402
from shared.backtest_statistics.compound_velocity_stat import compound_velocity_stat  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "gate_c_pbo"
MATRIX = FX / "jpn_volatilitycarry_long_20261017_perf_matrix.npz"
EXPECTED = FX / "jpn_volatilitycarry_long_20261017_expected.json"


def _fixture_matrix() -> pd.DataFrame:
    z = np.load(MATRIX, allow_pickle=False)
    idx = pd.DatetimeIndex(pd.to_datetime(z["index_ns"], unit="ns", utc=True))
    return pd.DataFrame(z["values"], index=idx, columns=[str(c) for c in z["columns"]])


class TestCompoundVelocityStat(unittest.TestCase):
    def test_velocity_over_drawdown(self):
        block = np.array([[0.02, 0.01], [-0.01, 0.01], [0.03, -0.04]])
        got = compound_velocity_stat(block)
        # col 0: mean 0.013333, cum 0.02,0.01,0.04 -> mdd 0.01 ; col 1: mean -0.006667, cum .01,.02,-.02 -> mdd 0.04
        self.assertAlmostEqual(got[0], (0.04 / 3) / 0.01, places=12)
        self.assertAlmostEqual(got[1], (-0.02 / 3) / 0.04, places=12)


class TestGateCPboRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads(EXPECTED.read_text(encoding="utf-8"))["expected"]
        cls.perf = _fixture_matrix()

    def test_fixture_shape(self):
        self.assertEqual(list(self.perf.shape), self.expected["pbo_matrix_shape"])

    def test_cscv_reproduces_recorded_verdict(self):
        p = G.pbo_from_matrix(self.perf)
        self.assertAlmostEqual(p["pbo"], self.expected["pbo"], places=12)
        self.assertAlmostEqual(p["prob_oos_winner_nonpositive"], self.expected["prob_oos_nonpositive"], places=12)
        ok = p["pbo"] < 0.5 and p["prob_oos_winner_nonpositive"] < 0.5
        self.assertEqual("PASS_PBO -> Steps 01-09 + Gate D" if ok else "KILLED_PBO", self.expected["verdict"])

    def test_driver_rebuilds_matrix_from_artifacts(self):
        meta = json.loads(EXPECTED.read_text(encoding="utf-8"))
        edge_root = ROOT / "assets" / "JPN" / "edges" / "volatilitycarry_long"
        art = edge_root / "artifacts" / "v3_horizon_lab"
        if not all((art / f"v3_execution_velocity_summary_{s}.csv").is_file() for s in meta["stamps"]):
            raise unittest.SkipTest("2026-10-17 Gate C artifacts (gitignored CSVs) not present on this machine")
        summ = G.load_run_summary(art, meta["stamps"])
        nc = summ[~summ["is_control"].fillna(False).astype(bool)]
        elig = nc[(nc["exec_mean_R"] > 0) & (nc["coverage_rate"] >= G.MIN_COVERAGE)]
        perf, scores = G.build_performance_matrix(art, elig)
        self.assertEqual(max(scores, key=scores.get), meta["expected"]["winner"])
        self.assertEqual(list(perf.columns), list(self.perf.columns))
        np.testing.assert_array_equal(perf.index.as_unit("ns").asi8 if hasattr(perf.index, "as_unit") else perf.index.asi8,
                                      self.perf.index.asi8)
        np.testing.assert_allclose(perf.to_numpy(dtype="float64"), self.perf.to_numpy(dtype="float64"), rtol=0, atol=1e-15)


class TestGateCPboAllTrials(unittest.TestCase):
    """D051 (owner 2026-10-26): the gate PBO uses every replayed configuration (AFML s.11.6 p.155)."""

    def test_book_n_on_jpn_volatilitycarry(self):
        meta = json.loads(EXPECTED.read_text(encoding="utf-8"))
        edge_root = ROOT / "assets" / "JPN" / "edges" / "volatilitycarry_long"
        art = edge_root / "artifacts" / "v3_horizon_lab"
        if not all((art / f"v3_execution_velocity_summary_{s}.csv").is_file() for s in meta["stamps"]):
            raise unittest.SkipTest("2026-10-17 Gate C artifacts (gitignored CSVs) not present on this machine")
        res = G.evaluate_edge(edge_root, meta["stamps"])
        self.assertEqual(res["pbo_matrix_shape"][1], 212)
        self.assertAlmostEqual(res["pbo"], 0.24452214452214452, places=12)
        self.assertAlmostEqual(res["diagnostic_positive_only"]["pbo"], self.__class__.recorded_pbo(), places=12)
        self.assertTrue(res["verdict"].startswith("PASS_PBO"))

    @staticmethod
    def recorded_pbo():
        return json.loads(EXPECTED.read_text(encoding="utf-8"))["expected"]["pbo"]


class TestGateCRunnerSelection(unittest.TestCase):
    def test_reproduces_20261017_jpn_volatilitycarry_batches(self):
        from shared.research import gate_c_runner as R
        art = ROOT / "assets" / "JPN" / "edges" / "volatilitycarry_long" / "artifacts" / "v3_horizon_lab"
        grid = art / "v3_hybrid_fast_grid_20261017T132019Z.csv"
        if not grid.is_file():
            raise unittest.SkipTest("2026-10-17 Gate A grid (gitignored CSV) not present on this machine")
        cells = R.gate_b_cells(pd.read_csv(grid))
        self.assertEqual(len(cells), 211)                        # recorded: batch 0 = 120 cells, batch 1 = 91 cells
        self.assertEqual([len(cells[i:i + 120]) for i in range(0, len(cells), 120)], [120, 91])
        self.assertIn("8:100:168:2:1", cells)                    # the recorded winner H8_E100_V168_PT2_SL1


if __name__ == "__main__":
    unittest.main()
