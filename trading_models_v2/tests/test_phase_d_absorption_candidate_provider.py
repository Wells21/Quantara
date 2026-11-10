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
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from shared.edges.candidate_continuations import create_candidate_provider
from shared.edges.candidate_continuations.providers import absorption


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 1400) -> pd.DataFrame:
    i = np.arange(rows, dtype="float64")
    close = 100.02 + 0.015 * np.sin(i / 11.0) + 0.008 * np.cos(i / 19.0)
    open_ = 100.00 + 0.004 * np.sin(i / 7.0)
    high = np.full(rows, 100.20)
    low = np.full(rows, 98.80)
    atr = np.full(rows, 1.0)
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
            "ATR_14": atr,
            "NATR": atr / close,
            "Regime_State": (np.arange(rows) % 4).astype(float),
            "Regime_Entropy": 0.5,
            "Regime_Duration": (np.arange(rows) % 20).astype(float),
            "Prob_State_0": 0.1,
            "Prob_State_1": 0.2,
            "Prob_State_2": 0.3,
            "Prob_State_3": 0.4,
        }
    )


class AbsorptionCandidateProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        cls.edges = [
            edge
            for edge in cls.d1.get("edges") or []
            if str(
                (edge.get("candidate") or {}).get(
                    "candidate_logic_signature"
                )
                or ""
            )
            in absorption.PROVIDER_SPEC["supported_signatures"]
        ]
        if not cls.edges:
            raise unittest.SkipTest(
                "no live edges currently use the absorption_long candidate family"
            )

    def _pair(self):
        edge = self.edges[0]
        candidate = edge["candidate"]
        module = _load_source(
            ROOT / candidate["path"],
            "d4_absorption_oracle",
        )
        entry_names = candidate.get("runtime_entry_functions") or []
        self.assertEqual(len(entry_names), 1)
        provider = create_candidate_provider(
            absorption.ABSORPTION_LONG_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return getattr(module, entry_names[0]), provider

    def test_all_current_members_bind_one_provider(self):
        for edge in self.edges:
            provider = create_candidate_provider(
                (edge.get("candidate") or {}).get("candidate_logic_signature"),
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(provider.identity.edge_id, edge["edge_id"])

    def test_source_replay_parity(self):
        oracle, provider = self._pair()
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
        _oracle, continuous = self._pair()
        _oracle, restarted = self._pair()
        features = _features(1600)

        all_events = continuous.replay(features)
        expected = all_events[
            all_events["Datetime"].isin(features["Datetime"].iloc[1300:])
        ].reset_index(drop=True)

        restarted.bootstrap(features.iloc[:1300])
        rows = []
        for _, row in features.iloc[1300:].iterrows():
            event = restarted.update_one(row)
            if event is not None:
                rows.append(event)

        actual = pd.DataFrame(
            rows,
            columns=list(all_events.columns),
        ).reset_index(drop=True)

        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
            rtol=2e-11,
            atol=2e-11,
        )

    def test_state_is_bounded_and_no_fallback(self):
        _oracle, provider = self._pair()
        provider.replay(_features(5000))
        state = provider.state_summary()
        self.assertLessEqual(
            state["high_history_rows"],
            provider.liquidity_lookback,
        )
        self.assertLessEqual(
            state["low_history_rows"],
            provider.liquidity_lookback,
        )
        self.assertLessEqual(
            state["close_history_rows"],
            provider.event_vol_lookback,
        )
        self.assertFalse(state["normal_path_history_disk_reads"])
        self.assertFalse(state["normal_path_full_candidate_fallback"])


if __name__ == "__main__":
    unittest.main()
