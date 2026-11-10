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
from shared.edges.candidate_continuations.providers import volatilitycarry


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 1800) -> pd.DataFrame:
    i = np.arange(rows, dtype="float64")
    block = (i.astype("int64") // 300) % 2
    direction = np.where(block == 0, 1.0, -1.0)
    step = direction * 0.00030 + 0.000025 * np.sin(i / 8.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    open_ = close / np.exp(0.4 * step)
    high = np.maximum(open_, close) * 1.0007
    low = np.minimum(open_, close) * 0.9993

    # Calm-volatility observations dominate so event gating is active,
    # with periodic higher-volatility interruptions for threshold dynamics.
    bar_vol = np.full(rows, 1.0)
    bar_vol[(i.astype("int64") % 11) == 0] = 1.8
    bar_vol[(i.astype("int64") % 7) == 0] = 0.7

    dt = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=rows,
        freq="min",
    )
    j = np.arange(rows, dtype="int64")
    return pd.DataFrame(
        {
            "Datetime": dt,
            "Bar_Start": dt,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Bar_Volatility_Bps": bar_vol,
            "Regime_State": (j % 4).astype(float),
            "Regime_Entropy": 0.5 + 0.01 * (j % 10),
            "Regime_Duration": (j % 20).astype(float),
            "Prob_State_0": 0.1,
            "Prob_State_1": 0.2,
            "Prob_State_2": 0.3,
            "Prob_State_3": 0.4,
        }
    )


class VolatilityCarryCandidateProviderTests(unittest.TestCase):
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
            in volatilitycarry.PROVIDER_SPEC["supported_signatures"]
        ]
        if not cls.edges:
            raise unittest.SkipTest(
                "no live edges currently use the volatilitycarry_long candidate family"
            )

    def _pair(self):
        edge = self.edges[0]
        candidate = edge["candidate"]
        module = _load_source(
            ROOT / candidate["path"],
            "d4_volatilitycarry_oracle",
        )
        entry_names = candidate.get("runtime_entry_functions") or []
        self.assertEqual(len(entry_names), 1)
        provider = create_candidate_provider(
            volatilitycarry.VOLATILITYCARRY_LONG_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return (
            edge,
            getattr(module, entry_names[0]),
            provider,
        )

    def test_all_members_bind_one_provider(self):
        for edge in self.edges:
            provider = create_candidate_provider(
                (edge.get("candidate") or {}).get("candidate_logic_signature"),
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(provider.identity.edge_id, edge["edge_id"])

    def test_source_replay_parity(self):
        _edge, oracle, provider = self._pair()
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
            rtol=3e-11,
            atol=3e-11,
        )

    def test_restart_matches_uninterrupted(self):
        _edge, _oracle, continuous = self._pair()
        _edge, _oracle, restarted = self._pair()
        features = _features(2000)

        all_events = continuous.replay(features)
        expected = all_events[
            all_events["Datetime"].isin(features["Datetime"].iloc[1650:])
        ].reset_index(drop=True)

        restarted.bootstrap(features.iloc[:1650])
        rows = []
        for _, row in features.iloc[1650:].iterrows():
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
            rtol=3e-11,
            atol=3e-11,
        )

    def test_state_is_bounded_and_no_fallback(self):
        _edge, _oracle, provider = self._pair()
        provider.replay(_features(5000))
        state = provider.state_summary()
        self.assertLessEqual(
            state["volatility_history_rows"],
            provider.vol_lookback,
        )
        self.assertLessEqual(
            state["bar_return_history_rows"],
            provider.drift_lookback,
        )
        self.assertLessEqual(
            state["close_history_rows"],
            # calendar-time event-vol history: pruned to a 2x lookback window on 1-minute synthetic bars
            int(round(2.0 * provider.event_vol_lookback_hours * 60.0)) + 1,
        )
        self.assertFalse(state["normal_path_history_disk_reads"])
        self.assertFalse(state["normal_path_full_candidate_fallback"])


if __name__ == "__main__":
    unittest.main()
