"""RETIRED 2026-11-04 (D075 amended, owner: "Delete item 4"): no longer a gate or a required step. It depended on the
allocator's final_live_threshold, but edge-level gates must not depend on the allocator, and on 2026-11-02 it gave the
same verdict as the raw event set on all 6 edges. Kept only to reproduce the 2026-11-02 record.

Model-filtered drift test (PREREGISTRATION_DRIFT_BASELINE.md, addendum 2026-11-02).

Taken trades (calibrated OOF rows with live_calibrated_probability >= final_live_threshold from
portfolio_live/live_manifest.json) vs the saved random same-side pool of run_drift_baseline.py.

usage (from trading_models_v2): python shared/research/counterfactual_tests/run_drift_filtered.py OUT_DIR
"""
from __future__ import annotations

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
import run_drift_baseline as db  # noqa: E402

V2 = rc.V2


def se_of(st: dict) -> float:
    return (st["mean_R"] - st["ci_lo"]) / 1.96


def main(out_dir: Path) -> None:
    manifest = json.loads((V2 / "portfolio_live" / "live_manifest.json").read_text(encoding="utf-8"))
    thresholds = {e["edge_id"]: float(e["final_live_threshold"]) for e in manifest["selected_edges"]}
    rows = []
    for edge_id, thr in sorted(thresholds.items()):
        asset, edge = edge_id.split("/")
        pool_path = out_dir / f"{asset}_{edge}_drift_pool.csv"
        pool = pd.read_csv(pool_path)
        pool["Datetime"] = to_datetime_iso(pool["Datetime"], utc=True)
        pr = pool.realized_r.to_numpy(dtype="float64")
        oof = pd.read_csv(V2 / "assets" / asset / "edges" / edge / "artifacts" / "calibration"
                          / f"{asset.lower()}_{edge}_calibrated_oof_predictions.csv",
                          usecols=["Datetime", "realized_r", "live_calibrated_probability"])
        oof["Datetime"] = to_datetime_iso(oof["Datetime"], utc=True)
        oof = oof[np.isfinite(pd.to_numeric(oof.realized_r, errors="coerce"))]
        taken = oof[oof.live_calibrated_probability >= thr]
        skipped = oof[oof.live_calibrated_probability < thr]
        res: dict = {"asset": asset, "edge": edge, "final_live_threshold": thr,
                     "preregistration": "logs/counterfactual_tests_20261031/PREREGISTRATION_DRIFT_BASELINE.md (addendum)",
                     "oof_rows": int(len(oof)), "all_oof": rc.clustered_ci(oof.realized_r, oof.Datetime),
                     "taken": rc.clustered_ci(taken.realized_r, taken.Datetime),
                     "not_taken": rc.clustered_ci(skipped.realized_r, skipped.Datetime)}
        st = res["taken"]
        n = st["n"]
        rng_u = np.random.default_rng(db.SEED + 11)
        draws_u = np.array([pr[rng_u.integers(0, len(pr), size=n)].mean() for _ in range(db.DRAWS)])
        to_month = lambda s: s.dt.tz_convert(None).dt.to_period("M").astype(str).to_numpy()  # noqa: E731
        draws_m = db.matched_draws(to_month(taken.Datetime), to_month(pool.Datetime), pr, np.random.default_rng(db.SEED + 12))
        draws_h = db.matched_draws(taken.Datetime.dt.hour.to_numpy(), pool.Datetime.dt.hour.to_numpy(), pr,
                                   np.random.default_rng(db.SEED + 13))
        u = rc.clustered_ci(pool.realized_r, pool.Datetime)
        res["F1_U_unconditional"] = db.compare(st, u["mean_R"], se_of(u), draws_u)
        res["F1_M_month_matched_PRIMARY"] = db.compare(st, float(draws_m.mean()), float(draws_m.std(ddof=1)), draws_m)
        res["F1_H_hour_matched"] = db.compare(st, float(draws_h.mean()), float(draws_h.std(ddof=1)), draws_h)
        res["verdict"] = res["F1_M_month_matched_PRIMARY"]["verdict"]
        ns = res["not_taken"]
        if n >= rc.MIN_EVENTS and ns["n"] >= rc.MIN_EVENTS:
            d = st["mean_R"] - ns["mean_R"]
            se = float(np.sqrt(se_of(st) ** 2 + se_of(ns) ** 2))
            res["F2_taken_minus_not_taken"] = {"diff": round(d, 5), "ci_lo": round(d - 1.96 * se, 5),
                                               "ci_hi": round(d + 1.96 * se, 5)}
        else:
            res["F2_taken_minus_not_taken"] = "not computed (< 100 rows in a set)"
        (out_dir / f"{asset}_{edge}_drift_filtered.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        rows.append(res)
        m = res["F1_M_month_matched_PRIMARY"]
        print(f"{edge_id} thr {thr} taken {n}/{len(oof)} R {st['mean_R']} | month-matched {m['baseline_mean_R']} "
              f"diff {m['diff']} [{m['diff_ci_lo']},{m['diff_ci_hi']}] {m['verdict']} | F2 {res['F2_taken_minus_not_taken']}",
              flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
