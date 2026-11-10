"""Book-exact replication of MLAM s.6.3 and s.6.6 (pp.77-90) on its own synthetic data.

Exactly as printed (scikit-learn renamed BaggingClassifier's base_estimator to estimator; nothing else changed):
  * Snippet 6.1  getTestData(40, 5, 30, 10000, sigmaStd=.1)
  * Snippet 6.6  clusterKMeansBase(X.corr(), maxNumClusters=10, n_init=10)          -> MLAM Fig 6.4: 6 clusters
  * Snippets 6.2/6.7  DecisionTreeClassifier(criterion='entropy', max_features=1, class_weight='balanced',
                      min_weight_fraction_leaf=0) in BaggingClassifier(n_estimators=1000, max_features=1., max_samples=1.,
                      oob_score=False); featImpMDI / featImpMDI_Clustered                -> Figs 6.2 / 6.5
  * Snippets 6.3/6.8  featImpMDA / featImpMDA_Clustered with KFold(n_splits=10), log loss, independent shuffles
                                                                                     -> Figs 6.3 / 6.6
Validates shared/features/clustered_feature_importance.py's ONC and importance formulas against the book's own setup.

Run from trading_models_v2:  python ./shared/research/mlam_ch6_synthetic_replication.py
Writes docs/mldp/feature_importance/mlam_ch6_synthetic_replication.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import BaggingClassifier
from sklearn.metrics import log_loss
from sklearn.model_selection import KFold
from sklearn.tree import DecisionTreeClassifier

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.features import clustered_feature_importance as cfi  # noqa: E402


def _clf() -> BaggingClassifier:
    tree = DecisionTreeClassifier(criterion="entropy", max_features=1, class_weight="balanced", min_weight_fraction_leaf=0)
    return BaggingClassifier(estimator=tree, n_estimators=1000, max_features=1.0, max_samples=1.0, oob_score=False)


def feat_imp_mdi(fit, feat_names, clstrs=None) -> pd.DataFrame:
    """MLAM Snippets 6.2 / 6.4."""
    df0 = pd.DataFrame({i: t.feature_importances_ for i, t in enumerate(fit.estimators_)}).T
    df0.columns = feat_names
    df0 = df0.replace(0, np.nan)
    if clstrs is None:
        imp = pd.concat({"mean": df0.mean(), "std": df0.std() * df0.shape[0] ** -0.5}, axis=1)
    else:
        out = {}
        for i, j in clstrs.items():
            df1 = df0[j].sum(axis=1)
            out[f"C_{i}"] = {"mean": df1.mean(), "std": df1.std() * df1.shape[0] ** -0.5}
        imp = pd.DataFrame(out).T
    return imp / imp["mean"].sum()


def feat_imp_mda(x: pd.DataFrame, y: pd.Series, groups: dict, n_splits: int = 10) -> pd.DataFrame:
    """MLAM Snippets 6.3 / 6.5."""
    scr0, scr1 = pd.Series(dtype=float), pd.DataFrame(columns=list(groups))
    for i, (train, test) in enumerate(KFold(n_splits=n_splits).split(X=x)):
        x0, y0, x1, y1 = x.iloc[train, :], y.iloc[train], x.iloc[test, :], y.iloc[test]
        fit = _clf().fit(X=x0, y=y0)
        scr0.loc[i] = -log_loss(y1, fit.predict_proba(x1), labels=fit.classes_)
        for j, members in groups.items():
            x1_ = x1.copy(deep=True)
            for k in members:
                x1_[k] = np.random.permutation(x1_[k].values)
            scr1.loc[i, j] = -log_loss(y1, fit.predict_proba(x1_), labels=fit.classes_)
    imp = (-1 * scr1.astype(float)).add(scr0, axis=0)
    imp = imp / (-1 * scr1.astype(float))
    return pd.concat({"mean": imp.mean(), "std": imp.std() * imp.shape[0] ** -0.5}, axis=1)


def main() -> int:
    t = time.time()
    np.random.seed(0)
    x, y = cfi.get_test_data(40, 5, 30, 10000, sigma_std=0.1)
    obs, _ = cfi.observation_matrix(x, "corr_signed", denoise=False, reduce=False)
    clstrs, silh = cfi.onc_base(obs, max_k=10, n_init=10, seed=0)
    fit = _clf().fit(x, y)
    out = {
        "onc_clusters": {f"C_{i}": sorted(m) for i, m in clstrs.items()},
        "mdi": feat_imp_mdi(fit, list(x.columns)).round(5).to_dict(orient="index"),
        "clustered_mdi": feat_imp_mdi(fit, list(x.columns), clstrs).round(5).to_dict(orient="index"),
        "mda": feat_imp_mda(x, y, {c: [c] for c in x.columns}).round(5).to_dict(orient="index"),
        "clustered_mda": feat_imp_mda(x, y, {f"C_{i}": m for i, m in clstrs.items()}).round(5).to_dict(orient="index"),
    }
    noise = [k for k, m in out["onc_clusters"].items() if all(f.startswith("N_") for f in m)]
    out["checks"] = {
        "six_clusters (MLAM Fig 6.4)": len(clstrs) == 6,
        "noise_cluster": noise,
        "noise_features_ranked_last_by_mda (Fig 6.3)": sorted(out["mda"], key=lambda f: out["mda"][f]["mean"])[:5],
        "seconds": round(time.time() - t, 1),
    }
    dest = V2_ROOT / "docs" / "mldp" / "feature_importance" / "mlam_ch6_synthetic_replication.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"clusters": out["onc_clusters"], "clustered_mdi": out["clustered_mdi"],
                      "clustered_mda": out["clustered_mda"], "checks": out["checks"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
