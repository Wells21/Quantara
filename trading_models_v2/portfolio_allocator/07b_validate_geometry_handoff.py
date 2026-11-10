"""Attach and verify finalized trade geometry on Step 06/07 row-level outputs.

This post-validation step does not recompute return streams, correlations, linkage,
HRP weights, robustness scores, or Step 08 eligibility. It enriches only explicitly
allowlisted row-level metadata/weight files and never rewrites matrix-shaped CSVs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for p in (V2_ROOT, REPO_ROOT):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from shared.edges.trade_geometry import GEOMETRY_COLUMNS, geometry_from_mapping  # noqa: E402

SCHEMA_VERSION = "v2.2"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "outputs"
ACTIVE_UNIVERSE = OUTPUT_DIR / "active_edge_universe.json"
RETURN_STREAMS_DIR = OUTPUT_DIR / "return_streams"
HRP_DIR = OUTPUT_DIR / "hrp_linkage_robustness_outputs"
STEP08_WEIGHTS = HRP_DIR / "hrp_linkage_weights_step08_primary.csv"
MANIFEST_PATH = HRP_DIR / "geometry_handoff_manifest.json"

RETURN_STREAM_ROW_LEVEL_EXACT = [
    "profile_representative_selection.csv",
    "candidate_return_streams_diagnostics_all_universes.csv",
    "profile_return_streams_diagnostics_all.csv",
]
RETURN_STREAM_ROW_LEVEL_PATTERNS = [
    "candidate_return_streams_diagnostics_*.csv",
    "profile_return_streams_diagnostics_*.csv",
]
HRP_ROW_LEVEL_EXACT = [
    "hrp_linkage_weights_all.csv",
    "hrp_linkage_weights_step08_primary.csv",
]
HRP_ROW_LEVEL_PATTERNS = [
    "hrp_weights_*.csv",
]

MATRIX_PREFIXES = (
    "candidate_return_streams_daily_",
    "candidate_return_streams_corr_",
    "candidate_return_streams_cov_",
    "profile_return_streams_daily_",
    "profile_return_streams_corr_",
    "profile_return_streams_cov_",
    "hrp_corr_",
    "hrp_cov_",
    "hrp_distance_",
    "hrp_linkage_raw_r_",
    "hrp_linkage_afml_weighted_r_",
    "hrp_portfolio_returns_",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def load_geometry_map() -> pd.DataFrame:
    require(ACTIVE_UNIVERSE.exists(), f"Missing active edge universe: {ACTIVE_UNIVERSE}")
    payload = json.loads(ACTIVE_UNIVERSE.read_text(encoding="utf-8"))
    require(payload.get("ingestion_status") == "PASS", "active_edge_universe ingestion_status must be PASS")
    rows: list[dict[str, Any]] = []
    for edge in payload.get("active_edges", []):
        geometry = geometry_from_mapping(edge, expected_side_name=str(edge.get("side_name", "")))
        rows.append({"edge_id": str(edge["edge_id"]), **geometry})
    require(rows, "active edge universe contains no geometry rows")
    df = pd.DataFrame(rows)
    require(not df["edge_id"].duplicated().any(), "active edge universe contains duplicate edge_id values")
    return df


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".geometry_tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def enrich_csv(path: Path, geometry_map: pd.DataFrame, *, write: bool) -> dict[str, Any] | None:
    try:
        header = pd.read_csv(path, nrows=0)
    except Exception:
        return None
    if "edge_id" not in header.columns:
        return None

    df = pd.read_csv(path)
    if df.empty:
        return {"file": str(path.relative_to(V2_ROOT)), "rows": 0, "status": "empty"}

    geometry_lookup = geometry_map.set_index("edge_id")["geometry_hash"]
    if "geometry_hash" in df.columns:
        existing = df["geometry_hash"].astype(str).str.strip()
        expected = df["edge_id"].astype(str).map(geometry_lookup).astype(str)
        populated = existing.ne("") & existing.str.lower().ne("nan")
        mismatch = populated & existing.ne(expected)
        require(not mismatch.any(), f"{path.name}: {int(mismatch.sum())} existing geometry hashes conflict with active universe")

    out = df.drop(columns=[c for c in GEOMETRY_COLUMNS if c in df.columns], errors="ignore")
    out = out.merge(geometry_map, on="edge_id", how="left", validate="many_to_one", sort=False)
    missing = out["geometry_hash"].isna()
    require(not missing.any(), f"{path.name}: {int(missing.sum())} rows have no finalized geometry mapping")

    conflicts = out.groupby("edge_id")["geometry_hash"].nunique(dropna=False)
    require(bool((conflicts == 1).all()), f"{path.name}: multiple geometry hashes found for edge(s): {conflicts[conflicts != 1].to_dict()}")
    if write:
        atomic_write_csv(out, path)
    return {
        "file": str(path.relative_to(V2_ROOT)),
        "rows": int(len(out)),
        "edges": int(out["edge_id"].nunique()),
        "status": "PASS",
    }


def add_exact_files(paths: set[Path], directory: Path, names: list[str]) -> None:
    for name in names:
        path = directory / name
        if path.exists() and path.is_file():
            paths.add(path)


def add_pattern_files(paths: set[Path], directory: Path, patterns: list[str]) -> None:
    if not directory.exists():
        return
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path.is_file():
                paths.add(path)


def row_level_candidate_files() -> list[Path]:
    paths: set[Path] = set()
    add_exact_files(paths, RETURN_STREAMS_DIR, RETURN_STREAM_ROW_LEVEL_EXACT)
    add_pattern_files(paths, RETURN_STREAMS_DIR, RETURN_STREAM_ROW_LEVEL_PATTERNS)
    add_exact_files(paths, HRP_DIR, HRP_ROW_LEVEL_EXACT)
    add_pattern_files(paths, HRP_DIR, HRP_ROW_LEVEL_PATTERNS)
    return sorted(paths)


def matrix_files_present() -> list[Path]:
    paths: list[Path] = []
    for directory in (RETURN_STREAMS_DIR, HRP_DIR):
        if not directory.exists():
            continue
        for path in directory.glob("*.csv"):
            if path.name.startswith(MATRIX_PREFIXES):
                paths.append(path)
    return sorted(paths)


def validate_step08_weights(path: Path) -> dict[str, Any]:
    require(path.exists(), f"Missing Step 08 primary HRP weights: {path}")
    df = pd.read_csv(path)
    require(not df.empty, f"Step 08 primary HRP weights are empty: {path}")
    missing_columns = [c for c in ["edge_id", *GEOMETRY_COLUMNS] if c not in df.columns]
    require(not missing_columns, f"Step 08 primary weights missing geometry columns after handoff: {missing_columns}")
    missing_values = df[list(GEOMETRY_COLUMNS)].isna().any(axis=1)
    require(not missing_values.any(), f"Step 08 primary weights contain {int(missing_values.sum())} rows with missing geometry values")
    conflicts = df.groupby("edge_id")["geometry_hash"].nunique(dropna=False)
    require(bool((conflicts == 1).all()), f"Step 08 primary weights contain geometry conflicts: {conflicts[conflicts != 1].to_dict()}")
    return {
        "rows": int(len(df)),
        "edges": int(df["edge_id"].nunique()),
        "geometry_missing_rows": int(missing_values.sum()),
        "geometry_conflict_edges": int((conflicts != 1).sum()),
    }


def build(write: bool = True) -> dict[str, Any]:
    geometry_map = load_geometry_map()
    targets = row_level_candidate_files()
    require(STEP08_WEIGHTS in targets, "07B internal allowlist error: Step 08 primary weights are not targeted")

    matrix_paths = matrix_files_present()
    target_set = set(targets)
    overlap = sorted(target_set.intersection(matrix_paths))
    require(not overlap, f"07B row-level allowlist overlaps matrix artifacts: {[str(p) for p in overlap]}")

    results: list[dict[str, Any]] = []
    no_edge_id_files: list[str] = []
    for path in targets:
        result = enrich_csv(path, geometry_map, write=write)
        if result is None:
            no_edge_id_files.append(str(path.relative_to(V2_ROOT)))
        else:
            results.append(result)

    step08_validation = validate_step08_weights(STEP08_WEIGHTS)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now_utc(),
        "geometry_handoff_status": "PASS",
        "geometry_contract_version": "trade_geometry_v1",
        "active_geometry_edges": int(len(geometry_map)),
        "row_level_files_targeted": int(len(targets)),
        "row_level_files_enriched": int(len(results)),
        "targeted_files_without_edge_id_skipped": int(len(no_edge_id_files)),
        "matrix_files_detected_and_excluded": int(len(matrix_paths)),
        "matrix_files_modified": 0,
        "step08_primary_weights": str(STEP08_WEIGHTS.relative_to(V2_ROOT)),
        "step08_geometry_columns_present": True,
        "step08_validation": step08_validation,
        "files": results,
        "targeted_files_without_edge_id": no_edge_id_files,
        "excluded_matrix_files": [str(path.relative_to(V2_ROOT)) for path in matrix_paths],
        "notes": [
            "No returns, correlations, covariance matrices, distance matrices, linkage matrices, HRP calculations, or robustness scores are recalculated.",
            "Only explicitly allowlisted row-level diagnostics and weight files are eligible for enrichment.",
            "Matrix-shaped CSVs are detected by their known output prefixes and are fail-closed excluded from all writes.",
            "geometry_hash is retained as a semantic geometry fingerprint; no artifact-file hash is consumed.",
        ],
    }
    if write:
        write_json(MANIFEST_PATH, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    manifest = build(write=not args.dry_run)
    print("V2 Phase B2.07B finalized trade geometry handoff")
    print(f"Status: {manifest['geometry_handoff_status']}")
    print(f"Active geometry edges: {manifest['active_geometry_edges']}")
    print(f"Row-level files enriched: {manifest['row_level_files_enriched']}")
    print(f"Matrix files excluded: {manifest['matrix_files_detected_and_excluded']}")
    print(f"Matrix files modified: {manifest['matrix_files_modified']}")
    print(f"Step08 geometry ready: {manifest['step08_geometry_columns_present']}")


if __name__ == "__main__":
    main()
