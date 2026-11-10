"""AFML Ch.9 walkthrough (2026-10-24): every edge's Step 02 tunes the AFML Snippet 9.1/9.3 way.

Purged k-fold (5) inner CV, scoring='f1' at the classifier's own decision (p > 0.5) for meta-labeling, randomized
search, max_depth 1-8, no time decay (c = 1).

Run from trading_models_v2:  python -m unittest tests.test_afml_ch9_tuning
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

V2 = Path(__file__).resolve().parents[1]


class Step02TuningContractTest(unittest.TestCase):
    def test_every_step02_follows_snippet_9_1_and_9_3(self) -> None:
        scripts = sorted(V2.glob("assets/*/edges/*/research/02_train_meta_model_optuna.py"))
        self.assertTrue(scripts)
        for path in scripts:
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=str(path.relative_to(V2))):
                self.assertIn("TUNING_N_SPLITS = 5", text)
                self.assertIn("SCORING_DECISION_THRESHOLD = 0.5", text)
                self.assertIn('"max_depth": (1, 8),', text)
                self.assertIn("optuna.samplers.RandomSampler(seed=RANDOM_STATE)", text)
                self.assertNotIn("TPESampler(", text)
                self.assertNotIn("cv.split(x_data)", text)
                objective = text[text.index("def evaluate_params_cpcv("): text.index("def optuna_objective(")]
                self.assertNotIn("find_best_f1_threshold(", objective)
                self.assertIn("np.array_split(np.arange(n_rows), TUNING_N_SPLITS)", objective)
                self.assertTrue(re.search(r"^TIME_DECAY_C = 1\.0", text, re.M))


if __name__ == "__main__":
    unittest.main()
