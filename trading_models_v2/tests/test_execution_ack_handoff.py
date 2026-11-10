from __future__ import annotations

import ast
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import pandas as pd


EXECUTOR = Path(__file__).resolve().parents[1] / "portfolio_live" / "portfolio_execution_bot.py"


def load_ack_function(namespace: dict[str, Any]):
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_read_dashboard_bar_ack"
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(EXECUTOR), "exec"), namespace)
    return namespace["_read_dashboard_bar_ack"]


class ExecutionAckHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ack = root / "latest_dashboard_bar_ack.json"
        self.status = root / "latest_dashboard_status.json"

        def read_json_object(path: Path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            return payload

        def ensure_utc_timestamp(value):
            if value in (None, ""):
                return None
            stamp = pd.Timestamp(value)
            return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")

        namespace = {
            "Any": Any,
            "DASHBOARD_BAR_ACK_JSON": self.ack,
            "DASHBOARD_STATUS_JSON": self.status,
            "DASHBOARD_BAR_ACK_SCHEMA_VERSION": "v2_dashboard_bar_ack_v2_sequence",
            "BAR_SEQUENCE_CONTRACT_VERSION": "v2_executable_bar_sequence_v1",
            "BAR_TIME_CONTRACT_VERSION": "v2_bar_time_v1_datetime_identity_closed_at_observation",
            "safe_int": lambda value, default=None: default if value in (None, "") else int(float(value)),
            "VERTICAL_BARRIER_STATUS_READ_RETRIES": 2,
            "LIVE_BAR_READ_RETRY_SECONDS": 0.001,
            "read_json_object": read_json_object,
            "ensure_utc_timestamp": ensure_utc_timestamp,
            "time": time,
        }
        self.fn = load_ack_function(namespace)
        self.logger = logging.getLogger("ack-test")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def payload(latest: str, schema: str, sequence: int = 42) -> dict[str, Any]:
        return {
            "schema_version": schema,
            "updated_at": "2026-06-30T10:00:00+00:00",
            "bar_sequence_contract_version": "v2_executable_bar_sequence_v1",
            "bar_time_contract_version": "v2_bar_time_v1_datetime_identity_closed_at_observation",
            "policy_gate_status": "POLICY_PASS",
            "live_policy_ready": True,
            "asset_continuity": {"BTCUSD": {"status": "PASS_HEALED_CONTINUOUS", "latest_live": latest}},
            "latest_acknowledged_bar_by_asset": {
                "BTCUSD": {
                    "asset": "BTCUSD", "bar_sequence_number": sequence,
                    "Datetime": latest, "Closed_At": "2026-06-30T10:00:00Z",
                    "bar_sequence_contract_version": "v2_executable_bar_sequence_v1",
                    "bar_time_contract_version": "v2_bar_time_v1_datetime_identity_closed_at_observation",
                }
            },
        }

    def test_reads_sequence_bar_ack(self):
        self.ack.write_text(json.dumps(self.payload("2026-06-30T09:59:00Z", "v2_dashboard_bar_ack_v2_sequence", 42)))
        result = self.fn("BTCUSD", self.logger)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ack_source"], "dashboard_bar_ack")
        self.assertEqual(result["bar_sequence_number"], 42)
        self.assertEqual(result["bar_datetime"], pd.Timestamp("2026-06-30T09:59:00Z"))

    def test_old_or_malformed_ack_fails_closed_without_status_fallback(self):
        self.ack.write_text(json.dumps(self.payload("2026-06-30T09:59:00Z", "v2_dashboard_bar_ack_v1")))
        self.status.write_text(json.dumps({"schema_version": "v2.1"}))
        result = self.fn("BTCUSD", self.logger)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ack_source"], "dashboard_bar_ack")
        self.assertTrue(result["reason"].startswith("dashboard_ack_read_failed:"))


    def test_policy_not_ready_fails_closed(self):
        payload = self.payload(
            "2026-06-30T09:59:00Z",
            "v2_dashboard_bar_ack_v2_sequence",
            42,
        )
        payload["live_policy_ready"] = False
        self.ack.write_text(json.dumps(payload))
        result = self.fn("BTCUSD", self.logger)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "dashboard_policy_not_ready")

    def test_sequence_ack_normalizes_asset_key(self):
        self.ack.write_text(json.dumps(self.payload(
            "2026-06-30T09:59:00Z",
            "v2_dashboard_bar_ack_v2_sequence",
            42,
        )))
        result = self.fn("btcusd", self.logger)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bar_sequence_number"], 42)

    def test_positive_sequence_requires_closed_at(self):
        payload = self.payload(
            "2026-06-30T09:59:00Z",
            "v2_dashboard_bar_ack_v2_sequence",
            42,
        )
        payload["latest_acknowledged_bar_by_asset"]["BTCUSD"]["Closed_At"] = None
        self.ack.write_text(json.dumps(payload))
        result = self.fn("BTCUSD", self.logger)
        self.assertFalse(result["ok"])
        self.assertIn("positive_sequence_missing_Closed_At", result["reason"])

    def test_missing_both_fails_closed(self):
        result = self.fn("BTCUSD", self.logger)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"].startswith("dashboard_ack_read_failed:"))


if __name__ == "__main__":
    unittest.main()
