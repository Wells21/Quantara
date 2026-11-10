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

from shared.edges.candidate_continuations import create_candidate_provider  # noqa: E402
from shared.edges.candidate_continuations.providers import cusum  # noqa: E402


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load oracle {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 900) -> pd.DataFrame:
    i = np.arange(rows, dtype="int64")
    log_step = np.where(i % 2 == 0, 0.0015, -0.0012)
    close = np.exp(np.log(100.0) + np.cumsum(log_step))
    dt = pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="min")
    return pd.DataFrame(
        {"Datetime": dt, "Bar_Start": dt, "Close": close}
    )


class RawCusumProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        cls.by_signature = {}
        for edge in d1.get("edges") or []:
            signature = str(
                (edge.get("candidate") or {}).get(
                    "candidate_logic_signature"
                )
                or ""
            )
            cls.by_signature.setdefault(signature, []).append(edge)

    def _oracle_provider(self, signature: str):
        if signature not in self.by_signature:
            raise unittest.SkipTest(
                f"no live edges currently use cusum signature {signature[:12]}..."
            )
        edge = self.by_signature[signature][0]
        candidate = edge["candidate"]
        entries = candidate.get("runtime_entry_functions") or []
        self.assertEqual(len(entries), 1)
        module = _load_source(
            ROOT / candidate["path"],
            "d4_oracle_" + signature[:12],
        )
        oracle = getattr(module, entries[0])
        provider = create_candidate_provider(
            signature,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return oracle, provider

    def _assert_replay(self, signature: str):
        oracle, provider = self._oracle_provider(signature)
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
            rtol=5e-12,
            atol=5e-12,
        )

    def test_positive_source_exact_replay(self):
        self._assert_replay(cusum.POSITIVE_CUSUM_SIGNATURE)

    def test_negative_source_exact_replay(self):
        self._assert_replay(cusum.NEGATIVE_CUSUM_SIGNATURE)

    def test_bootstrap_then_update_matches_uninterrupted_replay(self):
        signature = cusum.POSITIVE_CUSUM_SIGNATURE
        _oracle, continuous = self._oracle_provider(signature)
        _oracle, restarted = self._oracle_provider(signature)
        features = _features(950)

        all_events = continuous.replay(features)
        expected = all_events[
            all_events["Datetime"].isin(features["Datetime"].iloc[900:])
        ].reset_index(drop=True)

        restarted.bootstrap(features.iloc[:900])
        rows = []
        for _, row in features.iloc[900:].iterrows():
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
            rtol=5e-12,
            atol=5e-12,
        )

    def test_live_bar_index_uses_sliding_authority_coordinate(self):
        signature = cusum.POSITIVE_CUSUM_SIGNATURE
        _oracle, provider = self._oracle_provider(signature)
        features = _features(6010)
        provider.bootstrap(features.iloc[:6000])
        emitted = []
        for _, row in features.iloc[6000:].iterrows():
            event = provider.update_one(row)
            if event is not None:
                emitted.append(event)
        self.assertGreater(len(emitted), 0)
        self.assertTrue(
            all(int(event["bar_index"]) == 5999 for event in emitted)
        )


if __name__ == "__main__":
    unittest.main()
