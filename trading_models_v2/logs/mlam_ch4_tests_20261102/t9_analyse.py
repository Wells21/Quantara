"""T9 stage-1 analysis (PREREGISTRATION T9): best-of-S block winners vs the 60-seed winner."""
import json, sys
from pathlib import Path
import numpy as np
V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from shared.research.mlam_ch4_tests import partition_vi  # noqa: E402
OUT = Path(__file__).resolve().parent
EDGES = ["GBPJPY_dailyrangecompletionfade_long", "HK_drawdownhazardcontinuation_short", "JPN_rangestructure_long",
         "XAUUSD_rangestructure_long", "JPN_volatilitycarry_long", "XAUUSD_volatilitycarry_long"]
res = {}
for e in EDGES:
    runs = {}
    for s in range(60):
        f = next(p for p in (OUT / d / f"{e}_n50_s{s}.json" for d in ("t5", "t9")) if p.exists())
        r = json.loads(f.read_text(encoding="utf-8"))
        runs[s] = (r["q_overall"], np.array(r["labels"]), r["cluster_sizes"])
    n = len(next(iter(runs.values()))[1])
    win = max(runs, key=lambda s: runs[s][0])
    nvi = lambda a, b: partition_vi(runs[a][1], runs[b][1]) / np.log(n)
    qs = sorted({round(v[0], 4) for v in runs.values()}, reverse=True)
    er = {"global_winner_seed": win, "global_max_q": runs[win][0], "winner_sizes": runs[win][2],
          "seeds_reaching_global_partition": [s for s in runs if nvi(s, win) <= 0.01], "distinct_q_top5": qs[:5]}
    for S in (5, 10, 20):
        blocks = [list(range(b, b + S)) for b in range(0, 60, S)]
        winners = [max(bl, key=lambda s: runs[s][0]) for bl in blocks]
        er[f"S{S}"] = {"block_winner_seeds": winners, "block_winner_q": [round(runs[w][0], 4) for w in winners],
                       "share_same_as_global": float(np.mean([nvi(w, win) <= 0.01 for w in winners]))}
    res[e] = er
    print(e, "max q", round(runs[win][0], 4), "reach", len(er["seeds_reaching_global_partition"]), "/60",
          {S: er[f"S{S}"]["share_same_as_global"] for S in (5, 10, 20)}, flush=True)
(OUT / "t9_stage1.json").write_text(json.dumps(res, indent=1, default=int), encoding="utf-8")
