"""Scalable edge.yaml contract validation for SOTD.

edge.yaml must confirm the folder-derived identity:
assets/<ASSET>/edges/<family>_<side>/

PyYAML is supported when installed, but the project must not fail just because
PyYAML is absent. The fallback parser supports the nested YAML subset used by
SOTD edge.yaml files: mappings, booleans, ints/floats, strings, and simple lists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from shared.contracts import (  # noqa: E402
    BAR_MODE_STANDARD_QUOTE_TICK,
    PROBABILITY_SPACE_CALIBRATED,
    REQUIRED_EDGE_RUNTIME_FUNCTIONS,
    THRESHOLD_UNIT_TICKS,
    V2_CONTRACT_VERSION,
)
from shared.edges.edge_layout import EdgeLayout, get_edge_layout_from_edge_root  # noqa: E402


class EdgeYamlContractError(RuntimeError):
    """Raised when edge.yaml violates SOTD."""


PROBABILITY_FLOW = "raw_probability -> sigmoid calibrated_probability -> AFML bet size -> calibrated-threshold decision"

REQUIRED_TOP_LEVEL_FIELDS = (
    "schema_version", "edge_id", "asset", "family", "side_name", "side_value",
    "edge_stem", "status", "runtime_py", "bar_contract", "alpha_contract",
    "labeling", "research", "probability", "runtime_contract",
)

VALID_STATUSES = {
    "scaffold_not_deployable", "runtime_research_ready", "research_dataset_exported",
    "feature_surgery_complete", "model_trained_not_calibrated", "oof_generated",
    "calibrated_not_verified", "features_locked", "training_artifacts_pass",
    "runtime_scoring_ready", "smoke_test_pass", "replay_audit_pass",
    "deployability_pass", "allocator_candidate", "live_selected", "live_disabled",
    "watchlist", "research_in_progress", "audit_pending", "disabled",
}

# AFML Ch.1 s.1.3.1.3 (p.7): the strategy is the experiment that tests a theory naming the
# economic mechanism that makes an agent lose money to us. QUANTARA_MLDP_DOCTRINE principle 1
# makes that statement a promotion requirement.
THESIS_REQUIRED_STATUSES = {"deployability_pass", "allocator_candidate", "live_selected"}
THESIS_REQUIRED_FIELDS = (
    "schema_version", "written_utc", "provenance", "review_status", "trigger_summary",
    "mechanism", "who_loses_and_why", "regimes", "counterfactuals", "falsifiers",
)
# AFML Ch.11 sin 3 "storytelling" (p.152) and MLAM s.1.2 (theory first): owner rule 2026-10-26 -- every new edge's
# thesis card is written (pre-registered) before its first backtest. Cards written before the rule are post hoc.
THESIS_PROVENANCES = {"pre_registered", "post_hoc"}
THESIS_POST_HOC_CUTOFF_UTC = "2026-10-26"


def validate_thesis_block(payload: Mapping[str, Any], status: str) -> None:
    thesis = payload.get("thesis")
    if thesis is None and status not in THESIS_REQUIRED_STATUSES:
        return
    if not isinstance(thesis, Mapping):
        raise EdgeYamlContractError(f"edge.yaml.thesis is required for status {status!r} (AFML Ch.1 p.7).")
    _require_fields(thesis, THESIS_REQUIRED_FIELDS, "edge.yaml.thesis")
    provenance = str(thesis.get("provenance") or "")
    if provenance not in THESIS_PROVENANCES:
        raise EdgeYamlContractError(f"edge.yaml.thesis.provenance must be one of {sorted(THESIS_PROVENANCES)}.")
    if provenance == "post_hoc" and str(thesis.get("written_utc") or "")[:10] > THESIS_POST_HOC_CUTOFF_UTC:
        raise EdgeYamlContractError("edge.yaml.thesis: post_hoc cards are only accepted for edges written on or before "
                                    f"{THESIS_POST_HOC_CUTOFF_UTC}; new edges need a pre_registered card (AFML Ch.11 p.152).")
    for key in ("trigger_summary", "mechanism", "who_loses_and_why"):
        if not str(thesis.get(key) or "").strip():
            raise EdgeYamlContractError(f"edge.yaml.thesis.{key} must be a non-empty statement.")
    regimes = thesis.get("regimes")
    if not isinstance(regimes, Mapping) or not regimes.get("should_work") or not regimes.get("should_fail"):
        raise EdgeYamlContractError("edge.yaml.thesis.regimes needs non-empty should_work and should_fail lists.")
    for section in ("counterfactuals", "falsifiers"):
        items = thesis.get(section)
        if not isinstance(items, Mapping) or not items:
            raise EdgeYamlContractError(f"edge.yaml.thesis.{section} must contain at least one entry.")
        for item_id, item in items.items():
            if not isinstance(item, Mapping) or not str(item.get("statement") or "").strip() or not str(item.get("test") or "").strip():
                raise EdgeYamlContractError(f"edge.yaml.thesis.{section}.{item_id} needs a statement and a test.")


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if raw == "":
        return ""
    quoted = (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"'))
    if quoted:
        return raw[1:-1]
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"none", "null", "~"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _preprocess_yaml_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        lines.append((indent, line.strip()))
    return lines


def _parse_yaml_block(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    if pos >= len(lines) or lines[pos][0] < indent:
        return {}, pos

    if lines[pos][0] == indent and lines[pos][1].startswith("- "):
        arr: list[Any] = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
            item_text = lines[pos][1][2:].strip()
            if item_text == "":
                child, pos = _parse_yaml_block(lines, pos + 1, indent + 2)
                arr.append(child)
            elif ":" in item_text and not item_text.startswith(('"', "'")):
                key, value = item_text.split(":", 1)
                item: dict[str, Any] = {key.strip(): _parse_scalar(value.strip()) if value.strip() else {}}
                pos += 1
                arr.append(item)
            else:
                arr.append(_parse_scalar(item_text))
                pos += 1
        return arr, pos

    obj: dict[str, Any] = {}
    while pos < len(lines):
        current_indent, content = lines[pos]
        if current_indent < indent:
            break
        if current_indent > indent:
            break
        if content.startswith("- "):
            break
        if ":" not in content:
            pos += 1
            continue
        key, value = content.split(":", 1)
        key = key.strip()
        value = value.strip()
        pos += 1
        if value:
            obj[key] = _parse_scalar(value)
            continue
        if pos < len(lines) and lines[pos][0] > current_indent:
            child, pos = _parse_yaml_block(lines, pos, lines[pos][0])
            obj[key] = child
        else:
            obj[key] = {}
    return obj, pos


def _minimal_yaml_load(text: str) -> dict[str, Any]:
    lines = _preprocess_yaml_lines(text)
    parsed, pos = _parse_yaml_block(lines, 0, 0)
    if pos < len(lines):
        raise EdgeYamlContractError(f"Could not parse YAML near line: {lines[pos]!r}")
    if not isinstance(parsed, dict):
        raise EdgeYamlContractError("edge.yaml root must be a mapping")
    return parsed


def load_edge_yaml(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise EdgeYamlContractError(f"Missing edge.yaml: {file_path}")
    text = file_path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text) if yaml is not None else _minimal_yaml_load(text)
    if not isinstance(payload, dict):
        raise EdgeYamlContractError(f"edge.yaml must be a mapping: {file_path}")
    return payload


def _require_fields(payload: Mapping[str, Any], keys: tuple[str, ...], context: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise EdgeYamlContractError(f"{context} missing required fields: {missing}")


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise EdgeYamlContractError(f"edge.yaml.{key} must be a mapping.")
    return value


def expected_identity(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "edge_id": layout.edge_id,
        "asset": layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
        "edge_stem": layout.edge_stem,
        "runtime_py": "runtime.py",
    }


def validate_edge_yaml_payload(payload: Mapping[str, Any], layout: EdgeLayout) -> dict[str, Any]:
    _require_fields(payload, REQUIRED_TOP_LEVEL_FIELDS, "edge.yaml")
    if payload.get("schema_version") != V2_CONTRACT_VERSION:
        raise EdgeYamlContractError(f"schema_version must be {V2_CONTRACT_VERSION!r}")
    for key, expected_value in expected_identity(layout).items():
        if payload.get(key) != expected_value:
            raise EdgeYamlContractError(f"edge.yaml.{key} mismatch: expected {expected_value!r}, got {payload.get(key)!r}")
    status = str(payload.get("status"))
    if status not in VALID_STATUSES:
        raise EdgeYamlContractError(f"edge.yaml.status {status!r} is not valid.")
    validate_thesis_block(payload, status)

    bar_contract = _require_mapping(payload, "bar_contract")
    _require_fields(bar_contract, ("bar_type", "threshold_unit", "proxy_value_policy"), "edge.yaml.bar_contract")
    if bar_contract.get("bar_type") != BAR_MODE_STANDARD_QUOTE_TICK:
        raise EdgeYamlContractError("bar_contract.bar_type must be standard_quote_tick.")
    if bar_contract.get("threshold_unit") != THRESHOLD_UNIT_TICKS:
        raise EdgeYamlContractError("bar_contract.threshold_unit must be ticks.")

    alpha_contract = _require_mapping(payload, "alpha_contract")
    _require_fields(alpha_contract, ("candidate_event_generator", "primary_side_rule", "edge_specific_features_allowed"), "edge.yaml.alpha_contract")

    labeling = _require_mapping(payload, "labeling")
    _require_fields(labeling, ("row_builder", "vertical_horizon", "pt_multiplier", "sl_multiplier", "tie_policy"), "edge.yaml.labeling")

    research = _require_mapping(payload, "research")
    _require_fields(research, ("cv", "n_folds", "n_test_folds", "manual_embargo_size", "weights"), "edge.yaml.research")
    if research.get("cv") != "combinatorial_purged_cv":
        raise EdgeYamlContractError("research.cv must be combinatorial_purged_cv.")

    probability = _require_mapping(payload, "probability")
    _require_fields(probability, ("flow", "final_edge_threshold_space", "final_live_threshold_source"), "edge.yaml.probability")
    if probability.get("flow") != PROBABILITY_FLOW:
        raise EdgeYamlContractError("probability.flow does not match SOTD.")
    if probability.get("final_edge_threshold_space") != PROBABILITY_SPACE_CALIBRATED:
        raise EdgeYamlContractError("final_edge_threshold_space must be calibrated_probability.")

    runtime_contract = _require_mapping(payload, "runtime_contract")
    required_functions = runtime_contract.get("required_functions")
    if list(required_functions or []) != list(REQUIRED_EDGE_RUNTIME_FUNCTIONS):
        raise EdgeYamlContractError(
            f"runtime_contract.required_functions must match shared.required runtime API exactly. expected={list(REQUIRED_EDGE_RUNTIME_FUNCTIONS)!r}, got={required_functions!r}"
        )
    if runtime_contract.get("dashboard_must_not_compute_features") is not True:
        raise EdgeYamlContractError("runtime_contract.dashboard_must_not_compute_features must be true.")
    return {"edge_id": layout.edge_id, "edge_stem": layout.edge_stem, "status": status, "full_edge_yaml_contract": True}


def validate_edge_yaml_file(edge_root: str | Path) -> dict[str, Any]:
    layout = get_edge_layout_from_edge_root(edge_root)
    return validate_edge_yaml_payload(load_edge_yaml(layout.edge_yaml), layout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a full SOTD edge.yaml contract.")
    parser.add_argument("--edge-root", default=".")
    args = parser.parse_args()
    result = validate_edge_yaml_file(args.edge_root)
    print("edge.yaml full SOTD contract validation passed")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except EdgeYamlContractError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
