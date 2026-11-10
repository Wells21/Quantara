from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from shared.live_streaming import dependency_transactions as txn
from shared.live_streaming import state as hot_state


def table(times: list[pd.Timestamp]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Datetime": times,
            "Value": [float(index + 1) for index in range(len(times))],
        }
    )


class DependencyTransactionTests(unittest.TestCase):
    def test_internal_hole_behind_watermark_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            live = pd.date_range("2026-01-01T00:01:00Z", periods=3, freq="min")
            actual = table([static, live[0], live[2]])
            actual.to_csv(path, index=False)
            with self.assertRaises(txn.DependencyTransactionError):
                txn.certify_csv_prefix(
                    path,
                    static_latest=static,
                    expected_live_times=live,
                )

    def test_checkpoint_watermark_must_match_committed_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            table([static]).to_csv(path, index=False)
            metadata = txn.certify_csv_prefix(
                path,
                static_latest=static,
                expected_live_times=[],
            )
            self.assertEqual(
                metadata["committed_last_timestamp"],
                static.isoformat(),
            )
            txn.assert_checkpoint_watermark(
                metadata,
                static,
                dependency="FFD",
            )
            with self.assertRaises(txn.DependencyTransactionError):
                txn.assert_checkpoint_watermark(
                    metadata,
                    static + pd.Timedelta(minutes=1),
                    dependency="FFD",
                )

    def test_incomplete_generation_cannot_touch_durable_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            table([static]).to_csv(path, index=False)
            metadata = txn.certify_csv_prefix(
                path,
                static_latest=static,
                expected_live_times=[],
            )
            before = path.read_bytes()
            expected = pd.date_range("2026-01-01T00:01:00Z", periods=2, freq="min")
            incomplete = table([expected[0]])
            with self.assertRaises(txn.DependencyTransactionError):
                txn.commit_csv_generation(
                    path,
                    transaction=metadata,
                    expected_times=expected,
                    produced_rows=incomplete,
                    columns=["Datetime", "Value"],
                    generation_id_value="incomplete",
                )
            self.assertEqual(path.read_bytes(), before)

    def test_crash_after_append_before_checkpoint_reuses_durable_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            table([static]).to_csv(path, index=False, lineterminator="\n")
            metadata = txn.certify_csv_prefix(
                path,
                static_latest=static,
                expected_live_times=[],
            )
            expected = pd.date_range("2026-01-01T00:01:00Z", periods=2, freq="min")
            produced = table(list(expected))

            payload = produced.to_csv(
                index=False,
                header=False,
                lineterminator="\n",
                float_format="%.17g",
            ).encode("utf-8")
            with path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            result = txn.commit_csv_generation(
                path,
                transaction=metadata,
                expected_times=expected,
                produced_rows=produced,
                columns=["Datetime", "Value"],
                generation_id_value="recovered-generation",
            )
            self.assertEqual(result["durable_reused_rows"], 2)
            self.assertEqual(result["newly_appended_rows"], 0)
            persisted = pd.read_csv(path)
            self.assertEqual(len(persisted), 3)
            self.assertFalse(persisted["Datetime"].duplicated().any())

    def test_sparse_hmm_eligible_timestamps_commit_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hmm.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            table([static]).to_csv(path, index=False, lineterminator="\n")
            metadata = txn.certify_csv_prefix(
                path,
                static_latest=static,
                expected_live_times=[],
            )
            candidates = pd.date_range(
                "2026-01-01T00:01:00Z", periods=5, freq="min"
            )
            eligible = pd.DatetimeIndex([candidates[0], candidates[2], candidates[4]])
            produced = table(list(eligible))
            result = txn.commit_csv_generation(
                path,
                transaction=metadata,
                expected_times=eligible,
                produced_rows=produced,
                columns=["Datetime", "Value"],
                generation_id_value="hmm-sparse",
            )
            self.assertEqual(result["expected_rows"], 3)
            persisted = pd.read_csv(path)
            persisted_times = pd.to_datetime(persisted["Datetime"], utc=True)
            self.assertTrue(
                pd.DatetimeIndex(persisted_times.iloc[1:]).equals(eligible)
            )

    def test_coalesced_latest_snapshot_commits_every_intermediate_bar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            static = pd.Timestamp("2026-01-01T00:00:00Z")
            table([static]).to_csv(path, index=False, lineterminator="\n")
            metadata = txn.certify_csv_prefix(
                path,
                static_latest=static,
                expected_live_times=[],
            )
            suffix = pd.date_range("2026-01-01T00:01:00Z", periods=4, freq="min")
            result = txn.commit_csv_generation(
                path,
                transaction=metadata,
                expected_times=suffix,
                produced_rows=table(list(suffix)),
                columns=["Datetime", "Value"],
                generation_id_value="coalesced-four-bars",
            )
            self.assertEqual(result["expected_rows"], 4)
            persisted = pd.read_csv(path)
            actual = pd.DatetimeIndex(
                pd.to_datetime(persisted["Datetime"], utc=True).iloc[1:]
            )
            self.assertTrue(actual.equals(suffix))

    def test_same_asset_dependency_lock_serializes_mutation(self) -> None:
        active = 0
        maximum = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal active, maximum
            with hot_state.asset_lock("XAUUSD"):
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with guard:
                    active -= 1

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker) for _ in range(4)]
            for future in futures:
                future.result()
        self.assertEqual(maximum, 1)

    def test_stage2_source_contract_is_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        continuation = (
            root / "shared" / "live_streaming" / "continuation.py"
        ).read_text(encoding="utf-8")
        pipeline = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")
        ffd = (
            root / "shared" / "ffd" / "verify_asset_ffd_manifest.py"
        ).read_text(encoding="utf-8")
        hmm = (
            root / "shared" / "regime" / "verify_asset_hmm_regime_manifest.py"
        ).read_text(encoding="utf-8")

        self.assertIn("commit_csv_generation(", continuation)
        self.assertIn("assert_committed_boundary(", continuation)
        self.assertIn("certify_csv_prefix(", ffd)
        self.assertIn("certify_csv_prefix(", hmm)
        # Pipeline versions legitimately advance in later architecture stages.
        # Stage 2 protects the behavioral ownership contract, not a frozen
        # version label: recovery must still enter the same per-asset owner
        # before it can mutate FFD/HMM continuation state.
        self.assertIn(
            "with hot_state.asset_lock(asset):\n"
            "                    result = ns[\"ensure_assets_live_feature_continuation\"](",
            pipeline,
        )


if __name__ == "__main__":
    unittest.main()
