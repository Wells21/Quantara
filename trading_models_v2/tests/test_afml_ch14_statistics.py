"""AFML Ch.14 walkthrough (2026-10-26): book-number checks for shared/backtest_statistics/afml_ch14_dsr.py and the
Ch.14 s.14.3 / 14.4 functions in shared/backtest_statistics/afml_ch14_ch15_reference.py.

Run from trading_models_v2:  python -m unittest tests.test_afml_ch14_statistics
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd
from scipy import stats

from shared.backtest_statistics import afml_ch14_dsr as d
from shared.backtest_statistics import afml_ch14_ch15_reference as ref


class MlamCh8Numbers(unittest.TestCase):
    def test_expected_max_sr_1000_trials(self) -> None:
        # MLAM p.111: "if we conduct 1,000 trials, the expected maximum Sharpe ratio ... is 3.26" (std 1)
        self.assertAlmostEqual(d.expected_max_sr(1000), 3.26, places=2)

    def test_type1_type2_numerical_example(self) -> None:
        # MLAM s.8.8.2-8.8.3 (pp.118-120), Snippets 8.3 / 8.4
        t, skew, kurt, k, freq = 1250, -3.0, 10.0, 10, 250
        sr, sr_alt = 1.25 / freq ** 0.5, 1.0 / freq ** 0.5
        z = d.z_stat(sr, t, 0.0, skew, kurt)
        self.assertAlmostEqual(z, 2.4978, places=3)
        alpha_k = 1 - (1 - (1 - stats.norm.cdf(z))) ** k
        self.assertAlmostEqual(alpha_k, 0.0608, places=3)
        theta = sr_alt * np.sqrt(t - 1) / np.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr ** 2)
        self.assertAlmostEqual(theta, 1.9982, places=3)
        beta = stats.norm.cdf(stats.norm.ppf((1 - alpha_k) ** (1 / k)) - theta)
        self.assertAlmostEqual(beta, 0.6913, places=3)
        self.assertAlmostEqual(beta ** k, 0.0249, places=3)

    def test_dsr_report_matches_formulas(self) -> None:
        rng = np.random.default_rng(1)
        idx = pd.date_range("2020-01-01", periods=1461, freq="D", tz="UTC")
        r = rng.normal(0.05, 1.0, idx.size)
        rep = d.dsr_report(r, idx, e_k=10, v_annualised_sr=0.5)
        m = d.moments(r)
        f = d.frequency(idx, m["T"])
        sr_star = np.sqrt(0.5 / f) * d.expected_max_sr(10)
        self.assertAlmostEqual(rep["sr_star_nonannualised"], sr_star)
        self.assertAlmostEqual(rep["dsr"], stats.norm.cdf(d.z_stat(m["sr"], m["T"], sr_star, m["skew"], m["kurt"])))
        self.assertLess(rep["dsr"], rep["psr_vs_zero"])
        self.assertAlmostEqual(rep["fwer"]["alpha_K"], 1 - (1 - rep["fwer"]["alpha_single"]) ** 10)

    def test_min_variance_weights(self) -> None:
        cov = np.array([[1.0, 0.2], [0.2, 4.0]])
        w, singular = d.min_variance_weights(cov)
        inv = np.linalg.inv(cov) @ np.ones(2)
        np.testing.assert_allclose(w, inv / inv.sum())
        self.assertFalse(singular)
        w2, singular2 = d.min_variance_weights(np.ones((2, 2)))  # duplicate trials
        self.assertTrue(singular2)
        self.assertAlmostEqual(w2.sum(), 1.0)


class LdpLewis2018Snippet4(unittest.TestCase):
    def test_get_ivp_matches_sherman_morrison_formula(self) -> None:
        sd = np.array([1.0, 2.0, 0.5, 1.5])
        rho = 0.9
        cov = rho * np.outer(sd, sd) + (1 - rho) * np.diag(sd ** 2)  # exactly constant correlation
        w = d.get_ivp(cov, use_extended_terms=True)
        # on an exactly constant-correlation matrix the approximation is exact: equals Sigma^-1 1 / 1' Sigma^-1 1
        exact = np.linalg.inv(cov) @ np.ones(4)
        np.testing.assert_allclose(w, exact / exact.sum(), rtol=1e-10)
        # paper formula w_i ~ 1/sigma_i^2 - rho sum(1/sigma_j) / ((1 + (N-1) rho) sigma_i)
        f = 1 / sd ** 2 - rho * np.sum(1 / sd) / ((1 + 3 * rho) * sd)
        np.testing.assert_allclose(w, f / f.sum(), rtol=1e-12)

    def test_rho_zero_is_inverse_variance(self) -> None:
        cov = np.diag([1.0, 4.0, 2.0])
        w = d.get_ivp(cov, use_extended_terms=True)
        np.testing.assert_allclose(w, (1 / np.diag(cov)) / (1 / np.diag(cov)).sum())
        np.testing.assert_allclose(d.get_ivp(cov), w)


class AfmlCh14Snippets(unittest.TestCase):
    def setUp(self) -> None:
        self.trades = pd.DataFrame({
            "entry_time": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-10", "2025-01-20"], utc=True),
            "exit_time": pd.to_datetime(["2025-01-03", "2025-01-05", "2025-01-12", "2025-01-21"], utc=True),
            "side": [1, 1, -1, 1], "executed_r": [1.0, -0.5, 2.0, -1.0]})

    def test_bets_not_trades(self) -> None:
        t_pos = ref.target_positions_from_trades(self.trades)
        self.assertEqual(len(ref.bet_timestamps(t_pos)), 3)       # two overlapping longs = one bet
        bets = ref.bet_level_returns(self.trades, t_pos)
        np.testing.assert_allclose(bets["r"].to_numpy(), [0.5, 2.0, -1.0])

    def test_flip_is_a_bet_end(self) -> None:
        t_pos = pd.Series([1.0, -1.0, 0.0], index=pd.to_datetime(["2025-01-01", "2025-01-02", "2025-01-03"], utc=True))
        self.assertEqual(len(ref.bet_timestamps(t_pos)), 2)

    def test_holding_period(self) -> None:
        # durations 2, 3, 2, 1 days, one unit each -> 2.0
        self.assertAlmostEqual(ref.holding_period_days(ref.target_positions_from_trades(self.trades)), 2.0)

    def test_sizing_leverage_turnover(self) -> None:
        # two overlapping trades, each 1.0 lot: notional = lots * risk_per_1_lot * price / stop = 1 * 100 * 50 / 1 = 5000
        t = pd.DataFrame({
            "entry_time": pd.to_datetime(["2025-01-01", "2025-01-02"], utc=True),
            "exit_time": pd.to_datetime(["2025-01-03", "2025-01-04"], utc=True),
            "afml_bet_size": [1.0, 1.0], "risk_per_1_lot": [100.0, 100.0], "entry_price": [50.0, 50.0],
            "stop_distance_price": [1.0, 1.0], "broker_min_lot": [0.01, 0.01], "broker_lot_step": [0.01, 0.01],
            "broker_max_lot": [10.0, 10.0]})
        s = ref.sizing_and_capacity_stats(t, f=0.001, balance=100_000.0)  # target risk 100 -> 1.00 lot
        self.assertAlmostEqual(s["max_lots_single_trade"], 1.0)
        self.assertAlmostEqual(s["max_dollar_position"], 10_000.0)          # both open on Jan 2-3
        self.assertAlmostEqual(s["avg_open_notional"], (5000 + 10000 + 5000) / 3)  # 1 day each at 5k, 10k, 5k
        self.assertAlmostEqual(s["capacity_lot_headroom_x"], 10.0)

    def test_performance(self) -> None:
        s = ref.general_and_performance_stats(self.trades)
        self.assertEqual((s["n_trades"], s["n_bets"]), (4, 3))
        self.assertAlmostEqual(s["hit_ratio_bets"], 2 / 3)
        self.assertAlmostEqual(s["pnl_r_from_longs"], -0.5)


class MlamCh2TrialCorrelation(unittest.TestCase):
    """D076 (owner 2026-11-02): trials are clustered on the denoised, conditionally detoned correlation (MLAM p.24, p.87)."""

    def test_market_component_is_detoned(self) -> None:
        rng = np.random.default_rng(0)
        t, n = 600, 40
        market = rng.standard_normal((t, 1))
        perf = pd.DataFrame(market + 0.7 * rng.standard_normal((t, n)), columns=[f"c{i}" for i in range(n)])
        corr, diag = d.trial_clustering_corr(perf)
        self.assertTrue(diag["denoised"] and diag["market_component"] and diag["detoned"])
        self.assertGreaterEqual(diag["n_facts"], 1)
        self.assertLess(abs(np.linalg.eigvalsh(corr.to_numpy()).min()), 1e-6)   # detoned matrix is singular (MLAM p.32)

    def test_pure_noise_has_no_factor_and_gives_identity(self) -> None:
        rng = np.random.default_rng(1)
        perf = pd.DataFrame(rng.standard_normal((2000, 20)), columns=[f"c{i}" for i in range(20)])
        corr, diag = d.trial_clustering_corr(perf)
        self.assertEqual(diag["n_facts"], 0)        # Snippet 2.4: no floor of 1
        self.assertFalse(diag["detoned"])
        self.assertTrue(np.allclose(corr.to_numpy(), np.eye(20), atol=1e-8))

    def test_t_not_above_n_keeps_raw(self) -> None:
        rng = np.random.default_rng(2)
        perf = pd.DataFrame(rng.standard_normal((10, 12)), columns=[f"c{i}" for i in range(12)])
        corr, diag = d.trial_clustering_corr(perf)
        self.assertFalse(diag["denoised"])
        self.assertTrue(np.allclose(corr.to_numpy(), perf.corr().to_numpy()))


if __name__ == "__main__":
    unittest.main()
