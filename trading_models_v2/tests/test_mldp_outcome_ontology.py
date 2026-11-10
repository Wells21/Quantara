import unittest
import pandas as pd
from shared.labels.outcome_ontology import (
    OutcomeOntologyError,
    canonicalize_legacy_outcomes,
    validate_layer,
)

class TestOutcomeOntology(unittest.TestCase):
    def test_legacy_aliases_and_label_identity(self):
        t0 = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
        frame = pd.DataFrame({
            "Datetime": t0,
            "realized_r": [1.0, -0.2, 0.0],
            "meta_label": [1, 0, 0],
            "t1": t0 + pd.Timedelta(hours=1),
            "executed_r": [0.5, -0.3, 0.0],
            "executed_meta_label": [1, 0, 0],
            "exit_time": t0 + pd.Timedelta(minutes=30),
        })
        out = canonicalize_legacy_outcomes(frame)
        self.assertIn("gross_r", out.columns)
        self.assertIn("quote_executable_r", out.columns)
        self.assertEqual(validate_layer(out, "gross")["status"], "PASS")
        self.assertEqual(validate_layer(out, "quote_executable")["status"], "PASS")

    def test_conflicting_alias_fails(self):
        frame = pd.DataFrame({"realized_r": [1.0], "gross_r": [-1.0]})
        with self.assertRaises(OutcomeOntologyError):
            canonicalize_legacy_outcomes(frame)

if __name__ == "__main__":
    unittest.main()
