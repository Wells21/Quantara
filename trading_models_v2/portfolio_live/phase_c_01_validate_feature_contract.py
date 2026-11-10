"""Validate that the frozen Phase C.1 contract still matches current sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_V2_ROOT = HERE.parent
if str(DEFAULT_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_V2_ROOT))

from shared.edges.phase_c_feature_contract import validate_frozen_contract
DEFAULT_CONTRACT = HERE / "config" / "phase_c" / "feature_contract.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args()

    report = validate_frozen_contract(
        v2_root=args.v2_root.resolve(),
        contract_path=args.contract.resolve(),
        live_manifest_path=(args.manifest.resolve() if args.manifest is not None else None),
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
