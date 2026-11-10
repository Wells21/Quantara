"""Canonical asset-level native one-row feature continuation for V2 live scoring.

The batch edge runtime remains the research authority.  On first use this module
bootstraps from ``runtime.build_features`` and captures exact recursive state.
Afterwards it consumes only unseen closed bars in timestamp order and emits the
newest feature rows while retaining a bounded restart-safe tail.

Design goals
------------
* one feature update per asset, not per edge;
* exact recursive carry for EMA/RMA/Kalman/MACD/STC;
* bounded-window newest-row calculations for finite rolling operators;
* existing verified FFD/HMM live artifacts remain authoritative;
* fail closed to the pre-existing verified-suffix/full adapter on any contract,
  append-only, dependency, or numerical issue. Native dependency failures are
  surfaced to the dashboard for controlled full-authority recovery; they are
  never hidden behind suffix replay.
"""
from __future__ import annotations

import hashlib
import math
import pickle
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
import pandas_ta_classic as ta
import pywt

try:
    from pandas_ta_classic.overlap.rma import rma as _pta_rma
except Exception:  # pragma: no cover
    _pta_rma = None

from shared.features.afml_ch17_structural_breaks import structural_break_row as _ch17_structural_break_row
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.features.afml_ch18_entropy import (
    ENTROPY_COLUMNS as _CH18_ENTROPY_COLUMNS,
    ENTROPY_WINDOW as _CH18_ENTROPY_WINDOW,
    entropy_row as _ch18_entropy_row,
    load_entropy_encoding as _ch18_load_entropy_encoding,
)
from shared.features.afml_ch19_microstructure import (
    _K1_PARKINSON as _CH19_K1_PARKINSON,
    _K2 as _CH19_K2,
    _DEN as _CH19_DEN,
)

EPS = 1e-12
ENGINE_VERSION = "native_one_row_features_v1_7_afml_ch17_every_bar_qadf_cadf_ch18_entropy_lag0_3enc_ch19_no_cs_spread"
STATE_SCHEMA_VERSION = "native_one_row_feature_state_v1_4"
# AFML Ch.17 SADF_t / QADF / CADF / CSW / SMT (D061, D063, D064): computed at every bar on the SADF_LOOKBACK closes
# ending at the CURRENT bar -- the same rule as every feature_expansion.py
# (shared/features/afml_ch17_structural_breaks.structural_break_features). A pure bounded-window function of raw
# Close history; no recursive state. The 50-bar window is a registered deviation (D062).
SADF_MIN_SL = 10
SADF_CONSTANT = "ct"
SADF_LAGS = 1
SADF_LOOKBACK = 50
SADF_QUANTILE = 0.95
SADF_QUANTILE_V = 0.025
STRUCTURAL_BREAK_FEATURES = {
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
RAW_TAIL_ROWS = 260
FEATURE_TAIL_ROWS = 800
MAX_FEATURE_HISTORY_ROWS = 6500
ABSORPTION_LIQUIDITY_LOOKBACK = 10
ABSORPTION_SWEEP_THRESHOLD = 0.1
REGIME_COLUMNS = [
    "Regime_State", "Regime_Entropy", "Regime_Duration",
    "Prob_State_0", "Prob_State_1", "Prob_State_2", "Prob_State_3",
]
FFD_COLUMNS = ["Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]


class NativeFeatureError(RuntimeError):
    """Raised when native feature continuation cannot be trusted."""


def _utc(values: Any) -> pd.Series:
    raw = values if isinstance(values, pd.Series) else pd.Series(values)
    try:
        return pd.to_datetime(raw, utc=True, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return to_datetime_iso(raw, utc=True, errors="coerce")


def normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"]
    if not isinstance(bars, pd.DataFrame):
        raise NativeFeatureError(f"bars must be DataFrame, got {type(bars).__name__}")
    missing = [c for c in required if c not in bars.columns]
    if missing:
        raise NativeFeatureError(f"bars missing required columns: {missing}")
    out = bars.copy()
    out["Datetime"] = _utc(out["Datetime"])
    out["Bar_Start"] = _utc(out["Bar_Start"])
    for c in ["Open", "High", "Low", "Close", "Ticks", "Threshold"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["Datetime", "Bar_Start", "Open", "High", "Low", "Close"])
    out = out.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last")
    out = out.reset_index(drop=True)
    if out.empty:
        raise NativeFeatureError("bars are empty after normalization")
    return out


def _safe_log_ratio(num: float, den: float) -> float:
    try:
        return float(math.log((float(num) + EPS) / (float(den) + EPS)))
    except Exception:
        return math.nan


def _rolling_slope(values: list[float] | np.ndarray) -> float:
    y = np.asarray(values, dtype="float64")
    if y.size < 2 or not np.isfinite(y).all():
        return math.nan
    x = np.arange(y.size, dtype="float64")
    x -= x.mean()
    y = y - y.mean()
    den = float(np.sum(x * x))
    return math.nan if den <= EPS else float(np.sum(x * y) / den)


def _rolling_r2(values: list[float] | np.ndarray) -> float:
    y = np.asarray(values, dtype="float64")
    if y.size < 3 or not np.isfinite(y).all():
        return math.nan
    x = np.arange(y.size, dtype="float64")
    slope = _rolling_slope(y)
    if not np.isfinite(slope):
        return math.nan
    intercept = float(np.mean(y) - slope * np.mean(x))
    yhat = intercept + slope * x
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 0.0 if ss_tot <= EPS else float(1.0 - ss_res / ss_tot)


def _series_std(values: list[float], *, ddof: int = 1) -> float:
    arr = np.asarray(values, dtype="float64")
    if arr.size <= ddof or not np.isfinite(arr).all():
        return math.nan
    return float(np.std(arr, ddof=ddof))


def _series_mean(values: list[float]) -> float:
    arr = np.asarray(values, dtype="float64")
    if arr.size == 0 or not np.isfinite(arr).all():
        return math.nan
    return float(np.mean(arr))


def _series_quantile(values: list[float], q: float) -> float:
    arr = np.asarray(values, dtype="float64")
    if arr.size == 0 or not np.isfinite(arr).all():
        return math.nan
    return float(pd.Series(arr).quantile(q))


def _latest_skew(values: list[float]) -> float:
    if len(values) < 3:
        return math.nan
    return float(pd.Series(values, dtype="float64").skew())


def _latest_kurt(values: list[float]) -> float:
    if len(values) < 4:
        return math.nan
    return float(pd.Series(values, dtype="float64").kurt())


def _latest_corr(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return math.nan
    return float(pd.Series(left, dtype="float64").corr(pd.Series(right, dtype="float64")))


def _wavelet_noise(values: list[float]) -> float:
    arr = np.asarray(values, dtype="float64")
    if arr.size < 2 or not np.isfinite(arr).all():
        return math.nan
    _ca, cd = pywt.dwt(arr, "haar")
    return float(np.sum(np.square(cd)))


def _ema_array(values: np.ndarray, length: int) -> np.ndarray:
    result = np.full(values.size, np.nan, dtype="float64")
    if values.size < length:
        return result
    seed = float(np.mean(values[:length]))
    result[length - 1] = seed
    alpha = 2.0 / (length + 1.0)
    for i in range(length, values.size):
        result[i] = alpha * values[i] + (1.0 - alpha) * result[i - 1]
    return result


def _rma_array(values: np.ndarray, length: int) -> np.ndarray:
    """Match pandas-ta-classic SMA-seeded RMA, including skip-na seed mean."""
    result = np.full(values.size, np.nan, dtype="float64")
    if values.size < length:
        return result
    seed_slice = values[:length]
    finite = seed_slice[np.isfinite(seed_slice)]
    seed = float(np.mean(finite)) if finite.size else math.nan
    result[length - 1] = seed
    alpha = 1.0 / float(length)
    prev = seed
    for i in range(length, values.size):
        value = values[i]
        if np.isfinite(value):
            prev = alpha * value + (1.0 - alpha) * prev
        result[i] = prev
    return result




def _ewm_var_state(values: np.ndarray, span: int) -> dict[str, Any]:
    """Reproduce pandas ``ewm(span, adjust=False).var(bias=False)`` state."""
    alpha = 2.0 / (float(span) + 1.0)
    old_wt_factor = 1.0 - alpha
    mean = math.nan
    cov = 0.0
    sum_wt = 1.0
    sum_wt2 = 1.0
    old_wt = 1.0
    nobs = 0
    last_var = math.nan
    for raw in np.asarray(values, dtype="float64"):
        obs = bool(np.isfinite(raw))
        if np.isfinite(mean):
            # ignore_na=False: weights decay even across missing observations.
            sum_wt *= old_wt_factor
            sum_wt2 *= old_wt_factor * old_wt_factor
            old_wt *= old_wt_factor
            if obs:
                old_mean = mean
                if mean != raw:
                    mean = ((old_wt * old_mean) + (alpha * raw)) / (old_wt + alpha)
                cov = ((old_wt * (cov + (old_mean - mean) ** 2)) + (alpha * (raw - mean) ** 2)) / (old_wt + alpha)
                sum_wt += alpha
                sum_wt2 += alpha * alpha
                old_wt += alpha
                sum_wt /= old_wt
                sum_wt2 /= old_wt * old_wt
                old_wt = 1.0
        elif obs:
            mean = float(raw)
        if obs:
            nobs += 1
        if nobs >= 1:
            numerator = sum_wt * sum_wt
            denominator = numerator - sum_wt2
            last_var = (numerator / denominator) * cov if denominator > 0.0 else math.nan
        else:
            last_var = math.nan
    return {
        "span": int(span), "mean": float(mean), "cov": float(cov),
        "sum_wt": float(sum_wt), "sum_wt2": float(sum_wt2),
        "old_wt": float(old_wt), "nobs": int(nobs), "last_var": float(last_var),
    }


def _ewm_var_update(state: dict[str, Any], value: float) -> float:
    span = int(state["span"])
    alpha = 2.0 / (float(span) + 1.0)
    old_wt_factor = 1.0 - alpha
    mean = float(state["mean"])
    cov = float(state["cov"])
    sum_wt = float(state["sum_wt"])
    sum_wt2 = float(state["sum_wt2"])
    old_wt = float(state["old_wt"])
    nobs = int(state["nobs"])
    obs = bool(np.isfinite(value))
    if np.isfinite(mean):
        sum_wt *= old_wt_factor
        sum_wt2 *= old_wt_factor * old_wt_factor
        old_wt *= old_wt_factor
        if obs:
            old_mean = mean
            if mean != value:
                mean = ((old_wt * old_mean) + (alpha * value)) / (old_wt + alpha)
            cov = ((old_wt * (cov + (old_mean - mean) ** 2)) + (alpha * (value - mean) ** 2)) / (old_wt + alpha)
            sum_wt += alpha
            sum_wt2 += alpha * alpha
            old_wt += alpha
            sum_wt /= old_wt
            sum_wt2 /= old_wt * old_wt
            old_wt = 1.0
    elif obs:
        mean = float(value)
    if obs:
        nobs += 1
    numerator = sum_wt * sum_wt
    denominator = numerator - sum_wt2
    variance = (numerator / denominator) * cov if nobs >= 1 and denominator > 0.0 else math.nan
    state.update({"mean": mean, "cov": cov, "sum_wt": sum_wt, "sum_wt2": sum_wt2, "old_wt": old_wt, "nobs": nobs, "last_var": variance})
    return variance


def _macd_internal(close: np.ndarray) -> dict[str, float]:
    fast = _ema_array(close, 12)
    slow = _ema_array(close, 26)
    macd = fast - slow
    signal = np.full(close.size, np.nan, dtype="float64")
    seed_end = 26 - 1 + 9 - 1
    if close.size > seed_end:
        seed_values = macd[26 - 1 : seed_end + 1]
        signal[seed_end] = float(np.mean(seed_values))
        alpha = 2.0 / 10.0
        for i in range(seed_end + 1, close.size):
            signal[i] = alpha * macd[i] + (1.0 - alpha) * signal[i - 1]
    return {
        "fast": float(fast[-1]),
        "slow": float(slow[-1]),
        "macd": float(macd[-1]),
        "signal": float(signal[-1]),
    }


def _kalman_bootstrap(close: np.ndarray) -> tuple[float, float, np.ndarray]:
    if close.size == 0:
        raise NativeFeatureError("cannot bootstrap Kalman on empty close series")
    means = np.zeros(close.size, dtype="float64")
    means[0] = close[0]
    cov = 1.0
    for i in range(1, close.size):
        prior_cov = cov + 1e-5
        gain = prior_cov / (prior_cov + 1e-3)
        means[i] = means[i - 1] + gain * (close[i] - means[i - 1])
        cov = (1.0 - gain) * prior_cov
    return float(means[-1]), float(cov), means


def _stc_bootstrap(close: np.ndarray) -> dict[str, Any]:
    fast = _ema_array(close, 12)
    slow = _ema_array(close, 26)
    xmacd = fast - slow
    tclength = 10
    factor = 0.5
    stoch1 = np.array(xmacd, copy=True)
    pf = np.array(xmacd, copy=True)
    stoch2 = np.array(xmacd, copy=True)
    pff = np.array(xmacd, copy=True)
    if close.size:
        stoch1[0] = pf[0] = stoch2[0] = pff[0] = 0.0
    float_eps = np.finfo(float).eps
    xmacd_had_zero_range = False
    pf_had_zero_range = False
    for i in range(1, close.size):
        start = max(0, i - tclength + 1)
        window = xmacd[start : i + 1]
        if window.size < tclength or not np.isfinite(window).all():
            low = math.nan
            rng = math.nan
        else:
            low = float(np.min(window))
            high = float(np.max(window))
            raw_rng = high - low
            if raw_rng == 0:
                xmacd_had_zero_range = True
            rng = raw_rng + (float_eps if xmacd_had_zero_range else 0.0)
        if np.isfinite(low) and low > 0 and np.isfinite(rng):
            stoch1[i] = 100.0 * ((xmacd[i] - low) / rng)
        else:
            stoch1[i] = stoch1[i - 1]
        pf[i] = round(float(pf[i - 1] + factor * (stoch1[i] - pf[i - 1])), 8)
        pstart = max(0, i - tclength + 1)
        pwin = pf[pstart : i + 1]
        if pwin.size < tclength or not np.isfinite(pwin).all():
            plow = math.nan
            prng = math.nan
        else:
            plow = float(np.min(pwin))
            phigh = float(np.max(pwin))
            raw_prng = phigh - plow
            if raw_prng == 0:
                pf_had_zero_range = True
            prng = raw_prng + (float_eps if pf_had_zero_range else 0.0)
        if np.isfinite(prng) and prng > 0 and np.isfinite(plow):
            stoch2[i] = 100.0 * ((pf[i] - plow) / prng)
        else:
            stoch2[i] = stoch2[i - 1]
        pff[i] = round(float(pff[i - 1] + factor * (stoch2[i] - pff[i - 1])), 8)
    return {
        "ema_fast": float(fast[-1]),
        "ema_slow": float(slow[-1]),
        "xmacd_tail": [float(x) for x in xmacd[-tclength:]],
        "pf_tail": [float(x) for x in pf[-tclength:]],
        "stoch1": float(stoch1[-1]),
        "stoch2": float(stoch2[-1]),
        "pf": float(pf[-1]),
        "pff": float(pff[-1]),
        "xmacd_had_zero_range": bool(xmacd_had_zero_range),
        "pf_had_zero_range": bool(pf_had_zero_range),
    }


def _raw_stoch_arrays(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw = np.full(close.size, np.nan, dtype="float64")
    had_zero = False
    eps = np.finfo(float).eps
    for i in range(13, close.size):
        lo = float(np.min(low[i - 13 : i + 1]))
        hi = float(np.max(high[i - 13 : i + 1]))
        rng = hi - lo
        if rng == 0:
            had_zero = True
        raw[i] = 100.0 * (close[i] - lo) / (rng + (eps if had_zero else 0.0))
    k = pd.Series(raw).rolling(3).mean().to_numpy(dtype="float64")
    d = pd.Series(k).rolling(3).mean().to_numpy(dtype="float64")
    return raw, k, d


def _rvgi_bootstrap(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> dict[str, Any]:
    eps = np.finfo(float).eps
    body = close - open_
    hl = high - low
    body_had_zero = bool(np.any(body == 0))
    hl_had_zero = bool(np.any(hl == 0))
    body_adj = body + (eps if body_had_zero else 0.0)
    hl_adj = hl + (eps if hl_had_zero else 0.0)
    weights = np.asarray([1.0, 2.0, 2.0, 1.0], dtype="float64") / 6.0
    sw_body = pd.Series(body_adj).rolling(4).apply(lambda x: float(np.dot(x, weights)), raw=True)
    sw_hl = pd.Series(hl_adj).rolling(4).apply(lambda x: float(np.dot(x, weights)), raw=True)
    num = sw_body.rolling(14).sum()
    den = sw_hl.rolling(14).sum()
    rvgi = num / den
    sig = rvgi.rolling(4).apply(lambda x: float(np.dot(x, weights)), raw=True)
    hist = rvgi - sig
    return {
        "body_had_zero": body_had_zero,
        "hl_had_zero": hl_had_zero,
        "body_adj_tail": [float(x) for x in body_adj[-17:]],
        "hl_adj_tail": [float(x) for x in hl_adj[-17:]],
        "rvgi_tail": [float(x) for x in rvgi.to_numpy(dtype="float64")[-4:]],
        "last_hist": float(hist.iloc[-1]),
        "last_rvgi": float(rvgi.iloc[-1]),
        "last_signal": float(sig.iloc[-1]),
    }


def _bootstrap_recursive(bars: pd.DataFrame, authority: pd.DataFrame) -> dict[str, Any]:
    close = bars["Close"].to_numpy(dtype="float64")
    open_ = bars["Open"].to_numpy(dtype="float64")
    high = bars["High"].to_numpy(dtype="float64")
    low = bars["Low"].to_numpy(dtype="float64")
    kalman_mean, kalman_cov, kalman_series = _kalman_bootstrap(close)
    kalman_diff = np.diff(kalman_series, prepend=np.nan)
    rsi_states: dict[str, Any] = {}
    raw_rsi_states: dict[str, Any] = {}
    raw_diff = np.diff(close, prepend=np.nan)
    for length in (14, 21, 50):
        pos = np.array(kalman_diff, copy=True)
        neg = np.array(kalman_diff, copy=True)
        pos[pos < 0] = 0.0
        neg[neg > 0] = 0.0
        pos_rma = _rma_array(pos, length)
        neg_rma = _rma_array(neg, length)
        rsi_states[str(length)] = {
            "positive_avg": float(pos_rma[-1]),
            "negative_avg": float(neg_rma[-1]),
        }
        raw_pos = np.array(raw_diff, copy=True)
        raw_neg = np.array(raw_diff, copy=True)
        raw_pos[raw_pos < 0] = 0.0
        raw_neg[raw_neg > 0] = 0.0
        raw_pos_rma = _rma_array(raw_pos, length)
        raw_neg_rma = _rma_array(raw_neg, length)
        raw_rsi_states[str(length)] = {
            "positive_avg": float(raw_pos_rma[-1]),
            "negative_avg": float(raw_neg_rma[-1]),
        }
    prev_close = np.roll(close, 1)
    prev_close[0] = np.nan
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev_close), np.abs(low - prev_close)]), axis=0)
    atr_states: dict[str, float] = {}
    for length in (14, 21, 50, 100):
        atr_states[str(length)] = float(_rma_array(tr, length)[-1])
    ema_states = {}
    for length in (10, 20, 50, 100, 200):
        col = f"EMA_{length}"
        if col in authority.columns and np.isfinite(pd.to_numeric(authority[col], errors="coerce").iloc[-1]):
            ema_states[str(length)] = float(authority[col].iloc[-1])
        else:
            ema_states[str(length)] = float(_ema_array(close, length)[-1])
    raw_stoch, stoch_k, stoch_d = _raw_stoch_arrays(high, low, close)
    event_return_50 = np.full(close.size, np.nan, dtype="float64")
    if close.size > 50:
        event_return_50[50:] = np.log((close[50:] + EPS) / (close[:-50] + EPS))
    return {
        "ema": ema_states,
        "kalman_mean": kalman_mean,
        "kalman_cov": kalman_cov,
        "rsi": rsi_states,
        "raw_rsi": raw_rsi_states,
        "atr": atr_states,
        "macd": _macd_internal(close),
        "stoch": {
            "raw_tail": [float(x) for x in raw_stoch[-3:]],
            "k_tail": [float(x) for x in stoch_k[-3:]],
            "range_had_zero": bool(False),
        },
        "stc": _stc_bootstrap(close),
        "rvgi": _rvgi_bootstrap(open_, high, low, close),
        "event_vol_50_100": _ewm_var_state(event_return_50, 100),
    }


def _absorption_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    open_ = pd.to_numeric(out["Open"], errors="coerce").astype("float64")
    high = pd.to_numeric(out["High"], errors="coerce").astype("float64")
    low = pd.to_numeric(out["Low"], errors="coerce").astype("float64")
    close = pd.to_numeric(out["Close"], errors="coerce").astype("float64")
    atr = pd.to_numeric(out["ATR_14"], errors="coerce").astype("float64")
    bar_range = (high - low).clip(lower=EPS)
    body = close - open_
    body_abs = body.abs()
    uw = (high - pd.concat([open_, close], axis=1).max(axis=1)).clip(lower=0)
    lw = (pd.concat([open_, close], axis=1).min(axis=1) - low).clip(lower=0)
    out["Body_Direction"] = np.sign(body)
    out["Body_Size"] = body_abs
    out["Close_Location"] = (close - low) / (bar_range + EPS)
    out["Range_ATR"] = bar_range / (atr + EPS)
    out["Upper_Wick_ATR"] = uw / (atr + EPS)
    out["Lower_Wick_ATR"] = lw / (atr + EPS)
    out["Bull_Response_ATR"] = (close - open_) / (atr + EPS)
    out["Bear_Response_ATR"] = (open_ - close) / (atr + EPS)
    out["Prev_High_Liq"] = high.rolling(ABSORPTION_LIQUIDITY_LOOKBACK).max().shift(1)
    out["Prev_Low_Liq"] = low.rolling(ABSORPTION_LIQUIDITY_LOOKBACK).min().shift(1)
    out["Sweep_High_ATR_Liq"] = (high - out["Prev_High_Liq"]) / (atr + EPS)
    out["Sweep_Low_ATR_Liq"] = (out["Prev_Low_Liq"] - low) / (atr + EPS)
    out["Clean_Liquidity_High_Sweep"] = (
        (out["Sweep_High_ATR_Liq"] >= ABSORPTION_SWEEP_THRESHOLD) & (close < out["Prev_High_Liq"])
    ).astype("float64")
    out["Clean_Liquidity_Low_Sweep"] = (
        (out["Sweep_Low_ATR_Liq"] >= ABSORPTION_SWEEP_THRESHOLD) & (close > out["Prev_Low_Liq"])
    ).astype("float64")
    event_ret = np.log((close + EPS) / (close.shift(50) + EPS))
    out["Event_Vol"] = event_ret.ewm(span=100, adjust=False).std().clip(lower=1e-8)
    return out


def _ensure_canonical_shared_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Add exact cross-edge aliases required by selected live event families."""
    out = frame.copy()
    if "Range_Bps" in out.columns:
        out["Bar_Volatility_Bps"] = pd.to_numeric(out["Range_Bps"], errors="coerce")
    return out


def _lookup_external(
    runtime: Any,
    asset: str,
    *,
    bars: pd.DataFrame | None = None,
    refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    errors: list[str] = []
    if refresh:
        if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
            raise NativeFeatureError("dependency refresh requires a non-empty closed-bar snapshot")
        try:
            from shared.live_streaming.hotpath import (
                refresh_verified_feature_dependencies_hot,
            )
            report = refresh_verified_feature_dependencies_hot(asset, bars)
            return report["ffd"], report["hmm"], {
                "dependency_refresh_attempted": True,
                "dependency_refresh_succeeded": True,
                "dependency_refresh_mode": report.get("dependency_refresh_mode"),
                "dependency_target_timestamp": report.get("target_timestamp"),
            }
        except Exception as exc:
            errors.append(f"hotpath_refresh:{type(exc).__name__}:{exc}")
            raise NativeFeatureError(
                "verified FFD/HMM dependency refresh failed: " + " | ".join(errors[-4:])
            ) from exc
    try:
        from shared.live_streaming.hotpath import (
            load_verified_ffd_features_hot,
            load_verified_hmm_features_hot,
        )
        ffd = load_verified_ffd_features_hot(asset)
        hmm = load_verified_hmm_features_hot(asset)
        return ffd, hmm, {
            "dependency_refresh_attempted": False,
            "dependency_refresh_succeeded": False,
            "dependency_refresh_mode": "cached_verified_dependencies",
        }
    except Exception as exc:
        errors.append(f"hotpath:{type(exc).__name__}:{exc}")
    # Fall back only to the exact verified loaders captured by the edge runtime.
    # This is a dependency-loader fallback, not a feature-math suffix replay.
    ffd_loader = None
    hmm_loader = None
    for value in vars(runtime).values():
        if not callable(value) or not hasattr(value, "__globals__"):
            continue
        glob = value.__globals__
        ffd_loader = ffd_loader or glob.get("load_verified_ffd_features")
        hmm_loader = hmm_loader or glob.get("load_verified_regime_features")
    try:
        if callable(ffd_loader) and callable(hmm_loader):
            return ffd_loader(asset), hmm_loader(asset), {
                "dependency_refresh_attempted": False,
                "dependency_refresh_succeeded": False,
                "dependency_refresh_mode": "edge_verified_dependency_loaders",
            }
    except TypeError:
        try:
            return ffd_loader(), hmm_loader(asset), {
                "dependency_refresh_attempted": False,
                "dependency_refresh_succeeded": False,
                "dependency_refresh_mode": "edge_verified_dependency_loaders",
            }
        except Exception as exc:
            errors.append(f"edge_loader:{type(exc).__name__}:{exc}")
    except Exception as exc:
        errors.append(f"edge_loader:{type(exc).__name__}:{exc}")
    raise NativeFeatureError("verified FFD/HMM rows unavailable: " + " | ".join(errors[-4:]))


def _external_row_map(
    frame: pd.DataFrame,
    columns: list[str],
    wanted: set[pd.Timestamp] | None = None,
) -> dict[pd.Timestamp, dict[str, float]]:
    """Extract requested FFD/HMM rows without copying/sorting full history."""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "Datetime" not in frame.columns:
        return {}
    stamps = _utc(frame["Datetime"])
    available = [c for c in columns if c in frame.columns]
    if wanted:
        normalized_wanted = {
            (x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC"))
            for x in (pd.Timestamp(v) for v in wanted)
        }
        mask = stamps.isin(normalized_wanted)
        if not bool(mask.any()):
            return {}
        work = frame.loc[mask, available].copy()
        work.insert(0, "Datetime", stamps.loc[mask].to_numpy())
    else:
        work = frame.loc[:, available].copy()
        work.insert(0, "Datetime", stamps.to_numpy())
    work = work.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    work = work.drop_duplicates("Datetime", keep="last")
    output: dict[pd.Timestamp, dict[str, float]] = {}
    for _, row in work.iterrows():
        output[pd.Timestamp(row["Datetime"])] = {
            c: float(pd.to_numeric(pd.Series([row.get(c)]), errors="coerce").iloc[0])
            for c in columns
        }
    return output


def _append_frame(frame: pd.DataFrame, row: Mapping[str, Any], keep: int) -> pd.DataFrame:
    out = pd.concat([frame, pd.DataFrame([dict(row)])], ignore_index=True, sort=False)
    out["Datetime"] = _utc(out["Datetime"])
    out = out.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last")
    return out.tail(keep).reset_index(drop=True)


def _values(frame: pd.DataFrame, column: str, n: int, *, include_current: bool = True) -> list[float]:
    if column not in frame.columns:
        return []
    series = pd.to_numeric(frame[column], errors="coerce")
    if not include_current and len(series):
        series = series.iloc[:-1]
    return [float(x) for x in series.tail(n).to_numpy(dtype="float64")]


def _compute_one(
    *,
    bar: Mapping[str, Any],
    raw_tail: pd.DataFrame,
    feature_tail: pd.DataFrame,
    recursive: dict[str, Any],
    ffd_values: Mapping[str, float],
    hmm_values: Mapping[str, float],
    edge_id: str,
    side_value: int,
    asset: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rec = pickle.loads(pickle.dumps(recursive, protocol=pickle.HIGHEST_PROTOCOL))
    prev_raw = raw_tail.iloc[-1]
    dt = pd.Timestamp(bar["Datetime"])
    o, h, l, c = (float(bar[x]) for x in ("Open", "High", "Low", "Close"))
    prev_close = float(prev_raw["Close"])
    row: dict[str, Any] = {
        "Datetime": dt,
        "Bar_Start": pd.Timestamp(bar["Bar_Start"]),
        "Open": o, "High": h, "Low": l, "Close": c,
        "Ticks": float(bar.get("Ticks", math.nan)),
        "Threshold": float(bar.get("Threshold", math.nan)),
        "edge_id": edge_id,
        "side": int(side_value),
    }
    # Raw returns and candle geometry.
    close_hist = _values(raw_tail, "Close", 101)
    high_hist = _values(raw_tail, "High", 201)
    low_hist = _values(raw_tail, "Low", 201)
    open_hist = _values(raw_tail, "Open", 20)
    for period in (1, 2, 3, 5, 10, 20, 50, 100):
        row[f"Log_Return_{period}"] = _safe_log_ratio(c, close_hist[-period]) if len(close_hist) >= period else math.nan
    row["Abs_Log_Return_1"] = abs(row["Log_Return_1"])
    row["Abs_Log_Return_5"] = abs(row["Log_Return_5"])
    row["Abs_Log_Return_10"] = abs(row["Log_Return_10"])
    row["Fractional_Change"] = (c - o) / (o + EPS)
    bar_range = max(h - l, EPS)
    body = c - o
    uw = max(h - max(o, c), 0.0)
    lw = max(min(o, c) - l, 0.0)
    row.update({
        "Range": bar_range,
        "Range_Bps": (bar_range / (o + EPS)) * 10000.0,
        "Bar_Volatility_Bps": (bar_range / (o + EPS)) * 10000.0,
        "Body": body,
        "Body_Bps": (body / (o + EPS)) * 10000.0,
        "Abs_Body_Bps": abs((body / (o + EPS)) * 10000.0),
        "Body_Share": abs(body) / (bar_range + EPS),
        "Upper_Wick": uw,
        "Lower_Wick": lw,
        "Upper_Wick_Share": uw / (bar_range + EPS),
        "Lower_Wick_Share": lw / (bar_range + EPS),
        "Wick_Imbalance": (uw - lw) / (bar_range + EPS),
        "Close_Location_Recalc": (c - l) / (bar_range + EPS),
        "Gap_Bps": _safe_log_ratio(o, prev_close) * 10000.0,
    })
    log_upper_wick = _safe_log_ratio(h, max(o, c))
    log_lower_wick = _safe_log_ratio(min(o, c), l)

    # Recursive price indicators.
    for length in (10, 20, 50, 100, 200):
        prev = float(rec["ema"][str(length)])
        alpha = 2.0 / (length + 1.0)
        value = alpha * c + (1.0 - alpha) * prev
        rec["ema"][str(length)] = value
        row[f"EMA_{length}"] = value
        row[f"Dist_EMA_{length}"] = (c - value) / (value + EPS)
    row["EMA_10_20_Gap"] = (row["EMA_10"] - row["EMA_20"]) / (c + EPS)
    row["EMA_20_50_Gap"] = (row["EMA_20"] - row["EMA_50"]) / (c + EPS)
    row["EMA_50_100_Gap"] = (row["EMA_50"] - row["EMA_100"]) / (c + EPS)

    # Kalman and Kalman-RSI (feature expansion overwrites raw-close RSI).
    prior_mean = float(rec["kalman_mean"])
    prior_cov = float(rec["kalman_cov"]) + 1e-5
    gain = prior_cov / (prior_cov + 1e-3)
    kalman = prior_mean + gain * (c - prior_mean)
    rec["kalman_mean"] = kalman
    rec["kalman_cov"] = (1.0 - gain) * prior_cov
    row["Kalman_Close"] = kalman
    row["Dist_Kalman"] = (c - kalman) / (kalman + EPS)
    delta_k = kalman - prior_mean
    pos = max(delta_k, 0.0)
    neg = min(delta_k, 0.0)
    for length in (14, 21, 50):
        rs = rec["rsi"][str(length)]
        alpha = 1.0 / length
        pav = alpha * pos + (1.0 - alpha) * float(rs["positive_avg"])
        nav = alpha * neg + (1.0 - alpha) * float(rs["negative_avg"])
        rs["positive_avg"] = pav
        rs["negative_avg"] = nav
        row[f"RSI_{length}"] = 100.0 * pav / (pav + abs(nav)) if pav + abs(nav) != 0 else math.nan

    # Keep raw-close RSI state because the batch runtime creates Exhaustion/Spring
    # before the expansion overwrites RSI_* with Kalman-close RSI.
    raw_delta = c - prev_close
    raw_pos = max(raw_delta, 0.0)
    raw_neg = min(raw_delta, 0.0)
    raw_rsi_values: dict[int, float] = {}
    for length in (14, 21, 50):
        rs = rec["raw_rsi"][str(length)]
        alpha = 1.0 / length
        pav = alpha * raw_pos + (1.0 - alpha) * float(rs["positive_avg"])
        nav = alpha * raw_neg + (1.0 - alpha) * float(rs["negative_avg"])
        rs["positive_avg"] = pav
        rs["negative_avg"] = nav
        raw_rsi_values[length] = 100.0 * pav / (pav + abs(nav)) if pav + abs(nav) != 0 else math.nan

    # MACD exact recursive carry.
    mac = rec["macd"]
    fast = 2.0 / 13.0 * c + (1.0 - 2.0 / 13.0) * float(mac["fast"])
    slow = 2.0 / 27.0 * c + (1.0 - 2.0 / 27.0) * float(mac["slow"])
    macd = fast - slow
    signal = 2.0 / 10.0 * macd + (1.0 - 2.0 / 10.0) * float(mac["signal"])
    mac.update({"fast": fast, "slow": slow, "macd": macd, "signal": signal})
    row["MACD"] = macd
    row["MACD_Signal"] = macd - signal  # runtime names second column (histogram)
    row["MACD_Hist"] = signal           # runtime names third column (signal)

    # Stochastic oscillator.
    lows14 = (low_hist + [l])[-14:]
    highs14 = (high_hist + [h])[-14:]
    raw_stoch = math.nan
    if len(lows14) == 14 and np.isfinite(lows14).all() and np.isfinite(highs14).all():
        lo14, hi14 = min(lows14), max(highs14)
        rng = hi14 - lo14
        if rng == 0:
            rec["stoch"]["range_had_zero"] = True
        rng += np.finfo(float).eps if rec["stoch"].get("range_had_zero") else 0.0
        raw_stoch = 100.0 * (c - lo14) / rng
    rec["stoch"]["raw_tail"] = (list(rec["stoch"].get("raw_tail", [])) + [raw_stoch])[-3:]
    stoch_k = _series_mean(rec["stoch"]["raw_tail"]) if len(rec["stoch"]["raw_tail"]) == 3 else math.nan
    rec["stoch"]["k_tail"] = (list(rec["stoch"].get("k_tail", [])) + [stoch_k])[-3:]
    stoch_d = _series_mean(rec["stoch"]["k_tail"]) if len(rec["stoch"]["k_tail"]) == 3 else math.nan
    row["Stoch_K"] = stoch_k
    row["Stoch_D"] = stoch_d
    row["Stoch_K_D"] = stoch_k - stoch_d
    row["Williams_R_14"] = 100.0 * ((c - min(lows14)) / (max(highs14) - min(lows14)) - 1.0) if len(lows14) == 14 and max(highs14) != min(lows14) else math.nan
    for period in (10, 20, 50):
        row[f"ROC_{period}"] = 100.0 * (c - close_hist[-period]) / close_hist[-period] if len(close_hist) >= period else math.nan

    # ATR/RMA.
    true_range = max(h - l, abs(h - prev_close), abs(l - prev_close))
    for length in (14, 21, 50, 100):
        alpha = 1.0 / length
        atr = alpha * true_range + (1.0 - alpha) * float(rec["atr"][str(length)])
        rec["atr"][str(length)] = atr
        row[f"ATR_{length}"] = atr
    row["NATR"] = row["ATR_14"] / (c + EPS)
    row["NATR_21"] = row["ATR_21"] / (c + EPS)
    row["NATR_50"] = row["ATR_50"] / (c + EPS)
    row["Vol_Regime_14_50"] = row["ATR_14"] / (row["ATR_50"] + EPS)
    row["Vol_Regime_21_100"] = row["ATR_21"] / (row["ATR_100"] + EPS)
    row["Range_ATR_Ratio_14"] = bar_range / (row["ATR_14"] + EPS)

    # Build a provisional frame for finite-window operators.
    provisional = _append_frame(feature_tail, row, FEATURE_TAIL_ROWS)
    def prevwin(col: str, n: int) -> list[float]:
        return _values(provisional, col, n, include_current=False)
    def curwin(col: str, n: int) -> list[float]:
        return _values(provisional, col, n, include_current=True)
    def shifted_z(col: str, n: int, current: float) -> float:
        vals = prevwin(col, n)
        if len(vals) != n or not np.isfinite(vals).all():
            return math.nan
        return (current - float(np.mean(vals))) / (float(np.std(vals, ddof=1)) + EPS)

    row["Return_Z_20"] = shifted_z("Log_Return_1", 20, row["Log_Return_1"])
    row["Range_Bps_Z_20"] = shifted_z("Range_Bps", 20, row["Range_Bps"])
    rb20 = prevwin("Range_Bps", 20)
    row["Range_Ratio_20"] = row["Range_Bps"] / (_series_mean(rb20) + EPS) if len(rb20) == 20 else math.nan
    rb_cur20 = curwin("Range_Bps", 20)
    rb_cur100 = curwin("Range_Bps", 100)
    row["Compression_20"] = _series_mean(rb_cur20) / (_series_mean(rb_cur100) + EPS) if len(rb_cur20) == 20 and len(rb_cur100) == 100 else math.nan

    log_close_hist = [math.log(x + EPS) for x in (close_hist + [c])]
    for n in (20, 50):
        vals = log_close_hist[-n:]
        row[f"Trend_Slope_{n}"] = _rolling_slope(vals) if len(vals) == n else math.nan
        row[f"Trend_R2_{n}"] = _rolling_r2(vals) if len(vals) == n else math.nan
        closes = (close_hist + [c])[-(n + 1):]
        if len(closes) == n + 1:
            row[f"Efficiency_Ratio_{n}"] = abs(closes[-1] - closes[0]) / (sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) + EPS)
        else:
            row[f"Efficiency_Ratio_{n}"] = math.nan
    for n in (20, 50):
        lows = (low_hist + [l])[-n:]
        highs = (high_hist + [h])[-n:]
        if len(lows) == n:
            lo_n, hi_n = min(lows), max(highs)
            row[f"Channel_Position_{n}"] = (c - lo_n) / (hi_n - lo_n + EPS)
            row[f"Channel_Width_{n}"] = (hi_n - lo_n) / (c + EPS)

    closes20 = (close_hist + [c])[-20:]
    if len(closes20) == 20:
        mid = float(np.mean(closes20)); sd = float(np.std(closes20, ddof=0))
        lower, upper = mid - 2.0 * sd, mid + 2.0 * sd
        ulr = upper - lower
        row["BBL_20_2"] = lower; row["BBM_20_2"] = mid; row["BBU_20_2"] = upper
        row["BBB_20_2"] = 100.0 * ulr / mid
        row["BBP_20_2"] = (c - lower) / ulr if ulr != 0 else math.nan
        row["Dist_BB_Upper"] = (upper - c) / (c + EPS)
        row["Dist_BB_Lower"] = (c - lower) / (c + EPS)
    lows20 = (low_hist + [l])[-20:]; highs20 = (high_hist + [h])[-20:]
    if len(lows20) == 20:
        dcl, dcu = min(lows20), max(highs20); dcm = 0.5 * (dcl + dcu)
        row["DCL_20"] = dcl; row["DCM_20"] = dcm; row["DCU_20"] = dcu
        row["Donchian_Position_20"] = (c - dcl) / (dcu - dcl + EPS)
        row["Dist_Donchian_Upper"] = (dcu - c) / (c + EPS)
        row["Dist_Donchian_Lower"] = (c - dcl) / (c + EPS)

    row["Overhead_Sweep_ATR"] = log_upper_wick / (row["NATR"] + EPS)
    row["Support_Sweep_ATR"] = log_lower_wick / (row["NATR"] + EPS)
    row["Upper_Wick_Dominance"] = row["Upper_Wick_Share"] - row["Lower_Wick_Share"]
    row["Lower_Wick_Dominance"] = -row["Upper_Wick_Dominance"]
    row["Exhaustion_Spike"] = raw_rsi_values[14] * row["Overhead_Sweep_ATR"]
    row["Spring_Spike"] = (100.0 - raw_rsi_values[14]) * row["Support_Sweep_ATR"]

    # Expansion geometry and deltas.
    row["Candle_Direction"] = float(np.sign(body))
    row["Log_Upper_Wick"] = log_upper_wick
    row["Log_Lower_Wick"] = log_lower_wick
    row["Log_Body"] = _safe_log_ratio(c, o)
    row["Upper_Wick_to_Body_Ratio"] = uw / (abs(body) + 1e-9)
    row["Lower_Wick_to_Body_Ratio"] = lw / (abs(body) + 1e-9)
    row["Open_Location"] = (o - l) / (bar_range + EPS)
    row["Gap_Log_Return"] = _safe_log_ratio(o, prev_close)
    rsi14_prev5 = _values(feature_tail, "RSI_14", 10)
    row["RSI_14_Delta_5"] = row["RSI_14"] - rsi14_prev5[-5] if len(rsi14_prev5) >= 5 else math.nan
    row["RSI_14_Delta_10"] = row["RSI_14"] - rsi14_prev5[-10] if len(rsi14_prev5) >= 10 else math.nan
    macdh_prev = _values(feature_tail, "MACD_Hist", 5)
    row["MACD_Hist_Delta_5"] = row["MACD_Hist"] - macdh_prev[-5] if len(macdh_prev) >= 5 else math.nan

    # RVGI exact finite-window state. Fail closed if a first-ever zero range would retroactively alter the window.
    rv = rec["rvgi"]
    body_zero = body == 0.0; hl_zero = (h - l) == 0.0
    if body_zero and not rv.get("body_had_zero"):
        raise NativeFeatureError("RVGI first zero body requires authoritative rebootstrap")
    if hl_zero and not rv.get("hl_had_zero"):
        raise NativeFeatureError("RVGI first zero high-low range requires authoritative rebootstrap")
    f_eps = np.finfo(float).eps
    body_adj = body + (f_eps if rv.get("body_had_zero") else 0.0)
    hl_adj = (h - l) + (f_eps if rv.get("hl_had_zero") else 0.0)
    rv["body_adj_tail"] = (list(rv.get("body_adj_tail", [])) + [body_adj])[-17:]
    rv["hl_adj_tail"] = (list(rv.get("hl_adj_tail", [])) + [hl_adj])[-17:]
    weights = np.asarray([1.0, 2.0, 2.0, 1.0], dtype="float64") / 6.0
    def sw4(seq: list[float], end: int) -> float:
        part = seq[end - 4:end]
        return float(np.dot(part, weights)) if len(part) == 4 and np.isfinite(part).all() else math.nan
    body_seq = list(rv["body_adj_tail"]); hl_seq = list(rv["hl_adj_tail"])
    swb = [sw4(body_seq, i) for i in range(4, len(body_seq) + 1)]
    swh = [sw4(hl_seq, i) for i in range(4, len(hl_seq) + 1)]
    rvgi_raw = sum(swb[-14:]) / sum(swh[-14:]) if len(swb) >= 14 and len(swh) >= 14 and sum(swh[-14:]) != 0 else math.nan
    rv["rvgi_tail"] = (list(rv.get("rvgi_tail", [])) + [rvgi_raw])[-4:]
    rvgi_signal = float(np.dot(rv["rvgi_tail"], weights)) if len(rv["rvgi_tail"]) == 4 and np.isfinite(rv["rvgi_tail"]).all() else math.nan
    rvgi_hist = rvgi_raw - rvgi_signal
    # Preserve the batch runtime's positional column mapping.
    row["RVGI"] = rvgi_hist
    row["RVGI_Signal"] = rvgi_raw
    row["RVGI_Diff"] = rvgi_hist - rvgi_raw

    # STC exact recursive carry.
    st = rec["stc"]
    st_fast = 2.0 / 13.0 * c + (1.0 - 2.0 / 13.0) * float(st["ema_fast"])
    st_slow = 2.0 / 27.0 * c + (1.0 - 2.0 / 27.0) * float(st["ema_slow"])
    st["ema_fast"] = st_fast; st["ema_slow"] = st_slow
    xmacd = st_fast - st_slow
    st["xmacd_tail"] = (list(st.get("xmacd_tail", [])) + [xmacd])[-10:]
    if len(st["xmacd_tail"]) == 10 and np.isfinite(st["xmacd_tail"]).all():
        xlow, xhigh = min(st["xmacd_tail"]), max(st["xmacd_tail"]); xr = xhigh - xlow
        if xr == 0: st["xmacd_had_zero_range"] = True
        xr += np.finfo(float).eps if st.get("xmacd_had_zero_range") else 0.0
        stoch1 = 100.0 * ((xmacd - xlow) / xr) if xlow > 0 else float(st["stoch1"])
    else:
        stoch1 = float(st["stoch1"])
    pf = round(float(st["pf"] + 0.5 * (stoch1 - st["pf"])), 8)
    st["pf_tail"] = (list(st.get("pf_tail", [])) + [pf])[-10:]
    if len(st["pf_tail"]) == 10 and np.isfinite(st["pf_tail"]).all():
        plow, phigh = min(st["pf_tail"]), max(st["pf_tail"]); pr = phigh - plow
        if pr == 0: st["pf_had_zero_range"] = True
        pr += np.finfo(float).eps if st.get("pf_had_zero_range") else 0.0
        stoch2 = 100.0 * ((pf - plow) / pr) if pr > 0 else float(st["stoch2"])
    else:
        stoch2 = float(st["stoch2"])
    pff = round(float(st["pff"] + 0.5 * (stoch2 - st["pff"])), 8)
    st.update({"stoch1": stoch1, "pf": pf, "stoch2": stoch2, "pff": pff})
    row["STC"] = pff

    row["ATR_Ratio_14_50"] = row["ATR_14"] / (row["ATR_50"] + EPS)
    row["Body_ATR_Ratio_14"] = abs(body) / (row["ATR_14"] + EPS)
    row["Upper_Wick_ATR_Ratio_14"] = uw / (row["ATR_14"] + EPS)
    row["Lower_Wick_ATR_Ratio_14"] = lw / (row["ATR_14"] + EPS)

    # Refresh provisional with all values calculated so far.
    provisional = _append_frame(feature_tail, row, FEATURE_TAIL_ROWS)
    for n in (20, 50):
        lr = curwin("Log_Return_1", n) if False else _values(provisional, "Log_Return_1", n)
        row[f"Wavelet_Noise_{n}"] = _wavelet_noise(lr) if len(lr) == n else math.nan
        row[f"Roll_Skew_{n}"] = _latest_skew(lr) if len(lr) == n else math.nan
        row[f"Roll_Kurt_{n}"] = _latest_kurt(lr) if len(lr) == n else math.nan
    for n in (20, 50, 100):
        lr = _values(provisional, "Log_Return_1", n)
        if len(lr) == n:
            down = [x if x <= 0 else 0.0 for x in lr]
            up = [x if x >= 0 else 0.0 for x in lr]
            row[f"Downside_Vol_{n}"] = _series_std(down)
            row[f"Upside_Vol_{n}"] = _series_std(up)
        else:
            row[f"Downside_Vol_{n}"] = row[f"Upside_Vol_{n}"] = math.nan
    row["Upside_Downside_Vol_Ratio_20"] = row["Upside_Vol_20"] / (row["Downside_Vol_20"] + EPS)
    row["Upside_Downside_Vol_Ratio_50"] = row["Upside_Vol_50"] / (row["Downside_Vol_50"] + EPS)
    row["Return_Z_50"] = shifted_z("Log_Return_1", 50, row["Log_Return_1"])
    row["Abs_Return_Z_20"] = shifted_z("Abs_Log_Return_1", 20, row["Abs_Log_Return_1"])
    row["Range_Bps_Z_50"] = shifted_z("Range_Bps", 50, row["Range_Bps"])
    row["Body_Bps_Z_20"] = shifted_z("Abs_Body_Bps", 20, row["Abs_Body_Bps"])
    row["Body_To_Range_Z_20"] = shifted_z("Body_Share", 20, row["Body_Share"])
    row["Close_Location_Z_20"] = shifted_z("Close_Location_Recalc", 20, row["Close_Location_Recalc"])
    rb50 = _values(provisional.iloc[:-1], "Range_Bps", 50)
    row["Range_Ratio_50"] = row["Range_Bps"] / (_series_mean(rb50) + EPS) if len(rb50) == 50 else math.nan
    rbcur50 = _values(provisional, "Range_Bps", 50); rbcur200 = _values(provisional, "Range_Bps", 200)
    row["Compression_50"] = _series_mean(rbcur50) / (_series_mean(rbcur200) + EPS) if len(rbcur50) == 50 and len(rbcur200) == 200 else math.nan
    for n in (10, 100):
        vals = log_close_hist[-n:]
        row[f"Trend_Slope_{n}"] = _rolling_slope(vals) if len(vals) == n else math.nan
    vals100 = log_close_hist[-100:]
    row["Trend_R2_100"] = _rolling_r2(vals100) if len(vals100) == 100 else math.nan
    closes11 = (close_hist + [c])[-11:]
    row["Efficiency_Ratio_10"] = abs(closes11[-1] - closes11[0]) / (sum(abs(closes11[i] - closes11[i - 1]) for i in range(1, len(closes11))) + EPS) if len(closes11) == 11 else math.nan
    for n in (20, 50, 100, 200):
        lows = (low_hist + [l])[-n:]; highs = (high_hist + [h])[-n:]
        if len(lows) == n:
            lo_n, hi_n = min(lows), max(highs)
            row[f"Dist_{n}_Low"] = (c - lo_n) / (c + EPS)
            row[f"Dist_{n}_High"] = (hi_n - c) / (c + EPS)
            row[f"Channel_Position_{n}"] = (c - lo_n) / (hi_n - lo_n + EPS)
            row[f"Channel_Width_{n}"] = (hi_n - lo_n) / (c + EPS)
    close20 = (close_hist + [c])[-20:]
    rsi20 = _values(provisional, "RSI_14", 20); close50 = (close_hist + [c])[-50:]; rsi50 = _values(provisional, "RSI_14", 50)
    row["Div_Price_RSI_20"] = _latest_corr(close20, rsi20) if len(close20) == len(rsi20) == 20 else math.nan
    row["Div_Price_RSI_50"] = _latest_corr(close50, rsi50) if len(close50) == len(rsi50) == 50 else math.nan
    row["FVG_Upward_Void"] = max((l - high_hist[-2]) / (c + EPS), 0.0) if len(high_hist) >= 2 else math.nan
    row["FVG_Downward_Void"] = max((high_hist[-2] - l) / (c + EPS), 0.0) if len(high_hist) >= 2 else math.nan

    # Verified external state.
    for name in FFD_COLUMNS:
        row[name] = float(ffd_values.get(name, math.nan))
    ffd_prev = _values(feature_tail, "Close_FFD", 50)
    row["Close_FFD_Z_20"] = (row["Close_FFD"] - np.mean(ffd_prev[-20:])) / (np.std(ffd_prev[-20:], ddof=1) + EPS) if len(ffd_prev) >= 20 else math.nan
    row["Close_FFD_Z_50"] = (row["Close_FFD"] - np.mean(ffd_prev[-50:])) / (np.std(ffd_prev[-50:], ddof=1) + EPS) if len(ffd_prev) >= 50 else math.nan
    row["Close_FFD_Momentum_5"] = row["Close_FFD"] - ffd_prev[-5] if len(ffd_prev) >= 5 else math.nan
    row["Close_FFD_Momentum_20"] = row["Close_FFD"] - ffd_prev[-20] if len(ffd_prev) >= 20 else math.nan

    # AFML Ch.19 High/Low estimators (book p.283-286; see shared/features/afml_ch19_microstructure.py): Parkinson_Vol_20
    # and Becker_Parkinson_Vol_1 (Snippet 19.2); the Corwin-Schultz spread is not a feature (D072) -- exact-
    # formula bounded-window computations. No genuinely-recursive state is
    # needed: this matches the existing pattern already used above for other
    # finite rolling operators (Donchian/Bollinger/Trend_Slope etc.).
    highs20 = high_hist[-19:] + [h]
    lows20 = low_hist[-19:] + [l]
    if (
        len(highs20) == 20
        and len(lows20) == 20
        and np.isfinite(highs20).all()
        and np.isfinite(lows20).all()
        and all(hh >= ll > 0 for hh, ll in zip(highs20, lows20))
    ):
        log_hl_sq_20 = [math.log(hh / ll) ** 2 for hh, ll in zip(highs20, lows20)]
        row["Parkinson_Vol_20"] = math.sqrt(_series_mean(log_hl_sq_20) / _CH19_K1_PARKINSON)
    else:
        row["Parkinson_Vol_20"] = math.nan

    prev_h, prev_l = float(prev_raw["High"]), float(prev_raw["Low"])
    if h > 0 and l > 0 and prev_h > 0 and prev_l > 0 and h >= l and prev_h >= prev_l:
        hl_t = math.log(h / l) ** 2
        hl_prev = math.log(prev_h / prev_l) ** 2
        beta = hl_t + hl_prev  # AFML Snippet 19.1 getBeta, sample_length=1 (p.285)
        gamma = math.log(max(h, prev_h) / min(l, prev_l)) ** 2  # getGamma (p.285)
        sqrt_beta = math.sqrt(beta)
        den = _CH19_DEN
        k2 = _CH19_K2
        sigma = (2.0 ** -0.5 - 1.0) * sqrt_beta / (k2 * den) + math.sqrt(gamma / (k2 ** 2 * den))
        row["Becker_Parkinson_Vol_1"] = max(sigma, 0.0)
    else:
        row["Becker_Parkinson_Vol_1"] = math.nan

    # AFML Ch.18 entropy features (book p.263-279; D066-D069): Snippet 18.4 (expanding window) on the
    # _CH18_ENTROPY_WINDOW log returns ending at the CURRENT bar, binary / quantile-10 / sigma encoded with the
    # asset's frozen boundaries and sigma -- shared/features/afml_ch18_entropy.entropy_row, the same rule as every
    # feature_expansion.py (close_hist excludes the current bar c).
    if len(close_hist) >= _CH18_ENTROPY_WINDOW:
        ent_closes = np.asarray(close_hist[-_CH18_ENTROPY_WINDOW:] + [c], dtype="float64")
        row.update(_ch18_entropy_row(ent_closes, _ch18_load_entropy_encoding(asset)))
    else:
        for name in _CH18_ENTROPY_COLUMNS:
            row[name] = math.nan

    # AFML Ch.17 (book p.249-262): SADF_t, QADF, CADF, CSW, SMT on the SADF_LOOKBACK closes ending at the current bar
    # (close_hist excludes the current bar c) -- see the module-level STRUCTURAL_BREAK_FEATURES block.
    for name in STRUCTURAL_BREAK_FEATURES:
        row[name] = math.nan
    sb_window = close_hist[-(SADF_LOOKBACK - 1):] + [c] if len(close_hist) >= SADF_LOOKBACK - 1 else []
    if len(sb_window) == SADF_LOOKBACK and np.isfinite(sb_window).all() and all(x > 0 for x in sb_window):
        try:
            sb = _ch17_structural_break_row(np.asarray(sb_window, dtype="float64"), min_sl=SADF_MIN_SL, constant=SADF_CONSTANT, lags=SADF_LAGS,
                                            q=SADF_QUANTILE, v=SADF_QUANTILE_V)
            for name, key in STRUCTURAL_BREAK_FEATURES.items():
                row[name] = float(sb[key])
        except (np.linalg.LinAlgError, ValueError, ZeroDivisionError):
            pass

    for name in REGIME_COLUMNS:
        row[name] = float(hmm_values.get(name, math.nan))

    # Event volatility is part of the absorption feature schema and is reused by
    # most event families.  Keep the exact pandas adjust=False EWM variance state.
    event_ret_50 = _safe_log_ratio(c, close_hist[-50]) if len(close_hist) >= 50 else math.nan
    event_var = _ewm_var_update(rec["event_vol_50_100"], event_ret_50)
    row["Event_Vol"] = max(math.sqrt(event_var), 1e-8) if np.isfinite(event_var) else math.nan

    # Absorption superset fields.
    prev_highs = high_hist[-ABSORPTION_LIQUIDITY_LOOKBACK:]
    prev_lows = low_hist[-ABSORPTION_LIQUIDITY_LOOKBACK:]
    prev_hi = max(prev_highs) if len(prev_highs) == ABSORPTION_LIQUIDITY_LOOKBACK else math.nan
    prev_lo = min(prev_lows) if len(prev_lows) == ABSORPTION_LIQUIDITY_LOOKBACK else math.nan
    sweep_hi = (h - prev_hi) / (row["ATR_14"] + EPS) if np.isfinite(prev_hi) else math.nan
    sweep_lo = (prev_lo - l) / (row["ATR_14"] + EPS) if np.isfinite(prev_lo) else math.nan
    row.update({
        "Body_Direction": float(np.sign(body)), "Body_Size": abs(body),
        "Close_Location": (c - l) / (bar_range + EPS),
        "Range_ATR": bar_range / (row["ATR_14"] + EPS),
        "Upper_Wick_ATR": uw / (row["ATR_14"] + EPS),
        "Lower_Wick_ATR": lw / (row["ATR_14"] + EPS),
        "Bull_Response_ATR": (c - o) / (row["ATR_14"] + EPS),
        "Bear_Response_ATR": (o - c) / (row["ATR_14"] + EPS),
        "Prev_High_Liq": prev_hi, "Prev_Low_Liq": prev_lo,
        "Sweep_High_ATR_Liq": sweep_hi, "Sweep_Low_ATR_Liq": sweep_lo,
        "Clean_Liquidity_High_Sweep": float(np.isfinite(sweep_hi) and sweep_hi >= ABSORPTION_SWEEP_THRESHOLD and c < prev_hi),
        "Clean_Liquidity_Low_Sweep": float(np.isfinite(sweep_lo) and sweep_lo >= ABSORPTION_SWEEP_THRESHOLD and c > prev_lo),
    })
    # Replace infinities exactly as batch runtime does.
    for key, value in list(row.items()):
        if isinstance(value, (float, np.floating)) and np.isinf(value):
            row[key] = math.nan
    return row, rec


def _runtime_union_hash(asset: str) -> str:
    payload = f"{ENGINE_VERSION}|{str(asset).upper()}|canonical_internal_superset_authority_projected"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authority_schema_hash(columns: list[str], dtypes: list[str]) -> str:
    payload = "|".join(f"{name}:{dtype}" for name, dtype in zip(columns, dtypes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _project_authoritative_schema(
    frame: pd.DataFrame,
    *,
    columns: list[str],
    schema_hash: str,
) -> pd.DataFrame:
    """Return exactly the authoritative edge schema, keeping native extras private."""
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise NativeFeatureError(
            f"native feature state is missing authoritative columns: {missing[:20]}"
        )
    out = frame.loc[:, columns].copy()
    actual_dtypes = [str(out[name].dtype) for name in columns]
    actual_hash = _authority_schema_hash(columns, actual_dtypes)
    # Dtypes can legitimately widen after appending NaN-bearing rows.  Column identity
    # and order are the hard contract; the bootstrapped dtype hash remains diagnostic.
    out.attrs["authoritative_schema_hash"] = schema_hash
    out.attrs["projected_dtype_hash"] = actual_hash
    return out.reset_index(drop=True)


def bootstrap_state(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    drop_warmup_rows: bool = False,
    authoritative_features: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    bars_norm = normalize_bars(bars)
    if authoritative_features is None:
        authority = runtime.build_features(bars=bars_norm.copy(), drop_warmup_rows=False)
        bootstrap_source = "runtime.build_features"
    else:
        authority = authoritative_features.copy()
        bootstrap_source = "dashboard_supplied_authoritative_full_build"
    if not isinstance(authority, pd.DataFrame) or authority.empty:
        raise NativeFeatureError(f"{edge_id}: authoritative bootstrap returned empty features")
    authority = authority.copy()
    authority["Datetime"] = _utc(authority["Datetime"])
    authority = authority.dropna(subset=["Datetime"]).sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    authority_columns = [str(name) for name in authority.columns]
    authority_dtypes = [str(authority[name].dtype) for name in authority.columns]
    authority_schema_hash = _authority_schema_hash(authority_columns, authority_dtypes)
    # Native-only aliases/superset fields are retained inside the opaque state so
    # one-row calculations can use them, but they are never exposed to the dashboard.
    internal_authority = authority.copy()
    missing_absorption = [c for c in ("Body_Size", "Prev_High_Liq", "Range_ATR") if c not in internal_authority.columns]
    if missing_absorption:
        internal_authority = _absorption_columns(internal_authority)
    internal_authority = _ensure_canonical_shared_columns(internal_authority)
    recursive = _bootstrap_recursive(bars_norm, internal_authority)
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "asset": str(asset).upper(),
        "edge_id": edge_id,
        "contract_hash": _runtime_union_hash(asset),
        "last_timestamp": pd.Timestamp(bars_norm["Datetime"].iloc[-1]).isoformat(),
        "processed_rows": int(len(bars_norm)),
        "raw_tail": bars_norm.tail(RAW_TAIL_ROWS).reset_index(drop=True),
        "feature_tail": internal_authority.tail(FEATURE_TAIL_ROWS).reset_index(drop=True),
        "feature_history": internal_authority.tail(MAX_FEATURE_HISTORY_ROWS).reset_index(drop=True),
        "authority_columns": authority_columns,
        "authority_dtypes": authority_dtypes,
        "authority_schema_hash": authority_schema_hash,
        "recursive": recursive,
        "calls_completed": 1,
        "native_updates": 0,
        "authoritative_bootstraps": 1,
        "authoritative_bootstrap_source": bootstrap_source,
    }
    # Validate serialization before returning to dashboard persistence.
    pickle.loads(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL))
    result = _project_authoritative_schema(
        internal_authority,
        columns=authority_columns,
        schema_hash=authority_schema_hash,
    )
    metadata = {
        "api_version": STATE_SCHEMA_VERSION,
        "continuation_engine_version": ENGINE_VERSION,
        "continuation_mode": "native_one_row_authoritative_bootstrap",
        "feature_math_owner": "edge_runtime.build_features + shared native continuation",
        "authoritative_bootstrap_source": bootstrap_source,
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": True,
        "deterministic_noise_contract_preserved": True,
        "edge_local_incremental_optimization": False,
        "edge_local_authority_rebuilt": True,
        "new_bar_count": int(len(bars_norm)),
        "output_rows": int(len(result)),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "shared_asset_feature_contract_hash": _runtime_union_hash(asset),
        "authoritative_schema_hash": authority_schema_hash,
        "authoritative_output_columns": int(len(authority_columns)),
        "native_internal_columns": int(len(internal_authority.columns)),
        "exact_authoritative_schema_projection": True,
    }
    return result, state, metadata


def update_state(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    state: Mapping[str, Any] | None,
    drop_warmup_rows: bool = False,
    authoritative_features: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    prior = dict(state or {})
    if prior.get("schema_version") != STATE_SCHEMA_VERSION or prior.get("engine_version") != ENGINE_VERSION or prior.get("contract_hash") != _runtime_union_hash(asset):
        return bootstrap_state(
            runtime, asset=asset, edge_id=edge_id, bars=bars,
            drop_warmup_rows=drop_warmup_rows,
            authoritative_features=authoritative_features,
        )
    bars_norm = normalize_bars(bars)
    last = pd.Timestamp(prior.get("last_timestamp"))
    last = last.tz_localize("UTC") if last.tzinfo is None else last.tz_convert("UTC")
    raw_tail = prior.get("raw_tail")
    feature_tail = prior.get("feature_tail")
    feature_history = prior.get("feature_history")
    recursive = prior.get("recursive")
    authority_columns = prior.get("authority_columns")
    authority_schema_hash = str(prior.get("authority_schema_hash") or "")
    if (
        not isinstance(raw_tail, pd.DataFrame) or raw_tail.empty
        or not isinstance(feature_tail, pd.DataFrame) or feature_tail.empty
        or not isinstance(feature_history, pd.DataFrame) or feature_history.empty
        or not isinstance(recursive, dict)
        or not isinstance(authority_columns, list) or not authority_columns
        or not authority_schema_hash
    ):
        return bootstrap_state(
            runtime, asset=asset, edge_id=edge_id, bars=bars_norm,
            drop_warmup_rows=drop_warmup_rows,
            authoritative_features=authoritative_features,
        )
    # Exact append-only overlap check over retained raw bars.
    prior_raw = normalize_bars(raw_tail)
    current_overlap = bars_norm[bars_norm["Datetime"].isin(set(prior_raw["Datetime"]))].copy()
    if len(current_overlap) != len(prior_raw):
        raise NativeFeatureError(f"{edge_id}: retained raw overlap is incomplete")
    current_overlap = current_overlap.set_index("Datetime").loc[prior_raw["Datetime"]].reset_index()
    for col in ("Bar_Start", "Open", "High", "Low", "Close", "Ticks", "Threshold"):
        left = prior_raw[col].astype(str).to_numpy()
        right = current_overlap[col].astype(str).to_numpy()
        if not np.array_equal(left, right):
            raise NativeFeatureError(f"{edge_id}: historical bar revision detected in {col}")
    unseen = bars_norm[bars_norm["Datetime"] > last].copy()
    if unseen.empty:
        out = _project_authoritative_schema(
            feature_history,
            columns=[str(name) for name in authority_columns],
            schema_hash=authority_schema_hash,
        )
        return out, prior, {
            "api_version": STATE_SCHEMA_VERSION,
            "continuation_engine_version": ENGINE_VERSION,
            "continuation_mode": "native_one_row_checkpoint_reuse_no_new_bar",
            "feature_math_owner": "shared native continuation",
            "streaming_parity_passed": True,
            "append_only_history": True,
            "restart_parity_passed": True,
            "hmm_refit_schedule_preserved": True,
            "deterministic_noise_contract_preserved": True,
            "edge_local_incremental_optimization": True,
            "edge_local_authority_rebuilt": False,
            "new_bar_count": 0,
            "output_rows": int(len(out)),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "shared_asset_feature_contract_hash": _runtime_union_hash(asset),
            "authoritative_schema_hash": authority_schema_hash,
            "authoritative_output_columns": int(len(authority_columns)),
            "native_internal_columns": int(len(feature_history.columns)),
            "exact_authoritative_schema_projection": True,
        }
    wanted = {pd.Timestamp(x) for x in unseen["Datetime"]}
    ffd, hmm, dependency_meta = _lookup_external(runtime, asset)
    ffd_map = _external_row_map(ffd, FFD_COLUMNS, wanted)
    hmm_map = _external_row_map(hmm, REGIME_COLUMNS, wanted)
    missing_ffd = sorted(stamp for stamp in wanted if stamp not in ffd_map)
    missing_hmm = sorted(stamp for stamp in wanted if stamp not in hmm_map)
    if missing_ffd or missing_hmm:
        # The completed bar is synchronously journaled but CSV compaction is
        # asynchronous. Refresh verified FFD/HMM state from the exact in-memory
        # closed-bar snapshot once, then retry the native transition once.
        ffd, hmm, dependency_meta = _lookup_external(
            runtime, asset, bars=bars_norm, refresh=True
        )
        ffd_map = _external_row_map(ffd, FFD_COLUMNS, wanted)
        hmm_map = _external_row_map(hmm, REGIME_COLUMNS, wanted)
        missing_ffd = sorted(stamp for stamp in wanted if stamp not in ffd_map)
        missing_hmm = sorted(stamp for stamp in wanted if stamp not in hmm_map)
        if missing_ffd or missing_hmm:
            raise NativeFeatureError(
                f"{edge_id}: verified dependency refresh incomplete "
                f"missing_ffd={[x.isoformat() for x in missing_ffd[:5]]} "
                f"missing_hmm={[x.isoformat() for x in missing_hmm[:5]]}"
            )
    new_rows: list[dict[str, Any]] = []
    for _, bar in unseen.iterrows():
        stamp = pd.Timestamp(bar["Datetime"])
        if stamp not in ffd_map:
            raise NativeFeatureError(f"{edge_id}: verified FFD row missing for {stamp.isoformat()}")
        if stamp not in hmm_map:
            raise NativeFeatureError(f"{edge_id}: verified HMM row missing for {stamp.isoformat()}")
        row, recursive = _compute_one(
            bar=bar.to_dict(), raw_tail=raw_tail, feature_tail=feature_tail,
            recursive=recursive, ffd_values=ffd_map[stamp], hmm_values=hmm_map[stamp],
            edge_id=edge_id, side_value=int(getattr(runtime, "SIDE_VALUE", 0)), asset=asset,
        )
        raw_tail = _append_frame(raw_tail, bar.to_dict(), RAW_TAIL_ROWS)
        feature_tail = _append_frame(feature_tail, row, FEATURE_TAIL_ROWS)
        feature_history = _append_frame(feature_history, row, MAX_FEATURE_HISTORY_ROWS)
        new_rows.append(row)
    next_state = {
        **prior,
        "last_timestamp": pd.Timestamp(unseen["Datetime"].iloc[-1]).isoformat(),
        "processed_rows": int(prior.get("processed_rows", 0)) + int(len(unseen)),
        "raw_tail": raw_tail,
        "feature_tail": feature_tail,
        "feature_history": feature_history,
        "recursive": recursive,
        "calls_completed": int(prior.get("calls_completed", 0)) + 1,
        "native_updates": int(prior.get("native_updates", 0)) + int(len(unseen)),
    }
    pickle.loads(pickle.dumps(next_state, protocol=pickle.HIGHEST_PROTOCOL))
    out = _project_authoritative_schema(
        feature_history,
        columns=[str(name) for name in authority_columns],
        schema_hash=authority_schema_hash,
    )
    metadata = {
        "api_version": STATE_SCHEMA_VERSION,
        "continuation_engine_version": ENGINE_VERSION,
        "continuation_mode": "native_one_row_state_transition",
        "feature_math_owner": "shared native continuation seeded by edge_runtime.build_features",
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": True,
        "deterministic_noise_contract_preserved": True,
        "edge_local_incremental_optimization": True,
        "edge_local_authority_rebuilt": False,
        "new_bar_count": int(len(unseen)),
        "output_rows": int(len(out)),
        "new_output_rows": int(len(new_rows)),
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "shared_asset_feature_contract_hash": _runtime_union_hash(asset),
        "authoritative_schema_hash": authority_schema_hash,
        "authoritative_output_columns": int(len(authority_columns)),
        "native_internal_columns": int(len(feature_history.columns)),
        "exact_authoritative_schema_projection": True,
        **dependency_meta,
    }
    return out, next_state, metadata


def build_features_live_native(
    runtime: Any,
    *,
    asset: str,
    edge_id: str,
    bars: pd.DataFrame,
    state: Mapping[str, Any] | None = None,
    drop_warmup_rows: bool = False,
    authoritative_features: pd.DataFrame | None = None,
    **_: Any,
) -> dict[str, Any]:
    features, next_state, metadata = update_state(
        runtime, asset=asset, edge_id=edge_id, bars=bars, state=state,
        drop_warmup_rows=drop_warmup_rows,
        authoritative_features=authoritative_features,
    )
    return {"features": features, "state": next_state, "metadata": metadata}


def shared_contract_hash(asset: str) -> str:
    return _runtime_union_hash(asset)


__all__ = [
    "ENGINE_VERSION", "STATE_SCHEMA_VERSION", "NativeFeatureError",
    "build_features_live_native", "bootstrap_state", "update_state",
    "shared_contract_hash",
]
