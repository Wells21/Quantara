"""Regression test: Step 01's _validate_edge_contract must tolerate NaN
entry/pt/sl prices for events that never executed.

Task 2A's resolution CSV overwrites entry_price/pt_price/sl_price with the
*executed* prices, which are legitimately NaN for a TRUE_NO_FILL event
(e.g. execution_status == "stale_entry_quote"). Before this fix, the
contract check used `(pt > entry).all()`, which is False whenever ANY row
has NaN pt/entry -- even though those rows have nothing wrong with their
geometry, they simply never executed. This crashed Step 01 on real
BTCUSD/absorption_long data (2 stale-entry-quote rows out of 5639) the
first time the resolution-CSV architecture was exercised end to end.

This test loads the real pilot script as a module (it is not an importable
package -- it runs standalone on Colab) and exercises _validate_edge_contract
directly against a synthetic frame reproducing the exact failure shape.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STEP01_PATH = (
    _REPO_ROOT
    / "assets"
    / "XAUUSD"
    / "edges"
    / "volatilitycarry_long"
    / "research"
    / "01_feature_surgery_clustered_mda.py"
)


def _load_step01_module():
    spec = importlib.util.spec_from_file_location("step01_pilot", _STEP01_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestValidateEdgeContractNanTolerance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.step01 = _load_step01_module()

    def test_tolerates_nan_prices_from_unfilled_events(self):
        df = pd.DataFrame(
            {
                "edge_id": ["XAUUSD/volatilitycarry_long"] * 3,
                "side": [1, 1, 1],
                "entry_price": [16845.7, np.nan, 20000.0],
                "pt_price": [16975.9, np.nan, 20500.0],
                "sl_price": [16780.6, np.nan, 19800.0],
            }
        )
        # Must not raise: rows 0 and 2 satisfy pt>entry>sl; row 1 is all-NaN
        # (no-fill) and must be treated as not-applicable, not a violation.
        self.step01._validate_edge_contract(df)

    def test_still_rejects_a_genuine_geometry_violation(self):
        df = pd.DataFrame(
            {
                "edge_id": ["XAUUSD/volatilitycarry_long"] * 2,
                "side": [1, 1],
                "entry_price": [16845.7, 20000.0],
                "pt_price": [16975.9, 19000.0],  # genuinely below entry -- must fail
                "sl_price": [16780.6, 19800.0],
            }
        )
        with self.assertRaises(self.step01.FeatureSurgeryError):
            self.step01._validate_edge_contract(df)

    def test_all_nan_price_columns_do_not_raise(self):
        df = pd.DataFrame(
            {
                "edge_id": ["XAUUSD/volatilitycarry_long"] * 2,
                "side": [1, 1],
                "entry_price": [np.nan, np.nan],
                "pt_price": [np.nan, np.nan],
                "sl_price": [np.nan, np.nan],
            }
        )
        self.step01._validate_edge_contract(df)


if __name__ == "__main__":
    unittest.main()
