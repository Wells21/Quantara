"""Era-scaled broker spread (STANDING RULE 2026-11-09): v3_horizon_lab._xef_broker_loader multiplies the broker
half-spread by the monthly factor of the tick's UTC month; months before the first key get 1; later gaps inherit the
previous month's factor; without factors the loader is unchanged."""
import sys
from pathlib import Path

import numpy as np

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "shared" / "research"))

import v3_horizon_lab as lab  # noqa: E402


def _base(ts, mid):
    def base(_path):
        return ts, mid - 0.5, mid + 0.5
    return base


def _cm(factors=None):
    cm = {"model": "FIXED_POINTS", "fixed_points": 1.0, "ref_price": 100.0, "unit": "pts"}
    if factors is not None:
        cm["_era_scale_factors"] = factors
    return cm


def _ts(*iso):
    return np.array(list(iso), dtype="datetime64[ns]").astype("int64")


def test_no_factors_unchanged():
    ts = _ts("2010-01-15T10:00", "2026-01-15T10:00")
    mid = np.array([100.0, 200.0])
    _, bid, ask = lab._xef_broker_loader(_base(ts, mid), _cm())("x")
    np.testing.assert_allclose(ask - bid, mid * (1.0 / 100.0))


def test_monthly_factor_applied_and_inherited():
    ts = _ts("2004-12-31T23:59", "2005-01-10T00:00", "2005-02-10T00:00", "2005-03-10T00:00", "2026-01-01T00:00")
    mid = np.full(5, 100.0)
    f = {"2005-01": 4.0, "2005-02": 2.5, "2026-01": 1.0}
    _, bid, ask = lab._xef_broker_loader(_base(ts, mid), _cm(f))("y")
    # before the first key -> 1; Jan 4.0; Feb 2.5; Mar (gap) inherits Feb 2.5; 2026-01 -> 1.0
    np.testing.assert_allclose(ask - bid, np.array([1.0, 4.0, 2.5, 2.5, 1.0]))
    np.testing.assert_allclose((ask + bid) / 2.0, mid)   # mid unchanged
