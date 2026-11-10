from __future__ import annotations
import unittest
import pandas as pd

from shared.edges.composed_decision_continuations.runtime import (
    ComposedDecisionContinuationError,
    build_current_live_model_row,
)


class AlphaAgnosticRowBuilderTests(unittest.TestCase):
    def test_unknown_event_context_and_event_selected_feature_are_preserved(self):
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        event = {
            "Datetime": ts,
            "Bar_Start": ts - pd.Timedelta(minutes=1),
            "edge_id": "X/example_long",
            "side": 1,
            "event_id": "evt",
            "candidate_event_generator": "future_family_v9",
            "Absorption_Event_Type": -1,
            "Custom_Alpha_Context": 7.25,
            "Event_Level_Selected": 3.5,
            "uses_future_information": False,
        }
        canonical = pd.Series({
            "Datetime": ts,
            "Canonical_Selected": 2.0,
            "Unneeded_Wide_Column": 999.0,
        })
        row = build_current_live_model_row(
            current_feature=canonical,
            composed_event=event,
            selected_features=["Event_Level_Selected", "Canonical_Selected"],
        )
        self.assertEqual(len(row), 1)
        self.assertIn("Absorption_Event_Type", row.columns)
        self.assertIn("Custom_Alpha_Context", row.columns)
        self.assertEqual(float(row.loc[0, "Event_Level_Selected"]), 3.5)
        self.assertEqual(float(row.loc[0, "Canonical_Selected"]), 2.0)
        self.assertNotIn("Unneeded_Wide_Column", row.columns)
        self.assertNotIn("cusum_direction", row.columns)

    def test_missing_selected_feature_fails_closed(self):
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        with self.assertRaises(ComposedDecisionContinuationError):
            build_current_live_model_row(
                current_feature=pd.Series({"Datetime": ts}),
                composed_event={"Datetime": ts, "edge_id": "X/e"},
                selected_features=["Missing"],
            )

    def test_forbidden_scoring_field_fails_closed(self):
        ts = pd.Timestamp("2026-08-18T12:00:00Z")
        with self.assertRaises(ComposedDecisionContinuationError):
            build_current_live_model_row(
                current_feature=pd.Series({"Datetime": ts, "Feature": 1.0}),
                composed_event={
                    "Datetime": ts, "edge_id": "X/e", "raw_probability": 0.99
                },
                selected_features=["Feature"],
            )


if __name__ == "__main__":
    unittest.main()
