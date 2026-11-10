from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from tests.test_live_tick_cursor_safety import FakeLogger, load_module


def sample_bar(stamp: str, *, close: float = 100.0) -> dict:
    ts = pd.Timestamp(stamp)
    return {
        "Datetime": ts.isoformat(),
        "Bar_Start": (ts - pd.Timedelta(seconds=10)).isoformat(),
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


class LiveBarMemoryAuthorityTests(unittest.TestCase):
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
        self.m.settings.LIVE_BARS_DIR.mkdir(parents=True, exist_ok=True)
        with self.m._LIVE_BAR_MEMORY_LOCK:
            self.m._LIVE_BAR_MEMORY_BY_ASSET.clear()
            self.m._RUNTIME_BAR_MEMORY_BY_ASSET.clear()
            self.m._LIVE_BAR_DAILY_TICK_FALLBACK_BY_ASSET.clear()
            self.m._LIVE_BAR_MEMORY_INITIALIZED_ASSETS.clear()
            self.m._LIVE_BAR_MEMORY_STATS.clear()
        self.m.live_streaming_state.clear_live_bar_authority_reader()
        self.state = self.m.initial_state()
        self.logger = FakeLogger()
        self.seed_patcher = mock.patch.object(
            self.m,
            "load_seed_bars_for_asset",
            return_value=pd.DataFrame(columns=self.m.REQUIRED_BAR_COLUMNS),
        )
        self.seed_patcher.start()

    def tearDown(self):
        self.seed_patcher.stop()
        self.m.live_streaming_state.clear_live_bar_authority_reader()
        self.m.PENDING_LIVE_BAR_DIR = self.old_pending
        self.m.settings.LIVE_BARS_DIR = self.old_bars
        with self.m._LIVE_BAR_MEMORY_LOCK:
            self.m._LIVE_BAR_MEMORY_BY_ASSET.clear()
            self.m._RUNTIME_BAR_MEMORY_BY_ASSET.clear()
            self.m._LIVE_BAR_DAILY_TICK_FALLBACK_BY_ASSET.clear()
            self.m._LIVE_BAR_MEMORY_INITIALIZED_ASSETS.clear()
            self.m._LIVE_BAR_MEMORY_STATS.clear()
        self.temp.cleanup()

    def _write_canonical(self, asset: str, records: list[dict]) -> Path:
        path = self.m.live_bars_path(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(records).to_csv(path, index=False)
        return path

    def test_startup_reconstructs_checkpoint_plus_pending_once_then_reads_memory(self):
        asset = "XAUUSD"
        self._write_canonical(
            asset,
            [
                sample_bar("2026-07-02T06:00:00Z", close=100.0),
                sample_bar("2026-07-02T06:10:00Z", close=101.0),
            ],
        )
        self.m.journal_live_bar_record(
            asset,
            sample_bar("2026-07-02T06:20:00Z", close=102.0),
        )

        report = self.m.initialize_live_bar_memory_authority(
            [asset],
            {"bar_policies": {asset: {}}},
            self.state,
            self.logger,
        )
        self.assertEqual(report["assets"][asset]["rows"], 3)

        with mock.patch.object(
            self.m.pd,
            "read_csv",
            side_effect=AssertionError("memory read must not touch canonical CSV"),
        ):
            first = self.m.load_live_bars(asset)
            second = self.m.load_live_bars(asset)

        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertEqual(float(second.iloc[-1]["Close"]), 102.0)

    def test_durable_journal_precedes_memory_append_and_no_inline_compaction(self):
        asset = "BTCUSD"
        self.m.initialize_live_bar_memory_authority(
            [asset],
            {"bar_policies": {asset: {}}},
            self.state,
            self.logger,
        )
        order = []
        real_journal = self.m.journal_live_bar_record
        real_memory_append = self.m._append_live_bar_to_memory_authority

        def journal(*args, **kwargs):
            order.append("journal")
            return real_journal(*args, **kwargs)

        def memory_append(*args, **kwargs):
            order.append("memory")
            return real_memory_append(*args, **kwargs)

        with (
            mock.patch.object(self.m, "journal_live_bar_record", side_effect=journal),
            mock.patch.object(
                self.m,
                "_append_live_bar_to_memory_authority",
                side_effect=memory_append,
            ),
            mock.patch.object(
                self.m,
                "compact_pending_live_bars",
                side_effect=AssertionError("bar close path must not compact CSV"),
            ),
        ):
            self.m.append_live_bar(
                asset,
                sample_bar("2026-07-02T06:30:00Z", close=103.0),
                state=self.state,
                logger=self.logger,
            )

        self.assertEqual(order, ["journal", "memory"])
        self.assertEqual(len(self.m.load_live_bars(asset)), 1)
        self.assertEqual(
            len(list(self.m.pending_live_bar_asset_dir(asset).glob("*.json"))),
            1,
        )

    def test_ring_is_bounded_to_runtime_memory_rows(self):
        asset = "NASDAQ"
        count = int(self.m.LIVE_BAR_MEMORY_RING_ROWS) + 5
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        records = [
            sample_bar(
                (start + pd.Timedelta(minutes=i)).isoformat(),
                close=100.0 + i,
            )
            for i in range(count)
        ]
        self._write_canonical(asset, records)
        self.m.initialize_live_bar_memory_authority(
            [asset],
            {"bar_policies": {asset: {}}},
            self.state,
            self.logger,
        )
        frame = self.m.load_live_bars(asset)
        self.assertEqual(len(frame), self.m.LIVE_BAR_MEMORY_RING_ROWS)

    def test_shared_ffd_hotpath_uses_installed_memory_reader(self):
        asset = "XAUUSD"
        self._write_canonical(
            asset,
            [sample_bar("2026-07-02T06:00:00Z", close=100.0)],
        )
        self.m.initialize_live_bar_memory_authority(
            [asset],
            {"bar_policies": {asset: {}}},
            self.state,
            self.logger,
        )

        hot_state = self.m.live_streaming_state
        original_ffd_module = hot_state.FFD_MODULE

        class FFDShim:
            @staticmethod
            def _normalize_bar_table(frame, source):
                del source
                out = frame.copy()
                out["Datetime"] = pd.to_datetime(
                    out["Datetime"], utc=True, errors="coerce"
                )
                return out.dropna(subset=["Datetime"]).reset_index(drop=True)

        try:
            hot_state.FFD_MODULE = FFDShim
            with mock.patch.object(
                hot_state,
                "normalized_live_bars_disk",
                side_effect=AssertionError("hotpath must use memory provider"),
            ):
                frame = hot_state.normalized_live_bars(asset)
        finally:
            hot_state.FFD_MODULE = original_ffd_module

        self.assertEqual(len(frame), 1)
        self.assertIn("Close", frame.columns)

    def test_normal_combined_reader_uses_prebuilt_runtime_ring(self):
        asset = "XAUUSD"
        seed = pd.DataFrame(
            [sample_bar("2026-07-02T06:00:00Z", close=100.0)]
        )
        live = pd.DataFrame(
            [sample_bar("2026-07-02T06:10:00Z", close=101.0)]
        )
        self.m._memory_authority_record(
            asset,
            live,
            self.m._bounded_runtime_ring(seed, live, asset=asset),
        )

        with (
            mock.patch.object(
                self.m,
                "load_seed_bars_for_asset",
                side_effect=AssertionError("normal snapshot must not rebuild seed"),
            ),
            mock.patch.object(
                self.m,
                "_load_live_bars_disk_authority",
                side_effect=AssertionError("normal snapshot must not read disk"),
            ),
        ):
            bounded = self.m.combined_bars_for_asset(
                asset,
                {},
                max_rows=6000,
            )

        self.assertEqual(len(bounded), 2)
        self.assertEqual(float(bounded.iloc[-1]["Close"]), 101.0)

    def test_full_history_consumer_keeps_explicit_disk_authority(self):
        asset = "XAUUSD"
        disk_full = pd.DataFrame(
            [
                sample_bar("2026-07-02T06:00:00Z", close=100.0),
                sample_bar("2026-07-02T06:10:00Z", close=101.0),
            ]
        )
        seed = pd.DataFrame(columns=self.m.REQUIRED_BAR_COLUMNS)

        with (
            mock.patch.object(
                self.m,
                "load_seed_bars_for_asset",
                return_value=seed,
            ),
            mock.patch.object(
                self.m,
                "_load_live_bars_disk_authority",
                return_value=disk_full,
            ) as disk_reader,
        ):
            full = self.m.combined_bars_for_asset(
                asset,
                {},
                max_rows=None,
            )

        self.assertEqual(len(full), 2)
        disk_reader.assert_called_once()

    def test_tick_loop_contains_no_opportunistic_canonical_compaction(self):
        source = inspect.getsource(self.m.run_once)
        self.assertNotIn("compact_pending_live_bars(", source)

    def test_normal_dashboard_loop_contains_no_canonical_bar_compaction(self):
        source = inspect.getsource(self.m.main)
        after_start = source.split("closed_since_save = 0", 1)[1]
        normal_loop = after_start.split("except KeyboardInterrupt", 1)[0]
        self.assertNotIn("compact_all_pending_live_bars(", normal_loop)

    def test_periodic_threshold_profile_uses_memory_daily_cache_without_disk(self):
        asset = "XAUUSD"
        self._write_canonical(
            asset,
            [
                sample_bar("2026-07-02T06:00:00Z", close=100.0),
                sample_bar("2026-07-02T06:10:00Z", close=101.0),
            ],
        )
        self.m.initialize_live_bar_memory_authority(
            [asset],
            {"bar_policies": {asset: {}}},
            self.state,
            self.logger,
        )
        self.m.append_live_bar(
            asset,
            sample_bar("2026-07-02T06:20:00Z", close=102.0),
            state=self.state,
            logger=self.logger,
        )

        # Freeze "now" the day after the fixture bars: the profile drops days older than
        # ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS, so a wall-clock now made this test fail once 2026-07-02 aged out.
        with mock.patch.object(
            self.m,
            "combined_bars_for_asset",
            side_effect=AssertionError(
                "periodic threshold refresh must not rebuild full bar history"
            ),
        ), mock.patch.object(
            self.m,
            "utc_now_ts",
            return_value=pd.Timestamp("2026-07-03T12:00:00Z"),
        ):
            profile = self.m.completed_daily_tick_profile_from_bars(
                asset,
                {},
                self.state,
            )

        row = next(item for item in profile if item["date"] == "2026-07-02")
        self.assertEqual(row["tick_count"], 300)
        self.assertEqual(row["source"], "completed_bar_tick_sum_fallback")

    def test_recovery_contract_uses_disk_reader_and_forced_compaction(self):
        root = Path(__file__).resolve().parents[1]
        source = (
            root / "portfolio_live" / "production_snapshot_pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("normalized_live_bars_disk", source)
        self.assertIn('ns["compact_pending_live_bars"](', source)
        self.assertIn("force=True", source)


if __name__ == "__main__":
    unittest.main()
