from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.research.trial_ledger import (
    TrialLedgerError,
    append_trial_record,
    build_trial_record,
    log_trial,
    merge_trial_records,
    new_trial_id,
    read_trial_records,
    validate_against_json_schema,
)


def _sample_kwargs(**overrides):
    base = dict(
        trial_type="optuna_hyperparameter_search",
        hypothesis_id="btcusd_absorption_long_h1",
        parameters={"max_depth": [3, 4, 5], "n_trials": 100},
        data_window={"start": "2024-01-01", "end": "2026-01-01"},
        code_commit="abc1234",
        metric_name="f1_train_fold",
        result_artifacts=["assets/BTCUSD/edges/absorption_long/artifacts/model/optuna_study.csv"],
        outcome_seen=True,
        selection_influenced=True,
        historical_backfill=False,
        trial_count_quality="exact",
    )
    base.update(overrides)
    return base


class TestBuildAndValidate(unittest.TestCase):
    def test_build_trial_record_round_trips_all_fields(self):
        record = build_trial_record(**_sample_kwargs())
        self.assertTrue(record["trial_id"])
        self.assertEqual(record["trial_type"], "optuna_hyperparameter_search")
        self.assertIn("created_at_utc", record)

    def test_build_rejects_invalid_trial_count_quality(self):
        with self.assertRaises(TrialLedgerError):
            build_trial_record(**_sample_kwargs(trial_count_quality="pretty_sure"))

    def test_new_trial_id_is_unique_across_calls(self):
        ids = {new_trial_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)

    def test_validate_against_json_schema_accepts_valid_record(self):
        record = build_trial_record(**_sample_kwargs())
        validate_against_json_schema(record)  # must not raise

    def test_validate_against_json_schema_rejects_missing_field(self):
        record = build_trial_record(**_sample_kwargs())
        del record["metric_name"]
        with self.assertRaises(TrialLedgerError):
            validate_against_json_schema(record)

    def test_validate_against_json_schema_rejects_wrong_type(self):
        record = build_trial_record(**_sample_kwargs())
        record["outcome_seen"] = "yes"  # should be bool
        with self.assertRaises(TrialLedgerError):
            validate_against_json_schema(record)

    def test_parent_trial_id_null_is_valid(self):
        record = build_trial_record(**_sample_kwargs(parent_trial_id=None))
        validate_against_json_schema(record)  # must not raise


class TestAppendAndRead(unittest.TestCase):
    def test_append_then_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            record1 = build_trial_record(**_sample_kwargs())
            record2 = build_trial_record(**_sample_kwargs(trial_type="threshold_selection"))
            append_trial_record(path, record1)
            append_trial_record(path, record2)
            rows = read_trial_records(path)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["trial_id"] for r in rows}, {record1["trial_id"], record2["trial_id"]})

    def test_append_is_truly_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            first = build_trial_record(**_sample_kwargs())
            append_trial_record(path, first)
            second = build_trial_record(**_sample_kwargs())
            append_trial_record(path, second)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            # The first line's content must be untouched by the second append.
            self.assertEqual(json.loads(lines[0])["trial_id"], first["trial_id"])

    def test_read_trial_records_on_missing_file_returns_empty(self):
        rows = read_trial_records(Path(tempfile.mkdtemp()) / "does_not_exist.jsonl")
        self.assertEqual(rows, [])

    def test_append_cleans_up_its_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            append_trial_record(path, build_trial_record(**_sample_kwargs()))
            lock_path = path.with_suffix(path.suffix + ".lock")
            self.assertFalse(lock_path.exists())

    def test_read_raises_on_duplicate_trial_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            record = build_trial_record(**_sample_kwargs())
            # Write the identical line twice directly (append_trial_record
            # itself has no way to produce this, since new_trial_id() is
            # always fresh -- this simulates a corrupted/hand-edited ledger).
            line = json.dumps(record)
            with path.open("w", encoding="utf-8") as handle:
                handle.write(line + "\n" + line + "\n")
            with self.assertRaises(TrialLedgerError):
                read_trial_records(path)

    def test_log_trial_convenience_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            record = log_trial(ledger_path=path, **_sample_kwargs())
            rows = read_trial_records(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["trial_id"], record["trial_id"])


class TestMerge(unittest.TestCase):
    def test_merge_appends_new_records_from_multiple_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = tmp_path / "canonical.jsonl"
            source1 = tmp_path / "colab_source1.jsonl"
            source2 = tmp_path / "colab_source2.jsonl"
            r1 = build_trial_record(**_sample_kwargs())
            r2 = build_trial_record(**_sample_kwargs(trial_type="threshold_selection"))
            append_trial_record(source1, r1)
            append_trial_record(source2, r2)

            summary = merge_trial_records([source1, source2], ledger_path=ledger)
            self.assertEqual(summary["read"], 2)
            self.assertEqual(summary["appended"], 2)
            self.assertEqual(summary["already_present"], 0)
            self.assertEqual(len(read_trial_records(ledger)), 2)

    def test_merge_deduplicates_by_trial_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = tmp_path / "canonical.jsonl"
            source = tmp_path / "colab_source.jsonl"
            record = build_trial_record(**_sample_kwargs())
            append_trial_record(ledger, record)  # already in the canonical ledger
            append_trial_record(source, record)  # same trial_id, e.g. re-downloaded

            summary = merge_trial_records([source], ledger_path=ledger)
            self.assertEqual(summary["read"], 1)
            self.assertEqual(summary["appended"], 0)
            self.assertEqual(summary["already_present"], 1)
            self.assertEqual(len(read_trial_records(ledger)), 1)

    def test_merge_skips_invalid_records_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ledger = tmp_path / "canonical.jsonl"
            source = tmp_path / "colab_source.jsonl"
            bad = build_trial_record(**_sample_kwargs())
            bad["outcome_seen"] = "not-a-bool"  # invalid per schema
            with source.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(bad) + "\n")

            summary = merge_trial_records([source], ledger_path=ledger)
            self.assertEqual(summary["invalid_skipped"], 1)
            self.assertEqual(summary["appended"], 0)
            self.assertEqual(len(read_trial_records(ledger)), 0)


if __name__ == "__main__":
    unittest.main()
