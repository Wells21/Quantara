#!/usr/bin/env python3
"""Report candidate stale artifacts for an MLDP transformation change."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from shared.validation.mldp_invalidation import CHANGE_SCOPES, scan_invalidation

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--change", action="append", required=True, choices=sorted(CHANGE_SCOPES))
    parser.add_argument("--json-out")
    args = parser.parse_args()
    report = scan_invalidation(Path(__file__).resolve().parent, args.change)
    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")

if __name__ == "__main__":
    main()
