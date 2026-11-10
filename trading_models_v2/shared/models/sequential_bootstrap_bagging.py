"""Canonical AFML Ch.6 (Ensemble Methods, p.93-102) + Ch.4 SS4.5 (p.61-66)
bagging classifier for Quantara meta-models.

This is the exact configuration from AFML Snippet 6.2's clf1 ("a third way
of setting up an RF"), with sklearn's own bootstrap resampling replaced by
the sequential bootstrap in shared/models/sequential_bootstrap.py (AFML's
own "third and better [best]" solution to bagging under overlapping
financial labels, SS4.5, p.64):

    clf1 = DecisionTreeClassifier(criterion='entropy', max_features='auto',
                                   class_weight='balanced')
    clf1 = BaggingClassifier(base_estimator=clf1, n_estimators=1000, max_samples=avgU)

`max_features='auto'` was removed from scikit-learn (>=1.3); 'sqrt' is its
exact prior meaning for a classifier and is used here, not a deviation.
sklearn's BaggingClassifier has no hook to substitute an externally computed
resample (it only supports its own uniform bootstrap via max_samples), so
this module implements the ensemble loop directly: for each of n_estimators
rounds, draw a sample via sequential_bootstrap, fit one DecisionTreeClassifier
on it, and at inference time average the fitted trees' predict_proba output
(AFML p.94: "the bagging classifier may derive a mean of the probabilities").

Per AFML Ch.9 Snippet 9.1's clfHyperFit: hyperparameter tuning (Optuna, in
Quantara's case) validates the DecisionTreeClassifier's hyperparameters via
purged CV; THIS module performs the second stage that snippet also performs
-- "fit validated model on the entirety of the data" via bagging. Optuna
tunes the tree; this class deploys the bagged ensemble of that tuned tree.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.tree import DecisionTreeClassifier

from shared.models.sequential_bootstrap import get_avg_uniqueness, sequential_bootstrap

SEQUENTIAL_BOOTSTRAP_BAGGING_CONTRACT_VERSION = "quantara_afml_ch6_ch4_5_sequential_bootstrap_bagging_v1"


class SequentialBootstrapBaggingError(RuntimeError):
    """Raised when the bagging ensemble cannot be fit or scored safely."""


class SequentialBootstrapBaggingClassifier:
    """AFML Ch.6 bagging ensemble of decision trees, resampled via AFML Ch.4
    SS4.5's sequential bootstrap instead of a plain (IID) bootstrap.

    Not a sklearn BaseEstimator subclass (sequential bootstrap needs each
    event's bar-clock [t0, t1] span, which plain X/y/sample_weight cannot
    carry) -- but exposes the same predict_proba/classes_ surface every
    existing Quantara caller (shared/scoring/edge_scorer.py's
    positive_class_probability, shared/edges/probability_calibration_afml_
    replay_audit.py's positive_class_probability) already relies on, so no
    downstream scoring code needs to change.
    """

    def __init__(
        self,
        tree_params: dict[str, Any],
        *,
        n_estimators: int,
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
        self.estimators_: list[DecisionTreeClassifier] = []
        self.classes_: np.ndarray | None = None

    def fit(
        self,
        x_data: Any,
        y: Any,
        *,
        sample_weight: np.ndarray | None = None,
        event_start_pos: np.ndarray,
        event_end_pos: np.ndarray,
        n_bars: int,
    ) -> "SequentialBootstrapBaggingClassifier":
        x_arr = np.asarray(x_data)
        y_arr = np.asarray(y)
        n_rows = len(y_arr)
        if x_arr.shape[0] != n_rows:
            raise SequentialBootstrapBaggingError("x_data/y row-count mismatch.")
        if len(event_start_pos) != n_rows or len(event_end_pos) != n_rows:
            raise SequentialBootstrapBaggingError(
                "event_start_pos/event_end_pos must have one entry per training row."
            )
        weights = np.ones(n_rows, dtype="float64") if sample_weight is None else np.asarray(sample_weight, dtype="float64")
        if len(weights) != n_rows:
            raise SequentialBootstrapBaggingError("sample_weight row-count mismatch.")

        # AFML s.6.3.3 (p.97): with overlapping labels each estimator samples about average-uniqueness x n observations
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

        self.classes_ = self.estimators_[0].classes_
        for tree in self.estimators_[1:]:
            if not np.array_equal(tree.classes_, self.classes_):
                raise SequentialBootstrapBaggingError(
                    "A bagged tree saw a different class set than the ensemble; "
                    "some bootstrap draw is missing a class entirely (check class balance/n_estimators)."
                )
        # Matches sklearn's own RandomForest/BaggingClassifier convention: mean
        # of each tree's impurity-based feature_importances_ across the ensemble.
        self.feature_importances_ = np.mean(
            [tree.feature_importances_ for tree in self.estimators_], axis=0
        )
        return self

    def mean_pairwise_tree_correlation(self, x_data: Any) -> tuple[float, int]:
        """AFML's rho_bar (SS6.3.1, p.95): average pairwise correlation between
        the bagged trees' positive-class probability outputs on x_data. Used to
        plug the realized ensemble into AFML's own variance formula
        V[bagged] = sigma_bar^2 * (rho_bar + (1-rho_bar)/N) and judge, from real
        data, whether increasing n_estimators would meaningfully help -- rather
        than assuming the book's illustrative N=1000 applies here.

        Returns (rho_bar, n_degenerate_trees_excluded). A heavily regularized
        tree (large min_weight_fraction_leaf/min_samples_leaf) fit on a
        sequential-bootstrap resample can legitimately end up a single-leaf
        stump that predicts one constant probability for every row --
        observed on real data (2/100 trees on one edge's final fit). Pearson
        correlation is undefined (zero variance -> NaN), not zero, for such a
        tree, so it is excluded from the average rather than silently
        propagating NaN through the whole diagnostic. If it happens for many
        trees, that itself is informative (over-regularization), which is why
        the exclusion count is returned rather than hidden.
        """
        if len(self.estimators_) < 2:
            raise SequentialBootstrapBaggingError("Need at least 2 estimators to compute pairwise correlation.")
        x_arr = np.asarray(x_data)
        pos_col = int(np.where(self.classes_ == 1)[0][0]) if 1 in self.classes_ else 1
        preds = np.stack([tree.predict_proba(x_arr)[:, pos_col] for tree in self.estimators_], axis=0)
        non_degenerate = preds.std(axis=1) > 0.0
        n_excluded = int((~non_degenerate).sum())
        preds = preds[non_degenerate]
        if preds.shape[0] < 2:
            raise SequentialBootstrapBaggingError(
                f"Only {preds.shape[0]} of {len(self.estimators_)} bagged trees have non-constant "
                "predictions; cannot compute a pairwise correlation. Hyperparameters are likely too "
                "aggressively regularized for this data (most trees degenerated to single-leaf stumps)."
            )
        corr = np.corrcoef(preds)
        n = corr.shape[0]
        off_diag = corr[~np.eye(n, dtype=bool)]
        return float(np.mean(off_diag)), n_excluded

    def predict_proba(self, x_data: Any) -> np.ndarray:
        if not self.estimators_:
            raise SequentialBootstrapBaggingError("Ensemble is not fit yet.")
        x_arr = np.asarray(x_data)
        # AFML p.94: "the bagging classifier may derive a mean of the probabilities."
        proba_sum = np.zeros((x_arr.shape[0], len(self.classes_)), dtype="float64")
        for tree in self.estimators_:
            proba_sum += tree.predict_proba(x_arr)
        return proba_sum / len(self.estimators_)

    def predict(self, x_data: Any) -> np.ndarray:
        proba = self.predict_proba(x_data)
        return self.classes_[np.argmax(proba, axis=1)]


PORTABLE_PAYLOAD_MARKER_KEY = "quantara_sequential_bootstrap_bagging_portable_v1"


def to_portable_payload(clf: SequentialBootstrapBaggingClassifier) -> dict[str, Any]:
    """Convert a fitted ensemble to a plain dict for joblib.dump, instead of
    pickling the SequentialBootstrapBaggingClassifier instance directly.

    WHY: Step02/03 run standalone on Colab and inline their own copy of this
    class (cannot import shared/ at runtime -- same constraint as everywhere
    else in this codebase). When that inlined class is defined inside a
    script's own __main__ execution, pickle records it as
    __main__.SequentialBootstrapBaggingClassifier; no other process (Step05-
    09, shared/scoring/edge_scorer.py, live scoring) can resolve that later.
    Merely overriding __module__ on the inline class does NOT fix this --
    pickle's save_global performs a strict identity check (the inline class
    object is never `is` the canonical shared/ class object) and raises
    PicklingError, confirmed by direct test. The actual fix: persist only
    the ensemble's real, portable contents (a list of genuine sklearn
    DecisionTreeClassifier instances, which pickle identically everywhere)
    plus the plain data needed to reconstruct the wrapper, and reconstruct a
    real instance of *this* canonical class via from_portable_payload at
    load time, in the one place (EdgeScorer.load_model) that already
    imports this module.
    """
    return {
        PORTABLE_PAYLOAD_MARKER_KEY: True,
        "tree_params": clf.tree_params,
        "n_estimators": clf.n_estimators,
        "random_state": clf.random_state,
        "estimators_": clf.estimators_,
        "classes_": clf.classes_,
        "feature_importances_": getattr(clf, "feature_importances_", None),
    }


def is_portable_payload(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get(PORTABLE_PAYLOAD_MARKER_KEY) is True


def from_portable_payload(payload: dict[str, Any]) -> SequentialBootstrapBaggingClassifier:
    if not is_portable_payload(payload):
        raise SequentialBootstrapBaggingError("Payload is not a valid sequential-bootstrap-bagging portable payload.")
    clf = SequentialBootstrapBaggingClassifier(
        tree_params=payload["tree_params"],
        n_estimators=int(payload["n_estimators"]),
        random_state=payload.get("random_state"),
    )
    clf.estimators_ = list(payload["estimators_"])
    clf.classes_ = payload["classes_"]
    if payload.get("feature_importances_") is not None:
        clf.feature_importances_ = payload["feature_importances_"]
    return clf


def _self_test() -> None:
    rng = np.random.default_rng(0)
    n_rows, n_bars, max_h = 400, 3000, 30
    x = rng.normal(size=(n_rows, 5))
    y = (x[:, 0] + rng.normal(scale=0.5, size=n_rows) > 0).astype(int)
    start_pos = np.sort(rng.integers(0, n_bars - max_h, size=n_rows)).astype("int64")
    end_pos = (start_pos + rng.integers(1, max_h, size=n_rows)).astype("int64")
    end_pos = np.minimum(end_pos, n_bars - 1)

    clf = SequentialBootstrapBaggingClassifier(
        tree_params={"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced", "max_depth": 4},
        n_estimators=25,
        random_state=1,
    )
    clf.fit(x, y, event_start_pos=start_pos, event_end_pos=end_pos, n_bars=n_bars)
    proba = clf.predict_proba(x)
    if proba.shape != (n_rows, 2):
        raise SystemExit(f"self-test FAILED: predict_proba shape {proba.shape}")
    if not np.allclose(proba.sum(axis=1), 1.0):
        raise SystemExit("self-test FAILED: predict_proba rows do not sum to 1")
    if list(clf.classes_) != [0, 1]:
        raise SystemExit(f"self-test FAILED: unexpected classes_ {clf.classes_}")
    if len(clf.estimators_) != 25:
        raise SystemExit("self-test FAILED: wrong number of bagged estimators")

    import pickle
    restored = pickle.loads(pickle.dumps(clf))
    if not np.allclose(restored.predict_proba(x), proba):
        raise SystemExit("self-test FAILED: pickle round-trip changed predictions")

    # Portable-payload round trip: this is the form actually persisted to
    # model.pkl (see to_portable_payload's docstring for why the raw class
    # instance itself is never joblib.dump'd).
    payload = to_portable_payload(clf)
    if not is_portable_payload(payload):
        raise SystemExit("self-test FAILED: to_portable_payload output not recognized by is_portable_payload")
    restored_from_payload = from_portable_payload(pickle.loads(pickle.dumps(payload)))
    if not np.allclose(restored_from_payload.predict_proba(x), proba):
        raise SystemExit("self-test FAILED: portable-payload round trip changed predictions")
    if not np.allclose(restored_from_payload.feature_importances_, clf.feature_importances_):
        raise SystemExit("self-test FAILED: portable-payload round trip lost feature_importances_")

    # In-sample accuracy sanity check against an informative signal.
    acc = (clf.predict(x) == y).mean()
    if acc < 0.6:
        raise SystemExit(f"self-test FAILED: ensemble accuracy too low ({acc:.3f}) on a clearly separable signal")

    if clf.feature_importances_.shape != (5,) or not np.isclose(clf.feature_importances_.sum(), 1.0):
        raise SystemExit(f"self-test FAILED: feature_importances_ invalid: {clf.feature_importances_}")

    rho_bar, n_excluded = clf.mean_pairwise_tree_correlation(x)
    if not (-1.0 <= rho_bar <= 1.0):
        raise SystemExit(f"self-test FAILED: rho_bar out of range: {rho_bar}")

    print(f"shared.models.sequential_bootstrap_bagging self-test passed "
          f"(in-sample acc={acc:.3f}, rho_bar={rho_bar:.3f}, degenerate_trees_excluded={n_excluded})")


def _cross_process_self_test() -> None:
    """Reproduces the exact failure mode found and fixed this session: Step02
    runs standalone (its own __main__ process, own inline class definition)
    and must produce a model.pkl that a SEPARATE process -- one that only
    imports this canonical module, exactly like shared/scoring/edge_scorer.py
    -- can load. A same-process pickle round trip (see _self_test above)
    does NOT exercise this; it must be a genuine subprocess."""
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    producer_src = '''
import numpy as np
from sklearn.tree import DecisionTreeClassifier
import sys
sys.path.insert(0, {repo_root!r})
from shared.models.sequential_bootstrap import sequential_bootstrap

class SequentialBootstrapBaggingClassifier:
    def __init__(self, tree_params, *, n_estimators, random_state=None):
        self.tree_params = dict(tree_params); self.n_estimators = int(n_estimators)
        self.random_state = random_state; self.estimators_ = []; self.classes_ = None
    def fit(self, x_data, y, *, sample_weight=None, event_start_pos, event_end_pos, n_bars):
        x_arr = np.asarray(x_data); y_arr = np.asarray(y); n_rows = len(y_arr)
        weights = np.ones(n_rows) if sample_weight is None else np.asarray(sample_weight, dtype="float64")
        rng = np.random.default_rng(self.random_state); self.estimators_ = []
        for _ in range(self.n_estimators):
            draw = sequential_bootstrap(event_start_pos, event_end_pos, n_bars, sample_length=n_rows, random_state=rng)
            tree = DecisionTreeClassifier(**self.tree_params)
            tree.fit(x_arr[draw], y_arr[draw], sample_weight=weights[draw])
            self.estimators_.append(tree)
        self.classes_ = self.estimators_[0].classes_
        self.feature_importances_ = np.mean([t.feature_importances_ for t in self.estimators_], axis=0)
        return self
    def predict_proba(self, x_data):
        x_arr = np.asarray(x_data); s = np.zeros((x_arr.shape[0], len(self.classes_)))
        for tree in self.estimators_: s += tree.predict_proba(x_arr)
        return s / len(self.estimators_)

rng = np.random.default_rng(0)
n, n_bars = 60, 500
x = rng.normal(size=(n, 3)); y = (x[:, 0] > 0).astype(int)
start = np.sort(rng.integers(0, n_bars - 10, size=n)).astype("int64")
end = np.minimum((start + rng.integers(1, 10, size=n)).astype("int64"), n_bars - 1)
clf = SequentialBootstrapBaggingClassifier(
    tree_params={{"criterion": "entropy", "max_features": "sqrt", "class_weight": "balanced"}},
    n_estimators=5, random_state=0,
)
clf.fit(x, y, event_start_pos=start, event_end_pos=end, n_bars=n_bars)
np.save({x_path!r}, x)
np.save({proba_path!r}, clf.predict_proba(x))

import joblib
joblib.dump({{
    "quantara_sequential_bootstrap_bagging_portable_v1": True,
    "tree_params": clf.tree_params, "n_estimators": clf.n_estimators,
    "random_state": clf.random_state, "estimators_": clf.estimators_,
    "classes_": clf.classes_, "feature_importances_": clf.feature_importances_,
}}, {model_path!r})
'''
    repo_root = str(Path(__file__).resolve().parents[2])
    with tempfile.TemporaryDirectory() as tmpdir:
        model_path = str(Path(tmpdir) / "model.pkl")
        x_path = str(Path(tmpdir) / "x.npy")
        proba_path = str(Path(tmpdir) / "proba.npy")
        script = producer_src.format(repo_root=repo_root, model_path=model_path, x_path=x_path, proba_path=proba_path)
        script_path = Path(tmpdir) / "producer.py"
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"cross-process self-test FAILED: producer subprocess errored:\n{result.stderr}")

        import joblib
        raw = joblib.load(model_path)
        if not is_portable_payload(raw):
            raise SystemExit("cross-process self-test FAILED: dumped payload not recognized as portable")
        clf = from_portable_payload(raw)
        x = np.load(x_path)
        expected = np.load(proba_path)
        actual = clf.predict_proba(x)
        if not np.allclose(actual, expected):
            raise SystemExit("cross-process self-test FAILED: predict_proba mismatch after cross-process load")

    print("shared.models.sequential_bootstrap_bagging cross-process self-test passed "
          "(genuine subprocess producer, this-process consumer)")


if __name__ == "__main__":
    _self_test()
    _cross_process_self_test()
