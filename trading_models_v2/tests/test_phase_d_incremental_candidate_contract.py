from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent, ROOT / "portfolio_live"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges import phase_d_incremental_candidate_contract as d4c  # noqa: E402


class PhaseD4IncrementalCandidateContractTests(unittest.TestCase):
    def test_self_test_passes(self) -> None:
        self.assertEqual(d4c.self_test().get("status"), "PASS")

    def test_current_contract_covers_current_d1_exactly(self) -> None:
        report = d4c.compile_incremental_candidate_contract(ROOT)
        self.assertEqual(report.get("status"), "PASS", report.get("failures"))
        self.assertEqual(
            report.get("compiled_edge_count"),
            report.get("selected_edge_count"),
        )
        d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        self.assertEqual(
            report.get("candidate_logic_group_count"),
            d1.get("candidate_logic_group_count"),
        )

    def test_binding_is_only_source_derived_logic_signature(self) -> None:
        report = d4c.compile_incremental_candidate_contract(ROOT)
        self.assertEqual(
            report.get("provider_binding_mode"),
            "d1_candidate_logic_signature",
        )
        for group in report.get("candidate_logic_groups") or []:
            self.assertEqual(
                group.get("provider_binding_key"),
                group.get("candidate_logic_signature"),
            )
            self.assertEqual(group.get("provider_status"), "UNBOUND")

    def test_no_handwritten_candidate_recipe_or_asset_registry(self) -> None:
        source = inspect.getsource(d4c)
        self.assertNotIn("ENTRY_FUNCTION_RECIPES", source)
        self.assertNotIn("continuation_recipe", source)
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

    def test_contract_is_inert_and_fail_closed_until_providers_exist(self) -> None:
        report = d4c.compile_incremental_candidate_contract(ROOT)
        self.assertIs(report.get("runtime_routing_enabled"), False)
        self.assertIs(report.get("production_activation_allowed"), False)
        self.assertEqual(report.get("bound_provider_count"), 0)
        self.assertEqual(
            report.get("unbound_logic_group_count"),
            report.get("candidate_logic_group_count"),
        )

    def test_normal_path_forbids_historical_fallback(self) -> None:
        normal = (
            d4c.compile_incremental_candidate_contract(ROOT)
            .get("normal_path_contract")
            or {}
        )
        self.assertIs(normal.get("full_candidate_generator_allowed"), False)
        self.assertIs(normal.get("full_candidate_fallback_allowed"), False)
        self.assertIs(normal.get("candidate_history_disk_reads_allowed"), False)
        self.assertIs(normal.get("full_canonical_ring_copy_allowed"), False)

    def test_no_git_or_file_sha_execution_authority(self) -> None:
        report = d4c.compile_incremental_candidate_contract(ROOT)
        self.assertEqual(
            report.get("authority_identity_mode"),
            "structural_current_state_no_git_or_file_sha_gate",
        )
        source = inspect.getsource(d4c.validate_current_contract)
        self.assertNotIn("_file_sha", source)
        self.assertNotIn("file_sha256", source)


if __name__ == "__main__":
    unittest.main()
