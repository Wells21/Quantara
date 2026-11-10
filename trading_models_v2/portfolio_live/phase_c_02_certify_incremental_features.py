"""Offline certification for the Phase C.2 compact incremental feature engine.

This script never changes live routing.  It compares the candidate engine with
all current selected authoritative runtime implementation groups, then performs
sequential/restart and rolling-6,000-window checks for every selected asset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

HERE = Path(__file__).resolve().parent
DEFAULT_V2_ROOT = HERE.parent
if str(DEFAULT_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_V2_ROOT))

from shared.features.afml_ch17_structural_breaks import structural_break_features
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.features.afml_ch18_entropy import ENTROPY_COLUMNS, ENTROPY_WINDOW, entropy_row, load_entropy_encoding
from shared.edges.phase_c_incremental_feature_engine import (
    ENGINE_VERSION,
    FFD_COLUMNS,
    REGIME_COLUMNS,
    addon_asset_features,
    bootstrap_compact_state,
    deserialize_compact_state,
    implementation_fingerprint,
    load_phase_c1_contract,
    required_asset_features,
    serialize_compact_state,
    state_observability,
    update_one,
)
from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry

DEFAULT_C1 = HERE / "config" / "phase_c" / "feature_contract.json"
DEFAULT_MANIFEST = HERE / "live_manifest.json"
DEFAULT_OUTPUT = HERE / "config" / "phase_c" / "incremental_feature_certification.json"

# These features have authoritative batch implementations whose floating result
# depends measurably on the bounded input horizon. Production Phase B supplies a
# 6,000-bar feature window, so C.2 certifies them against that exact horizon.
# The AFML Ch.17 structural-break features (SADF_ct_50, QADF_*, CADF_*, CSW_*, SMT_*) are NOT
# horizon-sensitive since D061: they are computed at every bar on the 50 closes
# ending at that bar, so they are compared with the research batch every step and,
# in addition, with an independent oracle (STRUCTURAL_BREAK_ORACLE_FEATURES): the
# research rule structural_break_features() on the same rolling window.
# The AFML Ch.18 entropy features (ENTROPY_COLUMNS, D066-D069) are compared the same way: with the
# research batch every step and with the research rule entropy_row() on the last ENTROPY_WINDOW + 1 closes.
HORIZON_SENSITIVE_CORE_FEATURES = (
    "STC",
    "Close_FFD_Z_20",
    "Close_FFD_Z_50",
)
STRUCTURAL_BREAK_ORACLE_FEATURES = {
    "SADF_ct_50": "SADF",
    "QADF_ct_50": "QADF",
    "QADF_Dispersion_ct_50": "QADF_Dispersion",
    "CADF_ct_50": "CADF",
    "CADF_Dispersion_ct_50": "CADF_Dispersion",
    "CSW_Up_50": "CSW_Up",
    "CSW_Down_50": "CSW_Down",
    "SMT_Poly1_phi0_50": "SMT_Poly1_phi0",
    "SMT_Poly1_phi05_50": "SMT_Poly1_phi05",
    "SMT_Poly1_phi1_50": "SMT_Poly1_phi1",
    "SMT_Poly2_phi0_50": "SMT_Poly2_phi0",
    "SMT_Poly2_phi05_50": "SMT_Poly2_phi05",
    "SMT_Poly2_phi1_50": "SMT_Poly2_phi1",
    "SMT_Exp_phi0_50": "SMT_Exp_phi0",
    "SMT_Exp_phi05_50": "SMT_Exp_phi05",
    "SMT_Exp_phi1_50": "SMT_Exp_phi1",
    "SMT_Power_phi0_50": "SMT_Power_phi0",
    "SMT_Power_phi05_50": "SMT_Power_phi05",
    "SMT_Power_phi1_50": "SMT_Power_phi1",
}


def _shifted_ffd_z_latest(values: pd.Series, window: int) -> float:
    """Independent oracle matching edge feature_expansion.shifted_zscore."""
    series = pd.to_numeric(values, errors="coerce").astype("float64").reset_index(drop=True)
    mean = series.rolling(int(window)).mean().shift(1)
    std = series.rolling(int(window)).std().shift(1)
    result = (series - mean) / (std + 1e-12)
    value = result.iloc[-1]
    return float(value) if np.isfinite(float(value)) else math.nan


@contextmanager
def _isolated_dependency_authority(v2_root: Path, assets: list[str]):
    """Redirect verifier continuation writes into a disposable repository-local sandbox.

    Batch edge runtimes call the verified FFD/HMM loaders.  Those verifiers are
    allowed to extend continuation state and therefore normally write under the
    production live_feature_continuation directory.  Offline Phase C certification
    must never mutate that authority.  Seed a private copy of current per-asset
    continuation files, redirect both verifier modules there, then restore globals
    and remove the sandbox on exit.
    """
    import importlib

    source = v2_root / "portfolio_live" / "outputs" / "live_feature_continuation"
    parent = v2_root / "portfolio_live" / "outputs" / "phase_c2_dependency_sandbox"
    sandbox = parent / f"pid_{os.getpid()}_{time.time_ns()}"
    sandbox.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
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
            "seeded_files": copied,
        }
    finally:
        try:
            if 'ffd_module' in locals():
                ffd_module.LIVE_CONTINUATION_DIR = old_ffd
            if 'hmm_module' in locals():
                hmm_module.LIVE_CONTINUATION_DIR = old_hmm
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            try:
                parent.rmdir()
            except OSError:
                pass


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    return (
        out.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )


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
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
    missing = [name for name in required if name not in bars.columns]
    if missing:
        raise RuntimeError(f"verified bars missing columns: {missing}")
    bars = bars.copy()
    bars["Datetime"] = to_datetime_iso(bars["Datetime"], utc=True, errors="coerce")
    bars["Bar_Start"] = to_datetime_iso(bars["Bar_Start"], utc=True, errors="coerce")
    for name in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        bars[name] = pd.to_numeric(bars[name], errors="coerce")
    bars = bars.dropna(subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close"])
    return bars.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last").reset_index(drop=True)


def _compare(
    observed: Mapping[str, Any] | pd.Series,
    expected: Mapping[str, Any] | pd.Series,
    columns: list[str],
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    worst_abs = 0.0
    worst_rel = 0.0
    worst_column = None
    for name in columns:
        left = observed.get(name, math.nan)
        right = expected.get(name, math.nan)
        try:
            lv = float(left)
        except Exception:
            lv = math.nan
        try:
            rv = float(right)
        except Exception:
            rv = math.nan
        l_nan = bool(np.isnan(lv))
        r_nan = bool(np.isnan(rv))
        if l_nan or r_nan:
            if l_nan != r_nan:
                mismatches.append({"column": name, "reason": "nan_topology", "observed": lv, "expected": rv})
            continue
        if np.isinf(lv) or np.isinf(rv):
            if lv != rv:
                mismatches.append({"column": name, "reason": "infinity", "observed": lv, "expected": rv})
            continue
        abs_diff = abs(lv - rv)
        scale = max(abs(lv), abs(rv), 1.0)
        rel_diff = abs_diff / scale
        if abs_diff > worst_abs:
            worst_abs = abs_diff
            worst_rel = rel_diff
            worst_column = name
        if not math.isclose(lv, rv, abs_tol=atol, rel_tol=rtol):
            if len(mismatches) < 25:
                mismatches.append({
                    "column": name,
                    "reason": "numeric",
                    "observed": lv,
                    "expected": rv,
                    "abs_diff": abs_diff,
                    "rel_diff": rel_diff,
                })
    return {
        "ok": not mismatches,
        "column_count": len(columns),
        "mismatch_count_capped": len(mismatches),
        "mismatches": mismatches,
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_column": worst_column,
    }


def _compare_frames_tail(
    left: pd.DataFrame,
    right: pd.DataFrame,
    columns: list[str],
    *,
    tail_rows: int,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    l = _normalize(left)
    r = _normalize(right)
    common = sorted(set(l["Datetime"]).intersection(set(r["Datetime"])))
    common = common[-tail_rows:]
    if not common:
        return {"ok": False, "reason": "no_common_timestamps"}
    l = l[l["Datetime"].isin(common)].set_index("Datetime").loc[common]
    r = r[r["Datetime"].isin(common)].set_index("Datetime").loc[common]
    worst = {"max_abs_diff": 0.0, "max_rel_diff": 0.0, "worst_column": None}
    failures: list[dict[str, Any]] = []
    for ts in common:
        result = _compare(l.loc[ts], r.loc[ts], columns, atol=atol, rtol=rtol)
        if result["max_abs_diff"] > worst["max_abs_diff"]:
            worst = {
                "max_abs_diff": result["max_abs_diff"],
                "max_rel_diff": result["max_rel_diff"],
                "worst_column": result["worst_column"],
            }
        if not result["ok"] and len(failures) < 10:
            failures.append({"timestamp": pd.Timestamp(ts).isoformat(), "details": result})
    return {
        "ok": not failures,
        "rows_checked": len(common),
        **worst,
        "failure_samples": failures,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.quantile(np.asarray(values, dtype=float), q))


def _generation_from_authority(
    *,
    asset: str,
    row: pd.Series,
    previous_hmm_target: pd.Timestamp | None,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float] | None, pd.Timestamp | None]:
    ts = pd.Timestamp(row["Datetime"])
    ffd = {name: float(row[name]) for name in FFD_COLUMNS}
    hmm_raw = {name: float(row[name]) for name in REGIME_COLUMNS}
    eligible = all(np.isfinite(list(hmm_raw.values())))
    if eligible:
        hmm_target = ts
        hmm = hmm_raw
    else:
        if previous_hmm_target is None:
            raise RuntimeError(f"{asset}: first certification update is HMM-ineligible without a prior target")
        hmm_target = previous_hmm_target
        hmm = None
    generation_id = hashlib.sha256(
        f"phase_c2_cert|{asset}|{ts.isoformat()}|{hmm_target.isoformat()}|{eligible}".encode("utf-8")
    ).hexdigest()
    generation = {
        "status": "READY",
        "asset": asset,
        "generation_id": generation_id,
        "raw_target": ts.isoformat(),
        "hmm_target": hmm_target.isoformat(),
        "target_hmm_eligible": eligible,
    }
    return generation, ffd, hmm, hmm_target


def _asset_certify(
    *,
    asset: str,
    asset_contract: Mapping[str, Any],
    manifest_edges: dict[str, dict[str, Any]],
    v2_root: Path,
    c1: Mapping[str, Any],
    bootstrap_rows: int,
    steps: int,
    rolling_checkpoints: list[int],
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    required = required_asset_features(c1, asset)
    addons = addon_asset_features(c1, asset)
    core = [name for name in required if name not in set(addons)]
    groups = list(asset_contract.get("structural_contract_groups") or [])
    if not groups:
        raise RuntimeError(f"{asset}: no C.1 structural contract groups")

    representatives: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
    for group in groups:
        edge_ids = sorted(str(x) for x in group.get("edge_ids") or [])
        if not edge_ids:
            raise RuntimeError(f"{asset}: empty structural contract group")
        edge_id = edge_ids[0]
        entry = manifest_edges.get(edge_id)
        if not entry:
            raise RuntimeError(f"{asset}: manifest row missing for {edge_id}")
        runtime = load_edge_runtime_from_manifest_entry(entry, v2_root=v2_root)
        representatives.append((dict(group), entry, runtime))

    # Deterministic canonical core representative: largest non-absorption group,
    # then lexicographic group id.  New edges/assets naturally participate.
    core_candidates = [item for item in representatives if str(item[1].get("family") or "").lower() != "absorption"]
    if not core_candidates:
        raise RuntimeError(f"{asset}: no non-absorption canonical-core representative")
    core_group, core_entry, core_runtime = sorted(
        core_candidates,
        key=lambda item: (-int(item[0].get("edge_count") or 0), str(item[0].get("group_id") or "")),
    )[0]
    absorption_items = [item for item in representatives if str(item[1].get("family") or "").lower() == "absorption"]
    absorption_runtime = absorption_items[0][2] if absorption_items else None
    absorption_entry = absorption_items[0][1] if absorption_items else None

    bars_all = _load_bars(core_runtime, v2_root=v2_root)
    needed = bootstrap_rows + steps
    if len(bars_all) < needed:
        raise RuntimeError(f"{asset}: need >= {needed} verified bars, have {len(bars_all)}")
    bars = bars_all.tail(needed).reset_index(drop=True)

    authorities: dict[str, pd.DataFrame] = {}
    authority_timings: dict[str, float] = {}
    for group, entry, runtime in representatives:
        t0 = time.perf_counter()
        frame = runtime.build_features(bars=bars.copy(), drop_warmup_rows=False)
        frame = _normalize(frame)
        authorities[str(group.get("group_id"))] = frame
        authority_timings[str(group.get("group_id"))] = (time.perf_counter() - t0) * 1000.0

    core_authority = authorities[str(core_group.get("group_id"))]
    group_parity: list[dict[str, Any]] = []
    for group, entry, runtime in representatives:
        group_id = str(group.get("group_id"))
        frame = authorities[group_id]
        compare_cols = core
        result = _compare_frames_tail(
            frame, core_authority, compare_cols,
            tail_rows=min(512, bootstrap_rows), atol=atol, rtol=rtol,
        )
        group_parity.append({
            "group_id": group_id,
            "representative_edge_id": str(entry.get("edge_id")),
            "family": str(entry.get("family")),
            "compared_feature_count": len(compare_cols),
            "parity": result,
        })

    # Absorption's C.1 addon contract is separately checked against the private
    # superset generated by the compact bootstrap path later in the sequence.
    absorption_authority = None
    if addons:
        if absorption_runtime is None or absorption_entry is None:
            raise RuntimeError(f"{asset}: C.1 declares addon features but no absorption group exists")
        absorption_group_id = next(
            str(group.get("group_id"))
            for group, entry, _runtime in representatives
            if str(entry.get("family") or "").lower() == "absorption"
        )
        absorption_authority = authorities[absorption_group_id]
        missing_addons = [name for name in addons if name not in absorption_authority.columns]
        if missing_addons:
            raise RuntimeError(f"{asset}: absorption authority missing addons {missing_addons}")

    bootstrap_bars = bars.iloc[:bootstrap_rows].copy()
    # Seed from the exact production-sized 6,000-row batch authority, not from a
    # prefix sliced out of a larger 6,128-row build.  Some third-party indicators
    # have whole-input zero-range semantics, so the input horizon itself is part
    # of the batch authority contract.
    t_boot = time.perf_counter()
    bootstrap_authority = _normalize(
        core_runtime.build_features(bars=bootstrap_bars.copy(), drop_warmup_rows=False)
    )
    bootstrap_authority_build_ms = (time.perf_counter() - t_boot) * 1000.0
    state = bootstrap_compact_state(
        asset=asset,
        bars=bootstrap_bars,
        authoritative_features=bootstrap_authority,
        phase_c1_contract=c1,
    )
    bootstrap_obs = state_observability(state)
    if bootstrap_obs["has_unbounded_feature_history"]:
        raise RuntimeError(f"{asset}: compact state unexpectedly carries unbounded feature history")

    previous_hmm_target = None
    bootstrap_hmm = bootstrap_authority.dropna(subset=list(REGIME_COLUMNS))
    if not bootstrap_hmm.empty:
        previous_hmm_target = pd.Timestamp(bootstrap_hmm["Datetime"].iloc[-1])

    sequential_failures: list[dict[str, Any]] = []
    addon_failures: list[dict[str, Any]] = []
    restart_failures: list[dict[str, Any]] = []
    update_ms: list[float] = []
    restart_steps = sorted(set(x for x in (32, 64, 96) if 0 < x < steps))
    rolling_checkpoint_set = {
        int(x) for x in rolling_checkpoints if 1 <= int(x) <= steps
    }
    observed_at_checkpoint: dict[int, dict[str, Any]] = {}
    for step in range(1, steps + 1):
        bar = bars.iloc[bootstrap_rows + step - 1]
        ts = pd.Timestamp(bar["Datetime"])
        expected_rows = core_authority[core_authority["Datetime"] == ts]
        if len(expected_rows) != 1:
            raise RuntimeError(f"{asset}: canonical authority row count for {ts} is {len(expected_rows)}")
        expected = expected_rows.iloc[0]
        generation, ffd, hmm, previous_hmm_target = _generation_from_authority(
            asset=asset,
            row=expected,
            previous_hmm_target=previous_hmm_target,
        )
        observed, state, meta = update_one(
            state=state,
            bar=bar,
            dependency_generation=generation,
            ffd_values=ffd,
            hmm_values=hmm,
            phase_c1_contract=c1,
        )
        update_ms.append(float(meta["elapsed_ms"]))
        # Horizon-invariant core features are compared to the single 6,128-row
        # causal convenience build. The three horizon-sensitive features are
        # instead certified on EVERY step against the exact production-sized
        # 6,000-row authority. This is necessary because pandas rolling floating
        # accumulators can differ by e-9 between a 6,128-row and 6,000-row build
        # even though the mathematical 20/50-row window is identical.
        surrogate_core = [
            name for name in core if name not in HORIZON_SENSITIVE_CORE_FEATURES
        ]
        parity = _compare(observed, expected, surrogate_core, atol=atol, rtol=rtol)

        window_start = bootstrap_rows + step - bootstrap_rows
        rolling_window_bars = bars.iloc[window_start : bootstrap_rows + step].copy()
        if len(rolling_window_bars) != bootstrap_rows:
            raise RuntimeError(
                f"{asset}: production-sized rolling window length changed at step {step}"
            )

        stc_frame = ta.stc(
            pd.to_numeric(rolling_window_bars["Close"], errors="coerce").astype("float64")
        )
        if stc_frame is None or stc_frame.empty:
            raise RuntimeError(f"{asset}: rolling STC authority unavailable at step {step}")
        stc_expected = float(stc_frame.iloc[-1, 0])
        stc_parity = _compare(
            observed, {"STC": stc_expected}, ["STC"], atol=atol, rtol=rtol
        )

        sb_names = [name for name in STRUCTURAL_BREAK_ORACLE_FEATURES if name in core]
        sb_rolling = structural_break_features(
            pd.to_numeric(rolling_window_bars["Close"], errors="coerce").astype("float64").tail(50),
            min_sl=10, constant="ct", lags=1, lookback=50, q=0.95, v=0.025,
        ).iloc[-1]
        sadf_parity = _compare(
            observed,
            {name: float(sb_rolling[STRUCTURAL_BREAK_ORACLE_FEATURES[name]]) for name in sb_names},
            sb_names,
            atol=atol,
            rtol=rtol,
        )

        ent_names = [name for name in ENTROPY_COLUMNS if name in core]
        ent_expected = entropy_row(
            pd.to_numeric(rolling_window_bars["Close"], errors="coerce").to_numpy(dtype="float64")[-(ENTROPY_WINDOW + 1):],
            load_entropy_encoding(asset),
        )
        entropy_parity = _compare(observed, ent_expected, ent_names, atol=atol, rtol=rtol)

        rolling_times = pd.DatetimeIndex(
            to_datetime_iso(rolling_window_bars["Datetime"], utc=True)
        )
        ffd_window = core_authority[
            core_authority["Datetime"].isin(set(rolling_times))
        ].sort_values("Datetime", kind="mergesort")
        if (
            len(ffd_window) != bootstrap_rows
            or not pd.DatetimeIndex(ffd_window["Datetime"]).equals(rolling_times)
        ):
            raise RuntimeError(
                f"{asset}: rolling Close_FFD oracle is not timestamp-aligned at step {step}"
            )
        ffd_z_expected = {
            "Close_FFD_Z_20": _shifted_ffd_z_latest(ffd_window["Close_FFD"], 20),
            "Close_FFD_Z_50": _shifted_ffd_z_latest(ffd_window["Close_FFD"], 50),
        }
        ffd_z_parity = _compare(
            observed,
            ffd_z_expected,
            ["Close_FFD_Z_20", "Close_FFD_Z_50"],
            atol=atol,
            rtol=rtol,
        )

        if step in rolling_checkpoint_set:
            observed_at_checkpoint[step] = dict(observed)

        if (
            not parity["ok"]
            or not stc_parity["ok"]
            or not sadf_parity["ok"]
            or not entropy_parity["ok"]
            or not ffd_z_parity["ok"]
        ) and len(sequential_failures) < 25:
            sequential_failures.append({
                "step": step,
                "timestamp": ts.isoformat(),
                "surrogate_full_horizon_parity": parity,
                "stc_rolling_6000_parity": stc_parity,
                "structural_break_research_rule_parity": sadf_parity,
                "entropy_research_rule_parity": entropy_parity,
                "ffd_z_rolling_6000_parity": ffd_z_parity,
            })
        if addons and absorption_authority is not None:
            addon_rows = absorption_authority[absorption_authority["Datetime"] == ts]
            if len(addon_rows) != 1:
                raise RuntimeError(f"{asset}: absorption authority row count for {ts} is {len(addon_rows)}")
            addon_parity = _compare(observed, addon_rows.iloc[0], addons, atol=atol, rtol=rtol)
            if not addon_parity["ok"] and len(addon_failures) < 25:
                addon_failures.append({"step": step, "timestamp": ts.isoformat(), "parity": addon_parity})
        if step in restart_steps:
            before = state_observability(state)
            payload = serialize_compact_state(state, phase_c1_contract=c1)
            restored = deserialize_compact_state(payload, phase_c1_contract=c1)
            after = state_observability(restored)
            if before != after:
                restart_failures.append({"step": step, "reason": "observability_changed", "before": before, "after": after})
            state = restored

    rolling_results: list[dict[str, Any]] = []
    for step in sorted(set(x for x in rolling_checkpoints if 1 <= x <= steps)):
        end = bootstrap_rows + step
        window = bars.iloc[end - bootstrap_rows:end].copy().reset_index(drop=True)
        ts = pd.Timestamp(window["Datetime"].iloc[-1])
        t0 = time.perf_counter()
        rolling_core = _normalize(core_runtime.build_features(bars=window.copy(), drop_warmup_rows=False))
        core_ms = (time.perf_counter() - t0) * 1000.0
        expected = rolling_core.iloc[-1]
        stream_expected_rows = core_authority[core_authority["Datetime"] == ts]
        if len(stream_expected_rows) != 1:
            raise RuntimeError(
                f"{asset}: full-horizon diagnostic authority row count for {ts} "
                f"is {len(stream_expected_rows)}"
            )
        full_row = stream_expected_rows.iloc[0]
        observed_checkpoint = observed_at_checkpoint.get(step)
        if observed_checkpoint is None:
            raise RuntimeError(
                f"{asset}: incremental checkpoint row was not retained for step {step}"
            )

        # THIS is the production semantic gate: the incremental row must match a
        # fresh batch build on the exact current 6,000-bar window.
        incremental_vs_rolling = _compare(
            observed_checkpoint, expected, core, atol=atol, rtol=rtol
        )

        # Longer-horizon versus rolling-horizon comparisons are diagnostics only.
        # They deliberately expose features whose floating implementation depends
        # on the input horizon, but they are not allowed to redefine production's
        # actual 6,000-bar batch authority.
        invariant_core = [
            name for name in core if name not in HORIZON_SENSITIVE_CORE_FEATURES
        ]
        authority_shift = _compare(
            full_row, expected, invariant_core, atol=atol, rtol=rtol
        )
        horizon_sensitive_diagnostic = _compare(
            full_row,
            expected,
            [name for name in HORIZON_SENSITIVE_CORE_FEATURES if name in core],
            atol=atol,
            rtol=rtol,
        )
        item: dict[str, Any] = {
            "step": step,
            "timestamp": ts.isoformat(),
            "rolling_core_build_ms": core_ms,
            "incremental_vs_rolling_authority": incremental_vs_rolling,
            "full_history_vs_rolling_invariant_core_diagnostic": authority_shift,
            "horizon_sensitive_full_vs_rolling_diagnostic": horizon_sensitive_diagnostic,
        }
        if addons and absorption_runtime is not None and absorption_authority is not None:
            t1 = time.perf_counter()
            rolling_abs = _normalize(
                absorption_runtime.build_features(
                    bars=window.copy(), drop_warmup_rows=False
                )
            )
            item["rolling_absorption_build_ms"] = (
                time.perf_counter() - t1
            ) * 1000.0
            rolling_abs_latest = rolling_abs.iloc[-1]
            item["incremental_vs_rolling_absorption_authority"] = _compare(
                observed_checkpoint,
                rolling_abs_latest,
                addons,
                atol=atol,
                rtol=rtol,
            )
            full_abs = absorption_authority[
                absorption_authority["Datetime"] == ts
            ].iloc[0]
            item["absorption_full_history_vs_rolling_diagnostic"] = _compare(
                full_abs,
                rolling_abs_latest,
                addons,
                atol=atol,
                rtol=rtol,
            )
        rolling_results.append(item)

    group_ok = all(item["parity"].get("ok") is True for item in group_parity)
    sequential_ok = not sequential_failures
    addon_ok = not addon_failures
    restart_ok = not restart_failures
    # Gate against the actual production-sized 6,000-row batch authority.
    # Longer/full-horizon differences are diagnostics and cannot override the
    # production oracle.
    rolling_ok = all(
        item["incremental_vs_rolling_authority"].get("ok") is True
        and (
            "incremental_vs_rolling_absorption_authority" not in item
            or item["incremental_vs_rolling_absorption_authority"].get("ok") is True
        )
        for item in rolling_results
    )
    status = "PASS" if group_ok and sequential_ok and addon_ok and restart_ok and rolling_ok else "FAIL"
    return {
        "asset": asset,
        "status": status,
        "edge_count": int(asset_contract.get("edge_count") or 0),
        "structural_group_count": len(groups),
        "canonical_core_representative_edge_id": str(core_entry.get("edge_id")),
        "absorption_representative_edge_id": str(absorption_entry.get("edge_id")) if absorption_entry else None,
        "core_feature_count": len(core),
        "addon_feature_count": len(addons),
        "bars_available": len(bars_all),
        "certification_window_rows": len(bars),
        "bootstrap_rows": bootstrap_rows,
        "bootstrap_authority_mode": "canonical_core_plus_derived_addons_plus_bounded_same_library_stc_and_pandas_ffd_z",
        "stc_authority_mode": "bounded_rolling_6000_same_installed_pandas_ta_classic",
        "ffd_z_authority_mode": "bounded_rolling_6000_same_pandas_rolling_shift",
        "sequential_steps": steps,
        "strict_atol": atol,
        "strict_rtol": rtol,
        "group_parity": group_parity,
        "sequential_failure_samples": sequential_failures,
        "addon_failure_samples": addon_failures,
        "restart_failure_samples": restart_failures,
        "rolling_authority_checks": rolling_results,
        "bootstrap_state": bootstrap_obs,
        "bootstrap_authority_build_ms": bootstrap_authority_build_ms,
        "update_latency_ms": {
            "count": len(update_ms),
            "p50": _percentile(update_ms, 0.50),
            "p95": _percentile(update_ms, 0.95),
            "max": max(update_ms) if update_ms else None,
            "mean": statistics.fmean(update_ms) if update_ms else None,
        },
        "authority_build_ms_by_group": authority_timings,
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--c1-contract", type=Path, default=DEFAULT_C1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bootstrap-rows", type=int, default=6000)
    parser.add_argument("--steps", type=int, default=128)
    parser.add_argument("--rolling-checkpoints", default="1,32,64,96,128")
    parser.add_argument("--atol", type=float, default=5e-10)
    parser.add_argument("--rtol", type=float, default=5e-10)
    parser.add_argument("--assets", nargs="*", default=None)
    args = parser.parse_args()

    v2_root = args.v2_root.resolve()
    manifest_path = args.manifest.resolve()
    c1_path = args.c1_contract.resolve()
    output = args.output.resolve()
    manifest = _read_json(manifest_path)
    c1 = load_phase_c1_contract(c1_path)
    manifest_edges = {
        str(edge.get("edge_id")): edge
        for edge in (manifest.get("selected_edges") or [])
        if isinstance(edge, dict) and str(edge.get("edge_id") or "")
    }
    selected_assets = [str(x).upper() for x in c1.get("selected_assets") or []]
    if args.assets:
        requested = {str(x).upper() for x in args.assets}
        unknown = sorted(requested.difference(selected_assets))
        if unknown:
            raise SystemExit(f"Unknown/non-selected assets requested: {unknown}")
        selected_assets = [asset for asset in selected_assets if asset in requested]
    if not selected_assets:
        raise SystemExit("No assets selected for Phase C.2 certification")
    if args.bootstrap_rows < 6000:
        raise SystemExit("Phase C.2 certification may not reduce the 6000-bar authority")
    if args.steps < 128:
        raise SystemExit("Phase C.2 certification requires at least 128 sequential steps")
    checkpoints = [int(x.strip()) for x in str(args.rolling_checkpoints).split(",") if x.strip()]

    print("=" * 112)
    print("PHASE C.2 — COMPACT INCREMENTAL CANONICAL FEATURE ENGINE CERTIFICATION")
    print("=" * 112)
    print(f"Engine:       {ENGINE_VERSION}")
    print(f"Assets:       {', '.join(selected_assets)}")
    print(f"Bootstrap:    {args.bootstrap_rows}")
    print(f"Steps:        {args.steps}")
    print(f"Rolling:      {checkpoints}")
    print(f"Tolerance:    atol={args.atol:g} rtol={args.rtol:g}")
    print("Routing live: False")

    results: dict[str, Any] = {}
    overall = "PASS"
    sandbox_report: dict[str, Any] = {}
    with _isolated_dependency_authority(v2_root, selected_assets) as sandbox_info:
        sandbox_report = {
            "enabled": True,
            "production_continuation_mutated": False,
            "seeded_file_count": int(sandbox_info["seeded_file_count"]),
            "sandbox_path": str(sandbox_info["sandbox"]),
        }
        print(
            f"Dependency authority sandbox: ON "
            f"(seeded {sandbox_report['seeded_file_count']} current per-asset files; production outputs read-only)"
        )
        for asset in selected_assets:
            print(f"\n--- {asset} ---", flush=True)
            try:
                result = _asset_certify(
                    asset=asset,
                    asset_contract=(c1.get("assets") or {})[asset],
                    manifest_edges=manifest_edges,
                    v2_root=v2_root,
                    c1=c1,
                    bootstrap_rows=args.bootstrap_rows,
                    steps=args.steps,
                    rolling_checkpoints=checkpoints,
                    atol=args.atol,
                    rtol=args.rtol,
                )
            except Exception as exc:
                result = {
                    "asset": asset,
                    "status": "FAIL",
                    "fatal_error": f"{type(exc).__name__}: {exc}",
                }
            results[asset] = result
            if result.get("status") != "PASS":
                overall = "FAIL"
            latency = result.get("update_latency_ms") or {}
            print(
                f"status={result.get('status')} groups={result.get('structural_group_count')} "
                f"core={result.get('core_feature_count')} addons={result.get('addon_feature_count')} "
                f"p50_ms={latency.get('p50')} p95_ms={latency.get('p95')}"
            )
            if result.get("fatal_error"):
                print(f"fatal={result['fatal_error']}")
            if result.get("sequential_failure_samples"):
                sample = result["sequential_failure_samples"][0]
                print(f"first_sequential_failure={json.dumps(sample, default=str)[:1200]}")
            if result.get("addon_failure_samples"):
                sample = result["addon_failure_samples"][0]
                print(f"first_addon_failure={json.dumps(sample, default=str)[:1200]}")
            rolling_bad = [
                row for row in result.get("rolling_authority_checks") or []
                if row.get("incremental_vs_rolling_authority", {}).get("ok") is not True
                or row.get("incremental_vs_rolling_absorption_authority", {}).get("ok") is False
            ]
            if rolling_bad:
                print(f"first_rolling_authority_difference={json.dumps(rolling_bad[0], default=str)[:1200]}")

    payload = {
        "schema_version": "phase_c2_incremental_feature_certification_v1_3",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "runtime_routing_enabled": False,
        "engine_version": ENGINE_VERSION,
        "phase_c1_contract_sha256": c1.get("contract_sha256"),
        "phase_c1_contract_file_sha256": _sha256(c1_path),
        "live_manifest_file_sha256": _sha256(manifest_path),
        "implementation_fingerprint": implementation_fingerprint(),
        "dependency_authority_sandbox": sandbox_report,
        "bootstrap_rows": args.bootstrap_rows,
        "sequential_steps": args.steps,
        "rolling_checkpoints": checkpoints,
        "strict_atol": args.atol,
        "strict_rtol": args.rtol,
        "selected_assets": selected_assets,
        "selected_asset_count": len(selected_assets),
        "selected_edge_count": len(manifest_edges),
        "assets": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"\nOUTPUT: {output}")
    print(f"STATUS: {overall}")
    return 0 if overall == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
