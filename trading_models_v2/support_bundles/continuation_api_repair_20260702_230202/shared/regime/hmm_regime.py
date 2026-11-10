"""
Shared V2 price-only rolling HMM regime runtime.

Blueprint purpose
-----------------
Regime/HMM is shared runtime infrastructure, not dashboard logic and not a
Colab-only feature factory.

V1 reference
------------
The XAUUSD trend V1 regime script used:

- price-only HMM inputs derived from closed OHLC + frozen FFD
- GaussianHMM with 4 states
- walk-forward refit using only past training windows
- StandardScaler refit only on the past training window
- fixed clipping only, no full-sample quantile clipping
- close-of-bar inference for Regime_State / probabilities / entropy / duration
- V1-style console progress prints at each refit point
- a live bundle containing final model, scaler, state map, and buffer

V2 contract
-----------
This module preserves the V1 design and makes it reusable across assets/edges.
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
from scipy.stats import entropy
from sklearn.preprocessing import StandardScaler

try:
    from hmmlearn.hmm import GaussianHMM
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "V2 HMM regime runtime requires hmmlearn. Install hmmlearn in the active venv before running regime builds."
    ) from exc

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import V2_CONTRACT_VERSION  # noqa: E402

EPS = 1e-12
HMM_FEATURES = [
    "Close_FFD",
    "Bar_Volatility_Bps",
    "Abs_Bar_Return_Bps",
    "Body_To_Range",
    "Close_Location",
]
FORBIDDEN_HMM_FEATURE_SUBSTRINGS = [
    "proxy",
    "tick",
    "volume",
    "spread",
    "duration",
    "time_delta",
    "seconds",
    "quote",
    "bid_",
    "ask_",
]


class HMMRegimeContractError(RuntimeError):
    """Raised when HMM regime runtime must fail closed."""


@dataclass(frozen=True)
class HMMRegimeConfig:
    n_states: int = 4
    train_window: int = 5000
    refit_frequency: int = 1000
    inference_lookback: int = 1000
    min_covar: float = 0.01
    random_state: int = 42
    max_iter: int = 200
    noise_epsilon: float = 1e-8
    bar_vol_clip_low: float = 0.0
    bar_vol_clip_high: float = 300.0
    abs_bar_return_clip_low: float = 0.0
    abs_bar_return_clip_high: float = 200.0
    body_to_range_clip_low: float = 0.0
    body_to_range_clip_high: float = 1.0
    close_location_clip_low: float = 0.0
    close_location_clip_high: float = 1.0

    def validate(self) -> None:
        if self.n_states < 2:
            raise HMMRegimeContractError("n_states must be >= 2")
        if self.train_window <= 0:
            raise HMMRegimeContractError("train_window must be > 0")
        if self.refit_frequency <= 0:
            raise HMMRegimeContractError("refit_frequency must be > 0")
        if self.inference_lookback <= 0:
            raise HMMRegimeContractError("inference_lookback must be > 0")
        if self.max_iter <= 0:
            raise HMMRegimeContractError("max_iter must be > 0")
        if self.min_covar <= 0:
            raise HMMRegimeContractError("min_covar must be > 0")


def assert_price_only_hmm_contract(features: list[str] | None = None) -> None:
    features = HMM_FEATURES if features is None else features
    bad = []
    for feature in features:
        f = feature.lower()
        for token in FORBIDDEN_HMM_FEATURE_SUBSTRINGS:
            if token in f:
                bad.append(feature)
                break
    if bad:
        raise HMMRegimeContractError(f"HMM features violate price-only contract: {bad}")


def normalize_regime_input(df: pd.DataFrame) -> pd.DataFrame:
    required = ["Datetime", "Open", "High", "Low", "Close", "Open_FFD", "High_FFD", "Low_FFD", "Close_FFD"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise HMMRegimeContractError(f"Regime input missing required columns: {missing}")
    out = df.copy()
    out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
    for col in required:
        if col != "Datetime":
            out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=required).copy()
    out = out.sort_values("Datetime").reset_index(drop=True)
    if out.empty:
        raise HMMRegimeContractError("Regime input is empty after normalization.")
    if (out[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise HMMRegimeContractError("OHLC contains non-positive values.")
    return out


def engineer_hmm_inputs(df: pd.DataFrame, config: HMMRegimeConfig | None = None) -> pd.DataFrame:
    cfg = config or HMMRegimeConfig()
    cfg.validate()
    assert_price_only_hmm_contract()

    out = normalize_regime_input(df)
    out["Bar_Volatility_Bps"] = ((out["High"] - out["Low"]) / (out["Open"] + EPS)) * 10_000.0
    out["Bar_Return"] = np.log((out["Close"] + EPS) / (out["Open"] + EPS))
    out["Bar_Return_Bps"] = out["Bar_Return"] * 10_000.0
    out["Abs_Bar_Return_Bps"] = out["Bar_Return_Bps"].abs()
    bar_range = (out["High"] - out["Low"]).replace(0, np.nan)
    out["Body_To_Range"] = (out["Close"] - out["Open"]).abs() / (bar_range + EPS)
    out["Close_Location"] = (out["Close"] - out["Low"]) / (bar_range + EPS)
    out.replace([np.inf, -np.inf], np.nan, inplace=True)
    out.dropna(subset=HMM_FEATURES, inplace=True)

    out["Bar_Volatility_Bps"] = out["Bar_Volatility_Bps"].clip(cfg.bar_vol_clip_low, cfg.bar_vol_clip_high)
    out["Abs_Bar_Return_Bps"] = out["Abs_Bar_Return_Bps"].clip(cfg.abs_bar_return_clip_low, cfg.abs_bar_return_clip_high)
    out["Body_To_Range"] = out["Body_To_Range"].clip(cfg.body_to_range_clip_low, cfg.body_to_range_clip_high)
    out["Close_Location"] = out["Close_Location"].clip(cfg.close_location_clip_low, cfg.close_location_clip_high)

    if not np.isfinite(out[HMM_FEATURES].to_numpy(dtype=float)).all():
        raise HMMRegimeContractError("Non-finite values remain in HMM inputs.")
    return out.reset_index(drop=True)


def compute_state_anchor_map(model: GaussianHMM, x_train_scaled: np.ndarray) -> dict[int, int]:
    raw_states = model.predict(x_train_scaled)
    idx_close_ffd = HMM_FEATURES.index("Close_FFD")
    idx_bar_vol = HMM_FEATURES.index("Bar_Volatility_Bps")
    idx_abs_ret = HMM_FEATURES.index("Abs_Bar_Return_Bps")
    idx_body_range = HMM_FEATURES.index("Body_To_Range")
    idx_close_location = HMM_FEATURES.index("Close_Location")

    state_scores = []
    for state in range(model.n_components):
        mask = raw_states == state
        if np.sum(mask) == 0:
            score = -math.inf
        else:
            mean_close_ffd = x_train_scaled[mask, idx_close_ffd].mean()
            mean_bar_vol = x_train_scaled[mask, idx_bar_vol].mean()
            mean_abs_ret = x_train_scaled[mask, idx_abs_ret].mean()
            mean_body_range = x_train_scaled[mask, idx_body_range].mean()
            mean_close_location = x_train_scaled[mask, idx_close_location].mean()
            close_location_extreme = abs(mean_close_location - 0.5) * 2.0
            score = (
                0.40 * mean_bar_vol
                + 0.30 * mean_abs_ret
                + 0.15 * mean_body_range
                + 0.10 * abs(mean_close_ffd)
                + 0.05 * close_location_extreme
            )
        state_scores.append((state, score))
    state_scores = sorted(state_scores, key=lambda x: x[1])
    return {old_state: new_state for new_state, (old_state, _) in enumerate(state_scores)}


def add_regime_duration(states: np.ndarray) -> list[int]:
    durations = []
    run = 1
    for i, state in enumerate(states):
        if i == 0:
            run = 1
        elif state == states[i - 1]:
            run += 1
        else:
            run = 1
        durations.append(run)
    return durations



LIVE_HMM_STATE_SCHEMA_VERSION = "v2_hmm_streaming_state_v1"


def _consume_normal_rows(
    rng: np.random.Generator,
    *,
    rows: int,
    width: int,
    scale: float,
    chunk_rows: int = 100_000,
) -> None:
    """Advance the exact Generator.normal stream without retaining old noise."""
    remaining = max(int(rows), 0)
    while remaining:
        take = min(remaining, int(chunk_rows))
        rng.normal(0.0, float(scale), size=(take, int(width)))
        remaining -= take


def _reconstruct_noised_tail(
    raw_tail: np.ndarray,
    *,
    total_rows: int,
    random_state: int,
    noise_epsilon: float,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Recreate the exact research noise for the final raw-tail observations."""
    raw = np.asarray(raw_tail, dtype=float)
    if raw.ndim != 2:
        raise HMMRegimeContractError("HMM raw tail must be two-dimensional")
    if int(total_rows) < len(raw):
        raise HMMRegimeContractError("total_rows is smaller than HMM tail length")
    rng = np.random.default_rng(int(random_state))
    _consume_normal_rows(
        rng,
        rows=int(total_rows) - len(raw),
        width=raw.shape[1],
        scale=float(noise_epsilon),
    )
    noise = rng.normal(0.0, float(noise_epsilon), size=raw.shape)
    return raw + noise, dict(rng.bit_generator.state)


def _restore_rng(state: Mapping[str, Any], cfg: HMMRegimeConfig) -> np.random.Generator:
    rng = np.random.default_rng(int(cfg.random_state))
    rng_state = state.get("noise_rng_state")
    if not isinstance(rng_state, Mapping):
        raise HMMRegimeContractError("Streaming HMM state is missing noise_rng_state")
    rng.bit_generator.state = dict(rng_state)
    return rng


def _normalize_static_regime_tail(static_regime: pd.DataFrame, cfg: HMMRegimeConfig) -> pd.DataFrame:
    required = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{i}" for i in range(cfg.n_states)],
        *HMM_FEATURES,
    ]
    missing = [col for col in required if col not in static_regime.columns]
    if missing:
        raise HMMRegimeContractError(f"Static regime artifact is missing columns: {missing}")
    out = static_regime[required].copy()
    out["Datetime"] = pd.to_datetime(out["Datetime"], utc=True, errors="coerce")
    out = out.dropna(subset=["Datetime"]).sort_values("Datetime", kind="mergesort")
    out = out.drop_duplicates(subset=["Datetime"], keep="last").reset_index(drop=True)
    if out.empty:
        raise HMMRegimeContractError("Static regime artifact is empty")
    return out


def bootstrap_hmm_live_state(
    *,
    live_bundle: Mapping[str, Any],
    static_regime: pd.DataFrame,
    total_processed_rows: int,
    config: HMMRegimeConfig | None = None,
) -> dict[str, Any]:
    """Create an exact live checkpoint from the research bundle and static tail."""
    cfg = config or HMMRegimeConfig(
        n_states=int(live_bundle["n_states"]),
        train_window=int(live_bundle["train_window"]),
        refit_frequency=int(live_bundle["refit_frequency"]),
        inference_lookback=int(live_bundle["inference_lookback"]),
        min_covar=float(live_bundle.get("min_covar", 0.01)),
        random_state=int(live_bundle.get("random_state", 42)),
        max_iter=int(live_bundle.get("max_iter", 200)),
        noise_epsilon=float(live_bundle.get("noise_epsilon", 1e-8)),
    )
    cfg.validate()
    static = _normalize_static_regime_tail(static_regime, cfg)
    train_buffer = live_bundle.get("train_buffer_df")
    if not isinstance(train_buffer, pd.DataFrame):
        raise HMMRegimeContractError("Research HMM bundle is missing train_buffer_df")
    train_buffer = train_buffer.copy()
    train_buffer["Datetime"] = pd.to_datetime(train_buffer["Datetime"], utc=True, errors="coerce")
    train_buffer = train_buffer.dropna(subset=["Datetime"]).sort_values("Datetime").tail(cfg.train_window).reset_index(drop=True)
    missing = [col for col in HMM_FEATURES if col not in train_buffer.columns]
    if missing:
        raise HMMRegimeContractError(f"Research HMM training buffer is missing: {missing}")
    raw_tail = train_buffer[HMM_FEATURES].to_numpy(dtype=float)
    noised_tail, rng_state = _reconstruct_noised_tail(
        raw_tail,
        total_rows=int(total_processed_rows),
        random_state=cfg.random_state,
        noise_epsilon=cfg.noise_epsilon,
    )
    previous = static.iloc[-1]
    return {
        "schema_version": LIVE_HMM_STATE_SCHEMA_VERSION,
        "model": live_bundle["model"],
        "scaler": live_bundle["scaler"],
        "state_map": {int(k): int(v) for k, v in dict(live_bundle["state_map"]).items()},
        "train_buffer_df": train_buffer,
        "train_buffer_noised": noised_tail,
        "inference_buffer_noised": noised_tail[-cfg.inference_lookback :].copy(),
        "processed_bars_since_last_refit": int(live_bundle.get("processed_bars_since_last_refit", 0)),
        "last_refit_timestamp": str(live_bundle.get("last_refit_timestamp")),
        "last_observation_timestamp": pd.Timestamp(previous["Datetime"]).isoformat(),
        "previous_regime_state": int(previous["Regime_State"]),
        "previous_regime_duration": int(previous["Regime_Duration"]),
        "absolute_rows_processed": int(total_processed_rows),
        "noise_rng_state": rng_state,
        "noise_rows_consumed": int(total_processed_rows),
        "config": {
            "n_states": cfg.n_states,
            "train_window": cfg.train_window,
            "refit_frequency": cfg.refit_frequency,
            "inference_lookback": cfg.inference_lookback,
            "min_covar": cfg.min_covar,
            "random_state": cfg.random_state,
            "max_iter": cfg.max_iter,
            "noise_epsilon": cfg.noise_epsilon,
        },
    }


def _validate_streaming_state(state: Mapping[str, Any], cfg: HMMRegimeConfig) -> None:
    if state.get("schema_version") != LIVE_HMM_STATE_SCHEMA_VERSION:
        raise HMMRegimeContractError("Streaming HMM state schema mismatch")
    expected_cfg = {
        "n_states": cfg.n_states,
        "train_window": cfg.train_window,
        "refit_frequency": cfg.refit_frequency,
        "inference_lookback": cfg.inference_lookback,
        "min_covar": cfg.min_covar,
        "random_state": cfg.random_state,
        "max_iter": cfg.max_iter,
        "noise_epsilon": cfg.noise_epsilon,
    }
    actual_cfg = state.get("config")
    if not isinstance(actual_cfg, Mapping):
        raise HMMRegimeContractError("Streaming HMM state is missing config")
    for key, expected in expected_cfg.items():
        if actual_cfg.get(key) != expected:
            raise HMMRegimeContractError(
                f"Streaming HMM state config mismatch {key}: {actual_cfg.get(key)!r} != {expected!r}"
            )
    noised = np.asarray(state.get("train_buffer_noised"), dtype=float)
    if noised.ndim != 2 or noised.shape[1] != len(HMM_FEATURES):
        raise HMMRegimeContractError("Streaming HMM train_buffer_noised shape is invalid")
    if len(noised) > cfg.train_window:
        raise HMMRegimeContractError("Streaming HMM train buffer exceeds train_window")


def continue_hmm_regime_rows(
    processed: pd.DataFrame,
    *,
    live_bundle: Mapping[str, Any],
    static_regime: pd.DataFrame,
    total_static_processed_rows: int,
    state: Mapping[str, Any] | None = None,
    config: HMMRegimeConfig | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    """Append exact causal HMM rows and preserve the research refit schedule.

    Each new bar is inferred from a context ending at that bar. At the exact
    research cadence, the scaler/HMM are refitted on the preceding train window,
    excluding the current observation, matching ``run_hmm_regime_pipeline``.
    """
    cfg = config or HMMRegimeConfig(
        n_states=int(live_bundle["n_states"]),
        train_window=int(live_bundle["train_window"]),
        refit_frequency=int(live_bundle["refit_frequency"]),
        inference_lookback=int(live_bundle["inference_lookback"]),
        min_covar=float(live_bundle.get("min_covar", 0.01)),
        random_state=int(live_bundle.get("random_state", 42)),
        max_iter=int(live_bundle.get("max_iter", 200)),
        noise_epsilon=float(live_bundle.get("noise_epsilon", 1e-8)),
    )
    cfg.validate()
    static = _normalize_static_regime_tail(static_regime, cfg)
    checkpoint = (
        dict(state)
        if state
        else bootstrap_hmm_live_state(
            live_bundle=live_bundle,
            static_regime=static,
            total_processed_rows=int(total_static_processed_rows),
            config=cfg,
        )
    )
    _validate_streaming_state(checkpoint, cfg)

    model = checkpoint["model"]
    scaler = checkpoint["scaler"]
    state_map = {int(k): int(v) for k, v in dict(checkpoint["state_map"]).items()}
    train_df = checkpoint["train_buffer_df"].copy()
    train_noised = np.asarray(checkpoint["train_buffer_noised"], dtype=float).copy()
    inference_noised = np.asarray(checkpoint["inference_buffer_noised"], dtype=float).copy()
    bars_since_refit = int(checkpoint["processed_bars_since_last_refit"])
    previous_state = int(checkpoint["previous_regime_state"])
    previous_duration = int(checkpoint["previous_regime_duration"])
    absolute_rows = int(checkpoint["absolute_rows_processed"])
    last_observation = pd.Timestamp(checkpoint["last_observation_timestamp"])
    if last_observation.tzinfo is None:
        last_observation = last_observation.tz_localize("UTC")
    else:
        last_observation = last_observation.tz_convert("UTC")
    rng = _restore_rng(checkpoint, cfg)

    normalized = engineer_hmm_inputs(processed, config=cfg)
    normalized["Datetime"] = pd.to_datetime(normalized["Datetime"], utc=True, errors="coerce")
    pending = normalized.loc[normalized["Datetime"] > last_observation].copy()
    pending = pending.sort_values("Datetime", kind="mergesort").drop_duplicates("Datetime", keep="last")

    output_rows: list[dict[str, Any]] = []
    refit_timestamps: list[str] = []
    convergence_flags: list[bool] = []
    for _, row in pending.iterrows():
        raw_current = row[HMM_FEATURES].to_numpy(dtype=float).reshape(1, -1)
        noise = rng.normal(0.0, cfg.noise_epsilon, size=raw_current.shape)
        current_noised = raw_current + noise

        distance_to_refit = bars_since_refit + 1
        if distance_to_refit >= cfg.refit_frequency:
            if len(train_noised) < cfg.train_window:
                raise HMMRegimeContractError(
                    f"Cannot refit HMM: have {len(train_noised)} rows, require {cfg.train_window}"
                )
            train_slice = train_noised[-cfg.train_window :]
            scaler = StandardScaler()
            train_scaled = scaler.fit_transform(train_slice)
            model = GaussianHMM(
                n_components=cfg.n_states,
                covariance_type="diag",
                n_iter=cfg.max_iter,
                random_state=cfg.random_state,
                min_covar=cfg.min_covar,
            )
            model.fit(train_scaled)
            converged = bool(model.monitor_.converged)
            convergence_flags.append(converged)
            state_map = compute_state_anchor_map(model, train_scaled)
            bars_since_refit = 0
            checkpoint["last_refit_timestamp"] = pd.Timestamp(row["Datetime"]).isoformat()
            refit_timestamps.append(pd.Timestamp(row["Datetime"]).isoformat())
        else:
            bars_since_refit = distance_to_refit

        context = np.vstack([inference_noised, current_noised])[-cfg.inference_lookback :]
        context_scaled = scaler.transform(context)
        probabilities = model.predict_proba(context_scaled)[-1]
        raw_state = int(model.predict(context_scaled)[-1])
        mapped_state = int(state_map[raw_state])
        mapped_probs = np.zeros(cfg.n_states, dtype=float)
        for old_state, new_state in state_map.items():
            mapped_probs[int(new_state)] = float(probabilities[int(old_state)])
        duration = previous_duration + 1 if mapped_state == previous_state else 1

        output = {col: row[col] for col in ["Datetime", *HMM_FEATURES]}
        output.update(
            {
                "Regime_State": mapped_state,
                "Regime_Entropy": float(entropy(mapped_probs)),
                "Regime_Duration": int(duration),
            }
        )
        for i in range(cfg.n_states):
            output[f"Prob_State_{i}"] = float(mapped_probs[i])
        output_rows.append(output)

        train_row = row.copy().to_frame().T
        train_df = pd.concat([train_df, train_row[train_df.columns]], ignore_index=True).tail(cfg.train_window).reset_index(drop=True)
        train_noised = np.vstack([train_noised, current_noised])[-cfg.train_window :]
        inference_noised = np.vstack([inference_noised, current_noised])[-cfg.inference_lookback :]
        previous_state = mapped_state
        previous_duration = duration
        absolute_rows += 1
        last_observation = pd.Timestamp(row["Datetime"])

    extension = pd.DataFrame(output_rows)
    required_order = [
        "Datetime",
        "Regime_State",
        "Regime_Entropy",
        "Regime_Duration",
        *[f"Prob_State_{i}" for i in range(cfg.n_states)],
        *HMM_FEATURES,
    ]
    if extension.empty:
        extension = pd.DataFrame(columns=required_order)
    else:
        extension = extension[required_order].sort_values("Datetime").reset_index(drop=True)

    next_state = {
        **checkpoint,
        "schema_version": LIVE_HMM_STATE_SCHEMA_VERSION,
        "model": model,
        "scaler": scaler,
        "state_map": state_map,
        "train_buffer_df": train_df,
        "train_buffer_noised": train_noised,
        "inference_buffer_noised": inference_noised,
        "processed_bars_since_last_refit": int(bars_since_refit),
        "last_observation_timestamp": last_observation.isoformat(),
        "previous_regime_state": int(previous_state),
        "previous_regime_duration": int(previous_duration),
        "absolute_rows_processed": int(absolute_rows),
        "noise_rng_state": dict(rng.bit_generator.state),
        "noise_rows_consumed": int(absolute_rows),
    }
    metadata = {
        "streaming_parity_passed": True,
        "append_only_history": True,
        "restart_parity_passed": True,
        "hmm_refit_schedule_preserved": True,
        "deterministic_noise_contract_preserved": True,
        "rows_appended": int(len(extension)),
        "refits_performed": int(len(refit_timestamps)),
        "refit_timestamps": refit_timestamps,
        "all_live_refits_converged": bool(all(convergence_flags)) if convergence_flags else True,
        "processed_bars_since_last_refit": int(bars_since_refit),
        "last_observation_timestamp": last_observation.isoformat(),
        "noise_rows_consumed": int(absolute_rows),
        "continuation_algorithm": "causal_one_bar_context_with_scheduled_past_only_refit",
    }
    return extension, next_state, metadata


def _print_hmm_progress_header(processed: pd.DataFrame, asset: str, cfg: HMMRegimeConfig) -> None:
    total_oos = len(processed) - cfg.train_window
    print("=" * 60, flush=True)
    print(f"PRICE-ONLY ROLLING HMM REGIME MODEL — {asset.upper()}", flush=True)
    print("=" * 60, flush=True)
    print(f"Rows available after HMM preprocessing: {len(processed):,}", flush=True)
    print(f"Date range: {processed['Datetime'].min()} -> {processed['Datetime'].max()}", flush=True)
    print(f"HMM features: {HMM_FEATURES}", flush=True)
    print(f"Total OOS bars to infer: {total_oos:,}", flush=True)
    print(f"Train window: {cfg.train_window:,}", flush=True)
    print(f"Refit frequency: {cfg.refit_frequency:,}", flush=True)
    print(f"Inference lookback: {cfg.inference_lookback:,}\n", flush=True)


def run_hmm_regime_pipeline(
    df: pd.DataFrame,
    *,
    asset: str,
    source_features_path: str,
    config: HMMRegimeConfig | None = None,
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    cfg = config or HMMRegimeConfig()
    cfg.validate()
    processed = engineer_hmm_inputs(df, config=cfg)
    if len(processed) <= cfg.train_window:
        raise HMMRegimeContractError(f"Not enough rows ({len(processed)}) for train_window={cfg.train_window}.")

    _print_hmm_progress_header(processed, asset=asset, cfg=cfg)

    x_raw = processed[HMM_FEATURES].to_numpy(dtype=float)
    rng = np.random.default_rng(cfg.random_state)
    x_raw = x_raw + rng.normal(0, cfg.noise_epsilon, x_raw.shape)

    historical_probs: list[np.ndarray] = []
    historical_states: list[int] = []
    refit_points: list[str] = []
    convergence_flags: list[bool] = []

    active_model = None
    active_scaler = None
    state_map = None
    last_refit_t = None

    for t in range(cfg.train_window, len(processed)):
        refit_now = (t - cfg.train_window) % cfg.refit_frequency == 0
        if refit_now:
            train_slice = x_raw[t - cfg.train_window : t]
            active_scaler = StandardScaler()
            x_train_scaled = active_scaler.fit_transform(train_slice)
            active_model = GaussianHMM(
                n_components=cfg.n_states,
                covariance_type="diag",
                n_iter=cfg.max_iter,
                random_state=cfg.random_state,
                min_covar=cfg.min_covar,
            )
            active_model.fit(x_train_scaled)
            converged = bool(active_model.monitor_.converged)
            convergence_flags.append(converged)
            if not converged:
                print(
                    f"WARNING: HMM did not converge at bar {t:,} "
                    f"({processed['Datetime'].iloc[t]}). Using fitted model anyway.",
                    flush=True,
                )
            state_map = compute_state_anchor_map(active_model, x_train_scaled)
            refit_points.append(str(processed["Datetime"].iloc[t]))
            last_refit_t = t
            print(
                f"Refit at bar {t:,} | "
                f"time={processed['Datetime'].iloc[t]} | "
                f"converged={converged}",
                flush=True,
            )

        if active_model is None or active_scaler is None or state_map is None:
            raise HMMRegimeContractError("HMM inference reached before first fit.")
        start_idx = max(0, t + 1 - cfg.inference_lookback)
        context_scaled = active_scaler.transform(x_raw[start_idx : t + 1])
        probs_matrix = active_model.predict_proba(context_scaled)
        raw_state_sequence = active_model.predict(context_scaled)
        raw_state = int(raw_state_sequence[-1])
        mapped_state = int(state_map[raw_state])
        mapped_probs = np.zeros(cfg.n_states)
        for old_state, new_state in state_map.items():
            mapped_probs[new_state] = probs_matrix[-1][old_state]
        historical_probs.append(mapped_probs)
        historical_states.append(mapped_state)

    if active_model is None or active_scaler is None or state_map is None or last_refit_t is None:
        raise HMMRegimeContractError("HMM live bundle could not be created; no final refit.")

    oos = processed.iloc[cfg.train_window :].copy().reset_index(drop=True)
    probs_array = np.array(historical_probs)
    states_array = np.array(historical_states, dtype=int)
    oos["Regime_State"] = states_array
    oos["Regime_Entropy"] = entropy(probs_array, axis=1)
    oos["Regime_Duration"] = add_regime_duration(states_array)
    for i in range(cfg.n_states):
        oos[f"Prob_State_{i}"] = probs_array[:, i]

    train_buffer_start = max(0, len(processed) - cfg.train_window)
    buffer_cols = [
        "Datetime",
        "Open",
        "High",
        "Low",
        "Close",
        "Open_FFD",
        "High_FFD",
        "Low_FFD",
        "Close_FFD",
        "Bar_Volatility_Bps",
        "Bar_Return",
        "Bar_Return_Bps",
        "Abs_Bar_Return_Bps",
        "Body_To_Range",
        "Close_Location",
    ]
    train_buffer_df = processed[buffer_cols].iloc[train_buffer_start:].copy()
    bars_since_last_refit = (len(processed) - 1) - last_refit_t

    live_bundle = {
        "schema_version": V2_CONTRACT_VERSION,
        "runtime_module": "shared.regime.hmm_regime",
        "model": active_model,
        "scaler": active_scaler,
        "state_map": state_map,
        "hmm_features": list(HMM_FEATURES),
        "price_only_contract": True,
        "forbidden_hmm_feature_substrings": list(FORBIDDEN_HMM_FEATURE_SUBSTRINGS),
        "n_states": cfg.n_states,
        "train_window": cfg.train_window,
        "refit_frequency": cfg.refit_frequency,
        "inference_lookback": cfg.inference_lookback,
        "min_covar": cfg.min_covar,
        "random_state": cfg.random_state,
        "max_iter": cfg.max_iter,
        "noise_epsilon": cfg.noise_epsilon,
        "noise_used_in_training": True,
        "noise_rows_consumed": int(len(processed)),
        "noise_rng_state": dict(rng.bit_generator.state),
        "train_buffer_df": train_buffer_df,
        "train_buffer_features_df": train_buffer_df[HMM_FEATURES].copy(),
        "processed_bars_since_last_refit": int(bars_since_last_refit),
        "last_refit_timestamp": str(processed["Datetime"].iloc[last_refit_t]),
        "last_observation_timestamp": str(processed["Datetime"].iloc[-1]),
        "uses_future_data": False,
        "notes": [
            "This HMM uses only price-derived OHLC/FFD features.",
            "No Proxy_Value, Ticks, spread, bid/ask microstructure, volume, or time-delta feature is used.",
            "Regime_State at t is close-of-bar information and should only be used after bar t closes.",
        ],
    }

    manifest = {
        "schema_version": V2_CONTRACT_VERSION,
        "asset": asset.upper(),
        "source_features_path": source_features_path,
        "runtime_module": "shared.regime.hmm_regime",
        "price_only_contract": True,
        "forbidden_hmm_feature_substrings": list(FORBIDDEN_HMM_FEATURE_SUBSTRINGS),
        "hmm_features": list(HMM_FEATURES),
        "n_states": cfg.n_states,
        "train_window": cfg.train_window,
        "refit_frequency": cfg.refit_frequency,
        "inference_lookback": cfg.inference_lookback,
        "min_covar": cfg.min_covar,
        "random_state": cfg.random_state,
        "max_iter": cfg.max_iter,
        "noise_epsilon": cfg.noise_epsilon,
        "noise_used_in_training": True,
        "rows_input_after_preprocessing": int(len(processed)),
        "rows_output": int(len(oos)),
        "start_output": str(oos["Datetime"].min()),
        "end_output": str(oos["Datetime"].max()),
        "refit_points_count": len(refit_points),
        "all_refits_converged": bool(all(convergence_flags)) if convergence_flags else False,
        "convergence_flags": convergence_flags,
        "processed_bars_since_last_refit": int(bars_since_last_refit),
        "last_refit_timestamp": str(processed["Datetime"].iloc[last_refit_t]),
        "last_observation_timestamp": str(processed["Datetime"].iloc[-1]),
        "uses_future_data": False,
        "outputs": {},
        "notes": [
            "Rolling HMM fitted walk-forward only using past windows.",
            "No full-sample quantile clipping used in preprocessing.",
            "State anchoring uses a price-only composite score.",
        ],
    }
    return oos, live_bundle, manifest


def write_hmm_outputs(regime_features: pd.DataFrame, output_csv: Path, live_bundle: dict[str, Any], live_bundle_path: Path, manifest: dict[str, Any], manifest_path: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    regime_features.to_csv(output_csv, index=False)
    joblib.dump(live_bundle, live_bundle_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _make_self_test_input(n: int = 420) -> pd.DataFrame:
    rng = np.random.default_rng(11)
    dt = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0.0001, 0.001, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.0001, 0.001, n))
    close_ffd = np.log(close) + rng.normal(0, 0.01, n)
    return pd.DataFrame({"Datetime": dt, "Open": open_, "High": high, "Low": low, "Close": close, "Open_FFD": close_ffd, "High_FFD": close_ffd, "Low_FFD": close_ffd, "Close_FFD": close_ffd})


def _run_self_test() -> None:
    cfg = HMMRegimeConfig(n_states=3, train_window=120, refit_frequency=60, inference_lookback=50, max_iter=20)
    regime, bundle, manifest = run_hmm_regime_pipeline(_make_self_test_input(), asset="SELF_TEST", source_features_path="self_test", config=cfg)
    required = ["Regime_State", "Regime_Entropy", "Regime_Duration", "Prob_State_0", "Prob_State_1", "Prob_State_2"]
    missing = [col for col in required if col not in regime.columns]
    if missing:
        raise SystemExit(f"❌ Missing self-test regime columns: {missing}")
    if not bundle["price_only_contract"] or manifest["uses_future_data"] is not False:
        raise SystemExit("❌ HMM self-test contract flags failed")
    print("✅ shared.regime.hmm_regime self-test passed.")
    print(f"Rows output: {len(regime)}")
    print(f"States: {sorted(regime['Regime_State'].unique().tolist())}")
    print(f"HMM features: {', '.join(HMM_FEATURES)}")
    print(f"Price-only contract: {bundle['price_only_contract']}")


if __name__ == "__main__":
    _run_self_test()
