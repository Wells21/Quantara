"""CPCV backtest paths (AFML s.12.4.2 step 5, pp.165-166; owner decision D052, 2026-10-26).

Step 03 writes one column per CPCV path, cpcv_path_XX_raw_probability (the inlined writer in every edge's Step 03 uses the
same template). Step 04 fits the live calibrator on all paths stacked; the portfolio allocator (04b / 04c) scores every
threshold on every path and selects by the mean across paths (AFML s.12.5).
"""
from __future__ import annotations

import re
from typing import Iterable

import numpy as np
import pandas as pd

CPCV_PATH_COLUMN_TEMPLATE = "cpcv_path_{:02d}_raw_probability"
_PATH_RE = re.compile(r"^cpcv_path_(\d{2})_raw_probability$")


def path_columns(columns: Iterable[str]) -> list[str]:
    """CPCV path columns present, in path order."""
    found = sorted((int(m.group(1)), c) for c in columns if (m := _PATH_RE.match(str(c))))
    return [c for _, c in found]


def stacked_path_fit_inputs(frame: pd.DataFrame, target_col: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """All (path forecast, label) pairs, every path stacked; rows a path did not cover are dropped."""
    cols = path_columns(frame.columns)
    if not cols:
        raise ValueError("no cpcv_path_XX_raw_probability columns: run the D052 Step 03 first")
    y = pd.to_numeric(frame[target_col], errors="coerce").to_numpy(dtype=float)
    xs, ys, covered = [], [], []
    for c in cols:
        p = pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(p) & np.isfinite(y)
        xs.append(p[ok]); ys.append(y[ok]); covered.append(int(ok.sum()))
    info = {"n_paths": len(cols), "path_columns": cols, "rows_per_path": covered, "rows_stacked": int(sum(covered))}
    return np.concatenate(xs), np.concatenate(ys).astype(int), info


def stacked_path_fit_weights(frame: pd.DataFrame, target_col: str, weights) -> np.ndarray:
    """Per-row weights replicated in the exact order of stacked_path_fit_inputs (same path order, same row mask)."""
    cols = path_columns(frame.columns)
    if not cols:
        raise ValueError("no cpcv_path_XX_raw_probability columns: run the D052 Step 03 first")
    y = pd.to_numeric(frame[target_col], errors="coerce").to_numpy(dtype=float)
    w = np.asarray(weights, dtype=float)
    if len(w) != len(frame):
        raise ValueError("weights length must match the frame")
    out = []
    for c in cols:
        p = pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float)
        out.append(w[np.isfinite(p) & np.isfinite(y)])
    return np.concatenate(out)
