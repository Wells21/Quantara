from __future__ import annotations

"""Asset-generic live FFD/HMM continuation health, repair, and fail-closed guard.

This module is intentionally owned by ``portfolio_live`` rather than any edge.
It discovers feature dependencies from the active manifest, verifies that shared
asset-level FFD/HMM continuation reaches the latest live bar, archives stale or
corrupt continuation state, and deterministically rebuilds from immutable
research checkpoints through the existing shared verifiers.
"""

import json
import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import joblib
import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
OUTPUT_DIR = LIVE_DIR / "outputs" / "live_feature_continuation"
LIVE_BARS_DIR = LIVE_DIR / "live_bars"
ARCHIVE_DIR = OUTPUT_DIR / "archives"
TRANSACTION_DIR = OUTPUT_DIR / "feature_generation_transactions"
STAGING_DIR = OUTPUT_DIR / ".txn"
HEALTH_REPORT_JSON = OUTPUT_DIR / "live_feature_continuation_health_report.json"
HEALTH_REPORT_CSV = OUTPUT_DIR / "live_feature_continuation_health_report.csv"

for path in (LIVE_DIR, V2_ROOT, THIS_FILE.parents[2]):
    text = str(path)
    if text not in os.sys.path:
        os.sys.path.insert(0, text)

from shared.io import portable_pickle  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.ffd.verify_asset_ffd_manifest import (  # noqa: E402
    FFDManifestVerificationError,
    verify_asset_ffd_manifest,
)
from shared.regime.verify_asset_hmm_regime_manifest import (  # noqa: E402
    HMMRegimeManifestVerificationError,
    verify_asset_hmm_regime_manifest,
)
from shared.regime.hmm_regime import HMMRegimeConfig, engineer_hmm_inputs  # noqa: E402
from shared.live_streaming import state as hot_state  # noqa: E402

FFD_FEATURE_TOKENS = ("_FFD", "FFD_")
HMM_FEATURE_PREFIXES = ("Regime_", "Prob_State_")

_REPORT_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_HEALTH_CACHE: dict[tuple[str, str | None, bool, bool], dict[str, Any]] = {}


@dataclass
class DependencyRecord:
    asset: str
    requires_ffd: bool
    requires_hmm: bool
    selected_edge_count: int
    ffd_edges: list[str]
    hmm_edges: list[str]
    dependency_source: str


class LiveFeatureContinuationError(RuntimeError):
    """Raised when required live derived-feature coverage cannot be proven."""


WINDOWS_SAFE_PATH_LIMIT = 240


def _transaction_id(asset: str) -> str:
    """Return a short, collision-resistant ID that is safe in Windows paths."""
    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    identity = (
        f"{str(asset).upper()}|{os.getpid()}|{threading.get_ident()}|{time.time_ns()}"
    )
    token = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{created}-{token}"


def _staging_directory(transaction: Mapping[str, Any]) -> Path:
    asset = str(transaction.get("asset") or "asset").lower()
    token = hashlib.sha256(
        str(transaction.get("transaction_id") or "").encode("utf-8")
    ).hexdigest()[:12]
    return STAGING_DIR / f"{asset}-{token}"


def _staging_path_budget(staging_dir: Path, asset: str) -> dict[str, Any]:
    """Measure the longest conservative verifier temp path before building."""
    stem = str(asset).lower()
    names = [
        f".{stem}_ffd_features_live_continued.csv.ffffffff.ffffffffffffffff.tmp",
        f".{stem}_ffd_streaming_state.joblib.ffffffff.ffffffffffffffff.tmp",
        f".{stem}_hmm_regime_features_live_continued.csv.ffffffff.ffffffffffffffff.verified.tmp",
        f".{stem}_hmm_streaming_state.joblib.ffffffff.ffffffffffffffff.tmp",
    ]
    candidates = [str(Path(staging_dir) / name) for name in names]
    longest = max(candidates, key=len)
    return {
        "longest_path": longest,
        "longest_length": len(longest),
        "safe_limit": WINDOWS_SAFE_PATH_LIMIT,
        "passed": len(longest) < WINDOWS_SAFE_PATH_LIMIT,
    }


def _prepare_staging_directory(transaction: dict[str, Any]) -> Path:
    staging_dir = _staging_directory(transaction)
    budget = _staging_path_budget(staging_dir, str(transaction.get("asset") or ""))
    if os.name == "nt" and not budget["passed"]:
        raise LiveFeatureContinuationError(
            "windows_staging_path_budget_exceeded:"
            + json.dumps(budget, sort_keys=True)
        )
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=False)
    _write_transaction_state(
        transaction,
        "BUILDING_STAGED_GENERATION",
        staging_dir=str(staging_dir),
        staging_path_budget=budget,
    )
    return staging_dir


def _cleanup_staging_directory(transaction: Mapping[str, Any]) -> None:
    value = transaction.get("staging_dir")
    if value:
        shutil.rmtree(Path(str(value)), ignore_errors=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _asset_lock(asset: str) -> threading.RLock:
    return hot_state.asset_lock(str(asset).upper())


def _atomic_copy_replace(source: Path, destination: Path) -> None:
    """Copy one artifact durably, then expose it with a single rename."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _parse_utc(values: Any) -> pd.Series:
    """Parse heterogeneous timestamps and recover any non-empty scalar misses.

    ROBUST_MIXED_UTC_SCALAR_RECOVERY_V1

    Pandas versions differ in how a vector containing both whole-second and
    fractional-second ISO timestamps is inferred. Coverage checks must never
    silently turn one valid timestamp into NaT merely because its textual
    precision differs from neighbouring rows.
    """
    raw = values.copy() if isinstance(values, pd.Series) else pd.Series(values)
    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = to_datetime_iso(raw, utc=True, errors="coerce")

    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        recovered = []
        for value in raw.loc[missed]:
            try:
                stamp = pd.Timestamp(value)
                recovered.append(
                    stamp.tz_localize("UTC")
                    if stamp.tzinfo is None
                    else stamp.tz_convert("UTC")
                )
            except Exception:
                recovered.append(pd.NaT)
        parsed.loc[missed] = recovered
    return parsed


def _latest_csv_datetime(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path, usecols=["Datetime"])
    except Exception:
        return None
    if frame.empty:
        return None
    values = _parse_utc(frame["Datetime"]).dropna()
    return None if values.empty else pd.Timestamp(values.max())


def _state_latest(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    try:
        payload = portable_pickle.load(path)
        value = payload.get("last_observation_timestamp") if isinstance(payload, dict) else None
        if value is None:
            return None
        stamp = pd.Timestamp(value)
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    except Exception:
        return None


def _iso(value: pd.Timestamp | None) -> str | None:
    return None if value is None else pd.Timestamp(value).isoformat()




def _ffd_internal_live_coverage(
    *,
    ffd_static_path: Path | None,
    ffd_runtime_path: Path | None,
    live_bars_path: Path | None,
) -> dict[str, Any]:
    """Prove that no required live FFD timestamp is missing behind the watermark.

    This is a startup/recovery oracle, not a normal scoring-path operation.
    Latest-watermark equality alone is insufficient because an interior hole can
    poison rolling FFD features while still making the tail look healthy.
    """
    result: dict[str, Any] = {
        "schema_version": "INTERNAL_FFD_LIVE_COVERAGE_GUARD_V1",
        "checked": False,
        "passed": False,
        "expected_count": 0,
        "produced_count": 0,
        "missing_count": 0,
        "missing_sample": [],
        "nonfinite_count": 0,
        "nonfinite_sample": [],
        "error": None,
    }
    if (
        ffd_static_path is None
        or ffd_runtime_path is None
        or live_bars_path is None
    ):
        result["error"] = "required_path_missing"
        return result
    if (
        not ffd_static_path.exists()
        or not ffd_runtime_path.exists()
        or not live_bars_path.exists()
    ):
        result["error"] = "required_file_missing"
        return result
    try:
        static_latest = _latest_csv_datetime(ffd_static_path)
        if static_latest is None:
            result["error"] = "ffd_static_latest_missing"
            return result
        live = pd.read_csv(live_bars_path, usecols=["Datetime"])
        live["Datetime"] = _parse_utc(live["Datetime"])
        expected = set(
            live.loc[
                live["Datetime"].notna()
                & (live["Datetime"] > static_latest),
                "Datetime",
            ]
        )
        columns = [
            "Datetime",
            "Open_FFD",
            "High_FFD",
            "Low_FFD",
            "Close_FFD",
        ]
        produced = pd.read_csv(ffd_runtime_path, usecols=columns)
        produced["Datetime"] = _parse_utc(produced["Datetime"])
        runtime_live = produced.loc[
            produced["Datetime"].notna()
            & (produced["Datetime"] > static_latest)
        ].copy()
        produced_times = set(runtime_live["Datetime"])
        missing = sorted(expected.difference(produced_times))
        expected_rows = runtime_live.loc[
            runtime_live["Datetime"].isin(expected)
        ].copy()
        numeric = expected_rows[columns[1:]].apply(
            pd.to_numeric,
            errors="coerce",
        )
        invalid_mask = (
            ~np.isfinite(numeric.to_numpy(dtype=float))
        ).any(axis=1) if len(expected_rows) else np.asarray([], dtype=bool)
        nonfinite = sorted(
            {
                pd.Timestamp(value)
                for value, invalid in zip(
                    expected_rows["Datetime"].tolist(),
                    invalid_mask,
                )
                if bool(invalid)
            }
        )
        result.update(
            {
                "checked": True,
                "passed": not missing and not nonfinite,
                "expected_count": int(len(expected)),
                "produced_count": int(len(produced_times.intersection(expected))),
                "missing_count": int(len(missing)),
                "missing_sample": [
                    pd.Timestamp(value).isoformat()
                    for value in missing[:10]
                ],
                "nonfinite_count": int(len(nonfinite)),
                "nonfinite_sample": [
                    pd.Timestamp(value).isoformat()
                    for value in nonfinite[:10]
                ],
            }
        )
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
        return result


def _hmm_internal_eligible_coverage(
    *,
    ffd_runtime_path: Path | None,
    hmm_static_path: Path | None,
    hmm_runtime_path: Path | None,
) -> dict[str, Any]:
    """
    Verify internal HMM coverage using the canonical shared HMM preprocessor.

    CANONICAL_HMM_ELIGIBILITY_GUARD_V2

    Eligibility must not be independently reimplemented inside the health guard.
    The writer, replay validator, forensic validator, and health guard must all use
    ``shared.regime.hmm_regime.engineer_hmm_inputs``.
    """
    result: dict[str, Any] = {
        "schema_version": "INTERNAL_HMM_ELIGIBLE_COVERAGE_GUARD_V2",
        "eligibility_source": "shared.regime.hmm_regime.engineer_hmm_inputs",
        "checked": False,
        "passed": False,
        "eligible_count": 0,
        "produced_count": 0,
        "missing_count": 0,
        "missing_sample": [],
        "unexpected_count": 0,
        "unexpected_sample": [],
        "intentionally_excluded_count": 0,
        "intentionally_excluded_sample": [],
        "error": None,
    }

    if (
        ffd_runtime_path is None
        or hmm_static_path is None
        or hmm_runtime_path is None
    ):
        result["error"] = "required_path_missing"
        return result

    if (
        not ffd_runtime_path.exists()
        or not hmm_static_path.exists()
        or not hmm_runtime_path.exists()
    ):
        result["error"] = "required_file_missing"
        return result

    try:
        static_latest = _latest_csv_datetime(hmm_static_path)
        if static_latest is None:
            result["error"] = "hmm_static_latest_missing"
            return result

        source = pd.read_csv(ffd_runtime_path)

        manifest_path = hmm_static_path.parent / "hmm_regime_manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded

        config = HMMRegimeConfig(
            n_states=int(manifest.get("n_states", 4)),
            train_window=int(manifest.get("train_window", 5000)),
            refit_frequency=int(manifest.get("refit_frequency", 1000)),
            inference_lookback=int(manifest.get("inference_lookback", 1000)),
            min_covar=float(manifest.get("min_covar", 0.01)),
            random_state=int(manifest.get("random_state", 42)),
            max_iter=int(manifest.get("max_iter", 200)),
            noise_epsilon=float(manifest.get("noise_epsilon", 1e-8)),
            bar_vol_clip_low=float(manifest.get("bar_vol_clip_low", 0.0)),
            bar_vol_clip_high=float(manifest.get("bar_vol_clip_high", 300.0)),
            abs_bar_return_clip_low=float(
                manifest.get("abs_bar_return_clip_low", 0.0)
            ),
            abs_bar_return_clip_high=float(
                manifest.get("abs_bar_return_clip_high", 200.0)
            ),
            body_to_range_clip_low=float(
                manifest.get("body_to_range_clip_low", 0.0)
            ),
            body_to_range_clip_high=float(
                manifest.get("body_to_range_clip_high", 1.0)
            ),
            close_location_clip_low=float(
                manifest.get("close_location_clip_low", 0.0)
            ),
            close_location_clip_high=float(
                manifest.get("close_location_clip_high", 1.0)
            ),
        )

        processed = engineer_hmm_inputs(source, config=config)
        processed["Datetime"] = _parse_utc(processed["Datetime"])
        processed = (
            processed.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .drop_duplicates("Datetime", keep="last")
            .reset_index(drop=True)
        )
        eligible_times = set(
            processed.loc[
                processed["Datetime"] > static_latest,
                "Datetime",
            ]
        )

        raw_times: set[pd.Timestamp] = set()
        if "Datetime" in source.columns:
            raw_datetime = _parse_utc(source["Datetime"])
            raw_times = set(
                raw_datetime.loc[
                    raw_datetime.notna() & (raw_datetime > static_latest)
                ]
            )

        produced_frame = pd.read_csv(
            hmm_runtime_path,
            usecols=["Datetime"],
        )
        produced_frame["Datetime"] = _parse_utc(
            produced_frame["Datetime"]
        )
        produced_times = set(
            produced_frame.loc[
                produced_frame["Datetime"] > static_latest,
                "Datetime",
            ].dropna()
        )

        missing = sorted(eligible_times.difference(produced_times))
        unexpected = sorted(produced_times.difference(eligible_times))
        excluded = sorted(raw_times.difference(eligible_times))

        result.update(
            {
                "checked": True,
                "passed": not missing and not unexpected,
                "eligible_count": int(len(eligible_times)),
                "produced_count": int(len(produced_times)),
                "missing_count": int(len(missing)),
                "missing_sample": [
                    pd.Timestamp(value).isoformat()
                    for value in missing[:10]
                ],
                "unexpected_count": int(len(unexpected)),
                "unexpected_sample": [
                    pd.Timestamp(value).isoformat()
                    for value in unexpected[:10]
                ],
                "intentionally_excluded_count": int(len(excluded)),
                "intentionally_excluded_sample": [
                    pd.Timestamp(value).isoformat()
                    for value in excluded[:10]
                ],
            }
        )
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
        return result


def _continuation_paths(
    asset: str,
    root: Path | None = None,
) -> dict[str, Path]:
    stem = str(asset).lower()
    continuation_root = Path(root or OUTPUT_DIR)
    return {
        "ffd_features": continuation_root / f"{stem}_ffd_features_live_continued.csv",
        "ffd_state": continuation_root / f"{stem}_ffd_streaming_state.joblib",
        "hmm_features": continuation_root / f"{stem}_hmm_regime_features_live_continued.csv",
        "hmm_state": continuation_root / f"{stem}_hmm_streaming_state.joblib",
        "ffd_lock": continuation_root / f"{stem}_ffd_features_live_continued.csv.lock",
        "hmm_lock": continuation_root / f"{stem}_hmm_regime_features_live_continued.csv.lock",
        "generation_manifest": continuation_root / f"{stem}_feature_generation.json",
    }


def _live_bars_path(asset: str) -> Path:
    return LIVE_BARS_DIR / f"{str(asset).upper()}_live_standard_quote_tick_bars.csv"


def _selected_features_for_edge(edge: Mapping[str, Any]) -> list[str]:
    edge_id = str(edge.get("edge_id") or "").strip()
    asset = str(edge.get("asset") or (edge_id.split("/", 1)[0] if "/" in edge_id else "")).upper()
    edge_name = edge_id.split("/", 1)[1] if "/" in edge_id else ""
    candidates: list[Path] = []
    for key in ("selected_features_path", "selected_features_json"):
        value = edge.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            candidates.append(path if path.is_absolute() else V2_ROOT / path)
    if asset and edge_name:
        candidates.append(V2_ROOT / "assets" / asset / "edges" / edge_name / "artifacts" / "selected_features.json")
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            values = payload.get("selected_features")
            if isinstance(values, list) and values:
                return [str(value) for value in values]
        except Exception:
            continue
    embedded = edge.get("selected_features")
    return [str(value) for value in embedded] if isinstance(embedded, list) else []


def dependency_map_from_manifest(manifest: Mapping[str, Any], assets: Iterable[str] | None = None) -> dict[str, DependencyRecord]:
    requested = None if assets is None else {str(asset).upper() for asset in assets}
    grouped: dict[str, dict[str, Any]] = {}
    for edge in manifest.get("selected_edges", []) or []:
        edge_id = str(edge.get("edge_id") or "").strip()
        asset = str(edge.get("asset") or (edge_id.split("/", 1)[0] if "/" in edge_id else "")).upper()
        if not asset or (requested is not None and asset not in requested):
            continue
        rec = grouped.setdefault(asset, {"edge_count": 0, "ffd": [], "hmm": [], "feature_files_found": 0})
        rec["edge_count"] += 1
        features = _selected_features_for_edge(edge)
        if features:
            rec["feature_files_found"] += 1
        needs_ffd = any(any(token in feature for token in FFD_FEATURE_TOKENS) for feature in features)
        needs_hmm = any(feature.startswith(HMM_FEATURE_PREFIXES) for feature in features)
        if needs_ffd:
            rec["ffd"].append(edge_id)
        if needs_hmm:
            rec["hmm"].append(edge_id)

    if requested is not None:
        for asset in requested:
            grouped.setdefault(asset, {"edge_count": 0, "ffd": [], "hmm": [], "feature_files_found": 0})

    result: dict[str, DependencyRecord] = {}
    for asset, rec in grouped.items():
        # If selected-feature contracts cannot be read, fail safely by requiring both.
        feature_contracts_missing = rec["edge_count"] > 0 and rec["feature_files_found"] < rec["edge_count"]
        result[asset] = DependencyRecord(
            asset=asset,
            requires_ffd=bool(rec["ffd"] or rec["hmm"] or feature_contracts_missing),
            requires_hmm=bool(rec["hmm"] or feature_contracts_missing),
            selected_edge_count=int(rec["edge_count"]),
            ffd_edges=sorted(set(rec["ffd"])),
            hmm_edges=sorted(set(rec["hmm"])),
            dependency_source="selected_features_contracts" if not feature_contracts_missing else "conservative_missing_selected_features_contract",
        )
    return result


def _invalidate_asset_generation(asset: str) -> None:
    key = str(asset).upper()
    hot_state.invalidate(key, ffd=True, hmm=True)
    with _CACHE_LOCK:
        for cache_key in [value for value in _HEALTH_CACHE if value[0] == key]:
            _HEALTH_CACHE.pop(cache_key, None)


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generation_pairing(
    asset: str,
    required_components: set[str],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    paths = _continuation_paths(asset, root)
    pairing: dict[str, Any] = {}
    for kind in ("ffd", "hmm"):
        features_name = f"{kind}_features"
        state_name = f"{kind}_state"
        if features_name not in required_components and state_name not in required_components:
            continue
        features_path = paths[features_name]
        state_path = paths[state_name]
        if not features_path.exists() or not state_path.exists():
            raise LiveFeatureContinuationError(
                f"{str(asset).upper()}: generation pair missing "
                f"{features_name}={features_path.exists()} {state_name}={state_path.exists()}"
            )
        feature_latest = _latest_csv_datetime(features_path)
        checkpoint_latest = _state_latest(state_path)
        if feature_latest is None or checkpoint_latest is None or feature_latest != checkpoint_latest:
            raise LiveFeatureContinuationError(
                f"{str(asset).upper()}: {kind} output/checkpoint generation mismatch "
                f"features_latest={_iso(feature_latest)} checkpoint_latest={_iso(checkpoint_latest)}"
            )
        pairing[kind] = {
            "features_latest": _iso(feature_latest),
            "checkpoint_latest": _iso(checkpoint_latest),
            "matched": True,
        }
    return pairing


def _publish_generation_manifest(
    asset: str,
    result: Mapping[str, Any],
    *,
    transaction: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    paths = _continuation_paths(asset)
    dependency = dict(result.get("dependency") or {})
    required_components: set[str] = set()
    if bool(dependency.get("requires_ffd")):
        required_components.update({"ffd_features", "ffd_state"})
    if bool(dependency.get("requires_hmm")):
        required_components.update({"hmm_features", "hmm_state"})
    pairing = _generation_pairing(asset, required_components)
    identities = {
        name: {
            "path": str(path),
            "sha256": _sha256(path),
            "size": path.stat().st_size if path.exists() else None,
            "mtime_ns": path.stat().st_mtime_ns if path.exists() else None,
        }
        for name, path in paths.items()
        if name in required_components
    }
    identity = {
        "schema_version": "v2_atomic_feature_generation_v2",
        "asset": str(asset).upper(),
        "status": str(result.get("status")),
        "live_bar_latest": result.get("live_bar_latest"),
        "required_components": sorted(required_components),
        "identities": identities,
        "pairing": pairing,
        "transaction_id": (transaction or {}).get("transaction_id"),
        "previous_generation_id": (transaction or {}).get("previous_generation_id"),
    }
    payload = {
        **identity,
        "published_at": _utc_now(),
        "generation_id": hashlib.sha256(
            json.dumps(
                identity,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
    }
    _atomic_write_text(
        paths["generation_manifest"],
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
    )
    return payload


def _transaction_journal_path(asset: str) -> Path:
    return TRANSACTION_DIR / f"{str(asset).upper()}.json"


def _read_previous_generation_id(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("generation_id") or "") or None
    except Exception:
        return None


def _begin_generation_transaction(
    asset: str,
    *,
    reason: str,
    components: set[str] | None = None,
) -> dict[str, Any]:
    """Preserve the active generation before any rebuild mutation.

    The durable journal and backups are published before staged work begins.
    Active artifacts remain intact until the validated pair is switched; if the
    process stops during or after that switch, startup recovery restores every
    prior component before continuation verification resumes.
    """
    key = str(asset).upper()
    journal_path = _transaction_journal_path(key)
    if journal_path.exists():
        recover_interrupted_generation_transactions(assets=[key])
        if journal_path.exists():
            raise LiveFeatureContinuationError(
                f"{key}: unresolved feature-generation transaction already exists"
            )
    paths = _continuation_paths(key)
    targeted = sorted(
        components
        if components is not None
        else {name for name in paths if not name.endswith("_lock")}
    )
    if any(name.endswith("_lock") for name in targeted):
        raise LiveFeatureContinuationError(
            f"{key}: active lock files cannot be transactional artifacts"
        )
    transaction_id = _transaction_id(key)
    archive_dir = ARCHIVE_DIR / transaction_id / key
    archive_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for name in targeted:
        source_path = paths.get(name)
        if source_path is None:
            raise LiveFeatureContinuationError(
                f"{key}: unknown transaction component {name}"
            )
        backup = archive_dir / source_path.name
        existed = source_path.exists()
        previous_sha256 = _sha256(source_path) if existed else None
        if existed:
            _atomic_copy_replace(source_path, backup)
        records.append(
            {
                "name": name,
                "source": str(source_path),
                "backup": str(backup),
                "existed": bool(existed),
                "previous_sha256": previous_sha256,
            }
        )
    transaction: dict[str, Any] = {
        "schema_version": "v2_feature_generation_transaction_v3",
        "transaction_id": transaction_id,
        "asset": key,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "PREPARED",
        "reason": str(reason),
        "targeted_components": targeted,
        "records": records,
        "archive_dir": str(archive_dir),
        "journal_path": str(journal_path),
        "previous_generation_id": _read_previous_generation_id(
            paths["generation_manifest"]
        ),
    }
    _atomic_write_text(
        journal_path,
        json.dumps(transaction, indent=2, sort_keys=True) + "\n",
    )
    # Keep the last verified generation live while the replacement is built in
    # isolation. The per-asset lock prevents scoring from consuming it during
    # recovery, and the durable backup remains available for a partial switch.
    transaction.update({"status": "STAGING", "updated_at": _utc_now()})
    _atomic_write_text(
        journal_path,
        json.dumps(transaction, indent=2, sort_keys=True) + "\n",
    )
    return transaction


def _finalize_transaction_record(transaction: Mapping[str, Any]) -> None:
    archive_dir = Path(str(transaction["archive_dir"]))
    _atomic_write_text(
        archive_dir / "transaction_manifest.json",
        json.dumps(
            dict(transaction),
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
    )


def _rollback_generation_transaction(
    transaction: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    key = str(transaction.get("asset") or "").upper()
    restored: list[str] = []
    for record in transaction.get("records") or []:
        name = str(record.get("name") or "")
        if name.endswith("_lock"):
            raise LiveFeatureContinuationError(
                f"{key}: transaction illegally contains active lock {name}"
            )
        source_path = Path(str(record["source"]))
        backup = Path(str(record["backup"]))
        if source_path.exists():
            source_path.unlink()
        if bool(record.get("existed")):
            if not backup.exists():
                raise LiveFeatureContinuationError(
                    f"{key}: rollback backup missing for {name}: {backup}"
                )
            _atomic_copy_replace(backup, source_path)
            expected = record.get("previous_sha256")
            if expected and _sha256(source_path) != expected:
                raise LiveFeatureContinuationError(
                    f"{key}: rollback hash mismatch for {name}"
                )
            restored.append(name)
    completed = {
        **dict(transaction),
        "status": "ROLLED_BACK",
        "updated_at": _utc_now(),
        "rollback_reason": str(reason),
        "restored_components": sorted(restored),
    }
    _finalize_transaction_record(completed)
    _cleanup_staging_directory(completed)
    journal_path = Path(
        str(
            transaction.get("journal_path")
            or _transaction_journal_path(key)
        )
    )
    journal_path.unlink(missing_ok=True)
    return completed


def _commit_generation_transaction(
    transaction: Mapping[str, Any],
    generation: Mapping[str, Any],
) -> dict[str, Any]:
    completed = {
        **dict(transaction),
        "status": "COMMITTED",
        "updated_at": _utc_now(),
        "generation_id": generation.get("generation_id"),
        "generation_manifest": dict(generation),
    }
    journal_path = Path(str(transaction.get("journal_path") or ""))
    _atomic_write_text(
        journal_path,
        json.dumps(completed, indent=2, sort_keys=True, default=str) + "\n",
    )
    _finalize_transaction_record(completed)
    _cleanup_staging_directory(completed)
    journal_path.unlink(missing_ok=True)
    return completed


def _write_transaction_state(
    transaction: dict[str, Any],
    status: str,
    **fields: Any,
) -> None:
    transaction.update(
        {
            "status": str(status),
            "updated_at": _utc_now(),
            **fields,
        }
    )
    _atomic_write_text(
        Path(str(transaction["journal_path"])),
        json.dumps(transaction, indent=2, sort_keys=True, default=str) + "\n",
    )


def _activate_staged_generation(
    asset: str,
    transaction: dict[str, Any],
    staging_dir: Path,
) -> list[str]:
    """Expose a validated staged pair while the shared asset lock is held."""
    active_paths = _continuation_paths(asset)
    staged_paths = _continuation_paths(asset, staging_dir)
    components = sorted(
        set(transaction.get("targeted_components") or []).intersection(
            {"ffd_features", "ffd_state", "hmm_features", "hmm_state"}
        )
    )
    for name in components:
        if not staged_paths[name].exists():
            raise LiveFeatureContinuationError(
                f"{str(asset).upper()}: staged generation missing {name}"
            )
    _write_transaction_state(
        transaction,
        "SWITCHING",
        staging_dir=str(staging_dir),
        activated_components=components,
    )
    activated: list[str] = []
    for name in components:
        _atomic_copy_replace(staged_paths[name], active_paths[name])
        activated.append(name)
    _write_transaction_state(
        transaction,
        "SWITCHED",
        activated_components=activated,
    )
    return activated


def recover_interrupted_generation_transactions(
    *,
    assets: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Restore PREPARED/REBUILDING generations left by a stopped process."""
    requested = (
        None
        if assets is None
        else {str(value).upper() for value in assets}
    )
    recovered: list[dict[str, Any]] = []
    if not TRANSACTION_DIR.exists():
        return recovered
    for journal_path in sorted(TRANSACTION_DIR.glob("*.json")):
        try:
            transaction = json.loads(
                journal_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise LiveFeatureContinuationError(
                f"Unreadable feature-generation transaction journal "
                f"{journal_path}: {exc}"
            ) from exc
        key = str(
            transaction.get("asset") or journal_path.stem
        ).upper()
        if requested is not None and key not in requested:
            continue
        with _asset_lock(key):
            if str(transaction.get("status")) == "COMMITTED":
                _finalize_transaction_record(transaction)
                journal_path.unlink(missing_ok=True)
                continue
            restored = _rollback_generation_transaction(
                transaction,
                reason="startup_recovery_of_interrupted_generation_switch",
            )
            _invalidate_asset_generation(key)
            recovered.append(restored)
    return recovered


def _component_record(
    *,
    required: bool,
    static_path: Path | None,
    runtime_path: Path | None,
    state_path: Path | None,
    expected_latest: pd.Timestamp | None,
    warning: str | None,
    parity_passed: bool | None,
) -> dict[str, Any]:
    static_latest = _latest_csv_datetime(static_path) if static_path else None
    runtime_latest = _latest_csv_datetime(runtime_path) if runtime_path else None
    state_latest = _state_latest(state_path) if state_path else None
    coverage_target = static_latest
    if expected_latest is not None and (coverage_target is None or expected_latest > coverage_target):
        coverage_target = expected_latest
    continuation_required = bool(
        required
        and static_latest is not None
        and coverage_target is not None
        and coverage_target > static_latest
    )
    failures: list[str] = []
    if required:
        if static_latest is None:
            failures.append("static_latest_missing")
        if warning:
            failures.append(f"warning:{warning}")
        if parity_passed is False:
            failures.append("streaming_parity_failed")
        if coverage_target is not None and runtime_latest != coverage_target:
            failures.append(f"runtime_latest_mismatch:{_iso(runtime_latest)}!={_iso(coverage_target)}")
        if continuation_required and state_latest != runtime_latest:
            failures.append(f"state_latest_mismatch:{_iso(state_latest)}!={_iso(runtime_latest)}")
    return {
        "required": bool(required),
        "continuation_required": continuation_required,
        "static_path": None if static_path is None else static_path.relative_to(V2_ROOT).as_posix(),
        "runtime_path": None if runtime_path is None else runtime_path.relative_to(V2_ROOT).as_posix(),
        "state_path": None if state_path is None else state_path.relative_to(V2_ROOT).as_posix(),
        "static_latest": _iso(static_latest),
        "runtime_latest": _iso(runtime_latest),
        "state_latest": _iso(state_latest),
        "expected_latest": _iso(expected_latest),
        "coverage_target": _iso(coverage_target),
        "warning": warning,
        "streaming_parity_passed": parity_passed,
        "coverage_passed": not failures,
        "failures": failures,
    }


def _verify_asset_once(
    asset: str,
    dependency: DependencyRecord,
    *,
    continuation_dir: Path | None = None,
) -> dict[str, Any]:
    asset = str(asset).upper()
    live_latest = _latest_csv_datetime(_live_bars_path(asset))
    paths = _continuation_paths(asset, continuation_dir)
    ffd_result: dict[str, Any] | None = None
    hmm_result: dict[str, Any] | None = None
    errors: list[str] = []

    if dependency.requires_ffd or dependency.requires_hmm:
        try:
            ffd_result = verify_asset_ffd_manifest(
                asset,
                continuation_dir=continuation_dir,
            )
        except Exception as exc:
            errors.append(f"FFD:{type(exc).__name__}:{exc}")

    ffd_static = V2_ROOT / str(ffd_result["static_features"]) if ffd_result else None
    ffd_runtime = V2_ROOT / str(ffd_result["features"]) if ffd_result else None
    ffd = _component_record(
        required=dependency.requires_ffd or dependency.requires_hmm,
        static_path=ffd_static,
        runtime_path=ffd_runtime,
        state_path=paths["ffd_state"],
        expected_latest=live_latest,
        warning=None if ffd_result is None else ffd_result.get("live_continuation_warning"),
        parity_passed=None if ffd_result is None else bool(ffd_result.get("streaming_parity_passed")),
    )
    internal_ffd_coverage = (
        _ffd_internal_live_coverage(
            ffd_static_path=ffd_static,
            ffd_runtime_path=ffd_runtime,
            live_bars_path=_live_bars_path(asset),
        )
        if (dependency.requires_ffd or dependency.requires_hmm) and ffd_result is not None
        else {
            "schema_version": "INTERNAL_FFD_LIVE_COVERAGE_GUARD_V1",
            "checked": False,
            "passed": not (dependency.requires_ffd or dependency.requires_hmm),
            "missing_count": 0,
            "missing_sample": [],
            "nonfinite_count": 0,
            "nonfinite_sample": [],
            "error": None if not (dependency.requires_ffd or dependency.requires_hmm)
            else "ffd_verification_unavailable",
        }
    )
    ffd["internal_live_timestamp_coverage"] = internal_ffd_coverage
    if (
        (dependency.requires_ffd or dependency.requires_hmm)
        and not bool(internal_ffd_coverage.get("passed"))
    ):
        if internal_ffd_coverage.get("error"):
            ffd["failures"].append(
                "internal_ffd_coverage_error:"
                + str(internal_ffd_coverage.get("error"))
            )
        if int(internal_ffd_coverage.get("missing_count") or 0) > 0:
            ffd["failures"].append(
                "internal_missing_ffd_rows:"
                + ",".join(internal_ffd_coverage.get("missing_sample") or [])
            )
        if int(internal_ffd_coverage.get("nonfinite_count") or 0) > 0:
            ffd["failures"].append(
                "internal_nonfinite_ffd_rows:"
                + ",".join(internal_ffd_coverage.get("nonfinite_sample") or [])
            )
        ffd["coverage_passed"] = False

    if dependency.requires_hmm and ffd.get("coverage_passed"):
        try:
            hmm_result = verify_asset_hmm_regime_manifest(
                asset,
                continuation_dir=continuation_dir,
            )
        except Exception as exc:
            errors.append(f"HMM:{type(exc).__name__}:{exc}")

    # HMM_ELIGIBLE_TIMESTAMP_COVERAGE_V1
    # Use the latest timestamp that survives the immutable research HMM
    # preprocessing contract, not the raw latest FFD timestamp.
    hmm_expected = None
    if dependency.requires_hmm and hmm_result:
        expected_value = (
            (hmm_result.get("continuation_proof") or {})
            .get("expected_hmm_latest_timestamp")
        )
        if expected_value is not None:
            hmm_expected = pd.Timestamp(expected_value)
            hmm_expected = (
                hmm_expected.tz_localize("UTC")
                if hmm_expected.tzinfo is None
                else hmm_expected.tz_convert("UTC")
            )
    if hmm_expected is None:
        hmm_expected = (
            _latest_csv_datetime(ffd_runtime)
            if dependency.requires_hmm and ffd_runtime
            else live_latest
        )
    hmm_static = V2_ROOT / str(hmm_result["static_features"]) if hmm_result else None
    hmm_runtime = V2_ROOT / str(hmm_result["features"]) if hmm_result else None
    hmm = _component_record(
        required=dependency.requires_hmm,
        static_path=hmm_static,
        runtime_path=hmm_runtime,
        state_path=paths["hmm_state"],
        expected_latest=hmm_expected,
        warning=None if hmm_result is None else hmm_result.get("live_continuation_warning"),
        parity_passed=None if hmm_result is None else bool(hmm_result.get("streaming_parity_passed")),
    )

    # INTERNAL_HMM_ELIGIBLE_COVERAGE_GUARD_V1
    internal_coverage = (
        _hmm_internal_eligible_coverage(
            ffd_runtime_path=ffd_runtime,
            hmm_static_path=hmm_static,
            hmm_runtime_path=hmm_runtime,
        )
        if dependency.requires_hmm and hmm_result is not None
        else {
            "schema_version": "INTERNAL_HMM_ELIGIBLE_COVERAGE_GUARD_V1",
            "checked": False,
            "passed": not dependency.requires_hmm,
            "missing_count": 0,
            "missing_sample": [],
            "unexpected_count": 0,
            "unexpected_sample": [],
            "error": None if not dependency.requires_hmm else "hmm_verification_unavailable",
        }
    )
    hmm["internal_eligible_timestamp_coverage"] = internal_coverage
    if dependency.requires_hmm and not bool(internal_coverage.get("passed")):
        if internal_coverage.get("error"):
            hmm["failures"].append(
                "internal_eligible_coverage_error:"
                + str(internal_coverage.get("error"))
            )
        if int(internal_coverage.get("missing_count") or 0) > 0:
            hmm["failures"].append(
                "internal_missing_eligible_hmm_rows:"
                + ",".join(internal_coverage.get("missing_sample") or [])
            )
        if int(internal_coverage.get("unexpected_count") or 0) > 0:
            hmm["failures"].append(
                "internal_unexpected_hmm_rows:"
                + ",".join(internal_coverage.get("unexpected_sample") or [])
            )
        hmm["coverage_passed"] = False

    passed = not errors and ffd["coverage_passed"] and hmm["coverage_passed"]
    return {
        "asset": asset,
        "checked_at": _utc_now(),
        "status": "PASS" if passed else "FAIL",
        "dependency": asdict(dependency),
        "live_bar_path": _live_bars_path(asset).relative_to(V2_ROOT).as_posix(),
        "live_bar_latest": _iso(live_latest),
        "ffd": ffd,
        "hmm": hmm,
        "errors": errors,
        "repair_attempted": False,
        "repair_archive": None,
    }


def _cache_key(asset: str, dependency: DependencyRecord) -> tuple[str, str | None, bool, bool]:
    return (
        str(asset).upper(),
        _iso(_latest_csv_datetime(_live_bars_path(asset))),
        bool(dependency.requires_ffd),
        bool(dependency.requires_hmm),
    )


def verify_or_repair_asset(
    asset: str,
    dependency: DependencyRecord,
    *,
    auto_repair: bool,
    logger: logging.Logger | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    asset = str(asset).upper()
    lock = _asset_lock(asset)
    with lock:
        interrupted = recover_interrupted_generation_transactions(assets=[asset])
        if interrupted and logger is not None:
            logger.error(
                "LIVE_FEATURE_GENERATION_INTERRUPTED_TRANSACTION_RECOVERED "
                "asset=%s transactions=%s action=previous_generation_restored",
                asset,
                [record.get("transaction_id") for record in interrupted],
            )
        key = _cache_key(asset, dependency)
        if use_cache:
            with _CACHE_LOCK:
                cached = _HEALTH_CACHE.get(key)
                if cached and cached.get("status") in {"PASS", "REPAIRED_PASS", "NOT_REQUIRED"}:
                    return dict(cached)

        if not dependency.requires_ffd and not dependency.requires_hmm:
            result = {
                "asset": asset,
                "checked_at": _utc_now(),
                "status": "NOT_REQUIRED",
                "dependency": asdict(dependency),
                "live_bar_path": _live_bars_path(asset).relative_to(V2_ROOT).as_posix(),
                "live_bar_latest": _iso(_latest_csv_datetime(_live_bars_path(asset))),
                "ffd": {"required": False, "coverage_passed": True, "failures": []},
                "hmm": {"required": False, "coverage_passed": True, "failures": []},
                "errors": [],
                "repair_attempted": False,
                "repair_archive": None,
            }
        else:
            result = _verify_asset_once(asset, dependency)
            if result["status"] != "PASS" and auto_repair:
                reason = ";".join(
                    result.get("errors")
                    or result["ffd"].get("failures", [])
                    or result["hmm"].get("failures", [])
                )
                ffd_failed = bool(
                    any(
                        str(error).startswith("FFD:")
                        for error in (result.get("errors") or [])
                    )
                    or not result.get("ffd", {}).get("coverage_passed", False)
                )
                hmm_failed = bool(
                    dependency.requires_hmm
                    and (
                        any(
                            str(error).startswith("HMM:")
                            for error in (result.get("errors") or [])
                        )
                        or not result.get("hmm", {}).get("coverage_passed", False)
                    )
                )
                transaction_components = (
                    {"hmm_features", "hmm_state", "generation_manifest"}
                    if hmm_failed and not ffd_failed
                    else None
                )
                _invalidate_asset_generation(asset)
                transaction = _begin_generation_transaction(
                    asset,
                    reason=reason or "continuation_health_failed",
                    components=transaction_components,
                )
                try:
                    staging_dir = _prepare_staging_directory(transaction)
                    staged = _verify_asset_once(
                        asset,
                        dependency,
                        continuation_dir=staging_dir,
                    )
                    if staged["status"] != "PASS":
                        raise LiveFeatureContinuationError(
                            "staged generation failed verification: "
                            + json.dumps(
                                staged.get("errors")
                                or staged.get("ffd", {}).get("failures")
                                or staged.get("hmm", {}).get("failures"),
                                default=str,
                            )
                        )
                    required_components: set[str] = set()
                    if dependency.requires_ffd:
                        required_components.update(
                            {"ffd_features", "ffd_state"}
                        )
                    if dependency.requires_hmm:
                        required_components.update(
                            {"hmm_features", "hmm_state"}
                        )
                    staged_pairing = _generation_pairing(
                        asset,
                        required_components,
                        root=staging_dir,
                    )
                    _write_transaction_state(
                        transaction,
                        "STAGED_GENERATION_VALIDATED",
                        staged_pairing=staged_pairing,
                    )
                    activated_components = _activate_staged_generation(
                        asset,
                        transaction,
                        staging_dir,
                    )
                    _invalidate_asset_generation(asset)
                    repaired = _verify_asset_once(asset, dependency)
                    repaired["repair_attempted"] = True
                    repaired["repair_archive"] = {
                        "archive_path": transaction.get("archive_dir"),
                        "transaction_id": transaction.get("transaction_id"),
                        "targeted_components": transaction.get("targeted_components"),
                        "staging_dir": str(staging_dir),
                        "activated_components": activated_components,
                    }
                    if repaired["status"] != "PASS":
                        raise LiveFeatureContinuationError(
                            "activated generation failed independent verification: "
                            + json.dumps(
                                repaired.get("errors")
                                or repaired.get("ffd", {}).get("failures")
                                or repaired.get("hmm", {}).get("failures"),
                                default=str,
                            )
                        )
                    repaired["generation"] = _publish_generation_manifest(
                        asset,
                        repaired,
                        transaction=transaction,
                    )
                    repaired["transaction"] = _commit_generation_transaction(
                        transaction,
                        repaired["generation"],
                    )
                    repaired["status"] = "REPAIRED_PASS"
                    result = repaired
                except Exception as exc:
                    _invalidate_asset_generation(asset)
                    rollback = _rollback_generation_transaction(
                        transaction,
                        reason=f"{type(exc).__name__}:{exc}",
                    )
                    result["repair_attempted"] = True
                    result["repair_archive"] = {
                        "archive_path": transaction.get("archive_dir"),
                        "transaction_id": transaction.get("transaction_id"),
                        "targeted_components": transaction.get("targeted_components"),
                    }
                    result["status"] = "REPAIR_FAILED_ROLLED_BACK"
                    result.setdefault("errors", []).append(
                        f"RECOVERY:{type(exc).__name__}:{exc}"
                    )
                    result["rollback"] = {
                        "status": "PREVIOUS_GENERATION_RESTORED",
                        "created_at": _utc_now(),
                        "restored_components": rollback.get("restored_components"),
                    }
                finally:
                    _invalidate_asset_generation(asset)

        if logger is not None:
            level = logger.info if result["status"] in {"PASS", "REPAIRED_PASS", "NOT_REQUIRED"} else logger.error
            level(
                "LIVE_FEATURE_CONTINUATION_HEALTH asset=%s status=%s live_latest=%s ffd_required=%s ffd_static=%s ffd_runtime=%s ffd_state=%s hmm_required=%s hmm_static=%s hmm_runtime=%s hmm_state=%s repair_attempted=%s errors=%s",
                asset,
                result["status"],
                result.get("live_bar_latest"),
                result["dependency"].get("requires_ffd"),
                result.get("ffd", {}).get("static_latest"),
                result.get("ffd", {}).get("runtime_latest"),
                result.get("ffd", {}).get("state_latest"),
                result["dependency"].get("requires_hmm"),
                result.get("hmm", {}).get("static_latest"),
                result.get("hmm", {}).get("runtime_latest"),
                result.get("hmm", {}).get("state_latest"),
                result.get("repair_attempted"),
                result.get("errors") or result.get("ffd", {}).get("failures") or result.get("hmm", {}).get("failures"),
            )

        with _CACHE_LOCK:
            _HEALTH_CACHE[key] = dict(result)
        return result


def _write_health_report(payload: Mapping[str, Any]) -> None:
    with _REPORT_LOCK:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(HEALTH_REPORT_JSON, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
        rows: list[dict[str, Any]] = []
        for asset, record in (payload.get("assets") or {}).items():
            rows.append({
                "asset": asset,
                "status": record.get("status"),
                "live_bar_latest": record.get("live_bar_latest"),
                "requires_ffd": record.get("dependency", {}).get("requires_ffd"),
                "ffd_static_latest": record.get("ffd", {}).get("static_latest"),
                "ffd_runtime_latest": record.get("ffd", {}).get("runtime_latest"),
                "ffd_state_latest": record.get("ffd", {}).get("state_latest"),
                "requires_hmm": record.get("dependency", {}).get("requires_hmm"),
                "hmm_static_latest": record.get("hmm", {}).get("static_latest"),
                "hmm_runtime_latest": record.get("hmm", {}).get("runtime_latest"),
                "hmm_state_latest": record.get("hmm", {}).get("state_latest"),
                "repair_attempted": record.get("repair_attempted"),
                "errors": " | ".join(record.get("errors") or record.get("ffd", {}).get("failures") or record.get("hmm", {}).get("failures") or []),
            })
        frame = pd.DataFrame(rows)
        tmp = HEALTH_REPORT_CSV.with_name(f".{HEALTH_REPORT_CSV.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            frame.to_csv(tmp, index=False)
            os.replace(tmp, HEALTH_REPORT_CSV)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass


def ensure_assets_live_feature_continuation(
    *,
    assets: Iterable[str],
    manifest: Mapping[str, Any],
    logger: logging.Logger | None = None,
    auto_repair: bool = True,
    fail_closed: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    ordered_assets = sorted({str(asset).upper() for asset in assets if str(asset).strip()})
    dependencies = dependency_map_from_manifest(manifest, ordered_assets)
    records: dict[str, dict[str, Any]] = {}
    healthy: list[str] = []
    blocked: list[str] = []
    for asset in ordered_assets:
        dependency = dependencies.get(asset) or DependencyRecord(
            asset=asset,
            requires_ffd=True,
            requires_hmm=True,
            selected_edge_count=0,
            ffd_edges=[],
            hmm_edges=[],
            dependency_source="conservative_missing_manifest_asset",
        )
        record = verify_or_repair_asset(
            asset,
            dependency,
            auto_repair=auto_repair,
            logger=logger,
            use_cache=use_cache,
        )
        records[asset] = record
        if record["status"] in {"PASS", "REPAIRED_PASS", "NOT_REQUIRED"}:
            healthy.append(asset)
        else:
            blocked.append(asset)

    payload = {
        "schema_version": "live_feature_continuation_health_v1",
        "created_at": _utc_now(),
        "auto_repair": bool(auto_repair),
        "healthy_assets": healthy,
        "blocked_assets": blocked,
        "status": "PASS" if not blocked else ("PARTIAL_PASS" if healthy else "FAIL"),
        "assets": records,
    }
    _write_health_report(payload)
    if logger is not None:
        logger.info(
            "LIVE_FEATURE_CONTINUATION_BATCH status=%s healthy_assets=%s blocked_assets=%s auto_repair=%s report=%s",
            payload["status"], healthy, blocked, auto_repair, HEALTH_REPORT_JSON.relative_to(V2_ROOT).as_posix(),
        )
    if fail_closed and blocked:
        raise LiveFeatureContinuationError(f"Live feature continuation failed for assets: {blocked}")
    return payload


def clear_health_cache() -> None:
    with _CACHE_LOCK:
        _HEALTH_CACHE.clear()
