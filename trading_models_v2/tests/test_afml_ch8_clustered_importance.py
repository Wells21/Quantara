"""AFML Ch.8 / MLAM Ch.6 walkthrough (2026-10-24): clustered feature importance reproduces MLAM's synthetic experiment.

MLAM s.6.6 (pp.87-90): on getTestData(40, 5, 30, sigmaStd=.1) ONC finds six clusters (one per informative feature with
its redundant copies, plus the noise cluster) and clustered MDA/MDI give the noise cluster essentially zero importance.
REDUCED SIZE (2,000 rows, 100 trees, 5 folds; approved deviation D038) -- a fast regression check only. The validation is
the book-exact run shared/research/mlam_ch6_synthetic_replication.py (10,000 rows, 1,000 trees, KFold 10).

Run from trading_models_v2:  python -m unittest tests.test_afml_ch8_clustered_importance
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

from shared.features import clustered_feature_importance as cfi  # noqa: E402


class SyntheticClusteredImportanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.x, y = cfi.get_test_data(40, 5, 30, 2000, sigma_std=0.1)
        cls.y = y.to_numpy()
        n = len(cls.x)
        cls.w = np.ones(n)
        obs, _ = cfi.observation_matrix(cls.x, "corr_signed", denoise=False, reduce=False)
        cls.clusters, cls.silh = cfi.onc_base(obs, max_k=10, n_init=10, seed=0)
        t0 = pd.date_range("2000-01-03", periods=n, freq="B").values
        cls.splits = cfi.purged_kfold_splits(t0, t0, 5, 0)
        pos = np.arange(n)
        cls.bags, cls.draws = cfi.fit_fold_bags(cls.x, cls.y, cls.w, cls.splits, pos, pos, n, n_trees=100, seed=0,
                                               params=cfi.MLAM_TREE_PARAMS)
        groups = {f"C_{i}": m for i, m in cls.clusters.items()}
        cls.cmda, _ = cfi.mda(cls.x, cls.y, cls.w, cls.splits, cls.bags, groups)
        d, s = cfi._draws(pos, pos, n, 100, 9)
        cls.full = cfi._fit_bag(cls.x.to_numpy(), cls.y, cls.w, d, s, cfi.MLAM_TREE_PARAMS)
        cls.cmdi = cfi.mdi([cls.full], list(cls.x.columns), cls.clusters)

    def _noise_key(self) -> str:
        for i, m in self.clusters.items():
            if all(f.startswith("N_") for f in m):
                return f"C_{i}"
        self.fail("no pure-noise cluster found")

    def test_onc_finds_mlam_six_clusters(self) -> None:
        self.assertEqual(len(self.clusters), 6)
        for members in self.clusters.values():
            kinds = {f[0] for f in members}
            self.assertTrue(kinds == {"N"} or (kinds <= {"I", "R"} and sum(f.startswith("I_") for f in members) == 1),
                            members)

    def test_noise_cluster_has_essentially_zero_importance(self) -> None:
        noise = self._noise_key()
        others = self.cmda.drop(noise)["mean"]
        self.assertEqual(self.cmda["mean"].idxmin(), noise)
        self.assertLess(self.cmda.loc[noise, "mean"], 0.1 * others.median())
        self.assertEqual(self.cmdi["mean"].idxmin(), noise)

    def test_mdi_normalised_and_mda_relative(self) -> None:
        self.assertAlmostEqual(float(self.cmdi["mean"].sum()), 1.0, places=9)
        self.assertTrue((self.cmda["std"] >= 0).all())

    def test_purged_kfold_respects_overlap_and_embargo(self) -> None:
        t0 = pd.date_range("2024-01-01", periods=100, freq="h").values
        t1 = t0 + np.timedelta64(5, "h")
        for train, test in cfi.purged_kfold_splits(t0, t1, 5, 3):
            lo, hi = t0[test].min(), t1[test].max()
            self.assertFalse(((t0[train] <= hi) & (t1[train] >= lo)).any())
            self.assertFalse(((train > test.max()) & (train <= test.max() + 3)).any())

    def test_variation_of_information_bounds(self) -> None:
        a = self.x["I_0"].to_numpy()
        self.assertAlmostEqual(cfi.var_info(a, a), 0.0, places=6)
        self.assertGreater(cfi.var_info(a, self.x["N_0"].to_numpy()), 0.9)


class OncBestOfRestartsTest(unittest.TestCase):
    """D077 (MLAM Ch.4 T9): Step 01 keeps the highest-q partition over ONC restarts, ties to the lowest seed."""

    def test_best_of_restarts_is_the_max_q_restart(self) -> None:
        x, _ = cfi.get_test_data(20, 3, 10, 500, sigma_std=1.0)
        obs, _ = cfi.observation_matrix(x, "corr_signed", denoise=False, reduce=False)
        clusters, silh, search = cfi.onc_best_of_restarts(obs, n_restarts=4, n_init=2, seed=3)
        q = {s: cfi._quality(cfi.onc_top(obs, None, 2, s)[1].to_numpy()) for s in range(3, 7)}
        best = max(q, key=lambda s: (q[s], -s))
        self.assertEqual(search["onc_best_seed"], best)
        self.assertEqual(sorted(search["onc_q_by_seed"]), list(range(3, 7)))
        ref, _ = cfi.onc_top(obs, None, 2, best)
        self.assertEqual(sorted(map(sorted, clusters.values())), sorted(map(sorted, ref.values())))
        self.assertAlmostEqual(cfi._quality(silh.to_numpy()), q[best])


if __name__ == "__main__":
    unittest.main()


class Step01WiringTest(unittest.TestCase):
    """Every edge's Step 01 runs one pass of the shared MLAM clustered importance (owner decisions 2026-10-24)."""

    def test_every_step01_uses_shared_clustered_importance(self) -> None:
        scripts = sorted(V2.glob("assets/*/edges/*/research/01_feature_surgery_clustered_mda.py"))
        self.assertTrue(scripts)
        for path in scripts:
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=str(path.relative_to(V2))):
                self.assertIn("from shared.features.clustered_feature_importance import select_features_clustered", text)
                self.assertIn("= run_mlam_clustered_feature_importance(", text)
                self.assertNotIn("= iterative_feature_surgery(", text)

    def test_select_features_clustered_runs_and_reports(self) -> None:
        x, y = cfi.get_test_data(20, 3, 10, 600, sigma_std=0.1)
        n = len(x)
        t0 = pd.date_range("2000-01-03", periods=n, freq="B").values
        pos = np.arange(n)
        r = cfi.select_features_clustered(x, y.to_numpy(), np.ones(n), t0, t0, pos, pos, n, 0, workers=1, n_trees=20,
                                          n_splits=3, method="corr_signed")
        for key in ("selected_features", "clusters", "oos_neg_log_loss_weighted", "oos_auc", "estimator", "cv"):
            self.assertIn(key, r)
        self.assertEqual(r["estimator"]["min_weight_fraction_leaf"], 0.05)
        self.assertEqual(r["mda_shuffles_per_fold"], 5)  # D045
        self.assertEqual(sum(c["n_features"] for c in r["clusters"]), 20)
