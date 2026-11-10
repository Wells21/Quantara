"""Compile the frozen Phase C.1 manifest-driven feature contract."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_V2_ROOT = HERE.parent
if str(DEFAULT_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_V2_ROOT))

from shared.edges.phase_c_feature_contract import compile_feature_contract, contract_summary
DEFAULT_OUTPUT = HERE / "config" / "phase_c" / "feature_contract.json"
DEFAULT_REPORT = HERE / "config" / "phase_c" / "feature_contract_report.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()

    v2_root = args.v2_root.resolve()
    manifest = args.manifest.resolve() if args.manifest is not None else None
    contract = compile_feature_contract(v2_root=v2_root, live_manifest_path=manifest)
    summary = contract_summary(contract)
    report = {
        "schema_version": "phase_c_feature_contract_report_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "selected_assets": contract.get("selected_assets"),
        "fatal_errors": contract.get("fatal_errors"),
        "review_items": contract.get("review_items"),
        "assets": {
            asset: {
                "edge_count": row.get("edge_count"),
                "declared_feature_name_count": row.get("declared_feature_name_count"),
                "structural_feature_variant_count": row.get("structural_feature_variant_count"),
                "shared_core_candidate_count": row.get("shared_core_candidate_count"),
                "addon_candidate_count": row.get("addon_candidate_count"),
                "variant_conflict_count": row.get("variant_conflict_count"),
                "source_inputs": row.get("source_inputs"),
                "structural_contract_group_count": len(row.get("structural_contract_groups") or []),
            }
            for asset, row in (contract.get("assets") or {}).items()
        },
    }

    _write_json(args.output.resolve(), contract)
    _write_json(args.report.resolve(), report)

    print("=" * 104)
    print("PHASE C.1 — MANIFEST-DRIVEN FEATURE INVENTORY / FROZEN DAG CONTRACT")
    print("=" * 104)
    print(f"V2 root:       {v2_root}")
    print(f"Manifest:      {contract['source_live_manifest_path']}")
    print(f"Assets:        {summary['selected_asset_count']}")
    print(f"Edges:         {summary['compiled_edge_count']}/{summary['selected_edge_count']}")
    print(f"Feature names: {summary['global_declared_feature_name_count']}")
    print(f"Variants(sum): {summary['global_structural_feature_variant_count']}")
    print(f"Shared(sum):   {summary['asset_shared_core_candidate_count_sum']}")
    print(f"Addons(sum):   {summary['asset_addon_candidate_count_sum']}")
    print(f"Conflicts(sum):{summary['asset_variant_conflict_count_sum']}")
    print(f"Review items:  {summary['review_item_count']}")
    print(f"Fatal errors:  {summary['fatal_error_count']}")
    print(f"Routing live:  {summary['runtime_routing_enabled']}")
    print(f"Contract SHA:  {summary['contract_sha256']}")
    print(f"Output:        {args.output.resolve()}")
    print(f"Report:        {args.report.resolve()}")
    for asset, row in report["assets"].items():
        print(
            f"  {asset:<8} edges={row['edge_count']:<3} "
            f"features={row['declared_feature_name_count']:<4} "
            f"variants={row['structural_feature_variant_count']:<4} "
            f"shared={row['shared_core_candidate_count']:<4} "
            f"addons={row['addon_candidate_count']:<4} "
            f"conflicts={row['variant_conflict_count']:<3} "
            f"groups={row['structural_contract_group_count']}"
        )
    if contract.get("review_items"):
        print("\nREVIEW ITEMS (not runtime routing failures):")
        for item in contract["review_items"]:
            print(f"  - {item}")
    if contract.get("fatal_errors"):
        print("\nFATAL CONTRACT ERRORS:")
        for item in contract["fatal_errors"]:
            print(f"  - {item}")
    print(f"\nSTATUS: {contract['status']}")
    if args.require_pass and contract["status"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
