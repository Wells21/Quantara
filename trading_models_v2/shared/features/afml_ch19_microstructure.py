"""AFML Ch.19 "Microstructural Features" -- the High/Low estimators of s.19.3.3-19.3.4.

Source: Marcos Lopez de Prado, *Advances in Financial Machine Learning*
(AFML, 2018), Chapter 19, book p.283-286: the Parkinson (1980) high-low
volatility estimator (p.284, k1 form; no snippet printed) and Snippets
19.1-19.2 (Corwin & Schultz [2012] spread estimator and its volatility
byproduct, p.285-286), function for function (D071-D073).

Features (owner decision 2026-10-31, walkthrough s.4S.1):
- Parkinson_Vol_20 and Becker_Parkinson_Vol_1 are model features.
- corwin_schultz_spread is kept as the book's code but feeds NO feature
  (D072): its premise is that bar highs trade at the offer and lows at the
  bid (p.284), while our bars are built from MID quotes, so on our data it
  does not measure a spread (37-41 % zeros, rank correlation with the
  quoted spread -0.03..+0.11; logs/ch19_audit_evidence_20261031.txt).
- Snippet 19.2 is kept as printed; it uses k2 = sqrt(8/pi) where the
  Corwin-Schultz paper's eq.(12) uses sqrt(4 log 2), so with zero spread it
  returns 1.0435 sigma (a constant factor; D073).

Every other Ch.19 feature needs data we do not hold: the tick rule and the
Roll model need trade prices (s.19.3.1-19.3.2); Kyle's, Amihud's and
Hasbrouck's lambdas need signed traded volume (s.19.4); PIN/VPIN need
buy- and sell-initiated volume (s.19.5); s.19.6 needs trade, order-book or
options data. Our raw data are Bid/Ask quote ticks (Dukascopy), and the
bars carry no volume.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_LOG2 = np.log(2.0)
_K1_PARKINSON = 4.0 * _LOG2
_K2 = float(np.sqrt(8.0 / np.pi))
_DEN = 3.0 - 2.0 * np.sqrt(2.0)


class MicrostructureFeatureError(RuntimeError):
    """Fail-closed error for the AFML Ch.19 (High/Low subset) module."""


def parkinson_volatility(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """Parkinson (1980) high-low volatility estimator (book p.283-284):

        sigma_HL^2 = (1/(k1*T)) * sum_{t=1}^{T} (log[H_t/L_t])^2,  k1 = 4*log(2)

    estimated over a trailing `window` of bars, for continuously observed
    prices following geometric Brownian motion."""
    if (high < low).any():
        raise MicrostructureFeatureError("High must be >= Low for every bar.")
    log_hl_sq = np.log(high.astype("float64") / low.astype("float64")) ** 2
    mean_log_hl_sq = log_hl_sq.rolling(window=window).mean()
    return np.sqrt(mean_log_hl_sq / _K1_PARKINSON).rename(f"Parkinson_Vol_{window}")


def _get_beta(high: pd.Series, low: pd.Series, sample_length: int) -> pd.Series:
    """AFML Snippet 19.1's `getBeta` (book p.285): rolling-window estimate
    of beta_t = E[sum_{j=0}^{1} (log[H_{t-j}/L_{t-j}])^2]."""
    hl = np.log(high.astype("float64") / low.astype("float64")) ** 2
    rolling_sum_2 = hl.rolling(window=2).sum()
    return rolling_sum_2.rolling(window=sample_length).mean()


def _get_gamma(high: pd.Series, low: pd.Series) -> pd.Series:
    """AFML Snippet 19.1's `getGamma` (book p.285): gamma_t =
    [log(H_{t-1,t}/L_{t-1,t})]^2 over the 2-bar window {t-1,t}."""
    h2 = high.rolling(window=2).max()
    l2 = low.rolling(window=2).min()
    return (np.log(h2.astype("float64") / l2.astype("float64")) ** 2).rename("gamma")


def _get_alpha(beta: pd.Series, gamma: pd.Series) -> pd.Series:
    """AFML Snippet 19.1's `getAlpha` (book p.285): alpha_t, with negative
    values set to 0 per Corwin & Schultz (2012), p.727. Warm-up rows stay
    NaN, as the book's `alpha[alpha<0]=0` leaves them (D071)."""
    den = 3.0 - 2.0 * np.sqrt(2.0)
    alpha = (np.sqrt(2.0) * np.sqrt(beta) - np.sqrt(beta)) / den
    alpha = alpha - np.sqrt(gamma / den)
    return alpha.mask(alpha < 0.0, 0.0).rename("alpha")


def corwin_schultz_spread(high: pd.Series, low: pd.Series, sample_length: int = 1) -> pd.Series:
    """AFML Snippet 19.1 (book p.285): Corwin & Schultz (2012) high-low
    bid-ask spread estimator,

        S_t = 2*(exp(alpha_t)-1) / (1+exp(alpha_t))

    `sample_length` is the book's `sl` -- the rolling window used to
    estimate beta_t (default 1, matching the book's own default).
    Not a model feature: on mid-quote bars it is not a spread (D072)."""
    beta = _get_beta(high, low, sample_length)
    gamma = _get_gamma(high, low)
    alpha = _get_alpha(beta, gamma)
    spread = 2.0 * (np.exp(alpha) - 1.0) / (1.0 + np.exp(alpha))
    return spread.rename(f"Corwin_Schultz_Spread_{sample_length}")


def becker_parkinson_volatility(high: pd.Series, low: pd.Series, sample_length: int = 1) -> pd.Series:
    """AFML Snippet 19.2 (book p.286): the Becker-Parkinson volatility
    byproduct of the Corwin-Schultz decomposition (beta/gamma already
    absorb the bid-ask-spread component that the classic Parkinson
    estimator above does not). As printed: k2 where the paper has
    sqrt(4 log 2), i.e. 1.0435 sigma at zero spread (D073). Warm-up rows
    stay NaN (D071)."""
    beta = _get_beta(high, low, sample_length)
    gamma = _get_gamma(high, low)
    sigma = (2.0**-0.5 - 1.0) * np.sqrt(beta) / (_K2 * _DEN)
    sigma = sigma + np.sqrt(gamma / (_K2**2 * _DEN))
    sigma = sigma.mask(sigma < 0.0, 0.0)
    return sigma.rename(f"Becker_Parkinson_Vol_{sample_length}")
