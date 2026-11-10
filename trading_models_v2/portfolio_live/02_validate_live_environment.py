from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.contracts import REQUIRED_EDGE_RUNTIME_FUNCTIONS  # noqa: E402
from shared.edges.runtime_loader import EdgeRuntimeLoadError, load_edge_runtime_from_manifest_entry  # noqa: E402
from shared.io.artifact_hash import sha256_file  # noqa: E402
from shared.prop_account_rules import (  # noqa: E402
    active_rule_contract,
    load_active_account_rules,
    structurally_equal,
)
from shared.validation.edge_contracts import validate_runtime_required_api  # noqa: E402
import live_settings as settings  # type: ignore  # noqa: E402

SCHEMA_VERSION = "v2.3"
LIVE_MANIFEST_PATH = V2_ROOT / "portfolio_live" / "live_manifest.json"
LIVE_MANIFEST_VALIDATION_REPORT = V2_ROOT / "portfolio_live" / "live_manifest_validation_report.json"
REPORT_PATH = V2_ROOT / "portfolio_live" / "live_environment_validation_report.json"
LOCAL_SETTINGS_PATH = V2_ROOT / "portfolio_live" / "local_live_settings.json"
OUTPUT_DIRS = [
    V2_ROOT / "portfolio_live" / "outputs",
    V2_ROOT / "portfolio_live" / "live_bars",
    V2_ROOT / "portfolio_live" / "live_logs",
    V2_ROOT / "portfolio_live" / "live_state",
    V2_ROOT / "portfolio_live" / "config",
]
NON_LIVE_STATUSES = {
    "BOOTSTRAP_NOT_LIVE_APPROVED",
    "LIVE_READY_NOT_ARMED",
    "LIVE_REVIEW_REQUIRED_NOT_ARMED",
    "DRY_RUN_ONLY_STRESS_PASS_NOT_ARMED",
    "DRY_RUN_ONLY_STRESS_REVIEW_REQUIRED",
}
LIVE_APPROVED_STATUS = "LIVE_APPROVED"
DEFAULT_SYMBOL_MAP = dict(getattr(settings, "MT5_SYMBOL_MAP", {}))
ACCOUNT_RULES = load_active_account_rules()
ACCOUNT_RULE_CONTRACT = active_rule_contract(ACCOUNT_RULES)
EXPECTED_ACCOUNT_STAGE = str(ACCOUNT_RULES["account_stage"])
EXPECTED_ACCOUNT_PROFILE = str(ACCOUNT_RULES["profile_label"])
EXPECTED_SYMBOL_ENABLED = bool(ACCOUNT_RULES["daily_symbol_risk"]["enabled"])
EXPECTED_SYMBOL_MODE = str(ACCOUNT_RULES["daily_symbol_risk"]["enforcement_mode"])
EXPECTED_RISK_CONTRACT = "direct_weight_calibrated_afml_broker_executable"
HASH_VALIDATION_ENABLED = bool(getattr(settings, "LIVE_ARTIFACT_HASH_VALIDATION_ENABLED", False))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    return path.resolve().relative_to(V2_ROOT.resolve()).as_posix()


def add(rows: list[dict[str, Any]], severity: str, code: str, message: str, **extra: Any) -> None:
    rows.append({"severity": severity, "code": code, "message": message, **extra})


def load_local_settings() -> dict[str, Any]:
    if LOCAL_SETTINGS_PATH.exists():
        return load_json(LOCAL_SETTINGS_PATH)
    return {}


def symbol_map_from_settings(local_settings: dict[str, Any]) -> dict[str, str]:
    raw = local_settings.get("mt5_symbol_map", {})
    if not isinstance(raw, dict):
        raw = {}
    out = dict(DEFAULT_SYMBOL_MAP)
    for asset, symbol in raw.items():
        if str(asset).strip() and str(symbol).strip():
            out[str(asset).strip()] = str(symbol).strip()
    return out


def execution_guards_from_settings(local_settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {
        str(asset): dict(cfg)
        for asset, cfg in dict(getattr(settings, "PORTFOLIO_EXECUTION_GUARDS_BY_ASSET", {})).items()
        if isinstance(cfg, dict)
    }
    raw = local_settings.get("portfolio_execution_guards_by_asset", {})
    if isinstance(raw, dict):
        for asset, cfg in raw.items():
            if isinstance(cfg, dict):
                out[str(asset)] = dict(cfg)
    return out


def validate_compute_and_guard_contracts(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    local_settings: dict[str, Any],
) -> None:
    profile = str(local_settings.get("portfolio_compute_profile", getattr(settings, "PORTFOLIO_COMPUTE_PROFILE", "local_safe"))).strip().lower()
    add(rows, "PASS" if profile in {"local_safe", "vps_parallel", "custom"} else "FAIL", "ENV_COMPUTE_PROFILE", f"portfolio_compute_profile={profile}")
    runtime_contract = manifest.get("live_runtime_contract") or {}
    runtime_ok = (
        runtime_contract.get("runtime_mode") == "authoritative_batch_shared_cache"
        and runtime_contract.get("native_one_row_certification_required") is False
        and runtime_contract.get("native_one_row_continuation_allowed") is False
        and runtime_contract.get("stateful_incremental_continuation_allowed") is False
    )
    add(rows, "PASS" if runtime_ok else "FAIL", "ENV_AUTHORITATIVE_RUNTIME_MODE", f"live_runtime_contract={runtime_contract}")
    assets = sorted({str(edge.get("asset")) for edge in manifest.get("selected_edges", []) if edge.get("asset")})
    guards = execution_guards_from_settings(local_settings)
    required_fields = ("max_spread_points", "entry_drift_limit_price", "deviation_points", "max_lot_cap")
    for asset in assets:
        cfg = guards.get(asset)
        missing = [name for name in required_fields if not isinstance(cfg, dict) or cfg.get(name) is None]
        numeric_ok = not missing
        if numeric_ok:
            try:
                numeric_ok = (
                    float(cfg["max_spread_points"]) > 0
                    and float(cfg["entry_drift_limit_price"]) > 0
                    and int(cfg["deviation_points"]) >= 0
                    and float(cfg["max_lot_cap"]) > 0
                )
            except Exception:
                numeric_ok = False
        add(rows, "PASS" if numeric_ok else "FAIL", "ENV_ASSET_EXECUTION_GUARD", f"asset={asset} missing={missing} guard={cfg}", asset=asset)


def validate_live_manifest_report(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    if not LIVE_MANIFEST_VALIDATION_REPORT.exists():
        add(rows, "FAIL", "LIVE_MANIFEST_VALIDATION_REPORT_MISSING", "Run python ./portfolio_live/01_validate_live_manifest.py before environment validation.")
        return
    try:
        report = load_json(LIVE_MANIFEST_VALIDATION_REPORT)
        status = report.get("validation_status")
        add(rows, "PASS" if status == "PASS" else "FAIL", "LIVE_MANIFEST_VALIDATION_STATUS", f"01_validate_live_manifest status={status}")
        if HASH_VALIDATION_ENABLED:
            current_hash = sha256_file(LIVE_MANIFEST_PATH)
            add(rows, "PASS" if report.get("live_manifest_sha256") == current_hash else "FAIL", "LIVE_MANIFEST_VALIDATION_REPORT_CURRENT", f"report_hash={report.get('live_manifest_sha256')} current_hash={current_hash}")
        else:
            report_edges = sorted(str(x) for x in (report.get("selected_edge_ids") or []))
            manifest_edges = sorted(str(edge.get("edge_id")) for edge in (manifest.get("selected_edges") or []) if edge.get("edge_id"))
            current = report.get("manifest_created_at") == manifest.get("created_at") and report_edges == manifest_edges
            add(rows, "PASS" if current else "FAIL", "LIVE_MANIFEST_VALIDATION_REPORT_CURRENT", f"structural_current={current} created_at={manifest.get('created_at')} edges={len(manifest_edges)}")
        add(rows, "PASS" if report.get("approved_account_stage") == manifest.get("approved_account_stage") else "FAIL", "LIVE_MANIFEST_VALIDATION_STAGE_MATCH", f"report={report.get('approved_account_stage')} manifest={manifest.get('approved_account_stage')}")
    except Exception as exc:
        add(rows, "FAIL", "LIVE_MANIFEST_VALIDATION_REPORT_READ_FAILED", str(exc))


def validate_account_stage(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    local_settings: dict[str, Any],
) -> None:
    local_stage = str(
        local_settings.get(
            "portfolio_account_stage",
            getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", ""),
        )
        or ""
    ).strip().lower()
    manifest_stage = str(
        manifest.get("approved_account_stage")
        or manifest.get("account_stage")
        or ""
    ).strip().lower()
    manifest_profile = str(manifest.get("account_rule_profile") or "").strip()

    add(
        rows,
        "PASS" if not local_stage or local_stage == EXPECTED_ACCOUNT_STAGE else "FAIL",
        "LOCAL_ACCOUNT_STAGE",
        f"local_stage={local_stage!r} expected={EXPECTED_ACCOUNT_STAGE!r}",
    )
    add(
        rows,
        "PASS" if manifest_stage == EXPECTED_ACCOUNT_STAGE else "FAIL",
        "MANIFEST_ACCOUNT_STAGE",
        f"manifest_stage={manifest_stage!r}",
    )
    add(
        rows,
        "PASS" if not local_stage or local_stage == manifest_stage else "FAIL",
        "ACCOUNT_STAGE_MATCH",
        f"local={local_stage!r} manifest={manifest_stage!r}",
    )
    add(
        rows,
        "PASS" if manifest_profile == EXPECTED_ACCOUNT_PROFILE else "FAIL",
        "ACCOUNT_PROFILE_MATCH",
        f"manifest_profile={manifest_profile!r}",
    )
    add(
        rows,
        "PASS"
        if structurally_equal(manifest.get("active_account_rules"), ACCOUNT_RULE_CONTRACT)
        else "FAIL",
        "ACTIVE_ACCOUNT_RULES_MATCH",
        "live manifest structurally matches active_account_rules.json",
    )
    add(
        rows,
        "PASS" if manifest.get("risk_contract_version") == EXPECTED_RISK_CONTRACT else "FAIL",
        "ENV_RISK_CONTRACT",
        f"risk_contract_version={manifest.get('risk_contract_version')}",
    )
    add(
        rows,
        "PASS"
        if bool(manifest.get("daily_symbol_risk_rule_enabled", False))
        == EXPECTED_SYMBOL_ENABLED
        else "FAIL",
        "ENV_DAILY_SYMBOL_RULE_ENABLED",
        f"daily_symbol_risk_rule_enabled={manifest.get('daily_symbol_risk_rule_enabled')!r}",
    )
    add(
        rows,
        "PASS"
        if str(manifest.get("daily_symbol_risk_enforcement_mode"))
        == EXPECTED_SYMBOL_MODE
        else "FAIL",
        "ENV_DAILY_SYMBOL_RULE_MODE",
        f"daily_symbol_risk_enforcement_mode={manifest.get('daily_symbol_risk_enforcement_mode')!r}",
    )


def validate_manifest_status(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    status = str(manifest.get("deployment_status", ""))
    live_ready = manifest.get("live_ready")
    live_trade_enabled = manifest.get("live_trade_enabled")
    stress_status = manifest.get("stress_validation_status")
    if status in NON_LIVE_STATUSES:
        add(rows, "PASS", "ENV_NON_LIVE_DEPLOYMENT_STATUS", f"deployment_status={status}")
        add(rows, "PASS" if live_ready is False else "FAIL", "ENV_NON_LIVE_NOT_READY", f"live_ready={live_ready}")
        add(rows, "PASS" if live_trade_enabled is False else "FAIL", "ENV_NON_LIVE_TRADING_DISABLED", f"live_trade_enabled={live_trade_enabled}")
        if stress_status != "STRESS_PASS":
            add(rows, "PASS", "ENV_STRESS_REVIEW_REMAINS_NOT_ARMED", f"stress_validation_status={stress_status}")
    elif status == LIVE_APPROVED_STATUS:
        add(rows, "PASS" if stress_status == "STRESS_PASS" else "FAIL", "ENV_LIVE_APPROVED_REQUIRES_STRESS_PASS", f"stress_validation_status={stress_status}")
        add(rows, "PASS" if live_ready is True else "FAIL", "ENV_LIVE_READY", f"live_ready={live_ready}")
        add(rows, "PASS" if live_trade_enabled is True else "FAIL", "ENV_LIVE_TRADING_ENABLED", f"live_trade_enabled={live_trade_enabled}")
    else:
        add(rows, "FAIL", "ENV_UNKNOWN_DEPLOYMENT_STATUS", f"deployment_status={status}")


def ensure_output_dirs(rows: list[dict[str, Any]], write_dirs: bool) -> None:
    for path in OUTPUT_DIRS:
        try:
            if write_dirs:
                path.mkdir(parents=True, exist_ok=True)
            ok = path.exists() and path.is_dir()
            add(rows, "PASS" if ok else "FAIL", "ENV_OUTPUT_DIR", f"{rel(path)} exists={ok}", path=rel(path))
        except Exception as exc:
            add(rows, "FAIL", "ENV_OUTPUT_DIR_CREATE_FAILED", f"{rel(path)}: {exc}", path=rel(path))


def check_hash_record(rows: list[dict[str, Any]], edge_id: str, path_text: str, expected_sha: str, code: str) -> None:
    path = V2_ROOT / str(path_text)
    if not path.exists():
        add(rows, "FAIL", code, f"{edge_id}: missing file {path_text}", edge_id=edge_id)
        return
    actual = sha256_file(path)
    if actual != expected_sha:
        add(rows, "FAIL", code, f"{edge_id}: hash mismatch for {path_text}: expected {expected_sha}, got {actual}", edge_id=edge_id)
    else:
        add(rows, "PASS", code, f"{edge_id}: hash verified for {path_text}", edge_id=edge_id)


def validate_selected_edge(rows: list[dict[str, Any]], edge: dict[str, Any], symbol_map: dict[str, str]) -> None:
    edge_id = str(edge.get("edge_id", ""))
    asset = str(edge.get("asset", ""))
    runtime_module = str(edge.get("runtime_module", ""))
    edge_root_text = str(edge.get("edge_root", ""))
    edge_root = V2_ROOT / edge_root_text if edge_root_text else None

    add(rows, "PASS" if edge_id else "FAIL", "ENV_EDGE_ID", f"edge_id={edge_id}", edge_id=edge_id)
    add(rows, "PASS" if asset else "FAIL", "ENV_EDGE_ASSET", f"{edge_id}: asset={asset}", edge_id=edge_id)
    add(rows, "PASS" if asset in symbol_map and bool(symbol_map.get(asset)) else "FAIL", "ENV_ASSET_SYMBOL_MAP", f"{edge_id}: asset={asset} symbol={symbol_map.get(asset)}", edge_id=edge_id)

    if edge_root is None:
        add(rows, "FAIL", "ENV_EDGE_ROOT_MISSING", f"{edge_id}: edge_root missing", edge_id=edge_id)
    else:
        add(rows, "PASS" if edge_root.exists() else "FAIL", "ENV_EDGE_ROOT_EXISTS", f"{edge_id}: edge_root={edge_root_text} exists={edge_root.exists()}", edge_id=edge_id)

    edge_manifest_path = str(edge.get("edge_manifest_path", ""))
    edge_manifest_sha = str(edge.get("edge_manifest_hash", ""))
    if not HASH_VALIDATION_ENABLED:
        path = V2_ROOT / edge_manifest_path if edge_manifest_path else None
        ok = bool(path and path.exists())
        add(rows, "PASS" if ok else "FAIL", "ENV_EDGE_MANIFEST_PATH", f"{edge_id}: path={edge_manifest_path!r} exists={ok}", edge_id=edge_id)
        add(rows, "PASS", "ENV_EDGE_ARTIFACT_DIGESTS_NOT_REQUIRED", f"{edge_id}: structural-current-files mode", edge_id=edge_id)
    elif edge_manifest_path and edge_manifest_sha:
        check_hash_record(rows, edge_id, edge_manifest_path, edge_manifest_sha, "ENV_EDGE_MANIFEST_HASH")
    else:
        add(rows, "FAIL", "ENV_EDGE_MANIFEST_HASH_MISSING", f"{edge_id}: edge_manifest_path/hash missing", edge_id=edge_id)

    artifact_hashes = edge.get("artifact_hashes", {})
    if HASH_VALIDATION_ENABLED:
        add(rows, "PASS" if isinstance(artifact_hashes, dict) and artifact_hashes else "FAIL", "ENV_EDGE_ARTIFACT_HASHES_PRESENT", f"{edge_id}: artifact hash groups={len(artifact_hashes) if isinstance(artifact_hashes, dict) else 'invalid'}", edge_id=edge_id)

    try:
        module = load_edge_runtime_from_manifest_entry(edge, v2_root=V2_ROOT)
        add(rows, "PASS", "ENV_RUNTIME_IMPORT", f"{edge_id}: isolated runtime loaded from {edge_root_text or runtime_module}", edge_id=edge_id)
        missing = [name for name in REQUIRED_EDGE_RUNTIME_FUNCTIONS if not callable(getattr(module, name, None))]
        add(rows, "PASS" if not missing else "FAIL", "ENV_RUNTIME_REQUIRED_API", f"{edge_id}: missing_runtime_functions={missing}", edge_id=edge_id)
    except EdgeRuntimeLoadError as exc:
        add(rows, "FAIL", "ENV_RUNTIME_IMPORT", f"{edge_id}: failed isolated runtime load: {exc}", edge_id=edge_id)
    except Exception as exc:
        add(rows, "FAIL", "ENV_RUNTIME_IMPORT", f"{edge_id}: runtime validation failed: {exc}", edge_id=edge_id)

    if edge_root is not None and edge_root.exists():
        try:
            validate_runtime_required_api(edge_root, import_runtime=True)
            add(rows, "PASS", "ENV_RUNTIME_CONTRACT_VALIDATOR", f"{edge_id}: shared isolated runtime required API validator passed", edge_id=edge_id)
        except Exception as exc:
            add(rows, "FAIL", "ENV_RUNTIME_CONTRACT_VALIDATOR", f"{edge_id}: {exc}", edge_id=edge_id)

    add(rows, "PASS" if edge.get("dashboard_may_engineer_edge_features") is False else "FAIL", "ENV_DASHBOARD_FEATURE_ENGINEERING_FORBIDDEN", f"{edge_id}: dashboard_may_engineer_edge_features={edge.get('dashboard_may_engineer_edge_features')}", edge_id=edge_id)

    runtime_contract = edge.get("dashboard_runtime_contract", {})
    required_flags = ["must_import_runtime_module", "must_call_build_features", "must_call_generate_candidate_events", "must_call_build_model_rows", "must_call_score"]
    missing_flags = [flag for flag in required_flags if runtime_contract.get(flag) is not True]
    add(rows, "PASS" if not missing_flags else "FAIL", "ENV_DASHBOARD_RUNTIME_FLAGS", f"{edge_id}: missing_flags={missing_flags}", edge_id=edge_id)
    authoritative_ok = (
        runtime_contract.get("runtime_mode") == "authoritative_batch_shared_cache"
        and runtime_contract.get("native_one_row_continuation_allowed") is False
        and runtime_contract.get("stateful_incremental_continuation_allowed") is False
    )
    add(rows, "PASS" if authoritative_ok else "FAIL", "ENV_EDGE_AUTHORITATIVE_RUNTIME", f"{edge_id}: runtime_contract={runtime_contract}", edge_id=edge_id)


def validate_mt5_import(rows: list[dict[str, Any]]) -> Any | None:
    try:
        mt5 = importlib.import_module("MetaTrader5")
        add(rows, "PASS", "ENV_MT5_IMPORT", "MetaTrader5 package import passed")
        return mt5
    except Exception as exc:
        add(rows, "WARN", "ENV_MT5_IMPORT", f"MetaTrader5 package import failed. Broker-connected checks skipped unless --require-mt5 is used: {exc}")
        return None


def validate_mt5_connection(rows: list[dict[str, Any]], mt5: Any | None, manifest: dict[str, Any], symbol_map: dict[str, str], local_settings: dict[str, Any], require_mt5: bool) -> None:
    if mt5 is None:
        if require_mt5:
            add(rows, "FAIL", "ENV_MT5_REQUIRED_BUT_UNAVAILABLE", "--require-mt5 was passed but MetaTrader5 could not be imported")
        return
    if not require_mt5:
        add(rows, "WARN", "ENV_MT5_CONNECTION_SKIPPED", "MT5 connection checks skipped. Use --require-mt5 when validating against broker terminal.")
        return

    login = local_settings.get("mt5_login")
    password = local_settings.get("mt5_password")
    server = local_settings.get("mt5_server")
    path = local_settings.get("mt5_path")
    timeout_ms = int(local_settings.get("mt5_timeout_ms", 60000))
    init_kwargs: dict[str, Any] = {"timeout": timeout_ms}
    if path:
        init_kwargs["path"] = str(path)
    if login is not None:
        init_kwargs["login"] = int(login)
    if password:
        init_kwargs["password"] = str(password)
    if server:
        init_kwargs["server"] = str(server)

    try:
        ok = bool(mt5.initialize(**init_kwargs))
        if not ok:
            add(rows, "FAIL", "ENV_MT5_INITIALIZE", f"MT5 initialize failed: {mt5.last_error()}")
            return
        add(rows, "PASS", "ENV_MT5_INITIALIZE", "MT5 initialize passed")
        account = mt5.account_info()
        add(rows, "PASS" if account is not None else "FAIL", "ENV_MT5_ACCOUNT_INFO", "account_info present" if account is not None else "account_info unavailable")
        if account is None:
            return

        expected_login = local_settings.get("portfolio_expected_account_login", getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_LOGIN", None))
        expected_server = str(local_settings.get("portfolio_expected_account_server", getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_SERVER", "")) or "").strip()
        expected_currency = str(local_settings.get("portfolio_expected_account_currency", getattr(settings, "PORTFOLIO_EXPECTED_ACCOUNT_CURRENCY", "USD")) or "").strip().upper()
        actual_login = int(getattr(account, "login", 0) or 0)
        actual_server = str(getattr(account, "server", "") or "").strip()
        actual_currency = str(getattr(account, "currency", "") or "").strip().upper()
        add(rows, "PASS" if expected_login is None or actual_login == int(expected_login) else "FAIL", "ENV_EXPECTED_ACCOUNT_LOGIN", f"expected={expected_login} actual={actual_login}")
        add(rows, "PASS" if not expected_server or actual_server == expected_server else "FAIL", "ENV_EXPECTED_ACCOUNT_SERVER", f"expected={expected_server!r} actual={actual_server!r}")
        add(rows, "PASS" if not expected_currency or actual_currency == expected_currency else "FAIL", "ENV_EXPECTED_ACCOUNT_CURRENCY", f"expected={expected_currency!r} actual={actual_currency!r}")

        selected_edges = manifest.get("selected_edges", []) if isinstance(manifest.get("selected_edges"), list) else []
        assets = sorted({str(edge.get("asset")) for edge in selected_edges if edge.get("asset")})
        for asset in assets:
            symbol = symbol_map.get(asset, asset)
            selected = bool(mt5.symbol_select(symbol, True))
            info = mt5.symbol_info(symbol)
            add(rows, "PASS" if selected and info is not None else "FAIL", "ENV_MT5_SYMBOL_SELECT", f"asset={asset} symbol={symbol} selected={selected} info_present={info is not None}", asset=asset, symbol=symbol)
            if info is None:
                continue
            required_attrs = ["point", "digits", "volume_min", "volume_step", "volume_max", "trade_stops_level", "filling_mode", "trade_contract_size", "currency_profit"]
            missing_attrs = [name for name in required_attrs if getattr(info, name, None) is None]
            add(rows, "PASS" if not missing_attrs else "FAIL", "ENV_MT5_SYMBOL_INFO_FIELDS", f"asset={asset} symbol={symbol} missing={missing_attrs}", asset=asset, symbol=symbol)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                add(rows, "FAIL", "ENV_MT5_SYMBOL_TICK", f"asset={asset} symbol={symbol} symbol_info_tick unavailable", asset=asset, symbol=symbol)
                continue
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            point = float(getattr(info, "point", 0.0) or 0.0)
            stops = int(getattr(info, "trade_stops_level", 0) or 0)
            volume = float(getattr(info, "volume_min", 0.0) or 0.0)
            distance_points = max(stops + 10, 100)
            stop = ask - distance_points * point
            calc = None
            if ask > 0 and point > 0 and volume > 0 and stop > 0:
                calc = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, volume, ask, stop)
            calc_ok = calc is not None and math.isfinite(float(calc)) and float(calc) < 0
            add(
                rows,
                "PASS" if calc_ok else "FAIL",
                "ENV_MT5_ACCOUNT_CURRENCY_RISK_CALC",
                f"asset={asset} symbol={symbol} volume={volume} entry={ask} stop={stop} profit_at_stop={calc} last_error={mt5.last_error() if calc is None else None}",
                asset=asset,
                symbol=symbol,
                contract_size=float(getattr(info, "trade_contract_size", 0.0) or 0.0),
                profit_currency=str(getattr(info, "currency_profit", "") or ""),
                account_currency=actual_currency,
                volume_min=volume,
                volume_step=float(getattr(info, "volume_step", 0.0) or 0.0),
            )
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def validate_live_environment(write_report: bool = True, require_mt5: bool = False, write_dirs: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    try:
        manifest = load_json(LIVE_MANIFEST_PATH)
        add(rows, "PASS", "ENV_LIVE_MANIFEST_LOAD", f"Loaded {rel(LIVE_MANIFEST_PATH)}")
    except Exception as exc:
        report = {"schema_version": SCHEMA_VERSION, "created_at": now_utc(), "validation_status": "FAIL", "findings": [{"severity": "FAIL", "code": "ENV_LIVE_MANIFEST_LOAD", "message": str(exc)}]}
        if write_report:
            write_json(REPORT_PATH, report)
        return report

    validate_live_manifest_report(rows, manifest)
    validate_manifest_status(rows, manifest)
    ensure_output_dirs(rows, write_dirs=write_dirs)
    local_settings = load_local_settings()
    validate_account_stage(rows, manifest, local_settings)
    validate_compute_and_guard_contracts(rows, manifest, local_settings)
    if LOCAL_SETTINGS_PATH.exists():
        add(rows, "PASS", "ENV_LOCAL_SETTINGS", f"Loaded local settings from {rel(LOCAL_SETTINGS_PATH)}")
    else:
        add(rows, "WARN", "ENV_LOCAL_SETTINGS", "local_live_settings.json not found. Safe defaults are active; broker symbol map may need local override.")
    symbol_map = symbol_map_from_settings(local_settings)

    selected_edges = manifest.get("selected_edges", [])
    if not isinstance(selected_edges, list) or not selected_edges:
        add(rows, "FAIL", "ENV_SELECTED_EDGES", "live_manifest.selected_edges must be non-empty")
        selected_edges = []
    else:
        add(rows, "PASS", "ENV_SELECTED_EDGES", f"selected_edges={len(selected_edges)}")
    for edge in selected_edges:
        validate_selected_edge(rows, edge, symbol_map)

    live_execution = manifest.get("live_execution_contract", {})
    if manifest.get("live_trade_enabled") is False:
        add(rows, "PASS" if live_execution.get("orders_allowed") is False else "FAIL", "ENV_ORDER_SEND_NOT_ARMED_LOCK", f"orders_allowed={live_execution.get('orders_allowed')}")
    if os.environ.get("PORTFOLIO_ENABLE_ORDER_SEND", "").strip().lower() in {"1", "true", "yes"} and manifest.get("live_trade_enabled") is False:
        add(rows, "FAIL", "ENV_ORDER_SEND_ENV_CONFLICT", "PORTFOLIO_ENABLE_ORDER_SEND is true while live_manifest.live_trade_enabled=false")
    else:
        add(rows, "PASS", "ENV_ORDER_SEND_ENV_SAFE", "No environment override attempts to arm order_send against a non-live manifest")

    mt5 = validate_mt5_import(rows)
    validate_mt5_connection(rows, mt5, manifest, symbol_map, local_settings, require_mt5=require_mt5)

    fail_count = sum(1 for row in rows if row["severity"] == "FAIL")
    warn_count = sum(1 for row in rows if row["severity"] == "WARN")
    pass_count = sum(1 for row in rows if row["severity"] == "PASS")
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "validation_status": "FAIL" if fail_count else "PASS",
        "require_mt5": require_mt5,
        "manifest_path": rel(LIVE_MANIFEST_PATH),
        "live_manifest_sha256": sha256_file(LIVE_MANIFEST_PATH),  # audit only
        "manifest_created_at": manifest.get("created_at"),
        "selected_edge_ids": sorted(str(edge.get("edge_id")) for edge in selected_edges if edge.get("edge_id")),
        "artifact_hash_validation_enabled": HASH_VALIDATION_ENABLED,
        "approved_account_stage": manifest.get("approved_account_stage"),
        "configured_account_stage": str(getattr(settings, "PORTFOLIO_ACCOUNT_STAGE", "")).lower(),
        "risk_contract_version": manifest.get("risk_contract_version"),
        "deployment_status": manifest.get("deployment_status"),
        "live_ready": manifest.get("live_ready"),
        "live_trade_enabled": manifest.get("live_trade_enabled"),
        "selected_edge_count": len(selected_edges),
        "selected_asset_count": len({str(edge.get("asset")) for edge in selected_edges if edge.get("asset")}),
        "compute_profile": str(local_settings.get("portfolio_compute_profile", getattr(settings, "PORTFOLIO_COMPUTE_PROFILE", "local_safe"))),
        "runtime_mode": (manifest.get("live_runtime_contract") or {}).get("runtime_mode"),
        "symbol_map_used": {str(edge.get("asset")): symbol_map.get(str(edge.get("asset"))) for edge in selected_edges if edge.get("asset")},
        "summary": {"pass_count": pass_count, "warn_count": warn_count, "fail_count": fail_count},
        "findings": rows,
        "next_ordered_step_if_pass": "python ./portfolio_live/03_live_bar_threshold_diagnostic.py",
        "notes": [
            "This step validates environment readiness only. It does not build bars, features, scores, signals, or orders.",
            "Use --require-mt5 for broker-connected validation.",
            "Real credentials must not be committed. Use portfolio_live/local_live_settings.json locally.",
        ],
    }
    if write_report:
        write_json(REPORT_PATH, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-mt5", action="store_true", help="Require MetaTrader5 import/initialize/symbol checks against a local broker terminal.")
    parser.add_argument("--no-write-dirs", action="store_true", help="Do not create output directories during validation.")
    args = parser.parse_args()
    report = validate_live_environment(write_report=not args.dry_run, require_mt5=args.require_mt5, write_dirs=not args.no_write_dirs)
    summary = report.get("summary", {})
    print("V2 Portfolio Live 02 — validate live environment")
    print(f"Status: {report['validation_status']}")
    print(f"Require MT5: {report.get('require_mt5')}")
    print(f"Deployment status: {report.get('deployment_status')}")
    print(f"Live ready: {report.get('live_ready')}")
    print(f"Live trade enabled: {report.get('live_trade_enabled')}")
    print(f"Selected edges: {report.get('selected_edge_count')}")
    print(f"Pass: {summary.get('pass_count', 0)} | Warn: {summary.get('warn_count', 0)} | Fail: {summary.get('fail_count', 0)}")
    for row in report.get("findings", []):
        if row["severity"] != "PASS":
            print(f"{row['severity']} {row['code']}: {row['message']}")
    if report["validation_status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
