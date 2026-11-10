"""AFML s.8.5 (pp.121-122) parallelised feature importance across edges (diagnostic).

Reads every edge's artifacts/feature_importance_audit/feature_importance_audit.json (shared/research/
feature_importance_audit.py), ranks each feature per edge by per-feature MDA, MDI and SFI, and reports:
  * mean rank of each feature across edges per criterion (features important across instruments are more likely tied to
    a real phenomenon, AFML p.121)
  * rank agreement between edges (Spearman of per-feature MDA) and between criteria within each edge
Stacked importance (one model on all edges' data, AFML p.121 "features stacking") is not done here (architecture change).

Run from trading_models_v2:  python ./shared/research/cross_edge_feature_importance.py
Writes docs/mldp/feature_importance/cross_edge_feature_importance.json and prints the top features.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

V2_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    audits = {}
    for path in sorted(V2_ROOT.glob("assets/*/edges/*/artifacts/feature_importance_audit/feature_importance_audit.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("status") == "OK":
            audits[rec["edge_id"]] = rec
    if len(audits) < 2:
        print("need at least two audited edges")
        return 1
    ranks: dict[str, pd.DataFrame] = {}
    for crit in ("mda", "mdi", "sfi"):
        cols = {}
        for edge, rec in audits.items():
            s = pd.Series({f: v["mean"] for f, v in rec["per_feature"][crit].items()}, dtype="float64")
            cols[edge] = s.rank(ascending=False)  # 1 = most important (SFI: higher neg log loss is better)
        ranks[crit] = pd.DataFrame(cols)
    summary = pd.DataFrame({f"mean_rank_{c}": r.mean(axis=1) for c, r in ranks.items()}).sort_values("mean_rank_mda")
    edge_corr = ranks["mda"].corr(method="spearman").round(3)
    within = {e: {"mda_vs_mdi": round(float(ranks["mda"][e].corr(ranks["mdi"][e], method="spearman")), 3),
                  "mda_vs_sfi": round(float(ranks["mda"][e].corr(ranks["sfi"][e], method="spearman")), 3),
                  "mdi_vs_sfi": round(float(ranks["mdi"][e].corr(ranks["sfi"][e], method="spearman")), 3)}
              for e in audits}
    out = {"edges": list(audits), "n_features": int(len(summary)),
           "top_20_by_mean_mda_rank": summary.head(20).round(1).to_dict(orient="index"),
           "bottom_20_by_mean_mda_rank": summary.tail(20).round(1).to_dict(orient="index"),
           "edge_rank_agreement_spearman_mda": edge_corr.to_dict(),
           "within_edge_criterion_agreement": within}
    dest = V2_ROOT / "docs" / "mldp" / "feature_importance" / "cross_edge_feature_importance.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(summary.head(15).round(1).to_string())
    print("\nMDA rank agreement between edges (Spearman):\n", edge_corr.to_string())
    print("\nWithin-edge criterion agreement:", json.dumps(within, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
