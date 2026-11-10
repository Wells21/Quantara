"""AFML Ch.6 walkthrough (2026-10-24) regression tests for the sequential-bootstrap bagging ensemble.

Run from trading_models_v2:  python -m unittest tests.test_afml_ch6_ensemble
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

V2 = Path(__file__).resolve().parents[1]
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from shared.models.sequential_bootstrap import get_avg_uniqueness  # noqa: E402
from shared.models.sequential_bootstrap_bagging import SequentialBootstrapBaggingClassifier  # noqa: E402


def _toy(n: int = 400, n_bars: int = 2000, hold: int = 40, seed: int = 0):
    rng = np.random.default_rng(seed)
    start = np.sort(rng.integers(0, n_bars - hold - 1, n))
    end = start + rng.integers(5, hold, n)
    x = rng.normal(size=(n, 30))
    y = (x[:, 0] + rng.normal(scale=2.0, size=n) > 0).astype(int)
    return x, y, start, end, n_bars


class EnsembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.x, self.y, self.s, self.e, self.nb = _toy()
        params = {"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced", "max_depth": 2,
                  "random_state": 42}
        self.clf = SequentialBootstrapBaggingClassifier(params, n_estimators=60, random_state=7).fit(
            self.x, self.y, event_start_pos=self.s, event_end_pos=self.e, n_bars=self.nb)

    def test_each_tree_has_its_own_seed(self) -> None:
        """AFML s.6.4 p.98: trees must draw different feature subsets (a shared random_state overrode this)."""
        seeds = {t.get_params()["random_state"] for t in self.clf.estimators_}
        self.assertEqual(len(seeds), len(self.clf.estimators_))
        roots = {int(t.tree_.feature[0]) for t in self.clf.estimators_}
        self.assertGreater(len(roots), 3)

    def test_sample_length_is_avg_uniqueness_times_n(self) -> None:
        """AFML s.6.3.3 p.97 / Snippet 4.5 sLength."""
        avg_u = float(np.mean(get_avg_uniqueness(self.s, self.e, self.nb, np.arange(len(self.y)))))
        self.assertAlmostEqual(self.clf.avg_uniqueness_, avg_u, places=12)
        self.assertEqual(self.clf.sample_length_, int(round(avg_u * len(self.y))))
        self.assertLess(self.clf.sample_length_, len(self.y))

    def test_reproducible(self) -> None:
        params = self.clf.tree_params
        again = SequentialBootstrapBaggingClassifier(params, n_estimators=60, random_state=7).fit(
            self.x, self.y, event_start_pos=self.s, event_end_pos=self.e, n_bars=self.nb)
        self.assertTrue(np.allclose(again.predict_proba(self.x), self.clf.predict_proba(self.x)))

    def test_max_samples_n_keeps_full_draws(self) -> None:
        clf = SequentialBootstrapBaggingClassifier(self.clf.tree_params, n_estimators=3, random_state=1,
                                                   max_samples="n").fit(
            self.x, self.y, event_start_pos=self.s, event_end_pos=self.e, n_bars=self.nb)
        self.assertEqual(clf.sample_length_, len(self.y))


class EdgeScriptTest(unittest.TestCase):
    def test_every_edge_carries_the_ch6_settings(self) -> None:
        for path in sorted(V2.glob("assets/*/edges/*/research/0[23]_*.py")):
            if not path.name.startswith(("02_train", "03_generate")):
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=str(path.relative_to(V2))):
                self.assertIn("N_ESTIMATORS_BAGGING = 1000", text)
                self.assertIn('"random_state": int(rng.integers(0, 2**31 - 1))', text)
                self.assertIn("sample_length=self.sample_length_", text)
                if path.name.startswith("02_"):
                    self.assertIn("OPTUNA_SEARCH_SPACE[", text)
                    self.assertNotIn('"learning_rate": [0.01, 0.08]', text)


if __name__ == "__main__":
    unittest.main()
