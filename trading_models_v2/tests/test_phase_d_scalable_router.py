from __future__ import annotations
import unittest

from shared.edges.composed_decision_continuations.router import (
    AUTHORITATIVE_BATCH,
    FAST_CONTINUATION,
    PhaseDEdgeRoute,
    PhaseDFastPathFailure,
    capability_summary,
    classify_edge_capability,
    classify_selected_edges,
)


def d1(edge_id, c, p):
    return {
        "edge_id": edge_id,
        "candidate": {"candidate_logic_signature": c},
        "primary_side": {"primary_logic_signature": p},
    }


def manifest(edge_id):
    return {"edge_id": edge_id}


class Boom:
    def update_one(self, **kwargs):
        raise ValueError("boom")


class ScalableRouterTests(unittest.TestCase):
    def test_supported_is_fast(self):
        cap = classify_edge_capability(
            d1_edge=d1("A/x", "c1", "p1"),
            manifest_edge=manifest("A/x"),
            candidate_registry={"c1": object()},
            primary_registry={"p1": object()},
        )
        self.assertEqual(cap.mode, FAST_CONTINUATION)

    def test_unknown_signature_is_authoritative_and_still_tradable(self):
        cap = classify_edge_capability(
            d1_edge=d1("HK/new", "c9", "p9"),
            manifest_edge=manifest("HK/new"),
            candidate_registry={"c1": object()},
            primary_registry={"p1": object()},
        )
        self.assertEqual(cap.mode, AUTHORITATIVE_BATCH)

    def test_fast_failure_never_hidden_falls_back(self):
        cap = classify_edge_capability(
            d1_edge=d1("A/x", "c1", "p1"),
            manifest_edge=manifest("A/x"),
            candidate_registry={"c1": object()},
            primary_registry={"p1": object()},
        )
        route = PhaseDEdgeRoute(capability=cap, fast_engine=Boom())
        with self.assertRaises(PhaseDFastPathFailure):
            route.fast_update_one(candidate_row=None)
        self.assertEqual(route.mode, FAST_CONTINUATION)
        self.assertFalse(route.state_summary()["hidden_authoritative_fallback"])

    def test_transition_to_batch_is_explicit(self):
        cap = classify_edge_capability(
            d1_edge=d1("A/x", "c1", "p1"),
            manifest_edge=manifest("A/x"),
            candidate_registry={"c1": object()},
            primary_registry={"p1": object()},
        )
        route = PhaseDEdgeRoute(capability=cap, fast_engine=object())
        route.switch_to_authoritative(reason="controlled_recovery")
        self.assertEqual(route.mode, AUTHORITATIVE_BATCH)
        self.assertIsNone(route.fast_engine)

    def test_mixed_portfolio_summary_has_zero_unsupported_trading_edges(self):
        d1_edges = {
            "A/known": d1("A/known", "c1", "p1"),
            "HK/future": d1("HK/future", "c9", "p9"),
            "B/known2": d1("B/known2", "c1", "p1"),
        }
        manifest_edges = {key: manifest(key) for key in d1_edges}
        caps = classify_selected_edges(
            d1_edges=d1_edges,
            manifest_edges=manifest_edges,
            candidate_registry={"c1": object()},
            primary_registry={"p1": object()},
        )
        summary = capability_summary(caps)
        self.assertEqual(summary["selected_edges"], len(manifest_edges))
        self.assertEqual(summary["fast_continuation"], 2)
        self.assertEqual(summary["authoritative_batch"], 1)
        self.assertEqual(summary["unsupported_trading_edges"], 0)


if __name__ == "__main__":
    unittest.main()
