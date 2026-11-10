from __future__ import annotations

import ast
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.phase_d_candidate_contract import (  # noqa: E402
    _source_strings,
    compile_candidate_contract,
    contract_without_nondeterministic_fields,
)


class PhaseDCandidateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = compile_candidate_contract(v2_root=ROOT)

    def test_current_manifest_compiles_without_routing(self) -> None:
        self.assertEqual(self.report.get("status"), "PASS", self.report.get("fatal_errors"))
        self.assertIs(self.report.get("runtime_routing_enabled"), False)
        self.assertEqual(
            self.report.get("compiled_edge_count"),
            self.report.get("selected_edge_count"),
        )
        self.assertEqual(self.report.get("failed_edge_count"), 0)

    def test_dynamic_universe_has_no_fixed_current_names_or_edge_count(self) -> None:
        source = (
            ROOT / "shared/edges/phase_d_candidate_contract.py"
        ).read_text(encoding="utf-8")
        for token in (
            '"BTCUSD"', '"DAX"', '"GBPJPY"', '"JPN"',
            '"NASDAQ"', '"US500"', '"USOIL"', '"XAUUSD"',
        ):
            self.assertNotIn(token, source)
        self.assertNotIn("== 74", source)
        self.assertNotIn("!= 74", source)

    def test_every_edge_uses_current_parseable_candidate_and_runtime_source(self) -> None:
        for row in self.report.get("edges") or []:
            candidate = row.get("candidate") or {}
            runtime = row.get("runtime_candidate_wrapper") or {}
            self.assertTrue(
                candidate.get("runtime_entry_functions"), row.get("edge_id")
            )
            self.assertTrue(candidate.get("path"), row.get("edge_id"))
            self.assertTrue(runtime.get("path"), row.get("edge_id"))
            self.assertEqual(row.get("status"), "PASS", row.get("failures"))


    def test_candidate_logic_groups_cover_every_compiled_edge_once(self) -> None:
        seen: list[str] = []
        for group in self.report.get("candidate_logic_groups") or []:
            seen.extend(str(x) for x in group.get("edge_ids") or [])
        expected = sorted(
            str(row.get("edge_id"))
            for row in self.report.get("edges") or []
        )
        self.assertEqual(sorted(seen), expected)

    def test_candidate_inputs_are_subset_of_certified_projection(self) -> None:
        for row in self.report.get("edges") or []:
            candidate = row.get("candidate") or {}
            self.assertLessEqual(
                int(candidate.get("candidate_input_column_count") or 0),
                int(candidate.get("certified_projection_column_count") or 0),
                row.get("edge_id"),
            )

    def test_manifest_reordering_does_not_change_compiled_contract_content(self) -> None:
        manifest_path = ROOT / "portfolio_live/live_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        reversed_manifest = copy.deepcopy(manifest)
        reversed_manifest["selected_edges"] = list(
            reversed(reversed_manifest.get("selected_edges") or [])
        )
        with tempfile.TemporaryDirectory(prefix="phase_d1_test_", dir=str(ROOT)) as td:
            tmp_manifest = Path(td) / "live_manifest.json"
            tmp_manifest.write_text(
                json.dumps(reversed_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reordered = compile_candidate_contract(
                v2_root=ROOT,
                live_manifest_path=tmp_manifest,
            )

        left = contract_without_nondeterministic_fields(self.report)
        right = contract_without_nondeterministic_fields(reordered)
        left.pop("source_live_manifest_sha256", None)
        right.pop("source_live_manifest_sha256", None)
        self.assertEqual(left, right)


    def test_source_resolves_literal_fstring_feature_names(self) -> None:
        tree = ast.parse(
            "def f(df):\n"
            "    name = f\"Log_Return_{PULLBACK_LOOKBACK}\"\n"
            "    return df[name]\n"
        )
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
        )
        strings = _source_strings(
            [function],
            {"PULLBACK_LOOKBACK": 5},
        )
        self.assertIn("Log_Return_5", strings)

    def test_primary_side_canonical_dependencies_are_in_narrow_consumer(self) -> None:
        c3 = json.loads(
            (
                ROOT / "portfolio_live/config/phase_c/consumer_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        c3_map = {
            str(row.get("edge_id")): row
            for row in c3.get("edges") or []
            if isinstance(row, dict) and row.get("edge_id")
        }

        for row in self.report.get("edges") or []:
            edge_id = str(row.get("edge_id"))
            candidate = row.get("candidate") or {}
            primary = row.get("primary_side") or {}

            combined = set(
                candidate.get(
                    "candidate_input_columns_from_certified_projection"
                )
                or []
            )
            primary_columns = set(
                primary.get(
                    "feature_input_columns_from_certified_projection"
                )
                or []
            )

            self.assertTrue(
                primary_columns.issubset(combined),
                edge_id,
            )

            # Only reachable live primary-side functions are consumer authority.
            # Some edge packages define load_verified_ffd_features() solely as a
            # shared helper for feature_expansion.py; an unused definition must
            # not force Close_FFD into the candidate projection.
            reachable = set(
                primary.get("reachable_primary_functions") or []
            )
            canonical = set(
                (c3_map.get(edge_id) or {}).get(
                    "canonical_projection_columns"
                )
                or []
            )

            if (
                "load_verified_ffd_features" in reachable
                and "Close_FFD" in canonical
            ):
                self.assertIn(
                    "Close_FFD",
                    combined,
                    edge_id,
                )

    def test_d1_explicitly_forbids_production_routing(self) -> None:
        for row in self.report.get("edges") or []:
            routing = row.get("phase_d_routing") or {}
            self.assertIs(
                routing.get("incremental_candidate_routing_enabled"),
                False,
            )
            self.assertIs(
                routing.get("resident_scorer_routing_enabled"),
                False,
            )
            self.assertIs(
                routing.get("normal_full_candidate_fallback_allowed_after_d2_activation"),
                False,
            )


if __name__ == "__main__":
    unittest.main()
