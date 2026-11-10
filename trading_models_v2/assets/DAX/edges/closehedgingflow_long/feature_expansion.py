"""
V1-compatible feature expansion for DAX/closehedgingflow_long.

Ports the useful non-regime parts of V1 `3a_full_feature_factory.py` into the
V2 edge runtime boundary.

Boundary:
- Safe OHLC/TA/Kalman/FFD-derived features are added here.
- FFD columns come from the verified shared asset-level FFD artifact.
- Regime features come from the verified shared asset-level HMM artifact.
- The dashboard and Colab must not engineer these features separately.

Note:
- This module imports `load_verified_ffd_features` from closehedgingflow_long /primary_side.py,
  matching the trend_long/trend_short edge structure.
- Compile this module after primary_side.py exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

try:
    import pywt
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "DAX/closehedgingflow_long V1 feature expansion requires PyWavelets (`pywt`), matching V1. "
        "Install PyWavelets in the active venv before running expanded features."
    ) from exc

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from primary_side import ASSET, load_verified_ffd_features  # noqa: E402
from shared.features.afml_ch17_structural_breaks import structural_break_features  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.features.afml_ch18_entropy import entropy_features, load_entropy_encoding  # noqa: E402
from shared.features.afml_ch19_microstructure import becker_parkinson_volatility, parkinson_volatility  # noqa: E402

EPS = 1e-12


class FeatureExpansionError(RuntimeError):
    """Raised when V1 feature expansion must fail closed."""


def apply_1d_kalman_filter(prices: pd.Series) -> pd.Series:
    values = prices.to_numpy(dtype="float64")
    n = len(values)
    if n == 0:
        return pd.Series(dtype=float, index=prices.index)
    state_means = np.zeros(n, dtype="float64")
    state_covs = np.zeros(n, dtype="float64")
    state_means[0] = values[0]
    state_covs[0] = 1.0
    process_variance = 1e-5
    measurement_variance = 1e-3
    for t in range(1, n):
        prior_mean = state_means[t - 1]
        prior_cov = state_covs[t - 1] + process_variance
        kalman_gain = prior_cov / (prior_cov + measurement_variance)
        state_means[t] = prior_mean + kalman_gain * (values[t] - prior_mean)
        state_covs[t] = (1.0 - kalman_gain) * prior_cov
    return pd.Series(state_means, index=prices.index)


def rolling_wavelet_noise(arr: np.ndarray) -> float:
    if len(arr) < 2:
        return np.nan
    _c_a, c_d = pywt.dwt(arr, "haar")
    return float(np.sum(np.square(c_d)))


def shifted_zscore(x: pd.Series, window: int) -> pd.Series:
    return (x - x.rolling(window).mean().shift(1)) / (x.rolling(window).std().shift(1) + EPS)


def safe_log_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    return np.log((num.astype("float64") + EPS) / (den.astype("float64") + EPS))


def _rolling_slope(y: np.ndarray) -> float:
    if len(y) < 2 or not np.isfinite(y).all():
        return np.nan
    x = np.arange(len(y), dtype=float)
    x = x - x.mean()
    y = y.astype(float) - y.astype(float).mean()
    den = np.sum(x * x)
    return np.nan if den <= EPS else float(np.sum(x * y) / den)


def _rolling_r2(y: np.ndarray) -> float:
    if len(y) < 3 or not np.isfinite(y).all():
        return np.nan
    x = np.arange(len(y), dtype=float)
    slope = _rolling_slope(y)
    if not np.isfinite(slope):
        return np.nan
    intercept = float(np.mean(y) - slope * np.mean(x))
    y_hat = intercept + slope * x
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 0.0 if ss_tot <= EPS else float(1.0 - ss_res / ss_tot)


def _efficiency_ratio(close: pd.Series, window: int) -> pd.Series:
    return (close - close.shift(window)).abs() / (close.diff().abs().rolling(window).sum() + EPS)


def merge_verified_ffd(out: pd.DataFrame) -> pd.DataFrame:
    ffd = load_verified_ffd_features()
    cols = ["Datetime", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    return out.merge(ffd[cols], on="Datetime", how="left", validate="one_to_one")


def add_v1_feature_expansion(features: pd.DataFrame) -> pd.DataFrame:
    """Add V1-compatible safe feature expansion to an edge feature table."""

    out = features.copy()
    required = ["Datetime", "Open", "High", "Low", "Close"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise FeatureExpansionError(f"Cannot expand V1 features; missing columns: {missing}")

    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    for col in ["Open", "High", "Low", "Close"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required).copy().sort_values("Datetime").reset_index(drop=True)

    open_ = out["Open"].astype("float64")
    high = out["High"].astype("float64")
    low = out["Low"].astype("float64")
    close = out["Close"].astype("float64")

    out["Range"] = out.get("Range", (high - low).clip(lower=EPS))
    out["Body"] = out.get("Body", close - open_)
    out["Upper_Wick"] = out.get("Upper_Wick", (high - pd.concat([open_, close], axis=1).max(axis=1)).clip(lower=0))
    out["Lower_Wick"] = out.get("Lower_Wick", (pd.concat([open_, close], axis=1).min(axis=1) - low).clip(lower=0))

    real_body_abs = out["Body"].abs()
    out["Candle_Direction"] = np.sign(out["Body"])
    out["Log_Upper_Wick"] = safe_log_ratio(high, pd.concat([open_, close], axis=1).max(axis=1))
    out["Log_Lower_Wick"] = safe_log_ratio(pd.concat([open_, close], axis=1).min(axis=1), low)
    out["Log_Body"] = safe_log_ratio(close, open_)
    out["Upper_Wick_to_Body_Ratio"] = out["Upper_Wick"] / (real_body_abs + 1e-9)
    out["Lower_Wick_to_Body_Ratio"] = out["Lower_Wick"] / (real_body_abs + 1e-9)
    out["Open_Location"] = (open_ - low) / (out["Range"] + EPS)
    out["Gap_Log_Return"] = safe_log_ratio(open_, close.shift(1))

    out["Kalman_Close"] = apply_1d_kalman_filter(close)
    out["Dist_Kalman"] = (close - out["Kalman_Close"]) / (out["Kalman_Close"] + EPS)

    out["RSI_14"] = ta.rsi(out["Kalman_Close"], length=14)
    out["RSI_21"] = ta.rsi(out["Kalman_Close"], length=21)
    out["RSI_50"] = ta.rsi(out["Kalman_Close"], length=50)
    out["RSI_14_Delta_5"] = out["RSI_14"] - out["RSI_14"].shift(5)
    out["RSI_14_Delta_10"] = out["RSI_14"] - out["RSI_14"].shift(10)
    out["MACD_Hist_Delta_5"] = out["MACD_Hist"] - out["MACD_Hist"].shift(5)
    out["Stoch_K_D"] = out["Stoch_K"] - out["Stoch_D"]

    rvgi = ta.rvgi(open_, high, low, close, length=14)
    if rvgi is not None and isinstance(rvgi, pd.DataFrame) and rvgi.shape[1] >= 1:
        out["RVGI"] = rvgi.iloc[:, 0]
        out["RVGI_Signal"] = rvgi.iloc[:, 1] if rvgi.shape[1] > 1 else np.nan
        out["RVGI_Diff"] = out["RVGI"] - out["RVGI_Signal"]
    else:
        out["RVGI"] = np.nan
        out["RVGI_Signal"] = np.nan
        out["RVGI_Diff"] = np.nan

    stc = ta.stc(close)
    out["STC"] = stc.iloc[:, 0] if isinstance(stc, pd.DataFrame) else stc if stc is not None else np.nan

    out["ATR_Ratio_14_50"] = out["ATR_14"] / (out["ATR_50"] + EPS)
    out["Body_ATR_Ratio_14"] = real_body_abs / (out["ATR_14"] + EPS)
    out["Upper_Wick_ATR_Ratio_14"] = out["Upper_Wick"] / (out["ATR_14"] + EPS)
    out["Lower_Wick_ATR_Ratio_14"] = out["Lower_Wick"] / (out["ATR_14"] + EPS)
    out["Wavelet_Noise_20"] = out["Log_Return_1"].rolling(window=20).apply(rolling_wavelet_noise, raw=True)
    out["Wavelet_Noise_50"] = out["Log_Return_1"].rolling(window=50).apply(rolling_wavelet_noise, raw=True)
    out["Roll_Skew_20"] = out["Log_Return_1"].rolling(window=20).skew()
    out["Roll_Skew_50"] = out["Log_Return_1"].rolling(window=50).skew()
    out["Roll_Kurt_20"] = out["Log_Return_1"].rolling(window=20).kurt()
    out["Roll_Kurt_50"] = out["Log_Return_1"].rolling(window=50).kurt()

    down_returns = out["Log_Return_1"].mask(out["Log_Return_1"] > 0, 0.0)
    up_returns = out["Log_Return_1"].mask(out["Log_Return_1"] < 0, 0.0)
    for window in [20, 50, 100]:
        out[f"Downside_Vol_{window}"] = down_returns.rolling(window=window).std()
        out[f"Upside_Vol_{window}"] = up_returns.rolling(window=window).std()
    out["Upside_Downside_Vol_Ratio_20"] = out["Upside_Vol_20"] / (out["Downside_Vol_20"] + EPS)
    out["Upside_Downside_Vol_Ratio_50"] = out["Upside_Vol_50"] / (out["Downside_Vol_50"] + EPS)

    out["Return_Z_50"] = shifted_zscore(out["Log_Return_1"], 50)
    out["Abs_Return_Z_20"] = shifted_zscore(out["Abs_Log_Return_1"], 20)
    out["Range_Bps_Z_50"] = shifted_zscore(out["Range_Bps"], 50)
    out["Body_Bps_Z_20"] = shifted_zscore(out["Abs_Body_Bps"], 20)
    out["Body_To_Range_Z_20"] = shifted_zscore(out["Body_Share"], 20)
    out["Close_Location_Z_20"] = shifted_zscore(out["Close_Location_Recalc"], 20)
    out["Range_Ratio_50"] = out["Range_Bps"] / (out["Range_Bps"].rolling(50).mean().shift(1) + EPS)
    out["Compression_50"] = out["Range_Bps"].rolling(50).mean() / (out["Range_Bps"].rolling(200).mean() + EPS)

    log_close = np.log(close + EPS)
    out["Trend_Slope_10"] = log_close.rolling(10).apply(_rolling_slope, raw=True)
    out["Trend_Slope_100"] = log_close.rolling(100).apply(_rolling_slope, raw=True)
    out["Trend_R2_100"] = log_close.rolling(100).apply(_rolling_r2, raw=True)
    out["Efficiency_Ratio_10"] = _efficiency_ratio(close, 10)

    for window in [20, 50, 100, 200]:
        rolling_low = low.rolling(window=window).min()
        rolling_high = high.rolling(window=window).max()
        out[f"Dist_{window}_Low"] = (close - rolling_low) / (close + EPS)
        out[f"Dist_{window}_High"] = (rolling_high - close) / (close + EPS)
        out[f"Channel_Position_{window}"] = (close - rolling_low) / ((rolling_high - rolling_low) + EPS)
        out[f"Channel_Width_{window}"] = (rolling_high - rolling_low) / (close + EPS)

    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None and bb.shape[1] >= 5:
        out["BBL_20_2"] = bb.iloc[:, 0]
        out["BBM_20_2"] = bb.iloc[:, 1]
        out["BBU_20_2"] = bb.iloc[:, 2]
        out["BBB_20_2"] = bb.iloc[:, 3]
        out["BBP_20_2"] = bb.iloc[:, 4]
        out["Dist_BB_Upper"] = (out["BBU_20_2"] - close) / (close + EPS)
        out["Dist_BB_Lower"] = (close - out["BBL_20_2"]) / (close + EPS)

    dc = ta.donchian(high, low, lower_length=20, upper_length=20)
    if dc is not None and dc.shape[1] >= 3:
        out["DCL_20"] = dc.iloc[:, 0]
        out["DCM_20"] = dc.iloc[:, 1]
        out["DCU_20"] = dc.iloc[:, 2]
        out["Dist_Donchian_Upper"] = (out["DCU_20"] - close) / (close + EPS)
        out["Dist_Donchian_Lower"] = (close - out["DCL_20"]) / (close + EPS)
        out["Donchian_Position_20"] = (close - out["DCL_20"]) / ((out["DCU_20"] - out["DCL_20"]) + EPS)

    out["Div_Price_RSI_20"] = close.rolling(window=20).corr(out["RSI_14"])
    out["Div_Price_RSI_50"] = close.rolling(window=50).corr(out["RSI_14"])
    out["FVG_Upward_Void"] = ((low - high.shift(2)) / (close + EPS)).clip(lower=0)
    out["FVG_Downward_Void"] = ((high.shift(2) - low) / (close + EPS)).clip(lower=0)

    out = merge_verified_ffd(out)
    out["Close_FFD_Z_20"] = shifted_zscore(out["Close_FFD"], 20)
    out["Close_FFD_Z_50"] = shifted_zscore(out["Close_FFD"], 50)
    out["Close_FFD_Momentum_5"] = out["Close_FFD"] - out["Close_FFD"].shift(5)
    out["Close_FFD_Momentum_20"] = out["Close_FFD"] - out["Close_FFD"].shift(20)

    # AFML Ch.17 structural breaks (p.249-262): SADF_t (s.17.4.2), QADF (s.17.4.2.4), CADF (s.17.4.2.5),
    # CSW (s.17.3.2) and SMT (s.17.4.3) at every bar on the 50 closes ending at that bar -- the same rule
    # as the live engine (D061, D063, D064). The 50-bar window is a registered deviation from the book's
    # whole-sample start points (D062); see shared/features/afml_ch17_structural_breaks.py.
    sb = structural_break_features(close, min_sl=10, constant="ct", lags=1, lookback=50, q=0.95, v=0.025)
    out["SADF_ct_50"] = sb["SADF"]
    out["QADF_ct_50"] = sb["QADF"]
    out["QADF_Dispersion_ct_50"] = sb["QADF_Dispersion"]
    out["CADF_ct_50"] = sb["CADF"]
    out["CADF_Dispersion_ct_50"] = sb["CADF_Dispersion"]
    out["CSW_Up_50"] = sb["CSW_Up"]
    out["CSW_Down_50"] = sb["CSW_Down"]
    out["SMT_Poly1_phi0_50"] = sb["SMT_Poly1_phi0"]
    out["SMT_Poly1_phi05_50"] = sb["SMT_Poly1_phi05"]
    out["SMT_Poly1_phi1_50"] = sb["SMT_Poly1_phi1"]
    out["SMT_Poly2_phi0_50"] = sb["SMT_Poly2_phi0"]
    out["SMT_Poly2_phi05_50"] = sb["SMT_Poly2_phi05"]
    out["SMT_Poly2_phi1_50"] = sb["SMT_Poly2_phi1"]
    out["SMT_Exp_phi0_50"] = sb["SMT_Exp_phi0"]
    out["SMT_Exp_phi05_50"] = sb["SMT_Exp_phi05"]
    out["SMT_Exp_phi1_50"] = sb["SMT_Exp_phi1"]
    out["SMT_Power_phi0_50"] = sb["SMT_Power_phi0"]
    out["SMT_Power_phi05_50"] = sb["SMT_Power_phi05"]
    out["SMT_Power_phi1_50"] = sb["SMT_Power_phi1"]

    # AFML Ch.18 entropy features (p.263-279): Kontoyiannis' estimator (Snippet 18.4, expanding window) on the
    # 100 log returns ending at the current bar, encoded binary / quantile-10 / sigma (s.18.5) with the asset's
    # frozen boundaries and sigma -- the same rule as the live engine (D066-D069); see
    # shared/features/afml_ch18_entropy.py.
    ent = entropy_features(close, load_entropy_encoding(ASSET))
    for name in ent.columns:
        out[name] = ent[name]

    # AFML Ch.19 High/Low estimators (p.283-286): Parkinson volatility (p.284) and the Becker-Parkinson volatility
    # of Snippet 19.2. The Corwin-Schultz spread is not a feature: on mid-quote bars it is not a spread (D072). Every
    # other Ch.19 feature needs trades or volume we do not hold; see shared/features/afml_ch19_microstructure.py.
    out["Parkinson_Vol_20"] = parkinson_volatility(high, low, window=20)
    out["Becker_Parkinson_Vol_1"] = becker_parkinson_volatility(high, low, sample_length=1)

    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    return out


def expanded_feature_names() -> list[str]:
    return [
        "Range", "Body", "Upper_Wick", "Lower_Wick", "Candle_Direction", "Log_Upper_Wick", "Log_Lower_Wick", "Log_Body",
        "Upper_Wick_to_Body_Ratio", "Lower_Wick_to_Body_Ratio", "Open_Location", "Gap_Log_Return",
        "Kalman_Close", "Dist_Kalman", "RSI_14_Delta_5", "RSI_14_Delta_10", "MACD_Hist_Delta_5", "Stoch_K_D",
        "RVGI", "RVGI_Signal", "RVGI_Diff", "STC", "ATR_Ratio_14_50", "Body_ATR_Ratio_14", "Upper_Wick_ATR_Ratio_14", "Lower_Wick_ATR_Ratio_14",
        "Wavelet_Noise_20", "Wavelet_Noise_50", "Roll_Skew_20", "Roll_Skew_50", "Roll_Kurt_20", "Roll_Kurt_50",
        "Downside_Vol_20", "Downside_Vol_50", "Downside_Vol_100", "Upside_Vol_20", "Upside_Vol_50", "Upside_Vol_100",
        "Upside_Downside_Vol_Ratio_20", "Upside_Downside_Vol_Ratio_50", "Return_Z_50", "Abs_Return_Z_20", "Range_Bps_Z_50",
        "Body_Bps_Z_20", "Body_To_Range_Z_20", "Close_Location_Z_20", "Range_Ratio_50", "Compression_50",
        "Trend_Slope_10", "Trend_Slope_100", "Trend_R2_100", "Efficiency_Ratio_10",
        "Dist_20_Low", "Dist_20_High", "Dist_50_Low", "Dist_50_High", "Dist_100_Low", "Dist_100_High", "Dist_200_Low", "Dist_200_High",
        "Channel_Position_100", "Channel_Position_200", "Channel_Width_100", "Channel_Width_200",
        "BBL_20_2", "BBM_20_2", "BBU_20_2", "Dist_BB_Upper", "Dist_BB_Lower",
        "DCL_20", "DCM_20", "DCU_20", "Dist_Donchian_Upper", "Dist_Donchian_Lower",
        "Div_Price_RSI_20", "Div_Price_RSI_50", "FVG_Upward_Void", "FVG_Downward_Void",
        "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD", "Close_FFD_Z_20", "Close_FFD_Z_50", "Close_FFD_Momentum_5", "Close_FFD_Momentum_20",
        "SADF_ct_50", "QADF_ct_50", "QADF_Dispersion_ct_50", "CADF_ct_50", "CADF_Dispersion_ct_50",
        "CSW_Up_50", "CSW_Down_50", "SMT_Poly1_phi0_50", "SMT_Poly1_phi05_50", "SMT_Poly1_phi1_50", "SMT_Poly2_phi0_50", "SMT_Poly2_phi05_50", "SMT_Poly2_phi1_50",
        "SMT_Exp_phi0_50", "SMT_Exp_phi05_50", "SMT_Exp_phi1_50", "SMT_Power_phi0_50", "SMT_Power_phi05_50", "SMT_Power_phi1_50",
        "Entropy_Kontoyiannis_Binary_100", "Entropy_Kontoyiannis_Quantile10_100", "Entropy_Kontoyiannis_Sigma_100",
        "Parkinson_Vol_20", "Becker_Parkinson_Vol_1",
    ]
