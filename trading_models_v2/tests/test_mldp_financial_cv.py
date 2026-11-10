import unittest
import numpy as np
import pandas as pd
from shared.models.financial_cv import (
    embargo_after_test_blocks,
    purge_overlapping_train_indices,
)


def _brute_force_get_train_times(train_idx, test_idx, t0, t1):
    """Literal AFML Snippet 7.1 `getTrainTimes`: the union of the book's
    three named interval-overlap conditions, checked against EVERY
    individual test interval (not merged), O(n*m). Used only as an
    independent, unoptimized reference to fuzz-test the shared
    implementation against -- not itself the production code path."""
    starts = pd.to_datetime(pd.Series(t0), utc=True).to_numpy()
    ends = pd.to_datetime(pd.Series(t1), utc=True).to_numpy()
    keep = []
    for i in train_idx:
        ti0, ti1 = starts[i], ends[i]
        purged = False
        for j in test_idx:
            tj0, tj1 = starts[j], ends[j]
            cond1 = (tj0 <= ti0) and (ti0 <= tj1)  # train starts inside test
            cond2 = (tj0 <= ti1) and (ti1 <= tj1)  # train ends inside test
            cond3 = (ti0 <= tj0) and (tj1 <= ti1)  # train envelops test
            if cond1 or cond2 or cond3:
                purged = True
                break
        if not purged:
            keep.append(int(i))
    return np.array(sorted(keep), dtype=int)


class TestFinancialCV(unittest.TestCase):
    def test_exact_disjoint_interval_purge(self):
        t0 = pd.date_range("2026-01-01", periods=10, freq="h", tz="UTC")
        t1 = pd.Series(t0 + pd.Timedelta(minutes=30))
        # Row 5 sits in the gap between test blocks and must survive.
        # Row 4 starts in the gap but its label remains active into row 7 test time.
        t1.iloc[4] = t0[8]
        test_idx = np.array([2, 3, 7])
        train_idx = np.array([0, 1, 4, 5, 6, 8, 9])
        kept = purge_overlapping_train_indices(train_idx, test_idx, t0, t1)
        self.assertNotIn(4, kept.tolist())
        self.assertIn(5, kept.tolist())

    def test_embargo_is_after_contiguous_test_blocks(self):
        kept = embargo_after_test_blocks(
            train_indices=np.array([0, 1, 4, 5, 6, 8, 9]),
            test_indices=np.array([2, 3, 7]),
            n_samples=10,
            embargo_size=2,
        )
        self.assertEqual(kept.tolist(), [0, 1, 6])

    # --- AFML Snippet 7.1's three named overlap conditions, each in isolation ---

    def test_condition1_train_starts_inside_test(self):
        # test = [10, 20]; train = [15, 30] starts inside the test interval.
        t0 = pd.to_datetime([15], unit="h", utc=True, origin="2026-01-01")
        t1 = pd.to_datetime([30], unit="h", utc=True, origin="2026-01-01")
        test_t0 = pd.to_datetime([10], unit="h", utc=True, origin="2026-01-01")
        test_t1 = pd.to_datetime([20], unit="h", utc=True, origin="2026-01-01")
        starts = pd.concat([pd.Series(test_t0), pd.Series(t0)], ignore_index=True)
        ends = pd.concat([pd.Series(test_t1), pd.Series(t1)], ignore_index=True)
        kept = purge_overlapping_train_indices([1], [0], starts, ends)
        self.assertEqual(kept.tolist(), [])

    def test_condition2_train_ends_inside_test(self):
        # test = [10, 20]; train = [5, 15] ends inside the test interval.
        starts = pd.to_datetime([10, 5], unit="h", utc=True, origin="2026-01-01")
        ends = pd.to_datetime([20, 15], unit="h", utc=True, origin="2026-01-01")
        kept = purge_overlapping_train_indices([1], [0], starts, ends)
        self.assertEqual(kept.tolist(), [])

    def test_condition3_train_envelops_test(self):
        # test = [10, 20]; train = [0, 30] fully envelops the test interval.
        starts = pd.to_datetime([10, 0], unit="h", utc=True, origin="2026-01-01")
        ends = pd.to_datetime([20, 30], unit="h", utc=True, origin="2026-01-01")
        kept = purge_overlapping_train_indices([1], [0], starts, ends)
        self.assertEqual(kept.tolist(), [])

    def test_no_overlap_survives(self):
        # test = [10, 20]; train = [21, 30] -- strictly after, no overlap.
        starts = pd.to_datetime([10, 21], unit="h", utc=True, origin="2026-01-01")
        ends = pd.to_datetime([20, 30], unit="h", utc=True, origin="2026-01-01")
        kept = purge_overlapping_train_indices([1], [0], starts, ends)
        self.assertEqual(kept.tolist(), [1])

    def test_boundary_touching_is_inclusive_overlap(self):
        # test = [10, 20]; train = [20, 30] touches exactly at the boundary --
        # AFML's conditions use <=, so a shared boundary point IS an overlap.
        starts = pd.to_datetime([10, 20], unit="h", utc=True, origin="2026-01-01")
        ends = pd.to_datetime([20, 30], unit="h", utc=True, origin="2026-01-01")
        kept = purge_overlapping_train_indices([1], [0], starts, ends)
        self.assertEqual(kept.tolist(), [])

    def test_fuzz_matches_literal_get_train_times(self):
        """Exact cross-check against an independent, unoptimized literal
        implementation of AFML's getTrainTimes (Snippet 7.1), across many
        randomized interval configurations. This is the load-bearing test
        for A3: it does not merely check a handful of hand-picked cases, it
        confirms the production purge is mathematically equivalent to the
        book's own algorithm across a wide, randomized input space."""
        rng = np.random.default_rng(42)
        n_trials = 500
        for trial in range(n_trials):
            n = int(rng.integers(5, 40))
            trial_starts = np.sort(rng.integers(0, 200, size=n))
            spans = rng.integers(0, 15, size=n)
            trial_ends = trial_starts + spans
            t0 = pd.to_datetime(trial_starts, unit="h", utc=True, origin="2026-01-01")
            t1 = pd.to_datetime(trial_ends, unit="h", utc=True, origin="2026-01-01")
            all_idx = np.arange(n)
            n_test = int(rng.integers(1, max(2, n // 2)))
            test_idx = np.sort(rng.choice(all_idx, size=n_test, replace=False))
            train_idx = np.sort(np.setdiff1d(all_idx, test_idx))
            if len(train_idx) == 0:
                continue
            expected = _brute_force_get_train_times(train_idx, test_idx, t0, t1)
            actual = purge_overlapping_train_indices(train_idx, test_idx, t0, t1)
            self.assertTrue(
                np.array_equal(expected, actual),
                msg=(
                    f"trial {trial}: expected={expected.tolist()} "
                    f"actual={actual.tolist()} starts={trial_starts.tolist()} "
                    f"ends={trial_ends.tolist()} test_idx={test_idx.tolist()}"
                ),
            )


if __name__ == "__main__":
    unittest.main()
