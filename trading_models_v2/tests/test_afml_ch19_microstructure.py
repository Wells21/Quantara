"""Tests for shared/features/afml_ch19_microstructure.py.

Validates the Parkinson and Corwin-Schultz estimators (AFML Ch.19, High/Low
subset) against known reference behavior: recovering an injected constant
bid-ask spread and volatility on synthetic High/Low data.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from shared.features.afml_ch19_microstructure import (
    MicrostructureFeatureError,
    becker_parkinson_volatility,
    corwin_schultz_spread,
    parkinson_volatility,
)

V2_ROOT = Path(__file__).resolve().parents[1]


class TestParkinsonVolatility(unittest.TestCase):
    def test_rejects_high_below_low(self):
        high = pd.Series([1.0, 0.5])
        low = pd.Series([1.0, 0.9])
        with self.assertRaises(MicrostructureFeatureError):
            parkinson_volatility(high, low, window=2)

    def test_recovers_known_volatility_from_simulated_gbm(self):
        rng = np.random.default_rng(7)
        n = 5000
        true_vol_per_bar = 0.01
        n_intrabar = 50
        close = 100.0
        highs = np.empty(n)
        lows = np.empty(n)
        for t in range(n):
            path = close * np.exp(
                np.cumsum(rng.normal(scale=true_vol_per_bar / np.sqrt(n_intrabar), size=n_intrabar))
            )
            highs[t] = path.max()
            lows[t] = path.min()
            close = path[-1]
        vol = parkinson_volatility(pd.Series(highs), pd.Series(lows), window=500)
        estimated = vol.dropna().mean()
        # Parkinson estimator should recover the true per-bar vol within a
        # reasonable tolerance given finite intrabar sampling.
        self.assertAlmostEqual(estimated, true_vol_per_bar, delta=true_vol_per_bar * 0.5)


class TestCorwinSchultzSpread(unittest.TestCase):
    def test_recovers_injected_constant_spread(self):
        rng = np.random.default_rng(11)
        n = 3000
        true_spread_pct = 0.002  # 20 bps
        mid = 100.0 * np.exp(np.cumsum(rng.normal(scale=0.001, size=n)))
        half_spread = mid * true_spread_pct / 2.0
        # High is matched against the offer, Low against the bid (book p.284).
        high = mid + half_spread + np.abs(rng.normal(scale=0.0002 * mid, size=n))
        low = mid - half_spread - np.abs(rng.normal(scale=0.0002 * mid, size=n))

        spread = corwin_schultz_spread(pd.Series(high), pd.Series(low), sample_length=1)
        estimated = spread.dropna()
        estimated = estimated[np.isfinite(estimated)]
        self.assertGreater(len(estimated), 0)
        self.assertAlmostEqual(estimated.median(), true_spread_pct, delta=true_spread_pct * 1.5)

    def test_negative_alpha_clips_to_nonnegative_spread(self):
        # Near-identical High/Low (tiny range) should not produce a large
        # negative or exploding spread once alpha is clipped at 0.
        high = pd.Series([100.001, 100.002, 100.001, 100.003, 100.002])
        low = pd.Series([100.000, 100.001, 100.000, 100.002, 100.001])
        spread = corwin_schultz_spread(high, low, sample_length=1)
        finite = spread.dropna()
        finite = finite[np.isfinite(finite)]
        self.assertTrue((finite >= 0.0).all())


class TestBeckerParkinsonVolatility(unittest.TestCase):
    def test_nonnegative_and_finite_on_synthetic_data(self):
        rng = np.random.default_rng(13)
        n = 500
        mid = 100.0 * np.exp(np.cumsum(rng.normal(scale=0.001, size=n)))
        high = mid + np.abs(rng.normal(scale=0.001 * mid, size=n))
        low = mid - np.abs(rng.normal(scale=0.001 * mid, size=n))
        sigma = becker_parkinson_volatility(pd.Series(high), pd.Series(low), sample_length=1)
        finite = sigma.dropna()
        finite = finite[np.isfinite(finite)]
        self.assertGreater(len(finite), 0)
        self.assertTrue((finite >= 0.0).all())


class TestAgainstBookSnippets(unittest.TestCase):
    """Snippets 19.1-19.2 as printed (shared/research/ch19_microstructure_audit/book_snippets_19_1_19_2.py)."""

    def setUp(self):
        import importlib.util

        path = V2_ROOT / "shared" / "research" / "ch19_microstructure_audit" / "book_snippets_19_1_19_2.py"
        spec = importlib.util.spec_from_file_location("book_snippets_19_1_19_2", path)
        self.book = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.book)
        rng = np.random.default_rng(19)
        mid = 100.0 * np.exp(np.cumsum(rng.normal(scale=0.002, size=400)))
        self.bars = pd.DataFrame({"High": mid * (1 + np.abs(rng.normal(scale=0.002, size=400))),
                                  "Low": mid * (1 - np.abs(rng.normal(scale=0.002, size=400)))})

    def test_spread_equals_snippet_19_1_and_warm_up_stays_nan(self):
        for sl in (1, 2, 20):
            port = corwin_schultz_spread(self.bars["High"], self.bars["Low"], sl)
            book = self.book.corwinSchultz(self.bars, sl)["Spread"]
            np.testing.assert_allclose(port.loc[book.index].to_numpy(), book.to_numpy(), rtol=0, atol=1e-15)
            # D071: the rows the book drops are NaN in the port, never 0
            self.assertTrue(port.drop(book.index).isna().all())
            self.assertEqual(int(port.notna().sum()), len(book))

    def test_sigma_equals_snippet_19_2_and_warm_up_stays_nan(self):
        port = becker_parkinson_volatility(self.bars["High"], self.bars["Low"], 1)
        book = self.book.getSigma(self.book.getBeta(self.bars, 1), self.book.getGamma(self.bars))
        np.testing.assert_allclose(port.loc[book.index].to_numpy(), book.to_numpy(), rtol=0, atol=1e-15)
        self.assertTrue(np.isnan(port.iloc[0]))

    def test_snippet_19_2_is_1_0435_sigma_at_zero_spread(self):
        # D073: paper eq.(12) gives sigma; the snippet's k2 gives sqrt(4 log 2) / sqrt(8 / pi) sigma
        k1 = 4.0 * np.log(2.0)
        beta = gamma = 2.0 * k1  # sigma = 1, no spread
        sigma = self.book.getSigma(pd.Series([beta]), pd.Series([gamma])).iloc[0]
        self.assertAlmostEqual(sigma, np.sqrt(k1) / np.sqrt(8.0 / np.pi), places=12)
        self.assertAlmostEqual(sigma, 1.0434524642511516, places=12)


class TestCorwinSchultzIsNotAFeature(unittest.TestCase):
    """D072: on mid-quote bars the Corwin-Schultz statistic is not a spread; no edge carries it."""

    def test_no_edge_schema_or_expansion_has_it(self):
        schemas = sorted((V2_ROOT / "assets").glob("*/edges/*/contracts/feature_schema.json"))
        self.assertGreater(len(schemas), 0)
        for schema in schemas:
            names = [f["feature_name"] for f in json.loads(schema.read_text(encoding="utf-8"))["features"]]
            self.assertNotIn("Corwin_Schultz_Spread_1", names, schema)
            self.assertIn("Parkinson_Vol_20", names, schema)
            self.assertIn("Becker_Parkinson_Vol_1", names, schema)
            source = (schema.parents[1] / "feature_expansion.py").read_text(encoding="utf-8")
            self.assertNotIn("Corwin_Schultz_Spread_1", source, schema)

    def test_live_engine_does_not_compute_it(self):
        source = (V2_ROOT / "shared" / "edges" / "native_one_row_feature_engine.py").read_text(encoding="utf-8")
        self.assertNotIn('row["Corwin_Schultz_Spread_1"]', source)


if __name__ == "__main__":
    unittest.main()
