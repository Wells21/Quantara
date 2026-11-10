from __future__ import annotations

"""Phase D.3 candidate-first production-routing overlay.

D.3 installs after the certified Phase C.4 router.  It does not own bar
construction, dependency continuation, canonical feature math, candidate math,
primary-side math, model math, calibration, AFML, thresholds, allocation, risk,
or execution.

When (and only when) an exact D.3 activation artifact exists, the live edge
consumer path becomes:

    one immutable C.4 6,000-row canonical asset generation
        -> D.1/D.2 certified narrow candidate projection
        -> unchanged candidate + primary-side logic
        -> if no current executable candidate: stop
        -> if a candidate survives the existing dashboard cursor/live-time gate:
             build the exact full C.3 projection
             -> unchanged runtime.score / shared EdgeScorer

The existing dashboard runtime cache remains authoritative.  D.3 deliberately
does not create another runtime/model artifact cache.

Normal-path runtime.build_features remains forbidden.  There is no hidden
historical/full-projection fallback after D.3 activation.
"""

import copy
import hashlib
import inspect
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping

import pandas as pd

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

import phase_c_04_production_router as c4

from shared.edges.phase_c_consumer_adapter import strip_selected_feature_collisions
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.phase_d_candidate_first import (
    D2_ENGINE_VERSION,
    PhaseD2ContractError,
    edge_map as d_contract_edge_map,
    load_phase_d1_contract,
    manifest_edge_map,
    project_candidate_features,
    project_full_c3_features,
    source_guard,
)

D3_ROUTER_VERSION = "2026-08-17_phase_d3_candidate_first_deferred_model_projection_v1"
D3_SCHEMA_VERSION = "phase_d3_candidate_first_production_router_v1"
D3_ACTIVATION_SCHEMA_VERSION = "phase_d3_candidate_first_activation_v1"
D3_CERTIFICATION_SCHEMA_VERSION = "phase_d3_candidate_first_certification_v1"

PHASE_D_DIR = HERE / "config" / "phase_d"
D1_CONTRACT_PATH = PHASE_D_DIR / "candidate_contract.json"
D2_CERTIFICATION_PATH = PHASE_D_DIR / "candidate_first_certification.json"
D3_CERTIFICATION_PATH = PHASE_D_DIR / "candidate_first_routing_certification.json"
D3_ACTIVATION_PATH = PHASE_D_DIR / "production_routing_activation.json"
C3_CONTRACT_PATH = HERE / "config" / "phase_c" / "consumer_contract.json"
LIVE_MANIFEST_PATH = HERE / "live_manifest.json"

_INSTALL_LOCK = threading.RLock()
_EDGE_LOCKS_GUARD = threading.RLock()
_EDGE_LOCKS: dict[str, threading.RLock] = {}
_METRICS_LOCK = threading.RLock()

_NS: MutableMapping[str, Any] | None = None
_ACTIVATION: dict[str, Any] | None = None
_D1: dict[str, Any] | None = None
_D2_CERT: dict[str, Any] | None = None
_C3: dict[str, Any] | None = None
_D1_EDGE_MAP: dict[str, dict[str, Any]] = {}
_C3_EDGE_MAP: dict[str, dict[str, Any]] = {}

_METRICS: dict[str, Any] = {
    "candidate_projection_calls": 0,
    "candidate_generation_calls": 0,
    "deferred_full_projection_calls": 0,
    "edges_returned_without_model_score": 0,
    "score_calls": 0,
    "failures": 0,
    "candidate_projection_ms_sum": 0.0,
    "deferred_full_projection_ms_sum": 0.0,
    "authoritative_c4_edge_calls": 0,
}


class PhaseD3RuntimeError(RuntimeError):
    """Fail-closed Phase D.3 routing error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise PhaseD3RuntimeError(f"expected JSON object: {path}")
    return payload


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
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _git_head(v2_root: Path = V2_ROOT) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(Path(v2_root).resolve().parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise PhaseD3RuntimeError(
            "cannot resolve git HEAD: "
            + (proc.stderr.strip() or proc.stdout.strip())
        )
    return proc.stdout.strip()


def _edge_lock(edge_id: str) -> threading.RLock:
    with _EDGE_LOCKS_GUARD:
        return _EDGE_LOCKS.setdefault(str(edge_id), threading.RLock())


def _metric_add(name: str, value: float = 1.0) -> None:
    with _METRICS_LOCK:
        current = _METRICS.get(name, 0)
        _METRICS[name] = current + value


def _record_latency(stage: str, elapsed_ms: float) -> None:
    try:
        import production_snapshot_pipeline as snapshot_pipeline
        snapshot_pipeline._record_latency(stage, float(elapsed_ms))
    except Exception:
        pass


def _selected_assets(manifest_edges: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        {
            str(row.get("asset") or "").upper()
            for row in manifest_edges.values()
            if str(row.get("asset") or "")
        }
    )


# PHASE_D4_10_HYBRID_D3_ROUTING_V1
_D3_OPTIMIZED_EDGE_IDS: set[str] = set()
_D3_CURRENT_SELECTED_EDGE_IDS: set[str] = set()


def _phase_d310_pass_edge_ids(
    edge_results: Mapping[str, Any], current_edge_ids: set[str],
) -> list[str]:
    return sorted(
        str(edge_id)
        for edge_id, row in (edge_results or {}).items()
        if str(edge_id) in current_edge_ids
        and isinstance(row, Mapping)
        and str(row.get("status") or "").upper() == "PASS"
    )


def _phase_d310_d2_certified_edge_ids(
    d2_cert: Mapping[str, Any], current_edge_ids: set[str],
) -> list[str]:
    return _phase_d310_pass_edge_ids(d2_cert.get("edges") or {}, current_edge_ids)


def _phase_d310_d3_certified_edge_ids(
    routing_cert: Mapping[str, Any],
    d2_certified_edge_ids: set[str],
    current_edge_ids: set[str],
) -> list[str]:
    projected = set(
        _phase_d310_pass_edge_ids(
            routing_cert.get("deferred_projection_results") or {}, current_edge_ids,
        )
    )
    c5_results = (
        (routing_cert.get("c5_model_decision_parity") or {}).get("edge_results") or {}
    )
    if c5_results:
        projected &= set(_phase_d310_pass_edge_ids(c5_results, current_edge_ids))
    return sorted(projected & set(d2_certified_edge_ids) & current_edge_ids)

def static_contract_report(v2_root: Path = V2_ROOT) -> dict[str, Any]:
    """Validate current C4 authority plus the D3-optimized certified subset."""
    root = Path(v2_root).resolve()
    failures: list[str] = []
    try:
        paths = {
            "d1": root / "portfolio_live/config/phase_d/candidate_contract.json",
            "d2_cert": root / "portfolio_live/config/phase_d/candidate_first_certification.json",
            "c3": root / "portfolio_live/config/phase_c/consumer_contract.json",
            "manifest": root / "portfolio_live/live_manifest.json",
        }
        for label, path in paths.items():
            if not path.is_file():
                failures.append(f"missing {label}: {path}")
        if failures:
            return {"status": "FAIL", "failures": failures,
                    "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate"}

        d1 = load_phase_d1_contract(root, path=paths["d1"])
        d2_cert = _read_json(paths["d2_cert"])
        c3_contract = _read_json(paths["c3"])
        manifest = _read_json(paths["manifest"])
        d1_edges = d_contract_edge_map(d1)
        c3_edges = d_contract_edge_map(c3_contract)
        manifest_edges = manifest_edge_map(manifest)
        current_ids = set(manifest_edges)
        expected_edges = sorted(current_ids)
        assets = _selected_assets(manifest_edges)

        if set(d1_edges) != current_ids:
            failures.append("D.1/live-manifest selected-edge universe differs")
        if set(c3_edges) != current_ids:
            failures.append("C.3/live-manifest selected-edge universe differs")
        if d2_cert.get("status") != "PASS":
            failures.append("D.2 heavy certification is not PASS")
        if d2_cert.get("runtime_routing_enabled") is not False:
            failures.append("D.2 certification unexpectedly enables routing")
        if int(d2_cert.get("rows") or -1) != int(c4.CERTIFIED_CONSUMER_RING_ROWS):
            failures.append("D.2 did not certify exact C.4 consumer-ring authority")
        if list(d2_cert.get("edges_failed") or []):
            failures.append("D.2 contains failed edges")
        if list(d2_cert.get("fatal_errors") or []):
            failures.append("D.2 contains fatal errors")

        d2_certified = _phase_d310_d2_certified_edge_ids(d2_cert, current_ids)
        all_d2_pass = [
            edge_id for edge_id, row in (d2_cert.get("edges") or {}).items()
            if isinstance(row, Mapping) and str(row.get("status") or "").upper() == "PASS"
        ]
        if int(d2_cert.get("edges_passed") or -1) != len(all_d2_pass):
            failures.append("D.2 passed-edge count is inconsistent with its edge results")

        guard = source_guard(v2_root=root, d1_contract=d1, c3_contract=c3_contract)
        if guard.get("status") != "PASS":
            failures.extend("source_guard:" + str(x) for x in guard.get("failures") or [])

        c4_static = c4.static_contract_report(root)
        if c4_static.get("status") != "PASS":
            failures.extend("c4_static:" + str(x) for x in c4_static.get("failures") or [])
        else:
            if list(c4_static.get("selected_edge_ids") or []) != expected_edges:
                failures.append("C.4 selected-edge universe differs from current manifest")
            if list(c4_static.get("selected_assets") or []) != assets:
                failures.append("C.4 selected-asset universe differs from current manifest")

        authoritative_ids = sorted(current_ids.difference(d2_certified))
        return {
            "status": "PASS" if not failures else "FAIL",
            "failures": failures,
            "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
            "selected_edge_ids": expected_edges,
            "selected_edge_count": len(expected_edges),
            "selected_assets": assets,
            "selected_asset_count": len(assets),
            "d2_certified_edge_ids": d2_certified,
            "d2_certified_edge_count": len(d2_certified),
            "c4_authoritative_edge_ids": authoritative_ids,
            "c4_authoritative_edge_count": len(authoritative_ids),
            "d2_engine_version": d2_cert.get("engine_version"),
            "d2_aggregate_speedup_ratio": ((d2_cert.get("timing") or {}).get(
                "aggregate_full_to_narrow_speedup_ratio")),
            "c3_schema_version": c3_contract.get("schema_version"),
            "c4_router_version": c4.ROUTER_VERSION,
            "d3_router_version": D3_ROUTER_VERSION,
            "source_guard": guard,
            "scalability_contract": {
                "phase_d_is_optional_acceleration": True,
                "uncertified_selected_edges_trade_via_c4_authority": True,
                "unsupported_trading_edges": 0,
            },
        }
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
        return {"status": "FAIL", "failures": failures,
                "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate"}



def _activation_contract(
    report: Mapping[str, Any], *, optimized_edge_ids: list[str] | None = None, **_: Any,
) -> dict[str, Any]:
    optimized = list(
        optimized_edge_ids if optimized_edge_ids is not None
        else (report.get("d2_certified_edge_ids") or [])
    )
    optimized_assets = sorted({
        str(edge_id).split("/", 1)[0].upper()
        for edge_id in optimized if "/" in str(edge_id)
    })
    return {
        "schema_version": D3_ACTIVATION_SCHEMA_VERSION,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "router_version": D3_ROUTER_VERSION,
        "d2_engine_version": D2_ENGINE_VERSION,
        "c4_router_version": str(report.get("c4_router_version") or ""),
        "selected_assets": optimized_assets,
        "selected_edge_ids": sorted(optimized),
        "normal_path_batch_fallback_allowed": False,
        "candidate_projection_owner": "phase_d1_d2_current_state_narrow_projection",
        "full_projection_policy": "deferred_until_existing_dashboard_current_candidate_gate",
        "runtime_residency_owner": "existing_dashboard_runtime_cache",
    }



def build_activation_payload(
    *, v2_root: Path = V2_ROOT, certification_path: Path = D3_CERTIFICATION_PATH,
) -> dict[str, Any]:
    report = static_contract_report(v2_root)
    if report.get("status") != "PASS":
        raise PhaseD3RuntimeError(
            "cannot build D.3 activation from invalid current authority: "
            + " | ".join(str(x) for x in report.get("failures") or [])
        )
    cert_path = Path(certification_path).resolve()
    if not cert_path.is_file():
        raise PhaseD3RuntimeError(f"D.3 certification missing: {cert_path}")
    certification = _read_json(cert_path)
    if certification.get("status") != "PASS":
        raise PhaseD3RuntimeError("D.3 certification is not PASS")
    if certification.get("production_activation_allowed") is not True:
        raise PhaseD3RuntimeError("D.3 certification does not allow production activation")
    current_ids = set(report.get("selected_edge_ids") or [])
    d2_ids = set(report.get("d2_certified_edge_ids") or [])
    optimized = _phase_d310_d3_certified_edge_ids(certification, d2_ids, current_ids)
    if not optimized:
        raise PhaseD3RuntimeError("D.3 certification has no currently selected certified edges")
    if certification.get("deferred_projection_edges_failed"):
        raise PhaseD3RuntimeError("D.3 certification contains failed deferred projections")
    if list(certification.get("fatal_errors") or []):
        raise PhaseD3RuntimeError("D.3 certification contains fatal errors")
    if (certification.get("c5_model_decision_parity") or {}).get("status") != "PASS":
        raise PhaseD3RuntimeError("D.3 C.5 model/decision parity is not PASS")
    if (certification.get("d2_candidate_authority") or {}).get("status") != "PASS":
        raise PhaseD3RuntimeError("D.3 D.2 candidate authority is not PASS")
    contract = _activation_contract(report, optimized_edge_ids=optimized)
    return {
        "schema_version": D3_ACTIVATION_SCHEMA_VERSION,
        "status": "PASS",
        "runtime_routing_enabled": True,
        "generated_at": _utc_now(),
        "contract": contract,
        "activation_fingerprint": _json_hash(contract),
    }



def validate_activation(
    activation: Mapping[str, Any], *, v2_root: Path = V2_ROOT,
) -> dict[str, Any]:
    failures: list[str] = []
    report = static_contract_report(v2_root)
    if activation.get("schema_version") != D3_ACTIVATION_SCHEMA_VERSION:
        failures.append("D.3 activation schema mismatch")
    if activation.get("status") != "PASS":
        failures.append("D.3 activation status is not PASS")
    if activation.get("runtime_routing_enabled") is not True:
        failures.append("D.3 activation routing flag is not true")
    if report.get("status") != "PASS":
        failures.extend("static:" + str(x) for x in report.get("failures") or [])
        return {"status": "FAIL", "failures": failures,
                "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate"}
    cert_path = Path(v2_root).resolve() / "portfolio_live/config/phase_d/candidate_first_routing_certification.json"
    if not cert_path.is_file():
        failures.append("D.3 routing certification file is missing")
        return {"status": "FAIL", "failures": failures,
                "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate"}
    cert = _read_json(cert_path)
    if cert.get("status") != "PASS":
        failures.append("D.3 routing certification is not PASS")
    if cert.get("production_activation_allowed") is not True:
        failures.append("D.3 routing certification does not allow activation")
    if cert.get("deferred_projection_edges_failed"):
        failures.append("D.3 routing certification contains failed deferred projections")
    if list(cert.get("fatal_errors") or []):
        failures.append("D.3 routing certification contains fatal errors")
    if (cert.get("c5_model_decision_parity") or {}).get("status") != "PASS":
        failures.append("D.3 routing certification C.5 parity is not PASS")
    if (cert.get("d2_candidate_authority") or {}).get("status") != "PASS":
        failures.append("D.3 routing certification D.2 authority is not PASS")
    current_ids = set(report.get("selected_edge_ids") or [])
    d2_ids = set(report.get("d2_certified_edge_ids") or [])
    optimized = _phase_d310_d3_certified_edge_ids(cert, d2_ids, current_ids)
    if not optimized:
        failures.append("D.3 routing certification has no current certified edges")
    expected_contract = _activation_contract(report, optimized_edge_ids=optimized)
    if activation.get("contract") != expected_contract:
        failures.append("D.3 activation optimized-subset contract differs from current authority")
    activation_ids = set(((activation.get("contract") or {}).get("selected_edge_ids") or []))
    if not activation_ids.issubset(current_ids):
        failures.append("D.3 activation contains edges absent from current manifest")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "activation_fingerprint": activation.get("activation_fingerprint"),
        "optimized_edge_ids": sorted(optimized),
        "optimized_edge_count": len(optimized),
        "authoritative_c4_edge_ids": sorted(current_ids.difference(optimized)),
        "authoritative_c4_edge_count": len(current_ids.difference(optimized)),
    }



def _record_failure(edge_id: str, asset: str, exc: Any) -> None:
    _metric_add("failures", 1)
    try:
        c4._record_phase_c4_failure(
            edge_id,
            asset,
            PhaseD3RuntimeError(f"phase_d3_candidate_first:{exc}"),
        )
    except Exception:
        pass


def phase_d3_status() -> dict[str, Any]:
    with _METRICS_LOCK:
        metrics = copy.deepcopy(_METRICS)
    current = set(_D3_CURRENT_SELECTED_EDGE_IDS)
    optimized = set(_D3_OPTIMIZED_EDGE_IDS)
    authoritative = sorted(current.difference(optimized))
    return {
        "schema_version": D3_SCHEMA_VERSION,
        "router_version": D3_ROUTER_VERSION,
        "activated": _ACTIVATION is not None,
        "activation_fingerprint": (_ACTIVATION.get("activation_fingerprint") if _ACTIVATION else None),
        "runtime_mode": ("phase_d3_candidate_first_hybrid_c4_authority" if _ACTIVATION else "inactive_no_activation"),
        "normal_path_batch_fallback_allowed": (False if _ACTIVATION else None),
        "runtime_residency_owner": "existing_dashboard_runtime_cache",
        "consumer_ring_rows": c4.CERTIFIED_CONSUMER_RING_ROWS,
        "selected_edge_count": len(current),
        "optimized_edge_count": len(optimized),
        "optimized_edge_ids": sorted(optimized),
        "authoritative_c4_edge_count": len(authoritative),
        "authoritative_c4_edge_ids": authoritative,
        "unsupported_trading_edges": 0,
        "metrics": metrics,
        "phase_c4": c4.phase_c4_status(),
    }


def _replace_bound_argument(
    callable_obj: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    name: str,
    value: Any,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Replace one argument without assuming every edge runtime has one signature."""
    signature = inspect.signature(callable_obj)
    bound = signature.bind_partial(*args, **kwargs)
    bound.arguments[name] = value
    return tuple(bound.args), dict(bound.kwargs)


def _sanitize_generated_candidate_events(
    *,
    events: pd.DataFrame,
    selected_features: list[str],
    edge_id: str,
) -> pd.DataFrame:
    """Apply D.3 ownership without rejecting a valid no-candidate result."""
    if isinstance(events, pd.DataFrame) and events.empty:
        if "Datetime" not in events.columns:
            raise PhaseD3RuntimeError(
                "phase_d3_candidate_contract: "
                f"empty candidate event table lacks Datetime edge={edge_id}"
            )
        sanitized = events.copy()
        sanitized.attrs["phase_d3_selected_feature_collision_count"] = 0
        sanitized.attrs[
            "phase_d3_selected_feature_source_policy"
        ] = "canonical_feature_table_only"
        return sanitized

    sanitized, ownership = strip_selected_feature_collisions(
        events=events,
        selected_features=selected_features,
    )
    sanitized.attrs[
        "phase_d3_selected_feature_collision_count"
    ] = int(
        ownership.get("selected_feature_collision_count") or 0
    )
    sanitized.attrs[
        "phase_d3_selected_feature_source_policy"
    ] = str(
        ownership.get("selected_feature_source_policy") or ""
    )
    return sanitized


def install_phase_d3_production_router(
    namespace: MutableMapping[str, Any],
) -> dict[str, Any]:
    """Install D.3 only when an exact release-bound activation artifact exists."""
    global _NS, _ACTIVATION, _D1, _D2_CERT, _C3
    global _D1_EDGE_MAP, _C3_EDGE_MAP
    global _D3_OPTIMIZED_EDGE_IDS, _D3_CURRENT_SELECTED_EDGE_IDS

    with _INSTALL_LOCK:
        namespace["_PHASE_D3_ROUTER_AVAILABLE"] = True
        namespace["_PHASE_D3_ROUTER_VERSION"] = D3_ROUTER_VERSION

        if not D3_ACTIVATION_PATH.is_file():
            namespace["_PHASE_D3_ROUTER_ACTIVE"] = False
            namespace["_PHASE_D3_ROUTER_STATUS"] = phase_d3_status
            return {
                "installed": True,
                "activated": False,
                "version": D3_ROUTER_VERSION,
                "reason": "activation_artifact_absent",
                "current_runtime_unchanged": True,
            }

        if namespace.get("_PHASE_C4_ROUTER_ACTIVE") is not True:
            raise PhaseD3RuntimeError("D.3 requires active certified C.4 routing")

        activation = _read_json(D3_ACTIVATION_PATH)
        activation_validation = validate_activation(
            activation,
            v2_root=V2_ROOT,
        )
        if activation_validation.get("status") != "PASS":
            raise PhaseD3RuntimeError(
                "D.3 activation is stale/invalid: "
                + " | ".join(
                    str(x)
                    for x in activation_validation.get("failures") or []
                )
            )

        report = static_contract_report(V2_ROOT)
        if report.get("status") != "PASS":
            raise PhaseD3RuntimeError(
                "D.3 static authority failed: "
                + " | ".join(str(x) for x in report.get("failures") or [])
            )

        _NS = namespace
        _ACTIVATION = dict(activation)
        _D1 = load_phase_d1_contract(V2_ROOT)
        _D2_CERT = _read_json(D2_CERTIFICATION_PATH)
        _C3 = _read_json(C3_CONTRACT_PATH)
        _D1_EDGE_MAP = d_contract_edge_map(_D1)
        _C3_EDGE_MAP = d_contract_edge_map(_C3)
        _D3_CURRENT_SELECTED_EDGE_IDS = set(_C3_EDGE_MAP)
        _D3_OPTIMIZED_EDGE_IDS = set(
            str(x) for x in ((activation.get("contract") or {}).get("selected_edge_ids") or [])
        )

        previous_routing = dict(
            namespace.get("_PHASE_C4_PREVIOUS_ROUTING") or {}
        )
        base_score_edge = previous_routing.get("score_edge")
        if not callable(base_score_edge):
            raise PhaseD3RuntimeError(
                "D.3 cannot resolve the pre-C.4 dashboard score_edge authority"
            )

        c4_build_shared = namespace["build_shared_asset_runtime_features"]
        c4_prepare_edge = namespace["prepare_shared_features_for_edge"]
        c4_coverage_edge = namespace["shared_feature_cache_coverage_for_edge"]
        c4_score_edge = namespace["score_edge"]
        c4_status = namespace.get("authoritative_runtime_cache_status")

        def prepare_phase_d3_for_edge(
            *,
            edge: dict[str, Any],
            runtime: Any,
            shared_features_snapshot: pd.DataFrame | None,
            logger: Any,
        ) -> tuple[pd.DataFrame, str]:
            edge_id = str(edge.get("edge_id") or "")
            asset = str(edge.get("asset") or "").upper()
            if edge_id not in _D3_OPTIMIZED_EDGE_IDS:
                return c4_prepare_edge(
                    edge=edge, runtime=runtime,
                    shared_features_snapshot=shared_features_snapshot, logger=logger,
                )
            del logger
            d1_edge = _D1_EDGE_MAP.get(edge_id)
            if not d1_edge:
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: edge absent from D.1: {edge_id}"
                )
            if (
                not isinstance(shared_features_snapshot, pd.DataFrame)
                or shared_features_snapshot.empty
            ):
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: {edge_id} has no C.4 canonical generation"
                )
            if str(
                shared_features_snapshot.attrs.get("phase_c4_asset") or ""
            ).upper() != asset:
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: {edge_id} received another asset generation"
                )

            projected_edge = dict(edge)
            projected_edge["side_value"] = getattr(
                runtime,
                "SIDE_VALUE",
                edge.get("side_value"),
            )
            started = time.perf_counter()
            projected = project_candidate_features(
                ring=shared_features_snapshot,
                edge=projected_edge,
                d1_edge=d1_edge,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _metric_add("candidate_projection_calls", 1)
            _metric_add("candidate_projection_ms_sum", elapsed_ms)
            _record_latency("phase_d3_candidate_projection", elapsed_ms)
            projected.attrs["phase_d3_candidate_contract_sha256"] = str(
                _D1.get("contract_sha256") if _D1 else ""
            )
            return projected, "phase_d3_candidate_first_narrow_projection"

        def coverage_phase_d3(
            *,
            edge: dict[str, Any],
            shared_features_snapshot: pd.DataFrame | None,
            logger: Any,
        ) -> tuple[bool, str, int, list[str]]:
            edge_id = str(edge.get("edge_id") or "")
            if edge_id not in _D3_OPTIMIZED_EDGE_IDS:
                return c4_coverage_edge(
                    edge=edge, shared_features_snapshot=shared_features_snapshot, logger=logger,
                )
            del logger
            d1_edge = _D1_EDGE_MAP.get(edge_id)
            if not d1_edge:
                return False, "edge_absent_from_d1", 1, [edge_id]
            if (
                not isinstance(shared_features_snapshot, pd.DataFrame)
                or shared_features_snapshot.empty
            ):
                return False, "canonical_generation_missing", 1, [
                    "canonical_generation"
                ]
            required = [
                str(x)
                for x in (
                    (d1_edge.get("candidate") or {}).get(
                        "candidate_input_columns_from_certified_projection"
                    )
                    or []
                )
            ]
            missing = [
                name
                for name in required
                if name not in shared_features_snapshot.columns
            ]
            return (
                not missing,
                (
                    "phase_d3_candidate_first_narrow_projection"
                    if not missing
                    else "candidate_projection_missing_columns"
                ),
                len(missing),
                missing[:20],
            )

        def score_edge_phase_d3(
            edge: dict[str, Any],
            manifest: dict[str, Any],
            bar_policy: dict[str, Any],
            state: MutableMapping[str, Any],
            logger: Any,
            **kwargs: Any,
        ) -> list[Any]:
            edge_id = str(edge.get("edge_id") or "")
            asset = str(edge.get("asset") or "").upper()
            if edge_id not in _D3_OPTIMIZED_EDGE_IDS:
                _metric_add("authoritative_c4_edge_calls", 1)
                return c4_score_edge(edge, manifest, bar_policy, state, logger, **kwargs)
            d1_edge = _D1_EDGE_MAP.get(edge_id)
            c3_edge = _C3_EDGE_MAP.get(edge_id)
            if not d1_edge or not c3_edge:
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: missing D.1/C.3 edge authority: {edge_id}"
                )

            canonical_ring = kwargs.get("shared_features_snapshot")
            if not isinstance(canonical_ring, pd.DataFrame) or canonical_ring.empty:
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: {edge_id} has no canonical ring"
                )

            runtime = namespace["get_runtime"](edge)
            original_build = getattr(runtime, "build_features", None)
            original_generate = getattr(runtime, "generate_candidate_events", None)
            original_score = getattr(runtime, "score", None)
            if not all(
                callable(x)
                for x in (original_build, original_generate, original_score)
            ):
                raise PhaseD3RuntimeError(
                    f"phase_d3_candidate_contract: {edge_id} runtime consumer functions missing"
                )
            selected = [
                str(x)
                for x in c3_edge.get("selected_features") or []
            ]
            projected_edge = dict(edge)
            projected_edge["side_value"] = getattr(
                runtime,
                "SIDE_VALUE",
                edge.get("side_value"),
            )

            model_score_called = False

            def forbidden_build_features(*args: Any, **kw: Any) -> Any:
                raise PhaseD3RuntimeError(
                    "phase_d3_candidate_contract: "
                    "normal_path_runtime_build_features_forbidden "
                    f"edge={edge_id}; recovery owner must quarantine/bootstrap"
                )

            def certified_candidate(*args: Any, **kw: Any) -> pd.DataFrame:
                _metric_add("candidate_generation_calls", 1)
                events = original_generate(*args, **kw)
                return _sanitize_generated_candidate_events(
                    events=events,
                    selected_features=selected,
                    edge_id=edge_id,
                )

            def deferred_score(*args: Any, **kw: Any) -> Any:
                nonlocal model_score_called
                model_score_called = True
                _metric_add("score_calls", 1)
                started = time.perf_counter()
                full = project_full_c3_features(
                    ring=canonical_ring,
                    edge=projected_edge,
                    d1_edge=d1_edge,
                    c3_edge=c3_edge,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                _metric_add("deferred_full_projection_calls", 1)
                _metric_add(
                    "deferred_full_projection_ms_sum",
                    elapsed_ms,
                )
                _record_latency(
                    "phase_d3_deferred_full_projection",
                    elapsed_ms,
                )
                new_args, new_kw = _replace_bound_argument(
                    original_score,
                    tuple(args),
                    dict(kw),
                    name="features",
                    value=full,
                )
                return original_score(*new_args, **new_kw)

            with _edge_lock(edge_id):
                runtime.build_features = forbidden_build_features
                runtime.generate_candidate_events = certified_candidate
                runtime.score = deferred_score
                try:
                    rows = base_score_edge(
                        edge,
                        manifest,
                        bar_policy,
                        state,
                        logger,
                        **kwargs,
                    )
                except Exception as exc:
                    _record_failure(edge_id, asset, exc)
                    raise
                finally:
                    runtime.build_features = original_build
                    runtime.generate_candidate_events = original_generate
                    runtime.score = original_score

            if not model_score_called:
                _metric_add("edges_returned_without_model_score", 1)
            return rows

        def phase_d3_authoritative_status() -> dict[str, Any]:
            report = phase_d3_status()
            if callable(c4_status):
                report["underlying_c4"] = c4_status()
            return report

        namespace["build_shared_asset_runtime_features"] = c4_build_shared
        namespace["prepare_shared_features_for_edge"] = prepare_phase_d3_for_edge
        namespace["shared_feature_cache_coverage_for_edge"] = coverage_phase_d3
        namespace["score_edge"] = score_edge_phase_d3
        namespace["authoritative_runtime_cache_status"] = (
            phase_d3_authoritative_status
        )
        namespace["_PHASE_D3_ROUTER_ACTIVE"] = True
        namespace["_PHASE_D3_ROUTER_STATUS"] = phase_d3_status
        namespace["_PHASE_D3_PREVIOUS_ROUTING"] = {
            "prepare_shared_features_for_edge": (
                namespace.get("_PHASE_C4_PREVIOUS_ROUTING") or {}
            ).get("prepare_shared_features_for_edge"),
            "score_edge": base_score_edge,
            "c4_prepare_shared_features_for_edge": c4_prepare_edge,
            "c4_shared_feature_cache_coverage_for_edge": c4_coverage_edge,
            "c4_score_edge": c4_score_edge,
        }

        current_version = str(namespace.get("DASHBOARD_VERSION") or "")
        suffix = "+phase_d3_candidate_first_deferred_model_projection_v1"
        if suffix not in current_version:
            namespace["DASHBOARD_VERSION"] = current_version + suffix
        namespace["HOTPATH_VERSION"] = (
            "phase_d3_candidate_first_deferred_model_projection_v1"
        )

        return {
            "installed": True,
            "activated": True,
            "version": D3_ROUTER_VERSION,
            "dashboard_version": namespace["DASHBOARD_VERSION"],
            "activation_fingerprint": activation.get(
                "activation_fingerprint"
            ),
            "selected_asset_count": report.get("selected_asset_count"),
            "selected_edge_count": report.get("selected_edge_count"),
            "consumer_ring_rows": c4.CERTIFIED_CONSUMER_RING_ROWS,
            "candidate_projection": "phase_d1_d2_narrow",
            "model_projection": "deferred_exact_c3_only_after_candidate",
            "runtime_residency": "existing_dashboard_runtime_cache",
            "normal_path_batch_fallback_allowed": False,
        }


def self_test() -> dict[str, Any]:
    tests: dict[str, bool] = {}

    ring = pd.DataFrame(
        {
            "Datetime": to_datetime_iso(
                [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:01:00Z",
                ]
            ),
            "Bar_Start": to_datetime_iso(
                [
                    "2025-12-31T23:59:00Z",
                    "2026-01-01T00:00:00Z",
                ]
            ),
            "Close": [1.0, 1.1],
            "Close_FFD": [0.1, 0.2],
            "Feature_A": [2.0, 3.0],
            "Feature_B": [4.0, 5.0],
        }
    )
    edge = {"edge_id": "AAA/test_long", "side_value": 1}
    d1_edge = {
        "edge_id": "AAA/test_long",
        "side_value": 1,
        "candidate": {
            "candidate_input_columns_from_certified_projection": [
                "Datetime",
                "Bar_Start",
                "Close",
                "Close_FFD",
            ]
        },
    }
    c3_edge = {
        "edge_id": "AAA/test_long",
        "canonical_projection_columns": [
            "Datetime",
            "Bar_Start",
            "Close",
            "Close_FFD",
            "edge_id",
            "side",
            "Feature_A",
            "Feature_B",
        ],
    }
    narrow = project_candidate_features(
        ring=ring,
        edge=edge,
        d1_edge=d1_edge,
    )
    full = project_full_c3_features(
        ring=ring,
        edge=edge,
        d1_edge=d1_edge,
        c3_edge=c3_edge,
    )

    tests["candidate_projection_is_strictly_narrower"] = (
        len(narrow.columns) < len(full.columns)
    )
    tests["candidate_projection_contains_identity"] = (
        set(narrow["edge_id"]) == {"AAA/test_long"}
        and set(narrow["side"]) == {1}
    )
    tests["full_projection_restores_exact_c3_order"] = (
        list(full.columns)
        == c3_edge["canonical_projection_columns"]
    )

    source = inspect.getsource(install_phase_d3_production_router)
    tests["d3_reuses_existing_runtime_cache"] = (
        'namespace["get_runtime"]' in source
        and "_RUNTIME_CACHE" not in source
    )
    tests["normal_batch_feature_build_forbidden"] = (
        "normal_path_runtime_build_features_forbidden" in source
    )
    tests["full_projection_is_inside_deferred_score"] = (
        "def deferred_score" in source
        and "project_full_c3_features" in source
    )

    failed = [name for name, ok in tests.items() if not ok]
    return {
        "status": "PASS" if not failed else "FAIL",
        "router_version": D3_ROUTER_VERSION,
        "tests": tests,
        "failed": failed,
    }


__all__ = [
    "D3_ROUTER_VERSION",
    "D3_SCHEMA_VERSION",
    "D3_ACTIVATION_SCHEMA_VERSION",
    "D3_CERTIFICATION_SCHEMA_VERSION",
    "D3_ACTIVATION_PATH",
    "D3_CERTIFICATION_PATH",
    "PhaseD3RuntimeError",
    "static_contract_report",
    "build_activation_payload",
    "validate_activation",
    "install_phase_d3_production_router",
    "phase_d3_status",
    "self_test",
]
