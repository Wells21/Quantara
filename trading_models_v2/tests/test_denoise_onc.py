"""Tests for shared/colab_portable/denoise_onc_snippet.py (imported here via
shared.models.denoise_onc, the local re-export wrapper).

Validated against MLAM Ch.2/Ch.4's own stated experiments (block-diagonal
recovery, denoising RMSE-style condition-number improvement, Marcenko-Pastur
factor recovery under a diluted signal) rather than only hand-picked toy
cases, per the same standard applied to the purge/embargo verification in
tests/test_mldp_financial_cv.py.
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from shared.models.denoise_onc import (
    cluster_kmeans_top,
    cov_to_corr,
    denoise_correlation,
    find_max_eval,
    get_pca,
)


def _random_block_corr(
    n_cols: int, n_blocks: int, block_size: int, *, intrablock_sigma: float, seed: int
) -> tuple[np.ndarray, list[list[int]]]:
    """A simplified version of MLAM's own block-diagonal correlation-matrix
    generator (Code Snippet 4.3): each block shares one common Gaussian
    factor plus block-specific noise (sigma controls how tight the block
    is); blocks are then combined block-diagonally and an overall noise
    term is added, mirroring the book's own construction closely enough to
    exercise ONC's recovery property without reproducing every detail."""
    rng = np.random.default_rng(seed)
    n_obs = 200
    blocks = []
    true_groups: list[list[int]] = []
    col = 0
    for _ in range(n_blocks):
        common = rng.standard_normal((n_obs, 1))
        block = np.repeat(common, block_size, axis=1) + rng.normal(
            scale=intrablock_sigma, size=(n_obs, block_size)
        )
        blocks.append(block)
        true_groups.append(list(range(col, col + block_size)))
        col += block_size
    data = np.concatenate(blocks, axis=1)
    # Add a small amount of independent noise to every column so the matrix
    # isn't degenerate, then compute the empirical correlation matrix.
    data = data + rng.normal(scale=0.05, size=data.shape)
    corr = np.corrcoef(data, rowvar=False)
    return corr, true_groups


class TestMarcenkoPasturFit(unittest.TestCase):
    def test_pure_random_matrix_fits_var_near_one(self):
        rng = np.random.default_rng(0)
        t, n = 5000, 500
        x = rng.standard_normal((t, n))
        corr = np.corrcoef(x, rowvar=False)
        eigenvalues, _ = get_pca(corr)
        q = t / n
        _e_max, var = find_max_eval(np.diag(eigenvalues), q, bwidth=0.01)
        # A purely random correlation matrix should have almost all of its
        # variance attributed to noise (var close to 1).
        self.assertGreater(var, 0.8)

    def test_diluted_signal_recovers_approximate_factor_count(self):
        # MLAM's own worked example (p.27-28): inject a known number of
        # signal factors into an otherwise-random matrix and confirm the
        # Marcenko-Pastur fit recovers a plausible signal factor count.
        rng = np.random.default_rng(1)
        n_cols, n_facts, t = 200, 20, 2000
        w = rng.standard_normal((n_cols, n_facts))
        cov_signal = w @ w.T + np.eye(n_cols) * 0.5
        cov_random = np.eye(n_cols)
        alpha = 0.3  # weight on the random component; rest is signal
        cov = alpha * cov_random + (1 - alpha) * cov_signal
        corr = cov_to_corr(cov)
        result = denoise_correlation(corr, n_obs=t, bwidth=0.05)
        # Recovered factor count should be in the right neighborhood --
        # this is a noisy statistical fit, not an exact reconstruction, so
        # assert a wide but meaningful band rather than an exact value.
        self.assertGreater(result["n_facts"], 0)
        self.assertLess(result["n_facts"], n_cols // 2)


class TestDenoising(unittest.TestCase):
    def test_denoising_lowers_condition_number_and_stays_a_valid_correlation_matrix(self):
        rng = np.random.default_rng(2)
        n_cols, t = 100, 300
        # A noisy, moderately structured correlation matrix.
        w = rng.standard_normal((n_cols, 5))
        cov = w @ w.T + np.eye(n_cols) * rng.uniform(0.5, 1.5, size=n_cols)
        corr = cov_to_corr(cov)
        result = denoise_correlation(corr, n_obs=t, bwidth=0.05)
        denoised = result["denoised_corr"]
        self.assertLess(result["condition_number_after"], result["condition_number_before"])
        self.assertTrue(np.allclose(np.diag(denoised), 1.0, atol=1e-8))
        self.assertTrue(np.allclose(denoised, denoised.T, atol=1e-8))

    def test_detone_removes_market_component_and_matrix_becomes_singular(self):
        rng = np.random.default_rng(3)
        n_cols, t = 60, 400
        market = rng.standard_normal((400, 1))
        data = np.repeat(market, n_cols, axis=1) + rng.standard_normal((400, n_cols)) * 0.5
        corr = np.corrcoef(data, rowvar=False)
        result = denoise_correlation(corr, n_obs=t, bwidth=0.05, detone=True, market_components=1)
        self.assertTrue(result["detoned"])
        detoned = result["detoned_corr"]
        eigenvalues, _ = get_pca(detoned)
        smallest = np.diag(eigenvalues).min()
        self.assertLess(abs(smallest), 1e-6)


class TestOptimalNumberOfClusters(unittest.TestCase):
    def test_recovers_block_structure_after_shuffling(self):
        corr, true_groups = _random_block_corr(
            n_cols=24, n_blocks=4, block_size=6, intrablock_sigma=0.05, seed=42
        )
        labels = np.arange(24)
        rng = np.random.default_rng(42)
        shuffled = rng.permutation(labels)
        corr_df = pd.DataFrame(corr, index=labels, columns=labels)
        corr_df = corr_df.loc[shuffled, shuffled]

        _corr1, clstrs, _silh = cluster_kmeans_top(
            corr_df, max_num_clusters=10, n_init=4, random_state=42
        )

        # ONC should recover exactly 4 clusters...
        self.assertEqual(len(clstrs), 4)
        # ...and every recovered cluster should be a subset of exactly one
        # of the true injected groups (allowing ONC's own cluster-id
        # ordering to differ from the true group ordering).
        true_group_sets = [set(g) for g in true_groups]
        for members in clstrs.values():
            member_set = set(int(m) for m in members)
            self.assertTrue(
                any(member_set <= group for group in true_group_sets),
                msg=f"Cluster {member_set} is not a subset of any true group {true_group_sets}",
            )


if __name__ == "__main__":
    unittest.main()
