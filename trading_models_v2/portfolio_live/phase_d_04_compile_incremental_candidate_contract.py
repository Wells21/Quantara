from __future__ import annotations

"""Compile the inert D4 source-signature continuation-provider requirements."""

import argparse
import json
import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
for path in (LIVE_DIR, V2_ROOT, V2_ROOT.parent):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from shared.edges.phase_d_incremental_candidate_contract import (  # noqa: E402
    compile_incremental_candidate_contract,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", default=str(V2_ROOT))
    parser.add_argument("--output", default=None)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()

    root = Path(args.v2_root).resolve()
    output = (
        Path(args.output).resolve()
        if args.output
        else root
        / "portfolio_live/config/phase_d/incremental_candidate_contract.json"
    )

    report = compile_incremental_candidate_contract(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    print("=" * 112)
    print("PHASE D.4.1 — SOURCE-SIGNATURE CONTINUATION PROVIDER REQUIREMENTS")
    print("=" * 112)
    print("V2 root:                 ", root)
    print("Output:                  ", output)
    print("Status:                  ", report.get("status"))
    print("Assets:                  ", report.get("selected_asset_count"))
    print("Edges:                   ", report.get("selected_edge_count"))
    print("Compiled edges:          ", report.get("compiled_edge_count"))
    print("Candidate logic groups:  ", report.get("candidate_logic_group_count"))
    print("Bound providers:         ", report.get("bound_provider_count"))
    print("Unbound logic groups:    ", report.get("unbound_logic_group_count"))
    print("Provider binding mode:   ", report.get("provider_binding_mode"))
    print("Routing enabled:         ", report.get("runtime_routing_enabled"))
    print("Activation allowed:      ", report.get("production_activation_allowed"))
    print("Identity mode:           ", report.get("authority_identity_mode"))
    print()
    print("--- LOGIC GROUPS REQUIRING PROVIDERS ---")
    for group in report.get("candidate_logic_groups") or []:
        print(
            f"{group['candidate_logic_signature'][:12]} "
            f"edges={group['edge_count']:2d} "
            f"entries={','.join(group['runtime_entry_functions'])}"
        )
    print()
    failures = report.get("failures") or []
    print("--- FAILURES ---")
    if failures:
        for failure in failures:
            print(" -", failure)
    else:
        print(" none")
    print()
    print("D4.1 IS INERT. No provider is bound and no production routing changed.")

    if args.require_pass and report.get("status") != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
