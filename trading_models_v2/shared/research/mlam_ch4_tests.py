"""MLAM Ch.4 tests (logs/mlam_ch4_tests_20261102/PREREGISTRATION.md).

t1 N REPS OUT [KS]         the book's Monte Carlo (s.4.5, Snippet 4.3) for one N, K = 3, 6, ... <= N/2 (or KS), both
                           ONC implementations
t2 EDGE_ROOT SEEDS OUT     trial E[K] / DSR / FWER at ONC seeds SEEDS (comma list), D076 trial correlation
t3 EDGE_ROOT OUT           Step 01 feature ONC (VI observation matrix) at seeds 0-9; partition VI vs seed 0 (MLAM s.3.10)
t4 EDGE_ROOT OUT           trial ONC on the PCA-reduced observation matrix (MLAM p.55), seed 0
t5cache EDGE_ROOT OUT      cache Step 01's VI observation matrix
t5 EDGE_ROOT NINIT SEED OUT  Step 01 feature ONC at n_init / seed (T5)
t6 EDGE_ROOT NINIT SEED OUT  PCA-reduced trial ONC at n_init / seed (T6)
t7 EDGE_ROOT NINIT SEED OUT  unreduced D076 trial ONC at n_init / seed (T7)
t8 EDGE_ROOT SEED OUT       Step 01 rule-B selection with the n_init-50 ONC partition at SEED, trees seed 0 (T8)
t9 EDGE_ROOT NINIT SEED OUT  as t5, written to t9/ (T9 restart sweep)

usage (from trading_models_v2, venv312): python shared/research/mlam_ch4_tests.py t1 20 20 logs/mlam_ch4_tests_20261102
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.linalg import block_diag
from sklearn.utils import check_random_state

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from shared.backtest_statistics.afml_ch14_dsr import cluster_trial_variance, dsr_report, trial_clustering_corr  # noqa: E402
from shared.features.clustered_feature_importance import observation_matrix, onc_top  # noqa: E402
from shared.models.denoise_onc import cluster_kmeans_top, cov_to_corr  # noqa: E402

N_INIT = 10


# -- Snippet 4.3 (p.61) verbatim -------------------------------------------------------------------------------------
def get_cov_sub(n_obs: int, n_cols: int, sigma: float, rng) -> np.ndarray:
    if n_cols == 1:
        return np.ones((1, 1))
    ar0 = rng.normal(size=(n_obs, 1))
    ar0 = np.repeat(ar0, n_cols, axis=1)
    ar0 += rng.normal(scale=sigma, size=ar0.shape)
    return np.cov(ar0, rowvar=False)


def get_rnd_block_cov(n_cols: int, n_blocks: int, min_block_size: int, sigma: float, rng) -> np.ndarray:
    parts = rng.choice(range(1, n_cols - (min_block_size - 1) * n_blocks), n_blocks - 1, replace=False)
    parts.sort()
    parts = np.append(parts, n_cols - (min_block_size - 1) * n_blocks)
    parts = np.append(parts[0], np.diff(parts)) - 1 + min_block_size
    cov = None
    for n in parts:
        c = get_cov_sub(int(max(n * (n + 1) / 2.0, 100)), int(n), sigma, rng)
        cov = c.copy() if cov is None else block_diag(cov, c)
    return cov


def random_block_corr(n_cols: int, n_blocks: int, rng, min_block_size: int = 1) -> tuple[pd.DataFrame, np.ndarray]:
    cov0 = get_rnd_block_cov(n_cols, n_blocks, min_block_size, 0.5, rng)
    cov1 = get_rnd_block_cov(n_cols, 1, min_block_size, 1.0, rng)
    return pd.DataFrame(cov_to_corr(cov0 + cov1)), cov0


def true_labels(cov0: np.ndarray) -> np.ndarray:
    """Block membership from the block-diagonal sigma .5 covariance (non-zero off-diagonal = same block)."""
    n = cov0.shape[0]
    lab = -np.ones(n, dtype=int)
    k = 0
    for i in range(n):
        if lab[i] < 0:
            lab[(cov0[i] != 0)] = k
            k += 1
    return lab


def partition_vi(a: np.ndarray, b: np.ndarray) -> float:
    """MLAM s.3.10 (pp.46-47): VI(P, P') = H(P|P') + H(P'|P) from the contingency of two labelings."""
    a, b = np.asarray(a), np.asarray(b)
    n = len(a)
    _, ia = np.unique(a, return_inverse=True)
    _, ib = np.unique(b, return_inverse=True)
    c = np.zeros((ia.max() + 1, ib.max() + 1))
    np.add.at(c, (ia, ib), 1)
    p = c / n
    pa, pb = p.sum(1), p.sum(0)
    nz = p > 0
    h_ab = -np.sum(p[nz] * np.log(p[nz]))
    h_a = -np.sum(pa[pa > 0] * np.log(pa[pa > 0]))
    h_b = -np.sum(pb[pb > 0] * np.log(pb[pb > 0]))
    return float(2 * h_ab - h_a - h_b)


def labels_of(clusters: dict, index) -> np.ndarray:
    lab = pd.Series(-1, index=index)
    for i, m in clusters.items():
        lab.loc[list(m)] = i
    return lab.to_numpy()


def t1(n: int, reps: int, out: Path, ks: list[int] | None = None) -> None:
    rows = []
    for k in ks or range(3, n // 2 + 1, 3):
        for r in range(reps):
            rng = check_random_state(10_000 * n + 100 * k + r)
            corr, cov0 = random_block_corr(n, k, rng, min_block_size=2)
            truth = true_labels(cov0)
            perm = rng.permutation(n)
            corr = corr.iloc[perm, perm]
            truth = truth[perm]
            t0 = time.time()
            _, cl_a, _ = cluster_kmeans_top(corr, n_init=N_INIT, random_state=r)
            ta = time.time() - t0
            cl_b, _ = onc_top(((1 - corr) / 2.0) ** 0.5, None, N_INIT, r)
            for impl, cl in (("cluster_kmeans_top", cl_a), ("onc_top", cl_b)):
                lab = labels_of(cl, corr.index)
                rows.append({"N": n, "K": k, "rep": r, "impl": impl, "E_K": len(cl), "ratio": len(cl) / k,
                             "exact_partition": bool(partition_vi(lab, truth) < 1e-12),
                             "vi_norm": partition_vi(lab, truth) / np.log(n)})
            print(n, k, r, len(cl_a), len(cl_b), round(ta, 1), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / (f"t1_N{n}.csv" if not ks else f"t1_N{n}_K{'-'.join(map(str, ks))}.csv"), index=False)


def t2(edge_root: Path, seeds: list[int], out: Path) -> None:
    from shared.research.mlam_ch2_tests import trial_matrix
    perf, selected, rec = trial_matrix(edge_root)
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    corr, diag = trial_clustering_corr(perf)
    res = []
    for s in seeds:
        t0 = time.time()
        _, clusters, _ = cluster_kmeans_top(corr, n_init=N_INIT, random_state=s)
        tv = cluster_trial_variance(perf, clusters)
        rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"])
        res.append({"seed": s, "E_K": tv["E_K"], "cluster_sizes": sorted((len(m) for m in clusters.values()), reverse=True),
                    "dsr": rep["dsr"], "dsr_pass_0p95": rep["dsr_pass_0p95"], "fwer_alpha_K": rep["fwer"]["alpha_K"],
                    "fwer_pass_0p05": rep["fwer"]["pass_0p05"], "seconds": round(time.time() - t0, 1),
                    "labels": labels_of(clusters, corr.index).tolist()})
        print(name, s, {k: v for k, v in res[-1].items() if k != "labels"}, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"t2_{name}_seeds{'-'.join(map(str, seeds))}.json").write_text(
        json.dumps({"edge": name, "trial_corr": diag, "runs": res}, indent=1, default=str), encoding="utf-8")


def step01_matrix(edge_root: Path) -> pd.DataFrame:
    path = edge_root / "research" / "01_feature_surgery_clustered_mda.py"
    spec = importlib.util.spec_from_file_location(f"step01_{edge_root.parent.parent.name}_{edge_root.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schema = mod.load_feature_schema_allowed_features()
    df = mod.load_model_rows()
    for c in schema:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    fixed, cand, _ = mod.choose_candidate_features(df, schema)
    df = df.dropna(subset=fixed + cand)
    return df[cand].astype("float64")


def t3(edge_root: Path, out: Path) -> None:
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    x = step01_matrix(edge_root)
    t0 = time.time()
    obs, diag = observation_matrix(x, "vi")
    print(name, "obs", obs.shape, diag, round(time.time() - t0, 1), flush=True)
    base = None
    res = []
    for s in range(10):
        cl, _ = onc_top(obs, max_k=None, n_init=N_INIT, seed=s)
        lab = labels_of(cl, obs.index)
        base = lab if base is None else base
        vi = partition_vi(lab, base)
        res.append({"seed": s, "n_clusters": len(cl), "cluster_sizes": sorted((len(m) for m in cl.values()), reverse=True),
                    "partition_vi_vs_seed0": vi, "partition_vi_norm": vi / np.log(len(lab)), "labels": lab.tolist()})
        print(name, s, len(cl), round(vi / np.log(len(lab)), 4), flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"t3_{name}.json").write_text(json.dumps({"edge": name, "rows": len(x), "n_features": int(x.shape[1]),
                                                     "features": list(x.columns), "observation_matrix": diag,
                                                     "runs": res}, indent=1, default=str), encoding="utf-8")


def t4(edge_root: Path, out: Path) -> None:
    from shared.features.clustered_feature_importance import _reduce_observations, observation_signal_dims
    from shared.research.mlam_ch2_tests import trial_matrix
    perf, selected, rec = trial_matrix(edge_root)
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    corr, diag = trial_clustering_corr(perf)
    x = ((1 - corr) / 2.0) ** 0.5
    dims = observation_signal_dims(x)
    xr = _reduce_observations(x, dims)
    clusters, _ = onc_top(xr, None, N_INIT, 0)
    tv = cluster_trial_variance(perf, clusters)
    rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"])
    res = {"edge": name, "reduced_dims": dims, "E_K": tv["E_K"],
           "cluster_sizes": sorted((len(m) for m in clusters.values()), reverse=True), "dsr": rep["dsr"],
           "dsr_pass_0p95": rep["dsr_pass_0p95"], "fwer_alpha_K": rep["fwer"]["alpha_K"],
           "fwer_pass_0p05": rep["fwer"]["pass_0p05"]}
    print(res, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"t4_{name}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def t5(edge_root: Path, n_init: int, seed: int, out: Path, sub: str = "t5") -> None:
    """Step 01 feature ONC at n_init / seed on the cached VI observation matrix (stage with t5cache)."""
    from sklearn.metrics import silhouette_samples
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    obs = pd.read_pickle(out / "cache" / f"obs_{name}.pkl")
    t0 = time.time()
    cl, _ = onc_top(obs, max_k=None, n_init=n_init, seed=seed)
    lab = labels_of(cl, obs.index)
    sil = silhouette_samples(obs.to_numpy(), lab)
    res = {"edge": name, "n_init": n_init, "seed": seed, "n_clusters": len(cl),
           "cluster_sizes": sorted((len(m) for m in cl.values()), reverse=True), "q_overall": float(sil.mean() / sil.std()),
           "seconds": round(time.time() - t0, 1), "labels": lab.tolist()}
    print({k: v for k, v in res.items() if k != "labels"}, flush=True)
    (out / sub).mkdir(parents=True, exist_ok=True)
    (out / sub / f"{name}_n{n_init}_s{seed}.json").write_text(json.dumps(res), encoding="utf-8")


def t5cache(edge_root: Path, out: Path) -> None:
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    obs, _ = observation_matrix(step01_matrix(edge_root), "vi")
    (out / "cache").mkdir(parents=True, exist_ok=True)
    obs.to_pickle(out / "cache" / f"obs_{name}.pkl")
    print(name, obs.shape, flush=True)


def t6(edge_root: Path, n_init: int, seed: int, out: Path) -> None:
    """T4 (PCA-reduced trial X) at n_init / seed."""
    from shared.features.clustered_feature_importance import _reduce_observations, observation_signal_dims
    from shared.research.mlam_ch2_tests import trial_matrix
    perf, selected, _ = trial_matrix(edge_root)
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    cache = out / "cache" / f"trialX_{name}.pkl"
    if cache.exists():
        xr = pd.read_pickle(cache)
    else:
        corr, _ = trial_clustering_corr(perf)
        x = ((1 - corr) / 2.0) ** 0.5
        xr = _reduce_observations(x, observation_signal_dims(x))
        cache.parent.mkdir(parents=True, exist_ok=True)
        xr.to_pickle(cache)
    t0 = time.time()
    clusters, _ = onc_top(xr, None, n_init, seed)
    tv = cluster_trial_variance(perf, clusters)
    rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"])
    res = {"edge": name, "n_init": n_init, "seed": seed, "E_K": tv["E_K"], "dsr": rep["dsr"],
           "dsr_pass_0p95": rep["dsr_pass_0p95"], "fwer_alpha_K": rep["fwer"]["alpha_K"],
           "fwer_pass_0p05": rep["fwer"]["pass_0p05"], "seconds": round(time.time() - t0, 1)}
    print(res, flush=True)
    (out / "t6").mkdir(parents=True, exist_ok=True)
    (out / "t6" / f"{name}_n{n_init}_s{seed}.json").write_text(json.dumps(res), encoding="utf-8")


def t7(edge_root: Path, n_init: int, seed: int, out: Path) -> None:
    """Unreduced D076 trial ONC at n_init / seed (correlation cached per edge as corr_<name>.pkl)."""
    from shared.research.mlam_ch2_tests import trial_matrix
    perf, selected, _ = trial_matrix(edge_root)
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    corr = pd.read_pickle(out / "cache" / f"corr_{name}.pkl")
    t0 = time.time()
    _, clusters, _ = cluster_kmeans_top(corr, n_init=n_init, random_state=seed)
    tv = cluster_trial_variance(perf, clusters)
    rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"])
    res = {"edge": name, "n_init": n_init, "seed": seed, "E_K": tv["E_K"],
           "cluster_sizes": sorted((len(m) for m in clusters.values()), reverse=True), "dsr": rep["dsr"],
           "dsr_pass_0p95": rep["dsr_pass_0p95"], "fwer_alpha_K": rep["fwer"]["alpha_K"],
           "fwer_pass_0p05": rep["fwer"]["pass_0p05"], "seconds": round(time.time() - t0, 1)}
    print(res, flush=True)
    (out / "t7").mkdir(parents=True, exist_ok=True)
    (out / "t7" / f"{name}_n{n_init}_s{seed}.json").write_text(json.dumps(res), encoding="utf-8")


def t8(edge_root: Path, onc_seed: int, out: Path) -> None:
    """Step 01 rule-B selection with the ONC partition from n_init STEP01_ONC_N_INIT at onc_seed; trees/MDA seed fixed at
    0, so only the feature partition varies between runs (PREREGISTRATION T8)."""
    import shared.features.clustered_feature_importance as cfi
    name = f"{edge_root.parent.parent.name}_{edge_root.name}"
    path = edge_root / "research" / "01_feature_surgery_clustered_mda.py"
    spec = importlib.util.spec_from_file_location(f"step01_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    schema = mod.load_feature_schema_allowed_features()
    df = mod.load_model_rows()
    for c in schema:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    fixed, cand, _ = mod.choose_candidate_features(df, schema)
    df = df.dropna(subset=fixed + cand).copy()
    orig = cfi.onc_top
    seen: dict[str, Any] = {}

    def patched(obs, max_k=None, n_init=10, seed=0):
        cl, silh = orig(obs, max_k, n_init, onc_seed)
        seen.update(n_init=n_init, labels=labels_of(cl, obs.index).tolist())
        return cl, silh

    cfi.onc_top = patched
    t0 = time.time()
    try:
        selected, _, cluster_df, _, _, summary = mod.run_mlam_clustered_feature_importance(df, fixed, cand)
        failed = None
    except mod.FeatureSurgeryError as exc:
        selected, cluster_df, summary, failed = list(fixed), pd.DataFrame(), {}, str(exc)
    finally:
        cfi.onc_top = orig
    t5f = next((f for f in (out / d / f"{name}_n{seen['n_init']}_s{onc_seed}.json" for d in ("t5", "t9")) if f.exists()),
               out / "t5" / "missing.json")
    same_as_t5 = json.loads(t5f.read_text(encoding="utf-8"))["labels"] == seen["labels"] if t5f.exists() else None
    res = {"edge": name, "onc_n_init": seen["n_init"], "onc_seed": onc_seed, "partition_matches_t5": same_as_t5,
           "n_clusters": int(len(set(seen["labels"]))), "selected_features": sorted(selected), "n_selected": len(selected),
           "step01_failed": failed, "oos_auc": summary.get("oos_auc"), "seconds": round(time.time() - t0, 1),
           "clusters": cluster_df.drop(columns=[c for c in ("iteration",) if c in cluster_df.columns]).to_dict("records")}
    print({k: v for k, v in res.items() if k not in ("clusters", "selected_features")}, flush=True)
    (out / "t8").mkdir(parents=True, exist_ok=True)
    (out / "t8" / f"{name}_s{onc_seed}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "t1":
        t1(int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]),
           [int(k) for k in sys.argv[5].split(",")] if len(sys.argv) > 5 else None)
    elif cmd == "t8":
        er = Path(sys.argv[2])
        t8(er if er.is_absolute() else V2 / er, int(sys.argv[3]), Path(sys.argv[4]))
    elif cmd == "t7":
        er = Path(sys.argv[2])
        t7(er if er.is_absolute() else V2 / er, int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]))
    elif cmd == "t9":
        er = Path(sys.argv[2])
        t5(er if er.is_absolute() else V2 / er, int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]), sub="t9")
    elif cmd in ("t5", "t6"):
        er = Path(sys.argv[2])
        (t5 if cmd == "t5" else t6)(er if er.is_absolute() else V2 / er, int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]))
    elif cmd == "t5cache":
        er = Path(sys.argv[2])
        t5cache(er if er.is_absolute() else V2 / er, Path(sys.argv[3]))
    elif cmd == "t4":
        er = Path(sys.argv[2])
        t4(er if er.is_absolute() else V2 / er, Path(sys.argv[3]))
    elif cmd in ("t2", "t3"):
        er = Path(sys.argv[2])
        er = er if er.is_absolute() else V2 / er
        if cmd == "t2":
            t2(er, [int(s) for s in sys.argv[3].split(",")], Path(sys.argv[4]))
        else:
            t3(er, Path(sys.argv[3]))
    else:
        raise SystemExit(f"unknown command {cmd}")
