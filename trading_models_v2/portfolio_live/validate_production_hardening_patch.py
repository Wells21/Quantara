from __future__ import annotations

"""Offline validation for the August 2026 V2 production-hardening patch.

The checks use temporary files and fake MT5 records only. They never connect to
MT5, send orders, modify live state, or touch research artifacts.
"""

import builtins
import csv
import importlib.util
import json
import os
import subprocess
import sys
import symtable
import tempfile
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
V2_ROOT = THIS_DIR.parent
REPO_ROOT = V2_ROOT.parent

# Match the production launchers' import bootstrap so this standalone validator
# works from the repository root, trading_models_v2, portfolio_live, or any
# other current working directory.
for _path in (REPO_ROOT, V2_ROOT, THIS_DIR):
    _value = str(_path)
    if _value not in sys.path:
        sys.path.insert(0, _value)
del _path, _value


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _test_import_path_bootstrap() -> dict[str, Any]:
    expected_paths = [THIS_DIR, V2_ROOT, REPO_ROOT]
    missing = [str(path) for path in expected_paths if str(path) not in sys.path]
    _assert(not missing, f"Validator import paths are missing: {missing}")

    from shared.live_streaming import state as hot_state

    state_path = Path(hot_state.__file__).resolve()
    expected_shared_root = (V2_ROOT / "shared").resolve()
    _assert(
        expected_shared_root in state_path.parents,
        f"shared resolved outside trading_models_v2: {state_path}",
    )
    return {
        "working_directory": str(Path.cwd()),
        "portfolio_live_path": str(THIS_DIR),
        "v2_root": str(V2_ROOT),
        "repository_root": str(REPO_ROOT),
        "shared_state_path": str(state_path),
    }


def _test_static_global_resolution() -> dict[str, Any]:
    """Catch runtime NameError risks that compilation alone cannot detect."""
    checked = [
        "portfolio_live_dashboard_orchestrator.py",
        "production_readiness_p0.py",
        "production_snapshot_pipeline.py",
        "production_hmm_integrity.py",
        "portfolio_execution_bot.py",
        "production_observability.py",
        "production_release_identity.py",
        "production_state_integrity.py",
        "portfolio_closed_trade_ledger.py",
        "broker_clock_contract.py",
    ]
    unresolved_by_file: dict[str, list[str]] = {}
    allowed_dunders = {"__file__", "__name__", "__package__", "__spec__"}
    for filename in checked:
        path = THIS_DIR / filename
        table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
        module_symbols = {symbol.get_name(): symbol for symbol in table.get_symbols()}
        unresolved: set[str] = set()
        def visit(scope: Any) -> None:
            for symbol in scope.get_symbols():
                if not (symbol.is_referenced() and symbol.is_global()):
                    continue
                name = symbol.get_name()
                if name in builtins.__dict__ or name in allowed_dunders:
                    continue
                module_symbol = module_symbols.get(name)
                if module_symbol is None or not (
                    module_symbol.is_assigned()
                    or module_symbol.is_imported()
                    or module_symbol.is_namespace()
                ):
                    unresolved.add(name)
            for child in scope.get_children():
                visit(child)
        visit(table)
        if unresolved:
            unresolved_by_file[filename] = sorted(unresolved)
    _assert(not unresolved_by_file, f"Unresolved global names detected: {unresolved_by_file}")
    return {"checked_files": checked, "unresolved_globals": unresolved_by_file}

def _test_state_integrity_recovery(root: Path) -> dict[str, Any]:
    from production_state_integrity import atomic_write_state, load_state_with_recovery

    root.mkdir(parents=True, exist_ok=True)
    state_path = root / "dashboard_state.json"
    backup_path = root / "dashboard_state.json.last_good"
    corrupt_dir = root / "corrupt"

    first = {"version": 1, "cursor": "A"}
    second = {"version": 2, "cursor": "B"}
    atomic_write_state(
        state_path, first, backup_path=backup_path, backup_interval_seconds=0.0
    )
    atomic_write_state(
        state_path, second, backup_path=backup_path, backup_interval_seconds=0.0
    )
    _assert(backup_path.exists(), "Last-good backup was not created")

    # Simulate the exact observed VPS failure: a primary JSON truncated mid-write.
    state_path.write_text('{"version": 3, "cursor": ', encoding="utf-8")
    recovered, created, report = load_state_with_recovery(
        state_path,
        fresh_state_factory=lambda: {"fresh": True},
        backup_path=backup_path,
        corrupt_dir=corrupt_dir,
    )
    _assert(created is False, f"Valid backup should avoid fresh state: {report}")
    _assert(recovered.get("version") == 1, f"Unexpected recovered backup: {recovered}")
    _assert(report.get("status") == "recovered_from_last_good_backup", report)
    _assert(list(corrupt_dir.glob("*.corrupt.json")), "Corrupt primary was not quarantined")

    # With no valid backup, recovery must return fresh state so the existing
    # dashboard new-state cursor guard can skip all already-existing live bars.
    backup_path.unlink(missing_ok=True)
    state_path.write_text('{"broken": ', encoding="utf-8")
    fresh, fresh_created, fresh_report = load_state_with_recovery(
        state_path,
        fresh_state_factory=lambda: {"fresh": True},
        backup_path=backup_path,
        corrupt_dir=corrupt_dir,
    )
    _assert(fresh_created is True, f"Missing backup should create fresh state: {fresh_report}")
    _assert(fresh.get("fresh") is True, fresh)
    _assert(
        fresh_report.get("safety_contract")
        == "dashboard_new_state_cursor_guard_skips_existing_live_bars",
        fresh_report,
    )
    return {
        "backup_recovery_status": report.get("status"),
        "fresh_recovery_status": fresh_report.get("status"),
        "corrupt_archives": len(list(corrupt_dir.glob("*"))),
    }


def _test_feature_recovery_transaction(root: Path) -> dict[str, Any]:
    # This fault-injection test exercises file transaction mechanics only. Keep
    # optional research estimators out of the import path when the validation
    # host intentionally lacks the production fracdiff/hmmlearn wheels.
    if "fracdiff.sklearn" not in sys.modules:
        fracdiff = types.ModuleType("fracdiff")
        fracdiff_sklearn = types.ModuleType("fracdiff.sklearn")
        fracdiff_sklearn.Fracdiff = type("Fracdiff", (), {})
        fracdiff_sklearn.FracdiffStat = type("FracdiffStat", (), {})
        sys.modules.setdefault("fracdiff", fracdiff)
        sys.modules.setdefault("fracdiff.sklearn", fracdiff_sklearn)
    if "hmmlearn.hmm" not in sys.modules:
        hmmlearn = types.ModuleType("hmmlearn")
        hmmlearn_hmm = types.ModuleType("hmmlearn.hmm")
        hmmlearn_hmm.GaussianHMM = type("GaussianHMM", (), {})
        sys.modules.setdefault("hmmlearn", hmmlearn)
        sys.modules.setdefault("hmmlearn.hmm", hmmlearn_hmm)
    import joblib
    import live_feature_continuation_guard as guard

    previous_output = guard.OUTPUT_DIR
    previous_archive = guard.ARCHIVE_DIR
    previous_transactions = guard.TRANSACTION_DIR
    previous_staging = guard.STAGING_DIR
    guard.OUTPUT_DIR = root / "continuation"
    guard.ARCHIVE_DIR = guard.OUTPUT_DIR / "archives"
    guard.TRANSACTION_DIR = guard.OUTPUT_DIR / "feature_generation_transactions"
    guard.STAGING_DIR = guard.OUTPUT_DIR / ".txn"

    def write_pair(paths: dict[str, Path], kind: str, marker: str) -> None:
        stamp = "2026-08-12T08:00:00+00:00"
        paths[f"{kind}_features"].parent.mkdir(parents=True, exist_ok=True)
        paths[f"{kind}_features"].write_text(
            f"Datetime,Marker\n{stamp},{marker}\n",
            encoding="utf-8",
        )
        joblib.dump(
            {
                "schema_version": f"test_{kind}_state_v1",
                "last_observation_timestamp": stamp,
                "marker": marker,
            },
            paths[f"{kind}_state"],
        )

    try:
        paths = guard._continuation_paths("X")
        write_pair(paths, "ffd", "previous-ffd")
        write_pair(paths, "hmm", "previous-hmm")
        paths["generation_manifest"].write_text(
            '{"generation_id":"previous-generation"}\n',
            encoding="utf-8",
        )
        for name in {"ffd_lock", "hmm_lock"}:
            paths[name].write_text("active-lock", encoding="utf-8")
        original_hashes = {
            name: guard._sha256(paths[name])
            for name in {"ffd_features", "ffd_state", "hmm_features", "hmm_state"}
        }

        interrupted = guard._begin_generation_transaction(
            "X",
            reason="fault_injection_before_build",
        )
        _assert(
            paths["ffd_lock"].exists() and paths["hmm_lock"].exists(),
            "Active locks were moved by transaction preparation",
        )
        _assert(
            Path(interrupted["journal_path"]).exists(),
            "Durable recovery journal was not published before mutation",
        )
        _assert(
            all(
                guard._sha256(paths[name]) == expected
                for name, expected in original_hashes.items()
            ),
            "Transaction preparation removed or changed the active generation",
        )
        recovered = guard.recover_interrupted_generation_transactions(
            assets=["X"]
        )
        _assert(len(recovered) == 1, recovered)
        restored = {
            name: guard._sha256(paths[name]) == expected
            for name, expected in original_hashes.items()
        }
        _assert(all(restored.values()), restored)
        _assert(
            not Path(interrupted["journal_path"]).exists(),
            "Recovered transaction journal was not retired",
        )

        ffd_before_hmm_repair = guard._sha256(paths["ffd_features"])
        hmm_transaction = guard._begin_generation_transaction(
            "X",
            reason="hmm_only_fault_during_validation",
            components={"hmm_features", "hmm_state", "generation_manifest"},
        )
        hmm_staging = guard._prepare_staging_directory(hmm_transaction)
        write_pair(
            guard._continuation_paths("X", hmm_staging),
            "hmm",
            "invalid-hmm",
        )
        guard._rollback_generation_transaction(
            hmm_transaction,
            reason="simulated_validation_failure",
        )
        _assert(
            guard._sha256(paths["ffd_features"]) == ffd_before_hmm_repair,
            "HMM-only rollback modified the healthy FFD generation",
        )
        _assert(
            guard._sha256(paths["hmm_features"]) == original_hashes["hmm_features"]
            and guard._sha256(paths["hmm_state"]) == original_hashes["hmm_state"],
            "HMM-only rollback did not restore its prior pair",
        )
        _assert(not hmm_staging.exists(), "Rollback left staged artifacts behind")

        commit_transaction = guard._begin_generation_transaction(
            "X",
            reason="successful_atomic_switch",
            components={"hmm_features", "hmm_state", "generation_manifest"},
        )
        commit_staging = guard._prepare_staging_directory(commit_transaction)
        staged_paths = guard._continuation_paths("X", commit_staging)
        write_pair(staged_paths, "hmm", "committed-hmm")
        staged_pairing = guard._generation_pairing(
            "X",
            {"hmm_features", "hmm_state"},
            root=commit_staging,
        )
        _assert(staged_pairing["hmm"]["matched"], staged_pairing)
        activated = guard._activate_staged_generation(
            "X",
            commit_transaction,
            commit_staging,
        )
        _assert(
            activated == ["hmm_features", "hmm_state"],
            activated,
        )
        result = {
            "status": "PASS",
            "live_bar_latest": "2026-08-12T08:00:00+00:00",
            "dependency": {"requires_ffd": True, "requires_hmm": True},
        }
        generation = guard._publish_generation_manifest(
            "X",
            result,
            transaction=commit_transaction,
        )
        committed = guard._commit_generation_transaction(
            commit_transaction,
            generation,
        )
        published = json.loads(
            paths["generation_manifest"].read_text(encoding="utf-8")
        )
        _assert(committed["status"] == "COMMITTED", committed)
        _assert(
            published["generation_id"] == generation["generation_id"],
            "Atomic generation pointer does not identify committed artifacts",
        )
        _assert(
            published["pairing"]["ffd"]["matched"]
            and published["pairing"]["hmm"]["matched"],
            published,
        )
        _assert(
            not Path(commit_transaction["journal_path"]).exists(),
            "Committed transaction journal was not retired",
        )
        _assert(not commit_staging.exists(), "Commit left staged artifacts behind")
        committed_hashes = {
            name: guard._sha256(paths[name])
            for name in {"hmm_features", "hmm_state"}
        }
        interrupted_after_swap = guard._begin_generation_transaction(
            "X",
            reason="fault_injection_after_atomic_pair_switch",
            components={"hmm_features", "hmm_state", "generation_manifest"},
        )
        after_swap_staging = guard._prepare_staging_directory(
            interrupted_after_swap
        )
        write_pair(
            guard._continuation_paths("X", after_swap_staging),
            "hmm",
            "interrupted-after-swap",
        )
        guard._activate_staged_generation(
            "X",
            interrupted_after_swap,
            after_swap_staging,
        )
        recovered_after_swap = guard.recover_interrupted_generation_transactions(
            assets=["X"]
        )
        _assert(len(recovered_after_swap) == 1, recovered_after_swap)
        _assert(
            all(
                guard._sha256(paths[name]) == expected
                for name, expected in committed_hashes.items()
            ),
            "Interruption after atomic pair switch did not restore the last committed generation",
        )
        _assert(
            not after_swap_staging.exists(),
            "Interrupted switch recovery left staged artifacts behind",
        )
        vps_root = Path(
            r"C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
            r"\portfolio_live\outputs\live_feature_continuation\.txn"
        )
        vps_stage = vps_root / "x-0123456789ab"
        vps_budget = guard._staging_path_budget(vps_stage, "XAUUSD")
        legacy_stage = Path(
            r"C:\Users\Administrator\Desktop\Trading Models\trading_models_v2"
            r"\portfolio_live\outputs\live_feature_continuation\archives"
        ) / (
            "20260812T104500.249763Z-336-1786531500249763100"
        ) / "XAUUSD" / "staged_generation"
        legacy_budget = guard._staging_path_budget(legacy_stage, "XAUUSD")
        _assert(vps_budget["passed"], vps_budget)
        _assert(
            not legacy_budget["passed"],
            "Regression fixture no longer reproduces the VPS path-length failure",
        )
        return {
            "active_locks_preserved": True,
            "interrupted_prebuild_restored": True,
            "validation_failure_rolled_back": True,
            "previous_generation_restored": True,
            "restored_components": sorted(original_hashes),
            "hmm_only_rollback_preserved_ffd": True,
            "atomic_generation_switch_committed": True,
            "staged_generation_validated_before_switch": True,
            "interruption_after_switch_restored": True,
            "csv_checkpoint_pairing_verified": True,
            "committed_generation_id": generation["generation_id"],
            "active_generation_visible_during_stage": True,
            "staging_cleanup_verified": True,
            "vps_staging_path_length": vps_budget["longest_length"],
            "legacy_staging_path_length": legacy_budget["longest_length"],
        }
    finally:
        guard.OUTPUT_DIR = previous_output
        guard.ARCHIVE_DIR = previous_archive
        guard.TRANSACTION_DIR = previous_transactions
        guard.STAGING_DIR = previous_staging


def _test_hmm_dependency_capture_forwarding(root: Path) -> dict[str, Any]:
    """Reproduce the VPS capture(continuation_dir=...) compatibility failure."""
    from shared.live_streaming import continuation
    from shared.live_streaming import state as hot_state

    previous_ffd = hot_state.ORIGINAL_FFD_VERIFY
    previous_hmm = hot_state.ORIGINAL_HMM_VERIFY
    stage = root / "staged"
    observed: dict[str, Any] = {}

    def original_ffd(asset: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        observed["asset"] = asset
        observed["args"] = args
        observed["continuation_dir"] = kwargs.get("continuation_dir")
        return {"asset": asset, "features": "staged-ffd.csv"}

    verifier_globals: dict[str, Any] = {
        "verify_asset_ffd_manifest": lambda asset, **kwargs: {},
    }
    exec(
        "def original_hmm(asset, *, continuation_dir=None):\n"
        "    dependency = verify_asset_ffd_manifest(\n"
        "        asset, continuation_dir=continuation_dir\n"
        "    )\n"
        "    return {'asset': asset, 'dependency': dependency}\n",
        verifier_globals,
    )
    try:
        hot_state.ORIGINAL_FFD_VERIFY = original_ffd
        hot_state.ORIGINAL_HMM_VERIFY = verifier_globals["original_hmm"]
        result, captured = continuation._authoritative_hmm(
            "X",
            continuation_dir=stage,
        )
        _assert(observed.get("asset") == "X", observed)
        _assert(observed.get("continuation_dir") == stage, observed)
        _assert((captured or {}).get("features") == "staged-ffd.csv", captured)
        _assert(result.get("dependency", {}).get("features") == "staged-ffd.csv", result)
        return {
            "continuation_dir_forwarded": True,
            "dependency_capture_signature_transparent": True,
        }
    finally:
        hot_state.ORIGINAL_FFD_VERIFY = previous_ffd
        hot_state.ORIGINAL_HMM_VERIFY = previous_hmm


def _test_p0_self_test() -> dict[str, Any]:
    import production_readiness_p0 as p0

    result = p0.self_test()
    _assert(result.get("status") == "PASS", f"P0 self-test failed: {result}")
    return result


def _test_snapshot_pipeline_self_test() -> dict[str, Any]:
    import production_snapshot_pipeline as pipeline

    result = pipeline.self_test()
    _assert(result.get("status") == "PASS", f"Snapshot pipeline self-test failed: {result}")
    _assert(
        result.get("version") == "2026-08-15_asset_snapshot_coordinator_v8_phase_b_dependency_cache_hydration",
        f"Unexpected snapshot pipeline version: {result}",
    )
    return result


def _test_broker_clock_contract() -> dict[str, Any]:
    import broker_clock_contract

    result = broker_clock_contract.self_test()
    _assert(result.get("status") == "PASS", f"Broker clock contract failed: {result}")
    return result


def _test_live_policy_authority_contract() -> dict[str, Any]:
    """Prevent latency hardening from silently rewriting trading policy."""
    settings_text = (THIS_DIR / "live_settings.py").read_text(encoding="utf-8")
    executor_text = (THIS_DIR / "portfolio_execution_bot.py").read_text(encoding="utf-8")
    local_arming_keys = [
        "LIVE_MODE",
        "ALLOW_REAL_LIVE_TRADING_EXPORT",
        "ENABLE_ORDER_SEND",
        "PORTFOLIO_EXECUTION_MODE",
        "MT5_LOGIN",
        "MT5_PASSWORD",
        "MT5_SERVER",
    ]
    for key in local_arming_keys:
        _assert(
            f'"{key}"' in settings_text,
            f"local_live_settings.json no longer supports {key}",
        )
    _assert(
        "_apply_local_settings()" in settings_text,
        "live_settings.py no longer applies local_live_settings.json",
    )
    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8-sig")
    _assert(
        "trading_models_v2/portfolio_live/local_live_settings.json" in gitignore_text,
        "The live settings/credential override is no longer Git-ignored",
    )
    _assert(
        "PORTFOLIO_DASHBOARD_MAX_SIGNAL_AGE_SECONDS = 120" in settings_text,
        "Dashboard signal-validity policy no longer preserves the 120-second contract",
    )
    _assert(
        "PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 120" in settings_text,
        "Executor signal-validity policy no longer preserves the 120-second contract",
    )
    _assert(
        "LIVE_DASHBOARD_SNAPSHOT_MAX_AGE_SECONDS" not in settings_text,
        "Snapshot coordinator still exposes a second independent validity policy",
    )
    pipeline_text = (THIS_DIR / "production_snapshot_pipeline.py").read_text(
        encoding="utf-8"
    )
    _assert(
        'ns.get("MAX_SIGNAL_AGE_SECONDS", 120.0)' in pipeline_text,
        "Snapshot coordinator no longer consumes dashboard signal validity",
    )
    dashboard_text = (
        THIS_DIR / "portfolio_live_dashboard_orchestrator.py"
    ).read_text(encoding="utf-8")
    _assert(
        "startup_continuation_health = ensure_assets_live_feature_continuation" in dashboard_text
        and "startup_continuation_health" in dashboard_text
        and "auto_repair=False" in dashboard_text,
        "Startup still performs foreground transactional feature recovery",
    )
    _assert(
        "no approved signals in this run; lifetime_state contains historical signals only"
        in dashboard_text,
        "Heartbeat still presents historical signals as current-run emissions",
    )

    _assert(
        "PORTFOLIO_MAX_OPEN_POSITIONS_PER_EDGE_OVERRIDE" not in settings_text,
        "Local settings still expose an executor-owned per-edge position override",
    )
    _assert(
        "MAX_OPEN_POSITIONS_PER_EDGE_OVERRIDE" not in executor_text,
        "Executor still contains a hard-coded per-edge position override",
    )
    _assert(
        executor_text.count('"max_open_trades_per_edge"') >= 4,
        "Pre-fill and post-fill checks no longer consume manifest per-edge caps",
    )
    return {
        "dashboard_signal_age_seconds": 120,
        "executor_signal_age_seconds": 120,
        "snapshot_age_authority": "dashboard_MAX_SIGNAL_AGE_SECONDS",
        "per_edge_position_authority": "live_manifest",
        "local_live_arming_overrides": local_arming_keys,
        "local_live_settings_gitignored": True,
        "startup_recovery_mode": "verify_then_background_recovery",
        "signal_status_scope": "current_run",
    }


def _test_observed_semaphore() -> dict[str, Any]:
    from production_observability import ObservedSemaphore

    semaphore = ObservedSemaphore(2, name="validation")
    active = 0
    maximum = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal active, maximum
        with semaphore:
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stats = semaphore.snapshot()
    _assert(maximum == 2, f"Semaphore did not enforce capacity: {maximum}")
    _assert(stats["max_active_count"] == 2, f"Unexpected semaphore stats: {stats}")
    _assert(stats["acquire_count"] == 6, f"Unexpected acquire count: {stats}")
    return stats


def _test_release_identity_and_temp_recovery(root: Path) -> dict[str, Any]:
    from production_release_identity import build_release_identity, recover_stale_temp_files

    repo = root / "repo"
    v2 = repo / "trading_models_v2"
    output = v2 / "portfolio_live" / "outputs" / "release_identity"
    critical = v2 / "portfolio_live" / "sample.py"
    critical.parent.mkdir(parents=True, exist_ok=True)
    critical.write_text("VALUE = 1\n", encoding="utf-8")
    first = build_release_identity(
        process_name="validation", repo_root=repo, v2_root=v2,
        output_dir=output, critical_files=[critical],
    )
    second = build_release_identity(
        process_name="validation", repo_root=repo, v2_root=v2,
        output_dir=output, critical_files=[critical],
    )
    execution = build_release_identity(
        process_name="portfolio_execution_bot", repo_root=repo, v2_root=v2,
        output_dir=output, critical_files=[critical],
    )
    _assert(first["release_id"] == second["release_id"], "Release identity is unstable")
    validation_current = output / "current_validation_release_identity.json"
    execution_current = output / "current_portfolio_execution_bot_release_identity.json"
    _assert(validation_current.exists() and execution_current.exists(), "Process-specific release identities missing")
    _assert(not (output / "current_release_identity.json").exists(), "Shared current identity should not be overwritten")

    # Exercise the production cleanliness boundary in a real Git repository.
    git_repo = root / "git_release_repo"
    git_v2 = git_repo / "trading_models_v2"
    git_live = git_v2 / "portfolio_live"
    git_output = git_live / "outputs" / "release_identity"
    git_critical = git_live / "sample.py"
    git_state = git_live / "live_state" / "portfolio_execution_bot_state.json"
    git_manifest = git_live / "live_manifest.json"
    git_bar_policy = git_live / "config" / "live_bar_policy.json"
    git_other_config = git_live / "config" / "account_policy.json"
    git_state.parent.mkdir(parents=True, exist_ok=True)
    git_bar_policy.parent.mkdir(parents=True, exist_ok=True)
    git_critical.write_text("VALUE = 1\n", encoding="utf-8")
    git_state.write_text('{"cursor": 1}\n', encoding="utf-8")
    git_manifest.write_text('{"version": 1}\n', encoding="utf-8")
    git_bar_policy.write_text('{"threshold": 100}\n', encoding="utf-8")
    git_other_config.write_text('{"account_stage": "phase1"}\n', encoding="utf-8")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "quantara-validation@example.invalid"],
        ["git", "config", "user.name", "Quantara Validation"],
        ["git", "add", "."],
        ["git", "commit", "-q", "-m", "validation baseline"],
    ):
        subprocess.run(command, cwd=git_repo, check=True, capture_output=True, text=True)

    release_env_names = (
        "TRADING_MODELS_RELEASE_COMMIT", "TRADING_MODELS_RELEASE_BRANCH",
        "TRADING_MODELS_RELEASE_CLEAN",
    )
    release_env = {name: os.environ.pop(name, None) for name in release_env_names}
    try:
        clean_release = build_release_identity(
            process_name="validation_clean", repo_root=git_repo, v2_root=git_v2,
            output_dir=git_output,
            critical_files=[git_critical, git_manifest, git_bar_policy, git_other_config],
        )
        _assert(clean_release["immutable_git_release"] is True, clean_release)

        # Runtime state is tracked in the current repository but is expected to
        # change continuously; it must not disarm an otherwise immutable build.
        git_state.write_text('{"cursor": 2}\n', encoding="utf-8")
        runtime_mutation_release = build_release_identity(
            process_name="validation_runtime_mutation", repo_root=git_repo,
            v2_root=git_v2, output_dir=git_output,
            critical_files=[git_critical, git_manifest],
        )
        _assert(runtime_mutation_release["immutable_git_release"] is True, runtime_mutation_release)
        _assert(runtime_mutation_release["git_dirty"] is False, runtime_mutation_release)
        _assert(
            "trading_models_v2/portfolio_live/live_state"
            in runtime_mutation_release["mutable_runtime_git_exclusions"],
            runtime_mutation_release,
        )

        # The dashboard intentionally refreshes this one tracked policy from
        # completed sessions.  It remains tracked and hashed, but its expected
        # data-only update must not disarm real order sending.
        git_bar_policy.write_text('{"threshold": 101}\n', encoding="utf-8")
        policy_mutation_release = build_release_identity(
            process_name="validation_policy_mutation", repo_root=git_repo,
            v2_root=git_v2, output_dir=git_output,
            critical_files=[git_critical, git_manifest, git_bar_policy, git_other_config],
        )
        _assert(policy_mutation_release["immutable_git_release"] is True, policy_mutation_release)
        _assert(policy_mutation_release["git_dirty"] is False, policy_mutation_release)
        _assert(
            "trading_models_v2/portfolio_live/config/live_bar_policy.json"
            in policy_mutation_release["mutable_runtime_git_exclusions"],
            policy_mutation_release,
        )
        _assert(
            not policy_mutation_release["untracked_critical_files"]
            and policy_mutation_release["critical_file_tracking_unknown"] is False,
            policy_mutation_release,
        )

        # No other file in config is mutable by this exception.
        git_other_config.write_text('{"account_stage": "phase2"}\n', encoding="utf-8")
        config_mutation_release = build_release_identity(
            process_name="validation_config_mutation", repo_root=git_repo,
            v2_root=git_v2, output_dir=git_output,
            critical_files=[git_critical, git_manifest, git_bar_policy, git_other_config],
        )
        _assert(config_mutation_release["immutable_git_release"] is False, config_mutation_release)
        _assert(config_mutation_release["git_dirty"] is True, config_mutation_release)
        git_other_config.write_text('{"account_stage": "phase1"}\n', encoding="utf-8")

        # A tracked deployable input must still fail closed even while mutable
        # runtime state is also dirty.
        git_critical.write_text("VALUE = 2\n", encoding="utf-8")
        source_mutation_release = build_release_identity(
            process_name="validation_source_mutation", repo_root=git_repo,
            v2_root=git_v2, output_dir=git_output,
            critical_files=[git_critical, git_manifest],
        )
        _assert(source_mutation_release["immutable_git_release"] is False, source_mutation_release)
        _assert(source_mutation_release["git_dirty"] is True, source_mutation_release)
    finally:
        for name, value in release_env.items():
            if value is not None:
                os.environ[name] = value

    data = v2 / "portfolio_live" / "outputs" / "live_dashboard"
    data.mkdir(parents=True, exist_ok=True)
    target = data / "audit.csv"
    target.write_text("signal_id,status\nA,ok\n", encoding="utf-8")
    temp = data / ".audit.csv.123.456.tmp"
    temp.write_text("signal_id,status\nA,ok\nB,recovered\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(temp, (old, old))
    recovery = recover_stale_temp_files(
        roots=[data], archive_root=data / "recovered", min_age_seconds=1,
    )
    rows = list(csv.DictReader(target.open("r", encoding="utf-8")))
    _assert({row["signal_id"] for row in rows} == {"A", "B"}, f"Temp recovery failed: {rows}")
    _assert(recovery["recovered_row_count"] == 1, f"Unexpected recovery: {recovery}")
    return {
        "release_id": first["release_id"],
        "execution_release_id": execution["release_id"],
        "process_specific_current_files": 2,
        "mutable_runtime_state_preserves_immutable_release": True,
        "mutable_live_bar_policy_preserves_immutable_release": True,
        "other_config_mutation_fails_closed": True,
        "tracked_source_mutation_fails_closed": True,
        "temp_recovery": recovery,
    }



def _test_hmm_integrity_contract() -> dict[str, Any]:
    import pandas as pd
    import production_hmm_integrity as integrity

    names = ["shared", "shared.regime", "shared.regime.hmm_regime"]
    previous = {name: sys.modules.get(name) for name in names}
    shared = types.ModuleType("shared"); shared.__path__ = []
    regime = types.ModuleType("shared.regime"); regime.__path__ = []
    hmm = types.ModuleType("shared.regime.hmm_regime")
    hmm.HMM_FEATURES = [
        "Close_FFD", "Bar_Volatility_Bps", "Abs_Bar_Return_Bps",
        "Body_To_Range", "Close_Location",
    ]
    class Config:
        def __init__(self, n_states: int = 4, **kwargs: Any) -> None:
            self.n_states = n_states
    hmm.HMMRegimeConfig = Config
    def engineer(frame: Any, config: Any = None) -> Any:
        out = frame.copy()
        for column in ["Open", "High", "Low", "Close", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]:
            out[column] = pd.to_numeric(out[column], errors="coerce")
        out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
        bar_range = (out["High"] - out["Low"]).replace(0, float("nan"))
        out["Bar_Volatility_Bps"] = ((out["High"] - out["Low"]) / out["Open"]) * 10000.0
        out["Abs_Bar_Return_Bps"] = ((out["Close"] / out["Open"]).map(__import__("math").log) * 10000.0).abs()
        out["Body_To_Range"] = (out["Close"] - out["Open"]).abs() / bar_range
        out["Close_Location"] = (out["Close"] - out["Low"]) / bar_range
        return out.dropna(subset=["Datetime", *hmm.HMM_FEATURES]).reset_index(drop=True)
    hmm.engineer_hmm_inputs = engineer
    sys.modules["shared"] = shared
    sys.modules["shared.regime"] = regime
    sys.modules["shared.regime.hmm_regime"] = hmm
    try:
        times = pd.to_datetime([
            "2026-07-30T10:00:00Z", "2026-07-30T10:01:00Z", "2026-07-30T10:02:00Z"
        ], utc=True)
        rows = []
        for index, stamp in enumerate(times):
            high = 101.0 if index != 1 else 100.0
            low = 99.0 if index != 1 else 100.0
            row = {
                "Datetime": stamp, "Open": 100.0, "High": high, "Low": low, "Close": 100.5,
                "Open_FFD": 1.0, "High_FFD": 1.1, "Low_FFD": 0.9, "Close_FFD": 1.05,
                "Regime_State": 1.0 if index == 0 else float("nan"),
                "Regime_Entropy": 0.5 if index == 0 else float("nan"),
                "Regime_Duration": 2.0 if index == 0 else float("nan"),
                "Prob_State_0": 0.1 if index == 0 else float("nan"),
                "Prob_State_1": 0.6 if index == 0 else float("nan"),
                "Prob_State_2": 0.2 if index == 0 else float("nan"),
                "Prob_State_3": 0.1 if index == 0 else float("nan"),
            }
            rows.append(row)
        frame = pd.DataFrame(rows)
        classification = integrity.classify_candidate_hmm_coverage(frame, times, n_states=4)
        _assert(classification["valid"] == [times[0].isoformat()], f"Unexpected valid classification: {classification}")
        _assert(classification["ineligible"] == [times[1].isoformat()], f"Unexpected ineligible classification: {classification}")
        _assert(classification["repair_required"] == [times[2].isoformat()], f"Unexpected repair classification: {classification}")
        finite = integrity.validate_hmm_rows(frame.iloc[[0]], n_states=4)
        _assert(finite["status"] == "PASS", f"Finite HMM row rejected: {finite}")
        return {
            "valid": len(classification["valid"]),
            "ineligible": len(classification["ineligible"]),
            "repair_required": len(classification["repair_required"]),
            "version": integrity.HMM_INTEGRITY_VERSION,
        }
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _test_semantic_negative_and_canonical_cache(root: Path) -> dict[str, Any]:
    import pandas as pd
    import production_readiness_p0 as p0

    p0._SEMANTIC_FEATURE_CACHE.clear()
    p0._EXPLICIT_CONTRACT_PROOFS.clear()
    p0._CANONICAL_ALIAS_PROOFS.clear()
    p0._EXPLICIT_PROBE_LOCKS.clear()
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "shared_feature_contracts.json").write_text(
        json.dumps({"family_contracts": {"absorption": {"contract_id": "abs_v1"}}}),
        encoding="utf-8",
    )

    class Runtime:
        def __init__(self, name: str, mode: str = "same") -> None:
            self.name = name
            self.mode = mode
            self.calls = 0
            self.__file__ = str(THIS_DIR / "validate_production_hardening_patch.py")
        def build_features(self, bars: Any, drop_warmup_rows: bool = False) -> Any:
            self.calls += 1
            out = bars[["Datetime", "Open", "High", "Low", "Close"]].copy()
            out["x"] = range(len(out)); out["edge_id"] = self.name; out["side"] = 1
            if self.mode == "drop":
                out = out.iloc[1:].reset_index(drop=True)
            return out

    canonical = Runtime("canonical")
    alias = Runtime("alias")
    absorption = Runtime("absorption", "drop")
    edges = [
        {"edge_id": "X/A", "asset": "X", "family": "a", "runtime": canonical},
        {"edge_id": "X/B", "asset": "X", "family": "b", "runtime": alias},
        {"edge_id": "X/ABS", "asset": "X", "family": "absorption", "runtime": absorption},
    ]
    namespace = {
        "prepare_shared_features_for_edge": lambda **kwargs: (None, "old"),
        "shared_feature_cache_coverage_for_edge": lambda **kwargs: (False, "old", 0, []),
        "EDGE_LOCAL_FEATURE_FAMILIES": {"absorption"},
        "settings": SimpleNamespace(LIVE_CONFIG_DIR=config),
        "OUTPUT_DIR": root / "output", "V2_ROOT": root,
        "SHARED_FEATURE_CACHE_ENABLED": True,
        "get_runtime": lambda edge: edge["runtime"],
        "shared_feature_contract_identity": lambda edge, runtime: (
            "semantic", "aaa_canonical" if edge["family"] == "a" else "zzz_alias"
        ),
        "runtime_build_features_fingerprint": lambda runtime: runtime.name,
        "shared_feature_builder_priority": lambda edge: edge["edge_id"],
        "runtime_expected_feature_names": lambda runtime: ["x"],
        "_stable_frame_hash": lambda frame, exclude_columns=None: str(
            pd.util.hash_pandas_object(frame, index=True).sum()
        ),
    }
    original_readiness = p0._exact_readiness
    p0._exact_readiness = lambda *args, **kwargs: {"status": "PASS"}
    logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
    )
    try:
        p0._install_semantic_cache(namespace)
        bars = pd.DataFrame(
            {
                "Datetime": pd.date_range("2026-01-01", periods=3, tz="UTC"),
                "Open": [1.0, 2.0, 3.0], "High": [2.0, 3.0, 4.0],
                "Low": [0.5, 1.5, 2.5], "Close": [1.5, 2.5, 3.5],
            }
        )
        state: dict[str, Any] = {}
        namespace["build_shared_asset_runtime_features"](
            asset="X", asset_edges=edges, bars_snapshot=bars,
            live_event_times_snapshot=set(), state=state, logger=logger,
        )
        bars2 = __import__("pandas").concat(
            [
                bars,
                pd.DataFrame(
                    {
                        "Datetime": [pd.Timestamp("2026-01-01T00:03:00Z")],
                        "Open": [4.0], "High": [5.0], "Low": [3.5], "Close": [4.5],
                    }
                ),
            ],
            ignore_index=True,
        )
        namespace["build_shared_asset_runtime_features"](
            asset="X", asset_edges=edges, bars_snapshot=bars2,
            live_event_times_snapshot=set(), state=state, logger=logger,
        )
        _assert(absorption.calls == 1, f"Failed absorption proof was repeated: {absorption.calls}")
        _assert(alias.calls == 1, f"Proven canonical alias rebuilt unnecessarily: {alias.calls}")
        proof_file = root / "output" / "shared_feature_contract_proofs.json"
        proof_payload = json.loads(proof_file.read_text(encoding="utf-8"))
        explicit = list((proof_payload.get("proofs") or {}).values())
        canonical_proofs = list((proof_payload.get("canonical_alias_proofs") or {}).values())
        _assert(explicit and explicit[0]["status"] == "FAIL", f"Negative proof not persisted: {explicit}")
        _assert(canonical_proofs and canonical_proofs[0]["status"] == "PASS", f"Canonical proof missing: {canonical_proofs}")
        return {
            "absorption_probe_calls_across_two_snapshots": absorption.calls,
            "canonical_alias_builder_calls_across_two_snapshots": alias.calls,
            "negative_proof_cached": True,
            "canonical_alias_reused": True,
        }
    finally:
        p0._exact_readiness = original_readiness
        p0._SEMANTIC_FEATURE_CACHE.clear()
        p0._EXPLICIT_CONTRACT_PROOFS.clear()
        p0._CANONICAL_ALIAS_PROOFS.clear()
        p0._EXPLICIT_PROBE_LOCKS.clear()

def _test_orchestrator_asset_parallelism(root: Path) -> dict[str, Any]:
    settings = types.ModuleType("live_settings")
    settings.OUTPUT_DIR = root / "outputs"
    settings.LIVE_LOGS_DIR = root / "logs"
    settings.LIVE_STATE_DIR = root / "state"
    settings.LIVE_CONFIG_DIR = root / "config"
    settings.LIVE_BARS_DIR = root / "bars"
    settings.LIVE_DASHBOARD_ASSET_SCORING_WORKERS = 4
    settings.LIVE_DASHBOARD_MAX_CONCURRENT_RUNTIME_CALLS = 4
    settings.LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_ENABLED = True
    settings.LIVE_DASHBOARD_ASYNC_SCORING_QUEUE_MAX_PENDING_JOBS = 1
    settings.LIVE_DASHBOARD_SLOW_LANE_SCORING_ENABLED = False
    settings.LIVE_DASHBOARD_PARALLEL_EDGE_SCORING_ENABLED = False
    sys.modules["live_settings"] = settings

    rules = {"profile_label": "validation", "account_stage": "phase1", "daily_symbol_risk": {"enabled": False, "enforcement_mode": "disabled"}}
    shared = types.ModuleType("shared"); shared.__path__ = []
    prop = types.ModuleType("shared.prop_account_rules")
    prop.active_rule_contract = lambda value: dict(value)
    prop.load_active_account_rules = lambda: dict(rules)
    prop.structurally_equal = lambda left, right: left == right
    sys.modules["shared"] = shared; sys.modules["shared.prop_account_rules"] = prop

    common = types.ModuleType("common")
    common.ensure_dirs = lambda: None
    common.broker_symbol_for_asset = lambda *args, **kwargs: "TEST"
    common.import_edge_runtime = lambda *args, **kwargs: None
    common.init_mt5 = lambda *args, **kwargs: None
    common.load_live_manifest = lambda *args, **kwargs: {}
    common.read_json = lambda *args, **kwargs: {}
    common.setup_logger = lambda *args, **kwargs: None
    common.shutdown_mt5 = lambda *args, **kwargs: None
    common.write_json = lambda *args, **kwargs: None
    sys.modules["common"] = common

    continuation = types.ModuleType("live_feature_continuation_guard")
    continuation.ensure_assets_live_feature_continuation = lambda **kwargs: {"status": "PASS"}
    sys.modules["live_feature_continuation_guard"] = continuation

    live_streaming = types.ModuleType("shared.live_streaming"); live_streaming.__path__ = []
    hotpath = types.ModuleType("shared.live_streaming.hotpath")
    hotpath.install_hotpath_verifiers = lambda: {"installed": True}
    hotpath.cache_status = lambda: {}
    hotpath.verify_asset_ffd_manifest_hot = lambda *args, **kwargs: {}
    hotpath.verify_asset_hmm_regime_manifest_hot = lambda *args, **kwargs: {}
    hot_state = types.ModuleType("shared.live_streaming.state")
    asset_locks: dict[str, threading.RLock] = {}
    asset_locks_guard = threading.Lock()
    def asset_lock(asset: str) -> threading.RLock:
        with asset_locks_guard:
            return asset_locks.setdefault(str(asset), threading.RLock())
    hot_state.asset_lock = asset_lock
    sys.modules["shared.live_streaming"] = live_streaming
    sys.modules["shared.live_streaming.hotpath"] = hotpath
    sys.modules["shared.live_streaming.state"] = hot_state

    import production_readiness_p0 as p0
    p0.install_hmm_integrity_guard = lambda **kwargs: {"installed": True, "validation_stub": True}

    name = "portfolio_live_dashboard_orchestrator_validation"
    spec = importlib.util.spec_from_file_location(name, THIS_DIR / "portfolio_live_dashboard_orchestrator.py")
    _assert(spec is not None and spec.loader is not None, "Unable to load orchestrator spec")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)

    logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, error=lambda *args, **kwargs: None)
    manifest = {"selected_edges": [{"asset": asset, "edge_id": f"{asset}/edge"} for asset in ("A", "B", "C", "D")]}
    manager = module.ScoringQueueManager(manifest=manifest, bar_policy={}, state={}, logger=logger, state_lock=threading.RLock())
    active = 0; maximum = 0; completed: list[str] = []; guard = threading.Lock()
    def process_job(self: Any, job: dict[str, Any]) -> None:
        nonlocal active, maximum
        with guard:
            active += 1; maximum = max(maximum, active)
        time.sleep(0.08)
        with guard:
            active -= 1; completed.append(str(job["assets"][0]))
    manager._process_job = types.MethodType(process_job, manager)
    manager.start(); manager.enqueue(["A", "B", "C", "D"], reason="validation")
    deadline = time.time() + 5.0
    while len(completed) < 4 and time.time() < deadline:
        time.sleep(0.02)
    _assert(len(completed) == 4, f"Not all asset jobs completed: {completed}")

    # Exercise the exact public API used by the P0 readiness retry scheduler.
    manager.enqueue(["A"], lane="fast", reason="explicit_lane_retry_validation")
    deadline = time.time() + 5.0
    while len(completed) < 5 and time.time() < deadline:
        time.sleep(0.02)

    status = manager.status_snapshot(); manager.stop(wait=True)
    _assert(len(completed) == 5, f"Explicit-lane retry enqueue failed: {completed}")
    _assert(maximum == 4, f"Asset queue did not run concurrently: maximum={maximum}")
    _assert(status.get("p0_worker_count") == 4, f"Unexpected worker count: {status}")

    # Same active signature must not create a follow-up; a changed dependency
    # signature must create exactly one follow-up.
    module._DASHBOARD_SHUTDOWN_EVENT.clear()
    manager2 = module.ScoringQueueManager(
        manifest={"selected_edges": [{"asset": "A", "edge_id": "A/edge"}]},
        bar_policy={}, state={}, logger=logger, state_lock=threading.RLock(),
    )
    started = threading.Event(); release = threading.Event(); completed2: list[str] = []
    def process_job2(self: Any, job: dict[str, Any]) -> None:
        started.set(); release.wait(timeout=2.0); completed2.append(str(job["assets"][0]))
    manager2._process_job = types.MethodType(process_job2, manager2)
    manager2.start(); manager2.enqueue(["A"], reason="same_signature")
    _assert(started.wait(timeout=2.0), "Signature test job did not start")
    manager2.enqueue(["A"], reason="same_signature")
    manager2.enqueue(["A"], reason="same_signature")
    manager2.state["pending_feature_readiness_by_asset_snapshot"] = {
        "fast:A:changed": {
            "asset": "A", "lane": "fast", "retry_key": "fast:A:changed",
            "candidate_timestamp_set_hash": "changed", "dependency_contract_version": "v2",
        }
    }
    manager2.enqueue(["A"], lane="fast", reason="p0_due_asset_snapshot_readiness_retry")
    release.set()
    deadline = time.time() + 4.0
    while len(completed2) < 2 and time.time() < deadline:
        time.sleep(0.02)
    manager2.stop(wait=True)
    _assert(len(completed2) == 2, f"Expected one changed-signature follow-up only: {completed2}")

    return {
        "completed_assets": sorted(completed[:4]),
        "explicit_lane_retry_completed": completed[-1] == "A",
        "max_concurrent_assets": maximum,
        "worker_count": status.get("p0_worker_count"),
        "same_signature_followups": 0,
        "changed_signature_followups": 1,
    }


def _install_execution_import_stubs(root: Path) -> None:
    settings = types.ModuleType("live_settings")
    settings.OUTPUT_DIR = root / "outputs"
    settings.LIVE_LOGS_DIR = root / "logs"
    settings.LIVE_STATE_DIR = root / "state"
    settings.LIVE_CONFIG_DIR = root / "config"
    settings.LIVE_BARS_DIR = root / "bars"
    settings.LIVE_MANIFEST_PATH = root / "live_manifest.json"
    settings.ACTIVE_ACCOUNT_RULES_PATH = root / "active_account_rules.json"
    sys.modules["live_settings"] = settings

    rules = {
        "profile_label": "validation",
        "account_stage": "phase1",
        "initial_balance": 5000.0,
        "current_balance": 5000.0,
        "current_high_watermark": 5000.0,
        "daily_symbol_risk": {"enabled": False, "enforcement_mode": "disabled"},
    }
    shared = types.ModuleType("shared")
    shared.__path__ = []
    prop = types.ModuleType("shared.prop_account_rules")
    prop.active_rule_contract = lambda value: dict(value)
    prop.load_active_account_rules = lambda: dict(rules)
    prop.structurally_equal = lambda left, right: left == right
    prop.daily_breach_value = lambda *args, **kwargs: 0.0
    prop.daily_drawdown_amount = lambda *args, **kwargs: 0.0
    prop.daily_drawdown_floor = lambda *args, **kwargs: 0.0
    prop.overall_breach_value = lambda *args, **kwargs: 0.0
    prop.overall_drawdown_floor = lambda *args, **kwargs: 0.0
    prop.overall_high_watermark_value = lambda *args, **kwargs: 0.0
    prop.target_satisfied = lambda *args, **kwargs: False
    payoff = types.ModuleType("shared.live_payoff_contract")
    payoff.LIVE_PAYOFF_CONTRACT_VERSION = "quantara_v3_allocator_frozen_live_payoff_v1"
    payoff.validate_live_payoff_edge = lambda edge: dict(edge)
    payoff.verify_frozen_live_afml = lambda *args, **kwargs: {}
    sys.modules["shared"] = shared
    sys.modules["shared.prop_account_rules"] = prop
    sys.modules["shared.live_payoff_contract"] = payoff

    common = types.ModuleType("common")
    common.ensure_dirs = lambda: None
    common.init_mt5 = lambda: (_ for _ in ()).throw(RuntimeError("validation must not connect to MT5"))
    common.load_live_manifest = lambda: {}
    common.read_json = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    common.setup_logger = lambda *args, **kwargs: None
    common.shutdown_mt5 = lambda *args, **kwargs: None
    common.write_json = lambda path, payload: Path(path).write_text(json.dumps(payload), encoding="utf-8")
    sys.modules["common"] = common


def _load_execution_module(root: Path) -> Any:
    _install_execution_import_stubs(root)
    name = "portfolio_execution_bot_validation"
    spec = importlib.util.spec_from_file_location(name, THIS_DIR / "portfolio_execution_bot.py")
    _assert(spec is not None and spec.loader is not None, "Unable to load execution bot spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _test_closed_trade_ledger(root: Path) -> dict[str, Any]:
    from portfolio_closed_trade_ledger import reconcile_closed_trades

    root.mkdir(parents=True, exist_ok=True)
    bars = root / "bars"
    bars.mkdir(parents=True, exist_ok=True)
    now = __import__("pandas").Timestamp.now(tz="UTC")
    frame = __import__("pandas").DataFrame([
        {"Datetime": now - __import__("pandas").Timedelta(minutes=2), "Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.5, "Executable": True, "Gap_Healed": False},
        {"Datetime": now - __import__("pandas").Timedelta(minutes=1), "Open": 100.5, "High": 103.0, "Low": 100.0, "Close": 102.0, "Executable": True, "Gap_Healed": False},
    ])
    frame.to_csv(bars / "TEST_live_standard_quote_tick_bars.csv", index=False)

    entry = SimpleNamespace(ticket=1, order=11, position_id=100, time_msc=int((now - __import__("pandas").Timedelta(minutes=2)).timestamp() * 1000), time=0, type=0, entry=0, magic=1, reason=0, volume=0.1, price=100.0, commission=-1.0, swap=0.0, profit=0.0, fee=0.0, symbol="TEST", comment="entry", external_id="")
    exit_deal = SimpleNamespace(ticket=2, order=12, position_id=100, time_msc=int((now - __import__("pandas").Timedelta(minutes=1)).timestamp() * 1000), time=0, type=1, entry=1, magic=1, reason=2, volume=0.1, price=102.0, commission=-1.0, swap=0.0, profit=20.0, fee=0.0, symbol="TEST", comment="tp", external_id="")

    class FakeMT5:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 2
        DEAL_ENTRY_INOUT = 3
        DEAL_REASON_SL = 1
        DEAL_REASON_TP = 2
        def history_deals_get(self, start: Any = None, end: Any = None, **kwargs: Any) -> list[Any]:
            if kwargs:
                return []
            return [entry, exit_deal]
        def positions_get(self) -> list[Any]:
            return []

    state = {
        "open_position_metadata_by_ticket": {
            "100": {
                "signal_id": "SIG", "edge_id": "TEST/trend_long", "asset": "TEST",
                "symbol": "TEST", "family": "trend", "side_name": "long",
                "side_value": 1, "entry_price": 100.0, "stop_loss": 99.0,
                "take_profit": 102.0, "actual_risk_usd_at_entry": 10.0,
                "event_time": (now - __import__("pandas").Timedelta(minutes=2)).isoformat(),
            }
        }
    }
    written: list[dict[str, Any]] = []
    def append_rows(path: Path, rows: list[dict[str, Any]], dedup: list[str] | None = None) -> None:
        written.extend(rows)

    result = reconcile_closed_trades(
        mt5=FakeMT5(), state=state, live_bars_dir=bars, append_rows=append_rows,
        csv_path=root / "closed.csv", logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        minimum_interval_seconds=1,
    )
    _assert(result["positions_written"] == 1 and len(written) == 1, f"Closed ledger failed: {result}")
    row = written[0]
    _assert(abs(float(row["net_profit"]) - 18.0) < 1e-9, f"Unexpected net profit: {row}")
    _assert(abs(float(row["realized_r"]) - 1.8) < 1e-9, f"Unexpected realised R: {row}")
    _assert(row["exit_reason"] == "take_profit", f"Unexpected exit reason: {row}")

    partial_entry = SimpleNamespace(
        **{
            **entry.__dict__,
            "ticket": 21,
            "order": 201,
            "position_id": 200,
        }
    )
    partial_exit = SimpleNamespace(
        **{
            **exit_deal.__dict__,
            "ticket": 22,
            "order": 202,
            "position_id": 200,
        }
    )
    unresolved_exit = SimpleNamespace(
        **{
            **exit_deal.__dict__,
            "ticket": 42,
            "order": 402,
            "position_id": 400,
        }
    )

    class PartialHistoryMT5(FakeMT5):
        def __init__(self) -> None:
            self.targeted_position_ids: list[int] = []

        def history_deals_get(self, start: Any = None, end: Any = None, **kwargs: Any) -> list[Any]:
            if "position" not in kwargs:
                return [partial_exit, unresolved_exit]
            position_id = int(kwargs["position"])
            self.targeted_position_ids.append(position_id)
            if position_id == 200:
                return [partial_entry, partial_exit]
            if position_id == 400:
                return [unresolved_exit]
            return []

    def position_metadata(position_id: int) -> dict[str, Any]:
        return {
            "signal_id": f"SIG{position_id}",
            "edge_id": "TEST/trend_long",
            "asset": "TEST",
            "symbol": "TEST",
            "family": "trend",
            "side_name": "long",
            "side_value": 1,
            "entry_price": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "actual_risk_usd_at_entry": 10.0,
            "event_time": (now - __import__("pandas").Timedelta(minutes=2)).isoformat(),
        }

    partial_state = {
        "open_position_metadata_by_ticket": {
            str(position_id): position_metadata(position_id)
            for position_id in (200, 300, 400)
        },
        "closed_trade_ledger": {"processed_position_ids": ["300"]},
    }
    partial_written: list[dict[str, Any]] = []
    partial_mt5 = PartialHistoryMT5()
    partial_result = reconcile_closed_trades(
        mt5=partial_mt5,
        state=partial_state,
        live_bars_dir=bars,
        append_rows=lambda path, rows, dedup=None: partial_written.extend(rows),
        csv_path=root / "partial.csv",
        logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        minimum_interval_seconds=1,
    )
    _assert(
        [row["position_id"] for row in partial_written] == ["200"],
        f"Incomplete broad history was not recovered: {partial_result}",
    )
    _assert(
        partial_result["targeted_history_query_position_ids"] == ["200", "400"],
        f"Unexpected targeted history queries: {partial_result}",
    )
    _assert(
        partial_mt5.targeted_position_ids == [200, 400],
        f"Processed position was unnecessarily queried: {partial_mt5.targeted_position_ids}",
    )
    _assert(
        partial_result["status"] == "FAIL_UNEXPLAINED_EXECUTED_POSITIONS"
        and partial_result["unexplained_executed_position_ids"] == ["400"],
        f"Genuinely incomplete targeted history did not remain fail-closed: {partial_result}",
    )

    missing_state = {
        "terminal_signal_ids": {
            "MISSING": {"status": "executed"},
        },
        "open_position_metadata_by_signal_id": {
            "MISSING": {"signal_id": "MISSING", "edge_id": "TEST/missing"},
        },
    }
    missing_result = reconcile_closed_trades(
        mt5=FakeMT5(), state=missing_state, live_bars_dir=bars,
        append_rows=lambda *args, **kwargs: None,
        csv_path=root / "missing.csv",
        logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ),
        minimum_interval_seconds=1,
    )
    _assert(
        missing_result["status"] == "FAIL_UNEXPLAINED_EXECUTED_POSITIONS",
        f"Missing executed signal incorrectly passed: {missing_result}",
    )
    _assert(
        missing_result["unresolved_executed_signal_ids"] == ["MISSING"],
        f"Missing executed signal was not identified: {missing_result}",
    )
    return {
        "positions_written": 1,
        "net_profit": row["net_profit"],
        "realized_r": row["realized_r"],
        "exit_reason": row["exit_reason"],
        "partial_history_recovered_position_ids": [
            row["position_id"] for row in partial_written
        ],
        "processed_position_targeted_query_count": 0,
        "genuinely_incomplete_history_status": partial_result["status"],
        "missing_execution_invariant_status": missing_result["status"],
    }


def _test_execution_append_only_and_retry(root: Path) -> dict[str, Any]:
    bot = _load_execution_module(root)
    import broker_clock_contract as broker_clock
    out = root / "execution"
    out.mkdir(parents=True, exist_ok=True)
    bot.EXECUTION_AUDIT_CSV = out / "audit.csv"
    bot.EXECUTION_AUDIT_JSONL = out / "audit.jsonl"
    bot.REJECTED_SIGNALS_CSV = out / "rejected.csv"
    bot.REJECTED_SIGNALS_JSONL = out / "rejected.jsonl"
    bot.EXECUTED_TRADES_CSV = out / "executed.csv"
    bot.EXECUTED_TRADES_JSONL = out / "executed.jsonl"
    bot.CLOSED_TRADES_CSV = out / "closed.csv"
    bot.CLOSED_TRADES_JSONL = out / "closed.jsonl"
    bot._APPEND_ONLY_HEADERS.clear()
    bot._APPEND_ONLY_DEDUP_KEYS.clear()
    bot._RUN_APPEND_ROW_COUNTS.clear()
    bot._RUN_APPEND_SIGNAL_IDS.clear()

    row = {"signal_id": "S1", "reason": "ok", "value": 1}
    bot.append_rows_csv(bot.EXECUTED_TRADES_CSV, [row], dedup_cols=["signal_id"])
    bot.append_rows_csv(bot.EXECUTED_TRADES_CSV, [dict(row)], dedup_cols=["signal_id"])
    jsonl_rows = [json.loads(line) for line in bot.EXECUTED_TRADES_JSONL.read_text(encoding="utf-8").splitlines() if line.strip()]
    csv_rows = list(csv.DictReader(bot.EXECUTED_TRADES_CSV.open("r", encoding="utf-8")))
    _assert(len(jsonl_rows) == 1 and len(csv_rows) == 1, "Append-only dedup failed")

    state = bot.initial_state()
    old_event = (bot.utc_now_ts() - __import__("pandas").Timedelta(seconds=bot.MAX_SIGNAL_AGE_SECONDS + 5)).isoformat()
    signal = {"signal_id": "STALE1", "event_time": old_event, "edge_id": "A/E", "asset": "A"}
    audit = {"created_at": bot.utc_now(), "signal_id": "STALE1", "stage": "plan", "status": "transient_wait", "reason": "spread_too_high"}
    result = bot.write_transient_wait_if_due(
        state=state,
        signal=signal,
        stage="plan",
        reason="spread_too_high:999",
        row=audit,
    )
    _assert(result.get("expired") is True, f"Expired wait was not terminalised: {result}")
    _assert(bot.is_terminal(state, "STALE1"), "Expired wait did not mark terminal")
    _assert("STALE1" not in state["transient_plan_retry_by_signal"], "Expired retry was not cleared")
    state["run_seen_candidate_signal_ids"] = ["STALE1"]
    output_parity = bot.execution_output_parity(state)
    _assert(output_parity.get("parity_passed") is True, output_parity)
    market_mt5 = SimpleNamespace(TRADE_RETCODE_MARKET_CLOSED=10018)
    market_result = SimpleNamespace(retcode=10018, comment="Market closed")
    _assert(bot.is_market_closed_result(market_mt5, market_result), "Market-closed result not detected")
    cooldown = bot.record_market_closed_cooldown(
        state, symbol="TEST", result=market_result
    )
    _assert("TEST" in state["market_closed_cooldown_by_symbol"], "Market cooldown not stored")

    armed, blockers = bot.apply_operational_arming_blockers(
        True,
        [],
        release_identity={
            "immutable_git_release": False,
            "provenance_status": "UNVERIFIED",
        },
        state={
            "execution_state_integrity_blocked": True,
            "closed_trade_ledger_integrity_blocked": False,
        },
    )
    _assert(
        armed is False,
        "Execution-state integrity blocker did not fail closed",
    )
    _assert(
        not any(
            "immutable_git_release_required" in value
            for value in blockers
        ),
        f"Git/release provenance incorrectly remained an execution blocker: {blockers}",
    )
    _assert(
        "execution_state_integrity_operator_recovery_required"
        in blockers,
        blockers,
    )

    git_only_armed, git_only_blockers = (
        bot.apply_operational_arming_blockers(
            True,
            [
                "immutable_git_release_required:"
                "LEGACY_DIRTY_RELEASE"
            ],
            release_identity={
                "immutable_git_release": False,
                "provenance_status": "UNVERIFIED_OR_DIRTY_RELEASE",
            },
            state={
                "execution_state_integrity_blocked": False,
                "closed_trade_ledger_integrity_blocked": False,
            },
        )
    )
    _assert(
        git_only_armed is True,
        f"Git/release provenance incorrectly disarmed execution: {git_only_blockers}",
    )
    _assert(
        git_only_blockers == [],
        f"Legacy Git/release blocker was not retired: {git_only_blockers}",
    )

    original_token = bot.TELEGRAM_TOKEN
    bot.TELEGRAM_TOKEN = "123456:SECRET_TOKEN"
    try:
        redacted = bot.redact_sensitive_text(
            "https://api.telegram.org/bot123456:SECRET_TOKEN/sendMessage"
        )
    finally:
        bot.TELEGRAM_TOKEN = original_token
    _assert("SECRET_TOKEN" not in redacted, f"Telegram token was not redacted: {redacted}")
    fixed_now = __import__("pandas").Timestamp("2026-08-12T08:00:00Z")
    contract_path = root / "account_broker_clock_contract.json"
    samples = [
        broker_clock.infer_sample(
            asset="TEST",
            symbol="TEST",
            raw_timestamp=fixed_now + __import__("pandas").Timedelta(hours=3),
            observed_at=fixed_now,
        )
    ]
    clock_contract = broker_clock.build_contract(
        offset_seconds=10800,
        source="validator",
        selected_symbols=["TEST"],
        accepted_samples=samples,
        consensus_status="SINGLE_ACTIVE_SYMBOL",
        published_at=fixed_now,
        measurement_time=fixed_now,
    )
    broker_clock.atomic_write_contract(contract_path, clock_contract)
    bot.BROKER_CLOCK_CONTRACT_JSON = contract_path
    bot.utc_now_ts = lambda: fixed_now
    fake_mt5 = SimpleNamespace()
    fake_info = SimpleNamespace(trade_mode=1)
    clock_state: dict[str, Any] = {}
    ok, reason = bot.market_entry_guard(
        fake_mt5,
        asset="TEST",
        symbol="TEST",
        side_value=1,
        info=fake_info,
        tick=SimpleNamespace(
            time=int((fixed_now + __import__("pandas").Timedelta(hours=3)).timestamp()),
            time_msc=0,
        ),
        state=clock_state,
    )
    _assert(ok and reason == "market_entry_guard_pass", f"UTC+3 valid tick blocked: {reason}")
    future_ok, future_reason = bot.market_entry_guard(
        fake_mt5, asset="TEST", symbol="TEST", side_value=1, info=fake_info,
        tick=SimpleNamespace(
            time=int((fixed_now + __import__("pandas").Timedelta(hours=3, seconds=60)).timestamp()),
            time_msc=0,
        ), state=clock_state,
    )
    stale_ok, stale_reason = bot.market_entry_guard(
        fake_mt5, asset="TEST", symbol="TEST", side_value=1, info=fake_info,
        tick=SimpleNamespace(
            time=int((fixed_now + __import__("pandas").Timedelta(hours=3, seconds=-60)).timestamp()),
            time_msc=0,
        ), state=clock_state,
    )
    _assert(not future_ok and future_reason.startswith("future_broker_tick_normalised"), future_reason)
    _assert(not stale_ok and stale_reason.startswith("stale_broker_tick_normalised"), stale_reason)
    _assert(
        clock_state["last_broker_clock_diagnostic"]["reason_class"]
        == broker_clock.STALE_TICK,
        clock_state,
    )
    contract_path.unlink()
    missing_ok, _missing_reason = bot.market_entry_guard(
        fake_mt5,
        asset="TEST",
        symbol="TEST",
        side_value=1,
        info=fake_info,
        tick=SimpleNamespace(time=int(fixed_now.timestamp()), time_msc=0),
        state=clock_state,
    )
    _assert(not missing_ok, "Missing broker-clock contract did not fail closed")
    _assert(
        clock_state["last_broker_clock_diagnostic"]["reason_class"]
        == broker_clock.CLOCK_CONTRACT_RETRY,
        clock_state,
    )
    invalid_contract = {**clock_contract, "locked": False}
    invalid_contract["generation_id"] = broker_clock._generation_id(invalid_contract)
    broker_clock.atomic_write_contract(contract_path, invalid_contract)
    invalid_ok, _invalid_reason = bot.market_entry_guard(
        fake_mt5,
        asset="TEST",
        symbol="TEST",
        side_value=1,
        info=fake_info,
        tick=SimpleNamespace(time=int(fixed_now.timestamp()), time_msc=0),
        state=clock_state,
    )
    _assert(not invalid_ok, "Unlocked broker-clock contract did not fail closed")
    _assert(
        clock_state["last_broker_clock_diagnostic"]["reason_class"]
        == broker_clock.CLOCK_CONTRACT_INVALID,
        clock_state,
    )
    return {
        "append_only_jsonl_rows": len(jsonl_rows),
        "append_only_csv_rows": len(csv_rows),
        "expired_retry_terminal_reason": result.get("terminal_reason"),
        "market_closed_cooldown_until": cooldown.get("until"),
        "operational_arming_blockers": blockers,
        "telegram_token_redacted": True,
        "utc_plus_three_tick_normalised": True,
        "genuine_future_tick_blocked": future_reason,
        "genuine_stale_tick_blocked": stale_reason,
        "missing_contract_reason_class": broker_clock.CLOCK_CONTRACT_RETRY,
        "unlocked_contract_reason_class": broker_clock.CLOCK_CONTRACT_INVALID,
        "audit_rejected_status_parity": output_parity,
    }



def _test_production_requirements_and_proof_contract() -> dict[str, Any]:
    requirements = THIS_DIR / "requirements_production_observability.txt"
    text = requirements.read_text(encoding="utf-8")
    _assert("psutil" in text.lower(), "Production observability requirements omit psutil")
    p0_text = (THIS_DIR / "production_readiness_p0.py").read_text(encoding="utf-8")
    _assert("negative_cache_until" in p0_text, "Negative feature-proof caching is absent")
    _assert("canonical_alias_proofs" in p0_text, "Canonical semantic alias proof store is absent")
    _assert("threading.get_ident()" in p0_text, "Proof temp files are not writer-unique")
    return {"psutil_requirement": True, "negative_proof_cache": True, "canonical_alias_proofs": True}


def _configure_validation_temp_root() -> Path:
    """Own a short temporary root so Windows path length cannot break this validator.

    This is process-local. It does not modify persistent/system environment
    settings. Child validation processes inherit the short TEMP/TMP values.
    QUANTARA_VALIDATION_TEMP_ROOT is an optional host-specific override.
    """
    override = str(os.environ.get("QUANTARA_VALIDATION_TEMP_ROOT") or "").strip()
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        drive = str(os.environ.get("SystemDrive") or "").strip()
        if not drive:
            drive = Path(tempfile.gettempdir()).drive or "C:"
        root = Path(drive + "\\") / "QTmp"
    else:
        root = Path(tempfile.gettempdir())

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f".quantara_validation_probe_{os.getpid()}.tmp"
    try:
        probe.write_text("ok\n", encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(
            f"Hardening validator cannot use short temp root {root}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass

    os.environ["TEMP"] = str(root)
    os.environ["TMP"] = str(root)
    # tempfile caches the resolved temp directory; clear that cache after the
    # process-local environment is changed so all later calls use this root.
    tempfile.tempdir = None
    return root


def main() -> int:
    validation_temp_root = _configure_validation_temp_root()
    results: dict[str, Any] = {}
    results["import_path_bootstrap"] = _test_import_path_bootstrap()
    results["static_global_resolution"] = _test_static_global_resolution()
    results["p0"] = _test_p0_self_test()
    results["snapshot_pipeline"] = _test_snapshot_pipeline_self_test()
    results["broker_clock_contract"] = _test_broker_clock_contract()
    results["live_policy_authority"] = _test_live_policy_authority_contract()
    results["hmm_integrity"] = _test_hmm_integrity_contract()
    results["semaphore"] = _test_observed_semaphore()
    results["requirements_and_proofs"] = _test_production_requirements_and_proof_contract()
    with tempfile.TemporaryDirectory(prefix="qv_", dir=str(validation_temp_root)) as tmp:
        root = Path(tmp)
        results["release_and_temp"] = _test_release_identity_and_temp_recovery(root)
        results["state_integrity"] = _test_state_integrity_recovery(root / "state_integrity_test")
        results["feature_recovery_transaction"] = _test_feature_recovery_transaction(root / "feature_recovery_test")
        results["hmm_dependency_capture"] = _test_hmm_dependency_capture_forwarding(root / "hmm_capture_test")
        results["closed_trade_ledger"] = _test_closed_trade_ledger(root / "closed_trade_test")
        results["semantic_cache"] = _test_semantic_negative_and_canonical_cache(root / "semantic_cache_test")
        results["execution"] = _test_execution_append_only_and_retry(root / "execution_test")
    payload = {"status": "PASS", "validation_temp_root": str(validation_temp_root), "tests": results}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
