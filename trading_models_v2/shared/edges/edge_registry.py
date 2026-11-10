"""
V2 edge universe registry loader and validator.

The registry is scalable: each row describes one side-specialized edge package
under assets/<ASSET>/edges/<family>_<side>/. It must never require dashboard,
allocator, or execution-bot rewrites when future assets/edges are added.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import BAR_MODE_STANDARD_QUOTE_TICK, THRESHOLD_UNIT_TICKS, V2_CONTRACT_VERSION  # noqa: E402
from shared.edges.edge_layout import EdgeLayout, get_edge_layout, normalize_family, normalize_side  # noqa: E402


DEFAULT_EDGE_REGISTRY_PATH = _V2_ROOT / "config" / "edge_universe.json"

SOTD_EDGE_STATUSES = {
    "scaffold_not_deployable",
    "runtime_research_ready",
    "research_dataset_exported",
    "feature_surgery_complete",
    "model_trained_not_calibrated",
    "oof_generated",
    "calibrated_not_verified",
    "features_locked",
    "training_artifacts_pass",
    "runtime_scoring_ready",
    "smoke_test_pass",
    "replay_audit_pass",
    "deployability_pass",
    "allocator_candidate",
    "live_selected",
    "live_disabled",
    "watchlist",
    "research_in_progress",
    "audit_pending",
    "disabled",
}


class EdgeRegistryError(RuntimeError):
    """Raised when edge_universe.json violates the SOTD registry contract."""


@dataclass(frozen=True)
class EdgeRegistryRow:
    asset: str
    family: str
    side: str
    enabled: bool
    status: str
    bar_scope: str
    bar_mode: str
    threshold_unit: str
    candidate_event_generator: str
    notes: str = ""

    @property
    def edge_name(self) -> str:
        return f"{self.family}_{self.side}"

    @property
    def edge_id(self) -> str:
        return f"{self.asset}/{self.edge_name}"

    def to_layout(self, v2_root: str | Path | None = None) -> EdgeLayout:
        return get_edge_layout(self.asset, self.family, self.side, v2_root=v2_root)


def _require_str(row: Mapping[str, Any], key: str, context: str, default: str | None = None) -> str:
    if key not in row:
        if default is not None:
            return default
        raise EdgeRegistryError(f"{context} missing required key: {key}")
    value = row[key]
    if not isinstance(value, str):
        raise EdgeRegistryError(f"{context}.{key} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise EdgeRegistryError(f"{context}.{key} must not be empty")
    return value.strip()


def _require_bool(row: Mapping[str, Any], key: str, context: str, default: bool | None = None) -> bool:
    if key not in row:
        if default is not None:
            return default
        raise EdgeRegistryError(f"{context} missing required key: {key}")
    value = row[key]
    if not isinstance(value, bool):
        raise EdgeRegistryError(f"{context}.{key} must be boolean, got {type(value).__name__}")
    return value


def parse_edge_registry_row(row: Mapping[str, Any], idx: int) -> EdgeRegistryRow:
    context = f"edge_universe.edges[{idx}]"
    asset = _require_str(row, "asset", context).upper()
    family = normalize_family(_require_str(row, "family", context))
    side = normalize_side(_require_str(row, "side", context))
    enabled = _require_bool(row, "enabled", context, default=True)
    status = _require_str(row, "status", context, default="scaffold_not_deployable").lower()
    bar_scope = _require_str(row, "bar_scope", context, default="asset_level_shared_bars")
    bar_mode = _require_str(row, "bar_mode", context, default=BAR_MODE_STANDARD_QUOTE_TICK)
    threshold_unit = _require_str(row, "threshold_unit", context, default=THRESHOLD_UNIT_TICKS)
    candidate_event_generator = _require_str(row, "candidate_event_generator", context)
    notes = str(row.get("notes", ""))

    if status not in SOTD_EDGE_STATUSES:
        raise EdgeRegistryError(f"{context}.status {status!r} is not allowed. Allowed: {sorted(SOTD_EDGE_STATUSES)}")
    if bar_scope != "asset_level_shared_bars":
        raise EdgeRegistryError(f"{context}.bar_scope must be 'asset_level_shared_bars'")
    if bar_mode != BAR_MODE_STANDARD_QUOTE_TICK:
        raise EdgeRegistryError(f"{context}.bar_mode must be {BAR_MODE_STANDARD_QUOTE_TICK!r}")
    if threshold_unit != THRESHOLD_UNIT_TICKS:
        raise EdgeRegistryError(f"{context}.threshold_unit must be {THRESHOLD_UNIT_TICKS!r}")

    return EdgeRegistryRow(
        asset=asset,
        family=family,
        side=side,
        enabled=enabled,
        status=status,
        bar_scope=bar_scope,
        bar_mode=bar_mode,
        threshold_unit=threshold_unit,
        candidate_event_generator=candidate_event_generator,
        notes=notes,
    )


def load_edge_registry(path: str | Path = DEFAULT_EDGE_REGISTRY_PATH) -> tuple[EdgeRegistryRow, ...]:
    registry_path = Path(path)
    if not registry_path.exists():
        raise EdgeRegistryError(f"Edge registry not found: {registry_path}")
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise EdgeRegistryError("edge_universe.json must be a JSON object")
    schema_version = data.get("schema_version")
    if schema_version != V2_CONTRACT_VERSION:
        raise EdgeRegistryError(f"edge_universe.schema_version must be {V2_CONTRACT_VERSION!r}, got {schema_version!r}")
    raw_edges = data.get("edges")
    if not isinstance(raw_edges, list):
        raise EdgeRegistryError("edge_universe.json must contain an 'edges' list")
    rows = tuple(parse_edge_registry_row(row, idx) for idx, row in enumerate(raw_edges))
    seen: set[str] = set()
    duplicates: list[str] = []
    for row in rows:
        if row.edge_id in seen:
            duplicates.append(row.edge_id)
        seen.add(row.edge_id)
    if duplicates:
        raise EdgeRegistryError(f"Duplicate edge registry entries: {duplicates}")
    return rows


def enabled_edges(rows: tuple[EdgeRegistryRow, ...]) -> tuple[EdgeRegistryRow, ...]:
    return tuple(row for row in rows if row.enabled and row.status != "disabled")


def _run_self_test() -> None:
    rows = load_edge_registry()
    active = enabled_edges(rows)
    if not active:
        raise SystemExit("edge_universe.json has no enabled edges")
    print("shared.edges.edge_registry self-test passed")
    print(f"Registry: {DEFAULT_EDGE_REGISTRY_PATH.as_posix()}")
    print(f"Total edges: {len(rows)}")
    print(f"Enabled edges: {len(active)}")
    for row in active:
        layout = row.to_layout()
        print(f"  - {row.edge_id} | status={row.status} | root={layout.edge_root.as_posix()}")


if __name__ == "__main__":
    try:
        _run_self_test()
    except EdgeRegistryError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
