"""AFML Ch.7 CV leakage audit for one or more edges (diagnostic, not a gate).

Runs the book's own leakage checks on an edge's research dataset with the edge's Step 02 machinery (features, AFML Ch.4
weights, Ch.6 sequential-bootstrap bagging with the Step 02 best tree parameters):
  * Exercise 7.2 (pp.110-111): 10-fold CV unshuffled vs SHUFFLED, no purge -> shuffling inflates scores when leakage exists
  * Exercise 7.3 (p.111): 10-fold purged (label t1) + embargo (Step 01-04 rule, max(100, 1% of rows))
  * p.107 k-sweep: purged + embargoed CV at k = 5, 10, 20, 40; performance must not keep rising as k grows
Scores: AUC and sample-weighted negative log loss (Snippet 7.4's metric) of the pooled out-of-fold probabilities.

Verdict: LEAK_SUSPECT if the purged AUC rises at every step of the k-sweep by > 0.005 in total; otherwise OK.
GATE (D080, owner 2026-11-04): LEAK_SUSPECT is a formal stop before Gate D -- the script exits 1 if any edge is
LEAK_SUSPECT (exit 0 = every edge OK); the edge does not proceed until the leak is found and fixed (new version).
shuffle_inflation_auc (shuffled - purged) is reported to show what leakage would look like on this edge.

Run from trading_models_v2 (venv312) at each certification (runbook 6A Step 12E), after the research dataset export:
    python ./shared/research/cv_leakage_audit.py --edges XAUUSD/rangestructure_long
Writes assets/<A>/edges/<edge>/artifacts/cv_leakage_audit/cv_leakage_audit.json.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.models.sequential_bootstrap_bagging import SequentialBootstrapBaggingClassifier  # noqa: E402

AUDIT_VERSION = "afml_ch7_cv_leakage_audit_v1"
K_SWEEP = (5, 10, 20, 40)
LEAK_RISE_AUC = 0.005


def _load_step02(edge_root: Path):
    spec = importlib.util.spec_from_file_location(f"step02_{edge_root.name}", edge_root / "research" / "02_train_meta_model_optuna.py")
    module = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    return module


def audit_edge(edge_id: str, n_trees: int = 50, seed: int = 0) -> dict[str, Any]:
    asset, name = edge_id.split("/", 1)
    root = V2_ROOT / "assets" / asset / "edges" / name
    m = _load_step02(root)
    with redirect_stdout(io.StringIO()):
        feats = m.load_selected_features()
        df = m.load_dataset(feats)
        n = len(df)
        weights = m.make_train_weights(df=df, y=df[m.TARGET_COL], train_idx=np.arange(n), class_weight_mode="balanced")
        s_pos, e_pos, n_bars = m._event_bar_positions(df)
    x = df[feats].to_numpy()
    y = df[m.TARGET_COL].astype(int).to_numpy()
    t0 = df.index.values.astype("datetime64[ns]")
    t1 = m._label_end_times(df).values.astype("datetime64[ns]")
    embargo = int(m.embargo_size_for(n))
    best = json.loads(next((root / "artifacts" / "model").glob("*_best_params.json")).read_text(encoding="utf-8"))
    tree_params = {k: v for k, v in best.items() if k != "class_weight_mode"} | {
        "criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced"}

    def purge(train: np.ndarray, test: np.ndarray) -> np.ndarray:
        ts, te = t0[test], t1[test]
        keep = np.array([not ((t0[i] <= te) & (t1[i] >= ts)).any() for i in train], dtype=bool)
        return train[keep]

    def apply_embargo(train: np.ndarray, test: np.ndarray) -> np.ndarray:
        test = np.sort(test)
        ends = [test[i - 1] for i in range(1, len(test)) if test[i] != test[i - 1] + 1] + [test[-1]]
        keep = np.ones(len(train), dtype=bool)
        for end in ends:
            keep &= (train <= end) | (train > end + embargo)
        return train[keep]

    def run(k: int, scheme: str) -> dict[str, float]:
        order = np.random.default_rng(seed).permutation(n) if scheme == "shuffled" else np.arange(n)
        prob = np.full(n, np.nan)
        for test in np.array_split(order, k):
            test = np.sort(test)
            train = np.setdiff1d(np.arange(n), test)
            if scheme == "purged":
                train = apply_embargo(purge(train, test), test)
            clf = SequentialBootstrapBaggingClassifier(tree_params, n_estimators=n_trees, random_state=seed)
            clf.fit(x[train], y[train], sample_weight=weights[train], event_start_pos=s_pos[train],
                    event_end_pos=e_pos[train], n_bars=n_bars)
            prob[test] = clf.predict_proba(x[test])[:, 1]
        return {"auc": round(float(roc_auc_score(y, prob)), 4),
                "neg_log_loss_weighted": round(float(-log_loss(y, prob, sample_weight=weights)), 4)}

    plain = run(10, "plain")
    shuffled = run(10, "shuffled")
    sweep = {str(k): run(k, "purged") for k in K_SWEEP}
    aucs = [sweep[str(k)]["auc"] for k in K_SWEEP]
    rising = all(b > a for a, b in zip(aucs, aucs[1:])) and (aucs[-1] - aucs[0]) > LEAK_RISE_AUC
    out = {
        "edge_id": edge_id,
        "audit_version": AUDIT_VERSION,
        "written_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": n, "embargo_observations": embargo, "purge_t1": "label_t1 (fallback gross_t1)",
        "model": f"Ch.6 sequential-bootstrap bagging, {n_trees} trees, Step 02 best tree params",
        "kfold10_unshuffled_no_purge": plain,
        "kfold10_shuffled_no_purge": shuffled,
        "purged_embargoed_k_sweep": sweep,
        "shuffle_inflation_auc": round(shuffled["auc"] - sweep["10"]["auc"], 4),
        "k_sweep_auc_rises_monotonically": bool(rising),
        "verdict": "LEAK_SUSPECT" if rising else "OK",
    }
    dest = root / "artifacts" / "cv_leakage_audit" / "cv_leakage_audit.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edges", nargs="+", required=True, help="ASSET/family_side ids")
    ap.add_argument("--trees", type=int, default=50)
    args = ap.parse_args(argv)
    suspect = []
    for edge_id in args.edges:
        r = audit_edge(edge_id, n_trees=args.trees)
        if r["verdict"] != "OK":
            suspect.append(edge_id)
        sweep = " / ".join(f"{r['purged_embargoed_k_sweep'][str(k)]['auc']:.3f}" for k in K_SWEEP)
        print(f"{edge_id}: verdict {r['verdict']} | AUC 10-fold plain {r['kfold10_unshuffled_no_purge']['auc']:.3f}, "
              f"shuffled {r['kfold10_shuffled_no_purge']['auc']:.3f} | purged k=5/10/20/40 {sweep} | "
              f"shuffle inflation {r['shuffle_inflation_auc']:+.3f}", flush=True)
    if suspect:
        print(f"STOP (D080): LEAK_SUSPECT on {suspect} -- do not run Gate D", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
