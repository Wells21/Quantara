# QUANTARA_V3_SEMANTIC_PHASE2_EMPIRICAL_EXECUTABLE_PAYOFF_V1_REV3
# QUANTARA_V3_EXACT_OOF_ECONOMICS_PATCH_V1
from __future__ import annotations

# QUANTARA_V3_SEMANTIC_PHASE3A_PROVENANCE_CONTEXT_V1

import argparse
import gc
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = lambda x, **kwargs: x

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.broker_risk import (  # noqa: E402
    HistoricalConversionStore,
    load_broker_profile,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.calibration.sigmoid_afml import (  # noqa: E402
    AFML_SIZING_METHOD,
    PAYOFF_PROFILE_VERSION,
    apply_sigmoid_calibrator,
    empirical_payoff_profile,
    load_calibrator_json,
    payoff_profile_afml_components,
    validate_payoff_profile,
)
from shared.calibration.fit_sigmoid_calibrator_and_thresholds import (  # noqa: E402
    max_drawdown_from_r,
    safe_div as _shared_safe_div,
)
from shared.edges.edge_layout import get_edge_layout, split_edge_name  # noqa: E402
from shared.models.cpcv_paths import path_columns  # noqa: E402
from shared.validation.v3_semantic_contract import (  # noqa: E402
    attach_payoff_context_columns,
    validate_payoff_context_mapping,
)

SCHEMA_VERSION = "v2.6"
PIPELINE_MODE = "v2_manifest_driven_calibrated_afml"
PROBABILITY_COLUMN = "calibrated_probability"
AFML_COLUMN = "afml_bet_size"
INCLUDED_CLASSIFICATIONS = {"core", "watchlist"}
LOCAL_WINDOW = 5
PROBABILITY_EPSILON = 0.0
WRITE_ONLY_VIABLE_THRESHOLDS = True
STRICT_TRADE_COUNT_VALIDATION = True
EXECUTION_CONTRACT_VERSION = "v3_oof_exact_bidask_economic_label_v2"

# QUANTARA_V3_LIVE_CONSISTENT_THRESHOLD_GRID_FIX (2026-09-18): the threshold
# grid used to be loaded from each edge's Step04 calibrated_threshold_sweep.csv,
# which is built on CAL_PROB_COL (calibrated_probability, the CPCV-fold-averaged
# research diagnostic -- see config/mldp_deviation_register.json D015). But
# prepare_edge_oof() below deliberately REBUILDS calibrated_probability from
# raw_probability using the single live sigmoid calibrator, because trade
# SELECTION at allocator level must live on the same probability axis live
# scoring will use. Loading threshold VALUES and per-threshold economics from
# the OLD (CPCV-based) sweep while selecting actual ledger rows on the NEW
# (live-consistent) probability caused systematic trade-count mismatches
# between the two -- sometimes by over a thousand rows for the same edge,
# since the two calibrations can diverge sharply and even reorder ranks.
# Fix: build the ENTIRE threshold grid (candidate threshold values, trade
# metrics, empirical payoff profile, AFML components, payoff context) directly
# from prepare_edge_oof()'s own live-consistent frame, so grid gating and
# ledger row selection always operate on the exact same population. This
# duplicates (rather than imports) shared/calibration/fit_sigmoid_calibrator_
# and_thresholds.py's threshold-grid logic on purpose: that module is tightly
# coupled to its own CAL_PROB_COL/ECONOMIC_R_COL module constants for the
# edge-level Step04 diagnostic sweep, which must keep working unchanged; this
# allocator-level grid is a deliberately separate, live-consistent view.
THRESHOLD_STEP = 0.01
THRESHOLD_MIN_BOUND = 0.01
THRESHOLD_MAX_BOUND = 0.99
THRESHOLD_DECIMALS = 2

EXPECTED_SIDE_GATES = {
    "min_trade_abs": 100,
    "min_trade_frac_of_side_max": 0.08,
    "min_expectancy_r": 0.0,
    "min_profit_factor": 1.02,
    "min_cumulative_r": 0.0,
    "min_calmar_r": 0.50,
    "min_local_positive_ratio": 0.60,
}

OPTIONAL_OOF_COLUMNS_TO_KEEP = [
    "raw_probability", "oof_prob_raw", "calibration_pred_count_sigmoid", "calibration_status",
    "meta_label", "quote_executable_meta_label", "realized_r", "executed_r", "side", "t1", "barrier_hit", "Event_Vol",
    "execution_status", "execution_contract_version", "execution_geometry_hash",
    "signal_mid", "entry_bid", "entry_ask", "entry_spread", "entry_spread_bps",
    "entry_lag_ms", "entry_drift_bps", "exit_price", "exit_reason",
    "Regime_State", "Regime_Entropy", "Regime_Duration",
]

# Broker-risk inputs are the local, hashless artifacts produced by Steps 00A/00B.
BROKER_PROFILE_PATH = (
    V2_ROOT / "portfolio_allocator" / "config" / "broker_profiles" / "mt5_broker_profile.json"
)
BROKER_CONVERSION_MANIFEST_PATH = (
    V2_ROOT / "portfolio_allocator" / "config" / "broker_conversions" / "historical_conversion_manifest.json"
)
BROKER_PRICE_COLUMNS = ["entry_price", "pt_price", "sl_price"]
BROKER_RISK_MODEL = "price_distance_x_contract_size_x_causal_profit_to_account_rate"
BROKER_RISK_CONTRACT_VERSION = "historical_broker_conversion_v2_3"
STRICT_BROKER_RISK = True
LEDGER_WRITE_CHUNK_SIZE = 100_000
STREAM_BUFFER_ROW_LIMIT = 250_000
STREAM_TEMP_SUFFIX = ".stream_tmp"
THRESHOLD_SELECTION_MODE = "per_edge_probability_sort_searchsorted_restore_chronological_order"

# AFML Ch.12 walkthrough (owner decision D052, 2026-10-26): every threshold of the grid is also scored on every CPCV
# backtest path (AFML s.12.4.2 step 5). Each path's forecasts go through the same live calibrator, the same frozen
# payoff profile and the same broker-risk columns; the per-path trades are streamed to a compact path ledger that
# 04C uses to select the threshold by the mean across paths (s.12.5). Fail closed when an edge's calibrated OOF has no
# cpcv_path_XX_raw_probability columns, unless --allow-legacy-single-path is given (pre-D052 artifacts only).
REQUIRE_CPCV_PATHS = True
PATH_LEDGER_COLUMNS = [
    "grid_candidate_id", "edge_id", "asset", "threshold", "cpcv_path", "source_oof_row_number",
    "event_time", "exit_time", "executed_r", "afml_bet_size",
    "risk_per_1_lot", "broker_min_lot", "broker_lot_step", "has_valid_broker_min_lot_risk", "broker_min_lot_risk_status",
]

# Fixed, ordered schema for the single streamed core+watchlist ledger.
# A fixed schema is required because streaming appends cannot discover a later
# edge-specific column and retroactively add it to earlier CSV rows.
LEDGER_OUTPUT_COLUMNS = [
    "trade_uid",
    "grid_candidate_id",
    "candidate_id",
    "edge_id",
    "asset",
    "family",
    "side_name",
    "side_value",
    "threshold",
    "edge_classification",
    "raw_probability",
    "calibrated_probability",
    "afml_bet_size",
    "afml_sizing_method",
    "payoff_profile_version",
    "payoff_profile_valid",
    "payoff_profile_invalid_reason",
    "payoff_context_version",
    "payoff_context_id",
    "payoff_context_edge_id",
    "gross_label_contract_version",
    "executed_target_contract_version",
    "threshold_probability_column",
    "payoff_trade_count",
    "payoff_win_count",
    "payoff_loss_count",
    "payoff_zero_count",
    "payoff_zero_rate",
    "min_wins_required",
    "min_losses_required",
    "empirical_avg_win_r",
    "empirical_avg_loss_r",
    "empirical_reward_risk_ratio",
    "empirical_break_even_probability",
    "event_time",
    "entry_time",
    "exit_time",
    "executed_r",
    "execution_status",
    "execution_contract_version",
    "execution_geometry_hash",
    "signal_mid",
    "entry_bid",
    "entry_ask",
    "entry_spread",
    "entry_spread_bps",
    "entry_lag_ms",
    "entry_drift_bps",
    "exit_price",
    "exit_reason",
    "realized_r",
    "meta_label",
    "executed_meta_label",
    "source_oof_row_number",
    "entry_price",
    "pt_price",
    "sl_price",
    "stop_distance_price",
    "take_profit_distance_price",
    "price_reward_to_risk_ratio",
    "broker_contract_spec_key",
    "broker_symbol",
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
    "broker_min_lot",
    "broker_lot_step",
    "broker_contract_size",
    "broker_risk_model",
    "broker_quote_currency",
    "broker_simulation_currency",
    "broker_quote_to_simulation_rate",
    "risk_per_1_lot",
    "risk_per_min_lot",
    "risk_per_lot_step",
    "min_executable_risk_amount",
    "broker_min_lot_risk_status",
    "has_valid_broker_min_lot_risk",
    "calibrated_oof_path",
    "threshold_sweep_path",
    "edge_manifest_path",
    "edge_manifest_hash",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def clean_threshold_for_id(threshold: float) -> str:
    return f"{float(threshold):.2f}".replace(".", "p")


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()[:n]


def make_grid_candidate_id(edge_id: str, threshold: float) -> str:
    return f"{edge_id.replace('/', '_')}_grid_thr{clean_threshold_for_id(threshold)}"


def make_trade_uid(grid_candidate_id: str, event_time: Any, exit_time: Any, row_number: int) -> str:
    return short_hash(f"{grid_candidate_id}|{event_time}|{exit_time}|{row_number}")


def parse_datetime(series: pd.Series) -> pd.Series:
    return to_datetime_iso(series, utc=True, errors="coerce")


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
    gross_profit = float(r[r > 0].sum()) if np.any(r > 0) else 0.0
    gross_loss = float(abs(r[r <= 0].sum())) if np.any(r <= 0) else 0.0
    if gross_loss <= 0:
        return np.inf if gross_profit > 0 else np.nan
    return gross_profit / gross_loss



@dataclass(frozen=True)
class BrokerRiskContext:
    profile: Any
    conversion_store: HistoricalConversionStore
    profile_path: Path
    conversion_manifest_path: Path
    conversion_manifest: dict[str, Any]
    max_staleness_seconds: int


@dataclass(frozen=True)
class PreparedEdgeOOF:
    """One broker-enriched edge OOF prepared once for all of its thresholds."""

    frame: pd.DataFrame
    sorted_probabilities: np.ndarray
    sorted_positions: np.ndarray
    calibrated_oof_path: Path
    edge_id: str
    asset: str
    side_value: int | None
    path_calibrated: Any = None  # (n_paths, len(frame)) live-calibrated CPCV path probabilities (D052), or None

    def select_positions(self, threshold: float) -> np.ndarray:
        """Return chronological row positions passing threshold via binary search."""
        cutoff = int(
            np.searchsorted(
                self.sorted_probabilities,
                float(threshold) - PROBABILITY_EPSILON,
                side="left",
            )
        )
        if cutoff >= len(self.sorted_positions):
            return np.empty(0, dtype=np.int64)
        # sorted_positions is probability-ordered. Restore the original
        # chronological OOF order so trade metrics and downstream files remain
        # identical to the previous boolean-mask implementation.
        return np.sort(self.sorted_positions[cutoff:], kind="quicksort")


class CombinedLedgerStreamWriter:
    """Bounded-memory atomic writer for the single core+watchlist ledger."""

    def __init__(
        self,
        final_path: Path,
        *,
        columns: list[str],
        buffer_row_limit: int = STREAM_BUFFER_ROW_LIMIT,
        csv_chunk_size: int = LEDGER_WRITE_CHUNK_SIZE,
    ) -> None:
        self.final_path = Path(final_path)
        self.temp_path = self.final_path.with_suffix(self.final_path.suffix + STREAM_TEMP_SUFFIX)
        self.columns = list(columns)
        self.buffer_row_limit = int(buffer_row_limit)
        self.csv_chunk_size = int(csv_chunk_size)
        self._buffer: list[pd.DataFrame] = []
        self._buffer_rows = 0
        self._header_written = False
        self.rows_received = 0
        self.rows_flushed = 0
        self.flush_count = 0
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        if self.temp_path.exists():
            self.temp_path.unlink()

    def _normalise(self, frame: pd.DataFrame) -> pd.DataFrame:
        missing = [column for column in self.columns if column not in frame.columns]
        if missing:
            frame = frame.copy()
            for column in missing:
                frame[column] = np.nan
        extra = [column for column in frame.columns if column not in self.columns]
        if extra or list(frame.columns) != self.columns:
            frame = frame.reindex(columns=self.columns)
        return frame

    def append(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        normalised = self._normalise(frame)
        self._buffer.append(normalised)
        rows = int(len(normalised))
        self._buffer_rows += rows
        self.rows_received += rows
        if self._buffer_rows >= self.buffer_row_limit:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        if len(self._buffer) == 1:
            block = self._buffer[0]
        else:
            block = pd.concat(self._buffer, ignore_index=True, copy=False)
        block.to_csv(
            self.temp_path,
            index=False,
            mode="a" if self._header_written else "w",
            header=not self._header_written,
            chunksize=self.csv_chunk_size,
        )
        self._header_written = True
        self.rows_flushed += int(len(block))
        self.flush_count += 1
        self._buffer.clear()
        self._buffer_rows = 0
        del block

    def commit(self) -> None:
        self.flush()
        if not self._header_written:
            pd.DataFrame(columns=self.columns).to_csv(self.temp_path, index=False)
            self._header_written = True
        if self.rows_flushed != self.rows_received:
            raise RuntimeError(
                "Step 04B streamed ledger row-count validation failed: "
                f"received={self.rows_received} flushed={self.rows_flushed}"
            )
        os.replace(self.temp_path, self.final_path)

    def abort(self) -> None:
        self._buffer.clear()
        self._buffer_rows = 0
        if self.temp_path.exists():
            self.temp_path.unlink()


def load_broker_risk_context(
    profile_path: Path = BROKER_PROFILE_PATH,
    conversion_manifest_path: Path = BROKER_CONVERSION_MANIFEST_PATH,
) -> tuple[BrokerRiskContext, dict[str, Any]]:
    """Load local broker contracts and causal conversion files with no hash coupling."""
    profile_path = Path(profile_path).expanduser().resolve()
    conversion_manifest_path = Path(conversion_manifest_path).expanduser().resolve()
    if not conversion_manifest_path.exists():
        raise FileNotFoundError(
            "Missing historical conversion manifest. Run 00b_build_historical_conversion_artifacts.py first: "
            f"{conversion_manifest_path}"
        )
    manifest = json.loads(conversion_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError(f"Historical conversion manifest must be a JSON object: {conversion_manifest_path}")
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    if str(summary.get("status", "")).upper() != "PASS":
        raise ValueError(f"Historical conversion manifest status must be PASS: {summary.get('status')!r}")
    artifact_policy = str(manifest.get("artifact_policy") or "")
    if "hashless" not in artifact_policy.lower():
        raise ValueError("Historical conversion manifest must declare a hashless artifact policy")
    validation = manifest.get("validation_policy") if isinstance(manifest.get("validation_policy"), dict) else {}
    max_staleness_seconds = int(validation.get("max_staleness_seconds_for_allocator_lookup", 432000))
    profile = load_broker_profile(
        profile_path,
        max_staleness_seconds=max_staleness_seconds,
        require_status_pass=True,
    )
    manifest_currency = str(manifest.get("account_currency") or "").strip().upper()
    if manifest_currency and manifest_currency != profile.account_currency:
        raise ValueError(
            "Broker profile and conversion manifest account currencies differ: "
            f"{profile.account_currency} vs {manifest_currency}"
        )
    series = manifest.get("series") if isinstance(manifest.get("series"), dict) else {}
    required_symbols = sorted({
        conversion.symbol
        for conversion in profile.conversions.values()
        if conversion.mode in {"direct_symbol", "inverse_symbol"}
    })
    for symbol in required_symbols:
        metadata = series.get(symbol)
        if not isinstance(metadata, dict) or str(metadata.get("status", "")).upper() != "PASS":
            raise ValueError(f"Required historical conversion series is missing or not PASS: {symbol}")
    store = HistoricalConversionStore.from_manifest(conversion_manifest_path)
    for symbol in required_symbols:
        frame = store.get(symbol)
        if frame.empty:
            raise ValueError(f"Historical conversion series is empty: {symbol}")
    context = BrokerRiskContext(
        profile=profile,
        conversion_store=store,
        profile_path=profile_path,
        conversion_manifest_path=conversion_manifest_path,
        conversion_manifest=manifest,
        max_staleness_seconds=max_staleness_seconds,
    )
    context_manifest = {
        "enabled": True,
        "status": "PASS",
        "risk_contract_version": BROKER_RISK_CONTRACT_VERSION,
        "risk_model": BROKER_RISK_MODEL,
        "account_currency": profile.account_currency,
        "profile_path": str(profile_path),
        "conversion_manifest_path": str(conversion_manifest_path),
        "profile_artifact_policy": profile.artifact_policy,
        "conversion_artifact_policy": artifact_policy,
        "hash_coupling": "none",
        "legacy_static_broker_contract_specs_consumed": False,
        "contract_count": int(len(profile.contracts)),
        "required_conversion_symbols": required_symbols,
        "conversion_timeframe": manifest.get("timeframe"),
        "max_staleness_seconds": max_staleness_seconds,
        "warnings": [],
    }
    return context, context_manifest


def broker_risk_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or "min_executable_risk_amount" not in df.columns:
        return {
            "ledger_valid_broker_min_lot_risk_rows": 0,
            "ledger_missing_broker_min_lot_risk_rows": int(len(df)),
            "ledger_min_executable_risk_amount_min": np.nan,
            "ledger_min_executable_risk_amount_median": np.nan,
            "ledger_min_executable_risk_amount_p90": np.nan,
            "ledger_min_executable_risk_amount_max": np.nan,
            "ledger_broker_min_lot_risk_statuses": "",
            "ledger_broker_conversion_statuses": "",
        }
    valid = df.get("has_valid_broker_min_lot_risk", pd.Series(False, index=df.index)).astype(bool)
    amounts = pd.to_numeric(df.loc[valid, "min_executable_risk_amount"], errors="coerce").dropna()
    statuses = "|".join(sorted(set(df.get("broker_min_lot_risk_status", pd.Series(dtype=str)).astype(str).dropna().tolist())))
    conversion_statuses = "|".join(sorted(set(df.get("broker_conversion_status", pd.Series(dtype=str)).astype(str).dropna().tolist())))
    return {
        "ledger_valid_broker_min_lot_risk_rows": int(valid.sum()),
        "ledger_missing_broker_min_lot_risk_rows": int((~valid).sum()),
        "ledger_min_executable_risk_amount_min": float(amounts.min()) if not amounts.empty else np.nan,
        "ledger_min_executable_risk_amount_median": float(amounts.median()) if not amounts.empty else np.nan,
        "ledger_min_executable_risk_amount_p90": float(amounts.quantile(0.90)) if not amounts.empty else np.nan,
        "ledger_min_executable_risk_amount_max": float(amounts.max()) if not amounts.empty else np.nan,
        "ledger_broker_min_lot_risk_statuses": statuses,
        "ledger_broker_conversion_statuses": conversion_statuses,
    }


def add_broker_min_lot_risk_columns(
    ledger: pd.DataFrame,
    broker_context: BrokerRiskContext,
    *,
    asset: str | None = None,
) -> pd.DataFrame:
    """Attach broker-realistic causal minimum-lot risk without selecting trades."""
    out = ledger.copy()
    if asset is not None:
        out["asset"] = str(asset).strip().upper()
    for col in BROKER_PRICE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce") if col in out.columns else np.nan
    out["stop_distance_price"] = (out["entry_price"] - out["sl_price"]).abs()
    out["take_profit_distance_price"] = (out["pt_price"] - out["entry_price"]).abs()
    out["price_reward_to_risk_ratio"] = out["take_profit_distance_price"] / out["stop_distance_price"].replace(0.0, np.nan)

    text_defaults = {
        "broker_contract_spec_key": "",
        "broker_symbol": "",
        "broker_risk_model": BROKER_RISK_MODEL,
        "broker_risk_contract_version": BROKER_RISK_CONTRACT_VERSION,
        "broker_quote_currency": "",
        "broker_profit_currency": "",
        "broker_simulation_currency": "",
        "broker_account_currency": "",
        "broker_conversion_mode": "",
        "broker_conversion_symbol": "",
        "broker_conversion_price_basis": "",
        "broker_conversion_status": "unresolved",
        "broker_min_lot_risk_status": "missing_broker_profile_contract",
    }
    numeric_defaults = {
        "broker_min_lot": np.nan,
        "broker_lot_step": np.nan,
        "broker_max_lot": np.nan,
        "broker_contract_size": np.nan,
        "broker_trade_calc_mode": np.nan,
        "broker_quote_to_simulation_rate": np.nan,
        "broker_profit_to_account_rate": np.nan,
        "broker_conversion_source_price": np.nan,
        "broker_conversion_age_seconds": np.nan,
        "risk_per_1_lot": np.nan,
        "risk_per_min_lot": np.nan,
        "risk_per_lot_step": np.nan,
        "min_executable_risk_amount": np.nan,
    }
    for column, value in text_defaults.items():
        out[column] = value
    for column, value in numeric_defaults.items():
        out[column] = value
    out["broker_conversion_source_timestamp"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
    out["has_valid_broker_min_lot_risk"] = False
    if out.empty:
        return out
    if "asset" not in out.columns:
        out["broker_min_lot_risk_status"] = "missing_asset_column"
        return out

    event_times = (
        to_datetime_iso(out["entry_time"], utc=True, errors="coerce")
        if "entry_time" in out.columns
        else pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
    )
    canonical_assets = out["asset"].astype(str).str.strip().str.upper()
    for asset_value, grouped_index in out.groupby(canonical_assets, sort=False).groups.items():
        key = str(asset_value).strip().upper()
        indices = pd.Index(grouped_index)
        contract = broker_context.profile.contracts.get(key)
        conversion = broker_context.profile.conversions.get(key)
        if contract is None or conversion is None:
            continue
        out.loc[indices, "broker_contract_spec_key"] = key
        out.loc[indices, "broker_symbol"] = contract.broker_symbol
        out.loc[indices, "broker_min_lot"] = contract.min_lot
        out.loc[indices, "broker_lot_step"] = contract.lot_step
        out.loc[indices, "broker_max_lot"] = contract.max_lot
        out.loc[indices, "broker_contract_size"] = contract.contract_size
        out.loc[indices, "broker_trade_calc_mode"] = np.nan if contract.trade_calc_mode is None else contract.trade_calc_mode
        out.loc[indices, "broker_quote_currency"] = contract.profit_currency
        out.loc[indices, "broker_profit_currency"] = contract.profit_currency
        out.loc[indices, "broker_simulation_currency"] = contract.account_currency
        out.loc[indices, "broker_account_currency"] = contract.account_currency

        if conversion.mode == "identity":
            price_basis = "identity"
            instrument_prices = None
        elif conversion.mode == "instrument_inverse":
            # Forex P/L is converted at the known close/SL price. This remains causal
            # because the stop price is fixed before the trade is selected.
            price_basis = "known_stop_price"
            instrument_prices = out.loc[indices, "sl_price"]
        else:
            price_basis = "latest_h1_at_or_before_entry"
            instrument_prices = None
        resolved = broker_context.conversion_store.resolve(
            event_times=event_times.loc[indices],
            contract=conversion,
            instrument_prices=instrument_prices,
        )
        for column in [
            "broker_conversion_mode", "broker_conversion_symbol",
            "broker_conversion_source_price", "broker_conversion_age_seconds",
            "broker_profit_to_account_rate", "broker_conversion_status",
        ]:
            out.loc[indices, column] = resolved.loc[indices, column]
        out.loc[indices, "broker_conversion_source_timestamp"] = to_datetime_iso(
            resolved.loc[indices, "broker_conversion_source_timestamp"], utc=True, errors="coerce"
        )
        out.loc[indices, "broker_conversion_price_basis"] = price_basis
        out.loc[indices, "broker_quote_to_simulation_rate"] = resolved.loc[indices, "broker_profit_to_account_rate"]

        stop_distance = pd.to_numeric(out.loc[indices, "stop_distance_price"], errors="coerce")
        rate = pd.to_numeric(resolved.loc[indices, "broker_profit_to_account_rate"], errors="coerce")
        valid_stop = stop_distance.notna() & np.isfinite(stop_distance) & stop_distance.gt(0)
        valid_rate = rate.notna() & np.isfinite(rate) & rate.gt(0)
        valid = valid_stop & valid_rate
        out.loc[indices, "broker_min_lot_risk_status"] = "invalid_or_missing_stop_distance"
        bad_conversion = indices[valid_stop & ~valid_rate]
        if len(bad_conversion):
            status = resolved.loc[bad_conversion, "broker_conversion_status"].astype(str).str.strip()
            out.loc[bad_conversion, "broker_min_lot_risk_status"] = "conversion_" + status
        valid_indices = indices[valid]
        if len(valid_indices):
            risk_per_1_lot = stop_distance.loc[valid_indices] * float(contract.contract_size) * rate.loc[valid_indices]
            out.loc[valid_indices, "risk_per_1_lot"] = risk_per_1_lot
            out.loc[valid_indices, "risk_per_min_lot"] = risk_per_1_lot * float(contract.min_lot)
            out.loc[valid_indices, "risk_per_lot_step"] = risk_per_1_lot * float(contract.lot_step)
            out.loc[valid_indices, "min_executable_risk_amount"] = risk_per_1_lot * float(contract.min_lot)
            out.loc[valid_indices, "broker_min_lot_risk_status"] = "ok"
            out.loc[valid_indices, "has_valid_broker_min_lot_risk"] = True
    return out


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
    max_dd = max_drawdown_from_r(r)
    return {
        "ledger_trade_count": int(len(r)),
        "ledger_win_rate": float(np.mean(r > 0)),
        "ledger_expectancy_r": float(np.mean(r)),
        "ledger_profit_factor": profit_factor_from_r(r),
        "ledger_cumulative_r": cumulative,
        "ledger_max_drawdown_r": max_dd,
        "ledger_calmar_r": cumulative / abs(max_dd) if max_dd < 0 else np.nan,
    }


def add_local_stability(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    positive = (out["expectancy_r"] > 0) & (out["profit_factor"] > 1.0) & (out["cumulative_r"] > 0)
    out["is_positive_core"] = positive.astype(int)
    out["local_positive_ratio"] = out["is_positive_core"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    out["local_expectancy_mean"] = out["expectancy_r"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    out["local_pf_mean"] = out["profit_factor"].rolling(LOCAL_WINDOW, center=True, min_periods=1).mean()
    return out


def load_classification_report(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing classification report. Run 03_classify_edge_candidate_bank.py first: {path}")
    df = pd.read_csv(path)
    required = [
        "edge_id", "asset", "family", "side_name", "side_value", "edge_classification",
        "classification_reason", "watchlist_quality_tag", "threshold_probability_column",
        "afml_bet_size_column", "calibrated_oof_path", "threshold_sweep_path",
        "edge_manifest_path",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Classification report missing required columns: {missing}")
    if "edge_manifest_hash" not in df.columns:
        df["edge_manifest_hash"] = ""
    df["edge_classification"] = df["edge_classification"].astype(str).str.lower().str.strip()
    df = df[df["edge_classification"].isin(INCLUDED_CLASSIFICATIONS)].copy()
    if df.empty:
        raise ValueError(f"No rows with edge_classification in {sorted(INCLUDED_CLASSIFICATIONS)} found.")
    for col in ["side_value", "eligible_rows", "rows_in_sweep", "best_cumulative_r", "best_expectancy_r", "best_profit_factor", "best_calmar_r", "classification_rank_score"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["passed_gates", "empty_sweep"]:
        if col in df.columns:
            df[col] = df[col].apply(normalize_bool)
    if df["edge_id"].duplicated().any():
        dup = df[df["edge_id"].duplicated(keep=False)]
        raise ValueError("Duplicate edge_id rows found in classification report:\n" + dup[["edge_id", "asset", "edge_classification"]].to_string(index=False))
    if set(df["threshold_probability_column"].dropna().unique()) != {PROBABILITY_COLUMN}:
        raise ValueError("Classification report must use calibrated_probability only.")
    if set(df["afml_bet_size_column"].dropna().unique()) != {AFML_COLUMN}:
        raise ValueError("Classification report must use afml_bet_size only.")
    return df.reset_index(drop=True)


def v2_path(path_text: str, *, context: str) -> Path:
    if not isinstance(path_text, str) or not path_text.strip() or path_text.strip().lower() == "nan":
        raise ValueError(f"Empty path for {context}")
    path = V2_ROOT / path_text.strip()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{context} does not exist: {path_text}")
    return path


def _round_threshold(value: float) -> float:
    return round(float(value), THRESHOLD_DECIMALS)


def _floor_to_threshold_step(value: float) -> float:
    return _round_threshold(math.floor(float(value) / THRESHOLD_STEP) * THRESHOLD_STEP)


def _ceil_to_threshold_step(value: float) -> float:
    return _round_threshold(math.ceil(float(value) / THRESHOLD_STEP) * THRESHOLD_STEP)


def _live_trade_metrics(r: np.ndarray) -> dict[str, Any]:
    """Same formulas as shared/calibration/fit_sigmoid_calibrator_and_thresholds.py's
    trade_metrics_from_selection, parameterized on an R array directly instead of
    a dataframe+hardcoded column name (see QUANTARA_V3_LIVE_CONSISTENT_THRESHOLD_GRID_FIX)."""
    wins = r[r > 0]
    losses = r[r <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(abs(losses.sum())) if len(losses) else 0.0
    cumulative_r = float(r.sum())
    max_dd = max_drawdown_from_r(r)
    return {
        "trade_count": int(len(r)),
        "win_rate": float(np.mean(r > 0)),
        "expectancy_r": float(np.mean(r)),
        "profit_factor": _shared_safe_div(gp, gl),
        "gross_profit_r": gp,
        "gross_loss_r": gl,
        "cumulative_r": cumulative_r,
        "max_drawdown_r": max_dd,
        "calmar_r": _shared_safe_div(cumulative_r, abs(max_dd)) if max_dd < 0 else np.nan,
    }


def build_live_threshold_sweep(prepared: "PreparedEdgeOOF", *, geometry_hash: str) -> pd.DataFrame:
    """Build a Step04-threshold-sweep-equivalent DataFrame directly from
    prepare_edge_oof()'s own live-consistent frame, instead of loading Step04's
    calibrated_threshold_sweep.csv (built on the CPCV-fold-averaged
    calibrated_probability). See QUANTARA_V3_LIVE_CONSISTENT_THRESHOLD_GRID_FIX
    above for the full rationale."""
    sweep_columns = [
        "threshold", "probability_column", "trade_count", "win_rate", "expectancy_r", "profit_factor",
        "gross_profit_r", "gross_loss_r", "cumulative_r", "max_drawdown_r", "calmar_r",
        "avg_pred_prob", "avg_afml_bet_size", "positive_afml_trade_count",
        "positive_afml_trade_rate", "avg_positive_afml_bet_size",
        "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count",
        "payoff_zero_rate", "min_wins_required", "min_losses_required", "empirical_avg_win_r",
        "empirical_avg_loss_r", "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]
    valid = prepared.frame
    if valid.empty:
        out = pd.DataFrame(columns=sweep_columns)
        out.attrs["threshold_sweep_policy"] = {}
        return out

    probs = pd.to_numeric(valid[PROBABILITY_COLUMN], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    probs = probs[(probs >= 0.0) & (probs <= 1.0)]
    policy: dict[str, Any] = {
        "threshold_sweep_policy": "dynamic_observed_live_calibrated_probability_range",
        "probability_column": PROBABILITY_COLUMN,
        "threshold_step": THRESHOLD_STEP,
        "threshold_min_bound": THRESHOLD_MIN_BOUND,
        "threshold_max_bound": THRESHOLD_MAX_BOUND,
        "threshold_decimals": THRESHOLD_DECIMALS,
        "valid_probability_rows": int(len(probs)),
        "calibrated_probability_min": float(probs.min()) if len(probs) else np.nan,
        "calibrated_probability_max": float(probs.max()) if len(probs) else np.nan,
        "threshold_start": None,
        "threshold_end": None,
        "threshold_count": 0,
    }
    if probs.empty:
        out = pd.DataFrame(columns=sweep_columns)
        out.attrs["threshold_sweep_policy"] = policy
        return out

    start = _round_threshold(max(THRESHOLD_MIN_BOUND, _floor_to_threshold_step(float(probs.min()))))
    end = _round_threshold(min(THRESHOLD_MAX_BOUND, _ceil_to_threshold_step(float(probs.max()))))
    if start > end:
        start = end
    steps = int(round((end - start) / THRESHOLD_STEP))
    thresholds = [_round_threshold(start + i * THRESHOLD_STEP) for i in range(steps + 1)]
    thresholds = sorted(set(thr for thr in thresholds if THRESHOLD_MIN_BOUND <= thr <= THRESHOLD_MAX_BOUND))
    policy.update({
        "threshold_start": float(thresholds[0]) if thresholds else None,
        "threshold_end": float(thresholds[-1]) if thresholds else None,
        "threshold_count": int(len(thresholds)),
    })

    rows: list[dict[str, Any]] = []
    for thr in thresholds:
        sel = valid[valid[PROBABILITY_COLUMN] >= thr]
        if sel.empty:
            continue
        r = pd.to_numeric(sel["executed_r"], errors="coerce").dropna().to_numpy(dtype=float)
        if len(r) == 0:
            continue
        metrics = _live_trade_metrics(r)
        profile = empirical_payoff_profile(sel["executed_r"].to_numpy(dtype=float))
        afml = np.full(len(sel), np.nan, dtype=float)
        if profile.get("payoff_profile_valid") is True:
            components = payoff_profile_afml_components(
                pd.to_numeric(sel[PROBABILITY_COLUMN], errors="coerce").to_numpy(dtype=float), profile
            )
            afml = components[AFML_COLUMN]
        positive = np.isfinite(afml) & (afml > 0.0)
        rows.append({
            "threshold": float(thr),
            "probability_column": PROBABILITY_COLUMN,
            "avg_pred_prob": float(pd.to_numeric(sel[PROBABILITY_COLUMN], errors="coerce").mean()),
            "avg_afml_bet_size": float(np.nanmean(afml)) if np.isfinite(afml).any() else np.nan,
            "positive_afml_trade_count": int(positive.sum()),
            "positive_afml_trade_rate": float(positive.mean()) if len(positive) else np.nan,
            "avg_positive_afml_bet_size": float(np.nanmean(afml[positive])) if positive.any() else 0.0,
            **metrics,
            "payoff_profile_version": profile.get("payoff_profile_version"),
            "payoff_profile_valid": bool(profile.get("payoff_profile_valid", False)),
            "payoff_profile_invalid_reason": profile.get("payoff_profile_invalid_reason") or "",
            "payoff_trade_count": profile.get("payoff_trade_count"),
            "payoff_win_count": profile.get("payoff_win_count"),
            "payoff_loss_count": profile.get("payoff_loss_count"),
            "payoff_zero_count": profile.get("payoff_zero_count"),
            "payoff_zero_rate": profile.get("payoff_zero_rate"),
            "min_wins_required": profile.get("min_wins_required"),
            "min_losses_required": profile.get("min_losses_required"),
            "empirical_avg_win_r": profile.get("empirical_avg_win_r"),
            "empirical_avg_loss_r": profile.get("empirical_avg_loss_r"),
            "empirical_reward_risk_ratio": profile.get("empirical_reward_risk_ratio"),
            "empirical_break_even_probability": profile.get("empirical_break_even_probability"),
        })
    out = (
        pd.DataFrame(rows, columns=sweep_columns).sort_values("threshold").reset_index(drop=True)
        if rows else pd.DataFrame(columns=sweep_columns)
    )
    out.attrs["threshold_sweep_policy"] = policy
    if not out.empty:
        out = attach_payoff_context_columns(out, edge_id=prepared.edge_id, geometry_hash=geometry_hash)
        out["afml_sizing_method"] = AFML_SIZING_METHOD
    return out


BROKER_REPRICING_CONFIG = Path(__file__).resolve().parent / "config" / "broker_repricing.json"


def apply_broker_repricing(df: pd.DataFrame, oof_path: Path) -> pd.DataFrame:
    """RUNBOOK v5.1 (ADDED 2026-10-16): optionally replace the research (Dukascopy-priced) executed_r with the CURRENT
    firm's re-priced value from step 00d. Disabled unless config/broker_repricing.json has "enabled": true. Rows 00d
    did not resolve keep their research value (flag broker_repriced=0) so the row set/order is never changed (04bb
    validates geometry by row position). executed_meta_label is recomputed as executed_r > 0 (contract at ledger build)."""
    if not BROKER_REPRICING_CONFIG.exists():
        return df
    cfg = json.loads(BROKER_REPRICING_CONFIG.read_text(encoding="utf-8"))
    if not cfg.get("enabled"):
        return df
    parts = Path(oof_path).resolve().parts
    if "edges" not in parts or parts.index("edges") < 1:
        raise ValueError(f"Cannot infer edge identity from OOF path for broker re-pricing: {oof_path}")
    i = parts.index("edges"); asset, name = parts[i - 1], parts[i + 1]
    rp = Path(__file__).resolve().parent / "outputs" / "broker_repricing" / str(cfg["firm"]) / f"{asset}__{name}.csv"
    if not rp.exists():
        raise FileNotFoundError(f"broker re-pricing enabled for firm {cfg['firm']!r} but no 00d output for {asset}/{name}: {rp} "
                                "(run portfolio_allocator/00d_reprice_edge_trades_for_broker.py --firm <firm>)")
    rep = pd.read_csv(rp, float_precision="round_trip")
    rep = rep[rep["execution_status"].astype(str).eq("resolved")].copy()
    rep["event_time"] = parse_datetime(rep["Datetime"])
    keys = ["event_time", "side"] if "side" in df.columns else ["event_time"]
    if "side" in rep.columns:
        rep["side"] = pd.to_numeric(rep["side"], errors="coerce")
    rep = rep.drop_duplicates(subset=keys, keep="last")[keys + ["executed_r", "swap_R", "commission_R"]].rename(
        columns={"executed_r": "_broker_executed_r", "swap_R": "broker_swap_R", "commission_R": "broker_commission_R"})
    n0 = len(df)
    out = df.merge(rep, on=keys, how="left", validate="many_to_one")
    if len(out) != n0:
        raise ValueError(f"broker re-pricing changed the row count for {asset}/{name} ({n0} -> {len(out)})")
    has = out["_broker_executed_r"].notna()
    out["research_executed_r"] = out["executed_r"]
    out.loc[has, "executed_r"] = out.loc[has, "_broker_executed_r"]
    out["broker_repriced"] = has.astype(int)
    out["executed_meta_label"] = (pd.to_numeric(out["executed_r"], errors="coerce") > 0.0).astype(int)
    out = out.drop(columns=["_broker_executed_r"])
    print(f"BROKER_REPRICING {asset}/{name} firm={cfg['firm']} repriced={int(has.sum())}/{n0} "
          f"mean_r research={pd.to_numeric(out['research_executed_r'], errors='coerce').mean():+.5f} "
          f"firm={pd.to_numeric(out['executed_r'], errors='coerce').mean():+.5f}", flush=True)
    return out


def load_calibrated_oof(path: Path) -> pd.DataFrame:
    # V3 never permits realized_r to masquerade as executed_r.
    header = pd.read_csv(path, nrows=0)
    columns = list(header.columns)
    if not columns:
        raise ValueError(f"Calibrated OOF file has no columns: {path}")

    event_source = (
        "Datetime"
        if "Datetime" in columns
        else ("event_time" if "event_time" in columns else columns[0])
    )
    required = [
        "executed_r",
        "execution_status",
        "execution_contract_version",
        "execution_geometry_hash",
        "signal_mid",
        "entry_time",
        "exit_time",
        "entry_price",
        "pt_price",
        "sl_price",
        "meta_label",
        "quote_executable_meta_label",
        PROBABILITY_COLUMN,
        AFML_COLUMN,
        "Event_Vol",
    ]
    missing = [c for c in required if c not in columns]
    if missing:
        raise ValueError(
            f"Calibrated OOF missing V3 exact-execution columns {missing}: {path}. "
            "Rerun edge Step 03 then Step 04 (Step 03A was retired; see "
            "config/mldp_deviation_register.json)."
        )

    wanted = [
        event_source,
        *required,
        "realized_r" if "realized_r" in columns else None,
        "raw_probability" if "raw_probability" in columns else None,
        "side" if "side" in columns else None,
        "entry_bid" if "entry_bid" in columns else None,
        "entry_ask" if "entry_ask" in columns else None,
        "entry_spread" if "entry_spread" in columns else None,
        "entry_spread_bps" if "entry_spread_bps" in columns else None,
        "entry_lag_ms" if "entry_lag_ms" in columns else None,
        "entry_drift_bps" if "entry_drift_bps" in columns else None,
        "exit_price" if "exit_price" in columns else None,
        "exit_reason" if "exit_reason" in columns else None,
        *path_columns(columns),  # AFML s.12.4.2 CPCV paths (D052)
    ]
    usecols = list(dict.fromkeys(c for c in wanted if c is not None))
    # float_precision="round_trip": see load_threshold_sweep for why this is required.
    df = pd.read_csv(path, usecols=usecols, float_precision="round_trip")
    if df.empty:
        raise ValueError(f"Calibrated OOF file is empty: {path}")
    # The raw OOF/calibration artifact names this column quote_executable_meta_label
    # (Step 03A was retired; Step 03/03B's executable-target architecture renamed it --
    # see config/mldp_deviation_register.json). This ledger's own internal/output
    # schema has always used executed_meta_label, consumed by 04c/04d/05/06/07, so
    # the rename happens here at the read boundary rather than propagating the new
    # name through the rest of this file.
    df = df.rename(columns={"quote_executable_meta_label": "executed_meta_label"})

    df["event_time"] = parse_datetime(df[event_source])
    df["entry_time"] = parse_datetime(df["entry_time"])
    df["exit_time"] = parse_datetime(df["exit_time"])
    for col in [
        "executed_r",
        "meta_label",
        "executed_meta_label",
        PROBABILITY_COLUMN,
        AFML_COLUMN,
        "Event_Vol",
        "signal_mid",
        "entry_price",
        "pt_price",
        "sl_price",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "realized_r" in df.columns:
        df["realized_r"] = pd.to_numeric(df["realized_r"], errors="coerce")
    if "side" in df.columns:
        df["side"] = pd.to_numeric(df["side"], errors="coerce")
    # 2026-10-17 label/economics split (ledger LABEL_DEFINITION_DECISION_2026-10-17): quote_executable_meta_label is
    # now the firm-neutral MID-path training label, while executed_r is the research firm's net economics. This
    # ledger's executed_meta_label means "the trade made money", so it is derived from executed_r (as the broker
    # re-pricing path already does) -- never from the training label.
    df["executed_meta_label"] = (df["executed_r"] > 0.0).astype(int).where(df["executed_r"].notna())

    if not df["execution_status"].astype(str).eq("resolved").all():
        raise ValueError(f"Calibrated OOF contains unresolved Step 03A rows: {path}")
    df = apply_broker_repricing(df, path)  # RUNBOOK v5.1; no-op unless config/broker_repricing.json enabled
    if not df["execution_contract_version"].astype(str).eq(
        EXECUTION_CONTRACT_VERSION
    ).all():
        raise ValueError(f"Calibrated OOF exact-execution contract mismatch: {path}")
    if df["execution_geometry_hash"].astype(str).str.strip().isin(
        {"", "nan", "None"}
    ).any():
        raise ValueError(f"Calibrated OOF contains empty execution geometry hashes: {path}")

    df = df.dropna(
        subset=[
            "event_time",
            "entry_time",
            "exit_time",
            "executed_r",
            "meta_label",
            "executed_meta_label",
            PROBABILITY_COLUMN,
            AFML_COLUMN,
            "Event_Vol",
            "signal_mid",
            "entry_price",
            "pt_price",
            "sl_price",
        ]
    ).copy()
    df = df.sort_values(["event_time", "exit_time"]).reset_index(drop=True)
    df["source_oof_row_number"] = np.arange(len(df), dtype=int)
    return df

def build_edge_grid_summary(edge: pd.Series, prepared: "PreparedEdgeOOF") -> pd.DataFrame:
    geometry_hash = str(prepared.frame["execution_geometry_hash"].iloc[0])
    sweep = build_live_threshold_sweep(prepared, geometry_hash=geometry_hash)
    if sweep.empty:
        return pd.DataFrame()
    sweep = add_local_stability(sweep)
    gates = EXPECTED_SIDE_GATES
    _valid_payoff_sweep = sweep[sweep["payoff_profile_valid"].astype(bool)]
    max_trades = int(_valid_payoff_sweep["trade_count"].max()) if not _valid_payoff_sweep.empty else 0
    min_trade_count = max(int(gates["min_trade_abs"]), int(np.floor(max_trades * gates["min_trade_frac_of_side_max"])))
    summary = sweep.copy()
    summary["grid_candidate_id"] = summary["threshold"].apply(lambda thr: make_grid_candidate_id(str(edge["edge_id"]), float(thr)))
    for col in [
        "edge_id", "asset", "family", "side_name", "side_value", "edge_classification",
        "classification_reason", "watchlist_quality_tag", "threshold_probability_column",
        "afml_bet_size_column", "calibrated_oof_path", "threshold_sweep_path",
        "edge_manifest_path", "edge_manifest_hash", "classification_rank_score",
    ]:
        if col in edge.index:
            summary[col] = edge[col]
    summary["max_trade_count_in_edge_sweep"] = max_trades
    summary["min_trade_count_required"] = min_trade_count
    summary["gate_profile"] = "expected_side"
    summary["passes_grid_gates"] = (
        summary["payoff_profile_valid"].astype(bool)
        & (summary["trade_count"] >= min_trade_count)
        & (summary["expectancy_r"] > gates["min_expectancy_r"])
        & (summary["profit_factor"] >= gates["min_profit_factor"])
        & (summary["cumulative_r"] > gates["min_cumulative_r"])
        & (summary["calmar_r"] >= gates["min_calmar_r"])
        & (summary["local_positive_ratio"] >= gates["min_local_positive_ratio"])
    )
    summary["grid_reject_reason"] = ""
    summary.loc[~summary["payoff_profile_valid"].astype(bool), "grid_reject_reason"] += "invalid_empirical_payoff_profile;"
    summary.loc[summary["trade_count"] < min_trade_count, "grid_reject_reason"] += "too_few_trades;"
    summary.loc[summary["expectancy_r"] <= gates["min_expectancy_r"], "grid_reject_reason"] += "nonpositive_expectancy;"
    summary.loc[summary["profit_factor"] < gates["min_profit_factor"], "grid_reject_reason"] += "profit_factor_below_gate;"
    summary.loc[summary["cumulative_r"] <= gates["min_cumulative_r"], "grid_reject_reason"] += "nonpositive_cumulative_r;"
    summary.loc[summary["calmar_r"] < gates["min_calmar_r"], "grid_reject_reason"] += "calmar_below_gate;"
    summary.loc[summary["local_positive_ratio"] < gates["min_local_positive_ratio"], "grid_reject_reason"] += "weak_local_stability;"
    summary.loc[summary["passes_grid_gates"], "grid_reject_reason"] = "passed"
    return summary


def build_threshold_grid_summary(
    classification: pd.DataFrame,
    prepared_by_edge: dict[str, "PreparedEdgeOOF"],
) -> pd.DataFrame:
    frames = []
    for _, row in classification.iterrows():
        prepared = prepared_by_edge.get(str(row["edge_id"]))
        if prepared is None:
            continue
        frame = build_edge_grid_summary(row, prepared)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise RuntimeError("No threshold grid summary rows were built.")
    return pd.concat(frames, ignore_index=True).sort_values(["asset", "family", "edge_id", "threshold"]).reset_index(drop=True)



def prepare_edge_oof(
    edge_rows: pd.DataFrame,
    broker_context: BrokerRiskContext,
) -> PreparedEdgeOOF:
    """Load, broker-enrich, side-filter, and probability-sort one edge once."""
    if edge_rows.empty:
        raise ValueError("Cannot prepare an empty edge threshold group.")
    first = edge_rows.iloc[0]
    edge_id = str(first["edge_id"])
    asset = str(first["asset"]).strip().upper()
    side_value = safe_int(first["side_value"], default=None)
    oof_path = v2_path(
        str(first["calibrated_oof_path"]),
        context=f"{edge_id} calibrated_oof_path",
    )

    loaded = load_calibrated_oof(oof_path)
    loaded["asset"] = asset

    # QUANTARA_V3_LIVE_CONSISTENT_CALIBRATION_FIX (2026-09-15):
    # The calibrated_probability column loaded above is Step04's CPCV-fold-averaged
    # research diagnostic (shared/calibration/fit_sigmoid_calibrator_and_thresholds.py
    # ::cpcv_calibrate_edge) -- an average of predictions from several different
    # per-fold sigmoid fits. It is NOT the probability live scoring will ever
    # produce: shared/scoring/edge_scorer.py loads exactly one calibrator file,
    # probability_calibrator_sigmoid_side_<side>.json (the single sigmoid refit on
    # all data by ::fit_final_live_sigmoid), and applies it to raw_probability.
    # Building the threshold grid from the CPCV column therefore selects a
    # threshold calibrated against a probability distribution live scoring will
    # never see -- empirically confirmed to diverge sharply (up to 0.06 absolute,
    # full pairwise rank reordering, AUC sometimes <0.5 vs raw_probability's own
    # positive, significant AUC). Rebuild calibrated_probability here from
    # raw_probability using that same single live calibrator so the threshold grid
    # (and the afml_bet_size recomputed from it further below) live on exactly the
    # probability axis live scoring will use. See config/us500_edge_search_ledger.json's
    # PORTFOLIO_ALLOCATOR_CALIBRATION_MISMATCH_FIX_2026-09-15 entry for the full
    # derivation and empirical evidence.
    if "raw_probability" not in loaded.columns:
        raise ValueError(
            f"{edge_id}: calibrated OOF file has no raw_probability column; "
            "cannot rebuild a live-consistent calibrated_probability. Rerun Step 03."
        )
    _family, _side_name = split_edge_name(edge_id.split("/", 1)[1])
    _layout = get_edge_layout(asset, _family, _side_name)
    _live_calibrator = load_calibrator_json(_layout.calibrator_json)
    loaded[PROBABILITY_COLUMN] = apply_sigmoid_calibrator(
        pd.to_numeric(loaded["raw_probability"], errors="coerce").to_numpy(dtype=float),
        _live_calibrator,
    )

    enriched = add_broker_min_lot_risk_columns(
        loaded,
        broker_context,
        asset=asset,
    )

    base_mask = enriched[[PROBABILITY_COLUMN, AFML_COLUMN, "executed_r"]].notna().all(axis=1)
    if "side" in enriched.columns and side_value is not None:
        base_mask &= enriched["side"].astype(float) == float(side_value)

    frame = enriched.loc[base_mask].copy().reset_index(drop=True)
    probabilities = pd.to_numeric(frame[PROBABILITY_COLUMN], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(probabilities).all():
        raise ValueError(f"{edge_id}: non-finite calibrated probabilities remain after OOF filtering")

    # One stable probability sort per edge. Every threshold then uses
    # np.searchsorted instead of rescanning the full OOF DataFrame.
    sorted_positions = np.argsort(probabilities, kind="mergesort").astype(np.int64, copy=False)
    sorted_probabilities = probabilities[sorted_positions]
    cols = path_columns(frame.columns)
    if not cols and REQUIRE_CPCV_PATHS:
        raise ValueError(
            f"{edge_id}: calibrated OOF has no cpcv_path_XX_raw_probability columns (D052). Re-run the edge's Step 03 and "
            "Step 04, or pass --allow-legacy-single-path for pre-D052 artifacts."
        )
    path_calibrated = (
        np.vstack([
            apply_sigmoid_calibrator(pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float), _live_calibrator)
            for c in cols
        ])
        if cols else None
    )
    if path_calibrated is not None:
        path_calibrated[~np.isfinite(np.vstack([pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float) for c in cols]))] = np.nan
    return PreparedEdgeOOF(
        frame=frame,
        sorted_probabilities=sorted_probabilities,
        sorted_positions=sorted_positions,
        calibrated_oof_path=oof_path,
        edge_id=edge_id,
        asset=asset,
        side_value=side_value,
        path_calibrated=path_calibrated,
    )


def build_path_ledger(grid_row: pd.Series, prepared: PreparedEdgeOOF) -> pd.DataFrame:
    """AFML s.12.4.2 step 5 (D052): the threshold's trades on every CPCV path, with the same live calibrator, frozen
    payoff profile (AFML size) and broker-risk columns as the single-path ledger. Rows a path did not cover are dropped."""
    if prepared.path_calibrated is None:
        return pd.DataFrame(columns=PATH_LEDGER_COLUMNS)
    threshold = safe_float(grid_row["threshold"])
    profile = payoff_profile_from_grid_row(grid_row)
    frame = prepared.frame
    parts = []
    for path_no in range(prepared.path_calibrated.shape[0]):
        prob = prepared.path_calibrated[path_no]
        sel = np.isfinite(prob) & (prob >= threshold - PROBABILITY_EPSILON)
        if not sel.any():
            continue
        rows = frame.loc[sel]
        afml = payoff_profile_afml_components(prob[sel], profile)[AFML_COLUMN]
        part = pd.DataFrame({
            "grid_candidate_id": str(grid_row["grid_candidate_id"]),
            "edge_id": str(grid_row["edge_id"]),
            "asset": str(grid_row["asset"]),
            "threshold": threshold,
            "cpcv_path": path_no,
            "source_oof_row_number": rows["source_oof_row_number"].to_numpy(),
            "event_time": rows["event_time"].to_numpy(),
            "exit_time": rows["exit_time"].to_numpy(),
            "executed_r": pd.to_numeric(rows["executed_r"], errors="coerce").to_numpy(dtype=float),
            "afml_bet_size": afml,
            "risk_per_1_lot": rows["risk_per_1_lot"].to_numpy(),
            "broker_min_lot": rows["broker_min_lot"].to_numpy(),
            "broker_lot_step": rows["broker_lot_step"].to_numpy(),
            "has_valid_broker_min_lot_risk": rows["has_valid_broker_min_lot_risk"].to_numpy(),
            "broker_min_lot_risk_status": rows["broker_min_lot_risk_status"].to_numpy(),
        })
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=PATH_LEDGER_COLUMNS)


def _strict_validate_broker_risk_rows(ledger: pd.DataFrame) -> None:
    if ledger.empty:
        return
    valid = ledger.get(
        "has_valid_broker_min_lot_risk",
        pd.Series(False, index=ledger.index),
    ).astype(bool)
    if STRICT_BROKER_RISK and not valid.all():
        invalid = ledger.loc[~valid].copy()
        preview_cols = [
            column
            for column in [
                "grid_candidate_id",
                "trade_uid",
                "edge_id",
                "asset",
                "entry_time",
                "entry_price",
                "sl_price",
                "broker_conversion_status",
                "broker_min_lot_risk_status",
            ]
            if column in invalid.columns
        ]
        raise RuntimeError(
            "V2 Step 04B historical broker-risk validation failed. "
            f"invalid_rows={len(invalid)}\n"
            + invalid[preview_cols].head(30).to_string(index=False)
        )



def payoff_profile_from_grid_row(grid_row: pd.Series) -> dict[str, Any]:
    keys = [
        "payoff_profile_version", "payoff_profile_valid", "payoff_profile_invalid_reason",
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count", "payoff_zero_rate",
        "min_wins_required", "min_losses_required", "empirical_avg_win_r", "empirical_avg_loss_r",
        "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]
    missing = [key for key in keys if key not in grid_row.index]
    if missing:
        raise ValueError(f"{grid_row.get('edge_id')}: threshold sweep missing Phase-2 payoff fields: {missing}")
    profile = {key: grid_row.get(key) for key in keys}
    valid_raw = profile.get("payoff_profile_valid")
    if not isinstance(valid_raw, (bool, np.bool_)):
        profile["payoff_profile_valid"] = str(valid_raw).strip().lower() in {"true", "1", "yes"}
    return validate_payoff_profile(profile)

def build_grid_candidate_ledger(
    grid_row: pd.Series,
    prepared: PreparedEdgeOOF,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Materialize one threshold from a prepared edge using binary-search selection."""
    grid_candidate_id = str(grid_row["grid_candidate_id"])
    threshold = safe_float(grid_row["threshold"])
    selected_positions = prepared.select_positions(threshold)
    selected = (
        prepared.frame.iloc[selected_positions].copy()
        if len(selected_positions)
        else prepared.frame.iloc[0:0].copy()
    )

    expected_count = safe_int(grid_row.get("trade_count", 0))
    validation = {
        "grid_candidate_id": grid_candidate_id,
        "edge_id": grid_row["edge_id"],
        "asset": grid_row["asset"],
        "family": grid_row["family"],
        "side_name": grid_row["side_name"],
        "threshold": threshold,
        "edge_classification": grid_row["edge_classification"],
        "expected_trade_count": expected_count,
        "ledger_trade_count": int(len(selected)),
        "trade_count_diff": int(len(selected)) - expected_count,
        "calibrated_oof_path": str(prepared.calibrated_oof_path.relative_to(V2_ROOT)),
        "threshold_probability_column": PROBABILITY_COLUMN,
        "afml_bet_size_column": AFML_COLUMN,
        "passes_grid_gates": bool(grid_row.get("passes_grid_gates", False)),
        "status": "ok" if len(selected) else "empty_selection",
    }
    if selected.empty:
        return pd.DataFrame(columns=LEDGER_OUTPUT_COLUMNS), validation

    ledger = selected
    ledger["grid_candidate_id"] = grid_candidate_id
    ledger["candidate_id"] = grid_candidate_id
    ledger["edge_id"] = grid_row["edge_id"]
    ledger["asset"] = grid_row["asset"]
    ledger["family"] = grid_row["family"]
    ledger["side_name"] = grid_row["side_name"]
    ledger["side_value"] = safe_int(grid_row["side_value"])
    ledger["threshold"] = threshold
    ledger["edge_classification"] = grid_row["edge_classification"]
    ledger["calibrated_probability"] = pd.to_numeric(
        ledger[PROBABILITY_COLUMN],
        errors="coerce",
    )
    profile = payoff_profile_from_grid_row(grid_row)
    payoff_context = validate_payoff_context_mapping(
        grid_row,
        expected_edge_id=str(grid_row["edge_id"]),
        expected_threshold=threshold,
        expected_geometry_hash=str(grid_row["geometry_hash"]),
    )
    afml_components = payoff_profile_afml_components(
        pd.to_numeric(ledger[PROBABILITY_COLUMN], errors="coerce").to_numpy(dtype=float),
        profile,
    )
    ledger["afml_bet_size"] = afml_components[AFML_COLUMN]
    ledger["afml_sizing_method"] = AFML_SIZING_METHOD
    ledger["payoff_profile_version"] = profile["payoff_profile_version"]
    ledger["payoff_profile_valid"] = True
    ledger["payoff_profile_invalid_reason"] = profile.get("payoff_profile_invalid_reason") or ""
    ledger["payoff_context_version"] = payoff_context["payoff_context_version"]
    ledger["payoff_context_id"] = payoff_context["payoff_context_id"]
    ledger["payoff_context_edge_id"] = payoff_context["edge_id"]
    ledger["gross_label_contract_version"] = payoff_context["gross_label_contract_version"]
    ledger["executed_target_contract_version"] = payoff_context["executed_target_contract_version"]
    ledger["threshold_probability_column"] = payoff_context["threshold_probability_column"]
    ledger["geometry_hash"] = payoff_context["geometry_hash"]
    for _col in [
        "payoff_trade_count", "payoff_win_count", "payoff_loss_count", "payoff_zero_count", "payoff_zero_rate",
        "min_wins_required", "min_losses_required", "empirical_avg_win_r", "empirical_avg_loss_r",
        "empirical_reward_risk_ratio", "empirical_break_even_probability",
    ]:
        ledger[_col] = profile[_col]
    expected_executed_label = (pd.to_numeric(ledger["executed_r"], errors="coerce") > 0.0).astype(int)
    stored_executed_label = pd.to_numeric(ledger["executed_meta_label"], errors="coerce")
    if stored_executed_label.isna().any() or not stored_executed_label.astype(int).equals(expected_executed_label):
        raise ValueError(f"{grid_candidate_id}: executed_meta_label identity mismatch")
    ledger["raw_probability"] = (
        pd.to_numeric(ledger["raw_probability"], errors="coerce")
        if "raw_probability" in ledger.columns
        else np.nan
    )
    if "realized_r" not in ledger.columns:
        ledger["realized_r"] = np.nan
    ledger["calibrated_oof_path"] = str(
        prepared.calibrated_oof_path.relative_to(V2_ROOT)
    )
    ledger["threshold_sweep_path"] = grid_row.get("threshold_sweep_path", "")
    ledger["edge_manifest_path"] = grid_row.get("edge_manifest_path", "")
    ledger["edge_manifest_hash"] = grid_row.get("edge_manifest_hash", "")
    ledger["trade_uid"] = [
        make_trade_uid(grid_candidate_id, event_time, exit_time, row_number)
        for event_time, exit_time, row_number in zip(
            ledger["event_time"].astype(str),
            ledger["exit_time"].astype(str),
            ledger["source_oof_row_number"].astype(int),
        )
    ]

    validation.update(broker_risk_summary(ledger))
    ledger = ledger.reindex(columns=LEDGER_OUTPUT_COLUMNS)
    return ledger.reset_index(drop=True), validation


def build_ledgers(
    viable_grid: pd.DataFrame,
    broker_context: BrokerRiskContext,
    broker_context_manifest: dict[str, Any],
    *,
    ledger_output_path: Path | None,
    prepared_by_edge: dict[str, "PreparedEdgeOOF"],
    path_ledger_output_path: Path | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, int]]:
    """Build summaries while streaming one combined ledger in bounded memory."""
    broker_specs_manifest = dict(broker_context_manifest)
    validations: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    assets_with_valid_trade_rows: set[str] = set()
    counts = {
        "ledger_core_rows": 0,
        "ledger_watchlist_rows": 0,
        "ledger_core_plus_watchlist_rows": 0,
    }
    writer = (
        CombinedLedgerStreamWriter(
            ledger_output_path,
            columns=LEDGER_OUTPUT_COLUMNS,
        )
        if ledger_output_path is not None
        else None
    )
    path_writer = (
        CombinedLedgerStreamWriter(path_ledger_output_path, columns=PATH_LEDGER_COLUMNS)
        if path_ledger_output_path is not None
        else None
    )
    counts["path_ledger_rows"] = 0

    grouped = list(viable_grid.groupby("edge_id", sort=False))
    try:
        with tqdm(
            total=len(viable_grid),
            desc="Building and streaming threshold-grid ledger",
            unit="threshold",
        ) as progress:
            for edge_id, edge_rows in grouped:
                progress.set_postfix_str(str(edge_id), refresh=False)
                prepared = prepared_by_edge[str(edge_id)]

                for _, row in edge_rows.iterrows():
                    ledger, validation = build_grid_candidate_ledger(row, prepared)
                    validations.append(validation)

                    _strict_validate_broker_risk_rows(ledger)
                    classification = str(row["edge_classification"]).strip().lower()
                    row_count = int(len(ledger))
                    counts["ledger_core_plus_watchlist_rows"] += row_count
                    if classification == "core":
                        counts["ledger_core_rows"] += row_count
                    elif classification == "watchlist":
                        counts["ledger_watchlist_rows"] += row_count

                    if row_count:
                        assets_with_valid_trade_rows.add(
                            str(row["asset"]).strip().upper()
                        )

                    summary = {
                        "grid_candidate_id": row["grid_candidate_id"],
                        "edge_id": row["edge_id"],
                        "asset": row["asset"],
                        "family": row["family"],
                        "side_name": row["side_name"],
                        "side_value": safe_int(row["side_value"]),
                        "threshold": safe_float(row["threshold"]),
                        "edge_classification": row["edge_classification"],
                        "passes_grid_gates": bool(row.get("passes_grid_gates", False)),
                        "grid_trade_count": safe_int(row.get("trade_count", 0)),
                        "grid_expectancy_r": safe_float(row.get("expectancy_r")),
                        "grid_cumulative_r": safe_float(row.get("cumulative_r")),
                        "grid_max_drawdown_r": safe_float(row.get("max_drawdown_r")),
                        "grid_calmar_r": safe_float(row.get("calmar_r")),
                        "grid_avg_calibrated_probability": safe_float(
                            row.get("avg_pred_prob")
                        ),
                        "grid_avg_afml_bet_size": safe_float(
                            row.get("avg_afml_bet_size")
                        ),
                        **trade_metrics(ledger),
                        **broker_risk_summary(ledger),
                        "ledger_avg_calibrated_probability": (
                            float(ledger["calibrated_probability"].mean())
                            if not ledger.empty
                            else np.nan
                        ),
                        "ledger_avg_afml_bet_size": (
                            float(ledger["afml_bet_size"].mean())
                            if not ledger.empty
                            else np.nan
                        ),
                        "ledger_min_event_time": (
                            ledger["event_time"].min()
                            if not ledger.empty
                            else pd.NaT
                        ),
                        "ledger_max_event_time": (
                            ledger["event_time"].max()
                            if not ledger.empty
                            else pd.NaT
                        ),
                    }
                    summaries.append(summary)

                    if writer is not None and not ledger.empty:
                        writer.append(ledger)
                    path_ledger = build_path_ledger(row, prepared)  # D052: the same threshold on every CPCV path
                    counts["path_ledger_rows"] += int(len(path_ledger))
                    if path_writer is not None and not path_ledger.empty:
                        path_writer.append(path_ledger)
                    del path_ledger

                    del ledger
                    progress.update(1)

                del prepared
                gc.collect()

        validation_df = pd.DataFrame(validations)
        summary_df = pd.DataFrame(summaries)
        validate_trade_counts(validation_df)

        if writer is not None:
            print(
                "Finalizing streamed core+watchlist ledger: "
                f"{writer.rows_received:,} rows received in bounded buffers..."
            )
            writer.commit()
            if path_writer is not None:
                path_writer.commit()
            if writer.rows_flushed != counts["ledger_core_plus_watchlist_rows"]:
                raise RuntimeError(
                    "Step 04B final streamed row count differs from accumulated count: "
                    f"writer={writer.rows_flushed} "
                    f"expected={counts['ledger_core_plus_watchlist_rows']}"
                )
    except Exception:
        if writer is not None:
            writer.abort()
        if path_writer is not None:
            path_writer.abort()
        raise

    broker_specs_manifest["assets_with_valid_trade_rows"] = sorted(
        assets_with_valid_trade_rows
    )
    broker_specs_manifest["strict_all_selected_trade_rows_valid"] = True
    broker_specs_manifest["performance_policy"] = {
        "level_1_single_combined_raw_ledger_only": True,
        "level_2_stream_during_threshold_processing": True,
        "level_2_atomic_temp_then_replace": True,
        "level_2_stream_buffer_row_limit": STREAM_BUFFER_ROW_LIMIT,
        "level_2_csv_write_chunk_size": LEDGER_WRITE_CHUNK_SIZE,
        "level_2_full_combined_ledger_materialized_in_memory": False,
        "level_3_threshold_selection_mode": THRESHOLD_SELECTION_MODE,
        "level_3_probability_sort_count_per_edge": 1,
        "level_3_restore_chronological_order_before_metrics_and_write": True,
        "oof_columns": "required_columns_only",
        "oof_edges_held_in_memory": 1,
        "hash_coupling": "none",
    }
    if writer is not None:
        broker_specs_manifest["performance_policy"].update(
            {
                "stream_flush_count": int(writer.flush_count),
                "stream_rows_written": int(writer.rows_flushed),
            }
        )
    return validation_df, summary_df, broker_specs_manifest, counts


def validate_trade_counts(validation: pd.DataFrame) -> None:
    if validation.empty:
        return
    mismatches = validation[validation["trade_count_diff"] != 0]
    if not mismatches.empty and STRICT_TRADE_COUNT_VALIDATION:
        preview = mismatches[["grid_candidate_id", "expected_trade_count", "ledger_trade_count", "trade_count_diff", "calibrated_oof_path"]].head(30).to_string(index=False)
        raise RuntimeError("V2 Step 04B trade-count validation failed.\n" + preview)


def build(
    write: bool = True,
    *,
    profile_path: Path = BROKER_PROFILE_PATH,
    conversion_manifest_path: Path = BROKER_CONVERSION_MANIFEST_PATH,
) -> dict[str, Any]:
    out = V2_ROOT / "portfolio_allocator" / "outputs"
    classification = load_classification_report(
        out / "v2_edge_classification_report.csv"
    )

    # Broker context must be loaded before OOF preparation: prepare_edge_oof()
    # broker-enriches each edge's frame and rebuilds calibrated_probability
    # from raw_probability via the live sigmoid calibrator (see
    # QUANTARA_V3_LIVE_CONSISTENT_THRESHOLD_GRID_FIX above). Each edge is
    # prepared exactly once here and the SAME PreparedEdgeOOF is reused for
    # both the threshold-grid summary and the trade ledger, so grid gating and
    # ledger row selection are guaranteed to operate on identical data.
    broker_context, broker_context_manifest = load_broker_risk_context(
        profile_path=profile_path,
        conversion_manifest_path=conversion_manifest_path,
    )
    prepared_by_edge: dict[str, PreparedEdgeOOF] = {}
    for _, row in classification.iterrows():
        edge_id = str(row["edge_id"])
        prepared_by_edge[edge_id] = prepare_edge_oof(pd.DataFrame([row]), broker_context)

    grid_summary = build_threshold_grid_summary(classification, prepared_by_edge)
    viable_grid = (
        grid_summary[grid_summary["passes_grid_gates"]].copy()
        if WRITE_ONLY_VIABLE_THRESHOLDS
        else grid_summary.copy()
    )
    if viable_grid.empty:
        raise RuntimeError("No viable threshold-grid rows passed gates.")

    combined_path = (
        out / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv"
        if write
        else None
    )
    cpcv_paths_by_edge = {
        e: (0 if p.path_calibrated is None else int(p.path_calibrated.shape[0])) for e, p in prepared_by_edge.items()
    }
    cpcv_enabled = all(n > 0 for n in cpcv_paths_by_edge.values())
    path_ledger_path = (out / "v2_threshold_grid_cpcv_path_trade_ledger.csv") if (write and cpcv_enabled) else None
    validation, ledger_summary, broker_specs_manifest, ledger_counts = build_ledgers(
        viable_grid,
        broker_context,
        broker_context_manifest,
        ledger_output_path=combined_path,
        prepared_by_edge=prepared_by_edge,
        path_ledger_output_path=path_ledger_path,
    )
    validate_trade_counts(validation)

    core_row_count = int(ledger_counts["ledger_core_rows"])
    watchlist_row_count = int(ledger_counts["ledger_watchlist_rows"])
    combined_row_count = int(
        ledger_counts["ledger_core_plus_watchlist_rows"]
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "pipeline_mode": PIPELINE_MODE,
        "threshold_grid_ledger_status": "PASS",
        "counts": {
            "input_edges": int(classification["edge_id"].nunique()),
            "grid_summary_rows": int(len(grid_summary)),
            "viable_grid_rows": int(len(viable_grid)),
            "core_grid_rows": int(
                (viable_grid["edge_classification"] == "core").sum()
            ),
            "watchlist_grid_rows": int(
                (viable_grid["edge_classification"] == "watchlist").sum()
            ),
            "ledger_core_rows": core_row_count,
            "ledger_watchlist_rows": watchlist_row_count,
            "ledger_core_plus_watchlist_rows": combined_row_count,
            "summary_rows": int(len(ledger_summary)),
            "validation_rows": int(len(validation)),
        },
        "validation": {
            "strict_trade_count_validation": STRICT_TRADE_COUNT_VALIDATION,
            "mismatch_count": int(
                (validation["trade_count_diff"] != 0).sum()
            )
            if not validation.empty
            else 0,
            "empty_selection_count": int(
                (validation["status"] == "empty_selection").sum()
            )
            if not validation.empty
            else 0,
            "broker_min_lot_risk_missing_rows": int(
                validation.get(
                    "ledger_missing_broker_min_lot_risk_rows",
                    pd.Series(dtype=float),
                ).sum()
            )
            if not validation.empty
            else 0,
            "broker_min_lot_risk_valid_rows": int(
                validation.get(
                    "ledger_valid_broker_min_lot_risk_rows",
                    pd.Series(dtype=float),
                ).sum()
            )
            if not validation.empty
            else 0,
        },
        "broker_min_lot_risk_enrichment": broker_specs_manifest,
        "cpcv_paths": {
            "enabled": bool(cpcv_enabled),
            "policy": "AFML s.12.4.2 step 5 / s.12.5, D052: every grid threshold scored on every CPCV path; 04C selects by the mean across paths",
            "paths_by_edge": cpcv_paths_by_edge,
            "path_ledger": "portfolio_allocator/outputs/v2_threshold_grid_cpcv_path_trade_ledger.csv" if cpcv_enabled else None,
            "path_ledger_rows": int(ledger_counts.get("path_ledger_rows", 0)),
            "legacy_single_path_allowed": not REQUIRE_CPCV_PATHS,
        },
        "gates": EXPECTED_SIDE_GATES,
        "settings": {
            "write_only_viable_thresholds": WRITE_ONLY_VIABLE_THRESHOLDS,
            "local_window": LOCAL_WINDOW,
            "probability_column": PROBABILITY_COLUMN,
            "afml_column": AFML_COLUMN,
            "broker_profile_path": str(
                Path(profile_path).expanduser().resolve()
            ),
            "broker_conversion_manifest_path": str(
                Path(conversion_manifest_path).expanduser().resolve()
            ),
            "strict_broker_risk": STRICT_BROKER_RISK,
            "broker_risk_hash_coupling": "none",
            "raw_ledger_output_policy": "single_core_plus_watchlist_only",
            "stream_during_threshold_processing": True,
            "stream_buffer_row_limit": STREAM_BUFFER_ROW_LIMIT,
            "ledger_write_chunk_size": LEDGER_WRITE_CHUNK_SIZE,
            "threshold_selection_mode": THRESHOLD_SELECTION_MODE,
        },
        "outputs": {
            "grid_summary_all": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_summary_all.csv"
            ),
            "grid_viability": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_viability.csv"
            ),
            "ledger_core_plus_watchlist": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv"
            ),
            "ledger_summary": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_summary.csv"
            ),
            "validation": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_validation_report.csv"
            ),
            "manifest": (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_manifest.json"
            ),
        },
        "deprecated_outputs_not_written": [
            (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_core.csv"
            ),
            (
                "portfolio_allocator/outputs/"
                "v2_threshold_grid_trade_ledger_watchlist.csv"
            ),
        ],
        "notes": [
            (
                "Port of V1 Step 04B threshold-grid ledger logic using V2 "
                "edge_id and manifest-declared calibrated OOF paths."
            ),
            (
                "This step expands the calibrated threshold grid before "
                "Step 04C plateau compression."
            ),
            "No final position sizing is applied here.",
            (
                "Broker minimum-lot risk is derived from the local MT5 "
                "profile captured in Step 00A and causal H1 conversions "
                "built in Step 00B."
            ),
            (
                "The deprecated static quote_to_simulation_rate is not "
                "consumed; legacy compatibility columns carry the resolved "
                "per-trade conversion rate."
            ),
            (
                "File-content hashes are not consumed by Step 04B. MD5 "
                "values used for trade_uid are stable row identifiers only."
            ),
            (
                "Level 1: only the core+watchlist raw threshold ledger is "
                "written; core/watchlist separation is preserved as row "
                "metadata and recreated later at the representative stage."
            ),
            (
                "Level 2: selected threshold rows are streamed into one "
                "bounded-memory atomic CSV during threshold processing; the "
                "full combined ledger is never materialized in RAM."
            ),
            (
                "Level 3: each edge OOF is probability-sorted once and every "
                "threshold uses binary-search cutoff selection, then restores "
                "chronological row order before metrics and writing."
            ),
        ],
    }

    if write:
        print("Writing threshold summaries...")
        grid_summary.to_csv(
            out / "v2_threshold_grid_summary_all.csv",
            index=False,
        )
        viable_grid.to_csv(
            out / "v2_threshold_grid_viability.csv",
            index=False,
        )
        ledger_summary.to_csv(
            out / "v2_threshold_grid_trade_ledger_summary.csv",
            index=False,
        )
        validation.to_csv(
            out / "v2_threshold_grid_trade_ledger_validation_report.csv",
            index=False,
        )

        # Remove stale Level-0 legacy files only after the new combined ledger
        # has been successfully validated and atomically committed.
        for legacy_path in [
            out / "v2_threshold_grid_trade_ledger_core.csv",
            out / "v2_threshold_grid_trade_ledger_watchlist.csv",
        ]:
            if legacy_path.exists():
                legacy_path.unlink()

        write_json(
            out / "v2_threshold_grid_trade_ledger_manifest.json",
            manifest,
        )
    return manifest

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--profile", default=str(BROKER_PROFILE_PATH))
    parser.add_argument("--conversion-manifest", default=str(BROKER_CONVERSION_MANIFEST_PATH))
    parser.add_argument("--allow-legacy-single-path", action="store_true",
                        help="accept pre-D052 calibrated OOF files without CPCV path columns (04C then selects on one path)")
    args = parser.parse_args()
    global REQUIRE_CPCV_PATHS
    REQUIRE_CPCV_PATHS = not args.allow_legacy_single_path
    manifest = build(
        write=not args.dry_run,
        profile_path=Path(args.profile),
        conversion_manifest_path=Path(args.conversion_manifest),
    )
    print("V2 Phase B2.04B calibrated threshold-grid trade ledger")
    print(f"Status: {manifest['threshold_grid_ledger_status']}")
    c = manifest["counts"]
    print(f"Grid summary rows: {c['grid_summary_rows']}")
    print(f"Viable grid rows: {c['viable_grid_rows']}")
    print(f"Core ledger rows: {c['ledger_core_rows']}")
    print(f"Watchlist ledger rows: {c['ledger_watchlist_rows']}")
    print(f"Core+watchlist ledger rows: {c['ledger_core_plus_watchlist_rows']}")
    print(f"Validation mismatches: {manifest['validation']['mismatch_count']}")
    print(f"Broker min-lot risk valid rows: {manifest['validation']['broker_min_lot_risk_valid_rows']}")
    print(f"Broker min-lot risk missing rows: {manifest['validation']['broker_min_lot_risk_missing_rows']}")
    print(f"CPCV paths (D052): enabled={manifest['cpcv_paths']['enabled']} path-ledger rows={manifest['cpcv_paths']['path_ledger_rows']}")
    print("Historical conversion: PASS")
    print("Broker-risk hash coupling: none")


if __name__ == "__main__":
    main()
