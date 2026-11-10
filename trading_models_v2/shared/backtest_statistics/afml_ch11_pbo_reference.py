"""Canonical AFML Chapter 11 "The Dangers of Backtesting" reference:
Combinatorially Symmetric Cross-Validation (CSCV) and the Probability of
Backtest Overfitting (PBO).

Source: Marcos Lopez de Prado, Advances in Financial Machine Learning
(AFML, 2018), Ch.11 s.11.6 "Strategy Selection" (pp.155-157; page citations corrected 2026-10-26
against the book text -- Ch.11 has no s.11.7/11.8 and ends at p.160), which packages the CSCV
algorithm originally published in Bailey, Borwein, Lopez de Prado, and Zhu,
"The Probability of Backtest Overfitting" (Journal of Computational
Finance, 2015). Every step below follows the book's own description of the
algorithm (it does not print a numbered code snippet the way earlier
chapters do; the s.11.6 CSCV steps, pp.155-156, are transcribed here
step-by-step).

This module is a pure-function reference, analogous in spirit to
shared/backtest_statistics/afml_ch14_ch15_reference.py: it takes a
performance matrix (T aligned time/observation blocks x N candidate
strategies) and returns the CSCV diagnostics. It does not know anything
about Quantara's own scenario-search structure -- callers assemble the
performance matrix from their own strategy universe.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any, Callable

import numpy as np
import pandas as pd


class AFMLPBOError(RuntimeError):
    pass


def _split_into_blocks(matrix: np.ndarray, s: int) -> list[np.ndarray]:
    """AFML p.155 (second step): partition the T observations (rows) into S contiguous,
    equal-sized, non-overlapping blocks along the row axis."""
    t = matrix.shape[0]
    if s < 2 or s % 2 != 0:
        raise AFMLPBOError("s (number of blocks) must be an even integer >= 2.")
    if t < s:
        raise AFMLPBOError(f"Not enough rows ({t}) to form {s} blocks.")
    edges = np.linspace(0, t, s + 1).astype(int)
    return [matrix[edges[i]:edges[i + 1]] for i in range(s)]


def cscv_pbo(
    performance_matrix: pd.DataFrame,
    *,
    s: int = 16,
    higher_is_better: bool = True,
    performance_stat_fn: Callable[[np.ndarray], np.ndarray] | None = None,
) -> dict[str, Any]:
    """AFML s.11.6 CSCV (pp.155-156) and the PBO definition (p.156).

    performance_matrix: T rows (aligned observations -- e.g. rolling-start
    windows, all candidates evaluated on the SAME T observations) x N
    columns (candidate strategies/configurations). Values are a continuous
    per-observation performance measure (the book uses returns; any
    per-observation scalar that can be averaged and ranked is valid).

    performance_stat_fn: OPTIONAL. AFML p.155 requires that "the performance
    evaluation metric used to choose the 'optimal' strategy can be estimated
    on subsamples of each column" -- i.e. CSCV's internal per-split winner
    selection must use the SAME metric the researcher actually uses to pick
    their real winner, not an arbitrary proxy. Defaults to None, which
    preserves the original behavior (raw column mean, matching the book's
    own worked example convention and this project's portfolio-level PBO
    check at portfolio_allocator/08b_pbo_cscv_validation.py, whose real
    selection metric IS net-profit mean). If the caller's real selection
    metric is something else (e.g. a drawdown-adjusted ratio), pass a
    function here: given a (rows x N) block matrix, in ROW ORDER as joined
    (train or test), return an (N,) array of per-candidate statistics in
    that same metric. Verified 2026-09-29 against the book text directly
    (not from memory) before this parameter was added -- see
    config/us500_edge_search_ledger.json's PBO_CSCV_AFML_VERIFICATION_2026-09-29.

    Procedure (verbatim from the book):
      1. Partition the T rows into S contiguous equal-sized blocks.
      2. For every combination of S/2 blocks (train) vs the complementary
         S/2 blocks (test) -- C(S, S/2) combinations total -- form the
         train and test performance matrices.
      3. On the train matrix, find the strategy n* with the best performance
         by performance_stat_fn (the in-sample "winner").
      4. On the test matrix, find n*'s relative rank among all N strategies
         (rank_bar in (0,1), computed as (rank)/(N+1) so it is never
         exactly 0 or 1).
      5. logit lambda_c = ln(rank_bar / (1 - rank_bar)). lambda_c > 0 means
         the IS winner was also above the OOS median (good); lambda_c <= 0
         means the IS winner underperformed the OOS median -- a vote for
         overfitting.
      6. PBO = fraction of the C(S, S/2) combinations with lambda_c <= 0.

    Also reports the book's companion diagnostic (Figure 11.1, p.157, "OOS Perf.
    Degradation"): the linear regression of OOS performance on IS
    performance for the selected winners across all combinations. A
    negative slope means better in-sample performance predicts WORSE
    out-of-sample performance -- the clearest possible overfitting signal.
    """
    if performance_matrix.shape[1] < 2:
        raise AFMLPBOError("Need at least 2 candidate strategies to run CSCV.")
    cols = list(performance_matrix.columns)
    n = len(cols)
    mat = performance_matrix.to_numpy(dtype="float64")
    if not np.isfinite(mat).all():
        raise AFMLPBOError("performance_matrix must not contain NaN/inf.")
    sign = 1.0 if higher_is_better else -1.0

    def _stat(block_mat: np.ndarray) -> np.ndarray:
        if performance_stat_fn is not None:
            return sign * np.asarray(performance_stat_fn(block_mat), dtype="float64")
        return sign * block_mat.mean(axis=0)

    blocks = _split_into_blocks(mat, s)
    block_indices = list(range(s))
    half = s // 2

    logits: list[float] = []
    is_perf: list[float] = []
    oos_perf: list[float] = []
    winner_counts = np.zeros(n, dtype="int64")

    for train_idx in combinations(block_indices, half):
        test_idx = tuple(i for i in block_indices if i not in train_idx)
        train_mat = np.vstack([blocks[i] for i in train_idx])
        test_mat = np.vstack([blocks[i] for i in test_idx])

        train_mean = _stat(train_mat)
        test_mean = _stat(test_mat)

        n_star = int(np.argmax(train_mean))
        winner_counts[n_star] += 1

        # Relative rank of n* in the OOS set, per the book's convention:
        # rank 1 = worst, rank N = best; rank_bar = rank/(N+1) in (0,1).
        order = np.argsort(test_mean)  # ascending: order[0] is worst
        rank_of_n_star = int(np.where(order == n_star)[0][0]) + 1  # 1-indexed
        rank_bar = rank_of_n_star / (n + 1)
        logit = float(np.log(rank_bar / (1.0 - rank_bar)))

        logits.append(logit)
        is_perf.append(float(train_mean[n_star]))
        oos_perf.append(float(test_mean[n_star]))

    logits_arr = np.asarray(logits, dtype="float64")
    n_combinations = len(logits_arr)
    pbo = float(np.mean(logits_arr <= 0.0))

    # Performance degradation regression (AFML Figure 11.1, p.157): OOS perf on IS perf,
    # across the selected winners of every train/test split.
    is_arr = np.asarray(is_perf, dtype="float64")
    oos_arr = np.asarray(oos_perf, dtype="float64")
    if np.std(is_arr) > 0:
        slope, intercept = np.polyfit(is_arr, oos_arr, 1)
    else:
        slope, intercept = float("nan"), float("nan")

    # Stochastic dominance diagnostic (Bailey et al. 2017a; not in AFML Ch.11): fraction of OOS winner
    # performances that are non-positive (per the book's own performance
    # measure convention -- here: below the OOS cross-sectional mean of 0
    # after the sign convention, i.e. literally non-positive in the same
    # units as the input matrix).
    prob_oos_loss = float(np.mean(oos_arr <= 0.0))

    return {
        "n_strategies": n,
        "n_observations": int(mat.shape[0]),
        "n_blocks": s,
        "n_combinations": n_combinations,
        "pbo": pbo,
        "mean_logit": float(np.mean(logits_arr)),
        "median_logit": float(np.median(logits_arr)),
        "performance_degradation_slope": float(slope),
        "performance_degradation_intercept": float(intercept),
        "prob_oos_winner_nonpositive": prob_oos_loss,
        "is_winner_selection_counts": {cols[i]: int(winner_counts[i]) for i in range(n)},
        "candidate_columns": cols,
    }
