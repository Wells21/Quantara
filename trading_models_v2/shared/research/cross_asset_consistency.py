"""AFML Ch.11 s.11.5 #1 (p.154) cross-asset consistency flag (owner-approved 2026-10-26).

"If you find mistake X only on security Y, no matter how apparently profitable, it is likely a false discovery." For
every deployable edge (config/edge_universe.json, enabled), report on how many assets its family-side thesis was tried
(config/cross_asset_thesis_coverage.json) and on how many it is deployable. Diagnostic only: judged together with the
Ch.14 deflated Sharpe ratio, never a gate on its own.

usage (from trading_models_v2):  python -m shared.research.cross_asset_consistency
writes config/edge_cross_asset_consistency.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]


def build() -> dict:
    cov = json.loads((V2 / "config" / "cross_asset_thesis_coverage.json").read_text(encoding="utf-8"))["matrix"]
    uni = json.loads((V2 / "config" / "edge_universe.json").read_text(encoding="utf-8"))["edges"]
    deploy: dict[str, list[str]] = {}
    for e in uni:
        if e.get("enabled"):
            deploy.setdefault(f"{e['family']}_{e['side']}", []).append(e["asset"])
    edges = {}
    for e in uni:
        if not e.get("enabled"):
            continue
        fam = f"{e['family']}_{e['side']}"
        tried = sorted(set(cov.get(fam, {}).get("attempted_on", [])) | set(deploy[fam]))
        k, n = len(deploy[fam]), len(tried)
        edges[f"{e['asset']}/{fam}"] = {
            "family_side": fam, "deployable_on": sorted(deploy[fam]), "tried_on": tried,
            "worked_on_k_of_n": f"{k} of {n}", "k": k, "n": n,
            "single_asset_flag": bool(k == 1 and n > 1),
        }
    return {"schema_version": "edge_cross_asset_consistency_v1",
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "AFML Ch.11 s.11.5 #1 p.154; config/cross_asset_thesis_coverage.json + config/edge_universe.json",
            "note": "Diagnostic only (owner 2026-10-26): judge with the Ch.14 deflated Sharpe ratio. 'worked' = deployable now.",
            "edges": edges}


def main() -> int:
    out = build()
    (V2 / "config" / "edge_cross_asset_consistency.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    for k, v in out["edges"].items():
        print(f"{k:45s} worked on {v['worked_on_k_of_n']:8s} single_asset_flag={v['single_asset_flag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
