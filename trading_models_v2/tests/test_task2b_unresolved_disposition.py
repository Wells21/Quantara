from __future__ import annotations

import unittest

import pandas as pd

from shared.execution.unresolved_event_disposition import (
    DATA_DEFECT,
    RESOLVED,
    RIGHT_CENSORED_END_OF_HISTORY,
    TRUE_NO_FILL,
    UnresolvedDispositionError,
    annotate_dispositions,
    classify_disposition,
    inside_utc_blackout,
    summarize_dispositions,
    weekend_gap_blocks_entry,
)


def _catalog(end_dates: list[str]) -> list[dict]:
    return [{"_end": pd.Timestamp(d, tz="UTC")} for d in end_dates]


class TestFixedStatusDispositions(unittest.TestCase):
    """Statuses whose disposition never depends on calendar/catalog context."""

    def _classify(self, status: str) -> str:
        return classify_disposition(
            status,
            asset="BTCUSD",
            event_time=pd.Timestamp("2026-01-05T12:00:00Z"),  # a Monday
            vertical_deadline=None,
            catalog=_catalog(["2026-01-10T00:00:00Z"]),
            weekend_entry_guard_by_asset={},
            entry_blackout_windows_utc_by_asset={},
        )

    def test_resolved_passes_through(self):
        self.assertEqual(self._classify("resolved"), RESOLVED)

    def test_stale_entry_quote_is_true_no_fill(self):
        self.assertEqual(self._classify("stale_entry_quote"), TRUE_NO_FILL)

    def test_vertical_deadline_bar_not_found_is_right_censored(self):
        self.assertEqual(
            self._classify("vertical_deadline_bar_not_found"),
            RIGHT_CENSORED_END_OF_HISTORY,
        )

    def test_data_defect_statuses(self):
        for status in (
            "event_bar_not_found",
            "invalid_signal_mid",
            "research_entry_signal_mid_mismatch",
            "invalid_barrier_distance",
        ):
            with self.subTest(status=status):
                self.assertEqual(self._classify(status), DATA_DEFECT)

    def test_unknown_status_refuses_to_guess(self):
        with self.assertRaises(UnresolvedDispositionError):
            self._classify("some_new_status_the_resolver_does_not_document_yet")


class TestWeekendGapArithmetic(unittest.TestCase):
    def test_friday_after_cutoff_blocks(self):
        # Friday 2026-01-02, 21:00 UTC >= friday_block_hour_utc=20.0
        guard = {"XAUUSD": {"friday_block_hour_utc": 20.0, "monday_resume_hour_utc": 0.0}}
        reason = weekend_gap_blocks_entry(
            "XAUUSD", pd.Timestamp("2026-01-02T21:00:00Z"), guard
        )
        self.assertIsNotNone(reason)

    def test_saturday_blocks(self):
        guard = {"XAUUSD": {"friday_block_hour_utc": 20.0, "monday_resume_hour_utc": 0.0}}
        reason = weekend_gap_blocks_entry(
            "XAUUSD", pd.Timestamp("2026-01-03T12:00:00Z"), guard  # a Saturday
        )
        self.assertIsNotNone(reason)

    def test_monday_after_resume_does_not_block(self):
        guard = {"XAUUSD": {"friday_block_hour_utc": 20.0, "monday_resume_hour_utc": 0.0}}
        reason = weekend_gap_blocks_entry(
            "XAUUSD", pd.Timestamp("2026-01-05T01:00:00Z"), guard  # Monday 01:00
        )
        self.assertIsNone(reason)

    def test_asset_with_no_configured_guard_never_blocks(self):
        # e.g. BTCUSD trades weekends; deliberately absent from the guard dict.
        reason = weekend_gap_blocks_entry(
            "BTCUSD", pd.Timestamp("2026-01-03T12:00:00Z"), {}
        )
        self.assertIsNone(reason)

    def test_wednesday_daytime_never_blocks(self):
        guard = {"XAUUSD": {"friday_block_hour_utc": 20.0, "monday_resume_hour_utc": 0.0}}
        reason = weekend_gap_blocks_entry(
            "XAUUSD", pd.Timestamp("2026-01-07T12:00:00Z"), guard  # a Wednesday
        )
        self.assertIsNone(reason)


class TestBlackoutWindowArithmetic(unittest.TestCase):
    def test_inside_configured_window_blocks(self):
        windows = {
            "XAUUSD": [{"start": "21:55", "end": "22:05", "weekdays": list(range(7))}]
        }
        reason = inside_utc_blackout(
            "XAUUSD", pd.Timestamp("2026-01-05T22:00:00Z"), windows
        )
        self.assertIsNotNone(reason)

    def test_outside_configured_window_does_not_block(self):
        windows = {
            "XAUUSD": [{"start": "21:55", "end": "22:05", "weekdays": list(range(7))}]
        }
        reason = inside_utc_blackout(
            "XAUUSD", pd.Timestamp("2026-01-05T10:00:00Z"), windows
        )
        self.assertIsNone(reason)


class TestAmbiguousEntrySideDisambiguation(unittest.TestCase):
    def test_market_closure_wins_even_near_end_of_catalog(self):
        # Saturday event, catalog also happens to end right around then --
        # weekend/blackout must take priority over the end-of-history check.
        result = classify_disposition(
            "missing_entry_quote",
            asset="XAUUSD",
            event_time=pd.Timestamp("2026-01-03T12:00:00Z"),  # Saturday
            vertical_deadline=None,
            catalog=_catalog(["2026-01-03T13:00:00Z"]),
            weekend_entry_guard_by_asset={
                "XAUUSD": {"friday_block_hour_utc": 20.0, "monday_resume_hour_utc": 0.0}
            },
            entry_blackout_windows_utc_by_asset={},
        )
        self.assertEqual(result, TRUE_NO_FILL)

    def test_end_of_collected_history_is_right_censored(self):
        result = classify_disposition(
            "missing_entry_quote",
            asset="BTCUSD",  # no weekend guard configured for BTCUSD
            event_time=pd.Timestamp("2026-01-10T00:00:00Z"),
            vertical_deadline=None,
            catalog=_catalog(["2026-01-09T00:00:00Z"]),  # catalog ends BEFORE event
            weekend_entry_guard_by_asset={},
            entry_blackout_windows_utc_by_asset={},
        )
        self.assertEqual(result, RIGHT_CENSORED_END_OF_HISTORY)

    def test_unexplained_gap_despite_nominal_coverage_is_data_defect(self):
        result = classify_disposition(
            "missing_entry_quote",
            asset="BTCUSD",
            event_time=pd.Timestamp("2026-01-05T12:00:00Z"),  # a Monday, no guard
            vertical_deadline=None,
            catalog=_catalog(["2026-01-10T00:00:00Z"]),  # catalog extends well past
            weekend_entry_guard_by_asset={},
            entry_blackout_windows_utc_by_asset={},
        )
        self.assertEqual(result, DATA_DEFECT)


class TestAmbiguousExitSideDisambiguation(unittest.TestCase):
    def test_missing_deadline_refuses_to_guess(self):
        with self.assertRaises(UnresolvedDispositionError):
            classify_disposition(
                "unresolved_tick_coverage",
                asset="BTCUSD",
                event_time=pd.Timestamp("2026-01-05T12:00:00Z"),
                vertical_deadline=None,
                catalog=_catalog(["2026-01-10T00:00:00Z"]),
                weekend_entry_guard_by_asset={},
                entry_blackout_windows_utc_by_asset={},
            )

    def test_deadline_past_catalog_end_is_right_censored(self):
        result = classify_disposition(
            "unresolved_tick_coverage",
            asset="BTCUSD",
            event_time=pd.Timestamp("2026-01-05T12:00:00Z"),
            vertical_deadline=pd.Timestamp("2026-01-11T00:00:00Z"),
            catalog=_catalog(["2026-01-10T00:00:00Z"]),
            weekend_entry_guard_by_asset={},
            entry_blackout_windows_utc_by_asset={},
        )
        self.assertEqual(result, RIGHT_CENSORED_END_OF_HISTORY)

    def test_deadline_within_catalog_coverage_is_data_defect(self):
        result = classify_disposition(
            "unresolved_tick_coverage",
            asset="BTCUSD",
            event_time=pd.Timestamp("2026-01-05T12:00:00Z"),
            vertical_deadline=pd.Timestamp("2026-01-06T00:00:00Z"),
            catalog=_catalog(["2026-01-10T00:00:00Z"]),
            weekend_entry_guard_by_asset={},
            entry_blackout_windows_utc_by_asset={},
        )
        self.assertEqual(result, DATA_DEFECT)


class TestAnnotateAndSummarize(unittest.TestCase):
    def test_end_to_end_frame(self):
        df = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(
                    [
                        "2026-01-05T12:00:00Z",  # resolved
                        "2026-01-05T12:05:00Z",  # stale_entry_quote -> TRUE_NO_FILL
                        "2026-01-10T00:00:00Z",  # vertical_deadline_bar_not_found -> RIGHT_CENSORED
                    ]
                ),
                "vertical_deadline": pd.to_datetime([pd.NaT, pd.NaT, pd.NaT]),
                "execution_status": [
                    "resolved",
                    "stale_entry_quote",
                    "vertical_deadline_bar_not_found",
                ],
            }
        )
        annotated = annotate_dispositions(
            df,
            asset="BTCUSD",
            catalog=_catalog(["2026-01-09T00:00:00Z"]),
            calendar_guard_config={
                "weekend_entry_guard_by_asset": {},
                "entry_blackout_windows_utc_by_asset": {},
            },
        )
        self.assertEqual(
            annotated["task2b_disposition"].tolist(),
            [RESOLVED, TRUE_NO_FILL, RIGHT_CENSORED_END_OF_HISTORY],
        )
        summary = summarize_dispositions(annotated)
        self.assertEqual(summary["total_rows"], 3)
        self.assertEqual(summary["data_defect_count"], 0)
        self.assertEqual(summary["audit_status"], "PASS")

    def test_data_defect_flips_audit_status_to_fail(self):
        df = pd.DataFrame(
            {
                "Datetime": pd.to_datetime(["2026-01-05T12:00:00Z"]),
                "vertical_deadline": pd.to_datetime([pd.NaT]),
                "execution_status": ["invalid_signal_mid"],
            }
        )
        annotated = annotate_dispositions(
            df,
            asset="BTCUSD",
            catalog=_catalog(["2026-01-09T00:00:00Z"]),
            calendar_guard_config={
                "weekend_entry_guard_by_asset": {},
                "entry_blackout_windows_utc_by_asset": {},
            },
        )
        summary = summarize_dispositions(annotated)
        self.assertEqual(summary["data_defect_count"], 1)
        self.assertEqual(summary["audit_status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
