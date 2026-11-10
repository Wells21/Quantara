from __future__ import annotations

import ast
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent, ROOT / "portfolio_live"):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

import phase_d_03_production_router as d3


class PhaseD3ProductionRouterTests(unittest.TestCase):
    def test_self_test_passes(self) -> None:
        report = d3.self_test()
        self.assertEqual(
            report.get("status"),
            "PASS",
            report.get("failed"),
        )

    def test_router_is_inert_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            original = d3.D3_ACTIVATION_PATH
            try:
                d3.D3_ACTIVATION_PATH = Path(td) / "missing_activation.json"
                namespace = {}
                report = d3.install_phase_d3_production_router(namespace)
            finally:
                d3.D3_ACTIVATION_PATH = original

        self.assertTrue(report.get("installed"))
        self.assertFalse(report.get("activated"))
        self.assertTrue(report.get("current_runtime_unchanged"))
        self.assertIs(namespace.get("_PHASE_D3_ROUTER_ACTIVE"), False)

    def test_no_fixed_current_asset_list_or_edge_count(self) -> None:
        source = (
            ROOT / "portfolio_live/phase_d_03_production_router.py"
        ).read_text(encoding="utf-8")
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

    def test_router_reuses_existing_dashboard_runtime_residency(self) -> None:
        source = inspect.getsource(
            d3.install_phase_d3_production_router
        )
        self.assertIn('namespace["get_runtime"]', source)
        self.assertNotIn("_RUNTIME_CACHE =", source)
        self.assertNotIn("EdgeScorer.from_edge_root", source)

    def test_full_projection_only_inside_deferred_score(self) -> None:
        source = inspect.getsource(
            d3.install_phase_d3_production_router
        )
        tree = ast.parse(source)
        project_calls = []
        deferred_ranges = []

        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "deferred_score"
            ):
                deferred_ranges.append(
                    (
                        int(node.lineno),
                        int(node.end_lineno),
                    )
                )
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "project_full_c3_features"
                ):
                    project_calls.append(int(node.lineno))

        self.assertEqual(len(project_calls), 1)
        self.assertEqual(len(deferred_ranges), 1)
        start, end = deferred_ranges[0]
        self.assertTrue(start <= project_calls[0] <= end)

    def test_normal_path_has_no_batch_feature_fallback(self) -> None:
        source = inspect.getsource(
            d3.install_phase_d3_production_router
        )
        self.assertIn(
            "normal_path_runtime_build_features_forbidden",
            source,
        )
        self.assertNotIn(
            "original_build(",
            source,
        )

    def test_dashboard_installs_d3_after_c4(self) -> None:
        dashboard = (
            ROOT
            / "portfolio_live/portfolio_live_dashboard_orchestrator.py"
        ).read_text(encoding="utf-8")
        c4_pos = dashboard.find(
            "_PHASE_C4_PRODUCTION_ROUTER_REPORT"
        )
        d3_pos = dashboard.find(
            "_PHASE_D3_PRODUCTION_ROUTER_REPORT"
        )
        self.assertGreaterEqual(c4_pos, 0)
        self.assertGreater(d3_pos, c4_pos)

    def test_valid_empty_candidate_is_normal_no_event_result(self) -> None:
        events = pd.DataFrame(
            columns=[
                "Datetime",
                "event_id",
                "edge_id",
                "side",
                "candidate_event_generator",
                "event_known_after_close",
                "uses_future_information",
            ]
        )
        result = d3._sanitize_generated_candidate_events(
            events=events,
            selected_features=["feature_a"],
            edge_id="AAA/test",
        )
        self.assertIsInstance(result, pd.DataFrame)
        self.assertTrue(result.empty)
        self.assertIn("Datetime", result.columns)
        self.assertEqual(
            result.attrs.get("phase_d3_selected_feature_collision_count"),
            0,
        )
        self.assertEqual(
            result.attrs.get("phase_d3_selected_feature_source_policy"),
            "canonical_feature_table_only",
        )

    def test_malformed_empty_candidate_still_fails_closed(self) -> None:
        with self.assertRaises(d3.PhaseD3RuntimeError):
            d3._sanitize_generated_candidate_events(
                events=pd.DataFrame(columns=["event_id"]),
                selected_features=[],
                edge_id="AAA/test",
            )

    def test_nonempty_candidate_collision_policy_is_unchanged(self) -> None:
        events = pd.DataFrame(
            [{
                "Datetime": pd.Timestamp("2026-01-01T00:00:00Z"),
                "event_id": "AAA/test|2026-01-01T00:00:00+00:00|event",
                "edge_id": "AAA/test",
                "side": 1,
                "candidate_event_generator": "candidate",
                "event_known_after_close": True,
                "uses_future_information": False,
                "feature_a": 123.0,
                "event_context": 456.0,
            }]
        )
        result = d3._sanitize_generated_candidate_events(
            events=events,
            selected_features=["feature_a"],
            edge_id="AAA/test",
        )
        self.assertEqual(len(result), 1)
        self.assertNotIn("feature_a", result.columns)
        self.assertIn("event_context", result.columns)
        self.assertEqual(
            result.attrs.get("phase_d3_selected_feature_collision_count"),
            1,
        )
        self.assertEqual(
            result.attrs.get("phase_d3_selected_feature_source_policy"),
            "canonical_feature_table_only",
        )

    def test_activation_artifact_is_not_created_by_import(self) -> None:
        # Source staging must not create/enable an activation artifact.
        self.assertIsInstance(d3.D3_ACTIVATION_PATH, Path)


    def test_hybrid_routing_certificate_subset_helper(self) -> None:
        current = {"AAA/fast", "BBB/new"}
        d2 = {"edges": {"AAA/fast": {"status": "PASS"}}}
        d3_cert = {
            "deferred_projection_results": {"AAA/fast": {"status": "PASS"}},
            "c5_model_decision_parity": {"edge_results": {"AAA/fast": {"status": "PASS"}}},
        }
        d2_ids = set(d3._phase_d310_d2_certified_edge_ids(d2, current))
        optimized = d3._phase_d310_d3_certified_edge_ids(d3_cert, d2_ids, current)
        self.assertEqual(optimized, ["AAA/fast"])
        self.assertEqual(sorted(current.difference(optimized)), ["BBB/new"])


if __name__ == "__main__":
    unittest.main()
