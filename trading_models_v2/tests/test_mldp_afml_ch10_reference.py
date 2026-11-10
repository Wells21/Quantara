import unittest
import numpy as np
import pandas as pd
from scipy.stats import norm

from shared.sizing.afml_ch10_reference import (
    AFMLBetSizingError,
    average_active_signals,
    discretize_signal,
    get_signal,
    probability_to_signal,
)


class TestAFMLCh10Reference(unittest.TestCase):
    # --- Binary case: z = (p - 1/2) / sqrt(p(1-p)); m = 2*Phi(z) - 1 ---

    def test_binary_null_probability_has_zero_size(self):
        value = probability_to_signal([0.5], [1], num_classes=2)
        self.assertAlmostEqual(float(value[0]), 0.0, places=12)

    def test_binary_exact_values_cross_checked_against_scipy_norm_cdf(self):
        # Independent reference: compute z and m by hand, verify the module
        # under test agrees with scipy.stats.norm.cdf to numerical precision.
        for p in (0.55, 0.6, 0.75, 0.9, 0.99, 0.1, 0.25):
            with self.subTest(p=p):
                expected_z = (p - 0.5) / np.sqrt(p * (1.0 - p))
                expected_m = 2.0 * norm.cdf(expected_z) - 1.0
                actual = probability_to_signal([p], [1], num_classes=2)
                self.assertAlmostEqual(float(actual[0]), expected_m, places=10)

    def test_meta_side_changes_sign(self):
        long = probability_to_signal([0.9], [1], primary_side=[1])
        short = probability_to_signal([0.9], [1], primary_side=[-1])
        self.assertGreater(float(long[0]), 0.0)
        self.assertAlmostEqual(float(long[0]), -float(short[0]), places=12)

    def test_meta_predicted_pass_has_zero_size(self):
        value = probability_to_signal([0.9], [0], primary_side=[1])
        self.assertAlmostEqual(float(value[0]), 0.0, places=12)

    def test_binary_size_is_monotonic_in_probability(self):
        ps = np.linspace(0.51, 0.99, 25)
        sizes = probability_to_signal(ps.tolist(), [1] * len(ps), num_classes=2)
        self.assertTrue(np.all(np.diff(sizes) > 0))

    def test_binary_size_bounded_in_unit_interval(self):
        ps = np.linspace(0.001, 0.999, 50)
        sizes = probability_to_signal(ps.tolist(), [1] * len(ps), num_classes=2)
        self.assertTrue(np.all(sizes >= -1.0) and np.all(sizes <= 1.0))

    # --- One-vs-rest / multi-class case: z = (p_tilde - 1/K)/sqrt(p_tilde(1-p_tilde)) ---

    def test_one_vs_rest_null_probability_has_zero_size(self):
        # K=3 classes -> null p0 = 1/3.
        value = probability_to_signal([1.0 / 3.0], [1], num_classes=3)
        self.assertAlmostEqual(float(value[0]), 0.0, places=8)

    def test_one_vs_rest_exact_values_cross_checked_against_scipy(self):
        num_classes = 3
        p0 = 1.0 / num_classes
        for p_tilde, x in ((0.6, -1.0), (0.5, 1.0), (0.8, 1.0)):
            with self.subTest(p_tilde=p_tilde, x=x):
                expected_z = (p_tilde - p0) / np.sqrt(p_tilde * (1.0 - p_tilde))
                expected_m = x * (2.0 * norm.cdf(expected_z) - 1.0)
                actual = probability_to_signal([p_tilde], [x], num_classes=num_classes)
                self.assertAlmostEqual(float(actual[0]), expected_m, places=10)

    def test_one_vs_rest_sign_follows_predicted_label(self):
        pos = probability_to_signal([0.7], [1], num_classes=3)
        neg = probability_to_signal([0.7], [-1], num_classes=3)
        self.assertAlmostEqual(float(pos[0]), -float(neg[0]), places=12)

    # --- Validation / fail-closed behavior ---

    def test_rejects_probability_at_or_outside_open_interval(self):
        for p in (0.0, 1.0, -0.1, 1.1):
            with self.subTest(p=p):
                with self.assertRaises(AFMLBetSizingError):
                    probability_to_signal([p], [1], num_classes=2)

    def test_rejects_num_classes_below_two(self):
        with self.assertRaises(AFMLBetSizingError):
            probability_to_signal([0.6], [1], num_classes=1)

    def test_rejects_invalid_primary_side(self):
        with self.assertRaises(AFMLBetSizingError):
            probability_to_signal([0.6], [1], primary_side=[2])

    def test_rejects_broadcast_mismatch(self):
        # Shapes (2,) and (3,) are genuinely non-broadcastable (a length-1
        # array would legitimately broadcast against a longer one, so that
        # is not a valid mismatch case to test here).
        with self.assertRaises(AFMLBetSizingError):
            probability_to_signal([0.6, 0.7], [1, 1, 1], num_classes=2)

    # --- Averaging active bets (AFML 10.4) and discretization (AFML 10.5) ---

    def test_active_average_and_discretization(self):
        idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
        frame = pd.DataFrame(
            {
                "signal": [1.0, -1.0],
                "t1": [
                    idx[0] + pd.Timedelta(hours=2),
                    idx[1] + pd.Timedelta(hours=2),
                ],
            },
            index=idx,
        )
        avg = average_active_signals(frame)
        self.assertAlmostEqual(float(avg.loc[idx[1]]), 0.0)
        self.assertAlmostEqual(
            float(avg.loc[idx[0] + pd.Timedelta(hours=2)]), -1.0
        )
        disc = discretize_signal(np.array([0.13, -0.27]), 0.1)
        np.testing.assert_allclose(disc, [0.1, -0.3], atol=1e-12)

    def test_average_active_signals_averages_two_concurrent_bets(self):
        # Two bets active simultaneously over [t0, t0+3h): sizes 1.0 and 0.5,
        # both alive at the midpoint -- average must be their mean, 0.75.
        t0 = pd.Timestamp("2026-01-01T00:00:00Z")
        frame = pd.DataFrame(
            {"signal": [1.0, 0.5], "t1": [t0 + pd.Timedelta(hours=3)] * 2},
            index=[t0, t0 + pd.Timedelta(hours=1)],
        )
        avg = average_active_signals(frame)
        midpoint = t0 + pd.Timedelta(hours=1)
        self.assertAlmostEqual(float(avg.loc[midpoint]), 0.75, places=12)

    def test_discretize_signal_clips_beyond_unit_interval(self):
        disc = discretize_signal(np.array([1.5, -1.5]), 0.5)
        np.testing.assert_allclose(disc, [1.0, -1.0], atol=1e-12)

    # --- End-to-end composition (AFML 10.3 -> 10.4 -> 10.5) ---

    def test_get_signal_end_to_end(self):
        # Non-overlapping windows are deliberate: AFML 10.4 averages all
        # concurrently active bets, so if these two events' windows
        # overlapped, their equal-magnitude opposite-side signals would
        # legitimately cancel under averaging -- that would be correct
        # behavior, not a bug, but it would defeat this test's purpose of
        # checking that side sign propagates through the full 10.3->10.4
        # composition for each event on its own.
        idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
        events = pd.DataFrame(
            {"side": [1, -1], "t1": [idx[0] + pd.Timedelta(minutes=30), idx[1] + pd.Timedelta(minutes=30)]},
            index=idx,
        )
        signal = get_signal(
            events,
            probability=[0.9, 0.9],
            predicted_label=[1, 1],
            num_classes=2,
            step_size=0.1,
        )
        # Row 0: side=+1 -> positive size. Row 1: side=-1 -> negative size.
        self.assertGreater(float(signal.loc[idx[0]]), 0.0)
        self.assertLess(float(signal.loc[idx[1]]), 0.0)
        # Discretized to steps of 0.1.
        for value in signal.to_numpy():
            self.assertAlmostEqual(round(value / 0.1) * 0.1, value, places=8)


if __name__ == "__main__":
    unittest.main()
