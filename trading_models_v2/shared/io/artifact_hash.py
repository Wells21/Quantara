"""
Artifact hashing utilities for V2 reproducibility contracts.

Blueprint purpose
-----------------
The live manifest must include hashes for every trusted artifact. If a model,
calibrator, selected_features file, schema, runtime file, or edge manifest changes
after live manifest export, the dashboard must fail closed until the live manifest
is regenerated.

This module is intentionally standard-library only.

Run from ``trading_models_v2``:

    python ./shared/io/artifact_hash.py

or from the repository root:

    python trading_models_v2/shared/io/artifact_hash.py
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_HASH_ALGORITHM = "sha256"
DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ArtifactHash:
    """A stable record describing one hashed artifact."""

    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


class ArtifactHashError(RuntimeError):
    """Raised when artifact hash validation must fail closed."""


def sha256_file(path: str | Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Return the SHA-256 hash for a file.

    Parameters
    ----------
    path:
        File path to hash.
    chunk_size:
        Read chunk size in bytes. Default is 1 MiB.
    """

    file_path = Path(path)
    if not file_path.exists():
        raise ArtifactHashError(f"Artifact does not exist: {file_path}")
    if not file_path.is_file():
        raise ArtifactHashError(f"Artifact path is not a file: {file_path}")

    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_artifact(path: str | Path, root: str | Path | None = None) -> ArtifactHash:
    """Hash one artifact and return a manifest-safe record.

    If ``root`` is provided, the stored path is relative to that root.
    """

    file_path = Path(path)
    digest = sha256_file(file_path)
    size = file_path.stat().st_size

    if root is not None:
        stored_path = file_path.resolve().relative_to(Path(root).resolve()).as_posix()
    else:
        stored_path = file_path.as_posix()

    return ArtifactHash(path=stored_path, sha256=digest, size_bytes=size)


def hash_artifacts(paths: Iterable[str | Path], root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Hash many artifacts into a stable path-keyed manifest table."""

    table: dict[str, dict[str, Any]] = {}
    for path in sorted((Path(p) for p in paths), key=lambda p: p.as_posix()):
        record = hash_artifact(path, root=root)
        table[record.path] = record.to_dict()
    return table


def verify_artifact_hashes(
    hash_table: Mapping[str, Mapping[str, Any]],
    root: str | Path | None = None,
) -> list[str]:
    """Verify a manifest hash table.

    Returns a list of mismatch messages. Empty list means PASS.
    This function does not silently skip missing files.
    """

    mismatches: list[str] = []
    base = Path(root) if root is not None else Path(".")

    for stored_path, record in hash_table.items():
        artifact_path = base / stored_path
        expected = str(record.get("sha256", ""))
        try:
            actual = sha256_file(artifact_path)
        except ArtifactHashError as exc:
            mismatches.append(str(exc))
            continue

        if actual != expected:
            mismatches.append(
                f"Hash mismatch for {stored_path}: expected {expected}, got {actual}"
            )

        expected_size = record.get("size_bytes")
        if expected_size is not None:
            actual_size = artifact_path.stat().st_size
            if int(expected_size) != actual_size:
                mismatches.append(
                    f"Size mismatch for {stored_path}: expected {expected_size}, got {actual_size}"
                )

    return mismatches


def write_hash_table_json(
    paths: Iterable[str | Path],
    output_path: str | Path,
    root: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Hash artifacts and write the hash table as stable JSON."""

    table = hash_artifacts(paths=paths, root=root)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return table


def _run_self_test() -> None:
    this_file = Path(__file__)
    record = hash_artifact(this_file)

    if record.path.replace("\\", "/").endswith("shared/io/artifact_hash.py") is False:
        raise SystemExit(f"❌ Unexpected hash record path: {record.path}")

    table = {record.path: record.to_dict()}
    mismatches = verify_artifact_hashes(table)
    if mismatches:
        raise SystemExit("❌ Artifact hash self-test failed:\n" + "\n".join(mismatches))

    print("✅ shared.io.artifact_hash self-test passed.")
    print(f"File: {record.path}")
    print(f"SHA256: {record.sha256}")
    print(f"Size bytes: {record.size_bytes}")


if __name__ == "__main__":
    _run_self_test()
