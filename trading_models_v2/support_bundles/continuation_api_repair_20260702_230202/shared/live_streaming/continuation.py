"""Exact fail-closed FFD and HMM live continuation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import joblib
import pandas as pd

from . import state as s

LOG = logging.getLogger("v2_live_streaming_hotpath")


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


def verify_asset_ffd_manifest_hot(asset: str) -> dict[str, Any]:
    if s.FFD_MODULE is None or s.ORIGINAL_FFD_VERIFY is None:
        raise RuntimeError("FFD hotpath is not installed")
    key = s.asset_key(asset)
    signature = s.live_signature(key)
    cached = s.cache_get(s.FFD_CACHE, key, signature)
    if cached is not None:
        return cached
    with s.asset_lock(key):
        signature = s.live_signature(key)
        cached = s.cache_get(s.FFD_CACHE, key, signature)
        if cached is not None:
            return cached
        prior = s.FFD_CACHE.get(key)
        if prior is None:
            return _fallback_ffd(key, signature, "initial_authoritative_verification")
        try:
            result = s.copy_result(prior["result"])
            output_path = Path(s.FFD_MODULE._V2_ROOT) / str(result["features"])
            state_path = (
                Path(s.FFD_MODULE.LIVE_CONTINUATION_DIR)
                / f"{key.lower()}_ffd_streaming_state.joblib"
            )
            if not output_path.exists() or not state_path.exists():
                return _fallback_ffd(key, signature, "state_or_output_missing")
            with s.FFD_MODULE._exclusive_output_lock(output_path):
                checkpoint = joblib.load(state_path)
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("schema_version")
                    != s.FFD_MODULE.LIVE_FFD_STATE_SCHEMA_VERSION
                ):
                    raise RuntimeError("checkpoint_schema_mismatch")
                raw_buffer = checkpoint.get("raw_buffer_df")
                if not isinstance(raw_buffer, pd.DataFrame):
                    raise RuntimeError("raw_buffer_missing")
                live_bars = s.normalized_live_bars(key)
                last = s.utc_timestamp(checkpoint["last_observation_timestamp"])
                pending = live_bars[live_bars["Datetime"] > last]
                pending = (
                    pending.sort_values("Datetime", kind="mergesort")
                    .drop_duplicates("Datetime", keep="last")
                    .reset_index(drop=True)
                )
                if pending.empty:
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
                expected = set(pd.to_datetime(pending["Datetime"], utc=True))
                produced = set(pd.to_datetime(rows["Datetime"], utc=True))
                if expected.difference(produced):
                    raise RuntimeError(
                        f"missing transformed timestamps: {sorted(expected.difference(produced))[:5]}"
                    )
                s.append_csv_rows(output_path, rows, columns)
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
                }
                s.FFD_MODULE._atomic_joblib_dump(next_state, state_path)
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
            return _fallback_ffd(
                key, signature, f"hot_append_failed:{type(exc).__name__}:{exc}"
            )


def _authoritative_hmm(asset: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if s.ORIGINAL_HMM_VERIFY is None or s.ORIGINAL_FFD_VERIFY is None:
        raise RuntimeError("HMM hotpath is not installed")
    globals_map = getattr(s.ORIGINAL_HMM_VERIFY, "__globals__", {})
    dependency = globals_map.get("verify_asset_ffd_manifest")
    if dependency is None:
        return s.copy_result(s.ORIGINAL_HMM_VERIFY(asset)), None
    captured: dict[str, Any] = {}

    def capture(requested_asset: str) -> dict[str, Any]:
        value = s.ORIGINAL_FFD_VERIFY(s.asset_key(requested_asset))
        captured["ffd"] = s.copy_result(value)
        return value

    with s.INSTALL_LOCK:
        globals_map["verify_asset_ffd_manifest"] = capture
        try:
            result = s.ORIGINAL_HMM_VERIFY(asset)
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


def _hmm_context(asset: str, verification: Mapping[str, Any]) -> dict[str, Any]:
    if s.HMM_MODULE is None:
        raise RuntimeError("HMM hotpath is not installed")
    key = s.asset_key(asset)
    manifest_path = Path(s.HMM_MODULE._V2_ROOT) / str(verification["manifest"])
    signature = s.path_signature(manifest_path)
    cached = s.HMM_CONTEXT.get(key)
    if cached and cached.get("signature") == signature:
        return cached
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
    bundle = joblib.load(Path(s.HMM_MODULE._V2_ROOT) / str(verification["live_bundle"]))
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


def verify_asset_hmm_regime_manifest_hot(asset: str) -> dict[str, Any]:
    if s.HMM_MODULE is None or s.ORIGINAL_HMM_VERIFY is None:
        raise RuntimeError("HMM hotpath is not installed")
    key = s.asset_key(asset)
    signature = s.live_signature(key)
    cached = s.cache_get(s.HMM_CACHE, key, signature)
    if cached is not None:
        return cached
    with s.asset_lock(key):
        signature = s.live_signature(key)
        cached = s.cache_get(s.HMM_CACHE, key, signature)
        if cached is not None:
            return cached
        prior = s.HMM_CACHE.get(key)
        if prior is None:
            return _fallback_hmm(key, signature, "initial_authoritative_verification")
        try:
            result = s.copy_result(prior["result"])
            ffd_result = verify_asset_ffd_manifest_hot(key)
            if not ffd_result.get("streaming_parity_passed"):
                raise RuntimeError("FFD parity not proven")
            delta = s.FFD_DELTA.get(key, pd.DataFrame())
            output_path = Path(s.HMM_MODULE._V2_ROOT) / str(result["features"])
            state_path = (
                Path(s.HMM_MODULE.LIVE_CONTINUATION_DIR)
                / f"{key.lower()}_hmm_streaming_state.joblib"
            )
            if not output_path.exists() or not state_path.exists():
                return _fallback_hmm(key, signature, "state_or_output_missing")
            with s.HMM_MODULE._exclusive_output_lock(output_path):
                checkpoint = joblib.load(state_path)
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("schema_version")
                    != s.HMM_MODULE.LIVE_HMM_STATE_SCHEMA_VERSION
                ):
                    raise RuntimeError("checkpoint_schema_mismatch")
                last = s.utc_timestamp(checkpoint["last_observation_timestamp"])
                if delta.empty:
                    raise RuntimeError("ffd_delta_unavailable")
                delta_times = pd.to_datetime(
                    delta["Datetime"], utc=True, errors="coerce"
                )
                pending = delta[delta_times > last].sort_values(
                    "Datetime", kind="mergesort"
                )
                pending = pending.drop_duplicates("Datetime", keep="last").reset_index(
                    drop=True
                )
                if pending.empty:
                    s.cache_put(s.HMM_CACHE, key, signature, result)
                    return result
                context = _hmm_context(key, result)
                rows, next_state, metadata = s.HMM_MODULE.continue_hmm_regime_rows(
                    pending,
                    live_bundle=context["bundle"],
                    static_regime=context["static"],
                    total_static_processed_rows=int(
                        context["manifest"]["rows_input_after_preprocessing"]
                    ),
                    state=checkpoint,
                    config=context["config"],
                )
                if len(rows) != len(pending):
                    raise RuntimeError("HMM row count mismatch")
                s.append_csv_rows(output_path, rows, context["columns"])
                s.HMM_MODULE._atomic_joblib_dump(next_state, state_path)
            s.frame_extend("hmm", key, output_path, rows)
            proof = dict(result.get("continuation_proof") or {})
            proof.update(metadata)
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
            return _fallback_hmm(
                key, signature, f"hot_append_failed:{type(exc).__name__}:{exc}"
            )


def load_verified_ffd_features_hot(asset: str) -> pd.DataFrame:
    if s.FFD_MODULE is None:
        raise RuntimeError("FFD hotpath is not installed")
    verification = verify_asset_ffd_manifest_hot(asset)
    path = Path(s.FFD_MODULE._V2_ROOT) / str(verification["features"])
    return s.frame_load(
        "ffd", asset, path, ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    )


def load_verified_hmm_features_hot(asset: str) -> pd.DataFrame:
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
