"""Dependency-aware MLDP transformation invalidation scanner.

Reports potentially stale artifact scopes. Never deletes files.
"""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

class MLDPInvalidationError(RuntimeError):
    pass

CHANGE_SCOPES = {
    "bar_semantics": {
        "minimum_stale_from": "events",
        "patterns": ["assets/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "candidate_rule": {
        "minimum_stale_from": "labels",
        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "event_vol": {
        "minimum_stale_from": "labels",
        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "barrier_geometry": {
        "minimum_stale_from": "labels",
        "patterns": ["assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "signal_age": {
        "minimum_stale_from": "quote_executable_label",
        "patterns": ["assets/**/edges/**/artifacts/**/*exact*execution*", "assets/**/edges/**/artifacts/**/*calibrat*", "assets/**/edges/**/artifacts/**/*threshold*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "target_layer": {
        "minimum_stale_from": "feature_importance_and_modeling",
        "patterns": ["assets/**/edges/**/artifacts/feature_surgery/**/*", "assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "cv_splitter": {
        "minimum_stale_from": "feature_selection_and_model_cv",
        "patterns": ["assets/**/edges/**/artifacts/feature_surgery/**/*", "assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "ffd": {
        "minimum_stale_from": "ffd_feature_consumers",
        "patterns": ["assets/**/ffd/**/*", "assets/**/edges/**/artifacts/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "selected_features": {
        "minimum_stale_from": "model",
        "patterns": ["assets/**/edges/**/artifacts/model/**/*", "assets/**/edges/**/artifacts/oof/**/*", "assets/**/edges/**/artifacts/calibration/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "calibration": {
        "minimum_stale_from": "calibration",
        "patterns": ["assets/**/edges/**/artifacts/calibration/**/*", "assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "sizing": {
        "minimum_stale_from": "sizing_threshold_portfolio",
        "patterns": ["assets/**/edges/**/artifacts/threshold/**/*", "portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
    "portfolio_covariance": {
        "minimum_stale_from": "portfolio_allocation",
        "patterns": ["portfolio_allocator/outputs/**/*", "portfolio_live/live_manifest*.json"],
    },
}

def scan_invalidation(root: str | Path, changes: Iterable[str]) -> dict:
    base = Path(root).resolve()
    requested = [str(c) for c in changes]
    unknown = sorted(set(requested) - set(CHANGE_SCOPES))
    if unknown:
        raise MLDPInvalidationError(
            f"Unknown change keys: {unknown}; allowed={sorted(CHANGE_SCOPES)}"
        )
    found: dict[str, list[str]] = {}
    for change in requested:
        files: set[str] = set()
        for pattern in CHANGE_SCOPES[change]["patterns"]:
            for path in base.glob(pattern):
                if path.is_file():
                    files.add(path.relative_to(base).as_posix())
        found[change] = sorted(files)
    return {
        "root": str(base),
        "changes": requested,
        "scopes": {
            c: {
                "minimum_stale_from": CHANGE_SCOPES[c]["minimum_stale_from"],
                "candidate_file_count": len(found[c]),
                "candidate_files": found[c],
            }
            for c in requested
        },
        "destructive_actions": False,
    }
