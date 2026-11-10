from __future__ import annotations
import copy
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
LIVE=ROOT/"portfolio_live"
for p in (ROOT,LIVE):
    if str(p) not in sys.path: sys.path.insert(0,str(p))
import phase_c_04_production_router as c4


class PhaseC5ProductionCertificationTests(unittest.TestCase):
    def test_router_requires_c5_certificate_before_activation_install(self):
        source=(LIVE/"phase_c_04_production_router.py").read_text(encoding="utf-8")
        self.assertIn("PRODUCTION_CERTIFICATION_PATH",source)
        self.assertIn("validate_production_certification",source)
        self.assertIn("Phase C.5 production certification is missing",source)

    def test_activation_writer_requires_sealed_certificate(self):
        source=(LIVE/"phase_c_04_prepare_activation.py").read_text(encoding="utf-8")
        self.assertIn("validate_production_certification",source)
        self.assertIn("--seal-production",source)

    def test_c5_certifier_has_all_contract_gates(self):
        source=(LIVE/"phase_c_05_certify_production_routing.py").read_text(encoding="utf-8")
        for name in [
            "phase_c2_frozen_feature_authority","phase_c3_frozen_consumer_authority",
            "phase_c_model_decision_parity_all_selected_edges","p0_production_validator",
            "production_hardening_validator","full_unittest_discovery",
        ]: self.assertIn(name,source)
        self.assertIn("6000",source)
        self.assertIn("128",source)
        self.assertIn("current_c2_implementation_fingerprint",source)
        self.assertIn("_validate_frozen_c2_authority",source)
        self.assertIn("_validate_frozen_c3_authority",source)
        self.assertIn("runtime.score(model_rows=",source)
        self.assertNotIn('c2_out=tmp/"fresh_c2.json"', source)
        self.assertNotIn('c3_out=tmp/"fresh_c3.json"', source)

    def test_hardening_validator_owns_short_windows_temp_root(self):
        source=(LIVE/"validate_production_hardening_patch.py").read_text(encoding="utf-8")
        self.assertIn("QUANTARA_VALIDATION_TEMP_ROOT", source)
        self.assertIn("SystemDrive", source)
        self.assertIn('"QTmp"', source)
        self.assertIn('TemporaryDirectory(prefix="qv_", dir=str(validation_temp_root))', source)

    def test_c5_certifier_does_not_hardcode_current_asset_names(self):
        source=(LIVE/"phase_c_05_certify_production_routing.py").read_text(encoding="utf-8")
        for asset in ["BTCUSD","DAX","GBPJPY","JPN","NASDAQ","US500","USOIL","XAUUSD"]:
            self.assertNotIn(asset,source)

    def test_candidate_report_cannot_validate_as_production_certificate(self):
        activation=c4.build_activation_payload(ROOT)
        fake={
            "schema_version":"phase_c5_candidate_certification_v1",
            "status":"PASS","sealed_for_production":False,"production_activation_allowed":False,
        }
        result=c4.validate_production_certification(fake,activation,v2_root=ROOT)
        self.assertEqual(result["status"],"FAIL")

    def test_production_certificate_detects_structural_activation_mismatch(self):
        activation = c4.build_activation_payload(ROOT)
        report = c4.static_contract_report(ROOT)
        gates = {
            name: "PASS"
            for name in sorted(
                {
                    "c4_static_contract",
                    "c4_router_self_test",
                    "phase_c1_frozen_feature_authority",
                    "phase_c2_frozen_feature_authority",
                    "phase_c3_frozen_consumer_authority",
                    "phase_c_model_decision_parity_all_selected_edges",
                    "p0_production_validator",
                    "production_hardening_validator",
                    "full_unittest_discovery",
                }
            )
        }
        contract = {
            "schema_version": c4.PRODUCTION_CERTIFICATION_SCHEMA_VERSION,
            "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
            "router_version": c4.ROUTER_VERSION,
            "selected_assets": list(report["selected_assets"]),
            "selected_edge_ids": list(report["selected_edge_ids"]),
            "required_gates": gates,
        }
        cert = {
            **contract,
            "status": "PASS",
            "sealed_for_production": True,
            "production_activation_allowed": True,
            "certificate_contract": contract,
            "certificate_fingerprint": "informational-only",
        }
        self.assertEqual(
            c4.validate_production_certification(
                cert, activation, v2_root=ROOT
            )["status"],
            "PASS",
        )

        fingerprint_only = copy.deepcopy(activation)
        fingerprint_only["activation_fingerprint"] = "ignored-audit-id"
        self.assertEqual(
            c4.validate_production_certification(
                cert, fingerprint_only, v2_root=ROOT
            )["status"],
            "PASS",
        )

        bad = copy.deepcopy(activation)
        bad["contract"]["selected_edge_ids"] = bad["contract"][
            "selected_edge_ids"
        ][:-1]
        self.assertEqual(
            c4.validate_production_certification(
                cert, bad, v2_root=ROOT
            )["status"],
            "FAIL",
        )



if __name__=="__main__": unittest.main()
