"""T29 card diagnostic: quarter-clustered t of the edge-minus-C1 difference (read before the D090 verdict).
Each season (winter Nov-Apr labelled by the year it ends; summer May-Oct) is one unit: unit mean R per set, then a
Welch t of edge-season means vs C1-season means. usage: python season_clustered_t.py [ASSET ...]"""
import json, sys
from pathlib import Path
import numpy as np
V2 = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(V2)); sys.path.insert(0, str(V2 / "shared/research/gate0"))
import gate0_tool as g

def season(ts):
    return ts.dt.year.astype(str) + "Q" + ts.dt.quarter.astype(str)

out = {}
for a in (sys.argv[1:] or g.ASSETS):
    sets, _ = g.t29_sets(a, g.THESES["T29"]["cell"])
    out[a] = {}
    for side, d in sets.items():
        m = [d[k].groupby(season(d[k].Datetime)).realized_r.mean() for k in ("edge", "C1")]
        e, c = m[0].to_numpy(), m[1].to_numpy()
        diff = e.mean() - c.mean()
        se = np.sqrt(e.var(ddof=1) / len(e) + c.var(ddof=1) / len(c))
        r22 = [x[x.index.str[:4].astype(int) >= 2022] for x in m]
        out[a][side] = {"n_seasons": [len(e), len(c)], "diff": round(diff, 4), "season_t": round(diff / se, 2),
                        "diff_2022_on": round(r22[0].mean() - r22[1].mean(), 4)}
        print(a, side, out[a][side], flush=True)
Path(__file__).with_name("quarter_clustered_t.json").write_text(json.dumps(out, indent=2))
