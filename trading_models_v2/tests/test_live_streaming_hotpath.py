from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

import pandas as pd

from shared.live_streaming import absorption, continuation, hotpath, state
from shared.edges import live_stateful_runtime
from shared.edges import native_one_row_feature_engine as native_feature_engine
from shared.edges import native_one_row_contract
from shared.edges.native_one_row_event_engine import (
    NativeEventError,
    transition_events_live_native,
)
from shared.edges.native_one_row_feature_engine import _project_authoritative_schema


class HotpathTests(unittest.TestCase):
    def setUp(self) -> None:
        state.FRAME_CACHE.clear()
        state.FFD_CACHE.clear()
        state.HMM_CACHE.clear()
        state.FFD_DELTA.clear()

    def test_frame_cache_extends_exact_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            initial = pd.DataFrame(
                {
                    "Datetime": ["2026-06-24T10:00:00Z"],
                    "Open_FFD": [1.0],
                    "unused": [2.0],
                }
            )
            initial.to_csv(path, index=False)
            loaded = state.frame_load("ffd", "XAUUSD", path, ["Datetime", "Open_FFD"])
            self.assertEqual(list(loaded.columns), ["Datetime", "Open_FFD"])
            row = pd.DataFrame(
                {
                    "Datetime": ["2026-06-24T10:01:00Z"],
                    "Open_FFD": [3.0],
                    "unused": [4.0],
                }
            )
            state.append_csv_rows(path, row, list(row.columns))
            state.frame_extend("ffd", "XAUUSD", path, row)
            loaded = state.frame_load("ffd", "XAUUSD", path, ["Datetime", "Open_FFD"])
            self.assertEqual(len(loaded), 2)

    def test_frame_load_preserves_mixed_timestamp_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed_hmm.csv"
            pd.DataFrame(
                {
                    "Datetime": [
                        "2026-08-14T13:43:01.516000+00:00",
                        "2026-08-14 14:21:41.593000+00:00",
                    ],
                    "Regime_State": [1, 2],
                }
            ).to_csv(path, index=False)

            loaded = state.frame_load(
                "hmm",
                "BTCUSD",
                path,
                ["Datetime", "Regime_State"],
            )

            self.assertEqual(len(loaded), 2)
            self.assertFalse(loaded["Datetime"].isna().any())
            self.assertIn(
                pd.Timestamp("2026-08-14T14:21:41.593000Z"),
                set(loaded["Datetime"]),
            )

    def test_frame_load_preserves_whole_second_among_fractional_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed_ffd.csv"
            pd.DataFrame(
                {
                    "Datetime": [
                        "2026-08-12 01:26:01.505000+00:00",
                        "2026-08-12 01:42:16+00:00",
                        "2026-08-12 02:00:11.475000+00:00",
                    ],
                    "Open_FFD": [1.0, 2.0, 3.0],
                }
            ).to_csv(path, index=False)

            loaded = state.frame_load(
                "ffd",
                "JPN",
                path,
                ["Datetime", "Open_FFD"],
            )

            self.assertEqual(len(loaded), 3)
            self.assertFalse(loaded["Datetime"].isna().any())
            self.assertIn(
                pd.Timestamp("2026-08-12T01:42:16Z"),
                set(loaded["Datetime"]),
            )

    def test_frame_extend_preserves_space_serialized_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "extend_hmm.csv"
            initial = pd.DataFrame(
                {
                    "Datetime": ["2026-08-14T13:43:01.516000+00:00"],
                    "Regime_State": [1],
                }
            )
            initial.to_csv(path, index=False)
            state.frame_load(
                "hmm",
                "BTCUSD",
                path,
                ["Datetime", "Regime_State"],
            )

            appended = pd.DataFrame(
                {
                    "Datetime": ["2026-08-14 14:21:41.593000+00:00"],
                    "Regime_State": [2],
                }
            )
            state.append_csv_rows(
                path,
                appended,
                ["Datetime", "Regime_State"],
            )
            state.frame_extend("hmm", "BTCUSD", path, appended)

            loaded = state.frame_load(
                "hmm",
                "BTCUSD",
                path,
                ["Datetime", "Regime_State"],
            )
            self.assertEqual(len(loaded), 2)
            self.assertIn(
                pd.Timestamp("2026-08-14T14:21:41.593000Z"),
                set(loaded["Datetime"]),
            )

    def test_ffd_runtime_reader_preserves_mixed_precision_timestamp(self) -> None:
        original_ffd_module = state.FFD_MODULE
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "ffd.csv"
                pd.DataFrame(
                    {
                        "Datetime": [
                            "2026-08-12 01:26:01.505000+00:00",
                            "2026-08-12 01:42:16+00:00",
                            "2026-08-12 02:00:11.475000+00:00",
                        ],
                        "Open": [1.0, 2.0, 3.0],
                        "High": [2.0, 3.0, 4.0],
                        "Low": [0.5, 1.5, 2.5],
                        "Close": [1.5, 2.5, 3.5],
                        "Open_FFD": [1.0, 2.0, 3.0],
                        "High_FFD": [2.0, 3.0, 4.0],
                        "Low_FFD": [0.5, 1.5, 2.5],
                        "Close_FFD": [1.5, 2.5, 3.5],
                    }
                ).to_csv(path, index=False)

                module = ModuleType("fake_ffd_mixed_timestamp_reader")
                module._V2_ROOT = root
                state.FFD_MODULE = module

                loaded = continuation._read_ffd_runtime_rows(
                    {"features": "ffd.csv"}
                )

                self.assertEqual(len(loaded), 3)
                self.assertFalse(loaded["Datetime"].isna().any())
                self.assertIn(
                    pd.Timestamp("2026-08-12T01:42:16Z"),
                    set(loaded["Datetime"]),
                )
        finally:
            state.FFD_MODULE = original_ffd_module

    def test_hmm_hot_proof_watermark_advances_to_committed_target(self) -> None:
        stale = {
            "source_ffd_latest_timestamp":
                "2026-08-14T15:31:07.307000+00:00",
            "expected_hmm_latest_timestamp":
                "2026-08-14T15:31:07.307000+00:00",
        }
        ffd_source = pd.DataFrame(
            {
                "Datetime": [
                    "2026-08-14T15:31:07.307000+00:00",
                    "2026-08-14 16:43:45.122000+00:00",
                ]
            }
        )
        updated = continuation._refresh_hmm_proof_watermarks(
            stale,
            ffd_source=ffd_source,
            hmm_target="2026-08-14T16:43:45.122000+00:00",
        )
        self.assertEqual(
            updated["source_ffd_latest_timestamp"],
            "2026-08-14T16:43:45.122000+00:00",
        )
        self.assertEqual(
            updated["expected_hmm_latest_timestamp"],
            "2026-08-14T16:43:45.122000+00:00",
        )

    def test_live_common_runtime_import_binds_snapshot_hmm_generation(self) -> None:
        from portfolio_live import common as live_common

        target = pd.Timestamp("2026-08-14T16:43:45.122000Z")
        namespace = {"pd": pd}

        def forbidden_disk_loader(asset="BAD"):
            raise AssertionError(
                "edge-local disk HMM loader escaped live generation binding: "
                + str(asset)
            )

        namespace["load_verified_regime_features"] = forbidden_disk_loader
        exec(
            "def add_verified_regime_features(features, asset='BTCUSD'):\n"
            "    out = features.copy()\n"
            "    regime = load_verified_regime_features(asset)\n"
            "    return out.merge(regime, on='Datetime', how='left', "
            "validate='one_to_one')\n"
            "\n"
            "def build_features(bars=None, drop_warmup_rows=False):\n"
            "    return add_verified_regime_features(bars.copy())\n"
            "\n"
            "def generate_candidate_events(features):\n"
            "    return features[['Datetime']].copy()\n"
            "\n"
            "def build_model_rows(*args, **kwargs):\n"
            "    return pd.DataFrame(), {}\n"
            "\n"
            "def score(*args, **kwargs):\n"
            "    return pd.DataFrame()\n",
            namespace,
        )

        runtime = ModuleType("fake_live_generation_bound_runtime")
        runtime.ASSET = "BTCUSD"
        runtime.EDGE_ID = "BTCUSD/rangestructure_short"
        runtime.FAMILY = "rangestructure"
        runtime.SIDE_VALUE = -1
        for name in (
            "add_verified_regime_features",
            "build_features",
            "generate_candidate_events",
            "build_model_rows",
            "score",
        ):
            setattr(runtime, name, namespace[name])

        snapshot_hmm = pd.DataFrame(
            {
                "Datetime": [target],
                "Regime_State": [1.0],
                "Regime_Entropy": [0.641464],
                "Regime_Duration": [1.0],
                "Prob_State_0": [0.000066],
                "Prob_State_1": [0.660308],
                "Prob_State_2": [0.339626],
                "Prob_State_3": [0.0],
            }
        )
        bars = pd.DataFrame({"Datetime": [target]})
        snapshot = {
            "asset": "BTCUSD",
            "generation_id": "phase-a2-test-generation",
            "hmm": snapshot_hmm,
        }

        with (
            mock.patch.object(
                live_common,
                "load_edge_runtime_from_manifest_entry",
                return_value=runtime,
            ),
            state.feature_snapshot_scope(snapshot),
        ):
            loaded = live_common.import_edge_runtime(
                {"edge_id": "BTCUSD/rangestructure_short"}
            )
            features = loaded.build_features(bars)

        report = getattr(
            loaded,
            "_quantara_live_shared_loader_patch",
            {},
        )
        self.assertGreater(int(report.get("patched", 0)), 0)
        row = features.loc[features["Datetime"] == target].iloc[-1]
        for name in (
            "Regime_State",
            "Regime_Entropy",
            "Regime_Duration",
            "Prob_State_1",
            "Prob_State_3",
        ):
            self.assertTrue(pd.notna(row[name]), name)

    def test_ffd_fallback_invalidates_hmm_cache(self) -> None:
        original = state.ORIGINAL_FFD_VERIFY
        state.HMM_CACHE["NASDAQ"] = {"signature": (1, 1), "result": {}}
        try:
            state.ORIGINAL_FFD_VERIFY = lambda asset: {
                "asset": asset,
                "features": "ffd.csv",
            }
            result = continuation._fallback_ffd("NASDAQ", (2, 2), "test")
        finally:
            state.ORIGINAL_FFD_VERIFY = original
        self.assertEqual(result["asset"], "NASDAQ")
        self.assertNotIn("NASDAQ", state.HMM_CACHE)
        self.assertEqual(state.FFD_CACHE["NASDAQ"]["signature"], (2, 2))

    def test_hmm_fallback_reseeds_captured_ffd(self) -> None:
        original_ffd, original_hmm = (
            state.ORIGINAL_FFD_VERIFY,
            state.ORIGINAL_HMM_VERIFY,
        )
        namespace: dict[str, object] = {}
        exec(
            "def verify_asset_ffd_manifest(asset, *args, **kwargs):\n    raise AssertionError\n"
            "def hmm_verify(asset, *args, **kwargs):\n    ffd=verify_asset_ffd_manifest(asset, *args, **kwargs)\n    return {'asset':asset,'features':'hmm.csv','ffd':ffd['features']}\n",
            namespace,
        )
        try:
            state.ORIGINAL_FFD_VERIFY = lambda asset, *args, **kwargs: {
                "asset": asset,
                "features": "ffd.csv",
            }
            state.ORIGINAL_HMM_VERIFY = namespace["hmm_verify"]
            result = continuation._fallback_hmm("XAUUSD", (3, 3), "test")
        finally:
            state.ORIGINAL_FFD_VERIFY, state.ORIGINAL_HMM_VERIFY = (
                original_ffd,
                original_hmm,
            )
        self.assertEqual(result["ffd"], "ffd.csv")
        self.assertEqual(state.FFD_CACHE["XAUUSD"]["signature"], (3, 3))

    def test_absorption_non_event_skips_authoritative_builder(self) -> None:
        calls = {"features": 0}
        runtime = ModuleType("fake_absorption")
        runtime.ASSET, runtime.EDGE_ID, runtime.FAMILY, runtime.SIDE_VALUE = (
            "XAUUSD",
            "XAUUSD/absorption_long",
            "absorption",
            1,
        )
        runtime.ta = type(
            "TA",
            (),
            {
                "atr": staticmethod(
                    lambda high, low, close, length=14: pd.Series([1.0] * len(close))
                )
            },
        )()
        runtime.generate_candidate_events = lambda features: pd.DataFrame(
            columns=["Datetime"]
        )

        def original_features(**kwargs):
            calls["features"] += 1
            return {"features": kwargs["bars"], "state": {}, "metadata": {}}

        runtime.build_features_live_incremental = original_features
        runtime.generate_candidate_events_live = lambda **kwargs: {
            "events": pd.DataFrame(),
            "state": {},
            "metadata": {},
        }
        bars = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    ["2026-06-24T10:00:00Z", "2026-06-24T10:15:00Z"], utc=True
                ),
                "Bar_Start": pd.to_datetime(
                    ["2026-06-24T09:45:00Z", "2026-06-24T10:00:00Z"], utc=True
                ),
                "Open": [1.0, 2.0],
                "High": [2.0, 3.0],
                "Low": [0.5, 1.5],
                "Close": [1.5, 2.5],
            }
        )
        flags = {
            "streaming_parity_passed": True,
            "append_only_history": True,
            "restart_parity_passed": True,
            "hmm_refit_schedule_preserved": True,
            "deterministic_noise_contract_preserved": True,
        }
        with (
            mock.patch.object(
                absorption, "verify_asset_ffd_manifest_hot", return_value=flags
            ),
            mock.patch.object(
                absorption, "verify_asset_hmm_regime_manifest_hot", return_value=flags
            ),
        ):
            absorption.install_absorption_event_gate(runtime)
            result = runtime.build_features_live_incremental(
                bars=bars, candidate_times=[pd.Timestamp("2026-06-24T10:15:00Z")]
            )
        self.assertEqual(calls["features"], 0)
        self.assertIn(state.PROBE_MARKER, result["features"].columns)

    def test_absorption_candidate_uses_authoritative_builder(self) -> None:
        calls = {"features": 0}
        runtime = ModuleType("fake_absorption_candidate")
        runtime.ASSET, runtime.EDGE_ID, runtime.FAMILY, runtime.SIDE_VALUE = (
            "NASDAQ",
            "NASDAQ/absorption_long",
            "absorption",
            1,
        )
        runtime.ta = type(
            "TA",
            (),
            {
                "atr": staticmethod(
                    lambda high, low, close, length=14: pd.Series([1.0] * len(close))
                )
            },
        )()
        candidate = pd.Timestamp("2026-06-24T10:15:00Z")
        runtime.generate_candidate_events = lambda features: pd.DataFrame(
            {"Datetime": [candidate]}
        )

        def original_features(**kwargs):
            calls["features"] += 1
            return {"features": kwargs["bars"], "state": {}, "metadata": {}}

        runtime.build_features_live_incremental = original_features
        runtime.generate_candidate_events_live = lambda **kwargs: {
            "events": pd.DataFrame(),
            "state": {},
            "metadata": {},
        }
        bars = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    ["2026-06-24T10:00:00Z", "2026-06-24T10:15:00Z"], utc=True
                ),
                "Bar_Start": pd.to_datetime(
                    ["2026-06-24T09:45:00Z", "2026-06-24T10:00:00Z"], utc=True
                ),
                "Open": [1.0, 2.0],
                "High": [2.0, 3.0],
                "Low": [0.5, 1.5],
                "Close": [1.5, 2.5],
            }
        )
        absorption.install_absorption_event_gate(runtime)
        runtime.build_features_live_incremental(bars=bars, candidate_times=[candidate])
        self.assertEqual(calls["features"], 1)

    def test_runtime_loader_patch_is_asset_bound(self) -> None:
        namespace = {"load_verified_ffd_features": lambda asset="BAD": pd.DataFrame()}
        exec(
            "def build_features():\n    return load_verified_ffd_features()\n",
            namespace,
        )
        runtime = ModuleType("fake_runtime")
        runtime.ASSET, runtime.FAMILY, runtime.build_features = (
            "BTCUSD",
            "trend",
            namespace["build_features"],
        )
        with mock.patch.object(
            hotpath,
            "load_verified_ffd_features_hot",
            return_value=pd.DataFrame({"asset": ["BTCUSD"]}),
        ):
            report = hotpath.patch_runtime_shared_loaders(runtime)
            frame = runtime.build_features()
        self.assertGreater(report["patched"], 0)
        self.assertEqual(frame.iloc[0]["asset"], "BTCUSD")

    def test_original_dashboard_is_single_integrated_entrypoint(self) -> None:
        v2_root = Path(__file__).resolve().parents[1]
        live_dir = v2_root / "portfolio_live"
        orchestrator = live_dir / "portfolio_live_dashboard_orchestrator.py"
        p0_path = live_dir / "production_readiness_p0.py"
        snapshot_path = live_dir / "production_snapshot_pipeline.py"
        retired_launcher = live_dir / "portfolio_live_dashboard_hotpath.py"

        self.assertTrue(orchestrator.exists())
        self.assertTrue(p0_path.exists())
        self.assertTrue(snapshot_path.exists())
        self.assertFalse(retired_launcher.exists())

        orchestrator_source = orchestrator.read_text(encoding="utf-8")
        p0_source = p0_path.read_text(encoding="utf-8")
        snapshot_source = snapshot_path.read_text(encoding="utf-8")

        self.assertIn("install_p0_production_hardening", orchestrator_source)
        self.assertIn("install_snapshot_pipeline", orchestrator_source)
        self.assertIn("install_hotpath_verifiers", p0_source)
        self.assertIn("_install_hotpath(namespace)", p0_source)
        self.assertIn("asset_snapshot_coordinator", snapshot_source)
        common_source = (live_dir / "common.py").read_text(encoding="utf-8")
        self.assertIn(
            "patch_runtime_shared_loaders(runtime)",
            common_source,
        )
        self.assertIn(
            "PHASE_A_LIVE_GENERATION_CONSUMER_BINDING_V1",
            common_source,
        )
        continuation_source = (
            v2_root / "shared" / "live_streaming" / "continuation.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "PHASE_A_HMM_PROOF_WATERMARK_V1",
            continuation_source,
        )


    def test_native_event_checkpoint_proves_restart_and_rejects_revision(self) -> None:
        runtime = ModuleType("fake_native_event")
        runtime.ASSET = "XAUUSD"
        runtime.EDGE_ID = "XAUUSD/fake_long"
        runtime.FAMILY = "trend"
        runtime.SIDE_VALUE = 1

        def generate(features: pd.DataFrame) -> pd.DataFrame:
            work = features.copy()
            work["Datetime"] = pd.to_datetime(work["Datetime"], utc=True)
            out = work.loc[work["signal"] > 0.95, ["Datetime", "signal"]].copy()
            if out.empty:
                return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
            out["event_id"] = out["Datetime"].astype(str)
            out["side"] = 1
            return out

        runtime.generate_candidate_events = generate
        times = pd.date_range("2026-01-01", periods=360, freq="min", tz="UTC")
        features = pd.DataFrame(
            {
                "Datetime": times,
                "signal": pd.Series(range(360), dtype="float64") / 359.0,
                "category": ["x"] * 360,
            }
        )
        base = features.iloc[:350].copy()
        first = transition_events_live_native(
            runtime,
            asset="XAUUSD",
            edge_id=runtime.EDGE_ID,
            features=base,
            context_rows=120,
            overlap_rows=60,
            candidate_times=[base["Datetime"].iloc[-1]],
            state=None,
            certification_hash="test-cert",
            certified_restart_parity=True,
        )
        prefix = features.iloc[:351].copy()
        continuous = transition_events_live_native(
            runtime,
            asset="XAUUSD",
            edge_id=runtime.EDGE_ID,
            features=prefix,
            context_rows=120,
            overlap_rows=60,
            candidate_times=[prefix["Datetime"].iloc[-1]],
            state=first["state"],
            certification_hash="test-cert",
            certified_restart_parity=True,
        )
        import pickle

        restarted = transition_events_live_native(
            runtime,
            asset="XAUUSD",
            edge_id=runtime.EDGE_ID,
            features=prefix,
            context_rows=120,
            overlap_rows=60,
            candidate_times=[prefix["Datetime"].iloc[-1]],
            state=pickle.loads(pickle.dumps(first["state"])),
            certification_hash="test-cert",
            certified_restart_parity=True,
        )
        self.assertEqual(
            continuous["events"].to_json(date_format="iso"),
            restarted["events"].to_json(date_format="iso"),
        )
        for flag in live_stateful_runtime.EVENT_REQUIRED_PROOF_FLAGS:
            self.assertIs(continuous["metadata"].get(flag), True)

        tampered = prefix.copy()
        tampered.loc[tampered.index[-20], "signal"] += 0.1
        with self.assertRaises(NativeEventError):
            transition_events_live_native(
                runtime,
                asset="XAUUSD",
                edge_id=runtime.EDGE_ID,
                features=tampered,
                context_rows=120,
                overlap_rows=60,
                candidate_times=[tampered["Datetime"].iloc[-1]],
                state=first["state"],
                certification_hash="test-cert",
                certified_restart_parity=True,
            )

    def test_adapter_fails_closed_when_native_event_proof_is_incomplete(self) -> None:
        runtime = ModuleType("fake_adapter_runtime")
        runtime.ASSET = "BTCUSD"
        runtime.EDGE_ID = "BTCUSD/fake_long"
        runtime.FAMILY = "trend"
        runtime.SIDE_VALUE = 1
        runtime.build_features = lambda bars, drop_warmup_rows=False: bars.copy()
        runtime.generate_candidate_events = lambda features: pd.DataFrame(
            {"Datetime": pd.Series(dtype="datetime64[ns, UTC]")}
        )
        runtime.score = lambda *args, **kwargs: pd.DataFrame()
        features = pd.DataFrame(
            {
                "Datetime": pd.date_range("2026-01-01", periods=400, freq="min", tz="UTC"),
                "signal": [0.0] * 400,
            }
        )
        incomplete = {
            "events": pd.DataFrame(),
            "state": {},
            "metadata": {
                "streaming_parity_passed": True,
                "event_state_continuity_preserved": True,
            },
        }
        with (
            mock.patch.object(
                live_stateful_runtime,
                "event_certification",
                return_value={"event_parity_passed": True},
            ),
            mock.patch.object(
                live_stateful_runtime,
                "build_events_live_native",
                return_value=incomplete,
            ),
        ):
            live_stateful_runtime.install_stateful_runtime_api(
                runtime,
                {"edge_id": runtime.EDGE_ID, "asset": runtime.ASSET},
            )
            with self.assertRaises(
                live_stateful_runtime.StatefulRuntimeContractError
            ) as captured:
                runtime.generate_candidate_events_live(
                    features=features,
                    candidate_times=[features["Datetime"].iloc[-1]],
                    state={},
                )

        message = str(captured.exception)
        self.assertIn("silent suffix fallback is forbidden", message)
        self.assertIn("missing native event proof flags", message)

    def test_native_feature_projection_hides_internal_superset_columns(self) -> None:
        frame = pd.DataFrame(
            {
                "Datetime": pd.date_range("2026-01-01", periods=2, freq="min", tz="UTC"),
                "authoritative": [1.0, 2.0],
                "native_internal_only": [9.0, 9.0],
            }
        )
        projected = _project_authoritative_schema(
            frame,
            columns=["Datetime", "authoritative"],
            schema_hash="test",
        )
        self.assertEqual(list(projected.columns), ["Datetime", "authoritative"])

    def test_native_feature_bootstrap_reuses_dashboard_authority(self) -> None:
        times = pd.date_range("2026-01-01", periods=20, freq="min", tz="UTC")
        bars = pd.DataFrame(
            {
                "Datetime": times,
                "Bar_Start": times - pd.Timedelta(minutes=1),
                "Open": [100.0 + i for i in range(20)],
                "High": [101.0 + i for i in range(20)],
                "Low": [99.0 + i for i in range(20)],
                "Close": [100.5 + i for i in range(20)],
                "Ticks": [100] * 20,
                "Threshold": [100] * 20,
            }
        )
        authority = bars.copy()
        authority["Feature_A"] = [float(i) for i in range(20)]
        runtime = ModuleType("seeded_feature_runtime")
        runtime.SIDE_VALUE = 1
        runtime.build_features = mock.Mock(
            side_effect=AssertionError("authoritative feature builder must not run twice")
        )
        with (
            mock.patch.object(
                native_feature_engine, "_bootstrap_recursive", return_value={"seeded": True}
            ),
            mock.patch.object(
                native_feature_engine, "_absorption_columns", side_effect=lambda frame: frame
            ),
            mock.patch.object(
                native_feature_engine, "_ensure_canonical_shared_columns",
                side_effect=lambda frame: frame,
            ),
        ):
            output, state_payload, metadata = native_feature_engine.bootstrap_state(
                runtime,
                asset="XAUUSD",
                edge_id="XAUUSD/fake_long",
                bars=bars,
                authoritative_features=authority,
            )
        runtime.build_features.assert_not_called()
        self.assertEqual(list(output.columns), list(authority.columns))
        self.assertEqual(
            metadata.get("authoritative_bootstrap_source"),
            "dashboard_supplied_authoritative_full_build",
        )
        self.assertEqual(
            state_payload.get("authoritative_bootstrap_source"),
            "dashboard_supplied_authoritative_full_build",
        )

    def test_original_dashboard_has_broker_time_range_recovery(self) -> None:
        v2_root = Path(__file__).resolve().parents[1]
        source = (
            v2_root / "portfolio_live" / "portfolio_live_dashboard_orchestrator.py"
        ).read_text(encoding="utf-8")
        self.assertIn("LIVE_TICK_RANGE_RECOVERY", source)
        self.assertIn("true_utc_to_broker_time(recovery_true_start", source)
        self.assertIn("symbol_info_tick(symbol)", source)
        self.assertIn("snapshot_ahead", source)
        self.assertIn("copy_ticks_range", source)


    def test_full_authority_certification_never_enables_native_event_engine(self) -> None:
        native_row = {
            "feature_parity_passed": True,
            "event_parity_passed": True,
            "event_execution_mode": native_one_row_contract.NATIVE_EVENT_MODE,
            "native_event_eligible": True,
            "append_only_history_passed": True,
            "restart_parity_passed": True,
            "event_state_continuity_passed": True,
            "historical_revision_rejected": True,
            "rolling_window_event_parity_passed": True,
            "sequential_event_authority_parity_passed": True,
            "rolling_window_event_authority_parity_passed": True,
            "sequential_event_step_count":
                native_one_row_contract.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
            "rolling_window_event_step_count":
                native_one_row_contract.MIN_SEQUENTIAL_CERTIFICATION_STEPS,
            "long_sequential_event_certification_passed": True,
            "restart_checkpoint_count": 4,
            "context_selection_policy":
                "smallest_anchor_and_long_sequence_passing_context",
            "silent_suffix_fallback_forbidden": True,
        }
        full_row = {
            "feature_parity_passed": True,
            "event_parity_passed": True,
            "event_execution_mode": native_one_row_contract.FULL_AUTHORITY_EVENT_MODE,
            "native_event_eligible": False,
            "full_authority_required": True,
            "force_full_event_replay": True,
            "full_authority_determinism_passed": True,
        }
        self.assertTrue(native_one_row_contract.edge_certification_passed(native_row))
        self.assertTrue(native_one_row_contract.edge_certification_passed(full_row))

        payload = {
            "schema_version": native_one_row_contract.SCHEMA_VERSION,
            "status": "PASS",
            "selected_edge_count": 33,
            "implementation_hashes": {"x": "y"},
            "runtime_environment": {"python": "test"},
            "edges": {
                "BTCUSD/native_short": native_row,
                "BTCUSD/drawdownhazardcontinuation_short": full_row,
            },
            "assets": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "certification.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            with (
                mock.patch.object(
                    native_one_row_contract,
                    "implementation_hashes",
                    return_value={"x": "y"},
                ),
                mock.patch.object(
                    native_one_row_contract,
                    "runtime_environment",
                    return_value={"python": "test"},
                ),
            ):
                native = native_one_row_contract.event_certification(
                    "BTCUSD/native_short", path
                )
                full = native_one_row_contract.event_certification(
                    "BTCUSD/drawdownhazardcontinuation_short", path
                )
                classification = (
                    native_one_row_contract.event_execution_classification(
                        "BTCUSD/drawdownhazardcontinuation_short", path
                    )
                )
        self.assertTrue(native)
        self.assertEqual(full, {})
        self.assertEqual(
            classification["event_execution_mode"],
            native_one_row_contract.FULL_AUTHORITY_EVENT_MODE,
        )
        self.assertFalse(classification["native_event_eligible"])
        self.assertTrue(classification["full_authority_required"])

    def test_adapter_reports_explicit_full_authority_classification(self) -> None:
        runtime = ModuleType("fake_drawdown_hazard")
        runtime.EDGE_ID = "BTCUSD/drawdownhazardcontinuation_short"
        runtime.ASSET = "BTCUSD"
        runtime.FAMILY = "drawdownhazardcontinuation"
        runtime.SIDE_VALUE = -1

        def build_features(*, bars, drop_warmup_rows=False):
            del drop_warmup_rows
            return bars.copy()

        def generate_candidate_events(features):
            return pd.DataFrame({"Datetime": features["Datetime"].tail(1)})

        runtime.build_features = build_features
        runtime.generate_candidate_events = generate_candidate_events
        runtime.score = lambda rows: rows

        features = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    ["2026-06-24T10:00:00Z", "2026-06-24T10:01:00Z"],
                    utc=True,
                ),
                "Bar_Start": pd.to_datetime(
                    ["2026-06-24T09:59:00Z", "2026-06-24T10:00:00Z"],
                    utc=True,
                ),
                "Open": [1.0, 1.0],
                "High": [1.1, 1.1],
                "Low": [0.9, 0.9],
                "Close": [1.0, 1.0],
            }
        )
        full_policy = {
            "event_execution_mode": (
                native_one_row_contract.FULL_AUTHORITY_EVENT_MODE
            ),
            "native_event_eligible": False,
            "full_authority_required": True,
            "force_full_event_replay": True,
        }
        with (
            mock.patch.object(
                live_stateful_runtime,
                "feature_certified",
                return_value=False,
            ),
            mock.patch.object(
                live_stateful_runtime,
                "event_certification",
                return_value={},
            ),
            mock.patch.object(
                live_stateful_runtime,
                "event_execution_classification",
                return_value=full_policy,
            ),
        ):
            live_stateful_runtime.install_stateful_runtime_api(runtime)
            result = runtime.generate_candidate_events_live(features=features)

        self.assertFalse(runtime.NATIVE_ONE_ROW_EVENT_CERTIFIED)
        self.assertTrue(runtime.EVENT_FULL_AUTHORITY_REQUIRED)
        self.assertEqual(
            runtime.EVENT_CONTINUATION_CLASSIFICATION,
            native_one_row_contract.FULL_AUTHORITY_EVENT_MODE,
        )
        metadata = result["metadata"]
        self.assertEqual(
            metadata["continuation_mode"],
            "direct_full_authority_events",
        )
        self.assertTrue(metadata["full_authority_required"])
        self.assertFalse(metadata["silent_suffix_fallback_used"])
        self.assertFalse(runtime.NATIVE_ONE_ROW_EVENT_CERTIFIED)
        self.assertTrue(runtime.EVENT_FULL_AUTHORITY_REQUIRED)
        self.assertEqual(
            runtime.EVENT_CONTINUATION_CLASSIFICATION,
            native_one_row_contract.FULL_AUTHORITY_EVENT_MODE,
        )



if __name__ == "__main__":
    unittest.main()
