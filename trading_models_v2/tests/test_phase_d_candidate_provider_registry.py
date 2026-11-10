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

from shared.edges.candidate_continuations import (  # noqa: E402
    CandidateContinuationError,
    create_candidate_provider,
    discover_candidate_providers,
)
from shared.edges.candidate_continuations import registry  # noqa: E402
from shared.edges.candidate_continuations.providers import cusum  # noqa: E402


class CandidateProviderRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d1 = json.loads(
            (
                ROOT
                / "portfolio_live/config/phase_d/candidate_contract.json"
            ).read_text(encoding="utf-8-sig")
        )

    def test_discovery_finds_both_cusum_signatures(self):
        providers = discover_candidate_providers()
        self.assertIn(cusum.POSITIVE_CUSUM_SIGNATURE, providers)
        self.assertIn(cusum.NEGATIVE_CUSUM_SIGNATURE, providers)

    def test_binding_uses_d1_logic_signature(self):
        by_signature = {}
        for edge in self.d1.get("edges") or []:
            signature = str(
                (edge.get("candidate") or {}).get(
                    "candidate_logic_signature"
                )
                or ""
            )
            by_signature.setdefault(signature, []).append(edge)

        for signature in (
            cusum.POSITIVE_CUSUM_SIGNATURE,
            cusum.NEGATIVE_CUSUM_SIGNATURE,
        ):
            if signature not in by_signature:
                continue  # no live edge currently uses this cusum direction
            edge = by_signature[signature][0]
            provider = create_candidate_provider(
                signature,
                d1_edge=edge,
                v2_root=str(ROOT),
            )
            self.assertEqual(provider.identity.edge_id, edge["edge_id"])

    def test_missing_provider_fails_closed(self):
        with self.assertRaises(CandidateContinuationError):
            create_candidate_provider(
                "f" * 64,
                d1_edge={
                    "edge_id": "synthetic",
                    "candidate": {
                        "candidate_logic_signature": "f" * 64,
                    },
                },
                v2_root=str(ROOT),
            )

    def test_duplicate_signature_claim_fails_closed(self):
        signature = "a" * 64
        one = types.ModuleType("fake_provider_one")
        two = types.ModuleType("fake_provider_two")
        for module, provider_id in ((one, "one"), (two, "two")):
            module.PROVIDER_SPEC = {
                "provider_id": provider_id,
                "provider_version": "1",
                "supported_signatures": {signature: {}},
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
            with self.assertRaises(CandidateContinuationError):
                discover_candidate_providers([one.__name__, two.__name__])

    def test_provider_has_no_asset_registry(self):
        source = inspect.getsource(cusum)
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
        self.assertNotIn("ENTRY_FUNCTION_RECIPES", source)

    def test_update_one_has_no_batch_fallback_or_disk_read(self):
        source = inspect.getsource(
            cusum.RawCusumCandidateContinuation.update_one
        )
        for token in (
            "read_csv",
            "read_parquet",
            "generate_v1_positive",
            "generate_v1_negative",
            "candidate_events",
        ):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
