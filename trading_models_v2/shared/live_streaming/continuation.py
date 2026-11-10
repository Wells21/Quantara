"""Exact fail-closed FFD and HMM live continuation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np
import pandas as pd

from . import state as s
from . import dependency_transactions as dependency_txn
from . import generation_contract
from shared.io import portable_pickle
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

LOG = logging.getLogger("v2_live_streaming_hotpath")


def _dependency_generation_path(asset: str) -> Path:
    if s.FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    return (
        Path(s.FFD_MODULE.LIVE_CONTINUATION_DIR)
        / f"{s.asset_key(asset).lower()}_live_dependency_generation.json"
    )


def _atomic_write_generation(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _checkpoint_transaction(
    asset: str,
    dependency: str,
) -> Mapping[str, Any]:
    """Resolve small checkpoint authority when verifier proof omits its transaction.

    The authoritative FFD verifier persists timestamp_transaction in the joblib
    checkpoint but does not expose it in continuation_proof on every cold/recovery
    path. Generation publication must therefore fall back to the checkpoint rather
    than incorrectly declaring a successfully repaired cache incoherent.
    """
    key = s.asset_key(asset)
    name = str(dependency).upper()
    if name == "FFD":
        if s.FFD_MODULE is None:
            raise RuntimeError("FFD hotpath is not installed")
        state_path = (
            Path(s.FFD_MODULE.LIVE_CONTINUATION_DIR)
            / f"{key.lower()}_ffd_streaming_state.joblib"
        )
    elif name == "HMM":
        if s.HMM_MODULE is None:
            raise RuntimeError("HMM hotpath is not installed")
        state_path = (
            Path(s.HMM_MODULE.LIVE_CONTINUATION_DIR)
            / f"{key.lower()}_hmm_streaming_state.joblib"
        )
    else:
        raise RuntimeError(f"unsupported dependency checkpoint: {dependency}")
    if not state_path.exists():
        raise RuntimeError(f"{key}: {name} checkpoint missing: {state_path}")
    checkpoint = portable_pickle.load(state_path)
    if not isinstance(checkpoint, Mapping):
        raise RuntimeError(f"{key}: {name} checkpoint is not a mapping")
    transaction = checkpoint.get("timestamp_transaction") or {}
    if not dependency_txn.transaction_metadata_valid(transaction):
        raise RuntimeError(
            f"{key}: {name} checkpoint is missing valid timestamp transaction"
        )
    dependency_txn.assert_checkpoint_watermark(
        transaction,
        checkpoint.get("last_observation_timestamp"),
        dependency=name,
    )
    return transaction


def _verification_transaction(
    verification: Mapping[str, Any],
    dependency: str,
    *,
    asset: str | None = None,
) -> Mapping[str, Any]:
    proof = verification.get("continuation_proof") or {}
    transaction = proof.get("timestamp_transaction") or {}
    if dependency_txn.transaction_metadata_valid(transaction):
        return transaction
    if asset is not None:
        return _checkpoint_transaction(asset, dependency)
    raise RuntimeError(
        f"{dependency}: verification is missing valid timestamp transaction"
    )


def _publish_ready_dependency_generation(
    *,
    asset: str,
    raw_target: Any,
    hmm_target: Any,
    ffd_verification: Mapping[str, Any],
    hmm_verification: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    ffd_transaction = _verification_transaction(ffd_verification, "FFD", asset=asset)
    hmm_transaction = _verification_transaction(hmm_verification, "HMM", asset=asset)
    record = generation_contract.build_ready_generation(
        asset=asset,
        raw_target=raw_target,
        hmm_target=hmm_target,
        ffd_transaction=ffd_transaction,
        hmm_transaction=hmm_transaction,
        ffd_checkpoint_generation_id=(
            ffd_verification.get("dependency_generation_id")
            or ffd_transaction.get("last_generation_id")
        ),
        hmm_checkpoint_generation_id=(
            hmm_verification.get("dependency_generation_id")
            or hmm_transaction.get("last_generation_id")
        ),
        source=source,
    )
    _atomic_write_generation(_dependency_generation_path(asset), record)
    return s.publish_dependency_generation(record)


def _normalized_effective_live_bars(
    asset: str,
    live_bars_override: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return persisted live bars plus an optional durable in-memory snapshot.

    The dashboard journals a completed bar synchronously before scoring, while CSV
    compaction is intentionally asynchronous.  FFD/HMM continuation must therefore
    be able to consume that already-durable in-memory bar without waiting for the
    compactor.  This helper never rewrites the bar journal or live-bar CSV.
    """
    key = s.asset_key(asset)
    persisted = s.normalized_live_bars(key)
    if live_bars_override is None or not isinstance(live_bars_override, pd.DataFrame) or live_bars_override.empty:
        return persisted
    if s.FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    override = s.FFD_MODULE._normalize_bar_table(
        live_bars_override.copy(),
        source="dashboard_in_memory_closed_bars_hotpath",
    )
    combined = pd.concat([persisted, override], ignore_index=True, sort=False)
    return s.FFD_MODULE._normalize_bar_table(
        combined,
        source="persisted_plus_dashboard_in_memory_closed_bars_hotpath",
    )


def _effective_signature(
    asset: str,
    live_bars_override: pd.DataFrame | None = None,
) -> Any:
    base = s.live_signature(asset)
    if live_bars_override is None or not isinstance(live_bars_override, pd.DataFrame) or live_bars_override.empty:
        return base
    frame = _normalized_effective_live_bars(asset, live_bars_override)
    tail = frame.tail(8).copy()
    for column in tail.columns:
        if pd.api.types.is_datetime64_any_dtype(tail[column]):
            tail[column] = to_datetime_iso(tail[column], utc=True, errors="coerce").astype("int64")
        elif tail[column].dtype == "object":
            tail[column] = tail[column].astype(str)
    digest = hashlib.sha256(
        pd.util.hash_pandas_object(tail, index=False).to_numpy(dtype="uint64").tobytes()
    ).hexdigest()
    latest = None if frame.empty else s.utc_timestamp(frame["Datetime"].iloc[-1]).isoformat()
    return ("in_memory_closed_bar_override_v1", base, int(len(frame)), latest, digest)


def _fallback_ffd(asset: str, signature: Any, reason: str) -> dict[str, Any]:
    if s.ORIGINAL_FFD_VERIFY is None:
        raise RuntimeError("FFD hotpath is not installed")
    key = s.asset_key(asset)
    LOG.warning("FFD_HOTPATH_FALLBACK asset=%s reason=%s", key, reason)
    s.invalidate(key, ffd=True, hmm=True)
    result = s.ORIGINAL_FFD_VERIFY(key)
    s.invalidate(key, ffd=True, hmm=True)
    s.cache_put(s.FFD_CACHE, key, signature, result)
    return s.copy_result(result)


def verify_asset_ffd_manifest_hot(
    asset: str,
    *,
    live_bars_override: pd.DataFrame | None = None,
    continuation_dir: Path | None = None,
) -> dict[str, Any]:
    if s.FFD_MODULE is None or s.ORIGINAL_FFD_VERIFY is None:
        raise RuntimeError("FFD hotpath is not installed")
    key = s.asset_key(asset)
    if continuation_dir is not None:
        return s.copy_result(
            s.ORIGINAL_FFD_VERIFY(
                key,
                continuation_dir=continuation_dir,
            )
        )
    snapshot = s.current_feature_snapshot(key)
    if snapshot is not None and isinstance(snapshot.get("ffd_verification"), Mapping):
        return s.copy_result(snapshot["ffd_verification"])
    signature = _effective_signature(key, live_bars_override)
    cached = s.cache_get(s.FFD_CACHE, key, signature)
    if cached is not None:
        return cached
    with s.asset_lock(key):
        signature = _effective_signature(key, live_bars_override)
        cached = s.cache_get(s.FFD_CACHE, key, signature)
        if cached is not None:
            return cached
        prior = s.FFD_CACHE.get(key)
        if prior is None:
            if live_bars_override is not None:
                raise RuntimeError(
                    f"{key}: cold_cache_not_initialized; authoritative FFD initialization "
                    "is restricted to startup/dedicated recovery"
                )
            initial = _fallback_ffd(
                key,
                s.live_signature(key),
                "initial_authoritative_verification",
            )
            if live_bars_override is None:
                return initial
            prior = s.FFD_CACHE.get(key)
            if prior is None:
                raise RuntimeError("authoritative FFD initialization did not populate cache")
        try:
            result = s.copy_result(prior["result"])
            output_path = Path(s.FFD_MODULE._V2_ROOT) / str(result["features"])
            state_path = (
                Path(s.FFD_MODULE.LIVE_CONTINUATION_DIR)
                / f"{key.lower()}_ffd_streaming_state.joblib"
            )
            if not output_path.exists() or not state_path.exists():
                if live_bars_override is not None:
                    raise RuntimeError(
                        f"{key}: state_or_output_missing; authoritative FFD rebuild "
                        "is restricted to dedicated recovery"
                    )
                return _fallback_ffd(key, signature, "state_or_output_missing")
            with s.FFD_MODULE._exclusive_output_lock(output_path):
                checkpoint = portable_pickle.load(state_path)
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("schema_version")
                    != s.FFD_MODULE.LIVE_FFD_STATE_SCHEMA_VERSION
                    or checkpoint.get("continuation_writer_contract")
                    != s.FFD_MODULE.FFD_CONTINUATION_WRITER_CONTRACT
                    or not dependency_txn.transaction_metadata_valid(
                        checkpoint.get("timestamp_transaction")
                    )
                ):
                    raise RuntimeError(
                        "checkpoint_schema_writer_or_transaction_contract_mismatch"
                    )
                dependency_txn.assert_checkpoint_watermark(
                    checkpoint["timestamp_transaction"],
                    checkpoint.get("last_observation_timestamp"),
                    dependency="FFD",
                )
                raw_buffer = checkpoint.get("raw_buffer_df")
                if not isinstance(raw_buffer, pd.DataFrame):
                    raise RuntimeError("raw_buffer_missing")
                live_bars = _normalized_effective_live_bars(key, live_bars_override)
                last = s.utc_timestamp(checkpoint["last_observation_timestamp"])
                pending = live_bars[live_bars["Datetime"] > last]
                pending = (
                    pending.sort_values("Datetime", kind="mergesort")
                    .drop_duplicates("Datetime", keep="last")
                    .reset_index(drop=True)
                )
                if pending.empty:
                    dependency_txn.assert_committed_boundary(
                        output_path,
                        checkpoint["timestamp_transaction"],
                    )
                    s.cache_put(s.FFD_CACHE, key, signature, result)
                    s.FFD_DELTA[key] = pd.DataFrame()
                    return result
                raw_buffer = s.FFD_MODULE._normalize_bar_table(
                    raw_buffer, source="live_ffd_raw_buffer_hotpath"
                )
                source = s.FFD_MODULE._normalize_bar_table(
                    pd.concat([raw_buffer, pending], ignore_index=True),
                    source="ffd_streaming_input_hotpath",
                )
                transformed = s.FFD_MODULE.apply_frozen_ffd_to_ohlc(
                    source,
                    d=float(checkpoint["frozen_d"]),
                    window=int(checkpoint["ffd_window"]),
                )
                transformed["Datetime"] = s.FFD_MODULE._parse_utc_datetime_series(
                    transformed["Datetime"]
                )
                columns = [
                    "Datetime",
                    *s.FFD_MODULE.OHLC_COLUMNS,
                    *(f"{column}_FFD" for column in s.FFD_MODULE.OHLC_COLUMNS),
                ]
                rows = transformed[transformed["Datetime"] > last][columns]
                rows = (
                    rows.sort_values("Datetime", kind="mergesort")
                    .drop_duplicates("Datetime", keep="last")
                    .reset_index(drop=True)
                )
                expected = pd.DatetimeIndex(
                    to_datetime_iso(pending["Datetime"], utc=True)
                )
                produced = pd.DatetimeIndex(
                    to_datetime_iso(rows["Datetime"], utc=True)
                )
                if not produced.equals(expected):
                    missing = sorted(set(expected).difference(set(produced)))
                    unexpected = sorted(set(produced).difference(set(expected)))
                    raise RuntimeError(
                        "FFD transformed timestamp generation mismatch "
                        f"missing={[pd.Timestamp(v).isoformat() for v in missing[:5]]} "
                        f"unexpected={[pd.Timestamp(v).isoformat() for v in unexpected[:5]]}"
                    )
                ffd_generation_id = dependency_txn.generation_id(
                    key,
                    "FFD",
                    prior_watermark=last.isoformat(),
                    target_watermark=s.utc_timestamp(
                        pending["Datetime"].max()
                    ).isoformat(),
                    timestamps=expected,
                )
                ffd_transaction_commit = dependency_txn.commit_csv_generation(
                    output_path,
                    transaction=checkpoint["timestamp_transaction"],
                    expected_times=expected,
                    produced_rows=rows,
                    columns=columns,
                    generation_id_value=ffd_generation_id,
                )
                next_raw = s.FFD_MODULE._normalize_bar_table(
                    pd.concat([raw_buffer, pending], ignore_index=True),
                    source="ffd_next_raw_buffer_hotpath",
                )
                next_state = {
                    **checkpoint,
                    "schema_version": s.FFD_MODULE.LIVE_FFD_STATE_SCHEMA_VERSION,
                    "asset": key,
                    "last_observation_timestamp": s.utc_timestamp(
                        pending["Datetime"].max()
                    ).isoformat(),
                    "raw_buffer_df": next_raw.tail(
                        int(checkpoint["live_buffer_bars"])
                    ).reset_index(drop=True),
                    "rows_appended": int(checkpoint.get("rows_appended", 0))
                    + int(len(rows)),
                    "continuation_writer_contract":
                        s.FFD_MODULE.FFD_CONTINUATION_WRITER_CONTRACT,
                    "timestamp_transaction": ffd_transaction_commit["transaction"],
                    "dependency_generation_id": ffd_generation_id,
                }
                s.FFD_MODULE._atomic_joblib_dump(next_state, state_path)
                reloaded_state = portable_pickle.load(state_path)
                reloaded_txn = reloaded_state.get("timestamp_transaction") or {}
                if (
                    reloaded_state.get("dependency_generation_id") != ffd_generation_id
                    or reloaded_txn.get("last_generation_id") != ffd_generation_id
                ):
                    raise RuntimeError("FFD transactional checkpoint roundtrip failed")
            s.frame_extend("ffd", key, output_path, rows)
            s.FFD_DELTA[key] = rows.copy()
            proof = dict(result.get("continuation_proof") or {})
            proof.update(
                {
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "frozen_d_preserved": True,
                    "rows_appended": int(len(rows)),
                    "last_observation_timestamp": next_state[
                        "last_observation_timestamp"
                    ],
                    "state_path": s.FFD_MODULE._rel(state_path),
                    "hotpath_version": s.HOTPATH_VERSION,
                    "timestamp_transaction": dict(
                        ffd_transaction_commit["transaction"]
                    ),
                    "transaction_bytes_read": int(
                        ffd_transaction_commit["bytes_read_for_suffix_verification"]
                    ),
                    "transaction_bytes_appended": int(
                        ffd_transaction_commit["bytes_appended"]
                    ),
                    "transaction_durable_reused_rows": int(
                        ffd_transaction_commit["durable_reused_rows"]
                    ),
                }
            )
            result.update(
                {
                    "features": s.FFD_MODULE._rel(output_path),
                    "live_continuation_applied": True,
                    "live_continuation_warning": None,
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "frozen_d_preserved": True,
                    "continuation_proof": proof,
                }
            )
            s.cache_put(s.FFD_CACHE, key, signature, result)
            LOG.info("FFD_HOTPATH_APPEND asset=%s rows=%s", key, len(rows))
            return s.copy_result(result)
        except Exception as exc:
            if live_bars_override is not None:
                raise RuntimeError(
                    f"{key}: in-memory FFD dependency continuation failed closed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            return _fallback_ffd(
                key, signature, f"hot_append_failed:{type(exc).__name__}:{exc}"
            )


def _authoritative_hmm(
    asset: str,
    *,
    continuation_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if s.ORIGINAL_HMM_VERIFY is None or s.ORIGINAL_FFD_VERIFY is None:
        raise RuntimeError("HMM hotpath is not installed")
    globals_map = getattr(s.ORIGINAL_HMM_VERIFY, "__globals__", {})
    dependency = globals_map.get("verify_asset_ffd_manifest")
    if dependency is None:
        return s.copy_result(s.ORIGINAL_HMM_VERIFY(asset)), None
    captured: dict[str, Any] = {}

    def capture(
        requested_asset: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Preserve the canonical verifier's dependency-call contract.

        The HMM verifier forwards ``continuation_dir`` to its FFD dependency.
        This capture bridge must therefore remain signature-transparent; a
        one-positional-argument shim breaks both ordinary cold-cache startup
        and staged recovery after the verifier API gains keyword options.
        """
        if continuation_dir is not None:
            supplied = kwargs.get("continuation_dir")
            if supplied is not None and Path(supplied) != Path(continuation_dir):
                raise RuntimeError(
                    "HMM/FFD continuation directory mismatch "
                    f"hmm={continuation_dir} ffd={supplied}"
                )
            kwargs["continuation_dir"] = continuation_dir
        value = s.ORIGINAL_FFD_VERIFY(
            s.asset_key(requested_asset),
            *args,
            **kwargs,
        )
        captured["ffd"] = s.copy_result(value)
        return value

    with s.INSTALL_LOCK:
        globals_map["verify_asset_ffd_manifest"] = capture
        try:
            result = s.ORIGINAL_HMM_VERIFY(
                asset,
                continuation_dir=continuation_dir,
            )
        finally:
            globals_map["verify_asset_ffd_manifest"] = dependency
    return s.copy_result(result), captured.get("ffd")


def _fallback_hmm(asset: str, signature: Any, reason: str) -> dict[str, Any]:
    key = s.asset_key(asset)
    LOG.warning("HMM_HOTPATH_FALLBACK asset=%s reason=%s", key, reason)
    s.invalidate(key, ffd=True, hmm=True)
    result, ffd_result = _authoritative_hmm(key)
    s.invalidate(key, ffd=True, hmm=True)
    if ffd_result is not None:
        s.cache_put(s.FFD_CACHE, key, signature, ffd_result)
        s.FFD_DELTA[key] = pd.DataFrame()
    s.cache_put(s.HMM_CACHE, key, signature, result)
    return s.copy_result(result)


def _hmm_context(
    asset: str,
    verification: Mapping[str, Any],
    *,
    allow_disk: bool = True,
) -> dict[str, Any]:
    if s.HMM_MODULE is None:
        raise RuntimeError("HMM hotpath is not installed")
    key = s.asset_key(asset)
    manifest_path = Path(s.HMM_MODULE._V2_ROOT) / str(verification["manifest"])
    signature = s.path_signature(manifest_path)
    cached = s.HMM_CONTEXT.get(key)
    if cached and cached.get("signature") == signature:
        return cached
    if not allow_disk:
        raise RuntimeError(
            f"{key}: hmm_context_not_initialized_or_stale; "
            "startup/recovery hydration is required"
        )
    manifest = s.HMM_MODULE._load_manifest(manifest_path)
    state_count = int(manifest["n_states"])
    columns = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{index}" for index in range(state_count)],
        *s.HMM_MODULE.HMM_FEATURES,
    ]
    static = pd.read_csv(
        Path(s.HMM_MODULE._V2_ROOT) / str(verification["static_features"]),
        usecols=columns,
    )
    static["Datetime"] = s.HMM_MODULE._parse_utc_datetime_series(static["Datetime"])
    static = static.dropna(subset=["Datetime"]).sort_values(
        "Datetime", kind="mergesort"
    )
    static = static.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    bundle = portable_pickle.load(Path(s.HMM_MODULE._V2_ROOT) / str(verification["live_bundle"]))
    config = s.HMM_MODULE.HMMRegimeConfig(
        n_states=state_count,
        train_window=int(manifest["train_window"]),
        refit_frequency=int(manifest["refit_frequency"]),
        inference_lookback=int(manifest["inference_lookback"]),
        min_covar=float(manifest.get("min_covar", 0.01)),
        random_state=int(manifest.get("random_state", 42)),
        max_iter=int(manifest.get("max_iter", 200)),
        noise_epsilon=float(manifest.get("noise_epsilon", 1e-8)),
    )
    context = {
        "signature": signature,
        "manifest": manifest,
        "columns": columns,
        "static": static,
        "bundle": bundle,
        "config": config,
    }
    s.HMM_CONTEXT[key] = context
    return context


def _read_ffd_runtime_rows(result: Mapping[str, Any]) -> pd.DataFrame:
    """Read full verified FFD authority for startup/recovery hydration only.

    PHASE_B_STARTUP_RECOVERY_FFD_AUTHORITY_READ_V1

    This helper intentionally remains available to recovery/oracle tests, but
    normal HMM continuation and normal readiness must never call it.
    """
    if s.FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    path = Path(s.FFD_MODULE._V2_ROOT) / str(result["features"])
    columns = [
        "Datetime", "Open", "High", "Low", "Close",
        "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD",
    ]
    frame = pd.read_csv(path, usecols=columns)
    frame["Datetime"] = s.parse_utc_series(frame["Datetime"])
    for column in columns[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=columns).sort_values("Datetime", kind="mergesort")
    return frame.drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def _eligible_hmm_rows(
    frame: pd.DataFrame,
    config: Any,
    *,
    after: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if s.HMM_MODULE is None or frame is None or frame.empty:
        return pd.DataFrame()
    eligible = s.HMM_MODULE.engineer_hmm_inputs(frame.copy(), config=config)
    eligible["Datetime"] = s.parse_utc_series(eligible["Datetime"])
    eligible = eligible.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    eligible = eligible.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    if after is not None:
        eligible = eligible.loc[eligible["Datetime"] > after].reset_index(drop=True)
    return eligible




def _hmm_input_ring_capacity(context: Mapping[str, Any]) -> int:
    config = context["config"]
    # Preserve at least the live 6,000-row research window and enough rows for
    # one complete HMM train/refit interval.
    return max(
        6000,
        int(getattr(config, "train_window", 0) or 0)
        + int(getattr(config, "refit_frequency", 0) or 0)
        + 2,
        int(getattr(config, "inference_lookback", 0) or 0) + 2,
    )


def _dependency_frame_capacity(context: Mapping[str, Any]) -> int:
    """Bound live FFD/HMM snapshots without reducing research history.

    Current frozen research settings resolve to 6,002 rows:
    5,000 HMM train + 1,000 refit interval + a two-row safety margin.
    The floor remains 6,000 even if a future config is smaller.
    """
    return max(6000, _hmm_input_ring_capacity(context))

def _seed_hmm_input_ring_from_authority(
    asset: str,
    ffd_result: Mapping[str, Any],
    context: Mapping[str, Any],
) -> pd.DataFrame:
    """Startup/recovery-only hydration from persistent FFD authority."""
    key = s.asset_key(asset)
    source = _read_ffd_runtime_rows(ffd_result)
    eligible = _eligible_hmm_rows(source, context["config"])
    if eligible.empty:
        raise RuntimeError(
            f"{key}: startup/recovery HMM eligibility ring is empty"
        )
    return s.hmm_input_ring_set(
        key,
        eligible,
        capacity=_hmm_input_ring_capacity(context),
    )


def _refresh_hmm_input_ring_from_ffd_delta(
    asset: str,
    context: Mapping[str, Any],
) -> pd.DataFrame:
    """Advance eligibility only from exact FFD rows committed this generation."""
    key = s.asset_key(asset)
    current = s.hmm_input_ring_get(key)
    if current is None:
        raise RuntimeError(
            f"{key}: hmm_input_ring_not_initialized; "
            "normal continuation cannot read full FFD authority"
        )
    delta = s.FFD_DELTA.get(key)
    if isinstance(delta, pd.DataFrame) and not delta.empty:
        eligible_delta = _eligible_hmm_rows(delta, context["config"])
        if not eligible_delta.empty:
            current = s.hmm_input_ring_extend(key, eligible_delta)
    return current


def _pending_hmm_eligible_rows(
    asset: str,
    *,
    after: pd.Timestamp,
) -> pd.DataFrame:
    ring = s.hmm_input_ring_get(asset)
    if ring is None:
        raise RuntimeError(
            f"{s.asset_key(asset)}: hmm_input_ring_not_initialized"
        )
    if ring.empty:
        return ring
    work = ring.copy(deep=False)
    work["Datetime"] = s.parse_utc_series(work["Datetime"])
    return (
        work.loc[work["Datetime"] > s.utc_timestamp(after)]
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )

def _validate_hmm_output_rows(frame: pd.DataFrame, n_states: int) -> None:
    required = [
        "Datetime", "Regime_State", "Regime_Entropy", "Regime_Duration",
        *[f"Prob_State_{index}" for index in range(int(n_states))],
    ]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise RuntimeError(f"HMM output missing columns: {missing}")
    if frame.empty:
        return
    numeric = frame[required[1:]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise RuntimeError("HMM output contains non-finite values")
    probabilities = numeric[[column for column in required if column.startswith("Prob_State_")]]
    if ((probabilities < -1e-9) | (probabilities > 1.0 + 1e-9)).any().any():
        raise RuntimeError("HMM probabilities are outside [0,1]")
    if not np.allclose(probabilities.sum(axis=1).to_numpy(), 1.0, atol=1e-5):
        raise RuntimeError("HMM probability rows do not sum to one")


def _refresh_hmm_proof_watermarks(
    proof: Mapping[str, Any],
    *,
    ffd_source: pd.DataFrame | None = None,
    ffd_latest: Any | None = None,
    hmm_target: Any,
) -> dict[str, Any]:
    # PHASE_A_HMM_PROOF_WATERMARK_V1
    #
    # The hot path starts from the prior authoritative verifier result. After a
    # successful append its diagnostic expected_hmm_latest_timestamp must move
    # with the committed generation; otherwise recovery can compare a new HMM
    # runtime tail to an old proof watermark and rebuild a healthy generation.
    updated = dict(proof)
    if ffd_latest is None:
        if (
            ffd_source is None
            or ffd_source.empty
            or "Datetime" not in ffd_source.columns
        ):
            raise RuntimeError(
                "HMM proof watermark refresh requires FFD latest authority"
            )
        parsed = s.parse_utc_series(ffd_source["Datetime"]).dropna()
        if parsed.empty:
            raise RuntimeError(
                "HMM proof watermark refresh found no valid FFD timestamp"
            )
        ffd_latest = parsed.max()
    updated["source_ffd_latest_timestamp"] = (
        s.utc_timestamp(ffd_latest).isoformat()
    )
    updated["expected_hmm_latest_timestamp"] = (
        s.utc_timestamp(hmm_target).isoformat()
    )
    return updated


def verify_asset_hmm_regime_manifest_hot(
    asset: str,
    *,
    live_bars_override: pd.DataFrame | None = None,
    continuation_dir: Path | None = None,
) -> dict[str, Any]:
    if s.HMM_MODULE is None or s.ORIGINAL_HMM_VERIFY is None:
        raise RuntimeError("HMM hotpath is not installed")
    key = s.asset_key(asset)
    if continuation_dir is not None:
        return s.copy_result(
            s.ORIGINAL_HMM_VERIFY(
                key,
                continuation_dir=continuation_dir,
            )
        )
    snapshot = s.current_feature_snapshot(key)
    if snapshot is not None and isinstance(snapshot.get("hmm_verification"), Mapping):
        return s.copy_result(snapshot["hmm_verification"])
    signature = _effective_signature(key, live_bars_override)
    cached = s.cache_get(s.HMM_CACHE, key, signature)
    if cached is not None:
        return cached
    with s.asset_lock(key):
        signature = _effective_signature(key, live_bars_override)
        cached = s.cache_get(s.HMM_CACHE, key, signature)
        if cached is not None:
            return cached
        prior = s.HMM_CACHE.get(key)
        if prior is None:
            if live_bars_override is not None:
                raise RuntimeError(
                    f"{key}: cold_cache_not_initialized; authoritative HMM initialization "
                    "is restricted to startup/dedicated recovery"
                )
            initial = _fallback_hmm(
                key,
                s.live_signature(key),
                "initial_authoritative_verification",
            )
            if live_bars_override is None:
                return initial
            prior = s.HMM_CACHE.get(key)
            if prior is None:
                raise RuntimeError("authoritative HMM initialization did not populate cache")
        try:
            result = s.copy_result(prior["result"])
            ffd_result = verify_asset_ffd_manifest_hot(
                key, live_bars_override=live_bars_override
            )
            if not ffd_result.get("streaming_parity_passed"):
                raise RuntimeError("FFD parity not proven")
            output_path = Path(s.HMM_MODULE._V2_ROOT) / str(result["features"])
            state_path = (
                Path(s.HMM_MODULE.LIVE_CONTINUATION_DIR)
                / f"{key.lower()}_hmm_streaming_state.joblib"
            )
            if not output_path.exists() or not state_path.exists():
                if live_bars_override is not None:
                    raise RuntimeError(
                        f"{key}: state_or_output_missing; authoritative HMM rebuild "
                        "is restricted to dedicated recovery"
                    )
                return _fallback_hmm(key, signature, "state_or_output_missing")
            with s.HMM_MODULE._exclusive_output_lock(output_path):
                checkpoint = portable_pickle.load(state_path)
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("schema_version")
                    != s.HMM_MODULE.LIVE_HMM_STATE_SCHEMA_VERSION
                    or checkpoint.get("continuation_writer_contract")
                    != s.HMM_MODULE.HMM_CONTINUATION_WRITER_CONTRACT
                    or not dependency_txn.transaction_metadata_valid(
                        checkpoint.get("timestamp_transaction")
                    )
                ):
                    raise RuntimeError(
                        "checkpoint_schema_writer_or_transaction_contract_mismatch"
                    )
                dependency_txn.assert_checkpoint_watermark(
                    checkpoint["timestamp_transaction"],
                    checkpoint.get("last_observation_timestamp"),
                    dependency="HMM",
                )
                last = s.utc_timestamp(checkpoint["last_observation_timestamp"])
                context = _hmm_context(
                    key,
                    result,
                    allow_disk=False,
                )
                # PHASE_B_HMM_INPUT_RING_V1
                # The ring is hydrated before workers start (or by dedicated
                # recovery) and is then advanced only from this generation's FFD
                # delta. A prior HMM miss remains in the ring for exact retry.
                _refresh_hmm_input_ring_from_ffd_delta(key, context)
                eligible = _pending_hmm_eligible_rows(key, after=last)
                ffd_transaction = _verification_transaction(
                    ffd_result,
                    "FFD",
                    asset=key,
                )
                ffd_latest = s.utc_timestamp(
                    ffd_transaction["committed_last_timestamp"]
                )
                if eligible.empty:
                    dependency_txn.assert_committed_boundary(
                        output_path,
                        checkpoint["timestamp_transaction"],
                    )
                    result["continuation_proof"] = (
                        _refresh_hmm_proof_watermarks(
                            result.get("continuation_proof") or {},
                            ffd_latest=ffd_latest,
                            hmm_target=last,
                        )
                    )
                    s.cache_put(s.HMM_CACHE, key, signature, result)
                    return s.copy_result(result)
                rows, next_state, metadata = s.HMM_MODULE.continue_hmm_regime_rows(
                    eligible,
                    live_bundle=context["bundle"],
                    static_regime=context["static"],
                    total_static_processed_rows=int(
                        context["manifest"]["rows_input_after_preprocessing"]
                    ),
                    state=checkpoint,
                    config=context["config"],
                )
                expected_times = pd.DatetimeIndex(to_datetime_iso(eligible["Datetime"], utc=True))
                produced_times = pd.DatetimeIndex(to_datetime_iso(rows["Datetime"], utc=True))
                if not produced_times.equals(expected_times):
                    missing = sorted(set(expected_times).difference(set(produced_times)))
                    unexpected = sorted(set(produced_times).difference(set(expected_times)))
                    raise RuntimeError(
                        "HMM eligible timestamp coverage mismatch "
                        f"missing={[pd.Timestamp(value).isoformat() for value in missing[:5]]} "
                        f"unexpected={[pd.Timestamp(value).isoformat() for value in unexpected[:5]]}"
                    )
                _validate_hmm_output_rows(rows, int(context["config"].n_states))
                hmm_generation_id = dependency_txn.generation_id(
                    key,
                    "HMM",
                    prior_watermark=last.isoformat(),
                    target_watermark=s.utc_timestamp(
                        expected_times[-1]
                    ).isoformat(),
                    timestamps=expected_times,
                )
                hmm_transaction_commit = dependency_txn.commit_csv_generation(
                    output_path,
                    transaction=checkpoint["timestamp_transaction"],
                    expected_times=expected_times,
                    produced_rows=rows,
                    columns=context["columns"],
                    generation_id_value=hmm_generation_id,
                )
                next_state["continuation_writer_contract"] = (
                    s.HMM_MODULE.HMM_CONTINUATION_WRITER_CONTRACT
                )
                next_state["timestamp_transaction"] = (
                    hmm_transaction_commit["transaction"]
                )
                next_state["dependency_generation_id"] = hmm_generation_id
                s.HMM_MODULE._atomic_joblib_dump(next_state, state_path)
                reloaded_state = portable_pickle.load(state_path)
                reloaded_txn = reloaded_state.get("timestamp_transaction") or {}
                if (
                    reloaded_state.get("dependency_generation_id") != hmm_generation_id
                    or reloaded_txn.get("last_generation_id") != hmm_generation_id
                ):
                    raise RuntimeError("HMM transactional checkpoint roundtrip failed")
            s.frame_extend("hmm", key, output_path, rows)
            proof = dict(result.get("continuation_proof") or {})
            proof.update(metadata)
            proof = _refresh_hmm_proof_watermarks(
                proof,
                ffd_latest=ffd_latest,
                hmm_target=expected_times[-1],
            )
            proof.update(
                {
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "hmm_refit_schedule_preserved": True,
                    "deterministic_noise_contract_preserved": True,
                    "state_path": s.HMM_MODULE._rel(state_path),
                    "rows_appended": int(len(rows)),
                    "ffd_continuation_proof": ffd_result.get("continuation_proof"),
                    "hotpath_version": s.HOTPATH_VERSION,
                    "eligibility_filtered_input_rows": int(len(eligible)),
                    "timestamp_transaction": dict(
                        hmm_transaction_commit["transaction"]
                    ),
                    "transaction_bytes_read": int(
                        hmm_transaction_commit["bytes_read_for_suffix_verification"]
                    ),
                    "transaction_bytes_appended": int(
                        hmm_transaction_commit["bytes_appended"]
                    ),
                    "transaction_durable_reused_rows": int(
                        hmm_transaction_commit["durable_reused_rows"]
                    ),
                }
            )
            result.update(
                {
                    "features": s.HMM_MODULE._rel(output_path),
                    "live_continuation_applied": True,
                    "live_continuation_warning": None,
                    "streaming_parity_passed": True,
                    "append_only_history": True,
                    "restart_parity_passed": True,
                    "hmm_refit_schedule_preserved": True,
                    "deterministic_noise_contract_preserved": True,
                    "continuation_proof": proof,
                }
            )
            s.cache_put(s.HMM_CACHE, key, signature, result)
            LOG.info("HMM_HOTPATH_APPEND asset=%s rows=%s", key, len(rows))
            return s.copy_result(result)
        except Exception as exc:
            if live_bars_override is not None:
                raise RuntimeError(
                    f"{key}: in-memory HMM dependency continuation failed closed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            return _fallback_hmm(
                key, signature, f"hot_append_failed:{type(exc).__name__}:{exc}"
            )


def _refresh_verified_feature_dependencies_hot_locked(
    asset: str,
    bars: pd.DataFrame,
) -> dict[str, Any]:
    """Advance verified FFD and HMM state through the supplied closed-bar snapshot.

    This is the critical scoring dependency path.  It consumes only timestamps
    newer than the persisted continuation checkpoints, is guarded by the existing
    per-asset lock, and never modifies bar construction, journals, or compaction.
    """
    key = s.asset_key(asset)
    effective = _normalized_effective_live_bars(key, bars)
    if effective.empty:
        raise RuntimeError(f"{key}: effective live-bar snapshot is empty")
    raw_target = s.utc_timestamp(effective["Datetime"].iloc[-1])
    ffd_started = time.perf_counter()
    ffd_verification = verify_asset_ffd_manifest_hot(
        key, live_bars_override=effective
    )
    ffd_elapsed_ms = (time.perf_counter() - ffd_started) * 1000.0
    hmm_started = time.perf_counter()
    hmm_verification = verify_asset_hmm_regime_manifest_hot(
        key, live_bars_override=effective
    )
    hmm_elapsed_ms = (time.perf_counter() - hmm_started) * 1000.0
    if s.FFD_MODULE is None or s.HMM_MODULE is None:
        raise RuntimeError("FFD/HMM hotpath is not installed")
    ffd_path = Path(s.FFD_MODULE._V2_ROOT) / str(ffd_verification["features"])
    hmm_path = Path(s.HMM_MODULE._V2_ROOT) / str(hmm_verification["features"])
    ffd_frame = s.frame_cached(
        "ffd", key, ffd_path,
        ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"],
    )
    eligibility_ring = s.hmm_input_ring_get(key)
    if eligibility_ring is None or eligibility_ring.empty:
        raise RuntimeError(
            f"{key}: HMM eligibility ring is not warm after continuation"
        )
    ffd_transaction = _verification_transaction(
        ffd_verification,
        "FFD",
        asset=key,
    )
    hmm_transaction = _verification_transaction(
        hmm_verification,
        "HMM",
        asset=key,
    )
    ffd_committed = s.utc_timestamp(
        ffd_transaction["committed_last_timestamp"]
    )
    hmm_target = s.utc_timestamp(
        hmm_transaction["committed_last_timestamp"]
    )
    if ffd_committed != raw_target:
        raise RuntimeError(
            f"{key}: FFD committed watermark did not reach raw target "
            f"{ffd_committed.isoformat()} != {raw_target.isoformat()}"
        )
    hmm_columns = [
        "Datetime", "Regime_State", "Regime_Entropy", "Regime_Duration",
        *[f"Prob_State_{index}" for index in range(int(hmm_verification["n_states"]))],
    ]
    hmm_frame = s.frame_cached("hmm", key, hmm_path, hmm_columns)
    ffd_tail = ffd_frame.tail(2).copy(deep=False)
    hmm_tail = hmm_frame.tail(2).copy(deep=False)
    ffd_tail["Datetime"] = s.parse_utc_series(ffd_tail["Datetime"])
    hmm_tail["Datetime"] = s.parse_utc_series(hmm_tail["Datetime"])
    ffd_present = bool((ffd_tail["Datetime"] == raw_target).any())
    hmm_present = bool((hmm_tail["Datetime"] == hmm_target).any())
    if not ffd_present or not hmm_present:
        raise RuntimeError(
            f"{key}: dependency refresh did not reach targets "
            f"ffd_target={raw_target.isoformat()} ffd_present={ffd_present} "
            f"hmm_target={hmm_target.isoformat()} hmm_present={hmm_present}"
        )
    hmm_target_row = hmm_tail.loc[hmm_tail["Datetime"] == hmm_target]
    _validate_hmm_output_rows(hmm_target_row, int(hmm_verification["n_states"]))
    dependency_generation = _publish_ready_dependency_generation(
        asset=key,
        raw_target=raw_target,
        hmm_target=hmm_target,
        ffd_verification=ffd_verification,
        hmm_verification=hmm_verification,
        source="normal_hotpath_exact_snapshot",
    )
    return {
        "asset": key,
        "target_timestamp": raw_target.isoformat(),
        "hmm_target_timestamp": hmm_target.isoformat(),
        "target_hmm_eligible": bool(hmm_target == raw_target),
        "hmm_eligible_timestamps": [
            s.utc_timestamp(value).isoformat()
            for value in eligibility_ring["Datetime"].tail(512).tolist()
            if s.utc_timestamp(value) <= raw_target
        ],
        "ffd": ffd_frame,
        "hmm": hmm_frame,
        "ffd_verification": ffd_verification,
        "hmm_verification": hmm_verification,
        "ffd_elapsed_ms": ffd_elapsed_ms,
        "hmm_elapsed_ms": hmm_elapsed_ms,
        "dependency_refresh_mode": "phase_b_bounded_dependency_rings_exact_closed_bar_snapshot",
        "dependency_generation_id": dependency_generation["generation_id"],
        "dependency_generation": dependency_generation,
    }


def refresh_verified_feature_dependencies_hot(
    asset: str,
    bars: pd.DataFrame,
) -> dict[str, Any]:
    """Single-owner bar -> FFD -> HMM dependency generation for one asset."""
    key = s.asset_key(asset)
    with s.asset_lock(key):
        return _refresh_verified_feature_dependencies_hot_locked(key, bars)


def hydrate_verified_feature_dependencies_from_authority(
    asset: str,
    *,
    source: str = "dedicated_recovery",
) -> dict[str, Any]:
    """Hydrate every hot cache from independently verified persistent authority.

    Recovery is not complete until this function succeeds. It deliberately uses
    the no-override verifier path, so a repaired disk generation becomes the warm
    base from which the next in-memory bar can continue incrementally.
    """
    key = s.asset_key(asset)
    with s.asset_lock(key):
        if s.FFD_MODULE is None or s.HMM_MODULE is None:
            raise RuntimeError("FFD/HMM hotpath is not installed")
        ffd_verification = verify_asset_ffd_manifest_hot(key)
        hmm_verification = verify_asset_hmm_regime_manifest_hot(key)
        context = _hmm_context(
            key,
            hmm_verification,
            allow_disk=True,
        )
        eligibility_ring = _seed_hmm_input_ring_from_authority(
            key,
            ffd_verification,
            context,
        )
        frame_capacity = _dependency_frame_capacity(context)
        ffd_path = Path(s.FFD_MODULE._V2_ROOT) / str(
            ffd_verification["features"]
        )
        hmm_path = Path(s.HMM_MODULE._V2_ROOT) / str(
            hmm_verification["features"]
        )
        ffd_frame = s.frame_load(
            "ffd",
            key,
            ffd_path,
            ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"],
            max_rows=frame_capacity,
        )
        hmm_columns = [
            "Datetime",
            "Regime_State",
            "Regime_Entropy",
            "Regime_Duration",
            *[
                f"Prob_State_{index}"
                for index in range(int(hmm_verification["n_states"]))
            ],
        ]
        hmm_frame = s.frame_load(
            "hmm",
            key,
            hmm_path,
            hmm_columns,
            max_rows=frame_capacity,
        )
        ffd_transaction = _verification_transaction(ffd_verification, "FFD", asset=asset)
        hmm_transaction = _verification_transaction(hmm_verification, "HMM", asset=asset)
        raw_target = s.utc_timestamp(
            ffd_transaction["committed_last_timestamp"]
        )
        hmm_target = s.utc_timestamp(
            hmm_transaction["committed_last_timestamp"]
        )
        ffd_times = set(
            to_datetime_iso(ffd_frame["Datetime"], utc=True, errors="coerce")
        )
        hmm_times = set(
            to_datetime_iso(hmm_frame["Datetime"], utc=True, errors="coerce")
        )
        if raw_target not in ffd_times:
            raise RuntimeError(
                f"{key}: recovery hydration FFD target absent "
                f"target={raw_target.isoformat()}"
            )
        if hmm_target not in hmm_times:
            raise RuntimeError(
                f"{key}: recovery hydration HMM target absent "
                f"target={hmm_target.isoformat()}"
            )
        hmm_target_row = hmm_frame.loc[
            to_datetime_iso(
                hmm_frame["Datetime"], utc=True, errors="coerce"
            )
            == hmm_target
        ]
        _validate_hmm_output_rows(
            hmm_target_row,
            int(hmm_verification["n_states"]),
        )
        dependency_generation = _publish_ready_dependency_generation(
            asset=key,
            raw_target=raw_target,
            hmm_target=hmm_target,
            ffd_verification=ffd_verification,
            hmm_verification=hmm_verification,
            source=source,
        )
        required_frame_entries = {f"ffd:{key}", f"hmm:{key}"}
        frame_entries = {
            f"{kind}:{asset_name}"
            for kind, asset_name in s.FRAME_CACHE
        }
        if key not in s.FFD_CACHE:
            raise RuntimeError(f"{key}: recovery hydration left FFD cache cold")
        if key not in s.HMM_CACHE:
            raise RuntimeError(f"{key}: recovery hydration left HMM cache cold")
        if not required_frame_entries.issubset(frame_entries):
            raise RuntimeError(
                f"{key}: recovery hydration frame cache incomplete "
                f"required={sorted(required_frame_entries)} "
                f"actual={sorted(frame_entries)}"
            )
        current = s.current_dependency_generation(key)
        generation_contract.assert_ready_generation(
            current or {},
            asset=key,
            raw_target=raw_target,
            hmm_target=hmm_target,
        )
        return {
            "asset": key,
            "status": "READY",
            "target_timestamp": raw_target.isoformat(),
            "hmm_target_timestamp": hmm_target.isoformat(),
            "dependency_generation_id": dependency_generation["generation_id"],
            "dependency_generation": dependency_generation,
            "ffd": ffd_frame,
            "hmm": hmm_frame,
            "ffd_verification": ffd_verification,
            "hmm_verification": hmm_verification,
            "hot_cache_hydrated": True,
            "dependency_frame_capacity": int(frame_capacity),
            "ffd_frame_rows": int(len(ffd_frame)),
            "hmm_frame_rows": int(len(hmm_frame)),
            "hmm_input_ring_rows": int(len(eligibility_ring)),
            "hmm_input_ring_latest": (
                s.utc_timestamp(
                    eligibility_ring["Datetime"].iloc[-1]
                ).isoformat()
                if len(eligibility_ring)
                else None
            ),
        }


def load_verified_ffd_features_hot(asset: str) -> pd.DataFrame:
    snapshot = s.current_feature_snapshot(asset)
    if snapshot is not None and isinstance(snapshot.get("ffd"), pd.DataFrame):
        return snapshot["ffd"].copy(deep=True)
    if s.FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    verification = verify_asset_ffd_manifest_hot(asset)
    path = Path(s.FFD_MODULE._V2_ROOT) / str(verification["features"])
    return s.frame_load(
        "ffd", asset, path, ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    )


def load_verified_hmm_features_hot(asset: str) -> pd.DataFrame:
    snapshot = s.current_feature_snapshot(asset)
    if snapshot is not None and isinstance(snapshot.get("hmm"), pd.DataFrame):
        return snapshot["hmm"].copy(deep=True)
    if s.HMM_MODULE is None:
        raise RuntimeError("HMM hotpath is not installed")
    verification = verify_asset_hmm_regime_manifest_hot(asset)
    columns = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{index}" for index in range(int(verification["n_states"]))],
    ]
    path = Path(s.HMM_MODULE._V2_ROOT) / str(verification["features"])
    return s.frame_load("hmm", asset, path, columns)
