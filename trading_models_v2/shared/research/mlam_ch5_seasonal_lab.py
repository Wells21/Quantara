"""MLAM Ch.5 test T3 (logs/mlam_ch5_tests_20261103/PREREGISTRATION.md): v3_horizon_lab with intraday-seasonal barriers.

MLAM s.5.2 p.66: returns are heteroscedastic through the day, so a barrier that ignores the time of day transfers the
intraday seasonality to the labels; the fix is to standardise by "the volatility predicted over the interval". Here the
barrier unit Event_Vol (EWM std of H-hour log returns, an all-hours average) is multiplied by the predicted-volatility
ratio of the H clock hours STARTING at the event's UTC hour h0:

    v_D(h) = sum of squared bar-to-bar log returns in clock hour h over the SEASONAL_DAYS days before day D / trading days
    f_D(h0) = sqrt( sum_{j<H} v_D(h0 + j mod 24) / (H x mean_h v_D(h)) )

(causal: only days before the event's day; hours without trading, e.g. the daily break, have v ~ 0 and shrink the
window's predicted variance), f = 1 with fewer than SEASONAL_MIN_DAYS trading days of history, clipped to
[SEASONAL_CLIP_LO, SEASONAL_CLIP_HI].

Only the grid (Gate A) and execution-finalist (Gate C) Event_Vol calls are adjusted; the control-parity gate still checks
the UNADJUSTED control against the persisted model rows, so a passing parity proves the baseline is reproduced.

usage: identical to v3_horizon_lab.py (python shared/research/mlam_ch5_seasonal_lab.py --edge-root ... --hybrid-fast-grid ...)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

SEASONAL_DAYS = 60
SEASONAL_MIN_DAYS = 20
SEASONAL_CLIP_LO, SEASONAL_CLIP_HI = 0.25, 4.0
DAY_NS = np.int64(86_400 * 10**9)
HOUR_NS = np.int64(3_600 * 10**9)
EPS = 1e-12


def seasonal_factor(dt_ns: np.ndarray, close: np.ndarray, hours: float) -> np.ndarray:
    """Per-bar f_D(h0) for an H-hour window starting at the bar's hour h0 (see module docstring)."""
    n = len(dt_ns)
    r2 = np.zeros(n)
    r2[1:] = np.log((close[1:] + EPS) / (close[:-1] + EPS)) ** 2
    hour = ((dt_ns // HOUR_NS) % 24).astype(int)
    day = (dt_ns // DAY_NS).astype(np.int64)
    d0 = int(day.min())
    nd = int(day.max()) - d0 + 1
    s = np.bincount((day - d0) * 24 + hour, weights=r2, minlength=nd * 24).reshape(nd, 24)
    has = np.bincount(day - d0, minlength=nd) > 0
    cs = np.vstack([np.zeros((1, 24)), np.cumsum(s, axis=0)])
    cd = np.concatenate([[0], np.cumsum(has)])
    d_idx = np.arange(nd)
    lo = np.maximum(d_idx - SEASONAL_DAYS, 0)
    days = (cd[d_idx] - cd[lo]).astype("float64")[:, None]       # trading days in [D - 60, D - 1]
    v = (cs[d_idx] - cs[lo]) / np.where(days > 0, days, np.nan)    # variance per clock hour, by hour of day
    k = int(np.ceil(hours))
    win = sum(np.roll(v, -j, axis=1) for j in range(k))            # hours h0 .. h0 + k - 1 (mod 24)
    with np.errstate(invalid="ignore", divide="ignore"):
        f = np.sqrt(win / (k * v.mean(axis=1, keepdims=True)))
    f = np.where((days >= SEASONAL_MIN_DAYS) & np.isfinite(f), f, 1.0)
    f = np.clip(f, SEASONAL_CLIP_LO, SEASONAL_CLIP_HI)
    return f[day - d0, hour]


def _load_lab():
    spec = importlib.util.spec_from_file_location("v3_horizon_lab", V2 / "shared" / "research" / "v3_horizon_lab.py")
    lab = importlib.util.module_from_spec(spec)
    sys.modules["v3_horizon_lab"] = lab
    spec.loader.exec_module(lab)
    return lab


def patch(lab) -> None:
    orig_hfg, orig_xef, orig_parity = lab._hfg_event_vol_calendar_time, lab._xef_vol_calendar_time, lab._hfg_parity

    def _adjust(series: pd.Series, bars, hours: float, min_vol: float) -> pd.Series:
        dt_ns = pd.DatetimeIndex(bars["dt"]).as_unit("ns").asi8
        f = seasonal_factor(dt_ns, np.asarray(bars["close"], dtype="float64"), float(hours))
        v = series.to_numpy(dtype="float64") * f
        v = np.where(np.isfinite(v), np.maximum(v, float(min_vol)), v)
        return pd.Series(v, index=series.index, name=series.name)

    def hfg(bars, lookback_hours, span, min_vol):
        return _adjust(orig_hfg(bars, lookback_hours, span, min_vol), bars, lookback_hours, min_vol)

    def xef(bars, hours, ewm, min_vol):
        return _adjust(orig_xef(bars, hours, ewm, min_vol), bars, hours, min_vol)

    def parity(*a, **k):
        lab._hfg_event_vol_calendar_time = orig_hfg
        try:
            return orig_parity(*a, **k)
        finally:
            lab._hfg_event_vol_calendar_time = hfg

    lab._hfg_event_vol_calendar_time, lab._xef_vol_calendar_time, lab._hfg_parity = hfg, xef, parity
    print(f"MLAM Ch.5 T3: seasonal Event_Vol (days {SEASONAL_DAYS}, min days {SEASONAL_MIN_DAYS}, "
          f"clip [{SEASONAL_CLIP_LO}, {SEASONAL_CLIP_HI}]) on grid + execution finalists; parity unadjusted", flush=True)


def pin_schema(lab) -> None:
    """As the baseline (logs/ewm_span_test_20261031, grid_dimension_sensitivity/lab_with_schema_at_commit.py): feature
    eligibility uses the schema at SCHEMA_COMMIT (default 63f4a570), the one the persisted feature matrices were built
    with; the Ch.17-19 schema additions are only in the artifacts after the RECERT."""
    import json
    import os
    import subprocess
    commit = os.environ.get("SCHEMA_COMMIT", "63f4a570")

    def names(runtime) -> list[str]:
        rel = (Path(runtime.__file__).resolve().parent / "contracts" / "feature_schema.json").relative_to(V2.parent).as_posix()
        doc = json.loads(subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=V2.parent, capture_output=True,
                                        check=True).stdout)
        out = [str(f["feature_name"]) for f in doc["features"]]
        print(f"schema_feature_names from {commit}:{rel} -> {len(out)} features", flush=True)
        return out

    lab.schema_feature_names = names


if __name__ == "__main__":
    lab = _load_lab()
    pin_schema(lab)
    patch(lab)
    lab.main()
