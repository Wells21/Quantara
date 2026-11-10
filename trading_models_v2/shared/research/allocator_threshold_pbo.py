"""D057 (owner 2026-10-29: "include D057"; pre-registered in docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4P.1 before any result):
PBO of the allocator's threshold selection, and the edge's DSR record with the threshold trials added.

04c picks one calibrated-probability threshold per edge out of the thresholds 04b evaluated -- a selection among N trials
(AFML s.11.6 pp.155-157), and those trials belong in the edge's trial count (AFML s.14.7.3 p.205 third law; MLAM s.8.7).

  trials   : every threshold in v2_threshold_grid_summary_all.csv for the edge (04b's grid-gate rejects included). Trades of a
             threshold missing from the grid ledger are rebuilt from the lowest threshold's trade set filtered by
             calibrated_probability >= t (sets nest exactly, executed_r identical; afml_bet_size taken from the lowest threshold).
  PBO      : daily-prorated log(1 + 0.01 afml_bet_size exec_r), calendar days, zero-filled; compound_velocity_stat; CSCV S = 16.
             Sensitivities: unsized; exit-day booking; gate-passing thresholds only.
  DSR      : Gate C trials (the stamps of ch14_dsr_all_trials.json, gate_c_pbo units) + threshold trials (unsized, same
             units); ONC E[K] on the denoised (+ detoned if a market component) correlation (D076); Lopez de Prado &
             Lewis 2018 cluster weights; selected = the deployed threshold.
Diagnostic only.

usage (from trading_models_v2, venv39 or venv312):
  python -m shared.research.allocator_threshold_pbo --out artifacts/ch16/d057_threshold_pbo.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
if str(V2) not in sys.path:
    sys.path.insert(0, str(V2))

from shared.backtest_statistics.afml_ch11_pbo_reference import cscv_pbo  # noqa: E402
from shared.backtest_statistics.afml_ch14_dsr import (  # noqa: E402
    cluster_trial_variance, dsr_report, frequency, moments, onc_trial_clusters)
from shared.backtest_statistics.compound_velocity_stat import compound_velocity_stat  # noqa: E402
from shared.research.gate_c_pbo import build_performance_matrix, load_run_summary  # noqa: E402

OUT = V2 / "portfolio_allocator" / "outputs"
GRID_LEDGER = OUT / "v2_threshold_grid_trade_ledger_core_plus_watchlist.csv"
GRID_SUMMARY = OUT / "v2_threshold_grid_summary_all.csv"
PLATEAU = OUT / "v2_threshold_plateau_report.csv"
LIVE_MANIFEST = V2 / "portfolio_live" / "live_manifest.json"
EDGE_UNIVERSE = V2 / "config" / "edge_universe.json"
F = 0.01
CSCV_S = 16
ONC_SEED = 0
TOL = 1e-9


def deployable_edges() -> list[tuple[str, str, str]]:
    u = json.loads(EDGE_UNIVERSE.read_text(encoding="utf-8"))
    return [(e["asset"], f"{e['family']}_{e['side']}", f"{e['asset']}/{e['family']}_{e['side']}")
            for e in u["edges"] if e.get("enabled", True)]


def live_thresholds() -> dict[str, float]:
    man = json.loads(LIVE_MANIFEST.read_text(encoding="utf-8"))
    out: dict[str, float] = {}

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "edge_id" in o and "final_live_threshold" in o:
                out[o["edge_id"]] = float(o["final_live_threshold"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(man)
    return out


def threshold_trades(edge_id: str, ledger: pd.DataFrame, thresholds: list[float]) -> tuple[dict[float, pd.DataFrame], list[float]]:
    g = ledger[ledger["edge_id"] == edge_id]
    have = sorted(g["threshold"].unique())
    lo = g[np.isclose(g["threshold"], have[0])]
    out, rebuilt = {}, []
    for t in thresholds:
        hit = [h for h in have if abs(h - t) < TOL]
        if hit:
            out[t] = g[np.isclose(g["threshold"], hit[0])]
        else:
            if t < have[0] - TOL:
                raise ValueError(f"{edge_id}: threshold {t} below the lowest ledger threshold {have[0]}; cannot rebuild")
            out[t] = lo[lo["calibrated_probability"] >= t - 1e-12]
            rebuilt.append(t)
    return out, rebuilt


def daily_matrix(sets: dict[float, pd.DataFrame], *, sized: bool, prorated: bool) -> pd.DataFrame:
    series = {}
    for t, s in sets.items():
        r = s["executed_r"].to_numpy(dtype=float) * (s["afml_bet_size"].to_numpy(dtype=float) if sized else 1.0)
        lg = np.log(1.0 + F * r)
        d0 = s["entry_time"].dt.floor("D")
        d1 = s["exit_time"].dt.floor("D")
        daily: dict[pd.Timestamp, float] = {}
        if prorated:
            for a, b, v in zip(d0, d1, lg):
                n = int((b - a).days) + 1
                for k in range(n):
                    day = a + pd.Timedelta(days=k)
                    daily[day] = daily.get(day, 0.0) + v / n
        else:
            for b, v in zip(d1, lg):
                daily[b] = daily.get(b, 0.0) + v
        series[f"thr_{t:.2f}"] = pd.Series(daily)
    idx = pd.date_range(min(s.index.min() for s in series.values()), max(s.index.max() for s in series.values()), freq="D", tz="UTC")
    return pd.DataFrame({k: v.reindex(idx, fill_value=0.0) for k, v in series.items()})


def pbo(perf: pd.DataFrame, selected: str) -> dict[str, Any]:
    p = cscv_pbo(perf, s=CSCV_S, higher_is_better=True, performance_stat_fn=compound_velocity_stat)
    full = compound_velocity_stat(perf.to_numpy())
    ranks = pd.Series(full, index=perf.columns).rank(ascending=False)
    return {"n_trials": int(perf.shape[1]), "matrix_days": int(perf.shape[0]), "pbo": p["pbo"],
            "degradation_slope": p["performance_degradation_slope"], "prob_oos_nonpositive": p["prob_oos_winner_nonpositive"],
            "pass": bool(p["pbo"] < 0.5 and p["prob_oos_winner_nonpositive"] < 0.5),
            "full_sample_argmax": str(perf.columns[int(np.argmax(full))]), "selected": selected,
            "selected_full_sample_rank": int(ranks[selected])}


def dsr_record(edge_root: Path, stamps: list[str], thr_perf: pd.DataFrame, selected: str) -> dict[str, Any]:
    art = edge_root / "artifacts" / "v3_horizon_lab"
    summ = load_run_summary(art, stamps)
    gate_c, _ = build_performance_matrix(art, summ)
    idx = gate_c.index.union(thr_perf.index)
    comb = pd.concat([gate_c.reindex(idx, fill_value=0.0), thr_perf.reindex(idx, fill_value=0.0)], axis=1)
    out = {}
    for name, perf in (("combined_gate_c_plus_thresholds", comb), ("thresholds_only", thr_perf)):
        perf = perf.loc[:, perf.std(ddof=1) > 0]
        clusters, corr_diag = onc_trial_clusters(perf, seed=ONC_SEED)   # MLAM p.24/p.87 (D076)
        tv = cluster_trial_variance(perf, clusters)
        trial_sr = perf.apply(lambda c: moments(c.to_numpy())["sr"])
        rep = dsr_report(perf[selected].to_numpy(), perf.index, tv["E_K"], tv["V_annualised_sr"],
                         {"annualised_1p0": 1.0,
                          "median_trial_sr": float(trial_sr.median() * np.sqrt(frequency(perf.index, len(perf))))})
        out[name] = {"n_trials": int(perf.shape[1]), "matrix_days": int(perf.shape[0]), "E_K_onc": tv["E_K"], "trial_corr": corr_diag,
                     "V_annualised_sr": tv["V_annualised_sr"], "cluster_weighting": tv["weighting"],
                     "selected_sr_rank": int((trial_sr > trial_sr[selected]).sum() + 1),
                     **{k: rep[k] for k in ("psr_vs_zero", "dsr", "dsr_pass_0p95", "fwer", "sr_star_annualised")},
                     "selected": {k: rep[k] for k in ("T", "sr", "skew", "kurt", "frequency", "annualised_sr")}}
    out["n_gate_c_trials"] = int(gate_c.shape[1])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-dsr", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    ledger = pd.read_csv(GRID_LEDGER, usecols=["edge_id", "threshold", "entry_time", "exit_time", "executed_r", "afml_bet_size",
                                               "calibrated_probability"], float_precision="round_trip")
    for c in ("entry_time", "exit_time"):
        ledger[c] = pd.to_datetime(ledger[c], utc=True, format="mixed")
    summary = pd.read_csv(GRID_SUMMARY, usecols=["edge_id", "threshold", "passes_grid_gates"])
    plateau = pd.read_csv(PLATEAU, usecols=["edge_id", "threshold", "compound_velocity_gate_pass", "compound_velocity_score"])
    live = live_thresholds()
    res: dict[str, Any] = {"preregistration": "docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4P.1", "edges": {}}
    for asset, edge, edge_id in deployable_edges():
        thr = sorted(float(t) for t in summary.loc[summary["edge_id"] == edge_id, "threshold"])
        sets, rebuilt = threshold_trades(edge_id, ledger, thr)
        sel_t = live[edge_id]
        selected = f"thr_{sel_t:.2f}"
        pl = plateau[plateau["edge_id"] == edge_id]
        argmax_04c = float(pl.loc[pl["compound_velocity_score"].idxmax(), "threshold"])
        gate_ok = sorted(float(t) for t in pl.loc[pl["compound_velocity_gate_pass"].astype(str).str.lower() == "true", "threshold"])
        e: dict[str, Any] = {"thresholds_evaluated_by_04b": thr, "rebuilt_from_lowest_threshold": rebuilt,
                             "live_threshold": sel_t, "argmax_04c": argmax_04c, "live_equals_04c_argmax": abs(argmax_04c - sel_t) < TOL,
                             "trades_per_threshold": {f"{t:.2f}": int(len(s)) for t, s in sets.items()}}
        main_perf = daily_matrix(sets, sized=True, prorated=True)
        e["pbo_main_sized_prorated_all"] = pbo(main_perf, selected)
        e["pbo_unsized_prorated_all"] = pbo(unsized := daily_matrix(sets, sized=False, prorated=True), selected)
        e["pbo_sized_exitday_all"] = pbo(daily_matrix(sets, sized=True, prorated=False), selected)
        gsel = {t: sets[t] for t in thr if any(abs(t - g) < TOL for g in gate_ok)}
        e["pbo_sized_prorated_gate_passing_only"] = pbo(daily_matrix(gsel, sized=True, prorated=True), selected) \
            if len(gsel) >= 2 and selected in [f"thr_{t:.2f}" for t in gsel] else "n/a"
        print(edge_id, json.dumps({k: e["pbo_main_sized_prorated_all"][k] for k in ("n_trials", "pbo", "prob_oos_nonpositive", "pass")}),
              round(time.time() - t0, 1), flush=True)
        if not a.skip_dsr:
            rec = json.loads((V2 / "assets" / asset / "edges" / edge / "artifacts" / "feature_importance_audit" /
                              "ch14_dsr_all_trials.json").read_text(encoding="utf-8"))
            e["dsr_record"] = dsr_record(V2 / "assets" / asset / "edges" / edge, rec["stamps"], unsized, selected)
            e["dsr_record"]["ch14_level1_dsr_before"] = rec["dsr"]
            print(edge_id, "DSR", json.dumps({k: (v["dsr"], v["E_K_onc"], v["n_trials"]) for k, v in e["dsr_record"].items()
                                              if isinstance(v, dict)}), round(time.time() - t0, 1), flush=True)
        res["edges"][edge_id] = e
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    res["elapsed_s"] = round(time.time() - t0, 1)
    Path(a.out).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print("written", a.out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
