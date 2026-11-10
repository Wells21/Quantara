from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from shared.live_streaming import continuation, state


class PhaseBDependencyIOTests(unittest.TestCase):
    def setUp(self) -> None:
        state.FRAME_CACHE.clear()
        state.HMM_INPUT_RING.clear()
        state.FFD_DELTA.clear()

    def tearDown(self) -> None:
        state.FRAME_CACHE.clear()
        state.HMM_INPUT_RING.clear()
        state.FFD_DELTA.clear()

    def test_normal_hmm_path_does_not_read_full_ffd_csv(self) -> None:
        source = inspect.getsource(
            continuation.verify_asset_hmm_regime_manifest_hot
        )
        self.assertNotIn("_read_ffd_runtime_rows(", source)
        self.assertNotIn("pd.read_csv(", source)
        self.assertIn("_refresh_hmm_input_ring_from_ffd_delta", source)
        self.assertIn("_pending_hmm_eligible_rows", source)
        self.assertIn("allow_disk=False", source)

    def test_normal_snapshot_refresh_does_not_read_dependency_csv(self) -> None:
        source = inspect.getsource(
            continuation._refresh_verified_feature_dependencies_hot_locked
        )
        self.assertNotIn("_read_ffd_runtime_rows(", source)
        self.assertNotIn("pd.read_csv(", source)
        self.assertNotIn("frame_load(", source)
        self.assertIn("frame_cached(", source)
        self.assertIn("committed_last_timestamp", source)

    def test_frame_cached_has_no_disk_fallback_when_warm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ffd.csv"
            columns = [
                "Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"
            ]
            pd.DataFrame(
                {
                    "Datetime": ["2026-08-15T08:00:00Z"],
                    "Open_FFD": [1.0],
                    "High_FFD": [2.0],
                    "Low_FFD": [0.5],
                    "Close_FFD": [1.5],
                }
            ).to_csv(path, index=False)

            # Startup/recovery full read is explicit.
            state.frame_load(
                "ffd",
                "BTCUSD",
                path,
                columns,
                max_rows=6002,
            )

            with mock.patch.object(
                pd,
                "read_csv",
                side_effect=AssertionError(
                    "normal dependency cache reopened historical CSV"
                ),
            ):
                frame = state.frame_cached(
                    "ffd", "BTCUSD", path, columns
                )

            self.assertEqual(len(frame), 1)

    def test_frame_cached_fails_closed_when_cold_without_disk_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hmm.csv"
            pd.DataFrame(
                {
                    "Datetime": ["2026-08-15T08:00:00Z"],
                    "Regime_State": [1.0],
                }
            ).to_csv(path, index=False)
            with mock.patch.object(
                pd,
                "read_csv",
                side_effect=AssertionError(
                    "cold normal dependency cache must not read disk"
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "startup/recovery hydration is required",
                ):
                    state.frame_cached(
                        "hmm",
                        "BTCUSD",
                        path,
                        ["Datetime", "Regime_State"],
                    )

    def test_hmm_eligibility_ring_extends_without_disk(self) -> None:
        first = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    ["2026-08-15T08:00:00Z"], utc=True
                ),
                "Close_FFD": [0.1],
                "Bar_Volatility_Bps": [10.0],
                "Abs_Bar_Return_Bps": [2.0],
                "Body_To_Range": [0.5],
                "Close_Location": [0.7],
            }
        )
        second = first.copy()
        second["Datetime"] = pd.to_datetime(
            ["2026-08-15T08:01:00Z"], utc=True
        )
        state.hmm_input_ring_set("BTCUSD", first, capacity=6000)

        with mock.patch.object(
            pd,
            "read_csv",
            side_effect=AssertionError("ring extension must not read disk"),
        ):
            state.hmm_input_ring_extend("BTCUSD", second)
            ring = state.hmm_input_ring_get("BTCUSD")

        self.assertIsNotNone(ring)
        self.assertEqual(len(ring), 2)
        self.assertEqual(
            ring["Datetime"].iloc[-1],
            pd.Timestamp("2026-08-15T08:01:00Z"),
        )

    def test_hmm_preprocessing_is_row_local_for_delta_ring_safety(self) -> None:
        from shared.regime import hmm_regime

        source = inspect.getsource(hmm_regime.engineer_hmm_inputs)
        self.assertNotIn(".rolling(", source)
        self.assertNotIn(".shift(", source)
        self.assertNotIn(".diff(", source)
        self.assertNotIn(".pct_change(", source)
        for token in (
            "Bar_Volatility_Bps",
            "Abs_Bar_Return_Bps",
            "Body_To_Range",
            "Close_Location",
        ):
            self.assertIn(token, source)

    def test_full_ffd_reader_exists_only_for_startup_recovery_seed(self) -> None:
        source = Path(continuation.__file__).read_text(encoding="utf-8")
        call = "_read_ffd_runtime_rows("
        # One definition + one explicit startup/recovery ring seeder call.
        self.assertEqual(source.count(call), 2)
        seed = inspect.getsource(
            continuation._seed_hmm_input_ring_from_authority
        )
        self.assertIn(call, seed)

    def test_startup_hydration_precedes_snapshot_worker_start(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pipeline_path = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        )
        pipeline = pipeline_path.read_text(encoding="utf-8")
        self.assertIn(
            "PHASE_B_STARTUP_DEPENDENCY_CACHE_HYDRATION_V1",
            pipeline,
        )
        start = pipeline.split(
            "    def start_snapshot_manager(self: Any) -> None:",
            1,
        )[1].split(
            "    def stop_snapshot_manager",
            1,
        )[0]
        hydration_pos = start.index(
            "hydrate_verified_feature_dependencies_from_authority"
        )
        worker_start_pos = start.index("thread.start()")
        self.assertLess(hydration_pos, worker_start_pos)

    def test_normal_snapshot_path_never_invokes_full_recovery(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pipeline = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")
        normal = pipeline.split(
            "    def _process_snapshot(self: Any, job: dict[str, Any]) -> None:",
            1,
        )[1].split("    def _snapshot_worker", 1)[0]
        recovery = pipeline.split(
            "    def _recovery_loop(self: Any) -> None:",
            1,
        )[1].split("    def start_snapshot_manager", 1)[0]
        self.assertNotIn(
            'ns["ensure_assets_live_feature_continuation"](',
            normal,
        )
        self.assertIn(
            'ns["ensure_assets_live_feature_continuation"](',
            recovery,
        )


    def test_normal_bar_does_not_read_full_ffd_csv(self) -> None:
        normal_hmm = inspect.getsource(
            continuation.verify_asset_hmm_regime_manifest_hot
        )
        normal_refresh = inspect.getsource(
            continuation._refresh_verified_feature_dependencies_hot_locked
        )
        self.assertNotIn("_read_ffd_runtime_rows(", normal_hmm)
        self.assertNotIn("_read_ffd_runtime_rows(", normal_refresh)
        self.assertNotIn("pd.read_csv(", normal_hmm)
        self.assertNotIn("pd.read_csv(", normal_refresh)

    def test_normal_bar_does_not_read_full_hmm_csv(self) -> None:
        normal_refresh = inspect.getsource(
            continuation._refresh_verified_feature_dependencies_hot_locked
        )
        self.assertNotIn("frame_load(", normal_refresh)
        self.assertNotIn("pd.read_csv(", normal_refresh)
        self.assertIn('frame_cached("hmm"', normal_refresh)

    def test_normal_bar_does_not_run_full_dependency_recovery(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pipeline = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")
        normal = pipeline.split(
            "    def _process_snapshot(self: Any, job: dict[str, Any]) -> None:",
            1,
        )[1].split("    def _snapshot_worker", 1)[0]
        self.assertNotIn(
            'ns["ensure_assets_live_feature_continuation"](',
            normal,
        )

    def test_dependency_capacity_preserves_frozen_6000_row_contract(self) -> None:
        class Config:
            train_window = 5000
            refit_frequency = 1000
            inference_lookback = 1000

        capacity = continuation._dependency_frame_capacity(
            {"config": Config()}
        )
        self.assertEqual(capacity, 6002)
        self.assertGreaterEqual(capacity, 6000)

    def test_startup_authority_load_retains_only_bounded_live_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large_ffd.csv"
            rows = 25
            frame = pd.DataFrame(
                {
                    "Datetime": pd.date_range(
                        "2026-08-15T00:00:00Z",
                        periods=rows,
                        freq="min",
                    ),
                    "Open_FFD": list(range(rows)),
                }
            )
            frame.to_csv(path, index=False)

            loaded = state.frame_load(
                "ffd",
                "BTCUSD",
                path,
                ["Datetime", "Open_FFD"],
                max_rows=7,
            )

            self.assertEqual(len(loaded), 7)
            record = state.FRAME_CACHE[("ffd", "BTCUSD")]
            self.assertEqual(record["max_rows"], 7)
            self.assertEqual(len(record["frame"]), 7)
            self.assertEqual(
                loaded["Datetime"].iloc[0],
                frame["Datetime"].iloc[-7],
            )
            self.assertEqual(len(pd.read_csv(path)), rows)

    def test_normal_frame_extension_remains_bounded_as_history_grows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bounded_hmm.csv"
            initial = pd.DataFrame(
                {
                    "Datetime": pd.date_range(
                        "2026-08-15T00:00:00Z",
                        periods=8,
                        freq="min",
                    ),
                    "Regime_State": list(range(8)),
                }
            )
            initial.to_csv(path, index=False)
            state.frame_load(
                "hmm",
                "BTCUSD",
                path,
                ["Datetime", "Regime_State"],
                max_rows=4,
            )

            for minute in range(8, 18):
                row = pd.DataFrame(
                    {
                        "Datetime": [
                            pd.Timestamp("2026-08-15T00:00:00Z")
                            + pd.Timedelta(minutes=minute)
                        ],
                        "Regime_State": [minute],
                    }
                )
                state.append_csv_rows(
                    path,
                    row,
                    ["Datetime", "Regime_State"],
                )
                with mock.patch.object(
                    pd,
                    "read_csv",
                    side_effect=AssertionError(
                        "normal bounded frame extension reopened history"
                    ),
                ):
                    state.frame_extend("hmm", "BTCUSD", path, row)
                    cached = state.frame_cached(
                        "hmm",
                        "BTCUSD",
                        path,
                        ["Datetime", "Regime_State"],
                    )
                self.assertEqual(len(cached), 4)
                self.assertEqual(
                    int(cached["Regime_State"].iloc[-1]),
                    minute,
                )

            status = state.frame_cache_status()["hmm:BTCUSD"]
            self.assertEqual(status["rows"], 4)
            self.assertEqual(status["max_rows"], 4)

    def test_normal_cache_rejects_unbounded_startup_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unbounded_ffd.csv"
            pd.DataFrame(
                {
                    "Datetime": ["2026-08-15T08:00:00Z"],
                    "Open_FFD": [1.0],
                }
            ).to_csv(path, index=False)
            state.frame_load(
                "ffd",
                "BTCUSD",
                path,
                ["Datetime", "Open_FFD"],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "frame_cache_unbounded",
            ):
                state.frame_cached(
                    "ffd",
                    "BTCUSD",
                    path,
                    ["Datetime", "Open_FFD"],
                )

    def test_recovery_hydration_seeds_bounded_ffd_and_hmm_frames(self) -> None:
        source = inspect.getsource(
            continuation.hydrate_verified_feature_dependencies_from_authority
        )
        self.assertIn(
            "frame_capacity = _dependency_frame_capacity(context)",
            source,
        )
        self.assertGreaterEqual(
            source.count("max_rows=frame_capacity"),
            2,
        )
        self.assertIn('"dependency_frame_capacity"', source)

if __name__ == "__main__":
    unittest.main()
