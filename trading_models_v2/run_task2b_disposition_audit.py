#!/usr/bin/env python3
"""Task 2B.1 CLI: classify Task 2A's unresolved executable-target rows.

Reads the Task 2A resolution CSV/manifest an edge already has (from
`build_mldp_executable_targets.py`), rebuilds that edge's tick catalog, loads
the live executor's weekend-gap / session-blackout configuration, classifies
every unresolved row into TRUE_NO_FILL / RIGHT_CENSORED_END_OF_HISTORY /
DATA_DEFECT (see docs/mldp/TASK2B_POPULATION_RULE_DECISION.md), and writes an
addendum CSV plus a JSON summary next to the Task 2A outputs.

This does not modify Task 2A's own outputs and does not authorize training.
It is purely a diagnostic layer on top of audit-only data.

Usage (from trading_models_v2):
    python .\\run_task2b_disposition_audit.py --edge-root .\\assets\\BTCUSD\\edges\\absorption_long
    python .\\run_task2b_disposition_audit.py --all
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_THIS = Path(__file__).resolve()
V2_ROOT = _THIS.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

import pandas as pd

from shared.edges.edge_layout import get_edge_layout_from_edge_root
from shared.execution import oof_exact_execution as exact
from shared.execution.executable_target_dataset import (
    discover_edge_roots,
    target_artifact_paths,
)
from shared.execution.unresolved_event_disposition import (
    UnresolvedDispositionError,
    annotate_dispositions,
    load_live_calendar_guard_config,
    summarize_dispositions,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--edge-root", help="Path to assets/<ASSET>/edges/<family>_<side>.")
    g.add_argument("--asset", help="Audit every discovered edge for one asset.")
    g.add_argument("--all", action="store_true", help="Audit every discovered edge.")
    return p.parse_args()


def _selected_roots(args: argparse.Namespace) -> list[Path]:
    if args.edge_root:
        return [Path(args.edge_root).resolve()]
    return discover_edge_roots(V2_ROOT, asset=args.asset if args.asset else None)


def audit_one_edge(edge_root: Path, calendar_guard_config: dict) -> dict:
    layout = get_edge_layout_from_edge_root(edge_root)
    paths = target_artifact_paths(layout)
    if not paths.resolution_csv.exists():
        raise UnresolvedDispositionError(
            f"{layout.edge_id}: missing Task 2A resolution CSV: "
            f"{paths.resolution_csv}. Run build_mldp_executable_targets.py first."
        )

    resolution_df = pd.read_csv(paths.resolution_csv)
    runtime, model_rows_module = exact._import_edge_modules(layout.edge_root)
    catalog = exact._build_tick_catalog(model_rows_module)

    annotated = annotate_dispositions(
        resolution_df,
        asset=layout.asset,
        catalog=catalog,
        calendar_guard_config=calendar_guard_config,
    )
    summary = summarize_dispositions(annotated)
    summary["edge_id"] = layout.edge_id
    summary["asset"] = layout.asset

    out_csv = paths.resolution_csv.with_name(
        paths.resolution_csv.stem + "_task2b_disposition.csv"
    )
    out_json = paths.resolution_csv.with_name(
        paths.resolution_csv.stem + "_task2b_disposition_summary.json"
    )
    annotated.to_csv(out_csv, index=False)
    out_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["outputs"] = {"disposition_csv": str(out_csv), "summary_json": str(out_json)}
    return summary


def main() -> None:
    args = _parse_args()
    roots = _selected_roots(args)
    if not roots:
        raise UnresolvedDispositionError("No compatible edge package directories were discovered.")

    calendar_guard_config = load_live_calendar_guard_config(V2_ROOT)

    print("=" * 100)
    print("TASK 2B.1 - UNRESOLVED-EVENT DISPOSITION AUDIT")
    print("=" * 100)

    results = []
    failures = []
    for n, edge_root in enumerate(roots, start=1):
        print(f"\n[{n}/{len(roots)}] {edge_root}")
        try:
            summary = audit_one_edge(edge_root, calendar_guard_config)
        except UnresolvedDispositionError as exc:
            print(f"  SKIPPED: {exc}")
            continue
        results.append(summary)
        print(f"  edge_id: {summary['edge_id']}")
        print(f"  total_rows: {summary['total_rows']}")
        print(f"  disposition_counts: {summary['disposition_counts']}")
        print(f"  audit_status: {summary['audit_status']}")
        if summary["audit_status"] != "PASS":
            failures.append(summary["edge_id"])

    print("\n" + "=" * 100)
    print(f"Edges audited: {len(results)}")
    print(f"Edges with DATA_DEFECT rows: {len(failures)}")
    if failures:
        print("  -> " + ", ".join(failures))
        raise SystemExit(
            "ERROR: one or more edges have DATA_DEFECT unresolved rows. "
            "Per Task 2B policy these must be investigated at the source, "
            "not pooled with TRUE_NO_FILL/RIGHT_CENSORED_END_OF_HISTORY."
        )


if __name__ == "__main__":
    try:
        main()
    except UnresolvedDispositionError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
