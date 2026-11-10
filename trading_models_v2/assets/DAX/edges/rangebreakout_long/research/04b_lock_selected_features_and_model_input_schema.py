"""Step 04B wrapper: lock selected features and model input schema.

SOTD rule: Step 04B implementation lives in shared code. This edge-local file
is intentionally a thin wrapper that derives EDGE_ROOT from its own location and
calls the shared implementation. It contains no duplicated feature-locking logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
EDGE_ROOT = THIS_FILE.parents[1]
V2_ROOT = THIS_FILE.parents[5]
REPO_ROOT = THIS_FILE.parents[6]
for path in (V2_ROOT, REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.edges.edge_layout import get_edge_layout_from_edge_root  # noqa: E402
from shared.edges.lock_selected_features_and_model_input_schema import (  # noqa: E402
    LockSelectedFeaturesError,
    lock_selected_features_and_model_input_schema,
)


def main() -> None:
    layout = get_edge_layout_from_edge_root(EDGE_ROOT)
    print("=" * 80, flush=True)
    print(f"V2 STEP 04B: LOCK SELECTED FEATURES + MODEL INPUT SCHEMA — {layout.edge_id}", flush=True)
    print("=" * 80, flush=True)
    result = lock_selected_features_and_model_input_schema(EDGE_ROOT)
    print("Selected features locked into canonical contracts.", flush=True)
    print(f"Edge: {result['edge_id']}", flush=True)
    print(f"Selected features: {result['selected_feature_count']}", flush=True)
    print(f"Canonical selected features: {result['canonical_selected_features']}", flush=True)
    print(f"Model input schema: {result['model_input_schema']}", flush=True)
    print("Next ordered step: 05_training_artifact_verifier.py", flush=True)


if __name__ == "__main__":
    try:
        main()
    except LockSelectedFeaturesError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
