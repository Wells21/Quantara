from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from shared.edges.phase_c_feature_contract import compile_feature_contract, validate_frozen_contract


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _feature(name: str, inputs: list[str], *, formula: str | None = None, selected: bool = True) -> dict:
    return {
        "feature_name": name,
        "owner_stage": "edge_runtime_feature_factory",
        "dtype": "float64",
        "formula_description": formula or f"{name} formula",
        "required_inputs": inputs,
        "live_available": True,
        "uses_future_information": False,
        "allowed_for_model_input": selected,
        "missing_value_policy": "drop_until_available",
        "feature_tier": 1,
        "feature_group": "test",
        "computed_by": "runtime.py::build_features",
    }


def _make_edge(root: Path, asset: str, family: str, features: list[dict], selected: list[str]) -> dict:
    edge_id = f"{asset}/{family}_long"
    edge_root = root / "assets" / asset / "edges" / f"{family}_long"
    contracts = edge_root / "contracts"
    artifacts = edge_root / "artifacts"
    contracts.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    runtime = edge_root / "runtime.py"
    runtime.write_text(
        "def build_features(bars, drop_warmup_rows=False):\n"
        "    return bars.copy()\n",
        encoding="utf-8",
    )
    schema = contracts / "feature_schema.json"
    selected_path = artifacts / "selected_features.json"
    _write_json(
        schema,
        {
            "schema_version": "v2.1",
            "edge_id": edge_id,
            "candidate_scope": "synthetic",
            "features": features,
        },
    )
    _write_json(
        selected_path,
        {
            "schema_version": "v2.1",
            "edge_id": edge_id,
            "selected_features": selected,
        },
    )
    return {
        "edge_id": edge_id,
        "asset": asset,
        "family": family,
        "edge_root": edge_root.relative_to(root).as_posix(),
        "runtime_module": runtime.relative_to(root).as_posix(),
        "selected_for_live": True,
        "artifacts": {
            "feature_schema": schema.relative_to(root).as_posix(),
            "selected_features": selected_path.relative_to(root).as_posix(),
        },
    }


def _manifest(root: Path, edges: list[dict]) -> Path:
    path = root / "portfolio_live" / "live_manifest.json"
    _write_json(
        path,
        {
            "schema_version": "v2.3",
            "selected_edge_count": len(edges),
            "selected_edges": edges,
        },
    )
    return path


class PhaseCFeatureContractTests(unittest.TestCase):
    def test_dynamic_asset_and_edge_discovery_has_no_fixed_universe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = _make_edge(root, "AAA", "trend", [_feature("Ret1", ["Close"])], ["Ret1"])
            b = _make_edge(root, "AAA", "range", [_feature("Ret1", ["Close"])], ["Ret1"])
            first = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [a, b]))
            self.assertEqual(first["selected_asset_count"], 1)
            self.assertEqual(first["selected_edge_count"], 2)

            c = _make_edge(root, "BBB", "newfamily", [_feature("Ret1", ["Close"])], ["Ret1"])
            second = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [a, b, c]))
            self.assertEqual(second["selected_asset_count"], 2)
            self.assertEqual(second["selected_edge_count"], 3)
            self.assertEqual(second["selected_assets"], ["AAA", "BBB"])
            self.assertNotEqual(first["contract_sha256"], second["contract_sha256"])

    def test_compile_is_deterministic_under_manifest_edge_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = _make_edge(root, "AAA", "trend", [_feature("Ret1", ["Close"])], ["Ret1"])
            b = _make_edge(root, "AAA", "range", [_feature("Ret1", ["Close"])], ["Ret1"])
            path = _manifest(root, [a, b])
            left = compile_feature_contract(v2_root=root, live_manifest_path=path)
            path = _manifest(root, [b, a])
            right = compile_feature_contract(v2_root=root, live_manifest_path=path)
            self.assertEqual(left["assets"], right["assets"])
            self.assertEqual(left["edges"], right["edges"])

    def test_shared_feature_is_only_structural_candidate_not_certified_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            features = [_feature("Ret1", ["Close"])]
            a = _make_edge(root, "AAA", "trend", features, ["Ret1"])
            b = _make_edge(root, "AAA", "range", features, ["Ret1"])
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [a, b]))
            node = contract["assets"]["AAA"]["feature_nodes"][0]
            self.assertEqual(node["classification"], "shared_core_candidate")
            self.assertTrue(node["numerical_certification_required"])
            self.assertFalse(contract["runtime_routing_enabled"])

    def test_subset_feature_becomes_dynamic_addon_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = _feature("Ret1", ["Close"])
            special = _feature("Special", ["Ret1"])
            a = _make_edge(root, "AAA", "trend", [core, special], ["Special"])
            b = _make_edge(root, "AAA", "range", [core], ["Ret1"])
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [a, b]))
            asset = contract["assets"]["AAA"]
            self.assertIn("Special", asset["addon_candidate_features"])
            self.assertIn("Ret1", asset["shared_core_candidate_features"])

    def test_structural_conflict_preserves_variants_for_later_numeric_certification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = _make_edge(root, "AAA", "trend", [_feature("Ret1", ["Close"], formula="log return")], ["Ret1"])
            b = _make_edge(root, "AAA", "range", [_feature("Ret1", ["Close"], formula="simple return")], ["Ret1"])
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [a, b]))
            asset = contract["assets"]["AAA"]
            self.assertIn("Ret1", asset["variant_conflict_features"])
            node = next(row for row in asset["feature_nodes"] if row["feature_name"] == "Ret1")
            self.assertEqual(node["variant_count"], 2)
            self.assertEqual(node["classification"], "variant_conflict_requires_numeric_certification")
            self.assertEqual(contract["status"], "PASS")

    def test_dependency_dag_and_phase_ab_source_leaves_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            features = [
                _feature("Ret1", ["Close"]),
                _feature("Ret2", ["Ret1", "Close_FFD", "Prob_State_1"]),
            ]
            edge = _make_edge(root, "AAA", "trend", features, ["Ret2"])
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [edge]))
            asset = contract["assets"]["AAA"]
            self.assertLess(asset["topological_feature_order"].index("Ret1"), asset["topological_feature_order"].index("Ret2"))
            self.assertIn("Close_FFD", asset["source_inputs"]["phase_ab_dependency"])
            self.assertIn("Prob_State_1", asset["source_inputs"]["phase_ab_dependency"])
            self.assertIn("Close", asset["source_inputs"]["raw_bar"])

    def test_selected_feature_missing_from_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge = _make_edge(root, "AAA", "trend", [_feature("Ret1", ["Close"])], ["Ret1"])
            selected_path = root / edge["artifacts"]["selected_features"]
            selected = json.loads(selected_path.read_text(encoding="utf-8"))
            selected["selected_features"] = ["MISSING"]
            _write_json(selected_path, selected)
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [edge]))
            self.assertEqual(contract["status"], "FAIL")
            self.assertTrue(any("absent from feature schema" in item for item in contract["fatal_errors"]))

    def test_duplicate_edge_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edge = _make_edge(root, "AAA", "trend", [_feature("Ret1", ["Close"])], ["Ret1"])
            contract = compile_feature_contract(v2_root=root, live_manifest_path=_manifest(root, [edge, dict(edge)]))
            self.assertEqual(contract["status"], "FAIL")
            self.assertTrue(any("duplicate selected edge_id" in item for item in contract["fatal_errors"]))

    def test_frozen_contract_invalidates_when_selected_universe_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = _make_edge(
                root, "AAA", "trend", [_feature("Ret1", ["Close"])], ["Ret1"]
            )
            manifest = _manifest(root, [a])
            frozen = compile_feature_contract(
                v2_root=root, live_manifest_path=manifest
            )
            frozen_path = root / "frozen.json"
            _write_json(frozen_path, frozen)
            self.assertEqual(
                validate_frozen_contract(
                    v2_root=root,
                    contract_path=frozen_path,
                    live_manifest_path=manifest,
                )["status"],
                "PASS",
            )

            b = _make_edge(
                root, "BBB", "range", [_feature("Ret1", ["Close"])], ["Ret1"]
            )
            _manifest(root, [a, b])
            result = validate_frozen_contract(
                v2_root=root,
                contract_path=frozen_path,
                live_manifest_path=manifest,
            )
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(
                any(
                    "structural" in item.lower()
                    or "selected" in item.lower()
                    for item in result["failures"]
                )
            )


    def test_current_live_manifest_compiles_manifest_driven_without_routing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "portfolio_live" / "live_manifest.json"
        if not manifest_path.exists():
            self.skipTest("current live_manifest.json not present")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        contract = compile_feature_contract(v2_root=root, live_manifest_path=manifest_path)
        self.assertEqual(contract["selected_edge_count"], len(manifest.get("selected_edges") or []))
        self.assertEqual(
            contract["selected_assets"],
            sorted(
                {
                    str(edge.get("asset") or "").upper()
                    for edge in manifest.get("selected_edges") or []
                    if isinstance(edge, dict) and str(edge.get("asset") or "")
                }
            ),
        )
        self.assertFalse(contract["runtime_routing_enabled"])
        self.assertEqual(contract["status"], "PASS", contract["fatal_errors"])


if __name__ == "__main__":
    unittest.main()
