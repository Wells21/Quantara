"""AFML Ch.10 concurrency-aware bet sizing (ADDED 2026-10-17).

Lopez de Prado, Advances in Financial Machine Learning (2018), Chapter 10:
  * 10.2 (p.142), budgeting approach: "We compute the maximum number (or some other quantile) of concurrent long bets,
    max_i{c_i,l} ... Then we derive the bet size as m_t = c_t,l / max_i{c_i,l} ... The goal is that the maximum position
    is not reached before the last concurrent signal is triggered."
  * 10.3 (p.142-143): the size of each bet comes from the meta-model's predicted probability (Quantara's
    calibrated `afml_bet_size`, m_i in [0, 1]).
  * 10.4 (p.144), averaging active bets: "A more sensible approach is to average all sizes across all bets still active
    at a given point in time." A bet is active from its origin until its first barrier touch t1 (Chapter 3).

Quantara trades every signal as its own order with its own barriers (open positions are never resized), so the
book's continuous position q_t = Q * mean(m_active) is realised by giving each new signal i of an edge the slice

    risk_i = Q_edge * m_i / c_q(edge)

where Q_edge = base_portfolio_risk_budget_pct * hrp_weight(edge) is the edge's maximum position (AFML's Q) and c_q is
the q-quantile of that edge's concurrency c_t measured at each signal's arrival (10.2). With c_t bets open the edge's
exposure is Q_edge * (c_t / c_q) * mean(m_active): it equals the 10.4 average position Q_edge * mean(m_active) exactly
when c_t = c_q, scales linearly with concurrency below that (10.2), and the existing per-edge/total caps stop it
growing past Q_edge beyond the quantile. A quantile of 1.0 uses the sample maximum, as in the book's main formulation.

Concurrency definition (Chapter 3/4 convention used throughout AFML): signal j is active at time t when
entry_j <= t < exit_j; c_t at signal i's arrival counts every same-edge signal active at entry_i, including i itself.

The divisor is a pure function of the representative ledger (edge, entry, exit), so Steps 08, 08C, 09, 10, 11 and
portfolio_live/00 all derive identical values from the same ledger. A quantile of 0 / None means OFF: divisor 1.0 for
every edge, which reproduces the original direct-weight sizing exactly.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import numpy as np

SIZING_MODE_DIRECT = "direct_weight_calibrated_afml"
SIZING_MODE_AFML_CONCURRENCY = "afml_concurrency_budget_ch10_2_10_4"
_CACHE: Dict[tuple, Dict[int, float]] = {}


def is_enabled(quantile: Any) -> bool:
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(q) and q > 0.0)


def validate_quantile(quantile: Any) -> float:
    """0.0 = off; otherwise must lie in (0, 1]. None / NaN / blank (older artifacts without the field) -> 0.0 (off)."""
    if quantile is None or (isinstance(quantile, str) and not quantile.strip()):
        return 0.0
    q = float(quantile)
    if np.isnan(q):
        return 0.0
    if not np.isfinite(q) or q < 0.0 or q > 1.0:
        raise ValueError(f"afml_concurrency_quantile must be 0 (off) or in (0, 1]; got {quantile!r}")
    return q


def concurrency_at_arrival(edge_code: np.ndarray, entry_ns: np.ndarray, exit_ns: np.ndarray) -> np.ndarray:
    """c_t per signal: same-edge signals active at the signal's entry (entry_j <= t < exit_j), itself included."""
    edge_code = np.asarray(edge_code)
    entry_ns = np.asarray(entry_ns, dtype=np.int64)
    exit_ns = np.asarray(exit_ns, dtype=np.int64)
    out = np.zeros(len(edge_code), dtype=np.int64)
    for e in np.unique(edge_code):
        idx = np.flatnonzero(edge_code == e)
        en = np.sort(entry_ns[idx])
        ex = np.sort(exit_ns[idx])
        t = entry_ns[idx]
        started = np.searchsorted(en, t, side="right")   # entry_j <= t
        ended = np.searchsorted(ex, t, side="right")     # exit_j <= t  -> no longer active
        out[idx] = started - ended
    return out


def divisors_by_edge(edge_code: np.ndarray, entry_ns: np.ndarray, exit_ns: np.ndarray, quantile: Any) -> Dict[int, float]:
    """c_q per edge code (>= 1). Quantile 0/None -> 1.0 for every edge (sizing unchanged)."""
    codes = [int(c) for c in np.unique(np.asarray(edge_code))]
    if not is_enabled(quantile):
        return {c: 1.0 for c in codes}
    q = validate_quantile(quantile)
    key = (q, len(edge_code), int(np.asarray(entry_ns, dtype=np.int64).sum() % (2**61)), int(np.asarray(exit_ns, dtype=np.int64).sum() % (2**61)),
           tuple(codes))
    if key in _CACHE:
        return dict(_CACHE[key])
    c = concurrency_at_arrival(edge_code, entry_ns, exit_ns)
    edge_code = np.asarray(edge_code)
    out: Dict[int, float] = {}
    for e in codes:
        ce = c[edge_code == e]
        out[e] = float(max(1.0, np.quantile(ce, q, method="higher"))) if len(ce) else 1.0
    _CACHE[key] = dict(out)
    return out


def divisor_array(edge_code: np.ndarray, divisors: Mapping[int, float]) -> np.ndarray:
    return np.array([float(divisors.get(int(e), 1.0)) for e in np.asarray(edge_code)], dtype=float)


def scaled_afml_size(arr: Mapping[str, Any], quantile: Any) -> np.ndarray:
    """afml_bet_size / c_q(edge) for every trade in a Step 08-11 array dict (needs edge_code, entry_ns, exit_ns,
    afml_bet_size). Off -> afml_bet_size unchanged."""
    m = np.asarray(arr["afml_bet_size"], dtype=float)
    if not is_enabled(quantile):
        return m
    d = divisors_by_edge(arr["edge_code"], arr["entry_ns"], arr["exit_ns"], quantile)
    return m / divisor_array(arr["edge_code"], d)


_ARR_CACHE: Dict[tuple, np.ndarray] = {}


def divisor_for_arr(arr: Mapping[str, Any], quantile: Any) -> Optional[np.ndarray]:
    """Per-trade c_q array for a Step 09/10/11 simulation array dict, or None when sizing is off (callers then skip the
    division entirely, so non-AFML variants stay bit-for-bit identical). Cached per (data signature, quantile)."""
    q = validate_quantile(quantile)
    if not is_enabled(q):
        return None
    ec = np.asarray(arr["edge_code"])
    en = np.asarray(arr["entry_ns"], dtype=np.int64)
    ex = np.asarray(arr["exit_ns"], dtype=np.int64)
    key = (q, len(ec), int(en.sum() % (2**61)), int(ex.sum() % (2**61)), int(ec.astype(np.int64).sum()))
    if key not in _ARR_CACHE:
        _ARR_CACHE[key] = divisor_array(ec, divisors_by_edge(ec, en, ex, q))
    return _ARR_CACHE[key]


def sizing_mode(quantile: Any) -> str:
    return SIZING_MODE_AFML_CONCURRENCY if is_enabled(quantile) else SIZING_MODE_DIRECT


def divisors_by_edge_id(edge_ids: np.ndarray, entry_ns: np.ndarray, exit_ns: np.ndarray, quantile: Any) -> Dict[str, float]:
    """Same as divisors_by_edge but keyed by edge_id strings (for manifests / audit)."""
    ids = np.asarray(edge_ids).astype(str)
    uniq = sorted(set(ids.tolist()))
    code = np.array([uniq.index(x) for x in ids], dtype=np.int64)
    d = divisors_by_edge(code, entry_ns, exit_ns, quantile)
    return {uniq[k]: float(v) for k, v in d.items()}


def describe(quantile: Optional[float]) -> str:
    if not is_enabled(quantile):
        return "off (direct weight: base * hrp_weight * afml_bet_size, capped)"
    return (f"AFML 10.2/10.4: base * hrp_weight * afml_bet_size / c_q(edge), c_q = {float(quantile):g}-quantile of the "
            "edge's concurrent active signals at arrival")
