"""Canonical Colab-portable purge/embargo snippet -- single source of truth.

WHY THIS FILE EXISTS (do not "fix" this by importing shared.models instead):
Quantara's edge Step 01/02 research scripts (feature surgery, CPCV model
training) are not run from this machine/repo checkout. They are uploaded to
Google Colab as standalone files -- only the one script plus the data files
it needs travel there, never the rest of the `shared/` package -- run there,
and the resulting artifacts are downloaded back. Those scripts therefore
cannot `import shared.models.financial_cv` at runtime: the import would fail
on Colab. This is a real, permanent operational constraint, not a shortcut
to eventually remove.

The purge/embargo logic those scripts need is nonetheless verified exact
(see docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md and
tests/test_mldp_financial_cv.py's fuzz test, which cross-checks this exact
function pair against an independent literal implementation of AFML's
`getTrainTimes`, Snippet 7.1, across 500 randomized trials plus the three
named overlap conditions individually -- zero mismatches).

This file is the ONE canonical copy of that verified-correct, dependency-
free function pair. `tests/test_colab_portable_snippet_sync.py` scans every
edge's actual Step 01/02/03 script and asserts its local
`purge_train_overlaps`/`apply_embargo` definitions match this file's
function bodies byte-for-byte (after whitespace normalization), across all
occurrences in the repository -- so silent drift in any of them is caught by
the test suite even though none of them import this file at runtime.

When creating a NEW edge's Step 01/02 script: copy these two functions from
this file verbatim. Do not hand-retype them, and do not "improve" them
in-place in one edge's script without updating this file (and re-running the
sync test) first -- a change made only in one copy is exactly the silent-
drift failure mode this file exists to prevent.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def purge_train_overlaps(train_idx: np.ndarray, test_idx: np.ndarray, index: pd.Index, t1: pd.Series) -> np.ndarray:
    train_idx = np.asarray(train_idx, dtype=int)
    test_idx = np.asarray(test_idx, dtype=int)
    if len(train_idx) == 0 or len(test_idx) == 0:
        return train_idx
    train_starts = index[train_idx].values.astype("datetime64[ns]")
    train_ends = t1.iloc[train_idx].values.astype("datetime64[ns]")
    test_starts = index[test_idx].values.astype("datetime64[ns]")
    test_ends = t1.iloc[test_idx].values.astype("datetime64[ns]")
    keep_mask = np.ones(len(train_idx), dtype=bool)
    for i in range(len(train_idx)):
        overlaps = (train_starts[i] <= test_ends) & (train_ends[i] >= test_starts)
        if overlaps.any():
            keep_mask[i] = False
    return train_idx[keep_mask]


def apply_embargo(train_idx: np.ndarray, test_idx: np.ndarray, n_samples: int, embargo_size: int) -> np.ndarray:
    train_idx = np.asarray(train_idx, dtype=int)
    test_idx = np.sort(np.unique(np.asarray(test_idx, dtype=int)))
    if len(train_idx) == 0 or len(test_idx) == 0 or embargo_size <= 0:
        return train_idx
    keep_mask = np.ones(len(train_idx), dtype=bool)
    block_ends = []
    for i in range(1, len(test_idx)):
        if test_idx[i] != test_idx[i - 1] + 1:
            block_ends.append(test_idx[i - 1])
    block_ends.append(test_idx[-1])
    for block_end in block_ends:
        embargo_start = block_end + 1
        embargo_end = min(embargo_start + embargo_size, n_samples)
        keep_mask &= (train_idx < embargo_start) | (train_idx >= embargo_end)
    return train_idx[keep_mask]
