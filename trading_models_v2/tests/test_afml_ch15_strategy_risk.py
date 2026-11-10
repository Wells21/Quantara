"""AFML Ch.15 walkthrough (2026-10-27): every number the chapter prints, plus the s.15.4.1 bootstrap algorithm.

Run from trading_models_v2:  python -m unittest tests.test_afml_ch15_strategy_risk
"""
from __future__ import annotations

import unittest

import numpy as np
from scipy import stats

from shared.backtest_statistics import afml_ch14_ch15_reference as r


class BookNumbers(unittest.TestCase):
    def test_symmetric_p055(self) -> None:
        # p.212: p = .55 -> (2p - 1) / (2 sqrt(p(1 - p))) = 0.1005; theta = 2 needs 396 bets / year
        self.assertAlmostEqual(r.sr_symmetric(0.55, 1), 0.1005, places=4)
        self.assertAlmostEqual(r.sr_symmetric(0.55, 396), 2.0, places=2)

    def test_symmetric_weekly_precision(self) -> None:
        # p.213: weekly bets (n = 52) need p = 0.6336 for theta = 2
        self.assertAlmostEqual(r.implied_precision_symmetric(52, 2.0), 0.6336, places=4)

    def test_asymmetric_example(self) -> None:
        # p.213: n = 260, pi- = -.01, pi+ = .005, p = .7 -> theta = 1.173
        self.assertAlmostEqual(r.bin_sr(-0.01, 0.005, 260, 0.7), 1.173, places=3)

    def test_symmetric_case_reduces(self) -> None:
        # p.213: pi- = -pi+ reduces to the symmetric formula
        self.assertAlmostEqual(r.bin_sr(-0.02, 0.02, 52, 0.6), r.sr_symmetric(0.6, 52))

    def test_implied_precision_example(self) -> None:
        # p.214: n = 260, pi- = -.01, pi+ = .005, theta = 2 requires p = .72
        self.assertAlmostEqual(r.bin_hr(-0.01, 0.005, 260, 2.0), 0.72, places=2)

    def test_p_theta_zero_is_two_thirds(self) -> None:
        # p.216: for p_theta*=0 = 2/3 -- break-even precision of pi- = -.01, pi+ = .005
        self.assertAlmostEqual(r.bin_hr(-0.01, 0.005, 260, 0.0), 2 / 3, places=10)

    def test_bin_freq_round_trip_and_extraneous_root(self) -> None:
        f = r.bin_freq(-0.01, 0.005, 0.72, 2.0)
        self.assertIsNotNone(f)
        self.assertAlmostEqual(r.bin_sr(-0.01, 0.005, f, 0.72), 2.0, places=6)
        self.assertIsNone(r.bin_freq(-0.01, 0.005, 0.6, 2.0))  # p below break-even: root is extraneous (theta < 0)

    def test_snippet_approximation_is_flagged_as_printed(self) -> None:
        self.assertAlmostEqual(r.prob_failure_snippet_approximation(0.7, 0.6), stats.norm.cdf(0.6, 0.7, 0.21))


class SnippetChecks(unittest.TestCase):
    def test_snippet_15_1_monte_carlo(self) -> None:
        # Snippet 15.1: +1 / -1 bets with p = .55 -> mean / std ~ 0.1005 per bet
        rng = np.random.default_rng(0)
        x = np.where(rng.random(1_000_000) < 0.55, 1.0, -1.0)
        self.assertAlmostEqual(x.mean() / x.std(), r.sr_symmetric(0.55, 1), delta=0.003)

    def test_snippet_15_2_variance_factorisation(self) -> None:
        # Snippet 15.2 (SymPy): V[X] = p u^2 + (1 - p) d^2 - (p u + (1 - p) d)^2 = (u - d)^2 p (1 - p)
        for p, u, d in ((0.7, 0.005, -0.01), (0.4, 2.0, -1.0), (0.55, 1.0, -1.0)):
            m1, m2 = p * u + (1 - p) * d, p * u ** 2 + (1 - p) * d ** 2
            self.assertAlmostEqual(m2 - m1 ** 2, (u - d) ** 2 * p * (1 - p), places=12)

    def test_snippet_15_5_example_runs_through_the_book_algorithm(self) -> None:
        # Snippet 15.5 main(): mixture .05/.1 (75 %) and -.1/.1, 2,600 obs, freq 260, tSR 2 (the book prints no result)
        ret = r.mix_gaussians(0.05, -0.1, 0.05, 0.1, 0.75, 2600, seed=0)
        self.assertEqual(ret.size, 2600)
        res = r.prob_strategy_failure(ret, years=10.0, t_sr=2.0, k_years=2.0, n_boot=5000, seed=0)  # 2,600 / 260 = 10 years
        self.assertAlmostEqual(res["n"], 260.0)
        self.assertTrue(0.0 <= res["prob_failure"] <= 1.0)
        self.assertTrue(np.isfinite(res["psr_vs_theta_star_same_bets"]))


class BootstrapAlgorithm(unittest.TestCase):
    def test_book_algorithm_matches_binomial_spread(self) -> None:
        rng = np.random.default_rng(3)
        bets = np.where(rng.random(400) < 0.4, 2.0, -1.0)  # 400 bets over 4 years -> n = 100 / yr
        res = r.prob_strategy_failure(bets, years=4.0, t_sr=0.5, k_years=2.0, n_boot=20_000, seed=1)
        self.assertEqual(res["bets_per_draw_floor_nk"], 200)
        self.assertAlmostEqual(res["pi_minus"], -1.0)
        self.assertAlmostEqual(res["pi_plus"], 2.0)
        self.assertAlmostEqual(res["p_theta_star"], r.bin_hr(-1.0, 2.0, 100.0, 0.5))
        self.assertAlmostEqual(res["prob_failure"], res["prob_failure_normal_sqrt_p1p_over_nk"], delta=0.02)

    def test_pi_minus_includes_zero_outcomes(self) -> None:
        prm = r.bet_outcome_parameters(np.array([1.0, 0.0, -1.0, 0.0]), years=1.0)
        self.assertAlmostEqual(prm["pi_minus"], -1 / 3)  # book: E[pi | pi <= 0]
        self.assertAlmostEqual(prm["p"], 0.25)

    def test_sensitivity_solutions_hit_the_target(self) -> None:
        s = r.strategy_sensitivity(0.6, 52, -0.02, 0.02, 2.0)  # exercise 15.1 inputs
        self.assertFalse(s["viable"])
        self.assertAlmostEqual(r.bin_sr(-0.02, 0.02, 52, s["required_precision"]), 2.0, places=6)
        self.assertAlmostEqual(r.bin_sr(-0.02, 0.02, s["required_bets_per_year"], 0.6), 2.0, places=6)
        self.assertAlmostEqual(r.bin_sr(-0.02, s["required_pt_given_sl"], 52, 0.6), 2.0, places=6)
        self.assertAlmostEqual(r.bin_sr(s["required_sl_given_pt"], 0.02, 52, 0.6), 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
