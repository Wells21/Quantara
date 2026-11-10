from __future__ import annotations

import copy
import inspect
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "portfolio_live"
for _path in (ROOT, LIVE):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import phase_c_04_production_router as c4


class PhaseC4ProductionRouterTests(unittest.TestCase):
    def test_static_contract_is_dynamic_and_current(self) -> None:
        report = c4.static_contract_report(ROOT)
        self.assertEqual(report.get("status"), "PASS", report.get("failures"))
        self.assertEqual(
            report.get("selected_asset_count"),
            len(report.get("selected_assets") or []),
        )
        self.assertEqual(
            report.get("selected_edge_count"),
            len(report.get("selected_edge_ids") or []),
        )
        scale = report.get("scalability_contract") or {}
        self.assertIs(scale.get("fixed_asset_list"), False)
        self.assertIs(scale.get("fixed_edge_count"), False)

    def test_activation_candidate_uses_structural_contract_not_hash_gate(self) -> None:
        payload = c4.build_activation_payload(ROOT)
        self.assertEqual(payload.get("status"), "PASS")
        self.assertIs(payload.get("runtime_routing_enabled"), True)
        validation = c4.validate_activation(payload, v2_root=ROOT)
        self.assertEqual(
            validation.get("status"), "PASS", validation.get("failures")
        )

        informational = copy.deepcopy(payload)
        informational["activation_fingerprint"] = "informational-only"
        validation = c4.validate_activation(informational, v2_root=ROOT)
        self.assertEqual(
            validation.get("status"), "PASS", validation.get("failures")
        )

        structural = copy.deepcopy(payload)
        structural["contract"]["selected_edge_ids"] = structural["contract"][
            "selected_edge_ids"
        ][:-1]
        validation = c4.validate_activation(structural, v2_root=ROOT)
        self.assertEqual(validation.get("status"), "FAIL")


    def test_edge_projection_is_contract_driven(self) -> None:
        ring = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(["2026-01-01T00:00:00Z"]),
                "Bar_Start": pd.to_datetime(["2025-12-31T23:59:00Z"]),
                "Open": [1.0],
                "High": [1.1],
                "Low": [0.9],
                "Close": [1.0],
                "Ticks": [10.0],
                "Threshold": [10.0],
                "Feature_A": [3.0],
                "ATR_14": [0.2],
            }
        )
        contract = {
            "canonical_projection_columns": [
                "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
                "Ticks", "Threshold", "edge_id", "side", "Feature_A", "ATR_14",
            ],
            "consumer_contract_fingerprint": "unit",
        }
        projected = c4._project_edge_features(
            ring=ring,
            edge={"edge_id": "AAA/example_long", "asset": "AAA", "side_value": 1},
            edge_contract=contract,
        )
        self.assertEqual(
            list(projected.columns),
            contract["canonical_projection_columns"],
        )
        self.assertEqual(projected["edge_id"].iloc[0], "AAA/example_long")
        self.assertEqual(int(projected["side"].iloc[0]), 1)

    def test_hmm_eligibility_is_derived_from_generation_targets(self) -> None:
        source = inspect.getsource(c4._advance_one)
        self.assertIn('hmm_target == raw_target', source)
        self.assertIn('generation["target_hmm_eligible"] = bool(eligible)', source)

    def test_normal_update_never_uses_batch_or_dependency_io(self) -> None:
        source = inspect.getsource(c4._advance_one)
        self.assertNotIn("build_features", source)
        self.assertNotIn("read_csv", source)
        self.assertNotIn("load_verified_ffd", source)
        self.assertNotIn("load_verified_hmm", source)
        self.assertNotIn("refresh_verified_feature_dependencies_hot", source)

    def test_normal_edge_scoring_forbids_batch_fallback(self) -> None:
        source = inspect.getsource(c4.install_phase_c4_production_router)
        self.assertIn("normal_path_runtime_build_features_forbidden", source)
        self.assertIn("strip_selected_feature_collisions", source)

    def test_dashboard_install_order_is_p0_snapshot_then_c4(self) -> None:
        source = (
            ROOT / "portfolio_live" / "portfolio_live_dashboard_orchestrator.py"
        ).read_text(encoding="utf-8")
        p0 = source.index("_P0_PRODUCTION_HARDENING_REPORT")
        snapshot = source.index("_SNAPSHOT_PIPELINE_REPORT")
        c4pos = source.index("_PHASE_C4_PRODUCTION_ROUTER_REPORT")
        self.assertLess(p0, snapshot)
        self.assertLess(snapshot, c4pos)

    def test_recovery_invalidates_c4_before_latest_snapshot_replay(self) -> None:
        source = (
            ROOT / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_PHASE_C4_RECOVERY_RESET", source)
        reset = source.index("_PHASE_C4_RECOVERY_RESET")
        replay = source.index(
            'reason="dedicated_dependency_recovery_completed"',
            reset,
        )
        self.assertLess(reset, replay)


    def test_c3_portability_accepts_only_ast_or_manifest_metadata_variance(self) -> None:
        frozen = {
            "status": "PASS",
            "runtime_routing_enabled": False,
            "source_live_manifest_sha256": "manifest-old",
            "edges": [
                {
                    "edge_id": "AAA/example_long",
                    "consumer_contract_fingerprint": "derived-old",
                    "consumer_source": {
                        "edge_python_bundle_sha256": "bundle",
                        "runtime_file_sha256": "runtime",
                        "candidate_events_file_sha256": "candidate",
                        "runtime_consumer_function_ast_sha256": {"score": "ast-old"},
                    },
                }
            ],
        }
        frozen["contract_sha256"] = c4._json_hash(frozen)
        current = copy.deepcopy(frozen)
        current.pop("contract_sha256")
        current["edges"][0]["consumer_contract_fingerprint"] = "derived-new"
        current["edges"][0]["consumer_source"][
            "runtime_consumer_function_ast_sha256"
        ]["score"] = "ast-new"
        current["contract_sha256"] = c4._json_hash(current)
        report = c4._c3_portable_recompile_report(frozen, current)
        self.assertEqual(report.get("status"), "PASS", report)
        self.assertEqual(
            report.get("mode"),
            "SOURCE_BYTES_EQUIVALENT_AST_PORTABILITY_VARIANCE",
        )

        manifest_only = copy.deepcopy(frozen)
        manifest_only.pop("contract_sha256")
        manifest_only["source_live_manifest_sha256"] = "manifest-new"
        manifest_only["contract_sha256"] = c4._json_hash(manifest_only)
        report = c4._c3_portable_recompile_report(frozen, manifest_only)
        self.assertEqual(report.get("status"), "PASS", report)
        self.assertEqual(report.get("mode"), "FEATURE_EQUIVALENT_MANIFEST_METADATA_VARIANCE")

        drifted = copy.deepcopy(current)
        drifted.pop("contract_sha256")
        drifted["edges"][0]["consumer_source"]["runtime_file_sha256"] = "changed"
        drifted["contract_sha256"] = c4._json_hash(drifted)
        report = c4._c3_portable_recompile_report(frozen, drifted)
        self.assertEqual(report.get("status"), "FAIL", report)
        self.assertEqual(report.get("mode"), "NON_PORTABLE_C3_DRIFT")

    def test_router_bootstraps_v2_path_before_shared_imports(self) -> None:
        source = (
            ROOT / "portfolio_live" / "phase_c_04_production_router.py"
        ).read_text(encoding="utf-8")
        bootstrap = source.index("if str(V2_ROOT) not in sys.path:")
        shared_import = source.index("from shared.edges.phase_c_consumer_adapter")
        self.assertLess(bootstrap, shared_import)

    def test_current_asset_names_are_not_encoded_in_router(self) -> None:
        source = (
            ROOT / "portfolio_live" / "phase_c_04_production_router.py"
        ).read_text(encoding="utf-8")
        for token in (
            '"BTCUSD"', '"DAX"', '"GBPJPY"', '"JPN"',
            '"NASDAQ"', '"US500"', '"USOIL"', '"XAUUSD"',
        ):
            self.assertNotIn(token, source)
        self.assertNotIn("selected_edge_count == 74", source)
        self.assertNotIn("selected_edge_count != 74", source)


    def test_activation_requires_phase_c5_release_certificate(self):
        source = (ROOT / "portfolio_live/phase_c_04_production_router.py").read_text(encoding="utf-8")
        self.assertIn("PRODUCTION_CERTIFICATION_PATH", source)
        self.assertIn("validate_production_certification", source)
        install_pos = source.index("def install_phase_c4_production_router")
        gate_pos = source.index("Phase C.5 production certification is missing", install_pos)
        active_pos = source.index('namespace["_PHASE_C4_ROUTER_ACTIVE"] = True', install_pos)
        self.assertLess(gate_pos, active_pos)

    def test_activation_writer_requires_phase_c5_release_certificate(self):
        source = (ROOT / "portfolio_live/phase_c_04_prepare_activation.py").read_text(encoding="utf-8")
        self.assertIn("PRODUCTION_CERTIFICATION_PATH", source)
        self.assertIn("validate_production_certification", source)
        self.assertIn("--seal-production", source)

if __name__ == "__main__":
    unittest.main()
