"""Phase D provider-signature drift auditor.

Whenever the selected edge universe changes -- a new edge is added, an edge
is pruned for failing a deployability gate, or an edge's candidate/primary
logic is re-tuned -- the D.1 candidate contract's `candidate_logic_signature`
/ `primary_logic_signature` values change (see shared/edges/
phase_d_candidate_contract.py's `_logic_signature`: an AST-structural hash of
the reachable function source plus referenced semantic constants). Every
`shared/edges/candidate_continuations/providers/*.py` and
`shared/edges/primary_side_continuations/providers/*.py` module pins the
signature(s) it has been verified against in its own `PROVIDER_SPEC
["supported_signatures"]` dict. Those pins do NOT update themselves.

Historically this drift was only ever discovered indirectly, as a wall of
unrelated-looking `tests/test_phase_d_*_provider.py` failures/errors (empty
edge lists, IndexError on `edges[0]`, hardcoded edge-count assertions) days
or weeks after the edge universe actually changed -- see the 2026-09-19/20
incident where 100% of provider modules had drifted and one edge's candidate
logic had silently moved v1->v2 with the provider never re-pinned.

Run this any time `phase_d_01_compile_candidate_contract.py` (or the
incremental D.4.1 contract) is recompiled -- ideally as a standard step in
that runbook section -- to get a direct, actionable diff instead of a wall of
downstream test failures. It NEVER edits anything; it only reports.
"""
from __future__ import annotations

import argparse
import importlib
import json
import pkgutil
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
for _p in (V2_ROOT, V2_ROOT.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DEFAULT_CANDIDATE_CONTRACT = HERE / "config" / "phase_d" / "candidate_contract.json"
DEFAULT_D4_CERTIFICATION = HERE / "outputs" / "phase_d4_provider_real_history_certification_v2.json"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _current_edges(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(contract.get("edges") or [])


def _provider_supported_signatures(package_name: str) -> dict[str, set[str]]:
    """Return {module_name: {signature, ...}} for every provider module in a
    candidate_continuations/primary_side_continuations providers package."""
    package = importlib.import_module(package_name)
    out: dict[str, set[str]] = {}
    for _, modname, _ in pkgutil.iter_modules(package.__path__):
        full_name = f"{package_name}.{modname}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:  # pragma: no cover - reported, not raised
            out[modname] = {f"__import_error__:{type(exc).__name__}:{exc}"}
            continue
        spec = getattr(mod, "PROVIDER_SPEC", None)
        if not isinstance(spec, Mapping):
            continue
        supported = spec.get("supported_signatures")
        if isinstance(supported, Mapping):
            out[modname] = set(str(k) for k in supported)
    return out


def audit(
    *,
    v2_root: Path = V2_ROOT,
    candidate_contract_path: Path = DEFAULT_CANDIDATE_CONTRACT,
    d4_certification_path: Path = DEFAULT_D4_CERTIFICATION,
) -> dict[str, Any]:
    contract = _load_json(candidate_contract_path)
    edges = _current_edges(contract)

    candidate_supported = _provider_supported_signatures(
        "shared.edges.candidate_continuations.providers"
    )
    primary_supported = _provider_supported_signatures(
        "shared.edges.primary_side_continuations.providers"
    )
    all_candidate_sigs: set[str] = set()
    for sigs in candidate_supported.values():
        all_candidate_sigs |= sigs
    all_primary_sigs: set[str] = set()
    for sigs in primary_supported.values():
        all_primary_sigs |= sigs

    unbound_candidate: list[dict[str, str]] = []
    unbound_primary: list[dict[str, str]] = []
    for edge in edges:
        edge_id = str(edge.get("edge_id"))
        candidate_sig = str((edge.get("candidate") or {}).get("candidate_logic_signature") or "")
        primary_sig = str((edge.get("primary_side") or {}).get("primary_logic_signature") or "")
        entry_fns = (edge.get("candidate") or {}).get("runtime_entry_functions") or []
        if candidate_sig and candidate_sig not in all_candidate_sigs:
            unbound_candidate.append({
                "edge_id": edge_id,
                "candidate_logic_signature": candidate_sig,
                "runtime_entry_functions": ",".join(entry_fns),
            })
        if primary_sig and primary_sig not in all_primary_sigs:
            unbound_primary.append({
                "edge_id": edge_id,
                "primary_logic_signature": primary_sig,
            })

    d4_stale = None
    if d4_certification_path.is_file():
        cert = _load_json(d4_certification_path)
        cert_edge_ids = set((cert.get("edges") or {}).keys())
        current_edge_ids = {str(e.get("edge_id")) for e in edges}
        overlap = cert_edge_ids & current_edge_ids
        d4_stale = {
            "certification_created_at_utc": cert.get("created_at_utc"),
            "certification_covers_current_edge_count": len(overlap),
            "current_edge_count": len(current_edge_ids),
            "is_stale": len(overlap) < len(current_edge_ids),
        }

    import_errors = {
        modname: sorted(sigs)[0]
        for modname, sigs in {**candidate_supported, **primary_supported}.items()
        if any(str(s).startswith("__import_error__:") for s in sigs)
    }

    status = "PASS" if not unbound_candidate and not unbound_primary and not import_errors else "FAIL"
    return {
        "status": status,
        "checked_edge_count": len(edges),
        "unbound_candidate_signatures": unbound_candidate,
        "unbound_primary_signatures": unbound_primary,
        "provider_import_errors": import_errors,
        "d4_real_history_certification": d4_stale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-contract", type=Path, default=DEFAULT_CANDIDATE_CONTRACT)
    parser.add_argument("--d4-certification", type=Path, default=DEFAULT_D4_CERTIFICATION)
    args = parser.parse_args()

    report = audit(
        candidate_contract_path=args.candidate_contract,
        d4_certification_path=args.d4_certification,
    )
    print(json.dumps(report, indent=2, sort_keys=True))

    if report["status"] != "PASS":
        print(
            "\nSTATUS: FAIL -- one or more currently-selected edges have no matching "
            "provider signature. For each edge above: confirm whether its candidate/"
            "primary source changed structurally (needs real review) or only its "
            "semantic-constant values changed (safe to re-pin, since providers already "
            "read those values dynamically from the edge's own compiled contract at "
            "bind time -- see native_one_row_feature_engine.py-style factories). Add "
            "the new signature to that family's PROVIDER_SPEC['supported_signatures'] "
            "dict, then re-run this audit and the family's own tests/test_phase_d_*_"
            "provider.py to confirm parity actually holds.",
            file=sys.stderr,
        )
        if report.get("d4_real_history_certification", {}).get("is_stale"):
            print(
                "\nAlso: the D.4 real-history certification "
                f"({DEFAULT_D4_CERTIFICATION.name}) does not cover the current edge "
                "universe -- every currently-selected edge is running AUTHORITATIVE_"
                "BATCH only (safe, but not fast-continuation-optimized) until that "
                "certification is re-run.",
                file=sys.stderr,
            )
        return 2
    print("\nSTATUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
