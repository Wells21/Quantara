from __future__ import annotations

import pickle
import unittest
from types import ModuleType
from unittest import mock

import numpy as np
import pandas as pd

from shared.edges import live_stateful_runtime as runtime_adapter


def bars(count: int) -> pd.DataFrame:
    close = np.arange(1, count + 1, dtype="float64")
    dt = pd.date_range("2026-01-01", periods=count, freq="min", tz="UTC")
    return pd.DataFrame(
        {
            "Datetime": dt,
            "Bar_Start": dt - pd.Timedelta(minutes=1),
            "Open": close - 0.25,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Ticks": np.arange(count) + 100,
            "Threshold": np.full(count, 1000.0),
        }
    )


def parity_flags(asset: str) -> dict:
    return {
        "ffd": {"live_continuation_applied": True},
        "hmm": {"live_continuation_applied": True},
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": True,
        "deterministic_noise_contract_preserved": True,
    }


class VerifiedSuffixContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_adapter._PROCESS_BOOTSTRAPPED_FEATURE_EDGES.clear()

    def _rolling_runtime(self) -> ModuleType:
        runtime = ModuleType("rolling_runtime")
        runtime.ASSET = "XAUUSD"
        runtime.EDGE_ID = "XAUUSD/test_long"
        runtime.FAMILY = "test"
        runtime.SIDE_VALUE = 1
        runtime.LIVE_INCREMENTAL_REPLAY_PROFILE = {
            "feature_rows": [30, 50],
            "event_rows": [30, 50],
            "event_boundary_guard_rows": 8,
            "decision_history_rows": 40,
            "minimum_feature_overlap_rows": 12,
            "minimum_event_decision_overlap_rows": 12,
        }
        runtime.feature_input_lengths = []
        runtime.event_input_lengths = []

        def build_features(bars: pd.DataFrame, drop_warmup_rows: bool = False):
            runtime.feature_input_lengths.append(len(bars))
            out = bars.copy()
            out["edge_id"] = runtime.EDGE_ID
            out["side"] = runtime.SIDE_VALUE
            out["Rolling_5"] = out["Close"].rolling(5).mean()
            out["Delta_3"] = out["Close"] - out["Close"].shift(3)
            if drop_warmup_rows:
                out = out.dropna(subset=["Rolling_5", "Delta_3"])
            return out.reset_index(drop=True)

        def generate_candidate_events(features: pd.DataFrame):
            runtime.event_input_lengths.append(len(features))
            work = features.copy()
            mask = work["Close"].mod(10).eq(0)
            events = work.loc[mask, ["Datetime", "Bar_Start", "Close"]].copy()
            events["edge_id"] = runtime.EDGE_ID
            events["side"] = runtime.SIDE_VALUE
            events["event_id"] = events["Datetime"].map(
                lambda ts: f"{runtime.EDGE_ID}|{pd.Timestamp(ts).isoformat()}"
            )
            events["candidate_event_generator"] = "test"
            return events.reset_index(drop=True)

        runtime.build_features = build_features
        runtime.generate_candidate_events = generate_candidate_events
        return runtime

    def test_feature_bootstrap_then_verified_suffix(self) -> None:
        runtime = self._rolling_runtime()
        with (
            mock.patch.object(runtime_adapter, "_shared_asset_parity", side_effect=parity_flags),
            mock.patch.object(runtime_adapter, "_load_live_settings_module", return_value=None),
        ):
            first = runtime_adapter.build_features_live_incremental_for_runtime(
                runtime,
                asset=runtime.ASSET,
                edge_id=runtime.EDGE_ID,
                bars=bars(100),
                state={},
            )
            second = runtime_adapter.build_features_live_incremental_for_runtime(
                runtime,
                asset=runtime.ASSET,
                edge_id=runtime.EDGE_ID,
                bars=bars(101),
                state=pickle.loads(pickle.dumps(first["state"])),
            )
        self.assertEqual(runtime.feature_input_lengths[0], 100)
        self.assertLess(runtime.feature_input_lengths[-1], 101)
        self.assertEqual(
            second["metadata"]["continuation_mode"],
            "verified_authoritative_suffix_replay",
        )
        self.assertEqual(pd.Timestamp(second["features"]["Datetime"].iloc[-1]), bars(101)["Datetime"].iloc[-1])

    def test_path_dependent_feature_falls_back_to_full(self) -> None:
        runtime = self._rolling_runtime()

        def cumulative_features(bars: pd.DataFrame, drop_warmup_rows: bool = False):
            runtime.feature_input_lengths.append(len(bars))
            out = bars.copy()
            out["edge_id"] = runtime.EDGE_ID
            out["side"] = 1
            out["Cumulative"] = out["Close"].cumsum()
            return out

        runtime.build_features = cumulative_features
        with (
            mock.patch.object(runtime_adapter, "_shared_asset_parity", side_effect=parity_flags),
            mock.patch.object(runtime_adapter, "_load_live_settings_module", return_value=None),
        ):
            first = runtime_adapter.build_features_live_incremental_for_runtime(
                runtime, asset=runtime.ASSET, edge_id=runtime.EDGE_ID, bars=bars(100), state={}
            )
            second = runtime_adapter.build_features_live_incremental_for_runtime(
                runtime,
                asset=runtime.ASSET,
                edge_id=runtime.EDGE_ID,
                bars=bars(101),
                state=first["state"],
            )
        self.assertEqual(runtime.feature_input_lengths[-1], 101)
        self.assertEqual(
            second["metadata"]["continuation_mode"],
            "authoritative_full_bootstrap_recovery_or_audit",
        )
        self.assertTrue(second["metadata"]["replay_failures"])

    def test_event_bootstrap_then_verified_suffix(self) -> None:
        runtime = self._rolling_runtime()
        feature_100 = runtime.build_features(bars(100))
        feature_101 = runtime.build_features(bars(101))
        with mock.patch.object(runtime_adapter, "_load_live_settings_module", return_value=None):
            first = runtime_adapter.generate_candidate_events_live_for_runtime(
                runtime,
                asset=runtime.ASSET,
                edge_id=runtime.EDGE_ID,
                features=feature_100,
                candidate_times=[feature_100["Datetime"].iloc[-1]],
                state={},
            )
            second = runtime_adapter.generate_candidate_events_live_for_runtime(
                runtime,
                asset=runtime.ASSET,
                edge_id=runtime.EDGE_ID,
                features=feature_101,
                candidate_times=[feature_101["Datetime"].iloc[-1]],
                state=pickle.loads(pickle.dumps(first["state"])),
            )
        self.assertEqual(runtime.event_input_lengths[0], 100)
        self.assertLess(runtime.event_input_lengths[-1], 101)
        self.assertEqual(
            second["metadata"]["continuation_mode"],
            "verified_authoritative_event_suffix_replay",
        )
        self.assertTrue(second["metadata"]["decision_parity"]["ok"])
        self.assertEqual(len(second["events"]), 0)

    def test_raw_history_revision_forces_full_build(self) -> None:
        runtime = self._rolling_runtime()
        with (
            mock.patch.object(runtime_adapter, "_shared_asset_parity", side_effect=parity_flags),
            mock.patch.object(runtime_adapter, "_load_live_settings_module", return_value=None),
        ):
            first = runtime_adapter.build_features_live_incremental_for_runtime(
                runtime, asset=runtime.ASSET, edge_id=runtime.EDGE_ID, bars=bars(100), state={}
            )
            changed = bars(101)
            changed.loc[changed.index[-5], "Close"] += 2.0
            with self.assertRaises(runtime_adapter.StatefulRuntimeContractError):
                runtime_adapter.build_features_live_incremental_for_runtime(
                    runtime,
                    asset=runtime.ASSET,
                    edge_id=runtime.EDGE_ID,
                    bars=changed,
                    state=first["state"],
                )


if __name__ == "__main__":
    unittest.main()
