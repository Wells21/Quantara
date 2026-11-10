"""Canonical MLDP/Quantara event-outcome ontology.

AFML/MLAM do not define these four Quantara column names. This is a Quantara
production extension that keeps distinct economic objects distinct:
gross research, quote-executable, net simulated executable, and live realized.

A model probability, its label, its information end time, and its calibration
target must refer to one and the same outcome object.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

ONTOLOGY_VERSION = "quantara_mldp_event_outcome_v1"

class OutcomeOntologyError(RuntimeError):
    pass

@dataclass(frozen=True)
class OutcomeLayer:
    name: str
    return_column: str
    label_column: str
    end_time_column: str
    contract_version: str
    description: str

LAYERS: Mapping[str, OutcomeLayer] = {
    "gross": OutcomeLayer(
        "gross", "gross_r", "gross_meta_label", "gross_t1",
        "quantara_mldp_gross_outcome_v1",
        "Theoretical side-adjusted research outcome on frozen event geometry.",
    ),
    "quote_executable": OutcomeLayer(
        "quote_executable", "quote_executable_r", "quote_executable_meta_label",
        "quote_executable_t1", "quantara_mldp_quote_executable_outcome_v1",
        "Historical Bid/Ask executable outcome under the frozen execution contract.",
    ),
    "net_executable": OutcomeLayer(
        "net_executable", "net_executable_r", "net_executable_meta_label",
        "net_executable_t1", "quantara_mldp_net_executable_outcome_v1",
        "Quote-executable outcome after every defensible historically modelled cost.",
    ),
    "live_realized": OutcomeLayer(
        "live_realized", "live_realized_r", "live_realized_meta_label",
        "live_realized_t1", "quantara_mldp_live_realized_outcome_v1",
        "Actual broker-fill outcome and actual charged costs.",
    ),
}

LEGACY_ALIASES: Mapping[str, str] = {
    "realized_r": "gross_r",
    "meta_label": "gross_meta_label",
    "t1": "gross_t1",
    "executed_r": "quote_executable_r",
    "executed_meta_label": "quote_executable_meta_label",
    "exit_time": "quote_executable_t1",
}

def get_layer(name: str) -> OutcomeLayer:
    try:
        return LAYERS[str(name)]
    except KeyError as exc:
        raise OutcomeOntologyError(
            f"Unknown outcome layer {name!r}. Expected one of {sorted(LAYERS)}."
        ) from exc

def label_from_r(values: Any) -> np.ndarray:
    r = np.asarray(values, dtype="float64")
    if not np.isfinite(r).all():
        raise OutcomeOntologyError("Outcome R must be finite before a binary label is formed.")
    return (r > 0.0).astype("int64")

def _series_equal(left: pd.Series, right: pd.Series, canonical: str) -> bool:
    if canonical.endswith("_t1"):
        a = to_datetime_iso(left, utc=True, errors="coerce")
        b = to_datetime_iso(right, utc=True, errors="coerce")
        return bool(a.equals(b))
    if canonical.endswith("_meta_label"):
        a = pd.to_numeric(left, errors="coerce")
        b = pd.to_numeric(right, errors="coerce")
        if a.isna().any() or b.isna().any():
            return False
        return bool(np.array_equal(a.astype("int64").to_numpy(), b.astype("int64").to_numpy()))
    a = pd.to_numeric(left, errors="coerce").to_numpy(dtype="float64")
    b = pd.to_numeric(right, errors="coerce").to_numpy(dtype="float64")
    return bool(
        np.all(np.isfinite(a)) and np.all(np.isfinite(b))
        and np.allclose(a, b, rtol=0.0, atol=1e-12)
    )

def canonicalize_legacy_outcomes(
    frame: pd.DataFrame, *, copy: bool = True, strict: bool = True
) -> pd.DataFrame:
    out = frame.copy() if copy else frame
    for legacy, canonical in LEGACY_ALIASES.items():
        if legacy not in out.columns:
            continue
        if canonical in out.columns:
            if strict and not _series_equal(out[legacy], out[canonical], canonical):
                raise OutcomeOntologyError(
                    f"Legacy column {legacy!r} conflicts with canonical {canonical!r}."
                )
        else:
            out[canonical] = out[legacy]
    return out

def validate_layer(
    frame: pd.DataFrame,
    layer_name: str,
    *,
    t0_column: str = "Datetime",
    require_label: bool = True,
) -> dict[str, Any]:
    layer = get_layer(layer_name)
    required = [t0_column, layer.return_column, layer.end_time_column]
    if require_label:
        required.append(layer.label_column)
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise OutcomeOntologyError(f"{layer_name}: missing required columns: {missing}")
    t0 = to_datetime_iso(frame[t0_column], utc=True, errors="coerce")
    t1 = to_datetime_iso(frame[layer.end_time_column], utc=True, errors="coerce")
    r = pd.to_numeric(frame[layer.return_column], errors="coerce")
    if t0.isna().any() or t1.isna().any():
        raise OutcomeOntologyError(f"{layer_name}: event timestamps contain NaT.")
    if bool((t1 < t0).any()):
        raise OutcomeOntologyError(f"{layer_name}: end time precedes event start.")
    if r.isna().any() or not np.isfinite(r.to_numpy(dtype="float64")).all():
        raise OutcomeOntologyError(f"{layer_name}: R contains NaN/inf.")
    expected = label_from_r(r.to_numpy(dtype="float64"))
    if require_label:
        label = pd.to_numeric(frame[layer.label_column], errors="coerce")
        if label.isna().any():
            raise OutcomeOntologyError(f"{layer_name}: label contains invalid values.")
        actual = label.astype("int64").to_numpy()
        if not np.array_equal(actual, expected):
            mismatches = int(np.count_nonzero(actual != expected))
            raise OutcomeOntologyError(
                f"{layer_name}: {layer.label_column} != int({layer.return_column} > 0); "
                f"mismatches={mismatches}."
            )
    return {
        "status": "PASS",
        "ontology_version": ONTOLOGY_VERSION,
        "layer": layer.name,
        "contract_version": layer.contract_version,
        "rows": int(len(frame)),
        "positive_rate": float(expected.mean()) if len(expected) else float("nan"),
    }

def target_columns(layer_name: str) -> tuple[str, str, str]:
    layer = get_layer(layer_name)
    return layer.return_column, layer.label_column, layer.end_time_column
