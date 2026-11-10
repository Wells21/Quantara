# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
"""Validate and attach finalized trade geometry to Step 04B artifacts.

This is an artifact-boundary validation step. It does not recalculate thresholds,
change viability gates, select representatives, alter returns, or size positions.
Run it immediately after 04b_build_threshold_grid_trade_ledger.py and before 04C.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.edges.trade_geometry import GEOMETRY_COLUMNS, geometry_from_mapping  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

SCHEMA_VERSION = "v2.3"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs"
CLASSIFICATION_REPORT = OUTPUT_DIR / "v2_edge_classification_report.csv"
GEOMETRY_MANIFEST = OUTPUT_DIR / "v2_geometry_handoff_manifest.json"
GEOMETRY_REPORT = OUTPUT_DIR / "v2_geometry_handoff_validation.csv"

METADATA_FILES = [
    OUTPUT_DIR / "v2_threshold_grid_summary_all.csv",
    OUTPUT_DIR / "v2_threshold_grid_viability.csv",
    OUTPUT_DIR / "v2_threshold_grid_trade_ledger_summary.csv",
    OUTPUT_DIR / "v2_threshold_grid_trade_ledger_validation_report.csv",
]
LEDGER_FILES = [
    # Step 04B v2.5 writes one canonical raw threshold ledger only.
    # Core/watchlist separation remains available in edge_classification and is
    # recreated at the much smaller representative-ledger stage (Steps 04C/05).
    OUTPUT_DIR / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv",
]

PROBABILITY_COLUMN = "calibrated_probability"
AFML_COLUMN = "afml_bet_size"
EVENT_VOL_COLUMN = "Event_Vol"
CHUNK_SIZE = 150_000
RR_RTOL = 1e-3
RR_ATOL = 1e-6
DISTANCE_RTOL = 1e-6
DISTANCE_ATOL = 1e-8
PRICE_ATOL = 1e-12
EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_datetime(series: pd.Series) -> pd.Series:
    return to_datetime_iso(series, utc=True, errors="coerce")


def load_geometry_map() -> pd.DataFrame:
    require(CLASSIFICATION_REPORT.exists(), f"Run Step 03 first. Missing: {CLASSIFICATION_REPORT}")
    df = pd.read_csv(CLASSIFICATION_REPORT)
    require(not df.empty, "Classification report is empty")
    required = ["edge_id", "side_name", *GEOMETRY_COLUMNS]
    missing = [c for c in required if c not in df.columns]
    require(not missing, f"Classification report missing finalized geometry columns: {missing}. Rerun Steps 01-03.")

    rows: list[dict[str, Any]] = []
    for edge_id, g in df.groupby("edge_id", sort=True):
        hashes = g["geometry_hash"].astype(str).unique().tolist()
        require(len(hashes) == 1, f"{edge_id}: classification report contains multiple geometry hashes: {hashes}")
        row = g.iloc[0].to_dict()
        geometry = geometry_from_mapping(row, expected_side_name=str(row.get("side_name", "")))
        rows.append({"edge_id": str(edge_id), "side_name": str(row["side_name"]), **geometry})
    return pd.DataFrame(rows)


def attach_geometry(df: pd.DataFrame, geometry_map: pd.DataFrame, *, context: str) -> pd.DataFrame:
    require("edge_id" in df.columns, f"{context}: missing edge_id")
    if "geometry_hash" in df.columns:
        expected = df["edge_id"].astype(str).map(geometry_map.set_index("edge_id")["geometry_hash"])
        existing = df["geometry_hash"].astype(str).str.strip()
        populated = existing.ne("") & existing.str.lower().ne("nan")
        mismatch = populated & existing.ne(expected.astype(str))
        require(not mismatch.any(), f"{context}: {int(mismatch.sum())} existing geometry hashes conflict with the classification geometry map")
    out = df.drop(columns=[c for c in GEOMETRY_COLUMNS if c in df.columns], errors="ignore")
    out = out.merge(geometry_map.drop(columns=["side_name"]), on="edge_id", how="left", validate="many_to_one")
    missing = out["geometry_hash"].isna()
    require(not missing.any(), f"{context}: {int(missing.sum())} rows could not be mapped to finalized geometry")
    return out


def resolve_v2_path(path_text: Any, *, context: str) -> Path:
    value = str(path_text).strip()
    require(value and value.lower() != "nan", f"{context}: empty calibrated_oof_path")
    path = V2_ROOT / value
    require(path.exists() and path.is_file(), f"{context}: calibrated OOF does not exist: {value}")
    return path


def load_oof_event_vol_map(path: Path) -> pd.Series:
    """Reproduce Step 04B's filtered/sorted OOF row numbering and return Event_Vol."""
    # float_precision="round_trip": pandas' default C float parser is not
    # always bit-exact, which matters here given this validator's tight
    # geometry tolerances (DISTANCE_RTOL/ATOL). See config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    require(not df.empty, f"Calibrated OOF file is empty: {path}")
    if "Datetime" in df.columns:
        df["event_time"] = parse_datetime(df["Datetime"])
    elif "event_time" in df.columns:
        df["event_time"] = parse_datetime(df["event_time"])
    else:
        df["event_time"] = parse_datetime(df[df.columns[0]])
    if "exit_time" in df.columns:
        df["exit_time"] = parse_datetime(df["exit_time"])
    elif "t1" in df.columns:
        df["exit_time"] = parse_datetime(df["t1"])
    else:
        df["exit_time"] = pd.NaT
    if "executed_r" not in df.columns:
        raise ValueError(
            f"Calibrated OOF missing genuine Step 03A executed_r: {path}. "
            "V3 forbids realized_r fallback."
        )
    for required_execution in (
        "execution_status",
        "execution_contract_version",
        "execution_geometry_hash",
        "signal_mid",
    ):
        if required_execution not in df.columns:
            raise ValueError(
                f"Calibrated OOF missing {required_execution}: {path}"
            )
    if not df["execution_status"].astype(str).eq("resolved").all():
        raise ValueError(f"Calibrated OOF contains unresolved execution rows: {path}")
    if not df["execution_contract_version"].astype(str).eq(
        EXECUTION_CONTRACT_VERSION
    ).all():
        raise ValueError(f"Calibrated OOF execution contract mismatch: {path}")
    # This dropna/sort/row-numbering must reproduce Step 04B's
    # load_calibrated_oof() EXACTLY (see 04b_build_threshold_grid_trade_ledger.py)
    # -- source_oof_row_number is a positional index, so a narrower dropna
    # subset here silently shifts every row's index relative to 04B's ledger
    # once any row is null in a column 04B checks but this function does not
    # (confirmed empirically: 04B's stricter subset drops 1 more row than the
    # subset formerly used here, shifting every subsequent row by one position
    # and causing spurious "geometry does not match" failures). See
    # config/mldp_deviation_register.json for the full derivation.
    required = [
        "entry_time", "exit_time", "executed_r", "meta_label", "quote_executable_meta_label",
        PROBABILITY_COLUMN, AFML_COLUMN, EVENT_VOL_COLUMN,
        "signal_mid", "entry_price", "pt_price", "sl_price",
    ]
    missing = [c for c in required if c not in df.columns]
    require(not missing, f"Calibrated OOF missing columns required for geometry validation {missing}: {path}")
    df["entry_time"] = parse_datetime(df["entry_time"])
    for col in [
        "executed_r", "meta_label", "quote_executable_meta_label", PROBABILITY_COLUMN, AFML_COLUMN,
        EVENT_VOL_COLUMN, "signal_mid", "entry_price", "pt_price", "sl_price",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=[
        "event_time", "entry_time", "exit_time", "executed_r", "meta_label", "quote_executable_meta_label",
        PROBABILITY_COLUMN, AFML_COLUMN, EVENT_VOL_COLUMN, "signal_mid", "entry_price", "pt_price", "sl_price",
    ]).copy()
    df = df.sort_values(["event_time", "exit_time"]).reset_index(drop=True)
    df["source_oof_row_number"] = np.arange(len(df), dtype=int)
    require(df[EVENT_VOL_COLUMN].notna().all(), f"{path}: Event_Vol contains missing/non-numeric values after Step 04B row filtering")
    require(df[EVENT_VOL_COLUMN].gt(0).all(), f"{path}: Event_Vol must be positive for every Step 04B source row")
    return df.set_index("source_oof_row_number")[EVENT_VOL_COLUMN].astype(float)


def attach_event_vol(
    df: pd.DataFrame,
    oof_event_vol_cache: dict[str, pd.Series],
    *,
    context: str,
) -> pd.DataFrame:
    required = ["calibrated_oof_path", "source_oof_row_number"]
    missing = [c for c in required if c not in df.columns]
    require(not missing, f"{context}: missing Event_Vol source columns: {missing}")

    out = df.copy()
    existing = pd.to_numeric(out[EVENT_VOL_COLUMN], errors="coerce") if EVENT_VOL_COLUMN in out.columns else None
    canonical = pd.Series(np.nan, index=out.index, dtype=float)
    for path_text, indexes in out.groupby("calibrated_oof_path", sort=False).groups.items():
        path = resolve_v2_path(path_text, context=context)
        cache_key = str(path.resolve())
        if cache_key not in oof_event_vol_cache:
            oof_event_vol_cache[cache_key] = load_oof_event_vol_map(path)
        event_vol_map = oof_event_vol_cache[cache_key]
        row_numbers = pd.to_numeric(out.loc[indexes, "source_oof_row_number"], errors="coerce")
        require(row_numbers.notna().all(), f"{context}: source_oof_row_number contains non-numeric values for {path_text}")
        row_numbers = row_numbers.astype(int)
        values = event_vol_map.reindex(row_numbers.to_numpy()).to_numpy(dtype=float)
        require(np.isfinite(values).all(), f"{context}: source_oof_row_number could not be mapped to Event_Vol for {path_text}")
        canonical.loc[indexes] = values

    require(canonical.notna().all(), f"{context}: canonical Event_Vol mapping left missing rows")
    if existing is not None:
        populated = existing.notna()
        mismatch = populated & ~pd.Series(
            np.isclose(existing.to_numpy(dtype=float), canonical.to_numpy(dtype=float), rtol=1e-12, atol=1e-12, equal_nan=False),
            index=out.index,
        )
        require(not mismatch.any(), f"{context}: {int(mismatch.sum())} existing Event_Vol values conflict with the calibrated OOF source")
    out[EVENT_VOL_COLUMN] = canonical.astype(float)
    return out


def validate_geometry_chunk(df: pd.DataFrame, *, context: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    required = [
        "edge_id", "side_name", "signal_mid", "entry_price", "pt_price", "sl_price", EVENT_VOL_COLUMN,
        "execution_status", "execution_contract_version", "execution_geometry_hash",
        "pt_multiplier", "sl_multiplier", "reward_risk_ratio", "barrier_reference",
        "barrier_reference_units", "geometry_hash",
    ]
    missing = [c for c in required if c not in df.columns]
    require(not missing, f"{context}: missing geometry validation columns: {missing}")

    out = df.copy()
    signal_mid = pd.to_numeric(out["signal_mid"], errors="coerce")
    entry = pd.to_numeric(out["entry_price"], errors="coerce")
    pt = pd.to_numeric(out["pt_price"], errors="coerce")
    sl = pd.to_numeric(out["sl_price"], errors="coerce")
    event_vol = pd.to_numeric(out[EVENT_VOL_COLUMN], errors="coerce")
    pt_mult = pd.to_numeric(out["pt_multiplier"], errors="coerce")
    sl_mult = pd.to_numeric(out["sl_multiplier"], errors="coerce")
    declared_rr = pd.to_numeric(out["reward_risk_ratio"], errors="coerce")

    stop_distance = (entry - sl).abs()
    target_distance = (pt - entry).abs()
    observed_rr = target_distance / stop_distance.replace(0.0, np.nan)
    expected_rr = pt_mult / sl_mult.replace(0.0, np.nan)
    expected_stop_distance = signal_mid.abs() * event_vol.abs() * sl_mult
    expected_target_distance = signal_mid.abs() * event_vol.abs() * pt_mult

    numeric_ok = (
        signal_mid.notna() & signal_mid.gt(0)
        & entry.notna() & pt.notna() & sl.notna() & event_vol.notna() & event_vol.gt(0)
        & pt_mult.gt(0) & sl_mult.gt(0) & stop_distance.gt(PRICE_ATOL) & target_distance.gt(PRICE_ATOL)
    )
    reference_ok = (
        out["barrier_reference"].astype(str).str.strip().eq(EVENT_VOL_COLUMN)
        & out["barrier_reference_units"].astype(str).str.strip().eq("return_fraction")
    )
    side = out["side_name"].astype(str).str.lower().str.strip()
    direction_ok = (
        ((side == "long") & (sl < entry) & (entry < pt))
        | ((side == "short") & (pt < entry) & (entry < sl))
    )
    rr_ok = pd.Series(
        np.isclose(observed_rr.to_numpy(dtype=float), expected_rr.to_numpy(dtype=float), rtol=RR_RTOL, atol=RR_ATOL, equal_nan=False),
        index=out.index,
    )
    declared_rr_ok = pd.Series(
        np.isclose(declared_rr.to_numpy(dtype=float), expected_rr.to_numpy(dtype=float), rtol=1e-9, atol=1e-9, equal_nan=False),
        index=out.index,
    )
    stop_distance_ok = pd.Series(
        np.isclose(stop_distance.to_numpy(dtype=float), expected_stop_distance.to_numpy(dtype=float), rtol=DISTANCE_RTOL, atol=DISTANCE_ATOL, equal_nan=False),
        index=out.index,
    )
    target_distance_ok = pd.Series(
        np.isclose(target_distance.to_numpy(dtype=float), expected_target_distance.to_numpy(dtype=float), rtol=DISTANCE_RTOL, atol=DISTANCE_ATOL, equal_nan=False),
        index=out.index,
    )
    execution_identity_ok = (
        out["execution_status"].astype(str).eq("resolved")
        & out["execution_contract_version"].astype(str).eq(EXECUTION_CONTRACT_VERSION)
        & out["execution_geometry_hash"].astype(str).eq(out["geometry_hash"].astype(str))
    )
    valid = (
        numeric_ok & reference_ok & direction_ok & rr_ok & declared_rr_ok
        & stop_distance_ok & target_distance_ok & execution_identity_ok
    )

    out["geometry_observed_reward_risk_ratio"] = observed_rr
    out["geometry_expected_reward_risk_ratio"] = expected_rr
    out["geometry_expected_stop_distance_price"] = expected_stop_distance
    out["geometry_expected_take_profit_distance_price"] = expected_target_distance
    out["geometry_numeric_prices_valid"] = numeric_ok
    out["geometry_barrier_reference_valid"] = reference_ok
    out["geometry_barrier_direction_valid"] = direction_ok
    out["geometry_reward_risk_valid"] = rr_ok
    out["geometry_declared_ratio_valid"] = declared_rr_ok
    out["geometry_stop_distance_valid"] = stop_distance_ok
    out["geometry_take_profit_distance_valid"] = target_distance_ok
    out["geometry_execution_identity_valid"] = execution_identity_ok
    out["geometry_validation_status"] = np.where(valid, "ok", "invalid")

    stats = {
        "rows": int(len(out)),
        "valid_rows": int(valid.sum()),
        "invalid_rows": int((~valid).sum()),
        "numeric_invalid_rows": int((~numeric_ok).sum()),
        "reference_mismatch_rows": int((~reference_ok).sum()),
        "direction_mismatch_rows": int((~direction_ok).sum()),
        "reward_risk_mismatch_rows": int((~rr_ok).sum()),
        "declared_ratio_mismatch_rows": int((~declared_rr_ok).sum()),
        "stop_distance_mismatch_rows": int((~stop_distance_ok).sum()),
        "take_profit_distance_mismatch_rows": int((~target_distance_ok).sum()),
    }
    if stats["invalid_rows"]:
        cols = [
            "edge_id", "grid_candidate_id", "trade_uid", "side_name", EVENT_VOL_COLUMN,
            "entry_price", "pt_price", "sl_price", "pt_multiplier", "sl_multiplier",
            "stop_distance_price", "take_profit_distance_price",
            "geometry_expected_stop_distance_price", "geometry_expected_take_profit_distance_price",
            "geometry_observed_reward_risk_ratio", "geometry_expected_reward_risk_ratio",
            "geometry_validation_status",
        ]
        cols = [c for c in cols if c in out.columns]
        preview = out.loc[~valid, cols].head(30).to_string(index=False)
        raise RuntimeError(f"{context}: finalized geometry does not match OOF Event_Vol barriers.\n{preview}")
    return out, stats


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".geometry_tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def enrich_metadata_file(path: Path, geometry_map: pd.DataFrame, *, write: bool) -> dict[str, Any]:
    require(path.exists(), f"Missing Step 04B artifact: {path}")
    df = pd.read_csv(path)
    enriched = attach_geometry(df, geometry_map, context=path.name)
    if write:
        atomic_write_csv(enriched, path)
    return {"file": path.name, "rows": int(len(enriched)), "geometry_missing_rows": int(enriched["geometry_hash"].isna().sum())}


def enrich_ledger_file(
    path: Path,
    geometry_map: pd.DataFrame,
    oof_event_vol_cache: dict[str, pd.Series],
    *,
    write: bool,
) -> dict[str, Any]:
    require(path.exists(), f"Missing Step 04B ledger: {path}")
    tmp = path.with_suffix(path.suffix + ".geometry_tmp")
    if tmp.exists():
        tmp.unlink()

    totals = {
        "file": path.name,
        "rows": 0,
        "valid_rows": 0,
        "invalid_rows": 0,
        "numeric_invalid_rows": 0,
        "reference_mismatch_rows": 0,
        "direction_mismatch_rows": 0,
        "reward_risk_mismatch_rows": 0,
        "declared_ratio_mismatch_rows": 0,
        "stop_distance_mismatch_rows": 0,
        "take_profit_distance_mismatch_rows": 0,
    }
    first = True
    for chunk_number, chunk in enumerate(pd.read_csv(path, chunksize=CHUNK_SIZE), start=1):
        context = f"{path.name} chunk {chunk_number}"
        enriched = attach_geometry(chunk, geometry_map, context=context)
        enriched = attach_event_vol(enriched, oof_event_vol_cache, context=context)
        enriched, stats = validate_geometry_chunk(enriched, context=context)
        for key in totals:
            if key != "file" and key in stats:
                totals[key] += int(stats[key])
        if write:
            enriched.to_csv(tmp, index=False, mode="w" if first else "a", header=first)
        first = False
    require(not first, f"Step 04B ledger is empty: {path}")
    if write:
        os.replace(tmp, path)
    return totals


def build(write: bool = True) -> dict[str, Any]:
    geometry_map = load_geometry_map()
    oof_event_vol_cache: dict[str, pd.Series] = {}
    metadata_results = [enrich_metadata_file(path, geometry_map, write=write) for path in METADATA_FILES]
    ledger_results = [
        enrich_ledger_file(path, geometry_map, oof_event_vol_cache, write=write)
        for path in LEDGER_FILES
    ]

    edge_report = geometry_map.copy()
    edge_report["geometry_validation_status"] = "PASS"
    if write:
        edge_report.to_csv(GEOMETRY_REPORT, index=False)

    invalid_rows = int(sum(row["invalid_rows"] for row in ledger_results))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "geometry_handoff_status": "PASS" if invalid_rows == 0 else "FAIL",
        "geometry_contract_version": "trade_geometry_v1",
        "classification_edges": int(len(geometry_map)),
        "metadata_results": metadata_results,
        "ledger_results": ledger_results,
        "invalid_ledger_rows": invalid_rows,
        "reward_risk_tolerance": {"rtol": RR_RTOL, "atol": RR_ATOL},
        "absolute_distance_tolerance": {"rtol": DISTANCE_RTOL, "atol": DISTANCE_ATOL},
        "event_vol_distance_validation": "PASS",
        "event_vol_source": "calibrated_oof_reconstructed_with_step04b_filter_sort_row_number_contract",
        "event_vol_column_carried_into_step04b_ledgers": True,
        "outputs": {
            "manifest": str(GEOMETRY_MANIFEST.relative_to(V2_ROOT)),
            "edge_report": str(GEOMETRY_REPORT.relative_to(V2_ROOT)),
        },
        "ledger_scope": "single_core_plus_watchlist_raw_threshold_ledger",
        "legacy_separate_core_watchlist_ledgers_required": False,
        "notes": [
            "This step attaches metadata only and does not alter threshold metrics, trade membership, executed_r, AFML values, or broker risk values.",
            "Barrier direction, PT/SL reward-risk, and absolute Event_Vol-based stop/target distances are validated against finalized edge.yaml geometry.",
            "geometry_hash is a semantic geometry fingerprint; no artifact-file hashes are consumed or validated.",
            "Step 04B v2.5 writes only the canonical core+watchlist raw threshold ledger. edge_classification preserves the core/watchlist label on every row.",
        ],
    }
    if write:
        write_json(GEOMETRY_MANIFEST, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.04BB finalized trade geometry handoff")
    print(f"Status: {manifest['geometry_handoff_status']}")
    print(f"Edges with finalized geometry: {manifest['classification_edges']}")
    print(f"Invalid ledger rows: {manifest['invalid_ledger_rows']}")
    print(f"Event_Vol absolute-distance validation: {manifest['event_vol_distance_validation']}")
    for row in manifest["ledger_results"]:
        print(
            f"- {row['file']}: rows={row['rows']} valid={row['valid_rows']} "
            f"direction_mismatch={row['direction_mismatch_rows']} "
            f"rr_mismatch={row['reward_risk_mismatch_rows']} "
            f"sl_distance_mismatch={row['stop_distance_mismatch_rows']} "
            f"tp_distance_mismatch={row['take_profit_distance_mismatch_rows']}"
        )
    if manifest["geometry_handoff_status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
