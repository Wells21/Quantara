"""Canonical AFML financial-CV interval primitives.

AFML Chapter 7 purges training labels whose information intervals overlap
testing labels and embargoes observations immediately after each testing block.
This module is the single low-level interval authority for Quantara.
"""
from __future__ import annotations
from typing import Any, Sequence
import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

class FinancialCVError(RuntimeError):
    pass

def _utc_ns(values: Sequence[Any], name: str) -> np.ndarray:
    series = to_datetime_iso(pd.Series(values), utc=True, errors="coerce")
    if series.isna().any():
        raise FinancialCVError(f"{name} contains NaT/unparseable timestamps.")
    return series.astype("int64").to_numpy(dtype="int64")

def validate_intervals(
    t0: Sequence[Any], t1: Sequence[Any]
) -> tuple[np.ndarray, np.ndarray]:
    starts = _utc_ns(t0, "t0")
    ends = _utc_ns(t1, "t1")
    if len(starts) != len(ends):
        raise FinancialCVError("t0 and t1 length mismatch.")
    if np.any(ends < starts):
        raise FinancialCVError("At least one label interval ends before it starts.")
    return starts, ends

def contiguous_index_blocks(indices: Sequence[int]) -> list[tuple[int, int]]:
    idx = np.sort(np.unique(np.asarray(indices, dtype=int)))
    if len(idx) == 0:
        return []
    blocks: list[tuple[int, int]] = []
    start = prev = int(idx[0])
    for value in idx[1:]:
        value = int(value)
        if value != prev + 1:
            blocks.append((start, prev))
            start = value
        prev = value
    blocks.append((start, prev))
    return blocks

def merge_time_intervals(
    starts: np.ndarray, ends: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    if len(starts) == 0:
        return np.array([], dtype="int64"), np.array([], dtype="int64")
    order = np.argsort(starts, kind="stable")
    s = starts[order]
    e = ends[order]
    merged_s: list[int] = [int(s[0])]
    merged_e: list[int] = [int(e[0])]
    for start, end in zip(s[1:], e[1:]):
        start_i, end_i = int(start), int(end)
        if start_i <= merged_e[-1]:
            merged_e[-1] = max(merged_e[-1], end_i)
        else:
            merged_s.append(start_i)
            merged_e.append(end_i)
    return np.asarray(merged_s, dtype="int64"), np.asarray(merged_e, dtype="int64")

def purge_overlapping_train_indices(
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    t0: Sequence[Any],
    t1: Sequence[Any],
) -> np.ndarray:
    """Remove every train interval intersecting any test information interval."""
    starts, ends = validate_intervals(t0, t1)
    trn = np.sort(np.unique(np.asarray(train_indices, dtype=int)))
    tst = np.sort(np.unique(np.asarray(test_indices, dtype=int)))
    n = len(starts)
    if np.any(trn < 0) or np.any(trn >= n) or np.any(tst < 0) or np.any(tst >= n):
        raise FinancialCVError("train/test index out of bounds.")
    if len(trn) == 0 or len(tst) == 0:
        return trn.astype(int)
    test_s, test_e = merge_time_intervals(starts[tst], ends[tst])
    keep = np.ones(len(trn), dtype=bool)
    for pos, row in enumerate(trn):
        train_start = starts[row]
        train_end = ends[row]
        # Find the last test interval whose start is <= train_end.
        j = int(np.searchsorted(test_s, train_end, side="right") - 1)
        if j >= 0 and test_e[j] >= train_start:
            keep[pos] = False
    return trn[keep].astype(int)

def embargo_after_test_blocks(
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    n_samples: int,
    embargo_size: int,
) -> np.ndarray:
    """Embargo row positions immediately after each contiguous test block."""
    if embargo_size < 0:
        raise FinancialCVError("embargo_size must be >= 0.")
    trn = np.sort(np.unique(np.asarray(train_indices, dtype=int)))
    tst = np.sort(np.unique(np.asarray(test_indices, dtype=int)))
    if len(trn) == 0 or len(tst) == 0 or embargo_size == 0:
        return trn.astype(int)
    keep = np.ones(len(trn), dtype=bool)
    for _, block_end in contiguous_index_blocks(tst):
        start = block_end + 1
        stop = min(int(n_samples), start + int(embargo_size))
        keep &= (trn < start) | (trn >= stop)
    return trn[keep].astype(int)

def purge_and_embargo_train_indices(
    train_indices: Sequence[int],
    test_indices: Sequence[int],
    t0: Sequence[Any],
    t1: Sequence[Any],
    *,
    embargo_size: int = 0,
) -> np.ndarray:
    purged = purge_overlapping_train_indices(train_indices, test_indices, t0, t1)
    return embargo_after_test_blocks(
        purged, test_indices, len(t0), int(embargo_size)
    )
