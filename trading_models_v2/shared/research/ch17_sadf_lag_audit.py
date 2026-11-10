"""Ch.17 audit: which lag does the research SADF_ct_50 at each model row correspond to, vs live's fixed lag 9?"""
import json, sys, time
from pathlib import Path
import numpy as np, pandas as pd

V2 = Path(r"C:\Users\Administrator\Desktop\Trading Models\trading_models_v2")
sys.path.insert(0, str(V2))
from shared.features.afml_ch17_structural_breaks import get_bsadf

edge = sys.argv[1]  # e.g. HK/drawdownhazardcontinuation_short
asset, name = edge.split("/")
d = V2 / "assets" / asset / "edges" / name / "artifacts" / "research_dataset"
pref = f"{asset.lower()}_{name}"
t0 = time.time()
fm = pd.read_csv(d / f"{pref}_feature_matrix.csv", usecols=["Datetime", "Close", "SADF_ct_50"], float_precision="round_trip")
mr = pd.read_csv(d / f"{pref}_model_rows.csv", usecols=["Datetime", "SADF_ct_50"], float_precision="round_trip")
pos = pd.Series(np.arange(len(fm)), index=fm["Datetime"].astype(str))
idx = pos.reindex(mr["Datetime"].astype(str)).to_numpy()
ok = ~np.isnan(idx)
idx = idx[ok].astype(int)
research = mr["SADF_ct_50"].to_numpy(float)[ok]
logc = np.log(fm["Close"].to_numpy(float) + 1e-12)


def sadf_at(end):  # window of 50 log closes ending at bar `end` (inclusive)
    if end - 49 < 0:
        return np.nan
    return get_bsadf(pd.Series(logc[end - 49:end + 1]), min_sl=10, constant="ct", lags=1)


# matrix value == model-row value?
fm_vals = fm["SADF_ct_50"].to_numpy(float)[idx]
same_as_matrix = float(np.nanmax(np.abs(fm_vals - research)))
# live rule: lag 9
live = np.array([sadf_at(i - 9) for i in idx])
# matched lag on a sample
rng = np.random.default_rng(0)
samp = rng.choice(len(idx), size=min(600, len(idx)), replace=False)
lags = []
for s in samp:
    i, r = idx[s], research[s]
    if not np.isfinite(r):
        lags.append(-1); continue
    m = [k for k in range(10) if abs(sadf_at(i - k) - r) < 1e-9]
    lags.append(m[0] if m else -2)
lags = np.array(lags)
fin = np.isfinite(live) & np.isfinite(research)
diff = live[fin] - research[fin]
out = {
    "edge": edge, "matrix_bars": int(len(fm)), "model_rows": int(len(mr)), "rows_matched_to_matrix": int(ok.sum()),
    "model_row_value_equals_matrix_max_abs": same_as_matrix,
    "matched_lag_counts_sample": {int(k): int((lags == k).sum()) for k in np.unique(lags)},
    "rows_research_equals_live_share": float(np.mean(np.abs(diff) < 1e-9)),
    "corr_research_vs_live": float(np.corrcoef(live[fin], research[fin])[0, 1]),
    "research_std": float(np.std(research[fin])), "mean_abs_diff": float(np.mean(np.abs(diff))),
    "seconds": round(time.time() - t0, 1),
}
print(json.dumps(out))
