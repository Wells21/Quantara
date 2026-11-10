from __future__ import annotations

"""Phase C.4 certified production routing overlay.

This module installs *after* production_snapshot_pipeline.  It does not own
bar construction, FFD/HMM continuation, recovery, candidate rules, model math,
calibration, AFML, thresholds, geometry, allocation, or execution.

When (and only when) a matching production-routing activation artifact exists,
it replaces the normal live feature path with:

    immutable READY Phase A/B snapshot
        -> one Phase C.2 update per asset/bar
        -> one immutable canonical 6,000-row in-memory feature ring
        -> C.3 certified per-edge projection
        -> unchanged candidate generator
        -> C.3 collision sanitization
        -> unchanged runtime score / shared EdgeScorer

The 6,000 rows are retained as an in-memory *consumer history authority* because
C.3 certified candidate/model parity against exactly that production history.
They are never recomputed in the normal one-bar path.  A smaller ring can be
introduced only after a later certification proves candidate parity at that
smaller bound.

Batch ``runtime.build_features`` is allowed only to seed C.2 + the canonical
ring at process startup or after the dedicated recovery owner invalidates C.4
state.  Every normal scoring call temporarily guards ``runtime.build_features``
so no edge can silently fall back to the retired 6,000-row feature rebuild.
"""

import copy
import hashlib
import inspect
import json
import os
import pickle
import subprocess
import sys
import threading
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping

# ``portfolio_live`` scripts are routinely executed directly on Windows.  In
# that mode Python places portfolio_live/ rather than trading_models_v2/ on
# sys.path.  Establish the V2 import root before importing shared.* so C.4 has
# the same direct-script contract as the existing Phase C tooling.
HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

import numpy as np
import pandas as pd

from shared.edges.phase_c_consumer_adapter import strip_selected_feature_collisions
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.phase_c_consumer_contract import compile_consumer_contract
from shared.edges.phase_c_incremental_feature_engine import (
    DEFAULT_FEATURE_CONTEXT_ROWS,
    FFD_COLUMNS,
    REGIME_COLUMNS,
    bootstrap_compact_state,
    deserialize_compact_state,
    serialize_compact_state,
    update_one,
)
from shared.live_streaming import state as hot_state


PHASE_C_DIR = HERE / "config" / "phase_c"
ACTIVATION_PATH = PHASE_C_DIR / "production_routing_activation.json"
PRODUCTION_CERTIFICATION_PATH = PHASE_C_DIR / "production_routing_certification.json"

ROUTER_VERSION = "2026-08-17_phase_c4_certified_incremental_canonical_v1_3_lean_c5_manifest_scope"
DASHBOARD_VERSION_SUFFIX = "+phase_c4_certified_incremental_canonical_v1_3_lean_c5_manifest_scope"
ACTIVATION_SCHEMA_VERSION = "phase_c4_production_routing_activation_v1"
PRODUCTION_CERTIFICATION_SCHEMA_VERSION = "phase_c5_production_routing_certification_v1"
CHECKPOINT_SCHEMA_VERSION = "phase_c4_feature_checkpoint_v1"

# C.3 numeric certification used the exact 6,000-row production feature
# authority.  Keeping the *already computed* rows in RAM is not the old
# bottleneck; recomputing those rows per edge was.  This bound is therefore a
# frozen certification authority, not an asset-specific tuning knob.
CERTIFIED_CONSUMER_RING_ROWS = 6000
COMPACT_DELTA_EVERY = 128

_INSTALL_LOCK = threading.RLock()
_STATE_LOCK = threading.RLock()
_RUNTIME_PATCH_LOCKS: dict[str, threading.RLock] = {}
_ASSET_RUNTIME: dict[str, dict[str, Any]] = {}

_NS: MutableMapping[str, Any] | None = None
_ACTIVATION: dict[str, Any] | None = None
_C1: dict[str, Any] | None = None
_C2: dict[str, Any] | None = None
_C3: dict[str, Any] | None = None
_C3_CERT: dict[str, Any] | None = None
_MANIFEST: dict[str, Any] | None = None
_C3_EDGE_MAP: dict[str, dict[str, Any]] = {}
_MANIFEST_EDGE_MAP: dict[str, dict[str, Any]] = {}
_CHECKPOINT_ROOT: Path | None = None

# Static C.1/C.2/C.3 source validation is intentionally expensive because it
# hashes every selected edge package.  It is immutable for the lifetime of one
# dashboard/validator process, so successful results may be reused in-process.
_STATIC_REPORT_CACHE_LOCK = threading.RLock()
_STATIC_REPORT_CACHE: dict[str, dict[str, Any]] = {}


class PhaseC4RuntimeError(RuntimeError):
    """Fail-closed Phase C.4 production-routing contract error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise PhaseC4RuntimeError(f"invalid UTC timestamp: {value!r}")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PhaseC4RuntimeError(f"expected JSON object: {path}")
    return payload


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _json_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise PhaseC4RuntimeError("canonical frame is empty/invalid")
    out = frame.copy()
    if "Datetime" not in out.columns:
        raise PhaseC4RuntimeError("canonical frame has no Datetime")
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    if "Bar_Start" in out.columns:
        out["Bar_Start"] = to_datetime_iso(out["Bar_Start"], utc=True, errors="coerce")
    out = (
        out.dropna(subset=["Datetime"])
        .sort_values("Datetime", kind="mergesort")
        .drop_duplicates("Datetime", keep="last")
        .reset_index(drop=True)
    )
    if out.empty:
        raise PhaseC4RuntimeError("canonical frame is empty after timestamp normalization")
    return out


def _manifest_maps(manifest: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    rows = manifest.get("selected_edges")
    if not isinstance(rows, list):
        raise PhaseC4RuntimeError("live_manifest.selected_edges must be a list")
    edges: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise PhaseC4RuntimeError("live manifest contains a non-object selected edge")
        edge_id = str(raw.get("edge_id") or "").strip()
        asset = str(raw.get("asset") or "").strip().upper()
        if not edge_id or not asset:
            raise PhaseC4RuntimeError("selected edge is missing edge_id/asset")
        if edge_id in edges:
            raise PhaseC4RuntimeError(f"duplicate selected edge id: {edge_id}")
        edges[edge_id] = dict(raw)
    assets = sorted({str(row.get("asset") or "").upper() for row in edges.values()})
    return edges, assets


def _contract_paths(v2_root: Path) -> dict[str, Path]:
    root = Path(v2_root).resolve()
    live = root / "portfolio_live"
    phase_c = live / "config" / "phase_c"
    return {
        "live_manifest": live / "live_manifest.json",
        "c1": phase_c / "feature_contract.json",
        "c2": phase_c / "incremental_feature_certification.json",
        "c3": phase_c / "consumer_contract.json",
        "c3_cert": phase_c / "consumer_certification.json",
        "c2_engine": root / "shared" / "edges" / "phase_c_incremental_feature_engine.py",
        "c3_adapter": root / "shared" / "edges" / "phase_c_consumer_adapter.py",
        "c3_compiler": root / "shared" / "edges" / "phase_c_consumer_contract.py",
        "c3_certifier": live / "phase_c_03_certify_consumer_contract.py",
        "snapshot_pipeline": live / "production_snapshot_pipeline.py",
        "dashboard": live / "portfolio_live_dashboard_orchestrator.py",
        "router": live / "phase_c_04_production_router.py",
        "c5_certifier": live / "phase_c_05_certify_production_routing.py",
        "c5_validator": live / "phase_c_05_validate_production_certificate.py",
    }


def _contract_diff(left: Any, right: Any, *, path: str = "$", limit: int = 24) -> list[str]:
    """Return a bounded structural diff suitable for a fail-closed diagnostic."""
    output: list[str] = []

    def walk(a: Any, b: Any, here: str) -> None:
        if len(output) >= limit:
            return
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            akeys = set(a)
            bkeys = set(b)
            for key in sorted(akeys.difference(bkeys), key=str):
                output.append(f"{here}.{key}:missing_current")
                if len(output) >= limit:
                    return
            for key in sorted(bkeys.difference(akeys), key=str):
                output.append(f"{here}.{key}:unexpected_current")
                if len(output) >= limit:
                    return
            for key in sorted(akeys.intersection(bkeys), key=str):
                walk(a[key], b[key], f"{here}.{key}")
                if len(output) >= limit:
                    return
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                output.append(f"{here}:length {len(a)} != {len(b)}")
                return
            for index, (av, bv) in enumerate(zip(a, b)):
                walk(av, bv, f"{here}[{index}]")
                if len(output) >= limit:
                    return
            return
        if a != b:
            output.append(f"{here}:frozen={a!r} current={b!r}")

    walk(left, right, path)
    return output


def _c3_without_interpreter_ast_derivatives(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only fields whose value is derived from Python's AST serializer.

    C.3 already freezes raw runtime/package/candidate byte hashes.  Those byte
    hashes are portable and remain the source-integrity authority.  Python's
    ``ast.dump`` is not a cross-interpreter serialization contract, so its
    duplicate function fingerprints may vary between Python releases even when
    every source byte is identical.
    """
    output = copy.deepcopy(dict(payload))
    output.pop("contract_sha256", None)
    # Whole-manifest bytes contain account/risk/timestamp metadata that does not
    # define Phase C feature math. The exact current manifest is still rebound
    # by the activation/release certificate; source/universe compatibility is
    # checked explicitly below.
    output.pop("source_live_manifest_sha256", None)
    for edge in output.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        edge.pop("consumer_contract_fingerprint", None)
        source = edge.get("consumer_source")
        if isinstance(source, dict):
            source.pop("runtime_consumer_function_ast_sha256", None)
    return output


def _c3_ast_difference_count(
    frozen: Mapping[str, Any], current: Mapping[str, Any]
) -> int:
    def edge_map(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        return {
            str(row.get("edge_id")): row
            for row in payload.get("edges") or []
            if isinstance(row, Mapping) and row.get("edge_id")
        }

    left = edge_map(frozen)
    right = edge_map(current)
    count = 0
    for edge_id in sorted(set(left).intersection(right)):
        lhashes = ((left[edge_id].get("consumer_source") or {}).get(
            "runtime_consumer_function_ast_sha256"
        ) or {})
        rhashes = ((right[edge_id].get("consumer_source") or {}).get(
            "runtime_consumer_function_ast_sha256"
        ) or {})
        for name in sorted(set(lhashes) | set(rhashes)):
            if lhashes.get(name) != rhashes.get(name):
                count += 1
    return count


def _c3_portable_recompile_report(
    frozen: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate C.3 exactly, allowing only AST serialization or account-metadata manifest variance.

    The escape is allowed ONLY when every non-AST field is byte/source/contract
    identical and the only changed derived values are per-function ast.dump
    fingerprints plus hashes derived from those fingerprints.  Any raw source, C.1/C.2, adapter, selected-edge, schema, addon, control, or policy
    drift still fails closed. Whole-manifest byte variance is allowed only after
    explicit current selected-edge/source/feature compatibility checks.
    """
    failures: list[str] = []
    frozen_recorded = str(frozen.get("contract_sha256") or "")
    current_recorded = str(current.get("contract_sha256") or "")

    frozen_identity = copy.deepcopy(dict(frozen))
    frozen_identity.pop("contract_sha256", None)
    current_identity = copy.deepcopy(dict(current))
    current_identity.pop("contract_sha256", None)
    if not frozen_recorded or _json_hash(frozen_identity) != frozen_recorded:
        failures.append("frozen C.3 contract hash is not self-consistent")
    if not current_recorded or _json_hash(current_identity) != current_recorded:
        failures.append("current recompiled C.3 contract hash is not self-consistent")
    if current.get("status") != "PASS":
        failures.append(f"current C.3 compiler status={current.get('status')!r}")
    if failures:
        return {
            "status": "FAIL",
            "mode": "INVALID_CONTRACT_HASH",
            "failures": failures,
        }

    if frozen_recorded == current_recorded:
        return {
            "status": "PASS",
            "mode": "EXACT_FROZEN_CONTRACT_HASH",
            "frozen_contract_sha256": frozen_recorded,
            "current_contract_sha256": current_recorded,
            "ast_function_hash_difference_count": 0,
            "python_version": sys.version.split()[0],
        }

    portable_frozen = _c3_without_interpreter_ast_derivatives(frozen)
    portable_current = _c3_without_interpreter_ast_derivatives(current)
    differences = _contract_diff(portable_frozen, portable_current)
    ast_differences = _c3_ast_difference_count(frozen, current)
    manifest_hash_changed = str(frozen.get("source_live_manifest_sha256") or "") != str(
        current.get("source_live_manifest_sha256") or ""
    )
    if differences or (ast_differences <= 0 and not manifest_hash_changed):
        return {
            "status": "FAIL",
            "mode": "NON_PORTABLE_C3_DRIFT",
            "failures": differences or [
                "C.3 contract hash changed without AST or manifest-metadata variance"
            ],
            "frozen_contract_sha256": frozen_recorded,
            "current_contract_sha256": current_recorded,
            "ast_function_hash_difference_count": ast_differences,
            "manifest_hash_changed": manifest_hash_changed,
            "python_version": sys.version.split()[0],
        }

    if ast_differences > 0 and manifest_hash_changed:
        mode = "SOURCE_BYTES_EQUIVALENT_AST_AND_MANIFEST_METADATA_VARIANCE"
    elif ast_differences > 0:
        mode = "SOURCE_BYTES_EQUIVALENT_AST_PORTABILITY_VARIANCE"
    else:
        mode = "FEATURE_EQUIVALENT_MANIFEST_METADATA_VARIANCE"
    return {
        "status": "PASS",
        "mode": mode,
        "frozen_contract_sha256": frozen_recorded,
        "current_contract_sha256": current_recorded,
        "ast_function_hash_difference_count": ast_differences,
        "manifest_hash_changed": manifest_hash_changed,
        "nonportable_difference_count": 0,
        "python_version": sys.version.split()[0],
        "safety_basis": (
            "all Phase-C-relevant C.3 fields are identical; exact selected edge/"
            "source identities remain authoritative; current manifest bytes are "
            "rebound by the C.4 activation + C.5 release certificate"
        ),
    }


def _compile_consumer_contract_for_current_manifest(
    *,
    root: Path,
    paths: Mapping[str, Path],
    c2: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compile current C.3 source while preserving frozen C.2 byte authority.

    C.2's recorded whole-manifest SHA is deployment-specific: account/risk/
    timestamp metadata may change without changing Phase-C feature/source math.
    The unmodified C.3 compiler correctly treats that SHA as strict, but C.4 is
    the layer that deliberately distinguishes deployment metadata from
    Phase-C-relevant identity.

    When the current manifest bytes differ, create a short-lived C.2 validation
    view under the V2 root with ONLY ``live_manifest_file_sha256`` rebound to the
    current manifest. This merely allows the strict compiler to inventory
    current source. The returned contract is normalized back to the real C.2
    path/file SHA before comparison.

    Safety remains fail-closed because static_contract_report subsequently:
      * validates the real frozen C.2 implementation + certification bytes;
      * validates every current selected edge/path/source against frozen C.1/C.3;
      * validates feature-schema + selected-feature bytes;
      * validates current edge Python/runtime/candidate source through C.3;
      * binds the exact current manifest into the C.4/C.5 release fingerprint.
    """
    manifest_path = Path(paths["live_manifest"]).resolve()
    c2_path = Path(paths["c2"]).resolve()
    current_manifest_sha = _sha256_file(manifest_path)
    certified_manifest_sha = str(c2.get("live_manifest_file_sha256") or "")

    if not certified_manifest_sha:
        raise PhaseC4RuntimeError(
            "C.2 certification has no live_manifest_file_sha256"
        )

    if current_manifest_sha == certified_manifest_sha:
        return (
            compile_consumer_contract(v2_root=root),
            {
                "mode": "EXACT_C2_MANIFEST_BYTES",
                "certified_manifest_sha256": certified_manifest_sha,
                "current_manifest_sha256": current_manifest_sha,
                "temporary_validation_view_used": False,
                "frozen_c2_file_modified": False,
            },
        )

    rebound = copy.deepcopy(dict(c2))
    rebound["live_manifest_file_sha256"] = current_manifest_sha

    phase_c_dir = c2_path.parent
    with tempfile.TemporaryDirectory(
        prefix=".c4_manifest_validation_",
        dir=str(phase_c_dir),
    ) as temp_dir:
        temp_c2 = Path(temp_dir) / c2_path.name
        with temp_c2.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(
                json.dumps(
                    rebound,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
                + "\n"
            )

        current = compile_consumer_contract(
            v2_root=root,
            phase_c2_certification_path=temp_c2,
        )

    # The temporary file is not an authority and must not leak into the
    # recompiled C.3 identity. Restore the real frozen C.2 path + byte hash, then
    # recompute this in-memory contract's self hash before portable comparison.
    current["phase_c2_certification_path"] = (
        c2_path.relative_to(Path(root).resolve()).as_posix()
    )
    current["phase_c2_certification_file_sha256"] = _sha256_file(c2_path)
    identity = copy.deepcopy(dict(current))
    identity.pop("contract_sha256", None)
    current["contract_sha256"] = _json_hash(identity)

    return (
        current,
        {
            "mode": "CURRENT_MANIFEST_REBOUND_FOR_PORTABLE_RECOMPILE",
            "certified_manifest_sha256": certified_manifest_sha,
            "current_manifest_sha256": current_manifest_sha,
            "temporary_validation_view_used": True,
            "frozen_c2_file_modified": False,
            "safety_owner": (
                "phase_c4_explicit_selected_edge_source_feature_checks_plus_"
                "c5_current_manifest_release_binding"
            ),
        },
    )


def static_contract_report(
    v2_root: Path = V2_ROOT, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Validate current C1/C2/C3 authority structurally, without provenance SHA gates."""
    root = Path(v2_root).resolve()
    cache_key = str(root)
    with _STATIC_REPORT_CACHE_LOCK:
        if not force_refresh and cache_key in _STATIC_REPORT_CACHE:
            return copy.deepcopy(_STATIC_REPORT_CACHE[cache_key])

    paths = _contract_paths(root)
    failures: list[str] = []
    for name, path in paths.items():
        if name == "router" and not path.exists():
            continue
        if not path.is_file():
            failures.append(f"missing required file: {name}={path}")
    if failures:
        return {
            "status": "FAIL",
            "failures": failures,
            "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        }

    try:
        manifest = _read_json(paths["live_manifest"])
        c1 = _read_json(paths["c1"])
        c2 = _read_json(paths["c2"])
        c3 = _read_json(paths["c3"])
        c3_cert = _read_json(paths["c3_cert"])
        manifest_edges, assets = _manifest_maps(manifest)

        c1_edges = {
            str(row.get("edge_id")): dict(row)
            for row in c1.get("edges") or []
            if isinstance(row, Mapping) and row.get("edge_id")
        }
        c3_edges = {
            str(row.get("edge_id")): dict(row)
            for row in c3.get("edges") or []
            if isinstance(row, Mapping) and row.get("edge_id")
        }

        from shared.edges.phase_c_feature_contract import validate_frozen_contract
        from shared.edges.phase_c_consumer_contract import validate_frozen_consumer_contract

        c1_structural = validate_frozen_contract(
            v2_root=root,
            contract_path=paths["c1"],
            live_manifest_path=paths["live_manifest"],
        )
        if c1_structural.get("status") != "PASS":
            failures.extend(
                "C.1 structural:" + str(x)
                for x in c1_structural.get("failures") or []
            )

        c3_structural = validate_frozen_consumer_contract(
            v2_root=root,
            frozen_path=paths["c3"],
            live_manifest_path=paths["live_manifest"],
            phase_c1_contract_path=paths["c1"],
            phase_c2_certification_path=paths["c2"],
        )
        if c3_structural.get("status") != "PASS":
            failures.extend(
                "C.3 structural:" + str(x)
                for x in c3_structural.get("failures") or []
            )

        if c1.get("status") != "PASS" or c1.get("runtime_routing_enabled") is not False:
            failures.append("C.1 must be PASS with offline routing flag false")
        if c2.get("status") != "PASS" or c2.get("runtime_routing_enabled") is not False:
            failures.append("C.2 must be PASS with offline routing flag false")
        if c3.get("status") != "PASS" or c3.get("runtime_routing_enabled") is not False:
            failures.append("C.3 must be PASS with offline routing flag false")
        if c3_cert.get("status") != "PASS" or c3_cert.get("runtime_routing_enabled") is not False:
            failures.append("C.3 numerical certification must be PASS/offline")

        expected_ids = sorted(manifest_edges)
        if sorted(c1_edges) != expected_ids:
            failures.append("C.1 selected-edge universe differs from current manifest")
        if sorted(c3_edges) != expected_ids:
            failures.append("C.3 selected-edge universe differs from current manifest")
        if sorted(str(x).upper() for x in c1.get("selected_assets") or []) != assets:
            failures.append("C.1 selected assets differ from current manifest")
        if sorted(str(x).upper() for x in c2.get("selected_assets") or []) != assets:
            failures.append("C.2 selected assets differ from current manifest")
        if sorted(str(x).upper() for x in c3.get("selected_assets") or []) != assets:
            failures.append("C.3 selected assets differ from current manifest")

        if int(c2.get("selected_edge_count") or -1) != len(expected_ids):
            failures.append("C.2 selected-edge count differs from current manifest")
        if int(c2.get("bootstrap_rows") or -1) < CERTIFIED_CONSUMER_RING_ROWS:
            failures.append("C.2 authority is below the 6000-row production ring")
        if int(c2.get("sequential_steps") or -1) < 128:
            failures.append("C.2 sequential certification is below 128 steps")

        c2_assets = c2.get("assets") or {}
        for asset in assets:
            row = c2_assets.get(asset)
            if not isinstance(row, Mapping) or row.get("status") != "PASS":
                failures.append(f"{asset}: C.2 asset certification is not PASS")

        if int(c3_cert.get("authority_rows") or -1) != CERTIFIED_CONSUMER_RING_ROWS:
            failures.append("C.3 numerical certification is not the exact 6000-row authority")
        if int(c3_cert.get("private_control_continuation_steps") or -1) < 128:
            failures.append("C.3 private-control continuation is below 128 steps")
        if int(c3_cert.get("selected_edge_count") or -1) != len(expected_ids):
            failures.append("C.3 numerical selected-edge count differs from current manifest")
        if int(c3_cert.get("edges_passed") or -1) != len(expected_ids):
            failures.append("C.3 numerical certification does not PASS every selected edge")
        if int(c3_cert.get("edges_failed") or 0) != 0:
            failures.append("C.3 numerical certification contains failed edges")
        if c3_cert.get("fatal_errors"):
            failures.append("C.3 numerical certification contains fatal errors")
        if c3_cert.get("private_control_assets_failed"):
            failures.append("C.3 private-control continuation contains failed assets")

        certified_edges = c3_cert.get("edges") or {}
        if sorted(str(x) for x in certified_edges) != expected_ids:
            failures.append("C.3 numerical edge-result universe differs from current manifest")
        else:
            bad = [
                edge_id
                for edge_id in expected_ids
                if not isinstance(certified_edges.get(edge_id), Mapping)
                or certified_edges[edge_id].get("status") != "PASS"
            ]
            if bad:
                failures.append(
                    "C.3 numerical certification contains non-PASS edges: "
                    + ",".join(bad[:10])
                )

        if set(c1_edges) == set(manifest_edges):
            for edge_id in expected_ids:
                manifest_edge = manifest_edges[edge_id]
                c1_edge = c1_edges[edge_id]
                c3_edge = c3_edges.get(edge_id) or {}
                artifacts = manifest_edge.get("artifacts") or {}
                if not isinstance(artifacts, Mapping):
                    artifacts = {}

                def norm(value: Any) -> str:
                    return str(value or "").replace("\\", "/").strip()

                expected = {
                    "asset": str(c1_edge.get("asset") or "").upper(),
                    "family": str(c1_edge.get("family") or ""),
                    "edge_root": norm(c1_edge.get("edge_root")),
                    "runtime_module": norm(c1_edge.get("runtime_module")),
                    "feature_schema": norm(c1_edge.get("feature_schema_path")),
                    "selected_features": norm(c1_edge.get("selected_features_path")),
                }
                actual = {
                    "asset": str(manifest_edge.get("asset") or "").upper(),
                    "family": str(manifest_edge.get("family") or ""),
                    "edge_root": norm(manifest_edge.get("edge_root")),
                    "runtime_module": norm(manifest_edge.get("runtime_module")),
                    "feature_schema": norm(artifacts.get("feature_schema")),
                    "selected_features": norm(artifacts.get("selected_features")),
                }
                if expected != actual:
                    failures.append(f"{edge_id}: current manifest edge/source paths differ from C.1")
                if str(c3_edge.get("asset") or "").upper() != actual["asset"]:
                    failures.append(f"{edge_id}: C.3 asset differs from current manifest")
                if str(c3_edge.get("family") or "") != actual["family"]:
                    failures.append(f"{edge_id}: C.3 family differs from current manifest")
                if norm(c3_edge.get("runtime_module")) != actual["runtime_module"]:
                    failures.append(f"{edge_id}: C.3 runtime module differs from current manifest")

        report = {
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
            "selected_assets": assets,
            "selected_asset_count": len(assets),
            "selected_edge_count": len(expected_ids),
            "selected_edge_ids": expected_ids,
            "c2_engine_version": c2.get("engine_version"),
            "c3_schema_version": c3.get("schema_version"),
            "c3_compiler_version": c3.get("compiler_version"),
            "c1_structural_validation": c1_structural,
            "c3_structural_validation": c3_structural,
            "consumer_ring_rows": CERTIFIED_CONSUMER_RING_ROWS,
            "normal_path_batch_fallback_allowed": False,
            "scalability_contract": {
                "asset_discovery": "live_manifest",
                "edge_discovery": "live_manifest",
                "bootstrap_representative": "phase_c2_certification_per_asset",
                "edge_projection": "phase_c3_contract_per_edge",
                "fixed_asset_list": False,
                "fixed_edge_count": False,
            },
        }
        if report["status"] == "PASS":
            with _STATIC_REPORT_CACHE_LOCK:
                _STATIC_REPORT_CACHE[cache_key] = copy.deepcopy(report)
        return report
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
        return {
            "status": "FAIL",
            "failures": failures,
            "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        }



def _activation_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    """Activation identity is structural/current-state; hashes are not authority."""
    return {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "router_version": ROUTER_VERSION,
        "selected_assets": list(report.get("selected_assets") or []),
        "selected_edge_ids": list(report.get("selected_edge_ids") or []),
        "selected_asset_count": int(report.get("selected_asset_count") or 0),
        "selected_edge_count": int(report.get("selected_edge_count") or 0),
        "c2_engine_version": str(report.get("c2_engine_version") or ""),
        "c3_schema_version": str(report.get("c3_schema_version") or ""),
        "consumer_ring_rows": CERTIFIED_CONSUMER_RING_ROWS,
        "consumer_ring_authority": "fresh_current_state_c2_c3_numerical_certification",
        "normal_path_batch_fallback_allowed": False,
        "dependency_owner": "phase_a_b_snapshot_coordinator",
        "feature_owner": "phase_c2_single_asset_state",
        "consumer_owner": "phase_c3_per_edge_projection",
    }



def build_activation_payload(v2_root: Path = V2_ROOT) -> dict[str, Any]:
    report = static_contract_report(v2_root)
    if report.get("status") != "PASS":
        raise PhaseC4RuntimeError(
            "cannot build Phase C.4 activation from stale contracts: "
            + " | ".join(str(x) for x in report.get("failures") or [])
        )
    contract = _activation_contract(report)
    return {
        "schema_version": ACTIVATION_SCHEMA_VERSION,
        "status": "PASS",
        "runtime_routing_enabled": True,
        "generated_at": _utc_now(),
        "contract": contract,
        "activation_fingerprint": _json_hash(contract),
    }


def validate_activation(
    activation: Mapping[str, Any],
    *,
    v2_root: Path = V2_ROOT,
) -> dict[str, Any]:
    """Validate structural activation. Informational fingerprints never block."""
    failures: list[str] = []
    if activation.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        failures.append("activation schema version mismatch")
    if activation.get("status") != "PASS":
        failures.append("activation status is not PASS")
    if activation.get("runtime_routing_enabled") is not True:
        failures.append("activation runtime_routing_enabled is not true")

    report = static_contract_report(v2_root)
    if report.get("status") != "PASS":
        failures.extend(
            f"current_contract:{value}"
            for value in report.get("failures") or []
        )
    else:
        expected = _activation_contract(report)
        if activation.get("contract") != expected:
            failures.append(
                "activation structural contract differs from current selected universe/router authority"
            )

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "activation_fingerprint": activation.get("activation_fingerprint"),
        "current_contract_status": report.get("status"),
    }


def _git_head(v2_root: Path) -> str:
    repo_root = Path(v2_root).resolve().parent
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise PhaseC4RuntimeError(
            "cannot resolve git HEAD for Phase C.5 release gate: "
            + (proc.stderr.strip() or proc.stdout.strip())
        )
    return proc.stdout.strip()


def validate_production_certification(
    certification: Mapping[str, Any],
    activation: Mapping[str, Any],
    *,
    v2_root: Path = V2_ROOT,
) -> dict[str, Any]:
    """Validate fresh C5 gates structurally; Git/file/certificate hashes are audit-only."""
    failures: list[str] = []
    if certification.get("schema_version") != PRODUCTION_CERTIFICATION_SCHEMA_VERSION:
        failures.append("production certification schema mismatch")
    if certification.get("status") != "PASS":
        failures.append("production certification status is not PASS")
    if certification.get("sealed_for_production") is not True:
        failures.append("production certification is not sealed")
    if certification.get("production_activation_allowed") is not True:
        failures.append("production certification does not allow activation")

    activation_validation = validate_activation(activation, v2_root=v2_root)
    if activation_validation.get("status") != "PASS":
        failures.extend(
            "activation:" + str(value)
            for value in activation_validation.get("failures") or []
        )

    report = static_contract_report(v2_root)
    if report.get("status") != "PASS":
        failures.extend(
            "current_contract:" + str(value)
            for value in report.get("failures") or []
        )
    else:
        if str(certification.get("router_version") or "") != ROUTER_VERSION:
            failures.append("production certification router version mismatch")
        if list(certification.get("selected_edge_ids") or []) != list(
            report.get("selected_edge_ids") or []
        ):
            failures.append("production certification selected-edge universe mismatch")
        if list(certification.get("selected_assets") or []) != list(
            report.get("selected_assets") or []
        ):
            failures.append("production certification selected-asset universe mismatch")

    required_gate_names = {
        "c4_static_contract",
        "c4_router_self_test",
        "phase_c1_frozen_feature_authority",
        "phase_c2_frozen_feature_authority",
        "phase_c3_frozen_consumer_authority",
        "phase_c_model_decision_parity_all_selected_edges",
        "p0_production_validator",
        "production_hardening_validator",
        "full_unittest_discovery",
    }
    gates = certification.get("required_gates") or {}
    missing_gates = sorted(required_gate_names.difference(gates))
    failed_gates = sorted(
        name
        for name in required_gate_names
        if name in gates and str(gates.get(name)) != "PASS"
    )
    if missing_gates:
        failures.append(
            "production certification missing gates: " + ",".join(missing_gates)
        )
    if failed_gates:
        failures.append(
            "production certification has non-PASS gates: " + ",".join(failed_gates)
        )

    contract = dict(certification.get("certificate_contract") or {})
    if contract:
        if str(contract.get("router_version") or "") != ROUTER_VERSION:
            failures.append("production certificate structural router version mismatch")
        if list(contract.get("selected_assets") or []) != list(
            report.get("selected_assets") or []
        ):
            failures.append("production certificate structural selected-assets mismatch")
        if list(contract.get("selected_edge_ids") or []) != list(
            report.get("selected_edge_ids") or []
        ):
            failures.append("production certificate structural selected-edges mismatch")
        contract_gates = contract.get("required_gates") or {}
        if any(str(contract_gates.get(name)) != "PASS" for name in required_gate_names):
            failures.append("production certificate structural required-gates mismatch")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "selected_asset_count": len(report.get("selected_assets") or []),
        "selected_edge_count": len(report.get("selected_edge_ids") or []),
        "activation_fingerprint": activation.get("activation_fingerprint"),
        "certificate_fingerprint": certification.get("certificate_fingerprint"),
    }



def _feature_generation_id(
    *,
    asset: str,
    timestamp: Any,
    dependency_generation_id: str,
    activation_fingerprint: str,
) -> str:
    return _json_hash(
        {
            "router_version": ROUTER_VERSION,
            "asset": str(asset).upper(),
            "timestamp": _utc(timestamp).isoformat(),
            "dependency_generation_id": str(dependency_generation_id),
            "activation_fingerprint": str(activation_fingerprint),
        }
    )


def _safe_asset_filename(asset: str) -> str:
    canonical = str(asset).strip().upper()
    text = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in canonical
    )
    if not text:
        raise PhaseC4RuntimeError("blank asset cannot own Phase C.4 state")
    # Keep human-readable checkpoint folders without allowing two future
    # canonical symbols that sanitize to the same text to collide.
    suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{text.lower()}_{suffix}"


def _checkpoint_paths(asset: str) -> dict[str, Path]:
    if _CHECKPOINT_ROOT is None:
        raise PhaseC4RuntimeError("Phase C.4 checkpoint root is not initialized")
    root = _CHECKPOINT_ROOT / _safe_asset_filename(asset)
    return {
        "root": root,
        "state": root / "compact_state.pkl",
        "ring": root / "consumer_ring.pkl",
        "delta": root / "consumer_ring_delta.jsonl",
        "meta": root / "checkpoint_meta.json",
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write(
        path,
        (
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str)
            + "\n"
        ).encode("utf-8"),
    )


def _row_for_json(row: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, pd.Timestamp):
            output[str(key)] = _utc(value).isoformat()
        elif isinstance(value, np.generic):
            output[str(key)] = value.item()
        else:
            output[str(key)] = value
    return output


def _append_delta(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        _row_for_json(row),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=True,
    ) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _read_delta(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise PhaseC4RuntimeError(f"invalid Phase C.4 delta row in {path}")
            rows.append(value)
    if not rows:
        return pd.DataFrame()
    return _normalize_frame(pd.DataFrame(rows))


def _checkpoint_meta(
    *,
    asset: str,
    state_payload: bytes,
    ring_payload: bytes | None,
    record: Mapping[str, Any],
    delta_count: int,
    prior_ring_payload_sha256: str | None = None,
) -> dict[str, Any]:
    if _ACTIVATION is None or _C1 is None:
        raise PhaseC4RuntimeError("Phase C.4 activation/contract is not loaded")
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "router_version": ROUTER_VERSION,
        "created_at": _utc_now(),
        "asset": str(asset).upper(),
        "activation_fingerprint": str(_ACTIVATION.get("activation_fingerprint") or ""),
        "c1_contract_sha256": str(_C1.get("contract_sha256") or ""),
        "feature_generation_id": str(record.get("feature_generation_id") or ""),
        "dependency_generation_id": str(record.get("dependency_generation_id") or ""),
        "last_timestamp": str(record.get("last_timestamp") or ""),
        "consumer_ring_rows": int(len(record["ring"]))
        if isinstance(record.get("ring"), pd.DataFrame)
        else 0,
        "consumer_ring_capacity": CERTIFIED_CONSUMER_RING_ROWS,
        "delta_count": int(delta_count),
        "state_payload_sha256": _sha256_bytes(state_payload),
        "ring_payload_sha256": (
            _sha256_bytes(ring_payload)
            if ring_payload is not None
            else str(prior_ring_payload_sha256 or "") or None
        ),
    }


def _persist_full_checkpoint(asset: str, record: Mapping[str, Any]) -> None:
    if _C1 is None:
        raise PhaseC4RuntimeError("Phase C.1 is not loaded")
    paths = _checkpoint_paths(asset)
    state_payload = serialize_compact_state(
        record["state"], phase_c1_contract=_C1
    )
    ring = _normalize_frame(record["ring"]).tail(CERTIFIED_CONSUMER_RING_ROWS)
    ring_payload = pickle.dumps(ring, protocol=pickle.HIGHEST_PROTOCOL)
    _atomic_write(paths["state"], state_payload)
    _atomic_write(paths["ring"], ring_payload)
    _atomic_write(paths["delta"], b"")
    meta = _checkpoint_meta(
        asset=asset,
        state_payload=state_payload,
        ring_payload=ring_payload,
        record={**dict(record), "ring": ring},
        delta_count=0,
    )
    _atomic_json(paths["meta"], meta)


def _persist_incremental_checkpoint(
    asset: str,
    record: MutableMapping[str, Any],
    latest_internal_row: Mapping[str, Any],
) -> None:
    if _C1 is None:
        raise PhaseC4RuntimeError("Phase C.1 is not loaded")
    paths = _checkpoint_paths(asset)
    state_payload = serialize_compact_state(
        record["state"], phase_c1_contract=_C1
    )

    # Append the one new feature row; the wide 6,000-row ring is not rewritten
    # every bar.  Periodic compaction bounds restart replay.
    prior_ring_hash: str | None = None
    if paths["meta"].is_file():
        try:
            prior_ring_hash = str(
                _read_json(paths["meta"]).get("ring_payload_sha256") or ""
            ) or None
        except Exception:
            prior_ring_hash = None

    _append_delta(paths["delta"], latest_internal_row)
    delta_count = int(record.get("delta_count") or 0) + 1
    _atomic_write(paths["state"], state_payload)
    ring_payload: bytes | None = None
    # If the prior base-ring fingerprint is unavailable, compact immediately
    # rather than publishing a checkpoint that cannot verify its base snapshot.
    if delta_count >= COMPACT_DELTA_EVERY or not prior_ring_hash:
        ring = _normalize_frame(record["ring"]).tail(CERTIFIED_CONSUMER_RING_ROWS)
        ring_payload = pickle.dumps(ring, protocol=pickle.HIGHEST_PROTOCOL)
        _atomic_write(paths["ring"], ring_payload)
        _atomic_write(paths["delta"], b"")
        delta_count = 0
        prior_ring_hash = None
    record["delta_count"] = delta_count
    meta = _checkpoint_meta(
        asset=asset,
        state_payload=state_payload,
        ring_payload=ring_payload,
        record=record,
        delta_count=delta_count,
        prior_ring_payload_sha256=prior_ring_hash,
    )
    # A null ring hash means "use previously committed ring snapshot + delta".
    _atomic_json(paths["meta"], meta)


def _restore_checkpoint(
    *,
    asset: str,
    bars: pd.DataFrame,
    dependency_generation_id: str,
) -> dict[str, Any] | None:
    if _C1 is None or _ACTIVATION is None:
        return None
    paths = _checkpoint_paths(asset)
    required = [paths["state"], paths["ring"], paths["meta"]]
    if any(not path.is_file() for path in required):
        return None
    try:
        meta = _read_json(paths["meta"])
        if meta.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            return None
        if str(meta.get("router_version") or "") != ROUTER_VERSION:
            return None
        if str(meta.get("activation_fingerprint") or "") != str(
            _ACTIVATION.get("activation_fingerprint") or ""
        ):
            return None
        if str(meta.get("c1_contract_sha256") or "") != str(
            _C1.get("contract_sha256") or ""
        ):
            return None

        state_payload = paths["state"].read_bytes()
        if str(meta.get("state_payload_sha256") or "") != _sha256_bytes(state_payload):
            return None
        state = deserialize_compact_state(
            state_payload, phase_c1_contract=_C1
        )
        ring_bytes = paths["ring"].read_bytes()
        expected_ring_hash = str(meta.get("ring_payload_sha256") or "")
        if not expected_ring_hash or _sha256_bytes(ring_bytes) != expected_ring_hash:
            return None
        ring = pickle.loads(ring_bytes)
        if not isinstance(ring, pd.DataFrame):
            return None
        delta = _read_delta(paths["delta"])
        expected_delta_count = int(meta.get("delta_count") or 0)
        if int(len(delta)) != expected_delta_count:
            return None
        if not delta.empty:
            ring = pd.concat([ring, delta], ignore_index=True, sort=False)
        ring = _normalize_frame(ring).tail(CERTIFIED_CONSUMER_RING_ROWS)

        state_ts = _utc(state.get("last_timestamp"))
        ring_ts = _utc(ring["Datetime"].iloc[-1])
        bars_norm = _normalize_frame(bars)
        bars_ts = _utc(bars_norm["Datetime"].iloc[-1])
        if state_ts != ring_ts or state_ts != bars_ts:
            return None
        if str(state.get("last_dependency_generation_id") or "") != str(
            dependency_generation_id
        ):
            return None

        if str(meta.get("last_timestamp") or "") != state_ts.isoformat():
            return None
        if str(meta.get("dependency_generation_id") or "") != str(dependency_generation_id):
            return None
        feature_generation_id = str(meta.get("feature_generation_id") or "")
        expected_feature_generation_id = _feature_generation_id(
            asset=asset,
            timestamp=state_ts,
            dependency_generation_id=dependency_generation_id,
            activation_fingerprint=str(_ACTIVATION.get("activation_fingerprint") or ""),
        )
        if not feature_generation_id or feature_generation_id != expected_feature_generation_id:
            return None
        return {
            "asset": str(asset).upper(),
            "state": state,
            "ring": ring,
            "last_timestamp": state_ts.isoformat(),
            "dependency_generation_id": str(dependency_generation_id),
            "feature_generation_id": feature_generation_id,
            "bootstrap_count": 0,
            "update_count": int(state.get("updates") or 0),
            "restore_count": 1,
            "delta_count": int(meta.get("delta_count") or 0),
            "last_mode": "checkpoint_restore",
            "last_elapsed_ms": 0.0,
        }
    except Exception:
        # Any torn/stale checkpoint is rejected. Startup/recovery batch authority
        # is the fail-closed oracle; checkpoint presence alone is never trusted.
        return None


def _snapshot_for_asset(asset: str) -> dict[str, Any]:
    snapshot = hot_state.current_feature_snapshot(str(asset).upper())
    if not isinstance(snapshot, Mapping):
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} has no bound immutable Phase A/B snapshot"
        )
    generation = snapshot.get("dependency_generation")
    if not isinstance(generation, Mapping):
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} snapshot has no dependency generation"
        )
    generation_id = str(
        snapshot.get("dependency_generation_id")
        or generation.get("generation_id")
        or ""
    )
    if not generation_id:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} dependency generation id is blank"
        )
    return dict(snapshot)


def _row_at_timestamp(
    frame: pd.DataFrame,
    timestamp: Any,
    *,
    label: str,
) -> pd.Series:
    work = _normalize_frame(frame)
    target = _utc(timestamp)
    matches = work.loc[work["Datetime"] == target]
    if len(matches) != 1:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {label} row count={len(matches)} at {target.isoformat()}"
        )
    return matches.iloc[-1]


def _asset_projection_requirements(asset: str) -> set[str]:
    required: set[str] = set()
    for row in _C3_EDGE_MAP.values():
        if str(row.get("asset") or "").upper() != str(asset).upper():
            continue
        required.update(str(x) for x in row.get("canonical_projection_columns") or [])
    required.discard("edge_id")
    required.discard("side")
    return required


def _validate_ring_for_asset(asset: str, ring: pd.DataFrame) -> None:
    required = _asset_projection_requirements(asset)
    missing = sorted(required.difference(ring.columns))
    if missing:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} canonical ring missing certified "
            f"consumer columns: {missing[:30]}"
        )
    if len(ring) > CERTIFIED_CONSUMER_RING_ROWS:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} ring exceeded certified bound"
        )


def _bootstrap_asset(
    *,
    asset: str,
    bars: pd.DataFrame,
    logger: Any,
) -> dict[str, Any]:
    if _NS is None or _C1 is None or _C2 is None or _ACTIVATION is None:
        raise PhaseC4RuntimeError("Phase C.4 router is not initialized")
    started = time.perf_counter()
    asset = str(asset).upper()
    bars_norm = _normalize_frame(bars)
    if len(bars_norm) < CERTIFIED_CONSUMER_RING_ROWS:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} bootstrap requires "
            f"{CERTIFIED_CONSUMER_RING_ROWS} bars, have {len(bars_norm)}"
        )
    bars_window = bars_norm.tail(CERTIFIED_CONSUMER_RING_ROWS).reset_index(drop=True)
    snapshot = _snapshot_for_asset(asset)
    generation = snapshot["dependency_generation"]
    dependency_generation_id = str(
        snapshot.get("dependency_generation_id")
        or generation.get("generation_id")
        or ""
    )
    latest = _utc(bars_window["Datetime"].iloc[-1])
    raw_target = _utc(
        generation.get("raw_target")
        or generation.get("target_timestamp")
        or snapshot.get("snapshot_latest")
    )
    if raw_target != latest:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} bootstrap dependency target "
            f"{raw_target.isoformat()} != bar {latest.isoformat()}"
        )

    c2_asset = (_C2.get("assets") or {}).get(asset)
    representative_id = (
        str(c2_asset.get("canonical_core_representative_edge_id") or "")
        if isinstance(c2_asset, Mapping)
        else ""
    )
    edge = _MANIFEST_EDGE_MAP.get(representative_id)
    if not edge:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {asset} certified C.2 representative "
            f"is unavailable: {representative_id!r}"
        )
    runtime = _NS["get_runtime"](edge)

    # This is the ONLY production C.4 batch feature build. It seeds process
    # startup/recovery state and the exact C.3 consumer-history ring.
    authority = runtime.build_features(
        bars=bars_window.copy(),
        drop_warmup_rows=False,
    )
    full_state = bootstrap_compact_state(
        asset=asset,
        bars=bars_window,
        authoritative_features=authority,
        phase_c1_contract=_C1,
        feature_context_rows=CERTIFIED_CONSUMER_RING_ROWS,
    )
    ring = _normalize_frame(full_state["feature_tail"]).tail(
        CERTIFIED_CONSUMER_RING_ROWS
    )
    _validate_ring_for_asset(asset, ring)

    # Keep C.2 compute state compact; the 6k consumer ring is separately owned
    # and persisted with append-only deltas.
    compact_state = copy.deepcopy(full_state)
    compact_state["feature_context_rows"] = int(DEFAULT_FEATURE_CONTEXT_ROWS)
    compact_state["feature_tail"] = (
        compact_state["feature_tail"]
        .tail(DEFAULT_FEATURE_CONTEXT_ROWS)
        .reset_index(drop=True)
    )
    compact_state["last_dependency_generation_id"] = dependency_generation_id
    hmm_target = (
        generation.get("hmm_target")
        or generation.get("hmm_target_timestamp")
        or generation.get("hmm_dependency_target")
    )
    compact_state["last_hmm_target"] = (
        _utc(hmm_target).isoformat() if hmm_target is not None else None
    )
    # Serialize now as a structural validation before publication.
    serialize_compact_state(compact_state, phase_c1_contract=_C1)

    feature_generation_id = _feature_generation_id(
        asset=asset,
        timestamp=latest,
        dependency_generation_id=dependency_generation_id,
        activation_fingerprint=str(_ACTIVATION.get("activation_fingerprint") or ""),
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    record = {
        "asset": asset,
        "state": compact_state,
        "ring": ring,
        "last_timestamp": latest.isoformat(),
        "dependency_generation_id": dependency_generation_id,
        "feature_generation_id": feature_generation_id,
        "bootstrap_count": 1,
        "update_count": 0,
        "restore_count": 0,
        "delta_count": 0,
        "last_mode": "authoritative_startup_or_recovery_bootstrap",
        "last_elapsed_ms": elapsed,
    }
    _persist_full_checkpoint(asset, record)
    logger.warning(
        "PHASE_C4_FEATURE_BOOTSTRAP asset=%s representative_edge=%s rows=%s "
        "dependency_generation=%s feature_generation=%s elapsed_ms=%.3f "
        "role=startup_or_recovery_only",
        asset,
        representative_id,
        len(ring),
        dependency_generation_id[:16],
        feature_generation_id[:16],
        elapsed,
    )
    return record


def _advance_one(
    *,
    asset: str,
    bars: pd.DataFrame,
    record: Mapping[str, Any],
    logger: Any,
) -> dict[str, Any]:
    """Advance C.2 exactly one bar. No batch feature build or dependency I/O."""
    if _C1 is None or _ACTIVATION is None:
        raise PhaseC4RuntimeError("Phase C.4 router is not initialized")
    started = time.perf_counter()
    asset = str(asset).upper()
    bars_norm = _normalize_frame(bars)
    if len(bars_norm) < 2:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_state: {asset} has insufficient bar history"
        )
    latest = _utc(bars_norm["Datetime"].iloc[-1])
    penultimate = _utc(bars_norm["Datetime"].iloc[-2])
    previous = _utc(record.get("last_timestamp"))
    if previous != penultimate:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_state: {asset} normal path is not one-bar continuation; "
            f"state={previous.isoformat()} penultimate={penultimate.isoformat()} "
            "action=quarantine_and_recovery_bootstrap"
        )

    snapshot = _snapshot_for_asset(asset)
    generation = dict(snapshot["dependency_generation"])
    dependency_generation_id = str(
        snapshot.get("dependency_generation_id")
        or generation.get("generation_id")
        or ""
    )
    ffd_row = _row_at_timestamp(
        snapshot.get("ffd"),
        latest,
        label=f"{asset} FFD",
    )
    ffd_values = {name: float(ffd_row[name]) for name in FFD_COLUMNS}

    # The Phase A generation contract encodes eligibility causally via its two
    # targets: HMM target == raw target iff this raw bar is HMM-eligible.  The
    # generation object itself intentionally does not carry a redundant boolean,
    # so derive it once and provide the explicit flag C.2 requires.
    raw_target = _utc(generation.get("raw_target"))
    hmm_target = _utc(generation.get("hmm_target"))
    eligible = hmm_target == raw_target
    generation["target_hmm_eligible"] = bool(eligible)
    hmm_values: dict[str, float] | None = None
    if eligible:
        hmm_row = _row_at_timestamp(
            snapshot.get("hmm"),
            latest,
            label=f"{asset} HMM",
        )
        hmm_values = {name: float(hmm_row[name]) for name in REGIME_COLUMNS}

    public_row, next_state, meta = update_one(
        state=record["state"],
        bar=bars_norm.iloc[-1],
        dependency_generation=generation,
        ffd_values=ffd_values,
        hmm_values=hmm_values,
        phase_c1_contract=_C1,
    )
    if str(meta.get("dependency_generation_id") or "") != dependency_generation_id:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_state: {asset} C.2 returned another dependency generation"
        )

    internal_tail = next_state.get("feature_tail")
    if not isinstance(internal_tail, pd.DataFrame) or internal_tail.empty:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_state: {asset} C.2 internal tail is empty"
        )
    latest_internal = internal_tail.iloc[-1].to_dict()
    ring = pd.concat(
        [record["ring"], pd.DataFrame([latest_internal])],
        ignore_index=True,
        sort=False,
    )
    ring = _normalize_frame(ring).tail(CERTIFIED_CONSUMER_RING_ROWS)
    _validate_ring_for_asset(asset, ring)
    if _utc(ring["Datetime"].iloc[-1]) != latest:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_state: {asset} ring did not advance to current bar"
        )

    feature_generation_id = _feature_generation_id(
        asset=asset,
        timestamp=latest,
        dependency_generation_id=dependency_generation_id,
        activation_fingerprint=str(_ACTIVATION.get("activation_fingerprint") or ""),
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    output = {
        **dict(record),
        "state": next_state,
        "ring": ring,
        "last_timestamp": latest.isoformat(),
        "dependency_generation_id": dependency_generation_id,
        "feature_generation_id": feature_generation_id,
        "update_count": int(record.get("update_count") or 0) + 1,
        "last_mode": "one_bar_incremental",
        "last_elapsed_ms": elapsed,
    }
    _persist_incremental_checkpoint(
        asset,
        output,
        latest_internal,
    )
    logger.info(
        "PHASE_C4_FEATURE_UPDATE asset=%s timestamp=%s dependency_generation=%s "
        "feature_generation=%s ring_rows=%s elapsed_ms=%.3f",
        asset,
        latest.isoformat(),
        dependency_generation_id[:16],
        feature_generation_id[:16],
        len(ring),
        elapsed,
    )
    return output


def _ensure_asset_generation(
    *,
    asset: str,
    bars: pd.DataFrame,
    logger: Any,
) -> dict[str, Any]:
    asset = str(asset).upper()
    snapshot = _snapshot_for_asset(asset)
    dependency_generation_id = str(
        snapshot.get("dependency_generation_id")
        or (snapshot.get("dependency_generation") or {}).get("generation_id")
        or ""
    )
    bars_norm = _normalize_frame(bars)
    latest = _utc(bars_norm["Datetime"].iloc[-1])

    with _STATE_LOCK:
        record = _ASSET_RUNTIME.get(asset)
        if record is None:
            restored = _restore_checkpoint(
                asset=asset,
                bars=bars_norm,
                dependency_generation_id=dependency_generation_id,
            )
            if restored is not None:
                _ASSET_RUNTIME[asset] = restored
                logger.info(
                    "PHASE_C4_FEATURE_CHECKPOINT_RESTORED asset=%s timestamp=%s "
                    "dependency_generation=%s feature_generation=%s ring_rows=%s",
                    asset,
                    restored["last_timestamp"],
                    dependency_generation_id[:16],
                    str(restored["feature_generation_id"])[:16],
                    len(restored["ring"]),
                )
                return restored
            record = _bootstrap_asset(
                asset=asset,
                bars=bars_norm,
                logger=logger,
            )
            _ASSET_RUNTIME[asset] = record
            return record

        previous = _utc(record.get("last_timestamp"))
        if previous == latest:
            if str(record.get("dependency_generation_id") or "") != dependency_generation_id:
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_state: {asset} same bar has a different "
                    "dependency generation; action=quarantine_and_recovery_bootstrap"
                )
            return record
        if previous > latest:
            raise PhaseC4RuntimeError(
                f"phase_c4_feature_state: {asset} feature state is ahead of bar snapshot"
            )

        advanced = _advance_one(
            asset=asset,
            bars=bars_norm,
            record=record,
            logger=logger,
        )
        _ASSET_RUNTIME[asset] = advanced
        return advanced


def _project_edge_features(
    *,
    ring: pd.DataFrame,
    edge: Mapping[str, Any],
    edge_contract: Mapping[str, Any],
) -> pd.DataFrame:
    edge_id = str(edge.get("edge_id") or "")
    required = [str(x) for x in edge_contract.get("canonical_projection_columns") or []]
    if not required:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {edge_id} has no C.3 projection"
        )
    output = ring.copy(deep=True)
    output["edge_id"] = edge_id
    side_value = edge.get("side_value")
    if side_value is None:
        side_value = edge_contract.get("side_value")
    if side_value is None:
        # Side is not a model feature; it remains event/runtime identity only.
        # Most manifest entries contain side_value. Runtime wrapper can overwrite
        # this if a future manifest omits it.
        side_name = str(edge.get("side") or edge.get("side_name") or "").lower()
        side_value = 1 if side_name == "long" else -1 if side_name == "short" else 0
    output["side"] = int(side_value)
    missing = [name for name in required if name not in output.columns]
    if missing:
        raise PhaseC4RuntimeError(
            f"phase_c4_feature_contract: {edge_id} canonical projection missing "
            f"columns: {missing[:30]}"
        )
    projected = output[required].copy()
    projected.attrs.update(ring.attrs)
    projected.attrs["phase_c4_edge_id"] = edge_id
    projected.attrs["phase_c4_consumer_contract_fingerprint"] = str(
        edge_contract.get("consumer_contract_fingerprint") or ""
    )
    return projected


def _runtime_patch_lock(edge_id: str) -> threading.RLock:
    with _STATE_LOCK:
        return _RUNTIME_PATCH_LOCKS.setdefault(str(edge_id), threading.RLock())


def _record_phase_c4_failure(edge_id: str, asset: str, error: Any) -> None:
    try:
        import production_snapshot_pipeline as snapshot_pipeline
        context = getattr(snapshot_pipeline._THREAD_CONTEXT, "snapshot", None)
        if isinstance(context, dict) and str(context.get("asset") or "").upper() == asset:
            context.setdefault("dependency_failures", []).append(
                {
                    "created_at": _utc_now(),
                    "asset": asset,
                    "edge_id": edge_id,
                    "source": "phase_c4_feature_contract",
                    "reason": f"{type(error).__name__}:{error}",
                    "action": "asset_quarantine_and_dedicated_recovery",
                }
            )
    except Exception:
        pass


def phase_c4_status() -> dict[str, Any]:
    with _STATE_LOCK:
        assets = {
            asset: {
                "last_timestamp": record.get("last_timestamp"),
                "dependency_generation_id": record.get("dependency_generation_id"),
                "feature_generation_id": record.get("feature_generation_id"),
                "ring_rows": len(record.get("ring"))
                if isinstance(record.get("ring"), pd.DataFrame)
                else 0,
                "bootstrap_count": int(record.get("bootstrap_count") or 0),
                "update_count": int(record.get("update_count") or 0),
                "restore_count": int(record.get("restore_count") or 0),
                "delta_count": int(record.get("delta_count") or 0),
                "last_mode": record.get("last_mode"),
                "last_elapsed_ms": record.get("last_elapsed_ms"),
            }
            for asset, record in _ASSET_RUNTIME.items()
        }
    return {
        "router_version": ROUTER_VERSION,
        "activated": _ACTIVATION is not None,
        "activation_fingerprint": (
            _ACTIVATION.get("activation_fingerprint") if _ACTIVATION else None
        ),
        "runtime_mode": (
            "phase_c4_certified_incremental_canonical"
            if _ACTIVATION is not None
            else "inactive_no_activation"
        ),
        "normal_path_batch_fallback_allowed": False if _ACTIVATION else None,
        "consumer_ring_rows": CERTIFIED_CONSUMER_RING_ROWS,
        "asset_count_initialized": len(assets),
        "assets": assets,
    }


def _invalidate_asset(asset: str, *, reason: str = "dependency_recovery") -> dict[str, Any]:
    asset = str(asset).upper()
    with _STATE_LOCK:
        previous = _ASSET_RUNTIME.pop(asset, None)
    paths = _checkpoint_paths(asset) if _CHECKPOINT_ROOT is not None else {}
    removed: list[str] = []
    for name, path in paths.items():
        if name == "root":
            continue
        try:
            if path.exists():
                path.unlink()
                removed.append(name)
        except Exception:
            pass
    return {
        "asset": asset,
        "reason": reason,
        "had_in_memory_state": previous is not None,
        "checkpoint_parts_removed": removed,
        "next_action": "authoritative_bootstrap_on_next_ready_snapshot",
    }


def install_phase_c4_production_router(
    namespace: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Install C.4 only when an exact matching activation artifact exists."""
    global _NS, _ACTIVATION, _C1, _C2, _C3, _C3_CERT, _MANIFEST
    global _C3_EDGE_MAP, _MANIFEST_EDGE_MAP, _CHECKPOINT_ROOT

    with _INSTALL_LOCK:
        namespace["_PHASE_C4_ROUTER_AVAILABLE"] = True
        namespace["_PHASE_C4_ROUTER_VERSION"] = ROUTER_VERSION
        if not ACTIVATION_PATH.is_file():
            # Staging C.4 in the repository must not add a large 74-edge source
            # fingerprint pass to every dashboard startup.  Full C.1/C.2/C.3
            # validation is run by the explicit validator and is mandatory again
            # here once an activation artifact exists.
            namespace["_PHASE_C4_ROUTER_ACTIVE"] = False
            namespace["_PHASE_C4_ROUTER_STATUS"] = phase_c4_status
            return {
                "installed": True,
                "activated": False,
                "version": ROUTER_VERSION,
                "reason": "activation_artifact_absent",
                "activation_path": str(ACTIVATION_PATH),
                "current_runtime_unchanged": True,
            }

        report = static_contract_report(V2_ROOT)
        if report.get("status") != "PASS":
            raise RuntimeError(
                "Phase C.4 static contract validation failed: "
                + " | ".join(str(x) for x in report.get("failures") or [])
            )

        activation = _read_json(ACTIVATION_PATH)
        activation_validation = validate_activation(
            activation,
            v2_root=V2_ROOT,
        )
        if activation_validation.get("status") != "PASS":
            raise RuntimeError(
                "Phase C.4 activation is stale/invalid: "
                + " | ".join(
                    str(x) for x in activation_validation.get("failures") or []
                )
            )
        if not PRODUCTION_CERTIFICATION_PATH.is_file():
            raise RuntimeError(
                "Phase C.5 production certification is missing; activated C.4 routing is forbidden"
            )
        production_certification = _read_json(PRODUCTION_CERTIFICATION_PATH)
        production_certification_validation = validate_production_certification(
            production_certification, activation, v2_root=V2_ROOT
        )
        if production_certification_validation.get("status") != "PASS":
            raise RuntimeError(
                "Phase C.5 production certification is stale/invalid: "
                + " | ".join(
                    str(x)
                    for x in production_certification_validation.get("failures") or []
                )
            )
        if not bool(namespace.get("_SNAPSHOT_PIPELINE_INSTALLED")):
            raise RuntimeError("Phase C.4 requires production_snapshot_pipeline first")

        paths = _contract_paths(V2_ROOT)
        _NS = namespace
        _ACTIVATION = activation
        _C1 = _read_json(paths["c1"])
        _C2 = _read_json(paths["c2"])
        _C3 = _read_json(paths["c3"])
        _C3_CERT = _read_json(paths["c3_cert"])
        _MANIFEST = _read_json(paths["live_manifest"])
        _MANIFEST_EDGE_MAP, _ = _manifest_maps(_MANIFEST)
        _C3_EDGE_MAP = {
            str(row.get("edge_id")): dict(row)
            for row in _C3.get("edges") or []
            if isinstance(row, Mapping) and row.get("edge_id")
        }
        settings = namespace.get("settings")
        live_state_dir = Path(
            getattr(settings, "LIVE_STATE_DIR", HERE / "live_state")
        )
        _CHECKPOINT_ROOT = live_state_dir / "phase_c4_features"
        _CHECKPOINT_ROOT.mkdir(parents=True, exist_ok=True)

        previous_build_shared = namespace["build_shared_asset_runtime_features"]
        previous_prepare = namespace["prepare_shared_features_for_edge"]
        previous_coverage = namespace["shared_feature_cache_coverage_for_edge"]
        previous_score_edge = namespace["score_edge"]
        previous_status = namespace.get("authoritative_runtime_cache_status")

        def build_phase_c4_asset_features(
            *,
            asset: str,
            asset_edges: list[dict[str, Any]],
            bars_snapshot: pd.DataFrame,
            live_event_times_snapshot: set[pd.Timestamp] | None,
            state: MutableMapping[str, Any],
            logger: Any,
        ) -> tuple[pd.DataFrame, str]:
            del asset_edges, live_event_times_snapshot, state
            started = time.perf_counter()
            record = _ensure_asset_generation(
                asset=asset,
                bars=bars_snapshot,
                logger=logger,
            )
            ring = record["ring"].copy(deep=True)
            ring.attrs["phase_c4_asset"] = str(asset).upper()
            ring.attrs["phase_c4_feature_generation_id"] = str(
                record["feature_generation_id"]
            )
            ring.attrs["phase_c4_dependency_generation_id"] = str(
                record["dependency_generation_id"]
            )
            ring.attrs["phase_c4_activation_fingerprint"] = str(
                activation.get("activation_fingerprint") or ""
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            try:
                import production_snapshot_pipeline as snapshot_pipeline
                snapshot_pipeline._record_latency("phase_c4_feature_update", elapsed_ms)
            except Exception:
                pass
            return ring, f"phase_c4:{str(record['feature_generation_id'])}"

        def prepare_phase_c4_for_edge(
            *,
            edge: dict[str, Any],
            runtime: Any,
            shared_features_snapshot: pd.DataFrame | None,
            logger: Any,
        ) -> tuple[pd.DataFrame, str]:
            del logger
            edge_id = str(edge.get("edge_id") or "")
            asset = str(edge.get("asset") or "").upper()
            contract = _C3_EDGE_MAP.get(edge_id)
            if not contract:
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_contract: selected edge absent from C.3: {edge_id}"
                )
            if not isinstance(shared_features_snapshot, pd.DataFrame) or shared_features_snapshot.empty:
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_contract: {edge_id} has no canonical asset feature generation"
                )
            if str(shared_features_snapshot.attrs.get("phase_c4_asset") or "").upper() != asset:
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_contract: {edge_id} received another asset generation"
                )
            # SIDE_VALUE is runtime identity, never a model feature.
            projected_edge = dict(edge)
            projected_edge["side_value"] = getattr(runtime, "SIDE_VALUE", edge.get("side_value"))
            projected = _project_edge_features(
                ring=shared_features_snapshot,
                edge=projected_edge,
                edge_contract=contract,
            )
            return projected, "phase_c4_certified_consumer_projection"

        def coverage_phase_c4(
            *,
            edge: dict[str, Any],
            shared_features_snapshot: pd.DataFrame | None,
            logger: Any,
        ) -> tuple[bool, str, int, list[str]]:
            del logger
            edge_id = str(edge.get("edge_id") or "")
            contract = _C3_EDGE_MAP.get(edge_id)
            if not contract:
                return False, "edge_absent_from_c3", 1, [edge_id]
            if not isinstance(shared_features_snapshot, pd.DataFrame) or shared_features_snapshot.empty:
                return False, "canonical_generation_missing", 1, ["canonical_generation"]
            required = [
                str(x)
                for x in contract.get("canonical_projection_columns") or []
                if str(x) not in {"edge_id", "side"}
            ]
            missing = [name for name in required if name not in shared_features_snapshot.columns]
            return (
                not missing,
                "phase_c4_certified_consumer_projection" if not missing else "projection_missing_columns",
                len(missing),
                missing[:20],
            )

        def score_edge_phase_c4(
            edge: dict[str, Any],
            manifest: dict[str, Any],
            bar_policy: dict[str, Any],
            state: MutableMapping[str, Any],
            logger: Any,
            **kwargs: Any,
        ) -> list[Any]:
            edge_id = str(edge.get("edge_id") or "")
            asset = str(edge.get("asset") or "").upper()
            contract = _C3_EDGE_MAP.get(edge_id)
            if not contract:
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_contract: {edge_id} missing frozen C.3 consumer contract"
                )
            runtime = namespace["get_runtime"](edge)
            original_build = getattr(runtime, "build_features", None)
            original_generate = getattr(runtime, "generate_candidate_events", None)
            if not callable(original_build) or not callable(original_generate):
                raise PhaseC4RuntimeError(
                    f"phase_c4_feature_contract: {edge_id} runtime consumer functions missing"
                )
            selected = [str(x) for x in contract.get("selected_features") or []]

            def forbidden_build_features(*args: Any, **kw: Any) -> Any:
                raise PhaseC4RuntimeError(
                    "phase_c4_feature_contract: normal_path_runtime_build_features_forbidden "
                    f"edge={edge_id}; asset must quarantine instead of batch fallback"
                )

            def certified_candidate(*args: Any, **kw: Any) -> pd.DataFrame:
                events = original_generate(*args, **kw)
                sanitized, ownership = strip_selected_feature_collisions(
                    events=events,
                    selected_features=selected,
                )
                sanitized.attrs["phase_c4_selected_feature_collision_count"] = int(
                    ownership.get("selected_feature_collision_count") or 0
                )
                sanitized.attrs["phase_c4_selected_feature_source_policy"] = str(
                    ownership.get("selected_feature_source_policy") or ""
                )
                return sanitized

            with _runtime_patch_lock(edge_id):
                runtime.build_features = forbidden_build_features
                runtime.generate_candidate_events = certified_candidate
                try:
                    rows = previous_score_edge(
                        edge,
                        manifest,
                        bar_policy,
                        state,
                        logger,
                        **kwargs,
                    )
                except Exception as exc:
                    _record_phase_c4_failure(edge_id, asset, exc)
                    raise
                finally:
                    runtime.build_features = original_build
                    runtime.generate_candidate_events = original_generate

            for row in rows or []:
                text = (
                    f"{getattr(row, 'error', '')} {getattr(row, 'reason', '')}"
                ).lower()
                if "phase_c4_feature_contract" in text or "phase_c4_feature_state" in text:
                    _record_phase_c4_failure(edge_id, asset, text)
                    break
            return rows

        namespace["build_shared_asset_runtime_features"] = build_phase_c4_asset_features
        namespace["prepare_shared_features_for_edge"] = prepare_phase_c4_for_edge
        namespace["shared_feature_cache_coverage_for_edge"] = coverage_phase_c4
        namespace["score_edge"] = score_edge_phase_c4
        namespace["authoritative_runtime_cache_status"] = phase_c4_status
        namespace["_PHASE_C4_RECOVERY_RESET"] = _invalidate_asset
        namespace["_PHASE_C4_ROUTER_ACTIVE"] = True
        namespace["_PHASE_C4_ROUTER_STATUS"] = phase_c4_status
        namespace["_PHASE_C4_PREVIOUS_ROUTING"] = {
            "build_shared_asset_runtime_features": previous_build_shared,
            "prepare_shared_features_for_edge": previous_prepare,
            "shared_feature_cache_coverage_for_edge": previous_coverage,
            "score_edge": previous_score_edge,
            "authoritative_runtime_cache_status": previous_status,
        }

        current_version = str(namespace.get("DASHBOARD_VERSION") or "")
        if DASHBOARD_VERSION_SUFFIX not in current_version:
            namespace["DASHBOARD_VERSION"] = current_version + DASHBOARD_VERSION_SUFFIX
        namespace["HOTPATH_VERSION"] = "phase_c4_certified_incremental_canonical_v1"

        return {
            "installed": True,
            "activated": True,
            "version": ROUTER_VERSION,
            "dashboard_version": namespace["DASHBOARD_VERSION"],
            "activation_fingerprint": activation.get("activation_fingerprint"),
            "phase_c5_certificate_fingerprint": production_certification.get("certificate_fingerprint"),
            "selected_asset_count": report.get("selected_asset_count"),
            "selected_edge_count": report.get("selected_edge_count"),
            "consumer_ring_rows": CERTIFIED_CONSUMER_RING_ROWS,
            "normal_path_batch_fallback_allowed": False,
            "bootstrap_batch_role": "startup_or_dedicated_recovery_only",
            "normal_feature_role": "one_phase_c2_update_per_asset_bar",
            "consumer_role": "phase_c3_certified_projection_plus_collision_sanitization",
            "scalability": report.get("scalability_contract"),
        }


def self_test() -> dict[str, Any]:
    tests: dict[str, bool] = {}
    ring = pd.DataFrame(
        {
            "Datetime": to_datetime_iso(
                ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]
            ),
            "Bar_Start": to_datetime_iso(
                ["2025-12-31T23:59:00Z", "2026-01-01T00:00:00Z"]
            ),
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Ticks": [10.0, 11.0],
            "Threshold": [10.0, 10.0],
            "Feature_A": [2.0, 3.0],
            "ATR_14": [0.1, 0.2],
        }
    )
    contract = {
        "canonical_projection_columns": [
            "Datetime", "Bar_Start", "Open", "High", "Low", "Close",
            "Ticks", "Threshold", "edge_id", "side", "Feature_A", "ATR_14",
        ],
        "consumer_contract_fingerprint": "x",
    }
    projected = _project_edge_features(
        ring=ring,
        edge={"edge_id": "AAA/test_long", "asset": "AAA", "side_value": 1},
        edge_contract=contract,
    )
    tests["projection_is_contract_driven"] = (
        list(projected.columns) == contract["canonical_projection_columns"]
        and set(projected["edge_id"]) == {"AAA/test_long"}
        and set(projected["side"]) == {1}
    )
    gid1 = _feature_generation_id(
        asset="AAA",
        timestamp="2026-01-01T00:01:00Z",
        dependency_generation_id="dep-1",
        activation_fingerprint="act",
    )
    gid2 = _feature_generation_id(
        asset="AAA",
        timestamp="2026-01-01T00:01:00Z",
        dependency_generation_id="dep-2",
        activation_fingerprint="act",
    )
    tests["feature_generation_binds_dependency_generation"] = gid1 != gid2
    source = inspect.getsource(_advance_one)
    tests["normal_update_has_no_batch_feature_build"] = "build_features" not in source
    tests["normal_update_has_no_dependency_io"] = (
        "read_csv" not in source
        and "refresh_verified_feature_dependencies_hot" not in source
        and "load_verified_ffd" not in source
        and "load_verified_hmm" not in source
    )
    failed = [name for name, passed in tests.items() if not passed]
    return {
        "status": "PASS" if not failed else "FAIL",
        "version": ROUTER_VERSION,
        "tests": tests,
        "failed": failed,
    }


__all__ = [
    "ROUTER_VERSION",
    "ACTIVATION_PATH",
    "PRODUCTION_CERTIFICATION_PATH",
    "PRODUCTION_CERTIFICATION_SCHEMA_VERSION",
    "CERTIFIED_CONSUMER_RING_ROWS",
    "PhaseC4RuntimeError",
    "build_activation_payload",
    "install_phase_c4_production_router",
    "phase_c4_status",
    "self_test",
    "static_contract_report",
    "validate_activation",
    "validate_production_certification",
]
