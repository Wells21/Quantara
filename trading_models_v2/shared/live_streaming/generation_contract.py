"""Cross-dependency FFD+HMM generation contract for the live hot path."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import pandas as pd

from . import dependency_transactions as txn

SCHEMA_VERSION = "v2_asset_dependency_generation_v1"
READY_STATUS = "READY"


class DependencyGenerationError(RuntimeError):
    """Raised when FFD/HMM cannot be certified as one coherent generation."""


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise DependencyGenerationError("dependency generation timestamp is NaT")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _component(
    dependency: str,
    transaction: Mapping[str, Any],
    *,
    target: Any,
    checkpoint_generation_id: str | None = None,
) -> dict[str, Any]:
    name = str(dependency).upper()
    if not txn.transaction_metadata_valid(transaction):
        raise DependencyGenerationError(
            f"{name}: missing or invalid transactional persistence metadata"
        )
    target_stamp = _utc(target)
    try:
        txn.assert_checkpoint_watermark(
            transaction,
            target_stamp,
            dependency=name,
        )
    except Exception as exc:
        raise DependencyGenerationError(
            f"{name}: transaction watermark is not target-coherent: {exc}"
        ) from exc
    transaction_generation_id = str(transaction.get("last_generation_id") or "")
    if not transaction_generation_id:
        raise DependencyGenerationError(
            f"{name}: transaction is missing last_generation_id"
        )
    if checkpoint_generation_id and str(checkpoint_generation_id) != transaction_generation_id:
        raise DependencyGenerationError(
            f"{name}: checkpoint/transaction generation mismatch "
            f"checkpoint={checkpoint_generation_id} "
            f"transaction={transaction_generation_id}"
        )
    return {
        "target": target_stamp.isoformat(),
        "generation_id": transaction_generation_id,
        "generation_sequence": int(transaction.get("generation_sequence", 0) or 0),
        "committed_size_bytes": int(transaction.get("committed_size_bytes", 0) or 0),
        "committed_last_timestamp": str(transaction.get("committed_last_timestamp") or ""),
        "timestamp_chain_sha256": str(transaction.get("timestamp_chain_sha256") or ""),
        "boundary_anchor_sha256": str(transaction.get("boundary_anchor_sha256") or ""),
    }


def build_ready_generation(
    *,
    asset: str,
    raw_target: Any,
    hmm_target: Any,
    ffd_transaction: Mapping[str, Any],
    hmm_transaction: Mapping[str, Any],
    ffd_checkpoint_generation_id: str | None = None,
    hmm_checkpoint_generation_id: str | None = None,
    source: str,
) -> dict[str, Any]:
    key = str(asset).strip().upper()
    if not key:
        raise DependencyGenerationError("asset is required")
    raw_stamp = _utc(raw_target)
    hmm_stamp = _utc(hmm_target)
    if hmm_stamp > raw_stamp:
        raise DependencyGenerationError(
            f"{key}: HMM target cannot be newer than raw/FFD target "
            f"hmm={hmm_stamp.isoformat()} raw={raw_stamp.isoformat()}"
        )
    ffd = _component(
        "FFD",
        ffd_transaction,
        target=raw_stamp,
        checkpoint_generation_id=ffd_checkpoint_generation_id,
    )
    hmm = _component(
        "HMM",
        hmm_transaction,
        target=hmm_stamp,
        checkpoint_generation_id=hmm_checkpoint_generation_id,
    )
    # Generation identity is data/state identity. Diagnostic call-site metadata
    # such as source must not make the same committed FFD/HMM pair look like a
    # different generation after recovery followed by immediate replay.
    identity = {
        "schema_version": SCHEMA_VERSION,
        "asset": key,
        "raw_target": raw_stamp.isoformat(),
        "hmm_target": hmm_stamp.isoformat(),
        "ffd": ffd,
        "hmm": hmm,
    }
    generation_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    record = {
        **identity,
        "source": str(source),
        "status": READY_STATUS,
        "generation_id": generation_id,
    }
    assert_ready_generation(record, asset=key, raw_target=raw_stamp, hmm_target=hmm_stamp)
    return record


def assert_ready_generation(
    record: Mapping[str, Any],
    *,
    asset: str | None = None,
    raw_target: Any | None = None,
    hmm_target: Any | None = None,
) -> None:
    if str(record.get("schema_version") or "") != SCHEMA_VERSION:
        raise DependencyGenerationError(
            f"unsupported dependency generation schema={record.get('schema_version')}"
        )
    if str(record.get("status") or "") != READY_STATUS:
        raise DependencyGenerationError(
            f"dependency generation is not READY: {record.get('status')}"
        )
    if not str(record.get("generation_id") or ""):
        raise DependencyGenerationError("dependency generation_id is missing")
    key = str(record.get("asset") or "").upper()
    if asset is not None and key != str(asset).upper():
        raise DependencyGenerationError(
            f"dependency generation asset mismatch {key}!={str(asset).upper()}"
        )
    record_raw = _utc(record.get("raw_target"))
    record_hmm = _utc(record.get("hmm_target"))
    if record_hmm > record_raw:
        raise DependencyGenerationError(
            "dependency generation HMM target is newer than FFD target"
        )
    if raw_target is not None and record_raw != _utc(raw_target):
        raise DependencyGenerationError(
            f"dependency generation raw target mismatch "
            f"{record_raw.isoformat()}!={_utc(raw_target).isoformat()}"
        )
    if hmm_target is not None and record_hmm != _utc(hmm_target):
        raise DependencyGenerationError(
            f"dependency generation HMM target mismatch "
            f"{record_hmm.isoformat()}!={_utc(hmm_target).isoformat()}"
        )
    for name, expected_target in (("ffd", record_raw), ("hmm", record_hmm)):
        component = record.get(name)
        if not isinstance(component, Mapping):
            raise DependencyGenerationError(
                f"dependency generation component missing: {name}"
            )
        if not str(component.get("generation_id") or ""):
            raise DependencyGenerationError(
                f"dependency generation component generation_id missing: {name}"
            )
        if _utc(component.get("committed_last_timestamp")) != expected_target:
            raise DependencyGenerationError(
                f"{name}: committed watermark does not match generation target"
            )

    identity = {
        "schema_version": SCHEMA_VERSION,
        "asset": key,
        "raw_target": record_raw.isoformat(),
        "hmm_target": record_hmm.isoformat(),
        "ffd": dict(record["ffd"]),
        "hmm": dict(record["hmm"]),
    }
    expected_generation_id = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if str(record.get("generation_id") or "") != expected_generation_id:
        raise DependencyGenerationError(
            "dependency generation hash does not match its component identity"
        )
