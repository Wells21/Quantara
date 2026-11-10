"""POST-HOC (written after T3's results): silhouette quality of each seed's Step 01 partition on the same VI observation
matrix -- overall q = E[S]/sqrt(V[S]) (Snippet 4.1's objective) and mean per-cluster t-stat (Snippet 4.2's)."""
import json, sys
from pathlib import Path
import numpy as np
from sklearn.metrics import silhouette_samples
V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from shared.features.clustered_feature_importance import observation_matrix  # noqa: E402
from shared.research.mlam_ch4_tests import step01_matrix  # noqa: E402
er = V2 / sys.argv[1]
name = f"{er.parent.parent.name}_{er.name}"
out = V2 / "logs/mlam_ch4_tests_20261102"
t3 = json.loads((out / f"t3_{name}.json").read_text(encoding="utf-8"))
x = step01_matrix(er)
assert list(x.columns) == t3["features"]
obs, _ = observation_matrix(x, "vi")
res = []
for r in t3["runs"]:
    lab = np.asarray(r["labels"])
    s = silhouette_samples(obs.to_numpy(), lab)
    ts = [s[lab == k].mean() / s[lab == k].std() if s[lab == k].std() > 0 else np.nan for k in np.unique(lab)]
    res.append({"seed": r["seed"], "n_clusters": r["n_clusters"], "q_overall": float(s.mean() / s.std()),
                "mean_cluster_t": float(np.nanmean(ts))})
    print(name, res[-1], flush=True)
(out / f"t3_quality_posthoc_{name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
