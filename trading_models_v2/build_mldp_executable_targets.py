#!/usr/bin/env python3
"""CLI for Quantara MLDP Task 2A executable-outcome audits."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

_THIS = Path(__file__).resolve()
V2_ROOT = _THIS.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.execution.executable_target_dataset import (
    ExecutableTargetEngineError,
    compatibility_report,
    discover_edge_roots,
    resolve_edge_executable_targets,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Quantara MLDP Task 2A: exact Bid/Ask outcomes before model "
            "training. Task 2A is audit-only and does not migrate Step 01-09."
        )
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--edge-root",
        help="Path to assets/<ASSET>/edges/<family>_<side>.",
    )
    g.add_argument(
        "--asset",
        help="Audit/resolve every discovered edge for one asset.",
    )
    g.add_argument(
        "--all",
        action="store_true",
        help="Audit/resolve every discovered edge.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Only discover packages and verify source/artifact presence; "
            "no tick replay."
        ),
    )
    p.add_argument(
        "--strict-coverage",
        action="store_true",
        help="After diagnostics, fail if any source event is unresolved.",
    )
    p.add_argument(
        "--hash-ticks",
        action="store_true",
        help="SHA-256 every tick file (can be slow on large histories).",
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Resolve in memory only.",
    )
    return p.parse_args()


def _selected_roots(args: argparse.Namespace) -> list[Path]:
    if args.edge_root:
        return [Path(args.edge_root).resolve()]
    return discover_edge_roots(
        V2_ROOT, asset=args.asset if args.asset else None
    )


def main() -> None:
    args = _parse_args()
    roots = _selected_roots(args)
    if not roots:
        raise ExecutableTargetEngineError(
            "No compatible edge package directories were discovered."
        )

    pairs = [(root, compatibility_report(root)) for root in roots]
    assets = sorted({report["asset"] for _, report in pairs})
    ready = [
        (root, report)
        for root, report in pairs
        if report["compatible_for_resolution"]
    ]
    blocked = [
        (root, report)
        for root, report in pairs
        if not report["compatible_for_resolution"]
    ]

    print("=" * 100)
    print("QUANTARA MLDP TASK 2A - SHARED EXECUTABLE-TARGET STATION")
    print("=" * 100)
    print(f"Discovered edge packages : {len(pairs)}")
    print(f"Assets                   : {len(assets)} ({', '.join(assets)})")
    print(f"Ready model-row packages : {len(ready)}")
    print(f"Blocked/no artifacts      : {len(blocked)}")
    print(
        "Training authority        : AUDIT ONLY - Step 01-09 migration is NOT active"
    )

    if blocked:
        print("\nBlocked packages:")
        for _, report in blocked:
            failed = [
                k for k, ok in report["checks"].items() if not ok
            ]
            print(f"  - {report['edge_id']}: {', '.join(failed)}")

    if args.dry_run:
        print("\nDRY RUN COMPLETE - no tick replay and no files written.")
        return

    if not ready:
        raise ExecutableTargetEngineError(
            "No selected edge has the existing gross model-row artifacts "
            "required for Task 2A."
        )

    failures: list[str] = []
    for n, (edge_root, report) in enumerate(ready, start=1):
        print(f"\n[{n}/{len(ready)}] {report['edge_id']}")
        try:
            resolve_edge_executable_targets(
                edge_root,
                write=not args.no_write,
                strict_coverage=args.strict_coverage,
                hash_tick_contents=args.hash_ticks,
            )
        except Exception as exc:
            failures.append(f"{report['edge_id']}: {exc}")
            print(f"FAILED: {exc}")

    if failures:
        raise ExecutableTargetEngineError(
            "One or more edge audits failed:\n- " + "\n- ".join(failures)
        )


if __name__ == "__main__":
    try:
        main()
    except ExecutableTargetEngineError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
