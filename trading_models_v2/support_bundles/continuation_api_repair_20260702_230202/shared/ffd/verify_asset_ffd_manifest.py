"""
Verify a V2 asset-level FFD manifest and artifacts.

Blueprint purpose
-----------------
No edge should consume FFD features until the shared asset-level FFD artifact is
verified. This mirrors bar artifact verification and prevents research/live FFD
mismatch.

Run from trading_models_v2:

    python ./shared/ffd/verify_asset_ffd_manifest.py --asset XAUUSD
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
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.contracts import V2_CONTRACT_VERSION  # noqa: E402
from shared.ffd.fractional_differentiation import OHLC_COLUMNS, apply_frozen_ffd_to_ohlc  # noqa: E402
from shared.io.artifact_hash import verify_artifact_hashes  # noqa: E402

LIVE_CONTINUATION_DIR = _V2_ROOT / "portfolio_live" / "outputs" / "live_feature_continuation"
LIVE_BARS_DIR = _V2_ROOT / "portfolio_live" / "live_bars"



@contextmanager
def _exclusive_output_lock(target_path: Path, *, timeout_seconds: float = 900.0, poll_seconds: float = 0.10):
    """Cross-platform best-effort exclusive lock for live continuation outputs.

    The live dashboard can run multiple edge workers. Several runtimes may verify
    the same asset-level FFD continuation at once. The lock prevents one worker
    from reading a half-written continuation file while another worker is writing
    it. It does not change FFD math or artifact contracts.
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
            # If a previous dashboard died while holding the lock, clear a stale
            # lock after the timeout window and retry. Normal live builds should
            # be well below this timeout.
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


LIVE_FFD_STATE_SCHEMA_VERSION = "v2_ffd_streaming_state_v1"


def _atomic_joblib_dump(payload: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        joblib.dump(payload, tmp_path)
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _validate_static_ffd_prefix(static_path: Path, continued_path: Path) -> bool:
    required = ["Datetime", *OHLC_COLUMNS, *(f"{col}_FFD" for col in OHLC_COLUMNS)]
    try:
        static = pd.read_csv(static_path, usecols=required)
        continued = pd.read_csv(continued_path, usecols=required)
        static["Datetime"] = _parse_utc_datetime_series(static["Datetime"])
        continued["Datetime"] = _parse_utc_datetime_series(continued["Datetime"])
        static = static.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")
        continued = continued.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")
        prefix = continued[continued["Datetime"] <= static["Datetime"].max()]
        pd.testing.assert_frame_equal(
            prefix.reset_index(drop=True),
            static.reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
        return True
    except Exception:
        return False


def _parse_utc_datetime_series(values: Any) -> pd.Series:
    """Parse mixed ISO timestamp formats into UTC timestamps."""
    if isinstance(values, pd.Series):
        raw = values.copy()
    else:
        raw = pd.Series(values)

    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")

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



class FFDManifestVerificationError(RuntimeError):
    """Raised when an FFD artifact violates the V2 contract."""


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FFDManifestVerificationError(f"Missing FFD manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FFDManifestVerificationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FFDManifestVerificationError("FFD manifest must be a JSON object.")
    return data


def _require_equal(manifest: Mapping[str, Any], key: str, expected: Any) -> None:
    actual = manifest.get(key)
    if actual != expected:
        raise FFDManifestVerificationError(f"Manifest key {key!r} expected {expected!r}, got {actual!r}")


def _require_false(manifest: Mapping[str, Any], key: str) -> None:
    actual = manifest.get(key)
    if actual is not False:
        raise FFDManifestVerificationError(f"Manifest key {key!r} must be false, got {actual!r}")


def _hash_table_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise FFDManifestVerificationError("Manifest artifact_hashes must be an object.")
    table: dict[str, Mapping[str, Any]] = {}
    for key in ("ffd_features", "state_bundle"):
        record = hashes.get(key)
        if not isinstance(record, dict):
            raise FFDManifestVerificationError(f"artifact_hashes.{key} must be an object.")
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise FFDManifestVerificationError(f"artifact_hashes.{key}.path must be non-empty.")
        table[path] = record
    return table


def _validate_ffd_features(path: Path, expected_rows: int) -> None:
    if not path.exists():
        raise FFDManifestVerificationError(f"Missing FFD features file: {path}")
    df = pd.read_csv(path)
    required = ["Datetime", *OHLC_COLUMNS, *(f"{col}_FFD" for col in OHLC_COLUMNS)]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise FFDManifestVerificationError(f"FFD feature artifact missing required columns: {missing}")
    if len(df) != expected_rows:
        raise FFDManifestVerificationError(f"FFD feature rows expected {expected_rows}, got {len(df)}")
    for col in [*OHLC_COLUMNS, *(f"{col}_FFD" for col in OHLC_COLUMNS)]:
        values = pd.to_numeric(df[col], errors="coerce")
        if values.isna().any():
            raise FFDManifestVerificationError(f"FFD feature column contains NaN/non-numeric values: {col}")
    dt = _parse_utc_datetime_series(df["Datetime"])
    if dt.isna().any() or not dt.is_monotonic_increasing:
        raise FFDManifestVerificationError("FFD feature Datetime must be valid and sorted ascending.")


def _validate_state_bundle(path: Path, manifest: Mapping[str, Any]) -> None:
    if not path.exists():
        raise FFDManifestVerificationError(f"Missing FFD state bundle: {path}")
    bundle = joblib.load(path)
    if not isinstance(bundle, dict):
        raise FFDManifestVerificationError("FFD state bundle must be a dictionary.")
    expected = {
        "schema_version": V2_CONTRACT_VERSION,
        "frozen_d": manifest.get("frozen_d"),
        "ffd_window": manifest.get("ffd_window"),
        "live_buffer_bars": manifest.get("live_buffer_bars"),
        "d_reestimated_live": False,
        "uses_future_data": False,
    }
    for key, expected_value in expected.items():
        if bundle.get(key) != expected_value:
            raise FFDManifestVerificationError(
                f"State bundle key {key!r} expected {expected_value!r}, got {bundle.get(key)}"
            )
    raw_buffer = bundle.get("raw_buffer_df")
    if not isinstance(raw_buffer, pd.DataFrame):
        raise FFDManifestVerificationError("State bundle raw_buffer_df must be a pandas DataFrame.")
    if len(raw_buffer) > int(manifest["live_buffer_bars"]):
        raise FFDManifestVerificationError("State bundle raw_buffer_df exceeds live_buffer_bars.")


def _rel(path: Path) -> str:
    return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()


def _normalize_bar_table(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    required = ["Datetime", *OHLC_COLUMNS]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise FFDManifestVerificationError(f"{source} missing required OHLC/Datetime columns: {missing}")
    out = df.copy()
    out["Datetime"] = _parse_utc_datetime_series(out["Datetime"])
    for col in OHLC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required).copy()
    out = out[(out[OHLC_COLUMNS] > 0).all(axis=1)].copy()
    return out.sort_values("Datetime").drop_duplicates(subset=["Datetime"], keep="last").reset_index(drop=True)


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


def _maybe_build_live_continued_ffd_features(
    *,
    asset: str,
    manifest: Mapping[str, Any],
    static_features_rel: str,
    bundle_rel: str,
) -> tuple[str, bool, str | None, dict[str, Any]]:
    """Append frozen-d FFD rows using only the research live buffer.

    The verified static research CSV remains immutable. The live checkpoint holds
    the exact raw OHLC ring required by the fixed-width FFD window; new bars are
    transformed once and appended. Missing/corrupt checkpoints are rebuilt from
    the verified research state bundle, never by re-estimating ``d``.
    """
    proof = {
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "frozen_d_preserved": True,
        "ffd_window": int(manifest["ffd_window"]),
        "live_buffer_bars": int(manifest["live_buffer_bars"]),
        "continuation_algorithm": "fixed_width_frozen_d_raw_ring_append",
        "rows_appended": 0,
    }
    live_bars_path = LIVE_BARS_DIR / f"{asset}_live_standard_quote_tick_bars.csv"
    if not live_bars_path.exists():
        return static_features_rel, False, None, proof

    static_path = _V2_ROOT / static_features_rel
    bundle_path = _V2_ROOT / bundle_rel
    static_latest = _latest_datetime(static_path)
    if static_latest is None:
        return static_features_rel, False, "static_ffd_latest_missing", proof
    try:
        live_bars = _normalize_bar_table(pd.read_csv(live_bars_path), source="portfolio_live_bars")
    except Exception as exc:
        return static_features_rel, False, f"live_bar_load_failed:{exc}", proof
    if live_bars.empty or pd.Timestamp(live_bars["Datetime"].max()) <= static_latest:
        return static_features_rel, False, None, proof

    LIVE_CONTINUATION_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LIVE_CONTINUATION_DIR / f"{asset.lower()}_ffd_features_live_continued.csv"
    state_path = LIVE_CONTINUATION_DIR / f"{asset.lower()}_ffd_streaming_state.joblib"
    required = ["Datetime", *OHLC_COLUMNS, *(f"{col}_FFD" for col in OHLC_COLUMNS)]

    try:
        with _exclusive_output_lock(out_path):
            research_bundle = joblib.load(bundle_path)
            if not isinstance(research_bundle, dict):
                raise FFDManifestVerificationError("FFD research state bundle must be a dictionary")
            checkpoint: dict[str, Any] | None = None
            if state_path.exists():
                try:
                    loaded = joblib.load(state_path)
                    if (
                        isinstance(loaded, dict)
                        and loaded.get("schema_version") == LIVE_FFD_STATE_SCHEMA_VERSION
                        and float(loaded.get("frozen_d")) == float(manifest["frozen_d"])
                        and int(loaded.get("ffd_window")) == int(manifest["ffd_window"])
                        and int(loaded.get("live_buffer_bars")) == int(manifest["live_buffer_bars"])
                        and isinstance(loaded.get("raw_buffer_df"), pd.DataFrame)
                    ):
                        checkpoint = loaded
                except Exception:
                    checkpoint = None

            if checkpoint is None:
                raw_buffer = research_bundle.get("raw_buffer_df")
                if not isinstance(raw_buffer, pd.DataFrame):
                    raise FFDManifestVerificationError("Research FFD bundle is missing raw_buffer_df")
                raw_buffer = _normalize_bar_table(raw_buffer, source="research_ffd_raw_buffer")
                checkpoint = {
                    "schema_version": LIVE_FFD_STATE_SCHEMA_VERSION,
                    "asset": asset,
                    "frozen_d": float(manifest["frozen_d"]),
                    "ffd_window": int(manifest["ffd_window"]),
                    "live_buffer_bars": int(manifest["live_buffer_bars"]),
                    "last_observation_timestamp": static_latest.isoformat(),
                    "raw_buffer_df": raw_buffer.tail(int(manifest["live_buffer_bars"])).reset_index(drop=True),
                    "rows_appended": 0,
                }
            else:
                raw_buffer = _normalize_bar_table(checkpoint["raw_buffer_df"], source="live_ffd_raw_buffer")

            last_observation = pd.Timestamp(checkpoint["last_observation_timestamp"])
            if last_observation.tzinfo is None:
                last_observation = last_observation.tz_localize("UTC")
            else:
                last_observation = last_observation.tz_convert("UTC")
            if last_observation < static_latest:
                raise FFDManifestVerificationError("FFD checkpoint precedes immutable research horizon")

            pending = live_bars[live_bars["Datetime"] > last_observation].copy()
            pending = pending.sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)

            if out_path.exists() and _validate_static_ffd_prefix(static_path, out_path):
                existing = pd.read_csv(out_path, usecols=required)
                existing["Datetime"] = _parse_utc_datetime_series(existing["Datetime"])
                existing = existing.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")
            else:
                existing = pd.read_csv(static_path, usecols=required)
                existing["Datetime"] = _parse_utc_datetime_series(existing["Datetime"])
                existing = existing.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")

            existing_latest = pd.Timestamp(existing["Datetime"].max()) if not existing.empty else static_latest
            live_latest = pd.Timestamp(live_bars["Datetime"].max())
            # A continuation CSV and its raw-ring checkpoint are one atomic logical
            # state. Any mismatch or rollback triggers a clean replay from the
            # verified research bundle rather than mixing incompatible histories.
            if existing_latest != last_observation or last_observation > live_latest:
                raw_buffer = _normalize_bar_table(
                    research_bundle["raw_buffer_df"],
                    source="research_ffd_raw_buffer",
                )
                checkpoint = {
                    "schema_version": LIVE_FFD_STATE_SCHEMA_VERSION,
                    "asset": asset,
                    "frozen_d": float(manifest["frozen_d"]),
                    "ffd_window": int(manifest["ffd_window"]),
                    "live_buffer_bars": int(manifest["live_buffer_bars"]),
                    "last_observation_timestamp": static_latest.isoformat(),
                    "raw_buffer_df": raw_buffer.tail(int(manifest["live_buffer_bars"])).reset_index(drop=True),
                    "rows_appended": 0,
                }
                last_observation = static_latest
                pending = live_bars[live_bars["Datetime"] > static_latest].copy()
                pending = pending.sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)
                existing = pd.read_csv(static_path, usecols=required)
                existing["Datetime"] = _parse_utc_datetime_series(existing["Datetime"])
                existing = existing.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")

            new_rows = pd.DataFrame(columns=required)
            if not pending.empty:
                transform_input = pd.concat([raw_buffer, pending], ignore_index=True)
                transform_input = _normalize_bar_table(transform_input, source="ffd_streaming_input")
                transformed = apply_frozen_ffd_to_ohlc(
                    transform_input,
                    d=float(manifest["frozen_d"]),
                    window=int(manifest["ffd_window"]),
                )
                transformed["Datetime"] = _parse_utc_datetime_series(transformed["Datetime"])
                transformed = transformed.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last")
                new_rows = transformed[transformed["Datetime"] > last_observation][required].copy()
                expected_times = set(pending["Datetime"])
                produced_times = set(new_rows["Datetime"])
                missing_times = sorted(expected_times.difference(produced_times))
                if missing_times:
                    raise FFDManifestVerificationError(
                        f"FFD streaming continuation did not produce all pending bars; missing={missing_times[:5]}"
                    )

                out = pd.concat([existing, new_rows], ignore_index=True)
                out = out.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="first").reset_index(drop=True)
                _atomic_write_csv(out[required], out_path, index=False)

                next_raw_buffer = pd.concat([raw_buffer, pending], ignore_index=True)
                next_raw_buffer = _normalize_bar_table(next_raw_buffer, source="ffd_next_raw_buffer")
                checkpoint = {
                    "schema_version": LIVE_FFD_STATE_SCHEMA_VERSION,
                    "asset": asset,
                    "frozen_d": float(manifest["frozen_d"]),
                    "ffd_window": int(manifest["ffd_window"]),
                    "live_buffer_bars": int(manifest["live_buffer_bars"]),
                    "last_observation_timestamp": pd.Timestamp(pending["Datetime"].max()).isoformat(),
                    "raw_buffer_df": next_raw_buffer.tail(int(manifest["live_buffer_bars"])).reset_index(drop=True),
                    "rows_appended": int(checkpoint.get("rows_appended", 0) if checkpoint else 0) + int(len(new_rows)),
                }
                _atomic_joblib_dump(checkpoint, state_path)
                reloaded = joblib.load(state_path)
                if reloaded.get("last_observation_timestamp") != checkpoint["last_observation_timestamp"]:
                    raise FFDManifestVerificationError("FFD checkpoint restart roundtrip failed")
                proof["rows_appended"] = int(len(new_rows))
            elif not out_path.exists():
                _atomic_write_csv(existing[required], out_path, index=False)
                _atomic_joblib_dump(checkpoint, state_path)

            if not _validate_static_ffd_prefix(static_path, out_path):
                raise FFDManifestVerificationError("Written FFD continuation changed static research history")

        proof.update({
            "state_path": _rel(state_path),
            "last_observation_timestamp": checkpoint.get("last_observation_timestamp") if checkpoint else static_latest.isoformat(),
        })
        return _rel(out_path), True, None, proof
    except Exception as exc:
        proof.update({
            "streaming_parity_passed": False,
            "append_only_history": False,
            "restart_parity_passed": False,
        })
        return static_features_rel, False, f"live_ffd_build_failed:{exc}", proof


def verify_asset_ffd_manifest(asset: str) -> dict[str, Any]:
    layout = get_asset_layout(asset)
    manifest_path = layout.bars_root / "ffd" / "ffd_manifest.json"
    manifest = _load_manifest(manifest_path)

    _require_equal(manifest, "schema_version", V2_CONTRACT_VERSION)
    _require_equal(manifest, "asset", layout.asset)
    _require_equal(manifest, "runtime_module", "shared.ffd.fractional_differentiation")
    _require_equal(manifest, "d_estimation_policy", "initial_training_segment_only_then_freeze")
    _require_false(manifest, "uses_future_data")
    _require_false(manifest, "d_reestimated_live")

    if manifest.get("memory_retention_passed") is not True:
        raise FFDManifestVerificationError("FFD memory retention did not pass.")
    if float(manifest.get("frozen_d")) < 0 or float(manifest.get("frozen_d")) > 1.5:
        raise FFDManifestVerificationError(f"frozen_d looks abnormal: {manifest.get('frozen_d')}")

    mismatches = verify_artifact_hashes(_hash_table_from_manifest(manifest), root=_V2_ROOT)
    if mismatches:
        raise FFDManifestVerificationError("Artifact hash verification failed:\n" + "\n".join(mismatches))

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise FFDManifestVerificationError("Manifest outputs must be an object.")
    features_rel = outputs.get("ffd_features")
    bundle_rel = outputs.get("state_bundle")
    if not isinstance(features_rel, str) or not isinstance(bundle_rel, str):
        raise FFDManifestVerificationError("Manifest outputs must include ffd_features and state_bundle paths.")

    _validate_ffd_features(_V2_ROOT / features_rel, int(manifest["rows_output"]))
    _validate_state_bundle(_V2_ROOT / bundle_rel, manifest)

    runtime_features_rel, live_continued, live_continuation_warning, continuation_proof = _maybe_build_live_continued_ffd_features(
        asset=layout.asset,
        manifest=manifest,
        static_features_rel=features_rel,
        bundle_rel=bundle_rel,
    )

    return {
        "asset": layout.asset,
        "rows_input": manifest.get("rows_input"),
        "rows_output": manifest.get("rows_output"),
        "frozen_d": manifest.get("frozen_d"),
        "ffd_window": manifest.get("ffd_window"),
        "live_buffer_bars": manifest.get("live_buffer_bars"),
        "min_memory_retention_corr": manifest.get("min_memory_retention_corr"),
        "features": runtime_features_rel,
        "static_features": features_rel,
        "state_bundle": bundle_rel,
        "manifest": manifest_path.as_posix(),
        "artifact_hashes_passed": True,
        "ffd_contract_passed": True,
        "live_continuation_applied": bool(live_continued),
        "live_continuation_warning": live_continuation_warning,
        "streaming_parity_passed": bool(continuation_proof.get("streaming_parity_passed")),
        "append_only_history": bool(continuation_proof.get("append_only_history")),
        "restart_parity_passed": bool(continuation_proof.get("restart_parity_passed")),
        "frozen_d_preserved": bool(continuation_proof.get("frozen_d_preserved")),
        "continuation_proof": continuation_proof,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify V2 asset-level FFD manifest and artifacts.")
    parser.add_argument("--asset", required=True)
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = verify_asset_ffd_manifest(args.asset)
    print("✅ V2 asset FFD manifest verification passed.")
    print(f"Asset: {result['asset']}")
    print(f"Rows input: {result['rows_input']}")
    print(f"Rows output: {result['rows_output']}")
    print(f"Frozen d: {result['frozen_d']}")
    print(f"FFD window: {result['ffd_window']}")
    print(f"Live buffer bars: {result['live_buffer_bars']}")
    print(f"Min memory retention corr: {result['min_memory_retention_corr']}")
    print(f"Features: {result['features']}")
    print(f"Static features: {result['static_features']}")
    print(f"Live continuation applied: {result['live_continuation_applied']}")
    if result.get("live_continuation_warning"):
        print(f"Live continuation warning: {result['live_continuation_warning']}")
    print(f"State bundle: {result['state_bundle']}")
    print("Artifact hashes: PASS")
    print("FFD contract: PASS")


if __name__ == "__main__":
    try:
        _run()
    except FFDManifestVerificationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
