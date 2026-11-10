from __future__ import annotations

"""Transactional timestamp-generation persistence for live FFD/HMM dependencies.

Contract
--------
- A durable dependency watermark is allowed to advance only after every expected
  timestamp in the generation is durably present in order.
- The first certification (startup/recovery) may scan the full Datetime column.
- Normal live commits verify only the file boundary and bytes appended after the
  previously committed byte offset.
- If data reached disk but the checkpoint did not, the next call validates and
  reuses that durable suffix instead of appending duplicates.
- Malformed, missing, duplicate, out-of-order, or unexpected timestamps fail
  closed and are left for the authoritative recovery/oracle path.
"""

import hashlib
import io
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso


TRANSACTION_SCHEMA_VERSION = "v2_dependency_timestamp_generation_v1"
BOUNDARY_ANCHOR_BYTES = 1024


class DependencyTransactionError(RuntimeError):
    """Raised when durable dependency history cannot be proven transactional."""


def ordered_utc_index(values: Any, *, source: str) -> pd.DatetimeIndex:
    try:
        parsed = pd.to_datetime(values, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = to_datetime_iso(values, utc=True, errors="coerce")
    index = pd.DatetimeIndex(parsed)
    if index.isna().any():
        raise DependencyTransactionError(f"{source}: invalid timestamp")
    if index.has_duplicates:
        duplicate = index[index.duplicated()].unique()
        raise DependencyTransactionError(
            f"{source}: duplicate timestamps="
            f"{[pd.Timestamp(value).isoformat() for value in duplicate[:5]]}"
        )
    if not index.is_monotonic_increasing:
        raise DependencyTransactionError(f"{source}: timestamps not ascending")
    return index


def transaction_metadata_valid(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("schema_version") == TRANSACTION_SCHEMA_VERSION
        and int(value.get("committed_size_bytes", -1)) >= 0
        and int(value.get("generation_sequence", -1)) >= 0
        and isinstance(value.get("boundary_anchor_sha256"), str)
        and bool(value.get("timestamp_chain_sha256"))
    )


def generation_id(
    asset: str,
    dependency: str,
    *,
    prior_watermark: Any,
    target_watermark: Any,
    timestamps: Sequence[Any] | pd.DatetimeIndex,
) -> str:
    index = ordered_utc_index(timestamps, source=f"{dependency}_generation_timestamps")
    payload = "\n".join(
        [
            str(asset).upper(),
            str(dependency).upper(),
            str(prior_watermark or ""),
            str(target_watermark or ""),
            *[pd.Timestamp(value).isoformat() for value in index],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp_digest(index: pd.DatetimeIndex) -> str:
    payload = "\n".join(pd.Timestamp(value).isoformat() for value in index)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _advance_chain(previous: str, index: pd.DatetimeIndex) -> str:
    payload = previous + "\n" + "\n".join(
        pd.Timestamp(value).isoformat() for value in index
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _boundary_anchor(path: Path, committed_size: int) -> str:
    committed_size = max(int(committed_size), 0)
    width = min(committed_size, BOUNDARY_ANCHOR_BYTES)
    if width <= 0:
        return hashlib.sha256(b"").hexdigest()
    with Path(path).open("rb") as handle:
        handle.seek(committed_size - width)
        payload = handle.read(width)
    if len(payload) != width:
        raise DependencyTransactionError(
            f"could not read committed boundary anchor path={path} "
            f"expected_bytes={width} actual_bytes={len(payload)}"
        )
    return hashlib.sha256(payload).hexdigest()


def _stat(path: Path) -> tuple[int, int]:
    try:
        value = Path(path).stat()
    except FileNotFoundError as exc:
        raise DependencyTransactionError(f"dependency output missing: {path}") from exc
    return int(value.st_size), int(value.st_mtime_ns)


def _live_times_from_csv(path: Path, *, static_latest: Any) -> pd.DatetimeIndex:
    try:
        frame = pd.read_csv(path, usecols=["Datetime"])
    except Exception as exc:
        raise DependencyTransactionError(
            f"could not read dependency Datetime authority {path}: {exc}"
        ) from exc
    all_times = ordered_utc_index(frame["Datetime"], source=f"{path.name}_timestamps")
    static_stamp = pd.Timestamp(static_latest)
    static_stamp = (
        static_stamp.tz_localize("UTC")
        if static_stamp.tzinfo is None
        else static_stamp.tz_convert("UTC")
    )
    return all_times[all_times > static_stamp]


def certify_csv_prefix(
    path: Path,
    *,
    static_latest: Any,
    expected_live_times: Sequence[Any] | pd.DatetimeIndex,
    generation_sequence: int = 0,
    last_generation_id: str = "startup_or_recovery_prefix_certification",
) -> dict[str, Any]:
    """Full startup/recovery certification of the committed live prefix."""
    path = Path(path)
    static_stamp = pd.Timestamp(static_latest)
    static_stamp = (
        static_stamp.tz_localize("UTC")
        if static_stamp.tzinfo is None
        else static_stamp.tz_convert("UTC")
    )
    expected = ordered_utc_index(
        expected_live_times,
        source="expected_committed_live_dependency_timestamps",
    )
    actual = _live_times_from_csv(path, static_latest=static_stamp)
    if not actual.equals(expected):
        missing = sorted(set(expected).difference(set(actual)))
        unexpected = sorted(set(actual).difference(set(expected)))
        raise DependencyTransactionError(
            "committed dependency prefix mismatch "
            f"expected_count={len(expected)} actual_count={len(actual)} "
            f"missing={[pd.Timestamp(v).isoformat() for v in missing[:10]]} "
            f"unexpected={[pd.Timestamp(v).isoformat() for v in unexpected[:10]]}"
        )
    size, mtime_ns = _stat(path)
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "committed_size_bytes": size,
        "committed_mtime_ns": mtime_ns,
        "boundary_anchor_sha256": _boundary_anchor(path, size),
        "committed_live_row_count": int(len(expected)),
        "committed_last_timestamp": (
            pd.Timestamp(expected[-1]).isoformat()
            if len(expected)
            else static_stamp.isoformat()
        ),
        "timestamp_chain_sha256": _timestamp_digest(expected),
        "generation_sequence": max(int(generation_sequence), 0),
        "last_generation_id": str(last_generation_id),
        "certification_mode": "full_prefix_startup_or_recovery",
    }


def _validate_boundary(
    path: Path,
    transaction: Mapping[str, Any],
) -> tuple[int, int]:
    if not transaction_metadata_valid(transaction):
        raise DependencyTransactionError("missing or invalid transaction metadata")
    path = Path(path)
    size, mtime_ns = _stat(path)
    committed_size = int(transaction["committed_size_bytes"])
    if size < committed_size:
        raise DependencyTransactionError(
            f"dependency output truncated size={size} committed_size={committed_size}"
        )
    actual_anchor = _boundary_anchor(path, committed_size)
    if actual_anchor != str(transaction["boundary_anchor_sha256"]):
        raise DependencyTransactionError(
            "dependency output changed behind committed byte watermark"
        )
    # mtime is retained for observability only. The committed byte offset and
    # boundary digest are the correctness authority; metadata-only timestamp
    # changes must not trigger a false recovery.
    return size, mtime_ns


def assert_checkpoint_watermark(
    transaction: Mapping[str, Any],
    checkpoint_watermark: Any,
    *,
    dependency: str,
) -> None:
    """Prove checkpoint and durable transaction expose the same watermark."""
    if not transaction_metadata_valid(transaction):
        raise DependencyTransactionError(
            f"{dependency}: missing or invalid transaction metadata"
        )
    committed_raw = transaction.get("committed_last_timestamp")
    if committed_raw in (None, ""):
        raise DependencyTransactionError(
            f"{dependency}: committed transaction watermark is missing"
        )
    try:
        committed = pd.Timestamp(committed_raw)
        checkpoint = pd.Timestamp(checkpoint_watermark)
        committed = (
            committed.tz_localize("UTC")
            if committed.tzinfo is None
            else committed.tz_convert("UTC")
        )
        checkpoint = (
            checkpoint.tz_localize("UTC")
            if checkpoint.tzinfo is None
            else checkpoint.tz_convert("UTC")
        )
    except Exception as exc:
        raise DependencyTransactionError(
            f"{dependency}: invalid checkpoint/transaction watermark"
        ) from exc
    if checkpoint != committed:
        raise DependencyTransactionError(
            f"{dependency}: checkpoint watermark differs from committed "
            f"transaction watermark checkpoint={checkpoint.isoformat()} "
            f"committed={committed.isoformat()}"
        )


def assert_committed_boundary(
    path: Path,
    transaction: Mapping[str, Any],
) -> None:
    size, _ = _validate_boundary(path, transaction)
    committed_size = int(transaction["committed_size_bytes"])
    if size != committed_size:
        raise DependencyTransactionError(
            f"uncommitted dependency suffix exists with no expected input "
            f"bytes={size - committed_size}"
        )


def _read_suffix(
    path: Path,
    *,
    offset: int,
    columns: Sequence[str],
) -> tuple[pd.DataFrame, int]:
    path = Path(path)
    size, _ = _stat(path)
    if size == int(offset):
        return pd.DataFrame(columns=list(columns)), 0
    with path.open("rb") as handle:
        handle.seek(int(offset))
        payload = handle.read(size - int(offset))
    if not payload:
        return pd.DataFrame(columns=list(columns)), 0
    if not payload.endswith(b"\n"):
        raise DependencyTransactionError(
            "dependency output has a partial uncommitted CSV row"
        )
    try:
        text = payload.decode("utf-8")
        frame = pd.read_csv(
            io.StringIO(text),
            header=None,
            names=list(columns),
        )
    except Exception as exc:
        raise DependencyTransactionError(
            f"could not parse uncommitted dependency suffix: {exc}"
        ) from exc
    if len(frame):
        frame["Datetime"] = to_datetime_iso(
            frame["Datetime"], utc=True, errors="coerce"
        )
        ordered_utc_index(
            frame["Datetime"],
            source="durable_uncommitted_dependency_suffix",
        )
    return frame, len(payload)


def _compare_rows(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    *,
    columns: Sequence[str],
) -> None:
    if len(actual) != len(expected):
        raise DependencyTransactionError(
            f"durable suffix row count mismatch {len(actual)} != {len(expected)}"
        )
    if not len(actual):
        return
    actual_times = ordered_utc_index(
        actual["Datetime"], source="durable_suffix_compare_actual"
    )
    expected_times = ordered_utc_index(
        expected["Datetime"], source="durable_suffix_compare_expected"
    )
    if not actual_times.equals(expected_times):
        raise DependencyTransactionError(
            "durable suffix timestamps differ from recomputed generation"
        )
    for column in columns:
        if column == "Datetime":
            continue
        left = pd.to_numeric(actual[column], errors="coerce")
        right = pd.to_numeric(expected[column], errors="coerce")
        if not left.isna().all() or not right.isna().all():
            if left.isna().any() or right.isna().any():
                raise DependencyTransactionError(
                    f"durable suffix numeric validity mismatch column={column}"
                )
            if not np.allclose(
                left.to_numpy(dtype=float),
                right.to_numpy(dtype=float),
                rtol=1e-12,
                atol=1e-12,
                equal_nan=False,
            ):
                raise DependencyTransactionError(
                    f"durable suffix value mismatch column={column}"
                )
        else:
            if actual[column].astype(str).tolist() != expected[column].astype(str).tolist():
                raise DependencyTransactionError(
                    f"durable suffix text mismatch column={column}"
                )


def _append_rows(path: Path, rows: pd.DataFrame, columns: Sequence[str]) -> int:
    if rows.empty:
        return 0
    buffer = io.StringIO()
    rows.loc[:, list(columns)].to_csv(
        buffer,
        index=False,
        header=False,
        lineterminator="\n",
        float_format="%.17g",
    )
    payload = buffer.getvalue().encode("utf-8")
    with Path(path).open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return len(payload)


def commit_csv_generation(
    path: Path,
    *,
    transaction: Mapping[str, Any],
    expected_times: Sequence[Any] | pd.DatetimeIndex,
    produced_rows: pd.DataFrame,
    columns: Sequence[str],
    generation_id_value: str,
) -> dict[str, Any]:
    """Durably commit one exact timestamp generation, then return new metadata.

    The caller must persist the returned transaction metadata in its checkpoint.
    Until that checkpoint write succeeds, the old transaction remains the durable
    watermark and any appended rows are treated as an uncommitted suffix.
    """
    path = Path(path)
    expected = ordered_utc_index(
        expected_times,
        source="expected_dependency_generation_timestamps",
    )
    if any(column not in produced_rows.columns for column in columns):
        missing = [column for column in columns if column not in produced_rows.columns]
        raise DependencyTransactionError(
            f"produced dependency generation missing columns={missing}"
        )
    produced = produced_rows.loc[:, list(columns)].copy().reset_index(drop=True)
    produced_times = ordered_utc_index(
        produced["Datetime"],
        source="produced_dependency_generation_timestamps",
    )
    if not produced_times.equals(expected):
        missing = sorted(set(expected).difference(set(produced_times)))
        unexpected = sorted(set(produced_times).difference(set(expected)))
        raise DependencyTransactionError(
            "dependency generation is incomplete before persistence "
            f"missing={[pd.Timestamp(v).isoformat() for v in missing[:10]]} "
            f"unexpected={[pd.Timestamp(v).isoformat() for v in unexpected[:10]]}"
        )

    size_before, _ = _validate_boundary(path, transaction)
    committed_size = int(transaction["committed_size_bytes"])
    durable_suffix, bytes_read = _read_suffix(
        path,
        offset=committed_size,
        columns=columns,
    )
    durable_times = (
        ordered_utc_index(
            durable_suffix["Datetime"],
            source="durable_uncommitted_dependency_timestamps",
        )
        if len(durable_suffix)
        else pd.DatetimeIndex([], tz="UTC")
    )

    if len(durable_times) > len(expected):
        raise DependencyTransactionError(
            "durable uncommitted suffix is longer than the expected generation"
        )
    if len(durable_times) and not durable_times.equals(expected[: len(durable_times)]):
        raise DependencyTransactionError(
            "durable uncommitted suffix is not an exact prefix of expected timestamps"
        )
    _compare_rows(
        durable_suffix,
        produced.iloc[: len(durable_suffix)].reset_index(drop=True),
        columns=columns,
    )

    remaining = produced.iloc[len(durable_suffix) :].reset_index(drop=True)
    bytes_appended = _append_rows(path, remaining, columns)

    final_suffix, verification_bytes = _read_suffix(
        path,
        offset=committed_size,
        columns=columns,
    )
    final_times = (
        ordered_utc_index(
            final_suffix["Datetime"],
            source="persisted_dependency_generation_timestamps",
        )
        if len(final_suffix)
        else pd.DatetimeIndex([], tz="UTC")
    )
    if not final_times.equals(expected):
        raise DependencyTransactionError(
            "persisted dependency generation does not exactly equal expected timestamps"
        )
    _compare_rows(final_suffix, produced, columns=columns)

    final_size, final_mtime_ns = _stat(path)
    previous_chain = str(transaction["timestamp_chain_sha256"])
    next_chain = _advance_chain(previous_chain, expected)
    next_sequence = int(transaction["generation_sequence"]) + (1 if len(expected) else 0)
    next_live_count = int(transaction.get("committed_live_row_count", 0)) + len(expected)
    next_last = (
        pd.Timestamp(expected[-1]).isoformat()
        if len(expected)
        else transaction.get("committed_last_timestamp")
    )
    next_transaction = {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "committed_size_bytes": int(final_size),
        "committed_mtime_ns": int(final_mtime_ns),
        "boundary_anchor_sha256": _boundary_anchor(path, final_size),
        "committed_live_row_count": int(next_live_count),
        "committed_last_timestamp": next_last,
        "timestamp_chain_sha256": next_chain,
        "generation_sequence": int(next_sequence),
        "last_generation_id": str(generation_id_value),
        "certification_mode": "incremental_suffix_exact_commit",
    }
    return {
        "transaction": next_transaction,
        "expected_rows": int(len(expected)),
        "durable_reused_rows": int(len(durable_suffix)),
        "newly_appended_rows": int(len(remaining)),
        "bytes_read_for_suffix_verification": int(bytes_read + verification_bytes),
        "bytes_appended": int(bytes_appended),
        "size_before": int(size_before),
        "size_after": int(final_size),
    }


__all__ = [
    "DependencyTransactionError",
    "TRANSACTION_SCHEMA_VERSION",
    "assert_checkpoint_watermark",
    "assert_committed_boundary",
    "certify_csv_prefix",
    "commit_csv_generation",
    "generation_id",
    "ordered_utc_index",
    "transaction_metadata_valid",
]
