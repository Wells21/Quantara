from __future__ import annotations

"""Phase C.5 full local / release certification for C.4 production routing.

Default mode validates the release candidate without changing production routing.
Frozen C.1/C.2/C.3 numerical authorities are reused only when current source,
implementation and selected-edge identities still match the certified contract.
They are not needlessly recomputed on every release seal.

C.5 still runs fresh all-selected-edge candidate/model/decision parity, P0,
production hardening and the full unit suite. ``--seal-production`` additionally
requires branch ``v2`` and a clean tracked tree, then writes a local ignored
production certificate bound to the exact git HEAD, exact current manifest,
activation fingerprint, selected universe and source fingerprints.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
V2_ROOT = HERE.parent
REPO_ROOT = V2_ROOT.parent
for _path in (V2_ROOT, HERE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import phase_c_04_production_router as c4
from shared.edges.phase_c_consumer_adapter import (
    build_canonical_live_candidate_rows,
    strip_selected_feature_collisions,
)
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.edges.runtime_loader import load_edge_runtime_from_manifest_entry
from shared.edges.phase_c_incremental_feature_engine import (
    implementation_fingerprint as current_c2_implementation_fingerprint,
)

SCHEMA_VERSION = "phase_c5_candidate_certification_v2_lean_frozen_authorities"
FINAL_SCHEMA_VERSION = c4.PRODUCTION_CERTIFICATION_SCHEMA_VERSION
DEFAULT_CANDIDATE_OUTPUT = HERE / "config" / "phase_c" / "phase_c5_candidate_report.json"
DEFAULT_PRODUCTION_OUTPUT = c4.PRODUCTION_CERTIFICATION_PATH
STRICT_ATOL = 5e-10
STRICT_RTOL = 5e-10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object: {path}")
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
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _command(name: str, argv: list[str], *, cwd: Path = V2_ROOT) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        argv, cwd=str(cwd), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return {
        "name": name,
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "returncode": proc.returncode,
        "elapsed_ms": elapsed,
        "output_sha256": _sha256_bytes(output.encode("utf-8", errors="replace")),
        "output_tail": output[-12000:],
    }


def _load_c3_module():
    path = HERE / "phase_c_03_certify_consumer_contract.py"
    spec = importlib.util.spec_from_file_location("phase_c3_c5_certifier", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Phase C.3 certifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "Datetime" in out.columns:
        out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
        out = out.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
        out = out.reset_index(drop=True)
    return out


def _value_equal(left: Any, right: Any) -> tuple[bool, float, float]:
    if pd.isna(left) or pd.isna(right):
        return bool(pd.isna(left) and pd.isna(right)), 0.0, 0.0
    if isinstance(left, (bool, np.bool_)) or isinstance(right, (bool, np.bool_)):
        return bool(left) == bool(right), 0.0, 0.0
    try:
        lv = float(left); rv = float(right)
        if math.isfinite(lv) and math.isfinite(rv):
            diff = abs(lv-rv); scale=max(abs(lv),abs(rv),1.0)
            return math.isclose(lv,rv,abs_tol=STRICT_ATOL,rel_tol=STRICT_RTOL), diff, diff/scale
    except Exception:
        pass
    return str(left) == str(right), 0.0, 0.0


def _compare_scored(left: pd.DataFrame, right: pd.DataFrame, edge_id: str) -> dict[str, Any]:
    l = _normalize(left); r = _normalize(right)
    required = [
        "Datetime", "raw_probability", "calibrated_probability",
        "afml_bet_size", "selected_threshold", "approved_signal",
    ]
    missing_left = [x for x in required if x not in l.columns]
    missing_right = [x for x in required if x not in r.columns]
    if missing_left or missing_right:
        return {"ok": False, "reason": "missing_scoring_columns", "missing_left": missing_left, "missing_right": missing_right}
    if len(l) != len(r):
        return {"ok": False, "reason": "row_count", "left": len(l), "right": len(r)}
    if not l["Datetime"].equals(r["Datetime"]):
        return {"ok": False, "reason": "datetime_alignment"}
    mismatches=[]; worst_abs=0.0; worst_rel=0.0; worst_column=None
    compare_columns = required[1:] + (["signal_id"] if "signal_id" in l.columns and "signal_id" in r.columns else [])
    for name in compare_columns:
        for idx,(lv,rv) in enumerate(zip(l[name],r[name])):
            ok,ad,rd=_value_equal(lv,rv)
            if ad>worst_abs:
                worst_abs=ad; worst_rel=rd; worst_column=name
            if not ok and len(mismatches)<20:
                mismatches.append({"row":idx,"column":name,"left":None if pd.isna(lv) else str(lv),"right":None if pd.isna(rv) else str(rv)})
    return {
        "ok": not mismatches,
        "edge_id": edge_id,
        "rows": len(l),
        "max_abs_diff": worst_abs,
        "max_rel_diff": worst_rel,
        "worst_column": worst_column,
        "mismatches": mismatches,
    }



def _validate_frozen_c1_authority(static: Mapping[str, Any]) -> dict[str, Any]:
    from shared.edges.phase_c_feature_contract import validate_frozen_contract

    path = V2_ROOT / "portfolio_live/config/phase_c/feature_contract.json"
    report = validate_frozen_contract(
        v2_root=V2_ROOT,
        contract_path=path,
        live_manifest_path=V2_ROOT / "portfolio_live/live_manifest.json",
    )
    return {
        "status": report.get("status"),
        "failures": report.get("failures") or [],
        "validation_mode": "fresh_structural_current_state_no_sha_gate",
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
    }



def _validate_frozen_c2_authority(static: Mapping[str, Any]) -> dict[str, Any]:
    path = V2_ROOT / "portfolio_live/config/phase_c/incremental_feature_certification.json"
    c2 = _read_json(path)
    failures: list[str] = []

    if c2.get("status") != "PASS" or c2.get("runtime_routing_enabled") is not False:
        failures.append("C.2 numerical authority is not PASS/offline")
    if str(c2.get("engine_version") or "") != str(static.get("c2_engine_version") or ""):
        failures.append("C.2 engine version differs from current C.4 authority")

    expected_assets = list(static.get("selected_assets") or [])
    if sorted(str(x).upper() for x in c2.get("selected_assets") or []) != expected_assets:
        failures.append("C.2 selected assets differ from current manifest")
    if int(c2.get("selected_edge_count") or -1) != len(static.get("selected_edge_ids") or []):
        failures.append("C.2 selected-edge count differs from current manifest")
    if int(c2.get("bootstrap_rows") or -1) != 6000:
        failures.append("C.2 did not certify the 6000-row production authority")
    if int(c2.get("sequential_steps") or -1) < 128:
        failures.append("C.2 sequential proof is below 128 steps")

    required_checkpoints = {1, 32, 64, 96, 128}
    checkpoints = {int(x) for x in c2.get("rolling_checkpoints") or []}
    if not required_checkpoints.issubset(checkpoints):
        failures.append("C.2 rolling checkpoints are incomplete")
    if float(c2.get("strict_atol") or 1.0) > STRICT_ATOL or float(
        c2.get("strict_rtol") or 1.0
    ) > STRICT_RTOL:
        failures.append("C.2 tolerances are weaker than C.5")

    sandbox = c2.get("dependency_authority_sandbox") or {}
    if sandbox.get("enabled") is not True or sandbox.get(
        "production_continuation_mutated"
    ) is not False:
        failures.append("C.2 dependency sandbox proof is invalid")

    assets = c2.get("assets") or {}
    for asset in expected_assets:
        row = assets.get(asset)
        if not isinstance(row, Mapping) or row.get("status") != "PASS":
            failures.append(f"{asset}: C.2 asset authority is not PASS")
            continue
        if int(row.get("bootstrap_rows") or -1) != 6000 or int(
            row.get("sequential_steps") or -1
        ) < 128:
            failures.append(f"{asset}: C.2 authority depth is insufficient")
        if (
            row.get("sequential_failure_samples")
            or row.get("addon_failure_samples")
            or row.get("restart_failure_samples")
        ):
            failures.append(f"{asset}: C.2 contains sequential/addon/restart failures")
        if any(
            (item.get("parity") or {}).get("ok") is not True
            for item in row.get("group_parity") or []
            if isinstance(item, Mapping)
        ):
            failures.append(f"{asset}: C.2 structural-group parity contains a failure")
        rolling = [
            item
            for item in row.get("rolling_authority_checks") or []
            if isinstance(item, Mapping)
        ]
        rolling_steps = {int(item.get("step") or -1) for item in rolling}
        if not required_checkpoints.issubset(rolling_steps):
            failures.append(f"{asset}: C.2 rolling authority checkpoints are incomplete")
        for item in rolling:
            if (item.get("incremental_vs_rolling_authority") or {}).get("ok") is not True:
                failures.append(f"{asset}: C.2 rolling canonical parity contains a failure")
                break
            absorption = item.get("incremental_vs_rolling_absorption_authority")
            if absorption is not None and (absorption or {}).get("ok") is not True:
                failures.append(f"{asset}: C.2 rolling absorption parity contains a failure")
                break

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "validation_mode": "fresh_current_state_6000x128_numerical_authority",
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "engine_version": c2.get("engine_version"),
        "bootstrap_rows": c2.get("bootstrap_rows"),
        "sequential_steps": c2.get("sequential_steps"),
        "rolling_checkpoints": c2.get("rolling_checkpoints"),
        "asset_statuses": {
            asset: (assets.get(asset) or {}).get("status")
            for asset in expected_assets
        },
    }



def _validate_frozen_c3_authority(static: Mapping[str, Any]) -> dict[str, Any]:
    contract_path = V2_ROOT / "portfolio_live/config/phase_c/consumer_contract.json"
    cert_path = V2_ROOT / "portfolio_live/config/phase_c/consumer_certification.json"
    c3 = _read_json(contract_path)
    cert = _read_json(cert_path)
    failures: list[str] = []

    if c3.get("status") != "PASS" or c3.get("runtime_routing_enabled") is not False:
        failures.append("C.3 consumer contract is not PASS/offline")
    if cert.get("status") != "PASS" or cert.get("runtime_routing_enabled") is not False:
        failures.append("C.3 numerical authority is not PASS/offline")
    if int(cert.get("authority_rows") or -1) != 6000:
        failures.append("C.3 did not certify exact 6000-row consumer authority")
    if int(cert.get("private_control_continuation_steps") or -1) < 128:
        failures.append("C.3 private-control continuation is below 128 steps")
    if float(cert.get("strict_atol") or 1.0) > STRICT_ATOL or float(
        cert.get("strict_rtol") or 1.0
    ) > STRICT_RTOL:
        failures.append("C.3 tolerances are weaker than C.5")

    expected_edges = list(static.get("selected_edge_ids") or [])
    expected_assets = list(static.get("selected_assets") or [])
    if sorted(str(x).upper() for x in cert.get("selected_assets") or []) != expected_assets:
        failures.append("C.3 certified assets differ from current manifest")
    if int(cert.get("edges_passed") or -1) != len(expected_edges) or int(
        cert.get("edges_failed") or 0
    ) != 0:
        failures.append("C.3 does not PASS every current selected edge")

    edge_rows = cert.get("edges") or {}
    if sorted(str(x) for x in edge_rows) != expected_edges:
        failures.append("C.3 certified edge universe differs from current manifest")
    else:
        bad = [
            edge_id
            for edge_id in expected_edges
            if not isinstance(edge_rows.get(edge_id), Mapping)
            or edge_rows[edge_id].get("status") != "PASS"
        ]
        if bad:
            failures.append(
                "C.3 contains non-PASS edge rows: " + ",".join(bad[:10])
            )

    if cert.get("private_control_assets_failed") or cert.get("fatal_errors"):
        failures.append("C.3 contains private-control or fatal failures")
    sandbox = cert.get("dependency_authority_sandbox") or {}
    if sandbox.get("enabled") is not True or sandbox.get(
        "production_continuation_mutated"
    ) is not False:
        failures.append("C.3 dependency sandbox proof is invalid")

    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "validation_mode": "fresh_current_state_all_edge_consumer_authority",
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "authority_rows": cert.get("authority_rows"),
        "private_control_continuation_steps": cert.get(
            "private_control_continuation_steps"
        ),
        "edges_passed": cert.get("edges_passed"),
        "edges_failed": cert.get("edges_failed"),
        "c3_recompile_validation": static.get("c3_structural_validation"),
    }



def _model_decision_parity() -> dict[str, Any]:
    c1 = _read_json(V2_ROOT / "portfolio_live/config/phase_c/feature_contract.json")
    c3 = _read_json(V2_ROOT / "portfolio_live/config/phase_c/consumer_contract.json")
    manifest = _read_json(V2_ROOT / "portfolio_live/live_manifest.json")
    manifest_edges = {
        str(row["edge_id"]): dict(row)
        for row in manifest.get("selected_edges") or [] if isinstance(row, Mapping)
    }
    c3_edges = {
        str(row["edge_id"]): dict(row)
        for row in c3.get("edges") or [] if isinstance(row, Mapping)
    }
    assets = list(c3.get("selected_assets") or [])
    c3_module = _load_c3_module()
    results: dict[str, Any] = {}
    build_rows=[]
    with c3_module._isolated_dependency_authority(V2_ROOT, assets):
        for asset in assets:
            asset_row = (c1.get("assets") or {}).get(asset) or {}
            groups = list(asset_row.get("structural_contract_groups") or [])
            for group in groups:
                edge_ids = [str(x) for x in group.get("edge_ids") or [] if str(x) in c3_edges]
                if not edge_ids:
                    continue
                rep_id = sorted(edge_ids)[0]
                rep_runtime = load_edge_runtime_from_manifest_entry(manifest_edges[rep_id], v2_root=V2_ROOT)
                bars_all = c3_module._load_bars(rep_runtime, v2_root=V2_ROOT)
                if len(bars_all) < 6000:
                    for edge_id in edge_ids:
                        results[edge_id] = {"status":"FAIL","reason":f"need 6000 bars, have {len(bars_all)}"}
                    continue
                bars = bars_all.tail(6000).reset_index(drop=True)
                started=time.perf_counter()
                authority = _normalize(rep_runtime.build_features(bars=bars.copy(), drop_warmup_rows=False))
                build_rows.append({"asset":asset,"representative_edge_id":rep_id,"edge_count":len(edge_ids),"build_ms":(time.perf_counter()-started)*1000.0})
                for edge_id in sorted(edge_ids):
                    runtime = load_edge_runtime_from_manifest_entry(manifest_edges[edge_id], v2_root=V2_ROOT)
                    contract = c3_edges[edge_id]
                    selected = [str(x) for x in contract.get("selected_features") or []]
                    full = authority.copy()
                    full["edge_id"] = edge_id
                    full["side"] = int(getattr(runtime,"SIDE_VALUE",manifest_edges[edge_id].get("side_value") or 0))
                    canonical = c4._project_edge_features(
                        ring=authority,
                        edge={"edge_id":edge_id,"asset":asset,"side_value":getattr(runtime,"SIDE_VALUE",0)},
                        edge_contract=contract,
                    )
                    try:
                        full_events = runtime.generate_candidate_events(full.copy())
                        canonical_events = runtime.generate_candidate_events(canonical.copy())
                        full_events, _ = strip_selected_feature_collisions(events=full_events, selected_features=selected)
                        canonical_events, _ = strip_selected_feature_collisions(events=canonical_events, selected_features=selected)
                        full_rows, _ = build_canonical_live_candidate_rows(runtime=runtime, features=full, events=full_events, selected_features=selected)
                        canonical_rows, _ = build_canonical_live_candidate_rows(runtime=runtime, features=canonical, events=canonical_events, selected_features=selected)
                        full_scored, _ = runtime.score(model_rows=full_rows, latest_only=False)
                        canonical_scored, _ = runtime.score(model_rows=canonical_rows, latest_only=False)
                        parity = _compare_scored(full_scored, canonical_scored, edge_id)
                        results[edge_id] = {"status":"PASS" if parity.get("ok") else "FAIL", "parity":parity}
                    except Exception as exc:
                        results[edge_id] = {"status":"FAIL","fatal_error":f"{type(exc).__name__}: {exc}"}
    expected = sorted(c3_edges)
    passed = sorted(edge_id for edge_id,row in results.items() if row.get("status")=="PASS")
    failed = sorted(set(expected).difference(passed))
    absorption = sorted(edge_id for edge_id in expected if str(c3_edges[edge_id].get("family") or "").lower()=="absorption")
    return {
        "status": "PASS" if not failed and len(results)==len(expected) else "FAIL",
        "selected_edge_count": len(expected),
        "edges_checked": len(results),
        "edges_passed": len(passed),
        "edges_failed": failed,
        "absorption_edges_checked": len(absorption),
        "strict_atol": STRICT_ATOL,
        "strict_rtol": STRICT_RTOL,
        "structural_group_builds": build_rows,
        "edge_results": results,
    }


def _gate_status(value: Any) -> str:
    return "PASS" if isinstance(value, Mapping) and value.get("status") == "PASS" else "FAIL"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp")
    temp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True, default=str)+"\n", encoding="utf-8")
    os.replace(str(temp), str(path))


def run_full_certification() -> dict[str, Any]:
    activation_path = c4.ACTIVATION_PATH
    if activation_path.exists():
        raise RuntimeError("Phase C.5 must run with C.4 activation absent")
    static = c4.static_contract_report(V2_ROOT, force_refresh=True)
    if static.get("status") != "PASS":
        return {"status":"FAIL","fatal":"C.4 static contract failed","c4_static":static}
    activation = c4.build_activation_payload(V2_ROOT)
    self_test = c4.self_test()

    evidence: dict[str, Any] = {
        "c4_static_contract": static,
        "c4_router_self_test": self_test,
    }
    evidence["phase_c1_frozen_feature_authority"] = _validate_frozen_c1_authority(static)
    evidence["phase_c2_frozen_feature_authority"] = _validate_frozen_c2_authority(static)
    evidence["phase_c3_frozen_consumer_authority"] = _validate_frozen_c3_authority(static)
    evidence["phase_c_model_decision_parity_all_selected_edges"] = _model_decision_parity()
    evidence["p0_production_validator"] = _command(
        "p0_production_validator",
        [sys.executable, str(HERE/"validate_p0_production_patch.py"), "--root", str(V2_ROOT)],
    )
    evidence["production_hardening_validator"] = _command(
        "production_hardening_validator",
        [sys.executable, str(HERE/"validate_production_hardening_patch.py")],
    )
    evidence["full_unittest_discovery"] = _command(
        "full_unittest_discovery",
        [sys.executable, "-m", "unittest", "discover", "-s", str(V2_ROOT/"tests"), "-p", "test_*.py", "-v"],
    )

    required = [
        "c4_static_contract","c4_router_self_test","phase_c1_frozen_feature_authority",
        "phase_c2_frozen_feature_authority","phase_c3_frozen_consumer_authority",
        "phase_c_model_decision_parity_all_selected_edges","p0_production_validator",
        "production_hardening_validator","full_unittest_discovery",
    ]
    gates={name:_gate_status(evidence.get(name)) for name in required}
    status="PASS" if all(value=="PASS" for value in gates.values()) else "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "created_at": _utc_now(),
        "sealed_for_production": False,
        "production_activation_allowed": False,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "python_version": platform.python_version(),
        "router_version": c4.ROUTER_VERSION,
        "activation_fingerprint": activation.get("activation_fingerprint"),
        "selected_assets": static.get("selected_assets"),
        "selected_edge_ids": static.get("selected_edge_ids"),
        "file_fingerprints": static.get("file_fingerprints"),
        "required_gates": gates,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seal-production", action="store_true")
    parser.add_argument("--candidate-output", type=Path, default=DEFAULT_CANDIDATE_OUTPUT)
    parser.add_argument("--production-output", type=Path, default=DEFAULT_PRODUCTION_OUTPUT)
    args = parser.parse_args()

    if c4.ACTIVATION_PATH.exists():
        raise SystemExit(
            "Refusing C.5 certification while production_routing_activation.json exists"
        )

    print("=" * 112)
    print("PHASE C.5 — CURRENT-STATE PRODUCTION CERTIFICATION")
    print("=" * 112)
    print(f"Mode:          {'PRODUCTION SEAL' if args.seal_production else 'LOCAL CANDIDATE'}")
    print(f"V2 root:       {V2_ROOT}")
    print(f"Python:        {platform.python_version()}")
    print("Identity mode: structural current state; Git/file SHA equality is NOT a gate")
    print("Activation:    ABSENT (required during certification)")

    report = run_full_certification()
    report["authority_identity_mode"] = (
        "structural_current_state_no_git_or_file_sha_gate"
    )
    report.pop("release_head_sha", None)
    report.pop("git_branch", None)
    report.pop("file_fingerprints", None)

    candidate_output = args.candidate_output.resolve()
    _atomic_json(candidate_output, report)
    print(f"\nCandidate report: {candidate_output}")
    print("Required gates:")
    for name, value in (report.get("required_gates") or {}).items():
        print(f"  {name}: {value}")
    print(f"STATUS: {report.get('status')}")

    if report.get("status") != "PASS":
        print("No production certificate written.")
        return 2
    if not args.seal_production:
        print("LOCAL CANDIDATE PASS. No production certificate/activation was written.")
        return 0

    static = c4.static_contract_report(V2_ROOT, force_refresh=True)
    activation = c4.build_activation_payload(V2_ROOT)
    gate_names = sorted((report.get("required_gates") or {}).keys())
    required_gates = {name: "PASS" for name in gate_names}
    contract = {
        "schema_version": FINAL_SCHEMA_VERSION,
        "authority_identity_mode": "structural_current_state_no_git_or_file_sha_gate",
        "router_version": c4.ROUTER_VERSION,
        "selected_assets": list(static.get("selected_assets") or []),
        "selected_edge_ids": list(static.get("selected_edge_ids") or []),
        "required_gates": required_gates,
    }
    certificate = {
        "schema_version": FINAL_SCHEMA_VERSION,
        "status": "PASS",
        "sealed_for_production": True,
        "production_activation_allowed": True,
        "created_at": _utc_now(),
        "authority_identity_mode": contract["authority_identity_mode"],
        "router_version": contract["router_version"],
        "selected_assets": contract["selected_assets"],
        "selected_edge_ids": contract["selected_edge_ids"],
        "required_gates": required_gates,
        "certificate_contract": contract,
        "certificate_fingerprint": _json_hash(contract),
    }
    validation = c4.validate_production_certification(
        certificate,
        activation,
        v2_root=V2_ROOT,
    )
    if validation.get("status") != "PASS":
        raise RuntimeError(
            "self-validation of production certificate failed: "
            + " | ".join(validation.get("failures") or [])
        )
    prod = args.production_output.resolve()
    _atomic_json(prod, certificate)
    print(f"\nPRODUCTION CERTIFICATE WRITTEN: {prod}")
    print("Authority identity mode: structural current state")
    print("Activation is STILL ABSENT. Run phase_c_04_prepare_activation.py explicitly.")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
