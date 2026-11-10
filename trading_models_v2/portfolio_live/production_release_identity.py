from __future__ import annotations

"""Immutable release identity and stale-temp recovery for V2 live deployment.

This module records what is running. It does not alter trading decisions or make
artifact hashes an execution authority. The release identifier is operational
provenance and forensic evidence only.
"""

import csv
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RELEASE_IDENTITY_VERSION = "2026-08-11_v2_release_identity_v5"

# These directories contain process-owned state, journals, bars, logs, and
# append-only outputs. They are intentionally mutable after deployment and are
# not deployable release inputs. Critical source/config files remain subject to
# the Git cleanliness and tracking checks below.
_MUTABLE_RUNTIME_DIRECTORY_NAMES = (
    "live_state",
    "live_bars",
    "live_logs",
    "outputs",
)

# The dashboard causally refreshes this tracked operational policy from
# completed live sessions.  It remains a critical, tracked, hashed input, but
# its expected data-only updates must not make the deployed source tree dirty.
# Do not broaden this to the config directory: every other configuration file
# remains subject to the fail-closed Git cleanliness check.
_MUTABLE_RUNTIME_FILE_RELATIVE_PATHS = (
    Path("portfolio_live") / "config" / "live_bar_policy.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _git_value(repo_root: Path, args: Sequence[str]) -> str | None:
    env_value = None
    if list(args) == ["rev-parse", "HEAD"]:
        env_value = os.environ.get("TRADING_MODELS_RELEASE_COMMIT")
    elif list(args) == ["rev-parse", "--abbrev-ref", "HEAD"]:
        env_value = os.environ.get("TRADING_MODELS_RELEASE_BRANCH")
    if env_value:
        return str(env_value).strip() or None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return completed.stdout.strip() or None
    except Exception:
        return None


def _canonical_release_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repo_relative_git_path(repo_root: Path, path: Path) -> str | None:
    try:
        relative = Path(path).resolve().relative_to(Path(repo_root).resolve())
    except Exception:
        return None
    if not relative.parts:
        return None
    return relative.as_posix()


def _git_dirty(
    repo_root: Path,
    *,
    excluded_roots: Iterable[Path] = (),
    excluded_files: Iterable[Path] = (),
) -> bool | None:
    env_value = os.environ.get("TRADING_MODELS_RELEASE_CLEAN")
    if env_value is not None:
        return str(env_value).strip().lower() not in {"1", "true", "yes", "clean"}

    # Use Git pathspec exclusions instead of ignoring the whole working tree.
    # This permits tracked runtime state to evolve without hiding source,
    # manifest, model, or critical-configuration modifications.
    pathspecs = ["."]
    for root in excluded_roots:
        relative = _repo_relative_git_path(repo_root, Path(root))
        if relative:
            pathspecs.append(f":(exclude){relative}/**")
    for path in excluded_files:
        relative = _repo_relative_git_path(repo_root, Path(path))
        if relative:
            pathspecs.append(f":(exclude){relative}")
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=no",
                "--",
                *pathspecs,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return bool(completed.stdout.strip())
    except Exception:
        return None


def _git_tracked(repo_root: Path, path: Path) -> bool | None:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except Exception:
        return None
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", relative.as_posix()],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
    except Exception:
        return None


def _existing_paths_from_manifest(
    *,
    manifest: Mapping[str, Any],
    v2_root: Path,
) -> list[Path]:
    extensions = {".joblib", ".pkl", ".json", ".yaml", ".yml", ".csv"}
    hints = (
        "model",
        "calibrator",
        "selected_feature",
        "feature_schema",
        "threshold",
        "manifest",
        "edge_yaml",
        "contract",
    )
    found: set[Path] = set()

    def visit(value: Any, key_path: str = "") -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                visit(child, f"{key_path}.{key}" if key_path else str(key))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{key_path}[{index}]")
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        lower_key = key_path.lower()
        candidate = Path(text)
        if candidate.suffix.lower() not in extensions and not any(
            token in lower_key for token in hints
        ):
            return
        attempts = [candidate] if candidate.is_absolute() else [v2_root / candidate, v2_root.parent / candidate]
        for resolved in attempts:
            try:
                if resolved.exists() and resolved.is_file():
                    found.add(resolved.resolve())
                    return
            except Exception:
                continue

    visit(manifest)
    return sorted(found)


def build_release_identity(
    *,
    process_name: str,
    repo_root: Path,
    v2_root: Path,
    output_dir: Path,
    manifest: Mapping[str, Any] | None = None,
    critical_files: Iterable[Path] = (),
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    v2_root = Path(v2_root).resolve()
    output_dir = Path(output_dir)
    critical_files = tuple(critical_files)
    paths: set[Path] = set()
    for value in critical_files:
        path = Path(value)
        try:
            if path.exists() and path.is_file():
                paths.add(path.resolve())
        except Exception:
            continue
    if manifest:
        paths.update(
            _existing_paths_from_manifest(manifest=manifest, v2_root=v2_root)
        )

    file_records: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            relative = path.relative_to(v2_root).as_posix()
        except Exception:
            try:
                relative = path.relative_to(repo_root).as_posix()
            except Exception:
                relative = str(path)
        try:
            file_records.append(
                {
                    "path": relative,
                    "sha256": sha256_file(path),
                    "size_bytes": int(path.stat().st_size),
                }
            )
        except Exception as exc:
            file_records.append(
                {
                    "path": relative,
                    "error": f"{type(exc).__name__}:{exc}",
                }
            )

    source_provenance_path = v2_root / "portfolio_live" / "config" / "production_patch_source.json"
    source_provenance: dict[str, Any] | None = None
    if source_provenance_path.exists():
        try:
            loaded = json.loads(source_provenance_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                source_provenance = loaded
                if source_provenance_path.resolve() not in paths:
                    file_records.append(
                        {
                            "path": source_provenance_path.relative_to(v2_root).as_posix(),
                            "sha256": sha256_file(source_provenance_path),
                            "size_bytes": int(source_provenance_path.stat().st_size),
                        }
                    )
                    file_records = sorted(file_records, key=lambda record: str(record.get("path") or ""))
        except Exception as exc:
            source_provenance = {"error": f"{type(exc).__name__}:{exc}"}

    git_commit = _git_value(repo_root, ["rev-parse", "HEAD"])
    git_branch = _git_value(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    mutable_runtime_roots = tuple(
        v2_root / "portfolio_live" / name
        for name in _MUTABLE_RUNTIME_DIRECTORY_NAMES
    )
    mutable_runtime_files = tuple(
        v2_root / relative
        for relative in _MUTABLE_RUNTIME_FILE_RELATIVE_PATHS
    )
    mutable_runtime_git_exclusions = sorted(
        relative
        for path in (*mutable_runtime_roots, *mutable_runtime_files)
        if (relative := _repo_relative_git_path(repo_root, path))
    )
    git_dirty = _git_dirty(
        repo_root,
        excluded_roots=mutable_runtime_roots,
        excluded_files=mutable_runtime_files,
    )
    environment_attested_clean = os.environ.get("TRADING_MODELS_RELEASE_CLEAN") is not None
    explicit_critical_paths = {
        Path(value).resolve()
        for value in critical_files
        if Path(value).exists() and Path(value).is_file()
    }
    critical_git_tracking = {
        str(path): _git_tracked(repo_root, path)
        for path in sorted(explicit_critical_paths)
    }
    untracked_critical_files = sorted(
        path for path, tracked in critical_git_tracking.items() if tracked is False
    )
    tracking_unknown = any(value is None for value in critical_git_tracking.values())
    immutable_git_release = (
        bool(git_commit)
        and git_dirty is False
        and not untracked_critical_files
        and (environment_attested_clean or not tracking_unknown)
    )
    immutable_core = {
        "schema_version": RELEASE_IDENTITY_VERSION,
        "process_name": str(process_name),
        "git_commit": git_commit,
        "git_branch": git_branch,
        "git_dirty": git_dirty,
        "mutable_runtime_git_exclusions": mutable_runtime_git_exclusions,
        "untracked_critical_files": untracked_critical_files,
        "critical_file_tracking_unknown": tracking_unknown,
        "environment_attested_clean": environment_attested_clean,
        "immutable_git_release": immutable_git_release,
        "provenance_status": (
            "IMMUTABLE_GIT_RELEASE"
            if immutable_git_release
            else "UNVERIFIED_OR_DIRTY_RELEASE"
        ),
        "manual_patch_source": source_provenance,
        "files": file_records,
    }
    release_id = _canonical_release_digest(immutable_core)
    payload = {
        **immutable_core,
        "release_id": release_id,
        "created_at": utc_now(),
        "identity_role": "operational_provenance_not_execution_authority",
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    immutable_path = output_dir / f"release_{release_id}.json"
    if not immutable_path.exists():
        _atomic_write_json(immutable_path, payload)
    safe_process = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(process_name).strip().lower()
    ).strip("_") or "process"
    current_path = output_dir / f"current_{safe_process}_release_identity.json"
    payload["immutable_identity_path"] = str(immutable_path)
    payload["current_identity_path"] = str(current_path)
    _atomic_write_json(current_path, payload)
    return payload


def _infer_atomic_target(temp_path: Path) -> Path | None:
    name = temp_path.name
    if not name.startswith(".") or not name.endswith(".tmp"):
        return None
    core = name[1:-4]
    parts = core.split(".")
    # Atomic writers use .<target>.<pid>.<time_ns>.tmp. Strip the final two
    # numeric components only when they are actually numeric.
    if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit():
        target_name = ".".join(parts[:-2])
        return temp_path.with_name(target_name)
    return None


def _csv_row_digest(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recover_csv_rows(temp_path: Path, target_path: Path) -> dict[str, Any]:
    with temp_path.open("r", encoding="utf-8-sig", newline="") as handle:
        temp_rows = list(csv.DictReader(handle))
    if not temp_rows:
        return {"recovered_rows": 0, "temp_rows": 0}

    target_rows: list[dict[str, Any]] = []
    if target_path.exists():
        try:
            with target_path.open("r", encoding="utf-8-sig", newline="") as handle:
                target_rows = list(csv.DictReader(handle))
        except Exception:
            target_rows = []
    existing = {_csv_row_digest(row) for row in target_rows}
    missing = [row for row in temp_rows if _csv_row_digest(row) not in existing]
    if not missing:
        return {"recovered_rows": 0, "temp_rows": len(temp_rows)}

    columns: list[str] = []
    for row in [*target_rows, *missing]:
        for key in row:
            if key not in columns:
                columns.append(key)
    merged = [*target_rows, *missing]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_name(
        f".{target_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(merged)
        os.replace(tmp, target_path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return {"recovered_rows": len(missing), "temp_rows": len(temp_rows)}


def recover_stale_temp_files(
    *,
    roots: Iterable[Path],
    archive_root: Path,
    min_age_seconds: float = 60.0,
) -> dict[str, Any]:
    now = time.time()
    archive_root = Path(archive_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination_root = archive_root / stamp
    records: list[dict[str, Any]] = []

    candidates: set[Path] = set()
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        try:
            for path in root_path.rglob("*.tmp"):
                if path.is_file():
                    candidates.add(path)
        except Exception:
            continue

    for path in sorted(candidates):
        try:
            age = max(now - path.stat().st_mtime, 0.0)
        except Exception:
            continue
        if age < float(min_age_seconds):
            continue
        target = _infer_atomic_target(path)
        record: dict[str, Any] = {
            "temp_path": str(path),
            "age_seconds": age,
            "inferred_target": str(target) if target is not None else None,
            "status": "archived",
            "recovered_rows": 0,
        }
        if target is not None and path.suffix.lower() == ".tmp" and target.suffix.lower() == ".csv":
            try:
                result = _recover_csv_rows(path, target)
                record.update(result)
                record["status"] = "recovered_and_archived"
            except Exception as exc:
                record["recovery_error"] = f"{type(exc).__name__}:{exc}"

        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / path.name
        counter = 0
        while destination.exists():
            counter += 1
            destination = destination_root / f"{path.stem}.{counter}{path.suffix}"
        try:
            shutil.move(str(path), str(destination))
            record["archive_path"] = str(destination)
        except Exception as exc:
            record["status"] = "archive_failed"
            record["archive_error"] = f"{type(exc).__name__}:{exc}"
        records.append(record)

    payload = {
        "schema_version": RELEASE_IDENTITY_VERSION,
        "created_at": utc_now(),
        "status": "PASS" if not any(r.get("status") == "archive_failed" for r in records) else "PARTIAL",
        "scanned_roots": [str(Path(root)) for root in roots],
        "stale_temp_count": len(records),
        "recovered_row_count": sum(int(r.get("recovered_rows") or 0) for r in records),
        "records": records,
    }
    archive_root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(archive_root / "latest_stale_temp_recovery_report.json", payload)
    return payload


__all__ = [
    "RELEASE_IDENTITY_VERSION",
    "build_release_identity",
    "recover_stale_temp_files",
    "sha256_file",
]
