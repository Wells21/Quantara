from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


CONTRACT = Path(__file__).resolve().parents[1] / "shared" / "edges" / "native_one_row_contract.py"


def load_contract():
    spec = importlib.util.spec_from_file_location("native_contract_v5_test", CONTRACT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class NativeCertificationV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_contract()

    def native_row(self):
        return {
            "feature_parity_passed": True,
            "event_execution_mode": self.m.NATIVE_EVENT_MODE,
            "native_event_eligible": True,
            "event_parity_passed": True,
            "append_only_history_passed": True,
            "restart_parity_passed": True,
            "event_state_continuity_passed": True,
            "historical_revision_rejected": True,
            "rolling_window_event_parity_passed": True,
            "sequential_event_authority_parity_passed": True,
            "rolling_window_event_authority_parity_passed": True,
            "sequential_event_step_count": self.m.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
            "rolling_window_event_step_count": self.m.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
            "long_sequential_event_certification_passed": True,
            "restart_checkpoint_count": 4,
            "context_selection_policy": "smallest_anchor_and_long_sequence_passing_context",
            "silent_suffix_fallback_forbidden": True,
        }

    def test_schema_matches_current_long_sequential_contract(self):
        self.assertEqual(
            self.m.SCHEMA_VERSION,
            "native_one_row_certification_v7_fast_long_sequential",
        )

    def test_native_edge_requires_fixed_width_proof(self):
        row = self.native_row()
        self.assertTrue(self.m.edge_certification_passed(row))
        row["rolling_window_event_parity_passed"] = False
        self.assertFalse(self.m.edge_certification_passed(row))

    def test_full_authority_classification_is_unchanged(self):
        row = {
            "feature_parity_passed": True,
            "event_execution_mode": self.m.FULL_AUTHORITY_EVENT_MODE,
            "native_event_eligible": False,
            "full_authority_required": True,
            "event_parity_passed": True,
            "full_authority_determinism_passed": True,
            "force_full_event_replay": True,
        }
        self.assertTrue(self.m.edge_certification_passed(row))


if __name__ == "__main__":
    unittest.main()
