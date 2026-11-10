"""Canonical, dependency-free research-trial ledger writer.

Source of the schema this enforces: `config/research_trial_ledger.schema.json`.
Per the blueprint's P0.4 (AFML Ch.11's "record every backtest ever run,"
MLAM Ch.8's False Strategy theorem / DSR, both requiring an honest count of
trials): every outcome-informed research choice -- an Optuna hyperparameter
search, a threshold selection, a geometry grid point, an allocator variant
-- is a trial, and must be appended here, never silently left unrecorded.

WHY THIS FILE IS STANDALONE (same reasoning as
shared/colab_portable/purge_embargo_snippet.py and
shared/colab_portable/denoise_onc_snippet.py): Step 02 (CPCV/Optuna model
training -- exactly the station that generates the most trials per edge)
runs standalone on Google Colab and cannot import the rest of `shared/` at
runtime. A Colab-run script that needs to log trials should copy the
functions below verbatim and write its trial records to a local JSONL file
that travels back with the rest of that station's downloaded artifacts; a
later, locally-run consolidation step (see shared/research/trial_ledger.py's
`merge_trial_records`) merges those into the canonical
config/research_trial_ledger.jsonl, de-duplicating by trial_id.

This file intentionally does NOT depend on the `jsonschema` package (not
guaranteed present in a Colab runtime) -- validation here is a lightweight,
hardcoded check of the schema's required fields and basic types.
`tests/test_trial_ledger_schema_sync.py` asserts the hardcoded required-field
list below never drifts from the real schema file, the same drift-detection
discipline already used for the purge/embargo snippet.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Kept in sync with config/research_trial_ledger.schema.json's "required"
# array by tests/test_trial_ledger_schema_sync.py -- do not edit one without
# the other.
REQUIRED_FIELDS: tuple[str, ...] = (
    "trial_id",
    "trial_type",
    "hypothesis_id",
    "parameters",
    "data_window",
    "code_commit",
    "metric_name",
    "result_artifacts",
    "outcome_seen",
    "selection_influenced",
    "historical_backfill",
    "trial_count_quality",
    "created_at_utc",
)

VALID_TRIAL_COUNT_QUALITY = ("exact", "reconstructed", "lower_bound", "unknown")


class TrialLedgerError(RuntimeError):
    """Fail-closed error for the research-trial ledger."""


def new_trial_id() -> str:
    """A fresh, collision-safe id for one ledger row.

    Deliberately NOT a content hash of the trial's parameters: a genuinely
    repeated trial (same hyperparameters re-run, e.g. after a data refresh)
    is still a distinct trial event in an append-only ledger, not a
    duplicate to be merged away -- only two records sharing this exact id
    are treated as the same event (see merge de-duplication in
    shared/research/trial_ledger.py).
    """
    return str(uuid.uuid4())


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_trial_record(
    *,
    trial_type: str,
    hypothesis_id: str,
    parameters: dict[str, Any],
    data_window: dict[str, Any],
    code_commit: str,
    metric_name: str,
    result_artifacts: list[str],
    outcome_seen: bool,
    selection_influenced: bool,
    historical_backfill: bool,
    trial_count_quality: str,
    parent_trial_id: str | None = None,
    notes: str | None = None,
    trial_id: str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Construct one ledger row, matching
    config/research_trial_ledger.schema.json exactly. Raises
    TrialLedgerError immediately if any required field is missing or the
    wrong type -- this function never silently drops or guesses a field.
    """
    if trial_count_quality not in VALID_TRIAL_COUNT_QUALITY:
        raise TrialLedgerError(
            f"trial_count_quality must be one of {VALID_TRIAL_COUNT_QUALITY}, "
            f"got {trial_count_quality!r}"
        )
    record: dict[str, Any] = {
        "trial_id": trial_id or new_trial_id(),
        "trial_type": trial_type,
        "hypothesis_id": hypothesis_id,
        "parent_trial_id": parent_trial_id,
        "parameters": parameters,
        "data_window": data_window,
        "code_commit": code_commit,
        "metric_name": metric_name,
        "result_artifacts": list(result_artifacts),
        "outcome_seen": bool(outcome_seen),
        "selection_influenced": bool(selection_influenced),
        "historical_backfill": bool(historical_backfill),
        "trial_count_quality": trial_count_quality,
        "created_at_utc": created_at_utc or now_utc_iso(),
    }
    if notes is not None:
        record["notes"] = notes
    validate_trial_record(record)
    return record


def validate_trial_record(record: dict[str, Any]) -> None:
    """Lightweight, dependency-free validation mirroring the schema's
    required-fields/basic-type constraints. Raises TrialLedgerError listing
    every problem found, not just the first."""
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in record:
            problems.append(f"missing required field: {field}")

    def _check_type(field: str, expected: type | tuple[type, ...]) -> None:
        if field in record and not isinstance(record[field], expected):
            problems.append(
                f"{field} must be {expected}, got {type(record[field]).__name__}"
            )

    _check_type("trial_id", str)
    _check_type("trial_type", str)
    _check_type("hypothesis_id", str)
    _check_type("parameters", dict)
    _check_type("data_window", dict)
    _check_type("code_commit", str)
    _check_type("metric_name", str)
    _check_type("result_artifacts", list)
    _check_type("outcome_seen", bool)
    _check_type("selection_influenced", bool)
    _check_type("historical_backfill", bool)
    _check_type("created_at_utc", str)

    if isinstance(record.get("trial_id"), str) and len(record["trial_id"]) < 1:
        problems.append("trial_id must not be empty")
    if isinstance(record.get("result_artifacts"), list) and not all(
        isinstance(item, str) for item in record["result_artifacts"]
    ):
        problems.append("result_artifacts must be a list of strings")
    if record.get("trial_count_quality") not in VALID_TRIAL_COUNT_QUALITY:
        problems.append(
            f"trial_count_quality must be one of {VALID_TRIAL_COUNT_QUALITY}"
        )
    if "parent_trial_id" in record and record["parent_trial_id"] is not None:
        if not isinstance(record["parent_trial_id"], str):
            problems.append("parent_trial_id must be a string or null")

    if problems:
        raise TrialLedgerError(
            f"Invalid trial record ({len(problems)} problem(s)): " + "; ".join(problems)
        )


def _acquire_lock(lock_path: Path, timeout_seconds: float) -> int:
    """Simple cross-platform advisory lock via exclusive file creation
    (O_CREAT|O_EXCL). Guards against two concurrent writers (e.g. a local
    consolidation run and a manual append) interleaving partial lines."""
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TrialLedgerError(f"Timed out waiting for ledger lock: {lock_path}")
            time.sleep(0.05)


def append_trial_record(
    ledger_path: str | Path,
    record: dict[str, Any],
    *,
    lock_timeout_seconds: float = 5.0,
) -> None:
    """Validate and append one record as a single JSON line. Append-only,
    single-writer-at-a-time (via an exclusive lock file) and fsync'd before
    the lock is released: no record already written is ever rewritten or
    removed by this function, and a crash mid-write cannot silently corrupt
    or lose a prior line."""
    validate_trial_record(record)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = _acquire_lock(lock_path, lock_timeout_seconds)
    try:
        os.close(fd)
        fd = -1
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def read_trial_records(ledger_path: str | Path) -> list[dict[str, Any]]:
    """Read every record. Raises if the same trial_id appears twice -- the
    ledger is meant to be append-only with a fresh id per trial
    (see new_trial_id's docstring on why content-identical trials are still
    distinct events, not duplicates to silently collapse)."""
    path = Path(ledger_path)
    if not path.exists():
        return []
    records = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise TrialLedgerError(
                f"{path}:{line_number}: invalid JSON line: {exc}"
            ) from exc
        trial_id = record.get("trial_id")
        if trial_id in seen_ids:
            raise TrialLedgerError(
                f"{path}:{line_number}: duplicate trial_id {trial_id!r} -- "
                "the ledger must never contain the same trial_id twice."
            )
        seen_ids.add(trial_id)
        records.append(record)
    return records
