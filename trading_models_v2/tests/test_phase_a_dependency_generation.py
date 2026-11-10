from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import joblib
import pandas as pd

from shared.live_streaming import continuation
from shared.live_streaming import generation_contract
from shared.live_streaming import state as hot_state


def transaction(timestamp: str, generation_id: str, sequence: int) -> dict[str, object]:
    return {
        "schema_version": "v2_dependency_timestamp_generation_v1",
        "committed_size_bytes": 1000 + sequence,
        "committed_mtime_ns": 1,
        "boundary_anchor_sha256": "a" * 64,
        "committed_live_row_count": sequence,
        "committed_last_timestamp": pd.Timestamp(timestamp).isoformat(),
        "timestamp_chain_sha256": "b" * 64,
        "generation_sequence": sequence,
        "last_generation_id": generation_id,
        "certification_mode": "test",
    }


class PhaseADependencyGenerationTests(unittest.TestCase):
    def tearDown(self) -> None:
        hot_state.clear_dependency_generation("XAUUSD")

    def test_cross_dependency_generation_requires_both_committed_targets(self) -> None:
        record = generation_contract.build_ready_generation(
            asset="XAUUSD",
            raw_target="2026-08-14T10:00:00Z",
            hmm_target="2026-08-14T10:00:00Z",
            ffd_transaction=transaction(
                "2026-08-14T10:00:00Z", "ffd-generation", 11
            ),
            hmm_transaction=transaction(
                "2026-08-14T10:00:00Z", "hmm-generation", 9
            ),
            source="unit_test",
        )
        self.assertEqual(record["status"], "READY")
        self.assertTrue(record["generation_id"])
        generation_contract.assert_ready_generation(
            record,
            asset="XAUUSD",
            raw_target="2026-08-14T10:00:00Z",
            hmm_target="2026-08-14T10:00:00Z",
        )

    def test_hmm_lag_cannot_publish_ready_generation(self) -> None:
        with self.assertRaises(generation_contract.DependencyGenerationError):
            generation_contract.build_ready_generation(
                asset="XAUUSD",
                raw_target="2026-08-14T10:01:00Z",
                hmm_target="2026-08-14T10:01:00Z",
                ffd_transaction=transaction(
                    "2026-08-14T10:01:00Z", "ffd-generation", 12
                ),
                hmm_transaction=transaction(
                    "2026-08-14T10:00:00Z", "hmm-generation", 9
                ),
                source="unit_test",
            )

    def test_generation_identity_is_source_independent(self) -> None:
        kwargs = dict(
            asset="XAUUSD",
            raw_target="2026-08-14T10:00:00Z",
            hmm_target="2026-08-14T10:00:00Z",
            ffd_transaction=transaction(
                "2026-08-14T10:00:00Z", "ffd-generation", 11
            ),
            hmm_transaction=transaction(
                "2026-08-14T10:00:00Z", "hmm-generation", 9
            ),
        )
        recovery = generation_contract.build_ready_generation(
            **kwargs,
            source="dedicated_recovery_cache_hydration",
        )
        replay = generation_contract.build_ready_generation(
            **kwargs,
            source="normal_hotpath_exact_snapshot",
        )
        self.assertEqual(recovery["generation_id"], replay["generation_id"])

    def test_generation_hash_detects_component_tampering(self) -> None:
        record = generation_contract.build_ready_generation(
            asset="XAUUSD",
            raw_target="2026-08-14T10:00:00Z",
            hmm_target="2026-08-14T10:00:00Z",
            ffd_transaction=transaction(
                "2026-08-14T10:00:00Z", "ffd-generation", 11
            ),
            hmm_transaction=transaction(
                "2026-08-14T10:00:00Z", "hmm-generation", 9
            ),
            source="unit_test",
        )
        record["ffd"]["generation_id"] = "tampered"
        with self.assertRaises(generation_contract.DependencyGenerationError):
            generation_contract.assert_ready_generation(record)

    def test_cold_ffd_verification_falls_back_to_checkpoint_transaction(self) -> None:
        prior_module = hot_state.FFD_MODULE
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tx = transaction(
                    "2026-08-14T10:00:00Z",
                    "ffd-generation-from-checkpoint",
                    11,
                )
                state_path = root / "xauusd_ffd_streaming_state.joblib"
                joblib.dump(
                    {
                        "last_observation_timestamp": "2026-08-14T10:00:00Z",
                        "timestamp_transaction": tx,
                    },
                    state_path,
                )
                hot_state.FFD_MODULE = SimpleNamespace(
                    LIVE_CONTINUATION_DIR=root,
                )
                resolved = continuation._verification_transaction(
                    {"continuation_proof": {}},
                    "FFD",
                    asset="XAUUSD",
                )
                self.assertEqual(
                    resolved["last_generation_id"],
                    "ffd-generation-from-checkpoint",
                )
        finally:
            hot_state.FFD_MODULE = prior_module

    def test_state_invalidation_revokes_published_generation(self) -> None:
        record = generation_contract.build_ready_generation(
            asset="XAUUSD",
            raw_target="2026-08-14T10:00:00Z",
            hmm_target="2026-08-14T10:00:00Z",
            ffd_transaction=transaction(
                "2026-08-14T10:00:00Z", "ffd-generation", 11
            ),
            hmm_transaction=transaction(
                "2026-08-14T10:00:00Z", "hmm-generation", 9
            ),
            source="unit_test",
        )
        hot_state.publish_dependency_generation(record)
        self.assertEqual(
            hot_state.current_dependency_generation("XAUUSD")["generation_id"],
            record["generation_id"],
        )
        hot_state.invalidate("XAUUSD", ffd=True, hmm=True)
        self.assertIsNone(hot_state.current_dependency_generation("XAUUSD"))

    def test_phase_a_source_contract_is_wired(self) -> None:
        root = Path(__file__).resolve().parents[1]
        continuation = (
            root / "shared" / "live_streaming" / "continuation.py"
        ).read_text(encoding="utf-8")
        p0 = (
            root / "portfolio_live" / "production_readiness_p0.py"
        ).read_text(encoding="utf-8")
        guard = (
            root / "portfolio_live" / "live_feature_continuation_guard.py"
        ).read_text(encoding="utf-8")
        pipeline = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "hydrate_verified_feature_dependencies_from_authority",
            continuation,
        )
        self.assertIn("_publish_ready_dependency_generation(", continuation)
        self.assertIn("_checkpoint_transaction(", continuation)
        self.assertIn("dependency_generation_id = str(", p0)
        self.assertIn("INTERNAL_FFD_LIVE_COVERAGE_GUARD_V1", guard)
        self.assertIn(
            "single_worker_dedup_by_asset_target_generation",
            pipeline,
        )
        self.assertIn("recovery_count = 1", pipeline)
        self.assertIn("dedicated_recovery_cache_hydration", pipeline)
        self.assertNotIn("recovery_followup_key_by_asset", pipeline)


if __name__ == "__main__":
    unittest.main()
