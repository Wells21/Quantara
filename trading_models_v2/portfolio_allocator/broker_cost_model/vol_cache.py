"""Per-minute trailing 1-day realized volatility (bps) of the Dukascopy mid -- the volatility-state input of the broker
spread TABLE model. Firm-independent (built from the accurate research price path). Causal: sqrt(sum of squared 1-min
mid log returns over the trailing 1440 minutes), min 300 observations.
Output: portfolio_allocator/outputs/dukascopy_minute_vol/minute_rv1d_<asset>.npz (int64 UTC-minute ns `t`, float `rv1d`)."""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd



def _tick_root(v2_root: Path, asset: str) -> Path:
    """Research tick folder from AssetLayout (curated per config/asset_data_policy.json; AFML Ch.2 walkthrough)."""
    import sys

    if str(v2_root) not in sys.path:
        sys.path.insert(0, str(v2_root))
    from shared.assets.asset_layout import get_asset_layout

    return get_asset_layout(asset, v2_root).tick_root

def rv1d_from_minute_mid(mid: pd.Series) -> pd.Series:
    mid = mid.asfreq("min"); r = np.log(mid).diff() * 1e4
    return np.sqrt((r ** 2).rolling(1440, min_periods=300).sum())


def build(v2_root: Path, asset: str, out_dir: Path) -> Path:
    mids = []
    for f in sorted(glob.glob(str(_tick_root(v2_root, asset) / "*.csv"))):
        d = pd.read_csv(f, usecols=[0, 1, 2]); d.columns = ["ts", "bid", "ask"]
        d["ts"] = pd.to_datetime(d.ts, utc=True, format="mixed"); d = d[(d.bid > 0) & (d.ask >= d.bid)]
        if len(d):
            mids.append(((d.bid + d.ask) / 2).groupby(d.ts.dt.floor("min")).last())
    m = pd.concat(mids); m = m[~m.index.duplicated(keep="last")].sort_index()
    rv = rv1d_from_minute_mid(m).dropna()
    out_dir.mkdir(parents=True, exist_ok=True); p = out_dir / f"minute_rv1d_{asset.lower()}.npz"
    np.savez_compressed(p, t=rv.index.as_unit("ns").asi8, rv1d=rv.to_numpy(dtype="float64"))
    return p
