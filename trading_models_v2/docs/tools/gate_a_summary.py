"""Gate A / B summary of the latest v3_hybrid_fast_grid CSV per edge root.
usage: python docs/tools/gate_a_summary.py assets/<A>/edges/<stem> ...
Gate A: best gross_mean_R >= 0.05; Gate B: cells with net_mean_R > 0 (go to Gate C)."""
import sys
from pathlib import Path
import pandas as pd
print(f"{'edge':40s} {'rows':>6s} {'bestgross':>9s} {'net>0':>6s} {'med_net':>8s} {'max_net':>8s}  best cell  verdict")
for root in sys.argv[1:]:
    fs = sorted((Path(root) / "artifacts" / "v3_horizon_lab").glob("v3_hybrid_fast_grid_*.csv"))
    if not fs:
        print(root, "no grid"); continue
    g = pd.read_csv(fs[-1])
    b = g.loc[g.net_mean_R.idxmax()]
    lab = f"H{b.event_vol_lookback:g}_E{int(b.event_vol_ewm_span)}_V{b.vertical_horizon:g}_PT{b.pt:g}_SL{b.sl:g}"
    npos = int((g.net_mean_R > 0).sum())
    v = "KILL_A" if g.gross_mean_R.max() < 0.05 else ("KILL_B" if npos == 0 else "-> Gate C")
    print(f"{g.edge_id.iloc[0]:40s} {int(g.rows.median()):6d} {g.gross_mean_R.max():+9.3f} {npos:6d} {g.net_mean_R.median():+8.3f} {g.net_mean_R.max():+8.3f}  {lab}  {v}")
