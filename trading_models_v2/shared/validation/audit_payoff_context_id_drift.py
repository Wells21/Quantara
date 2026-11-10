"""Audit (and optionally fix) payoff_context_id drift across all edges' saved
threshold/payoff artifacts.

`payoff_context_id` is a hash of an edge's own already-computed threshold/
payoff numbers (win rate, R-multiples, threshold, etc.), stamped into
`artifacts/thresholds/*_selected_threshold.json` (and copied into several
sibling manifests/reports) when Step 04 runs. If the HASH FORMULA in
shared/validation/v3_semantic_contract.py ever changes for any reason (e.g.
the 2026-09-18 precision fix from .17g to .12g, made to fix genuine 1-ULP
floating-point non-determinism), every artifact generated before that change
stops matching its own recomputed identity -- even though the underlying
data never changed. This has nothing to do with creating a new edge; it is
purely a function of when an edge's Step 04 last ran relative to the last
time this hash formula changed.

Run this any time v3_semantic_contract.py's identity-hash logic changes, or
whenever `EdgeScorer.validate_artifacts()` / `create_composed_decision_
continuation()` starts failing with "Threshold/payoff semantic identity
mismatch" for edges whose numbers you know are unchanged.

Safety: an edge is only auto-fixable if EVERY field except payoff_context_id
already matches what the current code would rebuild from the artifact's own
data. If anything else differs, that is a real data question (not a pure
hash-format artifact) and this script reports it without touching it.
"""
from __future__ import annotations

import argparse
import glob
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent.parent
for _p in (V2_ROOT, V2_ROOT.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from shared.validation.v3_semantic_contract import context_from_mapping  # noqa: E402


def find_selected_threshold_files(v2_root: Path) -> list[Path]:
    return sorted(
        Path(p)
        for p in glob.glob(str(v2_root / "assets/*/edges/*/artifacts/thresholds/*_selected_threshold.json"))
    )


def audit_one(path: Path) -> dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8-sig"))
    mapping = d.get("payoff_context")
    if not mapping:
        return {"path": str(path), "status": "NO_PAYOFF_CONTEXT"}
    old_id = str(mapping.get("payoff_context_id", ""))
    try:
        rebuilt = context_from_mapping(mapping)
    except Exception as exc:
        return {"path": str(path), "status": "REBUILD_ERROR", "error": f"{type(exc).__name__}: {exc}"}
    new_id = str(rebuilt.get("payoff_context_id", ""))
    if old_id == new_id:
        return {"path": str(path), "status": "OK"}
    diffs = {
        k: (mapping.get(k), rebuilt.get(k))
        for k in set(mapping) | set(rebuilt)
        if k != "payoff_context_id" and mapping.get(k) != rebuilt.get(k)
    }
    if diffs:
        return {"path": str(path), "status": "UNSAFE_DATA_DIFFERS", "diffs": diffs}
    return {"path": str(path), "status": "STALE_ID_ONLY", "old_id": old_id, "new_id": new_id}


def fix_one(path: Path, old_id: str, new_id: str) -> list[str]:
    """Replace old_id with new_id across every JSON/CSV file under this
    edge's artifacts/ tree that references it, backing up each touched file
    (short `.bak` suffix -- several of these paths are already close to
    Windows' 260-char MAX_PATH)."""
    edge_artifacts_dir = path.parent.parent  # .../artifacts
    changed: list[str] = []
    for f in edge_artifacts_dir.rglob("*"):
        if not (f.is_file() and f.suffix.lower() in (".json", ".csv")):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old_id not in text:
            continue
        backup_path = f.with_name(f.name + ".bak")
        if not backup_path.exists():
            shutil.copy2(f, backup_path)
        f.write_text(text.replace(old_id, new_id), encoding="utf-8")
        changed.append(str(f.relative_to(V2_ROOT)))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="Apply safe re-stamps (default: report only)")
    args = parser.parse_args()

    results = [audit_one(p) for p in find_selected_threshold_files(V2_ROOT)]
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    print(json.dumps({"summary": by_status, "total": len(results)}, indent=2))
    unsafe = [r for r in results if r["status"] == "UNSAFE_DATA_DIFFERS"]
    if unsafe:
        print("\nUNSAFE (real data diffs -- needs manual review, not auto-fixable):")
        for r in unsafe:
            print(" -", r["path"], r["diffs"])

    stale = [r for r in results if r["status"] == "STALE_ID_ONLY"]
    if stale and not args.fix:
        print(f"\n{len(stale)} artifact(s) have pure hash-format drift (safe to re-stamp). "
              "Re-run with --fix to apply.")
        for r in stale:
            print(" -", r["path"])
        return 1
    if stale and args.fix:
        print(f"\nRe-stamping {len(stale)} artifact(s)...")
        for r in stale:
            changed = fix_one(Path(r["path"]), r["old_id"], r["new_id"])
            print(" -", r["path"], "->", len(changed), "files updated")
        return 0
    print("\nSTATUS: PASS -- no drift found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
