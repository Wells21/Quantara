"""AFML Ch.4 walkthrough (2026-10-23) regression tests.

Run from trading_models_v2:  python -m unittest tests.test_afml_ch4_weights
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[1]
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from shared.models import afml_sample_weights as w  # noqa: E402


class TimeDecayTest(unittest.TestCase):
    """AFML Snippet 4.11 (p.70) boundary conditions."""

    def setUp(self) -> None:
        idx = pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC")
        self.tw = pd.Series([0.2, 0.5, 1.0, 0.3, 0.4], index=idx)

    def test_c_one_is_no_decay(self) -> None:
        self.assertTrue(np.allclose(w.get_time_decay(self.tw, 1.0), 1.0))

    def test_newest_gets_one_and_decay_is_monotone(self) -> None:
        d = w.get_time_decay(self.tw, 0.25)
        self.assertAlmostEqual(float(d.iloc[-1]), 1.0)
        self.assertTrue(d.is_monotonic_increasing)
        self.assertTrue((d > 0).all())

    def test_negative_c_erases_oldest_share(self) -> None:
        d = w.get_time_decay(self.tw, -0.5)
        cum = self.tw.cumsum()
        self.assertTrue((d[cum <= 0.5 * cum.iloc[-1]] == 0).all())

    def test_c_out_of_range_rejected(self) -> None:
        with self.assertRaises(w.AFMLSampleWeightError):
            w.get_time_decay(self.tw, -1.0)


class LabelEndTimeTest(unittest.TestCase):
    def test_tick_time_snaps_to_containing_bar(self) -> None:
        bars = pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC")
        times = [bars[1] + pd.Timedelta(seconds=1), bars[2], bars[0] - pd.Timedelta(minutes=5)]
        self.assertEqual(w.snap_to_bar_clock(times, bars).tolist(), [2, 2, 0])
        with self.assertRaises(w.AFMLSampleWeightError):
            w.snap_to_bar_clock([bars[-1] + pd.Timedelta(seconds=1)], bars)

    def test_label_t1_falls_back_to_gross_t1(self) -> None:
        frame = pd.DataFrame({"label_t1": ["2024-01-01 01:00:00+00:00", None],
                              "gross_t1": ["2024-01-01 02:00:00+00:00", "2024-01-01 03:00:00+00:00"]})
        ends = w.label_end_times(frame)
        self.assertEqual([t.hour for t in ends], [1, 3])


class EdgeScriptContractTest(unittest.TestCase):
    """Every edge's Steps 01-03 carry the v2 weight contract; Steps 02/03 balance classes once (AFML s.4.8)."""

    def test_all_edges(self) -> None:
        scripts = sorted(V2.glob("assets/*/edges/*/research/0[123]_*.py"))
        scripts = [p for p in scripts if p.name.startswith(("01_feature", "02_train", "03_generate"))]
        self.assertTrue(scripts, "no edge research scripts found")
        for path in scripts:
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=str(path.relative_to(V2))):
                self.assertIn(f'AFML_SAMPLE_WEIGHT_CONTRACT_VERSION = "{w.AFML_SAMPLE_WEIGHT_CONTRACT_VERSION}"', text)
                self.assertIn('LABEL_T1_COL = "label_t1"', text)
                if not path.name.startswith("01_"):
                    self.assertNotIn('final_w = np.asarray(class_w, dtype="float64") * financial_w', text,
                                     "class balance multiplied into sample_weight on top of class_weight='balanced'")
                    self.assertIn('"class_weight": "balanced"', text)


if __name__ == "__main__":
    unittest.main()
