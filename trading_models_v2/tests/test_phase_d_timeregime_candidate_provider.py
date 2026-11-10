from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import timeregime


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load oracle {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 1800) -> pd.DataFrame:
    i = np.arange(rows, dtype="int64")
    direction = np.where((i // 240) % 2 == 0, 1.0, -1.0)
    step = direction * 0.00045 + 0.00003 * np.sin(i / 9.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    open_ = close * np.exp(-0.45 * step)
    high = np.maximum(open_, close) * 1.0006
    low = np.minimum(open_, close) * 0.9994
    bar_vol = np.where(i % 7 == 0, 2.5, 1.0)
    dt = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=rows,
        freq="min",
    )
    return pd.DataFrame(
        {
            "Datetime": dt,
            "Bar_Start": dt,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Bar_Volatility_Bps": bar_vol,
            "Body_Share": np.abs(close - open_) / (high - low),
            "Regime_State": (i % 4).astype(float),
            "Regime_Entropy": 0.5 + 0.01 * (i % 10),
            "Regime_Duration": (i % 20).astype(float),
            "Prob_State_0": 0.1,
            "Prob_State_1": 0.2,
            "Prob_State_2": 0.3,
            "Prob_State_3": 0.4,
        }
    )


class TimeRegimeCandidateProviderTests(unittest.TestCase):
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
                (edge.get("candidate") or {}).get(
                    "candidate_logic_signature"
                )
                or ""
            )
            in timeregime.PROVIDER_SPEC["supported_signatures"]
        ]
        if not cls.edges:
            raise unittest.SkipTest(
                "no live edges currently use the timeregime_long candidate family"
            )

    def _oracle_provider(self):
        edge = self.edges[0]
        candidate = edge["candidate"]
        entry = candidate["runtime_entry_functions"]
        self.assertEqual(len(entry), 1)
        module = _load_source(
            ROOT / candidate["path"],
            "d4_timeregime_oracle",
        )
        provider = create_candidate_provider(
            timeregime.TIMEREGIME_LONG_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return getattr(module, entry[0]), provider

    def test_all_d1_members_bind_same_provider(self):
        for edge in self.edges:
            provider = create_candidate_provider(
                timeregime.TIMEREGIME_LONG_SIGNATURE,
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(provider.identity.edge_id, edge["edge_id"])

    def test_source_replay_parity(self):
        oracle, provider = self._oracle_provider()
        features = _features()
        expected = oracle(features.copy()).reset_index(drop=True)
        actual = provider.replay(features.copy()).reset_index(drop=True)
        self.assertGreater(len(expected), 0)
        self.assertEqual(list(expected.columns), list(actual.columns))
        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
            rtol=2e-11,
            atol=2e-11,
        )

    def test_restart_matches_uninterrupted(self):
        _oracle, continuous = self._oracle_provider()
        _oracle, restarted = self._oracle_provider()
        features = _features(1900)

        all_events = continuous.replay(features)
        expected = all_events[
            all_events["Datetime"].isin(features["Datetime"].iloc[1600:])
        ].reset_index(drop=True)

        restarted.bootstrap(features.iloc[:1600])
        rows = []
        for _, row in features.iloc[1600:].iterrows():
            event = restarted.update_one(row)
            if event is not None:
                rows.append(event)
        actual = pd.DataFrame(rows).reset_index(drop=True)

        self.assertEqual(list(expected.columns), list(actual.columns))
        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
            rtol=2e-11,
            atol=2e-11,
        )

    def test_state_is_bounded_and_no_fallback(self):
        _oracle, provider = self._oracle_provider()
        provider.replay(_features(5000))
        summary = provider.state_summary()
        self.assertLessEqual(summary["hour_bucket_count"], 24)
        self.assertLessEqual(
            summary["max_hour_history_rows"],
            provider.bias_window,
        )
        self.assertLessEqual(
            summary["volatility_history_rows"],
            100,
        )
        self.assertLessEqual(
            summary["close_history_rows"],
            provider.event_vol_lookback,
        )
        self.assertFalse(summary["normal_path_history_disk_reads"])
        self.assertFalse(summary["normal_path_full_candidate_fallback"])


if __name__ == "__main__":
    unittest.main()
