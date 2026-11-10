"""Resumable certification for the guarded native one-row runtime.

This version avoids the two expensive behaviours in the original certifier:
1. it installs and uses the existing verified FFD/HMM hotpath;
2. it builds each asset's full batch authority once, not once per stream step.

Progress is checkpointed after every asset and edge. Re-running the same command
resumes compatible passed work. Use --fresh to discard the certification cache.

Certification has two explicit safe event modes:
* native_one_row_certified for bounded causal event generators;
* authoritative_full_history_required for cumulative/path-dependent generators.
The second mode passes global certification but can never enable the bounded
native event engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.edges.native_one_row_contract import (  # noqa: E402
    CERTIFICATION_PATH,
    SCHEMA_VERSION,
    FULL_AUTHORITY_EVENT_MODE,
    NATIVE_EVENT_MODE,
    clear_certification_cache,
    edge_certification_passed,
    implementation_hashes,
    runtime_environment,
)
from shared.edges.native_one_row_feature_engine import (  # noqa: E402
    NativeFeatureError,
    bootstrap_state,
    update_state,
)
from shared.edges.native_one_row_event_engine import (  # noqa: E402
    NativeEventError,
    is_expected_empty_event_exception,
    transition_events_live_native,
)
from shared.edges.incremental_runtime_primitives import runtime_replay_profile  # noqa: E402
from shared.edges.runtime_loader import load_edge_runtime  # noqa: E402
from shared.live_streaming.hotpath import (  # noqa: E402
    install_hotpath_verifiers,
    patch_runtime_shared_loaders,
)

FEATURE_ATOL = 5e-8
FEATURE_RTOL = 1e-7
# pandas-ta-classic STC is recursively rounded to 8 decimals after two
# stochastic stages.  Across supported pandas/numpy builds, the authoritative
# batch path and the scalar continuation can differ by a few 1e-7 solely from
# floating operation order.  Keep this override isolated to STC; all other
# features retain the stricter default gate.
STC_FEATURE_ATOL = 5e-7
STC_FEATURE_RTOL = 0.0
DEFAULT_HISTORY_ROWS = 6500
DEFAULT_STREAM_STEPS = 3
EVENT_OVERLAP_ROWS = 250
EVENT_AUDIT_ANCHOR_OFFSETS = (0, 500, 1000, 2000)
CONTEXT_CANDIDATES = (256, 320, 400, 600, 800, 1200, 1800, 2400, 4000, 6500)
CACHE_DIR = ROOT / "portfolio_live" / ".native_one_row_certification_cache_v5"
EDGE_LOCAL_FEATURE_FAMILIES = frozenset({"absorption"})
LIVE_RUNTIME_WINDOW_ROWS = 6000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_manifest() -> dict[str, Any]:
    path = ROOT / "portfolio_live" / "live_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    edges = payload.get("selected_edges")
    if not isinstance(edges, list) or len(edges) != 33:
        raise RuntimeError(f"Expected 33 selected edges, found {len(edges or [])}")
    return payload


def edge_root(edge_id: str) -> Path:
    asset, name = edge_id.split("/", 1)
    return ROOT / "assets" / asset / "edges" / name


def load_runtime(edge_id: str) -> Any:
    runtime = load_edge_runtime(edge_root(edge_id))
    patch_runtime_shared_loaders(runtime)
    return runtime


def bars_path(asset: str) -> Path:
    asset_l = asset.lower()
    candidates = [
        ROOT / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset_l}_standard_quote_tick_bars.csv",
        ROOT / "parity_samples" / f"{asset}_canonical_bars_tail_50000.csv",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Canonical standard_quote_tick bars not found for {asset}")


def read_tail_csv(path: Path, rows: int) -> pd.DataFrame:
    frame = pd.read_csv(path).tail(rows).reset_index(drop=True)
    frame["Datetime"] = pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    frame["Bar_Start"] = pd.to_datetime(frame["Bar_Start"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["Datetime", "Bar_Start"]).sort_values("Datetime", kind="mergesort")
    return frame.drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def expected_features(runtime: Any) -> list[str]:
    fn = getattr(runtime, "_schema_feature_names", None)
    return [str(x) for x in fn()] if callable(fn) else []


def shared_feature_builder_priority(edge: dict[str, Any]) -> tuple[int, str]:
    edge_id = str(edge.get("edge_id", "")).lower()
    family = str(edge.get("family", "")).lower()
    haystack = f"{edge_id} {family}"
    priorities = (
        ("volatilitycarry", 0),
        ("volatilityregime", 1),
        ("timeregime", 2),
        ("trend", 3),
        ("rangestructure", 4),
        ("compressionbreakoutcontinuation", 5),
        ("absorption", 6),
        ("reversal", 7),
    )
    for token, priority in priorities:
        if token in haystack:
            return priority, edge_id
    return 50, edge_id


def edge_uses_edge_local_feature_route(edge: dict[str, Any]) -> bool:
    """Mirror the dashboard families that bypass the shared feature table."""
    return str(edge.get("family") or "").strip().lower() in EDGE_LOCAL_FEATURE_FAMILIES


def normalize_timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def row_for_timestamp(frame: pd.DataFrame, stamp: pd.Timestamp) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        return pd.DataFrame()
    times = pd.to_datetime(frame["Datetime"], utc=True, errors="coerce")
    return frame.loc[times == normalize_timestamp(stamp)].tail(1).copy()


def feature_numeric_tolerance(column: str) -> tuple[float, float, str]:
    if str(column) == "STC":
        return STC_FEATURE_ATOL, STC_FEATURE_RTOL, "stc_recursive_roundoff_absolute_only"
    return FEATURE_ATOL, FEATURE_RTOL, "default_strict"


def compare_latest(authority: pd.DataFrame, native: pd.DataFrame, columns: list[str]) -> dict[str, Any]:
    if authority.empty or native.empty:
        return {"ok": False, "reason": "empty_frame"}
    left = authority.iloc[-1]
    right = native.iloc[-1]
    missing = [c for c in columns if c not in native.columns or c not in authority.columns]
    if missing:
        return {"ok": False, "reason": "missing_columns", "missing": missing[:20]}
    worst_abs = 0.0
    worst_rel = 0.0
    worst_column = None
    for column in columns:
        av = pd.to_numeric(pd.Series([left[column]]), errors="coerce").iloc[0]
        bv = pd.to_numeric(pd.Series([right[column]]), errors="coerce").iloc[0]
        if pd.isna(av) and pd.isna(bv):
            continue
        if pd.isna(av) != pd.isna(bv):
            return {
                "ok": False,
                "reason": "nan_topology_mismatch",
                "column": column,
                "authority": str(av),
                "native": str(bv),
            }
        a = float(av)
        b = float(bv)
        if math.isinf(a) or math.isinf(b):
            if a != b:
                return {"ok": False, "reason": "infinity_mismatch", "column": column}
            continue
        diff = abs(a - b)
        rel = diff / max(abs(a), abs(b), 1.0)
        if diff > worst_abs:
            worst_abs, worst_rel, worst_column = diff, rel, column
        atol, rtol, tolerance_policy = feature_numeric_tolerance(column)
        if not np.isclose(a, b, rtol=rtol, atol=atol, equal_nan=True):
            return {
                "ok": False,
                "reason": "numeric_mismatch",
                "column": column,
                "authority": a,
                "native": b,
                "abs_diff": diff,
                "rel_diff": rel,
                "atol": atol,
                "rtol": rtol,
                "tolerance_policy": tolerance_policy,
            }
    return {
        "ok": True,
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_column": worst_column,
    }


def compare_latest_rows(left_frame: pd.DataFrame, right_frame: pd.DataFrame) -> dict[str, Any]:
    if left_frame.empty or right_frame.empty:
        return {"ok": False, "reason": "empty_frame"}
    schema = compare_exact_schema(left_frame, right_frame)
    if not schema.get("ok"):
        return schema
    left = left_frame.iloc[-1]
    right = right_frame.iloc[-1]
    worst_abs = 0.0
    worst_rel = 0.0
    worst_column = None
    for column in left_frame.columns:
        if column == "Datetime":
            if normalize_timestamp(left[column]) != normalize_timestamp(right[column]):
                return {"ok": False, "reason": "datetime_mismatch", "column": column}
            continue
        lv = left[column]
        rv = right[column]
        if pd.isna(lv) and pd.isna(rv):
            continue
        if pd.isna(lv) != pd.isna(rv):
            return {"ok": False, "reason": "nan_topology_mismatch", "column": column}
        if pd.api.types.is_numeric_dtype(left_frame[column]) and pd.api.types.is_numeric_dtype(right_frame[column]):
            a = float(lv)
            b = float(rv)
            if math.isinf(a) or math.isinf(b):
                if a != b:
                    return {"ok": False, "reason": "infinity_mismatch", "column": column}
                continue
            diff = abs(a - b)
            rel = diff / max(abs(a), abs(b), 1.0)
            if diff > worst_abs:
                worst_abs, worst_rel, worst_column = diff, rel, column
            atol, rtol, policy = feature_numeric_tolerance(str(column))
            if not np.isclose(a, b, atol=atol, rtol=rtol, equal_nan=True):
                return {
                    "ok": False,
                    "reason": "numeric_mismatch",
                    "column": column,
                    "left": a,
                    "right": b,
                    "abs_diff": diff,
                    "rel_diff": rel,
                    "tolerance_policy": policy,
                }
        elif str(lv) != str(rv):
            return {
                "ok": False,
                "reason": "exact_value_mismatch",
                "column": column,
                "left": str(lv),
                "right": str(rv),
            }
    return {
        "ok": True,
        "reason": "latest_row_parity_pass",
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_column": worst_column,
    }


def compare_exact_schema(authority: pd.DataFrame, native: pd.DataFrame) -> dict[str, Any]:
    left = [str(name) for name in authority.columns]
    right = [str(name) for name in native.columns]
    if left != right:
        return {
            "ok": False,
            "reason": "exact_schema_mismatch",
            "authority_columns": left,
            "native_columns": right,
            "missing": [name for name in left if name not in set(right)][:20],
            "extra": [name for name in right if name not in set(left)][:20],
        }
    return {"ok": True, "reason": "exact_schema_match", "columns": len(left)}


def event_identity_rows(events: pd.DataFrame) -> list[tuple[Any, ...]]:
    if not isinstance(events, pd.DataFrame) or events.empty or "Datetime" not in events.columns:
        return []
    work = events.copy()
    work["Datetime"] = pd.to_datetime(work["Datetime"], utc=True, errors="coerce")
    work = work.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    columns = [
        name
        for name in ("Datetime", "event_id", "side", "candidate_event_generator", "cusum_direction")
        if name in work.columns
    ]
    rows: list[tuple[Any, ...]] = []
    for _, row in work[columns].iterrows():
        values: list[Any] = []
        for name in columns:
            value = row[name]
            if name == "Datetime":
                values.append(pd.Timestamp(value).isoformat())
            elif value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
                values.append(None)
            elif isinstance(value, np.generic):
                values.append(value.item())
            else:
                values.append(value)
        rows.append(tuple(values))
    return rows


def event_frame_digest(events: pd.DataFrame) -> str:
    """Stable digest for deterministic full-authority replay certification."""
    if not isinstance(events, pd.DataFrame) or events.empty:
        return hashlib.sha256(b"EMPTY_EVENTS").hexdigest()
    work = events.copy()
    if "Datetime" in work.columns:
        work["Datetime"] = pd.to_datetime(
            work["Datetime"], utc=True, errors="coerce"
        )
        work = (
            work.dropna(subset=["Datetime"])
            .sort_values("Datetime", kind="mergesort")
            .drop_duplicates("Datetime", keep="last")
            .reset_index(drop=True)
        )
    for column in work.columns:
        if pd.api.types.is_datetime64_any_dtype(work[column]):
            work[column] = work[column].astype("int64")
        elif work[column].dtype == "object":
            work[column] = work[column].astype(str)
    values = pd.util.hash_pandas_object(work, index=False).to_numpy(dtype="uint64")
    digest = hashlib.sha256()
    digest.update("|".join(str(name) for name in work.columns).encode("utf-8"))
    digest.update(values.tobytes())
    return digest.hexdigest()


def safe_events(runtime: Any, features: pd.DataFrame) -> pd.DataFrame:
    try:
        out = runtime.generate_candidate_events(features.copy())
    except Exception as exc:
        if is_expected_empty_event_exception(exc):
            return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
        raise
    if out is None or not isinstance(out, pd.DataFrame) or out.empty:
        return pd.DataFrame({"Datetime": pd.Series(dtype="datetime64[ns, UTC]")})
    out = out.copy()
    out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
    return (
        out.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )


def event_times(events: pd.DataFrame, cutoff: pd.Timestamp) -> set[pd.Timestamp]:
    if events.empty:
        return set()
    return set(pd.to_datetime(events.loc[events["Datetime"] >= cutoff, "Datetime"], utc=True))


def certify_edge_local_feature_route(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    stream_steps: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Certify a dashboard edge-local feature route against its own authority.

    Absorption is intentionally excluded from the dashboard's shared asset
    feature table.  Its native checkpoint must therefore be seeded and compared
    against the absorption runtime itself rather than a volatility-carry/shared
    projection.
    """
    steps_to_run = max(1, int(stream_steps))
    if len(bars) <= steps_to_run + 1000:
        raise RuntimeError(
            f"{edge_id}: insufficient bars for edge-local feature certification: {len(bars)}"
        )

    started = time.perf_counter()
    authority_full = runtime.build_features(
        bars=bars.copy(),
        drop_warmup_rows=False,
    )
    if not isinstance(authority_full, pd.DataFrame) or authority_full.empty:
        raise RuntimeError(f"{edge_id}: edge-local authority returned no features")
    authority_full = authority_full.copy()
    authority_full["Datetime"] = pd.to_datetime(
        authority_full["Datetime"], utc=True, errors="coerce"
    )
    authority_full = (
        authority_full.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )

    split = len(bars) - steps_to_run
    base_bars = bars.iloc[:split].copy()
    base_last = normalize_timestamp(base_bars["Datetime"].iloc[-1])
    base_authority = authority_full[
        pd.to_datetime(authority_full["Datetime"], utc=True, errors="coerce")
        <= base_last
    ].copy()
    latest, continuous_state, bootstrap_meta = bootstrap_state(
        runtime,
        asset=asset,
        edge_id=edge_id,
        bars=base_bars,
        authoritative_features=base_authority,
    )
    restarted_state = pickle.loads(
        pickle.dumps(continuous_state, protocol=pickle.HIGHEST_PROTOCOL)
    )

    step_reports: list[dict[str, Any]] = []
    for step_number, end in enumerate(
        range(split + 1, len(bars) + 1),
        start=1,
    ):
        prefix = bars.iloc[:end].copy()
        stamp = normalize_timestamp(prefix["Datetime"].iloc[-1])
        t0 = time.perf_counter()
        latest, continuous_state, metadata = update_state(
            runtime,
            asset=asset,
            edge_id=edge_id,
            bars=prefix,
            state=continuous_state,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        restarted_state = pickle.loads(
            pickle.dumps(restarted_state, protocol=pickle.HIGHEST_PROTOCOL)
        )
        restarted, restarted_state, restarted_metadata = update_state(
            runtime,
            asset=asset,
            edge_id=edge_id,
            bars=prefix,
            state=restarted_state,
        )

        authority_row = row_for_timestamp(authority_full, stamp)
        native_row = row_for_timestamp(latest, stamp)
        restarted_row = row_for_timestamp(restarted, stamp)
        authority_check = compare_latest_rows(authority_row, native_row)
        restart_check = compare_latest_rows(native_row, restarted_row)
        schema_check = compare_exact_schema(authority_full, latest)
        report = {
            "step": step_number,
            "timestamp": stamp.isoformat(),
            "native_ms": elapsed_ms,
            "continuation_mode": metadata.get("continuation_mode"),
            "restarted_continuation_mode": restarted_metadata.get("continuation_mode"),
            "authority_check": authority_check,
            "restart_check": restart_check,
            "schema_check": schema_check,
        }
        step_reports.append(report)
        if not authority_check.get("ok"):
            raise RuntimeError(
                f"{edge_id}: edge-local native row differs from authority at {stamp}: "
                f"{authority_check}"
            )
        if not restart_check.get("ok"):
            raise RuntimeError(
                f"{edge_id}: edge-local feature restart parity failed at {stamp}: "
                f"{restart_check}"
            )
        if not schema_check.get("ok"):
            raise RuntimeError(
                f"{edge_id}: edge-local exact schema parity failed: {schema_check}"
            )

    rolling_window_rows = min(LIVE_RUNTIME_WINDOW_ROWS, len(bars) - steps_to_run)
    rolling_base = bars.iloc[:rolling_window_rows].copy()
    rolling_base_last = normalize_timestamp(rolling_base["Datetime"].iloc[-1])
    rolling_authority = authority_full[
        pd.to_datetime(authority_full["Datetime"], utc=True, errors="coerce")
        <= rolling_base_last
    ].copy()
    _, rolling_state, _ = bootstrap_state(
        runtime,
        asset=asset,
        edge_id=edge_id,
        bars=rolling_base,
        authoritative_features=rolling_authority,
    )
    rolling_restart_state = pickle.loads(
        pickle.dumps(rolling_state, protocol=pickle.HIGHEST_PROTOCOL)
    )
    rolling_steps: list[dict[str, Any]] = []
    for rolling_step in range(1, steps_to_run + 1):
        rolling_bars = bars.iloc[
            rolling_step : rolling_step + rolling_window_rows
        ].copy()
        stamp = normalize_timestamp(rolling_bars["Datetime"].iloc[-1])
        rolling_native, rolling_state, rolling_meta = update_state(
            runtime,
            asset=asset,
            edge_id=edge_id,
            bars=rolling_bars,
            state=rolling_state,
        )
        rolling_restart_state = pickle.loads(
            pickle.dumps(rolling_restart_state, protocol=pickle.HIGHEST_PROTOCOL)
        )
        rolling_restarted, rolling_restart_state, _ = update_state(
            runtime,
            asset=asset,
            edge_id=edge_id,
            bars=rolling_bars,
            state=rolling_restart_state,
        )
        authority_check = compare_latest_rows(
            row_for_timestamp(authority_full, stamp),
            row_for_timestamp(rolling_native, stamp),
        )
        restart_check = compare_latest_rows(
            rolling_native.tail(1), rolling_restarted.tail(1)
        )
        rolling_steps.append({
            "step": rolling_step,
            "timestamp": stamp.isoformat(),
            "continuation_mode": rolling_meta.get("continuation_mode"),
            "authority_check": authority_check,
            "restart_check": restart_check,
        })
        if not authority_check.get("ok") or not restart_check.get("ok"):
            raise RuntimeError(
                f"{edge_id}: edge-local fixed-width feature continuation failed: "
                f"{rolling_steps[-1]}"
            )

    revision_rejected = False
    tampered = bars.copy()
    overlap_position = max(0, len(tampered) - 20)
    tampered.loc[
        tampered.index[overlap_position], "Close"
    ] = float(tampered.iloc[overlap_position]["Close"]) + 0.123456789
    try:
        update_state(
            runtime,
            asset=asset,
            edge_id=edge_id,
            bars=tampered,
            state=continuous_state,
        )
    except NativeFeatureError:
        revision_rejected = True
    if not revision_rejected:
        raise RuntimeError(
            f"{edge_id}: historical raw-bar revision was not rejected on edge-local route"
        )

    return latest.copy(), {
        "ok": True,
        "reason": "edge_local_native_feature_route_certified",
        "feature_execution_mode": "edge_local_native_authoritative_schema",
        "exact_schema_parity_passed": True,
        "append_only_history_passed": True,
        "restart_parity_passed": True,
        "rolling_window_feature_parity_passed": True,
        "rolling_window_rows": int(rolling_window_rows),
        "rolling_window_feature_steps": rolling_steps,
        "historical_revision_rejected": True,
        "bootstrap_elapsed_ms": float(bootstrap_meta.get("elapsed_ms") or 0.0),
        "native_mean_ms": float(np.mean([row["native_ms"] for row in step_reports])),
        "native_max_ms": float(np.max([row["native_ms"] for row in step_reports])),
        "steps": step_reports,
        "columns": int(len(latest.columns)),
        "total_elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def signature_for(selected: list[dict[str, Any]], history_rows: int, stream_steps: int) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "implementation_hashes": implementation_hashes(),
        "runtime_environment": runtime_environment(),
        "history_rows": int(history_rows),
        "stream_steps": int(stream_steps),
        "edge_ids": [str(x["edge_id"]) for x in selected],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def asset_cache_path(asset: str) -> Path:
    return CACHE_DIR / f"{asset}_asset_certification.pkl"


def load_asset_cache(asset: str, accepted_signatures: set[str]) -> dict[str, Any] | None:
    path = asset_cache_path(asset)
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict) or str(payload.get("run_signature") or "") not in accepted_signatures:
        return None
    if not isinstance(payload.get("features"), pd.DataFrame) or not isinstance(payload.get("bars"), pd.DataFrame):
        return None
    return payload


def save_asset_cache(asset: str, run_signature: str, bars: pd.DataFrame, features: pd.DataFrame, result: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = asset_cache_path(asset)
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(
            {
                "run_signature": run_signature,
                "bars": bars,
                "features": features,
                "result": result,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    temporary.replace(path)


def feature_tolerance_contract() -> dict[str, Any]:
    return {
        "default": {"atol": FEATURE_ATOL, "rtol": FEATURE_RTOL},
        "overrides": {
            "STC": {
                "atol": STC_FEATURE_ATOL,
                "rtol": STC_FEATURE_RTOL,
                "policy": "stc_recursive_roundoff_absolute_only",
            }
        },
    }


def new_payload(selected: list[dict[str, Any]], run_signature: str, history_rows: int, stream_steps: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "run_signature": run_signature,
        "implementation_hashes": implementation_hashes(),
        "runtime_environment": runtime_environment(),
        "feature_tolerances": feature_tolerance_contract(),
        "history_rows_requested": int(history_rows),
        "stream_steps_requested": int(stream_steps),
        "assets": {},
        "edges": {},
        "selected_edge_count": len(selected),
        "failures": [],
        "status": "RUNNING",
    }


ALLOWED_COMPATIBLE_RESUME_HASH_CHANGES: frozenset[str] = frozenset()



def compatible_partial_progress(
    existing: dict[str, Any],
    *,
    selected: list[dict[str, Any]],
    history_rows: int,
    stream_steps: int,
) -> bool:
    if not isinstance(existing, dict):
        return False
    if existing.get("schema_version") != SCHEMA_VERSION:
        return False
    if int(existing.get("history_rows_requested") or -1) != int(history_rows):
        return False
    if int(existing.get("stream_steps_requested") or -1) != int(stream_steps):
        return False
    if int(existing.get("selected_edge_count") or 0) != len(selected):
        return False
    if existing.get("runtime_environment") != runtime_environment():
        return False
    selected_ids = {str(row["edge_id"]) for row in selected}
    existing_ids = set((existing.get("edges") or {}).keys())
    if not existing_ids.issubset(selected_ids):
        return False
    old_hashes = existing.get("implementation_hashes") or {}
    current_hashes = implementation_hashes()
    for name, current_value in current_hashes.items():
        if name in ALLOWED_COMPATIBLE_RESUME_HASH_CHANGES:
            continue
        if old_hashes.get(name) != current_value:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-rows", type=int, default=DEFAULT_HISTORY_ROWS)
    parser.add_argument("--stream-steps", type=int, default=DEFAULT_STREAM_STEPS)
    parser.add_argument("--output", type=Path, default=CERTIFICATION_PATH)
    parser.add_argument("--fresh", action="store_true", help="Discard compatible partial progress and rerun everything.")
    args = parser.parse_args()

    install_report = install_hotpath_verifiers()
    print(f"[INFO] FFD/HMM hotpath installed={install_report.get('installed')} version={install_report.get('version')}")

    manifest = load_manifest()
    selected = [dict(x) for x in manifest["selected_edges"]]
    assets = sorted({str(x["edge_id"]).split("/", 1)[0] for x in selected})
    run_signature = signature_for(selected, int(args.history_rows), int(args.stream_steps))

    if args.fresh:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        if args.output.exists():
            args.output.unlink()

    payload = new_payload(selected, run_signature, int(args.history_rows), int(args.stream_steps))
    accepted_cache_signatures: set[str] = {run_signature}
    if args.output.exists() and not args.fresh:
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8-sig"))
        except Exception:
            existing = {}
        exact_resume = isinstance(existing, dict) and existing.get("run_signature") == run_signature
        compatible_resume = compatible_partial_progress(
            existing if isinstance(existing, dict) else {},
            selected=selected,
            history_rows=int(args.history_rows),
            stream_steps=int(args.stream_steps),
        )
        if exact_resume or compatible_resume:
            payload = existing
            old_signature = str(payload.get("run_signature") or "")
            if old_signature:
                accepted_cache_signatures.add(old_signature)
            payload["run_signature"] = run_signature
            payload["implementation_hashes"] = implementation_hashes()
            payload["runtime_environment"] = runtime_environment()
            payload["feature_tolerances"] = feature_tolerance_contract()
            payload["status"] = "RUNNING"
            payload["updated_at"] = utc_now()
            payload["failures"] = []
            mode = "exact" if exact_resume else "compatible-patch"
            print(
                f"[RESUME] {mode} progress found: "
                f"assets={len(payload.get('assets') or {})} edges={len(payload.get('edges') or {})}"
            )

    feature_by_asset: dict[str, pd.DataFrame] = {}
    bars_by_asset: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, Any]] = []
    payload["failures"] = []
    write_payload(args.output, payload)

    # Asset feature certification.
    for asset in assets:
        cached = None if args.fresh else load_asset_cache(asset, accepted_cache_signatures)
        if cached is not None and all(
            bool((cached.get("result") or {}).get(name))
            for name in (
                "feature_parity_passed",
                "exact_schema_parity_passed",
                "append_only_history_passed",
                "restart_parity_passed",
                "rolling_window_feature_parity_passed",
            )
        ):
            feature_by_asset[asset] = cached["features"]
            bars_by_asset[asset] = cached["bars"]
            payload["assets"][asset] = cached["result"]
            save_asset_cache(asset, run_signature, cached["bars"], cached["features"], cached["result"])
            print(f"[RESUME] {asset} native feature certification loaded from cache")
            continue

        try:
            asset_started = time.perf_counter()
            asset_edges = [x for x in selected if str(x["edge_id"]).startswith(asset + "/")]
            builder = sorted(asset_edges, key=shared_feature_builder_priority)[0]
            builder_id = str(builder["edge_id"])
            runtime = load_runtime(builder_id)
            bars = read_tail_csv(bars_path(asset), int(args.history_rows) + int(args.stream_steps))
            if len(bars) < 1000 + int(args.stream_steps):
                raise RuntimeError(f"{asset}: insufficient bars for certification: {len(bars)}")

            # One complete batch authority build for all stream checkpoints.
            authority_started = time.perf_counter()
            authority_full = runtime.build_features(bars=bars.copy(), drop_warmup_rows=False)
            authority_full_ms = (time.perf_counter() - authority_started) * 1000.0
            authority_full = authority_full.copy()
            authority_full["Datetime"] = pd.to_datetime(authority_full["Datetime"], utc=True, errors="coerce")

            split = len(bars) - int(args.stream_steps)
            base = bars.iloc[:split].copy()
            _, continuous_state, bootstrap_meta = bootstrap_state(
                runtime,
                asset=asset,
                edge_id=builder_id,
                bars=base,
            )
            restarted_state = pickle.loads(
                pickle.dumps(continuous_state, protocol=pickle.HIGHEST_PROTOCOL)
            )
            steps: list[dict[str, Any]] = []
            latest_native: pd.DataFrame | None = None
            exact_schema_passed = True
            restart_parity_passed = True
            for step_number, end in enumerate(range(split + 1, len(bars) + 1), start=1):
                prefix = bars.iloc[:end].copy()
                stamp = normalize_timestamp(prefix["Datetime"].iloc[-1])
                t0 = time.perf_counter()
                latest_native, continuous_state, meta = update_state(
                    runtime,
                    asset=asset,
                    edge_id=builder_id,
                    bars=prefix,
                    state=continuous_state,
                )
                native_ms = (time.perf_counter() - t0) * 1000.0
                restarted_state = pickle.loads(
                    pickle.dumps(restarted_state, protocol=pickle.HIGHEST_PROTOCOL)
                )
                restarted_native, restarted_state, restarted_meta = update_state(
                    runtime,
                    asset=asset,
                    edge_id=builder_id,
                    bars=prefix,
                    state=restarted_state,
                )
                authority_row = row_for_timestamp(authority_full, stamp)
                check = compare_latest(authority_row, latest_native, expected_features(runtime))
                schema_check = compare_exact_schema(authority_full, latest_native)
                restart_check = compare_latest_rows(
                    latest_native.tail(1),
                    restarted_native.tail(1),
                )
                exact_schema_passed = exact_schema_passed and bool(schema_check.get("ok"))
                restart_parity_passed = restart_parity_passed and bool(restart_check.get("ok"))
                steps.append(
                    {
                        "step": step_number,
                        "timestamp": stamp.isoformat(),
                        "native_ms": native_ms,
                        "continuation_mode": meta.get("continuation_mode"),
                        "restarted_continuation_mode": restarted_meta.get("continuation_mode"),
                        "schema_check": schema_check,
                        "restart_check": restart_check,
                        **check,
                    }
                )
                if not check.get("ok"):
                    raise RuntimeError(f"{asset}: feature parity failed at {stamp}: {check}")
                if not schema_check.get("ok"):
                    raise RuntimeError(f"{asset}: exact feature schema parity failed: {schema_check}")
                if not restart_check.get("ok"):
                    raise RuntimeError(f"{asset}: feature restart parity failed: {restart_check}")
            if latest_native is None:
                raise RuntimeError(f"{asset}: no native stream rows were produced")

            rolling_window_rows = min(
                LIVE_RUNTIME_WINDOW_ROWS,
                len(bars) - int(args.stream_steps),
            )
            if rolling_window_rows <= 1000:
                raise RuntimeError(f"{asset}: insufficient rolling-window history")
            rolling_base = bars.iloc[:rolling_window_rows].copy()
            _, rolling_state, _ = bootstrap_state(
                runtime,
                asset=asset,
                edge_id=builder_id,
                bars=rolling_base,
            )
            rolling_restart_state = pickle.loads(
                pickle.dumps(rolling_state, protocol=pickle.HIGHEST_PROTOCOL)
            )
            rolling_feature_steps: list[dict[str, Any]] = []
            for rolling_step in range(1, int(args.stream_steps) + 1):
                rolling_bars = bars.iloc[
                    rolling_step : rolling_step + rolling_window_rows
                ].copy()
                stamp = normalize_timestamp(rolling_bars["Datetime"].iloc[-1])
                rolling_native, rolling_state, rolling_meta = update_state(
                    runtime,
                    asset=asset,
                    edge_id=builder_id,
                    bars=rolling_bars,
                    state=rolling_state,
                )
                rolling_restart_state = pickle.loads(
                    pickle.dumps(rolling_restart_state, protocol=pickle.HIGHEST_PROTOCOL)
                )
                rolling_restarted, rolling_restart_state, _ = update_state(
                    runtime,
                    asset=asset,
                    edge_id=builder_id,
                    bars=rolling_bars,
                    state=rolling_restart_state,
                )
                authority_row = row_for_timestamp(authority_full, stamp)
                parity = compare_latest(
                    authority_row,
                    rolling_native,
                    expected_features(runtime),
                )
                restart = compare_latest_rows(
                    rolling_native.tail(1), rolling_restarted.tail(1)
                )
                rolling_feature_steps.append({
                    "step": rolling_step,
                    "timestamp": stamp.isoformat(),
                    "window_rows": rolling_window_rows,
                    "continuation_mode": rolling_meta.get("continuation_mode"),
                    "parity": parity,
                    "restart": restart,
                })
                if not parity.get("ok") or not restart.get("ok"):
                    raise RuntimeError(
                        f"{asset}: fixed-width feature continuation failed: "
                        f"{rolling_feature_steps[-1]}"
                    )

            feature_revision_rejected = False
            tampered_bars = bars.copy()
            overlap_position = max(0, len(tampered_bars) - 20)
            tampered_bars.loc[
                tampered_bars.index[overlap_position], "Close"
            ] = float(tampered_bars.iloc[overlap_position]["Close"]) + 0.123456789
            try:
                update_state(
                    runtime,
                    asset=asset,
                    edge_id=builder_id,
                    bars=tampered_bars,
                    state=continuous_state,
                )
            except NativeFeatureError:
                feature_revision_rejected = True
            if not feature_revision_rejected:
                raise RuntimeError("historical raw-bar revision was not rejected by native feature state")

            result = {
                "feature_parity_passed": True,
                "exact_schema_parity_passed": bool(exact_schema_passed),
                "append_only_history_passed": True,
                "restart_parity_passed": bool(restart_parity_passed),
                "rolling_window_feature_parity_passed": True,
                "rolling_window_rows": int(rolling_window_rows),
                "rolling_window_feature_steps": rolling_feature_steps,
                "historical_revision_rejected": True,
                "builder_edge_id": builder_id,
                "history_rows": len(bars),
                "stream_steps": steps,
                "native_mean_ms": float(np.mean([x["native_ms"] for x in steps])),
                "native_max_ms": float(np.max([x["native_ms"] for x in steps])),
                "authority_full_build_ms": authority_full_ms,
                "bootstrap_elapsed_ms": float(bootstrap_meta.get("elapsed_ms") or 0.0),
                "total_elapsed_ms": (time.perf_counter() - asset_started) * 1000.0,
            }
            feature_by_asset[asset] = latest_native.copy()
            bars_by_asset[asset] = bars.copy()
            payload["assets"][asset] = result
            save_asset_cache(asset, run_signature, bars, latest_native, result)
            payload["updated_at"] = utc_now()
            write_payload(args.output, payload)
            print(
                f"[PASS] {asset} native features mean={result['native_mean_ms']:.2f} ms "
                f"max={result['native_max_ms']:.2f} ms"
            )
        except Exception as exc:
            failure = {"scope": "asset", "id": asset, "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            payload["assets"][asset] = {
                "feature_parity_passed": False,
                "exact_schema_parity_passed": False,
                "append_only_history_passed": False,
                "restart_parity_passed": False,
                "rolling_window_feature_parity_passed": False,
                "failure": failure["error"],
            }
            payload["failures"] = failures
            payload["updated_at"] = utc_now()
            write_payload(args.output, payload)
            print(f"[FAIL] {asset}: {failure['error']}")

    # Edge projection and event certification. Failures do not discard passed edges.
    for index, edge in enumerate(selected, start=1):
        edge_id = str(edge["edge_id"])
        asset = edge_id.split("/", 1)[0]
        prior_edge = dict((payload.get("edges") or {}).get(edge_id) or {})
        if edge_certification_passed(prior_edge):
            print(f"[RESUME] {index:02d}/33 {edge_id} already passed")
            continue
        if asset not in feature_by_asset or asset not in bars_by_asset:
            failure = {
                "scope": "edge",
                "id": edge_id,
                "error": f"asset {asset} feature certification unavailable",
            }
            failures.append(failure)
            payload["edges"][edge_id] = {
                "feature_parity_passed": False,
                "event_parity_passed": False,
                "event_execution_mode": "uncertified",
                "native_event_eligible": False,
                "failure": failure["error"],
            }
            payload["failures"] = failures
            payload["updated_at"] = utc_now()
            write_payload(args.output, payload)
            continue

        try:
            edge_started = time.perf_counter()
            runtime = load_runtime(edge_id)
            bars = bars_by_asset[asset].copy()

            if edge_uses_edge_local_feature_route(edge):
                features, edge_feature_check = certify_edge_local_feature_route(
                    runtime,
                    asset=asset,
                    edge_id=edge_id,
                    bars=bars,
                    stream_steps=int(args.stream_steps),
                )
                feature_execution_mode = "edge_local_native_authoritative_schema"
            else:
                features = feature_by_asset[asset].copy()
                if "Range_Bps" in features.columns:
                    features["Bar_Volatility_Bps"] = pd.to_numeric(
                        features["Range_Bps"], errors="coerce"
                    )
                if "edge_id" in features.columns:
                    features["edge_id"] = edge_id
                if "side" in features.columns and edge.get("side_value") is not None:
                    features["side"] = int(edge["side_value"])

                edge_authority = runtime.build_features(
                    bars=bars.copy(),
                    drop_warmup_rows=False,
                )
                edge_feature_check = compare_latest(
                    edge_authority,
                    features,
                    expected_features(runtime),
                )
                if not edge_feature_check.get("ok"):
                    raise RuntimeError(
                        "projected native feature row differs from edge authority: "
                        f"{edge_feature_check}"
                    )
                feature_execution_mode = "shared_asset_native_projection"

            profile = runtime_replay_profile(runtime, None)
            full = safe_events(runtime, features)

            if bool(profile.force_full_event_replay):
                if tuple(profile.event_rows):
                    raise RuntimeError(
                        f"{edge_id}: force_full_event_replay profile unexpectedly declares "
                        f"bounded event rows={profile.event_rows}"
                    )
                replay_input = pickle.loads(
                    pickle.dumps(features, protocol=pickle.HIGHEST_PROTOCOL)
                )
                repeated = safe_events(runtime, replay_input)
                full_identity = event_identity_rows(full)
                repeated_identity = event_identity_rows(repeated)
                full_digest = event_frame_digest(full)
                repeated_digest = event_frame_digest(repeated)
                if full_identity != repeated_identity or full_digest != repeated_digest:
                    raise RuntimeError(
                        f"{edge_id}: full-authority deterministic replay mismatch "
                        f"identity_match={full_identity == repeated_identity} "
                        f"digest_match={full_digest == repeated_digest}"
                    )

                row = {
                    "feature_parity_passed": True,
                    "feature_parity": edge_feature_check,
                    "feature_execution_mode": feature_execution_mode,
                    "event_parity_passed": True,
                    "event_execution_mode": FULL_AUTHORITY_EVENT_MODE,
                    "native_event_eligible": False,
                    "full_authority_required": True,
                    "force_full_event_replay": True,
                    "full_authority_determinism_passed": True,
                    "full_authority_restart_equivalence_passed": True,
                    "full_authority_event_rows": int(len(full)),
                    "full_authority_digest": full_digest,
                    "candidate_full_confirmation_required": True,
                    "append_only_history_passed": None,
                    "restart_parity_passed": None,
                    "event_state_continuity_passed": None,
                    "historical_revision_rejected": None,
                    "rolling_window_event_parity_passed": None,
                    "certification_reason": (
                        "runtime replay profile declares cumulative/path-dependent "
                        "event mathematics; native bounded continuation is forbidden"
                    ),
                    "total_elapsed_ms": (
                        time.perf_counter() - edge_started
                    ) * 1000.0,
                }
                if not edge_certification_passed(row):
                    raise RuntimeError(
                        f"{edge_id}: full-authority classification failed contract validation"
                    )
                payload["edges"][edge_id] = row
                print(
                    f"[PASS] {index:02d}/33 {edge_id} "
                    f"event_mode={FULL_AUTHORITY_EVENT_MODE}"
                )
            else:
                chosen: int | None = None
                diagnostics: list[dict[str, Any]] = []
                valid_anchors = [
                    int(offset)
                    for offset in EVENT_AUDIT_ANCHOR_OFFSETS
                    if len(features) - int(offset) >= EVENT_OVERLAP_ROWS + 200
                ]
                seen_contexts: set[int] = set()
                for candidate in CONTEXT_CANDIDATES:
                    context = min(int(candidate), len(features))
                    if context in seen_contexts:
                        continue
                    seen_contexts.add(context)
                    anchor_diags: list[dict[str, Any]] = []
                    context_ok = True
                    total_elapsed = 0.0
                    for offset in valid_anchors:
                        end = len(features) - offset
                        prefix_features = features.iloc[:end].copy()
                        cutoff = pd.Timestamp(
                            prefix_features["Datetime"].iloc[-EVENT_OVERLAP_ROWS]
                        )
                        expected = event_times(full, cutoff)
                        anchor_last = pd.Timestamp(
                            prefix_features["Datetime"].iloc[-1]
                        )
                        expected = {x for x in expected if x <= anchor_last}
                        t0 = time.perf_counter()
                        trial = safe_events(
                            runtime,
                            prefix_features.tail(context).copy(),
                        )
                        elapsed = (time.perf_counter() - t0) * 1000.0
                        total_elapsed += elapsed
                        actual = event_times(trial, cutoff)
                        diag = {
                            "anchor_offset": offset,
                            "expected_events": len(expected),
                            "actual_events": len(actual),
                            "missing": len(expected - actual),
                            "extra": len(actual - expected),
                            "elapsed_ms": elapsed,
                        }
                        anchor_diags.append(diag)
                        if actual != expected:
                            context_ok = False
                    diagnostics.append(
                        {
                            "context_rows": context,
                            "elapsed_ms": total_elapsed,
                            "anchors": anchor_diags,
                            "ok": context_ok,
                        }
                    )
                    if context_ok:
                        chosen = context
                        break
                if chosen is None:
                    raise RuntimeError(
                        f"no causal context passed event-decision parity: {diagnostics}"
                    )

                # Prove native event append-only and restart behavior on consecutive bars.
                event_steps = max(1, min(int(args.stream_steps), 3))
                base_end = len(features) - event_steps
                if base_end <= max(int(chosen), EVENT_OVERLAP_ROWS):
                    raise RuntimeError(
                        "insufficient feature history for event restart certification"
                    )
                base_features = features.iloc[:base_end].copy()
                bootstrap_stamp = normalize_timestamp(
                    base_features["Datetime"].iloc[-1]
                )
                bootstrap_result = transition_events_live_native(
                    runtime,
                    asset=asset,
                    edge_id=edge_id,
                    features=base_features,
                    context_rows=int(chosen),
                    overlap_rows=EVENT_OVERLAP_ROWS,
                    full_audit_interval_bars=500,
                    candidate_times=[bootstrap_stamp],
                    state=None,
                    certification_hash="certifier_pending",
                    certified_restart_parity=True,
                )
                continuous_state = bootstrap_result["state"]
                restarted_state = pickle.loads(
                    pickle.dumps(
                        continuous_state,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                )
                event_restart_steps: list[dict[str, Any]] = []
                for step_number, end in enumerate(
                    range(base_end + 1, len(features) + 1),
                    start=1,
                ):
                    prefix_features = features.iloc[:end].copy()
                    stamp = normalize_timestamp(
                        prefix_features["Datetime"].iloc[-1]
                    )
                    continuous = transition_events_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        features=prefix_features,
                        context_rows=int(chosen),
                        overlap_rows=EVENT_OVERLAP_ROWS,
                        full_audit_interval_bars=500,
                        candidate_times=[stamp],
                        state=continuous_state,
                        certification_hash="certifier_pending",
                        certified_restart_parity=True,
                    )
                    continuous_state = continuous["state"]
                    restarted_state = pickle.loads(
                        pickle.dumps(
                            restarted_state,
                            protocol=pickle.HIGHEST_PROTOCOL,
                        )
                    )
                    restarted = transition_events_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        features=prefix_features,
                        context_rows=int(chosen),
                        overlap_rows=EVENT_OVERLAP_ROWS,
                        full_audit_interval_bars=500,
                        candidate_times=[stamp],
                        state=restarted_state,
                        certification_hash="certifier_pending",
                        certified_restart_parity=True,
                    )
                    restarted_state = restarted["state"]
                    continuous_identity = event_identity_rows(
                        continuous["events"]
                    )
                    restarted_identity = event_identity_rows(
                        restarted["events"]
                    )
                    proof_flags = (
                        "streaming_parity_passed",
                        "append_only_history",
                        "restart_parity_passed",
                        "event_state_continuity_preserved",
                    )
                    proof_ok = all(
                        continuous["metadata"].get(name) is True
                        for name in proof_flags
                    )
                    restart_match = (
                        continuous_identity == restarted_identity
                    )
                    event_restart_steps.append(
                        {
                            "step": step_number,
                            "timestamp": stamp.isoformat(),
                            "continuous_identity": continuous_identity,
                            "restarted_identity": restarted_identity,
                            "restart_match": restart_match,
                            "proof_flags_passed": proof_ok,
                            "elapsed_ms": float(
                                continuous["metadata"].get("elapsed_ms")
                                or 0.0
                            ),
                        }
                    )
                    if not restart_match or not proof_ok:
                        raise RuntimeError(
                            "event restart/proof parity failed at "
                            f"{stamp}: {event_restart_steps[-1]}"
                        )

                rolling_event_window_rows = min(
                    LIVE_RUNTIME_WINDOW_ROWS,
                    len(features) - event_steps,
                )
                if rolling_event_window_rows <= max(int(chosen), EVENT_OVERLAP_ROWS):
                    raise RuntimeError(
                        "insufficient fixed-width feature history for event certification"
                    )
                rolling_event_base = features.iloc[:rolling_event_window_rows].copy()
                rolling_bootstrap_stamp = normalize_timestamp(
                    rolling_event_base["Datetime"].iloc[-1]
                )
                rolling_bootstrap = transition_events_live_native(
                    runtime,
                    asset=asset,
                    edge_id=edge_id,
                    features=rolling_event_base,
                    context_rows=int(chosen),
                    overlap_rows=EVENT_OVERLAP_ROWS,
                    full_audit_interval_bars=500,
                    candidate_times=[rolling_bootstrap_stamp],
                    state=None,
                    certification_hash="certifier_pending",
                    certified_restart_parity=True,
                )
                rolling_event_state = rolling_bootstrap["state"]
                rolling_event_restart_state = pickle.loads(
                    pickle.dumps(
                        rolling_event_state, protocol=pickle.HIGHEST_PROTOCOL
                    )
                )
                rolling_event_steps: list[dict[str, Any]] = []
                for rolling_step in range(1, event_steps + 1):
                    rolling_features = features.iloc[
                        rolling_step : rolling_step + rolling_event_window_rows
                    ].copy()
                    stamp = normalize_timestamp(
                        rolling_features["Datetime"].iloc[-1]
                    )
                    continuous = transition_events_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        features=rolling_features,
                        context_rows=int(chosen),
                        overlap_rows=EVENT_OVERLAP_ROWS,
                        full_audit_interval_bars=500,
                        candidate_times=[stamp],
                        state=rolling_event_state,
                        certification_hash="certifier_pending",
                        certified_restart_parity=True,
                    )
                    rolling_event_state = continuous["state"]
                    rolling_event_restart_state = pickle.loads(
                        pickle.dumps(
                            rolling_event_restart_state,
                            protocol=pickle.HIGHEST_PROTOCOL,
                        )
                    )
                    restarted = transition_events_live_native(
                        runtime,
                        asset=asset,
                        edge_id=edge_id,
                        features=rolling_features,
                        context_rows=int(chosen),
                        overlap_rows=EVENT_OVERLAP_ROWS,
                        full_audit_interval_bars=500,
                        candidate_times=[stamp],
                        state=rolling_event_restart_state,
                        certification_hash="certifier_pending",
                        certified_restart_parity=True,
                    )
                    rolling_event_restart_state = restarted["state"]
                    identities_match = (
                        event_identity_rows(continuous["events"])
                        == event_identity_rows(restarted["events"])
                    )
                    proof_ok = all(
                        continuous["metadata"].get(name) is True
                        for name in (
                            "streaming_parity_passed",
                            "append_only_history",
                            "restart_parity_passed",
                            "event_state_continuity_preserved",
                        )
                    )
                    rolling_event_steps.append({
                        "step": rolling_step,
                        "timestamp": stamp.isoformat(),
                        "window_rows": rolling_event_window_rows,
                        "restart_match": identities_match,
                        "proof_flags_passed": proof_ok,
                    })
                    if not identities_match or not proof_ok:
                        raise RuntimeError(
                            "fixed-width event continuation failed: "
                            f"{rolling_event_steps[-1]}"
                        )

                revision_rejected = False
                tampered = features.copy()
                numeric_columns = [
                    name
                    for name in tampered.columns
                    if name != "Datetime"
                    and pd.api.types.is_numeric_dtype(tampered[name])
                ]
                if numeric_columns:
                    overlap_position = max(
                        0,
                        len(tampered)
                        - min(EVENT_OVERLAP_ROWS, len(tampered)),
                    )
                    column = numeric_columns[0]
                    value = pd.to_numeric(
                        pd.Series(
                            [tampered.iloc[overlap_position][column]]
                        ),
                        errors="coerce",
                    ).iloc[0]
                    tampered.loc[
                        tampered.index[overlap_position], column
                    ] = (
                        0.0 if pd.isna(value) else float(value)
                    ) + 1.23456789
                    try:
                        transition_events_live_native(
                            runtime,
                            asset=asset,
                            edge_id=edge_id,
                            features=tampered,
                            context_rows=int(chosen),
                            overlap_rows=EVENT_OVERLAP_ROWS,
                            full_audit_interval_bars=500,
                            candidate_times=[
                                normalize_timestamp(
                                    tampered["Datetime"].iloc[-1]
                                )
                            ],
                            state=continuous_state,
                            certification_hash="certifier_pending",
                            certified_restart_parity=True,
                        )
                    except NativeEventError:
                        revision_rejected = True
                if not revision_rejected:
                    raise RuntimeError(
                        "historical feature revision was not rejected by "
                        "native event state"
                    )

                row = {
                    "feature_parity_passed": True,
                    "feature_parity": edge_feature_check,
                    "feature_execution_mode": feature_execution_mode,
                    "event_parity_passed": True,
                    "event_execution_mode": NATIVE_EVENT_MODE,
                    "native_event_eligible": True,
                    "full_authority_required": False,
                    "force_full_event_replay": False,
                    "append_only_history_passed": True,
                    "restart_parity_passed": True,
                    "event_state_continuity_passed": True,
                    "rolling_window_event_parity_passed": True,
                    "rolling_window_event_rows": int(rolling_event_window_rows),
                    "rolling_window_event_steps": rolling_event_steps,
                    "historical_revision_rejected": True,
                    "event_restart_steps": event_restart_steps,
                    "context_rows": int(chosen),
                    "overlap_rows": EVENT_OVERLAP_ROWS,
                    "audit_anchor_offsets": valid_anchors,
                    "candidate_full_confirmation_required": True,
                    "full_audit_interval_bars": 500,
                    "context_trials": diagnostics,
                    "certification_reason": (
                        "bounded causal context and restart-safe native event "
                        "checkpoint passed"
                    ),
                    "total_elapsed_ms": (
                        time.perf_counter() - edge_started
                    ) * 1000.0,
                }
                if not edge_certification_passed(row):
                    raise RuntimeError(
                        f"{edge_id}: native event classification failed contract validation"
                    )
                payload["edges"][edge_id] = row
                print(
                    f"[PASS] {index:02d}/33 {edge_id} "
                    f"event_context={chosen}"
                )
        except Exception as exc:
            failure = {
                "scope": "edge",
                "id": edge_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
            failures.append(failure)
            payload["edges"][edge_id] = {
                "feature_parity_passed": False,
                "event_parity_passed": False,
                "event_execution_mode": "uncertified",
                "native_event_eligible": False,
                "failure": failure["error"],
            }
            print(
                f"[FAIL] {index:02d}/33 {edge_id}: "
                f"{failure['error']}"
            )
        payload["failures"] = failures
        payload["updated_at"] = utc_now()
        write_payload(args.output, payload)

    passed_assets = sum(
        all(
            bool((payload.get("assets") or {}).get(a, {}).get(name))
            for name in (
                "feature_parity_passed",
                "exact_schema_parity_passed",
                "append_only_history_passed",
                "restart_parity_passed",
            )
        )
        for a in assets
    )
    passed_edges = sum(
        edge_certification_passed(
            dict((payload.get("edges") or {}).get(str(e["edge_id"])) or {})
        )
        for e in selected
    )
    native_event_edges = sum(
        edge_certification_passed(dict(row or {}))
        and str((row or {}).get("event_execution_mode")) == NATIVE_EVENT_MODE
        for row in (payload.get("edges") or {}).values()
    )
    full_authority_event_edges = sum(
        edge_certification_passed(dict(row or {}))
        and str((row or {}).get("event_execution_mode")) == FULL_AUTHORITY_EVENT_MODE
        for row in (payload.get("edges") or {}).values()
    )
    payload["passed_asset_count"] = int(passed_assets)
    payload["passed_edge_count"] = int(passed_edges)
    payload["native_event_edge_count"] = int(native_event_edges)
    payload["full_authority_event_edge_count"] = int(full_authority_event_edges)
    payload["failures"] = failures
    payload["updated_at"] = utc_now()
    payload["status"] = "PASS" if passed_assets == len(assets) and passed_edges == len(selected) else "FAIL"
    if failures:
        payload["failure"] = f"{len(failures)} certification scope(s) failed; inspect failures[]"
    else:
        payload.pop("failure", None)
    write_payload(args.output, payload)
    clear_certification_cache()
    print(
        f"Certification status={payload['status']} assets={passed_assets}/{len(assets)} "
        f"edges={passed_edges}/{len(selected)} "
        f"native_events={native_event_edges} "
        f"full_authority_events={full_authority_event_edges}"
    )
    print(f"Certification written: {args.output}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
