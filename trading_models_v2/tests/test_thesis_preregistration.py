"""AFML Ch.11 walkthrough (owner rule 2026-10-26): new edges need a pre-registered thesis card; post hoc cards are
accepted only for edges written on or before 2026-10-26, and every deployable edge's card states its provenance.

Run from trading_models_v2:  python -m unittest tests.test_thesis_preregistration
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))

from shared.edges.edge_yaml_contract import EdgeYamlContractError, validate_thesis_block  # noqa: E402

CARD = {"thesis": {
    "schema_version": "thesis_card_v1", "written_utc": "2026-10-23", "provenance": "post_hoc", "review_status": "x",
    "trigger_summary": "t", "mechanism": "m", "who_loses_and_why": "w",
    "regimes": {"should_work": ["a"], "should_fail": ["b"]},
    "counterfactuals": {"c1": {"statement": "s", "test": "t"}}, "falsifiers": {"f1": {"statement": "s", "test": "t"}}}}


class ThesisProvenanceTest(unittest.TestCase):
    def test_grandfathered_post_hoc_ok(self) -> None:
        validate_thesis_block(CARD, "deployability_pass")

    def test_new_post_hoc_rejected(self) -> None:
        card = copy.deepcopy(CARD)
        card["thesis"]["written_utc"] = "2026-11-02"
        with self.assertRaises(EdgeYamlContractError):
            validate_thesis_block(card, "deployability_pass")

    def test_pre_registered_ok_and_unknown_rejected(self) -> None:
        card = copy.deepcopy(CARD)
        card["thesis"].update(written_utc="2026-11-02", provenance="pre_registered")
        validate_thesis_block(card, "deployability_pass")
        card["thesis"]["provenance"] = "later"
        with self.assertRaises(EdgeYamlContractError):
            validate_thesis_block(card, "deployability_pass")

    def test_current_edges_state_provenance(self) -> None:
        for y in sorted(V2.glob("assets/*/edges/*/edge.yaml")):
            text = y.read_text(encoding="utf-8")
            with self.subTest(edge=str(y.relative_to(V2))):
                self.assertTrue('provenance: "post_hoc"' in text or 'provenance: "pre_registered"' in text)


if __name__ == "__main__":
    unittest.main()
