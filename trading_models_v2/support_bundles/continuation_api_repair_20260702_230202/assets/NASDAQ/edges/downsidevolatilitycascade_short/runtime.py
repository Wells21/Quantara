"""
V2 edge runtime for NASDAQ/downsidevolatilitycascade_short.

Research and live must call this same runtime module. This file mirrors the
volatilitycarry_long runtime; side-specific changes are limited to identity,
negative-drift event filtering, and short-side model-row construction.

SOTD boundary:
- runtime.py owns live-safe generic feature generation, candidate events, and model rows.
- candidate_events.py owns V1 volatility-carry event/context fields.
- primary_side.py owns V1 Volatility_Carry_Event_Type -> side mapping.
- model_rows.py owns research-only triple-barrier labels.
- shared EdgeScorer owns model/calibrator/probability/AFML/threshold logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import pandas_ta_classic as ta
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "NASDAQ/downsidevolatilitycascade_short runtime requires pandas_ta_classic, matching the V1 feature factory. "
        "Install it in the venv before running this edge runtime."
    ) from exc

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from candidate_events import (  # noqa: E402
    CandidateEventError,
    generate_v1_downsidevolatilitycascade_short_candidate_events,
)
from feature_expansion import add_v1_feature_expansion  # noqa: E402
from model_rows import ModelRowsError, build_event_model_rows, model_row_config  # noqa: E402
from primary_side import (  # noqa: E402
    PrimarySideError,
    filter_short_events_with_primary_side,
    primary_side_config,
)
from regime_features import add_verified_regime_features  # noqa: E402
from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.runtime_environment import (  # noqa: E402
    EdgeRuntimeEnvironmentError,
    load_edge_contract_bundle,
    validate_edge_runtime_environment,
)
from shared.scoring.edge_scorer import (  # noqa: E402
    EdgeScorer,
    EdgeScoringContractError,
    EdgeScoringNotReady,
)

_LAYOUT = get_edge_layout_from_edge_root(_EDGE_ROOT)
EDGE_ID = _LAYOUT.edge_id
ASSET = _LAYOUT.asset
FAMILY = _LAYOUT.family
SIDE_NAME = _LAYOUT.side_name
SIDE_VALUE = _LAYOUT.side_value
EPS = 1e-12

RAW_PROBABILITY_COL = "raw_probability"
CALIBRATED_PROBABILITY_COL = "calibrated_probability"
AFML_BET_SIZE_COL = "afml_bet_size"
APPROVED_SIGNAL_COL = "approved_signal"


class EdgeRuntimeError(RuntimeError):
    """Raised when this edge runtime fails closed."""


def _layout():
    return _LAYOUT


def _edge_scorer() -> EdgeScorer:
    return EdgeScorer.from_edge_root(_EDGE_ROOT)


def _load_feature_schema() -> dict[str, Any]:
    return json.loads(
        (_layout().contracts_dir / "feature_schema.json").read_text(encoding="utf-8-sig")
    )


def _schema_feature_names() -> list[str]:
    return [str(row["feature_name"]) for row in _load_feature_schema().get("features", [])]


def _asset_bar_path_from_verified_manifest() -> Path:
    env = validate_environment()
    return _V2_ROOT / env["bar_verification"]["bars"]


def _load_asset_bars() -> pd.DataFrame:
    bar_path = _asset_bar_path_from_verified_manifest()
    if not bar_path.exists():
        raise EdgeRuntimeError(f"Verified bar path does not exist: {bar_path}")
    bars = pd.read_csv(bar_path)
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
    missing = [col for col in required if col not in bars.columns]
    if missing:
        raise EdgeRuntimeError(f"Asset bars missing required columns for feature build: {missing}")
    bars["Datetime"] = pd.to_datetime(bars["Datetime"], utc=True, errors="coerce")
    bars["Bar_Start"] = pd.to_datetime(bars["Bar_Start"], utc=True, errors="coerce")
    for col in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(
        subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    ).copy()
    bars = bars.sort_values("Datetime").reset_index(drop=True)
    if bars.empty:
        raise EdgeRuntimeError("No valid asset bars available after loading verified bar artifact.")
    return bars


def safe_log_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    return np.log((num.astype("float64") + EPS) / (den.astype("float64") + EPS))


def shifted_zscore(x: pd.Series, window: int) -> pd.Series:
    return (x - x.rolling(window).mean().shift(1)) / (
        x.rolling(window).std().shift(1) + EPS
    )


def rolling_slope(y: np.ndarray) -> float:
    if len(y) < 2 or not np.isfinite(y).all():
        return np.nan
    x = np.arange(len(y), dtype=float)
    x = x - x.mean()
    y = y.astype(float) - y.astype(float).mean()
    den = np.sum(x * x)
    return np.nan if den <= EPS else float(np.sum(x * y) / den)


def rolling_r2(y: np.ndarray) -> float:
    if len(y) < 3 or not np.isfinite(y).all():
        return np.nan
    x = np.arange(len(y), dtype=float)
    slope = rolling_slope(y)
    if not np.isfinite(slope):
        return np.nan
    intercept = float(np.mean(y) - slope * np.mean(x))
    y_hat = intercept + slope * x
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 0.0 if ss_tot <= EPS else float(1.0 - ss_res / ss_tot)


def efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    return (close - close.shift(window)).abs() / (
        close.diff().abs().rolling(window).sum() + EPS
    )


def _validate_feature_output(features: pd.DataFrame) -> None:
    expected = _schema_feature_names()
    missing = [name for name in expected if name not in features.columns]
    if missing:
        raise EdgeRuntimeError(f"build_features missing schema-declared features: {missing}")
    non_numeric = [
        name for name in expected if not pd.api.types.is_numeric_dtype(features[name])
    ]
    if non_numeric:
        raise EdgeRuntimeError(
            f"Schema-declared features must be numeric. Non-numeric: {non_numeric}"
        )
    inf_features = [
        name
        for name in expected
        if np.isinf(features[name].to_numpy(dtype="float64", copy=False)).any()
    ]
    if inf_features:
        raise EdgeRuntimeError(f"Features contain inf values: {inf_features}")


def load_contract(edge_root: str | Path | None = None) -> dict[str, Any]:
    layout = _layout()
    bundle = load_edge_contract_bundle(layout)
    return {
        "edge_id": EDGE_ID,
        "asset": ASSET,
        "family": FAMILY,
        "side_name": SIDE_NAME,
        "side_value": SIDE_VALUE,
        "runtime_status": "FEATURES_EVENTS_MODEL_ROWS_IMPLEMENTED_SCORING_FAIL_CLOSED_UNTIL_ARTIFACTS_PASS",
        "deployable": False,
        "edge_yaml": bundle["edge_yaml"],
        "loaded_contracts": sorted(bundle["contracts"].keys()),
    }


def validate_environment(*args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return validate_edge_runtime_environment(ASSET, FAMILY, SIDE_NAME)
    except EdgeRuntimeEnvironmentError as exc:
        raise EdgeRuntimeError(f"{EDGE_ID} runtime environment validation failed: {exc}") from exc


def build_features(
    bars: pd.DataFrame | None = None,
    *,
    drop_warmup_rows: bool = False,
) -> pd.DataFrame:
    """Build live-safe generic NASDAQ V2 features from standard_quote_tick bars.

    Volatility-carry event fields are deliberately not built here; they are
    computed inside candidate_events.py so they remain event/control/context fields.
    """

    df = _load_asset_bars() if bars is None else bars.copy()
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise EdgeRuntimeError(f"build_features input bars missing required columns: {missing}")
    df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True, errors="coerce")
    df["Bar_Start"] = pd.to_datetime(df["Bar_Start"], utc=True, errors="coerce")
    for col in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(
        subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close"]
    ).copy()
    df = df.sort_values("Datetime").reset_index(drop=True)

    out = df[
        ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
    ].copy()
    out["edge_id"] = EDGE_ID
    out["side"] = SIDE_VALUE
    open_ = out["Open"].astype("float64")
    high = out["High"].astype("float64")
    low = out["Low"].astype("float64")
    close = out["Close"].astype("float64")

    for period in [1, 2, 3, 5, 10, 20, 50, 100]:
        out[f"Log_Return_{period}"] = safe_log_ratio(close, close.shift(period))
    out["Abs_Log_Return_1"] = out["Log_Return_1"].abs()
    out["Abs_Log_Return_5"] = out["Log_Return_5"].abs()
    out["Abs_Log_Return_10"] = out["Log_Return_10"].abs()
    out["Fractional_Change"] = (close - open_) / (open_ + EPS)

    bar_range = (high - low).clip(lower=EPS)
    body = close - open_
    upper_wick_raw = (
        high - pd.concat([open_, close], axis=1).max(axis=1)
    ).clip(lower=0)
    lower_wick_raw = (
        pd.concat([open_, close], axis=1).min(axis=1) - low
    ).clip(lower=0)
    out["Range"] = bar_range
    out["Range_Bps"] = (bar_range / (open_ + EPS)) * 10_000.0
    out["Bar_Volatility_Bps"] = out["Range_Bps"]
    out["Body"] = body
    out["Body_Bps"] = (body / (open_ + EPS)) * 10_000.0
    out["Abs_Body_Bps"] = out["Body_Bps"].abs()
    out["Body_Share"] = body.abs() / (bar_range + EPS)
    out["Upper_Wick"] = upper_wick_raw
    out["Lower_Wick"] = lower_wick_raw
    out["Upper_Wick_Share"] = upper_wick_raw / (bar_range + EPS)
    out["Lower_Wick_Share"] = lower_wick_raw / (bar_range + EPS)
    out["Wick_Imbalance"] = (upper_wick_raw - lower_wick_raw) / (bar_range + EPS)
    out["Close_Location_Recalc"] = (close - low) / (bar_range + EPS)
    out["Gap_Bps"] = safe_log_ratio(open_, close.shift(1)) * 10_000.0
    log_upper_wick = safe_log_ratio(
        high,
        pd.concat([open_, close], axis=1).max(axis=1),
    )
    log_lower_wick = safe_log_ratio(
        pd.concat([open_, close], axis=1).min(axis=1),
        low,
    )

    for length in [10, 20, 50, 100, 200]:
        out[f"EMA_{length}"] = ta.ema(close, length=length)
        out[f"Dist_EMA_{length}"] = (
            close - out[f"EMA_{length}"]
        ) / (out[f"EMA_{length}"] + EPS)
    out["EMA_10_20_Gap"] = (out["EMA_10"] - out["EMA_20"]) / (close + EPS)
    out["EMA_20_50_Gap"] = (out["EMA_20"] - out["EMA_50"]) / (close + EPS)
    out["EMA_50_100_Gap"] = (out["EMA_50"] - out["EMA_100"]) / (close + EPS)

    out["RSI_14"] = ta.rsi(close, length=14)
    out["RSI_21"] = ta.rsi(close, length=21)
    out["RSI_50"] = ta.rsi(close, length=50)
    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is None or macd.shape[1] < 3:
        raise EdgeRuntimeError("pandas_ta_classic MACD did not return expected 3 columns")
    out["MACD"] = macd.iloc[:, 0]
    out["MACD_Signal"] = macd.iloc[:, 1]
    out["MACD_Hist"] = macd.iloc[:, 2]
    stoch = ta.stoch(high, low, close, k=14, d=3, smooth_k=3)
    if stoch is None or stoch.shape[1] < 2:
        raise EdgeRuntimeError("pandas_ta_classic Stoch did not return expected 2 columns")
    out["Stoch_K"] = stoch.iloc[:, 0]
    out["Stoch_D"] = stoch.iloc[:, 1]
    out["Williams_R_14"] = ta.willr(high, low, close, length=14)
    for period in [10, 20, 50]:
        out[f"ROC_{period}"] = ta.roc(close, length=period)

    for length in [14, 21, 50, 100]:
        out[f"ATR_{length}"] = ta.atr(high, low, close, length=length)
    out["NATR"] = out["ATR_14"] / (close + EPS)
    out["NATR_21"] = out["ATR_21"] / (close + EPS)
    out["NATR_50"] = out["ATR_50"] / (close + EPS)
    out["Vol_Regime_14_50"] = out["ATR_14"] / (out["ATR_50"] + EPS)
    out["Vol_Regime_21_100"] = out["ATR_21"] / (out["ATR_100"] + EPS)
    out["Range_ATR_Ratio_14"] = out["Range"] / (out["ATR_14"] + EPS)

    out["Return_Z_20"] = shifted_zscore(out["Log_Return_1"], 20)
    out["Range_Bps_Z_20"] = shifted_zscore(out["Range_Bps"], 20)
    out["Range_Ratio_20"] = out["Range_Bps"] / (
        out["Range_Bps"].rolling(20).mean().shift(1) + EPS
    )
    out["Compression_20"] = out["Range_Bps"].rolling(20).mean() / (
        out["Range_Bps"].rolling(100).mean() + EPS
    )

    log_close = np.log(close + EPS)
    out["Trend_Slope_20"] = log_close.rolling(20).apply(rolling_slope, raw=True)
    out["Trend_Slope_50"] = log_close.rolling(50).apply(rolling_slope, raw=True)
    out["Trend_R2_20"] = log_close.rolling(20).apply(rolling_r2, raw=True)
    out["Trend_R2_50"] = log_close.rolling(50).apply(rolling_r2, raw=True)
    out["Efficiency_Ratio_20"] = efficiency_ratio(close, 20)
    out["Efficiency_Ratio_50"] = efficiency_ratio(close, 50)

    for window in [20, 50]:
        rolling_low = low.rolling(window=window).min()
        rolling_high = high.rolling(window=window).max()
        out[f"Channel_Position_{window}"] = (close - rolling_low) / (
            (rolling_high - rolling_low) + EPS
        )
        out[f"Channel_Width_{window}"] = (rolling_high - rolling_low) / (close + EPS)
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is None or bb.shape[1] < 5:
        raise EdgeRuntimeError("pandas_ta_classic Bollinger Bands did not return expected 5 columns")
    out["BBB_20_2"] = bb.iloc[:, 3]
    out["BBP_20_2"] = bb.iloc[:, 4]
    dc = ta.donchian(high, low, lower_length=20, upper_length=20)
    if dc is None or dc.shape[1] < 3:
        raise EdgeRuntimeError("pandas_ta_classic Donchian did not return expected 3 columns")
    out["Donchian_Position_20"] = (close - dc.iloc[:, 0]) / (
        (dc.iloc[:, 2] - dc.iloc[:, 0]) + EPS
    )

    out["Overhead_Sweep_ATR"] = log_upper_wick / (out["NATR"] + EPS)
    out["Support_Sweep_ATR"] = log_lower_wick / (out["NATR"] + EPS)
    out["Upper_Wick_Dominance"] = out["Upper_Wick_Share"] - out["Lower_Wick_Share"]
    out["Lower_Wick_Dominance"] = out["Lower_Wick_Share"] - out["Upper_Wick_Share"]
    out["Exhaustion_Spike"] = out["RSI_14"] * out["Overhead_Sweep_ATR"]
    out["Spring_Spike"] = (100.0 - out["RSI_14"]) * out["Support_Sweep_ATR"]

    out = add_v1_feature_expansion(out)
    out = add_verified_regime_features(out)

    expected = _schema_feature_names()
    for name in expected:
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out[expected] = out[expected].replace([np.inf, -np.inf], np.nan)
    _validate_feature_output(out)
    if drop_warmup_rows:
        out = out.dropna(subset=expected).reset_index(drop=True)
    return out


def generate_candidate_events(
    features: pd.DataFrame | None = None,
    *args: Any,
    **kwargs: Any,
) -> pd.DataFrame:
    """Generate V1 volatility-carry events and apply V1 Step 5A short mapping."""
    try:
        feature_table = build_features(drop_warmup_rows=False) if features is None else features
        raw_events = generate_v1_downsidevolatilitycascade_short_candidate_events(
            feature_table,
            **kwargs,
        )
        return filter_short_events_with_primary_side(raw_events, feature_table)
    except (CandidateEventError, PrimarySideError) as exc:
        raise EdgeRuntimeError(f"{EDGE_ID} candidate event generation failed: {exc}") from exc


def build_model_rows(
    features: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    bars: pd.DataFrame | None = None,
    *,
    vertical_horizon: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    try:
        feature_table = build_features(drop_warmup_rows=False) if features is None else features
        event_table = generate_candidate_events(feature_table) if events is None else events
        bar_table = _load_asset_bars() if bars is None else bars
        return build_event_model_rows(
            features=feature_table,
            events=event_table,
            bars=bar_table,
            feature_names=_schema_feature_names(),
            vertical_horizon=vertical_horizon or model_row_config()["vertical_horizon"],
        )
    except ModelRowsError as exc:
        raise EdgeRuntimeError(f"{EDGE_ID} model row build failed: {exc}") from exc


def _load_selected_features() -> list[str]:
    try:
        return _edge_scorer().load_selected_features()
    except (EdgeScoringNotReady, EdgeScoringContractError) as exc:
        raise EdgeRuntimeError(
            f"{EDGE_ID} selected-feature contract is not scoring-ready: {exc}"
        ) from exc


def _build_live_candidate_model_rows(
    features: pd.DataFrame,
    events: pd.DataFrame,
    selected_features: list[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if features is None or features.empty:
        raise EdgeRuntimeError("Cannot score because feature table is empty.")
    if events is None or events.empty:
        raise EdgeRuntimeError("Cannot score because candidate event table is empty.")
    feature_table = features.copy()
    event_table = events.copy()
    feature_table["Datetime"] = pd.to_datetime(
        feature_table["Datetime"], utc=True, errors="coerce"
    )
    event_table["Datetime"] = pd.to_datetime(
        event_table["Datetime"], utc=True, errors="coerce"
    )
    feature_table = feature_table.dropna(subset=["Datetime"]).sort_values("Datetime")
    event_table = event_table.dropna(subset=["Datetime"]).sort_values("Datetime")
    forbidden_live_cols = {
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
    forbidden_in_events = sorted(forbidden_live_cols.intersection(event_table.columns))
    if forbidden_in_events:
        raise EdgeRuntimeError(
            f"Candidate events contain forbidden research/scoring columns: {forbidden_in_events}"
        )
    selected_from_features = [
        feature for feature in selected_features if feature in feature_table.columns
    ]
    missing_selected = [
        feature
        for feature in selected_features
        if feature not in feature_table.columns and feature not in event_table.columns
    ]
    if missing_selected:
        raise EdgeRuntimeError(
            f"Selected features missing from runtime feature/event tables: {missing_selected}"
        )
    selected_set = set(selected_features)
    event_context_cols = [
        col
        for col in event_table.columns
        if col not in forbidden_live_cols and col not in selected_set
    ]
    if "Datetime" not in event_context_cols:
        event_context_cols.insert(0, "Datetime")
    feature_cols = list(dict.fromkeys(["Datetime", *selected_from_features]))
    rows = event_table[event_context_cols].merge(
        feature_table[feature_cols],
        on="Datetime",
        how="inner",
        validate="many_to_one",
    )
    if rows.empty:
        raise EdgeRuntimeError("No live candidate rows survived event/feature alignment.")
    forbidden_present = sorted(forbidden_live_cols.intersection(rows.columns))
    if forbidden_present:
        raise EdgeRuntimeError(
            f"Live scoring rows contain forbidden research/scoring columns: {forbidden_present}"
        )
    before_drop = len(rows)
    for feature in selected_features:
        rows[feature] = pd.to_numeric(rows[feature], errors="coerce")
    rows = rows.replace([np.inf, -np.inf], np.nan).dropna(
        subset=selected_features
    ).copy()
    if rows.empty:
        raise EdgeRuntimeError("All candidate rows dropped after selected-feature NaN filtering.")
    rows = rows.sort_values("Datetime").reset_index(drop=True)
    return rows, {
        "edge_id": EDGE_ID,
        "row_builder": "downsidevolatilitycascade_short_live_candidate_model_rows_no_labels",
        "candidate_events_input": int(len(event_table)),
        "rows_before_selected_feature_filter": int(before_drop),
        "rows_after_selected_feature_filter": int(len(rows)),
        "selected_feature_count": int(len(selected_features)),
        "uses_future_information": False,
        "contains_research_labels": False,
        "scoring_owner": "shared.scoring.edge_scorer.EdgeScorer",
    }


def _build_scoring_rows(
    model_rows: pd.DataFrame | None = None,
    *,
    features: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    bars: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    selected = _load_selected_features()
    if model_rows is not None:
        rows = model_rows.copy()
        forbidden = {
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
        }
        forbidden_present = sorted(forbidden.intersection(rows.columns))
        if forbidden_present:
            raise EdgeRuntimeError(
                f"Provided model_rows contain forbidden label/future columns: {forbidden_present}"
            )
        missing = [feature for feature in selected if feature not in rows.columns]
        if missing:
            raise EdgeRuntimeError(f"Provided model_rows missing selected features: {missing}")
        return rows, {
            "edge_id": EDGE_ID,
            "row_builder": "provided_live_safe_model_rows",
            "rows_after_selected_feature_filter": int(len(rows)),
            "selected_feature_count": int(len(selected)),
            "uses_future_information": False,
            "contains_research_labels": False,
            "scoring_owner": "shared.scoring.edge_scorer.EdgeScorer",
        }
    feature_table = (
        build_features(bars=bars, drop_warmup_rows=False)
        if features is None
        else features.copy()
    )
    event_table = (
        generate_candidate_events(feature_table) if events is None else events.copy()
    )
    return _build_live_candidate_model_rows(feature_table, event_table, selected)


def load_model(*args: Any, **kwargs: Any) -> Any:
    if args or kwargs:
        raise EdgeRuntimeError(
            "load_model does not accept artifact overrides. Use canonical SOTD artifacts only."
        )
    try:
        return _edge_scorer().load_model()
    except (EdgeScoringNotReady, EdgeScoringContractError) as exc:
        raise EdgeRuntimeError(f"{EDGE_ID} model artifact is not scoring-ready: {exc}") from exc


def load_calibrator(*args: Any, **kwargs: Any) -> dict[str, Any]:
    if args or kwargs:
        raise EdgeRuntimeError(
            "load_calibrator does not accept artifact overrides. Use canonical SOTD artifacts only."
        )
    try:
        return _edge_scorer().load_calibrator()
    except (EdgeScoringNotReady, EdgeScoringContractError) as exc:
        raise EdgeRuntimeError(
            f"{EDGE_ID} calibrator artifact is not scoring-ready: {exc}"
        ) from exc


def score(
    model_rows: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    bars: pd.DataFrame | None = None,
    *,
    features: pd.DataFrame | None = None,
    latest_only: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if features is None and events is not None and model_rows is not None:
        features = model_rows
        model_rows = None
    candidate_rows, row_manifest = _build_scoring_rows(
        model_rows=model_rows,
        features=features,
        events=events,
        bars=bars,
    )
    try:
        scored = _edge_scorer().score(candidate_rows, latest_only=latest_only)
    except (EdgeScoringNotReady, EdgeScoringContractError) as exc:
        raise EdgeRuntimeError(f"{EDGE_ID} shared EdgeScorer failed closed: {exc}") from exc
    if "Datetime" in scored.columns and "selected_threshold" in scored.columns:
        scored["signal_id"] = [
            f"{EDGE_ID}|{pd.Timestamp(ts).isoformat()}|thr_{float(thr):.4f}"
            for ts, thr in zip(scored["Datetime"], scored["selected_threshold"])
        ]
    artifact_status = _edge_scorer().validate_artifacts()
    manifest = {
        "edge_id": EDGE_ID,
        "runtime_status": "SCORING_DELEGATED_TO_SHARED_EDGE_SCORER_REPLAY_AUDIT_REQUIRED",
        "deployable": False,
        "probability_flow": (
            "raw_probability -> sigmoid calibrated_probability -> AFML bet size -> calibrated-threshold decision"
        ),
        "selected_feature_count": int(artifact_status["selected_feature_count"]),
        "selected_threshold": (
            float(scored["selected_threshold"].iloc[-1])
            if "selected_threshold" in scored.columns and not scored.empty
            else None
        ),
        "threshold_probability_column": CALIBRATED_PROBABILITY_COL,
        "candidate_row_manifest": row_manifest,
        "rows_scored": int(len(scored)),
        "approved_signals": (
            int(scored[APPROVED_SIGNAL_COL].sum())
            if APPROVED_SIGNAL_COL in scored.columns
            else 0
        ),
        "scoring_owner": "shared.scoring.edge_scorer.EdgeScorer",
    }
    return scored, manifest


def explain_missing_features(
    model_rows: pd.DataFrame | None = None,
    *args: Any,
    features: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    bars: pd.DataFrame | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if args or kwargs:
        raise EdgeRuntimeError(
            "explain_missing_features accepts model_rows plus keyword inputs only."
        )
    try:
        if model_rows is None:
            rows, row_manifest = _build_scoring_rows(
                features=features,
                events=events,
                bars=bars,
            )
        else:
            rows, row_manifest = _build_scoring_rows(model_rows=model_rows)
        report = _edge_scorer().explain_missing_features(rows)
        report["runtime_status"] = "MISSING_FEATURE_EXPLANATION_FROM_SHARED_EDGE_SCORER"
        report["deployable"] = False
        report["candidate_row_manifest"] = row_manifest
        return report
    except (EdgeScoringNotReady, EdgeScoringContractError, EdgeRuntimeError) as exc:
        return {
            "edge_id": EDGE_ID,
            "runtime_status": "SCORING_ARTIFACTS_OR_ROWS_NOT_READY",
            "deployable": False,
            "message": str(exc),
        }


def _distribution(series: pd.Series) -> dict[str, float]:
    return {
        str(k): float(v)
        for k, v in series.value_counts(normalize=True).sort_index().to_dict().items()
    }


def _self_test() -> None:
    print("✅ NASDAQ/downsidevolatilitycascade_short runtime self-test passed.")
    contract = load_contract()
    env = validate_environment()
    features = build_features(drop_warmup_rows=False)
    full_schema_features = _schema_feature_names()
    feature_complete_rows = int(features.dropna(subset=full_schema_features).shape[0])
    events = generate_candidate_events(features)
    model_rows, manifest = build_model_rows(features=features, events=events)
    print(f"Edge: {EDGE_ID}")
    print(f"Runtime status: {contract['runtime_status']}")
    print(f"Environment: {env['runtime_environment_status']}")
    print("Deployable: False")
    print(
        f"Bar contract: {'PASS' if env['bar_verification']['bar_contract_passed'] else 'FAIL'}"
    )
    print(
        "Feature implementation: generic NASDAQ V2 features + V1 feature_expansion + verified regime features"
    )
    print(
        "Volatility-carry event fields are computed in candidate_events.py, not as generic model-schema features"
    )
    print(f"Bars/features rows: {len(features)}")
    print(f"Schema features produced: {len(full_schema_features)}")
    print(f"Rows with all schema features available: {feature_complete_rows}")
    print("Primary side core: Volatility_Carry_Event_Type=-1 -> side=-1 short")
    print(f"Primary side config: {primary_side_config()}")
    print(
        "Candidate event generator: downsidevolatilitycascade_short_v1_calm_vol_negative_drift_candidate_events"
    )
    print(f"Candidate events after primary side mapping: {len(events)}")
    print(f"Model row builder: {manifest['row_builder']}")
    print(f"Model rows built: {len(model_rows)}")
    print(f"Meta-label distribution: {_distribution(model_rows['meta_label'])}")
    print(f"Barrier hit distribution: {_distribution(model_rows['barrier_hit'])}")
    print(f"Resolution source distribution: {_distribution(model_rows['resolution_source'])}")
    print(f"Labeling stats: {manifest['labeling_stats']}")
    print("Model artifact/calibrator/score: FAIL-CLOSED UNTIL SHORT-SIDE ARTIFACTS PASS")


if __name__ == "__main__":
    _self_test()
