"""Encoding variants (AFML s.18.5) on 100-bar messages; no labels used. Expanding-window Snippet 18.4 (verbatim)."""
import sys, json, glob, os, time
V2 = r"C:/Users/Administrator/Desktop/Trading Models/trading_models_v2"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd
from scipy.stats import spearmanr
import book_snippets_18_1_18_4 as B
edge = sys.argv[1]
fm = glob.glob(f"{V2}/assets/{edge}/artifacts/research_dataset/*_feature_matrix.csv")[0]
df = pd.read_csv(fm, usecols=["Close", "Close_FFD"])
ffd = df["Close_FFD"].to_numpy(); r = np.diff(np.log(df["Close"].to_numpy()), prepend=np.nan); dffd = np.diff(ffd, prepend=np.nan)
n = len(df); half = n // 2   # boundaries from the first half only (stand-in for a training set, s.18.5.2)
q_r = np.nanquantile(r[1:half], np.linspace(0, 1, 11)[1:-1]); q_f = np.nanquantile(ffd[:half], np.linspace(0, 1, 11)[1:-1])
sig = np.nanstd(r[1:half])
rng = np.random.default_rng(0); ts = np.sort(rng.choice(np.arange(1002, n), size=2000, replace=False))
V = {k: [] for k in ["binary_dffd", "binary_r", "quantile10_r", "quantile10_ffd_level", "sigma_r"]}
letters = {k: [] for k in V}
t0 = time.perf_counter()
for t in ts:
    w = slice(t - 99, t + 1)
    msgs = {
        "binary_dffd": "".join("1" if x > 0 else "0" for x in dffd[w] if x != 0),
        "binary_r": "".join("1" if x > 0 else "0" for x in r[w] if x != 0),
        "quantile10_r": "".join(map(str, np.searchsorted(q_r, r[w]))),
        "quantile10_ffd_level": "".join(map(str, np.searchsorted(q_f, ffd[w]))),
        # sigma encoding (s.18.5.3): codes from min of the window, step sigma; >9 codes mapped to chr to keep 1 char/code
        "sigma_r": "".join(chr(48 + int(c)) for c in np.floor((r[w] - r[w].min()) / sig)),
    }
    for k, m in msgs.items():
        m = m[len(m) % 2:]
        V[k].append(B.konto(m)["h"]); letters[k].append(len(set(m)))
el = time.perf_counter() - t0
out = {"edge": edge, "sec_per_bar_all5": el / len(ts)}
for k in V:
    a = np.array(V[k]); out[k] = {"mean": round(a.mean(), 3), "std": round(a.std(), 3), "p5": round(np.percentile(a, 5), 3),
        "p95": round(np.percentile(a, 95), 3), "distinct": int(np.unique(np.round(a, 10)).size),
        "letters_median": float(np.median(letters[k])), "share_windows_1_letter": float(np.mean(np.array(letters[k]) == 1)),
        "spearman_vs_binary_dffd": round(float(spearmanr(a, V["binary_dffd"])[0]), 3)}
print(json.dumps(out))
