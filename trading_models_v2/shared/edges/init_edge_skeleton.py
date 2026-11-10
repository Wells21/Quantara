"""
Initialize V2 side-specialized edge skeletons.

Blueprint purpose
-----------------
Each deployable alpha edge must be a complete, side-specialized package:

    assets/{ASSET}/edges/{family}_{side}/

Examples:

    assets/XAUUSD/edges/trend_long/
    assets/XAUUSD/edges/trend_short/
    assets/BTCUSD/edges/absorption_long/
    assets/USTECH/edges/time_regime_short/

This initializer creates the empty contract-compliant skeleton. It does not train
models, copy V1 artifacts, or make any edge deployable.

The dashboard must never manually create edge-specific features. Every edge must
later fill in runtime.py, schemas, artifacts, audits, and edge_manifest.json.

Safe self-test:
---------------
Running this file with no arguments creates a temporary edge skeleton outside the
repo, validates it, and deletes it automatically.

    python ./shared/edges/init_edge_skeleton.py

Create a real edge skeleton manually:

    python ./shared/edges/init_edge_skeleton.py --asset XAUUSD --family trend --side long --write

Create both sides manually:

    python ./shared/edges/init_edge_skeleton.py --asset XAUUSD --family trend --side long short --write

Create all enabled scaffold edges from config/edge_universe.json:

    python ./shared/edges/init_edge_skeleton.py --from-registry --write
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from textwrap import dedent
from typing import Any

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import (  # noqa: E402
    BAR_MODE_STANDARD_QUOTE_TICK,
    LIVE_MANIFEST_CONTRACT_VERSION,
    REQUIRED_EDGE_RUNTIME_FUNCTIONS,
    THRESHOLD_UNIT_TICKS,
    V2_CONTRACT_VERSION,
)
from shared.edges.edge_layout import EdgeLayout, describe_edge_layout, get_edge_layout  # noqa: E402
from shared.edges.edge_registry import enabled_edges, load_edge_registry  # noqa: E402


class EdgeSkeletonError(RuntimeError):
    """Raised when edge skeleton initialization fails."""


EDGE_README_TEMPLATE = """# {edge_id}

This is a V2 side-specialized alpha edge package.

## Blueprint contract

- One asset: `{asset}`
- One family: `{family}`
- One side: `{side_name}`
- One side value: `{side_value}`
- One candidate event generator
- One meta-model
- One calibrator
- One threshold sweep
- One `runtime.py`
- One `edge_manifest.json`

## Ownership boundary

This edge may build edge-specific features/events/model rows only through its own
`runtime.py` functions. Research and live must call the same runtime functions.

The dashboard must not manually engineer this edge's selected model features.

## Deployability

This edge is not deployable until it has:

1. Complete feature schema with `live_available` and `allowed_for_model_input`.
2. Selected features that are reproducible live.
3. Model/calibrator/threshold artifacts.
4. Probability replay: raw_probability -> calibrated_probability.
5. AFML replay.
6. Deployability audit PASS.
7. `artifacts/edge_manifest.json` with deployment_status=PASS.
"""

RUNTIME_TEMPLATE = '''"""
V2 edge runtime for {edge_id}.

This file is intentionally a scaffold. It defines the required runtime functions
but fails closed until the edge research implementation is filled in.

Research and live must call these same functions. Do not duplicate feature logic
in dashboard/live scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared.labels.economic_meta_label import economic_model_rows_contract


EDGE_ID = "{edge_id}"
ASSET = "{asset}"
FAMILY = "{family}"
SIDE_NAME = "{side_name}"
SIDE_VALUE = {side_value}


class EdgeNotImplementedError(NotImplementedError):
    """Raised until this edge's runtime is fully implemented and audited."""


def load_contract(edge_root: str | Path | None = None) -> dict[str, Any]:
    """Load edge contract metadata.

    This scaffold returns static identity only. Real implementation must load and
    validate edge.yaml plus contracts/*.json.
    """
    return {{
        "edge_id": EDGE_ID,
        "asset": ASSET,
        "family": FAMILY,
        "side_name": SIDE_NAME,
        "side_value": SIDE_VALUE,
        "runtime_status": "SCAFFOLD_NOT_DEPLOYABLE",
    }}


def validate_environment(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Validate local runtime dependencies and artifact availability."""
    raise EdgeNotImplementedError("validate_environment is not implemented for this edge yet.")


def build_features(*args: Any, **kwargs: Any) -> Any:
    """Build edge-specific features from asset-level shared bars."""
    raise EdgeNotImplementedError("build_features is not implemented for this edge yet.")


def generate_candidate_events(*args: Any, **kwargs: Any) -> Any:
    """Generate this edge's candidate events from live-compatible features/bars."""
    raise EdgeNotImplementedError("generate_candidate_events is not implemented for this edge yet.")


@economic_model_rows_contract(edge_id=EDGE_ID)
def build_model_rows(*args: Any, **kwargs: Any) -> Any:
    """Build exact selected model rows for research/live scoring parity."""
    raise EdgeNotImplementedError("build_model_rows is not implemented for this edge yet.")


def load_model(*args: Any, **kwargs: Any) -> Any:
    """Load the edge's trained meta-model artifact."""
    raise EdgeNotImplementedError("load_model is not implemented for this edge yet.")


def load_calibrator(*args: Any, **kwargs: Any) -> Any:
    """Load the edge's sigmoid probability calibrator."""
    raise EdgeNotImplementedError("load_calibrator is not implemented for this edge yet.")


def score(*args: Any, **kwargs: Any) -> Any:
    """Score model rows and emit raw_probability/calibrated_probability/AFML fields."""
    raise EdgeNotImplementedError("score is not implemented for this edge yet.")


def explain_missing_features(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explain missing selected features without dashboard-side patching."""
    return {{
        "edge_id": EDGE_ID,
        "runtime_status": "SCAFFOLD_NOT_DEPLOYABLE",
        "message": "Feature implementation is not complete. Fix runtime.py/schema; do not patch dashboard.",
    }}
'''

RESEARCH_PIPELINE_TEMPLATE = '''"""
Research pipeline scaffold for {edge_id}.

This file will later orchestrate this edge's research steps using the same
runtime.py functions used by live.

It must not produce deployable artifacts unless runtime parity and deployability
audits pass.
"""

from __future__ import annotations


def main() -> None:
    print("Research pipeline scaffold for {edge_id}. Not implemented yet.")


if __name__ == "__main__":
    main()
'''


def _write_text_if_missing(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_json_if_missing(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _touch_gitkeep(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    gitkeep = directory / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")


def _edge_yaml_text(layout: EdgeLayout) -> str:
    return dedent(
        f"""
        schema_version: {V2_CONTRACT_VERSION}
        edge_id: {layout.edge_id}
        asset: {layout.asset_layout.asset}
        family: {layout.family}
        side_name: {layout.side_name}
        side_value: {layout.side_value}
        bar_mode: {BAR_MODE_STANDARD_QUOTE_TICK}
        threshold_unit: {THRESHOLD_UNIT_TICKS}
        status: scaffold_not_deployable
        notes: >-
          V2 side-specialized edge scaffold. This edge is not deployable until
          runtime.py, schemas, artifacts, calibration replay, AFML replay, and
          deployability audit all pass.
        """
    ).strip() + "\n"


def _bar_contract(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "asset_bar_scope": "asset_level_shared_bars",
        "bar_mode": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "proxy_value_definition": "Proxy_Value equals Ticks for backward compatibility only.",
        "required_source": f"assets/{layout.asset_layout.asset}/bars/{BAR_MODE_STANDARD_QUOTE_TICK}/",
        "dashboard_may_engineer_edge_features": False,
    }


def _feature_schema(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "features": [],
        "notes": [
            "Every model feature must be declared here before selection.",
            "Selected model features must have live_available=true and allowed_for_model_input=true.",
            "If a selected feature is missing live, fix runtime.py/schema; do not patch the dashboard.",
        ],
    }



def _label_schema(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "contract_name": "label_schema",
        "status": "scaffold_not_deployable",
        "economic_meta_label_contract_version": "quantara_v3_gross_economic_meta_label_v1",
        "meta_label": {
            "dtype": "binary_int",
            "semantics": "1 iff realized_r > 0.0; else 0",
            "source": "realized_r",
            "uses_future_information": True,
            "live_model_input_allowed": False,
        },
        "pt_hit_label": {
            "dtype": "binary_int",
            "semantics": "1 iff barrier_hit == 'pt'; else 0",
            "source": "barrier_hit",
            "diagnostic_only": True,
            "uses_future_information": True,
            "live_model_input_allowed": False,
        },
        "continuous_economic_outcome": {
            "column": "realized_r",
            "purpose": "gross continuous R outcome including vertical exits",
            "uses_future_information": True,
            "live_model_input_allowed": False,
        },
    }

def _model_input_schema(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "selected_features_artifact": "artifacts/selected_features.json",
        "model_input_columns": [],
        "model_input_order_locked": True,
        "live_must_reproduce_exact_selected_rows": True,
    }


def _generic_contract(layout: EdgeLayout, contract_name: str) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "contract_name": contract_name,
        "status": "scaffold_not_deployable",
    }


def _live_runtime_contract(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "required_runtime_functions": list(REQUIRED_EDGE_RUNTIME_FUNCTIONS),
        "research_live_same_runtime_functions": True,
        "dashboard_may_engineer_edge_features": False,
        "status": "scaffold_not_deployable",
    }


def _selected_features(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "selected_features": [],
        "feature_order_locked": True,
        "status": "empty_scaffold_not_deployable",
    }


def _training_summary(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "status": "scaffold_not_trained",
        "deployable": False,
    }


def _validation_report(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "schema_version": V2_CONTRACT_VERSION,
        "edge_id": layout.edge_id,
        "deployment_status": "FAIL",
        "reason": "Scaffold only. Runtime/research/artifacts/audits not implemented yet.",
    }


def create_edge_skeleton(layout: EdgeLayout) -> None:
    """Create a V2 edge skeleton without overwriting existing files."""

    layout.asset_layout.create_directories()
    layout.create_directories()

    _write_text_if_missing(layout.edge_root / "README.md", EDGE_README_TEMPLATE.format(**_template_vars(layout)))
    _write_text_if_missing(layout.edge_yaml, _edge_yaml_text(layout))
    _write_text_if_missing(layout.runtime_py, RUNTIME_TEMPLATE.format(**_template_vars(layout)))
    _write_text_if_missing(layout.research_pipeline_py, RESEARCH_PIPELINE_TEMPLATE.format(**_template_vars(layout)))

    _write_json_if_missing(layout.contracts_dir / "bar_contract.json", _bar_contract(layout))
    _write_json_if_missing(layout.contracts_dir / "feature_schema.json", _feature_schema(layout))
    _write_json_if_missing(layout.contracts_dir / "event_schema.json", _generic_contract(layout, "event_schema"))
    _write_json_if_missing(layout.contracts_dir / "label_schema.json", _label_schema(layout))
    _write_json_if_missing(layout.contracts_dir / "model_input_schema.json", _model_input_schema(layout))
    _write_json_if_missing(layout.contracts_dir / "calibration_schema.json", _generic_contract(layout, "calibration_schema"))
    _write_json_if_missing(layout.contracts_dir / "live_runtime_contract.json", _live_runtime_contract(layout))

    _write_text_if_missing(layout.configs_dir / "research.yaml", _config_yaml(layout, "research"))
    _write_text_if_missing(layout.configs_dir / "labeling.yaml", _config_yaml(layout, "labeling"))
    _write_text_if_missing(layout.configs_dir / "model.yaml", _config_yaml(layout, "model"))
    _write_text_if_missing(layout.configs_dir / "calibration.yaml", _config_yaml(layout, "calibration"))

    _write_json_if_missing(layout.artifacts_dir / "selected_features.json", _selected_features(layout))
    _write_json_if_missing(layout.artifacts_dir / "training_summary.json", _training_summary(layout))
    _write_json_if_missing(layout.reports_dir / "validation_report.json", _validation_report(layout))

    _touch_gitkeep(layout.artifacts_dir)
    _touch_gitkeep(layout.reports_dir)
    _touch_gitkeep(layout.plots_dir)


def _template_vars(layout: EdgeLayout) -> dict[str, Any]:
    return {
        "edge_id": layout.edge_id,
        "asset": layout.asset_layout.asset,
        "family": layout.family,
        "side_name": layout.side_name,
        "side_value": layout.side_value,
    }


def _config_yaml(layout: EdgeLayout, config_name: str) -> str:
    return dedent(
        f"""
        schema_version: {V2_CONTRACT_VERSION}
        edge_id: {layout.edge_id}
        config_name: {config_name}
        status: scaffold_not_deployable
        live_manifest_contract_version: {LIVE_MANIFEST_CONTRACT_VERSION}
        """
    ).strip() + "\n"


def validate_created_skeleton(layout: EdgeLayout) -> None:
    report = layout.validate_existing_skeleton(require_files=True)
    if report.failed:
        raise EdgeSkeletonError("Edge skeleton validation failed: " + "; ".join(report.fail_messages()))

    runtime_text = layout.runtime_py.read_text(encoding="utf-8")
    missing_functions = [fn for fn in REQUIRED_EDGE_RUNTIME_FUNCTIONS if f"def {fn}" not in runtime_text]
    if missing_functions:
        raise EdgeSkeletonError(f"runtime.py missing required function scaffold(s): {missing_functions}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize V2 side-specialized edge skeletons.")
    parser.add_argument("--asset", default="XAUUSD", help="Asset name, e.g. XAUUSD, BTCUSD, USTECH.")
    parser.add_argument("--family", default="trend", help="Edge family, e.g. trend, absorption, time_regime.")
    parser.add_argument(
        "--side",
        nargs="+",
        choices=["long", "short"],
        default=["long"],
        help="One or more sides to initialize.",
    )
    parser.add_argument(
        "--from-registry",
        action="store_true",
        help="Initialize all enabled edges from config/edge_universe.json.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Create real edge skeleton(s) in the repo. Without --write, only a temp self-test is run.",
    )
    return parser


def run_temp_self_test() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        temp_v2_root = Path(tmp) / "trading_models_v2"
        layout = get_edge_layout("XAUUSD", "trend", "long", v2_root=temp_v2_root)
        create_edge_skeleton(layout)
        validate_created_skeleton(layout)

        print("✅ shared.edges.init_edge_skeleton temp self-test passed.")
        print("Created and validated temporary scaffold:")
        for line in describe_edge_layout(layout):
            print(f"  - {line}")


def run_write(asset: str, family: str, sides: list[str]) -> None:
    layouts: list[EdgeLayout] = []
    for side in sides:
        layout = get_edge_layout(asset, family, side)
        create_edge_skeleton(layout)
        validate_created_skeleton(layout)
        layouts.append(layout)

    _print_initialized_layouts(layouts)


def run_registry_write() -> None:
    rows = enabled_edges(load_edge_registry())
    if not rows:
        raise EdgeSkeletonError("No enabled edges found in config/edge_universe.json")

    layouts: list[EdgeLayout] = []
    for row in rows:
        layout = row.to_layout()
        create_edge_skeleton(layout)
        validate_created_skeleton(layout)
        layouts.append(layout)

    _print_initialized_layouts(layouts)


def _print_initialized_layouts(layouts: list[EdgeLayout]) -> None:
    print("✅ V2 edge skeleton(s) initialized.")
    print("Reminder: skeleton edges are NOT deployable until runtime/artifacts/audits PASS.")
    print("Scalability: add edges in config/edge_universe.json or pass --asset/--family/--side.")
    for layout in layouts:
        print(f"\n[{layout.edge_id}]")
        for line in describe_edge_layout(layout):
            print(f"  - {line}")


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    if not args.write:
        run_temp_self_test()
        print("\nNo real repo edge files were created. Use --write to create actual edge skeletons.")
        print("Use --from-registry --write to initialize enabled registry edges.")
        return

    if args.from_registry:
        run_registry_write()
        return

    run_write(asset=args.asset, family=args.family, sides=args.side)


if __name__ == "__main__":
    _run()
