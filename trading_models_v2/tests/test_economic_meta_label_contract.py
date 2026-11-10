from __future__ import annotations

import unittest

import pandas as pd

from shared.labels.economic_meta_label import (
    CONTRACT_VERSION,
    EconomicMetaLabelContractError,
    economic_model_rows_contract,
    enforce_gross_economic_model_row_contract,
    executable_meta_label,
    validate_gross_economic_model_row_contract,
)


class EconomicMetaLabelContractTests(unittest.TestCase):
    def test_vertical_positive_is_positive_meta_label(self) -> None:
        df = pd.DataFrame({
            "barrier_hit": ["pt", "sl", "vertical", "vertical"],
            "realized_r": [2.0, -1.0, 0.40, -0.20],
            "meta_label": [1, 0, 0, 0],
        })
        out, manifest = enforce_gross_economic_model_row_contract(df, {}, edge_id="TEST/foo_long")
        self.assertEqual(out["meta_label"].tolist(), [1, 0, 1, 0])
        self.assertEqual(out["pt_hit_label"].tolist(), [1, 0, 0, 0])
        self.assertEqual(manifest["economic_meta_label_contract_version"], CONTRACT_VERSION)
        self.assertEqual(validate_gross_economic_model_row_contract(out, edge_id="TEST/foo_long")["status"], "PASS")

    def test_runtime_decorator_canonicalizes_builder_output(self) -> None:
        @economic_model_rows_contract(edge_id="TEST/decorated_long")
        def builder():
            return (
                pd.DataFrame({
                    "barrier_hit": ["vertical", "sl"],
                    "realized_r": [0.15, -1.0],
                    "meta_label": [0, 0],
                }),
                {"edge_id": "TEST/decorated_long"},
            )

        rows, manifest = builder()
        self.assertEqual(rows["meta_label"].tolist(), [1, 0])
        self.assertEqual(rows["pt_hit_label"].tolist(), [0, 0])
        self.assertEqual(manifest["economic_meta_label_contract_version"], CONTRACT_VERSION)

    def test_bad_pt_sign_fails_closed(self) -> None:
        df = pd.DataFrame({"barrier_hit": ["pt"], "realized_r": [-0.1], "meta_label": [1]})
        with self.assertRaises(EconomicMetaLabelContractError):
            enforce_gross_economic_model_row_contract(df, {}, edge_id="TEST/foo_long")

    def test_executable_label(self) -> None:
        self.assertEqual(executable_meta_label([0.2, 0.0, -0.1]).tolist(), [1, 0, 0])


if __name__ == "__main__":
    unittest.main()
