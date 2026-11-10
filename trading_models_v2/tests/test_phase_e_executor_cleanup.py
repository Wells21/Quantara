from __future__ import annotations

import tempfile
import threading
import time
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[1]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from portfolio_live.executor_runtime_io import AsyncNotificationWorker
from portfolio_live.portfolio_closed_trade_ledger import reconcile_closed_trades


class FakeMT5:
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_INOUT = 2
    DEAL_ENTRY_OUT_BY = 3
    DEAL_REASON_SL = 10
    DEAL_REASON_TP = 11

    def __init__(self, deals_by_position=None, broad_deals=None, open_ids=None):
        self.deals_by_position = dict(deals_by_position or {})
        self.broad_deals = list(broad_deals or [])
        self.open_ids = set(str(value) for value in (open_ids or []))
        self.broad_history_calls = 0
        self.position_history_calls = []
        self.ticket_history_calls = []

    def positions_get(self):
        return [SimpleNamespace(ticket=int(value), identifier=int(value)) for value in sorted(self.open_ids)]

    def history_deals_get(self, *args, **kwargs):
        if "position" in kwargs:
            key = str(kwargs["position"])
            self.position_history_calls.append(key)
            return list(self.deals_by_position.get(key, []))
        if "ticket" in kwargs:
            self.ticket_history_calls.append(str(kwargs["ticket"]))
            return []
        if len(args) == 2:
            self.broad_history_calls += 1
            return list(self.broad_deals)
        raise AssertionError("unexpected history_deals_get call: %r %r" % (args, kwargs))


def make_deal(ticket, position_id, entry, time_msc, price, profit=0.0):
    return SimpleNamespace(
        ticket=int(ticket), order=int(ticket) + 1000, position_id=int(position_id),
        time=int(time_msc // 1000), time_msc=int(time_msc), type=0, entry=int(entry),
        magic=260512, reason=0, volume=0.1, price=float(price), commission=0.0,
        swap=0.0, profit=float(profit), fee=0.0, symbol="TEST",
        comment="V2P|TEST|EDGE", external_id="",
    )


class PhaseEClosedTradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rows = []
        self.now = pd.Timestamp.now(tz="UTC")
        self.entry = make_deal(1, 101, FakeMT5.DEAL_ENTRY_IN, 1700000000000, 100.0)
        self.exit = make_deal(2, 101, FakeMT5.DEAL_ENTRY_OUT, 1700000060000, 101.0, 10.0)

    def tearDown(self):
        self.temp.cleanup()

    def append_rows(self, path, rows, dedup_cols=None):
        self.rows.extend(rows)

    def state(self, full_audit_at=True):
        meta = {
            "signal_id": "sig-1", "edge_id": "TEST/edge", "asset": "TEST",
            "family": "test", "side_name": "long", "side_value": 1,
            "position_identifier": "101", "position_ticket": "101",
            "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 102.0,
            "actual_risk_usd_at_entry": 10.0, "symbol": "TEST",
        }
        return {
            "terminal_signal_ids": {"sig-1": {"status": "executed", "timestamp": self.now.isoformat()}},
            "open_position_metadata_by_ticket": {"101": dict(meta)},
            "open_position_metadata_by_signal_id": {"sig-1": dict(meta)},
            "open_position_metadata_by_order": {}, "open_position_metadata_by_deal": {},
            "open_position_metadata_by_comment": {},
            "closed_trade_ledger": {
                "last_reconcile_at": (self.now - pd.Timedelta(seconds=60)).isoformat(),
                "last_full_audit_at": self.now.isoformat() if full_audit_at else None,
                "processed_position_ids": [], "last_history_cursor_msc": 0,
                "last_result": {}, "documented_position_exceptions": {},
                "last_unexplained_executed_position_ids": [],
            },
        }

    def run_reconcile(self, mt5, state, **kwargs):
        logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
        return reconcile_closed_trades(
            mt5=mt5, state=state, live_bars_dir=self.root / "live_bars",
            append_rows=self.append_rows, csv_path=self.root / "closed.csv", logger=logger,
            minimum_interval_seconds=1.0, history_overlap_days=7, history_lookback_days=120,
            full_audit_interval_seconds=21600.0, **kwargs
        )

    def test_normal_cycle_queries_only_disappeared_position_id(self):
        state = self.state(True)
        mt5 = FakeMT5({"101": [self.entry, self.exit]}, [self.entry, self.exit])
        result = self.run_reconcile(mt5, state)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reconcile_mode"], "targeted_positions")
        self.assertFalse(result["broad_history_query_used"])
        self.assertEqual(mt5.broad_history_calls, 0)
        self.assertEqual(mt5.position_history_calls, ["101"])
        self.assertEqual(result["positions_written"], 1)

    def test_startup_runs_one_full_integrity_audit(self):
        state = self.state(False)
        mt5 = FakeMT5({"101": [self.entry, self.exit]}, [self.entry, self.exit])
        result = self.run_reconcile(mt5, state)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reconcile_mode"], "full_audit")
        self.assertTrue(result["broad_history_query_used"])
        self.assertEqual(mt5.broad_history_calls, 1)
        self.assertEqual(mt5.position_history_calls, [])

    def test_force_startup_audit_overrides_recent_reconcile_and_full_audit(self):
        state = self.state(True)
        state["closed_trade_ledger"]["last_reconcile_at"] = self.now.isoformat()
        mt5 = FakeMT5({"101": [self.entry, self.exit]}, [self.entry, self.exit])
        result = self.run_reconcile(mt5, state, force_full_audit=True)
        self.assertEqual(result["reconcile_mode"], "full_audit")
        self.assertEqual(mt5.broad_history_calls, 1)

    def test_periodic_full_audit_after_six_hours(self):
        state = self.state(True)
        state["closed_trade_ledger"]["last_full_audit_at"] = (self.now - pd.Timedelta(hours=7)).isoformat()
        mt5 = FakeMT5({"101": [self.entry, self.exit]}, [self.entry, self.exit])
        result = self.run_reconcile(mt5, state)
        self.assertEqual(result["reconcile_mode"], "full_audit")
        self.assertEqual(mt5.broad_history_calls, 1)

    def test_open_position_does_not_query_history_on_targeted_cycle(self):
        state = self.state(True)
        mt5 = FakeMT5({"101": [self.entry, self.exit]}, [self.entry, self.exit], {"101"})
        result = self.run_reconcile(mt5, state)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(mt5.broad_history_calls, 0)
        self.assertEqual(mt5.position_history_calls, [])

    def test_missing_targeted_history_remains_fail_closed(self):
        state = self.state(True)
        mt5 = FakeMT5({"101": []}, [])
        result = self.run_reconcile(mt5, state)
        self.assertEqual(result["status"], "FAIL_UNEXPLAINED_EXECUTED_POSITIONS")
        self.assertEqual(result["unexplained_executed_position_ids"], ["101"])
        self.assertEqual(mt5.broad_history_calls, 0)
        self.assertEqual(mt5.position_history_calls, ["101"])


class PhaseEAsyncTelegramTests(unittest.TestCase):
    def test_submit_is_nonblocking_even_when_sender_waits(self):
        sender_started = threading.Event()
        release_sender = threading.Event()

        def sender(message):
            sender_started.set()
            release_sender.wait(timeout=2.0)
            return {"chat": "ok"}

        worker = AsyncNotificationWorker(sender=sender, max_queue_size=8, name="phase_e_nonblocking")
        started = time.perf_counter()
        submitted = worker.submit(purpose="execution", message="hello")
        elapsed = time.perf_counter() - started
        self.assertTrue(submitted["queued"])
        self.assertLess(elapsed, 0.20)
        self.assertTrue(sender_started.wait(timeout=1.0))
        self.assertEqual(worker.drain_results(), [])
        release_sender.set()
        shutdown = worker.stop(drain=True, timeout_seconds=2.0)
        results = worker.drain_results()
        self.assertTrue(shutdown["shutdown_clean"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["result"], {"chat": "ok"})

    def test_clean_shutdown_drains_queued_notifications(self):
        delivered = []
        def sender(message):
            delivered.append(message)
            return {"chat": "ok"}
        worker = AsyncNotificationWorker(sender=sender, max_queue_size=8, name="phase_e_drain")
        for index in range(3):
            self.assertTrue(worker.submit(purpose="test", message="message-%s" % index)["queued"])
        shutdown = worker.stop(drain=True, timeout_seconds=2.0)
        results = worker.drain_results()
        self.assertTrue(shutdown["shutdown_clean"])
        self.assertEqual(delivered, ["message-0", "message-1", "message-2"])
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
