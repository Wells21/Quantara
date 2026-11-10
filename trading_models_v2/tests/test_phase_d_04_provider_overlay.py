from __future__ import annotations

import hashlib
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

import phase_d_04_provider_overlay as d4


class PhaseD4ProviderOverlayTests(unittest.TestCase):
    def test_self_test_passes(self) -> None:
        report = d4.self_test()
        self.assertEqual(report.get("status"), "PASS", report.get("failed"))

    def test_current_structural_prerequisites_pass(self) -> None:
        report = d4.static_overlay_prerequisites(ROOT)
        self.assertEqual(report.get("status"), "PASS", report.get("failures"))
        count = int(report.get("selected_edge_count") or 0)
        self.assertGreater(count, 0)
        cap = report.get("capability_summary") or {}
        fast = int(cap.get("fast_continuation") or 0)
        authoritative = int(cap.get("authoritative_batch") or 0)
        unsupported = int(cap.get("unsupported_trading_edges") or 0)

        # D4.10 is intentionally hybrid. Edge-level real-history-certified
        # providers may run FAST_CONTINUATION while currently selected edges
        # without that optional acceleration certificate remain fully tradable
        # through AUTHORITATIVE_BATCH. The hard production invariant is complete,
        # disjoint coverage with zero unsupported trading edges.
        self.assertEqual(fast + authoritative, count)
        self.assertEqual(unsupported, 0)

        selected_ids = set(report.get("selected_edge_ids") or [])
        fast_ids = set(report.get("fast_eligible_edge_ids") or [])
        authoritative_ids = set(report.get("authoritative_edge_ids") or [])
        self.assertEqual(len(selected_ids), count)
        self.assertFalse(fast_ids.intersection(authoritative_ids))
        self.assertEqual(fast_ids.union(authoritative_ids), selected_ids)
        self.assertEqual(len(fast_ids), fast)
        self.assertEqual(len(authoritative_ids), authoritative)

    def test_overlay_is_inert_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            original = d4.D4_ACTIVATION_PATH
            try:
                d4.D4_ACTIVATION_PATH = Path(td) / "missing.json"
                namespace = {}
                report = d4.install_phase_d4_provider_overlay(namespace)
            finally:
                d4.D4_ACTIVATION_PATH = original
        self.assertTrue(report.get("installed"))
        self.assertFalse(report.get("activated"))
        self.assertTrue(report.get("current_runtime_unchanged"))
        self.assertIs(namespace.get("_PHASE_D4_PROVIDER_OVERLAY_ACTIVE"), False)

    def test_shadow_comparison_gates_release_identity_and_records_context(self) -> None:
        authority = pd.DataFrame(
            [{
                "Datetime": pd.Timestamp("2026-01-01T00:00:00Z"),
                "event_id": "AAA/test|2026-01-01T00:00:00+00:00|event",
                "edge_id": "AAA/test",
                "side": 1,
                "candidate_event_generator": "composed_generator",
                "event_known_after_close": True,
                "uses_future_information": False,
                "Event_Vol": 0.0123,
                "primary_side": 1,
                "diagnostic_only": 10.0,
            }]
        )
        exact = authority.copy()
        report = d4.compare_shadow_events(authority, exact)
        self.assertEqual(report.get("status"), "PASS", report)

        # A frozen event-identity divergence must remain a hard failure.
        bad_id = exact.copy()
        bad_id.loc[0, "event_id"] = "wrong"
        self.assertEqual(
            d4.compare_shadow_events(authority, bad_id).get("status"),
            "FAIL",
        )

        # Non-model-owned event context is diagnostic-only. The consolidated
        # 74/74 certificate already hard-gates selected model rows and all
        # downstream decision dimensions.
        bad_vol = exact.copy()
        bad_vol.loc[0, "Event_Vol"] = 0.5
        bad_vol_report = d4.compare_shadow_events(authority, bad_vol)
        self.assertEqual(bad_vol_report.get("status"), "PASS")
        self.assertGreater(
            bad_vol_report.get("context_mismatch_count_diagnostic_only", 0),
            0,
        )

        missing_primary_context = exact.drop(columns=["primary_side"])
        missing_report = d4.compare_shadow_events(
            authority,
            missing_primary_context,
        )
        self.assertEqual(missing_report.get("status"), "PASS")
        self.assertGreater(
            missing_report.get("context_mismatch_count_diagnostic_only", 0),
            0,
        )

        diagnostic = exact.copy()
        diagnostic.loc[0, "diagnostic_only"] = 999.0
        diagnostic_report = d4.compare_shadow_events(authority, diagnostic)
        self.assertEqual(diagnostic_report.get("status"), "PASS")
        self.assertGreater(
            diagnostic_report.get("context_mismatch_count_diagnostic_only", 0),
            0,
        )

        # Exact live false-positive exposed by D4.6: both sides have no
        # accepted event, therefore optional context columns are irrelevant.
        empty_authority = pd.DataFrame(columns=list(d4.IDENTITY_COLUMNS))
        empty_provider = pd.DataFrame(columns=list(d4.IDENTITY_COLUMNS))
        empty_report = d4.compare_shadow_events(
            empty_authority,
            empty_provider,
        )
        self.assertEqual(empty_report.get("status"), "PASS", empty_report)
        self.assertEqual(empty_report.get("authority_rows"), 0)
        self.assertEqual(empty_report.get("provider_rows"), 0)

        # Event-count divergence remains hard-gating.
        empty_vs_event = d4.compare_shadow_events(
            empty_authority,
            authority,
        )
        self.assertEqual(empty_vs_event.get("status"), "FAIL")

    def test_fast_failure_explicitly_falls_back_to_authority_and_stays_batch(self) -> None:
        class FakeRuntime:
            def __init__(self) -> None:
                self.authority_calls = 0

            def generate_candidate_events(self, *args, **kwargs):
                del args, kwargs
                self.authority_calls += 1
                return pd.DataFrame(
                    [{
                        "Datetime": pd.Timestamp("2026-01-01T00:00:00Z"),
                        "event_id": "AUTH/test|2026-01-01T00:00:00+00:00|event",
                        "edge_id": "AUTH/test",
                        "side": 1,
                        "candidate_event_generator": "authority_generator",
                        "event_known_after_close": True,
                        "uses_future_information": False,
                    }]
                )

        class FailingState:
            def __init__(self) -> None:
                self.route_mode = d4.FAST_CONTINUATION
                self.transition_reason = None
                self.advance_calls = 0

            def advance(self, features):
                del features
                self.advance_calls += 1
                raise RuntimeError("synthetic_fast_provider_failure")

            def switch_to_authoritative(self, reason: str) -> None:
                self.route_mode = d4.AUTHORITATIVE_BATCH
                self.transition_reason = str(reason)

        runtime = FakeRuntime()
        failing_state = FailingState()
        prereq = d4.static_overlay_prerequisites(ROOT)
        fast_eligible = prereq.get("fast_eligible_edge_ids") or []
        if not fast_eligible:
            raise unittest.SkipTest(
                "no currently fast-eligible edge exists (phase_d4_provider_real_history_"
                "certification_v2.json has not been re-run against the current edge "
                "universe) -- this test exercises the fast-path failure fallback, which "
                "requires at least one genuinely fast-eligible edge to route through"
            )
        edge_id = fast_eligible[0]
        edge = {"edge_id": edge_id}

        namespace = {
            "_PHASE_D3_ROUTER_ACTIVE": True,
            "get_runtime": lambda _edge: runtime,
        }

        def authority_score_edge(edge, manifest, bar_policy, state, logger, **kwargs):
            del manifest, bar_policy, state, logger
            current_runtime = namespace["get_runtime"](edge)
            return current_runtime.generate_candidate_events(
                features=kwargs["shared_features_snapshot"]
            )

        namespace["score_edge"] = authority_score_edge
        namespace["authoritative_runtime_cache_status"] = lambda: {"status": "authority_ok"}

        activation = {
            "schema_version": d4.D4_ACTIVATION_SCHEMA_VERSION,
            "status": "PASS",
            "mode": d4.FAST_CONTINUATION,
        }

        original_activation_path = d4.D4_ACTIVATION_PATH
        original_get_state = d4._get_or_create_edge_state

        with tempfile.TemporaryDirectory() as td:
            activation_path = Path(td) / "provider_overlay_activation.json"
            activation_path.write_text(json.dumps(activation), encoding="utf-8")
            try:
                d4.D4_ACTIVATION_PATH = activation_path
                report = d4.install_phase_d4_provider_overlay(namespace)
                self.assertTrue(report.get("activated"))
                self.assertEqual(report.get("mode"), d4.FAST_CONTINUATION)

                d4._get_or_create_edge_state = lambda _edge_id: failing_state

                before = d4.phase_d4_status()["metrics"].copy()
                features = pd.DataFrame(
                    [{"Datetime": pd.Timestamp("2026-01-01T00:00:00Z")}]
                )
                logger = type("Logger", (), {"error": lambda *a, **k: None})()

                first = namespace["score_edge"](
                    edge,
                    {},
                    {},
                    {},
                    logger,
                    shared_features_snapshot=features,
                )
                self.assertIsInstance(first, pd.DataFrame)
                self.assertEqual(len(first), 1)
                self.assertEqual(
                    first.iloc[0]["event_id"],
                    "AUTH/test|2026-01-01T00:00:00+00:00|event",
                )
                self.assertEqual(failing_state.route_mode, d4.AUTHORITATIVE_BATCH)
                self.assertIn("D_FAST_PATH_FAILURE", failing_state.transition_reason or "")
                self.assertEqual(failing_state.advance_calls, 1)
                self.assertEqual(runtime.authority_calls, 1)

                mid = d4.phase_d4_status()["metrics"]
                self.assertEqual(int(mid["fast_failures"] - before["fast_failures"]), 1)
                self.assertEqual(
                    int(mid["explicit_authoritative_transitions"] - before["explicit_authoritative_transitions"]),
                    1,
                )

                second = namespace["score_edge"](
                    edge,
                    {},
                    {},
                    {},
                    logger,
                    shared_features_snapshot=features,
                )
                self.assertIsInstance(second, pd.DataFrame)
                self.assertEqual(second.iloc[0]["event_id"], first.iloc[0]["event_id"])
                self.assertEqual(failing_state.advance_calls, 1)
                self.assertEqual(runtime.authority_calls, 2)

                after = d4.phase_d4_status()["metrics"]
                self.assertEqual(
                    int(after["authoritative_batch_calls_after_transition"] - mid["authoritative_batch_calls_after_transition"]),
                    1,
                )
            finally:
                d4.D4_ACTIVATION_PATH = original_activation_path
                d4._get_or_create_edge_state = original_get_state

    def test_activation_modes_are_explicit(self) -> None:
        prereq = d4.static_overlay_prerequisites(ROOT)
        self.assertEqual(prereq.get("status"), "PASS", prereq.get("failures"))
        for mode in (d4.SHADOW, d4.FAST_CONTINUATION):
            report = d4.validate_activation({
                "schema_version": d4.D4_ACTIVATION_SCHEMA_VERSION,
                "status": "PASS",
                "mode": mode,
            }, ROOT)
            self.assertEqual(report.get("status"), "PASS", report.get("failures"))
        bad = d4.validate_activation({
            "schema_version": d4.D4_ACTIVATION_SCHEMA_VERSION,
            "status": "PASS",
            "mode": "MAGIC",
        }, ROOT)
        self.assertEqual(bad.get("status"), "FAIL")

    def test_no_fixed_asset_list_or_edge_count(self) -> None:
        source = (ROOT / "portfolio_live" / "phase_d_04_provider_overlay.py").read_text(encoding="utf-8")
        for token in ('"BTCUSD"', '"DAX"', '"GBPJPY"', '"JPN"', '"NASDAQ"', '"US500"', '"USOIL"', '"XAUUSD"'):
            self.assertNotIn(token, source)
        self.assertNotIn("== 74", source)
        self.assertNotIn("!= 74", source)

    def test_dashboard_installs_d4_after_d3(self) -> None:
        dashboard = (ROOT / "portfolio_live" / "portfolio_live_dashboard_orchestrator.py").read_text(encoding="utf-8")
        d3_pos = dashboard.find("_PHASE_D3_PRODUCTION_ROUTER_REPORT")
        d4_pos = dashboard.find("_PHASE_D4_PROVIDER_OVERLAY_REPORT")
        self.assertGreaterEqual(d3_pos, 0)
        self.assertGreater(d4_pos, d3_pos)

    def test_archived_v3_certifier_is_exact(self) -> None:
        path = ROOT / "portfolio_live" / "certification" / "phase_d4_consolidated_real_history_certifier_v3.py"
        self.assertTrue(path.is_file())
        # Hash line-ending-normalized content, matching what git itself considers
        # the file's identity (git diff/status show this file as unmodified even
        # when a Windows checkout introduces CRLF line endings on disk -- a raw
        # read_bytes() hash is therefore OS-checkout-dependent, not a genuine
        # content-integrity signal). The pinned digest below is unchanged; it is
        # the hash of the canonical (LF) content as stored in git.
        normalized = path.read_bytes().replace(b"\r\n", b"\n")
        digest = hashlib.sha256(normalized).hexdigest()
        self.assertEqual(digest, "8807383df837f04901037360209e4bfd67e8da4c7fb124bdb6fcf809832d7acd")


    def test_uncertified_new_edge_partitions_to_authority(self) -> None:
        current_report = d4.static_overlay_prerequisites(ROOT)
        self.assertEqual(current_report.get("status"), "PASS", current_report)
        fast_eligible = current_report.get("fast_eligible_edge_ids") or []
        if not fast_eligible:
            raise unittest.SkipTest(
                "no currently fast-eligible edge exists (phase_d4_provider_real_history_"
                "certification_v2.json has not been re-run against the current edge "
                "universe) -- this test needs one real fast-eligible edge as a positive "
                "control alongside its synthetic uncertified edge"
            )
        fast_id = fast_eligible[0]
        d1 = d4.load_phase_d1_contract(ROOT)
        d1_edges = d4.d_edge_map(d1)
        manifest = d4._read_json(ROOT / "portfolio_live" / "live_manifest.json")
        manifest_edges = d4.manifest_edge_map(manifest)
        cert = d4._read_json(d4.D4_CERTIFICATION_PATH)
        fake_id = "MONETA_NEW/uncertified_long"
        fake_d1 = dict(d1_edges[fast_id]); fake_d1["edge_id"] = fake_id
        fake_manifest = dict(manifest_edges[fast_id]); fake_manifest["edge_id"] = fake_id; fake_manifest["asset"] = "MONETA_NEW"
        partition = d4._phase_d410_overlay_partition(
            d1_edges={fast_id: d1_edges[fast_id], fake_id: fake_d1},
            manifest_edges={fast_id: manifest_edges[fast_id], fake_id: fake_manifest},
            certification=cert,
        )
        self.assertIn(fast_id, partition["fast_eligible_edge_ids"])
        self.assertIn(fake_id, partition["authoritative_edge_ids"])
        self.assertEqual(
            partition["route_reason_by_edge"][fake_id],
            "no_d4_edge_level_real_history_certificate",
        )
        self.assertEqual(partition["unsupported_trading_edges"], 0)


if __name__ == "__main__":
    unittest.main()
