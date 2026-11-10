"""Live application of the decommission/shrink rule (AFML Ch.1 p.9; D021, D022)."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = V2_ROOT / "portfolio_live"
if str(LIVE_DIR) not in sys.path:
    sys.path.insert(0, str(LIVE_DIR))

import decommission_rule  # noqa: E402
import shadow_bets  # noqa: E402


def _load_bot():
    spec = importlib.util.spec_from_file_location("_test_execution_bot", LIVE_DIR / "portfolio_execution_bot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BOT = _load_bot()
CFG = decommission_rule.json.loads(decommission_rule.CONFIG_PATH.read_text(encoding="utf-8"))


def bars(rows):
    df = pd.DataFrame(rows, columns=["Datetime", "High", "Low", "Close"])
    df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True)
    return df


LONG = {"signal_id": "s1", "edge_id": "X/e_long", "asset": "X", "side_value": 1,
        "event_time": "2026-10-01T00:00:00+00:00", "signal_price": 100.0,
        "planned_stop_loss": 99.0, "planned_take_profit": 102.0, "vertical_horizon_hours": 10.0,
        "live_manifest_hash": "m1"}


class TestShadowResolution(unittest.TestCase):
    def test_take_profit(self):
        out = shadow_bets.resolve_one(LONG, bars([("2026-10-01T01:00Z", 101.0, 99.5, 100.5), ("2026-10-01T02:00Z", 102.1, 100.5, 102.0)]))
        self.assertEqual((out["exit_reason"], out["r"]), ("tp", 2.0))

    def test_stop_loss(self):
        out = shadow_bets.resolve_one(LONG, bars([("2026-10-01T01:00Z", 100.5, 98.9, 99.0)]))
        self.assertEqual((out["exit_reason"], out["r"]), ("sl", -1.0))

    def test_both_in_one_bar_is_stop_first(self):
        out = shadow_bets.resolve_one(LONG, bars([("2026-10-01T01:00Z", 102.5, 98.5, 100.0)]))
        self.assertEqual(out["exit_reason"], "sl")

    def test_vertical_barrier(self):
        out = shadow_bets.resolve_one(LONG, bars([("2026-10-01T05:00Z", 100.6, 99.6, 100.3), ("2026-10-01T10:00Z", 100.8, 99.8, 100.5)]))
        self.assertEqual(out["exit_reason"], "vertical")
        self.assertAlmostEqual(out["r"], 0.5)

    def test_short_side(self):
        short = {**LONG, "side_value": -1, "planned_stop_loss": 101.0, "planned_take_profit": 98.0}
        out = shadow_bets.resolve_one(short, bars([("2026-10-01T01:00Z", 100.5, 97.9, 98.0)]))
        self.assertEqual((out["exit_reason"], out["r"]), ("tp", 2.0))

    def test_unresolved_stays_pending(self):
        self.assertIsNone(shadow_bets.resolve_one(LONG, bars([("2026-10-01T01:00Z", 100.5, 99.5, 100.2)])))

    def test_bars_before_event_are_ignored(self):
        self.assertIsNone(shadow_bets.resolve_one(LONG, bars([("2026-09-30T23:00Z", 103.0, 98.0, 100.0)])))


class TestShadowCosts(unittest.TestCase):
    def test_broker_costs_are_charged(self):
        row = {**LONG, "asset": "XAUUSD", "signal_price": 4100.0, "planned_stop_loss": 4090.0,
               "planned_take_profit": 4120.0, "status": "resolved", "exit_reason": "tp",
               "exit_time": "2026-10-02T12:00:00+00:00", "r": 2.0}
        out = shadow_bets.apply_broker_costs(row)
        if out["cost_basis"] == "gross_no_cost_model":
            self.skipTest("no cost model for the active firm on this machine")
        self.assertGreater(out["spread_R"], 0.0)
        self.assertAlmostEqual(out["r"], out["r_gross"] - out["spread_R"] - out["swap_R"] - out["commission_R"])

    def test_missing_cost_model_stays_gross_and_is_marked(self):
        with mock.patch.object(shadow_bets, "load_cost_model", lambda asset: (None, "nofirm")):
            out = shadow_bets.apply_broker_costs({**LONG, "r": 2.0})
        self.assertEqual((out["r"], out["cost_basis"]), (2.0, "gross_no_cost_model"))


class TestOffIsNotPermanent(unittest.TestCase):
    def test_shadow_wins_bring_an_off_edge_back(self):
        backtest = np.array([2.0] * 360 + [-1.0] * 640)
        losing = np.array([2.0] * 25 + [-1.0] * 75)
        off = decommission_rule.decide("X/e_long", losing, backtest, CFG, "live")
        self.assertEqual(off.verdict, "off")
        recovered = decommission_rule.decide("X/e_long", np.concatenate([losing, [2.0] * 60 + [-1.0] * 60]), backtest, CFG, "live")
        self.assertNotEqual(recovered.verdict, "off")
        self.assertGreater(recovered.allocation, 0.0)


class TestBotAllocationHelper(unittest.TestCase):
    def _with(self, allocations=None, error=None):
        def fake(*_a, **_k):
            if error:
                raise error
            return allocations
        # patch the module object the bot's `import decommission_rule` resolves to
        return mock.patch.object(sys.modules["decommission_rule"], "live_allocations", fake)

    def test_off_edge(self):
        with self._with({"X/e_long": {"verdict": "off", "allocation": 0.0}}):
            self.assertEqual(BOT.edge_decommission_allocation({"edge_id": "X/e_long"}), (0.0, "decommission_off"))

    def test_reduced_edge(self):
        with self._with({"X/e_long": {"verdict": "reduced", "allocation": 0.4}}):
            self.assertEqual(BOT.edge_decommission_allocation({"edge_id": "X/e_long"}), (0.4, "ok"))

    def test_insufficient_sample_trades_full(self):
        with self._with({"X/e_long": {"verdict": "insufficient_sample", "allocation": 1.0}}):
            self.assertEqual(BOT.edge_decommission_allocation({"edge_id": "X/e_long"}), (1.0, "ok"))

    def test_rule_failure_fails_closed_and_is_retried(self):
        with self._with(error=RuntimeError("boom")):
            alloc, reason = BOT.edge_decommission_allocation({"edge_id": "X/e_long"})
        self.assertEqual(alloc, 0.0)
        self.assertTrue(reason.startswith("decommission_rule_unavailable"))

    def test_unknown_edge_fails_closed(self):
        with self._with({}):
            self.assertEqual(BOT.edge_decommission_allocation({"edge_id": "X/e_long"})[0], 0.0)


class TestBuildOrderPlanOffPath(unittest.TestCase):
    def test_off_edge_records_shadow_and_places_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(shadow_bets, "PENDING_PATH", Path(tmp) / "pending.jsonl"), \
                mock.patch.object(BOT, "edge_decommission_allocation", lambda s: (0.0, "decommission_off")):
            plan, reason = BOT.build_order_plan(None, dict(LONG), {}, {}, {}, None, {})
            self.assertIsNone(plan)
            self.assertEqual(reason, "decommission_off_shadow_tracking")
            self.assertIn('"signal_id": "s1"', (Path(tmp) / "pending.jsonl").read_text(encoding="utf-8"))


class TestSizingScales(unittest.TestCase):
    def test_allocation_multiplies_target_risk(self):
        manifest = {"risk_caps": {"base_portfolio_risk_budget_pct": 0.01, "max_single_trade_risk_pct": 0.02,
                                  "max_total_open_risk_pct": 0.05, "max_asset_open_risk_pct": 0.05,
                                  "max_family_open_risk_pct": 0.05, "max_edge_open_risk_pct": 0.05}}
        signal = {"edge_id": "X/e_long", "asset": "X", "family": "e", "portfolio_weight": 0.5, "afml_bet_size": 0.8}
        account = types.SimpleNamespace(balance=10000.0, equity=10000.0)
        with mock.patch.object(BOT, "throttle_multiplier", lambda *a: (1.0, "ok")), \
                mock.patch.object(BOT, "target_lock_multiplier", lambda *a: (1.0, "ok")):
            full = BOT.compute_candidate_sizing(signal, manifest, {}, account, {}, 1.0)
            reduced = BOT.compute_candidate_sizing(signal, manifest, {}, account, {}, 0.4)
        self.assertAlmostEqual(reduced.target_risk_pct_before_cap / full.target_risk_pct_before_cap, 0.4)
        self.assertEqual(reduced.decommission_allocation, 0.4)


if __name__ == "__main__":
    unittest.main()
