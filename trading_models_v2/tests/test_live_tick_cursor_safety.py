from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


ORCHESTRATOR = (
    Path(__file__).resolve().parents[1]
    / "portfolio_live"
    / "portfolio_live_dashboard_orchestrator.py"
)


def load_module():
    live_dir = ORCHESTRATOR.parent
    if str(live_dir) not in sys.path:
        sys.path.insert(0, str(live_dir))

    # TEST_IMPORT_ISOLATION_V2
    #
    # This helper must not leak its fake production modules into later test
    # modules. Phase A.2 imports the live hotpath binding at call time, so a
    # leaked fake shared.live_streaming.hotpath can silently replace the real
    # generation-binding function in unrelated tests.
    stubbed_module_names = (
        "live_settings",
        "common",
        "shared.edges.native_one_row_event_engine",
        "shared.live_streaming.hotpath",
        "production_readiness_p0",
        "production_snapshot_pipeline",
        "phase_c_04_production_router",
        "phase_d_03_production_router",
        "phase_d_04_provider_overlay",
    )
    saved_modules = {
        name: sys.modules.get(name)
        for name in stubbed_module_names
    }

    settings = types.ModuleType("live_settings")
    base = ORCHESTRATOR.parent
    settings.OUTPUT_DIR = base / "outputs"
    settings.LIVE_LOGS_DIR = base / "live_logs"
    settings.LIVE_STATE_DIR = base / "live_state"
    settings.LIVE_CONFIG_DIR = base / "config"
    settings.LIVE_BARS_DIR = base / "live_bars"
    settings.LIVE_MANIFEST_PATH = base / "live_manifest.json"
    # Match the real production contract: edges remain serial within each asset.
    settings.LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED = False
    settings.LIVE_DASHBOARD_EDGE_WORKERS_PER_ASSET = 1
    sys.modules["live_settings"] = settings

    common = types.ModuleType("common")
    common.broker_symbol_for_asset = lambda asset: {"NASDAQ": "US100"}.get(asset, asset)
    common.ensure_dirs = lambda: None
    common.import_edge_runtime = lambda edge: None
    common.init_mt5 = lambda: (None, None)
    common.load_live_manifest = lambda: {}
    common.read_json = lambda path: {}
    common.setup_logger = lambda *args, **kwargs: logging.getLogger("test")
    common.shutdown_mt5 = lambda mt5: None
    common.write_json = lambda *args, **kwargs: None
    sys.modules["common"] = common

    # Preserve the real shared package hierarchy so newly added production
    # submodules remain importable. Stub only the retired hot-path leaf modules
    # that this test intentionally isolates.
    _shared_pkg = importlib.import_module("shared")
    _edges_pkg = importlib.import_module("shared.edges")
    _live_streaming_pkg = importlib.import_module("shared.live_streaming")
    saved_edge_event_attr = getattr(
        _edges_pkg,
        "native_one_row_event_engine",
        None,
    )
    saved_live_hotpath_attr = getattr(
        _live_streaming_pkg,
        "hotpath",
        None,
    )

    event_engine = types.ModuleType("shared.edges.native_one_row_event_engine")
    event_engine.is_expected_empty_event_exception = lambda exc: False

    hotpath = types.ModuleType("shared.live_streaming.hotpath")
    hotpath.HOTPATH_VERSION = "test_hotpath"
    hotpath.cache_status = lambda: {}
    hotpath.install_hotpath_verifiers = lambda: {"installed": True}
    hotpath.patch_runtime_shared_loaders = lambda runtime: {}
    hotpath.verify_asset_ffd_manifest_hot = lambda asset: {}
    hotpath.verify_asset_hmm_regime_manifest_hot = lambda asset: {}

    sys.modules["shared.edges.native_one_row_event_engine"] = event_engine
    sys.modules["shared.live_streaming.hotpath"] = hotpath
    setattr(_edges_pkg, "native_one_row_event_engine", event_engine)
    setattr(_live_streaming_pkg, "hotpath", hotpath)

    # Test isolation: never execute production boot-time installers.
    # These installers mutate runtime globals and may prune real continuation
    # archives on import. Cursor/journal unit tests exercise the orchestrator
    # functions directly and must be hermetic.
    production_readiness_p0 = types.ModuleType("production_readiness_p0")
    production_readiness_p0.install_p0_production_hardening = (
        lambda namespace: {
            "installed": False,
            "test_stub": True,
            "reason": "unit_test_import_isolation",
        }
    )
    production_snapshot_pipeline = types.ModuleType("production_snapshot_pipeline")
    production_snapshot_pipeline.install_snapshot_pipeline = (
        lambda namespace: {
            "installed": False,
            "test_stub": True,
            "reason": "unit_test_import_isolation",
        }
    )
    sys.modules["production_readiness_p0"] = production_readiness_p0
    sys.modules["production_snapshot_pipeline"] = production_snapshot_pipeline

    # TEST_IMPORT_ISOLATION_PHASE_ROUTER_STUBS_V1
    # Cursor/journal tests import the dashboard only to exercise its functions.
    # Activated production overlays must not execute during that import.
    phase_c4 = types.ModuleType("phase_c_04_production_router")
    phase_c4.install_phase_c4_production_router = (
        lambda namespace: {
            "installed": False,
            "activated": False,
            "test_stub": True,
            "reason": "unit_test_import_isolation",
        }
    )

    phase_d3 = types.ModuleType("phase_d_03_production_router")
    phase_d3.install_phase_d3_production_router = (
        lambda namespace: {
            "installed": False,
            "activated": False,
            "test_stub": True,
            "reason": "unit_test_import_isolation",
        }
    )

    phase_d4 = types.ModuleType("phase_d_04_provider_overlay")
    phase_d4.install_phase_d4_provider_overlay = (
        lambda namespace: {
            "installed": False,
            "activated": False,
            "test_stub": True,
            "reason": "unit_test_import_isolation",
        }
    )

    sys.modules["phase_c_04_production_router"] = phase_c4
    sys.modules["phase_d_03_production_router"] = phase_d3
    sys.modules["phase_d_04_provider_overlay"] = phase_d4

    spec = importlib.util.spec_from_file_location(
        "cursor_safe_orchestrator",
        ORCHESTRATOR,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    saved_cursor_module = sys.modules.get(spec.name)
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore every global module slot this helper replaced. The imported
        # orchestrator keeps direct references to its test doubles in its own
        # globals, so restoring sys.modules does not change the test subject.
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

        if saved_cursor_module is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = saved_cursor_module

        if saved_edge_event_attr is None:
            try:
                delattr(_edges_pkg, "native_one_row_event_engine")
            except AttributeError:
                pass
        else:
            setattr(
                _edges_pkg,
                "native_one_row_event_engine",
                saved_edge_event_attr,
            )

        if saved_live_hotpath_attr is None:
            try:
                delattr(_live_streaming_pkg, "hotpath")
            except AttributeError:
                pass
        else:
            setattr(
                _live_streaming_pkg,
                "hotpath",
                saved_live_hotpath_attr,
            )


class FakeLogger:
    def __init__(self):
        self.messages = []

    def _add(self, level, message, *args):
        self.messages.append((level, message % args if args else message))

    def info(self, message, *args):
        self._add("info", message, *args)

    def warning(self, message, *args):
        self._add("warning", message, *args)

    def error(self, message, *args):
        self._add("error", message, *args)


class CursorSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def ticks(self, rows):
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["broker_timestamp"] = pd.to_datetime(frame["broker_timestamp"], utc=True)
        return frame

    def test_cursor_never_moves_backward(self):
        state = {}
        logger = FakeLogger()
        first = self.ticks(
            [
                {
                    "time_msc": 2000,
                    "timestamp": "2026-06-29T14:00:02Z",
                    "broker_timestamp": "2026-06-29T17:00:02Z",
                    "bid": 1.0,
                    "ask": 1.1,
                    "last": 0.0,
                    "volume": 0.0,
                    "flags": 1,
                    "volume_real": 0.0,
                }
            ]
        )
        older = first.copy()
        older["time_msc"] = 1000
        older["timestamp"] = pd.Timestamp("2026-06-29T14:00:01Z")
        older["broker_timestamp"] = pd.Timestamp("2026-06-29T17:00:01Z")
        self.assertTrue(self.m.update_last_tick("US100", first, state, logger=logger))
        self.assertFalse(self.m.update_last_tick("US100", older, state, logger=logger))
        self.assertEqual(state["last_tick_cursor_by_symbol"]["US100"]["time_msc"], 2000)
        self.assertEqual(state["live_tick_cursor_rejections"][-1]["reason"], "raw_time_msc_regression")

    def test_startup_guard_drops_archived_ticks_and_keeps_new_ticks(self):
        cutoff = pd.Timestamp("2026-06-29T14:00:00Z")
        state = {
            "live_tick_startup_guard_by_asset": {
                "NASDAQ": {
                    "cutoff_timestamp_utc": cutoff.isoformat(),
                    "cutoff_time_msc": 2000,
                }
            },
            "last_tick_cursor_by_symbol": {"US100": {"time_msc": 1000}},
        }
        frame = self.ticks(
            [
                {
                    "time_msc": 1500,
                    "timestamp": "2026-04-21T00:00:00Z",
                    "broker_timestamp": "2026-04-21T03:00:00Z",
                    "bid": 1.0,
                    "ask": 1.1,
                    "last": 0.0,
                    "volume": 0.0,
                    "flags": 1,
                    "volume_real": 0.0,
                },
                {
                    "time_msc": 3000,
                    "timestamp": "2026-06-29T14:00:01Z",
                    "broker_timestamp": "2026-06-29T17:00:01Z",
                    "bid": 2.0,
                    "ask": 2.1,
                    "last": 0.0,
                    "volume": 0.0,
                    "flags": 1,
                    "volume_real": 0.0,
                },
            ]
        )
        logger = FakeLogger()
        kept = self.m.apply_live_tick_startup_guard("NASDAQ", "US100", frame, state, logger)
        self.assertEqual(kept["time_msc"].tolist(), [3000])
        self.assertEqual(state["live_tick_guard_drop_count_by_asset"]["NASDAQ"], 1)
        self.assertGreaterEqual(state["last_tick_cursor_by_symbol"]["US100"]["time_msc"], 2000)

    def test_old_only_batch_cannot_form_executable_bar(self):
        state = {
            "live_tick_startup_guard_by_asset": {
                "NASDAQ": {
                    "cutoff_timestamp_utc": "2026-06-29T14:00:00+00:00",
                    "cutoff_time_msc": 2000,
                }
            },
            "last_tick_cursor_by_symbol": {"US100": {"time_msc": 2000}},
        }
        frame = self.ticks(
            [
                {
                    "time_msc": 1500,
                    "timestamp": "2026-04-21T00:00:00Z",
                    "broker_timestamp": "2026-04-21T03:00:00Z",
                    "broker_time_offset_seconds": 10800,
                    "bid": 1.0,
                    "ask": 1.1,
                    "last": 0.0,
                    "volume": 0.0,
                    "flags": 1,
                    "volume_real": 0.0,
                    "mid": 1.05,
                }
            ]
        )
        logger = FakeLogger()
        closed = self.m.process_ticks_to_bars(
            "NASDAQ",
            "US100",
            {"threshold_ticks": 10},
            frame,
            state,
            logger,
            gap_healed=False,
            executable=True,
        )
        self.assertEqual(closed, 0)
        self.assertNotIn("current_bars", state)

    def test_gap_healing_no_longer_advances_live_cursor(self):
        source = ORCHESTRATOR.read_text(encoding="utf-8")
        start = source.index("def heal_interval(")
        end = source.index("\ndef advance_edge_cursors_to_latest_live_bar", start)
        body = source[start:end]
        self.assertIn("record_gap_healing_last_tick", body)
        self.assertNotIn("update_last_tick(symbol, ticks", body)

    def test_prime_uses_post_gap_cutoff_not_old_historical_cursor(self):
        m = self.m
        fixed_now = pd.Timestamp("2026-06-29T14:00:00Z")
        snapshot_broker = pd.Timestamp("2026-06-29T17:00:01Z")

        class Tick:
            time_msc = int(snapshot_broker.value // 1_000_000)

        class MT5:
            @staticmethod
            def symbol_info_tick(symbol):
                return Tick()

        state = {
            "broker_time_offset_seconds_by_symbol": {"US100": 10800},
            "broker_time_offset_locked_by_symbol": {"US100": True},
        }
        logger = FakeLogger()
        with mock.patch.object(m, "utc_now_ts", return_value=fixed_now), mock.patch.object(
            m, "latest_live_bar_timestamp", return_value=pd.Timestamp("2026-06-29T10:36:09Z")
        ):
            m.prime_live_tick_cursors_after_gap_healing(
                MT5(),
                ["NASDAQ"],
                state,
                logger,
                pre_gap_snapshot={
                    "last_tick_cursor_by_symbol": {"US100": {"time_msc": 1}},
                    "last_tick_time_msc": {"US100": 1},
                    "last_normalized_tick_time_utc": {"US100": "2026-04-20T22:00:00Z"},
                },
            )
        guard = state["live_tick_startup_guard_by_asset"]["NASDAQ"]
        self.assertGreaterEqual(pd.Timestamp(guard["cutoff_timestamp_utc"]), fixed_now)
        self.assertGreaterEqual(guard["cutoff_time_msc"], Tick.time_msc)
        self.assertFalse(guard["historical_gap_healing_mutates_live_cursor"])


    def test_clock_transition_mixed_fresh_samples_are_blocked(self):
        m = self.m
        logger = FakeLogger()
        now = pd.Timestamp("2026-08-20T22:00:00Z")
        candidates = [
            m.infer_broker_clock_sample(
                asset="A%s" % i, symbol="A%s" % i,
                raw_timestamp=now + pd.Timedelta(hours=2), observed_at=now,
                max_offset_seconds=12 * 3600,
            ) for i in range(3)
        ]
        candidates.append(m.infer_broker_clock_sample(
            asset="OLD", symbol="OLD",
            raw_timestamp=now + pd.Timedelta(hours=3), observed_at=now,
            max_offset_seconds=12 * 3600,
        ))
        consensus = m.resolve_broker_clock_consensus(
            candidates, max_residual_seconds=5.0, previous_offset_seconds=10800,
        )
        state = {}
        with mock.patch.object(m, "utc_now_ts", return_value=now):
            with self.assertRaises(m.BrokerClockContractError):
                m._phase_d410_guard_account_clock_transition(
                    consensus=consensus, candidates=candidates,
                    previous_offset_seconds=10800, state=state, logger=logger,
                )
        self.assertEqual(state["broker_clock_transition_pending"]["status"], "MIXED_OLD_NEW_SAMPLES")
        self.assertTrue(state["broker_clock_transition_pending"]["publication_blocked"])

    def test_clock_transition_unanimous_multisymbol_samples_are_accepted(self):
        m = self.m
        logger = FakeLogger()
        now = pd.Timestamp("2026-08-20T22:05:00Z")
        candidates = [
            m.infer_broker_clock_sample(
                asset="A%s" % i, symbol="A%s" % i,
                raw_timestamp=now + pd.Timedelta(hours=2), observed_at=now,
                max_offset_seconds=12 * 3600,
            ) for i in range(3)
        ]
        consensus = m.resolve_broker_clock_consensus(
            candidates, max_residual_seconds=5.0, previous_offset_seconds=10800,
        )
        state = {}
        with mock.patch.object(m, "utc_now_ts", return_value=now):
            m._phase_d410_guard_account_clock_transition(
                consensus=consensus, candidates=candidates,
                previous_offset_seconds=10800, state=state, logger=logger,
            )
        self.assertEqual(state["broker_clock_transition_pending"]["status"], "APPROVED")
        self.assertFalse(state["broker_clock_transition_pending"]["publication_blocked"])

    def test_conflicting_fresh_live_snapshot_is_withheld(self):
        m = self.m
        logger = FakeLogger()
        now = pd.Timestamp("2026-08-20T22:00:00Z")
        class Tick:
            time_msc = int((now + pd.Timedelta(hours=2)).value // 1_000_000)
        class MT5:
            @staticmethod
            def symbol_info_tick(symbol):
                return Tick()
        state = {
            "account_broker_time_offset_seconds": 10800,
            "account_broker_time_offset_locked": True,
            "account_broker_time_offset_source": "test",
            "broker_clock_selected_assets": ["NASDAQ", "XAUUSD"],
        }
        with mock.patch.object(m, "utc_now_ts", return_value=now), mock.patch.object(
            m, "prime_broker_time_offsets", side_effect=m.BrokerClockContractError("mixed"),
        ):
            ok = m._phase_d410_live_snapshot_clock_compatible(
                mt5=MT5(), asset="NASDAQ", symbol="US100", state=state, logger=logger,
            )
        self.assertFalse(ok)
        self.assertEqual(state["broker_clock_live_snapshot_conflict_count"], 1)

    def test_accepted_clock_transition_resets_only_transient_state(self):
        m = self.m
        logger = FakeLogger()
        state = {
            "current_bars": {"NASDAQ": {"ticks": 7}},
            "last_tick_cursor_by_symbol": {"US100": {"time_msc": 123}},
            "last_tick_time_msc": {"US100": 123},
            "last_normalized_tick_time_utc": {"US100": "2026-08-20T22:00:00+00:00"},
            "live_tick_startup_guard_by_asset": {"NASDAQ": {"x": 1}},
            "asset_continuity": {"NASDAQ": {"status": "PASS"}},
            "last_processed_timestamp_by_edge": {"NASDAQ/trend_long": "2026-08-20T21:59:00+00:00"},
        }
        with mock.patch.object(m, "prime_live_tick_cursors_after_gap_healing", return_value=None):
            m._phase_d410_finalize_account_clock_transition(
                mt5=object(), assets=["NASDAQ"], state=state, logger=logger,
                previous_offset_seconds=10800, new_offset_seconds=7200,
                consensus={"status": "PASS", "accepted_samples": [], "excluded_samples": []},
            )
        self.assertNotIn("NASDAQ", state["current_bars"])
        self.assertNotIn("US100", state["last_tick_cursor_by_symbol"])
        self.assertNotIn("NASDAQ", state["live_tick_startup_guard_by_asset"])
        self.assertEqual(
            state["last_processed_timestamp_by_edge"]["NASDAQ/trend_long"],
            "2026-08-20T21:59:00+00:00",
        )
        self.assertFalse(state["broker_clock_transition_barrier"]["active"])


class RollingWindowLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    @staticmethod
    def frame(start: int, stop: int) -> pd.DataFrame:
        times = pd.date_range("2026-01-01", periods=stop, freq="min", tz="UTC")[start:stop]
        values = pd.Series(range(start, stop), dtype="float64")
        return pd.DataFrame({
            "Datetime": times,
            "Feature_A": values.to_numpy(),
            "Feature_B": (values * 2.0).to_numpy(),
        })

    def test_retired_stateful_feature_update_api_remains_disabled(self):
        # Current production authority is full/runtime-owned batch evaluation.
        # The retired mutable one-row/stateful merge path must not silently return.
        self.assertEqual(
            self.m.AUTHORITATIVE_RUNTIME_MODE,
            "authoritative_batch_shared_cache",
        )
        self.assertFalse(self.m.RUNTIME_STATEFUL_CONTINUATION_ENABLED)
        self.assertFalse(self.m.RUNTIME_STATE_PERSISTENCE_ENABLED)
        self.assertFalse(
            hasattr(self.m, "_merge_runtime_stateful_feature_update"),
            "retired stateful feature merge API must stay absent",
        )

    def test_incremental_and_native_event_approximations_remain_retired(self):
        self.assertFalse(self.m.INCREMENTAL_SHARED_FEATURES_ENABLED)
        self.assertFalse(self.m.LIVE_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED)
        self.assertFalse(self.m.LIVE_EVENT_ONLY_RUNTIME_API_ENABLED)
        self.assertFalse(self.m.CONFIRM_INCREMENTAL_CANDIDATES_WITH_FULL_RUNTIME)
        self.assertFalse(self.m.RUNTIME_STATEFUL_PARITY_PROOF_REQUIRED)

    def test_atomic_json_publish_never_exposes_partial_document(self):
        import json
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            stop = threading.Event()
            errors = []

            def reader():
                while not stop.is_set():
                    if not path.exists():
                        continue
                    try:
                        # Windows may transiently deny an open while os.replace
                        # swaps the destination. Atomicity means every successful
                        # read is complete JSON; a sharing violation is not a
                        # partially published document.
                        json.loads(path.read_text(encoding="utf-8"))
                    except (PermissionError, FileNotFoundError):
                        continue
                    except Exception as exc:  # pragma: no cover - captures malformed publish
                        errors.append(exc)
                        stop.set()

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for index in range(100):
                    self.m._atomic_write_json(path, {
                        "index": index,
                        "payload": "x" * 20000,
                    })
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(path.read_text())["index"], 99)


if __name__ == "__main__":
    unittest.main()
