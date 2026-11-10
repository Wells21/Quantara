"""AFML Ch.16 s.16.5 / Appendix 16.A.3: reproduce Table 16.1 (p.233) with shared/research/ch16_allocation_tests.py.

Book data: Snippet 16.4 generateData(nObs 10000, size0 5, size1 5, sigma1 .25), np.random.seed(12345). The perturbed columns
come from Python 2's random.randint (not reproducible in Python 3); Figure 16.5's caption gives them: 6 <- 3, 7 <- 1, 8 <- 5,
9 <- 2, 10 <- 2, i.e. cols = [2, 0, 4, 1, 1] (0-based). numpy's legacy normal draws are version-stable.
Run on venv39 (07 needs pandas < 3; see AFML_MLAM_WALKTHROUGH.md s.4P.2).
"""
import numpy as np
import pytest

from shared.research import ch16_allocation_tests as c

TABLE_16_1 = {  # percent, Table 16.1 p.233
    "CLA": [14.44, 19.93, 19.73, 19.87, 18.68, 0.00, 5.86, 1.49, 0.00, 0.00],
    "HRP": [7.00, 7.59, 10.84, 19.03, 9.72, 10.19, 6.62, 9.10, 7.12, 12.79],
    "IVP": [10.36, 10.28, 10.36, 10.25, 10.31, 9.74, 9.80, 9.65, 9.64, 9.61],
}


@pytest.fixture(scope="module")
def book_data():
    np.random.seed(12345)
    x = np.random.normal(0, 1, size=(10000, 5))
    y = x[:, [2, 0, 4, 1, 1]] + np.random.normal(0, .25, size=(10000, 5))
    x = np.append(x, y, axis=1)
    return np.cov(x, rowvar=False), np.corrcoef(x, rowvar=False)


def test_condition_number(book_data):
    ev = np.linalg.eigvalsh(book_data[0])
    assert ev.max() / ev.min() == pytest.approx(150.9324, abs=5e-5)  # p.231


@pytest.mark.parametrize("method", ["CLA", "HRP", "IVP"])
def test_table_16_1(book_data, method):
    cov, corr = book_data
    w = {"CLA": c.cla_min_variance(cov), "HRP": c.hrp(cov, corr, tilde=True), "IVP": c.ivp(cov)}[method]
    assert np.allclose(w * 100, TABLE_16_1[method], atol=0.006)


def test_portfolio_std(book_data):
    cov, corr = book_data
    w_h, w_c = c.hrp(cov, corr, tilde=True), c.cla_min_variance(cov)
    assert np.sqrt(w_h @ cov @ w_h) == pytest.approx(0.4640, abs=5e-5)  # p.232
    assert np.sqrt(w_c @ cov @ w_c) == pytest.approx(0.4486, abs=5e-5)


def test_linkage_on_d_is_not_the_book(book_data):
    """07 links on the condensed d; the book (Snippet 16.4 sch.linkage(dist, 'single')) on d~. On the book's data they differ."""
    cov, corr = book_data
    assert np.max(np.abs(c.hrp(cov, corr, tilde=False) - c.hrp(cov, corr, tilde=True))) > 0.05


def test_quasi_diag_clusters(book_data):
    import scipy.cluster.hierarchy as sch
    d = np.sqrt((1 - book_data[1]) / 2)
    np.fill_diagonal(d, 0)
    order = [i + 1 for i in c.M07.get_quasi_diag(sch.linkage(d, "single"))]
    assert order[:3] == [9, 2, 10] and order[3:5] == [1, 7] and order[5:7] == [3, 6]  # Figure 16.5 clusters


def test_nco_and_optport_minvar_agree_unconstrained():
    """MLAM s.7.6: with one cluster per item NCO reduces to Markowitz; check optPort is the analytic min-variance."""
    rng = np.random.default_rng(0)
    a = rng.normal(size=(500, 4))
    cov = np.cov(a, rowvar=False)
    w = c.opt_port(cov)
    inv = np.linalg.inv(cov)
    assert np.allclose(w, inv.sum(1) / inv.sum())
    assert np.isclose(w.sum(), 1.0)


# --------------------------------------------------------------------------- 07 as approved 2026-10-30 (s.4P.3)

def test_07_linkage_input_is_the_books(book_data):
    """07 builds the tree with linkage(pdist(D)); Snippet 16.4's sch.linkage(D, 'single') on the square D is the same tree."""
    import scipy.cluster.hierarchy as sch
    from scipy.spatial.distance import pdist
    d = np.sqrt((1 - book_data[1]) / 2)
    np.fill_diagonal(d, 0)
    assert np.allclose(sch.linkage(pdist(d, metric="euclidean"), method="single"), sch.linkage(d, "single"))


def _frame(x):
    import pandas as pd
    return pd.DataFrame(x, columns=[f"c{i}" for i in range(x.shape[1])])


def test_07_market_component_is_detoned():
    rng = np.random.default_rng(1)
    m = rng.normal(size=(2000, 1))
    r = c.M07.hrp_allocation(_frame(m + rng.normal(size=(2000, 10))), "single")["denoise_diagnostics"]
    assert r["applied"] and r["n_signal_eigenvalues"] >= 1
    assert r["first_eigenvector_same_sign_share"] >= 0.95 and r["market_component_exists"] and r["detoned"]


def test_07_two_equal_clusters_are_not_a_market():
    """A single-sample sign check can call this a market (the two top eigenvectors mix); the bootstrap must not."""
    rng = np.random.default_rng(2)
    f = rng.normal(size=(2000, 2))
    x = np.repeat(f, 5, axis=1) + rng.normal(size=(2000, 10))
    r = c.M07.hrp_allocation(_frame(x), "single")["denoise_diagnostics"]
    assert r["n_signal_eigenvalues"] == 2
    assert r["first_eigenvector_same_sign_share"] < 0.95 and not r["market_component_exists"] and not r["detoned"]


def test_07_pure_noise_gives_identity_and_ivp():
    """Snippets 2.4-2.5 with nFacts=0: every eigenvalue is replaced by their mean (1), so the denoised corr is I and HRP = IVP.
    lambda+ is asymptotic, so a finite pure-noise sample sometimes shows one eigenvalue above it (seeds 0-9 at T=2000,
    N=10: 1 in 10); seed 0 is one with none."""
    rng = np.random.default_rng(0)
    x = rng.normal(size=(2000, 10)) * np.linspace(0.5, 2.0, 10)
    out = c.M07.hrp_allocation(_frame(x), "single")
    assert out["denoise_diagnostics"]["n_signal_eigenvalues"] == 0 and not out["denoise_diagnostics"]["detoned"]
    assert np.allclose(out["corr"].to_numpy(), np.eye(10), atol=1e-12)
    ivp = 1.0 / np.diag(out["cov"].to_numpy())
    assert np.allclose(out["weights"].to_numpy(), ivp / ivp.sum(), atol=1e-12)


def test_07_bootstrap_share_matches_row_resampling():
    """The vectorised share equals resampling rows explicitly with the same draws."""
    rng = np.random.default_rng(4)
    x = rng.normal(size=(300, 4)) + rng.normal(size=(300, 1)) * 0.3
    n_boot, seed = 200, 0
    idx = np.random.default_rng(seed).integers(0, len(x), size=(n_boot, len(x)))
    same = []
    for i in idx:
        v = np.linalg.eigh(np.corrcoef(x[i], rowvar=False))[1][:, -1]
        same.append(bool(np.all(v > 0) or np.all(v < 0)))
    assert c.M07.first_eigenvector_same_sign_share(x, n_boot, seed) == pytest.approx(np.mean(same), abs=1e-12)


def _load_06():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "portfolio_allocator" / "06_build_representative_return_streams.py"
    spec = importlib.util.spec_from_file_location("m06_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_06_mark_to_market_booking_by_hand():
    """D059: daily mark-to-market at each UTC day's last bar close; costs on the exit day; sums to executed_r."""
    import pandas as pd
    m = _load_06()
    ts = lambda s: pd.Timestamp(s, tz="UTC").value
    bars = (np.array([ts("2024-01-01 23:00"), ts("2024-01-03 04:00")], dtype=np.int64), np.array([101.0, 103.0]))
    m.load_day_marks = lambda asset: bars
    ledger = pd.DataFrame({
        "representative_id": ["A", "B", "C"], "asset": "X", "side_value": [1, -1, 1],
        "entry_time": pd.to_datetime(["2024-01-01 10:00", "2024-01-02 01:00", "2024-01-01 23:30"], utc=True),
        "exit_time": pd.to_datetime(["2024-01-03 05:00", "2024-01-02 03:00", "2024-01-02 10:00"], utc=True),
        "entry_price": [100.0, 50.0, 100.0], "exit_price": [104.0, 49.0, 102.0], "stop_distance_price": [2.0, 1.0, 1.0],
        "executed_r": [1.9, 0.95, 2.0], "afml_bet_size": [0.5, 1.0, 1.0]})
    b, check = m.mark_to_market_bookings(ledger)
    got = {(r, str(d.date())): v for r, d, v in zip(b["representative_id"], b["booking_date"], b["return_raw_r"])}
    assert got == pytest.approx({("A", "2024-01-01"): 0.5, ("A", "2024-01-02"): 0.0, ("A", "2024-01-03"): 1.4,
                                 ("B", "2024-01-02"): 0.95, ("C", "2024-01-02"): 2.0})
    assert b.loc[b["representative_id"] == "A", "return_afml_weighted_r"].sum() == pytest.approx(0.95)
    assert check["max_abs_trade_sum_diff_vs_executed_r"] < 1e-12
