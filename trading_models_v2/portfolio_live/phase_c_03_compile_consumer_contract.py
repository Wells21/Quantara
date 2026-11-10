"""Compile and freeze the Phase C.3 edge-consumer contract."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_V2_ROOT = HERE.parent
if str(DEFAULT_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_V2_ROOT))

from shared.edges.phase_c_consumer_contract import (
    compile_consumer_contract,
    summarize_consumer_contract,
)

DEFAULT_OUTPUT = HERE / "config" / "phase_c" / "consumer_contract.json"
DEFAULT_REPORT = HERE / "config" / "phase_c" / "consumer_contract_report.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    contract = compile_consumer_contract(v2_root=args.v2_root)
    report = summarize_consumer_contract(contract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if contract.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
