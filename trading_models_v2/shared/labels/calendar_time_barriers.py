"""
Calendar-time (wall-clock) Event_Vol lookback and vertical-barrier deadline
helpers -- the shared mechanism for the 2026-09-24 bar-count-to-calendar-time
labeling rewrite (see config/us500_edge_search_ledger.json's
calendar_time_labeling_REWRITE_2026-09-24 entry for the full plan and the
exact AFML page citations this was derived from).

Why this exists: Quantara's bars are causal daily tick-count-threshold bars
(shared/bars/standard_quote_tick_builder.py), not fixed-time bars. A fixed
bar count therefore spans a variable amount of real time depending on
market activity (confirmed empirically 2026-09-24 on DAX/trend_long: a
nominal 20-bar lookback spans p10=3.3h to p90=16.0h in real time) and on
which broker's tick stream produced the bars (Dukascopy for research vs the
live broker) -- neither of which is a real time unit. AFML's own reference
implementation for BOTH the volatility lookback (Snippet 3.1, p.44-45,
getDailyVol) and the vertical barrier (Snippet 3.4, p.49) uses real
calendar time, not bar count. This module reproduces both mechanisms,
generalized from AFML's hardcoded "1 day" to an arbitrary Timedelta so they
can be used at whatever real-time window an edge's Gate A/B/C search
selects.

The two functions intentionally round in OPPOSITE directions -- this is not
a bug, it mirrors AFML exactly:
  - Event_Vol lookback is BACKWARD-looking (a historical return window):
    round to the nearest existing bar AT OR BEFORE the target time
    (searchsorted, then -1) -- a conservative, well-defined historical
    reference point.
  - The vertical barrier is FORWARD-looking (an expiry deadline): round to
    the nearest existing bar AT OR AFTER the target time (searchsorted,
    no -1) -- the position is not "expired" until real time has actually
    elapsed.

Return convention kept as Quantara's own (log return, ewm adjust=False),
not AFML's literal simple-return/adjust=True convention -- empirically
verified 2026-09-24 (corr=0.9998 between the two return conventions at the
same calendar-time window) that the return-type choice barely matters, so
there is no reason to introduce an inconsistency with the rest of
Quantara's log-return-based feature pipeline. Only the WINDOW unit changes,
from bar-count to calendar-time.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


def calendar_time_event_vol(
    dt_ns: np.ndarray,
    close: np.ndarray,
    *,
    hours: float,
    ewm_span: int,
    min_vol: float,
) -> np.ndarray:
    """Backward-looking EWM volatility of log returns taken over a real
    calendar-time window, replacing a bar-count `close.shift(N)` lookback.

    dt_ns: bar timestamps as int64 nanoseconds (e.g. pd.DatetimeIndex.asi8),
        strictly non-decreasing (bars are chronological; duplicate
        timestamps are fine, purely positional).
    close: bar close prices, same length and order as dt_ns.
    hours: the real-time lookback window, e.g. 6.0 for a 6-hour return.
    ewm_span: EWM smoothing span, in units of return OBSERVATIONS (one per
        bar) -- NOT re-derived to calendar time; AFML's own getDailyVol
        does the identical thing (span0=100 daily-observation-count, while
        the return window itself is calendar-time).
    min_vol: floor applied to the resulting volatility series.

    Returns an array the same length as close, with NaN wherever fewer than
    `hours` of history exists yet (mirrors AFML's df0>0 filter -- there is
    no valid backward reference near the start of the series).
    """
    n = len(dt_ns)
    target_ns = dt_ns - np.int64(hours * 3600 * 1e9)
    pos = np.searchsorted(dt_ns, target_ns, side="left") - 1  # AFML Snippet 3.1 convention
    valid = pos >= 0

    ret = np.full(n, np.nan, dtype="float64")
    idx_all = np.arange(n)
    ret[valid] = np.log((close[idx_all[valid]] + EPS) / (close[pos[valid]] + EPS))

    # Use pandas' own ewm implementation directly -- verified 2026-09-24 that
    # a hand-rolled EWMA-of-squared-deviations recursion does NOT match
    # pandas' actual (bias-corrected, cumulative-weight-based) ewm variance
    # algorithm closely enough to trust (max abs diff ~0.0007 on a test
    # series with values of that same order of magnitude -- a real
    # mismatch, not floating-point noise). Correctness matters far more
    # than avoiding this one pandas round-trip.
    vol = pd.Series(ret).ewm(span=int(ewm_span), adjust=False).std().to_numpy()
    vol = np.where(np.isfinite(vol), np.maximum(vol, min_vol), vol)
    return vol


def calendar_time_horizon_bar_offsets(
    dt_ns: np.ndarray,
    entry_positions: np.ndarray,
    *,
    hours: float,
) -> np.ndarray:
    """Per-event bar-count OFFSET (deadline_position - entry_position)
    equivalent to a real calendar-time horizon -- lets the existing
    bar-offset-based touch-path search in v3_horizon_lab.py's
    compute_first_touch_paths/materialize_horizon stay structurally
    unchanged, just fed a per-event (not uniform) horizon length. Returns
    -1 wherever the deadline runs past the end of available history
    (see calendar_time_vertical_deadline_positions)."""
    deadline_pos = calendar_time_vertical_deadline_positions(dt_ns, entry_positions, hours=hours)
    offsets = np.where(deadline_pos < 0, -1, deadline_pos - entry_positions)
    return offsets


def calendar_time_vertical_deadline_positions(
    dt_ns: np.ndarray,
    entry_positions: np.ndarray,
    *,
    hours: float,
) -> np.ndarray:
    """Forward-looking vertical-barrier deadline: the position of the
    nearest existing bar AT OR AFTER (entry_time + hours), replacing a
    bar-count `entry_pos + N` offset. Mirrors AFML Snippet 3.4 exactly
    (t1=close.index.searchsorted(tEvents+pd.Timedelta(...)), no -1 offset).

    entry_positions: integer bar positions of each event's entry bar.
    Returns an array of deadline positions, same length as entry_positions,
    with -1 wherever the deadline runs past the end of available history
    (AFML's `t1=t1[t1<close.shape[0]]` filter, expressed as a sentinel
    instead of a drop so callers can decide how to handle it).
    """
    n = len(dt_ns)
    entry_times = dt_ns[entry_positions]
    target_ns = entry_times + np.int64(hours * 3600 * 1e9)
    pos = np.searchsorted(dt_ns, target_ns, side="left")
    pos = np.where(pos >= n, -1, pos)
    return pos
