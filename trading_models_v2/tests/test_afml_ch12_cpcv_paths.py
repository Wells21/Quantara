"""AFML Ch.12 walkthrough, owner decision D052 (2026-10-26): CPCV backtest paths (AFML s.12.4.2 step 5, pp.165-166;
s.12.5) through Step 03 -> Step 04 -> portfolio_allocator 04B / 04C.

Checks:
  1. every edge's Step 03 carries the identical inlined path helper, N_FOLDS = 10, k = 2, and writes the path columns;
  2. the helper, driven by skfolio CombinatorialPurgedCV(10, 2), builds phi = 9 complete paths: every row appears on every
     path exactly once, with the forecast of the split that tested it there (book Fig. 12.1 / Snippet logic);
  3. Step 04 fits the live calibrator on all paths stacked and fails closed without paths;
  4. 04C scores a threshold by the mean across paths (the single-path winner can lose), keeps gates on the main path.

Run from trading_models_v2 (venv312, skfolio needed):  python -m unittest tests.test_afml_ch12_cpcv_paths
"""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[1]
START, END = "# QUANTARA_AFML_CH12_CPCV_PATHS_V1", "# END_QUANTARA_AFML_CH12_CPCV_PATHS_V1"
STEP03 = sorted((V2 / "assets").glob("*/edges/*/research/03_generate_oof_predictions.py"))


def helper_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    a, b = text.index(START), text.index(END)
    return text[a:b + len(END)]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Step03Paths(unittest.TestCase):
    def test_every_step03_is_patched_identically(self) -> None:
        self.assertTrue(STEP03)
        blocks = {p: helper_block(p) for p in STEP03}
        self.assertEqual(len(set(blocks.values())), 1, "inlined CPCV path helper differs between edges")
        for p in STEP03:
            text = p.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?m)^N_FOLDS = 10\b", p)
            self.assertRegex(text, r"(?m)^N_TEST_FOLDS = 2\b", p)
            self.assertIn("assign_cpcv_path_predictions(path_probs, path_ids, fold_num - 1, test_parts, test_idx, preds)", text)
            self.assertIn("oof_df[CPCV_PATH_COLUMN_TEMPLATE.format(path_no)] = path_probs[path_no]", text)

    def test_template_matches_shared_module(self) -> None:
        from shared.models.cpcv_paths import CPCV_PATH_COLUMN_TEMPLATE
        m = re.search(r'CPCV_PATH_COLUMN_TEMPLATE = "([^"]+)"', helper_block(STEP03[0]))
        self.assertEqual(m.group(1), CPCV_PATH_COLUMN_TEMPLATE)

    def test_helper_builds_complete_paths_with_skfolio(self) -> None:
        try:
            from skfolio.model_selection import CombinatorialPurgedCV
        except ImportError:  # pragma: no cover
            self.skipTest("skfolio not installed (use venv312)")
        ns: dict = {"np": np}
        exec(helper_block(STEP03[0]), ns)
        assign = ns["assign_cpcv_path_predictions"]

        n = 1000
        x = pd.DataFrame({"f": np.arange(n, dtype=float)})
        cv = CombinatorialPurgedCV(n_folds=10, n_test_folds=2, purged_size=0, embargo_size=0)
        path_ids = np.asarray(cv.get_path_ids())
        self.assertEqual(int(cv.n_test_paths), 9)  # phi[10, 2] = (2/10) * C(10, 8) = 9
        path_probs = np.full((int(cv.n_test_paths), n), np.nan)
        split_of = np.full((int(cv.n_test_paths), n), -1)
        n_splits = 0
        for s, (_, test_idx) in enumerate(cv.split(x)):
            parts = list(test_idx) if isinstance(test_idx, (list, tuple)) else [test_idx]
            flat = np.concatenate([np.atleast_1d(np.asarray(p, dtype=int)) for p in parts])
            preds = s * 10_000 + flat  # forecast encodes (split, row)
            assign(path_probs, path_ids, s, parts, flat, preds)
            for j, part in enumerate(parts):
                split_of[int(path_ids[s, j]), np.asarray(part, dtype=int)] = s
            n_splits += 1
        self.assertEqual(n_splits, 45)
        self.assertTrue(np.isfinite(path_probs).all(), "every path must cover every row")
        rows = np.arange(n)
        for k in range(path_probs.shape[0]):
            np.testing.assert_array_equal(path_probs[k], split_of[k] * 10_000 + rows)
        # each row is tested by 9 different splits, one per path
        self.assertTrue(all(len(set(split_of[:, r])) == 9 for r in range(0, n, 37)))


class Step04Stacking(unittest.TestCase):
    def test_stacked_inputs(self) -> None:
        from shared.models.cpcv_paths import path_columns, stacked_path_fit_inputs
        f = pd.DataFrame({
            "y": [1, 0, 1, 0],
            "cpcv_path_01_raw_probability": [0.6, 0.4, np.nan, 0.3],
            "cpcv_path_00_raw_probability": [0.7, 0.2, 0.8, 0.1],
            "raw_probability": [0.65, 0.3, 0.8, 0.2],
        })
        self.assertEqual(path_columns(f.columns), ["cpcv_path_00_raw_probability", "cpcv_path_01_raw_probability"])
        x, y, info = stacked_path_fit_inputs(f, "y")
        np.testing.assert_allclose(x, [0.7, 0.2, 0.8, 0.1, 0.6, 0.4, 0.3])
        np.testing.assert_array_equal(y, [1, 0, 1, 0, 1, 0, 0])
        self.assertEqual(info["rows_per_path"], [4, 3])
        self.assertEqual(info["rows_stacked"], 7)
        with self.assertRaises(ValueError):
            stacked_path_fit_inputs(f[["y", "raw_probability"]], "y")

    def test_live_calibrator_requires_paths(self) -> None:
        text = (V2 / "shared" / "calibration" / "fit_sigmoid_calibrator_and_thresholds.py").read_text(encoding="utf-8")
        body = text[text.index("def fit_final_live_sigmoid"):text.index("save_json(paths.sigmoid_calibrator_json")]
        self.assertIn("raise CalibrationError", body)
        self.assertIn("stacked_path_fit_inputs(oof, TARGET_COL)", body)


class AllocatorPathSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.m04c = load_module("alloc_04c", V2 / "portfolio_allocator" / "04c_select_representative_threshold_candidates.py")

    def test_04b_writes_path_ledger(self) -> None:
        text = (V2 / "portfolio_allocator" / "04b_build_threshold_grid_trade_ledger.py").read_text(encoding="utf-8")
        self.assertIn("*path_columns(columns),", text)
        self.assertIn("REQUIRE_CPCV_PATHS = True", text)
        self.assertIn("def build_path_ledger(", text)
        self.assertIn('"v2_threshold_grid_cpcv_path_trade_ledger.csv"', text)

    def test_summary(self) -> None:
        s = self.m04c.summarize_cpcv_paths([0.2, -0.1, 0.5], [0.01, -0.02, 0.03])
        self.assertAlmostEqual(s["cpcv_path_mean_score"], 0.2)
        self.assertAlmostEqual(s["cpcv_path_worst_score"], -0.1)
        self.assertAlmostEqual(s["cpcv_path_losing_share"], 1 / 3)
        self.assertEqual(s["cpcv_path_count"], 3)

    def test_mean_path_score_drives_selection_gates_stay(self) -> None:
        g = pd.DataFrame({
            "threshold": [0.55, 0.60, 0.65],
            "compound_velocity_score": [0.9, 0.5, -np.inf],   # main path: 0.55 wins, 0.65 failed its gate
            "compound_velocity_score_basis": ["x"] * 3,
            "compound_velocity_gate_pass": [True, True, False],
            "cpcv_path_mean_score": [0.1, 0.4, 2.0],          # across paths: 0.60 wins; 0.65 stays gated out
        })
        out = self.m04c.apply_cpcv_path_selection_score(g)
        self.assertEqual(out.sort_values("compound_velocity_score", ascending=False)["threshold"].iloc[0], 0.60)
        self.assertEqual(out.loc[2, "compound_velocity_score"], -np.inf)
        np.testing.assert_allclose(out["compound_velocity_score_single_path"].iloc[:2], [0.9, 0.5])
        self.assertEqual(out["compound_velocity_score_basis"].iloc[0], self.m04c.CPCV_SELECTION_SCORE_BASIS)

    def test_path_growth_score_matches_main_formula(self) -> None:
        m = self.m04c
        n = 40
        rng = np.random.default_rng(7)
        g = pd.DataFrame({
            "grid_candidate_id": "g1", "cpcv_path": 0,
            "exit_time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC").astype(str),
            "executed_r": rng.choice([1.0, -1.0], size=n, p=[0.6, 0.4]),
            "afml_bet_size": 0.5, "risk_per_1_lot": 1000.0, "broker_min_lot": 0.01, "broker_lot_step": 0.01,
            "has_valid_broker_min_lot_risk": True,
        })
        f_star = 0.01
        res = m.cpcv_path_growth_score(g, f_star, n)
        ev = m.broker_growth_eval_for_f(
            f_star, g["executed_r"].to_numpy() - m.COMPOUND_COST_STRESS_R, np.full(n, 0.5), np.full(n, 1000.0),
            np.full(n, 0.01), np.full(n, 0.01), n, n)
        self.assertAlmostEqual(res["log_growth"], ev["log_growth"])
        if ev["velocity_calendar"] > 0:
            self.assertAlmostEqual(res["score"], ev["score"])
        # a losing path keeps its sign (the main formula would return -inf, which would swamp the mean)
        losing = g.assign(executed_r=-g["executed_r"].abs())
        self.assertLess(m.cpcv_path_growth_score(losing, f_star, n)["score"], 0.0)
        self.assertTrue(np.isfinite(m.cpcv_path_growth_score(losing, f_star, n)["score"]))

    def test_streaming_reader_scores_every_path(self) -> None:
        import tempfile
        m = self.m04c
        rows = []
        for gid, sign in (("a", 1.0), ("b", -1.0)):
            for path in range(3):
                if gid == "b" and path == 2:
                    continue  # no trade on that path -> scores 0
                for d in range(5):
                    rows.append({"grid_candidate_id": gid, "edge_id": "E/x_long", "cpcv_path": path,
                                 "exit_time": f"2025-01-0{d + 1} 00:00:00+00:00", "executed_r": sign * (1 if d % 2 else 0.5),
                                 "afml_bet_size": 1.0, "risk_per_1_lot": 1000.0, "broker_min_lot": 0.01,
                                 "broker_lot_step": 0.01, "has_valid_broker_min_lot_risk": True})
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "paths.csv"
            pd.DataFrame(rows).to_csv(p, index=False)
            fs = pd.DataFrame({"grid_candidate_id": ["a", "b", "c"], "broker_optimized_f": [0.01, 0.01, 0.0],
                               "edge_backtest_span_days": [5, 5, 5]})
            old = m.CPCV_PATH_READ_CHUNK_ROWS
            m.CPCV_PATH_READ_CHUNK_ROWS = 4  # force chunk boundaries inside groups
            try:
                out = m.cpcv_path_stats_from_ledger(p, fs, {"E/x_long": 3}).set_index("grid_candidate_id")
            finally:
                m.CPCV_PATH_READ_CHUNK_ROWS = old
        self.assertEqual(out.loc["a", "cpcv_path_count"], 3)
        self.assertGreater(out.loc["a", "cpcv_path_worst_score"], 0)
        self.assertAlmostEqual(out.loc["b", "cpcv_path_losing_share"], 1.0)  # two losing paths + one empty (0 growth)
        self.assertEqual(out.loc["b", "cpcv_path_count"], 3)
        self.assertEqual(out.loc["c", "cpcv_path_count"], 0)


if __name__ == "__main__":
    unittest.main()
