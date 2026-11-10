from __future__ import annotations

import ast
import inspect
import json
import logging
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pandas as pd

from tests.test_live_tick_cursor_safety import FakeLogger, load_module

EXECUTOR = Path(__file__).resolve().parents[1] / "portfolio_live" / "portfolio_execution_bot.py"


def sample_bar(stamp: str, *, executable: bool = True, gap_healed: bool = False, sequence: int | None = None) -> dict[str, Any]:
    ts = pd.Timestamp(stamp)
    row = {
        "Datetime": ts.isoformat(), "Bar_Start": (ts - pd.Timedelta(seconds=10)).isoformat(),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5,
        "Ticks": 100, "Threshold": 100, "Proxy_Value": 100,
        "Gap_Healed": gap_healed, "Executable": executable,
        "Threshold_Unit": "ticks", "Research_Bar_Mode": "standard_quote_tick",
        "Threshold_Mode": "causal_daily_ewma_tick_count",
        "Closed_At": (ts + pd.Timedelta(seconds=1)).isoformat(),
    }
    if sequence is not None:
        row["Bar_Sequence_Number"] = sequence
        row["Bar_Time_Contract_Version"] = "v2_bar_time_v1_datetime_identity_closed_at_observation"
    return row


class DashboardBarSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_pending = self.m.PENDING_LIVE_BAR_DIR
        self.old_bars = self.m.settings.LIVE_BARS_DIR
        self.m.PENDING_LIVE_BAR_DIR = root / "pending"
        self.m.settings.LIVE_BARS_DIR = root / "bars"
        self.state = self.m.initial_state()
        self.logger = FakeLogger()

    def tearDown(self):
        self.m.PENDING_LIVE_BAR_DIR = self.old_pending
        self.m.settings.LIVE_BARS_DIR = self.old_bars
        self.temp.cleanup()

    def test_reconstruction_counts_only_executable_non_gap_bars(self):
        frame = self.m.standardize_bars(pd.DataFrame([
            sample_bar("2026-07-02T06:00:00Z"),
            sample_bar("2026-07-02T06:10:00Z", executable=False, gap_healed=True),
            sample_bar("2026-07-02T06:20:00Z"),
        ]), "sequence-test")
        out = self.m._reconstruct_vertical_bar_sequence_authority("BTCUSD", frame, self.state, self.logger)
        eligible = out[out["Executable"] & ~out["Gap_Healed"]]
        self.assertEqual(eligible["Bar_Sequence_Number"].tolist(), [1, 2])
        self.assertTrue(pd.isna(out.iloc[1]["Bar_Sequence_Number"]))
        self.assertEqual(self.state["bar_sequence_by_asset"]["BTCUSD"], 2)

    def test_conflicting_persisted_sequence_fails_closed(self):
        frame = self.m.standardize_bars(pd.DataFrame([
            sample_bar("2026-07-02T06:00:00Z", sequence=1),
            sample_bar("2026-07-02T06:20:00Z", sequence=7),
        ]), "sequence-test")
        with self.assertRaises(RuntimeError):
            self.m._reconstruct_vertical_bar_sequence_authority("BTCUSD", frame, self.state, self.logger)

    def test_sequence_is_in_wal_before_state_ack(self):
        source = inspect.getsource(self.m.process_ticks_to_bars)
        self.assertLess(source.index("_prepare_executable_bar_sequence_record"), source.index("append_live_bar("))
        self.assertLess(source.index("append_live_bar("), source.index("_commit_executable_bar_sequence_record"))
        record = self.m._prepare_executable_bar_sequence_record("BTCUSD", sample_bar("2026-07-02T06:00:00Z"), self.state)
        path = self.m.journal_live_bar_record("BTCUSD", record)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["record"]["Bar_Sequence_Number"], 1)
        self.m._commit_executable_bar_sequence_record("BTCUSD", record, self.state, self.logger)
        self.assertEqual(self.state["bar_sequence_by_asset"]["BTCUSD"], 1)

    def test_ack_exports_sequence_datetime_and_closed_at(self):
        record = self.m._prepare_executable_bar_sequence_record("BTCUSD", sample_bar("2026-07-02T06:00:00Z"), self.state)
        self.m._commit_executable_bar_sequence_record("BTCUSD", record, self.state, self.logger)
        payload = self.m.dashboard_bar_ack_payload(
            self.state, {"live_trade_enabled": False}, {"policy_gate_status": "TEST", "live_policy_ready": False}
        )
        self.assertEqual(payload["schema_version"], "v2_dashboard_bar_ack_v2_sequence")
        item = payload["latest_acknowledged_bar_by_asset"]["BTCUSD"]
        self.assertEqual(item["bar_sequence_number"], 1)
        self.assertEqual(item["Datetime"], "2026-07-02T06:00:00+00:00")
        self.assertIsNotNone(item["Closed_At"])


    def test_reconstruction_rejects_persisted_state_sequence_regression(self):
        self.state["bar_sequence_by_asset"]["BTCUSD"] = 3
        self.state["latest_acknowledged_bar_by_asset"]["BTCUSD"] = {
            "asset": "BTCUSD",
            "bar_sequence_number": 3,
            "Datetime": "2026-07-02T06:30:00Z",
            "Closed_At": "2026-07-02T06:30:01Z",
            "bar_sequence_contract_version": "v2_executable_bar_sequence_v1",
            "bar_time_contract_version": "v2_bar_time_v1_datetime_identity_closed_at_observation",
        }
        frame = self.m.standardize_bars(pd.DataFrame([
            sample_bar("2026-07-02T06:00:00Z"),
            sample_bar("2026-07-02T06:20:00Z"),
        ]), "sequence-regression-test")
        with self.assertRaises(RuntimeError):
            self.m._reconstruct_vertical_bar_sequence_authority(
                "BTCUSD", frame, self.state, self.logger
            )

    def test_stage4_contract_row_missing_durable_sequence_fails_closed(self):
        row = sample_bar("2026-07-02T06:00:00Z")
        row["Bar_Time_Contract_Version"] = (
            "v2_bar_time_v1_datetime_identity_closed_at_observation"
        )
        frame = self.m.standardize_bars(
            pd.DataFrame([row]), "sequence-contract-corruption-test"
        )
        with self.assertRaises(RuntimeError):
            self.m._reconstruct_vertical_bar_sequence_authority(
                "BTCUSD", frame, self.state, self.logger
            )


class ExecutorSequenceProgressTests(unittest.TestCase):
    def load_progress(self, read_ack):
        source = EXECUTOR.read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vertical_barrier_progress")
        mod = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(mod)
        def ensure_utc_timestamp(value):
            if value in (None, ""):
                return None
            stamp = pd.Timestamp(value)
            return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        def safe_int(value, default=None):
            try:
                if value is None or pd.isna(value):
                    return default
                return int(float(value))
            except Exception:
                return default
        ns = {
            "Any": Any, "pd": pd, "safe_int": safe_int,
            "ensure_utc_timestamp": ensure_utc_timestamp,
            "utc_now_ts": lambda: pd.Timestamp("2026-07-02T08:00:00Z"),
            "_read_dashboard_bar_ack": read_ack,
            "BAR_SEQUENCE_CONTRACT_VERSION": "v2_executable_bar_sequence_v1",
            "BAR_TIME_CONTRACT_VERSION": "v2_bar_time_v1_datetime_identity_closed_at_observation",
            "VERTICAL_BARRIER_FUTURE_TOLERANCE_SECONDS": 5.0,
        }
        exec(compile(mod, str(EXECUTOR), "exec"), ns)
        return ns["vertical_barrier_progress"]

    @staticmethod
    def ack(sequence=178240, stamp="2026-07-02T07:00:00Z"):
        return {
            "ok": True, "ack_source": "dashboard_bar_ack", "bar_sequence_number": sequence,
            "bar_datetime": pd.Timestamp(stamp),
            "bar_closed_at": pd.Timestamp("2026-07-02T06:59:00Z"),
        }

    def test_progress_is_sequence_arithmetic_closed_at_not_gate(self):
        fn = self.load_progress(lambda asset, logger: self.ack())
        result = fn("BTCUSD", "2026-07-02T06:00:00Z", logging.getLogger("seq"),
                    event_sequence=178234, position_open_time="2026-07-02T06:05:00Z")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["count"], 6)
        self.assertEqual(result["bars_after_position_open"], 1)

    def test_sequence_regression_fails_closed(self):
        fn = self.load_progress(lambda asset, logger: self.ack(sequence=99))
        result = fn("BTCUSD", "2026-07-02T06:00:00Z", logging.getLogger("seq"), event_sequence=100)
        self.assertEqual(result["status"], "defer")
        self.assertEqual(result["reason"], "dashboard_bar_sequence_precedes_event_sequence")

    def test_normal_progress_has_no_history_scan_or_old_deferral(self):
        source = EXECUTOR.read_text(encoding="utf-8")
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "vertical_barrier_progress")
        body = ast.get_source_segment(source, node) or ""
        self.assertNotIn("_read_live_bars_with_retry", body)
        self.assertNotIn("pd.read_csv", body)
        self.assertNotIn("bar_closed_at_precedes_datetime", body)


class ExecutorLegacySequenceMigrationTests(unittest.TestCase):
    def _load_functions(self, names: set[str], namespace: dict[str, Any]):
        source = EXECUTOR.read_text(encoding="utf-8")
        tree = ast.parse(source)
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        module = ast.Module(body=nodes, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, str(EXECUTOR), "exec"), namespace)
        return namespace

    def test_startup_index_matches_executable_non_gap_semantics(self):
        bars = pd.DataFrame([
            sample_bar("2026-07-02T06:00:00Z"),
            sample_bar(
                "2026-07-02T06:10:00Z",
                executable=False,
                gap_healed=True,
            ),
            sample_bar("2026-07-02T06:20:00Z"),
        ])

        def ensure_utc_timestamp(value):
            if value in (None, ""):
                return None
            stamp = pd.Timestamp(value)
            return (
                stamp.tz_localize("UTC")
                if stamp.tzinfo is None
                else stamp.tz_convert("UTC")
            )

        namespace = {
            "Any": Any,
            "pd": pd,
            "safe_bool": lambda value, default=False: bool(value),
            "safe_int": lambda value, default=None: (
                default if value in (None, "") else int(float(value))
            ),
            "ensure_utc_timestamp": ensure_utc_timestamp,
            "_read_live_bars_with_retry": (
                lambda asset, logger: (bars.copy(), None)
            ),
        }
        ns = self._load_functions(
            {"_parse_mixed_utc", "_build_startup_sequence_index"},
            namespace,
        )
        index, reason = ns["_build_startup_sequence_index"](
            "BTCUSD",
            {
                "bar_sequence_number": 2,
                "bar_datetime": pd.Timestamp("2026-07-02T06:20:00Z"),
            },
            logging.getLogger("migration-index"),
        )
        self.assertEqual(reason, "ok")
        self.assertEqual(
            index,
            {
                "2026-07-02T06:00:00+00:00": 1,
                "2026-07-02T06:20:00+00:00": 2,
            },
        )

    def test_ack_startup_race_retries_without_history_scan_until_ready(self):
        from types import SimpleNamespace

        position = SimpleNamespace(ticket=123, comment="V2|TEST")
        meta = {
            "signal_id": "SIG-1",
            "edge_id": "BTCUSD/test",
            "asset": "BTCUSD",
            "vertical_horizon_bars": 6,
            "entry_event_time": "2026-07-02T06:00:00Z",
        }
        state: dict[str, Any] = {}
        ack_ready = {"value": False}
        build_calls = {"count": 0}
        registered: list[dict[str, Any]] = []
        now = pd.Timestamp("2026-07-02T08:00:00Z")

        def ensure_utc_timestamp(value):
            if value in (None, ""):
                return None
            stamp = pd.Timestamp(value)
            return (
                stamp.tz_localize("UTC")
                if stamp.tzinfo is None
                else stamp.tz_convert("UTC")
            )

        def read_ack(asset, logger):
            if not ack_ready["value"]:
                return {
                    "ok": False,
                    "reason": "dashboard_ack_read_failed:missing",
                }
            return {
                "ok": True,
                "bar_sequence_number": 10,
                "bar_datetime": pd.Timestamp("2026-07-02T07:00:00Z"),
            }

        def build_index(asset, ack, logger):
            build_calls["count"] += 1
            return {"2026-07-02T06:00:00+00:00": 5}, "ok"

        def register(state_arg, metadata, **kwargs):
            registered.append(dict(metadata))

        namespace = {
            "Any": Any,
            "pd": pd,
            "safe_int": lambda value, default=None: (
                default if value in (None, "") else int(float(value))
            ),
            "safe_bool": lambda value, default=False: (
                default if value is None else bool(value)
            ),
            "ensure_utc_timestamp": ensure_utc_timestamp,
            "utc_now": lambda: now.isoformat(),
            "utc_now_ts": lambda: now,
            "metadata_key": lambda value: (
                str(value) if value is not None else ""
            ),
            "resolve_position_metadata": lambda pos, state_arg: (
                dict(meta), "test"
            ),
            "register_position_metadata": register,
            "_read_dashboard_bar_ack": read_ack,
            "_build_startup_sequence_index": build_index,
            "BAR_SEQUENCE_CONTRACT_VERSION": "v2_executable_bar_sequence_v1",
            "BAR_TIME_CONTRACT_VERSION": "v2_bar_time_v1_datetime_identity_closed_at_observation",
            "VERTICAL_SEQUENCE_MIGRATION_RETRY_SECONDS": 5.0,
        }
        ns = self._load_functions(
            {
                "migrate_open_position_vertical_sequences",
                "retry_pending_vertical_sequence_migration_if_due",
            },
            namespace,
        )
        ns["migrate_open_position_vertical_sequences"].__globals__[
            "migrate_open_position_vertical_sequences"
        ] = ns["migrate_open_position_vertical_sequences"]

        mt5 = SimpleNamespace(positions_get=lambda: [position])
        first = ns["migrate_open_position_vertical_sequences"](
            mt5, state, logging.getLogger("migration-race")
        )
        self.assertEqual(first["status"], "waiting_for_sequence_ack")
        self.assertEqual(first["failed"], 0)
        self.assertEqual(first["waiting_for_sequence_ack"], 1)
        self.assertEqual(build_calls["count"], 0)

        state["vertical_barrier_sequence_startup_migration"][
            "next_retry_at"
        ] = (now - pd.Timedelta(seconds=1)).isoformat()
        ack_ready["value"] = True
        second = ns["retry_pending_vertical_sequence_migration_if_due"](
            mt5, state, logging.getLogger("migration-race")
        )
        self.assertTrue(second["retry_attempted"])
        self.assertEqual(second["status"], "pass")
        self.assertEqual(second["migrated"], 1)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(build_calls["count"], 1)
        self.assertEqual(registered[-1]["entry_event_bar_sequence"], 5)


if __name__ == "__main__":
    unittest.main()
