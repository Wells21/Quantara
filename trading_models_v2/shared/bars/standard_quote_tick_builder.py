"""
V2 standard quote-tick bar builder.

Blueprint purpose
-----------------
Bars are asset-level shared infrastructure:

    assets/{ASSET}/raw/ticks/
        -> assets/{ASSET}/bars/standard_quote_tick/
        -> assets/{ASSET}/edges/{family}_{side}/runtime.py consumes those bars

This file builds only the production V2 bar type:

    Research_Bar_Mode = standard_quote_tick
    Threshold_Unit = ticks
    Proxy_Value = Ticks  # backward compatibility only

It uses only live-reproducible quote tick fields:

    timestamp, bid, ask

Historical files may name those columns as bidprice/askprice or bid_price/ask_price;
they are normalized here. No broker volume, vendor volume, or dollar/proxy volume is
used.

The builder supports two modes:

1. Fixed threshold mode
   Useful for unit tests, toy datasets, and live memory/state tests.

2. Causal daily EWMA threshold mode
   For date D, threshold_D is calculated only from completed daily tick counts
   before D. Warmup days are skipped, not emitted. This preserves the V1 research
   idea while making the implementation shared and Blueprint-compliant.

Performance note
----------------
The forging loop intentionally uses V1-style vectorized arrays plus a daily
threshold lookup. Do not put pandas Timestamp/floor operations inside the
per-tick inner loop.

Run from ``trading_models_v2``:

    python ./shared/bars/standard_quote_tick_builder.py

or from the repository root:

    python trading_models_v2/shared/bars/standard_quote_tick_builder.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

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
from shared.contracts import (  # noqa: E402
    BAR_MODE_STANDARD_QUOTE_TICK,
    THRESHOLD_UNIT_TICKS,
)

try:  # progress bars for long asset tick-folder passes
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None


# =============================================================================
# Config/data models
# =============================================================================

DEFAULT_TARGET_BARS_PER_DAY = 50
DEFAULT_MIN_WARMUP_DAYS = 20
DEFAULT_DAILY_TICK_EWMA_SPAN_DAYS = 20
DEFAULT_CHUNKSIZE = 500_000

TIMESTAMP_ALIASES = ("timestamp", "time", "datetime", "date_time")
BID_ALIASES = ("bid", "bidprice", "bid_price", "bid price")
ASK_ALIASES = ("ask", "askprice", "ask_price", "ask price")


@dataclass(frozen=True)
class CausalThresholdConfig:
    """Causal daily tick-threshold configuration."""

    target_bars_per_day: int = DEFAULT_TARGET_BARS_PER_DAY
    min_warmup_days: int = DEFAULT_MIN_WARMUP_DAYS
    daily_tick_ewma_span_days: int = DEFAULT_DAILY_TICK_EWMA_SPAN_DAYS

    def validate(self) -> None:
        if self.target_bars_per_day <= 0:
            raise ValueError("target_bars_per_day must be > 0")
        if self.min_warmup_days < 1:
            raise ValueError("min_warmup_days must be >= 1")
        if self.daily_tick_ewma_span_days <= 0:
            raise ValueError("daily_tick_ewma_span_days must be > 0")


@dataclass
class StandardQuoteTickBarState:
    """Mutable state for one currently forming standard quote-tick bar."""

    bar_start: Any = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    ticks: int = 0
    threshold_ticks: int | None = None

    def empty(self) -> bool:
        return self.ticks == 0 or self.open is None

    def freeze_threshold(self, threshold_ticks: int) -> None:
        if threshold_ticks <= 0:
            raise ValueError("threshold_ticks must be > 0")
        self.threshold_ticks = int(threshold_ticks)

    def update(self, timestamp: Any, price: float) -> None:
        """Update one bar with one normalized tick.

        Performance-sensitive: callers already pass normalized timestamps, so do
        not coerce with ``pd.Timestamp`` here for every tick.
        """

        px = float(price)
        ts = timestamp

        if self.empty():
            self.bar_start = ts
            self.open = px
            self.high = px
            self.low = px
            self.close = px
            self.ticks = 1
            return

        self.high = max(float(self.high), px)
        self.low = min(float(self.low), px)
        self.close = px
        self.ticks += 1

    def ready_to_close(self) -> bool:
        return self.threshold_ticks is not None and self.ticks >= self.threshold_ticks

    def finalize(self, close_timestamp: Any, source: str) -> dict[str, Any]:
        if self.empty():
            raise RuntimeError("Cannot finalize an empty standard quote-tick bar.")
        if self.threshold_ticks is None:
            raise RuntimeError("Cannot finalize bar because threshold_ticks was not frozen at bar start.")

        return {
            "Datetime": pd.Timestamp(close_timestamp),
            "Bar_Start": pd.Timestamp(self.bar_start),
            "Open": float(self.open),
            "High": float(self.high),
            "Low": float(self.low),
            "Close": float(self.close),
            "Proxy_Value": int(self.ticks),
            "Ticks": int(self.ticks),
            "Threshold": int(self.threshold_ticks),
            "Threshold_Unit": THRESHOLD_UNIT_TICKS,
            "Research_Bar_Mode": BAR_MODE_STANDARD_QUOTE_TICK,
            "Executable": True,
            "Gap_Healed": False,
            "Source": source,
        }

    def reset(self) -> None:
        self.bar_start = None
        self.open = None
        self.high = None
        self.low = None
        self.close = None
        self.ticks = 0
        self.threshold_ticks = None


class TickDataError(RuntimeError):
    """Raised when quote tick input violates the V2 live-available data contract."""


# =============================================================================
# Tick preparation
# =============================================================================

def _progress(iterable: Iterable[Any], **kwargs: Any) -> Iterable[Any]:
    if tqdm is None:
        return iterable
    return tqdm(iterable, **kwargs)


def _standardize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    return out


def _find_first_existing(columns: Iterable[str], aliases: Iterable[str], logical_name: str) -> str:
    col_set = set(columns)
    for alias in aliases:
        if alias in col_set:
            return alias
    raise TickDataError(f"Missing required quote tick column for {logical_name}. Accepted aliases: {tuple(aliases)}")


def _extract_required_quote_columns(ticks: pd.DataFrame, source: str) -> pd.DataFrame:
    """Return timestamp/bid/ask columns with aliases normalized, before cleaning."""

    df = _standardize_column_names(ticks)
    timestamp_col = _find_first_existing(df.columns, TIMESTAMP_ALIASES, "timestamp")
    bid_col = _find_first_existing(df.columns, BID_ALIASES, "bid")
    ask_col = _find_first_existing(df.columns, ASK_ALIASES, "ask")

    return pd.DataFrame(
        {
            # format="ISO8601" (2026-11-07): without it pandas infers the format from the chunk's FIRST value, so a
            # whole-second first tick ("...:30+00:00") turned every fractional timestamp into NaT (whole chunks
            # dropped, e.g. USDMXN 2018-09-16 week 389,133 -> 397 ticks) and a fractional first tick dropped every
            # whole-second tick (~0.1 % of all ticks). Garbage still coerces to NaT and is dropped as before.
            "timestamp": pd.to_datetime(df[timestamp_col], utc=True, errors="coerce", format="ISO8601"),
            "bid": pd.to_numeric(df[bid_col], errors="coerce"),
            "ask": pd.to_numeric(df[ask_col], errors="coerce"),
        }
    )


def normalize_quote_tick_timestamps(ticks: pd.DataFrame, source: str = "dataframe") -> pd.DataFrame:
    """Normalize raw quote ticks to timestamp only for daily profiling.

    This validates bid/ask live availability but avoids mid-price calculation and
    per-chunk sorting because profiling only needs daily valid quote-tick counts.
    """

    out = _extract_required_quote_columns(ticks, source=source)
    before = len(out)
    out = out.dropna(subset=["timestamp", "bid", "ask"]).copy()
    out = out[(out["bid"] > 0) & (out["ask"] > 0) & (out["ask"] >= out["bid"])].copy()

    if out.empty:
        raise TickDataError(f"No valid quote ticks remain after cleaning source={source!r}. Raw rows={before}")

    return out[["timestamp"]]


def normalize_quote_ticks(ticks: pd.DataFrame, source: str = "dataframe") -> pd.DataFrame:
    """Normalize raw quote ticks to timestamp/bid/ask/mid_price.

    Accepted input aliases:
    - timestamp: timestamp, time, datetime, date_time
    - bid: bid, bidprice, bid_price, bid price
    - ask: ask, askprice, ask_price, ask price
    """

    out = _extract_required_quote_columns(ticks, source=source)

    before = len(out)
    out = out.dropna(subset=["timestamp", "bid", "ask"]).copy()
    out = out[(out["bid"] > 0) & (out["ask"] > 0) & (out["ask"] >= out["bid"])].copy()

    if out.empty:
        raise TickDataError(f"No valid quote ticks remain after cleaning source={source!r}. Raw rows={before}")

    out = out.sort_values("timestamp").reset_index(drop=True)
    out["mid_price"] = (out["bid"] + out["ask"]) / 2.0
    return out[["timestamp", "bid", "ask", "mid_price"]]


def get_sorted_csv_files(folder: str | Path) -> list[Path]:
    """Return sorted CSV files from an asset tick folder."""

    tick_folder = Path(folder)
    if not tick_folder.exists():
        raise FileNotFoundError(f"Tick folder does not exist: {tick_folder}")
    files = sorted(tick_folder.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"No CSV tick files found in folder: {tick_folder}")
    return files


# =============================================================================
# Causal daily thresholds
# =============================================================================

def profile_daily_tick_counts_from_dataframe(ticks: pd.DataFrame, source: str = "dataframe") -> pd.DataFrame:
    """Build daily valid quote-tick counts from an in-memory tick DataFrame."""

    normalized = normalize_quote_tick_timestamps(ticks, source=source)
    daily = (
        normalized.assign(date=normalized["timestamp"].dt.floor("D"))
        .groupby("date", as_index=False)
        .size()
        .rename(columns={"size": "tick_count"})
        .sort_values("date")
        .reset_index(drop=True)
    )
    return daily


def profile_daily_tick_counts_from_csv_folder(
    tick_folder: str | Path,
    chunksize: int = DEFAULT_CHUNKSIZE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Profile daily valid quote ticks from one asset's raw tick CSV folder."""

    files = get_sorted_csv_files(tick_folder)
    daily_counts: dict[pd.Timestamp, int] = defaultdict(int)
    raw_rows_seen = 0
    valid_ticks_used = 0
    started = time.time()

    for file_path in _progress(files, desc="Profiling asset tick files", unit="file"):
        for chunk in pd.read_csv(file_path, chunksize=chunksize):
            raw_rows_seen += len(chunk)
            normalized = normalize_quote_tick_timestamps(chunk, source=file_path.name)
            valid_ticks_used += len(normalized)
            by_day = normalized["timestamp"].dt.floor("D").value_counts(sort=False)
            for date, count in by_day.items():
                daily_counts[pd.Timestamp(date)] += int(count)

    if not daily_counts:
        raise TickDataError(f"No valid quote ticks found in folder: {tick_folder}")

    daily = pd.DataFrame(
        [{"date": date, "tick_count": count} for date, count in sorted(daily_counts.items())]
    )

    stats = {
        "tick_folder": str(tick_folder),
        "raw_rows_seen": int(raw_rows_seen),
        "valid_ticks_used": int(valid_ticks_used),
        "trading_days": int(len(daily)),
        "date_start": str(daily["date"].min().date()),
        "date_end": str(daily["date"].max().date()),
        "median_daily_ticks": float(daily["tick_count"].median()),
        "mean_daily_ticks": float(daily["tick_count"].mean()),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    return daily, stats


def build_causal_daily_tick_thresholds(
    daily_tick_counts: pd.DataFrame,
    config: CausalThresholdConfig | None = None,
) -> tuple[dict[pd.Timestamp, int], dict[str, Any]]:
    """Build causal daily tick thresholds.

    For each date D:
        threshold_D = EWMA(prior completed daily tick counts) / target_bars_per_day

    The current day is never used to set its own threshold.
    """

    cfg = config or CausalThresholdConfig()
    cfg.validate()

    required = {"date", "tick_count"}
    missing = required.difference(daily_tick_counts.columns)
    if missing:
        raise TickDataError(f"daily_tick_counts missing required columns: {sorted(missing)}")

    daily = daily_tick_counts.copy()
    daily["date"] = to_datetime_iso(daily["date"], utc=True).dt.floor("D")
    daily["tick_count"] = pd.to_numeric(daily["tick_count"], errors="coerce")
    daily = daily.dropna(subset=["date", "tick_count"]).sort_values("date").reset_index(drop=True)

    if len(daily) <= cfg.min_warmup_days:
        raise TickDataError(
            f"Need at least {cfg.min_warmup_days + 1} daily observations; got {len(daily)}."
        )

    thresholds: dict[pd.Timestamp, int] = {}
    prior_counts: list[float] = []

    for _, row in daily.iterrows():
        date = pd.Timestamp(row["date"]).floor("D")

        if len(prior_counts) >= cfg.min_warmup_days:
            ewm_daily_ticks = float(
                pd.Series(prior_counts)
                .ewm(span=cfg.daily_tick_ewma_span_days, adjust=False)
                .mean()
                .iloc[-1]
            )
            thresholds[date] = max(1, int(round(ewm_daily_ticks / cfg.target_bars_per_day)))

        prior_counts.append(float(row["tick_count"]))

    active = pd.Series(list(thresholds.values()), dtype="float64")
    meta = {
        "bar_type": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_mode": "causal_daily_ewma_tick_count",
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "target_bars_per_day": cfg.target_bars_per_day,
        "min_warmup_days": cfg.min_warmup_days,
        "warmup_days_are_emitted": False,
        "daily_tick_ewma_span_days": cfg.daily_tick_ewma_span_days,
        "first_threshold_date": str(min(thresholds).date()),
        "last_threshold_date": str(max(thresholds).date()),
        "median_ticks_per_bar_threshold": float(active.median()),
        "mean_ticks_per_bar_threshold": float(active.mean()),
        "min_ticks_per_bar_threshold": float(active.min()),
        "max_ticks_per_bar_threshold": float(active.max()),
        "uses_future_data": False,
    }
    return thresholds, meta


# =============================================================================
# Bar forging
# =============================================================================

def _prepare_threshold_lookup(
    thresholds_by_date: Mapping[pd.Timestamp, int] | None,
) -> dict[Any, int] | None:
    """Convert threshold keys to Python date objects for fast per-tick lookup."""

    if thresholds_by_date is None:
        return None
    return {pd.Timestamp(date).date(): int(threshold) for date, threshold in thresholds_by_date.items()}


def _threshold_for_tick_date_fast(
    tick_date: Any,
    fixed_threshold_ticks: int | None,
    threshold_lookup: Mapping[Any, int] | None,
) -> int | None:
    if fixed_threshold_ticks is not None:
        return int(fixed_threshold_ticks)
    if threshold_lookup is None:
        raise ValueError("Either fixed_threshold_ticks or thresholds_by_date must be provided.")
    return threshold_lookup.get(tick_date)


def forge_standard_quote_tick_bars_from_ticks(
    ticks: pd.DataFrame,
    *,
    fixed_threshold_ticks: int | None = None,
    thresholds_by_date: Mapping[pd.Timestamp, int] | None = None,
    source: str = "dataframe",
    validate_output: bool = True,
) -> pd.DataFrame:
    """Forge standard_quote_tick bars from an in-memory quote tick DataFrame.

    This function is asset-agnostic. Asset scripts should call it once per asset
    and save the resulting bars under that asset's shared bar folder.
    """

    if fixed_threshold_ticks is not None and fixed_threshold_ticks <= 0:
        raise ValueError("fixed_threshold_ticks must be > 0")

    normalized = normalize_quote_ticks(ticks, source=source)
    threshold_lookup = _prepare_threshold_lookup(thresholds_by_date)
    state = StandardQuoteTickBarState()
    bars: list[dict[str, Any]] = []
    skipped_ticks_without_threshold = 0

    timestamps = normalized["timestamp"].to_numpy()
    prices = normalized["mid_price"].to_numpy(dtype="float64")
    dates = normalized["timestamp"].dt.date.to_numpy()

    for ts, px, tick_date in zip(timestamps, prices, dates):
        if state.empty():
            threshold = _threshold_for_tick_date_fast(tick_date, fixed_threshold_ticks, threshold_lookup)
            if threshold is None:
                skipped_ticks_without_threshold += 1
                continue
            state.freeze_threshold(threshold)

        state.update(ts, px)

        if state.ready_to_close():
            bars.append(state.finalize(close_timestamp=ts, source=source))
            state.reset()

    if not bars:
        raise TickDataError(
            "No bars were forged. Check threshold settings and input ticks. "
            f"Skipped ticks without threshold={skipped_ticks_without_threshold}."
        )

    bars_df = pd.DataFrame(bars)
    bars_df["Datetime"] = to_datetime_iso(bars_df["Datetime"], utc=True)
    bars_df["Bar_Start"] = to_datetime_iso(bars_df["Bar_Start"], utc=True)
    bars_df = bars_df.sort_values("Datetime").reset_index(drop=True)

    if validate_output:
        require_valid_bar_dataframe(bars_df)

    return bars_df


def forge_standard_quote_tick_bars_from_csv_folder(
    tick_folder: str | Path,
    *,
    output_path: str | Path | None = None,
    fixed_threshold_ticks: int | None = None,
    thresholds_by_date: Mapping[pd.Timestamp, int] | None = None,
    chunksize: int = DEFAULT_CHUNKSIZE,
    source: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Forge bars from one asset's tick CSV folder.

    For very large assets this still accumulates bars in memory, not raw ticks.
    Use causal thresholds from ``build_causal_daily_tick_thresholds`` for research
    production runs, or a fixed threshold for controlled tests.
    """

    files = get_sorted_csv_files(tick_folder)
    threshold_lookup = _prepare_threshold_lookup(thresholds_by_date)
    state = StandardQuoteTickBarState()
    bars: list[dict[str, Any]] = []
    raw_rows_seen = 0
    valid_ticks_seen = 0
    skipped_ticks_without_threshold = 0
    started = time.time()
    source_name = source or Path(tick_folder).name

    for file_path in _progress(files, desc="Forging asset bars", unit="file"):
        for chunk in pd.read_csv(file_path, chunksize=chunksize):
            raw_rows_seen += len(chunk)
            normalized = normalize_quote_ticks(chunk, source=file_path.name)
            valid_ticks_seen += len(normalized)

            timestamps = normalized["timestamp"].to_numpy()
            prices = normalized["mid_price"].to_numpy(dtype="float64")
            dates = normalized["timestamp"].dt.date.to_numpy()

            for ts, px, tick_date in zip(timestamps, prices, dates):
                if state.empty():
                    threshold = _threshold_for_tick_date_fast(tick_date, fixed_threshold_ticks, threshold_lookup)
                    if threshold is None:
                        skipped_ticks_without_threshold += 1
                        continue
                    state.freeze_threshold(threshold)

                state.update(ts, px)

                if state.ready_to_close():
                    bars.append(state.finalize(close_timestamp=ts, source=source_name))
                    state.reset()

    if not bars:
        raise TickDataError(
            "No bars were forged from CSV folder. Check threshold settings and input ticks. "
            f"Skipped ticks without threshold={skipped_ticks_without_threshold}."
        )

    bars_df = pd.DataFrame(bars)
    bars_df["Datetime"] = to_datetime_iso(bars_df["Datetime"], utc=True)
    bars_df["Bar_Start"] = to_datetime_iso(bars_df["Bar_Start"], utc=True)
    bars_df = bars_df.sort_values("Datetime").reset_index(drop=True)
    require_valid_bar_dataframe(bars_df)

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".csv":
            bars_df.to_csv(out, index=False)
        elif out.suffix.lower() in {".parquet", ".pq"}:
            bars_df.to_parquet(out, index=False)
        else:
            raise ValueError(f"Unsupported output format: {out.suffix}")

    stats = {
        "tick_folder": str(tick_folder),
        "output_path": str(output_path) if output_path is not None else None,
        "bar_mode": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "proxy_value_definition": "Proxy_Value equals Ticks for backward compatibility only.",
        "raw_rows_seen": int(raw_rows_seen),
        "valid_ticks_seen": int(valid_ticks_seen),
        "skipped_ticks_without_threshold": int(skipped_ticks_without_threshold),
        "bars_forged": int(len(bars_df)),
        "start": str(bars_df["Datetime"].min()),
        "end": str(bars_df["Datetime"].max()),
        "median_ticks_per_bar": float(bars_df["Ticks"].median()),
        "mean_ticks_per_bar": float(bars_df["Ticks"].mean()),
        "elapsed_seconds": round(time.time() - started, 2),
    }
    return bars_df, stats


# =============================================================================
# Manifest helpers
# =============================================================================

def write_bar_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    """Write a stable JSON bar manifest."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(manifest), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


# =============================================================================
# Self-test
# =============================================================================

def _make_example_ticks() -> pd.DataFrame:
    rows = []
    start = pd.Timestamp("2026-05-15T10:00:00Z")
    for i, mid in enumerate([100.0, 100.2, 100.1, 100.5, 100.7, 100.6, 100.9]):
        rows.append(
            {
                "timestamp": start + pd.Timedelta(seconds=i),
                "bidprice": mid - 0.01,
                "askprice": mid + 0.01,
            }
        )
    return pd.DataFrame(rows)


def _run_self_test() -> None:
    ticks = _make_example_ticks()
    bars = forge_standard_quote_tick_bars_from_ticks(
        ticks,
        fixed_threshold_ticks=3,
        source="self_test_asset_ticks",
    )

    if len(bars) != 2:
        raise SystemExit(f"❌ Expected 2 completed bars from 7 ticks at threshold 3; got {len(bars)}")

    if not (bars["Proxy_Value"] == bars["Ticks"]).all():
        raise SystemExit("❌ Proxy_Value must equal Ticks in V2 standard_quote_tick bars.")

    if not (bars["Threshold_Unit"] == THRESHOLD_UNIT_TICKS).all():
        raise SystemExit("❌ Threshold_Unit must be ticks.")

    if not (bars["Research_Bar_Mode"] == BAR_MODE_STANDARD_QUOTE_TICK).all():
        raise SystemExit("❌ Research_Bar_Mode must be standard_quote_tick.")

    daily = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=25, freq="D", tz="UTC"),
            "tick_count": [1000 + i * 10 for i in range(25)],
        }
    )
    thresholds, meta = build_causal_daily_tick_thresholds(
        daily,
        CausalThresholdConfig(target_bars_per_day=50, min_warmup_days=20, daily_tick_ewma_span_days=20),
    )
    if len(thresholds) != 5:
        raise SystemExit(f"❌ Expected 5 post-warmup thresholds; got {len(thresholds)}")
    if meta["uses_future_data"] is not False:
        raise SystemExit("❌ Causal threshold metadata must declare uses_future_data=False")

    print("✅ shared.bars.standard_quote_tick_builder self-test passed.")
    print(f"Example ticks: {len(ticks)}")
    print(f"Completed bars: {len(bars)}")
    print(f"Bar columns: {', '.join(bars.columns)}")
    print(f"Causal thresholds produced: {len(thresholds)}")


if __name__ == "__main__":
    _run_self_test()
