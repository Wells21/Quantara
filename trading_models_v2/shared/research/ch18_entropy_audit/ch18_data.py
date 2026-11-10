"""AFML Ch.18 audit on Quantara research data: current Entropy_Kontoyiannis_100 vs book-correct Snippet 18.3/18.4."""
import sys, json, glob, os
V2 = r"C:/Users/Administrator/Desktop/Trading Models/trading_models_v2"
sys.path[:0] = [V2, os.path.dirname(V2), os.path.dirname(os.path.abspath(__file__))]
import numpy as np, pandas as pd
from scipy.stats import spearmanr
import book_snippets_18_1_18_4 as B
from shared.features import afml_ch18_entropy as Q
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
edge = sys.argv[1]
fm = glob.glob(f"{V2}/assets/{edge}/artifacts/research_dataset/*_feature_matrix.csv")[0]
df = pd.read_csv(fm, usecols=["Datetime", "Close", "Close_FFD", "Log_Return_1", "Entropy_Kontoyiannis_100"])
df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True)
gap = df["Datetime"].diff().dt.total_seconds().div(60)
ffd = df["Close_FFD"].to_numpy(); dif = np.diff(ffd, prepend=np.nan)
lr = df["Log_Return_1"].to_numpy()
res = {"edge": edge, "rows": len(df), "bar_minutes_mode": float(gap.mode().iloc[0]),
       "ffd_diff_zero_share": float(np.mean(dif[1:] == 0)),
       "abs_return_cv": float(np.nanstd(np.abs(lr)) / np.nanmean(np.abs(lr))),
       "abs_return_p90_over_p10": float(np.nanpercentile(np.abs(lr[lr != 0]), 90) / np.nanpercentile(np.abs(lr[lr != 0]), 10))}
rng = np.random.default_rng(0)
cand = np.arange(102, len(df)); cand = cand[np.isfinite(df["Entropy_Kontoyiannis_100"].to_numpy()[cand])]
ts = np.sort(rng.choice(cand, size=min(5000, cand.size), replace=False))
stored, port, book25, book_exp, lag0 = [], [], [], [], []
for t in ts:
    msg = Q.binary_sign_encode(pd.Series(dif[t - 100:t]))
    stored.append(df["Entropy_Kontoyiannis_100"].iat[t])
    w = max(4, len(msg) // 4)
    port.append(Q.kontoyiannis_entropy(msg, window=w)["h"])
    book25.append(B.konto(msg, w)["h"])
    m2 = msg[len(msg) % 2:]  # book p.269: drop first bit of odd-length message
    book_exp.append(B.konto(m2)["h"])
    msg0 = Q.binary_sign_encode(pd.Series(dif[t - 99:t + 1]))
    lag0.append(B.konto(msg0, max(4, len(msg0) // 4))["h"])
stored, port, book25, book_exp, lag0 = map(np.array, (stored, port, book25, book_exp, lag0))
res.update({
    "sample": int(ts.size),
    "stored_equals_port_recompute_max_abs": float(np.max(np.abs(stored - port))),
    "spearman_port_vs_book_fixed_window": float(spearmanr(port, book25)[0]),
    "spearman_port_vs_book_expanding": float(spearmanr(port, book_exp)[0]),
    "spearman_book_fixed_lag1_vs_lag0": float(spearmanr(book25, lag0)[0]),
    "distinct_values_port": int(np.unique(np.round(port, 12)).size),
    "distinct_values_book_fixed": int(np.unique(np.round(book25, 12)).size),
    "mean_port": float(port.mean()), "mean_book_fixed": float(book25.mean()), "mean_book_expanding": float(book_exp.mean()),
})
print(json.dumps(res))
