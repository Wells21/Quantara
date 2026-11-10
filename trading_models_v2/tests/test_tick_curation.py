"""Curated tick layer: quality filter and roll adjustment (AFML Ch.2 walkthrough, 2026-10-23)."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.data.tick_curation import (  # noqa: E402
    FilterParams,
    QuoteTickQualityFilter,
    cumulative_backward_factor,
)

NS_MIN = 60 * 10**9


def series(start, minutes, values):
    t0 = pd.Timestamp(start, tz="UTC").value
    ts = np.array([t0 + int(m * NS_MIN) for m in minutes], dtype="int64")
    days = pd.to_datetime(ts, utc=True).strftime("%Y-%m-%d").to_numpy()
    return ts, np.asarray(values, dtype="float64"), days


def warm(f, days=8, level=42000.0, daily_move=0.01):
    """Feed `days` calm days so the daily sigma is established (~1%)."""
    for d in range(days):
        price = level * (1 + daily_move * (1 if d % 2 else -1))
        ts, m, dd = series(pd.Timestamp("2025-01-01") + pd.Timedelta(days=d), [0, 60, 120], [price] * 3)
        f.keep_mask(ts, m, dd)
    return f


class TestQualityFilter(unittest.TestCase):
    def test_jpn_style_spike_rejected_then_recovers(self):
        f = warm(QuoteTickQualityFilter(FilterParams()))
        ts, m, d = series("2025-01-20", range(200), [42400] * 10 + [15150] * 150 + [42380] * 40)  # 2.5h bad level
        keep = f.keep_mask(ts, m, d)
        self.assertTrue(keep[:10].all())
        self.assertFalse(keep[10:160].any())
        self.assertTrue(keep[160:].all())
        log = f.finish()
        self.assertEqual(log.rejected, 150)
        self.assertEqual(len(log.episodes), 1)
        self.assertEqual(log.reanchors, [])

    def test_persistent_new_level_reanchors_after_4_hours(self):
        f = warm(QuoteTickQualityFilter(FilterParams()))
        minutes = list(range(10)) + [10 + i for i in range(300)]
        ts, m, d = series("2025-01-20", minutes, [42400] * 10 + [30000] * 300)
        keep = f.keep_mask(ts, m, d)
        first_ok = 10 + int(np.argmax(keep[10:]))
        self.assertEqual(first_ok - 10, 240)  # accepted exactly when the level has persisted 4 hours
        self.assertTrue(keep[first_ok:].all())
        self.assertEqual(len(f.finish().reanchors), 1)

    def test_ordinary_moves_and_session_gaps_pass(self):
        f = warm(QuoteTickQualityFilter(FilterParams()))
        ts, m, d = series("2025-01-20", range(5), [42400, 42450, 41000, 41100, 42500])  # -3.4% gap < 10 sigma
        self.assertTrue(f.keep_mask(ts, m, d).all())

    def test_fast_path_equals_slow_path(self):
        rng = np.random.default_rng(1)
        prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 5000)))
        prices[2500:2510] *= 0.5  # bad block forces the slow path in one chunk
        ts, m, d = series("2025-02-01", np.arange(5000) * 7, prices)
        a = QuoteTickQualityFilter(FilterParams())
        b = QuoteTickQualityFilter(FilterParams())
        ka = np.concatenate([a.keep_mask(ts[i:i + 500], m[i:i + 500], d[i:i + 500]) for i in range(0, 5000, 500)])
        kb = b._keep_mask_slow(ts, m, d)
        self.assertTrue((ka == kb).all())
        self.assertEqual(a.s.ewm_var, b.s.ewm_var)
        self.assertEqual(a.s.last_mid, b.s.last_mid)

    def test_state_round_trip(self):
        f = warm(QuoteTickQualityFilter(FilterParams()))
        g = QuoteTickQualityFilter.from_state_dict(f.state_dict())
        ts, m, d = series("2025-01-20", range(3), [42400, 15000, 42410])
        self.assertTrue((f.keep_mask(ts, m, d) == g.keep_mask(ts, m, d)).all())


class TestRollAdjustment(unittest.TestCase):
    def test_multiplicative_backward_adjustment(self):
        ts, m, _ = series("2025-03-10", [0, 1, 2, 120, 121], [70.0, 70.7, 70.2, 69.0, 69.1])
        rolls = [{"resume_utc": str(pd.Timestamp(int(ts[3]), tz="UTC")), "stop_mid": 70.2, "resume_mid": 69.0}]
        adjusted = m * cumulative_backward_factor(ts, rolls)
        self.assertAlmostEqual(adjusted[3] / adjusted[2], 1.0)            # roll jump gone
        self.assertEqual(adjusted[-1], m[-1])                              # latest equals raw (matchEnd)
        self.assertAlmostEqual(adjusted[1] / adjusted[0], m[1] / m[0])     # % returns preserved (Snippet 2.3)


class TestLiveOrchestratorFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        import logging
        live = Path(__file__).resolve().parents[1] / "portfolio_live"
        sys.path.insert(0, str(live))
        spec = importlib.util.spec_from_file_location("_test_orchestrator", live / "portfolio_live_dashboard_orchestrator.py")
        cls.orch = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.orch
        spec.loader.exec_module(cls.orch)
        cls.log = logging.getLogger("test_tick_filter")

    def ticks(self, start, values):
        ts = pd.date_range(start, periods=len(values), freq="1min", tz="UTC")
        return pd.DataFrame({"timestamp": ts, "mid": np.asarray(values, dtype="float64")})

    def test_bad_block_rejected_and_state_persisted(self):
        state = {}
        df = self.ticks("2025-08-26 06:00", [42400] * 20 + [15150] * 30 + [42390] * 20)
        out = self.orch.apply_tick_quality_filter("JPN", df, state, self.log)
        self.assertEqual(len(out), 40)
        self.assertTrue((out["mid"] > 40000).all())
        self.assertIn("JPN", state["tick_quality_filter_by_asset"])
        self.assertEqual(state["tick_quality_rejected_total_by_asset"]["JPN"], 30)

    def test_split_polls_equal_single_batch(self):
        values = [42400] * 20 + [15150] * 30 + [42390] * 20
        one, two = {}, {}
        a = self.orch.apply_tick_quality_filter("JPN", self.ticks("2025-08-26 06:00", values), one, self.log)
        df = self.ticks("2025-08-26 06:00", values)
        b = pd.concat([self.orch.apply_tick_quality_filter("JPN", df.iloc[i:i + 7].reset_index(drop=True), two, self.log)
                       for i in range(0, len(df), 7)], ignore_index=True)
        self.assertTrue(a["mid"].equals(b["mid"]))
        self.assertEqual(one["tick_quality_filter_by_asset"], two["tick_quality_filter_by_asset"])

    def test_filter_state_is_part_of_the_asset_transaction(self):
        self.assertIn("tick_quality_filter_by_asset", self.orch._ASSET_TRANSACTION_MAP_KEYS)
