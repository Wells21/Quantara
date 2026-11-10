"""Gate C PBO/CSCV + compound-velocity winner (RUNBOOK v5.3, Section 6A STEP 7).

Uses ONLY the files produced by one Gate C run (the run's stamps), never older grids in the folder:
  candidates  = non-control cells with exec_mean_R > 0 and coverage_rate >= 0.99 (Gate C eligibility)
  winner      = argmax compound_velocity_score (log_growth = sum log(1 + 0.01*exec_r) over resolved trades by exit time,
                / span days, / max drawdown of the log-growth curve)
  PBO matrix  = per configuration, daily-prorated log(1 + 0.01*exec_r) (each trade spread evenly over the UTC days it spans)
  PBO trials  = EVERY configuration replayed in the run, controls included (AFML s.11.6 p.155: M holds the N trials).
                Owner decision 2026-10-26 (D051); the old positive-only candidate set is reported as a diagnostic.
  PBO         = cscv_pbo(s=16, compound_velocity_stat) -- AFML Ch.11 s.11.6 pp.155-157
  PASS        = PBO < 0.5 AND Prob[OOS <= 0] < 0.5 (project convention; the book gives no cutoff)

Moved into the repo 2026-10-22 from the session scratchpad (pbo_rerun.py, written 2026-10-14; it decided every Gate C/PBO
verdict 2026-10-14..10-17). Logic unchanged; regression test: tests/test_gate_c_pbo_regression.py (JPN/volatilitycarry_long,
PBO 0.5071484071484071).

usage:
  python shared/research/gate_c_pbo.py --edge-root assets/JPN/edges/volatilitycarry_long --stamps 20261017T...Z[,...]
  python shared/research/gate_c_pbo.py --jsonl <gatec_rerun.jsonl> EDGE [EDGE ...]      (legacy lane input)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.backtest_statistics.afml_ch11_pbo_reference import cscv_pbo  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.backtest_statistics.compound_velocity_stat import compound_velocity_stat  # noqa: E402

F = 0.01
EPS = 1e-12
CSCV_S = 16
MIN_COVERAGE = 0.99


def daily_log_growth(trades: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    trades = trades[trades["status"] == "resolved"].copy() if "status" in trades.columns else trades.copy()
    trades["entry_ts"] = to_datetime_iso(trades["entry_ts"], utc=True, errors="coerce")
    trades["exit_ts"] = to_datetime_iso(trades["exit_ts"], utc=True, errors="coerce")
    trades = trades.dropna(subset=["entry_ts", "exit_ts", "exec_r"])
    trades["log_g"] = np.log(1.0 + F * trades["exec_r"].astype(float))
    daily: dict[pd.Timestamp, float] = {}
    for tr in trades.itertuples():
        d0, d1 = tr.entry_ts.normalize(), tr.exit_ts.normalize()
        n = max(int((d1 - d0).days) + 1, 1)
        for k in range(n):
            day = d0 + pd.Timedelta(days=k)
            daily[day] = daily.get(day, 0.0) + tr.log_g / n
    return pd.Series(daily).sort_index(), trades


def compound_velocity_score(trades: pd.DataFrame) -> float:
    # 04C-style: log_growth over all resolved trades ordered by exit_ts / edge span days, / max DD of log curve
    t = trades.sort_values("exit_ts")
    lg = np.log(1.0 + F * t["exec_r"].astype(float).to_numpy())
    span = max((t["exit_ts"].max() - t["entry_ts"].min()).total_seconds() / 86400.0, 1e-9)
    cum = np.cumsum(lg)
    mdd = float(np.max(np.maximum.accumulate(np.r_[0.0, cum]) - np.r_[0.0, cum]))
    return (lg.sum() / span) / max(mdd, EPS)


def load_run_summary(art: Path, stamps: list[str]) -> pd.DataFrame:
    return pd.concat([pd.read_csv(art / f"v3_execution_velocity_summary_{s}.csv").assign(run_stamp=s) for s in stamps],
                     ignore_index=True).drop_duplicates(subset="label")


def build_performance_matrix(art: Path, elig: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float]]:
    series: dict[str, pd.Series] = {}
    scores: dict[str, float] = {}
    for r in elig.itertuples():
        tr = pd.read_csv(art / f"v3_execution_trades_{r.label}_{r.run_stamp}.csv")
        s, good = daily_log_growth(tr)
        series[r.label] = s
        scores[r.label] = compound_velocity_score(good)
    if not series:
        return pd.DataFrame(), scores
    idx = pd.date_range(min(s.index.min() for s in series.values()), max(s.index.max() for s in series.values()), freq="D", tz="UTC")
    perf = pd.DataFrame({k: v.reindex(idx, fill_value=0.0) for k, v in series.items()})
    return perf, scores


def pbo_from_matrix(perf: pd.DataFrame) -> dict[str, Any]:
    return cscv_pbo(perf, s=CSCV_S, higher_is_better=True, performance_stat_fn=compound_velocity_stat)


def evaluate_edge(edge_root: Path, stamps: list[str], *, edge: str | None = None, asset: str | None = None,
                  extras: list | None = None) -> dict[str, Any]:
    edge_root = Path(edge_root)
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, stamps)
    ctrl = summ["is_control"].fillna(False).astype(bool) if "is_control" in summ.columns else pd.Series(False, index=summ.index)
    nc = summ[~ctrl]
    elig = nc[(nc["exec_mean_R"] > 0) & (nc["coverage_rate"] >= MIN_COVERAGE)]
    res: dict[str, Any] = {"asset": asset or edge_root.parent.parent.name, "edge": edge or edge_root.name, "stamps": stamps,
                           "audit_extra_cost_bps": extras if extras is not None else [],
                           "n_replayed": int(len(nc)), "n_exec_positive_cov99": int(len(elig))}
    top = nc.sort_values("exec_mean_R", ascending=False).head(5)
    res["top5_exec_mean_R"] = [{"label": r.label, "exec_mean_R": round(float(r.exec_mean_R), 6),
                                "ci95": [round(float(r.weekly_cluster_bootstrap_mean_R_95_lo), 4), round(float(r.weekly_cluster_bootstrap_mean_R_95_hi), 4)],
                                "n": int(r.resolved_trades) if hasattr(r, "resolved_trades") else None} for r in top.itertuples()]
    if len(elig) == 0:
        res["verdict"] = "KILLED_GATE_C (no replayed cell with exec_mean_R>0 at >=99% coverage)"
        return res
    perf_pos, scores = build_performance_matrix(art, elig)
    winner = max(scores, key=scores.get)
    w = elig[elig["label"] == winner].iloc[0]
    res["winner_compound_velocity"] = {"label": winner, "score": scores[winner], "exec_mean_R": float(w["exec_mean_R"]),
                                       "ci95": [float(w["weekly_cluster_bootstrap_mean_R_95_lo"]), float(w["weekly_cluster_bootstrap_mean_R_95_hi"])],
                                       "R_per_week": float(w["calendar_exec_R_per_week"]), "mean_hold_h": float(w["mean_hold_hours"])}
    perf, _ = build_performance_matrix(art, summ)  # AFML s.11.6: all N trials of the run (D051)
    if perf.shape[1] < 2:
        res["pbo"] = None
        res["verdict"] = "GATE_C_PASS_ONLY_1_CANDIDATE (CSCV needs >=2 candidates; PBO not computable)"
        return res
    p = pbo_from_matrix(perf)
    res.update({"pbo_trial_set": "all_replayed (AFML s.11.6, D051)", "pbo_matrix_shape": list(perf.shape), "pbo": p["pbo"],
                "degradation_slope": p["performance_degradation_slope"], "prob_oos_nonpositive": p["prob_oos_winner_nonpositive"]})
    if perf_pos.shape[1] >= 2:
        q = pbo_from_matrix(perf_pos)
        res["diagnostic_positive_only"] = {"pbo_matrix_shape": list(perf_pos.shape), "pbo": q["pbo"],
                                           "prob_oos_nonpositive": q["prob_oos_winner_nonpositive"]}
    ok = p["pbo"] < 0.5 and p["prob_oos_winner_nonpositive"] < 0.5
    res["verdict"] = "PASS_PBO -> Steps 01-09 + Gate D" if ok else "KILLED_PBO"
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--edge-root", help="edge folder (assets/<ASSET>/edges/<edge>)")
    ap.add_argument("--stamps", help="comma-separated Gate C run stamps (v3_execution_velocity_summary_<stamp>.csv)")
    ap.add_argument("--jsonl", help="legacy: Gate C lane jsonl with {asset, edge, stamp, audit_extra_cost_bps} records")
    ap.add_argument("edges", nargs="*", help="edge names to read from --jsonl")
    ap.add_argument("--output", help="write the result JSON here")
    a = ap.parse_args(argv)
    out: dict[str, Any] = {}
    if a.jsonl:
        recs = [json.loads(l) for l in Path(a.jsonl).read_text(encoding="utf-8").splitlines() if l.strip()]
        for edge in a.edges:
            rr = [r for r in recs if r["edge"] == edge and r.get("stamp")]
            asset = rr[0]["asset"]
            out[edge] = evaluate_edge(V2_ROOT / "assets" / asset / "edges" / edge, [r["stamp"] for r in rr], edge=edge,
                                      asset=asset, extras=sorted({r.get("audit_extra_cost_bps") for r in rr}, key=str))
            print(json.dumps(out[edge], indent=1, default=str))
        default_out = Path(a.jsonl).with_name(Path(a.jsonl).stem + "_pbo.json")
    else:
        if not (a.edge_root and a.stamps):
            ap.error("give --edge-root and --stamps, or --jsonl EDGE ...")
        er = Path(a.edge_root)
        if not er.is_absolute():
            er = V2_ROOT / er
        out[er.name] = evaluate_edge(er, [s.strip() for s in a.stamps.split(",") if s.strip()])
        print(json.dumps(out[er.name], indent=1, default=str))
        default_out = er / "artifacts" / "v3_horizon_lab" / f"gate_c_pbo_{a.stamps.split(',')[0].strip()}.json"
    target = Path(a.output) if a.output else default_out
    target.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print("written", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
