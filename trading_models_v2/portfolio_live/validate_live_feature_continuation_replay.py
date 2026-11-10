from __future__ import annotations

"""Validate post-research FFD/HMM coverage, immutable prefixes, and restart parity."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
OUTPUT_DIR = LIVE_DIR / "outputs" / "live_feature_continuation"
REPORT_JSON = OUTPUT_DIR / "live_feature_continuation_replay_report.json"
for path in (LIVE_DIR, V2_ROOT, THIS_FILE.parents[2]):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from live_feature_continuation_guard import (  # noqa: E402
    dependency_map_from_manifest,
    ensure_assets_live_feature_continuation,
)
from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.regime.verify_asset_hmm_regime_manifest import verify_asset_hmm_regime_manifest  # noqa: E402
from shared.regime.hmm_regime import HMMRegimeConfig, engineer_hmm_inputs  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_manifest() -> dict[str, Any]:
    path = LIVE_DIR / "live_manifest.json"
    if not path.exists():
        path = LIVE_DIR / "live_deployment_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _parse(series: pd.Series) -> pd.Series:
    """Parse heterogeneous timestamps with scalar recovery.

    ROBUST_MIXED_UTC_SCALAR_RECOVERY_V1
    """
    raw = series.copy() if isinstance(series, pd.Series) else pd.Series(series)
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


def _read(path: Path, columns: list[str]) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=columns)
    frame["Datetime"] = _parse(frame["Datetime"])
    return frame.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def _prefix_equal(static: pd.DataFrame, runtime: pd.DataFrame, columns: list[str]) -> tuple[bool, str | None]:
    if static.empty:
        return False, "static_empty"
    historical = runtime.loc[runtime["Datetime"] <= static["Datetime"].max(), columns].reset_index(drop=True)
    static = static[columns].reset_index(drop=True)
    if len(static) != len(historical) or not static["Datetime"].equals(historical["Datetime"]):
        return False, "datetime_or_row_count_mismatch"
    for column in columns:
        if column == "Datetime":
            continue
        left = pd.to_numeric(static[column], errors="coerce")
        right = pd.to_numeric(historical[column], errors="coerce")
        if not np.allclose(left.to_numpy(float), right.to_numpy(float), rtol=1e-12, atol=1e-12, equal_nan=True):
            return False, f"value_mismatch:{column}"
    return True, None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate live continuation beyond the research horizon.")
    parser.add_argument("--assets", nargs="*", help="Optional assets. Default: all manifest assets.")
    args = parser.parse_args()

    manifest = _load_manifest()
    default_assets = sorted({str(row.get("asset") or "").upper() for row in manifest.get("selected_edges", []) or [] if row.get("asset")})
    assets = [str(asset).upper() for asset in (args.assets or default_assets)]
    health = ensure_assets_live_feature_continuation(
        assets=assets,
        manifest=manifest,
        auto_repair=False,
        fail_closed=False,
        use_cache=False,
    )
    dependencies = dependency_map_from_manifest(manifest, assets)
    records: dict[str, Any] = {}

    for asset in assets:
        dep = dependencies[asset]
        findings: list[dict[str, Any]] = []
        try:
            ffd_first = verify_asset_ffd_manifest(asset) if (dep.requires_ffd or dep.requires_hmm) else None
            hmm_first = verify_asset_hmm_regime_manifest(asset) if dep.requires_hmm else None
            ffd_second = verify_asset_ffd_manifest(asset) if ffd_first else None
            hmm_second = verify_asset_hmm_regime_manifest(asset) if hmm_first else None

            if ffd_first:
                columns = ["Datetime", "Open", "High", "Low", "Close", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
                static = _read(V2_ROOT / ffd_first["static_features"], columns)
                runtime = _read(V2_ROOT / ffd_first["features"], columns)
                prefix_pass, reason = _prefix_equal(static, runtime, columns)
                findings.append({"component": "FFD", "check": "immutable_static_prefix", "passed": prefix_pass, "reason": reason})
                live_path = LIVE_DIR / "live_bars" / f"{asset}_live_standard_quote_tick_bars.csv"
                live = _read(live_path, ["Datetime"]) if live_path.exists() else pd.DataFrame(columns=["Datetime"])
                expected = set(live.loc[live["Datetime"] > static["Datetime"].max(), "Datetime"])
                produced = set(runtime.loc[runtime["Datetime"] > static["Datetime"].max(), "Datetime"])
                missing = sorted(expected.difference(produced))
                findings.append({"component": "FFD", "check": "post_research_timestamp_coverage", "passed": not missing, "missing_count": len(missing), "missing_sample": [str(x) for x in missing[:5]]})
                findings.append({
                    "component": "FFD",
                    "check": "restart_idempotence",
                    "passed": ffd_first.get("features") == ffd_second.get("features") and ffd_first.get("continuation_proof", {}).get("last_observation_timestamp") == ffd_second.get("continuation_proof", {}).get("last_observation_timestamp"),
                })

            if hmm_first:
                n_states = int(hmm_first.get("n_states") or 4)
                columns = ["Datetime", "Regime_State", "Regime_Entropy", "Regime_Duration", *[f"Prob_State_{i}" for i in range(n_states)]]
                static = _read(V2_ROOT / hmm_first["static_features"], columns)
                runtime = _read(V2_ROOT / hmm_first["features"], columns)
                prefix_pass, reason = _prefix_equal(static, runtime, columns)
                findings.append({"component": "HMM", "check": "immutable_static_prefix", "passed": prefix_pass, "reason": reason})
                # HMM_ELIGIBLE_TIMESTAMP_COVERAGE_V1
                # Research HMM preprocessing intentionally excludes rows with
                # undefined HMM inputs (notably zero-range bars). Replay coverage
                # must compare against the processed eligible timestamps.
                if ffd_first:
                    ffd_source = pd.read_csv(V2_ROOT / ffd_first["features"])
                    ffd_source["Datetime"] = _parse(ffd_source["Datetime"])
                    raw_post_research = set(
                        ffd_source.loc[
                            ffd_source["Datetime"] > static["Datetime"].max(),
                            "Datetime",
                        ].dropna()
                    )
                    processed = engineer_hmm_inputs(
                        ffd_source,
                        config=HMMRegimeConfig(
                            n_states=n_states,
                            train_window=int(hmm_first.get("train_window") or 5000),
                            refit_frequency=int(hmm_first.get("refit_frequency") or 1000),
                            inference_lookback=int(hmm_first.get("inference_lookback") or 1000),
                            min_covar=float(hmm_first.get("min_covar") or 0.01),
                            random_state=int(hmm_first.get("random_state") or 42),
                            max_iter=int(hmm_first.get("max_iter") or 200),
                            noise_epsilon=float(hmm_first.get("noise_epsilon") or 1e-8),
                        ),
                    )
                    expected = set(
                        processed.loc[
                            processed["Datetime"] > static["Datetime"].max(),
                            "Datetime",
                        ]
                    )
                    intentionally_excluded = sorted(
                        raw_post_research.difference(expected)
                    )
                else:
                    expected = set()
                    intentionally_excluded = []
                produced = set(runtime.loc[runtime["Datetime"] > static["Datetime"].max(), "Datetime"])
                missing = sorted(expected.difference(produced))
                findings.append({
                    "component": "HMM",
                    "check": "research_preprocessing_exclusions",
                    "passed": True,
                    "excluded_count": len(intentionally_excluded),
                    "excluded_sample": [str(x) for x in intentionally_excluded[:5]],
                    "reason": "rows intentionally excluded by immutable HMM input preprocessing",
                })
                findings.append({"component": "HMM", "check": "post_research_eligible_timestamp_coverage", "passed": not missing, "missing_count": len(missing), "missing_sample": [str(x) for x in missing[:5]]})
                findings.append({
                    "component": "HMM",
                    "check": "restart_idempotence",
                    "passed": hmm_first.get("features") == hmm_second.get("features") and hmm_first.get("continuation_proof", {}).get("rows_appended") == 0 and hmm_second.get("continuation_proof", {}).get("rows_appended") == 0,
                })
        except Exception as exc:
            findings.append({"component": "SYSTEM", "check": "exception_free", "passed": False, "error": f"{type(exc).__name__}: {exc}"})

        passed = all(bool(item.get("passed")) for item in findings) and health["assets"].get(asset, {}).get("status") in {"PASS", "REPAIRED_PASS", "NOT_REQUIRED"}
        records[asset] = {"status": "PASS" if passed else "FAIL", "findings": findings}

    failed = [asset for asset, rec in records.items() if rec["status"] != "PASS"]
    payload = {
        "schema_version": "live_feature_continuation_replay_v1",
        "created_at": _now(),
        "status": "PASS" if not failed else "FAIL",
        "failed_assets": failed,
        "assets": records,
    }
    _atomic_json(REPORT_JSON, payload)
    print("=" * 88)
    print("LIVE FEATURE CONTINUATION REPLAY VALIDATION")
    print("=" * 88)
    print(f"Status: {payload['status']}")
    print(f"Failed assets: {failed}")
    print("Report: portfolio_live/outputs/live_feature_continuation/live_feature_continuation_replay_report.json")
    for asset, record in records.items():
        print(f"{asset:<8} {record['status']}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
