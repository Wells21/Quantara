"""AFML Ch.3 label audit -- primary vs meta-model report for every edge (read-only diagnostic, not a gate).

What it reports per edge (owner decision 2026-10-23, Ch.3 walkthrough, docs/mldp/AFML_MLAM_WALKTHROUGH.md s.4C):
  * label population   -- rows, positive rate, PT / SL / vertical shares, share of vertical exits with r > 0,
                          same-bar tie resolution (tick vs fallback), Event_Vol distribution (AFML s.3.3-3.5, pp.44-49)
  * cost per R by Event_Vol quintile -- evidence for/against a minRet filter (Snippet 3.3, p.48; deviation D027)
  * AFML Exercises 3.4/3.5 (p.55) -- accuracy, precision, recall and F1 of the primary model alone (it takes every
    event, recall = 1) versus primary + meta-model at the live threshold, on the calibrated OOF predictions
  * AFML s.3.7 (pp.51-53) check -- does the meta-model raise F1? plus the Step 02 CPCV F1 against the approve-all
    baseline (deviation D028; root cause is examined in Ch.9 / Ch.10 / Ch.14)
  * AFML Ch.4 overlap (pp.59-68, Exercises 4.1/4.2) -- mean/median average uniqueness on the label's own t1 (label_t1
    snapped to the bar clock), effective number of independent outcomes (sum of uniqueness), share of events that start
    while an earlier event is still open, AR(1) of uniqueness, and one seeded standard vs sequential bootstrap draw

Threshold (D081, owner 2026-11-04): the edge-level audit is THRESHOLD-FREE -- the Exercise 3.4/3.5 primary vs
primary + meta comparison is not computed (the Step 04 selected_threshold is not the live threshold and is not used
anywhere in the audit). With --live-threshold (allocator / deployment stage, after the allocator run), Exercise 3.5 is
added at final_live_threshold from portfolio_allocator/outputs/active_edge_universe.json (report only, not a gate).

Run from trading_models_v2 (venv312), at every certification (runbook 6A Step 4C):
    python ./shared/research/label_audit.py                       # every edge folder on every asset
    python ./shared/research/label_audit.py --edges XAUUSD/rangestructure_long
Writes assets/{A}/edges/{edge}/artifacts/label_audit/label_audit.json and prints a table.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

V2_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_UNIVERSE = V2_ROOT / "portfolio_allocator" / "outputs" / "active_edge_universe.json"
AUDIT_VERSION = "afml_ch3_ch4_label_audit_v2"
BOOTSTRAP_SEED = 7
TRUE_STRINGS = {"true", "1"}


def _live_thresholds() -> dict[str, float]:
    if not ACTIVE_UNIVERSE.exists():
        return {}
    payload = json.loads(ACTIVE_UNIVERSE.read_text(encoding="utf-8"))
    return {
        str(e["edge_id"]): float(e["final_live_threshold"])
        for e in payload.get("active_edges", [])
        if e.get("edge_id") and e.get("final_live_threshold") is not None
    }


def _classification(y: pd.Series, take: pd.Series) -> dict[str, float]:
    tp = int((take & (y == 1)).sum())
    tn = int((~take & (y == 0)).sum())
    approved = int(take.sum())
    positives = int((y == 1).sum())
    precision = tp / approved if approved else float("nan")
    recall = tp / positives if positives else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if approved and positives and (precision + recall) > 0 else 0.0
    return {
        "approved_share": round(approved / len(y), 4),
        "accuracy": round((tp + tn) / len(y), 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def _overlap(edge_root: Path, asset: str, resolution: Path) -> dict[str, Any]:
    if str(V2_ROOT) not in sys.path:
        sys.path.insert(0, str(V2_ROOT))
    from shared.assets.asset_layout import get_asset_layout
    from shared.models.afml_sample_weights import compute_afml_event_weight_table, label_end_times, snap_to_bar_clock
    from shared.models.sequential_bootstrap import get_avg_uniqueness, sequential_bootstrap

    bars = pd.read_csv(get_asset_layout(asset, V2_ROOT).standard_quote_tick_bars_csv, usecols=["Datetime", "Close"])
    bar_index = pd.DatetimeIndex(to_datetime_iso(bars["Datetime"], utc=True))
    ev = pd.read_csv(resolution, usecols=lambda c: c in ("Datetime", "gross_t1", "label_t1"))
    ev["Datetime"] = to_datetime_iso(ev["Datetime"], utc=True)
    ev = ev.sort_values("Datetime").reset_index(drop=True)
    on_clock = ev["Datetime"].isin(bar_index)
    if not on_clock.all():
        return {"status": "STALE_BAR_CLOCK", "rows_not_on_bar_clock": int((~on_clock).sum()),
                "note": "research dataset predates the current bars; re-export before trusting weights"}
    start = bar_index.get_indexer(pd.DatetimeIndex(ev["Datetime"]))
    end = snap_to_bar_clock(label_end_times(ev), bar_index)
    table = compute_afml_event_weight_table(ev["Datetime"], bar_index[end], bar_index, bars["Close"])
    u = table["average_uniqueness"]
    latest_end = np.maximum.accumulate(end)[:-1]
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    std = rng.integers(0, len(start), len(start))
    seq = sequential_bootstrap(start, end, len(bar_index), random_state=BOOTSTRAP_SEED)
    return {
        "status": "OK",
        "t1_source": "label_t1 (fallback gross_t1) snapped to the bar clock",
        "mean_uniqueness": round(float(u.mean()), 4),
        "median_uniqueness": round(float(u.median()), 4),
        "effective_independent_outcomes": round(float(u.sum()), 1),
        "rows": int(len(u)),
        "share_starting_while_previous_open": round(float((start[1:] <= latest_end).mean()), 4),
        "uniqueness_ar1": round(float(u.autocorr(1)), 4),
        "median_hold_bars": float(np.median(end - start + 1)),
        "bootstrap_mean_uniqueness": {
            "standard": round(float(get_avg_uniqueness(start, end, len(bar_index), std).mean()), 4),
            "sequential": round(float(get_avg_uniqueness(start, end, len(bar_index), seq).mean()), 4),
            "seed": BOOTSTRAP_SEED,
        },
    }


def audit_edge(edge_root: Path, live_thresholds: dict[str, float] | None) -> dict[str, Any]:
    asset, name = edge_root.parts[-3], edge_root.name
    edge_id = f"{asset}/{name}"
    artifacts = edge_root / "artifacts"
    resolution = next((artifacts / "research_dataset").glob("*_mldp_execution_resolution.csv"), None)
    if resolution is None:
        return {"edge_id": edge_id, "status": "SKIPPED", "reason": "no *_mldp_execution_resolution.csv"}

    cols = {"Event_Vol", "barrier_hit", "resolution_source", "realized_r", "label_r", "executed_r",
            "quote_executable_meta_label", "mldp_target_eligible"}
    d = pd.read_csv(resolution, usecols=lambda c: c in cols)
    if "mldp_target_eligible" in d:
        d = d[d["mldp_target_eligible"].astype(str).str.lower().isin(TRUE_STRINGS)]
    y = pd.to_numeric(d["quote_executable_meta_label"], errors="coerce")
    vol = pd.to_numeric(d["Event_Vol"], errors="coerce")
    vertical = d["barrier_hit"].eq("vertical")
    vertical_r = pd.to_numeric(d.loc[vertical, "realized_r"], errors="coerce")

    out: dict[str, Any] = {
        "edge_id": edge_id,
        "status": "OK",
        "audit_version": AUDIT_VERSION,
        "written_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(resolution.relative_to(V2_ROOT).as_posix()),
        "labels": {
            "rows": int(len(d)),
            "positive_rate": round(float(y.mean()), 4),
            "pt_share": round(float(d["barrier_hit"].eq("pt").mean()), 4),
            "sl_share": round(float(d["barrier_hit"].eq("sl").mean()), 4),
            "vertical_share": round(float(vertical.mean()), 4),
            "vertical_positive_share": round(float((vertical_r > 0).mean()), 4) if len(vertical_r) else None,
            "tie_tick_resolved": int(d["resolution_source"].eq("tick").sum()),
            "tie_fallback": int(d["resolution_source"].astype(str).str.startswith("fallback").sum()),
            "event_vol_bps": {q: round(float(vol.quantile(p) * 1e4), 2)
                              for q, p in (("min", 0.0), ("p01", 0.01), ("median", 0.5), ("p99", 0.99))},
        },
    }

    if {"label_r", "executed_r"} <= set(d.columns):
        econ = d.assign(Event_Vol=vol, label_r=pd.to_numeric(d["label_r"], errors="coerce"),
                        executed_r=pd.to_numeric(d["executed_r"], errors="coerce")).dropna(subset=["label_r", "executed_r"])
        econ["quintile"] = pd.qcut(econ["Event_Vol"], 5, labels=False, duplicates="drop")
        out["cost_by_event_vol_quintile"] = [
            {
                "quintile": int(q),
                "event_vol_median_bps": round(float(g["Event_Vol"].median() * 1e4), 2),
                "rows": int(len(g)),
                "mean_label_r": round(float(g["label_r"].mean()), 4),
                "mean_executed_r": round(float(g["executed_r"].mean()), 4),
                "mean_cost_r": round(float((g["label_r"] - g["executed_r"]).mean()), 4),
            }
            for q, g in econ.groupby("quintile")
        ]

    oof_path = next((artifacts / "calibration").glob("*_calibrated_oof_predictions.csv"), None)
    threshold = None if live_thresholds is None else live_thresholds.get(edge_id)
    threshold_source = "allocator_final_live_threshold"
    out["threshold_mode"] = "edge_level_threshold_free" if live_thresholds is None else "allocator_live_threshold"
    if oof_path is not None and threshold is not None:
        oof = pd.read_csv(oof_path, usecols=lambda c: c in ("meta_label", "calibrated_probability",
                                                            "live_calibrated_probability"))
        prob_col = "live_calibrated_probability" if "live_calibrated_probability" in oof else "calibrated_probability"
        oof_y = pd.to_numeric(oof["meta_label"], errors="coerce")
        prob = pd.to_numeric(oof[prob_col], errors="coerce")
        keep = oof_y.notna() & prob.notna()
        oof_y, prob = oof_y[keep].astype(int), prob[keep]
        primary = _classification(oof_y, pd.Series(True, index=oof_y.index))
        meta = _classification(oof_y, prob >= threshold)
        out["exercise_3_5"] = {
            "threshold": threshold,
            "threshold_source": threshold_source,
            "probability_column": prob_col,
            "rows": int(len(oof_y)),
            "primary_only": primary,
            "primary_plus_meta": meta,
            "meta_raises_f1": bool(meta["f1"] > primary["f1"]),
            "meta_raises_precision": bool(meta["precision"] > primary["precision"]),
        }

    out["afml_ch4_overlap"] = _overlap(edge_root, asset, resolution)

    manifest_path = artifacts / "model" / "model_training_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scoring = manifest.get("scoring", {})
        baseline = scoring.get("always_positive_cpcv_f1_baseline", {})
        out["step02_cpcv_f1"] = {
            "metric": scoring.get("primary_metric"),
            "model_f1": scoring.get("best_cpcv_f1"),
            "approve_all_f1": baseline.get("mean_test_f1"),
            "model_minus_approve_all": baseline.get("mean_model_minus_baseline_f1"),
        }

    dest = artifacts / "label_audit" / "label_audit.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def _discover(edges: list[str] | None) -> list[Path]:
    if edges:
        roots = []
        for edge_id in edges:
            asset, name = edge_id.split("/", 1)
            roots.append(V2_ROOT / "assets" / asset / "edges" / name)
        return roots
    return sorted(p for p in (V2_ROOT / "assets").glob("*/edges/*") if (p / "model_rows.py").exists())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--edges", nargs="*", help="ASSET/family_side ids; default = every edge folder")
    parser.add_argument("--live-threshold", action="store_true",
                        help="allocator stage only: add Exercise 3.5 at the allocator's final_live_threshold (D081)")
    args = parser.parse_args(argv)

    thresholds = _live_thresholds() if args.live_threshold else None
    rows = []
    for root in _discover(args.edges):
        result = audit_edge(root, thresholds)
        if result["status"] != "OK":
            print(f"{result['edge_id']}: {result['status']} ({result.get('reason')})")
            continue
        lab, ex = result["labels"], result.get("exercise_3_5", {})
        cv = result.get("step02_cpcv_f1", {})
        rows.append({
            "edge": result["edge_id"],
            "rows": lab["rows"],
            "pos_rate": lab["positive_rate"],
            "pt/sl/vert": f"{lab['pt_share']:.2f}/{lab['sl_share']:.2f}/{lab['vertical_share']:.2f}",
            "thr": ex.get("threshold"),
            "approve": ex.get("primary_plus_meta", {}).get("approved_share"),
            "prec p->m": f"{ex['primary_only']['precision']:.3f}->{ex['primary_plus_meta']['precision']:.3f}" if ex else None,
            "F1 p->m": f"{ex['primary_only']['f1']:.3f}->{ex['primary_plus_meta']['f1']:.3f}" if ex else None,
            "cpcv F1-allpos": cv.get("model_minus_approve_all"),
            "meanU": result["afml_ch4_overlap"].get("mean_uniqueness", result["afml_ch4_overlap"]["status"]),
            "effN": result["afml_ch4_overlap"].get("effective_independent_outcomes"),
            "seqU std->seq": (lambda b: f"{b['standard']:.3f}->{b['sequential']:.3f}" if b else None)(
                result["afml_ch4_overlap"].get("bootstrap_mean_uniqueness")),
        })
    if rows:
        pd.set_option("display.width", 250)
        print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
