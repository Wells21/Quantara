from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.phase_d_candidate_first import (  # noqa: E402
    RUNTIME_ROUTING_ENABLED,
    PhaseD2ContractError,
    edge_map,
    load_phase_d1_contract,
    project_candidate_features,
    projection_reduction_record,
    source_guard,
)


class PhaseD2CandidateFirstTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.d1 = load_phase_d1_contract(ROOT)
        cls.c3 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_c/consumer_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        cls.d1_edges = edge_map(cls.d1)
        cls.c3_edges = edge_map(cls.c3)

    def test_routing_remains_disabled(self) -> None:
        self.assertIs(RUNTIME_ROUTING_ENABLED, False)

    def test_source_guard_matches_d1_and_c3(self) -> None:
        report = source_guard(
            v2_root=ROOT,
            d1_contract=self.d1,
            c3_contract=self.c3,
        )
        self.assertEqual(report.get("status"), "PASS", report.get("failures"))
        self.assertEqual(report.get("checked_edges"), len(self.d1_edges))

    def test_every_candidate_projection_is_strictly_narrower_than_c3(self) -> None:
        for edge_id in sorted(self.d1_edges):
            record = projection_reduction_record(
                d1_edge=self.d1_edges[edge_id],
                c3_edge=self.c3_edges[edge_id],
            )
            self.assertLess(
                record["candidate_projection_columns_including_identity"],
                record["full_c3_projection_columns"],
                edge_id,
            )

    def test_synthetic_projection_selects_before_copy_and_adds_identity(self) -> None:
        d1_edge = {
            "edge_id": "AAA/test_long",
            "side_value": 1,
            "candidate": {
                "candidate_input_columns_from_certified_projection": [
                    "Datetime",
                    "Close",
                ]
            },
        }
        ring = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]
                ),
                "Close": [1.0, 2.0],
                "Unused_A": [10.0, 20.0],
                "Unused_B": [30.0, 40.0],
            }
        )
        projected = project_candidate_features(
            ring=ring,
            edge={"edge_id": "AAA/test_long", "side_value": 1},
            d1_edge=d1_edge,
        )
        self.assertEqual(
            list(projected.columns),
            ["Datetime", "Close", "edge_id", "side"],
        )
        self.assertNotIn("Unused_A", projected.columns)
        self.assertEqual(set(projected["edge_id"]), {"AAA/test_long"})
        self.assertEqual(set(projected["side"]), {1})
        self.assertIs(projected.attrs.get("phase_d2_full_projection_deferred"), True)

    def test_missing_candidate_column_fails_closed(self) -> None:
        d1_edge = {
            "edge_id": "AAA/test_long",
            "side_value": 1,
            "candidate": {
                "candidate_input_columns_from_certified_projection": [
                    "Datetime",
                    "Missing",
                ]
            },
        }
        ring = pd.DataFrame(
            {"Datetime": pd.to_datetime(["2026-01-01T00:00:00Z"])}
        )
        with self.assertRaises(PhaseD2ContractError):
            project_candidate_features(
                ring=ring,
                edge={"edge_id": "AAA/test_long", "side_value": 1},
                d1_edge=d1_edge,
            )

    def test_no_fixed_current_asset_or_edge_count_in_engine(self) -> None:
        source = (
            ROOT / "shared/edges/phase_d_candidate_first.py"
        ).read_text(encoding="utf-8")
        for token in (
            '"BTCUSD"', '"DAX"', '"GBPJPY"', '"JPN"',
            '"NASDAQ"', '"US500"', '"USOIL"', '"XAUUSD"',
        ):
            self.assertNotIn(token, source)
        self.assertNotIn("== 74", source)
        self.assertNotIn("!= 74", source)

    def test_engine_never_copies_full_ring_before_narrow_selection(self) -> None:
        import ast
        import inspect
        import textwrap

        function_source = textwrap.dedent(
            inspect.getsource(project_candidate_features)
        )
        tree = ast.parse(function_source)

        direct_full_ring_copy_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "copy":
                continue
            if isinstance(func.value, ast.Name) and func.value.id == "ring":
                direct_full_ring_copy_calls.append(node)

        self.assertEqual(
            direct_full_ring_copy_calls,
            [],
            "project_candidate_features must narrow-select from ring before copying",
        )

        uses_ring_loc = any(
            isinstance(node, ast.Attribute)
            and node.attr == "loc"
            and isinstance(node.value, ast.Name)
            and node.value.id == "ring"
            for node in ast.walk(tree)
        )
        self.assertTrue(
            uses_ring_loc,
            "project_candidate_features must select candidate columns from ring.loc",
        )


if __name__ == "__main__":
    unittest.main()
