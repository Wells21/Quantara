from __future__ import annotations

import inspect
import unittest
from dataclasses import dataclass

import pandas as pd

from shared.edges.composed_decision_continuations import (
    ComposedDecisionContinuation,
    ComposedDecisionContinuationError,
    RuntimeCompositionIdentity,
    build_current_live_model_row,
)


class FakeCandidateProvider:
    def __init__(self, events):
        self.events = list(events)
        self.update_calls = 0
        self.bootstrap_calls = 0

    def bootstrap(self, frame):
        self.bootstrap_calls += 1

    def update_one(self, row):
        self.update_calls += 1
        return self.events.pop(0)

    def state_summary(self):
        return {"update_calls": self.update_calls}


class FakePrimaryProvider:
    def __init__(self, accepted):
        self.accepted = list(accepted)
        self.update_calls = 0
        self.filter_calls = 0
        self.bootstrap_calls = 0

    def bootstrap(self, frame):
        self.bootstrap_calls += 1

    def update_one(self, row):
        self.update_calls += 1
        return {"primary_side": 1}

    def filter_event(self, event):
        self.filter_calls += 1
        decision = self.accepted.pop(0)
        if not decision:
            return None
        out = dict(event)
        out["primary_side_filter"] = 1
        return out

    def state_summary(self):
        return {"update_calls": self.update_calls}


class FakeResidentScorer:
    edge_id = "TEST/trend_long"
    selected_features = ["f1", "f2"]

    def __init__(self):
        self.score_calls = 0
        self.last_row = None

    def score_current(self, row):
        self.score_calls += 1
        self.last_row = row.copy()
        # Match shared EdgeScorer replacement semantics: edge_id is an
        # output/audit column, so any input copy is removed before the
        # authoritative scorer edge_id is inserted.
        out = row.drop(columns=["edge_id"], errors="ignore").copy()
        out.insert(0, "edge_id", self.edge_id)
        out["selected_threshold"] = 0.51
        out["approved_signal"] = True
        return out

    def state_summary(self):
        return {"score_calls": self.score_calls}


def identity():
    return RuntimeCompositionIdentity(
        edge_id="TEST/trend_long",
        side_value=1,
        runtime_source_path="assets/TEST/edges/trend_long/runtime.py",
        composed_candidate_event_generator=(
            "trend_long_v1_cusum_plus_ffd_primary_side"
        ),
    )


def row(ts, f1=1.25, f2=2.5):
    return pd.Series(
        {
            "Datetime": pd.Timestamp(ts, tz="UTC"),
            "Bar_Start": pd.Timestamp(ts, tz="UTC"),
            "Close": 100.0,
            "Close_FFD": 1.0,
            "f1": f1,
            "f2": f2,
        }
    )


def raw_event(ts):
    return {
        "Datetime": pd.Timestamp(ts, tz="UTC"),
        "Bar_Start": pd.Timestamp(ts, tz="UTC"),
        "event_id": f"event-{ts}",
        "edge_id": "TEST/trend_long",
        "side": 1,
        "Event_Vol": 0.2,
        "candidate_event_generator": "raw_cusum",
        "cusum_direction": 1,
        "event_known_after_close": True,
        "uses_future_information": False,
    }


class ComposedDecisionContinuationTests(unittest.TestCase):
    def make_engine(self, events, accepted):
        candidate = FakeCandidateProvider(events)
        primary = FakePrimaryProvider(accepted)
        scorer = FakeResidentScorer()
        engine = ComposedDecisionContinuation(
            edge_id="TEST/trend_long",
            identity=identity(),
            candidate_provider=candidate,
            primary_provider=primary,
            resident_scorer=scorer,
        )
        return engine, candidate, primary, scorer

    def test_bootstrap_advances_both_stateful_providers(self):
        engine, candidate, primary, _ = self.make_engine([], [])
        frame = pd.DataFrame(
            [
                {
                    "Datetime": pd.Timestamp("2026-01-01", tz="UTC"),
                    "Close": 1.0,
                    "Close_FFD": 1.0,
                }
            ]
        )
        engine.bootstrap(frame)
        self.assertEqual(candidate.bootstrap_calls, 1)
        self.assertEqual(primary.bootstrap_calls, 1)
        self.assertEqual(engine.processed_rows, 1)

    def test_nonfiring_bar_never_builds_or_scores(self):
        engine, candidate, primary, scorer = self.make_engine([None], [])
        current = row("2026-01-02")
        result = engine.update_one(
            candidate_row=current,
            current_canonical_row=current,
        )
        self.assertIsNone(result)
        self.assertEqual(candidate.update_calls, 1)
        self.assertEqual(primary.update_calls, 1)
        self.assertEqual(primary.filter_calls, 0)
        self.assertEqual(engine.model_row_call_count, 0)
        self.assertEqual(engine.score_call_count, 0)
        self.assertEqual(scorer.score_calls, 0)

    def test_primary_rejection_never_builds_or_scores(self):
        ts = "2026-01-03"
        engine, candidate, primary, scorer = self.make_engine(
            [raw_event(ts)],
            [False],
        )
        current = row(ts)
        result = engine.update_one(
            candidate_row=current,
            current_canonical_row=current,
        )
        self.assertIsNone(result)
        self.assertEqual(candidate.update_calls, 1)
        self.assertEqual(primary.update_calls, 1)
        self.assertEqual(primary.filter_calls, 1)
        self.assertEqual(engine.raw_candidate_count, 1)
        self.assertEqual(engine.primary_rejected_count, 1)
        self.assertEqual(engine.model_row_call_count, 0)
        self.assertEqual(engine.score_call_count, 0)
        self.assertEqual(scorer.score_calls, 0)

    def test_accepted_event_gets_runtime_composed_identity_and_exact_signal_id(self):
        ts = "2026-01-04"
        engine, _, primary, scorer = self.make_engine(
            [raw_event(ts)],
            [True],
        )
        current = row(ts)
        scored = engine.update_one(
            candidate_row=current,
            current_canonical_row=current,
        )
        self.assertIsNotNone(scored)
        assert scored is not None
        self.assertEqual(primary.filter_calls, 1)
        self.assertEqual(engine.model_row_call_count, 1)
        self.assertEqual(engine.score_call_count, 1)
        self.assertEqual(scorer.score_calls, 1)
        self.assertEqual(
            scorer.last_row.iloc[0]["candidate_event_generator"],
            "trend_long_v1_cusum_plus_ffd_primary_side",
        )
        self.assertEqual(
            scored.iloc[0]["signal_id"],
            (
                "TEST/trend_long|"
                + pd.Timestamp(ts, tz="UTC").isoformat()
                + "|thr_0.5100"
            ),
        )

    def test_model_row_uses_only_current_canonical_selected_features(self):
        ts = "2026-01-05"
        event = raw_event(ts)
        event["candidate_event_generator"] = (
            "trend_long_v1_cusum_plus_ffd_primary_side"
        )
        event["primary_side_filter"] = 1
        current = row(ts, f1=7.0, f2=8.0)
        model_row = build_current_live_model_row(
            current_feature=current,
            composed_event=event,
            selected_features=["f1", "f2"],
        )
        self.assertEqual(len(model_row), 1)
        self.assertEqual(float(model_row.iloc[0]["f1"]), 7.0)
        self.assertEqual(float(model_row.iloc[0]["f2"]), 8.0)
        self.assertNotIn("Close", model_row.columns)
        self.assertNotIn("Close_FFD", model_row.columns)

    def test_timestamp_mismatch_fails_closed(self):
        engine, _, _, _ = self.make_engine([None], [])
        with self.assertRaises(ComposedDecisionContinuationError):
            engine.update_one(
                candidate_row=row("2026-01-06"),
                current_canonical_row=row("2026-01-07"),
            )

    def test_update_one_contains_no_full_authority_or_artifact_reload(self):
        source = inspect.getsource(ComposedDecisionContinuation.update_one)
        forbidden = [
            "generate_candidate_events",
            "_build_live_candidate_model_rows",
            "build_features",
            "load_model(",
            "load_calibrator(",
            "load_threshold(",
            "load_trade_geometry(",
            "read_csv",
            "read_parquet",
            "joblib.load",
        ]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
