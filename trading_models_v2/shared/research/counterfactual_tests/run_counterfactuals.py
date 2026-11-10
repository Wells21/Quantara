"""Thesis-card counterfactual tests (MLAM s.1.1 p.2, s.1.2.2 p.4; logs/counterfactual_tests_20261031/PREREGISTRATION.md).

Per edge: the edge's own trigger-field function, its gate column recomputed with ONE clause replaced, its own event
extraction / primary-side rule, its own triple-barrier labeler (locked geometry, tick-resolved mid; pre-label gate
validator disabled). Reproduction check: no clause replaced -> the stored candidate events and model rows.

usage (from trading_models_v2): python shared/research/counterfactual_tests/run_counterfactuals.py ASSET EDGE OUT_DIR
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

V2 = Path(__file__).resolve().parents[3]
MIN_EVENTS = 100


def clustered_ci(r: pd.Series, ts: pd.Series) -> dict:
    r = pd.to_numeric(r, errors="coerce").to_numpy(dtype="float64")
    wk = to_datetime_iso(ts, utc=True).dt.tz_convert(None).dt.to_period("W").astype(str).to_numpy()
    ok = np.isfinite(r)
    r, wk = r[ok], wk[ok]
    n = len(r)
    if n == 0:
        return {"n": 0, "mean_R": None, "ci_lo": None, "ci_hi": None, "weeks": 0}
    m = float(r.mean())
    g = pd.Series(r - m).groupby(wk).sum().to_numpy()
    se = float(np.sqrt((g ** 2).sum()) / n)
    return {"n": int(n), "mean_R": round(m, 5), "ci_lo": round(m - 1.96 * se, 5), "ci_hi": round(m + 1.96 * se, 5),
            "weeks": int(len(g))}


# --- per-family gate recomputation ------------------------------------------------------------------------------------
def gate_rangestructure(ce, out: pd.DataFrame, test: str | None) -> pd.DataFrame:
    rp, t = out["Range_Position"], out["Structure_Bias_TStat"]
    near_low = rp <= ce.EXTREME_ZONE_THRESHOLD
    near_high = rp >= (1.0 - ce.EXTREME_ZONE_THRESHOLD)
    away = (rp <= ce.MAX_MID_RANGE_POSITION) | (rp >= (1.0 - ce.MAX_MID_RANGE_POSITION))
    bias = t.abs() >= ce.STRUCTURE_BIAS_TSTAT_THRESHOLD
    if test == "C1":
        bias = t.abs() < ce.STRUCTURE_BIAS_TSTAT_THRESHOLD
    out = out.copy()
    out["Range_Structure_Event_Type"] = 0
    out.loc[near_low & away & bias, "Range_Structure_Event_Type"] = -1
    out.loc[near_high & away & bias, "Range_Structure_Event_Type"] = 1
    return out


def gate_volatilitycarry(ce, out: pd.DataFrame, test: str | None) -> pd.DataFrame:
    calm = out["Is_Calm_Vol"].astype(bool)
    ratio = out["Volatility_Ratio"] <= ce.MAX_ACTIVITY_RATIO
    t = out["Drift_TStat"]
    drift_long = t >= ce.DRIFT_TSTAT_THRESHOLD
    drift_short = t <= -ce.DRIFT_TSTAT_THRESHOLD
    if test == "C1":
        calm = ~calm
    if test == "C2":
        drift_long = (t > 0) & (t < ce.DRIFT_TSTAT_THRESHOLD)
        drift_short = pd.Series(False, index=out.index)
    out = out.copy()
    out["Volatility_Carry_Event_Type"] = 0
    out.loc[calm & ratio & drift_long, "Volatility_Carry_Event_Type"] = 1
    out.loc[calm & ratio & drift_short, "Volatility_Carry_Event_Type"] = -1
    return out


def gate_dailyrange(ce, out: pd.DataFrame, test: str | None) -> pd.DataFrame:
    completion = out["Daily_Range_Completion"] >= ce.MIN_DAILY_RANGE_COMPLETION
    if test == "C1":
        completion = out["Daily_Range_Completion"] < 0.50
    cond = (
        out["Expected_Daily_Range"].notna() & out["Event_Vol"].notna()
        & (out["Bars_Into_Day"] >= ce.MIN_DAY_PROGRESS_BARS) & completion
        & (out["Downside_Extension_ATR"] >= ce.MIN_DOWNSIDE_EXTENSION_ATR)
        & (out["Day_Position"] <= ce.MAX_DAY_POSITION) & (out["Close"] < out["Day_Open"])
    )
    out = out.copy()
    out["Daily_Range_Fade_Event"] = cond.astype("int64")
    return out


def gate_drawdownhazard(ce, out: pd.DataFrame, test: str | None) -> pd.DataFrame:
    recovery = out["Recovery_Fraction"].le(ce.MAX_RECOVERY_FRACTION)
    hazard = out["Drawdown_Hazard_Probability"].ge(ce.MIN_HAZARD_PROBABILITY)
    if test == "C1":
        hazard = out["Drawdown_Hazard_Probability"].lt(0.48)
    if test == "C2":
        recovery = out["Recovery_Fraction"].gt(ce.MAX_RECOVERY_FRACTION)
    out = out.copy()
    out["Drawdown_Hazard_Active_State"] = (
        out["Drawdown_Active"].astype(bool)
        & out["Drawdown_Severity_Percentile_100_Over_1000"].ge(ce.MIN_DRAWDOWN_SEVERITY_PERCENTILE)
        & recovery & hazard
        & out["Drawdown_Hazard_Cell_Count"].ge(ce.MIN_HAZARD_CELL_OBSERVATIONS)
    ).astype("int64")
    return out


FAMILIES = {
    "rangestructure_long": dict(add="add_v1_range_structure_features", gate=gate_rangestructure,
                                extract="_extract_v1_spaced_range_structure_events",
                                validator="_validate_rangestructure_long_events", tests=["C1"]),
    "volatilitycarry_long": dict(add="add_v1_volatility_carry_features", gate=gate_volatilitycarry,
                                 extract="_extract_v1_spaced_volatility_carry_events",
                                 validator="_validate_volatilitycarry_long_events", tests=["C1", "C2"]),
    "dailyrangecompletionfade_long": dict(add="add_daily_range_completion_fade_fields", gate=gate_dailyrange,
                                          extract="_extract_spaced_events",
                                          validator="_validate_dailyrangecompletionfade_long_events", tests=["C1"]),
    "drawdownhazardcontinuation_short": dict(add="add_drawdown_hazard_features", gate=gate_drawdownhazard,
                                             extract="_extract_high_recall_events", validator="_validate_events",
                                             tests=["C1", "C2"]),
}


def primary_side(edge: str, ev: pd.DataFrame) -> pd.DataFrame:
    """The edge's primary-side rule minus the replaced gate (which the event set already reflects)."""
    ev = ev.copy()
    if edge == "rangestructure_long":
        ev = ev[pd.to_numeric(ev["Structure_Bias_TStat"], errors="coerce") > 0]
        ev = ev[pd.to_numeric(ev["Range_Structure_Event_Type"], errors="coerce").isin([-1, 1])]
        side = 1
    elif edge == "volatilitycarry_long":
        ev = ev[pd.to_numeric(ev["Volatility_Carry_Event_Type"], errors="coerce") == 1]
        side = 1
    elif edge == "dailyrangecompletionfade_long":
        ev = ev[pd.to_numeric(ev["Daily_Range_Fade_Event"], errors="coerce") == 1]
        side = 1
    else:   # HK: primary_side.py valid mask without the gate clauses (they are in Drawdown_Hazard_Active_State)
        ev = ev[ev["Drawdown_Hazard_Event"].eq(1) & ev["Drawdown_Hazard_Active_State"].eq(1)
                & ev["Drawdown_Active"].astype(bool) & ev["Negative_Event_Bar"].astype(bool)
                & ev["Log_Return_1"].lt(0) & ev["Event_Vol"].gt(0)
                & (ev["Drawdown_Hazard_State_Entry"] + ev["Drawdown_Hazard_Renewed_Impulse"]).eq(1)]
        ev = ev.dropna(subset=["Datetime", "Bar_Start", "Event_Vol", "Log_Return_1",
                               "Drawdown_Severity_Percentile_100_Over_1000", "Recovery_Fraction",
                               "Drawdown_Hazard_Probability", "Drawdown_Hazard_Cell_Count"])
        side = -1
    ev = ev.dropna(subset=["Datetime", "Event_Vol"])
    ev = ev[np.isfinite(pd.to_numeric(ev["Event_Vol"], errors="coerce"))]
    ev["side"] = side
    ev["edge_id"] = None
    ev["Datetime"] = to_datetime_iso(ev["Datetime"], utc=True)
    return ev.sort_values("Datetime").reset_index(drop=True)


def main(asset: str, edge: str, out_dir: Path) -> None:
    root = V2 / "assets" / asset / "edges" / edge
    sys.path.insert(0, str(V2)); sys.path.insert(0, str(root))
    runtime = importlib.import_module("runtime")
    ce = importlib.import_module("candidate_events")
    mr = importlib.import_module("model_rows")
    fam = FAMILIES[edge]
    setattr(mr, fam["validator"], lambda *a, **k: None)   # pre-label gate validator off (rejects inverted sets)
    rd = root / "artifacts" / "research_dataset"
    stem = f"{asset.lower()}_{edge}"
    feats = pd.read_csv(rd / f"{stem}_feature_matrix.csv")
    stored_ev = pd.read_csv(rd / f"{stem}_candidate_events.csv", usecols=["Datetime"])
    stored_mr = pd.read_csv(rd / f"{stem}_model_rows.csv", usecols=["Datetime", "realized_r"])
    bars = runtime._load_asset_bars()  # noqa: SLF001
    print(f"{asset}/{edge}: features {len(feats)} rows, bars {len(bars)}, stored events {len(stored_ev)}", flush=True)
    try:
        expanded = getattr(ce, fam["add"])(feats)
        feature_input = "stored research_dataset feature_matrix"
    except ce.CandidateEventError as exc:   # stored matrix lacks a non-schema trigger input -> the edge's own builder
        print(f"stored matrix insufficient ({exc}); using runtime.build_features", flush=True)
        feats = runtime.build_features(drop_warmup_rows=False)
        expanded = getattr(ce, fam["add"])(feats)
        feature_input = "runtime.build_features(drop_warmup_rows=False)"

    def run(test: str | None) -> pd.DataFrame:
        g = fam["gate"](ce, expanded, test)
        ev = primary_side(edge, getattr(ce, fam["extract"])(g))
        lab = mr.apply_triple_barrier_labels(bars, ev)
        lab = lab[0] if isinstance(lab, tuple) else lab
        lab["Datetime"] = to_datetime_iso(lab["Datetime"], utc=True)
        lab.attrs["pre_label_times"] = set(ev["Datetime"])
        return lab

    res: dict = {"asset": asset, "edge": edge, "preregistration": "logs/counterfactual_tests_20261031/PREREGISTRATION.md",
                 "feature_input": feature_input}
    base = run(None)
    s_ev = set(to_datetime_iso(stored_ev.Datetime, utc=True))
    m = base.merge(stored_mr.assign(Datetime=to_datetime_iso(stored_mr.Datetime, utc=True)), on="Datetime",
                   how="inner", suffixes=("", "_stored"))
    res["reproduction"] = {
        "events_regenerated": int(len(base.attrs["pre_label_times"])), "events_labeled": int(len(base)),
        "events_stored": int(len(s_ev)), "event_times_equal": bool(base.attrs["pre_label_times"] == s_ev),
        "stored_model_rows": int(len(stored_mr)), "matched_model_rows": int(len(m)),
        "max_abs_realized_r_diff": float((m.realized_r - m.realized_r_stored).abs().max()) if len(m) else None,
    }
    rep = res["reproduction"]
    rep["PASS"] = bool(rep["event_times_equal"] and rep["matched_model_rows"] == rep["stored_model_rows"]
                       and (rep["max_abs_realized_r_diff"] or 0.0) < 1e-9)
    print("reproduction", rep, flush=True)
    res["edge_events"] = {"set": "edge events (no clause replaced)", **clustered_ci(base.realized_r, base.Datetime)}
    res["tests"] = {}
    if rep["PASS"]:
        for t in fam["tests"]:
            lab = run(t)
            st = clustered_ci(lab.realized_r, lab.Datetime)
            overlap = int(len(set(lab.Datetime) & set(base.Datetime)))
            if st["n"] < MIN_EVENTS:
                verdict = "INCONCLUSIVE"
            elif edge == "volatilitycarry_long" and t == "C1":
                verdict = "PASS" if st["mean_R"] < res["edge_events"]["mean_R"] else "FAIL"
            else:
                verdict = "PASS" if (st["mean_R"] < res["edge_events"]["mean_R"] and st["ci_lo"] <= 0) else "FAIL"
            res["tests"][t] = {**st, "overlap_with_edge_events": overlap, "verdict": verdict}
            print(t, res["tests"][t], flush=True)
        if edge == "rangestructure_long":   # C2 branch split of the edge's own labeled events
            br = {}
            for k, name in ((1, "near_high"), (-1, "near_low")):
                sub = base[pd.to_numeric(base.Range_Structure_Event_Type, errors="coerce") == k]
                st = clustered_ci(sub.realized_r, sub.Datetime)
                st["verdict"] = ("INCONCLUSIVE" if st["n"] < MIN_EVENTS or st["mean_R"] is None else
                                 "PASS" if st["ci_lo"] > 0 else "INCONCLUSIVE" if st["mean_R"] > 0 else "FAIL")
                br[name] = st
            res["tests"]["C2"] = {"branches": br,
                                  "verdict": "PASS" if all(b["verdict"] == "PASS" for b in br.values()) else
                                  "FAIL" if any(b["verdict"] == "FAIL" for b in br.values()) else "INCONCLUSIVE"}
            print("C2", res["tests"]["C2"], flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{asset}_{edge}.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print("written", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
