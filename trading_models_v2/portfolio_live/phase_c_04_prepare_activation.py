from __future__ import annotations

"""Prepare (or explicitly write) the Phase C.4 production-routing activation.

By default this script is read-only.  It validates C.1/C.2/C.3 and prints the
candidate activation fingerprint.  Writing the activation requires BOTH
``--activate`` and the long acknowledgement flag so a routine validator run
cannot accidentally switch production routing.
"""

import argparse
import json
from pathlib import Path

from phase_c_04_production_router import (
    ACTIVATION_PATH,
    PRODUCTION_CERTIFICATION_PATH,
    ROUTER_VERSION,
    build_activation_payload,
    static_contract_report,
    validate_production_certification,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--v2-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--activate", action="store_true")
    parser.add_argument(
        "--i-understand-this-enables-phase-c4-production-routing",
        action="store_true",
        dest="ack",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override activation path (normally portfolio_live/config/phase_c/production_routing_activation.json).",
    )
    args = parser.parse_args()

    root = args.v2_root.resolve()
    report = static_contract_report(root)
    print("=" * 112)
    print("PHASE C.4 — PRODUCTION ROUTING ACTIVATION PREP")
    print("=" * 112)
    print(f"Router version: {ROUTER_VERSION}")
    print(f"V2 root:        {root}")
    print(f"Static status:  {report.get('status')}")
    print(f"Assets:         {report.get('selected_asset_count')}")
    print(f"Edges:          {report.get('selected_edge_count')}")
    print(f"Ring authority: {report.get('consumer_ring_rows')} rows")
    print("Normal batch fallback: FORBIDDEN")
    if report.get("failures"):
        print("\nFAILURES:")
        for value in report["failures"]:
            print(f"  - {value}")
        return 2

    payload = build_activation_payload(root)
    print(f"Candidate activation fingerprint: {payload['activation_fingerprint']}")
    print("Candidate status: PASS")
    print("Routing enabled in candidate: True")

    if not args.activate:
        print("\nREAD-ONLY CHECK COMPLETE.")
        print("No activation file was written and current production routing is unchanged.")
        return 0

    if not args.ack:
        raise SystemExit(
            "Refusing to write activation. Re-run with "
            "--i-understand-this-enables-phase-c4-production-routing only after "
            "the required pre-production tests have passed."
        )

    if not PRODUCTION_CERTIFICATION_PATH.is_file():
        raise SystemExit(
            "Refusing to write activation: sealed Phase C.5 production certification is missing. "
            "Run phase_c_05_certify_production_routing.py --seal-production on the clean release first."
        )
    certification = json.loads(PRODUCTION_CERTIFICATION_PATH.read_text(encoding="utf-8-sig"))
    certification_validation = validate_production_certification(
        certification, payload, v2_root=root
    )
    if certification_validation.get("status") != "PASS":
        raise SystemExit(
            "Refusing to write activation: Phase C.5 production certification is stale/invalid: "
            + " | ".join(str(x) for x in certification_validation.get("failures") or [])
        )
    print(
        "Phase C.5 release certificate: PASS "
        f"({certification.get('certificate_fingerprint')})"
    )

    output = (
        args.output.resolve()
        if args.output is not None
        else root / "portfolio_live" / "config" / "phase_c" / "production_routing_activation.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name("." + output.name + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(output)
    print(f"\nACTIVATION WRITTEN: {output}")
    print("Restart the dashboard for Phase C.4 routing to install.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
