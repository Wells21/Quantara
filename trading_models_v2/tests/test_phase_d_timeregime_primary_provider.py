from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.primary_side_continuations import (
    create_primary_side_provider,
)
from shared.edges.primary_side_continuations.providers import timeregime


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TimeRegimePrimaryProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        cls.edges = [
            edge
            for edge in d1.get("edges") or []
            if str(
                (edge.get("primary_side") or {}).get(
                    "primary_logic_signature"
                )
                or ""
            )
            in timeregime.PROVIDER_SPEC["supported_signatures"]
        ]
        if not cls.edges:
            raise unittest.SkipTest(
                "no live edges currently use the timeregime_long primary-side family"
            )

    def test_all_d1_members_bind_same_provider(self):
        for edge in self.edges:
            provider = create_primary_side_provider(
                timeregime.TIMEREGIME_LONG_PRIMARY_SIGNATURE,
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(provider.identity.edge_id, edge["edge_id"])

    def test_accepted_event_matches_source(self):
        edge = self.edges[0]
        primary = edge["primary_side"]
        module = _load_source(
            ROOT / primary["path"],
            "d4_timeregime_primary_oracle",
        )
        provider = create_primary_side_provider(
            timeregime.TIMEREGIME_LONG_PRIMARY_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )

        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        provider.bootstrap(pd.DataFrame({"Datetime": [ts]}))
        event = {
            "Datetime": ts,
            "Bar_Start": ts,
            "edge_id": edge["edge_id"],
            "side": 1,
            "Event_Vol": 0.01,
            "event_id": "evt",
            "candidate_event_generator": "raw_time_regime",
            "Time_Regime_Event_Type": 1,
            "Time_Bias_TStat": 1.2,
            "Volatility_Ratio": 1.4,
            "Time_Bucket": "ny_overlap",
            "event_known_after_close": True,
            "uses_future_information": False,
        }

        filter_name = [
            name
            for name in primary["runtime_called_functions"]
            if name.startswith("filter_")
        ][0]
        expected = getattr(module, filter_name)(
            pd.DataFrame([event])
        ).reset_index(drop=True)

        actual_event = provider.filter_event(event)
        self.assertIsNotNone(actual_event)
        actual = pd.DataFrame(
            [actual_event],
            columns=list(expected.columns),
        )
        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
        )

    def test_negative_raw_event_is_rejected(self):
        edge = self.edges[0]
        provider = create_primary_side_provider(
            timeregime.TIMEREGIME_LONG_PRIMARY_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        provider.bootstrap(pd.DataFrame({"Datetime": [ts]}))
        event = {
            "Datetime": ts,
            "edge_id": edge["edge_id"],
            "side": -1,
            "Event_Vol": 0.01,
            "Time_Regime_Event_Type": -1,
            "Time_Bias_TStat": -1.2,
            "Volatility_Ratio": 1.4,
            "Time_Bucket": "ny_overlap",
        }
        self.assertIsNone(provider.filter_event(event))

    def test_state_no_disk_or_hidden_fallback(self):
        edge = self.edges[0]
        provider = create_primary_side_provider(
            timeregime.TIMEREGIME_LONG_PRIMARY_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        dt = pd.date_range(
            "2026-01-01T00:00:00Z",
            periods=1000,
            freq="min",
        )
        provider.bootstrap(pd.DataFrame({"Datetime": dt}))
        summary = provider.state_summary()
        self.assertEqual(summary["processed_rows"], 1000)
        self.assertFalse(summary["normal_path_ffd_disk_reads"])
        self.assertFalse(summary["normal_path_history_disk_reads"])
        self.assertFalse(summary["normal_path_full_primary_fallback"])


if __name__ == "__main__":
    unittest.main()
