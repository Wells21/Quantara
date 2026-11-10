from __future__ import annotations

import argparse
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
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

SCHEMA_VERSION = "v2.2"
PIPELINE_MODE = "v2_manifest_driven_calibrated_afml_compound_velocity_broker_minlot_validated"
from shared.backtest_statistics import afml_ch14_ch15_reference as ch15
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs" / "return_streams"
ASSETS_DIR = V2_ROOT / "assets"
# AFML exercise 16.1(a) p.244 (D059, owner approved 2026-10-30): returns are aligned to the average frequency of the
# bets (s.14.3) and booked daily by mark-to-market -- an open trade is marked at the last research mid bar close of
# each UTC day, the exit day books the move to exit_price plus the cost residual, so each trade sums to executed_r.
# "exit_date" (the whole executed_r on the exit day) is kept only to reproduce pre-D059 outputs.
DATE_AGGREGATION = "mark_to_market_daily"
DATE_AGGREGATIONS = ("exit_date", "mark_to_market_daily")
MTM_BAR_GLOB = "*_standard_quote_tick_bars.csv"
MTM_REQUIRED_COLUMNS = ["asset", "side_value", "entry_price", "exit_price", "stop_distance_price"]
MTM_SUM_TOLERANCE_R = 1e-9
NS_PER_DAY = 86_400_000_000_000
ALIGNMENT_PERIODS_DAYS = {"daily": 1.0, "weekly": 7.0, "monthly": 365.25 / 12.0}
IMPLEMENTED_ALIGNMENT_PERIOD = "daily"
FILL_MISSING_RETURNS_WITH_ZERO = True
PROFILE_ROLES = ["compound_velocity"]
UNIVERSES = {"core": ["core"], "watchlist": ["watchlist"], "core_plus_watchlist": ["core", "watchlist"]}
RETURN_MODES = {
    "raw_r": {"description": "Daily sum of executed_r.", "output_suffix": ""},
    "afml_weighted_r": {"description": "Daily sum of executed_r * afml_bet_size.", "output_suffix": "_afml_weighted_r"},
}
EPS = 1e-12

BROKER_EXECUTABLE_RISK_REQUIRED = True
BROKER_RISK_BOOL_COLUMN = "has_valid_broker_min_lot_risk"
BROKER_RISK_REQUIRED_COLUMNS = [
    "risk_per_1_lot",
    "broker_min_lot",
    "broker_lot_step",
    "broker_contract_size",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
    "broker_min_lot_risk_status",
    BROKER_RISK_BOOL_COLUMN,
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
    "risk_per_1_lot",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
]
BROKER_RISK_POSITIVE_REQUIRED_COLUMNS = [
    "broker_min_lot",
    "broker_lot_step",
    "broker_contract_size",
    "risk_per_1_lot",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
]



def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def safe_div(a: Any, b: Any) -> float:
    try:
        b = float(b)
        if abs(b) < EPS or not np.isfinite(b):
            return np.nan
        return float(a) / b
    except Exception:
        return np.nan


def normalize_bool(x: Any) -> bool:
    return str(x).strip().lower() in {"true", "1", "yes", "y"}


def coerce_if_fully_numeric(series: pd.Series) -> pd.Series:
    """Convert a metadata column only when every populated value is numeric.

    This avoids pandas ``errors="ignore"`` deprecation while preserving mixed
    text/audit columns exactly as written by upstream steps.
    """
    converted = pd.to_numeric(series, errors="coerce")
    populated = series.notna()
    if int(converted[populated].notna().sum()) == int(populated.sum()):
        return converted
    return series


def percentile_or_nan(values: Any, q: float) -> float:
    x = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    if len(x) == 0:
        return np.nan
    return float(np.nanpercentile(x, q))


def status_summary_text(series: pd.Series) -> str:
    if series.empty:
        return ""
    return json.dumps(series.astype(str).value_counts(dropna=False).to_dict(), sort_keys=True)


def validate_broker_minlot_metadata(df: pd.DataFrame, context: str) -> None:
    if not BROKER_EXECUTABLE_RISK_REQUIRED:
        return
    missing = [c for c in BROKER_RISK_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{context}: missing broker min-lot metadata columns from Step 05/04B: {missing}. "
            "Run the broker-min-lot-aware 04B and 05 scripts first."
        )
    valid_flag = df[BROKER_RISK_BOOL_COLUMN].apply(normalize_bool)
    numeric_positive = pd.Series(True, index=df.index)
    for col in BROKER_RISK_POSITIVE_REQUIRED_COLUMNS:
        vals = pd.to_numeric(df[col], errors="coerce")
        numeric_positive &= vals.gt(EPS) & vals.notna()
    invalid = ~(valid_flag & numeric_positive)
    if invalid.any():
        status_summary = status_summary_text(df.loc[invalid, "broker_min_lot_risk_status"]) if "broker_min_lot_risk_status" in df.columns else ""
        sample_cols = [c for c in [
            "representative_id", "edge_id", "asset", "trade_uid", "broker_min_lot_risk_status",
            "has_valid_broker_min_lot_risk", "risk_per_1_lot", "broker_min_lot",
            "broker_lot_step", "min_executable_risk_amount",
        ] if c in df.columns]
        sample = df.loc[invalid, sample_cols].head(20).to_string(index=False)
        raise ValueError(
            f"{context}: invalid broker min-lot metadata rows={int(invalid.sum())}; "
            f"statuses={status_summary}. Sample:\n{sample}"
        )


def broker_metadata_metrics(df: pd.DataFrame, prefix: str = "broker") -> dict[str, Any]:
    if df.empty or not all(c in df.columns for c in BROKER_RISK_REQUIRED_COLUMNS):
        return {
            f"{prefix}_valid_risk_row_count": 0,
            f"{prefix}_invalid_risk_row_count": 0,
            f"{prefix}_valid_risk_row_rate": np.nan,
            f"{prefix}_min_executable_risk_amount_min": np.nan,
            f"{prefix}_min_executable_risk_amount_median": np.nan,
            f"{prefix}_min_executable_risk_amount_p90": np.nan,
            f"{prefix}_min_executable_risk_amount_max": np.nan,
            f"{prefix}_risk_per_1_lot_median": np.nan,
            f"{prefix}_risk_per_min_lot_median": np.nan,
            f"{prefix}_risk_status_summary": "",
            f"{prefix}_symbols": "",
        }
    valid_flag = df[BROKER_RISK_BOOL_COLUMN].apply(normalize_bool)
    min_risk = pd.to_numeric(df["min_executable_risk_amount"], errors="coerce")
    risk_1 = pd.to_numeric(df["risk_per_1_lot"], errors="coerce")
    risk_min = pd.to_numeric(df["risk_per_min_lot"], errors="coerce")
    valid_count = int(valid_flag.sum())
    total = int(len(df))
    symbols = ""
    if "broker_symbol" in df.columns:
        symbols = "|".join(sorted(df["broker_symbol"].dropna().astype(str).unique()))
    return {
        f"{prefix}_valid_risk_row_count": valid_count,
        f"{prefix}_invalid_risk_row_count": int(total - valid_count),
        f"{prefix}_valid_risk_row_rate": safe_div(valid_count, total),
        f"{prefix}_min_executable_risk_amount_min": percentile_or_nan(min_risk, 0),
        f"{prefix}_min_executable_risk_amount_median": percentile_or_nan(min_risk, 50),
        f"{prefix}_min_executable_risk_amount_p90": percentile_or_nan(min_risk, 90),
        f"{prefix}_min_executable_risk_amount_max": percentile_or_nan(min_risk, 100),
        f"{prefix}_risk_per_1_lot_median": percentile_or_nan(risk_1, 50),
        f"{prefix}_risk_per_min_lot_median": percentile_or_nan(risk_min, 50),
        f"{prefix}_risk_status_summary": status_summary_text(df["broker_min_lot_risk_status"]) if "broker_min_lot_risk_status" in df.columns else "",
        f"{prefix}_symbols": symbols,
    }


def build_broker_metadata_by_representative(ledger: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if ledger.empty or "representative_id" not in ledger.columns:
        return pd.DataFrame(columns=["representative_id"])
    for representative_id, g in ledger.groupby("representative_id", sort=True):
        row = {"representative_id": representative_id}
        row.update(broker_metadata_metrics(g, prefix="broker"))
        rows.append(row)
    return pd.DataFrame(rows)


def max_drawdown_from_series(x: pd.Series) -> float:
    x = pd.to_numeric(x, errors="coerce").fillna(0.0)
    equity = x.cumsum()
    dd = equity - equity.cummax()
    return float(dd.min()) if len(dd) else 0.0


def stream_metrics(x: pd.Series) -> dict[str, Any]:
    x = pd.to_numeric(x, errors="coerce").fillna(0.0)
    total = float(x.sum())
    max_dd = max_drawdown_from_series(x)
    std = float(x.std(ddof=1)) if len(x) > 1 else np.nan
    downside = x[x < 0]
    downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else np.nan
    pos = int((x > 0).sum())
    neg = int((x < 0).sum())
    return {
        "total_r": total,
        "mean_daily_r": float(x.mean()) if len(x) else np.nan,
        "median_daily_r": float(x.median()) if len(x) else np.nan,
        "std_daily_r": std,
        "sharpe_like_daily": safe_div(float(x.mean()) if len(x) else np.nan, std),
        "sortino_like_daily": safe_div(float(x.mean()) if len(x) else np.nan, downside_std),
        "nonzero_days": int((x != 0).sum()),
        "positive_days": pos,
        "negative_days": neg,
        "zero_days": int((x == 0).sum()),
        "positive_day_rate": safe_div(pos, pos + neg),
        "max_drawdown_r_daily": max_dd,
        "calmar_r_daily": safe_div(total, abs(max_dd)) if max_dd < 0 else np.nan,
    }


def portfolio_metrics_from_matrix(mat: pd.DataFrame) -> dict[str, Any]:
    if mat.empty:
        return {"columns": 0, "days": 0, "portfolio_total_r": 0.0, "portfolio_max_drawdown_r": 0.0, "portfolio_nonzero_days": 0}
    m = stream_metrics(mat.sum(axis=1))
    return {**m, "columns": int(mat.shape[1]), "days": int(mat.shape[0]), "portfolio_total_r": m["total_r"], "portfolio_max_drawdown_r": m["max_drawdown_r_daily"], "portfolio_nonzero_days": m["nonzero_days"]}


def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 05_build_representative_trade_ledger.py first. Missing: {path}")
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    required = [
        "representative_id", "edge_id", "asset", "family", "edge_classification",
        "representative_roles", "primary_representative_role", "threshold", "entry_time",
        "exit_time", "executed_r", "afml_bet_size", "calibrated_probability",
    ]
    if BROKER_EXECUTABLE_RISK_REQUIRED:
        required += BROKER_RISK_REQUIRED_COLUMNS
    if DATE_AGGREGATION not in DATE_AGGREGATIONS:
        raise ValueError(f"DATE_AGGREGATION={DATE_AGGREGATION} is not one of {DATE_AGGREGATIONS}.")
    required += [c for c in MTM_REQUIRED_COLUMNS if c not in required]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Representative ledger missing required columns: {missing}")
    if "side_name" not in df.columns:
        df["side_name"] = ""
    for col in ["entry_time", "exit_time", "event_time"]:
        if col in df.columns:
            df[col] = to_datetime_iso(df[col], utc=True, errors="coerce")
    for col in ["executed_r", "afml_bet_size", "calibrated_probability", "threshold", "side_value", "exit_price", *BROKER_RISK_NUMERIC_COLUMNS]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["representative_id", "edge_id", "exit_time", "executed_r", "afml_bet_size", "calibrated_probability"]).copy()
    validate_broker_minlot_metadata(df, context="Step 06 representative trade ledger load")
    df["entry_date"] = df["entry_time"].dt.floor("D")
    df["exit_date"] = df["exit_time"].dt.floor("D")
    if "event_time" in df.columns:
        df["event_date"] = df["event_time"].dt.floor("D")
    df["return_raw_r"] = df["executed_r"].astype(float)
    df["return_afml_weighted_r"] = df["executed_r"].astype(float) * df["afml_bet_size"].astype(float)
    return df.sort_values(["exit_date", "representative_id"]).reset_index(drop=True)


def to_ns(x: pd.Series) -> np.ndarray:
    return pd.DatetimeIndex(pd.to_datetime(x, utc=True, format="mixed")).as_unit("ns").asi8


def load_day_marks(asset: str) -> tuple[np.ndarray, np.ndarray]:
    """(bar close times as int64 ns UTC, mid Close) of the asset's research bars, sorted; Datetime = bar close."""
    files = sorted((ASSETS_DIR / asset / "bars" / "standard_quote_tick").glob(MTM_BAR_GLOB))
    if len(files) != 1:
        raise FileNotFoundError(f"Mark-to-market needs exactly one {MTM_BAR_GLOB} for {asset}; found {[str(f) for f in files]}")
    b = pd.read_csv(files[0], usecols=["Datetime", "Close"], float_precision="round_trip")
    tt = to_ns(b["Datetime"])
    order = np.argsort(tt, kind="stable")
    return tt[order], b["Close"].to_numpy(dtype=float)[order]


def mark_to_market_bookings(ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One row per trade per UTC day it is open. Day R = side * (mark - previous mark) / stop_distance_price, the mark
    being the last bar close of the day after entry and the first previous mark entry_price; the exit day takes the
    move to exit_price plus executed_r - side * (exit_price - entry_price) / stop_distance_price (spread, swap,
    commission). A day without a new bar books 0; a bar at or before the entry is never a mark."""
    bad = ledger[MTM_REQUIRED_COLUMNS + ["entry_time"]].isna().any(axis=1) | ~ledger["side_value"].isin([-1, 1]) \
        | ~(ledger["stop_distance_price"] > EPS) | (ledger["exit_time"] < ledger["entry_time"])
    if bad.any():
        raise ValueError(f"Mark-to-market: invalid trade rows={int(bad.sum())} (missing prices/times, side not +-1, "
                         f"stop_distance_price <= 0 or exit before entry).")
    marks = {a: load_day_marks(a) for a in sorted(ledger["asset"].astype(str).unique())}
    rows_i, rows_d, rows_r = [], [], []
    cols = zip(ledger["asset"].astype(str), to_ns(ledger["entry_time"]), to_ns(ledger["exit_time"]),
               to_ns(ledger["entry_time"].dt.floor("D")), ledger["side_value"].to_numpy(float),
               ledger["entry_price"].to_numpy(float), ledger["exit_price"].to_numpy(float),
               ledger["stop_distance_price"].to_numpy(float), ledger["executed_r"].to_numpy(float))
    for i, (asset, a_ns, b_ns, d0_ns, side, p0, p1, sd, r) in enumerate(cols):
        bt, bc = marks[asset]
        n_days = int((b_ns - d0_ns) // NS_PER_DAY)          # full UTC days before the exit day
        prev = p0
        for k in range(n_days):
            j = int(np.searchsorted(bt, d0_ns + (k + 1) * NS_PER_DAY, side="left")) - 1
            if j >= 0 and bt[j] > a_ns:
                rows_i.append(i)
                rows_d.append(d0_ns + k * NS_PER_DAY)
                rows_r.append(side * (bc[j] - prev) / sd)
                prev = bc[j]
        rows_i.append(i)
        rows_d.append(d0_ns + n_days * NS_PER_DAY)
        rows_r.append(side * (p1 - prev) / sd + (r - side * (p1 - p0) / sd))
    idx = np.asarray(rows_i, dtype=np.int64)
    raw = np.asarray(rows_r, dtype=float)
    bookings = pd.DataFrame({
        "representative_id": ledger["representative_id"].to_numpy()[idx],
        "booking_date": to_datetime_iso(np.asarray(rows_d, dtype=np.int64), utc=True),
        "return_raw_r": raw,
        "return_afml_weighted_r": raw * ledger["afml_bet_size"].to_numpy(float)[idx],
    })
    sum_diff = float(np.abs(np.bincount(idx, weights=raw, minlength=len(ledger)) - ledger["executed_r"].to_numpy(float)).max())
    if sum_diff > MTM_SUM_TOLERANCE_R:
        raise ValueError(f"Mark-to-market: daily R does not sum to executed_r (max abs diff {sum_diff}).")
    check = {"trades": int(len(ledger)), "booking_rows": int(len(bookings)), "multi_day_trades": int(pd.Series(idx).value_counts().gt(1).sum()),
             "max_abs_trade_sum_diff_vs_executed_r": sum_diff, "bar_source": f"assets/<asset>/bars/standard_quote_tick/{MTM_BAR_GLOB}"}
    return bookings, check


def build_daily_bookings(ledger: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    if DATE_AGGREGATION == "exit_date":
        bookings = ledger[["representative_id", "exit_date", "return_raw_r", "return_afml_weighted_r"]].rename(columns={"exit_date": "booking_date"})
        return bookings.reset_index(drop=True), {"trades": int(len(ledger)), "booking_rows": int(len(bookings))}
    return mark_to_market_bookings(ledger)


def bet_frequency_alignment(ledger: pd.DataFrame, selected_reps: pd.DataFrame) -> dict[str, Any]:
    """AFML exercise 16.1(a): per edge the s.14.3 bet frequency (bets / year) of its profile representative; the average
    over edges -> average interval between bets -> the largest of daily / weekly / monthly not longer than it (floor
    daily). Only daily streams are built, so another period stops Step 06 for an owner decision."""
    per: dict[str, Any] = {}
    for rid, edge in zip(selected_reps["representative_id"].astype(str), selected_reps["edge_id"].astype(str)):
        t = ledger.loc[ledger["representative_id"].astype(str) == rid, ["entry_time", "exit_time", "side_value", "executed_r"]]
        g = ch15.general_and_performance_stats(t.rename(columns={"side_value": "side"}))
        per[edge] = {k: g[k] for k in ("n_trades", "n_bets", "frequency_bets_per_year", "avg_holding_period_days", "years")}
    if not per:
        raise ValueError("Bet-frequency alignment: no profile representatives.")
    avg = float(np.mean([v["frequency_bets_per_year"] for v in per.values()]))
    interval = 365.25 / avg
    fit = [k for k, d in ALIGNMENT_PERIODS_DAYS.items() if d <= interval]
    period = max(fit, key=ALIGNMENT_PERIODS_DAYS.get) if fit else "daily"
    out = {"per_edge": per, "average_bets_per_year": avg, "average_interval_between_bets_days": interval,
           "periods_days": ALIGNMENT_PERIODS_DAYS, "selected_period": period, "implemented_period": IMPLEMENTED_ALIGNMENT_PERIOD}
    if period != IMPLEMENTED_ALIGNMENT_PERIOD:
        raise ValueError(f"Bet-frequency alignment selects {period} (interval {interval:.2f} days) but Step 06 builds "
                         f"{IMPLEMENTED_ALIGNMENT_PERIOD} streams; owner decision needed (D059). {json.dumps(out, default=str)}")
    return out


def load_representatives(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Run 04c_select_representative_threshold_candidates.py first. Missing: {path}")
    # float_precision="round_trip": see config/mldp_deviation_register.json.
    df = pd.read_csv(path, float_precision="round_trip")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    required = ["representative_id", "edge_id", "asset", "family", "side_name", "side_value", "edge_classification", "threshold", "representative_roles", "primary_representative_role", "trade_count", "expectancy_r", "profit_factor", "cumulative_r", "max_drawdown_r", "calmar_r", "avg_calibrated_probability", "avg_afml_bet_size", "threshold_probability_column", "afml_bet_size_column", "calibrated_oof_path"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Representative candidates missing required columns: {missing}")
    if df["representative_id"].duplicated().any():
        bad = df[df["representative_id"].duplicated(keep=False)]
        raise ValueError("Duplicate representative_id values in representative candidates:\n" + bad[["representative_id", "edge_id", "threshold"]].to_string(index=False))
    for col in [c for c in df.columns if c not in {"representative_id", "candidate_id", "source_grid_candidate_id", "grid_candidate_id", "edge_id", "asset", "family", "side_name", "edge_classification", "classification_reason", "watchlist_quality_tag", "candidate_type", "representative_roles", "primary_representative_role", "threshold_probability_column", "afml_bet_size_column", "calibrated_oof_path", "threshold_sweep_path", "edge_manifest_path", "edge_manifest_hash", "plateau_id", "trade_set_hash", "compound_velocity_gate_level", "compound_velocity_gate_reason"}]:
        df[col] = coerce_if_fully_numeric(df[col])
    return df.reset_index(drop=True)


def get_full_date_index(bookings: pd.DataFrame) -> pd.DatetimeIndex:
    min_date = bookings["booking_date"].min()
    max_date = bookings["booking_date"].max()
    if pd.isna(min_date) or pd.isna(max_date):
        raise ValueError("Could not determine min/max date for return stream index.")
    return pd.date_range(start=min_date, end=max_date, freq="D", tz="UTC")


def return_column_for_mode(return_mode: str) -> str:
    if return_mode == "raw_r":
        return "return_raw_r"
    if return_mode == "afml_weighted_r":
        return "return_afml_weighted_r"
    raise ValueError(f"Unknown return_mode: {return_mode}")


def suffix_name(base: str, return_mode: str) -> str:
    return f"{base}{RETURN_MODES[return_mode]['output_suffix']}.csv"


def save_matrix(matrix: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    matrix.to_csv(path, index_label="date")


def build_empty_matrix(full_index: pd.DatetimeIndex) -> pd.DataFrame:
    mat = pd.DataFrame(index=full_index)
    mat.index.name = "date"
    return mat


def build_candidate_level_matrix(bookings: pd.DataFrame, representative_ids: list[str], full_index: pd.DatetimeIndex, return_mode: str) -> pd.DataFrame:
    if not representative_ids:
        return build_empty_matrix(full_index)
    subset = bookings[bookings["representative_id"].isin(representative_ids)].copy()
    if subset.empty:
        return build_empty_matrix(full_index)
    grouped = subset.groupby(["booking_date", "representative_id"], observed=True)[return_column_for_mode(return_mode)].sum().reset_index()
    matrix = grouped.pivot(index="booking_date", columns="representative_id", values=return_column_for_mode(return_mode)).reindex(full_index)
    if FILL_MISSING_RETURNS_WITH_ZERO:
        matrix = matrix.fillna(0.0)
    for rid in representative_ids:
        if rid not in matrix.columns:
            matrix[rid] = 0.0
    matrix = matrix[representative_ids].copy()
    matrix.index.name = "date"
    return matrix


def representative_has_role(roles_text: Any, role: str) -> bool:
    return role in str(roles_text).split("|")


def sort_reps_for_role(g: pd.DataFrame, role: str) -> pd.DataFrame:
    # Step 04C now makes compound_velocity_score the broker-aware, drawdown-adjusted
    # representative-selection score. Prefer explicit optimized columns when present,
    # then fall back to the canonical score for backward-compatible outputs.
    desired = [
        "broker_optimized_drawdown_adjusted_compound_velocity",
        "compound_velocity_score",
        "expected_calendar_days_to_target",
        "expected_active_days_to_target",
        "broker_optimized_compound_velocity_calendar",
        "compound_velocity",
        "compound_power",
        "stressed_profit_factor",
        "formula_trade_count",
        "local_positive_ratio",
    ]
    cols = [c for c in desired if c in g.columns]
    ascending_map = {
        "broker_optimized_drawdown_adjusted_compound_velocity": False,
        "compound_velocity_score": False,
        "expected_calendar_days_to_target": True,
        "expected_active_days_to_target": True,
        "broker_optimized_compound_velocity_calendar": False,
        "compound_velocity": False,
        "compound_power": False,
        "stressed_profit_factor": False,
        "formula_trade_count": False,
        "local_positive_ratio": False,
    }
    ascending = [ascending_map[c] for c in cols]
    return g.sort_values(cols or ["cumulative_r"], ascending=ascending or [False], na_position="last")


def select_profile_representatives(reps: pd.DataFrame, universe_name: str, allowed_classes: list[str], role: str) -> pd.DataFrame:
    universe_reps = reps[reps["edge_classification"].isin(allowed_classes)].copy()
    if universe_reps.empty:
        return pd.DataFrame(columns=list(reps.columns) + ["profile_role", "profile_universe"])
    role_reps = universe_reps[universe_reps["representative_roles"].apply(lambda x: representative_has_role(x, role))].copy()
    if role_reps.empty:
        return pd.DataFrame(columns=list(reps.columns) + ["profile_role", "profile_universe"])
    selected = pd.DataFrame([sort_reps_for_role(g, role).iloc[0] for _, g in role_reps.groupby("edge_id", sort=True)]).reset_index(drop=True)
    selected["profile_role"] = role
    selected["profile_universe"] = universe_name
    return selected


def build_profile_matrix(bookings: pd.DataFrame, selected_reps: pd.DataFrame, full_index: pd.DatetimeIndex, return_mode: str) -> pd.DataFrame:
    if selected_reps.empty:
        return build_empty_matrix(full_index)
    rep_ids = selected_reps["representative_id"].astype(str).tolist()
    rep_to_edge = dict(zip(selected_reps["representative_id"].astype(str), selected_reps["edge_id"].astype(str)))
    matrix = build_candidate_level_matrix(bookings, rep_ids, full_index, return_mode).rename(columns=rep_to_edge)
    if matrix.columns.duplicated().any():
        matrix = matrix.T.groupby(level=0).sum().T
    return matrix


def build_stream_diagnostics(matrix: pd.DataFrame, metadata: pd.DataFrame, id_col: str, return_mode: str) -> pd.DataFrame:
    rows = []
    for col in matrix.columns:
        md = metadata[metadata[id_col].astype(str) == str(col)] if id_col in metadata.columns and not metadata.empty else pd.DataFrame()
        first = md.iloc[0].to_dict() if not md.empty else {}
        rows.append({
            id_col: col,
            "return_mode": return_mode,
            "asset": first.get("asset", ""),
            "family": first.get("family", ""),
            "side_name": first.get("side_name", ""),
            "edge_id": first.get("edge_id", col if id_col == "edge_id" else ""),
            "representative_id": first.get("representative_id", col if id_col == "representative_id" else ""),
            "threshold": first.get("threshold", np.nan),
            "representative_roles": first.get("representative_roles", ""),
            "primary_representative_role": first.get("primary_representative_role", ""),
            "edge_classification": first.get("edge_classification", ""),
            "avg_calibrated_probability": first.get("avg_calibrated_probability", np.nan),
            "avg_afml_bet_size": first.get("avg_afml_bet_size", np.nan),
            "compound_velocity_score": first.get("compound_velocity_score", np.nan),
            "broker_optimized_drawdown_adjusted_compound_velocity": first.get("broker_optimized_drawdown_adjusted_compound_velocity", np.nan),
            "broker_optimized_compound_velocity_calendar": first.get("broker_optimized_compound_velocity_calendar", np.nan),
            "expected_calendar_days_to_target": first.get("expected_calendar_days_to_target", np.nan),
            "broker_valid_risk_row_rate": first.get("broker_valid_risk_row_rate", np.nan),
            "broker_min_executable_risk_amount_median": first.get("broker_min_executable_risk_amount_median", np.nan),
            "broker_min_executable_risk_amount_p90": first.get("broker_min_executable_risk_amount_p90", np.nan),
            "broker_min_executable_risk_amount_max": first.get("broker_min_executable_risk_amount_max", np.nan),
            "broker_risk_per_1_lot_median": first.get("broker_risk_per_1_lot_median", np.nan),
            "broker_risk_per_min_lot_median": first.get("broker_risk_per_min_lot_median", np.nan),
            "broker_risk_status_summary": first.get("broker_risk_status_summary", ""),
            "broker_symbols": first.get("broker_symbols", ""),
            "edge_manifest_hash": first.get("edge_manifest_hash", ""),
            **stream_metrics(matrix[col]),
        })
    return pd.DataFrame(rows).sort_values(["asset", "family", id_col]).reset_index(drop=True) if rows else pd.DataFrame(columns=[id_col, "return_mode", "total_r"])


def build(write: bool = True) -> dict[str, Any]:
    out = V2_ROOT / "portfolio_allocator" / "outputs"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger(out / "v2_representative_trade_ledger_core_plus_watchlist.csv")
    reps = load_representatives(out / "v2_representative_threshold_candidates_core_plus_watchlist.csv")
    broker_rep_summary = build_broker_metadata_by_representative(ledger)
    if not broker_rep_summary.empty:
        reps = reps.merge(broker_rep_summary, on="representative_id", how="left", validate="one_to_one")
    alignment = bet_frequency_alignment(ledger, select_profile_representatives(reps, "core_plus_watchlist", UNIVERSES["core_plus_watchlist"], PROFILE_ROLES[0]))
    bookings, booking_check = build_daily_bookings(ledger)
    full_index = get_full_date_index(bookings)
    ledger_broker_metrics = broker_metadata_metrics(ledger, prefix="ledger_broker")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "return_stream_status": "PASS",
        "input_ledger": "portfolio_allocator/outputs/v2_representative_trade_ledger_core_plus_watchlist.csv",
        "input_representatives": "portfolio_allocator/outputs/v2_representative_threshold_candidates_core_plus_watchlist.csv",
        "output_dir": "portfolio_allocator/outputs/return_streams",
        "date_aggregation": DATE_AGGREGATION,
        "booking_check": booking_check,
        "bet_frequency_alignment": alignment,
        "fill_missing_returns_with_zero": FILL_MISSING_RETURNS_WITH_ZERO,
        "return_modes": RETURN_MODES,
        "profile_roles": PROFILE_ROLES,
        "universes": UNIVERSES,
        "counts": {
            "ledger_rows_loaded": int(len(ledger)),
            "representatives_loaded": int(len(reps)),
            "unique_representatives": int(reps["representative_id"].nunique()),
            "unique_edges": int(reps["edge_id"].nunique()),
            "daily_rows": int(len(full_index)),
        },
        "broker_min_lot_validation": {
            "broker_executable_risk_required": BROKER_EXECUTABLE_RISK_REQUIRED,
            "required_columns": BROKER_RISK_REQUIRED_COLUMNS,
            "return_streams_remain_r_space": True,
            "risk_sizing_applied_here": False,
            "artifact_file_hash_coupling": "none",
            **ledger_broker_metrics,
        },
        "date_start": str(full_index.min()),
        "date_end": str(full_index.max()),
        "outputs": {},
        "notes": [
            "Only compound_velocity profile matrices are produced.",
            "Raw R files keep backward-compatible names; AFML-weighted files use _afml_weighted_r suffix.",
            "Step 06 keeps return streams in R-space for HRP/correlation analysis; broker-dollar sizing is still deferred to Steps 08+.",
            "Broker min-lot metadata from Step 05 is required and validated fail-closed before return streams are built.",
            "AFML exercise 16.1(a) (D059): streams are aligned to the average bet frequency (s.14.3; rule recorded in bet_frequency_alignment) and booked daily by mark-to-market at each UTC day's last research mid bar close; each trade's daily R sums to its executed_r (booking_check).",
        ],
    }
    candidate_diagnostics_all, profile_diagnostics_all, portfolio_summary_rows, profile_selection_rows = [], [], [], []
    for return_mode in RETURN_MODES:
        for universe_name, allowed_classes in UNIVERSES.items():
            universe_reps = reps[reps["edge_classification"].isin(allowed_classes)].copy()
            matrix = build_candidate_level_matrix(bookings, universe_reps["representative_id"].astype(str).tolist(), full_index, return_mode)
            matrix_path = OUTPUT_DIR / suffix_name(f"candidate_return_streams_daily_{universe_name}", return_mode)
            diagnostics_path = OUTPUT_DIR / suffix_name(f"candidate_return_streams_diagnostics_{universe_name}", return_mode)
            corr_path = OUTPUT_DIR / suffix_name(f"candidate_return_streams_corr_{universe_name}", return_mode)
            cov_path = OUTPUT_DIR / suffix_name(f"candidate_return_streams_cov_{universe_name}", return_mode)
            save_matrix(matrix, matrix_path)
            diagnostics = build_stream_diagnostics(matrix, universe_reps, "representative_id", return_mode)
            diagnostics["universe"] = universe_name
            diagnostics.to_csv(diagnostics_path, index=False)
            matrix.corr().to_csv(corr_path, index_label="representative_id")
            matrix.cov().to_csv(cov_path, index_label="representative_id")
            pm = portfolio_metrics_from_matrix(matrix)
            pm.update({"return_mode": return_mode, "stream_type": "candidate_level", "universe": universe_name, "profile_role": "", "columns": int(matrix.shape[1])})
            portfolio_summary_rows.append(pm)
            candidate_diagnostics_all.append(diagnostics)
            manifest["outputs"].update({f"candidate_matrix_{universe_name}_{return_mode}": str(matrix_path.relative_to(V2_ROOT)), f"candidate_diagnostics_{universe_name}_{return_mode}": str(diagnostics_path.relative_to(V2_ROOT)), f"candidate_corr_{universe_name}_{return_mode}": str(corr_path.relative_to(V2_ROOT)), f"candidate_cov_{universe_name}_{return_mode}": str(cov_path.relative_to(V2_ROOT))})
    profile_selection_cache: dict[tuple[str, str], pd.DataFrame] = {}
    for universe_name, allowed_classes in UNIVERSES.items():
        for role in PROFILE_ROLES:
            selected = select_profile_representatives(reps, universe_name, allowed_classes, role)
            profile_selection_cache[(universe_name, role)] = selected
            if not selected.empty:
                profile_selection_rows.append(selected)
    profile_selection = pd.concat(profile_selection_rows, ignore_index=True) if profile_selection_rows else pd.DataFrame()
    profile_selection_path = OUTPUT_DIR / "profile_representative_selection.csv"
    profile_selection.to_csv(profile_selection_path, index=False)
    manifest["outputs"]["profile_representative_selection"] = str(profile_selection_path.relative_to(V2_ROOT))
    broker_summary_path = OUTPUT_DIR / "representative_broker_minlot_metadata_summary.csv"
    broker_rep_summary.to_csv(broker_summary_path, index=False)
    manifest["outputs"]["representative_broker_minlot_metadata_summary"] = str(broker_summary_path.relative_to(V2_ROOT))
    for return_mode in RETURN_MODES:
        for universe_name in UNIVERSES:
            for role in PROFILE_ROLES:
                selected = profile_selection_cache[(universe_name, role)]
                matrix = build_profile_matrix(bookings, selected, full_index, return_mode)
                matrix_path = OUTPUT_DIR / suffix_name(f"profile_return_streams_daily_{universe_name}_{role}", return_mode)
                diagnostics_path = OUTPUT_DIR / suffix_name(f"profile_return_streams_diagnostics_{universe_name}_{role}", return_mode)
                corr_path = OUTPUT_DIR / suffix_name(f"profile_return_streams_corr_{universe_name}_{role}", return_mode)
                cov_path = OUTPUT_DIR / suffix_name(f"profile_return_streams_cov_{universe_name}_{role}", return_mode)
                save_matrix(matrix, matrix_path)
                diagnostics = build_stream_diagnostics(matrix, selected, "edge_id", return_mode)
                diagnostics["profile_universe"] = universe_name
                diagnostics["profile_role"] = role
                diagnostics.to_csv(diagnostics_path, index=False)
                matrix.corr().to_csv(corr_path, index_label="edge_id")
                matrix.cov().to_csv(cov_path, index_label="edge_id")
                pm = portfolio_metrics_from_matrix(matrix)
                pm.update({"return_mode": return_mode, "stream_type": "profile_level", "universe": universe_name, "profile_role": role, "columns": int(matrix.shape[1])})
                portfolio_summary_rows.append(pm)
                profile_diagnostics_all.append(diagnostics)
                manifest["outputs"].update({f"profile_matrix_{universe_name}_{role}_{return_mode}": str(matrix_path.relative_to(V2_ROOT)), f"profile_diagnostics_{universe_name}_{role}_{return_mode}": str(diagnostics_path.relative_to(V2_ROOT)), f"profile_corr_{universe_name}_{role}_{return_mode}": str(corr_path.relative_to(V2_ROOT)), f"profile_cov_{universe_name}_{role}_{return_mode}": str(cov_path.relative_to(V2_ROOT))})
    nonempty_candidate_diagnostics = [df for df in candidate_diagnostics_all if not df.empty]
    nonempty_profile_diagnostics = [df for df in profile_diagnostics_all if not df.empty]
    candidate_diag = (
        pd.concat(nonempty_candidate_diagnostics, ignore_index=True)
        if nonempty_candidate_diagnostics
        else pd.DataFrame()
    )
    profile_diag = (
        pd.concat(nonempty_profile_diagnostics, ignore_index=True)
        if nonempty_profile_diagnostics
        else pd.DataFrame()
    )
    portfolio_summary = pd.DataFrame(portfolio_summary_rows)
    leading = ["return_mode", "stream_type", "universe", "profile_role", "columns", "days", "portfolio_total_r", "portfolio_max_drawdown_r", "calmar_r_daily", "portfolio_nonzero_days"]
    if not portfolio_summary.empty:
        portfolio_summary = portfolio_summary[[c for c in leading if c in portfolio_summary.columns] + [c for c in portfolio_summary.columns if c not in leading]]
    candidate_diag_path = OUTPUT_DIR / "candidate_return_streams_diagnostics_all_universes.csv"
    profile_diag_path = OUTPUT_DIR / "profile_return_streams_diagnostics_all.csv"
    portfolio_summary_path = OUTPUT_DIR / "return_streams_portfolio_summary.csv"
    candidate_diag.to_csv(candidate_diag_path, index=False)
    profile_diag.to_csv(profile_diag_path, index=False)
    portfolio_summary.to_csv(portfolio_summary_path, index=False)
    manifest["outputs"].update({"candidate_diagnostics_all_universes": str(candidate_diag_path.relative_to(V2_ROOT)), "profile_diagnostics_all": str(profile_diag_path.relative_to(V2_ROOT)), "portfolio_summary": str(portfolio_summary_path.relative_to(V2_ROOT))})
    manifest["candidate_matrices"] = {f"{universe}_{mode}": {"columns": int(reps[reps["edge_classification"].isin(classes)]["representative_id"].nunique())} for mode in RETURN_MODES for universe, classes in UNIVERSES.items()}
    manifest["profile_matrices"] = {f"{universe}_{role}_{mode}": {"columns": int(profile_selection_cache[(universe, role)]["edge_id"].nunique()) if not profile_selection_cache[(universe, role)].empty else 0} for mode in RETURN_MODES for universe in UNIVERSES for role in PROFILE_ROLES}
    if write:
        write_json(OUTPUT_DIR / "return_streams_manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.06 representative return streams")
    print(f"Status: {manifest['return_stream_status']}")
    c = manifest["counts"]
    print(f"Ledger rows loaded: {c['ledger_rows_loaded']}")
    print(f"Representatives loaded: {c['representatives_loaded']}")
    print(f"Unique edges: {c['unique_edges']}")
    print(f"Daily rows: {c['daily_rows']}")
    print(f"Booking: {manifest['date_aggregation']} (alignment rule: {manifest['bet_frequency_alignment']['selected_period']}, "
          f"max trade-sum diff {manifest['booking_check'].get('max_abs_trade_sum_diff_vs_executed_r', 0.0):.2e})")
    print(f"Profiles: {', '.join(PROFILE_ROLES)}")
    print("Broker min-lot metadata: required and validated; return streams remain R-space")
    print(f"Output dir: {manifest['output_dir']}")


if __name__ == "__main__":
    main()
