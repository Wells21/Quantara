from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.edges.phase_c_consumer_contract import (
    PRIVATE_CANDIDATE_CONTROL_COLUMNS,
    REQUIRED_RUNTIME_CONSUMER_FUNCTIONS,
    compile_consumer_contract,
    validate_frozen_consumer_contract,
)
from shared.edges.phase_c_consumer_adapter import (
    build_canonical_live_candidate_rows,
    latest_candidate_control_values,
    strip_selected_feature_collisions,
)


class PhaseCConsumerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v2_root = Path(__file__).resolve().parents[1]
        cls.contract = compile_consumer_contract(v2_root=cls.v2_root)

    def test_current_contract_passes_without_routing(self):
        self.assertEqual(self.contract["status"], "PASS")
        self.assertFalse(self.contract["runtime_routing_enabled"])
        self.assertGreater(self.contract["selected_asset_count"], 0)
        self.assertGreater(self.contract["selected_edge_count"], 0)

    def test_dynamic_universe_matches_live_manifest(self):
        manifest = json.loads(
            (self.v2_root / "portfolio_live/live_manifest.json").read_text(
                encoding="utf-8-sig"
            )
        )
        manifest_ids = sorted(
            str(row["edge_id"]) for row in manifest.get("selected_edges") or []
        )
        compiled_ids = sorted(str(row["edge_id"]) for row in self.contract["edges"])
        self.assertEqual(compiled_ids, manifest_ids)
        self.assertEqual(self.contract["selected_edge_count"], len(manifest_ids))
        self.assertFalse(
            self.contract["scalability_contract"]["fixed_asset_list"]
        )
        self.assertFalse(
            self.contract["scalability_contract"]["fixed_edge_count"]
        )

    def test_every_edge_has_exact_schema_projection_and_selected_subset(self):
        for edge in self.contract["edges"]:
            schema = set(edge["schema_features"])
            core = set(edge["core_features"])
            addons = set(edge["required_addon_features"])
            selected = set(edge["selected_features"])
            self.assertEqual(schema, core | addons, edge["edge_id"])
            self.assertFalse(core & addons, edge["edge_id"])
            self.assertTrue(selected <= schema, edge["edge_id"])

    def test_addon_registry_is_per_edge_not_per_fixed_asset_map(self):
        registry = self.contract["addon_registry"]
        edge_rows = {row["edge_id"]: row for row in self.contract["edges"]}
        for asset, edges in registry.items():
            for edge_id, addons in edges.items():
                self.assertEqual(
                    list(addons),
                    edge_rows[edge_id]["required_addon_features"],
                    edge_id,
                )
                self.assertEqual(asset, edge_rows[edge_id]["asset"])

    def test_all_runtime_consumer_function_hashes_are_frozen(self):
        for edge in self.contract["edges"]:
            hashes = edge["consumer_source"]["runtime_consumer_function_ast_sha256"]
            for name in REQUIRED_RUNTIME_CONSUMER_FUNCTIONS:
                self.assertTrue(hashes.get(name), f"{edge['edge_id']}:{name}")

    def test_c2_pass_and_authority_depth_are_prerequisites(self):
        c2 = json.loads(
            (
                self.v2_root
                / "portfolio_live/config/phase_c/incremental_feature_certification.json"
            ).read_text(encoding="utf-8-sig")
        )
        self.assertEqual(c2.get("status"), "PASS")
        self.assertGreaterEqual(int(c2.get("bootstrap_rows") or 0), 6000)
        self.assertGreaterEqual(int(c2.get("sequential_steps") or 0), 128)


    def test_manifest_universe_change_invalidates_compilation(self):
        manifest_path = self.v2_root / "portfolio_live/live_manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        self.assertGreater(len(payload["selected_edges"]), 1)
        payload["selected_edges"] = payload["selected_edges"][:-1]
        payload["selected_edge_count"] = len(payload["selected_edges"])
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td) / "manifest.json"
            temp.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(Exception):
                compile_consumer_contract(
                    v2_root=self.v2_root,
                    live_manifest_path=temp,
                )

    def test_failed_c2_certification_fails_closed(self):
        c2_path = (
            self.v2_root
            / "portfolio_live/config/phase_c/incremental_feature_certification.json"
        )
        payload = json.loads(c2_path.read_text(encoding="utf-8-sig"))
        payload["status"] = "FAIL"
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td) / "c2.json"
            temp.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(Exception):
                compile_consumer_contract(
                    v2_root=self.v2_root,
                    phase_c2_certification_path=temp,
                )

    def test_private_candidate_controls_are_source_driven_and_separate(self):
        controlled = 0
        for edge in self.contract["edges"]:
            controls = list(edge.get("candidate_control_columns") or [])
            self.assertTrue(
                set(controls) <= set(PRIVATE_CANDIDATE_CONTROL_COLUMNS),
                edge["edge_id"],
            )
            self.assertFalse(
                set(controls).intersection(edge["schema_features"]),
                edge["edge_id"],
            )
            if controls:
                controlled += 1
                source = (
                    self.v2_root / edge["candidate_events_module"]
                ).read_text(encoding="utf-8-sig")
                for control in controls:
                    self.assertIn(control, source, edge["edge_id"])
        self.assertGreater(controlled, 0)

    def test_no_edge_or_family_control_lookup_table_exists(self):
        source = (
            self.v2_root / "shared/edges/phase_c_consumer_contract.py"
        ).read_text(encoding="utf-8-sig")
        self.assertIn("_candidate_private_controls", source)
        self.assertIn("ast.walk", source)
        self.assertNotIn("CONTROL_BY_EDGE", source)
        self.assertNotIn("CONTROL_BY_FAMILY", source)

    def test_private_control_adapter_reads_bounded_feature_tail_without_math(self):
        import pandas as pd
        edge = next(
            row for row in self.contract["edges"]
            if row.get("candidate_control_columns")
        )
        controls = list(edge["candidate_control_columns"])
        values = {name: float(i + 1) for i, name in enumerate(controls)}
        state = {
            "feature_tail": pd.DataFrame(
                [{"Datetime": pd.Timestamp("2026-01-01", tz="UTC"), **values}]
            )
        }
        observed = latest_candidate_control_values(
            state=state,
            edge_contract=edge,
        )
        self.assertEqual(observed, values)

    def test_consumer_adapter_exists_and_policy_is_frozen(self):
        adapter = self.v2_root / self.contract["consumer_adapter_path"]
        self.assertTrue(adapter.is_file())
        for edge in self.contract["edges"]:
            self.assertEqual(
                edge["selected_feature_event_collision_policy"],
                "strip_after_candidate_detection_before_live_model_row_build",
            )


    def test_selected_feature_collisions_are_stripped_after_candidate_detection(self):
        import pandas as pd
        events = pd.DataFrame(
            {
                "Datetime": [pd.Timestamp("2026-01-01", tz="UTC")],
                "side": [1],
                "event_context": [7.0],
                "Feature_A": [-9001.0],
            }
        )
        sanitized, meta = strip_selected_feature_collisions(
            events=events,
            selected_features=["Feature_A"],
        )
        self.assertNotIn("Feature_A", sanitized.columns)
        self.assertIn("event_context", sanitized.columns)
        self.assertEqual(meta["selected_feature_collision_columns"], ["Feature_A"])

    def test_canonical_live_row_adapter_forces_feature_table_ownership(self):
        import pandas as pd

        class DummyRuntime:
            EDGE_ID = "DUMMY/absorption_long"

            @staticmethod
            def _build_live_candidate_model_rows(features, events, selected_features):
                # Mimic the current absorption precedence rule: if a selected
                # column exists in events it wins; otherwise merge from features.
                event_table = events.copy()
                feature_table = features.copy()
                selected_from_events = [
                    name for name in selected_features if name in event_table.columns
                ]
                selected_from_features = [
                    name for name in selected_features
                    if name in feature_table.columns and name not in event_table.columns
                ]
                rows = event_table.merge(
                    feature_table[["Datetime", *selected_from_features]],
                    on="Datetime",
                    how="inner",
                    validate="many_to_one",
                )
                return rows, {
                    "row_builder": "dummy_absorption_precedence",
                    "selected_features_from_events": len(selected_from_events),
                }

        ts = pd.Timestamp("2026-01-01", tz="UTC")
        features = pd.DataFrame({"Datetime": [ts], "Feature_A": [123.0]})
        events = pd.DataFrame(
            {"Datetime": [ts], "side": [1], "Feature_A": [-999.0]}
        )
        rows, meta = build_canonical_live_candidate_rows(
            runtime=DummyRuntime(),
            features=features,
            events=events,
            selected_features=["Feature_A"],
        )
        self.assertEqual(float(rows["Feature_A"].iloc[0]), 123.0)
        self.assertEqual(meta["selected_feature_collision_count"], 1)
        self.assertEqual(meta["selected_feature_collision_columns"], ["Feature_A"])
        self.assertEqual(
            meta["selected_feature_source_policy"],
            "canonical_feature_table_only",
        )

    def test_frozen_contract_validates_by_structural_current_state(self):
        frozen = (
            self.v2_root
            / "portfolio_live/config/phase_c/consumer_contract.json"
        )
        if not frozen.exists():
            return
        result = validate_frozen_consumer_contract(
            v2_root=self.v2_root,
            frozen_path=frozen,
        )
        self.assertEqual(result.get("status"), "PASS", result)
        self.assertEqual(
            result.get("authority_identity_mode"),
            "structural_current_state_no_git_or_file_sha_gate",
        )



if __name__ == "__main__":
    unittest.main()
