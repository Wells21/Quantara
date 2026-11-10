"""Gate C speed changes (2026-11-09) must not change a single replayed number:
1. the fast tick-timestamp parse equals the general ISO-8601 parser (whole-second and fractional stamps mixed);
2. the parallel chunked replay (_xef_chunk) gives exactly the rows of the serial loop, in the same order."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "shared" / "research"))

import v3_horizon_lab as lab  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402


def test_fast_ts_parse_matches_general_parser():
    col = pd.Series(["2014-02-28 06:56:36+00:00", "2014-02-28 06:56:36.848000+00:00", "2025-06-01 23:59:59.999000+00:00"])
    want = to_datetime_iso(col, utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    np.testing.assert_array_equal(lab._xef_parse_tick_ts(col), want)


def test_fast_ts_parse_falls_back_on_other_offsets():
    col = pd.Series(["2014-02-28 07:56:36+01:00", "2014-02-28 06:56:37+00:00"])
    want = to_datetime_iso(col, utc=True).to_numpy(dtype="datetime64[ns]").astype("int64")
    np.testing.assert_array_equal(lab._xef_parse_tick_ts(col), want)


def _synthetic_catalog(tmp_path: Path) -> list[dict]:
    rng = np.random.default_rng(7)
    cat = []
    start = pd.Timestamp("2024-01-01", tz="UTC")
    for w in range(3):
        t0 = start + pd.Timedelta(days=7 * w)
        ts = t0 + pd.to_timedelta(np.sort(rng.integers(0, 7 * 86400 * 1000, 20000)), unit="ms")
        mid = 2000 + np.cumsum(rng.normal(0, 0.05, len(ts)))
        f = tmp_path / f"x_ticks_{t0.date()}_to_{(t0 + pd.Timedelta(days=7)).date()}.csv"
        stamps = [x.isoformat(sep=" ") for x in ts]
        pd.DataFrame({"timestamp": stamps, "bidPrice": mid - 0.1, "askPrice": mid + 0.1}).to_csv(f, index=False)
        cat.append({"path": f, "filename": f.name, "start_date": t0.normalize(), "end_date": (t0 + pd.Timedelta(days=7)).normalize()})
    return cat


def test_parallel_chunks_equal_serial(tmp_path):
    cat = _synthetic_catalog(tmp_path)
    times = list(pd.date_range("2024-01-01 06:00", "2024-01-18 18:00", freq="7h", tz="UTC"))
    cfg = {"A": 1.0, "B": 24.0}
    recs_by_t = {}
    for t in times:
        recs_by_t[t] = {k: {"side": 1, "signal_mid": 2000.0, "event_vol": 0.001,
                            "deadline_ns": int((t + pd.Timedelta(hours=h)).value),
                            "sl_dist": 2.0, "tp_dist": 3.0} for k, h in cfg.items()}
    base = lab._xef_loader()
    serial = {k: [] for k in cfg}
    for t in times:
        for k, v in lab._xef_event(t, recs_by_t[t], cat, base, 5700, 0.0, None, 300.0).items():
            v["config_label"] = k
            serial[k].append(v)
    par = {k: [] for k in cfg}
    bounds = np.linspace(0, len(times), 4).astype(int)
    for w in range(3):
        part = lab._xef_chunk({"events": [(t, recs_by_t[t]) for t in times[bounds[w]:bounds[w + 1]]], "catalog": cat,
                               "mode": "raw", "lvl": None, "cost_model": None, "labels": list(cfg), "delay_ms": 5700,
                               "extra_bps": 0.0, "gbps": None, "guard_win": 300.0, "sfx": "", "cache_size": 4})
        for k, v in part.items():
            par[k].extend(v)
    for k in cfg:
        a, b = pd.DataFrame(serial[k]), pd.DataFrame(par[k])
        pd.testing.assert_frame_equal(a, b)
        assert (a.status == "resolved").sum() > 0
