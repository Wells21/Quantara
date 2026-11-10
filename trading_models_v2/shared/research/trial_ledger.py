"""Local-import wrapper around the canonical trial-ledger snippet, plus
schema-file-based validation and a merge utility for Colab-downloaded trial
records.

The base writer lives at `shared/colab_portable/trial_ledger_snippet.py` --
see that module's docstring for why (Step 02's Optuna trials, exactly the
station generating the most trials per edge, run standalone on Google Colab
and cannot import this module at runtime). Locally-run code (portfolio_
allocator scripts, shared/ test code, any locally-run research station)
should import from here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared.colab_portable.trial_ledger_snippet import (
    REQUIRED_FIELDS,
    VALID_TRIAL_COUNT_QUALITY,
    TrialLedgerError,
    append_trial_record,
    build_trial_record,
    new_trial_id,
    now_utc_iso,
    read_trial_records,
    validate_trial_record,
)

_THIS = Path(__file__).resolve()
V2_ROOT = _THIS.parent.parent.parent
DEFAULT_LEDGER_PATH = V2_ROOT / "config" / "research_trial_ledger.jsonl"
SCHEMA_PATH = V2_ROOT / "config" / "research_trial_ledger.schema.json"

__all__ = [
    "REQUIRED_FIELDS",
    "VALID_TRIAL_COUNT_QUALITY",
    "TrialLedgerError",
    "append_trial_record",
    "build_trial_record",
    "new_trial_id",
    "now_utc_iso",
    "read_trial_records",
    "validate_trial_record",
    "DEFAULT_LEDGER_PATH",
    "SCHEMA_PATH",
    "validate_against_json_schema",
    "log_trial",
    "merge_trial_records",
]


def _load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise TrialLedgerError(f"Missing trial ledger schema: {SCHEMA_PATH}")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


_TYPE_MAP = {
    "string": str,
    "object": dict,
    "array": list,
    "boolean": bool,
    "number": (int, float),
    "integer": int,
}


def validate_against_json_schema(record: dict[str, Any]) -> None:
    """Stronger local-only validation: reads
    config/research_trial_ledger.schema.json directly (rather than relying
    on the Colab-portable snippet's hardcoded field list), so this check
    automatically reflects any future schema change with no separate sync
    step needed -- unlike the Colab-portable snippet, this code runs only
    locally and can always read the real schema file.
    """
    schema = _load_schema()
    problems: list[str] = []
    for field in schema.get("required", []):
        if field not in record:
            problems.append(f"missing required field: {field}")
    properties = schema.get("properties", {})
    for field, spec in properties.items():
        if field not in record:
            continue
        value = record[field]
        if field == "parent_trial_id" and value is None:
            continue
        expected_types = spec.get("type")
        if expected_types is None:
            continue
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        expected_types = [t for t in expected_types if t != "null"]
        if not expected_types:
            continue
        python_types = tuple(
            _TYPE_MAP[t] if not isinstance(_TYPE_MAP[t], tuple) else _TYPE_MAP[t]
            for t in expected_types
            if t in _TYPE_MAP
        )
        flat_types: list[type] = []
        for t in python_types:
            if isinstance(t, tuple):
                flat_types.extend(t)
            else:
                flat_types.append(t)
        if flat_types and not isinstance(value, tuple(flat_types)):
            problems.append(
                f"{field} must be one of {expected_types}, got {type(value).__name__}"
            )
        if spec.get("enum") is not None and value not in spec["enum"]:
            problems.append(f"{field} must be one of {spec['enum']}, got {value!r}")
        if spec.get("minLength") is not None and isinstance(value, str):
            if len(value) < spec["minLength"]:
                problems.append(f"{field} must have length >= {spec['minLength']}")

    if problems:
        raise TrialLedgerError(
            f"Trial record fails config/research_trial_ledger.schema.json "
            f"({len(problems)} problem(s)): " + "; ".join(problems)
        )


def log_trial(
    *,
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build, schema-validate (against the real schema file), and append one
    trial record to the canonical ledger in one call. This is the function
    every locally-run research station (Optuna searches, threshold
    selection, geometry grids, allocator variants) should call from now on,
    per the blueprint's P0.4 -- every outcome-informed choice is a trial."""
    record = build_trial_record(**kwargs)
    validate_against_json_schema(record)
    append_trial_record(ledger_path, record)
    return record


def merge_trial_records(
    source_paths: list[str | Path],
    *,
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
) -> dict[str, int]:
    """Merge trial records downloaded back from a Colab-run station (or any
    other source JSONL file(s) built with the Colab-portable snippet) into
    the canonical ledger, de-duplicating by trial_id against what the
    ledger already contains. Never rewrites or removes an existing record;
    only appends genuinely new trial_ids.

    Returns counts: {"read": N, "already_present": N, "appended": N,
    "invalid_skipped": N}.
    """
    existing = read_trial_records(ledger_path)
    existing_ids = {rec.get("trial_id") for rec in existing}
    read_count = 0
    already_present = 0
    appended = 0
    invalid_skipped = 0
    for source_path in source_paths:
        for record in read_trial_records(source_path):
            read_count += 1
            trial_id = record.get("trial_id")
            if trial_id in existing_ids:
                already_present += 1
                continue
            try:
                validate_against_json_schema(record)
            except TrialLedgerError:
                invalid_skipped += 1
                continue
            append_trial_record(ledger_path, record)
            existing_ids.add(trial_id)
            appended += 1
    return {
        "read": read_count,
        "already_present": already_present,
        "appended": appended,
        "invalid_skipped": invalid_skipped,
    }
