"""Decommission / shrink rule (AFML Ch.1 p.8-9; PSR AFML s.14.7.2 p.203)."""
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy import stats

V2_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("decommission_rule", V2_ROOT / "portfolio_live" / "decommission_rule.py")
dr = importlib.util.module_from_spec(_spec)
sys.modules["decommission_rule"] = dr
_spec.loader.exec_module(dr)

CFG = json.loads((V2_ROOT / "portfolio_live" / "config" / "decommission_rule.json").read_text(encoding="utf-8"))


def bets(n, wins):
    return np.array([2.0] * wins + [-1.0] * (n - wins))


BACKTEST = bets(1000, 360)  # +2R/-1R, 36% wins, mean +0.08R


class TestPSR(unittest.TestCase):
    def test_matches_book_formula(self):
        x = np.random.default_rng(0).normal(0.05, 1.0, 500)
        sr = x.mean() / x.std(ddof=1)
        g3, g4 = stats.skew(x), stats.kurtosis(x, fisher=False)
        expected = stats.norm.cdf(sr * math.sqrt(499) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2))
        self.assertAlmostEqual(dr.probabilistic_sharpe_ratio(x, 0.0)[0], expected, places=12)

    def test_worked_examples_shown_to_user(self):
        _, sr_bt = dr.probabilistic_sharpe_ratio(BACKTEST, 0.0)
        self.assertAlmostEqual(sr_bt, 0.056, places=3)
        self.assertAlmostEqual(dr.probabilistic_sharpe_ratio(bets(30, 5), 0.0)[0], 0.045, places=3)
        self.assertAlmostEqual(dr.probabilistic_sharpe_ratio(bets(60, 15), sr_bt)[0], 0.044, places=3)
        self.assertAlmostEqual(dr.probabilistic_sharpe_ratio(bets(100, 25), 0.0)[0], 0.043, places=3)

    def test_backtest_like_sample_scores_about_one_half(self):
        _, sr_bt = dr.probabilistic_sharpe_ratio(BACKTEST, 0.0)
        self.assertAlmostEqual(dr.probabilistic_sharpe_ratio(bets(100, 36), sr_bt)[0], 0.5, delta=0.02)


class TestLevels(unittest.TestCase):
    def test_bands(self):
        levels = CFG["shrink_levels"]
        self.assertEqual(dr.allocation_for(0.50, levels), 1.0)
        self.assertEqual(dr.allocation_for(0.20, levels), 0.8)
        self.assertEqual(dr.allocation_for(0.12, levels), 0.6)
        self.assertEqual(dr.allocation_for(0.07, levels), 0.4)
        self.assertEqual(dr.allocation_for(0.05, levels), 0.2)
        self.assertEqual(dr.allocation_for(0.0, levels), 0.2)


class TestDecide(unittest.TestCase):
    def test_insufficient_sample_takes_no_action(self):
        v = dr.decide("X/e_long", bets(29, 0), BACKTEST, CFG, "live")
        self.assertEqual(v.verdict, "insufficient_sample")
        self.assertIsNone(v.allocation)

    def test_on_track_is_full(self):
        v = dr.decide("X/e_long", bets(30, 11), BACKTEST, CFG, "live")
        self.assertEqual((v.verdict, v.allocation), ("full", 1.0))

    def test_worse_than_backtest_is_reduced_not_off(self):
        v = dr.decide("X/e_long", bets(60, 15), BACKTEST, CFG, "live")
        self.assertEqual((v.verdict, v.allocation), ("reduced", 0.2))

    def test_losing_money_is_off(self):
        v = dr.decide("X/e_long", bets(100, 25), BACKTEST, CFG, "live")
        self.assertEqual((v.verdict, v.allocation), ("off", 0.0))

    def test_recovery_raises_allocation_again(self):
        bad = dr.decide("X/e_long", bets(60, 15), BACKTEST, CFG, "live")
        recovered = dr.decide("X/e_long", np.concatenate([bets(60, 15), bets(60, 26)]), BACKTEST, CFG, "live")
        self.assertGreater(recovered.allocation, bad.allocation)


class TestStrategyRiskDiagnostic(unittest.TestCase):
    """AFML s.15.4 live precision vs p_theta* (D055, 2026-10-27): p_theta* from the backtest bets, bootstrap of live bets."""
    SR_CFG = {"theta_star": 0.0, "bootstrap": {"n_boot": 20000, "seed": 0}}

    def test_break_even_precision_for_two_to_one(self):
        # +2/-1 break-even precision is 1/3; a 36% sample of 1000 bets is above it, 30 bets cannot show it
        big = dr.strategy_risk_diagnostic(BACKTEST, BACKTEST, 4.0, self.SR_CFG)
        self.assertAlmostEqual(big["p_theta_star"], 1 / 3, places=6)
        self.assertLess(big["prob_live_precision_below_p_theta_star"], 0.05)
        small = dr.strategy_risk_diagnostic(bets(30, 11), BACKTEST, 4.0, self.SR_CFG)
        self.assertGreater(small["prob_live_precision_below_p_theta_star"], 0.2)

    def test_overlapping_trades_form_one_bet(self):
        import pandas as pd
        t = pd.DataFrame({"r": [1.0, -0.5, 2.0], "side": [1, 1, 1],
                          "entry_time": ["2025-01-01", "2025-01-02", "2025-01-10"],
                          "exit_time": ["2025-01-03", "2025-01-05", "2025-01-12"]})
        np.testing.assert_allclose(dr.to_bets(t), [0.5, 2.0])
        np.testing.assert_allclose(dr.to_bets(pd.DataFrame({"r": [1.0, -1.0]})), [1.0, -1.0])  # no times -> one bet each

    def test_d056_minimum_sample_counts_bets_not_trades(self):
        # 40 trades in 10 bursts of 4 overlapping same-side trades -> 10 bets < 30: no verdict yet
        import pandas as pd
        rows = []
        for b in range(10):
            for j in range(4):
                rows.append({"r": 1.0 if b % 2 else -1.0, "side": 1,
                             "entry_time": f"2025-01-{b * 2 + 1:02d} 0{j}:00", "exit_time": f"2025-01-{b * 2 + 1:02d} 0{j + 4}:00"})
        t = pd.DataFrame(rows)
        self.assertEqual(len(dr.to_bets(t)), 10)
        v = dr.decide("X/e_long", dr.to_bets(t), BACKTEST, CFG, "live")
        self.assertEqual(v.verdict, "insufficient_sample")


if __name__ == "__main__":
    unittest.main()
