"""Gate D v4.1 (D078) unit tests on synthetic data: weighted cross-fitted calibration, D1 F1 bootstrap at calibrated
P(win) > b, D2 weighted log loss; and the D082 weighted live-calibrator fit."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(V2))
from shared.edges import gate_d_calibration_monotonicity as gd  # noqa: E402


def _frame(n: int, skill: float, seed: int = 0) -> tuple[pd.DataFrame, list[str]]:
    rng = np.random.default_rng(seed)
    t0 = pd.date_range("2022-01-03", periods=n, freq="5h", tz="UTC")
    y = (rng.random(n) < 0.4).astype(int)
    paths = []
    for k in range(9):
        z = skill * (y - 0.4) + rng.normal(0, 0.15, n)
        paths.append(np.clip(0.6 + z, 0.01, 0.99))           # shifted like our edges (neutral point above 0.5)
    cols = [f"cpcv_path_{k:02d}_raw_probability" for k in range(9)]
    df = pd.DataFrame({"Datetime": t0.astype(str), "label_t1": (t0 + pd.Timedelta(hours=12)).astype(str),
                       "quote_executable_meta_label": y, **dict(zip(cols, paths))})
    df["raw_probability"] = df[cols].mean(axis=1)
    return df, cols


def _run(df: pd.DataFrame, cols: list[str], w: np.ndarray) -> tuple[dict, dict]:
    cf = gd.cross_fit_calibration(df, cols, w)
    d1 = gd.d1_act_or_pass(cf["y"], cf["p_event"] > cf["b"], gd._weeks(df.Datetime), draws=2000)
    return d1, gd.d2_sizing(df, cf)


class GateDV41Test(unittest.TestCase):
    def test_f1_primary_formula(self):
        y = np.array([1, 0, 0, 1, 0])
        r = gd.d1_act_or_pass(y, np.ones(5, bool), np.array(["w"] * 5), draws=200)
        b = 0.4
        self.assertAlmostEqual(r["f1_primary"], 2 * b / (1 + b))
        self.assertAlmostEqual(r["f1_meta"], r["f1_primary"])     # acting on everything = primary

    def test_no_skill_fails_and_skill_passes(self):
        for skill, expect in ((0.0, False), (0.6, True)):
            df, cols = _frame(3000, skill)
            d1, d2 = _run(df, cols, np.ones(len(df)))
            self.assertEqual(d1["PASS"], expect, (skill, d1))
            self.assertEqual(d2["PASS"], expect, (skill, d2))
            if skill > 0:
                self.assertLess(d1["acted_share"], 0.9)          # calibrated P > b filters despite the shifted raw scale

    def test_weights_reach_calibrator_and_base_rate(self):
        df, cols = _frame(2000, 0.3, seed=1)
        y = df.quote_executable_meta_label.to_numpy()
        w = np.where(y == 1, 2.0, 1.0)                           # winners carry twice the weight
        cf = gd.cross_fit_calibration(df, cols, w)
        self.assertTrue(np.all(cf["b"] > y.mean()))              # weighted base rate above the count rate
        cf1 = gd.cross_fit_calibration(df, cols, np.ones(len(df)))
        self.assertGreater(np.nanmean(cf["p_event"]), np.nanmean(cf1["p_event"]))

    def test_cross_fit_blocks_purge_and_embargo(self):
        t0 = np.arange(1000) * 10
        t1 = t0 + 25                       # overlaps the next two events
        for train, test in gd._cross_fit_blocks(t0, t1, 10):
            self.assertFalse(set(train) & set(test))
            lo, hi = t0[test].min(), t1[test].max()
            self.assertFalse(np.any((t0[train] <= hi) & (t1[train] >= lo)))
            after = np.arange(test.max() + 1, min(test.max() + 1 + 100, 1000))
            self.assertFalse(set(after) & set(train))

    def test_live_sigmoid_fit_is_weighted(self):
        from shared.calibration.fit_sigmoid_calibrator_and_thresholds import fit_sigmoid_params
        from shared.models.cpcv_paths import stacked_path_fit_inputs, stacked_path_fit_weights
        df, cols = _frame(1500, 0.3, seed=2)
        df.loc[df.index[:10], cols[0]] = np.nan
        x, y, _ = stacked_path_fit_inputs(df, "quote_executable_meta_label")
        w = stacked_path_fit_weights(df, "quote_executable_meta_label", np.where(df.quote_executable_meta_label == 1, 2.0, 1.0))
        self.assertEqual(len(w), len(y))
        self.assertTrue(np.all(w[y == 1] == 2.0) and np.all(w[y == 0] == 1.0))
        pu, pw = fit_sigmoid_params(x, y), fit_sigmoid_params(x, y, sample_weight=w)
        self.assertGreater(pw["intercept"], pu["intercept"])     # up-weighting winners raises the fitted P(win)
        text = (V2 / "shared" / "calibration" / "fit_sigmoid_calibrator_and_thresholds.py").read_text(encoding="utf-8")
        body = text[text.index("def fit_final_live_sigmoid"):text.index("save_json(paths.sigmoid_calibrator_json")]
        self.assertIn("sample_weight=stacked_path_fit_weights(oof, TARGET_COL, event_w)", body)


if __name__ == "__main__":
    unittest.main()
