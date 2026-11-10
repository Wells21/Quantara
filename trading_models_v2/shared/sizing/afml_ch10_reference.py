"""Canonical AFML Chapter 10 probability-based bet-sizing reference.

This implements the AFML reference object without Quantara's empirical
break-even rescaling. Quantara-specific sizing extensions must live elsewhere
and be benchmarked against this reference.
"""
from __future__ import annotations
import math
from typing import Any, Sequence
import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

class AFMLBetSizingError(RuntimeError):
    pass

def _normal_cdf(x: Any) -> np.ndarray:
    arr = np.asarray(x, dtype="float64")
    erf = np.vectorize(math.erf)
    return 0.5 * (1.0 + erf(arr / math.sqrt(2.0)))

def probability_to_signal(
    probability: Any,
    predicted_label: Any,
    *,
    num_classes: int = 2,
    primary_side: Any | None = None,
) -> np.ndarray:
    """AFML 10.3: z=(p-1/K)/sqrt(p(1-p)); signal=pred*(2*Phi(z)-1)."""
    if int(num_classes) < 2:
        raise AFMLBetSizingError("num_classes must be >= 2.")
    p = np.asarray(probability, dtype="float64")
    pred = np.asarray(predicted_label, dtype="float64")
    try:
        p, pred = np.broadcast_arrays(p, pred)
    except ValueError as exc:
        raise AFMLBetSizingError(
            "probability and predicted_label are not broadcast-compatible."
        ) from exc
    if not np.isfinite(p).all() or np.any((p <= 0.0) | (p >= 1.0)):
        raise AFMLBetSizingError("probability must be finite and strictly inside (0,1).")
    if not np.isfinite(pred).all():
        raise AFMLBetSizingError("predicted_label must be finite.")
    p0 = 1.0 / float(num_classes)
    z = (p - p0) / np.sqrt(p * (1.0 - p))
    signal = pred * (2.0 * _normal_cdf(z) - 1.0)
    if primary_side is not None:
        side = np.asarray(primary_side, dtype="float64")
        try:
            signal, side = np.broadcast_arrays(signal, side)
        except ValueError as exc:
            raise AFMLBetSizingError("primary_side is not broadcast-compatible.") from exc
        if not np.isfinite(side).all() or not np.isin(side, [-1.0, 1.0]).all():
            raise AFMLBetSizingError("primary_side must contain only -1 or +1.")
        signal = signal * side
    return np.clip(np.asarray(signal, dtype="float64"), -1.0, 1.0)

def average_active_signals(
    signals: pd.DataFrame,
    *,
    signal_column: str = "signal",
    end_time_column: str = "t1",
) -> pd.Series:
    """AFML 10.4: average sizes of all bets active at each change point."""
    if not isinstance(signals.index, pd.DatetimeIndex):
        raise AFMLBetSizingError("signals must be indexed by event start DatetimeIndex.")
    missing = [c for c in (signal_column, end_time_column) if c not in signals.columns]
    if missing:
        raise AFMLBetSizingError(f"signals missing columns: {missing}")
    frame = signals.copy().sort_index()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    else:
        frame.index = frame.index.tz_convert("UTC")
    frame[end_time_column] = to_datetime_iso(
        frame[end_time_column], utc=True, errors="coerce"
    )
    frame[signal_column] = pd.to_numeric(frame[signal_column], errors="coerce")
    if frame[signal_column].isna().any():
        raise AFMLBetSizingError("signal contains NaN/non-numeric values.")
    change_points = set(frame.index)
    change_points.update(frame[end_time_column].dropna().tolist())
    out: dict[pd.Timestamp, float] = {}
    for loc in sorted(change_points):
        active = (frame.index <= loc) & (
            frame[end_time_column].isna().to_numpy()
            | (loc < frame[end_time_column]).to_numpy()
        )
        out[pd.Timestamp(loc)] = (
            float(frame.loc[active, signal_column].mean()) if bool(active.any()) else 0.0
        )
    return pd.Series(out, name="signal", dtype="float64").sort_index()

def discretize_signal(signal: Any, step_size: float) -> Any:
    """AFML 10.5: round to step size and clip to [-1,1]."""
    step = float(step_size)
    if not (0.0 < step <= 1.0):
        raise AFMLBetSizingError("step_size must be in (0,1].")
    if isinstance(signal, pd.Series):
        return ((signal / step).round() * step).clip(-1.0, 1.0)
    arr = np.asarray(signal, dtype="float64")
    if not np.isfinite(arr).all():
        raise AFMLBetSizingError("signal must be finite.")
    return np.clip(np.round(arr / step) * step, -1.0, 1.0)

def get_signal(
    events: pd.DataFrame,
    probability: Sequence[float],
    predicted_label: Sequence[float],
    *,
    num_classes: int = 2,
    side_column: str = "side",
    end_time_column: str = "t1",
    step_size: float = 0.0,
) -> pd.Series:
    """Compose AFML 10.3 -> 10.4 -> optional 10.5."""
    if len(events) != len(probability) or len(events) != len(predicted_label):
        raise AFMLBetSizingError("events/probability/predicted_label length mismatch.")
    side = events[side_column].to_numpy() if side_column in events.columns else None
    raw = probability_to_signal(
        probability, predicted_label, num_classes=num_classes, primary_side=side
    )
    signals = pd.DataFrame(
        {"signal": raw, end_time_column: events[end_time_column].to_numpy()},
        index=events.index,
    )
    averaged = average_active_signals(signals, end_time_column=end_time_column)
    return discretize_signal(averaged, step_size) if step_size > 0 else averaged

# ---------------------------------------------------------------------------
# AFML SS10.6 "Dynamic Bet Sizes and Limit Prices" (Snippets 10.4-10.5)
# ---------------------------------------------------------------------------
#
# A different sizing scheme from 10.3-10.5 above: instead of deriving size
# from a classifier's predicted probability, it derives size from the
# distance between the market price and a forecast price f, via a sigmoid
# of the form betSize(w,x) = x*(w+x**2)**-0.5, calibrated so that a given
# divergence m = f - marketPrice produces a given bet size x (get_w solves
# for the calibration constant w). inv_price/limit_price invert the sizing
# function to find the price at which the position should next be adjusted
# (the resting limit order price), given a target position size.

def bet_size_sigmoid(w: float, divergence: Any) -> Any:
    """AFML Snippet 10.4 betSize: x*(w+x**2)**-0.5, x = f - market_price
    (the forecast/price divergence). Bounded in (-1, 1) for any finite w>0."""
    x = np.asarray(divergence, dtype="float64")
    if not (float(w) > 0.0):
        raise AFMLBetSizingError("w must be > 0.")
    return x * (w + x ** 2) ** -0.5

def get_w(divergence: float, bet_size: float) -> float:
    """AFML Snippet 10.4 getW: solve x**2*(m**-2 - 1) for the calibration
    constant w such that bet_size_sigmoid(w, divergence) == bet_size.
    divergence and bet_size must be a matched (x, m) calibration pair with
    0 < |bet_size| < 1 and divergence != 0."""
    x = float(divergence)
    m = float(bet_size)
    if not (0.0 < abs(m) < 1.0):
        raise AFMLBetSizingError("bet_size must satisfy 0 < |bet_size| < 1.")
    if x == 0.0:
        raise AFMLBetSizingError("divergence must be nonzero.")
    return float(x ** 2 * (m ** -2 - 1.0))

def get_target_position(w: float, forecast_price: float, market_price: float, max_position: int) -> int:
    """AFML Snippet 10.4 getTargetPos: target position (integer units, scaled
    by max_position) implied by the current forecast/market-price divergence."""
    size = float(bet_size_sigmoid(w, forecast_price - market_price))
    return int(size * max_position)

def inv_price(forecast_price: float, w: float, bet_size: float) -> float:
    """AFML Snippet 10.5 invPrice: invert bet_size_sigmoid to recover the
    market price m at which the sigmoid would produce the given bet_size,
    given calibration w and forecast_price f. Requires |bet_size| < 1."""
    m = float(bet_size)
    if not (-1.0 < m < 1.0):
        raise AFMLBetSizingError("bet_size must satisfy |bet_size| < 1.")
    return float(forecast_price - m * (w / (1.0 - m ** 2)) ** 0.5)

def limit_price(
    target_position: int,
    current_position: int,
    forecast_price: float,
    w: float,
    max_position: int,
) -> float:
    """AFML Snippet 10.5 limitPrice: the average limit price to execute the
    incremental order from current_position to target_position, computed by
    averaging inv_price over each unit step of position size between the two
    (the book's own numerical-integration approximation of the fill price a
    monotonically-sizing order sweep would realize)."""
    if int(max_position) <= 0:
        raise AFMLBetSizingError("max_position must be > 0.")
    if int(target_position) == int(current_position):
        raise AFMLBetSizingError("target_position must differ from current_position.")
    sgn = 1 if target_position >= current_position else -1
    total = 0.0
    lo = abs(current_position + sgn)
    hi = abs(target_position + 1)
    for j in range(lo, hi):
        total += inv_price(forecast_price, w, j / float(max_position))
    return float(total / (target_position - current_position))
