"""AFML Snippets 19.1-19.2 (book p.285-286) as printed, for the Ch.19 audit.

Only two changes, both forced by the language, not by the method:
- pd.stats.moments.rolling_sum / rolling_mean / rolling_max / rolling_min (removed from pandas in 0.23)
  -> Series.rolling(window).sum() / .mean() / .max() / .min(), which compute the same numbers;
- the en-dashes the book typesets in `3–2*2**.5`, `(2**.5–1)` and `(2**-.5–1)` -> minus signs.
"""
import numpy as np
import pandas as pd


def getBeta(series, sl):
    hl = series[['High', 'Low']].values
    hl = np.log(hl[:, 0] / hl[:, 1]) ** 2
    hl = pd.Series(hl, index=series.index)
    beta = hl.rolling(window=2).sum()
    beta = beta.rolling(window=sl).mean()
    return beta.dropna()


def getGamma(series):
    h2 = series['High'].rolling(window=2).max()
    l2 = series['Low'].rolling(window=2).min()
    gamma = np.log(h2.values / l2.values) ** 2
    gamma = pd.Series(gamma, index=h2.index)
    return gamma.dropna()


def getAlpha(beta, gamma):
    den = 3 - 2 * 2 ** .5
    alpha = (2 ** .5 - 1) * (beta ** .5) / den
    alpha -= (gamma / den) ** .5
    alpha[alpha < 0] = 0  # set negative alphas to 0 (see p.727 of paper)
    return alpha.dropna()


def corwinSchultz(series, sl=1):
    # Note: S<0 iif alpha<0
    beta = getBeta(series, sl)
    gamma = getGamma(series)
    alpha = getAlpha(beta, gamma)
    spread = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    startTime = pd.Series(series.index[0:spread.shape[0]], index=spread.index)
    spread = pd.concat([spread, startTime], axis=1)
    spread.columns = ['Spread', 'Start_Time']  # 1st loc used to compute beta
    return spread


def getSigma(beta, gamma):
    k2 = (8 / np.pi) ** .5
    den = 3 - 2 * 2 ** .5
    sigma = (2 ** -.5 - 1) * beta ** .5 / (k2 * den)
    sigma += (gamma / (k2 ** 2 * den)) ** .5
    sigma[sigma < 0] = 0
    return sigma
