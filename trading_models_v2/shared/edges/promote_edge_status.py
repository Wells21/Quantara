"""Promote an edge lifecycle status after verified SOTD gates.

This tool exists to prevent manual one-off edits after Step 09.

Typical use after Step 09 PASS:

    python ./shared/edges/promote_edge_status.py \
        --edge-root ./assets/XAUUSD/edges/absorption_long \
        --status deployability_pass \
        --write

SOTD rule: deployability_pass may only be written when edge_manifest.json proves
PASS, deployable=true, and replay-audit compatibility fields are true.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.edge_registry import SOTD_EDGE_STATUSES  # noqa: E402
from shared.edges.edge_yaml_contract import load_edge_yaml, validate_edge_yaml_payload  # noqa: E402


class EdgeStatusPromotionError(RuntimeError):
    """Raised when lifecycle status promotion would violate SOTD."""


REPLAY_PASS_FIELDS = (
    "selected_row_replay_passed",
    "raw_probability_replay_passed",
    "calibrated_probability_replay_passed",
    "afml_bet_size_replay_passed",
    "threshold_decision_replay_passed",
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EdgeStatusPromotionError(f"Missing JSON file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EdgeStatusPromotionError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EdgeStatusPromotionError(f"JSON must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _validate_pass_manifest(layout: Any) -> dict[str, Any]:
    manifest = _load_json(layout.edge_manifest_json)
    if manifest.get("deployment_status") != "PASS":
        raise EdgeStatusPromotionError("Cannot promote to deployability_pass: edge_manifest deployment_status is not PASS.")
    if manifest.get("deployable") is not True:
        raise EdgeStatusPromotionError("Cannot promote to deployability_pass: edge_manifest deployable is not true.")

    identity = manifest.get("identity", {})
    if not isinstance(identity, Mapping):
        raise EdgeStatusPromotionError("edge_manifest.identity must be an object.")
    manifest_edge_id = identity.get("edge_id") or f"{identity.get('asset')}/{identity.get('family')}_{identity.get('side_name')}"
    if manifest_edge_id != layout.edge_id:
        raise EdgeStatusPromotionError(f"edge_manifest identity mismatch: expected {layout.edge_id}, got {manifest_edge_id!r}")

    live_compatibility = manifest.get("live_compatibility", {})
    if not isinstance(live_compatibility, Mapping):
        raise EdgeStatusPromotionError("edge_manifest.live_compatibility must be an object.")
    missing_or_false = [field for field in REPLAY_PASS_FIELDS if live_compatibility.get(field) is not True]
    if missing_or_false:
        raise EdgeStatusPromotionError(f"Cannot promote to deployability_pass: replay fields missing/false: {missing_or_false}")
    return manifest


def _update_edge_yaml_status(path: Path, new_status: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    out: list[str] = []
    for line in lines:
        if not changed and line.startswith("status:"):
            out.append(f"status: {new_status}")
            changed = True
        else:
            out.append(line)
    if not changed:
        raise EdgeStatusPromotionError(f"Could not find top-level status field in {path}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _update_registry_status(registry_path: Path, layout: Any, new_status: str) -> None:
    registry = _load_json(registry_path)
    edges = registry.get("edges")
    if not isinstance(edges, list):
        raise EdgeStatusPromotionError("edge_universe.json must contain an edges list.")
    updated = False
    for row in edges:
        if not isinstance(row, dict):
            continue
        if row.get("asset") == layout.asset and row.get("family") == layout.family and row.get("side") == layout.side_name:
            row["status"] = new_status
            if new_status == "deployability_pass":
                row["notes"] = (
                    f"{layout.edge_id} has edge_manifest deployment_status=PASS after Step 08 replay audit and Step 09 export. "
                    "Allocator may consume this edge manifest; final live threshold/risk remains controlled by live_manifest."
                )
            updated = True
            break
    if not updated:
        raise EdgeStatusPromotionError(f"Registry row not found for {layout.edge_id}")
    _write_json(registry_path, registry)


def promote_edge_status(edge_root: str | Path, status: str, *, registry_path: str | Path | None = None, write: bool = False) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    new_status = status.strip().lower()
    if new_status not in SOTD_EDGE_STATUSES:
        raise EdgeStatusPromotionError(f"Unsupported SOTD status: {new_status!r}")

    before_yaml = load_edge_yaml(layout.edge_yaml)
    validate_edge_yaml_payload(before_yaml, layout)
    old_status = str(before_yaml.get("status"))

    manifest_summary: dict[str, Any] | None = None
    if new_status == "deployability_pass":
        manifest = _validate_pass_manifest(layout)
        manifest_summary = {
            "deployment_status": manifest.get("deployment_status"),
            "deployable": manifest.get("deployable"),
            "selected_feature_count": manifest.get("selected_features", {}).get("selected_feature_count"),
            "edge_manifest": str(layout.edge_manifest_json),
        }

    registry = Path(registry_path) if registry_path is not None else layout.v2_root / "config" / "edge_universe.json"
    plan = {
        "edge_id": layout.edge_id,
        "old_status": old_status,
        "new_status": new_status,
        "edge_yaml": str(layout.edge_yaml),
        "registry": str(registry),
        "manifest_summary": manifest_summary,
        "write": write,
    }
    if not write:
        return plan

    _update_edge_yaml_status(layout.edge_yaml, new_status)
    _update_registry_status(registry, layout, new_status)

    after_yaml = load_edge_yaml(layout.edge_yaml)
    validate_edge_yaml_payload(after_yaml, layout)
    return plan | {"updated": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote SOTD edge lifecycle status after verified gates.")
    parser.add_argument("--edge-root", required=True)
    parser.add_argument("--status", required=True, choices=sorted(SOTD_EDGE_STATUSES))
    parser.add_argument("--registry", default=None)
    parser.add_argument("--write", action="store_true", help="Write status changes. Without this, prints a dry-run plan.")
    args = parser.parse_args()
    result = promote_edge_status(args.edge_root, args.status, registry_path=args.registry, write=args.write)
    print(json.dumps(result, indent=2, default=str))
    if not args.write:
        print("Dry-run only. Re-run with --write to update edge.yaml and edge_universe.json.")


if __name__ == "__main__":
    try:
        main()
    except EdgeStatusPromotionError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
