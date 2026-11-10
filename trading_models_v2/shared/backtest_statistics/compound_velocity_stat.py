"""Drawdown-adjusted compound-velocity per-subsample statistic for cscv_pbo.

Given a (rows x N) block of daily log-growth-contribution values (in whatever
row order they were joined for this CSCV combination), compute per candidate:
  velocity = mean(column)   (log-growth per day, matches the real
             compound_velocity_score's numerator exactly)
  max_drawdown = max drawdown of the CUMULATIVE log-growth path formed by
             cumsum(column) IN THE GIVEN ROW ORDER (the CSCV-joined order,
             per AFML's own treatment of a joined block-set as one sequence)
  score = velocity / max(max_drawdown, EPS)

This mirrors this project's real edge-search Gate C selection rule
(argmax(compound_velocity_score) = velocity / max_drawdown_of_log_growth_curve)
so the CSCV internal per-split "winner" is selected by the SAME metric used
for real selection, per AFML p.155's explicit requirement.

Moved into the repo 2026-10-22 from the session scratchpad (afml_cv_stat.py, written 2026-09-29/2026-10-14), byte-for-byte
in logic; used by shared/research/gate_c_pbo.py.
"""
import numpy as np

EPS = 1e-12


def compound_velocity_stat(block: np.ndarray) -> np.ndarray:
    n_days, n_cols = block.shape
    out = np.empty(n_cols, dtype="float64")
    for j in range(n_cols):
        col = block[:, j]
        velocity = float(np.mean(col))
        cum = np.cumsum(col)
        running_max = np.maximum.accumulate(cum)
        drawdown = running_max - cum
        mdd = max(float(np.max(drawdown)), EPS)
        out[j] = velocity / mdd
    return out
