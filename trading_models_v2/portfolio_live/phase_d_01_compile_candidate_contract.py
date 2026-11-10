from __future__ import annotations

"""Compile the Phase D.1 candidate-detector source contract.

OFFLINE ONLY. This script never activates Phase D routing and never changes
dashboard/executor production behavior.
"""

import argparse
import json
import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
for path in (V2_ROOT, V2_ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.phase_d_candidate_contract import compile_candidate_contract  # noqa: E402


DEFAULT_OUTPUT = (
    V2_ROOT
    / "portfolio_live"
    / "config"
    / "phase_d"
    / "candidate_contract.json"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile Phase D.1 candidate detector inventory/contract."
    )
    parser.add_argument("--v2-root", default=str(V2_ROOT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    root = Path(args.v2_root).resolve()
    output = Path(args.output).resolve()

    report = compile_candidate_contract(v2_root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print("=" * 112)
    print("PHASE D.1 — CANDIDATE DETECTOR CONTRACT / INVENTORY")
    print("=" * 112)
    print(f"V2 root:                    {root}")
    print(f"Output:                     {output}")
    print(f"Status:                     {report.get('status')}")
    print(f"Assets:                     {report.get('selected_asset_count')}")
    print(f"Edges:                      {report.get('selected_edge_count')}")
    print(f"Compiled edges:             {report.get('compiled_edge_count')}")
    print(f"Failed edges:               {report.get('failed_edge_count')}")
    print(f"Candidate logic groups:     {report.get('candidate_logic_group_count')}")
    print(f"Multi-edge logic groups:    {report.get('multi_edge_logic_group_count')}")
    print(f"Contract SHA256:            {report.get('contract_sha256')}")
    print("Production routing enabled: False")
    print()
    print("--- HISTORY SHAPES ---")
    for name, count in (report.get("history_shape_counts") or {}).items():
        print(f"{count:>4}  {name}")
    print()
    print("--- LARGEST SOURCE-DERIVED CANDIDATE LOGIC GROUPS ---")
    for row in (report.get("candidate_logic_groups") or [])[:12]:
        print(
            f"{int(row.get('edge_count') or 0):>4}  "
            f"{str(row.get('candidate_logic_signature') or '')[:16]}  "
            + ", ".join((row.get("edge_ids") or [])[:6])
            + (" ..." if len(row.get("edge_ids") or []) > 6 else "")
        )
    if report.get("fatal_errors"):
        print()
        print("--- FATAL ERRORS ---")
        for error in (report.get("fatal_errors") or [])[:30]:
            print(f"- {error}")
    print()
    print(
        "D.1 is inventory/certification planning only. "
        "No Phase D production routing was installed."
    )
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
