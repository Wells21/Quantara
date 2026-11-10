"""AFML s.18.8.3 (p.275-276) portfolio concentration in allocator Step 07 (D070, report only).

Run on venv39 (07 needs pandas < 3; see AFML_MLAM_WALKTHROUGH.md s.4P.2).
"""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "portfolio_allocator" / "07_hrp_linkage_robustness.py"
_SPEC = importlib.util.spec_from_file_location("step07_hrp", _PATH)
step07 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(step07)


def _returns(cov: np.ndarray, n: int = 200_000, seed: int = 0) -> pd.DataFrame:
    x = np.random.default_rng(seed).multivariate_normal(np.zeros(len(cov)), cov, size=n)
    return pd.DataFrame(x, columns=[f"e{i}" for i in range(len(cov))])


def _by_hand(returns: pd.DataFrame, w: np.ndarray) -> float:
    lam, vec = np.linalg.eigh(returns.cov().to_numpy())
    risk = (vec.T @ w) ** 2 * lam
    theta = risk / risk.sum()
    return float(np.exp(-np.sum(theta * np.log(theta))))


def test_equal_risk_shares_give_zero_concentration():
    # Exercise 18.5(a): theta_i = 1/N for every component -> exp(entropy) = N, H = 0.
    r = pd.DataFrame(np.vstack([np.eye(10), -np.eye(10)]), columns=[f"e{i}" for i in range(10)])  # V = c * I exactly
    out = step07.principal_component_concentration(r, pd.Series(0.1, index=r.columns))
    assert out["pc_effective_number_of_bets"] == pytest.approx(10.0, abs=1e-9)
    assert out["pc_meucci_concentration"] == pytest.approx(0.0, abs=1e-12)


def test_matches_the_book_formula_on_correlated_streams():
    cov = np.array([[1.0, 0.6, 0.1], [0.6, 2.0, 0.3], [0.1, 0.3, 0.5]])
    r = _returns(cov)
    w = np.array([0.5, 0.2, 0.3])
    out = step07.principal_component_concentration(r, pd.Series(w, index=r.columns))
    assert out["pc_effective_number_of_bets"] == pytest.approx(_by_hand(r, w), rel=1e-12)
    assert out["pc_meucci_concentration"] == pytest.approx(1 - _by_hand(r, w) / 3, rel=1e-12)


def test_single_uncorrelated_holding_is_one_bet():
    r = _returns(np.diag([1.0, 2.0, 3.0]), seed=1)
    out = step07.principal_component_concentration(r, pd.Series([0.0, 1.0, 0.0], index=r.columns))
    assert out["pc_effective_number_of_bets"] == pytest.approx(1.0, abs=0.01)


def test_report_only_not_in_score():
    src = _PATH.read_text(encoding="utf-8")
    score = src[src.index("def add_robustness_scores"):src.index("def build(")]
    warn = src[src.index("def concentration_metrics"):src.index("def principal_component_concentration")]
    assert "pc_" not in score and "pc_" not in warn
