"""RECERT step 1 correction: re-read one edge's 2026-10-31 Gate C runs restricted to events on or after the first bar
that carries every feature (here: the HMM regime features, which start later after the 2026-10-23 FFD / HMM rebuild,
D031). The 2026-10-31 lab took its valid bar times from the stored pre-rebuild dataset, so it replayed early events
that the re-exported dataset cannot hold. Gate C replays each event independently, so dropping those trades from the
saved trade files equals a replay without them.

Same rules as recert_step1_reselection.py (405 grid + control, Gate B = Gate A net_mean_R > 0, gate_c_pbo winner rule,
PBO over 405 + control and over all 540 + control), plus the stress replay's trades with the same cut.

usage (from trading_models_v2, venv312): python shared/research/recert_step1_feature_window.py ASSET/edge OUT.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
import shared.research.gate_c_pbo as gcp  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

LOG = V2 / "logs" / "ewm_span_test_20261031"
SPANS = (100, 150, 200)
KEY = ["event_vol_lookback", "event_vol_ewm_span", "vertical_horizon", "pt", "sl"]


def main(edge: str, out: Path) -> None:
    asset, name = edge.split("/")
    root = V2 / "assets" / asset / "edges" / name
    art = root / "artifacts" / "v3_horizon_lab"
    reg = pd.read_csv(V2 / "assets" / asset / "bars" / "regime" / f"{asset.lower()}_hmm_regime_features.csv",
                      usecols=["Datetime", "Regime_State"])
    cut = to_datetime_iso(reg.loc[reg.Regime_State.notna(), "Datetime"].iloc[0], utc=True)
    orig_read = pd.read_csv

    def read_cut(path, *a, **k):
        df = orig_read(path, *a, **k)
        if "v3_execution_trades_" in str(path) and "event_ts" in df.columns:
            df = df[to_datetime_iso(df.event_ts, utc=True) >= cut]
        return df

    gcp.pd.read_csv = read_cut          # build_performance_matrix reads the trade files through gate_c_pbo's pd
    try:
        recs = [json.loads(x) for x in (LOG / f"{asset}_{name}_ewm.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        stamps, grid = [r["stamp"] for r in recs], recs[0]["grid_stamp"]
        summ = orig_read(art / f"v3_execution_velocity_summary_{stamps[0]}.csv").iloc[0:0]
        summ = pd.concat([orig_read(art / f"v3_execution_velocity_summary_{s}.csv").assign(run_stamp=s) for s in stamps],
                         ignore_index=True).drop_duplicates(subset="label")
        ctrl = summ.is_control.fillna(False).astype(bool)
        ga = orig_read(art / f"v3_hybrid_fast_grid_{grid}.csv")
        for c in KEY:
            ga[c] = ga[c].astype(float)
            summ[c] = summ[c].astype(float)
        nc = summ[~ctrl].merge(ga[KEY + ["net_mean_R"]], on=KEY, how="left")
        # per-cell statistics recomputed on the cut trade set
        rows = []
        for r in nc.itertuples():
            t = read_cut(art / f"v3_execution_trades_{r.label}_{r.run_stamp}.csv")
            exp = t[t.status.isin(["resolved"]) | t.status.notna()]
            res = t[t.status == "resolved"].dropna(subset=["exec_r"])
            rows.append({"label": r.label, "run_stamp": r.run_stamp, "span": r.event_vol_ewm_span, "net_mean_R": r.net_mean_R,
                         "exec_mean_R": float(res.exec_r.mean()) if len(res) else np.nan,
                         "coverage_rate": len(res) / max(len(exp), 1), "resolved_trades": int(len(res))})
        cells = pd.DataFrame(rows)
        g405 = cells[cells.span.isin(SPANS)]
        elig = g405[(g405.net_mean_R > 0) & (g405.exec_mean_R > 0) & (g405.coverage_rate >= gcp.MIN_COVERAGE)]
        _, scores = gcp.build_performance_matrix(art, elig[["label", "run_stamp"]])
        win = max(scores, key=scores.get)
        w = elig[elig.label == win].iloc[0]
        grid_cells = pd.concat([summ[ctrl][["label", "run_stamp"]], g405[["label", "run_stamp"]]])
        p405 = gcp.pbo_from_matrix(gcp.build_performance_matrix(art, grid_cells)[0])
        p540 = gcp.pbo_from_matrix(gcp.build_performance_matrix(art, summ[["label", "run_stamp"]])[0])
        st = json.loads((V2 / "logs" / "recert_20261103" / "stress" / f"{asset}_{name}_stress.json").read_text(encoding="utf-8"))
        stress = None
        if st.get("winner") == win and st.get("stress_stamp"):
            tt = read_cut(art / f"v3_execution_trades_{win}_{st['stress_stamp']}.csv")
            tt = tt[tt.status == "resolved"]
            stress = {"spread_factor": st["spread_factor"], "stress_exec_mean_R": float(tt.exec_r.mean()), "trades": int(len(tt))}
    finally:
        gcp.pd.read_csv = orig_read
    res = {"edge": edge, "feature_window_start": str(cut), "winner": win, "winner_cv_score": scores[win],
           "winner_exec_mean_R": float(w.exec_mean_R), "winner_resolved_trades": int(w.resolved_trades),
           "winner_coverage": float(w.coverage_rate), "gate_b_eligible": int(len(elig)),
           "pbo_405_plus_control": {"pbo": round(float(p405["pbo"]), 4), "prob_oos_nonpositive": round(float(p405["prob_oos_winner_nonpositive"]), 4)},
           "pbo_540_plus_control": {"pbo": round(float(p540["pbo"]), 4), "prob_oos_nonpositive": round(float(p540["prob_oos_winner_nonpositive"]), 4)},
           "stress_same_cut": stress,
           "top5_by_cv": sorted(scores.items(), key=lambda kv: -kv[1])[:5]}
    out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps(res, default=str), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
