"""Tests for shared/features/afml_ch17_structural_breaks.py (AFML Ch.17, book pp.249-262).

- get_bsadf / get_yx / lag_df / get_betas reproduce Snippets 17.1-17.4 run verbatim (p.258-259);
- structural_break_features evaluates every bar on the window ending at that bar (D061);
- QADF (s.17.4.2.4 p.255) and CADF (s.17.4.2.5 p.256) by hand; CSW (s.17.3.2 p.251) by hand; SMT (s.17.4.3
  pp.259-261) against an independent least-squares fit; exercise 17.5 (ADF on log 1/y = ADF on log y);
- SADF spikes on an injected explosive segment (the book's qualitative claim, p.253 Fig 17.1).
"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from shared.features.afml_ch17_structural_breaks import (
    STRUCTURAL_BREAK_COLUMNS,
    StructuralBreakError,
    SMT_PHIS,
    SMT_SPECS,
    cadf,
    csw,
    get_adf_stats,
    get_betas,
    get_bsadf,
    get_yx,
    lag_df,
    qadf,
    smt,
    structural_break_features,
    structural_break_row,
)


# ---- AFML Snippets 17.1-17.4 verbatim (Python 3: Snippet 17.1's None comparison guarded) ----
def _book_lagDF(df0, lags):
    df1 = pd.DataFrame()
    if isinstance(lags, int):
        lags = range(lags + 1)
    else:
        lags = [int(lag) for lag in lags]
    for lag in lags:
        df_ = df0.shift(lag).copy(deep=True)
        df_.columns = [str(i) + '_' + str(lag) for i in df_.columns]
        df1 = df1.join(df_, how='outer')
    return df1


def _book_getYX(series, constant, lags):
    series_ = series.diff().dropna()
    x = _book_lagDF(series_, lags).dropna()
    x.iloc[:, 0] = series.values[-x.shape[0] - 1:-1, 0]  # lagged level
    y = series_.iloc[-x.shape[0]:].values
    if constant != 'nc':
        x = np.append(x, np.ones((x.shape[0], 1)), axis=1)
        if constant[:2] == 'ct':
            trend = np.arange(x.shape[0]).reshape(-1, 1)
            x = np.append(x, trend, axis=1)
        if constant == 'ctt':
            x = np.append(x, trend ** 2, axis=1)
    return y, x


def _book_getBetas(y, x):
    xy = np.dot(x.T, y)
    xx = np.dot(x.T, x)
    xxinv = np.linalg.inv(xx)
    bMean = np.dot(xxinv, xy)
    err = y - np.dot(x, bMean)
    bVar = np.dot(err.T, err) / (x.shape[0] - x.shape[1]) * xxinv
    return bMean, bVar


def _book_get_bsadf(logP, minSL, constant, lags):
    y, x = _book_getYX(logP, constant=constant, lags=lags)
    startPoints, bsadf, allADF = range(0, y.shape[0] + lags - minSL + 1), None, []
    for start in startPoints:
        y_, x_ = y[start:], x[start:]
        bMean_, bStd_ = _book_getBetas(y_, x_)
        bMean_, bStd_ = bMean_[0, 0], bStd_[0, 0] ** .5
        allADF.append(bMean_ / bStd_)
        if bsadf is None or allADF[-1] > bsadf:
            bsadf = allADF[-1]
    return {'Time': logP.index[-1], 'gsadf': bsadf}


class TestBookSnippets(unittest.TestCase):
    def test_lag_df_includes_lag_zero_and_shifts_correctly(self):
        df = pd.DataFrame({"x": [1.0, 2.0, 3.0, 4.0, 5.0]})
        out = lag_df(df, 2)
        self.assertEqual(list(out.columns), ["x_0", "x_1", "x_2"])
        self.assertEqual(out["x_0"].tolist(), [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertTrue(np.isnan(out["x_1"].iloc[0]))
        self.assertEqual(out["x_1"].iloc[1], 1.0)
        self.assertEqual(out["x_2"].iloc[2], 1.0)

    def test_get_betas_recovers_known_linear_relationship(self):
        rng = np.random.default_rng(1)
        x = np.column_stack([rng.normal(size=500), np.ones(500)])
        y = x @ np.array([2.5, -1.0]) + rng.normal(scale=0.01, size=500)
        b_mean, _ = get_betas(y, x)
        np.testing.assert_allclose(b_mean, [2.5, -1.0], atol=0.05)

    def test_port_equals_verbatim_snippets(self):
        """Non-degenerate cases (final regression rows minSL - lags > columns): identical designs and SADF."""
        rng = np.random.default_rng(7)
        checked = 0
        for trial in range(120):
            n, lags, constant = int(rng.integers(30, 120)), int(rng.integers(0, 3)), ["nc", "ct", "ctt"][trial % 3]
            min_sl = int(rng.integers(8, 20))
            series = pd.Series(np.log(100.0) + np.cumsum(rng.normal(0, 0.01, n)))
            y_b, x_b = _book_getYX(series.to_frame(), constant, lags)
            y_p, x_p = get_yx(series, constant, lags)
            np.testing.assert_array_equal(np.asarray(x_b, dtype=float), x_p)
            np.testing.assert_array_equal(y_b[:, 0], y_p)
            if min_sl - lags <= x_p.shape[1]:
                continue  # 0 degrees of freedom in the book's last regression (it divides by zero there)
            book = _book_get_bsadf(series.to_frame(), min_sl, constant, lags)["gsadf"]
            self.assertAlmostEqual(get_bsadf(series, min_sl, constant, lags), book, places=9)
            checked += 1
        self.assertGreater(checked, 80)

    def test_final_regression_has_min_sl_minus_lags_rows(self):
        series = pd.Series(np.cumsum(np.random.default_rng(0).normal(size=50)))
        for lags in (0, 1, 2):
            y, _ = get_yx(series, "ct", lags)
            n_starts = y.shape[0] + lags - 10 + 1
            self.assertEqual(y.shape[0] - (n_starts - 1), 10 - lags)
            self.assertEqual(get_adf_stats(series, 10, "ct", lags).size, n_starts)


class TestQadfCadf(unittest.TestCase):
    def test_by_hand(self):
        s = np.arange(40.0)  # sorted ADF values 0..39
        q_c, q_d = qadf(s, q=0.95, v=0.025)
        self.assertAlmostEqual(q_c, 0.95 * 39)                 # linear quantile: 37.05
        self.assertAlmostEqual(q_d, 0.975 * 39 - 0.925 * 39)   # 1.95
        c_c, c_d = cadf(s, q=0.95)
        self.assertAlmostEqual(c_c, 38.5)                      # values >= 37.05: {38, 39}
        self.assertAlmostEqual(c_d, 0.5)
        self.assertLessEqual(c_c, s.max())                     # C_t,q <= SADF_t (p.256)

    def test_v_must_respect_book_bounds(self):
        with self.assertRaises(StructuralBreakError):
            qadf(np.arange(10.0), q=0.95, v=0.06)              # v > 1 - q


class TestCswSmt(unittest.TestCase):
    def test_csw_by_hand(self):
        y = np.array([0.0, 1.0, 0.0, 2.0])          # diffs 1, -1, 2 -> sigma^2 = 6 / 3 = 2
        up, down = csw(y)
        s = [(2 - 0) / (np.sqrt(2) * np.sqrt(3)), (2 - 1) / (np.sqrt(2) * np.sqrt(2)), (2 - 0) / (np.sqrt(2) * 1)]
        self.assertAlmostEqual(up, max(s))
        self.assertAlmostEqual(down, max(-x for x in s))

    def test_smt_equals_independent_least_squares(self):
        rng = np.random.default_rng(11)
        price = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 50)))
        got = smt(price, min_sl=10)
        t = price.size
        for spec in SMT_SPECS:
            best = {phi: -np.inf for phi in SMT_PHIS}
            for t0 in range(0, t - 10):
                n = t - t0
                s_ = np.arange(1.0, n + 1)
                y = price[t0:] if spec == "Poly1" else np.log(price[t0:] + 1e-12)
                x = {"Poly1": np.column_stack([np.ones(n), s_, s_ ** 2]), "Poly2": np.column_stack([np.ones(n), s_, s_ ** 2]),
                     "Exp": np.column_stack([np.ones(n), s_]), "Power": np.column_stack([np.ones(n), np.log(s_)])}[spec]
                b, *_ = np.linalg.lstsq(x, y, rcond=None)
                e = y - x @ b
                se = np.sqrt(e @ e / (n - x.shape[1]) * np.linalg.inv(x.T @ x)[-1, -1])
                for phi in SMT_PHIS:
                    best[phi] = max(best[phi], abs(b[-1]) / (se * (n - 1) ** phi))
            for phi in SMT_PHIS:
                key = f"SMT_{spec}_{ {0.0: 'phi0', 0.5: 'phi05', 1.0: 'phi1'}[phi] }".replace(" ", "")
                self.assertAlmostEqual(got[key], best[phi], places=7)

    def test_exercise_17_5_adf_is_invariant_to_the_reciprocal_on_log_prices(self):
        rng = np.random.default_rng(5)
        lp = pd.Series(np.log(100.0) + np.cumsum(rng.normal(0, 0.01, 80)))
        for constant in ("nc", "ct", "ctt"):
            np.testing.assert_array_equal(get_adf_stats(lp, 15, constant, 1), get_adf_stats(-lp, 15, constant, 1))


class TestStructuralBreakFeatures(unittest.TestCase):
    def test_rejects_lookback_too_small(self):
        series = pd.Series(np.arange(1.0, 50.0) + 100.0)
        with self.assertRaises(StructuralBreakError):
            structural_break_features(series, min_sl=10, lags=1, lookback=15)

    def test_every_bar_on_window_ending_at_that_bar(self):
        rng = np.random.default_rng(3)
        close = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 120))))
        out = structural_break_features(close, min_sl=10, constant="ct", lags=1, lookback=50)
        self.assertEqual(list(out.columns), list(STRUCTURAL_BREAK_COLUMNS))
        self.assertTrue(out.iloc[:49].isna().all().all())
        self.assertFalse(out.iloc[49:].isna().any().any())
        for t in (49, 50, 57, 83, 119):
            row = structural_break_row(close.to_numpy()[t - 49:t + 1], min_sl=10, constant="ct", lags=1)
            for col in STRUCTURAL_BREAK_COLUMNS:
                self.assertEqual(out[col].iloc[t], row[col])
        self.assertTrue((out["CADF"] <= out["SADF"] + 1e-12).iloc[49:].all())

    def test_sadf_spikes_during_injected_explosive_segment(self):
        rng = np.random.default_rng(42)
        rw = np.cumsum(rng.normal(scale=0.01, size=300))
        bubble = np.cumsum(rng.normal(loc=0.02, scale=0.01, size=100))
        tail = np.cumsum(rng.normal(scale=0.01, size=100))
        log_price = np.concatenate([rw, bubble + rw[-1], tail + bubble[-1] + rw[-1]]) + np.log(100.0)
        sadf = structural_break_features(pd.Series(np.exp(log_price)), min_sl=20, constant="ct", lags=1, lookback=150)["SADF"]
        self.assertGreater(sadf.iloc[300:400].mean(), sadf.iloc[150:300].mean())


if __name__ == "__main__":
    unittest.main()
