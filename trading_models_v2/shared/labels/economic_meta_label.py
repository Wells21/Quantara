"""Quantara canonical economic meta-label contract.

The triple-barrier exit type and economic trade outcome are intentionally
separate concepts.

Canonical gross research target:
    meta_label = 1 iff realized_r > 0.0, else 0

Diagnostic PT-hit target:
    pt_hit_label = 1 iff barrier_hit == "pt", else 0
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Mapping

import numpy as np
import pandas as pd

CONTRACT_VERSION = "quantara_v3_gross_economic_meta_label_v1"
META_LABEL_COL = "meta_label"
PT_HIT_LABEL_COL = "pt_hit_label"
REALIZED_R_COL = "realized_r"
BARRIER_HIT_COL = "barrier_hit"
EXECUTED_R_COL = "executed_r"
EXECUTED_META_LABEL_COL = "executed_meta_label"


class EconomicMetaLabelContractError(RuntimeError):
    """Raised when economic label semantics cannot be enforced safely."""


def _distribution(series: pd.Series) -> dict[str, float]:
    if len(series) == 0:
        return {}
    values = pd.to_numeric(series, errors="coerce")
    if values.isna().any():
        raise EconomicMetaLabelContractError("Label distribution contains non-numeric values.")
    return {
        str(int(k)): float(v)
        for k, v in values.astype("int64").value_counts(normalize=True).sort_index().to_dict().items()
    }


def _validated_economics(df: pd.DataFrame, *, context: str) -> tuple[pd.Series, pd.Series]:
    missing = [c for c in (REALIZED_R_COL, BARRIER_HIT_COL) if c not in df.columns]
    if missing:
        raise EconomicMetaLabelContractError(
            f"{context}: model rows missing required economic label columns: {missing}"
        )

    r = pd.to_numeric(df[REALIZED_R_COL], errors="coerce")
    arr = r.to_numpy(dtype="float64")
    if r.isna().any() or not np.isfinite(arr).all():
        raise EconomicMetaLabelContractError(
            f"{context}: realized_r must be finite for every canonical model row."
        )

    barrier = df[BARRIER_HIT_COL].astype(str).str.strip().str.lower()
    pt_bad = barrier.eq("pt") & ~(r > 0.0)
    sl_bad = barrier.eq("sl") & ~(r < 0.0)
    if pt_bad.any():
        raise EconomicMetaLabelContractError(
            f"{context}: {int(pt_bad.sum())} PT rows have non-positive realized_r."
        )
    if sl_bad.any():
        raise EconomicMetaLabelContractError(
            f"{context}: {int(sl_bad.sum())} SL rows have non-negative realized_r."
        )
    return r.astype("float64"), barrier


def enforce_gross_economic_model_row_contract(
    model_rows: pd.DataFrame,
    model_row_manifest: Mapping[str, Any] | None = None,
    *,
    edge_id: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Canonicalize model rows and annotate their model-row manifest."""

    context = edge_id or "unknown_edge"
    if model_rows.empty:
        raise EconomicMetaLabelContractError(f"{context}: model rows are empty.")

    out = model_rows.copy()
    r, barrier = _validated_economics(out, context=context)
    out[META_LABEL_COL] = (r > 0.0).astype("int64")
    out[PT_HIT_LABEL_COL] = barrier.eq("pt").astype("int64")

    manifest = dict(model_row_manifest or {})
    vertical = barrier.eq("vertical")
    vertical_r = r.loc[vertical]
    manifest.update({
        "economic_meta_label_contract_version": CONTRACT_VERSION,
        "meta_label_semantics": "1 iff realized_r > 0.0; else 0",
        "pt_hit_label_semantics": "1 iff barrier_hit == 'pt'; else 0",
        "meta_label_distribution": _distribution(out[META_LABEL_COL]),
        "pt_hit_label_distribution": _distribution(out[PT_HIT_LABEL_COL]),
        "vertical_exit_economics": {
            "count": int(vertical.sum()),
            "rate": float(vertical.mean()),
            "positive_count": int((vertical_r > 0.0).sum()),
            "positive_rate_within_vertical": float((vertical_r > 0.0).mean()) if len(vertical_r) else 0.0,
            "mean_realized_r": float(vertical_r.mean()) if len(vertical_r) else 0.0,
            "median_realized_r": float(vertical_r.median()) if len(vertical_r) else 0.0,
        },
    })
    notes = list(manifest.get("notes", []))
    note = (
        "Canonical Quantara V3 meta_label is economic: 1 iff realized_r > 0.0. "
        "PT-hit identity is preserved separately in pt_hit_label."
    )
    if note not in notes:
        notes.append(note)
    manifest["notes"] = notes
    return out, manifest


def economic_model_rows_contract(*, edge_id: str | None = None):
    """Decorate runtime.build_model_rows with the canonical economic target.

    The runtime boundary is uniform across Quantara even where local model-row
    builders use different legacy variable names. The wrapped function must
    return ``(model_rows, model_row_manifest)``.
    """

    def decorate(func):
        @wraps(func)
        def wrapped(*args: Any, **kwargs: Any):
            result = func(*args, **kwargs)
            if not isinstance(result, tuple) or len(result) != 2:
                raise EconomicMetaLabelContractError(
                    f"{edge_id or getattr(func, '__module__', 'unknown_edge')}: "
                    "build_model_rows must return (DataFrame, manifest)."
                )
            rows, manifest = result
            if not isinstance(rows, pd.DataFrame):
                raise EconomicMetaLabelContractError(
                    f"{edge_id or getattr(func, '__module__', 'unknown_edge')}: "
                    "build_model_rows first return value must be a pandas DataFrame."
                )
            if manifest is not None and not isinstance(manifest, Mapping):
                raise EconomicMetaLabelContractError(
                    f"{edge_id or getattr(func, '__module__', 'unknown_edge')}: "
                    "build_model_rows manifest must be mapping-like."
                )
            return enforce_gross_economic_model_row_contract(
                rows, manifest, edge_id=edge_id
            )

        return wrapped

    return decorate


def validate_gross_economic_model_row_contract(
    model_rows: pd.DataFrame,
    *,
    edge_id: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless rows already satisfy the canonical contract."""

    canonical, _ = enforce_gross_economic_model_row_contract(model_rows, {}, edge_id=edge_id)
    for col in (META_LABEL_COL, PT_HIT_LABEL_COL):
        if col not in model_rows.columns:
            raise EconomicMetaLabelContractError(f"{edge_id or 'unknown_edge'}: {col} is missing.")

    actual_meta = pd.to_numeric(model_rows[META_LABEL_COL], errors="coerce")
    actual_pt = pd.to_numeric(model_rows[PT_HIT_LABEL_COL], errors="coerce")
    if actual_meta.isna().any() or not np.array_equal(
        actual_meta.astype("int64").to_numpy(), canonical[META_LABEL_COL].to_numpy()
    ):
        raise EconomicMetaLabelContractError(
            f"{edge_id or 'unknown_edge'}: meta_label does not match realized_r > 0."
        )
    if actual_pt.isna().any() or not np.array_equal(
        actual_pt.astype("int64").to_numpy(), canonical[PT_HIT_LABEL_COL].to_numpy()
    ):
        raise EconomicMetaLabelContractError(
            f"{edge_id or 'unknown_edge'}: pt_hit_label does not match barrier_hit == 'pt'."
        )
    return {
        "status": "PASS",
        "contract_version": CONTRACT_VERSION,
        "rows": int(len(model_rows)),
        "positive_rate": float(canonical[META_LABEL_COL].mean()),
        "pt_hit_rate": float(canonical[PT_HIT_LABEL_COL].mean()),
    }


def executable_meta_label(executed_r: Any) -> np.ndarray:
    """Phase-2 primitive: executable success is 1 iff executed_r > 0."""

    r = np.asarray(executed_r, dtype="float64")
    if not np.isfinite(r).all():
        raise EconomicMetaLabelContractError("executed_r must be finite.")
    return (r > 0.0).astype("int64")


def _run_self_test() -> None:
    df = pd.DataFrame({
        "barrier_hit": ["pt", "sl", "vertical", "vertical"],
        "realized_r": [2.0, -1.0, 0.25, -0.10],
        "meta_label": [1, 0, 0, 0],
    })
    out, manifest = enforce_gross_economic_model_row_contract(df, {}, edge_id="SELFTEST/x_long")
    assert out["meta_label"].tolist() == [1, 0, 1, 0]
    assert out["pt_hit_label"].tolist() == [1, 0, 0, 0]
    assert manifest["economic_meta_label_contract_version"] == CONTRACT_VERSION
    validate_gross_economic_model_row_contract(out, edge_id="SELFTEST/x_long")
    print("✅ economic_meta_label contract self-test passed.")


if __name__ == "__main__":
    _run_self_test()
