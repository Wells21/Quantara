"""Version-neutral joblib payloads for the FFD / HMM bundles and live continuation states.

venv39 (live, Phase C, allocator) runs pandas 2.3.3 and venv312 (research) pandas 3. A pandas object pickled under
pandas 3 cannot be unpickled under pandas 2 (NotImplementedError on the StringDtype / datetime64[us] array state),
and research runs on venv312 rewrite these files through the FFD / HMM verifiers. `dump` therefore stores every
DataFrame, Series, Index and Timestamp inside the payload as plain numpy arrays plus metadata, and `load` rebuilds
them (datetimes as datetime64[ns, tz]); payloads written before this module (plain pandas objects) load unchanged.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

_TAG = "__portable_pickle_v1__"


def _array(values: pd.Series | pd.Index) -> dict[str, Any]:
    dtype = values.dtype
    if isinstance(dtype, pd.DatetimeTZDtype) or (isinstance(dtype, np.dtype) and dtype.kind == "M"):
        idx = pd.DatetimeIndex(values)
        return {"kind": "datetime", "ns": idx.as_unit("ns").asi8.copy(), "nat": np.asarray(idx.isna()),
                "tz": None if idx.tz is None else str(idx.tz), "unit": getattr(idx, "unit", "ns")}
    if isinstance(dtype, pd.CategoricalDtype) or (isinstance(dtype, np.dtype) and dtype.kind == "m"):
        raise TypeError(f"portable_pickle: unsupported dtype {dtype}")
    if isinstance(dtype, np.dtype) and dtype.kind in "biuf":
        return {"kind": "numpy", "values": np.asarray(values).copy()}
    return {"kind": "object", "values": np.asarray(values, dtype=object).copy(),
            "text": isinstance(dtype, pd.StringDtype)}


_PANDAS_3 = int(pd.__version__.split(".")[0]) >= 3


def _restore_array(a: dict[str, Any]) -> Any:
    """numpy array or DatetimeIndex (original unit and time zone)."""
    if a["kind"] == "datetime":
        idx = pd.DatetimeIndex(np.asarray(a["ns"], dtype="int64").view("datetime64[ns]"))
        idx = idx.where(~np.asarray(a["nat"], dtype=bool)).as_unit(a.get("unit", "ns"))
        return idx if a["tz"] is None else idx.tz_localize("UTC").tz_convert(a["tz"])
    return a["values"]


def _restore_column(a: dict[str, Any], index: pd.Index) -> pd.Series:
    if a["kind"] == "object":
        col = pd.Series(a["values"], index=index, dtype=object)
        return col.astype("str") if a.get("text") and _PANDAS_3 else col
    return pd.Series(_restore_array(a), index=index)


def _index(index: pd.Index) -> dict[str, Any]:
    if isinstance(index, pd.RangeIndex):
        return {"kind": "range", "start": index.start, "stop": index.stop, "step": index.step, "name": index.name}
    return {"kind": "index", "array": _array(index), "name": index.name}


def _restore_index(i: dict[str, Any]) -> pd.Index:
    if i["kind"] == "range":
        return pd.RangeIndex(i["start"], i["stop"], i["step"], name=i["name"])
    return pd.Index(_restore_array(i["array"]), name=i["name"])


def to_portable(obj: Any) -> Any:
    if isinstance(obj, pd.DataFrame):
        if isinstance(obj.columns, pd.MultiIndex) or isinstance(obj.index, pd.MultiIndex):
            raise TypeError("portable_pickle: MultiIndex is not supported")
        return {_TAG: "dataframe", "columns": [c for c in obj.columns],
                "data": [_array(obj.iloc[:, i]) for i in range(obj.shape[1])], "index": _index(obj.index)}
    if isinstance(obj, pd.Series):
        return {_TAG: "series", "data": _array(obj), "index": _index(obj.index), "name": obj.name}
    if isinstance(obj, pd.Index):
        return {_TAG: "index", "index": _index(obj)}
    if isinstance(obj, pd.Timestamp):
        return {_TAG: "timestamp", "ns": int(obj.as_unit("ns").value), "tz": None if obj.tz is None else str(obj.tz)}
    if isinstance(obj, dict):
        return {k: to_portable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_portable(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(to_portable(v) for v in obj)
    return obj


def from_portable(obj: Any) -> Any:
    if isinstance(obj, dict):
        kind = obj.get(_TAG)
        if kind == "dataframe":
            index = _restore_index(obj["index"])
            # An object column keeps dtype object (never re-inferred); a text column (pandas 3 'str') comes back as
            # the running pandas' native text dtype: 'str' on pandas 3, object on pandas 2.
            cols = [_restore_column(a, index) for a in obj["data"]]
            frame = pd.concat(cols, axis=1) if cols else pd.DataFrame(index=index)
            frame.columns = obj["columns"]
            return frame
        if kind == "series":
            return _restore_column(obj["data"], _restore_index(obj["index"])).rename(obj["name"])
        if kind == "index":
            return _restore_index(obj["index"])
        if kind == "timestamp":
            stamp = pd.Timestamp(obj["ns"], unit="ns")
            return stamp if obj["tz"] is None else stamp.tz_localize("UTC").tz_convert(obj["tz"])
        return {k: from_portable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_portable(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(from_portable(v) for v in obj)
    return obj


def dump(obj: Any, path: str | Path) -> None:
    joblib.dump(to_portable(obj), path)


def load(path: str | Path) -> Any:
    return from_portable(joblib.load(path))
