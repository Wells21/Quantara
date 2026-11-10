"""Shared AFML CPCV training utilities for SOTD.

This module is edge-agnostic. Edge Step 02/03 scripts should pass model rows,
selected features, labels, t1/end-time columns, and config into this shared
logic instead of carrying their own CPCV/purge/embargo/weighting implementation.
"""

from __future__ import annotations

# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1

from dataclasses import dataclass
from itertools import combinations
from typing import Any, Callable, Iterable, Iterator, Sequence

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso
from sklearn.metrics import f1_score



try:
    from shared.models.afml_sample_weights import compute_afml_event_weight_table
except ImportError:  # direct execution from shared/models
    from afml_sample_weights import compute_afml_event_weight_table

try:
    from shared.models.financial_cv import (
        embargo_after_test_blocks,
        purge_overlapping_train_indices,
    )
except ImportError:  # direct execution from shared/models
    from financial_cv import embargo_after_test_blocks, purge_overlapping_train_indices

# QUANTARA_MLDP_TASK1_FOUNDATION_V1

class CPCVTrainingError(RuntimeError):
    """Raised when CPCV training inputs or folds are invalid."""


@dataclass(frozen=True)
class CPCVSplit:
    split_id: int
    train_indices: np.ndarray
    test_indices: np.ndarray
    test_fold_ids: tuple[int, ...]
    purged_train_count: int
    embargoed_train_count: int


class CombinatorialPurgedCV:
    """Combinatorial Purged CV with manual t1-overlap purge and embargo.

    Parameters
    ----------
    n_splits:
        Number of sequential folds.
    n_test_splits:
        Number of folds held out in each combinatorial test split.
    embargo_size:
        Number of row positions to embargo after each test block.
    t1:
        Event end times aligned to rows. Used to purge training events whose
        [t0, t1] interval overlaps test event start times.
    time_index:
        Event start times aligned to rows. Defaults to row index if omitted.
    """

    def __init__(
        self,
        n_splits: int = 5,
        n_test_splits: int = 2,
        embargo_size: int = 0,
        t1: Sequence[Any] | pd.Series | None = None,
        time_index: Sequence[Any] | pd.Series | None = None,
    ) -> None:
        if n_splits < 2:
            raise CPCVTrainingError("n_splits must be >= 2")
        if n_test_splits < 1 or n_test_splits >= n_splits:
            raise CPCVTrainingError("n_test_splits must be >=1 and < n_splits")
        if embargo_size < 0:
            raise CPCVTrainingError("embargo_size must be >= 0")
        self.n_splits = int(n_splits)
        self.n_test_splits = int(n_test_splits)
        self.embargo_size = int(embargo_size)
        self.t1 = None if t1 is None else to_datetime_iso(pd.Series(t1), utc=True, errors="coerce")
        self.time_index = None if time_index is None else to_datetime_iso(pd.Series(time_index), utc=True, errors="coerce")

    def _fold_bounds(self, n_samples: int) -> list[np.ndarray]:
        if n_samples < self.n_splits:
            raise CPCVTrainingError(f"n_samples={n_samples} is smaller than n_splits={self.n_splits}")
        return [fold.astype(int) for fold in np.array_split(np.arange(n_samples), self.n_splits)]

    def split(self, x_data: Any, y: Any | None = None) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        for split in self.iter_splits(x_data, y):
            yield split.train_indices, split.test_indices

    def iter_splits(self, x_data: Any, y: Any | None = None) -> Iterator[CPCVSplit]:
        n_samples = len(x_data)
        folds = self._fold_bounds(n_samples)
        split_id = 0
        for test_fold_ids in combinations(range(self.n_splits), self.n_test_splits):
            test_indices = np.sort(np.concatenate([folds[i] for i in test_fold_ids]))
            train_mask = np.ones(n_samples, dtype=bool)
            train_mask[test_indices] = False
            purged = self._purge_mask(train_mask.copy(), test_indices)
            embargoed = self._embargo_mask(purged.copy(), test_indices, n_samples)
            train_indices = np.flatnonzero(embargoed)
            if len(train_indices) == 0 or len(test_indices) == 0:
                raise CPCVTrainingError(f"CPCV split {split_id} produced empty train/test indices")
            yield CPCVSplit(
                split_id=split_id,
                train_indices=train_indices.astype(int),
                test_indices=test_indices.astype(int),
                test_fold_ids=tuple(int(i) for i in test_fold_ids),
                purged_train_count=int(train_mask.sum() - purged.sum()),
                embargoed_train_count=int(purged.sum() - embargoed.sum()),
            )
            split_id += 1

    def _purge_mask(self, train_mask: np.ndarray, test_indices: np.ndarray) -> np.ndarray:
        if self.t1 is None or self.time_index is None:
            return train_mask
        t0 = self.time_index.reset_index(drop=True)
        t1 = self.t1.reset_index(drop=True)
        if len(t0) != len(train_mask) or len(t1) != len(train_mask):
            raise CPCVTrainingError("t1/time_index length must match input rows")
        train_idx = np.flatnonzero(train_mask)
        try:
            kept = purge_overlapping_train_indices(train_idx, test_indices, t0, t1)
        except Exception as exc:
            raise CPCVTrainingError(f"Exact AFML interval purge failed: {exc}") from exc
        out = np.zeros(len(train_mask), dtype=bool)
        out[kept] = True
        return out

    def _embargo_mask(self, train_mask: np.ndarray, test_indices: np.ndarray, n_samples: int) -> np.ndarray:
        if self.embargo_size <= 0:
            return train_mask
        train_idx = np.flatnonzero(train_mask)
        try:
            kept = embargo_after_test_blocks(
                train_idx, test_indices, n_samples, self.embargo_size
            )
        except Exception as exc:
            raise CPCVTrainingError(f"AFML test-block embargo failed: {exc}") from exc
        out = np.zeros(n_samples, dtype=bool)
        out[kept] = True
        return out


def average_uniqueness_weights(
    t0: Sequence[Any],
    t1: Sequence[Any],
    *,
    bar_index: Sequence[Any] | None = None,
) -> np.ndarray:
    """Compute AFML average uniqueness on the underlying price-bar clock.

    The old event-grid approximation is intentionally rejected. Callers must
    supply the actual bar_index used by the labelled events.
    """
    if bar_index is None:
        raise CPCVTrainingError(
            "average_uniqueness_weights now requires bar_index; event-grid uniqueness is not AFML-correct."
        )
    # Close values do not affect uniqueness; a positive dummy series is enough
    # for the shared AFML engine's interval/concurrency validation.
    dummy_close = np.exp(np.linspace(0.0, 1e-6 * max(len(bar_index) - 1, 0), len(bar_index)))
    try:
        table = compute_afml_event_weight_table(t0, t1, bar_index, dummy_close)
    except Exception as exc:
        raise CPCVTrainingError(f"AFML uniqueness construction failed: {exc}") from exc
    return table["average_uniqueness"].to_numpy(dtype="float64")



def class_weight_vector(y: Sequence[Any], mode: str = "balanced") -> np.ndarray:
    y_arr = np.asarray(y)
    if mode in {"none", "off", "disabled", ""}:
        return np.ones(len(y_arr), dtype="float64")
    values, counts = np.unique(y_arr, return_counts=True)
    if len(values) < 2:
        return np.ones(len(y_arr), dtype="float64")
    total = float(len(y_arr))
    weight_by_class = {value: total / (len(values) * float(count)) for value, count in zip(values, counts)}
    return np.asarray([weight_by_class[value] for value in y_arr], dtype="float64")


def event_vol_weight_vector(event_vol: Sequence[Any] | None, n_rows: int) -> np.ndarray:
    if event_vol is None:
        return np.ones(n_rows, dtype="float64")
    vol = pd.to_numeric(pd.Series(event_vol), errors="coerce").astype("float64")
    vol = vol.replace([np.inf, -np.inf], np.nan).fillna(vol.median())
    if len(vol) != n_rows:
        raise CPCVTrainingError("Event_Vol length must match number of rows")
    median = float(vol.median())
    if not np.isfinite(median) or median <= 0:
        return np.ones(n_rows, dtype="float64")
    weights = np.sqrt(np.clip(vol.to_numpy(dtype="float64") / median, 0.25, 4.0))
    mean = float(np.mean(weights))
    return weights / mean if mean > 0 else weights


def combined_sample_weight(
    y: Sequence[Any],
    *,
    class_weight_mode: str = "balanced",
    event_vol: Sequence[Any] | None = None,
    t0: Sequence[Any] | None = None,
    t1: Sequence[Any] | None = None,
    bar_index: Sequence[Any] | None = None,
    close: Sequence[Any] | None = None,
) -> np.ndarray:
    """Combine class balancing with canonical AFML financial observation weights.

    event_vol is retained only for source/API compatibility and is deliberately
    NOT multiplied into the observation weight.
    """
    n_rows = len(y)
    weights = class_weight_vector(y, class_weight_mode)
    if event_vol is not None:
        # Explicitly ignored: Event_Vol is barrier geometry, not AFML attribution.
        if len(event_vol) != n_rows:
            raise CPCVTrainingError("Event_Vol length must match number of rows")
    if (t0 is None) != (t1 is None):
        raise CPCVTrainingError("t0 and t1 must be supplied together")
    if t0 is not None:
        if bar_index is None or close is None:
            raise CPCVTrainingError(
                "Canonical AFML weighting requires bar_index and close when t0/t1 are supplied."
            )
        try:
            table = compute_afml_event_weight_table(t0, t1, bar_index, close)
        except Exception as exc:
            raise CPCVTrainingError(f"AFML return-attribution construction failed: {exc}") from exc
        financial = table["return_attribution_weight"].to_numpy(dtype="float64")
        if len(financial) != n_rows:
            raise CPCVTrainingError("AFML financial weight length must match y")
        weights = weights * financial
    mean = float(np.mean(weights)) if len(weights) else 1.0
    if not np.isfinite(weights).all() or not np.isfinite(mean) or mean <= 0:
        raise CPCVTrainingError("Combined sample weights are invalid/non-positive")
    return weights / mean



def select_f1_threshold_train_only(
    y_true: Sequence[Any],
    probability: Sequence[Any],
    thresholds: Iterable[float] | None = None,
) -> dict[str, float]:
    """Select a classification threshold using only train-fold predictions."""
    y_arr = np.asarray(y_true).astype(int)
    p_arr = np.asarray(probability, dtype="float64")
    if thresholds is None:
        thresholds = np.linspace(0.05, 0.95, 91)
    best_threshold = 0.5
    best_f1 = -1.0
    for threshold in thresholds:
        pred = (p_arr >= float(threshold)).astype(int)
        score = float(f1_score(y_arr, pred, zero_division=0))
        if score > best_f1:
            best_f1 = score
            best_threshold = float(threshold)
    return {"threshold": best_threshold, "f1": best_f1}


def cpcv_oof_predictions(
    model_factory: Callable[[], Any],
    x_data: pd.DataFrame,
    y: Sequence[Any],
    cv: CombinatorialPurgedCV,
    *,
    sample_weight: Sequence[Any] | None = None,
    positive_class: int = 1,
) -> pd.DataFrame:
    """Generate out-of-fold probabilities with shared CPCV logic."""
    if len(x_data) != len(y):
        raise CPCVTrainingError("x_data and y length mismatch")
    y_arr = np.asarray(y).astype(int)
    weights = None if sample_weight is None else np.asarray(sample_weight, dtype="float64")
    rows: list[pd.DataFrame] = []
    for split in cv.iter_splits(x_data, y_arr):
        model = model_factory()
        fit_kwargs: dict[str, Any] = {}
        if weights is not None:
            fit_kwargs["sample_weight"] = weights[split.train_indices]
        model.fit(x_data.iloc[split.train_indices], y_arr[split.train_indices], **fit_kwargs)
        proba = np.asarray(model.predict_proba(x_data.iloc[split.test_indices]))
        classes = getattr(model, "classes_", None)
        if classes is None:
            pos = proba[:, -1]
        else:
            class_to_col = {int(cls): i for i, cls in enumerate(classes)}
            if positive_class not in class_to_col:
                raise CPCVTrainingError(f"Model classes_ does not contain positive class {positive_class}")
            pos = proba[:, class_to_col[positive_class]]
        rows.append(pd.DataFrame({
            "row_pos": split.test_indices,
            "cpcv_split_id": split.split_id,
            "test_fold_ids": ",".join(str(i) for i in split.test_fold_ids),
            "raw_probability": pos.astype("float64"),
            "label": y_arr[split.test_indices],
        }))
    out = pd.concat(rows, ignore_index=True).sort_values(["row_pos", "cpcv_split_id"]).reset_index(drop=True)
    return out


def _run_self_test() -> None:
    idx = pd.date_range("2020-01-01", periods=30, freq="h", tz="UTC")
    t1 = idx + pd.Timedelta(hours=3)
    cv = CombinatorialPurgedCV(n_splits=5, n_test_splits=2, embargo_size=1, t1=t1, time_index=idx)
    splits = list(cv.iter_splits(pd.DataFrame({"x": np.arange(30)})))
    if len(splits) != 10:
        raise SystemExit(f"Expected 10 CPCV splits, got {len(splits)}")
    close = np.exp(np.linspace(np.log(100.0), np.log(103.0), len(idx)))
    weights = combined_sample_weight(
        [0, 1, 1, 0],
        event_vol=[1.0, 2.0, 1.5, 1.0],
        t0=idx[:4],
        t1=t1[:4],
        bar_index=idx,
        close=close,
    )
    if len(weights) != 4 or not np.isfinite(weights).all() or not np.isclose(float(np.mean(weights)), 1.0):
        raise SystemExit("Sample-weight self-test failed")
    print("shared.models.cpcv_training self-test passed")
    print(f"CPCV splits: {len(splits)}")



if __name__ == "__main__":
    _run_self_test()
