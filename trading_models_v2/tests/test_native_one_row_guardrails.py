from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from shared.edges import native_one_row_contract as contract
from shared.edges import native_one_row_event_engine as native_event_engine
from shared.edges.native_one_row_event_engine import NativeEventError, build_events_live_native
from shared.edges.native_one_row_feature_engine import _ewm_var_state, _ewm_var_update


class _Runtime:
    def generate_candidate_events(self, features: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame()


class NativeOneRowGuardrailTests(unittest.TestCase):
    def test_certification_requires_global_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cert.json"
            base = {
                "schema_version": contract.SCHEMA_VERSION,
                "selected_edge_count": 33,
                "implementation_hashes": contract.implementation_hashes(),
                "runtime_environment": contract.runtime_environment(),
                "assets": {},
                "edges": {},
            }
            path.write_text(json.dumps({**base, "status": "FAIL"}), encoding="utf-8")
            self.assertEqual(contract.load_certification(path), {})
            path.write_text(json.dumps({**base, "status": "PASS"}), encoding="utf-8")
            self.assertEqual(contract.load_certification(path).get("status"), "PASS")

    def test_certification_rejects_stale_hash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cert.json"
            payload = {
                "schema_version": contract.SCHEMA_VERSION,
                "status": "PASS",
                "selected_edge_count": 33,
                "implementation_hashes": {"bad": "hash"},
                "runtime_environment": contract.runtime_environment(),
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(contract.load_certification(path), {})

    def test_event_engine_refuses_uncertified_edge(self) -> None:
        features = pd.DataFrame({
            "Datetime": pd.to_datetime(["2026-01-01T00:00:00Z"]),
            "Bar_Start": pd.to_datetime(["2026-01-01T00:00:00Z"]),
            "Close": [1.0],
        })
        # Another hermetic orchestrator test temporarily replaces the
        # sys.modules entry for this module. Patch the real module object captured
        # at import time so this guardrail is independent of test execution order.
        with patch.object(native_event_engine, "event_certification", return_value={}):
            with self.assertRaises(NativeEventError):
                build_events_live_native(
                    _Runtime(), asset="XAUUSD", edge_id="XAUUSD/test_long",
                    features=features, state=None,
                )

    def test_ewm_variance_state_matches_pandas_across_restart(self) -> None:
        rng = np.random.default_rng(7)
        values = rng.normal(size=500).astype("float64")
        values[:50] = np.nan
        expected = pd.Series(values).ewm(span=100, adjust=False).var(bias=False).to_numpy()
        state = _ewm_var_state(values[:300], 100)
        observed = []
        for value in values[300:]:
            observed.append(_ewm_var_update(state, float(value)))
        np.testing.assert_allclose(
            np.asarray(observed), expected[300:], rtol=1e-13, atol=1e-15, equal_nan=True,
        )


if __name__ == "__main__":
    unittest.main()
