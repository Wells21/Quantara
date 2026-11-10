from __future__ import annotations

"""Production HMM continuation integrity and deterministic repair helpers.

This module does not change HMM features, model parameters, state mapping, noise,
refit cadence, or inference math. It validates the rows produced by the existing
shared HMM runtime and, when an HMM-eligible live timestamp is missing/corrupt,
forces a clean rebuild through the same authoritative verifier.
"""

import copy
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[1]))
    from shared.io.timeparse import to_datetime_iso


HMM_INTEGRITY_VERSION = "2026-08-12_hmm_continuation_integrity_v3_canonical_eligibility"
_INSTALL_LOCK = threading.RLock()
_INSTALLED = False
_ORIGINAL_CONTINUE: Any = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def parse_utc(values: Any) -> pd.Series:
    raw = values.copy() if isinstance(values, pd.Series) else pd.Series(values)
    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = to_datetime_iso(raw, utc=True, errors="coerce")
    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        recovered: list[Any] = []
        for value in raw.loc[missed]:
            try:
                recovered.append(ensure_utc(value))
            except Exception:
                recovered.append(pd.NaT)
        parsed.loc[missed] = recovered
    return parsed


def dependency_contract_version() -> str:
    try:
        from shared.regime.verify_asset_hmm_regime_manifest import (
            HMM_CONTINUATION_WRITER_CONTRACT,
        )
        writer = str(HMM_CONTINUATION_WRITER_CONTRACT)
    except Exception:
        writer = "unknown_writer"
    return f"{HMM_INTEGRITY_VERSION}:{writer}"


def tombstone_key(asset: str, timestamp: Any) -> str:
    stamp = ensure_utc(timestamp).isoformat()
    payload = f"{str(asset).upper()}|{stamp}|{dependency_contract_version()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def required_hmm_columns(n_states: int) -> list[str]:
    return [
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{index}" for index in range(int(n_states))],
    ]


def validate_hmm_rows(
    frame: pd.DataFrame,
    *,
    n_states: int,
    timestamps: Iterable[Any] | None = None,
    require_hmm_inputs: bool = False,
) -> dict[str, Any]:
    """Validate finite state/probability rows without altering their values."""
    from shared.regime.hmm_regime import HMM_FEATURES

    required = required_hmm_columns(n_states)
    if require_hmm_inputs:
        required.extend(HMM_FEATURES)
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        return {
            "status": "FAIL",
            "missing_timestamps": [],
            "invalid_by_timestamp": {"<frame>": ["Datetime", *required]},
            "checked_rows": 0,
        }
    work = frame.copy()
    work["Datetime"] = parse_utc(work["Datetime"])
    work = work.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    work = work.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    requested: list[pd.Timestamp] = []
    if timestamps is not None:
        for value in timestamps:
            try:
                requested.append(ensure_utc(value))
            except Exception:
                continue
        requested = sorted(set(requested))
        work = work.loc[work["Datetime"].isin(requested)].copy()
    missing_timestamps: list[str] = []
    if requested:
        present = set(work["Datetime"])
        missing_timestamps = [value.isoformat() for value in requested if value not in present]

    invalid_by_timestamp: dict[str, list[str]] = {}
    for _, row in work.iterrows():
        invalid: list[str] = []
        for column in required:
            if column not in work.columns:
                invalid.append(column)
                continue
            value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
            try:
                if pd.isna(value) or not np.isfinite(float(value)):
                    invalid.append(column)
            except Exception:
                invalid.append(column)
        if not invalid:
            state = float(row["Regime_State"])
            duration = float(row["Regime_Duration"])
            if abs(state - round(state)) > 1e-9 or state < 0 or state >= int(n_states):
                invalid.append("Regime_State_domain")
            if abs(duration - round(duration)) > 1e-9 or duration < 1:
                invalid.append("Regime_Duration_domain")
            probabilities = [float(row[f"Prob_State_{i}"]) for i in range(int(n_states))]
            if any(value < -1e-9 or value > 1.0 + 1e-9 for value in probabilities):
                invalid.append("probability_out_of_bounds")
            if abs(sum(probabilities) - 1.0) > 1e-5:
                invalid.append("probability_sum_not_one")
            entropy = float(row["Regime_Entropy"])
            if entropy < -1e-9 or entropy > math.log(max(int(n_states), 1)) + 1e-5:
                invalid.append("Regime_Entropy_domain")
        if invalid:
            invalid_by_timestamp[ensure_utc(row["Datetime"]).isoformat()] = sorted(set(invalid))

    status = "PASS" if not missing_timestamps and not invalid_by_timestamp else "FAIL"
    return {
        "status": status,
        "missing_timestamps": missing_timestamps,
        "invalid_by_timestamp": invalid_by_timestamp,
        "checked_rows": int(len(work)),
        "required_columns": required,
    }


def classify_candidate_hmm_coverage(
    features: pd.DataFrame,
    candidate_timestamps: Iterable[Any],
    *,
    n_states: int | None = None,
) -> dict[str, Any]:
    """Separate valid, research-ineligible and genuinely repairable timestamps.

    `engineer_hmm_inputs` is the same preprocessing authority used by research and
    live HMM continuation. A timestamp excluded there must not be synthetically
    filled; it is terminally ineligible for any model requiring HMM fields.
    """
    from shared.regime.hmm_regime import HMMRegimeConfig, engineer_hmm_inputs

    requested: list[pd.Timestamp] = []
    for value in candidate_timestamps:
        try:
            requested.append(ensure_utc(value))
        except Exception:
            continue
    requested = sorted(set(requested))
    if not requested:
        return {"status": "NO_TIMESTAMPS", "valid": [], "ineligible": [], "repair_required": []}

    if not isinstance(features, pd.DataFrame):
        return {
            "status": "UNKNOWN",
            "valid": [],
            "ineligible": [],
            "repair_required": [stamp.isoformat() for stamp in requested],
            "reason": "features_not_dataframe",
        }
    required_inputs = [
        "Datetime", "Open", "High", "Low", "Close",
        "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD",
    ]
    missing_inputs = [column for column in required_inputs if column not in features.columns]
    if missing_inputs:
        return {
            "status": "UNKNOWN",
            "valid": [],
            "ineligible": [],
            "repair_required": [stamp.isoformat() for stamp in requested],
            "reason": f"missing_hmm_inputs:{missing_inputs}",
        }

    cfg = HMMRegimeConfig(n_states=int(n_states or 4))
    eligible_frame = engineer_hmm_inputs(features[required_inputs].copy(), config=cfg)
    eligible_frame["Datetime"] = parse_utc(eligible_frame["Datetime"])
    eligible = set(eligible_frame["Datetime"].dropna())
    ineligible = [stamp for stamp in requested if stamp not in eligible]
    eligible_requested = [stamp for stamp in requested if stamp in eligible]

    detected_probabilities = sorted(
        [column for column in features.columns if str(column).startswith("Prob_State_")],
        key=lambda value: int(str(value).rsplit("_", 1)[-1]),
    )
    resolved_states = int(n_states or len(detected_probabilities) or 4)
    validation = validate_hmm_rows(
        features,
        n_states=resolved_states,
        timestamps=eligible_requested,
        require_hmm_inputs=False,
    )
    invalid_keys = set(validation.get("missing_timestamps") or []) | set(
        (validation.get("invalid_by_timestamp") or {}).keys()
    )
    repair_required = [stamp for stamp in eligible_requested if stamp.isoformat() in invalid_keys]
    valid = [stamp for stamp in eligible_requested if stamp not in repair_required]
    return {
        "status": "PASS" if not repair_required else "REPAIR_REQUIRED",
        "valid": [stamp.isoformat() for stamp in valid],
        "ineligible": [stamp.isoformat() for stamp in ineligible],
        "repair_required": [stamp.isoformat() for stamp in repair_required],
        "validation": validation,
        "eligibility_contract": "shared.regime.hmm_regime.engineer_hmm_inputs",
    }


def authoritative_rebuild_live_hmm(asset: str, *, logger: Any = None) -> dict[str, Any]:
    """Compatibility wrapper for the canonical transactional repair authority.

    Direct HMM-only archive/rebuild used a different rollback protocol from the
    live continuation guard.  Keeping this name for callers is harmless, but all
    validation and mutation now use the same per-asset lock, cache invalidation,
    durable transaction journal, generation pairing, and atomic manifest switch.
    """
    from live_feature_continuation_guard import (
        DependencyRecord,
        verify_or_repair_asset,
    )

    key = str(asset).upper()
    started = time.perf_counter()
    result = verify_or_repair_asset(
        key,
        DependencyRecord(
            asset=key,
            requires_ffd=True,
            requires_hmm=True,
            selected_edge_count=0,
            ffd_edges=[],
            hmm_edges=[],
            dependency_source="hmm_integrity_compatibility_wrapper",
        ),
        auto_repair=True,
        logger=logger,
        use_cache=False,
    )
    if result.get("status") not in {"PASS", "REPAIRED_PASS"}:
        raise RuntimeError(
            f"{key}: transactional live-feature recovery failed: "
            + json.dumps(result, sort_keys=True, default=str)[:3000]
        )
    record = {
        **result,
        "version": HMM_INTEGRITY_VERSION,
        "asset": key,
        "status": result.get("status"),
        "created_at": utc_now(),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "recovery_authority": "live_feature_continuation_guard_transaction_v2",
    }
    if logger is not None:
        logger.warning(
            "P0_HMM_TRANSACTIONAL_RECOVERY asset=%s status=%s elapsed_ms=%.3f generation=%s",
            key,
            record["status"],
            record["elapsed_ms"],
            (record.get("generation") or {}).get("generation_id"),
        )
    return record


def _install_hotpath_integrity_wrappers() -> dict[str, Any]:
    """Report the canonical eligibility-aware continuation contract.

    Eligibility is implemented in ``shared.live_streaming.continuation``.  Keeping
    it canonical removes the former import-order-dependent runtime monkey patch.
    """
    return {
        "installed": False,
        "retired_runtime_monkey_patch": True,
        "canonical_module": "shared.live_streaming.continuation",
        "hmm_eligibility_aware_hotpath": True,
        "ffd_delta_recovery_from_verified_authority": True,
    }


def install_hmm_integrity_guard(*, logger: Any = None) -> dict[str, Any]:
    """Install finite-row and HMM-eligibility integrity around existing code."""
    global _INSTALLED, _ORIGINAL_CONTINUE
    with _INSTALL_LOCK:
        if _INSTALLED:
            return {"installed": True, "version": HMM_INTEGRITY_VERSION, "already_installed": True}
        import shared.regime.hmm_regime as hmm_math
        import shared.regime.verify_asset_hmm_regime_manifest as verifier

        original = verifier.continue_hmm_regime_rows
        _ORIGINAL_CONTINUE = original
        # Changing the writer contract invalidates every older checkpoint once.
        # The original verifier then rebuilds all live rows with this guard.
        verifier.HMM_CONTINUATION_WRITER_CONTRACT = (
            "HMM_CONTINUATION_WRITER_V3_FINITE_ROWS_ELIGIBILITY_AWARE"
        )

        def guarded_continue(*args: Any, **kwargs: Any):
            extension, next_state, metadata = original(*args, **kwargs)
            config = kwargs.get("config")
            n_states = int(
                getattr(config, "n_states", 0)
                or next_state.get("config", {}).get("n_states", 0)
                or 4
            )
            validation = (
                validate_hmm_rows(
                    extension,
                    n_states=n_states,
                    require_hmm_inputs=True,
                )
                if isinstance(extension, pd.DataFrame) and not extension.empty
                else {"status": "PASS", "checked_rows": 0}
            )
            if validation.get("status") != "PASS":
                from shared.regime.hmm_regime import HMMRegimeContractError
                raise HMMRegimeContractError(
                    "HMM continuation produced incomplete rows: "
                    + json.dumps(validation, sort_keys=True, default=str)[:3000]
                )
            metadata = dict(metadata)
            metadata["production_hmm_integrity"] = {
                "version": HMM_INTEGRITY_VERSION,
                "status": "PASS",
                "rows_checked": int(len(extension)) if isinstance(extension, pd.DataFrame) else 0,
            }
            return extension, next_state, metadata

        original_persist = verifier._atomic_write_hmm_csv_verified

        def verified_persist(df: pd.DataFrame, path: Path, *, required: list[str]):
            probability_columns = [column for column in required if str(column).startswith("Prob_State_")]
            validation = validate_hmm_rows(
                df,
                n_states=len(probability_columns) or 4,
                require_hmm_inputs=True,
            )
            if validation.get("status") != "PASS":
                raise verifier.HMMRegimeManifestVerificationError(
                    "Refusing to persist incomplete HMM continuation rows: "
                    + json.dumps(validation, sort_keys=True, default=str)[:3000]
                )
            proof = original_persist(df, path, required=required)
            reloaded = pd.read_csv(path, usecols=required)
            reloaded_validation = validate_hmm_rows(
                reloaded,
                n_states=len(probability_columns) or 4,
                require_hmm_inputs=True,
            )
            if reloaded_validation.get("status") != "PASS":
                raise verifier.HMMRegimeManifestVerificationError(
                    "Persisted HMM continuation failed finite-row reload: "
                    + json.dumps(reloaded_validation, sort_keys=True, default=str)[:3000]
                )
            return {
                **dict(proof),
                "finite_row_contract_passed": True,
                "hmm_integrity_version": HMM_INTEGRITY_VERSION,
            }

        guarded_continue.__name__ = getattr(original, "__name__", "continue_hmm_regime_rows")
        setattr(guarded_continue, "_production_hmm_integrity_guard", True)
        verifier.continue_hmm_regime_rows = guarded_continue
        hmm_math.continue_hmm_regime_rows = guarded_continue
        verifier._atomic_write_hmm_csv_verified = verified_persist
        hotpath_report = _install_hotpath_integrity_wrappers()
        _INSTALLED = True
        if logger is not None:
            logger.info("P0_HMM_INTEGRITY_GUARD_INSTALLED version=%s", HMM_INTEGRITY_VERSION)
        return {
            "installed": True,
            "version": HMM_INTEGRITY_VERSION,
            "continuation_math_changed": False,
            "validation": "finite_state_duration_entropy_probability_and_hmm_inputs",
            "writer_contract": verifier.HMM_CONTINUATION_WRITER_CONTRACT,
            "hotpath": hotpath_report,
        }


__all__ = [
    "HMM_INTEGRITY_VERSION",
    "authoritative_rebuild_live_hmm",
    "classify_candidate_hmm_coverage",
    "dependency_contract_version",
    "install_hmm_integrity_guard",
    "required_hmm_columns",
    "tombstone_key",
    "validate_hmm_rows",
]
