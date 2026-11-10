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

from shared.edges.primary_side_continuations import (  # noqa: E402
    create_primary_side_provider,
)
from shared.edges.primary_side_continuations.providers import trend  # noqa: E402


def _load_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(
        name,
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(rows: int = 900) -> pd.DataFrame:
    i = np.arange(rows, dtype="float64")

    log_step = (
        0.00035 * np.sin(i / 11.0)
        + 0.00022 * np.cos(i / 23.0)
        + np.where(
            (i.astype("int64") // 120) % 2 == 0,
            0.00008,
            -0.00007,
        )
    )
    close = np.exp(
        np.log(100.0) + np.cumsum(log_step)
    )

    close_ffd = (
        0.8 * np.sin(i / 17.0)
        + 0.35 * np.cos(i / 31.0)
        + np.where(
            (i.astype("int64") // 140) % 2 == 0,
            i * 0.0007,
            -i * 0.00055,
        )
    )

    dt = pd.date_range(
        "2026-01-01T00:00:00Z",
        periods=rows,
        freq="min",
    )
    return pd.DataFrame(
        {
            "Datetime": dt,
            "Close": close,
            "Close_FFD": close_ffd,
        }
    )


class TrendPrimaryProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )
        cls.by_primary_signature = {}
        for edge in cls.d1.get("edges") or []:
            primary = edge.get("primary_side") or {}
            signature = str(
                primary.get("primary_logic_signature") or ""
            )
            cls.by_primary_signature.setdefault(
                signature,
                [],
            ).append(edge)

    def _source_provider(self, signature: str):
        if signature not in self.by_primary_signature:
            raise unittest.SkipTest(
                f"no live edges currently use trend primary signature {signature[:12]}..."
            )
        edge = self.by_primary_signature[signature][0]
        primary = edge["primary_side"]
        module = _load_source(
            ROOT / primary["path"],
            "d4_primary_source_" + signature[:12],
        )
        provider = create_primary_side_provider(
            signature,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        return edge, module, provider

    def _assert_context_replay(self, signature: str):
        _edge, module, provider = self._source_provider(
            signature
        )
        features = _features()

        expected = (
            module.build_primary_side_context(
                features.copy()
            )[trend.CONTEXT_COLUMNS]
            .reset_index(drop=True)
        )
        actual = (
            provider.replay(features.copy())
            .reset_index(drop=True)
        )

        self.assertEqual(
            list(expected.columns),
            list(actual.columns),
        )
        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
            rtol=5e-12,
            atol=5e-12,
        )

    def test_long_source_context_replay(self):
        self._assert_context_replay(
            trend.TREND_LONG_PRIMARY_SIGNATURE
        )

    def test_short_source_context_replay(self):
        self._assert_context_replay(
            trend.TREND_SHORT_PRIMARY_SIGNATURE
        )

    def test_bootstrap_then_update_matches_source(self):
        signature = trend.TREND_LONG_PRIMARY_SIGNATURE
        _edge, module, provider = self._source_provider(
            signature
        )
        features = _features(950)

        provider.bootstrap(features.iloc[:900])
        rows = []
        for _, row in features.iloc[900:].iterrows():
            rows.append(provider.update_one(row))

        actual = pd.DataFrame(
            rows,
            columns=trend.CONTEXT_COLUMNS,
        ).reset_index(drop=True)

        expected = (
            module.build_primary_side_context(
                features.copy()
            )[trend.CONTEXT_COLUMNS]
            .iloc[900:]
            .reset_index(drop=True)
        )

        pd.testing.assert_frame_equal(
            expected,
            actual,
            check_dtype=False,
            check_exact=False,
            rtol=5e-12,
            atol=5e-12,
        )

    def _assert_filter_event_parity(
        self,
        signature: str,
    ):
        edge, module, _provider = self._source_provider(
            signature
        )
        features = _features(1000)
        context = module.build_primary_side_context(
            features.copy()
        )

        side = int(edge["side_value"])
        accepted = context.index[
            context["primary_side"].eq(side)
        ]
        accepted = [
            int(index)
            for index in accepted
            if int(index) > 100
        ]
        self.assertTrue(accepted)
        index = accepted[-1]

        provider = create_primary_side_provider(
            signature,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        prefix = features.iloc[: index + 1].copy()
        provider.bootstrap(prefix)

        event = {
            "Datetime": prefix.iloc[-1]["Datetime"],
            "edge_id": edge["edge_id"],
            "side": side,
            "event_id": (
                edge["edge_id"]
                + "|synthetic_raw_event"
            ),
            "candidate_event_generator": (
                "synthetic_raw_candidate"
            ),
        }

        primary = edge["primary_side"]
        runtime_calls = (
            primary.get("runtime_called_functions")
            or []
        )
        filter_names = [
            name
            for name in runtime_calls
            if name.startswith("filter_")
        ]
        self.assertEqual(len(filter_names), 1)
        source_filter = getattr(
            module,
            filter_names[0],
        )

        expected = source_filter(
            pd.DataFrame([event]),
            prefix,
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
            rtol=5e-12,
            atol=5e-12,
        )

        # D4.2C must not perform the runtime wrapper's composed generator rename.
        self.assertEqual(
            actual_event["candidate_event_generator"],
            "synthetic_raw_candidate",
        )

    def test_long_filter_event_matches_source(self):
        self._assert_filter_event_parity(
            trend.TREND_LONG_PRIMARY_SIGNATURE
        )

    def test_short_filter_event_matches_source(self):
        self._assert_filter_event_parity(
            trend.TREND_SHORT_PRIMARY_SIGNATURE
        )

    def test_rejected_event_returns_none(self):
        signature = trend.TREND_LONG_PRIMARY_SIGNATURE
        edge, module, _provider = self._source_provider(
            signature
        )
        features = _features(700)
        context = module.build_primary_side_context(
            features.copy()
        )

        rejected = context.index[
            context["primary_side"].eq(0)
        ]
        rejected = [
            int(index)
            for index in rejected
            if int(index) > 100
        ]
        self.assertTrue(rejected)
        index = rejected[-1]

        provider = create_primary_side_provider(
            signature,
            d1_edge=edge,
            v2_root=str(ROOT),
        )
        prefix = features.iloc[: index + 1]
        provider.bootstrap(prefix)

        event = {
            "Datetime": prefix.iloc[-1]["Datetime"],
            "edge_id": edge["edge_id"],
            "side": int(edge["side_value"]),
            "event_id": "rejected",
        }
        self.assertIsNone(
            provider.filter_event(event)
        )

    def test_state_is_bounded_after_long_replay(self):
        signature = trend.TREND_LONG_PRIMARY_SIGNATURE
        _edge, _module, provider = self._source_provider(
            signature
        )
        provider.replay(_features(12000))
        summary = provider.state_summary()

        self.assertEqual(
            summary["ffd_history_rows"],
            summary["trend_window"],
        )
        self.assertEqual(
            summary["close_history_rows"],
            summary["return_lookback"],
        )
        self.assertEqual(
            summary["processed_rows"],
            12000,
        )
        self.assertTrue(
            summary["kalman_initialized"]
        )
        self.assertFalse(
            summary["normal_path_ffd_disk_reads"]
        )
        self.assertFalse(
            summary["normal_path_history_disk_reads"]
        )
        self.assertFalse(
            summary["normal_path_full_primary_fallback"]
        )


if __name__ == "__main__":
    unittest.main()
