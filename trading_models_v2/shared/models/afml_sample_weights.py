"""Canonical AFML Chapter 4 observation-weight utilities for Quantara.

This module implements the Lopez de Prado sampling concepts on the *underlying
price-bar clock*:

- bar concurrency c_t across event intervals [t0, t1]
- average uniqueness mean(1 / c_t) per event
- absolute concurrency-adjusted log-return attribution
- normalization of event weights so sum(weights) == number of events
- piecewise-linear time decay on cumulative uniqueness (Snippet 4.11, p.70)

Event end t1 is the TRAINING LABEL's own end (Task 2A ``label_t1``, a tick
timestamp), snapped to the bar that contains it; rows without a label_t1 fall
back to ``gross_t1`` (AFML Ch.3-4 use the label's t1; owner decision
2026-10-23, Ch.4 walkthrough).

It intentionally does not use Event_Vol as an observation-weight magnitude.
Event_Vol belongs to Quantara's event/barrier geometry, not AFML return
attribution.
"""

from __future__ import annotations

# QUANTARA_AFML_CH4_SAMPLE_WEIGHT_V1

from pathlib import Path
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

AFML_SAMPLE_WEIGHT_CONTRACT_VERSION = "quantara_afml_ch4_bar_clock_return_attribution_v2_label_t1"
AVERAGE_UNIQUENESS_COL = "average_uniqueness"
RETURN_ATTRIBUTION_WEIGHT_COL = "return_attribution_weight"
RAW_RETURN_ATTRIBUTION_COL = "raw_return_attribution"


class AFMLSampleWeightError(RuntimeError):
    """Raised when AFML sample weights cannot be constructed safely."""


def _as_utc_index(values: Sequence[Any], *, name: str) -> pd.DatetimeIndex:
    parsed = to_datetime_iso(pd.Series(values), utc=True, errors="coerce")
    if parsed.isna().any():
        count = int(parsed.isna().sum())
        raise AFMLSampleWeightError(f"{name} contains {count} unparsable/NaT timestamp(s).")
    return pd.DatetimeIndex(parsed)


def snap_to_bar_clock(times: Sequence[Any], bar_index: Sequence[Any]) -> np.ndarray:
    """Position of the bar that contains each timestamp: the first bar whose Datetime (bar close) is >= the time.

    A bar's Datetime is its last tick, so a tick at time tau belongs to the first bar closing at or after tau.
    Raises when a time falls after the last bar (the label would end outside the bar clock).
    """
    stamps = _as_utc_index(times, name="times").as_unit("ns").asi8
    bars = _as_utc_index(bar_index, name="bar_index").as_unit("ns").asi8
    pos = np.searchsorted(bars, stamps, side="left")
    if (pos >= len(bars)).any():
        raise AFMLSampleWeightError(f"{int((pos >= len(bars)).sum())} event end time(s) fall after the last bar.")
    return pos.astype("int64")


def label_end_times(frame: pd.DataFrame, *, label_t1_col: str = "label_t1", fallback_t1_col: str = "gross_t1") -> pd.Series:
    """Training-label end time per row: label_t1, else fallback_t1 (rows Task 2A did not resolve)."""
    fallback = to_datetime_iso(frame[fallback_t1_col], utc=True, errors="coerce")
    if label_t1_col not in frame.columns:
        return fallback
    label = to_datetime_iso(frame[label_t1_col], utc=True, errors="coerce")
    return label.fillna(fallback)


def get_time_decay(uniqueness: pd.Series, last_weight: float = 1.0) -> pd.Series:
    """AFML Snippet 4.11 (p.70): piecewise-linear decay on CUMULATIVE UNIQUENESS (not calendar time).

    ``uniqueness`` is indexed by event time. The newest observation gets 1, the oldest gets ``last_weight`` (c):
    c = 1 no decay; 0 < c < 1 linear decay, all weights > 0; c = 0 oldest -> 0; -1 < c < 0 the oldest
    fraction |c| of cumulative uniqueness gets 0. Returns factors aligned to the input index.
    """
    c = float(last_weight)
    if not (-1.0 < c <= 1.0):
        raise AFMLSampleWeightError(f"time-decay c must be in (-1, 1]; got {c}")
    tw = pd.Series(uniqueness, dtype="float64")
    order = tw.sort_index()
    cum = order.cumsum()
    total = float(cum.iloc[-1])
    if not np.isfinite(total) or total <= 0.0:
        raise AFMLSampleWeightError("Cumulative uniqueness must be positive for time decay.")
    slope = (1.0 - c) / total if c >= 0 else 1.0 / ((c + 1.0) * total)
    const = 1.0 - slope * total
    decay = (const + slope * cum).clip(lower=0.0)
    return decay.reindex(tw.index)


def compute_afml_event_weight_table(
    event_start: Sequence[Any],
    event_end: Sequence[Any],
    bar_index: Sequence[Any],
    close: Sequence[Any],
) -> pd.DataFrame:
    """Compute canonical AFML uniqueness and return-attribution weights.

    Event intervals and price bars are inclusive at both t0 and t1, matching the
    AFML indicator-matrix / return-attribution convention.
    """
    starts = _as_utc_index(event_start, name="event_start")
    ends = _as_utc_index(event_end, name="event_end")
    bars = _as_utc_index(bar_index, name="bar_index")

    if len(starts) == 0:
        raise AFMLSampleWeightError("No labelled events were supplied.")
    if len(starts) != len(ends):
        raise AFMLSampleWeightError("event_start and event_end lengths differ.")
    if len(bars) == 0:
        raise AFMLSampleWeightError("Underlying bar clock is empty.")
    if not bars.is_monotonic_increasing:
        raise AFMLSampleWeightError("Underlying bar timestamps are not monotonically increasing.")
    if bars.has_duplicates:
        raise AFMLSampleWeightError("Underlying bar timestamps contain duplicates.")
    if starts.has_duplicates:
        raise AFMLSampleWeightError("Event start timestamps contain duplicates; event identity is ambiguous.")
    if (ends.asi8 < starts.asi8).any():
        raise AFMLSampleWeightError("At least one event has t1 earlier than t0.")

    close_arr = pd.to_numeric(pd.Series(close), errors="coerce").to_numpy(dtype="float64")
    if len(close_arr) != len(bars):
        raise AFMLSampleWeightError("Close length does not match underlying bar clock length.")
    if not np.isfinite(close_arr).all():
        raise AFMLSampleWeightError("Underlying Close contains NaN/inf values.")
    if (close_arr <= 0.0).any():
        raise AFMLSampleWeightError("Underlying Close must be strictly positive for log returns.")

    start_pos = bars.get_indexer(starts)
    end_pos = bars.get_indexer(ends)
    if (start_pos < 0).any():
        missing = starts[start_pos < 0][:5].astype(str).tolist()
        raise AFMLSampleWeightError(
            "Event start timestamp(s) are absent from the standard_quote_tick bar clock; "
            f"examples={missing}"
        )
    if (end_pos < 0).any():
        missing = ends[end_pos < 0][:5].astype(str).tolist()
        raise AFMLSampleWeightError(
            "Event t1 timestamp(s) are absent from the standard_quote_tick bar clock; "
            f"examples={missing}"
        )
    if (end_pos < start_pos).any():
        raise AFMLSampleWeightError("At least one event maps to an end bar before its start bar.")

    # Concurrency c_t on the actual price-bar clock.
    diff = np.zeros(len(bars) + 1, dtype="float64")
    np.add.at(diff, start_pos, 1.0)
    np.add.at(diff, end_pos + 1, -1.0)
    concurrency = np.cumsum(diff[:-1])

    # Every bar used by every event must have strictly positive concurrency.
    active_mask = concurrency > 0.0
    if not active_mask.any():
        raise AFMLSampleWeightError("No active event bars were found after concurrency construction.")

    inv_concurrency = np.zeros_like(concurrency)
    inv_concurrency[active_mask] = 1.0 / concurrency[active_mask]
    inv_prefix = np.concatenate(([0.0], np.cumsum(inv_concurrency)))
    lengths = (end_pos - start_pos + 1).astype("float64")
    uniqueness = (inv_prefix[end_pos + 1] - inv_prefix[start_pos]) / lengths

    if not np.isfinite(uniqueness).all():
        raise AFMLSampleWeightError("Average uniqueness contains NaN/inf values.")
    if (uniqueness <= 0.0).any() or (uniqueness > 1.0 + 1e-12).any():
        raise AFMLSampleWeightError("Average uniqueness is outside the valid interval (0, 1].")

    # AFML return attribution: sum(log-return / concurrency) over [t0, t1],
    # absolute value, then normalize to sum N (mean 1).
    log_close = np.log(close_arr)
    log_ret = np.empty_like(log_close)
    log_ret[0] = np.nan
    log_ret[1:] = np.diff(log_close)

    attributed_bar_return = np.zeros_like(log_ret)
    valid_ret = active_mask & np.isfinite(log_ret)
    attributed_bar_return[valid_ret] = log_ret[valid_ret] / concurrency[valid_ret]
    attr_prefix = np.concatenate(([0.0], np.cumsum(attributed_bar_return)))
    raw_attr = np.abs(attr_prefix[end_pos + 1] - attr_prefix[start_pos])

    if not np.isfinite(raw_attr).all():
        raise AFMLSampleWeightError("Raw return-attribution weights contain NaN/inf values.")
    total = float(raw_attr.sum())
    if not np.isfinite(total) or total <= 0.0:
        raise AFMLSampleWeightError(
            "All AFML return-attribution weights sum to zero/non-finite; cannot normalize."
        )
    normalized = raw_attr * (float(len(raw_attr)) / total)
    if not np.isfinite(normalized).all():
        raise AFMLSampleWeightError("Normalized AFML weights contain NaN/inf values.")
    if not np.isclose(float(normalized.mean()), 1.0, rtol=1e-10, atol=1e-12):
        raise AFMLSampleWeightError(
            f"Normalized AFML weights do not have mean 1; mean={float(normalized.mean()):.16g}"
        )

    return pd.DataFrame(
        {
            AVERAGE_UNIQUENESS_COL: uniqueness.astype("float64"),
            RAW_RETURN_ATTRIBUTION_COL: raw_attr.astype("float64"),
            RETURN_ATTRIBUTION_WEIGHT_COL: normalized.astype("float64"),
        },
        index=starts,
    )


def build_afml_event_weight_table(
    model_rows_path: str | Path,
    bars_path: str | Path,
    *,
    time_col: str = "Datetime",
    t1_col: str = "t1",
    close_col: str = "Close",
    label_t1_col: str | None = None,
) -> pd.DataFrame:
    """Load labelled event intervals + standard_quote_tick bars and compute weights.

    label_t1_col: when given, each event ends at that column (fallback ``t1_col``), snapped to the bar clock.
    """
    model_rows_path = Path(model_rows_path)
    bars_path = Path(bars_path)
    if not model_rows_path.exists():
        raise AFMLSampleWeightError(f"Missing model rows: {model_rows_path}")
    if not bars_path.exists():
        raise AFMLSampleWeightError(f"Missing standard_quote_tick bars: {bars_path}")

    try:
        cols = [time_col, t1_col] + ([label_t1_col] if label_t1_col else [])
        events = pd.read_csv(model_rows_path, usecols=cols)
    except Exception as exc:
        raise AFMLSampleWeightError(
            f"Could not read {time_col!r}/{t1_col!r} from model rows {model_rows_path}: {exc}"
        ) from exc
    try:
        bars = pd.read_csv(bars_path, usecols=[time_col, close_col])
    except Exception as exc:
        raise AFMLSampleWeightError(
            f"Could not read {time_col!r}/{close_col!r} from standard_quote_tick bars {bars_path}: {exc}"
        ) from exc

    events[time_col] = to_datetime_iso(events[time_col], utc=True, errors="coerce")
    events[t1_col] = to_datetime_iso(events[t1_col], utc=True, errors="coerce")
    if events[[time_col, t1_col]].isna().any().any():
        bad = int(events[[time_col, t1_col]].isna().any(axis=1).sum())
        raise AFMLSampleWeightError(f"Model rows contain {bad} event(s) with invalid t0/t1.")
    if events[time_col].duplicated().any():
        raise AFMLSampleWeightError("Model rows contain duplicate event start timestamps.")
    events = events.sort_values(time_col).reset_index(drop=True)

    bars[time_col] = to_datetime_iso(bars[time_col], utc=True, errors="coerce")
    bars[close_col] = pd.to_numeric(bars[close_col], errors="coerce")
    if bars[[time_col, close_col]].isna().any().any():
        bad = int(bars[[time_col, close_col]].isna().any(axis=1).sum())
        raise AFMLSampleWeightError(f"standard_quote_tick bars contain {bad} invalid Datetime/Close row(s).")
    if bars[time_col].duplicated().any():
        raise AFMLSampleWeightError("standard_quote_tick bars contain duplicate Datetime values.")
    if not bars[time_col].is_monotonic_increasing:
        raise AFMLSampleWeightError(
            "standard_quote_tick bars are not chronologically sorted; refusing to silently reorder them."
        )

    event_end = events[t1_col]
    if label_t1_col:
        ends = label_end_times(events, label_t1_col=label_t1_col, fallback_t1_col=t1_col)
        event_end = pd.DatetimeIndex(bars[time_col])[snap_to_bar_clock(ends, bars[time_col])]
    return compute_afml_event_weight_table(
        event_start=events[time_col],
        event_end=event_end,
        bar_index=bars[time_col],
        close=bars[close_col],
    )


def _self_test() -> None:
    bars = pd.date_range("2024-01-01", periods=8, freq="h", tz="UTC")
    close = np.asarray([100.0, 101.0, 102.0, 101.5, 103.0, 104.0, 103.5, 105.0])
    starts = pd.DatetimeIndex([bars[1], bars[2], bars[5]])
    ends = pd.DatetimeIndex([bars[4], bars[3], bars[6]])
    out = compute_afml_event_weight_table(starts, ends, bars, close)
    if len(out) != 3:
        raise SystemExit("AFML self-test: wrong event count")
    if not np.isclose(float(out[RETURN_ATTRIBUTION_WEIGHT_COL].mean()), 1.0):
        raise SystemExit("AFML self-test: normalized weight mean != 1")
    if not ((out[AVERAGE_UNIQUENESS_COL] > 0) & (out[AVERAGE_UNIQUENESS_COL] <= 1)).all():
        raise SystemExit("AFML self-test: uniqueness outside (0,1]")
    # Snippet 4.11 boundary conditions (p.70): newest = 1, oldest = c, c < 0 zeroes the oldest |c| share.
    tw = pd.Series([1.0, 1.0, 1.0, 1.0], index=bars[:4])
    if not np.allclose(get_time_decay(tw, 1.0), 1.0):
        raise SystemExit("AFML self-test: c=1 must give no decay")
    d = get_time_decay(tw, 0.5)
    if not (np.isclose(d.iloc[-1], 1.0) and np.isclose(d.iloc[0], 0.5 + 0.5 / 4)):
        raise SystemExit(f"AFML self-test: c=0.5 decay wrong {d.tolist()}")
    if not np.isclose(get_time_decay(tw, -0.5).iloc[0], 0.0):
        raise SystemExit("AFML self-test: c=-0.5 must zero the oldest observation")
    if snap_to_bar_clock([bars[2] - pd.Timedelta(minutes=30), bars[2]], bars).tolist() != [2, 2]:
        raise SystemExit("AFML self-test: snap_to_bar_clock must map a tick to the bar that contains it")
    print("shared.models.afml_sample_weights self-test passed")


if __name__ == "__main__":
    _self_test()
