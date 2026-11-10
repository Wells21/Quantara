"""AFML Ch.5 walkthrough (2026-10-23) tests for the shared FFD runtime.

Run from trading_models_v2:  python -m unittest tests.test_afml_ch5_ffd
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

from fracdiff import fdiff  # noqa: E402
from fracdiff.fdiff import fdiff_coef  # noqa: E402

from shared.ffd import fractional_differentiation as ffd  # noqa: E402


def afml_snippet_5_1_weights(d: float, size: int) -> np.ndarray:
    """AFML Snippet 5.1 getWeights (p.79), newest-first order."""
    w = [1.0]
    for k in range(1, size):
        w.append(-w[-1] / k * (d - k + 1))
    return np.array(w)


def _bars(close: np.ndarray) -> pd.DataFrame:
    n = len(close)
    t = pd.date_range("2024-01-01", periods=n, freq="min", tz="UTC")
    return pd.DataFrame({"Datetime": t, "Open": close, "High": close * 1.0005, "Low": close * 0.9995, "Close": close})


class WeightsTest(unittest.TestCase):
    def test_package_weights_equal_snippet_5_1(self) -> None:
        for d in (0.05, 0.21, 0.44, 0.9, 1.3):
            self.assertLess(float(np.abs(afml_snippet_5_1_weights(d, 1000) - fdiff_coef(d, 1000)).max()), 1e-12)

    def test_exercise_5_4_inverse_recovers_series(self) -> None:
        rng = np.random.default_rng(1)
        x = np.cumsum(rng.normal(size=6000)) + 100.0
        z = fdiff(fdiff(x, 0.3, window=1000, mode="valid"), -0.3, window=1000, mode="valid")
        self.assertGreater(float(np.corrcoef(z, x[-len(z):])[0, 1]), 0.99)


class DSelectionTest(unittest.TestCase):
    """d is chosen on the whole sample (AFML Snippet 5.4) and must give a stationary Close_FFD."""

    def setUp(self) -> None:
        rng = np.random.default_rng(3)
        # range-bound first part, trending second part: a first-segment estimate is too small for the whole series
        calm = 0.0005 * np.sin(np.arange(4000) / 40.0) + rng.normal(0, 0.0002, 4000)
        trend = np.cumsum(rng.normal(0.00015, 0.001, 4000))
        self.close = 100.0 * np.exp(np.r_[calm, calm[-1] + trend])
        self.cfg = ffd.FFDConfig(ffd_window=200, min_corr_threshold=0.10)

    def test_default_uses_full_history_and_passes_adf(self) -> None:
        bars, _, manifest = ffd.run_ffd_pipeline(_bars(self.close), asset="T", source_bars_path="t", config=self.cfg)
        self.assertEqual(manifest["d_estimation_policy"], ffd.D_POLICY_ESTIMATED)
        self.assertEqual(manifest["d_estimation_rows"], len(self.close))
        self.assertTrue(manifest["stationarity"]["full_passed"])
        self.assertLess(manifest["stationarity"]["full"]["p_value"], 0.05)

    def test_first_segment_d_is_not_enough(self) -> None:
        d_first = ffd.estimate_optimal_d(pd.Series(self.close), window=200, estimation_bars=3000)
        d_full = ffd.estimate_optimal_d(pd.Series(self.close), window=200)
        self.assertLess(d_first, d_full)

    def test_supplied_d_is_kept_and_not_gated(self) -> None:
        _, bundle, manifest = ffd.run_ffd_pipeline(_bars(self.close), asset="T", source_bars_path="t",
                                                   config=self.cfg, frozen_d=0.0001)
        self.assertEqual(manifest["d_estimation_policy"], ffd.D_POLICY_SUPPLIED)
        self.assertEqual(manifest["frozen_d"], 0.0001)
        self.assertEqual(bundle["frozen_d"], 0.0001)
        self.assertIsNone(manifest["d_estimation_rows"])


if __name__ == "__main__":
    unittest.main()
