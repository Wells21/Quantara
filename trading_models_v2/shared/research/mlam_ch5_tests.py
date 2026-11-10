"""MLAM Ch.5 tests (logs/mlam_ch5_tests_20261103/PREREGISTRATION.md).

t1 OUT     trend-scanning replication (Snippets 5.1-5.3, pp.67-72) at seeds 0-99
t2 OUT     label seasonality by entry hour (s.5.2 p.66) on every edge's research dataset

usage (from trading_models_v2, venv312): python shared/research/mlam_ch5_tests.py t1 logs/mlam_ch5_tests_20261103
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso
import statsmodels.api as sm1
from scipy.stats import chi2_contingency

V2 = Path(__file__).resolve().parents[2]


def t_val_lin_r(close: np.ndarray) -> float:
    """MLAM Snippet 5.1 (p.68): t-value of the slope of a linear time trend."""
    x = np.ones((close.shape[0], 2))
    x[:, 1] = np.arange(close.shape[0])
    ols = sm1.OLS(close, x).fit()
    return float(ols.tvalues[1])


def get_bins_from_trend(molecule, close: pd.Series, span) -> pd.DataFrame:
    """MLAM Snippet 5.2 (pp.68-69); xrange -> range, the book's t1 = farthest observation used ('prevent leakage')."""
    out = pd.DataFrame(index=molecule, columns=["t1", "tVal", "bin"], dtype="float64")
    hrzns = range(*span)
    for dt0 in molecule:
        df0 = pd.Series(dtype="float64")
        iloc0 = close.index.get_loc(dt0)
        if iloc0 + max(hrzns) > close.shape[0]:
            continue
        for hrzn in hrzns:
            dt1 = close.index[iloc0 + hrzn - 1]
            df1 = close.loc[dt0:dt1]
            df0.loc[dt1] = t_val_lin_r(df1.values)
        dt1 = df0.replace([-np.inf, np.inf, np.nan], 0).abs().idxmax()
        out.loc[dt0, ["t1", "tVal", "bin"]] = df0.index[-1], df0[dt1], np.sign(df0[dt1])
    return out.dropna(subset=["bin"])


def t1(out: Path) -> None:
    rows = []
    for seed in range(100):
        rng = np.random.default_rng(seed)
        df0 = pd.Series(rng.normal(0, 0.1, 100)).cumsum()
        grid = np.linspace(0, 10, df0.shape[0])
        df0 += np.sin(grid)
        df1 = get_bins_from_trend(df0.index, df0, [3, 10, 1])
        # true deterministic trend over each point's chosen window: sign of sin(end) - sin(start), window from |tVal| argmax
        agree = []
        for dt0, r in df1.iterrows():
            ts = {h: t_val_lin_r(df0.loc[dt0:dt0 + h - 1].values) for h in range(3, 10)}
            h = max(ts, key=lambda k: abs(ts[k]))
            agree.append(np.sign(np.sin(grid[dt0 + h - 1]) - np.sin(grid[dt0])) == r["bin"])
        runs = int((df1["bin"].diff().fillna(0) != 0).sum() + 1)
        rows.append({"seed": seed, "labelled": len(df1), "agreement": float(np.mean(agree)), "runs": runs,
                     "t1_after_t0": bool((df1["t1"] > df1.index).all()), "share_up": float((df1["bin"] > 0).mean())})
    res = pd.DataFrame(rows)
    summ = {"median_agreement": float(res["agreement"].median()), "p10_agreement": float(res["agreement"].quantile(0.1)),
            "median_runs": float(res["runs"].median()), "t1_after_t0_all": bool(res["t1_after_t0"].all()),
            "labelled_per_series": int(res["labelled"].iloc[0])}
    print(summ, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "t1_trend_scanning.json").write_text(json.dumps({"summary": summ, "runs": rows}, indent=1), encoding="utf-8")


def cramers_v(table: pd.DataFrame) -> tuple[float, float]:
    chi2, p, _, _ = chi2_contingency(table.to_numpy())
    n = table.to_numpy().sum()
    k = min(table.shape) - 1
    return float(p), float(np.sqrt(chi2 / (n * k))) if k > 0 else 0.0


def t2(out: Path) -> None:
    res = {}
    for f in sorted(V2.glob("assets/*/edges/*/artifacts/research_dataset/*_mldp_execution_resolution.csv")):
        edge = f"{f.parents[4].name}_{f.parents[2].name}"
        d = pd.read_csv(f, usecols=["Datetime", "barrier_hit", "label_r", "label_t1", "mldp_target_eligible"])
        d = d[d["mldp_target_eligible"].astype(str).str.lower() == "true"].copy()
        t0 = to_datetime_iso(d["Datetime"], utc=True)
        d["block"] = (t0.dt.hour // 4) * 4
        d["target"] = (d["label_r"] > 0).astype(int)
        d["hold_h"] = (to_datetime_iso(d["label_t1"], utc=True) - t0).dt.total_seconds() / 3600.0
        pa, va = cramers_v(pd.crosstab(d["block"], d["target"]))
        pb, vb = cramers_v(pd.crosstab(d["block"], d["barrier_hit"]))
        per = d.groupby("block").agg(rows=("target", "size"), pos_rate=("target", "mean"),
                                     vertical_share=("barrier_hit", lambda s: float((s == "vertical").mean())),
                                     pt_share=("barrier_hit", lambda s: float((s == "pt").mean())),
                                     median_hold_h=("hold_h", "median"))
        res[edge] = {"rows": int(len(d)), "target_vs_block": {"p": pa, "cramers_v": va},
                     "barrier_vs_block": {"p": pb, "cramers_v": vb},
                     "hour_dependent_by_rule": bool(pb < 0.01 and vb >= 0.10),
                     "per_block": per.round(4).reset_index().to_dict("records")}
        print(edge, len(d), "target p %.3g V %.3f | barrier p %.3g V %.3f" % (pa, va, pb, vb), flush=True)
        print(per.round(3).to_string(), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "t2_label_seasonality.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    {"t1": t1, "t2": t2}[sys.argv[1]](Path(sys.argv[2]))
