"""Offline Phase C.3 consumer-contract certification.

This script does NOT route the C.2 engine into production.  It proves that each
selected edge can consume only its frozen canonical projection and still produce
the same candidate events and live-safe model/scoring rows as the unchanged
authoritative edge feature frame.

Heavy work is reduced by C.1 structural feature groups: one authoritative
6,000-row feature build is performed per structural group, while every selected
edge's own candidate and live-row consumer is still exercised.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_V2_ROOT = HERE.parent
if str(DEFAULT_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_V2_ROOT))

from shared.edges.phase_c_consumer_contract import (
    PRIVATE_CANDIDATE_CONTROL_COLUMNS,
    RAW_CONSUMER_COLUMNS,
    compile_consumer_contract,
    validate_frozen_consumer_contract,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.phase_c_consumer_adapter import (
    build_canonical_live_candidate_rows,
    latest_candidate_control_values,
)
from shared.edges.phase_c_incremental_feature_engine import (
    FFD_COLUMNS,
    REGIME_COLUMNS,
    bootstrap_compact_state,
    deserialize_compact_state,
    serialize_compact_state,
    update_one,
)
from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry

DEFAULT_CONTRACT = HERE / "config" / "phase_c" / "consumer_contract.json"
DEFAULT_C1 = HERE / "config" / "phase_c" / "feature_contract.json"
DEFAULT_C2 = HERE / "config" / "phase_c" / "incremental_feature_certification.json"
DEFAULT_MANIFEST = HERE / "live_manifest.json"
DEFAULT_OUTPUT = HERE / "config" / "phase_c" / "consumer_certification.json"

FORBIDDEN_LIVE_COLUMNS = {
    "meta_label",
    "label",
    "target",
    "t1",
    "barrier_hit",
    "resolution_source",
    "entry_price",
    "pt_price",
    "sl_price",
    "realized_r",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "approved_signal",
}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise RuntimeError(f"Expected DataFrame, got {type(frame).__name__}")
    out = frame.copy()
    if "Datetime" in out.columns:
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
        out = out.dropna(subset=["Datetime"]).sort_values(
            "Datetime", kind="mergesort"
        )
        out = out.drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    return out


def _load_bars(runtime: Any, *, v2_root: Path) -> pd.DataFrame:
    loader = getattr(runtime, "_load_asset_bars", None)
    if callable(loader):
        bars = loader()
    else:
        validator = getattr(runtime, "validate_environment", None)
        if not callable(validator):
            raise RuntimeError("runtime has no _load_asset_bars or validate_environment")
        env = validator()
        rel = ((env or {}).get("bar_verification") or {}).get("bars")
        if not rel:
            raise RuntimeError("runtime environment has no verified bar path")
        bars = pd.read_csv(v2_root / str(rel))
    required = [
        "Datetime",
        "Bar_Start",
        "Open",
        "High",
        "Low",
        "Close",
        "Ticks",
        "Threshold",
    ]
    missing = [name for name in required if name not in bars.columns]
    if missing:
        raise RuntimeError(f"verified bars missing columns: {missing}")
    out = bars.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
    for name in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out = out.dropna(
        subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    )
    return out.sort_values("Datetime", kind="mergesort").drop_duplicates(
        "Datetime", keep="last"
    ).reset_index(drop=True)


@contextmanager
def _isolated_dependency_authority(v2_root: Path, assets: list[str]):
    """Use disposable FFD/HMM verifier continuation state during C.3."""
    import importlib

    source = v2_root / "portfolio_live" / "outputs" / "live_feature_continuation"
    parent = v2_root / "portfolio_live" / "outputs" / "phase_c3_dependency_sandbox"
    sandbox = parent / f"pid_{os.getpid()}_{time.time_ns()}"
    sandbox.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in source.iterdir()
        if source.exists() and path.is_file()
    } if source.exists() else {}
    try:
        if source.exists():
            prefixes = tuple(f"{str(asset).lower()}_" for asset in assets)
            for path in sorted(source.iterdir()):
                if not path.is_file() or not path.name.lower().startswith(prefixes):
                    continue
                if path.name.lower().endswith(".lock") or path.name.startswith("."):
                    continue
                target = sandbox / path.name
                shutil.copyfile(path, target)
                try:
                    os.chmod(target, 0o666)
                except OSError:
                    pass
                copied.append(path.name)

        ffd_module = importlib.import_module("shared.ffd.verify_asset_ffd_manifest")
        hmm_module = importlib.import_module("shared.regime.verify_asset_hmm_regime_manifest")
        old_ffd = getattr(ffd_module, "LIVE_CONTINUATION_DIR")
        old_hmm = getattr(hmm_module, "LIVE_CONTINUATION_DIR")
        ffd_module.LIVE_CONTINUATION_DIR = sandbox
        hmm_module.LIVE_CONTINUATION_DIR = sandbox
        yield {
            "sandbox": sandbox,
            "source": source,
            "seeded_file_count": len(copied),
        }
    finally:
        try:
            if "ffd_module" in locals():
                ffd_module.LIVE_CONTINUATION_DIR = old_ffd
            if "hmm_module" in locals():
                hmm_module.LIVE_CONTINUATION_DIR = old_hmm
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            try:
                parent.rmdir()
            except OSError:
                pass
        after = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in source.iterdir()
            if source.exists() and path.is_file()
        } if source.exists() else {}
        if before != after:
            raise RuntimeError(
                "Phase C.3 certification mutated production live_feature_continuation"
            )


def _frame_digest(frame: pd.DataFrame) -> str:
    work = frame.copy()
    payload = hashlib.sha256()
    payload.update("|".join(str(x) for x in work.columns).encode("utf-8"))
    payload.update("|".join(str(x) for x in work.dtypes).encode("utf-8"))
    try:
        hashed = pd.util.hash_pandas_object(work, index=True).to_numpy(dtype="uint64")
        payload.update(hashed.tobytes())
    except Exception:
        payload.update(work.to_csv(index=True).encode("utf-8"))
    return payload.hexdigest()


def _value_equal(left: Any, right: Any, *, atol: float, rtol: float) -> tuple[bool, float, float]:
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right)), 0.0, 0.0
    if isinstance(left, (bool, np.bool_)) or isinstance(right, (bool, np.bool_)):
        return bool(left) == bool(right), 0.0, 0.0
    try:
        lv = float(left)
        rv = float(right)
        if np.isfinite(lv) and np.isfinite(rv):
            abs_diff = abs(lv - rv)
            scale = max(abs(lv), abs(rv), 1.0)
            return math.isclose(lv, rv, abs_tol=atol, rel_tol=rtol), abs_diff, abs_diff / scale
        return lv == rv, 0.0, 0.0
    except Exception:
        return str(left) == str(right), 0.0, 0.0


def _compare_frames(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    atol: float,
    rtol: float,
    required_columns: list[str] | None = None,
) -> dict[str, Any]:
    l = _normalize(left)
    r = _normalize(right)
    if len(l) != len(r):
        return {
            "ok": False,
            "reason": "row_count",
            "left_rows": len(l),
            "right_rows": len(r),
        }

    if "Datetime" in l.columns and "Datetime" in r.columns:
        if not l["Datetime"].equals(r["Datetime"]):
            return {"ok": False, "reason": "datetime_alignment"}

    if required_columns is None:
        columns = sorted(set(l.columns).intersection(r.columns))
        missing_left = sorted(set(r.columns).difference(l.columns))
        missing_right = sorted(set(l.columns).difference(r.columns))
        if missing_left or missing_right:
            return {
                "ok": False,
                "reason": "column_set",
                "missing_left": missing_left[:20],
                "missing_right": missing_right[:20],
            }
    else:
        columns = list(dict.fromkeys(required_columns))
        missing_left = [name for name in columns if name not in l.columns]
        missing_right = [name for name in columns if name not in r.columns]
        if missing_left or missing_right:
            return {
                "ok": False,
                "reason": "required_columns",
                "missing_left": missing_left,
                "missing_right": missing_right,
            }

    mismatches: list[dict[str, Any]] = []
    worst_abs = 0.0
    worst_rel = 0.0
    worst_column = None
    for name in columns:
        if name == "Datetime":
            continue
        for i, (lv, rv) in enumerate(zip(l[name], r[name])):
            ok, abs_diff, rel_diff = _value_equal(lv, rv, atol=atol, rtol=rtol)
            if abs_diff > worst_abs:
                worst_abs = abs_diff
                worst_rel = rel_diff
                worst_column = name
            if not ok and len(mismatches) < 20:
                mismatches.append(
                    {
                        "row": i,
                        "column": name,
                        "left": None if pd.isna(lv) else str(lv),
                        "right": None if pd.isna(rv) else str(rv),
                        "abs_diff": abs_diff,
                        "rel_diff": rel_diff,
                    }
                )
    return {
        "ok": not mismatches,
        "rows_checked": len(l),
        "column_count": len(columns),
        "mismatch_count_capped": len(mismatches),
        "mismatches": mismatches,
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_column": worst_column,
    }


def _call_candidate(runtime: Any, features: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    before = _frame_digest(features)
    started = time.perf_counter()
    events = runtime.generate_candidate_events(features)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    after = _frame_digest(features)
    if before != after:
        raise RuntimeError(f"{getattr(runtime, 'EDGE_ID', '<edge>')}: candidate generator mutated input frame")
    if not isinstance(events, pd.DataFrame) or events.empty:
        raise RuntimeError(f"{getattr(runtime, 'EDGE_ID', '<edge>')}: candidate generator returned no events")
    forbidden = sorted(FORBIDDEN_LIVE_COLUMNS.intersection(events.columns))
    if forbidden:
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: candidate events contain forbidden columns {forbidden}"
        )
    return _normalize(events), {"elapsed_ms": elapsed_ms, "rows": int(len(events))}


def _call_live_rows(
    runtime: Any,
    *,
    features: pd.DataFrame,
    events: pd.DataFrame,
    selected_features: list[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    started = time.perf_counter()
    rows, ownership_manifest = build_canonical_live_candidate_rows(
        runtime=runtime,
        features=features,
        events=events,
        selected_features=selected_features,
    )

    # Exercise the unchanged runtime scoring-row acceptance path using already
    # canonicalized model rows. This proves the adapter output is a valid live
    # scoring input without allowing events to re-own selected features.
    scoring_builder = getattr(runtime, "_build_scoring_rows", None)
    if not callable(scoring_builder):
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: missing _build_scoring_rows"
        )
    validated_rows, runtime_manifest = scoring_builder(model_rows=rows)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not isinstance(validated_rows, pd.DataFrame) or validated_rows.empty:
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: live row acceptance returned no rows"
        )
    forbidden = sorted(FORBIDDEN_LIVE_COLUMNS.intersection(validated_rows.columns))
    if forbidden:
        raise RuntimeError(
            f"{getattr(runtime, 'EDGE_ID', '<edge>')}: live rows contain forbidden columns {forbidden}"
        )
    return _normalize(validated_rows), {
        "elapsed_ms": elapsed_ms,
        "rows": int(len(validated_rows)),
        "row_builder": (runtime_manifest or {}).get("row_builder"),
        "ownership_adapter": ownership_manifest.get("adapter"),
        "selected_feature_collision_count": int(
            ownership_manifest.get("selected_feature_collision_count") or 0
        ),
        "selected_feature_collision_columns": list(
            ownership_manifest.get("selected_feature_collision_columns") or []
        ),
        "selected_feature_source_policy": ownership_manifest.get(
            "selected_feature_source_policy"
        ),
        "collision_policy": ownership_manifest.get("collision_policy"),
        "edge_row_builder": ownership_manifest.get("edge_row_builder"),
    }


def _poison_selected_feature_sources(
    runtime: Any,
    *,
    features: pd.DataFrame,
    events: pd.DataFrame,
    selected_features: list[str],
) -> dict[str, Any]:
    sample_events = _normalize(events).tail(min(8, len(events))).copy()
    event_times = set(sample_events["Datetime"])
    feature_sample = _normalize(features)
    feature_sample = feature_sample[feature_sample["Datetime"].isin(event_times)].copy()
    if len(feature_sample) != len(sample_events):
        return {
            "ok": False,
            "reason": "event_feature_alignment",
            "event_rows": len(sample_events),
            "feature_rows": len(feature_sample),
        }

    expected: dict[str, dict[pd.Timestamp, float]] = {}
    feature_poison: dict[str, list[float]] = {}
    event_poison: dict[str, list[float]] = {}
    for j, name in enumerate(selected_features):
        expected[name] = {}
        feature_values: list[float] = []
        event_values: list[float] = []
        for i, ts in enumerate(feature_sample["Datetime"]):
            canonical_value = 1000000.0 + (j + 1) * 1000.0 + (i + 1) / 100.0
            event_value = -9000000.0 - (j + 1) * 1000.0 - (i + 1) / 100.0
            feature_values.append(canonical_value)
            event_values.append(event_value)
            expected[name][pd.Timestamp(ts)] = canonical_value
        feature_poison[name] = feature_values
        event_poison[name] = event_values

    # Assign as blocks to avoid highly-fragmented DataFrames during the poison proof.
    feature_sample = feature_sample.drop(
        columns=[name for name in selected_features if name in feature_sample.columns],
        errors="ignore",
    )
    sample_events = sample_events.drop(
        columns=[name for name in selected_features if name in sample_events.columns],
        errors="ignore",
    )
    feature_sample = pd.concat(
        [
            feature_sample.reset_index(drop=True),
            pd.DataFrame(feature_poison),
        ],
        axis=1,
    )
    sample_events = pd.concat(
        [
            sample_events.reset_index(drop=True),
            pd.DataFrame(event_poison),
        ],
        axis=1,
    )

    rows, ownership_manifest = build_canonical_live_candidate_rows(
        runtime=runtime,
        features=feature_sample,
        events=sample_events,
        selected_features=selected_features,
    )
    rows = _normalize(rows)
    mismatches: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        ts = pd.Timestamp(row["Datetime"])
        for name in selected_features:
            actual = float(row[name])
            wanted = float(expected[name][ts])
            if actual != wanted and len(mismatches) < 20:
                mismatches.append(
                    {
                        "timestamp": ts.isoformat(),
                        "feature": name,
                        "actual": actual,
                        "expected_feature_table_sentinel": wanted,
                    }
                )
    return {
        "ok": not mismatches,
        "sample_event_rows": len(sample_events),
        "selected_feature_count": len(selected_features),
        "selected_feature_collision_count": int(
            ownership_manifest.get("selected_feature_collision_count") or 0
        ),
        "selected_feature_collision_columns": list(
            ownership_manifest.get("selected_feature_collision_columns") or []
        ),
        "source_policy": ownership_manifest.get("selected_feature_source_policy"),
        "mismatches": mismatches,
    }


def _manifest_map(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row["edge_id"]): dict(row)
        for row in manifest.get("selected_edges") or []
        if isinstance(row, Mapping)
    }


def _c1_edge_map(c1: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row["edge_id"]): dict(row)
        for row in c1.get("edges") or []
        if isinstance(row, Mapping)
    }



def _generation_from_authority(
    *,
    asset: str,
    row: pd.Series,
    previous_hmm_target: pd.Timestamp | None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float] | None, pd.Timestamp]:
    ts = pd.Timestamp(row["Datetime"])
    ffd = {name: float(row[name]) for name in FFD_COLUMNS}
    hmm_raw = {name: float(row[name]) for name in REGIME_COLUMNS}
    eligible = all(np.isfinite(list(hmm_raw.values())))
    if eligible:
        hmm_target = ts
        hmm = hmm_raw
    else:
        if previous_hmm_target is None:
            raise RuntimeError(
                f"{asset}: private-control certification hit an HMM-ineligible "
                "bar before any eligible HMM target"
            )
        hmm_target = previous_hmm_target
        hmm = None
    generation_id = hashlib.sha256(
        (
            f"phase_c3_private_controls|{asset}|{ts.isoformat()}|"
            f"{hmm_target.isoformat()}|{eligible}"
        ).encode("utf-8")
    ).hexdigest()
    return (
        {
            "status": "READY",
            "asset": asset,
            "generation_id": generation_id,
            "raw_target": ts.isoformat(),
            "hmm_target": hmm_target.isoformat(),
            "target_hmm_eligible": eligible,
        },
        ffd,
        hmm,
        hmm_target,
    )


def _control_expected(row: Mapping[str, Any], control: str) -> float:
    if control in row:
        value = float(row[control])
        if np.isfinite(value):
            return value
    if control == "Bar_Volatility_Bps" and "Range_Bps" in row:
        value = float(row["Range_Bps"])
        if np.isfinite(value):
            return value
    raise RuntimeError(f"authoritative row cannot provide private control {control}")


def _compare_control_mapping(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
    controls: list[str],
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    worst_abs = 0.0
    worst_rel = 0.0
    worst_control = None
    for name in controls:
        ok, abs_diff, rel_diff = _value_equal(
            observed.get(name), expected.get(name), atol=atol, rtol=rtol
        )
        if abs_diff > worst_abs:
            worst_abs = abs_diff
            worst_rel = rel_diff
            worst_control = name
        if not ok:
            mismatches.append(
                {
                    "control": name,
                    "observed": observed.get(name),
                    "expected": expected.get(name),
                    "abs_diff": abs_diff,
                    "rel_diff": rel_diff,
                }
            )
    return {
        "ok": not mismatches,
        "control_count": len(controls),
        "mismatches": mismatches,
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_control": worst_control,
    }


def _certify_c2_private_control_continuation(
    *,
    asset: str,
    controls: list[str],
    representative_runtime: Any,
    edge_contract_for_controls: Mapping[str, Any],
    bars_all: pd.DataFrame,
    c1: Mapping[str, Any],
    steps: int,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    """Prove C.2 internal private controls continue exactly without changing C.2."""
    if not controls:
        return {
            "status": "PASS",
            "controls": [],
            "steps": 0,
            "sequential_failures": [],
            "restart_failures": [],
            "final_values": {},
        }
    needed = 6000 + int(steps)
    if len(bars_all) < needed:
        raise RuntimeError(
            f"{asset}: private-control certification needs >= {needed} bars, "
            f"have {len(bars_all)}"
        )
    bars = bars_all.tail(needed).reset_index(drop=True)
    bootstrap_bars = bars.iloc[:6000].copy()

    bootstrap_authority = _normalize(
        representative_runtime.build_features(
            bars=bootstrap_bars.copy(), drop_warmup_rows=False
        )
    )
    full_authority = _normalize(
        representative_runtime.build_features(
            bars=bars.copy(), drop_warmup_rows=False
        )
    )
    # Canonical shared alias: some authoritative edge runtimes expose
    # Bar_Volatility_Bps explicitly while others expose its exact source
    # Range_Bps only.
    for frame in (bootstrap_authority, full_authority):
        if "Bar_Volatility_Bps" not in frame.columns and "Range_Bps" in frame.columns:
            frame["Bar_Volatility_Bps"] = pd.to_numeric(
                frame["Range_Bps"], errors="coerce"
            )

    for control in controls:
        if control not in bootstrap_authority.columns:
            raise RuntimeError(
                f"{asset}: bootstrap authority missing private control {control}"
            )
        if control not in full_authority.columns:
            raise RuntimeError(
                f"{asset}: sequential authority missing private control {control}"
            )

    state = bootstrap_compact_state(
        asset=asset,
        bars=bootstrap_bars,
        authoritative_features=bootstrap_authority,
        phase_c1_contract=c1,
    )
    previous_hmm_target = None
    eligible_bootstrap = bootstrap_authority.dropna(subset=list(REGIME_COLUMNS))
    if not eligible_bootstrap.empty:
        previous_hmm_target = pd.Timestamp(
            eligible_bootstrap["Datetime"].iloc[-1]
        )

    failures: list[dict[str, Any]] = []
    restart_failures: list[dict[str, Any]] = []
    restart_steps = {x for x in (32, 64, 96) if x < int(steps)}
    update_ms: list[float] = []
    final_values: dict[str, float] = {}

    for step in range(1, int(steps) + 1):
        bar = bars.iloc[6000 + step - 1]
        ts = pd.Timestamp(bar["Datetime"])
        expected_rows = full_authority[full_authority["Datetime"] == ts]
        if len(expected_rows) != 1:
            raise RuntimeError(
                f"{asset}: private-control authority row count={len(expected_rows)} "
                f"at {ts}"
            )
        expected_row = expected_rows.iloc[0]
        generation, ffd, hmm, previous_hmm_target = _generation_from_authority(
            asset=asset,
            row=expected_row,
            previous_hmm_target=previous_hmm_target,
        )
        public_row, state, meta = update_one(
            state=state,
            bar=bar,
            dependency_generation=generation,
            ffd_values=ffd,
            hmm_values=hmm,
            phase_c1_contract=c1,
        )
        update_ms.append(float(meta["elapsed_ms"]))
        observed = latest_candidate_control_values(
            state=state,
            edge_contract=edge_contract_for_controls,
        )
        expected = {
            name: _control_expected(expected_row, name)
            for name in controls
        }
        parity = _compare_control_mapping(
            observed, expected, controls, atol=atol, rtol=rtol
        )
        if not parity["ok"] and len(failures) < 20:
            failures.append(
                {
                    "step": step,
                    "timestamp": ts.isoformat(),
                    "parity": parity,
                }
            )
        final_values = {name: float(observed[name]) for name in controls}

        if step in restart_steps:
            try:
                payload = serialize_compact_state(
                    state, phase_c1_contract=c1
                )
                state = deserialize_compact_state(
                    payload, phase_c1_contract=c1
                )
                restarted = latest_candidate_control_values(
                    state=state,
                    edge_contract=edge_contract_for_controls,
                )
                restart_parity = _compare_control_mapping(
                    restarted,
                    final_values,
                    controls,
                    atol=0.0,
                    rtol=0.0,
                )
                if not restart_parity["ok"]:
                    restart_failures.append(
                        {"step": step, "parity": restart_parity}
                    )
            except Exception as exc:
                restart_failures.append(
                    {
                        "step": step,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    return {
        "status": "PASS" if not failures and not restart_failures else "FAIL",
        "controls": controls,
        "steps": int(steps),
        "sequential_failures": failures,
        "restart_failures": restart_failures,
        "final_values": final_values,
        "update_latency_ms": {
            "count": len(update_ms),
            "mean": float(np.mean(update_ms)) if update_ms else None,
            "p50": float(np.quantile(update_ms, 0.50)) if update_ms else None,
            "p95": float(np.quantile(update_ms, 0.95)) if update_ms else None,
            "max": float(np.max(update_ms)) if update_ms else None,
        },
    }


def _certify_edge(
    *,
    runtime: Any,
    edge_contract: Mapping[str, Any],
    full_features: pd.DataFrame,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    edge_id = str(edge_contract["edge_id"])
    schema_features = [str(x) for x in edge_contract.get("schema_features") or []]
    candidate_controls = [
        str(x) for x in edge_contract.get("candidate_control_columns") or []
    ]
    selected_features = [str(x) for x in edge_contract.get("selected_features") or []]
    projection_columns = [
        name
        for name in [
            *RAW_CONSUMER_COLUMNS,
            *schema_features,
            *candidate_controls,
        ]
        if name in full_features.columns
    ]
    required_projection = [
        "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
        *schema_features,
        *candidate_controls,
    ]
    missing = [name for name in required_projection if name not in full_features.columns]
    if missing:
        raise RuntimeError(f"{edge_id}: structural-group authority missing projection columns {missing[:20]}")

    full = full_features.copy()
    full["edge_id"] = edge_id
    full["side"] = int(getattr(runtime, "SIDE_VALUE"))
    canonical = full[projection_columns].copy()
    hidden_extras = sorted(set(full.columns).difference(canonical.columns))

    full_events, full_event_meta = _call_candidate(runtime, full)
    canonical_events, canonical_event_meta = _call_candidate(runtime, canonical)
    candidate_parity = _compare_frames(
        canonical_events, full_events, atol=atol, rtol=rtol
    )

    full_rows, full_row_meta = _call_live_rows(
        runtime,
        features=full,
        events=full_events,
        selected_features=selected_features,
    )
    canonical_rows, canonical_row_meta = _call_live_rows(
        runtime,
        features=canonical,
        events=canonical_events,
        selected_features=selected_features,
    )
    model_parity = _compare_frames(
        canonical_rows,
        full_rows,
        atol=atol,
        rtol=rtol,
        required_columns=["Datetime", *selected_features],
    )

    poison = _poison_selected_feature_sources(
        runtime,
        features=canonical,
        events=canonical_events,
        selected_features=selected_features,
    )

    status = (
        "PASS"
        if candidate_parity.get("ok")
        and model_parity.get("ok")
        and poison.get("ok")
        else "FAIL"
    )
    return {
        "edge_id": edge_id,
        "asset": str(edge_contract.get("asset")),
        "family": str(edge_contract.get("family")),
        "status": status,
        "schema_feature_count": len(schema_features),
        "required_addon_feature_count": int(
            edge_contract.get("required_addon_feature_count") or 0
        ),
        "candidate_control_columns": candidate_controls,
        "candidate_control_count": len(candidate_controls),
        "selected_feature_count": len(selected_features),
        "hidden_full_authority_extra_column_count": len(hidden_extras),
        "hidden_full_authority_extra_columns": hidden_extras,
        "canonical_projection_column_count": len(canonical.columns),
        "candidate_events_full": full_event_meta,
        "candidate_events_canonical": canonical_event_meta,
        "candidate_parity": candidate_parity,
        "live_rows_full": full_row_meta,
        "live_rows_canonical": canonical_row_meta,
        "live_selected_feature_parity": model_parity,
        "selected_feature_poison_source_proof": poison,
        "selected_feature_event_collision_policy": edge_contract.get(
            "selected_feature_event_collision_policy"
        ),
        "canonical_live_row_collision_count": int(
            canonical_row_meta.get("selected_feature_collision_count") or 0
        ),
        "canonical_live_row_collision_columns": list(
            canonical_row_meta.get("selected_feature_collision_columns") or []
        ),
        "forbidden_live_columns_present": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--rows", type=int, default=6000)
    parser.add_argument("--control-steps", type=int, default=128)
    parser.add_argument("--assets", nargs="*", default=None)
    parser.add_argument("--edges", nargs="*", default=None)
    parser.add_argument("--atol", type=float, default=5e-10)
    parser.add_argument("--rtol", type=float, default=5e-10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    root = args.v2_root.resolve()
    if args.rows != 6000:
        raise RuntimeError(
            f"Phase C.3 production consumer certification requires exactly 6000 rows; got {args.rows}"
        )
    if args.control_steps <= 0:
        raise RuntimeError("--control-steps must be positive")

    frozen_validation = validate_frozen_consumer_contract(
        v2_root=root, frozen_path=root / "portfolio_live/config/phase_c/consumer_contract.json"
    )
    if frozen_validation.get("status") != "PASS":
        raise RuntimeError(f"frozen C.3 contract is stale: {frozen_validation}")

    contract = _read_json(root / "portfolio_live/config/phase_c/consumer_contract.json")
    c1 = _read_json(root / "portfolio_live/config/phase_c/feature_contract.json")
    c2 = _read_json(root / "portfolio_live/config/phase_c/incremental_feature_certification.json")
    manifest = _read_json(root / "portfolio_live/live_manifest.json")
    manifest_edges = _manifest_map(manifest)
    c1_edges = _c1_edge_map(c1)
    c3_edges = {
        str(row["edge_id"]): dict(row)
        for row in contract.get("edges") or []
        if isinstance(row, Mapping)
    }

    selected_assets = list(contract.get("selected_assets") or [])
    if args.assets:
        wanted_assets = {str(x).upper() for x in args.assets}
        unknown = sorted(wanted_assets.difference(selected_assets))
        if unknown:
            raise RuntimeError(f"unknown --assets values: {unknown}")
        selected_assets = [asset for asset in selected_assets if asset in wanted_assets]

    selected_edges = set(c3_edges)
    if args.edges:
        wanted_edges = {str(x) for x in args.edges}
        unknown = sorted(wanted_edges.difference(selected_edges))
        if unknown:
            raise RuntimeError(f"unknown --edges values: {unknown}")
        selected_edges = wanted_edges

    if args.assets:
        selected_edges = {
            edge_id
            for edge_id in selected_edges
            if str(c3_edges[edge_id].get("asset")).upper() in set(selected_assets)
        }

    print("=" * 112)
    print("PHASE C.3 — EDGE CONSUMER / ADDON / LIVE MODEL-INPUT CERTIFICATION")
    print("=" * 112)
    print(f"Contract:     {contract.get('contract_sha256')}")
    print(f"Assets:       {', '.join(selected_assets)}")
    print(f"Edges:        {len(selected_edges)}")
    print("Rows:         6000 exact production feature authority")
    print(f"Control seq:  {args.control_steps} C.2 internal continuation steps")
    print(f"Tolerance:    atol={args.atol} rtol={args.rtol}")
    print("Routing live: False")
    print("Dependency authority sandbox: ON")

    edge_results: dict[str, Any] = {}
    group_builds: list[dict[str, Any]] = []
    private_control_results: dict[str, Any] = {}
    fatal_errors: list[str] = []
    sandbox_meta: dict[str, Any] = {}

    with _isolated_dependency_authority(root, selected_assets) as sandbox:
        sandbox_meta = {
            "enabled": True,
            "seeded_file_count": int(sandbox["seeded_file_count"]),
            "production_continuation_mutated": False,
        }
        for asset in selected_assets:
            asset_row = (c1.get("assets") or {}).get(asset)
            if not isinstance(asset_row, Mapping):
                fatal_errors.append(f"{asset}: C.1 asset contract missing")
                continue
            groups = list(asset_row.get("structural_contract_groups") or [])
            if not groups:
                fatal_errors.append(f"{asset}: no structural groups")
                continue

            # Load bars once from any selected edge for this asset.
            asset_edge_ids = [
                edge_id
                for edge_id in sorted(selected_edges)
                if str(c3_edges[edge_id].get("asset")).upper() == asset
            ]
            if not asset_edge_ids:
                continue
            first_runtime = load_edge_runtime_from_manifest_entry(
                manifest_edges[asset_edge_ids[0]], v2_root=root
            )
            bars_all = _load_bars(first_runtime, v2_root=root)
            if len(bars_all) < args.rows:
                fatal_errors.append(
                    f"{asset}: need >= {args.rows} verified bars, have {len(bars_all)}"
                )
                continue

            asset_controls = sorted(
                {
                    control
                    for edge_id in asset_edge_ids
                    for control in (
                        c3_edges[edge_id].get("candidate_control_columns") or []
                    )
                }
            )
            if asset_controls:
                control_candidates = sorted(
                    asset_edge_ids,
                    key=lambda edge_id: (
                        -len(
                            set(
                                c3_edges[edge_id].get(
                                    "candidate_control_columns"
                                )
                                or []
                            ).intersection(asset_controls)
                        ),
                        edge_id,
                    ),
                )
                control_edge_id = control_candidates[0]
                control_runtime = load_edge_runtime_from_manifest_entry(
                    manifest_edges[control_edge_id], v2_root=root
                )
                synthetic_control_contract = {
                    **c3_edges[control_edge_id],
                    "candidate_control_columns": asset_controls,
                }
                try:
                    private_control_results[asset] = (
                        _certify_c2_private_control_continuation(
                            asset=asset,
                            controls=asset_controls,
                            representative_runtime=control_runtime,
                            edge_contract_for_controls=synthetic_control_contract,
                            bars_all=bars_all,
                            c1=c1,
                            steps=args.control_steps,
                            atol=args.atol,
                            rtol=args.rtol,
                        )
                    )
                except Exception as exc:
                    private_control_results[asset] = {
                        "status": "FAIL",
                        "controls": asset_controls,
                        "fatal_error": f"{type(exc).__name__}: {exc}",
                    }
            else:
                private_control_results[asset] = {
                    "status": "PASS",
                    "controls": [],
                    "steps": 0,
                    "sequential_failures": [],
                    "restart_failures": [],
                    "final_values": {},
                }

            bars = bars_all.tail(args.rows).reset_index(drop=True)

            print(f"\n--- {asset} ---")
            control_status = private_control_results[asset].get("status")
            print(
                f"private_controls={asset_controls or 'none'} "
                f"continuation={control_status}"
            )
            rolling_control_expected: dict[str, float] | None = None
            for group in groups:
                group_edge_ids = [
                    str(x)
                    for x in group.get("edge_ids") or []
                    if str(x) in selected_edges
                ]
                if not group_edge_ids:
                    continue
                representative_id = sorted(group_edge_ids)[0]
                rep_runtime = load_edge_runtime_from_manifest_entry(
                    manifest_edges[representative_id], v2_root=root
                )
                t0 = time.perf_counter()
                try:
                    authority = _normalize(
                        rep_runtime.build_features(
                            bars=bars.copy(), drop_warmup_rows=False
                        )
                    )
                except Exception as exc:
                    message = (
                        f"{asset}/{group.get('group_id')}: authoritative build failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    fatal_errors.append(message)
                    print(f"group={group.get('group_id')} BUILD_FAIL {message}")
                    continue
                build_ms = (time.perf_counter() - t0) * 1000.0
                group_builds.append(
                    {
                        "asset": asset,
                        "group_id": str(group.get("group_id")),
                        "representative_edge_id": representative_id,
                        "edge_count_certified": len(group_edge_ids),
                        "authority_build_ms": build_ms,
                        "authority_rows": len(authority),
                    }
                )

                if asset_controls and rolling_control_expected is None:
                    last = authority.iloc[-1]
                    try:
                        rolling_control_expected = {
                            name: _control_expected(last, name)
                            for name in asset_controls
                        }
                    except Exception:
                        rolling_control_expected = None

                group_pass = 0
                for edge_id in sorted(group_edge_ids):
                    runtime = load_edge_runtime_from_manifest_entry(
                        manifest_edges[edge_id], v2_root=root
                    )
                    try:
                        result = _certify_edge(
                            runtime=runtime,
                            edge_contract=c3_edges[edge_id],
                            full_features=authority,
                            atol=args.atol,
                            rtol=args.rtol,
                        )
                    except Exception as exc:
                        result = {
                            "edge_id": edge_id,
                            "asset": asset,
                            "family": str(c3_edges[edge_id].get("family")),
                            "status": "FAIL",
                            "fatal_error": f"{type(exc).__name__}: {exc}",
                        }
                    edge_results[edge_id] = result
                    if result.get("status") == "PASS":
                        group_pass += 1
                    else:
                        print(
                            f"FAIL edge={edge_id} "
                            f"error={result.get('fatal_error') or result.get('candidate_parity') or result.get('live_selected_feature_parity')}"
                        )
                print(
                    f"group={str(group.get('group_id'))[:12]} "
                    f"rep={representative_id} build_ms={build_ms:.1f} "
                    f"edges={len(group_edge_ids)} pass={group_pass}"
                )

            if asset_controls:
                control_result = private_control_results[asset]
                final_values = control_result.get("final_values") or {}
                if rolling_control_expected is None:
                    control_result["rolling_6000_final_parity"] = {
                        "ok": False,
                        "reason": "no_6000_authority_group_exposed_all_private_controls",
                    }
                    control_result["status"] = "FAIL"
                else:
                    rolling_parity = _compare_control_mapping(
                        final_values,
                        rolling_control_expected,
                        asset_controls,
                        atol=args.atol,
                        rtol=args.rtol,
                    )
                    control_result["rolling_6000_final_parity"] = rolling_parity
                    if not rolling_parity.get("ok"):
                        control_result["status"] = "FAIL"

    expected_count = len(selected_edges)
    pass_count = sum(1 for row in edge_results.values() if row.get("status") == "PASS")
    fail_count = expected_count - pass_count
    if len(edge_results) != expected_count:
        fatal_errors.append(
            f"edge result count mismatch expected={expected_count} actual={len(edge_results)}"
        )

    private_control_failures = sorted(
        asset
        for asset, row in private_control_results.items()
        if row.get("status") != "PASS"
    )
    status = (
        "PASS"
        if not fatal_errors and fail_count == 0 and not private_control_failures
        else "FAIL"
    )
    output = {
        "schema_version": "phase_c3_consumer_certification_v1_1_private_controls",
        "status": status,
        "runtime_routing_enabled": False,
        "consumer_contract_sha256": str(contract.get("contract_sha256") or ""),
        "consumer_contract_file_sha256": _sha256(
            root / "portfolio_live/config/phase_c/consumer_contract.json"
        ),
        "phase_c3_implementation_fingerprint": {
            "consumer_contract_compiler_sha256": _sha256(
                root / "shared/edges/phase_c_consumer_contract.py"
            ),
            "consumer_adapter_sha256": _sha256(
                root / "shared/edges/phase_c_consumer_adapter.py"
            ),
            "consumer_certifier_sha256": _sha256(
                root / "portfolio_live/phase_c_03_certify_consumer_contract.py"
            ),
        },
        "phase_c1_contract_sha256": str(c1.get("contract_sha256") or ""),
        "phase_c2_engine_version": str(c2.get("engine_version") or ""),
        "phase_c2_certification_status": str(c2.get("status") or ""),
        "selected_asset_count": len(selected_assets),
        "selected_assets": selected_assets,
        "selected_edge_count": expected_count,
        "edges_passed": pass_count,
        "edges_failed": fail_count,
        "authority_rows": args.rows,
        "private_control_continuation_steps": args.control_steps,
        "private_control_assets_failed": private_control_failures,
        "private_control_continuation": private_control_results,
        "strict_atol": args.atol,
        "strict_rtol": args.rtol,
        "dependency_authority_sandbox": sandbox_meta,
        "structural_group_authority_builds": group_builds,
        "edges": edge_results,
        "fatal_errors": fatal_errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("\n" + "=" * 112)
    print(f"OUTPUT: {args.output.resolve()}")
    print(
        f"STATUS: {status} | edges_passed={pass_count}/{expected_count} "
        f"private_control_assets_failed={len(private_control_failures)} "
        f"groups_built={len(group_builds)}"
    )
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
