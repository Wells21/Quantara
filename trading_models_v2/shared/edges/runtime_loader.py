"""Isolated runtime loader for SOTD edge runtimes.

Edge runtime.py files intentionally own edge-specific alpha logic. Many of them
import sibling modules with short names such as candidate_events, primary_side,
model_rows, feature_expansion, and regime_features. A portfolio/live process must
import many edge runtimes in one Python interpreter, so imports must be isolated
per edge to avoid module-cache collisions.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

from shared.edges.edge_layout import get_edge_layout_from_edge_root

EDGE_LOCAL_MODULE_NAMES = {
    "candidate_events",
    "feature_expansion",
    "model_rows",
    "primary_side",
    "regime_features",
    "runtime",
}


class EdgeRuntimeLoadError(RuntimeError):
    """Raised when an edge runtime cannot be loaded safely."""


def _coerce_edge_root(edge_root: str | Path) -> Path:
    path = Path(edge_root)
    if path.is_file() and path.name == "runtime.py":
        return path.parent.resolve()
    return path.resolve()


def _module_name_for_edge_runtime(edge_root: Path) -> str:
    layout = get_edge_layout_from_edge_root(edge_root)
    return f"v2_edge_runtime__{layout.edge_stem}"


@contextmanager
def isolated_edge_import_context(edge_root: str | Path) -> Iterator[None]:
    """Temporarily isolate sibling-module imports for one edge runtime.

    The context puts the edge root first on sys.path, removes known short-name
    edge-local modules from sys.modules, and restores sys.path/sys.modules after
    the import. Runtime globals keep references to the loaded sibling objects, so
    multiple edge runtimes can coexist without candidate_events/model_rows name
    collisions.
    """

    root = _coerce_edge_root(edge_root)
    original_sys_path = list(sys.path)
    saved_modules: dict[str, ModuleType | None] = {name: sys.modules.get(name) for name in EDGE_LOCAL_MODULE_NAMES}
    try:
        for name in EDGE_LOCAL_MODULE_NAMES:
            sys.modules.pop(name, None)
        root_str = str(root)
        if root_str in sys.path:
            sys.path.remove(root_str)
        sys.path.insert(0, root_str)
        yield
    finally:
        for name in EDGE_LOCAL_MODULE_NAMES:
            sys.modules.pop(name, None)
            previous = saved_modules.get(name)
            if previous is not None:
                sys.modules[name] = previous
        sys.path[:] = original_sys_path


def load_edge_runtime(edge_root: str | Path) -> ModuleType:
    """Load an edge runtime.py with isolated sibling imports."""

    root = _coerce_edge_root(edge_root)
    runtime_path = root / "runtime.py"
    if not runtime_path.exists():
        raise EdgeRuntimeLoadError(f"Missing runtime.py: {runtime_path}")
    module_name = _module_name_for_edge_runtime(root)
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, runtime_path)
    if spec is None or spec.loader is None:
        raise EdgeRuntimeLoadError(f"Could not create import spec for runtime.py: {runtime_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        with isolated_edge_import_context(root):
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise EdgeRuntimeLoadError(f"Failed to load edge runtime {runtime_path}: {exc}") from exc
    return module


def load_edge_runtime_from_manifest_entry(edge: dict[str, Any], *, v2_root: str | Path) -> ModuleType:
    """Load a selected edge runtime from a live-manifest selected_edges row."""

    edge_root_text = str(edge.get("edge_root", "")).strip()
    if edge_root_text:
        return load_edge_runtime(Path(v2_root) / edge_root_text)
    runtime_module = str(edge.get("runtime_module", "")).strip()
    if runtime_module.endswith("runtime.py"):
        return load_edge_runtime(Path(v2_root) / runtime_module)
    raise EdgeRuntimeLoadError(
        f"{edge.get('edge_id', '<unknown>')}: manifest selected edge must include edge_root or runtime.py path."
    )
