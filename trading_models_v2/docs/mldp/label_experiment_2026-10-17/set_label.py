"""Write label variant V into the edge's resolution CSV (quote_executable_meta_label only; rows/order/eligibility unchanged).
usage: set_label.py ORIG_RES_CSV TARGET_RES_CSV V REPRICED_DIR ASSET EDGE    (V in A|B|C)"""
import shutil, sys
from pathlib import Path
import pandas as pd
orig, tgt, v, rdir, asset, edge = sys.argv[1:7]
if v == "A":
    shutil.copy2(orig, tgt); print("LABEL A restored"); sys.exit(0)
firm = {"B": "midpath", "C": "moneta"}[v]
d = pd.read_csv(orig, low_memory=False)
r = pd.read_csv(Path(rdir) / firm / f"{asset}__{edge}.csv")
r = r[r.execution_status.eq("resolved")]
key = lambda x: pd.to_datetime(x["Datetime"], utc=True, format="mixed").astype("int64").astype(str) + "|" + x["side"].astype(int).astype(str)
lab = dict(zip(key(r), (pd.to_numeric(r.executed_r) > 0).astype(int)))
k = key(d); new = k.map(lab)
old = d["quote_executable_meta_label"]; has = old.notna()          # unresolved/ineligible rows keep their original (NaN) label
n_match = int((new.notna() & has).sum())
d["quote_executable_meta_label"] = new.where(has & new.notna(), old).astype(old.dtype)
o, n = old[has].astype(int), d.loc[has, "quote_executable_meta_label"].astype(int)
d.to_csv(tgt, index=False)
print(f"LABEL {v} ({firm}) matched={n_match}/{int(has.sum())} labelled rows (of {len(d)}) pos_rate {o.mean():.3f}->{n.mean():.3f} flips={(o != n).mean():.3f}")
