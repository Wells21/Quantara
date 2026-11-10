"""AFML Ch.6 walkthrough patch (owner decision 2026-10-24) for every edge's Steps 02/03 and the shared bagging module.

1. Per-tree seeds (AFML s.6.4 p.98): every bagged tree was built with the same random_state, so all trees drew the same
   random feature subsets (GBPJPY: all 100 trees split the root on one feature). Each tree now gets its own seed from
   the seeded ensemble generator, as sklearn's BaggingClassifier does; still reproducible.
2. Bootstrap sample size = average uniqueness x n (AFML s.6.3.3 p.97: "we should sample ... but not more"; Snippet 4.5
   sLength). avgU is computed on the training set's own events at fit time (max_samples="avgU").
3. N_ESTIMATORS_BAGGING = 1000 (AFML Snippet 6.2).
4. Step 02 logs its real Optuna search space (one OPTUNA_SEARCH_SPACE dict drives both the objective and the trial
   ledger); it used to log a stale XGBoost space (deviation D035).
Idempotent. Every anchor must occur exactly once or the file is left untouched and the run fails.

usage (from trading_models_v2):  python ./docs/tools/patch_afml_ch6_ensemble.py [--edges ASSET/edge ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
MARK = "AFML s.6.3.3 (p.97): with overlapping labels"

INIT_OLD = "    def __init__(self, tree_params: dict[str, Any], *, n_estimators: int, random_state: int | None = None) -> None:"
INIT_NEW = ("    def __init__(self, tree_params: dict[str, Any], *, n_estimators: int, random_state: int | None = None,\n"
            "                 max_samples: str = \"avgU\") -> None:")
INIT_STORE_OLD = "        self.random_state = random_state\n        self.estimators_: list[DecisionTreeClassifier] = []"
INIT_STORE_NEW = ("        self.random_state = random_state\n"
                  "        if max_samples not in (\"avgU\", \"n\"):\n"
                  "            raise SequentialBootstrapError(\"max_samples must be 'avgU' or 'n'.\")\n"
                  "        self.max_samples = max_samples\n"
                  "        self.estimators_: list[DecisionTreeClassifier] = []")

FIT_OLD = """        rng = np.random.default_rng(self.random_state)
        self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(event_start_pos, event_end_pos, n_bars, sample_length=n_rows, random_state=rng)
            tree = DecisionTreeClassifier(**self.tree_params)
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)
"""
FIT_NEW = """        # AFML s.6.3.3 (p.97): with overlapping labels each estimator samples about average-uniqueness x n observations,
        # "but not more" (Snippet 4.5 sLength); avgU is that of THIS training set (concurrency among its own events).
        start_arr = np.asarray(event_start_pos, dtype="int64")
        end_arr = np.asarray(event_end_pos, dtype="int64")
        diff = np.zeros(int(n_bars) + 1, dtype="float64")
        np.add.at(diff, start_arr, 1.0)
        np.add.at(diff, end_arr + 1, -1.0)
        concurrency = np.cumsum(diff[:-1])
        inv = np.where(concurrency > 0, 1.0 / np.maximum(concurrency, 1.0), 0.0)
        prefix = np.concatenate(([0.0], np.cumsum(inv)))
        self.avg_uniqueness_ = float(np.mean((prefix[end_arr + 1] - prefix[start_arr]) / (end_arr - start_arr + 1)))
        self.sample_length_ = (max(1, int(round(self.avg_uniqueness_ * n_rows))) if self.max_samples == "avgU" else n_rows)
        rng = np.random.default_rng(self.random_state)
        self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(event_start_pos, event_end_pos, n_bars, sample_length=self.sample_length_, random_state=rng)
            # AFML s.6.4 (p.98): every tree draws its own random feature subsets. One shared random_state made all trees
            # draw the same subsets (fixed 2026-10-24); per-tree seeds come from the seeded ensemble rng, as in sklearn.
            tree = DecisionTreeClassifier(**{**self.tree_params, "random_state": int(rng.integers(0, 2**31 - 1))})
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)
"""

SPACE_OLD = """        "max_depth": trial.suggest_int("max_depth", 2, 8),
        "min_samples_leaf": trial.suggest_int("min_samples_leaf", 5, 200, log=True),
        "min_samples_split": trial.suggest_int("min_samples_split", 10, 400, log=True),
        "min_weight_fraction_leaf": trial.suggest_float("min_weight_fraction_leaf", 0.0, 0.05),
        "ccp_alpha": trial.suggest_float("ccp_alpha", 1e-6, 1e-2, log=True),"""
SPACE_NEW = """        "max_depth": trial.suggest_int("max_depth", *OPTUNA_SEARCH_SPACE["max_depth"]),
        "min_samples_leaf": trial.suggest_int("min_samples_leaf", *OPTUNA_SEARCH_SPACE["min_samples_leaf"], log=True),
        "min_samples_split": trial.suggest_int("min_samples_split", *OPTUNA_SEARCH_SPACE["min_samples_split"], log=True),
        "min_weight_fraction_leaf": trial.suggest_float("min_weight_fraction_leaf", *OPTUNA_SEARCH_SPACE["min_weight_fraction_leaf"]),
        "ccp_alpha": trial.suggest_float("ccp_alpha", *OPTUNA_SEARCH_SPACE["ccp_alpha"], log=True),"""
LEDGER_OLD = """            "search_space": {
                "n_estimators": [250, 1000],
                "max_depth": [2, 6],
                "learning_rate": [0.01, 0.08],
                "min_child_weight": [5.0, 100.0],
                "subsample": [0.55, 0.95],
                "colsample_bytree": [0.55, 0.95],
                "gamma": [0.0, 8.0],
                "reg_alpha": [1e-4, 10.0],
                "reg_lambda": [1e-3, 30.0],
            },"""
LEDGER_NEW = """            # AFML Ch.6 walkthrough 2026-10-24: the real decision-tree space (was a stale XGBoost space, D035)
            "search_space": {**{k: list(v) for k, v in OPTUNA_SEARCH_SPACE.items()},
                             "class_weight_mode": ["balanced"], "estimator": "DecisionTreeClassifier (entropy, max_features=sqrt)"},"""
N_OLD = "N_ESTIMATORS_BAGGING = 100\n"
N_NEW = ("N_ESTIMATORS_BAGGING = 1000  # AFML Snippet 6.2 (owner decision 2026-10-24, Ch.6 walkthrough)\n"
         "# Optuna search space for the Snippet 6.2 base tree (one dict drives the objective AND the trial-ledger record).\n"
         "OPTUNA_SEARCH_SPACE = {\n"
         "    \"max_depth\": (2, 8),\n"
         "    \"min_samples_leaf\": (5, 200),\n"
         "    \"min_samples_split\": (10, 400),\n"
         "    \"min_weight_fraction_leaf\": (0.0, 0.05),\n"
         "    \"ccp_alpha\": (1e-6, 1e-2),\n"
         "}\n")
N_NEW_03 = "N_ESTIMATORS_BAGGING = 1000  # AFML Snippet 6.2 (owner decision 2026-10-24, Ch.6 walkthrough)\n"
MANIFEST_OLD = '        "model_n_estimators_bagging": int(N_ESTIMATORS_BAGGING),'
MANIFEST_NEW = ('        "model_n_estimators_bagging": int(N_ESTIMATORS_BAGGING),\n'
                '        "model_bootstrap_max_samples": final_model.max_samples,\n'
                '        "model_training_avg_uniqueness": float(final_model.avg_uniqueness_),\n'
                '        "model_bootstrap_sample_length": int(final_model.sample_length_),\n'
                '        "model_tree_seeds": "per-tree, drawn from the seeded ensemble rng",')


def _once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"anchor {label!r} found {n} times")
    return text.replace(old, new)


def patch_step(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    text = _once(text, INIT_OLD, INIT_NEW, "__init__ signature")
    text = _once(text, INIT_STORE_OLD, INIT_STORE_NEW, "__init__ body")
    text = _once(text, FIT_OLD, FIT_NEW, "fit loop")
    if path.name.startswith("02_"):
        text = _once(text, N_OLD, N_NEW, "N_ESTIMATORS_BAGGING")
        text = _once(text, SPACE_OLD, SPACE_NEW, "optuna suggest")
        text = _once(text, LEDGER_OLD, LEDGER_NEW, "ledger search_space")
        text = _once(text, MANIFEST_OLD, MANIFEST_NEW, "manifest")
    else:
        text = _once(text, N_OLD, N_NEW_03, "N_ESTIMATORS_BAGGING")
    path.write_text(text, encoding="utf-8")
    return "patched"


SHARED_INIT_OLD = """        n_estimators: int,
        random_state: int | None = None,
    ) -> None:
        if n_estimators <= 0:
            raise SequentialBootstrapBaggingError("n_estimators must be > 0.")
        self.tree_params = dict(tree_params)
        self.n_estimators = int(n_estimators)
        self.random_state = random_state
"""
SHARED_INIT_NEW = """        n_estimators: int,
        random_state: int | None = None,
        max_samples: str = "avgU",
    ) -> None:
        if n_estimators <= 0:
            raise SequentialBootstrapBaggingError("n_estimators must be > 0.")
        if max_samples not in ("avgU", "n"):
            raise SequentialBootstrapBaggingError("max_samples must be 'avgU' or 'n'.")
        self.tree_params = dict(tree_params)
        self.n_estimators = int(n_estimators)
        self.random_state = random_state
        self.max_samples = max_samples
"""
SHARED_FIT_OLD = """        rng = np.random.default_rng(self.random_state)
        self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(
                event_start_pos, event_end_pos, n_bars,
                sample_length=n_rows, random_state=rng,
            )
            tree = DecisionTreeClassifier(**self.tree_params)
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)
"""
SHARED_FIT_NEW = """        # AFML s.6.3.3 (p.97): with overlapping labels each estimator samples about average-uniqueness x n observations
        # ("but not more"; Snippet 4.5 sLength), avgU of this training set. AFML s.6.4 (p.98): per-tree seeds from the
        # seeded ensemble rng so trees draw different feature subsets (Ch.6 walkthrough 2026-10-24).
        avg_u = get_avg_uniqueness(event_start_pos, event_end_pos, n_bars, np.arange(n_rows))
        self.avg_uniqueness_ = float(np.mean(avg_u))
        self.sample_length_ = (max(1, int(round(self.avg_uniqueness_ * n_rows))) if self.max_samples == "avgU" else n_rows)
        rng = np.random.default_rng(self.random_state)
        self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(
                event_start_pos, event_end_pos, n_bars,
                sample_length=self.sample_length_, random_state=rng,
            )
            tree = DecisionTreeClassifier(**{**self.tree_params, "random_state": int(rng.integers(0, 2**31 - 1))})
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)
"""
SHARED_IMPORT_OLD = "from sklearn.tree import DecisionTreeClassifier\n\nfrom shared.models.sequential_bootstrap import sequential_bootstrap\n"
SHARED_IMPORT_NEW = ("from sklearn.tree import DecisionTreeClassifier\n\n"
                     "from shared.models.sequential_bootstrap import get_avg_uniqueness, sequential_bootstrap\n")


def patch_shared(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already patched"
    text = _once(text, SHARED_INIT_OLD, SHARED_INIT_NEW, "shared __init__")
    text = _once(text, SHARED_FIT_OLD, SHARED_FIT_NEW, "shared fit")
    text = _once(text, SHARED_IMPORT_OLD, SHARED_IMPORT_NEW, "shared import (module level)")
    path.write_text(text, encoding="utf-8")
    return "patched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edges", nargs="*")
    args = ap.parse_args(argv)
    failed = 0
    if not args.edges:
        try:
            print(f"{patch_shared(V2 / 'shared/models/sequential_bootstrap_bagging.py'):16s} shared/models/sequential_bootstrap_bagging.py")
        except RuntimeError as exc:
            print(f"FAILED   shared/models/sequential_bootstrap_bagging.py: {exc}")
            failed += 1
    roots = ([V2 / "assets" / e.split("/")[0] / "edges" / e.split("/")[1] for e in args.edges] if args.edges
             else sorted(p for p in (V2 / "assets").glob("*/edges/*") if (p / "research").is_dir()))
    for root in roots:
        for name in ("02_train_meta_model_optuna.py", "03_generate_oof_predictions.py"):
            path = root / "research" / name
            try:
                print(f"{patch_step(path):16s} {path.relative_to(V2)}")
            except (RuntimeError, FileNotFoundError) as exc:
                print(f"FAILED   {path.relative_to(V2)}: {exc}")
                failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
