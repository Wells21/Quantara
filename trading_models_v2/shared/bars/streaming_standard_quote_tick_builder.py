"""
Single-pass causal streaming standard quote-tick bar builder.

Blueprint purpose
-----------------
This is a faster asset-level bar forge engine.

It keeps the same Blueprint contract:

    assets/{ASSET}/raw/ticks/
        -> assets/{ASSET}/bars/standard_quote_tick/

and the same bar semantics:

    Research_Bar_Mode = standard_quote_tick
    Threshold_Unit = ticks
    Proxy_Value = Ticks  # backward compatibility only

Why this exists
---------------
The two-pass engine profiles all daily ticks first, then reads all ticks again to
forge bars. That is safe but slow on large tick folders.

The causal threshold for day D only needs completed daily tick counts before D.
Therefore, if files are globally chronological, we can do a single streaming pass:

1. Count completed days as we pass them.
2. Compute threshold for the new day from prior completed days only.
3. Forge bars immediately for that day.
4. Never use current-day or future-day tick counts for the current-day threshold.

This is MLDP/AFML-safe because it preserves causal threshold construction.

Important assumption
--------------------
Input CSV files must be globally chronological after filename sorting and within
chunk sorting. The builder fails closed if timestamps move backward.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.bars.bar_contract_validator import require_valid_bar_dataframe  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)
from shared.bars.standard_quote_tick_builder import (  # noqa: E402
    DEFAULT_CHUNKSIZE,
    CausalThresholdConfig,
    StandardQuoteTickBarState,
    get_sorted_csv_files,
    normalize_quote_ticks,
)
from shared.contracts import BAR_MODE_STANDARD_QUOTE_TICK, THRESHOLD_UNIT_TICKS  # noqa: E402

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None


class StreamingBarForgeError(RuntimeError):
    """Raised when single-pass streaming forge must fail closed."""


@dataclass(frozen=True)
class StreamingForgeResult:
    bars: pd.DataFrame
    daily_profile: pd.DataFrame
    thresholds_by_date: dict[pd.Timestamp, int]
    threshold_meta: dict[str, Any]
    profile_stats: dict[str, Any]
    forge_stats: dict[str, Any]


def _progress(iterable: Any, **kwargs: Any) -> Any:
    if tqdm is None:
        return iterable
    return tqdm(iterable, **kwargs)


def _threshold_from_prior_counts(prior_counts: list[float], config: CausalThresholdConfig) -> int | None:
    if len(prior_counts) < config.min_warmup_days:
        return None
    ewm_daily_ticks = float(
        pd.Series(prior_counts)
        .ewm(span=config.daily_tick_ewma_span_days, adjust=False)
        .mean()
        .iloc[-1]
    )
    return max(1, int(round(ewm_daily_ticks / config.target_bars_per_day)))


def _threshold_meta(thresholds_by_date: Mapping[pd.Timestamp, int], config: CausalThresholdConfig) -> dict[str, Any]:
    if not thresholds_by_date:
        raise StreamingBarForgeError(
            f"No thresholds produced. Need at least {config.min_warmup_days + 1} trading days."
        )
    active = pd.Series(list(thresholds_by_date.values()), dtype="float64")
    return {
        "bar_type": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_mode": "single_pass_causal_daily_ewma_tick_count",
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "target_bars_per_day": config.target_bars_per_day,
        "min_warmup_days": config.min_warmup_days,
        "warmup_days_are_emitted": False,
        "daily_tick_ewma_span_days": config.daily_tick_ewma_span_days,
        "first_threshold_date": str(min(thresholds_by_date).date()),
        "last_threshold_date": str(max(thresholds_by_date).date()),
        "median_ticks_per_bar_threshold": float(active.median()),
        "mean_ticks_per_bar_threshold": float(active.mean()),
        "min_ticks_per_bar_threshold": float(active.min()),
        "max_ticks_per_bar_threshold": float(active.max()),
        "uses_future_data": False,
        "engine": "single_pass_causal_streaming",
    }


def forge_standard_quote_tick_bars_single_pass_from_csv_folder(
    tick_folder: str | Path,
    *,
    config: CausalThresholdConfig,
    output_path: str | Path | None = None,
    chunksize: int = DEFAULT_CHUNKSIZE,
    source: str | None = None,
    enforce_monotonic_timestamps: bool = True,
) -> StreamingForgeResult:
    """Forge standard_quote_tick bars in one causal chronological pass.

    The threshold for a new day is computed at the first tick of that day from
    prior completed daily counts only. Current-day ticks are not used for the
    current-day threshold.
    """

    config.validate()
    files = get_sorted_csv_files(tick_folder)
    state = StandardQuoteTickBarState()
    bars: list[dict[str, Any]] = []

    daily_counts: dict[pd.Timestamp, int] = {}
    thresholds_by_date: dict[pd.Timestamp, int] = {}
    prior_counts: list[float] = []

    current_date: Any | None = None
    current_day_count = 0
    current_day_threshold: int | None = None
    last_timestamp: Any | None = None

    raw_rows_seen = 0
    valid_ticks_seen = 0
    skipped_ticks_without_threshold = 0
    active_ticks_used = 0
    started = time.time()
    source_name = source or Path(tick_folder).name

    def close_completed_day() -> None:
        nonlocal current_date, current_day_count
        if current_date is None:
            return
        day_ts = pd.Timestamp(current_date).tz_localize("UTC") if pd.Timestamp(current_date).tzinfo is None else pd.Timestamp(current_date)
        day_ts = day_ts.floor("D")
        daily_counts[day_ts] = int(current_day_count)
        prior_counts.append(float(current_day_count))

    for file_path in _progress(files, desc="Streaming forge asset bars", unit="file"):
        for chunk in pd.read_csv(file_path, chunksize=chunksize):
            raw_rows_seen += len(chunk)
            normalized = normalize_quote_ticks(chunk, source=file_path.name)
            valid_ticks_seen += len(normalized)

            timestamps = normalized["timestamp"].to_numpy()
            prices = normalized["mid_price"].to_numpy(dtype="float64")
            dates = normalized["timestamp"].dt.date.to_numpy()

            for ts, px, tick_date in zip(timestamps, prices, dates):
                if enforce_monotonic_timestamps and last_timestamp is not None and ts < last_timestamp:
                    raise StreamingBarForgeError(
                        "Input ticks are not globally chronological after file sorting. "
                        "Use the two-pass engine or fix tick file ordering. "
                        f"Previous timestamp={last_timestamp}, current timestamp={ts}, file={file_path.name}"
                    )
                last_timestamp = ts

                if current_date is None:
                    current_date = tick_date
                    current_day_count = 0
                    current_day_threshold = _threshold_from_prior_counts(prior_counts, config)
                    if current_day_threshold is not None:
                        thresholds_by_date[pd.Timestamp(current_date).tz_localize("UTC").floor("D")] = int(current_day_threshold)

                elif tick_date != current_date:
                    close_completed_day()
                    current_date = tick_date
                    current_day_count = 0
                    current_day_threshold = _threshold_from_prior_counts(prior_counts, config)
                    if current_day_threshold is not None:
                        thresholds_by_date[pd.Timestamp(current_date).tz_localize("UTC").floor("D")] = int(current_day_threshold)

                current_day_count += 1

                if current_day_threshold is None:
                    skipped_ticks_without_threshold += 1
                    continue

                active_ticks_used += 1

                if state.empty():
                    # Freeze the causal daily threshold at bar start.
                    state.freeze_threshold(current_day_threshold)

                state.update(ts, px)

                if state.ready_to_close():
                    bars.append(state.finalize(close_timestamp=ts, source=source_name))
                    state.reset()

    close_completed_day()

    if not bars:
        raise StreamingBarForgeError(
            "No bars were forged. Check warmup days, thresholds, and input tick data. "
            f"Skipped ticks without threshold={skipped_ticks_without_threshold}."
        )

    bars_df = pd.DataFrame(bars)
    bars_df["Datetime"] = to_datetime_iso(bars_df["Datetime"], utc=True)
    bars_df["Bar_Start"] = to_datetime_iso(bars_df["Bar_Start"], utc=True)
    bars_df = bars_df.sort_values("Datetime").reset_index(drop=True)
    require_valid_bar_dataframe(bars_df)

    daily_profile = pd.DataFrame(
        [
            {"date": date, "tick_count": count}
            for date, count in sorted(daily_counts.items())
        ]
    ).reset_index(drop=True)

    if daily_profile.empty:
        raise StreamingBarForgeError("No daily tick profile was produced.")

    threshold_metadata = _threshold_meta(thresholds_by_date, config=config)

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".csv":
            bars_df.to_csv(out, index=False)
        elif out.suffix.lower() in {".parquet", ".pq"}:
            bars_df.to_parquet(out, index=False)
        else:
            raise ValueError(f"Unsupported output format: {out.suffix}")

    elapsed = round(time.time() - started, 2)
    profile_stats = {
        "tick_folder": str(tick_folder),
        "raw_rows_seen": int(raw_rows_seen),
        "valid_ticks_used": int(valid_ticks_seen),
        "trading_days": int(len(daily_profile)),
        "date_start": str(to_datetime_iso(daily_profile["date"]).min().date()),
        "date_end": str(to_datetime_iso(daily_profile["date"]).max().date()),
        "median_daily_ticks": float(daily_profile["tick_count"].median()),
        "mean_daily_ticks": float(daily_profile["tick_count"].mean()),
        "elapsed_seconds": elapsed,
        "engine": "single_pass_causal_streaming",
    }

    forge_stats = {
        "tick_folder": str(tick_folder),
        "output_path": str(output_path) if output_path is not None else None,
        "bar_mode": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "proxy_value_definition": "Proxy_Value equals Ticks for backward compatibility only.",
        "raw_rows_seen": int(raw_rows_seen),
        "valid_ticks_seen": int(valid_ticks_seen),
        "skipped_ticks_without_threshold": int(skipped_ticks_without_threshold),
        "active_ticks_used": int(active_ticks_used),
        "bars_forged": int(len(bars_df)),
        "start": str(bars_df["Datetime"].min()),
        "end": str(bars_df["Datetime"].max()),
        "median_ticks_per_bar": float(bars_df["Ticks"].median()),
        "mean_ticks_per_bar": float(bars_df["Ticks"].mean()),
        "elapsed_seconds": elapsed,
        "engine": "single_pass_causal_streaming",
    }

    return StreamingForgeResult(
        bars=bars_df,
        daily_profile=daily_profile,
        thresholds_by_date=thresholds_by_date,
        threshold_meta=threshold_metadata,
        profile_stats=profile_stats,
        forge_stats=forge_stats,
    )


def _run_self_test() -> None:
    import tempfile

    rows: list[dict[str, Any]] = []
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    # 25 days, 10 ticks per day. Warmup 20 days, threshold after warmup = 2 ticks/bar.
    for day in range(25):
        for tick in range(10):
            ts = start + pd.Timedelta(days=day, seconds=tick)
            mid = 100.0 + day + tick * 0.01
            rows.append({"timestamp": ts.isoformat(), "bidprice": mid - 0.01, "askprice": mid + 0.01})

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "ticks"
        folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(folder / "ticks.csv", index=False)

        result = forge_standard_quote_tick_bars_single_pass_from_csv_folder(
            folder,
            config=CausalThresholdConfig(target_bars_per_day=5, min_warmup_days=20, daily_tick_ewma_span_days=20),
            source="self_test_streaming_ticks",
        )

    if len(result.thresholds_by_date) != 5:
        raise SystemExit(f"❌ Expected 5 post-warmup thresholds; got {len(result.thresholds_by_date)}")
    if result.threshold_meta["uses_future_data"] is not False:
        raise SystemExit("❌ Streaming threshold metadata must declare uses_future_data=False")
    if not (result.bars["Proxy_Value"] == result.bars["Ticks"]).all():
        raise SystemExit("❌ Proxy_Value must equal Ticks in streaming standard_quote_tick bars")

    print("✅ shared.bars.streaming_standard_quote_tick_builder self-test passed.")
    print(f"Daily rows: {len(result.daily_profile)}")
    print(f"Threshold days: {len(result.thresholds_by_date)}")
    print(f"Bars forged: {len(result.bars)}")


if __name__ == "__main__":
    _run_self_test()
