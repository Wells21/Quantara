"""
Verified HMM/regime feature integration for XAUUSD/roundlevelcascade_long.

Blueprint boundary:
- Regime features must come from the shared asset-level verified HMM artifact.
- The edge runtime may consume verified regime features through this module.
- The dashboard and Colab must not create or patch regime features.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_EDGE_ROOT = _THIS_FILE.parent
_V2_ROOT = _THIS_FILE.parents[4]
_REPO_ROOT = _THIS_FILE.parents[5]
for _path in (_EDGE_ROOT, _V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.regime.verify_asset_hmm_regime_manifest import verify_asset_hmm_regime_manifest  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

ASSET = "XAUUSD"
REGIME_FEATURE_NAMES = [
    "Regime_State",
    "Regime_Entropy",
    "Regime_Duration",
    "Prob_State_0",
    "Prob_State_1",
    "Prob_State_2",
    "Prob_State_3",
]


class RegimeFeatureError(RuntimeError):
    """Raised when verified regime feature integration must fail closed."""


def load_verified_regime_features(asset: str = ASSET) -> pd.DataFrame:
    """Load verified shared asset-level HMM regime features."""

    verification = verify_asset_hmm_regime_manifest(asset)
    path = _V2_ROOT / verification["features"]
    if not path.exists():
        raise RegimeFeatureError(f"Verified regime feature path does not exist: {path}")

    df = pd.read_csv(path)
    required = ["Datetime", *REGIME_FEATURE_NAMES]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise RegimeFeatureError(f"Regime feature artifact missing required columns: {missing}")

    df["Datetime"] = to_datetime_iso(df["Datetime"], utc=True, errors="coerce")
    for col in REGIME_FEATURE_NAMES:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required).copy()
    df = df.sort_values("Datetime").reset_index(drop=True)

    if df.empty:
        raise RegimeFeatureError("Verified regime feature artifact is empty after normalization.")
    if not np.isfinite(df[REGIME_FEATURE_NAMES].to_numpy(dtype=float)).all():
        raise RegimeFeatureError("Regime features contain non-finite values.")
    return df[["Datetime", *REGIME_FEATURE_NAMES]].copy()


def add_verified_regime_features(features: pd.DataFrame, *, asset: str = ASSET) -> pd.DataFrame:
    """Attach verified HMM regime features to an edge feature table."""

    if "Datetime" not in features.columns:
        raise RegimeFeatureError("Feature table missing Datetime.")
    out = features.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    existing = [col for col in REGIME_FEATURE_NAMES if col in out.columns]
    if existing:
        out.drop(columns=existing, inplace=True)

    regime = load_verified_regime_features(asset)
    out = out.merge(regime, on="Datetime", how="left", validate="one_to_one")
    return out.sort_values("Datetime").reset_index(drop=True)


def regime_feature_names() -> list[str]:
    return list(REGIME_FEATURE_NAMES)
