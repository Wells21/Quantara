"""AFML Ch.9 walkthrough patch (owner decisions 2026-10-24) for every edge's Step 02 (hyper-parameter tuning).

  1. Tuning CV = AFML Snippet 9.1's purged k-fold (contiguous folds, purge on label_t1, embargo max(100, 1% of rows)),
     5 folds, instead of CPCV(5, 2). CPCV stays for Ch.12 backtest paths.
  2. Score = F1 at the classifier's own decision (p > 0.5), AFML Snippet 9.1/9.3 scoring='f1' for meta-labeling
     (p.130, s.14.8 pp.206-207); the train-fold-optimised 0.30-0.80 threshold is removed.
  3. Search = randomized search (Optuna RandomSampler, seeded; AFML s.9.3 / Snippet 9.3) instead of TPE; 30 trials.
  4. max_depth search range 1-8 (was 2-8; depth 2 was the bound on every edge).
Time decay stays c = 1 (D029 closed on data). Idempotent; every anchor must occur exactly once.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch9_step02.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "TUNING_N_SPLITS = 5"

PAIRS = [
    ('''OPTUNA_SEARCH_SPACE = {
    "max_depth": (2, 8),''',
     '''# AFML Ch.9 walkthrough 2026-10-24: tuning CV = Snippet 9.1 purged k-fold; score = F1 at the classifier's own decision
# (Snippet 9.1/9.3 scoring='f1' for meta-labeling, p.130); randomized search (s.9.3); max_depth range widened to 1.
TUNING_N_SPLITS = 5
SCORING_DECISION_THRESHOLD = 0.5
OPTUNA_SEARCH_SPACE = {
    "max_depth": (1, 8),'''),
    ('''    if CombinatorialPurgedCV is None:
        raise MetaModelTrainingError(
            "skfolio.model_selection.CombinatorialPurgedCV is required. "
            f"Original import error: {SKFOLIO_IMPORT_ERROR}"
        )
    class_weight_mode = params.get("class_weight_mode", "balanced")
    cv = CombinatorialPurgedCV(
        n_folds=N_FOLDS,
        n_test_folds=N_TEST_FOLDS,
        purged_size=PURGED_SIZE,
        embargo_size=EMBARGO_SIZE,
    )
    f1_scores = []
    fold_rows = []
    label = f"Trial {trial_number}" if trial_number is not None else "Trial"

    for split_num, (train_idx, test_idx) in enumerate(cv.split(x_data), start=1):
        train_idx = flatten_cv_indices(train_idx)
        test_idx = flatten_cv_indices(test_idx)
        raw_train_rows = len(train_idx)''',
     '''    class_weight_mode = params.get("class_weight_mode", "balanced")
    f1_scores = []
    fold_rows = []
    label = f"Trial {trial_number}" if trial_number is not None else "Trial"

    # AFML Snippet 9.1 (p.130): inner_cv = PurgedKFold -- contiguous test folds; purge on the label t1 and embargo below.
    n_rows = len(df)
    for split_num, test_idx in enumerate(np.array_split(np.arange(n_rows), TUNING_N_SPLITS), start=1):
        train_idx = np.setdiff1d(np.arange(n_rows), test_idx)
        raw_train_rows = len(train_idx)'''),
    ('''        train_proba = get_positive_class_proba(model, x_train)
        best_thr, train_f1 = find_best_f1_threshold(y_train, train_proba)
        test_proba = get_positive_class_proba(model, x_test)
        metrics = score_at_threshold(y_test, test_proba, best_thr)''',
     '''        # AFML Snippet 9.1/9.3: scoring='f1' at the classifier's own decision (predict = p > 0.5), no tuned threshold.
        train_proba = get_positive_class_proba(model, x_train)
        best_thr = SCORING_DECISION_THRESHOLD
        train_f1 = float(f1_score(np.asarray(y_train).astype(int), (train_proba > best_thr).astype(int), zero_division=0))
        test_proba = get_positive_class_proba(model, x_test)
        test_pred = (test_proba > best_thr).astype(int)
        metrics = {
            "f1": float(f1_score(y_test, test_pred, zero_division=0)),
            "precision": float(precision_score(y_test, test_pred, zero_division=0)),
            "recall": float(recall_score(y_test, test_pred, zero_division=0)),
        }'''),
    ("        sampler=optuna.samplers.TPESampler(seed=RANDOM_STATE),",
     "        sampler=optuna.samplers.RandomSampler(seed=RANDOM_STATE),  # AFML s.9.3 randomized search (Ch.9 2026-10-24)"),
    ('''    print(f"CPCV combinations expected: C({N_FOLDS}, {N_TEST_FOLDS}) = 10 folds per trial", flush=True)''',
     '''    print(f"Tuning CV: purged + embargoed {TUNING_N_SPLITS}-fold (AFML Snippet 9.1); score F1 at p > 0.5; random search", flush=True)'''),
    ('''            "threshold_selection": "best F1 threshold selected on train fold only, applied to test fold",
            "threshold_grid": F1_THRESHOLD_GRID.tolist(),''',
     '''            "threshold_selection": "none: F1 at the classifier's own decision p > 0.5 (AFML Snippet 9.1 scoring='f1', p.130)",
            "search": "randomized (Optuna RandomSampler, seeded; AFML s.9.3)",'''),
    ('''            "mode": "manual_t1_overlap_purge_after_cpcv_split_generation",
            "best_trial_fold_rows": best_fold_rows,''',
     '''            "mode": "purged_kfold_label_t1_with_embargo (AFML Snippet 9.1); CPCV fields above are not used for tuning",
            "tuning_n_splits": TUNING_N_SPLITS,
            "best_trial_fold_rows": best_fold_rows,'''),
    ('''"class_weight_mode": ["balanced"], "estimator": "DecisionTreeClassifier (entropy, max_features=sqrt)"},''',
     '''"class_weight_mode": ["balanced"], "estimator": "DecisionTreeClassifier (entropy, max_features=sqrt)",
                             "sampler": "random (AFML s.9.3)", "cv": f"purged {TUNING_N_SPLITS}-fold", "score": "f1 at p > 0.5"},'''),
]


def patch(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    for old, new in PAIRS:
        n = text.count(old)
        if n != 1:
            raise RuntimeError(f"anchor found {n} times: {old[:70]!r}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return "patched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", nargs="*")
    args = ap.parse_args(argv)
    roots = ([V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges] if args.edges
             else sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir()))
    failed = 0
    for root in roots:
        path = root / "research" / "02_train_meta_model_optuna.py"
        try:
            print(f"{patch(path):16s} {path.relative_to(V2)}")
        except (RuntimeError, FileNotFoundError) as exc:
            print(f"FAILED           {path.relative_to(V2)}: {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
