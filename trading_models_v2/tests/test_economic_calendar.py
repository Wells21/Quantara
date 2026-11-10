"""Economic event calendar (Stage 1, owner 2026-11-05): file integrity, times, fail-closed coverage, causality."""
import unittest

import numpy as np
import pandas as pd

from shared.features import economic_calendar as ec


class EconomicCalendarTest(unittest.TestCase):
    def test_file_integrity(self):
        doc, ev = ec.load_calendar()
        self.assertEqual(doc["schema"], "quantara_economic_event_calendar_v1")
        self.assertTrue(ev["t_ns"].is_monotonic_increasing)
        self.assertEqual(set(ev["type"]), set(ec.EVENT_TYPES))
        self.assertFalse(ev.duplicated(["type", "release_utc"]).any())
        local = ev["release_et"].str[11:16]
        self.assertTrue((local[ev["type"] == "FOMC"] == "14:00").all())
        self.assertTrue((local[ev["type"] != "FOMC"] == "08:30").all())
        # UTC field agrees with the New York local time (DST handled)
        et = pd.to_datetime(ev["release_et"].map(lambda s: pd.Timestamp(s).tz_convert("UTC")))
        self.assertTrue((et.dt.tz_convert("UTC") == pd.to_datetime(ev["release_utc"], utc=True)).all())

    def test_known_dates(self):
        _, ev = ec.load_calendar()
        got = set(zip(ev["type"], ev["release_et"].str[:10]))
        for item in [("FOMC", "2024-11-07"), ("CPI", "2025-10-24"), ("NFP", "2025-11-20"), ("NFP", "2022-02-04"),
                     ("CPI", "2027-12-10")]:
            self.assertIn(item, got)
        self.assertNotIn(("FOMC", "2025-08-22"), got)          # notation vote excluded

    def test_fail_closed_past_coverage(self):
        late = np.array([pd.Timestamp("2027-01-04", tz="UTC").value])
        with self.assertRaises(ec.CalendarCoverageError):
            ec.hours_to_next(late, "FOMC")
        ec.hours_to_next(late, "CPI")                          # CPI still covered

    def test_causal_features(self):
        ts = pd.date_range("2024-01-01", "2024-12-31", freq="7h", tz="UTC").as_unit("ns").asi8
        for kind in ec.EVENT_TYPES:
            nxt, last = ec.hours_to_next(ts, kind), ec.hours_since_last(ts, kind)
            self.assertTrue((nxt[np.isfinite(nxt)] > 0).all())
            self.assertTrue((last[np.isfinite(last)] >= 0).all())
        cpi = pd.Timestamp("2024-02-13T13:30:00Z").value
        self.assertEqual(ec.hours_since_last(np.array([cpi]), "CPI")[0], 0.0)
        self.assertAlmostEqual(ec.hours_to_next(np.array([cpi - 3_600_000_000_000]), "CPI")[0], 1.0)


if __name__ == "__main__":
    unittest.main()
