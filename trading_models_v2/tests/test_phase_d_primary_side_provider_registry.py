from __future__ import annotations

import inspect
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.primary_side_continuations import (  # noqa: E402
    PrimarySideContinuationError,
    create_primary_side_provider,
    discover_primary_side_providers,
)
from shared.edges.primary_side_continuations import registry  # noqa: E402
from shared.edges.primary_side_continuations.providers import trend  # noqa: E402


class PrimarySideProviderRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )

    def test_discovery_finds_both_trend_primary_signatures(self):
        providers = discover_primary_side_providers()
        self.assertIn(
            trend.TREND_LONG_PRIMARY_SIGNATURE,
            providers,
        )
        self.assertIn(
            trend.TREND_SHORT_PRIMARY_SIGNATURE,
            providers,
        )

    def test_binding_uses_d1_primary_logic_signature(self):
        by_signature = {}
        for edge in self.d1.get("edges") or []:
            primary = edge.get("primary_side") or {}
            signature = str(
                primary.get("primary_logic_signature") or ""
            )
            by_signature.setdefault(signature, []).append(edge)

        for signature in (
            trend.TREND_LONG_PRIMARY_SIGNATURE,
            trend.TREND_SHORT_PRIMARY_SIGNATURE,
        ):
            if signature not in by_signature:
                continue  # no live edge currently uses this trend side
            edge = by_signature[signature][0]
            provider = create_primary_side_provider(
                signature,
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(
                provider.identity.edge_id,
                edge["edge_id"],
            )
            self.assertEqual(
                provider.side_value,
                int(edge["side_value"]),
            )

    def test_missing_provider_fails_closed(self):
        with self.assertRaises(
            PrimarySideContinuationError
        ):
            create_primary_side_provider(
                "f" * 64,
                d1_edge={
                    "edge_id": "synthetic",
                    "side_value": 1,
                    "primary_side": {
                        "present": True,
                        "primary_logic_signature": "f" * 64,
                    },
                },
                v2_root=str(ROOT),
            )

    def test_duplicate_signature_claim_fails_closed(self):
        signature = "a" * 64
        one = types.ModuleType("fake_primary_provider_one")
        two = types.ModuleType("fake_primary_provider_two")

        for module, provider_id in (
            (one, "one"),
            (two, "two"),
        ):
            module.PROVIDER_SPEC = {
                "provider_id": provider_id,
                "provider_version": "1",
                "supported_signatures": {
                    signature: {}
                },
            }
            module.create_provider = lambda **kwargs: None

        real_import = __import__("importlib").import_module

        def importer(name):
            if name == one.__name__:
                return one
            if name == two.__name__:
                return two
            return real_import(name)

        with mock.patch.object(
            registry.importlib,
            "import_module",
            side_effect=importer,
        ):
            with self.assertRaises(
                PrimarySideContinuationError
            ):
                discover_primary_side_providers(
                    [one.__name__, two.__name__]
                )

    def test_provider_has_no_asset_registry_or_candidate_signature(self):
        source = inspect.getsource(trend)
        for token in (
            '"BTCUSD"',
            '"DAX"',
            '"GBPJPY"',
            '"JPN"',
            '"NASDAQ"',
            '"US500"',
            '"USOIL"',
            '"XAUUSD"',
        ):
            self.assertNotIn(token, source)

        self.assertNotIn("== 74", source)
        self.assertNotIn(
            "999248685dd1ddfbd8bb0263bd7759685ee11a307cd2d2fea333468cec8c1d34",
            source,
        )
        self.assertNotIn(
            "4e85d64e02d3594c3d063f3981e8e2207f644ff078799fe7b8c32ef462601ac6",
            source,
        )

    def test_update_one_has_no_batch_fallback_or_disk_read(self):
        source = inspect.getsource(
            trend.TrendPrimarySideContinuation.update_one
        )
        for token in (
            "read_csv",
            "read_parquet",
            "load_verified_ffd_features",
            "build_primary_side_context",
            "filter_long_events_with_primary_side",
            "filter_short_events_with_primary_side",
            "primary_side.py",
        ):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
