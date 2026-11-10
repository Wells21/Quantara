"""
Verify a V2 asset-level HMM regime manifest and artifacts.

No edge should consume regime/HMM features until the shared asset-level regime
artifact is verified. This mirrors FFD artifact verification and prevents
research/live HMM mismatch.

Run from trading_models_v2:

    python ./shared/regime/verify_asset_hmm_regime_manifest.py --asset XAUUSD
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.io import portable_pickle  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.contracts import V2_CONTRACT_VERSION  # noqa: E402
from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: E402
from shared.io.artifact_hash import verify_artifact_hashes  # noqa: E402
from shared.live_streaming import dependency_transactions as dependency_txn  # noqa: E402
from shared.regime.hmm_regime import (  # noqa: E402
    HMM_FEATURES,
    FORBIDDEN_HMM_FEATURE_SUBSTRINGS,
    HMMRegimeConfig,
    LIVE_HMM_STATE_SCHEMA_VERSION,
    continue_hmm_regime_rows,
    engineer_hmm_inputs,
)

LIVE_CONTINUATION_DIR = _V2_ROOT / "portfolio_live" / "outputs" / "live_feature_continuation"

# Every reusable HMM checkpoint must have been written by the current
# integrity-aware continuation implementation. This prevents a long-running
# process that imported an older writer from silently reusing a hole-bearing
# CSV/state pair after the source file has been patched.
HMM_CONTINUATION_WRITER_CONTRACT = "HMM_CONTINUATION_WRITER_V3_TRANSACTIONAL_PREFIX_COMMIT"



@contextmanager
def _exclusive_output_lock(target_path: Path, *, timeout_seconds: float = 900.0, poll_seconds: float = 0.10):
    """Cross-platform best-effort exclusive lock for live continuation outputs.

    The live dashboard can run multiple edge workers. Several runtimes may verify
    the same asset-level HMM continuation at once. The lock prevents one worker
    from reading a half-written continuation file while another worker is writing
    it. It does not change HMM math or artifact contracts.
    """
    lock_path = target_path.with_name(f"{target_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    fd: int | None = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"pid={os.getpid()} created_at={time.time()}\n".encode("utf-8"))
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            except Exception:
                age = 0.0
            if age > timeout_seconds:
                try:
                    lock_path.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except Exception:
                    pass
            if (time.monotonic() - start) > timeout_seconds:
                raise TimeoutError(f"Timed out waiting for live continuation lock: {lock_path}")
            time.sleep(poll_seconds)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_csv(df: pd.DataFrame, path: Path, *, index: bool = False) -> None:
    """Write a CSV through a unique temp file then atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        df.to_csv(tmp_path, index=index)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _ordered_utc_timestamp_index(values: Any, *, source: str) -> pd.DatetimeIndex:
    """Return a strict ordered UTC timestamp index for persistence validation."""
    parsed = _parse_utc_datetime_series(values)
    if parsed.isna().any():
        raise HMMRegimeManifestVerificationError(
            f"{source} contains an invalid Datetime value"
        )
    index = pd.DatetimeIndex(parsed)
    if index.has_duplicates:
        duplicates = index[index.duplicated()].unique()
        raise HMMRegimeManifestVerificationError(
            f"{source} contains duplicate Datetime values: "
            f"{[pd.Timestamp(value).isoformat() for value in duplicates[:5]]}"
        )
    if not index.is_monotonic_increasing:
        raise HMMRegimeManifestVerificationError(
            f"{source} Datetime is not sorted ascending"
        )
    return index


def _live_continuation_prefix_timestamp_coverage(
    processed: pd.DataFrame,
    existing: pd.DataFrame,
    *,
    static_latest: pd.Timestamp,
) -> dict[str, Any]:
    """Prove that an existing live HMM file is an exact eligible timestamp prefix.

    A checkpoint is reusable only when every canonical HMM-eligible timestamp up
    to the checkpoint's latest observation is present, in order, with no extra
    timestamps. Merely matching the latest timestamp is insufficient: an internal
    hole can otherwise survive indefinitely because no row is considered pending.
    """

    expected_live = _ordered_utc_timestamp_index(
        processed.loc[processed["Datetime"] > static_latest, "Datetime"],
        source="canonical_hmm_eligible_live_timestamps",
    )
    actual_live = _ordered_utc_timestamp_index(
        existing.loc[existing["Datetime"] > static_latest, "Datetime"],
        source="existing_hmm_live_timestamps",
    )

    if len(actual_live):
        cutoff = pd.Timestamp(actual_live[-1])
        expected_prefix = expected_live[expected_live <= cutoff]
    else:
        cutoff = None
        expected_prefix = expected_live[:0]

    expected_set = set(expected_prefix)
    actual_set = set(actual_live)
    missing = sorted(expected_set.difference(actual_set))
    unexpected = sorted(actual_set.difference(expected_set))
    passed = bool(actual_live.equals(expected_prefix))

    return {
        "schema_version": "HMM_EXISTING_PREFIX_TIMESTAMP_COVERAGE_V1",
        "checked": True,
        "passed": passed,
        "expected_prefix_count": int(len(expected_prefix)),
        "actual_count": int(len(actual_live)),
        "cutoff_timestamp": cutoff.isoformat() if cutoff is not None else None,
        "missing_count": int(len(missing)),
        "missing_sample": [pd.Timestamp(value).isoformat() for value in missing[:10]],
        "unexpected_count": int(len(unexpected)),
        "unexpected_sample": [pd.Timestamp(value).isoformat() for value in unexpected[:10]],
        "sequence_equal": passed,
    }


def _atomic_write_hmm_csv_verified(
    df: pd.DataFrame,
    path: Path,
    *,
    required: list[str],
) -> dict[str, Any]:
    """Persist HMM rows only after exact temp/final timestamp validation.

    VERIFIED_HMM_TIMESTAMP_PERSISTENCE_V2
    """
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise HMMRegimeManifestVerificationError(
            f"HMM persistence table is missing required columns: {missing}"
        )

    payload = df.loc[:, required].copy()
    expected = _ordered_utc_timestamp_index(
        payload["Datetime"],
        source="in_memory_hmm_output",
    )
    if len(expected) != len(payload):
        raise HMMRegimeManifestVerificationError(
            "In-memory HMM timestamp count does not match output row count"
        )

    # Use one uniform, lossless UTC timestamp representation for every row.
    payload["Datetime"] = [
        pd.Timestamp(value).isoformat(timespec="microseconds")
        for value in expected
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.verified.tmp"
    )

    def verify_written(candidate: Path, *, source: str) -> None:
        try:
            reloaded = pd.read_csv(candidate, usecols=["Datetime"])
        except Exception as exc:
            raise HMMRegimeManifestVerificationError(
                f"Could not reload {source}: {exc}"
            ) from exc

        actual = _ordered_utc_timestamp_index(
            reloaded["Datetime"],
            source=source,
        )
        if len(reloaded) != len(payload):
            raise HMMRegimeManifestVerificationError(
                f"{source} row count changed during persistence: "
                f"{len(reloaded)} != {len(payload)}"
            )
        if len(actual) != len(expected) or not actual.equals(expected):
            expected_set = set(expected)
            actual_set = set(actual)
            missing_values = sorted(expected_set.difference(actual_set))
            unexpected_values = sorted(actual_set.difference(expected_set))
            raise HMMRegimeManifestVerificationError(
                f"{source} Datetime contract failed; "
                f"missing={[pd.Timestamp(v).isoformat() for v in missing_values[:5]]}; "
                f"unexpected={[pd.Timestamp(v).isoformat() for v in unexpected_values[:5]]}"
            )

    try:
        payload.to_csv(
            tmp_path,
            index=False,
            lineterminator="\n",
            float_format="%.17g",
        )
        verify_written(tmp_path, source="temporary_hmm_csv")
        os.replace(tmp_path, path)
        verify_written(path, source="persisted_hmm_csv")
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

    return {
        "schema_version": "VERIFIED_HMM_TIMESTAMP_PERSISTENCE_V2",
        "row_count": int(len(payload)),
        "first_timestamp": (
            pd.Timestamp(expected[0]).isoformat() if len(expected) else None
        ),
        "last_timestamp": (
            pd.Timestamp(expected[-1]).isoformat() if len(expected) else None
        ),
        "ordered_timestamp_contract_passed": True,
        "temporary_reload_passed": True,
        "final_reload_passed": True,
    }


def _atomic_joblib_dump(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        portable_pickle.dump(payload, tmp_path)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _parse_utc_datetime_series(values: Any) -> pd.Series:
    """Parse mixed ISO timestamp formats into UTC timestamps."""
    if isinstance(values, pd.Series):
        raw = values.copy()
    else:
        raw = pd.Series(values)

    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = to_datetime_iso(raw, utc=True, errors="coerce")

    non_empty = raw.notna() & raw.astype(str).str.strip().ne("")
    missed = parsed.isna() & non_empty
    if missed.any():
        reparsed = []
        for value in raw.loc[missed]:
            try:
                stamp = pd.Timestamp(value)
                reparsed.append(stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC"))
            except Exception:
                reparsed.append(pd.NaT)
        parsed.loc[missed] = reparsed
    return parsed



class HMMRegimeManifestVerificationError(RuntimeError):
    """Raised when an HMM regime artifact violates the V2 contract."""


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise HMMRegimeManifestVerificationError(f"Missing HMM regime manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HMMRegimeManifestVerificationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HMMRegimeManifestVerificationError("HMM regime manifest must be a JSON object.")
    return data


def _require_equal(manifest: Mapping[str, Any], key: str, expected: Any) -> None:
    actual = manifest.get(key)
    if actual != expected:
        raise HMMRegimeManifestVerificationError(f"Manifest key {key!r} expected {expected!r}, got {actual!r}")


def _require_false(manifest: Mapping[str, Any], key: str) -> None:
    actual = manifest.get(key)
    if actual is not False:
        raise HMMRegimeManifestVerificationError(f"Manifest key {key!r} must be false, got {actual!r}")


def _hash_table_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise HMMRegimeManifestVerificationError("Manifest artifact_hashes must be an object.")
    table: dict[str, Mapping[str, Any]] = {}
    for key in ("regime_features", "live_bundle"):
        record = hashes.get(key)
        if not isinstance(record, dict):
            raise HMMRegimeManifestVerificationError(f"artifact_hashes.{key} must be an object.")
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise HMMRegimeManifestVerificationError(f"artifact_hashes.{key}.path must be non-empty.")
        table[path] = record
    return table


def _validate_price_only_features(manifest: Mapping[str, Any]) -> None:
    hmm_features = manifest.get("hmm_features")
    if hmm_features != list(HMM_FEATURES):
        raise HMMRegimeManifestVerificationError(f"Unexpected HMM features: {hmm_features}")
    for feature in hmm_features:
        lower = str(feature).lower()
        for token in FORBIDDEN_HMM_FEATURE_SUBSTRINGS:
            if token in lower:
                raise HMMRegimeManifestVerificationError(f"Forbidden HMM feature detected: {feature}")


def _validate_regime_features(path: Path, manifest: Mapping[str, Any]) -> None:
    if not path.exists():
        raise HMMRegimeManifestVerificationError(f"Missing HMM regime features file: {path}")
    df = pd.read_csv(path)
    n_states = int(manifest["n_states"])
    required = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{i}" for i in range(n_states)],
        *HMM_FEATURES,
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise HMMRegimeManifestVerificationError(f"HMM regime feature artifact missing columns: {missing}")
    expected_rows = int(manifest["rows_output"])
    if len(df) != expected_rows:
        raise HMMRegimeManifestVerificationError(f"Regime feature rows expected {expected_rows}, got {len(df)}")
    dt = _parse_utc_datetime_series(df["Datetime"])
    if dt.isna().any() or not dt.is_monotonic_increasing:
        raise HMMRegimeManifestVerificationError("Regime Datetime must be valid and sorted ascending.")
    state_values = pd.to_numeric(df["Regime_State"], errors="coerce")
    if state_values.isna().any():
        raise HMMRegimeManifestVerificationError("Regime_State contains NaN/non-numeric values.")
    if not set(state_values.astype(int).unique()).issubset(set(range(n_states))):
        raise HMMRegimeManifestVerificationError("Regime_State contains values outside configured state range.")
    prob_cols = [f"Prob_State_{i}" for i in range(n_states)]
    probs = df[prob_cols].apply(pd.to_numeric, errors="coerce")
    if probs.isna().any().any():
        raise HMMRegimeManifestVerificationError("Probability columns contain NaN/non-numeric values.")
    if not np.allclose(probs.sum(axis=1).to_numpy(dtype=float), 1.0, atol=1e-5):
        raise HMMRegimeManifestVerificationError("HMM state probabilities do not sum to 1 within tolerance.")
    numeric_cols = ["Regime_Entropy", "Regime_Duration", *HMM_FEATURES]
    for col in numeric_cols:
        values = pd.to_numeric(df[col], errors="coerce")
        if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all():
            raise HMMRegimeManifestVerificationError(f"Column contains NaN/non-finite values: {col}")


def _validate_live_bundle(path: Path, manifest: Mapping[str, Any]) -> None:
    if not path.exists():
        raise HMMRegimeManifestVerificationError(f"Missing HMM live bundle: {path}")
    bundle = portable_pickle.load(path)
    if not isinstance(bundle, dict):
        raise HMMRegimeManifestVerificationError("HMM live bundle must be a dictionary.")
    expected = {
        "schema_version": V2_CONTRACT_VERSION,
        "runtime_module": "shared.regime.hmm_regime",
        "price_only_contract": True,
        "hmm_features": list(HMM_FEATURES),
        "n_states": manifest.get("n_states"),
        "train_window": manifest.get("train_window"),
        "refit_frequency": manifest.get("refit_frequency"),
        "inference_lookback": manifest.get("inference_lookback"),
        "noise_epsilon": float(manifest.get("noise_epsilon", 1e-8)),
        "noise_used_in_training": True,
        "uses_future_data": False,
    }
    for key, expected_value in expected.items():
        if bundle.get(key) != expected_value:
            raise HMMRegimeManifestVerificationError(
                f"Live bundle key {key!r} expected {expected_value!r}, got {bundle.get(key)!r}"
            )
    required_objects = ["model", "scaler", "state_map", "train_buffer_df", "train_buffer_features_df"]
    missing = [key for key in required_objects if key not in bundle]
    if missing:
        raise HMMRegimeManifestVerificationError(f"HMM live bundle missing keys: {missing}")
    if not isinstance(bundle["train_buffer_df"], pd.DataFrame):
        raise HMMRegimeManifestVerificationError("train_buffer_df must be a pandas DataFrame.")
    if not isinstance(bundle["train_buffer_features_df"], pd.DataFrame):
        raise HMMRegimeManifestVerificationError("train_buffer_features_df must be a pandas DataFrame.")


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()


def _latest_datetime(path: Path) -> pd.Timestamp | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, usecols=["Datetime"])
    except Exception:
        return None
    if df.empty:
        return None
    dt = _parse_utc_datetime_series(df["Datetime"]).dropna()
    if dt.empty:
        return None
    return pd.Timestamp(dt.max())


def _entropy_rows(probs: np.ndarray) -> np.ndarray:
    clipped = np.clip(probs.astype(float), 1e-15, 1.0)
    return -np.sum(clipped * np.log(clipped), axis=1)


def _add_regime_duration(df: pd.DataFrame) -> pd.DataFrame:
    out = df.sort_values("Datetime").reset_index(drop=True).copy()
    states = pd.to_numeric(out["Regime_State"], errors="coerce").astype("Int64")
    durations: list[int] = []
    run = 0
    prev: int | None = None
    for value in states:
        state = None if pd.isna(value) else int(value)
        if state is None:
            run = 0
            prev = None
        elif prev is None or state != prev:
            run = 1
            prev = state
        else:
            run += 1
        durations.append(run)
    out["Regime_Duration"] = durations
    return out


def _continue_regime_duration(
    static_regime: pd.DataFrame,
    live_extension: pd.DataFrame,
) -> pd.DataFrame:
    """Continue Regime_Duration after the immutable static research horizon."""

    extension = live_extension.sort_values("Datetime", kind="mergesort").reset_index(drop=True).copy()
    if extension.empty:
        return extension

    if static_regime.empty:
        return _add_regime_duration(extension)

    static_states = pd.to_numeric(static_regime["Regime_State"], errors="raise")
    static_durations = pd.to_numeric(static_regime["Regime_Duration"], errors="raise")
    previous_state = int(static_states.iloc[-1])
    run = int(static_durations.iloc[-1])

    durations: list[int] = []
    for value in pd.to_numeric(extension["Regime_State"], errors="raise"):
        state = int(value)
        if state == previous_state:
            run += 1
        else:
            previous_state = state
            run = 1
        durations.append(run)

    extension["Regime_Duration"] = durations
    return extension


def _continuation_preserves_static_history(
    static_path: Path,
    continuation_path: Path,
    *,
    static_latest: pd.Timestamp,
    required: list[str],
) -> bool:
    """Return True only when the continuation contains the exact static prefix."""

    if not continuation_path.exists():
        return False

    try:
        static = pd.read_csv(static_path, usecols=required)
        continued = pd.read_csv(continuation_path, usecols=required)
    except Exception:
        return False

    for frame in (static, continued):
        frame["Datetime"] = _parse_utc_datetime_series(frame["Datetime"])

    static = (
        static.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates(subset=["Datetime"], keep="last")
        .reset_index(drop=True)
    )
    historical = (
        continued.loc[continued["Datetime"] <= static_latest]
        .dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates(subset=["Datetime"], keep="last")
        .reset_index(drop=True)
    )

    if len(static) != len(historical):
        return False
    if not static["Datetime"].equals(historical["Datetime"]):
        return False

    exact_integer_columns = {"Regime_State", "Regime_Duration"}
    for column in required:
        if column == "Datetime":
            continue

        left = pd.to_numeric(static[column], errors="coerce")
        right = pd.to_numeric(historical[column], errors="coerce")
        if left.shape != right.shape:
            return False

        if column in exact_integer_columns:
            if left.isna().any() or right.isna().any():
                return False
            if not np.array_equal(
                left.to_numpy(dtype=np.int64),
                right.to_numpy(dtype=np.int64),
            ):
                return False
            continue

        # Continuous HMM fields are written to CSV and read back. Preserve the
        # static history semantically while allowing only round-trip float noise.
        if not np.allclose(
            left.to_numpy(dtype=float),
            right.to_numpy(dtype=float),
            rtol=1e-12,
            atol=1e-12,
            equal_nan=True,
        ):
            return False

    return True


def _maybe_build_live_continued_regime_features(
    *,
    asset: str,
    manifest: Mapping[str, Any],
    static_features_rel: str,
    bundle_rel: str,
    continuation_dir: Path | None = None,
) -> tuple[str, bool, str | None, dict[str, Any]]:
    """Append immutable, one-bar-at-a-time HMM continuation rows.

    The shared research bundle remains the initial model/scaler/state-map source.
    Live state persists the exact noise stream, rolling training buffer, inference
    context, regime duration, and scheduled refit counter.
    """
    proof: dict[str, Any] = {
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": True,
        "deterministic_noise_contract_preserved": True,
        "continuation_algorithm": "causal_one_bar_context_with_scheduled_past_only_refit",
        "rows_appended": 0,
    }
    static_path = _V2_ROOT / static_features_rel
    static_latest = _latest_datetime(static_path)
    ffd_verification = verify_asset_ffd_manifest(
        asset,
        continuation_dir=continuation_dir,
    )
    if not ffd_verification.get("streaming_parity_passed"):
        return static_features_rel, False, "ffd_streaming_parity_not_proven", {
            **proof,
            "streaming_parity_passed": False,
            "append_only_history": False,
            "restart_parity_passed": False,
        }
    ffd_path = _V2_ROOT / str(ffd_verification["features"])
    ffd_latest = _latest_datetime(ffd_path)
    if static_latest is None or ffd_latest is None:
        return static_features_rel, False, "static_or_ffd_latest_missing", proof

    n_states = int(manifest["n_states"])
    required = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{i}" for i in range(n_states)],
        *HMM_FEATURES,
    ]
    continuation_root = Path(continuation_dir or LIVE_CONTINUATION_DIR)
    continuation_root.mkdir(parents=True, exist_ok=True)
    out_path = continuation_root / f"{asset.lower()}_hmm_regime_features_live_continued.csv"
    state_path = continuation_root / f"{asset.lower()}_hmm_streaming_state.joblib"

    try:
        with _exclusive_output_lock(out_path):
            static_regime = pd.read_csv(static_path, usecols=required)
            static_regime["Datetime"] = _parse_utc_datetime_series(static_regime["Datetime"])
            static_regime = static_regime.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
            static_regime = static_regime.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
            if static_regime.empty or pd.Timestamp(static_regime["Datetime"].max()) != static_latest:
                raise HMMRegimeManifestVerificationError("Static HMM artifact normalization changed its research horizon")

            processed = engineer_hmm_inputs(
                pd.read_csv(ffd_path),
                config=HMMRegimeConfig(
                    n_states=n_states,
                    train_window=int(manifest["train_window"]),
                    refit_frequency=int(manifest["refit_frequency"]),
                    inference_lookback=int(manifest["inference_lookback"]),
                    min_covar=float(manifest.get("min_covar", 0.01)),
                    random_state=int(manifest.get("random_state", 42)),
                    max_iter=int(manifest.get("max_iter", 200)),
                    noise_epsilon=float(manifest.get("noise_epsilon", 1e-8)),
                ),
            )

            # HMM_ELIGIBLE_TIMESTAMP_COVERAGE_V1
            # The research HMM contract intentionally excludes rows whose derived
            # price-only HMM inputs are undefined (for example a zero-range bar).
            # Coverage must therefore target the latest HMM-eligible row rather
            # than every raw FFD timestamp.
            expected_hmm_latest = (
                pd.Timestamp(processed["Datetime"].max())
                if not processed.empty
                else static_latest
            )
            proof.update({
                "source_ffd_latest_timestamp": (
                    ffd_latest.isoformat() if ffd_latest is not None else None
                ),
                "expected_hmm_latest_timestamp": (
                    expected_hmm_latest.isoformat()
                    if expected_hmm_latest is not None
                    else None
                ),
            })
            bundle = portable_pickle.load(_V2_ROOT / bundle_rel)
            if not isinstance(bundle, dict):
                raise HMMRegimeManifestVerificationError("HMM research live bundle must be a dictionary")

            prior_state: dict[str, Any] | None = None
            if state_path.exists():
                try:
                    loaded = portable_pickle.load(state_path)
                    if (
                        isinstance(loaded, dict)
                        and loaded.get("schema_version") == LIVE_HMM_STATE_SCHEMA_VERSION
                    ):
                        loaded_writer_contract = loaded.get("continuation_writer_contract")
                        if loaded_writer_contract == HMM_CONTINUATION_WRITER_CONTRACT:
                            prior_state = loaded
                        else:
                            proof["prior_state_rejected_reason"] = (
                                "writer_contract_mismatch:"
                                f"{loaded_writer_contract!r}!="
                                f"{HMM_CONTINUATION_WRITER_CONTRACT!r}"
                            )
                except Exception:
                    prior_state = None

            existing = static_regime.copy()
            existing_valid = False
            if out_path.exists() and _continuation_preserves_static_history(
                static_path,
                out_path,
                static_latest=static_latest,
                required=required,
            ):
                existing = pd.read_csv(out_path, usecols=required)
                existing["Datetime"] = _parse_utc_datetime_series(existing["Datetime"])
                existing = existing.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
                existing = existing.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
                existing_valid = True

            existing_prefix_coverage: dict[str, Any] = {
                "schema_version": "HMM_EXISTING_PREFIX_TIMESTAMP_COVERAGE_V1",
                "checked": False,
                "passed": not out_path.exists(),
                "reason": "continuation_file_missing" if not out_path.exists() else "static_prefix_invalid",
            }
            if existing_valid:
                existing_prefix_coverage = _live_continuation_prefix_timestamp_coverage(
                    processed,
                    existing,
                    static_latest=static_latest,
                )
                if not existing_prefix_coverage["passed"]:
                    existing_valid = False
            proof["existing_continuation_prefix_coverage"] = existing_prefix_coverage

            if prior_state is not None:
                state_latest = pd.Timestamp(prior_state.get("last_observation_timestamp"))
                if state_latest.tzinfo is None:
                    state_latest = state_latest.tz_localize("UTC")
                else:
                    state_latest = state_latest.tz_convert("UTC")
                existing_latest = pd.Timestamp(existing["Datetime"].max()) if not existing.empty else static_latest
                if (
                    not existing_valid
                    or state_latest != existing_latest
                    or state_latest > ffd_latest
                ):
                    prior_state = None
                    existing = static_regime.copy()
            else:
                # Never preserve an old continuation without its matching state.
                # Rebuild every live row from the verified research checkpoint.
                existing = static_regime.copy()

            extension, next_state, continuation_meta = continue_hmm_regime_rows(
                processed,
                live_bundle=bundle,
                static_regime=static_regime,
                total_static_processed_rows=int(manifest["rows_input_after_preprocessing"]),
                state=prior_state,
                config=HMMRegimeConfig(
                    n_states=n_states,
                    train_window=int(manifest["train_window"]),
                    refit_frequency=int(manifest["refit_frequency"]),
                    inference_lookback=int(manifest["inference_lookback"]),
                    min_covar=float(manifest.get("min_covar", 0.01)),
                    random_state=int(manifest.get("random_state", 42)),
                    max_iter=int(manifest.get("max_iter", 200)),
                    noise_epsilon=float(manifest.get("noise_epsilon", 1e-8)),
                ),
            )
            out = (
                existing.copy()
                if extension.empty
                else pd.concat([existing, extension], ignore_index=True)
            )
            out["Datetime"] = _parse_utc_datetime_series(out["Datetime"])
            out = out.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
            out = out.drop_duplicates("Datetime", keep="first").reset_index(drop=True)
            if out["Datetime"].duplicated().any() or not out["Datetime"].is_monotonic_increasing:
                raise HMMRegimeManifestVerificationError("HMM continuation is not strictly append-only")

            expected_live_times = set(
                processed.loc[processed["Datetime"] > static_latest, "Datetime"]
            )
            produced_live_times = set(
                out.loc[out["Datetime"] > static_latest, "Datetime"]
            )
            missing_eligible_times = sorted(
                expected_live_times.difference(produced_live_times)
            )
            if missing_eligible_times:
                raise HMMRegimeManifestVerificationError(
                    "HMM continuation omitted eligible post-research rows; "
                    f"missing={missing_eligible_times[:5]}"
                )
            proof.update({
                "eligible_post_research_rows": int(len(expected_live_times)),
                "covered_eligible_post_research_rows": int(len(expected_live_times)),
                "missing_eligible_post_research_rows": 0,
            })

            historical = out.loc[out["Datetime"] <= static_latest, required].reset_index(drop=True)
            pd.testing.assert_frame_equal(
                historical,
                static_regime[required].reset_index(drop=True),
                check_dtype=False,
                check_exact=False,
                rtol=1e-12,
                atol=1e-12,
            )
            persistence_proof = _atomic_write_hmm_csv_verified(
                out,
                out_path,
                required=required,
            )
            continuation_meta["persistence_proof"] = persistence_proof
            canonical_live_times_for_commit = _ordered_utc_timestamp_index(
                processed.loc[
                    processed["Datetime"] > static_latest,
                    "Datetime",
                ],
                source="canonical_hmm_transaction_live_timestamps",
            )
            previous_hmm_sequence = int(
                ((prior_state or {}).get("timestamp_transaction") or {}).get(
                    "generation_sequence", 0
                )
            )
            extension_times_for_generation = (
                _ordered_utc_timestamp_index(
                    extension["Datetime"],
                    source="hmm_transaction_generation_extension",
                )
                if not extension.empty
                else pd.DatetimeIndex([], tz="UTC")
            )
            hmm_generation_id = dependency_txn.generation_id(
                asset,
                "HMM",
                prior_watermark=(
                    (prior_state or {}).get("last_observation_timestamp")
                    or static_latest.isoformat()
                ),
                target_watermark=next_state.get("last_observation_timestamp"),
                timestamps=extension_times_for_generation,
            )
            committed_hmm_transaction = dependency_txn.certify_csv_prefix(
                out_path,
                static_latest=static_latest,
                expected_live_times=canonical_live_times_for_commit,
                generation_sequence=previous_hmm_sequence + (
                    1 if len(extension_times_for_generation) else 0
                ),
                last_generation_id=hmm_generation_id,
            )
            continuation_meta["timestamp_transaction"] = committed_hmm_transaction
            next_state["continuation_writer_contract"] = HMM_CONTINUATION_WRITER_CONTRACT
            next_state["timestamp_transaction"] = committed_hmm_transaction
            next_state["dependency_generation_id"] = hmm_generation_id
            next_state["continuation_writer_pid"] = int(os.getpid())
            next_state["continuation_written_at_utc"] = pd.Timestamp.now(tz="UTC").isoformat()
            continuation_meta["continuation_writer_contract"] = HMM_CONTINUATION_WRITER_CONTRACT
            _atomic_joblib_dump(next_state, state_path)
            reloaded = portable_pickle.load(state_path)
            if reloaded.get("last_observation_timestamp") != next_state.get("last_observation_timestamp"):
                raise HMMRegimeManifestVerificationError("HMM checkpoint restart roundtrip failed")
            if not _continuation_preserves_static_history(
                static_path,
                out_path,
                static_latest=static_latest,
                required=required,
            ):
                raise HMMRegimeManifestVerificationError("Written HMM continuation changed static research history")

            persisted_times = _ordered_utc_timestamp_index(
                pd.read_csv(out_path, usecols=["Datetime"])["Datetime"],
                source="post_checkpoint_persisted_hmm_csv",
            )
            expected_times = _ordered_utc_timestamp_index(
                out["Datetime"],
                source="post_checkpoint_in_memory_hmm_output",
            )
            if not persisted_times.equals(expected_times):
                raise HMMRegimeManifestVerificationError(
                    "Persisted HMM timestamp sequence changed after checkpoint write"
                )

            canonical_live_times = _ordered_utc_timestamp_index(
                processed.loc[processed["Datetime"] > static_latest, "Datetime"],
                source="canonical_post_write_hmm_eligible_live_timestamps",
            )
            persisted_live_times = persisted_times[persisted_times > static_latest]
            if not persisted_live_times.equals(canonical_live_times):
                missing_values = sorted(
                    set(canonical_live_times).difference(set(persisted_live_times))
                )
                unexpected_values = sorted(
                    set(persisted_live_times).difference(set(canonical_live_times))
                )
                raise HMMRegimeManifestVerificationError(
                    "Persisted HMM live coverage differs from the canonical eligible sequence; "
                    f"missing={[pd.Timestamp(v).isoformat() for v in missing_values[:10]]}; "
                    f"unexpected={[pd.Timestamp(v).isoformat() for v in unexpected_values[:10]]}"
                )
            continuation_meta["post_write_live_coverage"] = {
                "schema_version": "HMM_POST_WRITE_LIVE_COVERAGE_V1",
                "passed": True,
                "canonical_count": int(len(canonical_live_times)),
                "persisted_count": int(len(persisted_live_times)),
            }

        proof.update(continuation_meta)
        proof.update({
            "state_path": _rel(state_path),
            "rows_appended": int(len(extension)),
            "ffd_continuation_proof": ffd_verification.get("continuation_proof"),
        })
        return _rel(out_path), bool(ffd_latest > static_latest), None, proof
    except Exception as exc:
        proof.update({
            "streaming_parity_passed": False,
            "append_only_history": False,
            "restart_parity_passed": False,
            "hmm_refit_schedule_preserved": False,
            "deterministic_noise_contract_preserved": False,
        })
        return static_features_rel, False, f"live_regime_build_failed:{exc}", proof


def verify_asset_hmm_regime_manifest(
    asset: str,
    *,
    continuation_dir: Path | None = None,
) -> dict[str, Any]:
    layout = get_asset_layout(asset)
    manifest_path = layout.bars_root / "regime" / "hmm_regime_manifest.json"
    manifest = _load_manifest(manifest_path)

    _require_equal(manifest, "schema_version", V2_CONTRACT_VERSION)
    _require_equal(manifest, "asset", layout.asset)
    _require_equal(manifest, "runtime_module", "shared.regime.hmm_regime")
    _require_equal(manifest, "price_only_contract", True)
    _require_false(manifest, "uses_future_data")
    _validate_price_only_features(manifest)

    if int(manifest.get("n_states", 0)) < 2:
        raise HMMRegimeManifestVerificationError("n_states must be >= 2")
    if int(manifest.get("train_window", 0)) <= 0:
        raise HMMRegimeManifestVerificationError("train_window must be > 0")
    if int(manifest.get("rows_output", 0)) <= 0:
        raise HMMRegimeManifestVerificationError("rows_output must be > 0")

    mismatches = verify_artifact_hashes(_hash_table_from_manifest(manifest), root=_V2_ROOT)
    if mismatches:
        raise HMMRegimeManifestVerificationError("Artifact hash verification failed:\n" + "\n".join(mismatches))

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise HMMRegimeManifestVerificationError("Manifest outputs must be an object.")
    features_rel = outputs.get("regime_features")
    bundle_rel = outputs.get("live_bundle")
    if not isinstance(features_rel, str) or not isinstance(bundle_rel, str):
        raise HMMRegimeManifestVerificationError("Manifest outputs must include regime_features and live_bundle paths.")

    _validate_regime_features(_V2_ROOT / features_rel, manifest)
    _validate_live_bundle(_V2_ROOT / bundle_rel, manifest)

    runtime_features_rel, live_continued, live_continuation_warning, continuation_proof = _maybe_build_live_continued_regime_features(
        asset=layout.asset,
        manifest=manifest,
        static_features_rel=features_rel,
        bundle_rel=bundle_rel,
        continuation_dir=continuation_dir,
    )


    # LIVE_FEATURE_CONTINUATION_STRICT_COVERAGE_V1
    # HMM coverage follows the exact research preprocessing contract. Some valid
    # FFD rows are intentionally not HMM-eligible (for example zero-range bars),
    # so requiring an HMM row for every raw FFD timestamp is incorrect.
    ffd_runtime_verification = verify_asset_ffd_manifest(
        layout.asset,
        continuation_dir=continuation_dir,
    )
    ffd_runtime_latest = _latest_datetime(
        _V2_ROOT / str(ffd_runtime_verification["features"])
    )
    static_latest = _latest_datetime(_V2_ROOT / features_rel)
    runtime_latest = _latest_datetime(_V2_ROOT / runtime_features_rel)
    expected_value = continuation_proof.get("expected_hmm_latest_timestamp")
    expected_hmm_latest = None
    if expected_value is not None:
        expected_hmm_latest = pd.Timestamp(expected_value)
        expected_hmm_latest = (
            expected_hmm_latest.tz_localize("UTC")
            if expected_hmm_latest.tzinfo is None
            else expected_hmm_latest.tz_convert("UTC")
        )
    live_coverage_required = bool(
        expected_hmm_latest is not None
        and static_latest is not None
        and expected_hmm_latest > static_latest
    )
    live_coverage_failures: list[str] = []
    if live_coverage_required:
        if live_continuation_warning:
            live_coverage_failures.append(str(live_continuation_warning))
        if not bool(continuation_proof.get("streaming_parity_passed")):
            live_coverage_failures.append("streaming_parity_not_proven")
        if runtime_latest != expected_hmm_latest:
            live_coverage_failures.append(
                f"runtime_latest={runtime_latest} expected_hmm_latest={expected_hmm_latest}"
            )
        state_value = continuation_proof.get("last_observation_timestamp")
        state_latest = None
        if state_value is not None:
            state_latest = pd.Timestamp(state_value)
            state_latest = (
                state_latest.tz_localize("UTC")
                if state_latest.tzinfo is None
                else state_latest.tz_convert("UTC")
            )
        if state_value is not None and state_latest != runtime_latest:
            live_coverage_failures.append(
                f"state_latest={state_latest} runtime_latest={runtime_latest}"
            )
    if live_coverage_failures:
        raise HMMRegimeManifestVerificationError(
            f"{layout.asset} live HMM continuation failed closed: "
            + "; ".join(live_coverage_failures)
        )

    return {
        "asset": layout.asset,
        "rows_input_after_preprocessing": manifest.get("rows_input_after_preprocessing"),
        "rows_output": manifest.get("rows_output"),
        "n_states": manifest.get("n_states"),
        "train_window": manifest.get("train_window"),
        "refit_frequency": manifest.get("refit_frequency"),
        "inference_lookback": manifest.get("inference_lookback"),
        "noise_epsilon": float(manifest.get("noise_epsilon", 1e-8)),
        "noise_used_in_training": True,
        "refit_points_count": manifest.get("refit_points_count"),
        "all_refits_converged": manifest.get("all_refits_converged"),
        "features": runtime_features_rel,
        "static_features": features_rel,
        "live_bundle": bundle_rel,
        "manifest": manifest_path.as_posix(),
        "artifact_hashes_passed": True,
        "hmm_regime_contract_passed": True,
        "live_continuation_applied": bool(live_continued),
        "live_continuation_warning": live_continuation_warning,
        "streaming_parity_passed": bool(continuation_proof.get("streaming_parity_passed")),
        "append_only_history": bool(continuation_proof.get("append_only_history")),
        "restart_parity_passed": bool(continuation_proof.get("restart_parity_passed")),
        "hmm_refit_schedule_preserved": bool(continuation_proof.get("hmm_refit_schedule_preserved")),
        "deterministic_noise_contract_preserved": bool(continuation_proof.get("deterministic_noise_contract_preserved")),
        "continuation_proof": continuation_proof,
        "static_latest_timestamp": static_latest.isoformat() if static_latest is not None else None,
        "runtime_latest_timestamp": runtime_latest.isoformat() if runtime_latest is not None else None,
        "ffd_runtime_latest_timestamp": ffd_runtime_latest.isoformat() if ffd_runtime_latest is not None else None,
        "expected_hmm_latest_timestamp": expected_hmm_latest.isoformat() if expected_hmm_latest is not None else None,
        "live_coverage_required": live_coverage_required,
        "live_coverage_passed": not live_coverage_failures,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify V2 asset-level HMM regime manifest and artifacts.")
    parser.add_argument("--asset", required=True)
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = verify_asset_hmm_regime_manifest(args.asset)
    print("✅ V2 asset HMM regime manifest verification passed.")
    print(f"Asset: {result['asset']}")
    print(f"Rows after preprocessing: {result['rows_input_after_preprocessing']}")
    print(f"Rows output: {result['rows_output']}")
    print(f"States: {result['n_states']}")
    print(f"Train window: {result['train_window']}")
    print(f"Refit frequency: {result['refit_frequency']}")
    print(f"Inference lookback: {result['inference_lookback']}")
    print(f"Refit points: {result['refit_points_count']}")
    print(f"All refits converged: {result['all_refits_converged']}")
    print(f"Features: {result['features']}")
    print(f"Static features: {result['static_features']}")
    print(f"Live continuation applied: {result['live_continuation_applied']}")
    if result.get("live_continuation_warning"):
        print(f"Live continuation warning: {result['live_continuation_warning']}")
    print(f"Live bundle: {result['live_bundle']}")
    print("Artifact hashes: PASS")
    print("HMM regime contract: PASS")


if __name__ == "__main__":
    try:
        _run()
    except HMMRegimeManifestVerificationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
