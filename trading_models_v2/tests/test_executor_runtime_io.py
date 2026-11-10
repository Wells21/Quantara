from __future__ import annotations

import json
import tempfile
import time
import unittest
import sys
from pathlib import Path

import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from portfolio_live.executor_runtime_io import (
    ExecutorCriticalStateStore,
    RecentJsonlSignalFeed,
)


class ListLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple]] = []

    def info(self, message, *args):
        self.records.append(("info", (message, *args)))

    def warning(self, message, *args):
        self.records.append(("warning", (message, *args)))


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


class SignalFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jsonl = self.root / "live_approved_signals.jsonl"
        self.csv = self.root / "live_approved_signals.csv"
        self.logger = ListLogger()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def row(self, signal_id: str) -> dict:
        return {
            "signal_id": signal_id,
            "event_time": pd.Timestamp.now(tz="UTC").isoformat(),
            "signal_status": "approved_signal",
            "approved_signal": True,
        }

    def feed(self) -> RecentJsonlSignalFeed:
        return RecentJsonlSignalFeed(
            jsonl_path=self.jsonl,
            csv_fallback_path=self.csv,
            max_age_seconds=1000,
            bootstrap_max_bytes=1024 * 1024,
        )

    def test_idle_poll_reads_zero_additional_payload_bytes(self):
        append_jsonl(self.jsonl, [self.row("A"), self.row("B")])
        feed = self.feed()

        first = feed.read(self.logger)
        bytes_after_first = feed.snapshot()["jsonl_bytes_read"]
        second = feed.read(self.logger)
        snapshot = feed.snapshot()

        self.assertEqual(set(first["signal_id"]), {"A", "B"})
        self.assertEqual(set(second["signal_id"]), {"A", "B"})
        self.assertEqual(snapshot["jsonl_bytes_read"], bytes_after_first)
        self.assertEqual(snapshot["idle_polls"], 1)

    def test_append_reads_only_new_bytes_and_preserves_retry_window(self):
        append_jsonl(self.jsonl, [self.row("A")])
        feed = self.feed()
        first = feed.read(self.logger)
        before = feed.snapshot()["jsonl_bytes_read"]

        append_jsonl(self.jsonl, [self.row("B")])
        second = feed.read(self.logger)
        after = feed.snapshot()

        self.assertEqual(set(first["signal_id"]), {"A"})
        self.assertEqual(set(second["signal_id"]), {"A", "B"})
        self.assertGreater(after["jsonl_bytes_read"], before)
        self.assertEqual(after["jsonl_append_reads"], 1)

    def test_restart_bootstraps_recent_window(self):
        append_jsonl(self.jsonl, [self.row("A"), self.row("B")])
        first = self.feed()
        self.assertEqual(set(first.read()["signal_id"]), {"A", "B"})

        restarted = self.feed()
        self.assertEqual(set(restarted.read()["signal_id"]), {"A", "B"})
        self.assertEqual(restarted.snapshot()["jsonl_bootstraps"], 1)

    def test_same_path_rewrite_is_detected_and_rebootstrapped(self):
        first_row = self.row("AAAA")
        second_row = dict(first_row)
        second_row["signal_id"] = "BBBB"
        raw_first = json.dumps(first_row, sort_keys=True) + "\n"
        raw_second = json.dumps(second_row, sort_keys=True) + "\n"
        self.assertEqual(len(raw_first), len(raw_second))

        self.jsonl.write_text(raw_first, encoding="utf-8")
        feed = self.feed()
        self.assertEqual(set(feed.read()["signal_id"]), {"AAAA"})

        # Ensure mtime changes even on coarse filesystems.
        time.sleep(0.01)
        self.jsonl.write_text(raw_second, encoding="utf-8")
        self.assertEqual(set(feed.read()["signal_id"]), {"BBBB"})
        self.assertGreaterEqual(feed.snapshot()["rotations_or_truncations"], 1)

    def test_csv_fallback_is_cached_until_file_changes(self):
        pd.DataFrame([self.row("A")]).to_csv(self.csv, index=False)
        feed = self.feed()

        first = feed.read()
        second = feed.read()

        self.assertEqual(set(first["signal_id"]), {"A"})
        self.assertEqual(set(second["signal_id"]), {"A"})
        self.assertEqual(feed.snapshot()["csv_fallback_reads"], 1)


class CriticalStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "critical.json"
        self.backup = self.root / "critical.json.last_good"
        self.write_count = 0

        def writer(path, payload, *, backup_path, backup_interval_seconds, logger=None):
            self.write_count += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
            return {"status": "ok", "write_count": self.write_count}

        self.store = ExecutorCriticalStateStore(
            path=self.path,
            backup_path=self.backup,
            atomic_writer=writer,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sample_state(self) -> dict:
        meta = {
            "signal_id": "sig-1",
            "edge_id": "X/edge",
            "asset": "X",
            "position_ticket": "123",
            "order": "456",
            "deal": "789",
            "comment": "V2P|X|EDGE",
        }
        return {
            "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "terminal_signal_ids": {
                "sig-1": {
                    "status": "executed",
                    "reason": "ok",
                    "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
                    "plan": {"very_large": list(range(100))},
                    "order": 456,
                }
            },
            "open_position_metadata_by_ticket": {"123": dict(meta)},
            "open_position_metadata_by_signal_id": {"sig-1": dict(meta)},
            "open_position_metadata_by_order": {"456": dict(meta)},
            "open_position_metadata_by_deal": {"789": dict(meta)},
            "open_position_metadata_by_comment": {"V2P|X|EDGE": dict(meta)},
            "ambiguous_position_metadata_comments": {},
            "unresolved_position_metadata_by_ticket": {},
            "transient_order_retry_by_signal": {},
            "transient_plan_retry_by_signal": {"sig-retry": {"next_retry_at": "2099-01-01T00:00:00+00:00"}},
            "market_closed_cooldown_by_symbol": {},
            "preferred_filling_mode_by_symbol": {"X": 1},
            "prop_risk_state": {
                "overall_high_watermark": 101000.0,
                "equity_high_watermark": 101000.0,
                "realized_balance_high_watermark": 101000.0,
                "daily_anchor_utc_day": "2026-08-13",
                "daily_anchor_start_balance": 100000.0,
                "last_history_sync_utc": "changes-every-loop-and-must-not-trigger-write",
            },
            "equity_high_watermark_migration": {},
            "execution_state_integrity_blocked": False,
            "closed_trade_ledger_integrity_blocked": False,
            "last_manifest_hash": "manifest",
            "last_bar_policy_hash": "policy",
            "runtime_observability": {"huge": list(range(1000))},
        }

    def test_timestamp_and_observability_only_changes_do_not_rewrite(self):
        state = self.sample_state()
        first = self.store.persist_if_changed(state)
        self.assertTrue(first["written"])
        self.assertEqual(self.write_count, 1)

        state["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        state["runtime_observability"] = {"changed": True}
        state["prop_risk_state"]["last_history_sync_utc"] = "different"
        second = self.store.persist_if_changed(state)

        self.assertFalse(second["written"])
        self.assertEqual(self.write_count, 1)

    def test_high_watermark_change_is_persisted_immediately(self):
        state = self.sample_state()
        self.store.persist_if_changed(state)
        state["prop_risk_state"]["overall_high_watermark"] = 102000.0
        state["prop_risk_state"]["equity_high_watermark"] = 102000.0
        state["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()

        result = self.store.persist_if_changed(state)

        self.assertTrue(result["written"])
        self.assertEqual(self.write_count, 2)

    def test_restore_rebuilds_position_indexes_and_retry_state(self):
        state = self.sample_state()
        self.store.persist_if_changed(state)

        older = {
            "updated_at": "2000-01-01T00:00:00+00:00",
            "terminal_signal_ids": {},
            "open_position_metadata_by_ticket": {},
            "open_position_metadata_by_signal_id": {},
            "open_position_metadata_by_order": {},
            "open_position_metadata_by_deal": {},
            "open_position_metadata_by_comment": {},
            "prop_risk_state": {},
        }
        result = self.store.restore_into(older)

        self.assertEqual(result["status"], "restored_newer_or_equal_critical_checkpoint")
        self.assertIn("sig-1", older["terminal_signal_ids"])
        self.assertIn("123", older["open_position_metadata_by_ticket"])
        self.assertIn("sig-1", older["open_position_metadata_by_signal_id"])
        self.assertIn("sig-retry", older["transient_plan_retry_by_signal"])
        self.assertEqual(older["prop_risk_state"]["overall_high_watermark"], 101000.0)

    def test_checkpoint_compacts_duplicate_position_maps_and_terminal_plan(self):
        state = self.sample_state()
        self.store.persist_if_changed(state)
        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["position_metadata_records"]), 1)
        self.assertNotIn("plan", payload["terminal_signal_ids"]["sig-1"])


if __name__ == "__main__":
    unittest.main()
