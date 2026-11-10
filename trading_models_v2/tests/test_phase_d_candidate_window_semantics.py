from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent, ROOT / "portfolio_live"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

import phase_d_04_probe_candidate_window_semantics as probe  # noqa: E402


class PhaseD4CandidateWindowSemanticsProbeTests(unittest.TestCase):
    def test_current_event_no_event_parity(self) -> None:
        empty = pd.DataFrame(columns=["Datetime", "edge_id", "side"])
        result = probe._compare_current(
            empty,
            empty,
            selected_features=[],
        )
        self.assertTrue(result["identity_ok"])
        self.assertTrue(result["context_ok"])

    def test_bar_index_is_ignored_only_when_not_selected(self) -> None:
        ts = pd.Timestamp("2026-08-18T00:00:00Z")
        left = pd.DataFrame(
            [{
                "Datetime": ts,
                "Bar_Start": ts,
                "edge_id": "A/x",
                "side": 1,
                "event_id": "A/x|t",
                "candidate_event_generator": "g",
                "bar_index": 6000,
                "Event_Vol": 1.25,
            }]
        )
        right = left.copy()
        right["bar_index"] = 5999
        result = probe._compare_current(
            left,
            right,
            selected_features=[],
        )
        self.assertTrue(result["identity_ok"])
        self.assertTrue(result["context_ok"])
        result_selected = probe._compare_current(
            left,
            right,
            selected_features=["bar_index"],
        )
        self.assertTrue(result_selected["identity_ok"])
        self.assertFalse(result_selected["context_ok"])

    def test_event_identity_divergence_is_detected(self) -> None:
        ts = pd.Timestamp("2026-08-18T00:00:00Z")
        left = pd.DataFrame(
            [{
                "Datetime": ts,
                "edge_id": "A/x",
                "side": 1,
                "event_id": "one",
                "candidate_event_generator": "g",
            }]
        )
        right = left.copy()
        right["event_id"] = "two"
        result = probe._compare_current(
            left,
            right,
            selected_features=[],
        )
        self.assertFalse(result["identity_ok"])

    def test_probe_is_inert(self) -> None:
        source = inspect.getsource(probe)
        self.assertIn('"production_routing_enabled": False', source)
        self.assertIn('"production_activation_allowed": False', source)
        self.assertNotIn("install_phase_d3_production_router(", source)
        self.assertNotIn("portfolio_execution_bot", source)

    def test_probe_has_no_asset_or_fixed_edge_registry(self) -> None:
        source = inspect.getsource(probe)
        for token in (
            '"BTCUSD"',
            '"DAX"',
            '"GBPJPY"',
            '"JPN"',
            '"NASDAQ"',
            '"US500"',
            '"USOIL"',
            '"XAUUSD"',
        ):
            self.assertNotIn(token, source)
        self.assertNotIn("== 74", source)
        self.assertNotIn("!= 74", source)

    def test_probe_only_writes_its_report(self) -> None:
        source = inspect.getsource(probe.run_probe)
        writes = [
            line.strip()
            for line in source.splitlines()
            if ".write_text(" in line
        ]
        self.assertEqual(writes, ["output.write_text("])


if __name__ == "__main__":
    unittest.main()
