"""Score label variants A (Dukascopy-cost), B (mid path), C (Moneta-cost) on ONE yardstick: Moneta net R (spread+swap+
commission, 5700 ms) per trade. Paired month-block bootstrap for the differences. usage: evaluate.py LABELEXP_DIR"""
import json, sys
from pathlib import Path
import numpy as np, pandas as pd
from scipy.stats import mannwhitneyu, spearmanr
D = Path(sys.argv[1]); rng = np.random.default_rng(7); NB = 2000
QS = (0.5, 0.4, 0.3, 0.2, 0.1)
def auc(p, y):
    y = np.asarray(y).astype(bool); return mannwhitneyu(p[y], p[~y], alternative="two-sided").statistic / (y.sum() * (~y).sum())
def metrics(p, r, yc, yown):
    o = np.argsort(-p); out = {"auc_vs_moneta_label": auc(p, yc), "auc_vs_own_label": auc(p, yown),
                              "spearman_vs_moneta_R": spearmanr(p, r).statistic}
    for q in QS:
        out[f"top{int(q*100)}_meanR"] = float(r[o[:max(1, int(len(r) * q))]].mean())
    out["top10to50_avg_meanR"] = float(np.mean([out[f"top{int(q*100)}_meanR"] for q in QS]))
    return out
res = {}; pooled = []
for ed in sorted((D / "exp").iterdir()):
    asset, edge = ed.name.split("_", 1)
    vs = {v: pd.read_csv(ed / f"{v}_oof.csv", usecols=["Datetime", "side", "raw_probability"]) for v in "ABC" if (ed / f"{v}_oof.csv").exists()}
    if len(vs) < 3:
        continue
    c = pd.read_csv(D / "repriced" / "moneta" / f"{asset}__{edge}.csv"); c = c[c.execution_status.eq("resolved")]
    b = pd.read_csv(D / "repriced" / "midpath" / f"{asset}__{edge}.csv"); b = b[b.execution_status.eq("resolved")]
    a = pd.read_csv(D / "backup" / ed.name / "resolution.csv", usecols=["Datetime", "side", "quote_executable_meta_label"]).dropna(subset=["quote_executable_meta_label"])
    k = lambda x: pd.to_datetime(x["Datetime"], utc=True, format="mixed").astype("int64").astype(str) + "|" + x["side"].astype(int).astype(str)
    base = pd.DataFrame({"k": k(c), "R": pd.to_numeric(c.executed_r).to_numpy(), "t": pd.to_datetime(c["Datetime"], utc=True, format="mixed").to_numpy()})
    base = base.merge(pd.DataFrame({"k": k(b), "yB": (pd.to_numeric(b.executed_r) > 0).astype(int).to_numpy()}), on="k")
    base = base.merge(pd.DataFrame({"k": k(a), "yA": a.quote_executable_meta_label.astype(int).to_numpy()}), on="k")
    for v, df in vs.items():
        base = base.merge(pd.DataFrame({"k": k(df), f"p{v}": df.raw_probability.to_numpy()}), on="k")
    base["yC"] = (base.R > 0).astype(int); base = base.dropna()
    own = {"A": "yA", "B": "yB", "C": "yC"}
    m = {v: metrics(base[f"p{v}"].to_numpy(), base.R.to_numpy(), base.yC.to_numpy(), base[own[v]].to_numpy()) for v in "ABC"}
    months = pd.DatetimeIndex(base.t).tz_localize(None).to_period("M").astype(str).to_numpy(); um = np.unique(months)
    idx = {mm: np.where(months == mm)[0] for mm in um}
    boot = {f"{x}-{y}": {"auc_vs_moneta_label": [], "top10to50_avg_meanR": []} for x, y in (("B", "A"), ("C", "A"), ("B", "C"))}
    for _ in range(NB):
        ii = np.concatenate([idx[mm] for mm in rng.choice(um, len(um))]); s = base.iloc[ii]
        mm_ = {v: metrics(s[f"p{v}"].to_numpy(), s.R.to_numpy(), s.yC.to_numpy(), s[own[v]].to_numpy()) for v in "ABC"}
        for key in boot:
            x, y = key.split("-")
            for met in boot[key]:
                boot[key][met].append(mm_[x][met] - mm_[y][met])
    diff = {key: {met: {"point": float(m[key[0]][met] - m[key[2]][met]), "ci95": [float(np.percentile(v_, 2.5)), float(np.percentile(v_, 97.5))],
                        "p_gt0": float(np.mean(np.asarray(v_) > 0))} for met, v_ in d_.items()} for key, d_ in boot.items()}
    flips = {"A_vs_C": float((base.yA != base.yC).mean()), "B_vs_C": float((base.yB != base.yC).mean()), "A_vs_B": float((base.yA != base.yB).mean())}
    res[ed.name] = {"n": int(len(base)), "baseline_meanR_all_trades": float(base.R.mean()), "label_pos_rate": {x: float(base[own[x]].mean()) for x in "ABC"},
                    "label_disagreement": flips, "metrics": m, "paired_diff": diff}
    pooled.append((ed.name, m))
    print(f"\n=== {ed.name}  n={len(base)}  all-trades Moneta meanR={base.R.mean():+.4f}  flips A/C={flips['A_vs_C']:.3f} B/C={flips['B_vs_C']:.3f}")
    for v in "ABC":
        print(f"  {v}: AUC(moneta lbl)={m[v]['auc_vs_moneta_label']:.4f} AUC(own)={m[v]['auc_vs_own_label']:.4f} rho={m[v]['spearman_vs_moneta_R']:+.4f} "
              + " ".join(f"top{int(q*100)}={m[v][f'top{int(q*100)}_meanR']:+.4f}" for q in QS) + f" | avg10-50={m[v]['top10to50_avg_meanR']:+.4f}")
    for key, d_ in diff.items():
        print(f"  {key}: dAUC={d_['auc_vs_moneta_label']['point']:+.4f} CI{np.round(d_['auc_vs_moneta_label']['ci95'],4).tolist()} P>0={d_['auc_vs_moneta_label']['p_gt0']:.2f} | "
              f"dTopR={d_['top10to50_avg_meanR']['point']:+.4f} CI{np.round(d_['top10to50_avg_meanR']['ci95'],4).tolist()} P>0={d_['top10to50_avg_meanR']['p_gt0']:.2f}")
if pooled:
    print("\n=== POOLED (mean over edges)")
    for v in "ABC":
        print(f"  {v}: AUC(moneta lbl)={np.mean([m[v]['auc_vs_moneta_label'] for _, m in pooled]):.4f} rho={np.mean([m[v]['spearman_vs_moneta_R'] for _, m in pooled]):+.4f} "
              f"avg10-50 meanR={np.mean([m[v]['top10to50_avg_meanR'] for _, m in pooled]):+.4f}")
    for x, y in (("B", "A"), ("C", "A"), ("B", "C")):
        w = sum(m[x]["top10to50_avg_meanR"] > m[y]["top10to50_avg_meanR"] for _, m in pooled)
        wa = sum(m[x]["auc_vs_moneta_label"] > m[y]["auc_vs_moneta_label"] for _, m in pooled)
        print(f"  {x} beats {y}: top-R on {w}/{len(pooled)} edges, AUC on {wa}/{len(pooled)} edges")
(D / "evaluation.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
