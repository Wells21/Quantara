"""Drift baseline: edge events vs random same-side entries (logs/counterfactual_tests_20261031/PREREGISTRATION_DRIFT_BASELINE.md).

Reuses run_counterfactuals.py's edge path (own trigger fields, extraction, primary side, labeler; reproduction check).

usage (from trading_models_v2): python shared/research/counterfactual_tests/run_drift_baseline.py ASSET EDGE OUT_DIR
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[3]))
    from shared.io.timeparse import to_datetime_iso

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_counterfactuals as rc  # noqa: E402

V2 = rc.V2
SEED = 20261102
POOL = 20_000
DRAWS = 10_000


def matched_draws(edge_keys: np.ndarray, pool_keys: np.ndarray, pool_r: np.ndarray, rng) -> np.ndarray:
    """DRAWS means; each draw replaces every edge event by a random pool bar with the same key (with replacement)."""
    sums = np.zeros(DRAWS)
    count = 0
    for key, k in zip(*np.unique(edge_keys, return_counts=True)):
        cand = pool_r[pool_keys == key]
        if len(cand) == 0:
            continue
        sums += cand[rng.integers(0, len(cand), size=(DRAWS, k))].sum(axis=1)
        count += k
    return sums / count


def compare(edge: dict, base_mean: float, base_se: float, draws: np.ndarray | None) -> dict:
    se_edge = (edge["mean_R"] - edge["ci_lo"]) / 1.96
    d = edge["mean_R"] - base_mean
    se = float(np.sqrt(se_edge ** 2 + base_se ** 2))
    lo, hi = d - 1.96 * se, d + 1.96 * se
    out = {"baseline_mean_R": round(base_mean, 5), "baseline_se": round(base_se, 5), "diff": round(d, 5),
           "diff_ci_lo": round(lo, 5), "diff_ci_hi": round(hi, 5),
           "verdict": "ADDS BEYOND DRIFT" if lo > 0 else "WORSE THAN DRIFT" if hi < 0 else "NOT DISTINGUISHABLE FROM DRIFT"}
    if draws is not None:
        out["perm_p_one_sided"] = round(float((1 + (draws >= edge["mean_R"]).sum()) / (1 + len(draws))), 5)
    return out


def main(asset: str, edge: str, out_dir: Path) -> None:
    root = V2 / "assets" / asset / "edges" / edge
    sys.path.insert(0, str(V2)); sys.path.insert(0, str(root))
    runtime = importlib.import_module("runtime")
    ce = importlib.import_module("candidate_events")
    mr = importlib.import_module("model_rows")
    fam = rc.FAMILIES[edge]
    setattr(mr, fam["validator"], lambda *a, **k: None)
    rd = root / "artifacts" / "research_dataset"
    stem = f"{asset.lower()}_{edge}"
    feats = pd.read_csv(rd / f"{stem}_feature_matrix.csv")
    stored_ev = pd.read_csv(rd / f"{stem}_candidate_events.csv", usecols=["Datetime"])
    stored_mr = pd.read_csv(rd / f"{stem}_model_rows.csv", usecols=["Datetime", "realized_r"])
    bars = runtime._load_asset_bars()  # noqa: SLF001
    try:
        expanded = getattr(ce, fam["add"])(feats)
        feature_input = "stored research_dataset feature_matrix"
    except ce.CandidateEventError:
        feats = runtime.build_features(drop_warmup_rows=False)
        expanded = getattr(ce, fam["add"])(feats)
        feature_input = "runtime.build_features(drop_warmup_rows=False)"

    g = fam["gate"](ce, expanded, None)
    ev = rc.primary_side(edge, getattr(ce, fam["extract"])(g))
    side = int(ev["side"].iloc[0])
    lab = mr.apply_triple_barrier_labels(bars, ev)
    lab = lab[0] if isinstance(lab, tuple) else lab
    lab["Datetime"] = to_datetime_iso(lab["Datetime"], utc=True)
    s_ev = set(to_datetime_iso(stored_ev.Datetime, utc=True))
    m = lab.merge(stored_mr.assign(Datetime=to_datetime_iso(stored_mr.Datetime, utc=True)), on="Datetime",
                  how="inner", suffixes=("", "_stored"))
    rep = {"event_times_equal": bool(set(ev["Datetime"]) == s_ev), "matched_model_rows": int(len(m)),
           "stored_model_rows": int(len(stored_mr)),
           "max_abs_realized_r_diff": float((m.realized_r - m.realized_r_stored).abs().max()) if len(m) else None}
    rep["PASS"] = bool(rep["event_times_equal"] and rep["matched_model_rows"] == rep["stored_model_rows"]
                       and (rep["max_abs_realized_r_diff"] or 0.0) < 1e-9)
    print(f"{asset}/{edge} reproduction {rep}", flush=True)
    res: dict = {"asset": asset, "edge": edge, "side": side, "feature_input": feature_input,
                 "preregistration": "logs/counterfactual_tests_20261031/PREREGISTRATION_DRIFT_BASELINE.md",
                 "reproduction": rep}
    out_dir.mkdir(parents=True, exist_ok=True)
    if not rep["PASS"]:
        (out_dir / f"{asset}_{edge}_drift.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        return
    edge_st = rc.clustered_ci(lab.realized_r, lab.Datetime)
    res["edge_events"] = edge_st

    pool = pd.DataFrame({"Datetime": to_datetime_iso(g["Datetime"], utc=True),
                         "Event_Vol": pd.to_numeric(g["Event_Vol"], errors="coerce")})
    lo_t, hi_t = ev["Datetime"].min(), ev["Datetime"].max()
    pool = pool[np.isfinite(pool.Event_Vol) & (pool.Event_Vol > 0) & pool.Datetime.between(lo_t, hi_t)
                & ~pool.Datetime.isin(set(ev["Datetime"]))].drop_duplicates("Datetime")
    rng = np.random.default_rng(SEED)
    pool = pool.iloc[np.sort(rng.choice(len(pool), size=min(POOL, len(pool)), replace=False))].copy()
    pool["side"] = side
    pool["edge_id"] = None
    if edge == "rangestructure_long":   # pass-through tag for the labeler's post-check (t > 0); not used in labeling
        pool["Structure_Bias_TStat"] = 1.0
    print(f"labeling pool {len(pool)} random bars", flush=True)
    pl = mr.apply_triple_barrier_labels(bars, pool.reset_index(drop=True))
    pl = pl[0] if isinstance(pl, tuple) else pl
    pl["Datetime"] = to_datetime_iso(pl["Datetime"], utc=True)
    pl = pl[np.isfinite(pd.to_numeric(pl.realized_r, errors="coerce"))]
    pr = pl.realized_r.to_numpy(dtype="float64")
    pl[["Datetime", "realized_r"]].to_csv(out_dir / f"{asset}_{edge}_drift_pool.csv", index=False)
    u = rc.clustered_ci(pl.realized_r, pl.Datetime)
    res["pool"] = {"eligible_window": [str(lo_t), str(hi_t)], "labeled": int(len(pl)), **u}

    er = lab.realized_r.to_numpy(dtype="float64")
    n = int(np.isfinite(er).sum())
    edt = lab.Datetime[np.isfinite(er)]
    rng_u = np.random.default_rng(SEED + 1)
    draws_u = np.array([pr[rng_u.integers(0, len(pr), size=n)].mean() for _ in range(DRAWS)])
    to_month = lambda s: s.dt.tz_convert(None).dt.to_period("M").astype(str).to_numpy()  # noqa: E731
    draws_m = matched_draws(to_month(edt), to_month(pl.Datetime), pr, np.random.default_rng(SEED + 2))
    draws_h = matched_draws(edt.dt.hour.to_numpy(), pl.Datetime.dt.hour.to_numpy(), pr, np.random.default_rng(SEED + 3))
    u_se = (u["mean_R"] - u["ci_lo"]) / 1.96
    res["baseline_U_unconditional"] = compare(edge_st, u["mean_R"], u_se, draws_u)
    res["baseline_M_month_matched_PRIMARY"] = compare(edge_st, float(draws_m.mean()), float(draws_m.std(ddof=1)), draws_m)
    res["baseline_H_hour_matched"] = compare(edge_st, float(draws_h.mean()), float(draws_h.std(ddof=1)), draws_h)
    res["verdict"] = res["baseline_M_month_matched_PRIMARY"]["verdict"]
    for k in ("pool", "baseline_U_unconditional", "baseline_M_month_matched_PRIMARY", "baseline_H_hour_matched"):
        print(k, res[k], flush=True)
    (out_dir / f"{asset}_{edge}_drift.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print("written", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
