from __future__ import annotations

import ast
import logging
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

import pandas as pd

from tests.test_live_tick_cursor_safety import FakeLogger, load_module


EXECUTOR = Path(__file__).resolve().parents[1] / "portfolio_live" / "portfolio_execution_bot.py"


def sample_bar(stamp: str, *, close: float = 100.0) -> dict[str, Any]:
    ts = pd.Timestamp(stamp)
    start = ts - pd.Timedelta(seconds=10)
    return {
        "Datetime": ts.isoformat(),
        "Bar_Start": start.isoformat(),
        "Open": close - 1.0,
        "High": close + 1.0,
        "Low": close - 2.0,
        "Close": close,
        "Ticks": 100,
        "Threshold": 100,
        "Proxy_Value": 100,
        "Gap_Healed": False,
        "Executable": True,
        "Threshold_Unit": "ticks",
        "Research_Bar_Mode": "standard_quote_tick",
        "Threshold_Mode": "causal_daily_ewma_tick_count",
        "Threshold_Source": "test",
        "Closed_At": ts.isoformat(),
    }


class DurableBarJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_pending = self.m.PENDING_LIVE_BAR_DIR
        self.old_bars = self.m.settings.LIVE_BARS_DIR
        self.m.PENDING_LIVE_BAR_DIR = self.root / "state" / "pending_live_bars"
        self.m.settings.LIVE_BARS_DIR = self.root / "live_bars"
        self.state = self.m.initial_state()
        self.logger = FakeLogger()

    def tearDown(self):
        self.m.PENDING_LIVE_BAR_DIR = self.old_pending
        self.m.settings.LIVE_BARS_DIR = self.old_bars
        self.temp.cleanup()

    def test_locked_canonical_csv_does_not_stop_logical_bar_stream(self):
        record = sample_bar("2026-07-02T06:37:40Z")
        self.m.append_live_bar("XAUUSD", record, state=self.state, logger=self.logger)

        pending_dir = self.m.pending_live_bar_asset_dir("XAUUSD")
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 1)

        with mock.patch.object(
            self.m,
            "atomic_write_dataframe_csv",
            side_effect=PermissionError(5, "Access is denied"),
        ):
            compacted = self.m.compact_pending_live_bars(
                "XAUUSD", self.state, self.logger, force=True
            )
        self.assertFalse(compacted)
        self.assertEqual(
            self.state["bar_persistence_status_by_asset"]["XAUUSD"]["status"],
            "canonical_csv_locked_journal_authoritative",
        )
        logical = self.m.load_live_bars("XAUUSD")
        self.assertEqual(len(logical), 1)
        self.assertEqual(float(logical.iloc[0]["Close"]), 100.0)

        compacted = self.m.compact_pending_live_bars(
            "XAUUSD", self.state, self.logger, force=True
        )
        self.assertTrue(compacted)
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 0)
        canonical = pd.read_csv(self.m.live_bars_path("XAUUSD"))
        self.assertEqual(len(canonical), 1)

    def test_journal_replay_is_idempotent(self):
        record = sample_bar("2026-07-02T06:37:40Z")
        first = self.m.journal_live_bar_record("XAUUSD", record)
        second = self.m.journal_live_bar_record("XAUUSD", dict(record))
        self.assertEqual(first, second)
        self.assertEqual(len(list(first.parent.glob("*.json"))), 1)

    def test_asset_state_rolls_back_and_cursor_does_not_advance_on_journal_failure(self):
        state = self.m.initial_state()
        state["current_bars"]["XAUUSD"] = {"ticks": 5, "threshold_ticks": 10}
        state["raw_daily_tick_counts_by_asset"]["XAUUSD"] = {"2026-07-02": 7}
        state["closed_bar_counts"]["XAUUSD"] = 3
        state["total_closed_bars_since_state_created"] = 3
        before = self.m.snapshot_asset_tick_transaction_state("XAUUSD", state)

        ticks = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-07-02T06:37:40Z"], utc=True),
                "broker_timestamp": pd.to_datetime(["2026-07-02T09:37:40Z"], utc=True),
                "time_msc": [1],
                "mid": [100.0],
                "broker_time_offset_seconds": [10800],
            }
        )

        def fail_after_mutation(*args, **kwargs):
            state["current_bars"]["XAUUSD"] = {"ticks": 99}
            state["raw_daily_tick_counts_by_asset"]["XAUUSD"] = {"2026-07-02": 999}
            state["closed_bar_counts"]["XAUUSD"] = 4
            state["total_closed_bars_since_state_created"] = 4
            raise self.m.LiveBarJournalError("simulated journal failure")

        with mock.patch.object(self.m, "active_assets_from_manifest", return_value=["XAUUSD"]), \
             mock.patch.object(self.m, "fetch_ticks", return_value=ticks), \
             mock.patch.object(self.m, "apply_live_tick_startup_guard", side_effect=lambda *a: a[2]), \
             mock.patch.object(self.m, "continuity_ok", return_value=True), \
             mock.patch.object(self.m, "process_ticks_to_bars", side_effect=fail_after_mutation), \
             mock.patch.object(self.m, "update_last_tick") as update_cursor, \
             mock.patch.object(self.m, "compact_pending_live_bars", return_value=True), \
             mock.patch.object(self.m, "score_selected_assets", return_value=[]):
            closed = self.m.run_once(
                None,
                {},
                {"bar_policies": {"XAUUSD": {"threshold_ticks": 10}}},
                state,
                self.logger,
                scoring_queue=None,
            )

        self.assertEqual(closed, 0)
        update_cursor.assert_not_called()
        after = self.m.snapshot_asset_tick_transaction_state("XAUUSD", state)
        self.assertEqual(before, after)
        self.assertTrue(state["asset_io_paused_by_asset"]["XAUUSD"])


class RoutingAndConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def test_native_event_api_remains_retired_under_full_authority_runtime(self):
        # The live dashboard no longer carries a native-event fallback API.
        # Full authoritative runtime generation is the only supported path.
        self.assertEqual(
            self.m.AUTHORITATIVE_RUNTIME_MODE,
            "authoritative_batch_shared_cache",
        )
        self.assertFalse(self.m.LIVE_EVENT_ONLY_RUNTIME_API_ENABLED)
        self.assertFalse(self.m.LIVE_INCREMENTAL_FEATURE_RUNTIME_API_ENABLED)
        self.assertFalse(
            hasattr(self.m, "call_runtime_live_event_api"),
            "retired native event API must stay absent",
        )
        self.assertFalse(
            hasattr(self.m, "generate_candidate_events_with_live_api_fallback"),
            "retired native/full fallback router must stay absent",
        )

    def test_runtime_calls_are_globally_bounded(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def fake_score_edge(*args, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return []

        edge = {"edge_id": "X/test", "asset": "X"}
        with mock.patch.object(self.m, "score_edge", side_effect=fake_score_edge), \
             mock.patch.object(self.m, "_edge_state_delta_after_parallel_worker", return_value={}):
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [
                    executor.submit(
                        self.m._score_edge_parallel_worker,
                        edge,
                        {},
                        {},
                        {},
                        FakeLogger(),
                        bars_snapshot=pd.DataFrame(),
                        live_event_times_snapshot=set(),
                    )
                    for _ in range(10)
                ]
                for future in futures:
                    future.result()
        self.assertLessEqual(maximum, self.m.MAX_CONCURRENT_RUNTIME_CALLS)
        self.assertGreater(maximum, 1)


class AtomicStatePersistenceTests(unittest.TestCase):
    def test_state_writer_retries_transient_replace_contention(self):
        import importlib.util
        import json
        import tempfile

        module_path = (
            Path(__file__).resolve().parents[1]
            / "portfolio_live"
            / "production_state_integrity.py"
        )
        spec = importlib.util.spec_from_file_location(
            "state_integrity_atomic_retry_test",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            real_replace = module.os.replace
            attempts = {"count": 0}

            def flaky_replace(source, destination):
                if Path(destination) == path and attempts["count"] < 4:
                    attempts["count"] += 1
                    raise PermissionError(13, "simulated Windows sharing violation")
                return real_replace(source, destination)

            with mock.patch.object(module.os, "replace", side_effect=flaky_replace):
                report = module.atomic_write_state(
                    path,
                    {"generation": 7, "status": "ready"},
                    backup_interval_seconds=0.0,
                )

            self.assertEqual(report["status"], "saved")
            self.assertGreaterEqual(attempts["count"], 4)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["generation"],
                7,
            )



class ExecutorHandoffTests(unittest.TestCase):
    def _load_functions(self, names: set[str], namespace: dict[str, Any]):
        tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(EXECUTOR), "exec"), namespace)
        return namespace

    def test_pending_journal_rows_are_merged_for_vertical_exit_reads(self):
        namespace = self._load_functions(
            {"_parse_mixed_utc", "_merge_executor_live_bar_sources"},
            {"pd": pd, "Any": Any},
        )
        base = pd.DataFrame([sample_bar("2026-07-02T06:00:00Z", close=100.0)])
        pending = pd.DataFrame([
            sample_bar("2026-07-02T06:00:00Z", close=101.0),
            sample_bar("2026-07-02T06:10:00Z", close=102.0),
        ])
        merged = namespace["_merge_executor_live_bar_sources"](base, pending)
        self.assertEqual(len(merged), 2)
        self.assertEqual(float(merged.iloc[0]["Close"]), 101.0)
        self.assertEqual(float(merged.iloc[1]["Close"]), 102.0)

    def test_ack_catchup_retries_until_target_bar_is_published(self):
        sequence = [
            {
                "ok": True,
                "latest_live": pd.Timestamp("2026-07-02T06:00:00Z"),
            },
            {
                "ok": True,
                "latest_live": pd.Timestamp("2026-07-02T06:10:00Z"),
            },
        ]

        def read_ack(asset, logger):
            return sequence.pop(0)

        namespace = self._load_functions(
            {"_retry_dashboard_ack_until"},
            {
                "pd": pd,
                "Any": Any,
                "time": time,
                "VERTICAL_BARRIER_ACK_CATCHUP_RETRIES": 4,
                "VERTICAL_BARRIER_ACK_CATCHUP_SLEEP_SECONDS": 0.001,
                "_read_dashboard_bar_ack": read_ack,
            },
        )
        result = namespace["_retry_dashboard_ack_until"](
            "BTCUSD",
            pd.Timestamp("2026-07-02T06:10:00Z"),
            logging.getLogger("ack-catchup"),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["catchup_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
