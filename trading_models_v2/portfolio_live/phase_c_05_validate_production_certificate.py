from __future__ import annotations

"""Read-only validator for a sealed Phase C.5 production certificate."""
import argparse
import json
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
V2_ROOT=HERE.parent
if str(V2_ROOT) not in sys.path: sys.path.insert(0,str(V2_ROOT))
import phase_c_04_production_router as c4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=V2_ROOT)
    parser.add_argument("--certificate", type=Path, default=c4.PRODUCTION_CERTIFICATION_PATH)
    parser.add_argument("--activation", type=Path, default=c4.ACTIVATION_PATH)
    args = parser.parse_args()
    root = args.v2_root.resolve()

    print("=" * 112)
    print("PHASE C.5 — CURRENT-STATE PRODUCTION CERTIFICATE VALIDATOR")
    print("=" * 112)
    if not args.certificate.is_file():
        print(f"STATUS: FAIL\nCertificate missing: {args.certificate}")
        return 2

    if not args.activation.is_file():
        activation = c4.build_activation_payload(root)
        activation_source = "read_only_candidate"
    else:
        activation = json.loads(args.activation.read_text(encoding="utf-8-sig"))
        activation_source = "written_activation"

    cert = json.loads(args.certificate.read_text(encoding="utf-8-sig"))
    result = c4.validate_production_certification(
        cert,
        activation,
        v2_root=root,
    )
    print(f"Activation source: {activation_source}")
    print(
        "Identity mode:     "
        + str(
            result.get("authority_identity_mode")
            or "structural_current_state_no_git_or_file_sha_gate"
        )
    )
    print(f"Selected assets:   {result.get('selected_asset_count')}")
    print(f"Selected edges:    {result.get('selected_edge_count')}")
    if result.get("failures"):
        for value in result["failures"]:
            print(f"  - {value}")
    print(f"STATUS: {result.get('status')}")
    return 0 if result.get("status") == "PASS" else 2



if __name__=="__main__": raise SystemExit(main())
