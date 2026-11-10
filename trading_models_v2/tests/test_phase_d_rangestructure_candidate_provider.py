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
from shared.edges.candidate_continuations.providers import rangestructure


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 1600) -> pd.DataFrame:
    i = np.arange(rows, dtype="float64")
    regime = np.where(
        ((i.astype("int64") // 260) % 2) == 0,
        1.0,
        -1.0,
    )
    step = regime * 0.00032 + 0.00004 * np.sin(i / 13.0)
    close = np.exp(np.log(100.0) + np.cumsum(step))
    open_ = close / np.exp(0.45 * step)
    high = np.maximum(open_, close) * 1.0008
    low = np.minimum(open_, close) * 0.9992

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
            "Regime_State": (j % 4).astype(float),
            "Regime_Entropy": 0.5 + 0.01 * (j % 10),
            "Regime_Duration": (j % 20).astype(float),
            "Prob_State_0": 0.1,
            "Prob_State_1": 0.2,
            "Prob_State_2": 0.3,
            "Prob_State_3": 0.4,
        }
    )


class RangeStructureCandidateProviderTests(unittest.TestCase):
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
            in rangestructure.PROVIDER_SPEC["supported_signatures"]
        ]
        if not cls.edges:
            raise unittest.SkipTest(
                "no live edges currently use the rangestructure_long candidate family"
            )

    def _pair(self):
        edge = self.edges[0]
        candidate = edge["candidate"]
        module = _load_source(
            ROOT / candidate["path"],
            "d4_rangestructure_oracle",
        )
        entry_names = candidate.get("runtime_entry_functions") or []
        self.assertEqual(len(entry_names), 1)
        provider = create_candidate_provider(
            rangestructure.RANGESTRUCTURE_LONG_SIGNATURE,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return getattr(module, entry_names[0]), provider

    def test_all_members_bind_one_provider(self):
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
            rtol=3e-11,
            atol=3e-11,
        )

    def test_restart_matches_uninterrupted(self):
        _oracle, continuous = self._pair()
        _oracle, restarted = self._pair()
        features = _features(1800)

        all_events = continuous.replay(features)
        expected = all_events[
            all_events["Datetime"].isin(features["Datetime"].iloc[1500:])
        ].reset_index(drop=True)

        restarted.bootstrap(features.iloc[:1500])
        rows = []
        for _, row in features.iloc[1500:].iterrows():
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
        _oracle, provider = self._pair()
        provider.replay(_features(5000))
        state = provider.state_summary()
        self.assertLessEqual(
            state["high_history_rows"], provider.range_lookback
        )
        self.assertLessEqual(
            state["low_history_rows"], provider.range_lookback
        )
        self.assertLessEqual(
            state["range_width_history_rows"], provider.range_lookback
        )
        self.assertLessEqual(
            state["bar_return_history_rows"],
            provider.structure_bias_lookback,
        )
        self.assertLessEqual(
            # calendar-time event-vol history: pruned to a 2x lookback window on 1-minute synthetic bars
            state["close_history_rows"], int(round(2.0 * provider.event_vol_lookback_hours * 60.0)) + 1
        )
        self.assertFalse(state["normal_path_history_disk_reads"])
        self.assertFalse(state["normal_path_full_candidate_fallback"])


if __name__ == "__main__":
    unittest.main()
