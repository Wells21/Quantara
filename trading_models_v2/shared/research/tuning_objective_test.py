"""AFML Ch.9 data test (owner-approved 2026-10-24): which tuning score / search method gives the best OOS meta-model?

Nested CV on an edge's Step 02 data (its current selected features):
  outer   purged + embargoed 5-fold (label t1)                       -> honest evaluation
  inner   purged + embargoed 5-fold on each outer-train set           -> AFML Snippet 9.1 stage 1 (tune the estimator)
          estimator = Step 02's tree: DecisionTreeClassifier(entropy, max_features='sqrt', class_weight='balanced'),
          AFML Ch.4 return-attribution sample weights (x time decay c, Snippet 4.11)
          space   max_depth 1-8 (widened from 2-8), min_samples_leaf 5-200 log, min_samples_split 10-400 log,
                  min_weight_fraction_leaf 0-0.05, ccp_alpha 1e-6-1e-2 log (AFML s.9.3.1 log-uniform)
          scores  A  F1 at the threshold optimised on the inner-train fold (0.30-0.80 grid)   [current Step 02]
                  B  F1 at the classifier's own decision (p > 0.5), unweighted                [AFML p.130]
                  C  sample-weighted neg log loss                                              [AFML s.9.4, Snippet 7.4]
          search  random (Optuna RandomSampler = randomized search, AFML s.9.3) or TPE (current), 30 trials each
          time decay c in {1, 0.5, 0} for score C + random search (D029)
  stage 2 bag the tuned tree on the outer-train set (Ch.6: sequential bootstrap round(avgU x n) rows, per-tree seeds,
          1,000 trees; draws shared by every configuration)            -> AFML Snippet 9.1 stage 2
  metrics on the pooled outer-test predictions: AFML-weighted neg log loss, AUC, Spearman(prob, executed_r);
          plus the tuned max_depth per outer fold.

Run from trading_models_v2:
    python ./shared/research/tuning_objective_test.py --edge XAUUSD/rangestructure_long --workers 2
Writes assets/<A>/edges/<edge>/artifacts/feature_importance_audit/tuning_objective_test.json (+ trial ledger record by caller).
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

N_OUTER, N_INNER, N_TRIALS, N_TREES, SEED = 5, 5, 30, 1000, 42
THRESHOLD_GRID = np.round(np.arange(0.30, 0.81, 0.01), 2)
CONFIGS = [  # (name, score, sampler, c)
    ("A_f1_tuned_threshold|tpe", "A", "tpe", 1.0),
    ("A_f1_tuned_threshold|random", "A", "random", 1.0),
    ("B_f1_at_0.5|tpe", "B", "tpe", 1.0),
    ("B_f1_at_0.5|random", "B", "random", 1.0),
    ("C_weighted_neg_log_loss|tpe", "C", "tpe", 1.0),
    ("C_weighted_neg_log_loss|random", "C", "random", 1.0),
    ("C_weighted_neg_log_loss|random|c=0.5", "C", "random", 0.5),
    ("C_weighted_neg_log_loss|random|c=0", "C", "random", 0.0),
]
BASE_TREE = {"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced"}


def _decay(t_index: np.ndarray, uniq: np.ndarray, c: float) -> np.ndarray:
    from shared.models.afml_sample_weights import get_time_decay

    if c == 1.0:
        return np.ones(len(uniq))
    s = pd.Series(np.asarray(uniq, dtype="float64"), index=pd.DatetimeIndex(t_index))
    return get_time_decay(s, c).to_numpy(dtype="float64")


def _wnll(y, p, w) -> float:
    p = np.clip(p, 1e-15, 1 - 1e-15)
    return float(-np.average(-(y * np.log(p) + (1 - y) * np.log(1 - p)), weights=w))


def _pos(model, x) -> np.ndarray:
    pr = model.predict_proba(x)
    cls = list(model.classes_)
    return pr[:, cls.index(1)] if 1 in cls else np.zeros(len(x))


def _tune(x, y, w, inner, score: str, sampler: str) -> tuple[dict, float]:
    import optuna
    from sklearn.metrics import f1_score
    from sklearn.tree import DecisionTreeClassifier

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial):
        params = {
            "max_depth": trial.suggest_int("max_depth", 1, 8),
            "min_samples_leaf": trial.suggest_int("min_samples_leaf", 5, 200, log=True),
            "min_samples_split": trial.suggest_int("min_samples_split", 10, 400, log=True),
            "min_weight_fraction_leaf": trial.suggest_float("min_weight_fraction_leaf", 0.0, 0.05),
            "ccp_alpha": trial.suggest_float("ccp_alpha", 1e-6, 1e-2, log=True),
        }
        vals = []
        for tr, te in inner:
            m = DecisionTreeClassifier(**BASE_TREE, random_state=SEED, **params).fit(x[tr], y[tr], sample_weight=w[tr])
            pte = _pos(m, x[te])
            if score == "A":
                ptr = _pos(m, x[tr])
                best = max(THRESHOLD_GRID, key=lambda t: f1_score(y[tr], (ptr >= t).astype(int), zero_division=0))
                vals.append(f1_score(y[te], (pte >= best).astype(int), zero_division=0))
            elif score == "B":
                vals.append(f1_score(y[te], (pte > 0.5).astype(int), zero_division=0))
            else:
                vals.append(_wnll(y[te], pte, w[te]))
        return float(np.mean(vals))

    smp = optuna.samplers.TPESampler(seed=SEED) if sampler == "tpe" else optuna.samplers.RandomSampler(seed=SEED)
    study = optuna.create_study(direction="maximize", sampler=smp)
    study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=False)
    return dict(study.best_params), float(study.best_value)


def _outer_job(a: dict) -> dict:
    from sklearn.tree import DecisionTreeClassifier

    from shared.features import clustered_feature_importance as cfi

    train, test = a["train"], a["test"]
    x, y, w_ret = a["x"], a["y"], a["w"]
    inner = cfi.purged_kfold_splits(a["t0"][train], a["t1"][train], N_INNER, a["embargo"])
    draws, seeds = cfi._draws(a["start"][train], a["end"][train], a["n_bars"], N_TREES, a["seed"])
    draws = [train[d] for d in draws]
    out = {}
    for name, score, sampler, c in CONFIGS:
        decay = _decay(a["t0"][train], a["uniq"][train], c)
        w_train = np.zeros(len(y))
        w_train[train] = w_ret[train] * decay
        w_train[train] /= w_train[train].mean()
        params, inner_best = _tune(x[train], y[train], w_train[train], inner, score, sampler)
        trees = [DecisionTreeClassifier(**BASE_TREE, random_state=s, **params).fit(x[d], y[d], sample_weight=w_train[d])
                 for d, s in zip(draws, seeds)]
        p = np.zeros(len(test))
        for t in trees:
            p += _pos(t, x[test])
        out[name] = {"prob": p / len(trees), "params": params, "inner_best": inner_best}
    return out


def run(edge_id: str, workers: int) -> dict:
    import importlib.util

    from scipy.stats import spearmanr
    from sklearn.metrics import roc_auc_score

    from shared.features import clustered_feature_importance as cfi

    t_start = time.time()
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
    spec = importlib.util.spec_from_file_location("s02", root / "research/02_train_meta_model_optuna.py")
    m = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
        feats = m.load_selected_features()
        df = m.load_dataset(feats)
        w = np.asarray(m._afml_return_weights(df), dtype="float64")
        uniq = np.asarray(m.compute_uniqueness(df), dtype="float64")
        start, end, n_bars = m._event_bar_positions(df)
    sys.argv = argv
    w = w / w.mean()
    y = df[m.TARGET_COL].astype(int).to_numpy()
    r_exec = pd.to_numeric(df["executed_r"], errors="coerce").to_numpy()
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = m._label_end_times(df).values.astype("datetime64[ns]")
    embargo = int(m.embargo_size_for(len(df)))
    outer = cfi.purged_kfold_splits(t0, t1, N_OUTER, embargo)
    base = dict(x=df[feats].to_numpy(dtype="float64"), y=y, w=w, uniq=uniq, t0=t0, t1=t1, start=start, end=end,
                n_bars=n_bars, embargo=embargo)
    jobs = [dict(base, train=tr, test=te, seed=SEED + k) for k, (tr, te) in enumerate(outer)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_outer_job, jobs))

    report = {"edge_id": edge_id, "rows": int(len(df)), "features": len(feats), "outer_folds": N_OUTER,
              "inner_folds": N_INNER, "trials": N_TRIALS, "trees": N_TREES, "results": {}}
    for name, *_ in CONFIGS:
        p = np.full(len(y), np.nan)
        fold_nll = []
        for (tr, te), r in zip(outer, res):
            p[te] = r[name]["prob"]
            fold_nll.append(_wnll(y[te], r[name]["prob"], w[te]))
        mask = ~np.isnan(p)
        ok = mask & np.isfinite(r_exec)
        report["results"][name] = {
            "oos_weighted_neg_log_loss": round(_wnll(y[mask], p[mask], w[mask]), 5),
            "oos_auc": round(float(roc_auc_score(y[mask], p[mask])), 4),
            "spearman_prob_vs_executed_r": round(float(spearmanr(p[ok], r_exec[ok])[0]), 4),
            "fold_weighted_neg_log_loss": [round(v, 5) for v in fold_nll],
            "tuned_max_depth_per_fold": [r[name]["params"]["max_depth"] for r in res],
            "tuned_params_per_fold": [r[name]["params"] for r in res],
        }
    report["seconds"] = round(time.time() - t_start, 1)
    dest = root / "artifacts" / "feature_importance_audit" / "tuning_objective_test.json"
    dest.write_text(json.dumps(report, indent=1, default=float), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edge", required=True)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    r = run(a.edge, a.workers)
    for name, v in r["results"].items():
        print(f"{r['edge_id']} | {name:38s} | OOS wNLL {v['oos_weighted_neg_log_loss']:+.4f} | AUC {v['oos_auc']:.4f} "
              f"| rho(p, R) {v['spearman_prob_vs_executed_r']:+.4f} | depth {v['tuned_max_depth_per_fold']}", flush=True)
    print(f"{r['edge_id']} | {r['seconds']} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
