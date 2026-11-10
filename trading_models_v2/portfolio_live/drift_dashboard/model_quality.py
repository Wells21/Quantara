"""
Model-quality panel: live meta-label calibration vs. each edge's Step04 OOF
reference calibration curve (v3 follow-up, built 2026-09-24).

Reference side: each edge's own
assets/<ASSET>/edges/<family_side>/artifacts/calibration/*_calibrated_oof_predictions.csv
(the Step04 output). Per reference_calibrated_probability_columns.md /
config/mldp_deviation_register.json D015: `live_calibrated_probability` on
THIS file is raw_probability through the single final live sigmoid
calibrator -- the live-consistent probability space -- so it (not the
CPCV-fold-averaged `calibrated_probability` column on this same file) is
what live results must be compared against. `quote_executable_meta_label`
is the exact label Gate D itself gates on (reference_gate_d_v3_auc_methodology.md).

Live side: closed trades don't carry their own scored probability, so this
joins portfolio_closed_trades.jsonl -> live_signal_audit.csv by signal_id to
recover the probability that was actually used to approve/size that trade
(live_signal_audit.csv's own "calibrated_probability" field IS the live
scoring pipeline's live-consistent value, despite the confusing column-name
overlap with the Step04 file's diagnostic column of the same name -- see
REQUIRED_THRESHOLD_PROBABILITY_COLUMN in live_settings.py). live_signal_audit.csv
is a rotating log, so not every historical closed trade's signal is still
present -- this is reported honestly as a match-rate, never backfilled or
assumed.

Outcome proxy for a closed live trade: realized_r > 0 (win/loss), the same
binary meta-label concept AFML uses, since live trades don't carry a
recorded meta_label field of their own.
"""

from __future__ import annotations

import glob
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

N_REFERENCE_BINS = 10


class ModelQualityError(RuntimeError):
    pass


def resolve_oof_predictions_path(edge_id: str) -> Path | None:
    if "/" not in edge_id:
        raise ModelQualityError(f"Malformed edge_id: {edge_id!r}")
    asset, family_side = edge_id.split("/", 1)
    pattern = str(
        _V2_ROOT / "assets" / asset / "edges" / family_side / "artifacts"
        / "calibration" / "*_calibrated_oof_predictions.csv"
    )
    matches = sorted(glob.glob(pattern))
    return Path(matches[-1]) if matches else None


def _roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """Mann-Whitney U based AUC -- no sklearn dependency, consistent with
    Gate D's own test family. Returns None if only one class is present."""
    pos = scores[y_true == 1]
    neg = scores[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    from scipy.stats import mannwhitneyu

    u_stat, _ = mannwhitneyu(pos, neg, alternative="two-sided")
    return float(u_stat / (len(pos) * len(neg)))


def _reference_calibration_curve(oof: pd.DataFrame) -> list[dict[str, float]]:
    df = oof[["live_calibrated_probability", "quote_executable_meta_label"]].dropna()
    if df.empty:
        return []
    df = df.copy()
    try:
        df["bin"] = pd.qcut(df["live_calibrated_probability"], q=N_REFERENCE_BINS, duplicates="drop")
    except ValueError:
        return []
    grouped = df.groupby("bin", observed=True).agg(
        mean_pred=("live_calibrated_probability", "mean"),
        mean_actual=("quote_executable_meta_label", "mean"),
        n=("quote_executable_meta_label", "size"),
    )
    return grouped.reset_index(drop=True).to_dict(orient="records")


@dataclass
class EdgeModelQuality:
    edge_id: str
    gap: str | None = None
    reference_auc: float | None = None
    reference_n: int = 0
    reference_curve: list[dict[str, float]] | None = None
    live_auc: float | None = None
    live_n_priced: int = 0
    live_n_total_closed: int = 0
    live_points: list[dict[str, float]] | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "gap": self.gap,
            "reference_auc": self.reference_auc,
            "reference_n": self.reference_n,
            "reference_curve": self.reference_curve,
            "live_auc": self.live_auc,
            "live_n_priced": self.live_n_priced,
            "live_n_total_closed": self.live_n_total_closed,
            "live_points": self.live_points,
            "note": self.note,
        }


def build_edge_model_quality(
    edge_id: str,
    live_trades: pd.DataFrame,
    signal_audit: pd.DataFrame | None,
) -> EdgeModelQuality:
    oof_path = resolve_oof_predictions_path(edge_id)
    if oof_path is None:
        return EdgeModelQuality(edge_id=edge_id, gap="no_step04_oof_predictions_available")

    oof = pd.read_csv(oof_path)
    required = {"live_calibrated_probability", "quote_executable_meta_label", "raw_probability"}
    if not required.issubset(oof.columns):
        return EdgeModelQuality(edge_id=edge_id, gap=f"oof_file_missing_columns:{required - set(oof.columns)}")

    labeled = oof.dropna(subset=["raw_probability", "quote_executable_meta_label"])
    reference_auc = _roc_auc(
        labeled["quote_executable_meta_label"].to_numpy(dtype=float),
        labeled["raw_probability"].to_numpy(dtype=float),
    )
    reference_curve = _reference_calibration_curve(oof)

    result = EdgeModelQuality(
        edge_id=edge_id,
        reference_auc=reference_auc,
        reference_n=len(labeled),
        reference_curve=reference_curve,
        live_n_total_closed=len(live_trades),
    )

    if signal_audit is None or signal_audit.empty or live_trades.empty:
        result.note = "No live_signal_audit rows available to join -- cannot recover the probability used on live trades."
        return result

    merged = live_trades[["signal_id", "realized_r"]].merge(
        signal_audit[["signal_id", "raw_probability", "calibrated_probability"]],
        on="signal_id",
        how="inner",
    )
    result.live_n_priced = len(merged)
    if merged.empty:
        result.note = (
            f"0/{len(live_trades)} closed trades' signal_id still present in live_signal_audit.csv "
            "(rotating log) -- cannot compare live calibration yet."
        )
        return result

    merged["win"] = (merged["realized_r"] > 0).astype(int)
    result.live_auc = _roc_auc(merged["win"].to_numpy(dtype=float), merged["raw_probability"].to_numpy(dtype=float))
    result.live_points = merged[["calibrated_probability", "win"]].rename(
        columns={"calibrated_probability": "predicted_probability", "win": "outcome"}
    ).to_dict(orient="records")

    match_pct = 100.0 * result.live_n_priced / max(result.live_n_total_closed, 1)
    note_parts = [
        f"{result.live_n_priced}/{result.live_n_total_closed} closed trades ({match_pct:.0f}%) "
        "matched a still-present live_signal_audit row."
    ]
    if result.live_auc is None:
        note_parts.append("Live AUC not computable yet -- need both wins and losses in the matched sample.")
    return EdgeModelQuality(
        edge_id=edge_id,
        reference_auc=reference_auc,
        reference_n=len(labeled),
        reference_curve=reference_curve,
        live_auc=result.live_auc,
        live_n_priced=result.live_n_priced,
        live_n_total_closed=result.live_n_total_closed,
        live_points=result.live_points,
        note=" ".join(note_parts),
    )


if __name__ == "__main__":
    import json as _json

    from portfolio_live.drift_dashboard.data_sources import (
        load_current_live_edge_ids,
        load_live_signal_audit,
        scope_closed_trades,
    )

    edge_ids = load_current_live_edge_ids()
    scoped = scope_closed_trades(live_edge_ids=edge_ids)
    try:
        audit = load_live_signal_audit()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load live_signal_audit.csv: {exc}")
        audit = None

    for eid in sorted(edge_ids):
        rows = scoped.in_scope[scoped.in_scope["edge_id"] == eid]
        if rows.empty:
            continue
        report = build_edge_model_quality(eid, rows, audit)
        print(f"=== {eid} ===")
        print(_json.dumps(report.to_dict(), indent=2, default=str))
