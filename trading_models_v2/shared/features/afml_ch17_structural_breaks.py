"""AFML Ch.17 "Structural Breaks" -- Supremum Augmented Dickey-Fuller (SADF).

Source: Marcos Lopez de Prado, *Advances in Financial Machine Learning*
(AFML, 2018), Chapter 17, book p.249-262. This module ports Snippets
17.1-17.4 (book p.258-259) function-for-function into readable Python; the
regression mechanics (get_betas), the lag/level design matrix construction
(get_yx, lag_df) and the backward-expanding-window inner loop (get_bsadf)
are unchanged from the book's own pseudocode.

    SADF_t = sup_{t0 in [1, t-tau]} { ADF_{t0,t} } = sup_{t0 in [1, t-tau]} { beta_hat_{t0,t} / sigma_hat_{beta_{t0,t}} }

fit on the regression specification (book p.252-253, p.258):

    delta_y_t = alpha + beta * y_{t-1} + sum_{l=1}^{L} gamma_l * delta_y_{t-l} + eps_t

This is deliberately run on LOG prices (book p.254: the log-price
specification conditions returns' *mean*, not their *volatility*, on price
levels -- the raw-price specification implicitly assumes returns'
volatility is invariant to price level, which is the wrong assumption for
a series spanning multiple regimes/orders of magnitude).

BOUNDED WINDOW -- A REGISTERED DEVIATION (D062, owner approved 2026-10-30):
the book searches t0 over [1, t - tau], the whole sample (p.253). Here t0
ranges over the last `lookback` bars only, so the supremum is taken over a
different set: a rolling-window SADF, not the book's statistic. Reasons: the
book's own cost analysis (p.254-255, Table 17.1: a full SADF series is
O(T^3); HPC / Chapter 20) and live parity (live holds a bounded bar window,
so a whole-sample start point cannot be reproduced). Each ADF_t0,t inside the
window is the book's regression exactly (Snippets 17.1-17.4).

EVERY BAR, CURRENT BAR (D061, owner approved 2026-10-30): the book computes
SADF_t at each t in [tau, T] (p.253). `structural_break_features` evaluates
every bar on the window ending at that bar; research (feature_expansion.py)
and live (native_one_row_feature_engine.py) use the same rule. The former
stride-10 forward-fill made research rows 0-9 bars stale while live was
always 9 bars stale (AFML_MLAM_WALKTHROUGH.md s.4Q finding 1).

QADF / CADF (D063, s.17.4.2.4-17.4.2.5 pp.255-256): the book's robustness
measures of the same set s_t = {ADF_t0,t} that SADF takes the supremum of.
CSW (s.17.3.2) and SMT (s.17.4.3) (D064, owner approved 2026-10-30) on the same
50-bar window; see `csw` and `smt` for the choices the book leaves open.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


class StructuralBreakError(RuntimeError):
    """Fail-closed error for the SADF structural-break module."""


def lag_df(df0: pd.DataFrame, lags: "int | Sequence[int]") -> pd.DataFrame:
    """AFML Snippet 17.3 (book p.259): apply the given lags to a dataframe,
    joining each shifted-and-renamed copy into one output frame."""
    if isinstance(lags, int):
        lag_values = range(lags + 1)
    else:
        lag_values = [int(lag) for lag in lags]
    df1 = pd.DataFrame(index=df0.index)
    for lag in lag_values:
        shifted = df0.shift(lag).copy(deep=True)
        shifted.columns = [f"{col}_{lag}" for col in shifted.columns]
        df1 = df1.join(shifted, how="outer")
    return df1


def get_yx(series: pd.Series, constant: str, lags: int) -> tuple[np.ndarray, np.ndarray]:
    """AFML Snippet 17.2 (book p.258): build the (y, x) design for the ADF
    regression `delta_y_t = alpha + beta*y_{t-1} + sum gamma_l*delta_y_{t-l} + eps_t`.
    Column 0 of x is overwritten with the LAGGED LEVEL of the original
    series (the beta_hat coefficient of interest); the remaining lag
    columns hold lagged first differences (the gamma_l coefficients)."""
    series_diff = series.diff().dropna()
    x = lag_df(series_diff.to_frame(), lags).dropna()
    x_values = x.to_numpy(dtype="float64").copy()
    x_values[:, 0] = series.to_numpy()[-x_values.shape[0] - 1 : -1]  # lagged level
    y = series_diff.iloc[-x_values.shape[0] :].to_numpy()
    if constant != "nc":
        x_values = np.append(x_values, np.ones((x_values.shape[0], 1)), axis=1)
        if constant[:2] == "ct":
            trend = np.arange(x_values.shape[0], dtype="float64").reshape(-1, 1)
            x_values = np.append(x_values, trend, axis=1)
        if constant == "ctt":
            x_values = np.append(x_values, trend**2, axis=1)
    return y.astype("float64"), x_values


def get_betas(y: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """AFML Snippet 17.4 (book p.259): OLS fit of the ADF regression."""
    xy = x.T @ y
    xx = x.T @ x
    xx_inv = np.linalg.inv(xx)
    b_mean = xx_inv @ xy
    err = y - x @ b_mean
    b_var = (err.T @ err) / (x.shape[0] - x.shape[1]) * xx_inv
    return b_mean, b_var


def get_adf_stats(log_p: pd.Series, min_sl: int, constant: str, lags: int) -> np.ndarray:
    """s_t = {ADF_t0,t}: AFML Snippet 17.1's (book p.258) allADF list -- one ADF
    t-value per backward-expanding start point, for the end point t =
    `log_p.index[-1]`. Snippet 17.1's start points give a final regression of
    minSL - lags rows. A start point whose regression has no degrees of freedom
    (rows <= columns), or a non-finite / non-positive variance, is skipped (the
    book's snippet would divide by zero there)."""
    y, x = get_yx(log_p, constant=constant, lags=lags)
    start_points = range(0, y.shape[0] + lags - min_sl + 1)
    all_adf: list[float] = []
    for start in start_points:
        y_, x_ = y[start:], x[start:]
        if x_.shape[0] <= x_.shape[1]:
            continue
        b_mean, b_var = get_betas(y_, x_)
        b_mean_0, b_var_00 = float(b_mean[0]), float(b_var[0, 0])
        # check the variance BEFORE the square root: a negative float ** 0.5 is complex in Python and broke the
        # comparison below (near-singular flat windows in the extended history, 2026-11-09). Same skip as documented.
        if not np.isfinite(b_mean_0) or not np.isfinite(b_var_00) or b_var_00 <= 0.0:
            continue
        b_std_0 = b_var_00 ** 0.5
        all_adf.append(b_mean_0 / b_std_0)
    return np.asarray(all_adf, dtype="float64")


def get_bsadf(log_p: pd.Series, min_sl: int, constant: str, lags: int) -> float:
    """AFML Snippet 17.1 (book p.258): SADF's inner loop -- the supremum of s_t
    over the backward-expanding start points for one fixed end point t (t is
    implicitly `log_p.index[-1]`; `min_sl` is tau)."""
    s = get_adf_stats(log_p, min_sl=min_sl, constant=constant, lags=lags)
    return float(s.max()) if s.size else -np.inf


def qadf(s: np.ndarray, q: float = 0.95, v: float = 0.025) -> tuple[float, float]:
    """AFML s.17.4.2.4 (p.255): Q_t,q = Q[s_t, q], the q quantile of s_t (centrality
    of high ADF values), and Q._t,q,v = Q_t,q+v - Q_t,q-v (dispersion), 0 < v <=
    min{q, 1 - q}; the book's example q = 0.95, v = 0.025. The book names no
    quantile estimator: numpy's default (linear interpolation) is used."""
    if not 0.0 < v <= min(q, 1.0 - q):
        raise StructuralBreakError(f"QADF needs 0 < v <= min(q, 1 - q); got q={q}, v={v}.")
    if s.size == 0:
        return np.nan, np.nan
    q_c = float(np.quantile(s, q))
    return q_c, float(np.quantile(s, q + v) - np.quantile(s, q - v))


def cadf(s: np.ndarray, q: float = 0.95) -> tuple[float, float]:
    """AFML s.17.4.2.5 (p.256): C_t,q = K^-1 int_{Q_t,q}^inf x f[x] dx and
    C._t,q = sqrt(K^-1 int_{Q_t,q}^inf (x - C_t,q)^2 f[x] dx), K = int_{Q_t,q}^inf
    f[x] dx; the book's example q = 0.95. f is the empirical distribution of s_t,
    so C_t,q is the mean and C._t,q the (population) standard deviation of the
    values of s_t at or above Q_t,q (Q_t,q as in `qadf`). C_t,q <= SADF_t."""
    if s.size == 0:
        return np.nan, np.nan
    tail = s[s >= np.quantile(s, q)]
    c = float(tail.mean())
    return c, float(np.sqrt(np.mean((tail - c) ** 2)))


LOG_EPS = 1e-12
SMT_SPECS = ("Poly1", "Poly2", "Exp", "Power")
SMT_PHIS = (0.0, 0.5, 1.0)


def _phi_tag(phi: float) -> str:
    return {0.0: "phi0", 0.5: "phi05", 1.0: "phi1"}.get(float(phi), f"phi{phi:g}".replace(".", ""))


STRUCTURAL_BREAK_COLUMNS = (
    ("SADF", "QADF", "QADF_Dispersion", "CADF", "CADF_Dispersion", "CSW_Up", "CSW_Down")
    + tuple(f"SMT_{spec}_{_phi_tag(phi)}" for spec in SMT_SPECS for phi in SMT_PHIS)
)


def csw(log_window: np.ndarray) -> tuple[float, float]:
    """AFML s.17.3.2 (p.251), Chu-Stinchcombe-White CUSUM on log-price levels, on the window y_1..y_t:
    sigma_t^2 = (t - 1)^-1 sum_{i=2}^{t} (Delta y_i)^2, S_n,t = (y_t - y_n) / (sigma_t sqrt(t - n)),
    S_t = sup over n. n = t is excluded (0 / 0). Returns (sup S_n,t on log y, the same on -log y = log(1/y)):
    the test is one-sided, so bursts need the reciprocal (exercise 17.5). NaN if sigma_t = 0."""
    y = np.asarray(log_window, dtype="float64")
    t = y.size
    sigma = float(np.sqrt(np.sum(np.diff(y) ** 2) / (t - 1)))
    if not np.isfinite(sigma) or sigma <= 0.0:
        return np.nan, np.nan
    s = (y[-1] - y[:-1]) / (sigma * np.sqrt(np.arange(t - 1, 0, -1, dtype="float64")))
    return float(s.max()), float((-s).max())


_SMT_DESIGN_CACHE: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}


def _smt_design(spec: str, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Regressors on sample-relative time s = 1..n (the trend starts at the sample's own start t0; the book fixes no
    origin -- it matters only for Power's log s) and (X'X)^-1; the coefficient of interest (beta) is the LAST column."""
    key = (spec, n)
    if key not in _SMT_DESIGN_CACHE:
        s = np.arange(1, n + 1, dtype="float64")
        if spec in ("Poly1", "Poly2"):
            x = np.column_stack([np.ones(n), s, s ** 2])
        elif spec == "Exp":
            x = np.column_stack([np.ones(n), s])
        elif spec == "Power":
            x = np.column_stack([np.ones(n), np.log(s)])
        else:
            raise StructuralBreakError(f"unknown SMT spec {spec!r}")
        _SMT_DESIGN_CACHE[key] = (x, np.linalg.inv(x.T @ x))
    return _SMT_DESIGN_CACHE[key]


def smt(close_window: np.ndarray, *, min_sl: int, phis: Sequence[float] = SMT_PHIS) -> dict[str, float]:
    """AFML s.17.4.3 (pp.259-261), sub- / super-martingale tests: for each start point t0 in [1, t - tau] (tau =
    `min_sl`; samples of t - t0 + 1 = n >= tau + 1 points) fit
        SM-Poly1  y_t      = alpha + gamma t + beta t^2
        SM-Poly2  log y_t  = alpha + gamma t + beta t^2
        SM-Exp    log y_t  = log alpha + beta t        (linearised y = alpha e^(beta t), as printed)
        SM-Power  log y_t  = log alpha + beta log t    (linearised y = alpha t^beta, as printed)
    by OLS (sigma_beta^2 = SSE / (n - k) (X'X)^-1, Snippet 17.4's estimator) and take
    SMT_t = sup_t0 |beta| / (sigma_beta (t - t0)^phi) for each phi (phi = 0 is the unpenalised SMT_t)."""
    price = np.asarray(close_window, dtype="float64")
    logp = np.log(price + LOG_EPS)
    t = price.size
    out = {f"SMT_{spec}_{_phi_tag(phi)}": -np.inf for spec in SMT_SPECS for phi in phis}
    for t0 in range(0, t - min_sl):           # 0-based start; t - t0 - 1 = (t - t0) in the book's 1-based terms
        n = t - t0
        for spec in SMT_SPECS:
            y = price[t0:] if spec == "Poly1" else logp[t0:]
            x, xx_inv = _smt_design(spec, n)
            b = xx_inv @ (x.T @ y)
            err = y - x @ b
            var_b = float((err @ err) / (n - x.shape[1]) * xx_inv[-1, -1])
            if not np.isfinite(var_b) or var_b <= 0.0:
                continue
            stat = abs(float(b[-1])) / np.sqrt(var_b)
            for phi in phis:
                key = f"SMT_{spec}_{_phi_tag(phi)}"
                out[key] = max(out[key], stat / float(n - 1) ** phi)
    return {k: (v if np.isfinite(v) else np.nan) for k, v in out.items()}


def structural_break_row(
    close_window: "pd.Series | np.ndarray", *, min_sl: int, constant: str, lags: int, q: float = 0.95, v: float = 0.025
) -> dict[str, float]:
    """All Ch.17 features for the end point t = the last bar of `close_window` (raw closes): SADF_t, QADF (Q_t,q,
    Q._t,q,v) and CADF (C_t,q, C._t,q) from one s_t on log(close + 1e-12); CSW up / down; SMT for the 4 specs x phi."""
    price = np.asarray(close_window, dtype="float64")
    logp = np.log(price + LOG_EPS)
    s = get_adf_stats(pd.Series(logp), min_sl=min_sl, constant=constant, lags=lags)
    q_c, q_d = qadf(s, q=q, v=v)
    c_c, c_d = cadf(s, q=q)
    up, down = csw(logp)
    row = {"SADF": float(s.max()) if s.size else np.nan, "QADF": q_c, "QADF_Dispersion": q_d,
           "CADF": c_c, "CADF_Dispersion": c_d, "CSW_Up": up, "CSW_Down": down}
    row.update(smt(price, min_sl=min_sl))
    return row


def structural_break_features(
    close: pd.Series,
    *,
    min_sl: int = 10,
    constant: str = "ct",
    lags: int = 1,
    lookback: int = 50,
    q: float = 0.95,
    v: float = 0.025,
) -> pd.DataFrame:
    """All Ch.17 features at EVERY bar t, each on the `lookback` raw closes ending at t inclusive (D061: no stride,
    no lag); NaN until `lookback` closes exist or when the window has a non-finite or non-positive close. The live
    engine applies the same rule to the same window (see module docstring)."""
    if lookback <= min_sl + lags + 5:
        raise StructuralBreakError(
            f"lookback ({lookback}) must comfortably exceed min_sl+lags ({min_sl + lags})."
        )
    values = close.to_numpy(dtype="float64")
    out = np.full((len(values), len(STRUCTURAL_BREAK_COLUMNS)), np.nan, dtype="float64")
    for t in range(lookback - 1, len(values)):
        window = values[t - lookback + 1 : t + 1]
        if not (np.isfinite(window).all() and (window > 0).all()):
            continue
        try:
            row = structural_break_row(window, min_sl=min_sl, constant=constant, lags=lags, q=q, v=v)
        except np.linalg.LinAlgError:
            continue
        out[t] = [row[c] for c in STRUCTURAL_BREAK_COLUMNS]
    return pd.DataFrame(out, index=close.index, columns=list(STRUCTURAL_BREAK_COLUMNS))
