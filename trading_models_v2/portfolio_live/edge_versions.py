"""Edge version identity for live oversight (drift dashboard, decommission rule).

AFML Ch.1 s.1.3.1.6 (p.8-9) judges a strategy on its production performance.
A judgement is only valid on bets placed by ONE certified version of the edge:
the closed-trade ledger keeps trades of earlier versions under the same edge_id.

Version identity = sha256 of the edge's meta-model and calibrator files.
Closed trades carry `live_manifest_hash` (sha256 of portfolio_live/live_manifest.json
at trade time). The registry below records, for every live manifest seen, which
version of each edge it ran. A trade counts for an edge's current version when
its live_manifest_hash is registered with that same version.

Nothing here is per-edge configuration: edges, model and calibrator paths come
from live_manifest.json `selected_edges[*].artifacts`.

Registering: `register_current_manifest()` runs from the decommission rule, the
drift dashboard and the end of 00_build_live_deployment_manifest.py. A manifest is
registered with the artifact files as they are at registration time, so register
before trading on a new manifest (00_ does this automatically).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V2_ROOT = Path(__file__).resolve().parents[1]
LIVE_MANIFEST_PATH = V2_ROOT / "portfolio_live" / "live_manifest.json"
REGISTRY_PATH = V2_ROOT / "portfolio_live" / "config" / "edge_version_registry.json"
VERSION_ARTIFACTS = ("model", "calibrator_pkl")


class EdgeVersionError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_manifest(path: Path = LIVE_MANIFEST_PATH) -> dict[str, Any]:
    if not path.exists():
        raise EdgeVersionError(f"live manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def selected_edges(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    edges = {}
    for entry in manifest.get("selected_edges") or []:
        if isinstance(entry, dict) and entry.get("edge_id"):
            edges[entry["edge_id"]] = entry
    if not edges:
        raise EdgeVersionError("live manifest has no selected_edges")
    return edges


def version_of(entry: dict[str, Any]) -> dict[str, str]:
    """{artifact: sha256} of the files that define this edge's decisions."""
    artifacts = entry.get("artifacts") or {}
    out = {}
    for key in VERSION_ARTIFACTS:
        rel = artifacts.get(key)
        if not rel:
            raise EdgeVersionError(f"{entry.get('edge_id')}: manifest artifacts has no {key!r}")
        path = V2_ROOT / rel
        if not path.exists():
            raise EdgeVersionError(f"{entry.get('edge_id')}: missing {key} file {rel}")
        out[key] = sha256_file(path)
    return out


def version_id(version: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()[:16]


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "edge_version_registry_v1", "manifests": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def register_current_manifest(manifest_path: Path = LIVE_MANIFEST_PATH, registry_path: Path = REGISTRY_PATH) -> str:
    """Record which version of each edge the current live manifest runs. Idempotent."""
    manifest_hash = sha256_file(manifest_path)
    registry = load_registry(registry_path)
    if manifest_hash in registry["manifests"]:
        return manifest_hash
    manifest = load_manifest(manifest_path)
    edges = {}
    for edge_id, entry in selected_edges(manifest).items():
        version = version_of(entry)
        edges[edge_id] = {"version_id": version_id(version), "artifacts_sha256": version}
    registry["manifests"][manifest_hash] = {
        "registered_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_created_at": manifest.get("created_at"),
        "edges": edges,
    }
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return manifest_hash


def current_versions(manifest_path: Path = LIVE_MANIFEST_PATH) -> dict[str, dict[str, Any]]:
    """{edge_id: {version_id, artifacts_sha256, manifest_entry}} for the current live manifest."""
    out = {}
    for edge_id, entry in selected_edges(load_manifest(manifest_path)).items():
        version = version_of(entry)
        out[edge_id] = {"version_id": version_id(version), "artifacts_sha256": version, "manifest_entry": entry}
    return out


def manifest_hashes_for_version(edge_id: str, vid: str, registry_path: Path = REGISTRY_PATH) -> set[str]:
    registry = load_registry(registry_path)
    return {
        mh for mh, rec in registry["manifests"].items()
        if rec["edges"].get(edge_id, {}).get("version_id") == vid
    }


def filter_current_version_trades(trades, edge_id: str, vid: str, registry_path: Path = REGISTRY_PATH):
    """Trades of `edge_id` placed under any live manifest registered with version `vid`."""
    hashes = manifest_hashes_for_version(edge_id, vid, registry_path)
    if "live_manifest_hash" not in trades.columns:
        raise EdgeVersionError("closed trades have no live_manifest_hash column")
    mask = (trades["edge_id"] == edge_id) & trades["live_manifest_hash"].isin(hashes)
    return trades.loc[mask].copy()


if __name__ == "__main__":
    mh = register_current_manifest()
    print(f"registered live manifest {mh[:12]}")
    for eid, v in sorted(current_versions().items()):
        print(f"{eid:40s} version {v['version_id']} manifests {len(manifest_hashes_for_version(eid, v['version_id']))}")
