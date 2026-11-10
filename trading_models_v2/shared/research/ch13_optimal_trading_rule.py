"""AFML Ch.13 walkthrough (owner-approved 2026-10-26): optimal trading rule (OTR) from synthetic data, AFML s.13.4-13.5
and Snippets 13.1-13.2 (pp.172-176). Research period only (holdout sealed).

Step 1 (eq. 13.2, 13.5-13.7): each trade's mark-to-market P&L in R units, x_t = side (close_t - entry) / SL, observed at
  the edge's research bar closes from entry (x_0 = 0, the book's P_i,0 = 0) to the VERTICAL DEADLINE, ignoring our own
  barriers (fix 2026-10-26: a first version used only bars while the trade was open, i.e. paths truncated at the
  barriers, which biases the fit -- the closer stop cuts more downside and fakes an upward pull), is fitted to the
  discrete O-U process x_t = (1 - phi) E0 + phi x_(t-1) + sigma eps by pooled OLS over consecutive pairs within trades.
  Quantara's strategy gives a probability, not a price forecast E0[P_i,T] per trade, so ONE common E0 (in R) is estimated:
  phi_hat = cov[Y, X] / cov[X, X] (the book's estimator; a common E0 does not change it), E0_hat = intercept / (1 - phi_hat),
  sigma_hat = std of the residual. Half-life tau = -log 2 / log phi (p.174).
Null check (data decides, s.13.6: near a random walk no rule is optimal): the same estimator on random walks with the
  same path lengths, drift and step volatility (200 replications) -> the null distribution of phi_hat; report where the
  real phi_hat falls.
Step 2: meshes of (PT, SL): the book's 20 x 20 at 0.5 .. 10 sigma_hat (Snippet 13.2 default), and the owner-approved R
  mesh PT 0.25 .. 6 R x SL 0.25 .. 4 R (step 0.25); the current rule (PT 2 R, SL 1 R) is evaluated exactly.
Step 3: 100,000 synthetic paths with (phi_hat, sigma_hat, E0_hat), seed 0, maximum holding period maxHP = the median number
  of research bars from entry to the vertical deadline.
Step 4: Snippet 13.2 logic on every node: p = (1 - phi) forecast + phi p + sigma N(0,1); exit at the first step with
  p > PT or p < -SL or hp > maxHP, at the value reached; Sharpe = mean / std of the exits (eq. 13.1, np.std ddof 0).
Step 5: (5a) the optimal node; (5b) the optimal SL given PT = 2 R; (5c) the optimal PT given SL <= 1 R.
Also with costs: the population's mean Moneta cost per trade in R (label_r - executed_r) subtracted from every exit (the book
plots without costs, p.176). Populations: approved trades (live exits) and all primary events (labels).

usage (from trading_models_v2, venv312):
  python -m shared.research.ch13_optimal_trading_rule --edge HK/drawdownhazardcontinuation_short --out <json>
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from shared.research.ch10_intrade_forecast_test import _ts, event_mask

V2 = Path(__file__).resolve().parents[2]
N_PATHS = 100_000
SEED = 0
CURRENT = (2.0, 1.0)


def ou_ols(paths: list[np.ndarray]) -> dict:
    x = np.concatenate([q[:-1] for q in paths]); y = np.concatenate([q[1:] for q in paths])
    phi = float(np.cov(y, x, bias=True)[0, 1] / np.var(x))
    c = float(y.mean() - phi * x.mean())
    sigma = float(np.sqrt(np.var(y - c - phi * x)))
    e0 = c / (1.0 - phi) if abs(1.0 - phi) > 1e-12 else float("nan")
    tau = float(-np.log(2) / np.log(phi)) if 0 < phi < 1 else float("nan")
    return {"phi": phi, "intercept": c, "sigma": sigma, "E0_R": e0, "half_life_bars": tau, "n_pairs": int(len(x))}


def full_paths(ev: pd.DataFrame, bar_t: np.ndarray, close: np.ndarray) -> list[np.ndarray]:
    """x_0 = 0 at entry, then every research bar close up to the vertical deadline (our barriers ignored)."""
    entry_mid = ((ev.entry_bid + ev.entry_ask) / 2).to_numpy(float)
    side, sl = ev.side.to_numpy(float), ev.stop_distance_price.to_numpy(float)
    lo = np.searchsorted(bar_t, _ts(ev.entry_time), side="right")
    hi = np.searchsorted(bar_t, _ts(ev.vertical_deadline), side="right")
    return [np.concatenate(([0.0], side[i] * (close[lo[i]:hi[i]] - entry_mid[i]) / sl[i])) for i in range(len(ev)) if hi[i] - lo[i] >= 2]


def random_walk_null(paths: list[np.ndarray], reps: int = 200, seed: int = SEED) -> dict:
    inc = np.concatenate([np.diff(q) for q in paths])
    mu, sd = float(inc.mean()), float(inc.std())
    lens = [len(q) for q in paths]
    rng = np.random.default_rng(seed)
    phis = []
    for _ in range(reps):
        sim = [np.concatenate(([0.0], np.cumsum(mu + sd * rng.standard_normal(n - 1)))) for n in lens]
        phis.append(ou_ols(sim)["phi"])
    phis = np.array(phis)
    return {"rw_drift_per_bar_R": mu, "rw_step_sd_R": sd, "null_phi_mean": float(phis.mean()),
            "null_phi_p2_5": float(np.percentile(phis, 2.5)), "null_phi_p97_5": float(np.percentile(phis, 97.5)), "reps": reps}


def simulate(phi: float, sigma: float, e0: float, max_hp: int, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    p = np.zeros((N_PATHS, max_hp + 1))
    cur = np.zeros(N_PATHS)
    for t in range(max_hp + 1):
        cur = (1 - phi) * e0 + phi * cur + sigma * rng.standard_normal(N_PATHS)
        p[:, t] = cur
    return p


def first_cross(p: np.ndarray, levels: np.ndarray, above: bool) -> np.ndarray:
    """(len(levels), N) first index where p > level (above) or p < -level (below); n_steps if never."""
    n = p.shape[1]
    out = np.empty((len(levels), p.shape[0]), dtype=np.int32)
    for k, lv in enumerate(levels):
        hit = p > lv if above else p < -lv
        any_ = hit.any(axis=1)
        out[k] = np.where(any_, hit.argmax(axis=1), n)
    return out


def surface(p: np.ndarray, pts: np.ndarray, sls: np.ndarray, cost: float) -> np.ndarray:
    n = p.shape[1]
    tp, ts = first_cross(p, pts, True), first_cross(p, sls, False)
    rows = np.arange(p.shape[0])
    sr = np.empty((len(pts), len(sls)))
    for i in range(len(pts)):
        for j in range(len(sls)):
            k = np.minimum(np.minimum(tp[i], ts[j]), n - 1)  # hp > maxHP -> exit at the last step
            v = p[rows, k] - cost
            sd = v.std()
            sr[i, j] = v.mean() / sd if sd > 0 else np.nan
    return sr


def summarise(sr: np.ndarray, pts: np.ndarray, sls: np.ndarray, cur_sr: float) -> dict:
    i, j = np.unravel_index(np.nanargmax(sr), sr.shape)
    flat = sr[np.isfinite(sr)]
    return {"otr_PT": float(pts[i]), "otr_SL": float(sls[j]), "otr_sharpe": float(sr[i, j]),
            "median_sharpe": float(np.median(flat)), "flatness_best_minus_median": float(sr[i, j] - np.median(flat)),
            "current_rule_sharpe": cur_sr, "current_rule_percentile": float((flat < cur_sr).mean())}


def run_population(asset: str, edge: str, approved: bool) -> dict:
    root = V2 / "assets" / asset / "edges" / edge
    cal = next((root / "artifacts" / "calibration").glob("*_calibrated_oof_predictions.csv"))
    ev = pd.read_csv(cal, usecols=["entry_time", "vertical_deadline", "label_r", "executed_r", "execution_status", "oof_prob_raw",
                                   "live_calibrated_probability", "afml_bet_size", "side", "entry_bid", "entry_ask",
                                   "stop_distance_price"])
    ev = ev[event_mask(ev, f"{asset}/{edge}", approved)].reset_index(drop=True)
    fm = pd.read_csv(next((root / "artifacts" / "research_dataset").glob("*_feature_matrix.csv")), usecols=["Datetime", "Close"])
    fm = fm.assign(t_ns=_ts(fm.Datetime)).sort_values("t_ns")
    bar_t, close = fm.t_ns.to_numpy(), fm.Close.to_numpy(float)
    paths = full_paths(ev, bar_t, close)
    ou = {**ou_ols(paths), "n_trades": len(paths)}
    null = random_walk_null(paths)
    info = {"n_events": int(len(ev)), "n_paths": len(paths)}
    bars_to_vertical = np.searchsorted(bar_t, _ts(ev.vertical_deadline), side="right") - np.searchsorted(bar_t, _ts(ev.entry_time), side="right")
    max_hp = int(np.median(bars_to_vertical))
    cost = float((ev.label_r - ev.executed_r).mean())
    realised = ev.label_r.to_numpy(float)
    p = simulate(ou["phi"], ou["sigma"], ou["E0_R"], max_hp)
    out = {"population": "approved" if approved else "all_primary_events", "rows": info, "ou": ou, "random_walk_null": null,
           "phi_below_random_walk_null": bool(ou["phi"] < null["null_phi_p2_5"]), "maxHP_bars": max_hp,
           "mean_cost_R": cost, "realised_mid_R_mean": float(realised.mean()), "realised_mid_R_sharpe": float(realised.mean() / realised.std())}
    s = ou["sigma"]
    book = np.linspace(0.5, 10, 20) * s
    rmesh_pt, rmesh_sl = np.arange(0.25, 6.0001, 0.25), np.arange(0.25, 4.0001, 0.25)
    for tag, c in (("no_cost", 0.0), ("with_cost", cost)):
        cur = float(surface(p, np.array([CURRENT[0]]), np.array([CURRENT[1]]), c)[0, 0])
        sb = surface(p, book, book, c)
        sr = surface(p, rmesh_pt, rmesh_sl, c)
        j1 = int(np.argmin(np.abs(rmesh_pt - CURRENT[0])))
        k5b = int(np.nanargmax(sr[j1]))
        mask_sl = rmesh_sl <= CURRENT[1] + 1e-12
        sub = sr[:, mask_sl]
        i5c, j5c = np.unravel_index(np.nanargmax(sub), sub.shape)
        out[tag] = {
            "book_mesh_sigma_multiples": {**summarise(sb, book, book, cur), "otr_PT_sigma": float(book[np.unravel_index(np.nanargmax(sb), sb.shape)[0]] / s),
                                          "otr_SL_sigma": float(book[np.unravel_index(np.nanargmax(sb), sb.shape)[1]] / s),
                                          "mesh_max_R": float(book[-1]), "current_rule_in_sigma": [CURRENT[0] / s, CURRENT[1] / s]},
            "R_mesh": {**summarise(sr, rmesh_pt, rmesh_sl, cur),
                       "step5b_best_SL_given_PT_2R": float(rmesh_sl[k5b]), "step5b_sharpe": float(sr[j1, k5b]),
                       "step5c_best_PT_given_SL_le_1R": float(rmesh_pt[i5c]), "step5c_SL": float(rmesh_sl[mask_sl][j5c]),
                       "step5c_sharpe": float(sub[i5c, j5c])},
            "R_mesh_surface": {"PT": rmesh_pt.tolist(), "SL": rmesh_sl.tolist(), "sharpe": np.round(sr, 5).tolist()},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    asset, edge = a.edge.split("/")
    t0 = time.time()
    res = {"edge": a.edge, "test": "AFML Ch.13 OTR from synthetic O-U paths (Snippets 13.1-13.2), research period only",
           "n_paths": N_PATHS, "seed": SEED, "current_rule_R": list(CURRENT)}
    for approved in (True, False):
        r = run_population(asset, edge, approved)
        res[r["population"]] = r
        print(f"[{a.edge}] {r['population']}: ou {r['ou']} maxHP {r['maxHP_bars']} | no_cost R-mesh {r['no_cost']['R_mesh']} "
              f"| book-mesh {r['no_cost']['book_mesh_sigma_multiples']}", flush=True)
    res["elapsed_s"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
