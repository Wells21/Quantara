import tempfile
import unittest
from pathlib import Path
from shared.research.trial_ledger import append_trial_record, build_trial_record, read_trial_records

class TestResearchTrialLedger(unittest.TestCase):
    def test_append_only_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.jsonl"
            row = build_trial_record(
                trial_type="geometry_grid_point",
                hypothesis_id="H-001",
                parameters={"H": 10, "EWM": 50, "pt": 1.0, "sl": 1.0},
                data_window={"start": "2024-01-01", "end": "2024-12-31"},
                code_commit="abc123",
                metric_name="quote_executable_mean_r",
                result_artifacts=[],
                outcome_seen=True,
                selection_influenced=False,
                historical_backfill=False,
                trial_count_quality="exact",
            )
            append_trial_record(path, row)
            rows = read_trial_records(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trial_id"], row["trial_id"])

if __name__ == "__main__":
    unittest.main()
