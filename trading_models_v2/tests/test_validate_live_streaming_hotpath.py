from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shared.edges import native_one_row_contract as validator


def _native_row() -> dict:
    return {
        "feature_parity_passed": True,
        "event_parity_passed": True,
        "event_execution_mode": validator.NATIVE_EVENT_MODE,
        "native_event_eligible": True,
        "append_only_history_passed": True,
        "restart_parity_passed": True,
        "event_state_continuity_passed": True,
        "historical_revision_rejected": True,
        "rolling_window_event_parity_passed": True,
        "sequential_event_authority_parity_passed": True,
        "rolling_window_event_authority_parity_passed": True,
        "sequential_event_step_count": validator.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
        "rolling_window_event_step_count": validator.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
        "long_sequential_event_certification_passed": True,
        "restart_checkpoint_count": 4,
        "context_selection_policy": "smallest_anchor_and_long_sequence_passing_context",
        "silent_suffix_fallback_forbidden": True,
    }


def _full_authority_row() -> dict:
    return {
        "feature_parity_passed": True,
        "event_parity_passed": True,
        "event_execution_mode": validator.FULL_AUTHORITY_EVENT_MODE,
        "native_event_eligible": False,
        "full_authority_required": True,
        "force_full_event_replay": True,
        "full_authority_determinism_passed": True,
    }


def _certification_payload(edges: dict[str, dict]) -> dict:
    return {
        "schema_version": validator.SCHEMA_VERSION,
        "status": "PASS",
        "selected_edge_count": 33,
        "implementation_hashes": {"contract": "test"},
        "runtime_environment": {"python": "test"},
        "edges": edges,
        "assets": {},
    }


class EventExecutionCertificationValidationTests(unittest.TestCase):
    def _write_payload(self, directory: str, edges: dict[str, dict]) -> Path:
        path = Path(directory) / "native_one_row_certification.json"
        path.write_text(
            json.dumps(_certification_payload(edges)),
            encoding="utf-8",
        )
        return path

    def _contract_mocks(self):
        return (
            mock.patch.object(
                validator,
                "implementation_hashes",
                return_value={"contract": "test"},
            ),
            mock.patch.object(
                validator,
                "runtime_environment",
                return_value={"python": "test"},
            ),
        )

    def test_native_and_full_authority_edges_are_both_safe(self) -> None:
        native_edge = "BTCUSD/trend_long"
        full_edge = "BTCUSD/drawdownhazardcontinuation_short"

        with tempfile.TemporaryDirectory() as directory:
            path = self._write_payload(
                directory,
                {
                    native_edge: _native_row(),
                    full_edge: _full_authority_row(),
                },
            )
            hash_mock, env_mock = self._contract_mocks()
            with hash_mock, env_mock:
                native = validator.event_execution_classification(native_edge, path)
                full = validator.event_execution_classification(full_edge, path)

        self.assertEqual(
            native["event_execution_mode"],
            validator.NATIVE_EVENT_MODE,
        )
        self.assertTrue(native["native_event_eligible"])
        self.assertFalse(native["full_authority_required"])

        self.assertEqual(
            full["event_execution_mode"],
            validator.FULL_AUTHORITY_EVENT_MODE,
        )
        self.assertFalse(full["native_event_eligible"])
        self.assertTrue(full["full_authority_required"])
        self.assertTrue(full["force_full_event_replay"])

    def test_full_authority_edge_never_exposes_native_certificate(self) -> None:
        edge_id = "BTCUSD/drawdownhazardcontinuation_short"

        with tempfile.TemporaryDirectory() as directory:
            path = self._write_payload(
                directory,
                {edge_id: _full_authority_row()},
            )
            hash_mock, env_mock = self._contract_mocks()
            with hash_mock, env_mock:
                classification = validator.event_execution_classification(
                    edge_id,
                    path,
                )
                native_certificate = validator.event_certification(edge_id, path)

        self.assertEqual(
            classification["event_execution_mode"],
            validator.FULL_AUTHORITY_EVENT_MODE,
        )
        self.assertEqual(native_certificate, {})

    def test_invalid_full_authority_native_eligibility_fails_closed(self) -> None:
        edge_id = "BTCUSD/drawdownhazardcontinuation_short"
        invalid = _full_authority_row()
        invalid["native_event_eligible"] = True

        with tempfile.TemporaryDirectory() as directory:
            path = self._write_payload(directory, {edge_id: invalid})
            hash_mock, env_mock = self._contract_mocks()
            with hash_mock, env_mock:
                classification = validator.event_execution_classification(
                    edge_id,
                    path,
                )
                native_certificate = validator.event_certification(edge_id, path)

        self.assertEqual(classification, {})
        self.assertEqual(native_certificate, {})


if __name__ == "__main__":
    unittest.main()
