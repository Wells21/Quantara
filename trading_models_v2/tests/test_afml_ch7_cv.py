"""AFML Ch.7 walkthrough (2026-10-24) tests: purge on the label's own t1, embargo max(100, 1% of rows).

Run from trading_models_v2:  python -m unittest tests.test_afml_ch7_cv
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

from shared.calibration import fit_sigmoid_calibrator_and_thresholds as cal  # noqa: E402


class CalibrationCvTest(unittest.TestCase):
    def test_embargo_scales_with_rows(self) -> None:
        self.assertEqual(cal.embargo_size_for(4400), 100)
        self.assertEqual(cal.embargo_size_for(10000), 100)
        self.assertEqual(cal.embargo_size_for(20000), 200)
        self.assertEqual(cal.embargo_size_for(20001), 201)

    def test_purge_t1_prefers_label_t1(self) -> None:
        idx = pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC")
        frame = pd.DataFrame({cal.T1_COL: idx + pd.Timedelta(hours=2),
                              "label_t1": [idx[0] + pd.Timedelta(hours=3), None, idx[2] + pd.Timedelta(hours=1)]},
                             index=idx)
        t1 = cal._purge_t1(frame)
        self.assertEqual(list(t1), [idx[0] + pd.Timedelta(hours=3), idx[1] + pd.Timedelta(hours=2),
                                    idx[2] + pd.Timedelta(hours=1)])

    def test_purge_removes_every_overlap_condition(self) -> None:
        """AFML s.7.4.1 p.106: train starts within test, ends within test, or envelops test."""
        idx = pd.DatetimeIndex(pd.to_datetime(["2024-01-01 00:00", "2024-01-01 05:00", "2024-01-01 10:00",
                                               "2024-01-01 11:00", "2024-01-01 20:00"], utc=True))
        t1 = pd.Series(pd.to_datetime(["2024-01-01 10:30", "2024-01-01 06:00", "2024-01-01 12:00",
                                       "2024-01-01 11:30", "2024-01-01 21:00"], utc=True), index=idx)
        # test = event 2 [10:00, 12:00]; event 0 ends inside, event 3 starts inside, event 1 and 4 do not touch it
        kept = cal.purge_train_overlaps(np.array([0, 1, 3, 4]), np.array([2]), idx, t1)
        self.assertEqual(kept.tolist(), [1, 4])
        # envelops: train [09:00, 13:00] around test [10:00, 12:00]
        idx2 = pd.DatetimeIndex(pd.to_datetime(["2024-01-01 09:00", "2024-01-01 10:00"], utc=True))
        t12 = pd.Series(pd.to_datetime(["2024-01-01 13:00", "2024-01-01 12:00"], utc=True), index=idx2)
        self.assertEqual(cal.purge_train_overlaps(np.array([0]), np.array([1]), idx2, t12).tolist(), [])


class EdgeScriptTest(unittest.TestCase):
    def test_every_edge_step_uses_label_t1_purge_and_scaled_embargo(self) -> None:
        for path in sorted(V2.glob("assets/*/edges/*/research/0[123]_*.py")):
            if not path.name.startswith(("01_feature", "02_train", "03_generate")):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=str(path.relative_to(V2))):
                self.assertIn("pd.Series(_label_end_times(df), index=df.index)", text)
                self.assertIn("embargo_size_for(len(df))", text)
                self.assertNotIn("df.index, df[T1_COL])", text)


if __name__ == "__main__":
    unittest.main()
