"""ISO-8601-safe drop-in for pandas.to_datetime (history extension, 2026-11-07).

Since pandas 2, `pd.to_datetime` on strings without `format` infers ONE format from the first non-null value. pandas
writes tz-aware datetimes to CSV value by value, so a bar whose timestamp falls on a whole second is written
"2014-02-28 06:56:36+00:00" while its neighbours carry ".848000". When inference picks the fractional format, the
whole-second rows raise, or under errors="coerce" silently become NaT and get dropped. The 2022-2026 bars never had a
whole-second timestamp because the bar forge itself dropped those ticks (same bug, fixed 2026-11-07); the rebuilt bars
have ~0.1 %.

`to_datetime_iso` behaves exactly like `pd.to_datetime` except that string input with no `format` / `unit` is parsed
with format="ISO8601" (every value parsed on its own as ISO 8601). Numbers, datetimes and DataFrames are passed through
unchanged: format="ISO8601" would turn integer nanoseconds into NaT.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _first_non_null(values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, float) and np.isnan(v):
            continue
        if v is pd.NaT:
            continue
        return v
    return None


def _is_string_input(arg: Any) -> bool:
    if isinstance(arg, str):
        return True
    if isinstance(arg, pd.DataFrame):
        return False
    if isinstance(arg, (pd.Series, pd.Index)):
        if not (pd.api.types.is_object_dtype(arg.dtype) or pd.api.types.is_string_dtype(arg.dtype)):
            return False
        return isinstance(_first_non_null(arg.array), str)
    if isinstance(arg, np.ndarray):
        if arg.dtype.kind in "OU":
            return isinstance(_first_non_null(arg.ravel()), (str, np.str_))
        return False
    if isinstance(arg, (list, tuple)):
        return isinstance(_first_non_null(arg), str)
    return False


def to_datetime_iso(arg: Any, *args: Any, **kwargs: Any) -> Any:
    if "format" not in kwargs and "unit" not in kwargs and not args and _is_string_input(arg):
        kwargs["format"] = "ISO8601"
    return pd.to_datetime(arg, *args, **kwargs)
