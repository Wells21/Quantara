from __future__ import annotations

import inspect
import math
import pickle
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from shared.edges import native_one_row_feature_engine as legacy
from shared.edges import phase_c_incremental_feature_engine as phase_c2
from shared.edges.phase_c_incremental_feature_engine import (
    ENGINE_VERSION,
    REGIME_COLUMNS,
    PhaseCIncrementalFeatureError,
    bootstrap_compact_state,
    deserialize_compact_state,
    serialize_compact_state,
    state_observability,
    update_one,
    validate_dependency_generation,
    _bootstrap_stc_authority,
    _update_stc_authority,
    _bootstrap_ffd_z_authority,
    _update_ffd_z_authority,
    _shifted_zscore_latest_same_pandas,
)


def _contract() -> dict:
    return {
        "status": "PASS",
        "runtime_routing_enabled": False,
        "contract_sha256": "c1-test",
        "selected_assets": ["AAA"],
        "assets": {
            "AAA": {
                "declared_feature_name_count": 1,
                "topological_feature_order": ["Feature_A"],
                "addon_candidate_features": [],
            }
        },
    }


def _state() -> dict:
    raw = pd.DataFrame({
        "Datetime": pd.to_datetime(["2026-01-01T00:00:00Z"]),
        "Bar_Start": pd.to_datetime(["2025-12-31T23:59:00Z"]),
        "Open": [1.0], "High": [1.1], "Low": [0.9], "Close": [1.0],
        "Ticks": [10.0], "Threshold": [10.0],
    })
    features = raw.copy()
    features["Feature_A"] = 1.0
    return {
        "schema_version": phase_c2.STATE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "asset": "AAA",
        "phase_c1_contract_sha256": "c1-test",
        "math_kernel_version": legacy.ENGINE_VERSION,
        "last_timestamp": "2026-01-01T00:00:00+00:00",
        "processed_rows": 6000,
        "raw_context_rows": 260,
        "feature_context_rows": 512,
        "raw_tail": raw,
        "feature_tail": features,
        "recursive": {
            "opaque": True,
            "phase_c2_stc_authority": {
                "mode": "bounded_same_library_rolling_authority",
                "authority_window_rows": 64,
                "close_ring": [1.0] * 64,
                "last_value": 0.0,
            },
            "phase_c2_ffd_z_authority": {
                "mode": "bounded_same_pandas_rolling_shift_authority",
                "authority_window_rows": 64,
                "close_ffd_ring": [1.0] * 64,
                "last_values": {
                    "Close_FFD_Z_20": 0.0,
                    "Close_FFD_Z_50": 0.0,
                },
            },
        },
        "required_features": ["Feature_A"],
        "addon_features": [],
        "last_dependency_generation_id": None,
        "last_hmm_target": None,
        "updates": 0,
    }


def _bar() -> dict:
    return {
        "Datetime": pd.Timestamp("2026-01-01T00:01:00Z"),
        "Bar_Start": pd.Timestamp("2026-01-01T00:00:00Z"),
        "Open": 1.0, "High": 1.2, "Low": 0.95, "Close": 1.1,
        "Ticks": 11.0, "Threshold": 10.0,
    }


def _ffd() -> dict:
    return {"Open_FFD": 1.0, "High_FFD": 1.0, "Low_FFD": 1.0, "Close_FFD": 1.0}


def _hmm() -> dict:
    return {
        "Regime_State": 1.0,
        "Regime_Entropy": 0.2,
        "Regime_Duration": 3.0,
        "Prob_State_0": 0.1,
        "Prob_State_1": 0.6,
        "Prob_State_2": 0.2,
        "Prob_State_3": 0.1,
    }


def _generation(*, eligible: bool = True) -> dict:
    return {
        "status": "READY",
        "asset": "AAA",
        "generation_id": "gen-1",
        "raw_target": "2026-01-01T00:01:00Z",
        "hmm_target": "2026-01-01T00:01:00Z" if eligible else "2026-01-01T00:00:00Z",
        "target_hmm_eligible": eligible,
    }


class PhaseCIncrementalFeatureEngineTests(unittest.TestCase):
    def test_generation_requires_exact_raw_target(self) -> None:
        generation = _generation()
        generation["raw_target"] = "2026-01-01T00:02:00Z"
        with self.assertRaises(PhaseCIncrementalFeatureError):
            validate_dependency_generation(
                asset="AAA", bar_timestamp=_bar()["Datetime"], generation=generation,
                ffd_values=_ffd(), hmm_values=_hmm(),
            )

    def test_hmm_eligible_requires_finite_probabilities(self) -> None:
        hmm = _hmm(); hmm["Prob_State_3"] = math.nan
        with self.assertRaises(PhaseCIncrementalFeatureError):
            validate_dependency_generation(
                asset="AAA", bar_timestamp=_bar()["Datetime"], generation=_generation(),
                ffd_values=_ffd(), hmm_values=hmm,
            )

    def test_hmm_ineligible_preserves_nan_left_join_semantics(self) -> None:
        _gid, _ffd_values, hmm, eligible, target = validate_dependency_generation(
            asset="AAA", bar_timestamp=_bar()["Datetime"], generation=_generation(eligible=False),
            ffd_values=_ffd(), hmm_values=None,
        )
        self.assertFalse(eligible)
        self.assertTrue(all(np.isnan(hmm[name]) for name in REGIME_COLUMNS))
        self.assertEqual(target, pd.Timestamp("2026-01-01T00:00:00Z"))

    def test_update_one_never_owns_or_refreshes_dependencies(self) -> None:
        captured = {}
        def fake_compute_one(**kwargs):
            captured.update(kwargs)
            return {
                "Datetime": kwargs["bar"]["Datetime"],
                "Bar_Start": kwargs["bar"]["Bar_Start"],
                "Open": kwargs["bar"]["Open"], "High": kwargs["bar"]["High"],
                "Low": kwargs["bar"]["Low"], "Close": kwargs["bar"]["Close"],
                "Ticks": kwargs["bar"]["Ticks"], "Threshold": kwargs["bar"]["Threshold"],
                "Feature_A": 2.0,
            }, {"next": True}
        with patch.object(legacy, "_compute_one", side_effect=fake_compute_one), \
             patch.object(legacy, "_lookup_external", side_effect=AssertionError("must not be called")), \
             patch.object(phase_c2, "_update_stc_authority", return_value=(0.0, _state()["recursive"]["phase_c2_stc_authority"])), \
             patch.object(phase_c2, "_update_ffd_z_authority", return_value=(
                 {"Close_FFD_Z_20": 0.0, "Close_FFD_Z_50": 0.0},
                 _state()["recursive"]["phase_c2_ffd_z_authority"],
             )):
            row, state, meta = update_one(
                state=_state(), bar=_bar(), dependency_generation=_generation(),
                ffd_values=_ffd(), hmm_values=_hmm(), phase_c1_contract=_contract(),
            )
        self.assertEqual(row["Feature_A"], 2.0)
        self.assertEqual(captured["ffd_values"]["Close_FFD"], 1.0)
        self.assertEqual(captured["hmm_values"]["Regime_State"], 1.0)
        self.assertEqual(meta["dependency_generation_id"], "gen-1")
        self.assertFalse(meta["runtime_routing_enabled"])
        self.assertNotIn("feature_history", state)

    def test_update_one_ineligible_hmm_passes_nan_regime_to_math(self) -> None:
        captured = {}
        def fake_compute_one(**kwargs):
            captured.update(kwargs)
            return {
                "Datetime": kwargs["bar"]["Datetime"], "Bar_Start": kwargs["bar"]["Bar_Start"],
                "Open": 1.0, "High": 1.2, "Low": 0.95, "Close": 1.1,
                "Ticks": 11.0, "Threshold": 10.0, "Feature_A": 2.0,
            }, {"next": True}
        with patch.object(legacy, "_compute_one", side_effect=fake_compute_one), \
             patch.object(phase_c2, "_update_stc_authority", return_value=(0.0, _state()["recursive"]["phase_c2_stc_authority"])), \
             patch.object(phase_c2, "_update_ffd_z_authority", return_value=(
                 {"Close_FFD_Z_20": 0.0, "Close_FFD_Z_50": 0.0},
                 _state()["recursive"]["phase_c2_ffd_z_authority"],
             )):
            update_one(
                state=_state(), bar=_bar(), dependency_generation=_generation(eligible=False),
                ffd_values=_ffd(), hmm_values=None, phase_c1_contract=_contract(),
            )
        self.assertTrue(all(np.isnan(captured["hmm_values"][name]) for name in REGIME_COLUMNS))

    def test_compact_state_roundtrip_has_no_unbounded_feature_history(self) -> None:
        state = _state()
        payload = serialize_compact_state(state, phase_c1_contract=_contract())
        restored = deserialize_compact_state(payload, phase_c1_contract=_contract())
        self.assertEqual(state_observability(state), state_observability(restored))
        self.assertFalse(state_observability(restored)["has_unbounded_feature_history"])

    def test_contract_fingerprint_change_invalidates_state(self) -> None:
        changed = _contract(); changed["contract_sha256"] = "new-c1"
        with self.assertRaises(PhaseCIncrementalFeatureError):
            serialize_compact_state(_state(), phase_c1_contract=changed)

    def test_normal_update_source_has_no_dependency_read_or_refresh(self) -> None:
        source = inspect.getsource(update_one)
        self.assertNotIn("refresh_verified_feature_dependencies_hot", source)
        self.assertNotIn("load_verified_ffd", source)
        self.assertNotIn("load_verified_hmm", source)
        self.assertNotIn("read_csv", source)
        self.assertNotIn("build_features", source)

    def test_stc_bounded_authority_matches_same_library_exactly(self) -> None:
        rng = np.random.default_rng(20260815)
        close = 100.0 + np.cumsum(rng.normal(0.0, 0.35, size=700))
        window = 600
        state = _bootstrap_stc_authority(close[:window], authority_window_rows=window)
        for i in range(window, 700):
            observed, state = _update_stc_authority(state, float(close[i]))
            expected_frame = ta.stc(pd.Series(close[i - window + 1 : i + 1], dtype="float64"))
            self.assertIsNotNone(expected_frame)
            expected = float(expected_frame.iloc[-1, 0])
            self.assertEqual(observed, expected)
            self.assertLessEqual(len(state["close_ring"]), window)


    def test_ffd_z_bounded_authority_matches_same_pandas_exactly(self) -> None:
        rng = np.random.default_rng(20260816)
        # Large base values intentionally exercise the numerical path that caused
        # e-9 drift when np.mean/std replaced pandas rolling accumulators.
        values = 1.0e8 + np.cumsum(rng.normal(0.0, 7.0, size=760))
        window = 600
        state = _bootstrap_ffd_z_authority(
            values[:window], authority_window_rows=window
        )
        for i in range(window, 760):
            observed, state = _update_ffd_z_authority(state, float(values[i]))
            rolling = values[i - window + 1 : i + 1]
            for z_window in (20, 50):
                series = pd.Series(rolling, dtype="float64")
                expected_series = (
                    series - series.rolling(z_window).mean().shift(1)
                ) / (
                    series.rolling(z_window).std().shift(1) + legacy.EPS
                )
                expected = float(expected_series.iloc[-1])
                self.assertEqual(
                    observed[f"Close_FFD_Z_{z_window}"], expected
                )
            self.assertLessEqual(
                len(state["close_ffd_ring"]), window
            )

    def test_ffd_z_state_is_bounded_and_not_feature_history(self) -> None:
        values = np.linspace(1000.0, 1100.0, 650, dtype="float64")
        state = _bootstrap_ffd_z_authority(
            values, authority_window_rows=600
        )
        self.assertEqual(len(state["close_ffd_ring"]), 600)
        self.assertEqual(state["authority_window_rows"], 600)
        self.assertNotIn("feature_history", state)

    def test_stc_state_is_bounded_and_not_feature_history(self) -> None:
        rng = np.random.default_rng(11)
        close = 100.0 + np.cumsum(rng.normal(size=650))
        state = _bootstrap_stc_authority(close, authority_window_rows=600)
        self.assertEqual(len(state["close_ring"]), 600)
        self.assertEqual(state["authority_window_rows"], 600)
        self.assertNotIn("feature_history", state)

    def test_bootstrap_source_derives_addons_before_required_feature_gate(self) -> None:
        source = inspect.getsource(bootstrap_compact_state)
        derive = source.index("internal = authority.copy()")
        gate = source.index("internal_missing =")
        self.assertLess(derive, gate)
        self.assertNotIn("batch authority missing C.1 features", source)

    def test_certification_source_sandboxes_dependency_verifier_writes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "portfolio_live" / "phase_c_02_certify_incremental_features.py").read_text(encoding="utf-8")
        self.assertIn("phase_c2_dependency_sandbox", source)
        self.assertIn("production_continuation_mutated", source)
        self.assertIn("ffd_module.LIVE_CONTINUATION_DIR = sandbox", source)
        self.assertIn("hmm_module.LIVE_CONTINUATION_DIR = sandbox", source)
        self.assertIn("shutil.rmtree(sandbox", source)
        self.assertIn("HORIZON_SENSITIVE_CORE_FEATURES", source)
        self.assertIn("incremental_vs_rolling_authority", source)
        self.assertIn("ffd_z_rolling_6000_parity", source)

    def test_current_phase_b_hotpath_marker_remains_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        text = (root / "shared" / "live_streaming" / "state.py").read_text(encoding="utf-8")
        self.assertIn("v2_live_streaming_hotpath_v11_phase_b_bounded_dependency_rings", text)

    def test_current_c1_contract_still_has_routing_disabled_and_dynamic_assets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "portfolio_live" / "config" / "phase_c" / "feature_contract.json"
        if not path.exists():
            self.skipTest("Phase C.1 contract not present")
        import json
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(payload.get("status"), "PASS")
        self.assertIs(payload.get("runtime_routing_enabled"), False)
        self.assertEqual(payload.get("selected_asset_count"), len(payload.get("selected_assets") or []))
        self.assertEqual(payload.get("compiled_edge_count"), payload.get("selected_edge_count"))


if __name__ == "__main__":
    unittest.main()
