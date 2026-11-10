from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from shared.execution.executable_target_dataset import discover_edge_roots


class TestMLDPExecutableTargetDiscovery(unittest.TestCase):
    def _edge(
        self,
        root: Path,
        asset: str,
        edge: str,
        *,
        model_rows_artifact: bool,
    ) -> Path:
        e = root / "assets" / asset / "edges" / edge
        e.mkdir(parents=True)
        for name in ("edge.yaml", "runtime.py", "model_rows.py"):
            (e / name).write_text("# fixture\n", encoding="utf-8")
        if model_rows_artifact:
            stem = f"{asset.lower()}_{edge}"
            d = e / "artifacts" / "research_dataset"
            d.mkdir(parents=True)
            (d / f"{stem}_model_rows.csv").write_text(
                "Datetime\n", encoding="utf-8"
            )
        return e

    def test_discovery_is_multi_asset_and_not_edge_hardcoded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            e1 = self._edge(
                root, "AAA", "alpha_long", model_rows_artifact=False
            )
            e2 = self._edge(
                root, "BBB", "beta_short", model_rows_artifact=False
            )
            found = discover_edge_roots(root)
            self.assertEqual(found, [e1.resolve(), e2.resolve()])
            self.assertEqual(
                discover_edge_roots(root, asset="BBB"), [e2.resolve()]
            )

    def test_require_model_rows_is_artifact_driven(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._edge(
                root, "AAA", "alpha_long", model_rows_artifact=False
            )
            e2 = self._edge(
                root, "BBB", "beta_short", model_rows_artifact=True
            )
            found = discover_edge_roots(root, require_model_rows=True)
            self.assertEqual(found, [e2.resolve()])


if __name__ == "__main__":
    unittest.main()
