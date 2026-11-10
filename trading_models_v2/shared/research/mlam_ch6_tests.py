"""MLAM Ch.6 tests (logs/mlam_ch6_tests_20261103/PREREGISTRATION.md).

t1 SIGMA OUT       Snippet 6.1 data through the production Step 01 path (Ex.3 / Ex.5) + book-exact unclustered MDI/MDA
t2 EDGE_ROOT OUT   Step 01 with the D077 partition fixed: log loss (baseline) vs probability-weighted accuracy (Ex.4)
t3 EDGE_ROOT OUT   per-cluster silhouettes + rule B on residual features (s.6.5.2) with the same partition
t4 EDGE_ROOT OUT   (POST HOC) Step 01 with the D077 partition fixed, importance-tree mwfl from CH6_TREE_MWFL (unset = D041)

usage (from trading_models_v2, venv312): python shared/research/mlam_ch6_tests.py t2 assets/XAUUSD/edges/rangestructure_long logs/mlam_ch6_tests_20261103
"""
from __future__ import annotations

import importlib.util
import json
import pickle
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

import shared.features.clustered_feature_importance as cfi  # noqa: E402
from shared.research.mlam_ch4_tests import partition_vi  # noqa: E402

ORIG_SELECT, ORIG_ONC, ORIG_SCORE = cfi.select_features_clustered, cfi.onc_best_of_restarts, cfi._neg_log_loss
# T1m (POST HOC): CH6_TREE_MWFL overrides Step 01's min_weight_fraction_leaf. Module top level, so Windows spawn workers
# (which re-import the main script) get it too.
import os  # noqa: E402
MWFL = os.environ.get("CH6_TREE_MWFL")
if MWFL is not None:
    cfi.TREE_PARAMS["min_weight_fraction_leaf"] = float(MWFL)


class _Captured(Exception):
    pass


def _pwa_minus_one(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> float:
    """Weighted PWA (MLAM p.83) minus 1, so Step 01's (scr0 - scr1)/(-scr1) equals (pwa0 - pwa1)/(1 - pwa1)."""
    pn = np.maximum(p, 1 - p)
    correct = ((p > 0.5).astype(int) == y).astype(float)
    den = np.sum(w * (pn - 0.5))
    return float(np.sum(w * correct * (pn - 0.5)) / den - 1.0) if den > 0 else 0.0


def step01_inputs(edge_root: Path) -> tuple[str, tuple, dict]:
    """Run the edge's own Step 01 up to its select_features_clustered call and capture the arguments."""
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    spec = importlib.util.spec_from_file_location(f"step01_{name}", edge_root / "research" / "01_feature_surgery_clustered_mda.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schema = mod.load_feature_schema_allowed_features()
    df = mod.load_model_rows()
    for c in schema:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    fixed, cand, _ = mod.choose_candidate_features(df, schema)
    df = df.dropna(subset=fixed + cand).copy()
    got: dict[str, Any] = {}

    def grab(*a, **k):
        got.update(a=a, k=k)
        raise _Captured

    cfi.select_features_clustered = grab
    try:
        mod.run_mlam_clustered_feature_importance(df, fixed, cand)
    except _Captured:
        pass
    finally:
        cfi.select_features_clustered = ORIG_SELECT
    return name, got["a"], got["k"]


def cached_onc(out: Path, name: str, x: pd.DataFrame):
    f = out / "cache" / f"onc_{name}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    obs, _ = cfi.observation_matrix(x, cfi.STEP01_METHOD)
    res = ORIG_ONC(obs, seed=0, workers=4)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(pickle.dumps(res))
    return res


def run_variant(args: tuple, kw: dict, onc_res, *, score=None, x_override: pd.DataFrame | None = None) -> dict:
    cfi.onc_best_of_restarts = lambda obs, **k: onc_res
    cfi._neg_log_loss = score or ORIG_SCORE
    try:
        a = (x_override if x_override is not None else args[0],) + tuple(args[1:])
        k = dict(kw, workers=1 if score is not None else kw.get("workers", 4))  # a patched score must run in-process
        return ORIG_SELECT(*a, **k)
    finally:
        cfi.onc_best_of_restarts, cfi._neg_log_loss = ORIG_ONC, ORIG_SCORE


def _brief(r: dict) -> dict:
    return {"n_selected": len(r["selected_features"]), "selected_features": r["selected_features"],
            "clusters": [{k: c[k] for k in ("cluster_id", "n_features", "mda_mean", "mda_se", "mda_t", "kept_rule_B")}
                         for c in r["clusters"]], "oos_auc": r["oos_auc"]}


def t2(edge_root: Path, out: Path) -> None:
    t = time.time()
    name, a, k = step01_inputs(edge_root)
    onc_res = cached_onc(out, name, a[0])
    base = run_variant(a, k, onc_res)
    pwa = run_variant(a, k, onc_res, score=_pwa_minus_one)
    res = {"edge": name, "logloss": _brief(base), "pwa": _brief(pwa),
           "same_kept_set": sorted(base["selected_features"]) == sorted(pwa["selected_features"]),
           "seconds": round(time.time() - t, 1)}
    print(name, "same kept set", res["same_kept_set"], len(base["selected_features"]), len(pwa["selected_features"]), flush=True)
    (out / "t2").mkdir(parents=True, exist_ok=True)
    (out / "t2" / f"{name}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def t3(edge_root: Path, out: Path) -> None:
    t = time.time()
    name, a, k = step01_inputs(edge_root)
    clusters, silh, _ = onc_res = cached_onc(out, name, a[0])
    sil = {f"C_{i}": {"n": len(m), "mean_silhouette": round(float(silh.loc[m].mean()), 4)} for i, m in clusters.items()}
    base = run_variant(a, k, onc_res)
    resid = run_variant(a, k, onc_res, x_override=cfi.residualize(a[0], clusters))
    res = {"edge": name, "silhouette_by_cluster": sil, "raw": _brief(base), "residual": _brief(resid),
           "same_kept_set": sorted(base["selected_features"]) == sorted(resid["selected_features"]),
           "seconds": round(time.time() - t, 1)}
    print(name, sil, "same kept set", res["same_kept_set"], flush=True)
    (out / "t3").mkdir(parents=True, exist_ok=True)
    (out / "t3" / f"{name}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def t3r(edge_root: Path, out: Path) -> None:
    """POST HOC (not pre-registered): T3 with the cluster order reversed, because p.85 residualises cluster k only on the
    clusters l < k, so the first cluster keeps all shared information."""
    t = time.time()
    name, a, k = step01_inputs(edge_root)
    clusters, silh, _ = onc_res = cached_onc(out, name, a[0])
    order = sorted(clusters, reverse=True)
    resid = run_variant(a, k, onc_res, x_override=cfi.residualize(a[0], {r: clusters[c] for r, c in enumerate(order)}))
    res = {"edge": name, "post_hoc": True, "residual_order": [f"C_{c}" for c in order], "residual_reversed": _brief(resid),
           "seconds": round(time.time() - t, 1)}
    print(name, "reversed", [(c["n_features"], round(c["mda_t"], 2), c["kept_rule_B"]) for c in res["residual_reversed"]["clusters"]], flush=True)
    (out / "t3r").mkdir(parents=True, exist_ok=True)
    (out / "t3r" / f"{name}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def t4(edge_root: Path, out: Path) -> None:
    """POST HOC (T1m follow-up): one arm of D041 (mwfl 0.05, env unset) vs Snippet 6.8 (CH6_TREE_MWFL=0) on a real edge."""
    t = time.time()
    name, a, k = step01_inputs(edge_root)
    r = run_variant(a, k, cached_onc(out, name, a[0]))
    res = {"edge": name, "post_hoc": True, "min_weight_fraction_leaf": cfi.TREE_PARAMS["min_weight_fraction_leaf"], **_brief(r),
           "oos_neg_log_loss_weighted": r["oos_neg_log_loss_weighted"], "seconds": round(time.time() - t, 1)}
    print(name, res["min_weight_fraction_leaf"], res["n_selected"], res["oos_auc"], [(c["n_features"], round(c["mda_t"], 2), c["kept_rule_B"]) for c in res["clusters"]], flush=True)
    (out / "t4").mkdir(parents=True, exist_ok=True)
    (out / "t4" / f"{name}_mwfl{res['min_weight_fraction_leaf']:g}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def t1(sigma: float, out: Path) -> None:
    from shared.research import mlam_ch6_synthetic_replication as rep
    t = time.time()
    np.random.seed(0)
    x, y = cfi.get_test_data(40, 5, 30, 10000, sigma_std=sigma)
    # truth: each R_ belongs to its source I_ (recover from the generator's own draw), N_ together
    rng = np.random.RandomState(0)
    src = rng.choice(range(5), size=30)
    truth = {**{f"I_{i}": i for i in range(5)}, **{f"R_{k}": int(j) for k, j in enumerate(src)}, **{f"N_{i}": 5 for i in range(5)}}
    n = len(x)
    ts = pd.date_range("2020-01-01", periods=n, freq="h").values.astype("datetime64[ns]")
    r = ORIG_SELECT(x, y.to_numpy(), np.ones(n), ts, ts, np.arange(n), np.arange(n), n, 0, workers=4)
    clusters = {c["cluster_id"]: c["features"] for c in r["clusters"]}
    lab = np.array([next(i for i, m in clusters.items() if f in m) for f in x.columns])
    tru = np.array([truth[f] for f in x.columns])
    kept = set(r["selected_features"])
    signal = {f for f in x.columns if not f.startswith("N_")}
    res = {"sigma": sigma, "n_clusters": len(clusters), "partition_vi_to_truth": partition_vi(lab, tru),
           "clusters": [{k: c[k] for k in ("n_features", "mda_mean", "mda_t", "kept_rule_B")} | {"features": sorted(c["features"])}
                        for c in r["clusters"]],
           "rule_B_keeps_all_signal": signal <= kept, "rule_B_drops_all_noise": not (kept - signal),
           "oos_auc": r["oos_auc"]}
    res["pass"] = bool(res["partition_vi_to_truth"] < 1e-9 and res["rule_B_keeps_all_signal"] and res["rule_B_drops_all_noise"])
    if sigma == 0 and MWFL is None:  # Ex.3 literally: book-exact unclustered MDI / MDA
        fit = rep._clf().fit(x, y)
        res["book_mdi"] = rep.feat_imp_mdi(fit, list(x.columns)).round(5).to_dict(orient="index")
        res["book_mda"] = rep.feat_imp_mda(x, y, {c: [c] for c in x.columns}).round(5).to_dict(orient="index")
    res["seconds"] = round(time.time() - t, 1)
    print({k: v for k, v in res.items() if k not in ("clusters", "book_mdi", "book_mda")}, flush=True)
    (out / (f"t1_sigma{sigma:g}" + (f"_mwfl{MWFL}" if MWFL is not None else "") + ".json")).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "t1":
        t1(float(sys.argv[2]), Path(sys.argv[3]))
    else:
        er = Path(sys.argv[2])
        {"t2": t2, "t3": t3, "t3r": t3r, "t4": t4}[cmd](er if er.is_absolute() else V2 / er, Path(sys.argv[3]))
