# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import hashlib
import json
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

from shared.edges.trade_geometry import GEOMETRY_COLUMNS  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.validation.v3_semantic_contract import validate_payoff_context_frame  # noqa: E402

SCHEMA_VERSION = "v2.2"
PIPELINE_MODE = "v2_manifest_driven_calibrated_afml"
STRICT_TRADE_COUNT_VALIDATION = True
STRICT_MISSING_REPRESENTATIVE_VALIDATION = True
BROKER_EXECUTABLE_RISK_REQUIRED = True

GEOMETRY_NUMERIC_COLUMNS = [
    "pt_multiplier",
    "sl_multiplier",
    "reward_risk_ratio",
    "vertical_horizon_bars",
]

BROKER_RISK_REQUIRED_COLUMNS = [
    "entry_price",
    "sl_price",
    "stop_distance_price",
    "broker_min_lot",
    "broker_lot_step",
    "broker_contract_size",
    "risk_per_1_lot",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
    "broker_min_lot_risk_status",
    "has_valid_broker_min_lot_risk",
]

BROKER_RISK_OPTIONAL_COLUMNS = [
    "pt_price",
    "take_profit_distance_price",
    "price_reward_to_risk_ratio",
    "broker_contract_spec_key",
    "broker_symbol",
    "broker_risk_model",
    "broker_quote_currency",
    "broker_simulation_currency",
    "broker_quote_to_simulation_rate",
    "broker_max_lot",
    "broker_trade_calc_mode",
    "broker_risk_contract_version",
    "broker_profit_currency",
    "broker_account_currency",
    "broker_profit_to_account_rate",
    "broker_conversion_mode",
    "broker_conversion_symbol",
    "broker_conversion_price_basis",
    "broker_conversion_source_timestamp",
    "broker_conversion_source_price",
    "broker_conversion_age_seconds",
    "broker_conversion_status",
]

BROKER_RISK_NUMERIC_COLUMNS = [
    "entry_price",
    "pt_price",
    "sl_price",
    "stop_distance_price",
    "take_profit_distance_price",
    "price_reward_to_risk_ratio",
    "broker_min_lot",
    "broker_lot_step",
    "broker_contract_size",
    "broker_quote_to_simulation_rate",
    "broker_max_lot",
    "broker_trade_calc_mode",
    "broker_profit_to_account_rate",
    "broker_conversion_source_price",
    "broker_conversion_age_seconds",
    "risk_per_1_lot",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
]

UNIVERSE_INPUTS = {
    "all": "v2_representative_threshold_candidates_all.csv",
    "core": "v2_representative_threshold_candidates_core.csv",
    "watchlist": "v2_representative_threshold_candidates_watchlist.csv",
    "core_plus_watchlist": "v2_representative_threshold_candidates_core_plus_watchlist.csv",
}
UNIVERSE_OUTPUTS = {
    "all": "v2_representative_trade_ledger_all.csv",
    "core": "v2_representative_trade_ledger_core.csv",
    "watchlist": "v2_representative_trade_ledger_watchlist.csv",
    "core_plus_watchlist": "v2_representative_trade_ledger_core_plus_watchlist.csv",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _require_step04d(out: Path) -> None:
    manifest_path = out / "v3_final_representative_executable_economics_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            "Run 04d_validate_final_representative_executable_economics.py "
            f"before Step 05. Missing: {manifest_path}"
        )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("final_representative_executable_economics_status") != "PASS":
        raise RuntimeError("Allocator Step 04D exact executable economics is not PASS.")
    if payload.get("payoff_context_version") != "quantara_v3_threshold_payoff_context_v1":
        raise RuntimeError("Allocator Step 04D payoff-context semantic generation is stale.")

    expected = payload.get("source_sha256") or {}
    for filename in (
        "v2_representative_threshold_candidates_all.csv",
        "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv",
    ):
        path = out / filename
        if not path.exists():
            raise RuntimeError(f"Step 04D source artifact is missing: {path}")
        declared = str(expected.get(filename, ""))
        actual = _sha256_file(path)
        if not declared or declared != actual:
            raise RuntimeError(
                "Step 04D is stale relative to current allocator artifacts: "
                f"{filename}. Rerun Step 04D."
            )


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_float(x: Any, default: float = np.nan) -> float:
    try:
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x: Any, default: int = 0) -> int:
    try:
        if pd.isna(x):
            return default
        return int(float(x))
    except Exception:
        return default


def normalize_bool(x: Any) -> bool:
    return str(x).strip().lower() in {"true", "1", "yes", "y"}


def validate_single_geometry_per_edge(df: pd.DataFrame, *, context: str) -> None:
    missing = [c for c in ["edge_id", *GEOMETRY_COLUMNS] if c not in df.columns]
    if missing:
        raise ValueError(f"{context} missing finalized geometry columns: {missing}")
    empty_hash = df["geometry_hash"].astype(str).str.strip().isin({"", "nan", "None"})
    if empty_hash.any():
        raise ValueError(f"{context} contains {int(empty_hash.sum())} rows with an empty geometry_hash")
    conflicts = df.groupby("edge_id")["geometry_hash"].nunique(dropna=False)
    bad = conflicts[conflicts != 1]
    if not bad.empty:
        raise ValueError(f"{context} contains multiple geometry hashes for edge(s): {bad.to_dict()}")


def validate_geometry_hash_handoff(merged: pd.DataFrame, *, universe: str) -> pd.DataFrame:
    required = ["geometry_hash", "geometry_hash_repmeta", "edge_id", "edge_id_repmeta"]
    missing = [c for c in required if c not in merged.columns]
    if missing:
        raise ValueError(
            f"{universe}: Step 05 geometry handoff is missing comparison columns {missing}. "
            "Run Steps 01-04BB and 04C in order."
        )

    out = merged.copy()
    ledger_hash = out["geometry_hash"].astype(str).str.strip()
    representative_hash = out["geometry_hash_repmeta"].astype(str).str.strip()
    ledger_edge = out["edge_id"].astype(str).str.strip()
    representative_edge = out["edge_id_repmeta"].astype(str).str.strip()
    nonempty = (
        ledger_hash.ne("") & representative_hash.ne("")
        & ledger_hash.str.lower().ne("nan") & representative_hash.str.lower().ne("nan")
    )
    hash_match = nonempty & ledger_hash.eq(representative_hash)
    edge_match = ledger_edge.eq(representative_edge)
    valid = hash_match & edge_match
    if not valid.all():
        preview_cols = [
            "representative_id", "source_grid_candidate_id", "grid_candidate_id",
            "edge_id", "edge_id_repmeta", "geometry_hash", "geometry_hash_repmeta",
        ]
        preview_cols = [c for c in preview_cols if c in out.columns]
        preview = out.loc[~valid, preview_cols].drop_duplicates().head(30).to_string(index=False)
        raise RuntimeError(
            f"{universe}: representative geometry does not match threshold-grid ledger geometry. "
            f"mismatched_rows={int((~valid).sum())}\n{preview}"
        )

    out["representative_geometry_hash"] = representative_hash
    out["geometry_hash_match"] = True
    validate_payoff_context_frame(out, context=f"{universe} Step05 representative handoff")
    return out


def percentile_or_nan(values: Any, q: float) -> float:
    x = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    if len(x) == 0:
        return np.nan
    return float(np.nanpercentile(x, q))


def max_drawdown_from_r(values: Any) -> float:
    r = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return 0.0
    equity = pd.Series(r).cumsum()
    dd = equity - equity.cummax()
    return float(dd.min())


def profit_factor_from_r(values: Any) -> float:
    r = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return np.nan
    gp = float(r[r > 0].sum()) if np.any(r > 0) else 0.0
    gl = float(abs(r[r <= 0].sum())) if np.any(r <= 0) else 0.0
    if gl <= 0:
        return np.inf if gp > 0 else np.nan
    return gp / gl


def trade_metrics(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or "executed_r" not in df.columns:
        return {
            "ledger_trade_count": 0,
            "ledger_win_rate": np.nan,
            "ledger_expectancy_r": np.nan,
            "ledger_profit_factor": np.nan,
            "ledger_cumulative_r": 0.0,
            "ledger_max_drawdown_r": 0.0,
            "ledger_calmar_r": np.nan,
        }
    r = pd.to_numeric(df["executed_r"], errors="coerce").dropna().to_numpy(dtype=float)
    if len(r) == 0:
        return {
            "ledger_trade_count": 0,
            "ledger_win_rate": np.nan,
            "ledger_expectancy_r": np.nan,
            "ledger_profit_factor": np.nan,
            "ledger_cumulative_r": 0.0,
            "ledger_max_drawdown_r": 0.0,
            "ledger_calmar_r": np.nan,
        }
    cumulative = float(r.sum())
    dd = max_drawdown_from_r(r)
    return {
        "ledger_trade_count": int(len(r)),
        "ledger_win_rate": float(np.mean(r > 0)),
        "ledger_expectancy_r": float(np.mean(r)),
        "ledger_profit_factor": profit_factor_from_r(r),
        "ledger_cumulative_r": cumulative,
        "ledger_max_drawdown_r": dd,
        "ledger_calmar_r": cumulative / abs(dd) if dd < 0 else np.nan,
    }


def broker_risk_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Summarize broker-min-lot risk metadata already produced by Step 04B.

    Step 05 does not size positions. It only validates and carries these fields
    forward so Steps 08+ can use broker-realistic executable risk.
    """
    out: dict[str, Any] = {
        "ledger_broker_valid_risk_rows": 0,
        "ledger_broker_invalid_risk_rows": 0,
        "ledger_broker_valid_risk_row_rate": np.nan,
        "ledger_min_executable_risk_amount_min": np.nan,
        "ledger_min_executable_risk_amount_median": np.nan,
        "ledger_min_executable_risk_amount_p90": np.nan,
        "ledger_min_executable_risk_amount_max": np.nan,
        "ledger_risk_per_1_lot_median": np.nan,
        "ledger_risk_per_min_lot_median": np.nan,
        "ledger_broker_min_lot_risk_status_summary": "",
    }
    if df.empty or "has_valid_broker_min_lot_risk" not in df.columns:
        return out

    valid = df["has_valid_broker_min_lot_risk"].apply(normalize_bool)
    total = int(len(df))
    valid_count = int(valid.sum())
    out["ledger_broker_valid_risk_rows"] = valid_count
    out["ledger_broker_invalid_risk_rows"] = int(total - valid_count)
    out["ledger_broker_valid_risk_row_rate"] = float(valid_count / total) if total else np.nan

    if "min_executable_risk_amount" in df.columns:
        x = pd.to_numeric(df["min_executable_risk_amount"], errors="coerce")
        out["ledger_min_executable_risk_amount_min"] = percentile_or_nan(x, 0)
        out["ledger_min_executable_risk_amount_median"] = percentile_or_nan(x, 50)
        out["ledger_min_executable_risk_amount_p90"] = percentile_or_nan(x, 90)
        out["ledger_min_executable_risk_amount_max"] = percentile_or_nan(x, 100)
    if "risk_per_1_lot" in df.columns:
        out["ledger_risk_per_1_lot_median"] = percentile_or_nan(df["risk_per_1_lot"], 50)
    if "risk_per_min_lot" in df.columns:
        out["ledger_risk_per_min_lot_median"] = percentile_or_nan(df["risk_per_min_lot"], 50)
    if "broker_min_lot_risk_status" in df.columns:
        out["ledger_broker_min_lot_risk_status_summary"] = json.dumps(
            df["broker_min_lot_risk_status"].astype(str).value_counts(dropna=False).to_dict(),
            sort_keys=True,
        )
    return out


def validate_broker_risk_metadata(df: pd.DataFrame, *, context: str) -> None:
    if not BROKER_EXECUTABLE_RISK_REQUIRED:
        return
    missing = [c for c in BROKER_RISK_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{context} missing broker-min-lot metadata columns from patched Step 04B: {missing}"
        )

    valid_bool = df["has_valid_broker_min_lot_risk"].apply(normalize_bool)
    numeric_positive = pd.Series(True, index=df.index)
    for col in [
        "stop_distance_price",
        "broker_min_lot",
        "broker_lot_step",
        "broker_contract_size",
        "risk_per_1_lot",
        "risk_per_min_lot",
        "risk_per_lot_step",
        "min_executable_risk_amount",
    ]:
        numeric_positive &= pd.to_numeric(df[col], errors="coerce").gt(0)

    valid = valid_bool & numeric_positive
    invalid = df.loc[~valid].copy()
    if not invalid.empty:
        status_summary = {}
        if "broker_min_lot_risk_status" in invalid.columns:
            status_summary = invalid["broker_min_lot_risk_status"].astype(str).value_counts(dropna=False).to_dict()
        preview_cols = [
            "representative_id", "grid_candidate_id", "trade_uid", "edge_id", "asset",
            "entry_price", "sl_price", "stop_distance_price", "risk_per_1_lot",
            "broker_min_lot", "broker_lot_step", "min_executable_risk_amount",
            "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk",
        ]
        preview_cols = [c for c in preview_cols if c in invalid.columns]
        preview = invalid[preview_cols].head(30).to_string(index=False)
        raise RuntimeError(
            f"{context}: invalid broker-min-lot metadata in representative trade rows. "
            f"invalid_rows={len(invalid)}, statuses={status_summary}\n{preview}"
        )


def parse_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["event_time", "entry_time", "exit_time"]:
        if col in out.columns:
            out[col] = to_datetime_iso(out[col], utc=True, errors="coerce")
    return out


def load_representatives(path: Path, universe: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 04c_select_representative_threshold_candidates.py first. Missing: {path}")
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    if df.empty:
        df["representative_universe"] = universe
        return df
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    required = [
        "representative_id", "candidate_id", "source_grid_candidate_id", "grid_candidate_id",
        "edge_id", "asset", "family", "side_name", "side_value", "edge_classification",
        "candidate_type", "representative_roles", "primary_representative_role", "role_count",
        "threshold", "calibrated_threshold", "threshold_probability_column", "afml_bet_size_column",
        "calibrated_oof_path", "threshold_sweep_path", "edge_manifest_path",
        "trade_count", "expectancy_r", "profit_factor", "cumulative_r", "max_drawdown_r",
        "calmar_r", "avg_calibrated_probability", "avg_afml_bet_size",
        *GEOMETRY_COLUMNS,
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} missing required Step 05 columns: {missing}")
    if df["representative_id"].duplicated().any():
        bad = df[df["representative_id"].duplicated(keep=False)]
        raise ValueError("Duplicate representative_id values:\n" + bad[["representative_id", "edge_id", "threshold", "representative_roles"]].to_string(index=False))
    if df["source_grid_candidate_id"].duplicated().any():
        bad = df[df["source_grid_candidate_id"].duplicated(keep=False)]
        raise ValueError("Duplicate source_grid_candidate_id values:\n" + bad[["representative_id", "source_grid_candidate_id", "edge_id", "representative_roles"]].to_string(index=False))
    numeric = [
        "side_value", "role_count", "threshold", "calibrated_threshold", "plateau_index",
        "plateau_min_threshold", "plateau_max_threshold", "plateau_row_count", "plateau_trade_count_min",
        "plateau_trade_count_max", "trade_set_size", "trade_count", "take_rate", "win_rate",
        "expectancy_r", "profit_factor", "gross_profit_r", "gross_loss_r", "cumulative_r",
        "max_drawdown_r", "calmar_r", "precision", "avg_pred_prob", "avg_calibrated_probability",
        "avg_afml_bet_size", "local_positive_ratio", "local_expectancy_mean", "local_pf_mean",
        "high_recall_score", "balanced_score", "conservative_score", "max_afml_size_score",
        "score_trade_count", "score_expectancy", "score_profit_factor", "score_cumulative",
        "score_calmar", "score_local_stability", "score_afml_size", "score_calibrated_prob",
        *GEOMETRY_NUMERIC_COLUMNS,
    ]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    validate_single_geometry_per_edge(df, context=str(path.relative_to(V2_ROOT)))
    df["representative_universe"] = universe
    return df


def load_all_representatives(out_dir: Path) -> dict[str, pd.DataFrame]:
    return {universe: load_representatives(out_dir / filename, universe) for universe, filename in UNIVERSE_INPUTS.items()}


def load_threshold_grid_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 04b_build_threshold_grid_trade_ledger.py first. Missing: {path}")
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    if df.empty:
        raise ValueError(f"Threshold-grid ledger is empty: {path}")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    required = [
        "trade_uid", "grid_candidate_id", "candidate_id", "edge_id", "asset", "family",
        "side_name", "side_value", "threshold", "edge_classification", "calibrated_probability",
        "afml_bet_size", "event_time", "entry_time", "exit_time", "executed_r", "meta_label",
        "source_oof_row_number", "calibrated_oof_path", "threshold_sweep_path", "edge_manifest_path",
        "Event_Vol", *GEOMETRY_COLUMNS,
    ]
    if BROKER_EXECUTABLE_RISK_REQUIRED:
        required = required + BROKER_RISK_REQUIRED_COLUMNS
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{path} missing required threshold-grid ledger columns: {missing}")
    df = parse_datetime_columns(df)
    numeric_cols = [
        "side_value", "threshold", "raw_probability", "calibrated_probability", "afml_bet_size",
        "executed_r", "realized_r", "meta_label", "source_oof_row_number", "Event_Vol",
        *GEOMETRY_NUMERIC_COLUMNS,
        *BROKER_RISK_NUMERIC_COLUMNS,
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["grid_candidate_id", "event_time", "executed_r"]).copy()
    validate_single_geometry_per_edge(df, context=str(path.relative_to(V2_ROOT)))
    validate_broker_risk_metadata(df, context=str(path.relative_to(V2_ROOT)))
    return df.sort_values(["event_time", "grid_candidate_id", "trade_uid"]).reset_index(drop=True)


def build_representative_ledger_for_universe(reps: pd.DataFrame, grid_ledger: pd.DataFrame, universe: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if reps.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    selected_grid_ids = set(reps["source_grid_candidate_id"].astype(str))
    available_grid_ids = set(grid_ledger["grid_candidate_id"].astype(str))
    missing = sorted(selected_grid_ids - available_grid_ids)
    if missing and STRICT_MISSING_REPRESENTATIVE_VALIDATION:
        raise RuntimeError(f"{universe}: representatives missing from threshold-grid ledger:\n" + "\n".join(missing[:30]))

    subset = grid_ledger[grid_ledger["grid_candidate_id"].astype(str).isin(selected_grid_ids)].copy()
    rep_meta = reps.copy().rename(columns={
        "grid_candidate_id": "rep_grid_candidate_id",
        "candidate_id": "rep_candidate_id",
        "threshold": "rep_threshold",
        "trade_count": "rep_trade_count",
        "expectancy_r": "rep_expectancy_r",
        "profit_factor": "rep_profit_factor",
        "cumulative_r": "rep_cumulative_r",
        "max_drawdown_r": "rep_max_drawdown_r",
        "calmar_r": "rep_calmar_r",
        "avg_calibrated_probability": "rep_avg_calibrated_probability",
        "avg_afml_bet_size": "rep_avg_afml_bet_size",
    })
    merged = subset.merge(
        rep_meta,
        left_on="grid_candidate_id",
        right_on="source_grid_candidate_id",
        how="inner",
        suffixes=("", "_repmeta"),
        validate="many_to_one",
    )
    if merged.empty:
        raise RuntimeError(f"{universe}: no representative ledger rows after merge.")
    merged = validate_geometry_hash_handoff(merged, universe=universe)

    merged["representative_id"] = merged["representative_id"].astype(str)
    merged["candidate_id"] = merged["representative_id"]
    merged["source_grid_candidate_id"] = merged["source_grid_candidate_id"].astype(str)
    merged["selected_grid_candidate_id"] = merged.get("rep_grid_candidate_id", merged["grid_candidate_id"])
    merged["representative_universe"] = universe
    merged["candidate_type"] = merged["primary_representative_role"].astype(str)
    merged["representative_roles"] = merged["representative_roles"].astype(str)
    merged["primary_representative_role"] = merged["primary_representative_role"].astype(str)
    merged["role_count"] = pd.to_numeric(merged["role_count"], errors="coerce").fillna(1).astype(int)

    for base_col, rep_col in [
        ("threshold", "rep_threshold"),
        ("trade_count", "rep_trade_count"),
        ("expectancy_r", "rep_expectancy_r"),
        ("profit_factor", "rep_profit_factor"),
        ("cumulative_r", "rep_cumulative_r"),
        ("max_drawdown_r", "rep_max_drawdown_r"),
        ("calmar_r", "rep_calmar_r"),
        ("avg_calibrated_probability", "rep_avg_calibrated_probability"),
        ("avg_afml_bet_size", "rep_avg_afml_bet_size"),
    ]:
        if rep_col in merged.columns:
            merged[f"representative_{base_col}"] = merged[rep_col]
    merged["calibrated_threshold"] = merged["representative_threshold"]
    merged["representative_trade_uid"] = merged["representative_id"].astype(str) + "|" + merged["trade_uid"].astype(str)

    validation_rows = []
    summary_rows = []
    for representative_id, g in merged.groupby("representative_id", sort=True):
        rep_row = reps[reps["representative_id"].astype(str) == str(representative_id)].iloc[0]
        expected = safe_int(rep_row["trade_count"], 0)
        actual = int(len(g))
        broker_metrics = broker_risk_metrics(g)
        geometry_match = bool(g["geometry_hash_match"].apply(normalize_bool).all())
        validation_status = "ok" if actual == expected else "trade_count_mismatch"
        if not geometry_match:
            validation_status = "geometry_hash_mismatch" if validation_status == "ok" else validation_status + ";geometry_hash_mismatch"
        if BROKER_EXECUTABLE_RISK_REQUIRED and broker_metrics["ledger_broker_invalid_risk_rows"] > 0:
            validation_status = "broker_risk_metadata_invalid" if validation_status == "ok" else validation_status + ";broker_risk_metadata_invalid"
        validation_rows.append({
            "universe": universe,
            "representative_id": representative_id,
            "source_grid_candidate_id": rep_row["source_grid_candidate_id"],
            "edge_id": rep_row["edge_id"],
            "asset": rep_row["asset"],
            "family": rep_row["family"],
            "side_name": rep_row["side_name"],
            "threshold": safe_float(rep_row["threshold"]),
            "representative_roles": rep_row["representative_roles"],
            "edge_classification": rep_row["edge_classification"],
            "ledger_geometry_hash": str(g["geometry_hash"].iloc[0]),
            "representative_geometry_hash": str(rep_row["geometry_hash"]),
            "geometry_hash_match": geometry_match,
            "expected_trade_count": expected,
            "ledger_trade_count": actual,
            "trade_count_diff": actual - expected,
            "broker_valid_risk_rows": broker_metrics["ledger_broker_valid_risk_rows"],
            "broker_invalid_risk_rows": broker_metrics["ledger_broker_invalid_risk_rows"],
            "broker_valid_risk_row_rate": broker_metrics["ledger_broker_valid_risk_row_rate"],
            "broker_min_executable_risk_amount_median": broker_metrics["ledger_min_executable_risk_amount_median"],
            "broker_min_executable_risk_amount_p90": broker_metrics["ledger_min_executable_risk_amount_p90"],
            "broker_min_executable_risk_amount_max": broker_metrics["ledger_min_executable_risk_amount_max"],
            "broker_min_lot_risk_status_summary": broker_metrics["ledger_broker_min_lot_risk_status_summary"],
            "status": validation_status,
        })
        metrics = trade_metrics(g)
        summary_rows.append({
            "universe": universe,
            "representative_id": representative_id,
            "candidate_id": representative_id,
            "source_grid_candidate_id": rep_row["source_grid_candidate_id"],
            "edge_id": rep_row["edge_id"],
            "asset": rep_row["asset"],
            "family": rep_row["family"],
            "side_name": rep_row["side_name"],
            "side_value": safe_int(rep_row.get("side_value", np.nan)),
            **{col: rep_row.get(col, "") for col in GEOMETRY_COLUMNS},
            "geometry_hash_match": geometry_match,
            "threshold": safe_float(rep_row["threshold"]),
            "calibrated_threshold": safe_float(rep_row.get("calibrated_threshold", rep_row["threshold"])),
            "edge_classification": rep_row["edge_classification"],
            "candidate_type": rep_row.get("candidate_type", rep_row.get("primary_representative_role", "")),
            "representative_roles": rep_row["representative_roles"],
            "primary_representative_role": rep_row.get("primary_representative_role", ""),
            "role_count": safe_int(rep_row.get("role_count", 1)),
            "plateau_id": rep_row.get("plateau_id", ""),
            "plateau_min_threshold": safe_float(rep_row.get("plateau_min_threshold", np.nan)),
            "plateau_max_threshold": safe_float(rep_row.get("plateau_max_threshold", np.nan)),
            "plateau_row_count": safe_int(rep_row.get("plateau_row_count", 0)),
            "representative_trade_count": safe_int(rep_row.get("trade_count", 0)),
            "representative_expectancy_r": safe_float(rep_row.get("expectancy_r", np.nan)),
            "representative_profit_factor": safe_float(rep_row.get("profit_factor", np.nan)),
            "representative_cumulative_r": safe_float(rep_row.get("cumulative_r", np.nan)),
            "representative_max_drawdown_r": safe_float(rep_row.get("max_drawdown_r", np.nan)),
            "representative_calmar_r": safe_float(rep_row.get("calmar_r", np.nan)),
            "representative_avg_calibrated_probability": safe_float(rep_row.get("avg_calibrated_probability", np.nan)),
            "representative_avg_afml_bet_size": safe_float(rep_row.get("avg_afml_bet_size", np.nan)),
            **metrics,
            **broker_metrics,
            "ledger_avg_calibrated_probability": float(g["calibrated_probability"].mean()) if "calibrated_probability" in g.columns else np.nan,
            "ledger_avg_afml_bet_size": float(g["afml_bet_size"].mean()) if "afml_bet_size" in g.columns else np.nan,
            "ledger_min_event_time": g["event_time"].min() if "event_time" in g.columns else pd.NaT,
            "ledger_max_event_time": g["event_time"].max() if "event_time" in g.columns else pd.NaT,
            "ledger_nonzero_days": int(g["event_time"].dt.floor("D").nunique()) if "event_time" in g.columns else 0,
            "threshold_probability_column": rep_row.get("threshold_probability_column", ""),
            "afml_bet_size_column": rep_row.get("afml_bet_size_column", ""),
            "calibrated_oof_path": rep_row.get("calibrated_oof_path", ""),
            "threshold_sweep_path": rep_row.get("threshold_sweep_path", ""),
            "edge_manifest_path": rep_row.get("edge_manifest_path", ""),
            "edge_manifest_hash": rep_row.get("edge_manifest_hash", ""),
        })

    validation = pd.DataFrame(validation_rows)
    summary = pd.DataFrame(summary_rows)
    front = [
        "representative_trade_uid", "representative_id", "candidate_id", "source_grid_candidate_id",
        "selected_grid_candidate_id", "grid_candidate_id", "trade_uid", "edge_id", "asset", "family",
        "side_name", "side_value", "edge_classification", "classification_reason", "watchlist_quality_tag",
        "candidate_type", "representative_roles", "primary_representative_role", "role_count",
        "threshold", "representative_threshold", "calibrated_threshold", "plateau_id", "plateau_index",
        "plateau_min_threshold", "plateau_max_threshold", "plateau_row_count", "calibration_method",
        "threshold_probability_column", "afml_bet_size_column", "raw_probability", "calibrated_probability",
        "afml_bet_size", "event_time", "entry_time", "exit_time", "executed_r", "realized_r",
        "meta_label", "source_oof_row_number", "Event_Vol", "entry_price", "pt_price", "sl_price",
        "stop_distance_price", "take_profit_distance_price", "price_reward_to_risk_ratio",
        *GEOMETRY_COLUMNS, "representative_geometry_hash", "geometry_hash_match",
        "broker_contract_spec_key", "broker_symbol", "broker_min_lot", "broker_lot_step",
        "broker_contract_size", "broker_max_lot", "broker_trade_calc_mode",
        "broker_risk_model", "broker_risk_contract_version", "broker_quote_currency",
        "broker_profit_currency", "broker_simulation_currency", "broker_account_currency",
        "broker_quote_to_simulation_rate", "broker_profit_to_account_rate",
        "broker_conversion_mode", "broker_conversion_symbol", "broker_conversion_price_basis",
        "broker_conversion_source_timestamp", "broker_conversion_source_price",
        "broker_conversion_age_seconds", "broker_conversion_status", "risk_per_1_lot",
        "risk_per_min_lot", "risk_per_lot_step", "min_executable_risk_amount",
        "broker_min_lot_risk_status", "has_valid_broker_min_lot_risk",
        "representative_trade_count", "representative_expectancy_r",
        "representative_profit_factor", "representative_cumulative_r", "representative_max_drawdown_r",
        "representative_calmar_r", "representative_avg_calibrated_probability", "representative_avg_afml_bet_size",
        "calibrated_oof_path", "threshold_sweep_path", "edge_manifest_path", "edge_manifest_hash", "representative_universe",
    ]
    remaining = [c for c in merged.columns if c not in front]
    merged = merged[[c for c in front if c in merged.columns] + remaining]
    return merged.sort_values(["event_time", "representative_id", "representative_trade_uid"]).reset_index(drop=True), validation, summary


def validate_outputs(validation_report: pd.DataFrame) -> None:
    if validation_report.empty:
        return
    mismatches = validation_report[validation_report["trade_count_diff"] != 0]
    if not mismatches.empty and STRICT_TRADE_COUNT_VALIDATION:
        raise RuntimeError(
            "Step 05 trade-count validation failed. Representative ledger counts do not match selected representative trade_count.\n"
            + mismatches.head(30).to_string(index=False)
        )
    if "geometry_hash_match" not in validation_report.columns:
        raise RuntimeError("Step 05 geometry validation report is missing geometry_hash_match.")
    geometry_bad = validation_report[~validation_report["geometry_hash_match"].apply(normalize_bool)]
    if not geometry_bad.empty:
        raise RuntimeError(
            "Step 05 geometry-hash validation failed. Representative geometry differs from threshold-grid ledger geometry.\n"
            + geometry_bad.head(30).to_string(index=False)
        )
    if BROKER_EXECUTABLE_RISK_REQUIRED and "broker_invalid_risk_rows" in validation_report.columns:
        bad = validation_report[pd.to_numeric(validation_report["broker_invalid_risk_rows"], errors="coerce").fillna(0).astype(int) > 0]
        if not bad.empty:
            raise RuntimeError(
                "Step 05 broker-min-lot validation failed. Representative ledgers contain invalid broker risk metadata.\n"
                + bad.head(30).to_string(index=False)
            )


def build(write: bool = True) -> dict[str, Any]:
    out = V2_ROOT / "portfolio_allocator" / "outputs"
    _require_step04d(out)
    reps_by_universe = load_all_representatives(out)
    grid_ledger = load_threshold_grid_ledger(out / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv")
    ledgers: dict[str, pd.DataFrame] = {}
    validations = []
    summaries = []
    for universe, reps in reps_by_universe.items():
        ledger, validation, summary = build_representative_ledger_for_universe(reps, grid_ledger, universe)
        ledgers[universe] = ledger
        validations.append(validation)
        summaries.append(summary)
    validation_report = pd.concat(validations, ignore_index=True) if validations else pd.DataFrame()
    summary_report = pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame()
    validate_outputs(validation_report)
    geometry_mismatch_count = int((~validation_report["geometry_hash_match"].apply(normalize_bool)).sum()) if not validation_report.empty else 0
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "representative_trade_ledger_status": "PASS",
        "counts": {
            "representative_rows_all": int(len(reps_by_universe["all"])),
            "representative_rows_core": int(len(reps_by_universe["core"])),
            "representative_rows_watchlist": int(len(reps_by_universe["watchlist"])),
            "representative_rows_core_plus_watchlist": int(len(reps_by_universe["core_plus_watchlist"])),
            "representative_edges_all": int(reps_by_universe["all"]["edge_id"].nunique()) if not reps_by_universe["all"].empty else 0,
            "representative_edges_core": int(reps_by_universe["core"]["edge_id"].nunique()) if not reps_by_universe["core"].empty else 0,
            "representative_edges_watchlist": int(reps_by_universe["watchlist"]["edge_id"].nunique()) if not reps_by_universe["watchlist"].empty else 0,
            "ledger_rows_all": int(len(ledgers["all"])),
            "ledger_rows_core": int(len(ledgers["core"])),
            "ledger_rows_watchlist": int(len(ledgers["watchlist"])),
            "ledger_rows_core_plus_watchlist": int(len(ledgers["core_plus_watchlist"])),
            "summary_rows": int(len(summary_report)),
            "validation_rows": int(len(validation_report)),
            "geometry_hash_mismatch_count": geometry_mismatch_count,
            "broker_valid_risk_rows": int(pd.to_numeric(summary_report.get("ledger_broker_valid_risk_rows", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not summary_report.empty else 0,
            "broker_invalid_risk_rows": int(pd.to_numeric(summary_report.get("ledger_broker_invalid_risk_rows", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not summary_report.empty else 0,
        },
        "validation": {
            "strict_trade_count_validation": STRICT_TRADE_COUNT_VALIDATION,
            "strict_missing_representative_validation": STRICT_MISSING_REPRESENTATIVE_VALIDATION,
            "geometry_hash_equality_required": True,
            "geometry_required_columns": list(GEOMETRY_COLUMNS),
            "geometry_hash_mismatch_count": geometry_mismatch_count,
            "broker_executable_risk_required": BROKER_EXECUTABLE_RISK_REQUIRED,
            "broker_required_columns": BROKER_RISK_REQUIRED_COLUMNS,
            "mismatch_count": int((validation_report["trade_count_diff"] != 0).sum()) if not validation_report.empty else 0,
            "broker_invalid_representative_count": int((pd.to_numeric(validation_report.get("broker_invalid_risk_rows", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum()) if not validation_report.empty else 0,
        },
        "calibrated_afml_logic": {
            "representative_source": "04C selected source_grid_candidate_id",
            "trade_rows_source": "04B threshold-grid trade ledger core_plus_watchlist",
            "risk_sizing_applied_here": False,
            "notes": [
                "This step extracts selected representative thresholds from the threshold-grid ledger.",
                "No raw model inference or threshold recomputation is performed here.",
                "AFML bet size is preserved at trade level for later sizing.",
                "Finalized Event_Vol and trade geometry are required and preserved at trade level.",
                "Representative and threshold-grid geometry hashes must match exactly before any ledger is written.",
                "Broker min-lot metadata from 04B is required, validated, and preserved for later executable-risk simulation.",
                "No target risk, actual lot, account balance, or PnL sizing is calculated in Step 05.",
            ],
        },
        "broker_min_lot_handoff": {
            "broker_executable_risk_required": BROKER_EXECUTABLE_RISK_REQUIRED,
            "required_columns": BROKER_RISK_REQUIRED_COLUMNS,
            "optional_columns_preserved_if_present": BROKER_RISK_OPTIONAL_COLUMNS,
            "risk_sizing_applied_here": False,
            "purpose": "Strict carry-forward checkpoint from 04B/04C into representative ledgers consumed by downstream portfolio simulation.",
            "hash_coupling": "none for artifact files; geometry_hash remains a semantic geometry fingerprint",
        },
        "outputs": {
            "representative_trade_ledger_all": "portfolio_allocator/outputs/v2_representative_trade_ledger_all.csv",
            "representative_trade_ledger_core": "portfolio_allocator/outputs/v2_representative_trade_ledger_core.csv",
            "representative_trade_ledger_watchlist": "portfolio_allocator/outputs/v2_representative_trade_ledger_watchlist.csv",
            "representative_trade_ledger_core_plus_watchlist": "portfolio_allocator/outputs/v2_representative_trade_ledger_core_plus_watchlist.csv",
            "representative_trade_ledger_summary": "portfolio_allocator/outputs/v2_representative_trade_ledger_summary.csv",
            "representative_trade_ledger_validation_report": "portfolio_allocator/outputs/v2_representative_trade_ledger_validation_report.csv",
            "manifest": "portfolio_allocator/outputs/v2_representative_trade_ledger_manifest.json",
        },
    }
    if write:
        for universe, filename in UNIVERSE_OUTPUTS.items():
            ledgers[universe].to_csv(out / filename, index=False)
        summary_report.to_csv(out / "v2_representative_trade_ledger_summary.csv", index=False)
        validation_report.to_csv(out / "v2_representative_trade_ledger_validation_report.csv", index=False)
        write_json(out / "v2_representative_trade_ledger_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.05 representative trade ledger")
    print(f"Status: {manifest['representative_trade_ledger_status']}")
    counts = manifest["counts"]
    print(f"Representative rows all: {counts['representative_rows_all']}")
    print(f"Representative edges all: {counts['representative_edges_all']}")
    print(f"Ledger rows all: {counts['ledger_rows_all']}")
    print(f"Ledger rows core: {counts['ledger_rows_core']}")
    print(f"Ledger rows watchlist: {counts['ledger_rows_watchlist']}")
    print(f"Ledger rows core+watchlist: {counts['ledger_rows_core_plus_watchlist']}")
    print(f"Validation mismatches: {manifest['validation']['mismatch_count']}")
    print(f"Geometry hash mismatches: {manifest['validation']['geometry_hash_mismatch_count']}")
    print(f"Broker invalid representative groups: {manifest['validation']['broker_invalid_representative_count']}")


if __name__ == "__main__":
    main()
