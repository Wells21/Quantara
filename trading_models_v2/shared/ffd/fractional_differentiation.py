"""
Shared V2 fractional differentiation runtime.

Blueprint purpose
-----------------
FFD is shared runtime infrastructure, not dashboard logic and not a hidden V1
research-only artifact.

V1 reference
------------
The XAUUSD trend V1 FFD script:

- estimated d from the initial training segment only
- froze d after estimation
- applied Fracdiff(d, window=1000, mode="valid") to log OHLC
- reused the Close-estimated d for Open/High/Low/Close
- saved a live bundle with the latest raw OHLC buffer for continuation

V2 contract
-----------
This module preserves that idea, but makes it reusable across assets/edges.

d selection (AFML Ch.5 walkthrough, owner decision 2026-10-23)
-------------------------------------------------------------
AFML s.5.6 / Snippet 5.4 (pp.84-85): d* is the minimum d whose FFD(log price) passes the ADF test at 95%,
measured on the whole series. Quantara: FracdiffStat (binary search on [0, 1], precision 0.01, ADF =
statsmodels defaults with AIC lag selection) on log Close of the FULL research history (every bar in the
research store; the sealed holdout and live never enter), fixed 1,000-term window (deviation D032). d is then
frozen: holdout replays and live receive it (frozen_d) and never re-estimate it. The retired rule (first
20,000 bars only) gave d too small for 8 of 10 assets, whose FFD series then failed ADF on the full history.

No edge may use FFD features until:

1. This shared runtime produces the FFD columns.
2. The FFD artifact manifest is written and hash-verifiable.
3. The edge feature schema marks the FFD features live_available only after
   research/live parity is proven.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np
import pandas as pd

try:
    from fracdiff.sklearn import Fracdiff, FracdiffStat
except ImportError as exc:  # pragma: no cover - environment specific
    raise ImportError(
        "V2 FFD runtime requires fracdiff.sklearn, matching the V1 FFD pipeline. "
        "Install the fracdiff package in the active venv before running FFD."
    ) from exc

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.io import portable_pickle  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.contracts import V2_CONTRACT_VERSION  # noqa: E402


DEFAULT_D_ESTIMATION_BARS: int | None = None  # None = full research history (AFML Snippet 5.4)
D_POLICY_ESTIMATED = "full_research_history_then_freeze"
D_POLICY_SUPPLIED = "frozen_d_supplied"
ADF_PVALUE = 0.05
DEFAULT_FFD_WINDOW = 1000
DEFAULT_MIN_CORR_THRESHOLD = 0.80
DEFAULT_LIVE_BUFFER_MARGIN = 10
OHLC_COLUMNS = ["Open", "High", "Low", "Close"]
EPS = 1e-12


class FFDContractError(RuntimeError):
    """Raised when FFD inputs/outputs violate the V2 runtime contract."""


@dataclass(frozen=True)
class FFDConfig:
    """Configuration for V1-compatible frozen fractional differentiation."""

    d_estimation_bars: int | None = DEFAULT_D_ESTIMATION_BARS
    ffd_window: int = DEFAULT_FFD_WINDOW
    min_corr_threshold: float = DEFAULT_MIN_CORR_THRESHOLD
    live_buffer_margin: int = DEFAULT_LIVE_BUFFER_MARGIN

    @property
    def live_buffer_bars(self) -> int:
        return int(self.ffd_window + self.live_buffer_margin)

    def validate(self) -> None:
        if self.d_estimation_bars is not None and self.d_estimation_bars <= 0:
            raise FFDContractError("d_estimation_bars must be > 0 (or None for the full research history)")
        if self.ffd_window <= 0:
            raise FFDContractError("ffd_window must be > 0")
        if not (0.0 < self.min_corr_threshold <= 1.0):
            raise FFDContractError("min_corr_threshold must be in (0, 1]")
        if self.live_buffer_margin < 0:
            raise FFDContractError("live_buffer_margin must be >= 0")


def normalize_bars_for_ffd(bars: pd.DataFrame) -> pd.DataFrame:
    """Normalize a V2 bar table for FFD computation."""

    required = ["Datetime", *OHLC_COLUMNS]
    missing = [col for col in required if col not in bars.columns]
    if missing:
        raise FFDContractError(f"FFD input bars missing required columns: {missing}")

    out = bars.copy()
    out["Datetime"] = to_datetime_iso(out["Datetime"], utc=True, errors="coerce")
    for col in OHLC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=required).copy()
    out = out.sort_values("Datetime").reset_index(drop=True)

    if out.empty:
        raise FFDContractError("No valid FFD input rows remain after normalization.")
    if (out[OHLC_COLUMNS] <= 0).any().any():
        raise FFDContractError("FFD input OHLC contains non-positive values; cannot take logs safely.")
    if not out["Datetime"].is_monotonic_increasing:
        raise FFDContractError("FFD input Datetime must be sorted ascending.")

    return out


def estimate_optimal_d(close_series: pd.Series, *, window: int, estimation_bars: int | None = None) -> float:
    """Minimum d whose FFD(log Close) passes ADF (p < 0.05): full research history when estimation_bars is None,
    else the first estimation_bars rows. The caller freezes it."""

    need = window + 10 if estimation_bars is None else max(window + 10, estimation_bars)
    if len(close_series) < need:
        raise FFDContractError(f"Not enough rows to estimate d safely. Need at least {need} rows, got {len(close_series)}.")

    train_segment = close_series.copy() if estimation_bars is None else close_series.iloc[:estimation_bars].copy()
    close_log = np.log(train_segment.to_numpy(dtype="float64")).reshape(-1, 1)

    fs = FracdiffStat(window=window, mode="valid")
    fs.fit(close_log)
    d = float(fs.d_[0])

    if not math.isfinite(d):
        raise FFDContractError(f"Estimated d is not finite: {d}")
    if d < 0 or d > 1.5:
        raise FFDContractError(f"Estimated d looks abnormal: {d:.6f}")

    return d


def apply_frozen_ffd_to_ohlc(bars: pd.DataFrame, *, d: float, window: int) -> pd.DataFrame:
    """Apply a frozen FFD transform to log OHLC using one frozen d."""

    if not math.isfinite(d):
        raise FFDContractError(f"Frozen d must be finite, got {d}")
    if window <= 0:
        raise FFDContractError("FFD window must be > 0")

    df = normalize_bars_for_ffd(bars)
    if len(df) <= window:
        raise FFDContractError(f"Need more than window={window} rows for FFD; got {len(df)}")

    fd = Fracdiff(d=d, window=window, mode="valid")

    close_log = np.log(df["Close"].to_numpy(dtype="float64")).reshape(-1, 1)
    close_ffd = fd.fit_transform(close_log)
    valid_length = len(close_ffd)
    if valid_length == 0:
        raise FFDContractError("FFD produced zero rows. Check window size and input length.")

    out = df.iloc[-valid_length:].copy().reset_index(drop=True)

    for col in OHLC_COLUMNS:
        col_log = np.log(df[col].to_numpy(dtype="float64")).reshape(-1, 1)
        transformed = close_ffd.flatten() if col == "Close" else fd.transform(col_log).flatten()
        if len(transformed) != valid_length:
            raise FFDContractError(f"Length mismatch after FFD on {col}: {len(transformed)} != {valid_length}")
        out[f"{col}_FFD"] = transformed

    return out


def memory_retention_report(ffd_bars: pd.DataFrame) -> dict[str, float]:
    """Report correlation between log original OHLC and FFD columns."""

    required = [*OHLC_COLUMNS, *(f"{col}_FFD" for col in OHLC_COLUMNS)]
    missing = [col for col in required if col not in ffd_bars.columns]
    if missing:
        raise FFDContractError(f"Cannot build memory report; missing columns: {missing}")

    report: dict[str, float] = {}
    for col in OHLC_COLUMNS:
        original_log = np.log(ffd_bars[col].to_numpy(dtype="float64"))
        ffd_values = ffd_bars[f"{col}_FFD"].to_numpy(dtype="float64")
        corr = np.corrcoef(original_log, ffd_values)[0, 1]
        report[f"{col}_orig_vs_ffd_corr"] = float(corr)
    return report


def stationarity_report(ffd_bars: pd.DataFrame) -> dict[str, Any]:
    """ADF (statsmodels defaults, the test FracdiffStat uses) on Close_FFD: full sample and each half (diagnostic)."""
    from statsmodels.tsa.stattools import adfuller

    x = ffd_bars["Close_FFD"].to_numpy(dtype="float64")
    half = len(x) // 2

    def _adf(values: np.ndarray) -> dict[str, float]:
        import warnings

        with warnings.catch_warnings():  # statsmodels >= 0.15 FutureWarning about its return type; tuple kept here
            warnings.simplefilter("ignore", FutureWarning)
            stat, pvalue, lags, nobs, *_ = adfuller(values)
        return {"adf_stat": float(stat), "p_value": float(pvalue), "lags": int(lags), "nobs": int(nobs)}

    full = _adf(x)
    return {
        "test": "statsmodels.adfuller defaults (regression='c', autolag='AIC')",
        "p_value_threshold": ADF_PVALUE,
        "full": full,
        "first_half": _adf(x[:half]),
        "second_half": _adf(x[half:]),
        "full_passed": bool(full["p_value"] < ADF_PVALUE),
    }


def build_live_bundle(raw_bars: pd.DataFrame, *, d: float, config: FFDConfig) -> dict[str, Any]:
    """Build the V2 live-continuation bundle for FFD."""

    df = normalize_bars_for_ffd(raw_bars)
    raw_buffer_df = df[["Datetime", *OHLC_COLUMNS]].iloc[-config.live_buffer_bars :].copy()

    return {
        "schema_version": V2_CONTRACT_VERSION,
        "runtime_module": "shared.ffd.fractional_differentiation",
        "frozen_d": float(d),
        "ffd_window": int(config.ffd_window),
        "live_buffer_bars": int(config.live_buffer_bars),
        "ohlc_columns": list(OHLC_COLUMNS),
        "raw_buffer_df": raw_buffer_df,
        "last_observation_timestamp": str(df["Datetime"].iloc[-1]),
        "uses_future_data": False,
        "d_reestimated_live": False,
        "notes": [
            "d is frozen after initial research estimation and must not be re-estimated live.",
            "Live should append each new closed bar to raw_buffer_df and keep latest live_buffer_bars rows.",
            "FFD is computed from closed-bar OHLC only."
        ],
    }


def write_ffd_outputs(
    *,
    ffd_bars: pd.DataFrame,
    output_csv: Path,
    live_bundle: Mapping[str, Any],
    live_bundle_path: Path,
    manifest: Mapping[str, Any],
    manifest_path: Path,
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    ffd_bars.to_csv(output_csv, index=False)
    portable_pickle.dump(dict(live_bundle), live_bundle_path)
    manifest_path.write_text(json.dumps(dict(manifest), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def run_ffd_pipeline(
    bars: pd.DataFrame,
    *,
    asset: str,
    source_bars_path: str,
    config: FFDConfig | None = None,
    frozen_d: float | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Run the V2 FFD pipeline in memory and return bars, bundle, manifest."""

    cfg = config or FFDConfig()
    cfg.validate()
    df = normalize_bars_for_ffd(bars)

    d = float(frozen_d) if frozen_d is not None else estimate_optimal_d(
        df["Close"],
        window=cfg.ffd_window,
        estimation_bars=cfg.d_estimation_bars,
    )
    ffd_bars = apply_frozen_ffd_to_ohlc(df, d=d, window=cfg.ffd_window)
    retention = memory_retention_report(ffd_bars)
    min_corr = min(retention.values())
    stationarity = stationarity_report(ffd_bars)
    # An estimated d must give a stationary feature on the data it was chosen on (AFML s.5.6). A supplied d (holdout
    # workspace, reproduction) is reported, not gated: its sample includes data the d was not chosen on.
    if frozen_d is None and not stationarity["full_passed"]:
        raise FFDContractError(
            f"Close_FFD at estimated d={d:.6f} fails ADF on the estimation sample (p={stationarity['full']['p_value']:.4f})."
        )

    live_bundle = build_live_bundle(df, d=d, config=cfg)
    manifest = {
        "schema_version": V2_CONTRACT_VERSION,
        "asset": asset.upper(),
        "source_bars_path": source_bars_path,
        "runtime_module": "shared.ffd.fractional_differentiation",
        "d_estimation_policy": D_POLICY_SUPPLIED if frozen_d is not None else D_POLICY_ESTIMATED,
        "d_estimation_bars": None if cfg.d_estimation_bars is None else int(cfg.d_estimation_bars),
        "d_estimation_rows": None if frozen_d is not None else int(len(df) if cfg.d_estimation_bars is None else cfg.d_estimation_bars),
        "d_selection_rule": "min d on [0,1] (precision 0.01) with ADF p < 0.05 on FFD(log Close), fixed window (AFML Snippet 5.4)",
        "d_selection_scope": "full research history; sealed holdout and live never used; frozen after selection",
        "stationarity": stationarity,
        "ffd_window": int(cfg.ffd_window),
        "live_buffer_bars": int(cfg.live_buffer_bars),
        "frozen_d": float(d),
        "rows_input": int(len(df)),
        "rows_output": int(len(ffd_bars)),
        "start_output": str(ffd_bars["Datetime"].min()),
        "end_output": str(ffd_bars["Datetime"].max()),
        "memory_retention": retention,
        "min_memory_retention_corr": float(min_corr),
        "min_corr_threshold": float(cfg.min_corr_threshold),
        "memory_retention_passed": bool(min_corr >= cfg.min_corr_threshold),
        "uses_future_data": False,
        "d_reestimated_live": False,
        "applied_columns": list(OHLC_COLUMNS),
        "output_columns": [f"{col}_FFD" for col in OHLC_COLUMNS],
        "notes": [
            "d estimated on the full research history (AFML Snippet 5.4), then frozen; holdout/live receive it.",
            "Frozen d is reused for Open/High/Low/Close to preserve structural consistency.",
            "FFD is applied to log OHLC.",
            "Live runtime must use frozen d and buffer continuation; it must not re-estimate d live."
        ],
    }
    return ffd_bars, live_bundle, manifest


def _make_self_test_bars(n: int = 1300) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    timestamps = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, size=n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0001, 0.001, size=n))
    low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0001, 0.001, size=n))
    return pd.DataFrame({"Datetime": timestamps, "Open": open_, "High": high, "Low": low, "Close": close})


def _run_self_test() -> None:
    bars = _make_self_test_bars()
    cfg = FFDConfig(d_estimation_bars=300, ffd_window=100, min_corr_threshold=0.10, live_buffer_margin=10)
    ffd_bars, bundle, manifest = run_ffd_pipeline(
        bars,
        asset="SELF_TEST",
        source_bars_path="self_test",
        config=cfg,
        frozen_d=0.4,
    )

    if len(ffd_bars) == 0:
        raise SystemExit("❌ FFD self-test produced zero rows")
    for col in ["Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]:
        if col not in ffd_bars.columns:
            raise SystemExit(f"❌ Missing FFD column: {col}")
    if bundle["d_reestimated_live"] is not False:
        raise SystemExit("❌ Live bundle must declare d_reestimated_live=False")
    if manifest["uses_future_data"] is not False:
        raise SystemExit("❌ Manifest must declare uses_future_data=False")

    print("✅ shared.ffd.fractional_differentiation self-test passed.")
    print(f"Input rows: {len(bars)}")
    print(f"Output rows: {len(ffd_bars)}")
    print(f"Frozen d: {manifest['frozen_d']}")
    print(f"FFD window: {manifest['ffd_window']}")
    print(f"Live buffer bars: {manifest['live_buffer_bars']}")


if __name__ == "__main__":
    _run_self_test()
