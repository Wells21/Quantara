"""shared/io/portable_pickle.py: exact round trip of the pandas objects in the FFD / HMM bundles and live states.

The cross-version property (written on pandas 3, read on pandas 2 and the reverse) needs both interpreters; it was
verified on the 31 real files on 2026-10-30 (AFML_MLAM_WALKTHROUGH.md s.4Q.4). These tests pin the single-version
round trip and the legacy pass-through.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from shared.io import portable_pickle as pp


def _frame() -> pd.DataFrame:
    dt = pd.DatetimeIndex(["2026-01-01 00:00:01.5", None, "2026-01-02"]).tz_localize("UTC").as_unit("us")
    return pd.DataFrame({
        "Datetime": dt,
        "Close": [1.5, np.nan, 2.5],
        "Ticks": np.array([1, 2, 3], dtype="int64"),
        "Mode": pd.array(["a", None, "c"], dtype=object),
        "Stamp": pd.Series([pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-01-02", tz="UTC"), None], dtype=object),
        "Flag": pd.Series([True, None, False], dtype=object),
    })


class TestPortablePickle(unittest.TestCase):
    def roundtrip(self, obj):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.joblib"
            pp.dump(obj, path)
            raw = joblib.load(path)
            return raw, pp.load(path)

    def test_frame_roundtrip_is_exact(self):
        frame = _frame()
        raw, back = self.roundtrip({"raw_buffer_df": frame, "frozen_d": 0.25, "arr": np.arange(3.0)})
        self.assertNotIsInstance(raw["raw_buffer_df"], pd.DataFrame)   # stored without pandas objects
        out = back["raw_buffer_df"]
        pd.testing.assert_frame_equal(out, frame)
        self.assertEqual(str(out["Datetime"].dtype), "datetime64[us, UTC]")
        self.assertEqual(out["Stamp"].dtype, object)                  # object column is not re-inferred
        self.assertEqual(back["frozen_d"], 0.25)
        np.testing.assert_array_equal(back["arr"], np.arange(3.0))

    def test_series_index_timestamp(self):
        s = pd.Series([1.0, 2.0], index=pd.DatetimeIndex(["2026-01-01", "2026-01-02"], tz="UTC"), name="x")
        ts = pd.Timestamp("2026-03-04 05:06:07.123456789", tz="UTC")
        _, back = self.roundtrip({"s": s, "ts": ts, "idx": pd.RangeIndex(2, 10, 2)})
        pd.testing.assert_series_equal(back["s"], s)
        self.assertEqual(back["ts"], ts)
        pd.testing.assert_index_equal(back["idx"], pd.RangeIndex(2, 10, 2))

    def test_legacy_payload_loads_unchanged(self):
        frame = _frame()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "legacy.joblib"
            joblib.dump({"raw_buffer_df": frame}, path)
            pd.testing.assert_frame_equal(pp.load(path)["raw_buffer_df"], frame)

    def test_unsupported_dtype_fails_loudly(self):
        with self.assertRaises(TypeError):
            pp.to_portable(pd.DataFrame({"c": pd.Categorical(["a", "b"])}))


if __name__ == "__main__":
    unittest.main()
