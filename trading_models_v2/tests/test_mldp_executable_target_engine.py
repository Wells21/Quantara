from __future__ import annotations

import unittest
import numpy as np
import pandas as pd

from shared.execution.executable_target_dataset import (
    TRAINING_AUTHORITY,
    _event_id,
    assemble_executable_outcome_frame,
)


class TestMLDPExecutableTargetEngine(unittest.TestCase):
    def _gross(self) -> pd.DataFrame:
        t0 = pd.to_datetime(
            ["2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"]
        )
        t1 = pd.to_datetime(
            ["2026-01-01T00:05:00Z", "2026-01-01T00:15:00Z"]
        )
        return pd.DataFrame(
            {
                "Datetime": t0,
                "Event_Vol": [0.01, 0.02],
                "side": [1, 1],
                "realized_r": [1.0, -1.0],
                "meta_label": [1, 0],
                "t1": t1,
                "source_event_row_number": [0, 1],
                "event_id": ["a", "b"],
                "trgt": [0.01, 0.02],
                "geometry_hash": ["g", "g"],
            }
        )

    def test_resolved_row_gets_same_quote_r_label_t1(self):
        gross = self._gross()
        execution = pd.DataFrame(
            {
                "source_oof_row_number": [0, 1],
                "execution_status": ["resolved", "resolved"],
                "executed_r": [-0.25, 0.50],
                "exit_time": pd.to_datetime(
                    [
                        "2026-01-01T00:04:00Z",
                        "2026-01-01T00:14:00Z",
                    ]
                ),
                "entry_time": pd.to_datetime(
                    [
                        "2026-01-01T00:00:01Z",
                        "2026-01-01T00:10:01Z",
                    ]
                ),
            }
        )
        out = assemble_executable_outcome_frame(gross, execution)
        self.assertEqual(
            out["quote_executable_meta_label"].astype(int).tolist(),
            [0, 1],
        )
        self.assertTrue(
            np.allclose(
                out["quote_executable_r"].to_numpy(float),
                [-0.25, 0.50],
            )
        )
        self.assertEqual(int(out["gross_to_quote_label_flip"].sum()), 2)
        self.assertTrue(out["mldp_target_eligible"].all())
        self.assertTrue(
            (out["mldp_training_authority"] == TRAINING_AUTHORITY).all()
        )

    def test_unresolved_is_not_silently_coerced_to_zero(self):
        gross = self._gross()
        execution = pd.DataFrame(
            {
                "source_oof_row_number": [0, 1],
                "execution_status": ["resolved", "stale_entry_quote"],
                "executed_r": [0.20, np.nan],
                "exit_time": [
                    pd.Timestamp("2026-01-01T00:04:00Z"),
                    pd.NaT,
                ],
            }
        )
        out = assemble_executable_outcome_frame(gross, execution)
        self.assertEqual(
            int(out.loc[0, "quote_executable_meta_label"]), 1
        )
        self.assertTrue(
            pd.isna(out.loc[1, "quote_executable_meta_label"])
        )
        self.assertTrue(pd.isna(out.loc[1, "quote_executable_r"]))
        self.assertTrue(pd.isna(out.loc[1, "quote_executable_t1"]))
        self.assertFalse(bool(out.loc[1, "mldp_target_eligible"]))

    def test_event_id_binds_geometry(self):
        a = _event_id(
            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom1"
        )
        b = _event_id(
            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom1"
        )
        c = _event_id(
            "BTCUSD/x_long", "2026-01-01T00:00:00Z", 1, "geom2"
        )
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main()
