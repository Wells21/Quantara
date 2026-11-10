"""Gate 0 verdict under D090 (owner 2026-11-09: "i approve this"; "all assets must not agree for an edge in a
particular asset to work"). Canonical: docs/mldp/rules/THEORY_CHECK_RULE_D075.md, section D090.

Gate 0 tests the THEORY on each asset's own events, per side. PASS on (asset, side) needs all of:
  1. full history: main counterfactual C1, own diff - 1.28 SE > 0 (one-sided 90 %, joint week-clustered SE);
  2. full history: every other card counterfactual, own diff > 0;
  3. recency: own C1 diff > 0 on 2022-01-01 onward;
  4. >= 100 edge events and >= 100 events in every counterfactual (else INCONCLUSIVE);
  5. firing rate: >= 2 edge events per week on 2022-01-01 onward (D092, owner 2026-11-10; was >= 3, D089).
Only assets in the card's pre-registered universe get a verdict (the mechanism must exist there); the rest are
reported as PLACEBO.
No pooling requirement: other assets need not agree; the pooled result is a diagnostic. The month-matched drift
baseline is a reported DIAGNOSTIC, not a gate.

Inputs: OUT_DIR/gate0_<THESIS>.json (full history) and OUT_DIR/gate0_<THESIS>_2022_on.json (2022+ run), both from
gate0_tool.py. With GATE0_CELL set (post-Gate C re-check), pass the files' tag suffix as TAG.

usage (venv312): python shared/research/gate0/gate0_verdict_d090.py THESIS OUT_DIR [TAG]
writes OUT_DIR/gate0_<THESIS><TAG>_verdict_d090.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

V2 = Path(__file__).resolve().parents[3]
RECENT = pd.Timestamp("2022-01-01", tz="UTC")
MIN_PER_WEEK = 2.0   # D092 (owner 2026-11-10); was 3.0 (D089)


def data_end(asset: str) -> pd.Timestamp:
    f = V2 / "assets" / asset / "bars" / "standard_quote_tick" / f"{asset.lower()}_standard_quote_tick_bars.csv"
    with f.open("rb") as h:
        h.seek(0, 2)
        h.seek(max(0, h.tell() - 4096))
        last = h.read().decode("utf-8", "ignore").strip().splitlines()[-1]
    return pd.Timestamp(last.split(",")[0]).tz_convert("UTC")


Z_C1, MIN_EVENTS = 1.28, 100   # = gate0_tool.Z_DRIFT_TIERED, MIN_EVENTS


def window_tests(full: dict) -> dict:
    """Same per-asset test as gate0_tool's window_tests_d090, rebuilt from the stored per-asset results (files written
    before D090 lack the field)."""
    out = {}
    for asset, ar in full["assets"].items():
        out[asset] = {}
        for name in ("LONG", "SHORT"):
            a = ar.get(name, {})
            n = int(a.get("n_edge") or 0)
            cfs = a.get("counterfactuals", {})
            if n < MIN_EVENTS or "C1" not in cfs:
                out[asset][name] = {"verdict": "INCONCLUSIVE", "n_edge": n}
                continue
            thin = [c for c, v in cfs.items() if int(v.get("n") or 0) < MIN_EVENTS]
            c1 = cfs["C1"]
            c1_ok = bool(c1["diff"] - Z_C1 * c1["se"] > 0)
            others_ok = all(v["diff"] > 0 for c, v in cfs.items() if c != "C1")
            out[asset][name] = {
                "verdict": "INCONCLUSIVE" if thin else ("PASS" if (c1_ok and others_ok) else "FAIL"), "n_edge": n,
                "C1_diff": c1["diff"], "C1_se": c1["se"], "C1_z": round(c1["diff"] / c1["se"], 3) if c1["se"] else None,
                "C1_pass_90": c1_ok, "other_cf_diffs": {c: w["diff"] for c, w in cfs.items() if c != "C1"},
                "other_cf_ok": others_ok, "thin_counterfactuals": thin,
                "diag_drift_month_matched": a.get("drift_month_matched", {}).get("diff"),
                "in_card_universe": asset in full.get("universe", [])}
    return out


def verdict(thesis: str, out: Path, tag: str = "") -> dict:
    full = json.loads((out / f"gate0_{thesis}{tag}.json").read_text(encoding="utf-8"))
    rec = json.loads((out / f"gate0_{thesis}{tag}_2022_on.json").read_text(encoding="utf-8"))
    res = {"thesis": thesis, "rule": "D090 (owner 2026-11-09)", "cell": full["cell"], "card": full["card"],
           "universe": full["universe"], "by_asset": {}}
    for asset, sides in (full.get("window_tests_d090") or window_tests(full)).items():
        weeks = max((data_end(asset) - RECENT).days / 7.0, 1e-9)
        res["by_asset"][asset] = {}
        for name, t in sides.items():
            r = rec["assets"].get(asset, {}).get(name, {})
            n_recent = int(r.get("n_edge") or 0)
            c1r = r.get("counterfactuals", {}).get("C1", {}).get("diff")
            per_week = n_recent / weeks
            row = {"full": t, "recent_n_edge": n_recent, "recent_events_per_week": round(per_week, 2),
                   "recent_C1_diff": c1r, "recent_diag_drift": r.get("drift_month_matched", {}).get("diff")}
            if asset not in full["universe"]:
                row["verdict"] = "PLACEBO"           # outside the card's pre-registered universe: reported, no verdict
            elif t["verdict"] == "INCONCLUSIVE":
                row["verdict"] = "INCONCLUSIVE"
            else:
                row["recency_ok"] = bool(c1r is not None and c1r > 0)
                row["firing_ok"] = bool(per_week >= MIN_PER_WEEK)
                row["verdict"] = "PASS" if (t["verdict"] == "PASS" and row["recency_ok"] and row["firing_ok"]) else "FAIL"
            res["by_asset"][asset][name] = row
    p = out / f"gate0_{thesis}{tag}_verdict_d090.json"
    p.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    for asset, sides in res["by_asset"].items():
        print(asset, {k: (v["verdict"], v["full"].get("C1_z"), v["recent_events_per_week"]) for k, v in sides.items()})
    print("written", p)
    return res


if __name__ == "__main__":
    a = sys.argv[1:] + [""]
    verdict(a[0], Path(a[1]), a[2])
