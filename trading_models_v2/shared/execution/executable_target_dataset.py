"""Quantara MLDP Task 2A: pre-model exact Bid/Ask executable-outcome engine.

This is one shared research station for every compatible Quantara edge.

AFML Chapter 3 and MLAM Chapter 5 motivate path-dependent financial labels and
meta-labeling when a primary rule/model supplies side. Quantara's exact
historical Bid/Ask replay is a production extension, not a canonical AFML/MLAM
routine. Task 2A reuses Quantara's existing resolver before secondary-model
training, without duplicating its execution mathematics.

Task 2A is AUDIT-ONLY. It does not authorize Step 01-09 to train on the new
target. Unresolved/no-timely-entry observations are never silently coerced to
losses or label 0: AFML/MLAM do not define Quantara's quote-feed/data-gap/no-fill
semantics, so that population policy must be audited and frozen in Task 2B.

LABEL / ECONOMICS SPLIT (2026-10-17, user-approved after a 9-edge head-to-head; ledger
LABEL_DEFINITION_DECISION_2026-10-17, docs/mldp/label_experiment_2026-10-17/):
  * quote_executable_meta_label (training target + Gate D) = triple barrier replayed on the DUKASCOPY MID
    path (zero spread, no carry, no entry delay) -> label_r / label_t1. Firm-neutral: AFML Snippet 3.7
    (pp.50-51) labels from price; costs are kept out of the series (AFML 2.4.1, p.34). Dukascopy's own
    bid/ask is NOT used for labels: its fixed-$ spread made labels encode the era (false Gate D passes).
  * quote_executable_r / executed_r / quote_executable_t1 / entry_* / exit_* (economics: Step 04/05
    thresholds, allocator) = replay at the RESEARCH FIRM's broker cost model (portfolio_allocator/config/
    broker_repricing.json "research_firm"; spread + swap per rollover + commission) with the measured live
    entry delay ("entry_delay_ms"). On a firm switch the allocator re-prices (00d); edge folders untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import hashlib
import json
import time

import numpy as np
import pandas as pd

from shared.edges.edge_layout import get_edge_layout_from_edge_root
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.edge_yaml_contract import validate_edge_yaml_file
from shared.edges.trade_geometry import load_trade_geometry
from shared.execution import oof_exact_execution as exact
from shared.labels.outcome_ontology import (
    ONTOLOGY_VERSION,
    canonicalize_legacy_outcomes,
    label_from_r,
    validate_layer,
)

ENGINE_VERSION = "quantara_mldp_task2a_executable_target_engine_v1"
TARGET_LAYER = "quote_executable"
EXPECTED_EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"
TRAINING_AUTHORITY = "AUDIT_ONLY_TASK2A_NOT_YET_AUTHORIZED"

TIME_COL = "Datetime"
EVENT_VOL_COL = "Event_Vol"
SIDE_COL = "side"
SOURCE_ROW_COL = "source_event_row_number"
LABEL_SOURCE = "dukascopy_mid_zero_spread_no_carry_0ms"
LABEL_R_COL = "label_r"
LABEL_T1_COL = "label_t1"
_V2_ROOT = Path(__file__).resolve().parents[2]
BROKER_CONFIG = _V2_ROOT / "portfolio_allocator" / "config" / "broker_repricing.json"
BROKER_PROFILES = _V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles"


class ExecutableTargetEngineError(RuntimeError):
    """Fail-closed Task-2A executable-target engine error."""


@dataclass(frozen=True)
class TargetArtifactPaths:
    resolution_csv: Path
    manifest_json: Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path.resolve()).replace("\\", "/")


def _model_rows_path(layout: Any) -> Path:
    return layout.research_dataset_dir / f"{layout.edge_stem}_model_rows.csv"


def _model_rows_manifest_path(layout: Any) -> Path:
    return layout.research_dataset_dir / "model_row_manifest.json"


def target_artifact_paths(layout: Any) -> TargetArtifactPaths:
    return TargetArtifactPaths(
        resolution_csv=layout.research_dataset_dir
        / f"{layout.edge_stem}_mldp_execution_resolution.csv",
        manifest_json=layout.research_dataset_dir
        / f"{layout.edge_stem}_mldp_executable_target_manifest.json",
    )


def discover_edge_roots(
    v2_root: str | Path,
    *,
    asset: str | None = None,
    require_model_rows: bool = False,
) -> list[Path]:
    """Discover side-specialized packages; no asset/edge list is hard-coded."""
    root = Path(v2_root).resolve()
    assets_root = root / "assets"
    if not assets_root.exists():
        raise ExecutableTargetEngineError(f"Missing assets root: {assets_root}")

    wanted = None if asset is None else str(asset).strip().upper()
    out: list[Path] = []
    for asset_dir in sorted(
        (p for p in assets_root.iterdir() if p.is_dir()), key=lambda p: p.name
    ):
        if wanted is not None and asset_dir.name.upper() != wanted:
            continue
        edges_root = asset_dir / "edges"
        if not edges_root.exists():
            continue
        for edge_root in sorted(
            (p for p in edges_root.iterdir() if p.is_dir()), key=lambda p: p.name
        ):
            required = (
                edge_root / "edge.yaml",
                edge_root / "runtime.py",
                edge_root / "model_rows.py",
            )
            if not all(p.exists() for p in required):
                continue
            if require_model_rows:
                stem = f"{asset_dir.name.lower()}_{edge_root.name}"
                model_rows = (
                    edge_root
                    / "artifacts"
                    / "research_dataset"
                    / f"{stem}_model_rows.csv"
                )
                if not model_rows.exists():
                    continue
            out.append(edge_root.resolve())
    return out


def compatibility_report(edge_root: str | Path) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    paths = target_artifact_paths(layout)
    checks = {
        "edge_yaml": layout.edge_yaml.exists(),
        "runtime_py": layout.runtime_py.exists(),
        "model_rows_py": (layout.edge_root / "model_rows.py").exists(),
        "gross_model_rows_csv": _model_rows_path(layout).exists(),
        "gross_model_rows_manifest": _model_rows_manifest_path(layout).exists(),
    }
    return {
        "edge_root": str(layout.edge_root),
        "edge_id": layout.edge_id,
        "asset": layout.asset,
        "side": layout.side_name,
        "compatible_for_resolution": bool(all(checks.values())),
        "checks": checks,
        "source_model_rows": _rel(_model_rows_path(layout), layout.v2_root),
        "task2a_resolution_output": _rel(paths.resolution_csv, layout.v2_root),
        "task2a_manifest_output": _rel(paths.manifest_json, layout.v2_root),
    }


def _event_id(edge_id: str, t0: Any, side: int, geometry_hash: str) -> str:
    ts = pd.Timestamp(t0)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    payload = f"{edge_id}|{int(ts.value)}|{int(side)}|{geometry_hash}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_gross_model_rows(
    edge_root: str | Path, geometry_hash: str
) -> tuple[Any, pd.DataFrame, Path]:
    """Load every gross model row; do not filter using future outcomes."""
    layout = get_edge_layout_from_edge_root(edge_root)
    path = _model_rows_path(layout)
    if not path.exists():
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: missing gross model rows: {path}. "
            "Task 2A does not regenerate upstream model rows."
        )

    frame = pd.read_csv(path)
    if frame.empty:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: gross model rows are empty: {path}"
        )
    if TIME_COL not in frame.columns:
        frame = frame.rename(columns={frame.columns[0]: TIME_COL})

    try:
        frame = canonicalize_legacy_outcomes(frame, copy=True, strict=True)
    except Exception as exc:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: gross outcome canonicalization failed: {exc}"
        ) from exc

    if SIDE_COL not in frame.columns:
        frame[SIDE_COL] = int(layout.side_value)

    required = [
        TIME_COL,
        EVENT_VOL_COL,
        SIDE_COL,
        "gross_r",
        "gross_meta_label",
        "gross_t1",
    ]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: source model rows missing Task-2A fields: {missing}"
        )

    frame[TIME_COL] = to_datetime_iso(frame[TIME_COL], utc=True, errors="coerce")
    frame["gross_t1"] = to_datetime_iso(frame["gross_t1"], utc=True, errors="coerce")
    for col in (EVENT_VOL_COL, SIDE_COL, "gross_r", "gross_meta_label"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    invalid = {c: int(frame[c].isna().sum()) for c in required if frame[c].isna().any()}
    if invalid:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: invalid required source values: {invalid}"
        )
    if bool(frame[TIME_COL].duplicated().any()):
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: event timestamps must be unique for one side-specialized edge."
        )
    if bool((frame[SIDE_COL].astype(int) != int(layout.side_value)).any()):
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: source rows violate the package side."
        )
    ev = frame[EVENT_VOL_COL].to_numpy(dtype="float64")
    if not (np.isfinite(ev) & (ev > 0.0)).all():
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: Event_Vol must be finite and > 0."
        )

    try:
        validate_layer(frame, "gross", t0_column=TIME_COL)
    except Exception as exc:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: invalid gross outcome layer: {exc}"
        ) from exc

    frame = frame.sort_values(TIME_COL).reset_index(drop=True)
    frame[SOURCE_ROW_COL] = np.arange(len(frame), dtype="int64")
    frame["event_id"] = [
        _event_id(layout.edge_id, t0, int(side), geometry_hash)
        for t0, side in zip(frame[TIME_COL], frame[SIDE_COL])
    ]
    frame["trgt"] = frame[EVENT_VOL_COL].astype("float64")
    frame["geometry_hash"] = str(geometry_hash)
    return layout, frame, path


def assemble_executable_outcome_frame(
    gross_frame: pd.DataFrame,
    execution_frame: pd.DataFrame,
) -> pd.DataFrame:
    """Attach execution results without inventing outcomes for unresolved rows."""
    gross = canonicalize_legacy_outcomes(gross_frame, copy=True, strict=True)
    if SOURCE_ROW_COL not in gross.columns:
        gross[SOURCE_ROW_COL] = np.arange(len(gross), dtype="int64")
    if "source_oof_row_number" not in execution_frame.columns:
        raise ExecutableTargetEngineError(
            "Execution frame missing source_oof_row_number identity."
        )

    execution = execution_frame.copy()
    execution["source_oof_row_number"] = pd.to_numeric(
        execution["source_oof_row_number"], errors="coerce"
    )
    if execution["source_oof_row_number"].isna().any():
        raise ExecutableTargetEngineError("Execution source identity contains NaN.")
    execution["source_oof_row_number"] = execution["source_oof_row_number"].astype(
        "int64"
    )
    if execution["source_oof_row_number"].duplicated().any():
        raise ExecutableTargetEngineError("Execution source identity is duplicated.")

    expected = set(gross[SOURCE_ROW_COL].astype(int).tolist())
    actual = set(execution["source_oof_row_number"].astype(int).tolist())
    if expected != actual:
        raise ExecutableTargetEngineError(
            f"Gross/execution row identity mismatch: "
            f"missing={sorted(expected-actual)[:10]} extra={sorted(actual-expected)[:10]}"
        )

    execution = execution.set_index("source_oof_row_number").reindex(
        gross[SOURCE_ROW_COL].astype(int)
    )
    execution.index = gross.index

    out = gross.copy()
    protected = {
        TIME_COL,
        SIDE_COL,
        EVENT_VOL_COL,
        "realized_r",
        "meta_label",
        "t1",
        "gross_r",
        "gross_meta_label",
        "gross_t1",
        SOURCE_ROW_COL,
        "event_id",
        "trgt",
        "geometry_hash",
    }
    for col in execution.columns:
        if col not in protected:
            out[col] = execution[col].to_numpy()

    if "execution_status" not in out.columns:
        raise ExecutableTargetEngineError("Execution results lack execution_status.")
    resolved = out["execution_status"].astype(str).eq("resolved")

    executed_r = pd.to_numeric(
        out["executed_r"]
        if "executed_r" in out.columns
        else pd.Series(np.nan, index=out.index),
        errors="coerce",
    )
    exit_time = to_datetime_iso(
        out["exit_time"]
        if "exit_time" in out.columns
        else pd.Series(pd.NaT, index=out.index),
        utc=True,
        errors="coerce",
    )

    if LABEL_R_COL in out.columns:  # 2026-10-17 label/economics split: eligible only when BOTH replays resolved
        resolved = resolved & pd.to_numeric(out[LABEL_R_COL], errors="coerce").notna()
    out["quote_executable_r"] = np.where(resolved, executed_r, np.nan)
    out["quote_executable_t1"] = exit_time.where(resolved, pd.NaT)
    labels = pd.Series(
        pd.array([pd.NA] * len(out), dtype="Int64"), index=out.index
    )

    if bool(resolved.any()):
        resolved_r = pd.to_numeric(
            out.loc[resolved, "quote_executable_r"], errors="coerce"
        )
        if resolved_r.isna().any() or not np.isfinite(
            resolved_r.to_numpy(dtype="float64")
        ).all():
            raise ExecutableTargetEngineError(
                "Resolved rows contain invalid quote_executable_r."
            )
        if out.loc[resolved, "quote_executable_t1"].isna().any():
            raise ExecutableTargetEngineError(
                "Resolved rows contain invalid quote_executable_t1."
            )
        label_source_r = (pd.to_numeric(out.loc[resolved, LABEL_R_COL], errors="coerce")
                          if LABEL_R_COL in out.columns else resolved_r)
        labels.loc[resolved] = label_from_r(label_source_r.to_numpy(dtype="float64"))

    out["quote_executable_meta_label"] = labels
    out["mldp_target_eligible"] = resolved.astype(bool)

    gross_label = pd.to_numeric(
        out["gross_meta_label"], errors="coerce"
    ).astype("Int64")
    out["gross_to_quote_label_flip"] = (
        resolved
        & out["quote_executable_meta_label"].notna()
        & (gross_label != out["quote_executable_meta_label"])
    )

    if bool(resolved.any()):
        try:
            split = LABEL_R_COL in out.columns
            validate_layer(
                out.loc[resolved].copy(),
                "quote_executable",
                t0_column=TIME_COL,
                require_label=not split,
            )
            if split:
                exp_lbl = label_from_r(pd.to_numeric(out.loc[resolved, LABEL_R_COL]).to_numpy(dtype="float64"))
                got = out.loc[resolved, "quote_executable_meta_label"].astype("int64").to_numpy()
                if not np.array_equal(got, exp_lbl):
                    raise ExecutableTargetEngineError("quote_executable_meta_label != int(label_r > 0)")
        except Exception as exc:
            raise ExecutableTargetEngineError(
                f"Resolved quote-executable layer is invalid: {exc}"
            ) from exc

    out["mldp_training_authority"] = TRAINING_AUTHORITY
    return out


def _catalog_provenance(
    catalog: list[dict[str, Any]],
    *,
    v2_root: Path,
    hash_tick_contents: bool,
) -> tuple[list[dict[str, Any]], str]:
    records: list[dict[str, Any]] = []
    for rec in catalog:
        path = Path(rec["path"]).resolve()
        stat = path.stat()
        item: dict[str, Any] = {
            "path": _rel(path, v2_root),
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "end_date": str(rec.get("end_date", "")),
        }
        if hash_tick_contents:
            item["sha256"] = sha256_file(path)
        records.append(item)
    blob = json.dumps(
        records, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return records, hashlib.sha256(blob).hexdigest()


def _numeric_summary(series: pd.Series) -> dict[str, float | int | None]:
    x = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype="float64")
    if not len(x):
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "positive_rate": None,
        }
    return {
        "count": int(len(x)),
        "mean": float(np.mean(x)),
        "median": float(np.median(x)),
        "positive_rate": float(np.mean(x > 0.0)),
    }


def research_cost_setup(asset: str) -> tuple[str, int, dict[str, Any], Path]:
    """Research firm + entry delay (portfolio_allocator/config/broker_repricing.json) and that firm's cost model."""
    if not BROKER_CONFIG.exists():
        raise ExecutableTargetEngineError(f"Missing {BROKER_CONFIG} (research_firm / entry_delay_ms).")
    cfg = json.loads(BROKER_CONFIG.read_text(encoding="utf-8"))
    firm = str(cfg.get("research_firm") or cfg.get("firm") or "")
    delay = int(cfg.get("entry_delay_ms", 5700))
    cm_path = BROKER_PROFILES / f"{firm}_cost_models" / f"{asset}.json"
    if not firm or not cm_path.exists():
        raise ExecutableTargetEngineError(
            f"No broker cost model for research firm {firm!r}/{asset}: {cm_path} "
            "(run portfolio_allocator/00c_build_broker_cost_model.py --firm <firm>)."
        )
    return firm, delay, json.loads(cm_path.read_text(encoding="utf-8")), cm_path


def _mid_cost_model(cm: dict[str, Any]) -> dict[str, Any]:
    mid = dict(cm, model="FIXED_POINTS", fixed_points=0.0, commission_bps_rt=0.0)
    mid["swap"] = dict(cm.get("swap", {}), mode=0, long=0.0, short=0.0)
    return mid


def _replay(gross: pd.DataFrame, cm: dict[str, Any], delay_ms: int, *, label: str, **kw: Any) -> pd.DataFrame:
    """Exact Task-2A replay with Bid/Ask = Dukascopy MID +/- the cost model's spread (zero for the mid label)."""
    from shared.research.v3_horizon_lab import _xef_broker_loader
    original = exact._load_tick_file
    exact._load_tick_file = _xef_broker_loader(original, cm)
    rows: list[dict[str, Any]] = []
    started = time.time()
    try:
        for i, (_, source) in enumerate(gross.iterrows(), start=1):
            r = source.copy()
            r["source_oof_row_number"] = int(source[SOURCE_ROW_COL])
            rows.append(exact._resolve_one(r, entry_delay_ms=delay_ms, **kw))
            if i % 500 == 0 or i == len(gross):
                print(f"  [{label}] {i:,}/{len(gross):,} | elapsed={time.time()-started:.1f}s", flush=True)
    finally:
        exact._load_tick_file = original
    return pd.DataFrame(rows)


def _apply_carry(ex: pd.DataFrame, cm: dict[str, Any]) -> pd.DataFrame:
    """Swap per rollover crossed + commission (same function Gate C / 00d use); executed_r becomes net."""
    from shared.research.v3_horizon_lab import _xef_apply_carry_costs
    ok = ex["execution_status"].astype(str).eq("resolved")
    carry = _xef_apply_carry_costs(pd.DataFrame({
        "status": np.where(ok, "resolved", "unresolved"), "entry_price": ex.get("entry_price"),
        "sl_dist": ex.get("stop_distance_price"), "side": ex[SIDE_COL], "entry_ts": ex.get("entry_time"),
        "exit_ts": ex.get("exit_time"), "exec_r": ex.get("executed_r")}), cm)
    out = ex.copy()
    out["executed_r_pre_carry"] = carry.get("exec_r_pre_carry")
    out["swap_R"] = carry.get("swap_R")
    out["commission_R"] = carry.get("commission_R")
    out["swap_nights"] = carry.get("swap_nights")
    out["executed_r"] = carry["exec_r"]
    return out


def resolve_edge_executable_targets(
    edge_root: str | Path,
    *,
    write: bool = True,
    strict_coverage: bool = False,
    hash_tick_contents: bool = False,
) -> dict[str, Any]:
    """Resolve pre-model exact Bid/Ask outcomes for one arbitrary edge."""
    if exact.EXECUTION_CONTRACT_VERSION != EXPECTED_EXECUTION_CONTRACT_VERSION:
        raise ExecutableTargetEngineError(
            "Exact execution contract changed; Task 2A must be re-audited. "
            f"Found {exact.EXECUTION_CONTRACT_VERSION!r}."
        )

    layout = get_edge_layout_from_edge_root(edge_root)
    validate_edge_yaml_file(layout.edge_root)
    geometry = load_trade_geometry(
        layout.edge_yaml,
        expected_edge_id=layout.edge_id,
        expected_side_name=layout.side_name,
    )
    layout, gross, model_rows_path = load_gross_model_rows(
        layout.edge_root, str(geometry["geometry_hash"])
    )

    runtime, model_rows_module = exact._import_edge_modules(layout.edge_root)
    max_signal_age_seconds = exact._effective_live_max_signal_age_seconds(
        layout.v2_root
    )
    bars_dt, bars_close = exact._load_bars(runtime)
    catalog = exact._build_tick_catalog(model_rows_module)

    print("=" * 100, flush=True)
    print(
        f"MLDP TASK 2A - PRE-MODEL EXACT EXECUTION AUDIT - {layout.edge_id}",
        flush=True,
    )
    print("=" * 100, flush=True)
    print(f"Gross events : {len(gross):,}", flush=True)
    _hv_display = (
        f"{geometry['vertical_horizon_hours']:g}h" if geometry.get("calendar_time")
        else f"{geometry['vertical_horizon_bars']}bars"
    )
    print(
        f"Geometry     : Hv={_hv_display} "
        f"PT={geometry['pt_multiplier']:g} SL={geometry['sl_multiplier']:g}",
        flush=True,
    )
    print(f"Signal age   : {max_signal_age_seconds}s", flush=True)
    print(
        f"Target layer : {TARGET_LAYER} (AUDIT ONLY; training not authorized)",
        flush=True,
    )

    # Precompute ONCE, not per-event -- pandas' DatetimeIndex.asi8 resolution
    # is NOT guaranteed nanoseconds across pandas versions (confirmed
    # 2026-09-25: pandas 2.3.3/venv39 returns ns, pandas 3.0.2/venv312 --
    # this module's own venv -- returns microseconds by default).
    # calendar_time_barriers.py hardcodes a nanosecond assumption, so an
    # un-normalized .asi8 would silently compute deadlines ~1000x too far
    # out (nearly every event unresolved).
    bars_dt_ns = bars_dt.as_unit("ns").asi8 if bool(geometry.get("calendar_time")) else None

    firm, delay_ms, cost_model, cost_model_path = research_cost_setup(layout.asset)
    if max_signal_age_seconds * 1000 <= delay_ms:
        raise ExecutableTargetEngineError(
            f"max signal age {max_signal_age_seconds}s <= entry delay {delay_ms}ms: every entry would be stale")
    print(f"Economics    : {firm} broker cost model ({cost_model.get('model')}), entry delay {delay_ms} ms, swap+commission", flush=True)
    print(f"Label        : {LABEL_SOURCE}", flush=True)
    replay_kw = dict(bars_dt=bars_dt, bars_close=bars_close, catalog=catalog, geometry=geometry,
                     max_signal_age_seconds=max_signal_age_seconds, bars_dt_ns=bars_dt_ns)
    execution = _apply_carry(_replay(gross, cost_model, delay_ms, label="economics", **replay_kw), cost_model)
    mid = _replay(gross, _mid_cost_model(cost_model), 0, label="mid-label", **replay_kw)
    mid_ok = mid["execution_status"].astype(str).eq("resolved")
    execution[LABEL_R_COL] = pd.to_numeric(mid["executed_r"], errors="coerce").where(mid_ok).to_numpy()
    execution[LABEL_T1_COL] = to_datetime_iso(mid["exit_time"], utc=True, errors="coerce").where(mid_ok).to_numpy()
    combined = assemble_executable_outcome_frame(gross, execution)
    resolved = combined["mldp_target_eligible"].astype(bool)
    total = int(len(combined))
    resolved_count = int(resolved.sum())
    unresolved_count = total - resolved_count
    status_counts = (
        combined["execution_status"]
        .astype(str)
        .value_counts(dropna=False)
        .to_dict()
    )

    paths = target_artifact_paths(layout)
    tick_records, tick_catalog_digest = _catalog_provenance(
        catalog,
        v2_root=layout.v2_root,
        hash_tick_contents=hash_tick_contents,
    )

    quote_r = (
        combined.loc[resolved, "quote_executable_r"]
        if resolved_count
        else pd.Series(dtype=float)
    )
    flip_count = (
        int(combined.loc[resolved, "gross_to_quote_label_flip"].sum())
        if resolved_count
        else 0
    )

    resolver_path = Path(exact.__file__).resolve()
    import shared.labels.outcome_ontology as ontology_module
    ontology_path = Path(ontology_module.__file__).resolve()
    live_settings_path = layout.v2_root / "portfolio_live" / "live_settings.py"

    manifest: dict[str, Any] = {
        "schema_version": ENGINE_VERSION,
        "created_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "task": "TASK2A_PRE_MODEL_EXECUTABLE_TARGET_AUDIT",
        "training_authority": TRAINING_AUTHORITY,
        "task2b_may_consume_for_training": False,
        "edge_id": layout.edge_id,
        "asset": layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": int(layout.side_value),
        "target_layer_candidate": TARGET_LAYER,
        "outcome_ontology_version": ONTOLOGY_VERSION,
        "execution_contract_version": exact.EXECUTION_CONTRACT_VERSION,
        "geometry_hash": str(geometry["geometry_hash"]),
        "trade_geometry": geometry,
        "label_source": LABEL_SOURCE,
        "label_definition": (
            "quote_executable_meta_label = int(label_r > 0); label_r = exact replay on the Dukascopy mid "
            "(zero spread, no carry, 0 ms entry delay)"
        ),
        "economics_source": {
            "firm": firm,
            "cost_model": _rel(cost_model_path, layout.v2_root),
            "cost_model_sha256": sha256_file(cost_model_path),
            "entry_delay_ms": int(delay_ms),
            "definition": (
                "quote_executable_r = executed_r = net R at the research firm's broker cost model "
                "(spread + swap per rollover + commission) with the live entry delay"
            ),
        },
        "label_r_summary_resolved_only": (
            _numeric_summary(combined.loc[resolved, LABEL_R_COL]) if resolved_count else None
        ),
        "live_signal_age_max_seconds": int(max_signal_age_seconds),
        "event_count": total,
        "resolved_count": resolved_count,
        "unresolved_count": int(unresolved_count),
        "coverage_rate": float(resolved_count / total) if total else 0.0,
        "execution_status_counts": {
            str(k): int(v) for k, v in status_counts.items()
        },
        "gross_r_summary": _numeric_summary(combined["gross_r"]),
        "quote_executable_r_summary_resolved_only": _numeric_summary(quote_r),
        "gross_to_quote_label_flip_count_resolved_only": flip_count,
        "gross_to_quote_label_flip_rate_resolved_only": (
            float(flip_count / resolved_count) if resolved_count else None
        ),
        "unresolved_target_policy": (
            "NO_LABEL_ASSIGNED. Task 2A does not coerce stale/no-entry/"
            "data-gap/right-censored rows to 0 or to a loss. AFML/MLAM do "
            "not define these Quantara execution/data semantics; Task 2B "
            "must freeze the universal population rule first."
        ),
        "source_model_rows": _rel(model_rows_path, layout.v2_root),
        "source_model_rows_sha256": sha256_file(model_rows_path),
        "source_model_rows_manifest": _rel(
            _model_rows_manifest_path(layout), layout.v2_root
        ),
        "source_model_rows_manifest_sha256": (
            sha256_file(_model_rows_manifest_path(layout))
            if _model_rows_manifest_path(layout).exists()
            else None
        ),
        "edge_yaml_sha256": sha256_file(layout.edge_yaml),
        "exact_execution_resolver": _rel(resolver_path, layout.v2_root),
        "exact_execution_resolver_sha256": sha256_file(resolver_path),
        "outcome_ontology_sha256": sha256_file(ontology_path),
        "live_settings_sha256": sha256_file(live_settings_path),
        "tick_provenance_level": (
            "sha256_content"
            if hash_tick_contents
            else "metadata_signature_only"
        ),
        "tick_catalog_metadata_digest": tick_catalog_digest,
        "tick_catalog": tick_records,
        "execution_semantics": {
            "entry": (
                "first valid quote strictly after event; LONG Ask / SHORT Bid"
            ),
            "horizontal_distance": (
                "signal_mid * Event_Vol * multiplier frozen at event, then "
                "PT/SL levels re-anchored around executable entry"
            ),
            "exit": (
                "LONG Bid / SHORT Ask; first PT/SL touch before vertical "
                "deadline; vertical uses first valid quote strictly after deadline"
            ),
            "spread_embedded": True,
            "label_vs_economics": (
                "label on the Dukascopy mid; economics at the research firm's modelled bid/ask + carry"
            ),
        },
        "outputs": {
            "resolution_csv": _rel(paths.resolution_csv, layout.v2_root),
            "manifest_json": _rel(paths.manifest_json, layout.v2_root),
        },
    }

    if write:
        paths.resolution_csv.parent.mkdir(parents=True, exist_ok=True)
        tmp_csv = paths.resolution_csv.with_suffix(
            paths.resolution_csv.suffix + ".tmp"
        )
        tmp_json = paths.manifest_json.with_suffix(
            paths.manifest_json.suffix + ".tmp"
        )
        combined.to_csv(tmp_csv, index=False)
        manifest["resolution_csv_sha256"] = sha256_file(tmp_csv)
        tmp_json.write_text(
            json.dumps(
                manifest, indent=2, sort_keys=True, default=str
            )
            + "\n",
            encoding="utf-8",
        )
        tmp_csv.replace(paths.resolution_csv)
        tmp_json.replace(paths.manifest_json)

    print("", flush=True)
    print(
        f"Resolution coverage: {resolved_count:,}/{total:,} "
        f"({manifest['coverage_rate']:.6f})",
        flush=True,
    )
    print(f"Statuses: {manifest['execution_status_counts']}", flush=True)
    qsum = manifest["quote_executable_r_summary_resolved_only"]
    if qsum["count"]:
        lsum = manifest["label_r_summary_resolved_only"] or {}
        if lsum.get("count"):
            print(
                f"Mid-path label R: mean={lsum['mean']:+.6f} positive_rate={lsum['positive_rate']:.4f}",
                flush=True,
            )
        print(
            f"Resolved quote-executable R: mean={qsum['mean']:+.6f} "
            f"median={qsum['median']:+.6f} "
            f"positive_rate={qsum['positive_rate']:.4f}",
            flush=True,
        )
    print(f"Training authority: {TRAINING_AUTHORITY}", flush=True)
    if write:
        print(f"Saved: {paths.resolution_csv}", flush=True)
        print(f"Saved: {paths.manifest_json}", flush=True)

    if strict_coverage and unresolved_count:
        raise ExecutableTargetEngineError(
            f"{layout.edge_id}: strict coverage requested but "
            f"{unresolved_count} events are unresolved."
        )
    return manifest
