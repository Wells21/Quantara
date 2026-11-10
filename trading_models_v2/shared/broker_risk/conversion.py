from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
try:
    from shared.io.timeparse import to_datetime_iso  # ISO-8601-safe pd.to_datetime (2026-11-07)
except ModuleNotFoundError:  # run as a script without the repo root on sys.path
    import sys as _sys_td
    from pathlib import Path as _Path_td
    _sys_td.path.insert(0, str(_Path_td(__file__).resolve().parents[2]))
    from shared.io.timeparse import to_datetime_iso

from .contract import BrokerRiskError, ConversionContract


TIMESTAMP_COLUMN_CANDIDATES = (
    "timestamp",
    "time",
    "datetime",
    "Datetime",
    "date",
)
PRICE_COLUMN_CANDIDATES = (
    "price",
    "open",
    "mid",
    "close",
)


@dataclass(frozen=True)
class ConversionLookupSummary:
    row_count: int
    valid_count: int
    invalid_count: int
    status_counts: dict[str, int]
    min_rate: float | None
    median_rate: float | None
    max_rate: float | None
    max_age_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "valid_count": self.valid_count,
            "invalid_count": self.invalid_count,
            "status_counts": dict(self.status_counts),
            "min_rate": self.min_rate,
            "median_rate": self.median_rate,
            "max_rate": self.max_rate,
            "max_age_seconds": self.max_age_seconds,
        }


def _first_existing(columns: Sequence[str], candidates: Sequence[str]) -> str | None:
    lookup = {str(column): str(column) for column in columns}
    lower_lookup = {str(column).lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate in lookup:
            return lookup[candidate]
        if candidate.lower() in lower_lookup:
            return lower_lookup[candidate.lower()]
    return None


def normalize_conversion_frame(
    frame: pd.DataFrame,
    *,
    timestamp_column: str | None = None,
    price_column: str | None = None,
    source_symbol: str = "",
) -> pd.DataFrame:
    """Return a canonical causal conversion series.

    The timestamp represents when the price became available. Callers should use
    a bar-open price or a true tick timestamp; a bar close must not be stamped at
    its bar-open time because that would introduce look-ahead.
    """

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    if frame.empty:
        raise BrokerRiskError("conversion frame is empty")

    ts_col = timestamp_column or _first_existing(frame.columns, TIMESTAMP_COLUMN_CANDIDATES)
    px_col = price_column or _first_existing(frame.columns, PRICE_COLUMN_CANDIDATES)
    if not ts_col or ts_col not in frame.columns:
        raise BrokerRiskError(
            f"conversion frame has no timestamp column; columns={list(frame.columns)}"
        )
    if not px_col or px_col not in frame.columns:
        raise BrokerRiskError(
            f"conversion frame has no price column; columns={list(frame.columns)}"
        )

    out = pd.DataFrame(
        {
            "conversion_timestamp": to_datetime_iso(
                frame[ts_col], utc=True, errors="coerce"
            ),
            "conversion_price": pd.to_numeric(frame[px_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["conversion_timestamp", "conversion_price"]).copy()
    out = out[np.isfinite(out["conversion_price"]) & (out["conversion_price"] > 0)]
    out = (
        out.sort_values("conversion_timestamp")
        .drop_duplicates("conversion_timestamp", keep="last")
        .reset_index(drop=True)
    )
    if out.empty:
        raise BrokerRiskError("conversion frame contains no valid positive observations")
    out["conversion_source_symbol"] = str(source_symbol or "").strip().upper()
    return out


def load_conversion_frame(
    path: str | Path,
    *,
    timestamp_column: str | None = None,
    price_column: str | None = None,
    source_symbol: str = "",
) -> pd.DataFrame:
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    else:
        raise BrokerRiskError(
            f"unsupported conversion file extension {path.suffix!r}; use CSV or Parquet"
        )
    return normalize_conversion_frame(
        frame,
        timestamp_column=timestamp_column,
        price_column=price_column,
        source_symbol=source_symbol or path.stem,
    )


def _empty_result(index: pd.Index, *, mode: str, symbol: str) -> pd.DataFrame:
    out = pd.DataFrame(index=index)
    out["broker_conversion_mode"] = mode
    out["broker_conversion_symbol"] = symbol
    out["broker_conversion_source_timestamp"] = pd.Series(
        pd.NaT, index=index, dtype="datetime64[ns, UTC]"
    )
    out["broker_conversion_source_price"] = np.nan
    out["broker_conversion_age_seconds"] = np.nan
    out["broker_profit_to_account_rate"] = np.nan
    out["broker_conversion_status"] = "unresolved"
    return out


def resolve_conversion_rates(
    *,
    event_times: pd.Series | Sequence[Any],
    contract: ConversionContract,
    conversion_frame: pd.DataFrame | None = None,
    instrument_prices: pd.Series | Sequence[Any] | None = None,
) -> pd.DataFrame:
    """Resolve account-currency units per profit-currency unit for each event.

    Direct and inverse symbol conversions use a backward ``merge_asof`` only, so
    a conversion observation after the event can never be selected.
    """

    raw_times = pd.Series(event_times)
    original_index = raw_times.index
    parsed_times = to_datetime_iso(raw_times, utc=True, errors="coerce")
    result = _empty_result(
        original_index,
        mode=contract.mode,
        symbol=contract.symbol,
    )

    valid_time = parsed_times.notna()
    result.loc[~valid_time, "broker_conversion_status"] = "invalid_event_time"

    if contract.mode in {"identity", "explicit_rate"}:
        rate = contract.resolve_rate()
        result.loc[valid_time, "broker_profit_to_account_rate"] = rate
        result.loc[valid_time, "broker_conversion_age_seconds"] = 0.0
        result.loc[valid_time, "broker_conversion_status"] = (
            "ok_identity" if contract.mode == "identity" else "ok_explicit_rate"
        )
        return result

    if contract.mode == "instrument_inverse":
        if instrument_prices is None:
            result.loc[valid_time, "broker_conversion_status"] = (
                "missing_instrument_price"
            )
            return result
        prices = pd.to_numeric(
            pd.Series(instrument_prices, index=original_index), errors="coerce"
        )
        valid_price = prices.notna() & np.isfinite(prices) & (prices > 0)
        valid = valid_time & valid_price
        result.loc[valid_time & ~valid_price, "broker_conversion_status"] = (
            "invalid_instrument_price"
        )
        result.loc[valid, "broker_conversion_source_price"] = prices.loc[valid]
        result.loc[valid, "broker_conversion_age_seconds"] = 0.0
        result.loc[valid, "broker_profit_to_account_rate"] = 1.0 / prices.loc[valid]
        result.loc[valid, "broker_conversion_status"] = "ok_instrument_inverse"
        return result

    if contract.mode not in {"direct_symbol", "inverse_symbol"}:
        result.loc[valid_time, "broker_conversion_status"] = (
            f"unsupported_mode:{contract.mode}"
        )
        return result

    if conversion_frame is None or conversion_frame.empty:
        result.loc[valid_time, "broker_conversion_status"] = (
            "missing_conversion_series"
        )
        return result

    canonical_input = {
        "conversion_timestamp",
        "conversion_price",
    }.issubset(conversion_frame.columns)
    source = normalize_conversion_frame(
        conversion_frame,
        timestamp_column="conversion_timestamp" if canonical_input else None,
        price_column="conversion_price" if canonical_input else None,
        source_symbol=contract.symbol,
    )
    left = pd.DataFrame(
        {
            "_original_index": original_index[valid_time],
            "event_time": parsed_times.loc[valid_time].to_numpy(),
        }
    ).sort_values("event_time")
    matched = pd.merge_asof(
        left,
        source[
            [
                "conversion_timestamp",
                "conversion_price",
                "conversion_source_symbol",
            ]
        ].sort_values("conversion_timestamp"),
        left_on="event_time",
        right_on="conversion_timestamp",
        direction="backward",
        allow_exact_matches=True,
    )
    matched = matched.set_index("_original_index")
    matched.index.name = None

    has_match = matched["conversion_timestamp"].notna()
    result.loc[valid_time, "broker_conversion_status"] = "missing_prior_conversion"
    if not has_match.any():
        return result

    matched_valid = matched.loc[has_match].copy()
    ages = (
        matched_valid["event_time"] - matched_valid["conversion_timestamp"]
    ).dt.total_seconds()
    prices = pd.to_numeric(matched_valid["conversion_price"], errors="coerce")
    positive = prices.notna() & np.isfinite(prices) & (prices > 0)

    matched_indices = matched_valid.index
    result.loc[matched_indices, "broker_conversion_source_timestamp"] = (
        matched_valid["conversion_timestamp"]
    )
    result.loc[matched_indices, "broker_conversion_source_price"] = prices
    result.loc[matched_indices, "broker_conversion_age_seconds"] = ages
    result.loc[matched_indices, "broker_conversion_status"] = (
        "invalid_conversion_price"
    )

    usable_indices = matched_valid.index[positive]
    usable_prices = prices.loc[usable_indices]
    rates = (
        usable_prices
        if contract.mode == "direct_symbol"
        else 1.0 / usable_prices
    )
    result.loc[usable_indices, "broker_profit_to_account_rate"] = rates
    result.loc[usable_indices, "broker_conversion_status"] = "ok"

    if contract.max_staleness_seconds is not None:
        stale = ages > float(contract.max_staleness_seconds)
        stale_indices = matched_valid.index[stale]
        result.loc[stale_indices, "broker_profit_to_account_rate"] = np.nan
        result.loc[stale_indices, "broker_conversion_status"] = "stale_conversion"

    return result


def summarize_conversion_lookup(result: pd.DataFrame) -> ConversionLookupSummary:
    required = {
        "broker_profit_to_account_rate",
        "broker_conversion_age_seconds",
        "broker_conversion_status",
    }
    missing = sorted(required - set(result.columns))
    if missing:
        raise BrokerRiskError(f"conversion lookup result missing columns: {missing}")

    rates = pd.to_numeric(
        result["broker_profit_to_account_rate"], errors="coerce"
    )
    valid = rates.notna() & np.isfinite(rates) & (rates > 0)
    ages = pd.to_numeric(
        result.loc[valid, "broker_conversion_age_seconds"], errors="coerce"
    ).dropna()
    valid_rates = rates.loc[valid]
    statuses = (
        result["broker_conversion_status"]
        .astype(str)
        .value_counts(dropna=False)
        .sort_index()
        .to_dict()
    )
    return ConversionLookupSummary(
        row_count=int(len(result)),
        valid_count=int(valid.sum()),
        invalid_count=int((~valid).sum()),
        status_counts={str(k): int(v) for k, v in statuses.items()},
        min_rate=float(valid_rates.min()) if not valid_rates.empty else None,
        median_rate=float(valid_rates.median()) if not valid_rates.empty else None,
        max_rate=float(valid_rates.max()) if not valid_rates.empty else None,
        max_age_seconds=float(ages.max()) if not ages.empty else None,
    )


class HistoricalConversionStore:
    """Lazy local conversion-series loader with no hash coupling."""

    def __init__(
        self,
        symbol_paths: Mapping[str, str | Path],
        *,
        timestamp_column: str = "timestamp",
        price_column: str = "price",
    ) -> None:
        self.symbol_paths = {
            str(symbol).strip().upper(): Path(path)
            for symbol, path in symbol_paths.items()
            if str(symbol).strip()
        }
        self.timestamp_column = timestamp_column
        self.price_column = price_column
        self._cache: dict[str, pd.DataFrame] = {}

    @classmethod
    def from_manifest(cls, path: str | Path) -> "HistoricalConversionStore":
        manifest_path = Path(path)
        if not manifest_path.exists() or not manifest_path.is_file():
            raise FileNotFoundError(manifest_path)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(
                f"conversion manifest must be a JSON object: {manifest_path}"
            )
        series = payload.get("series")
        if not isinstance(series, dict):
            raise BrokerRiskError("conversion manifest contains no series mapping")
        paths: dict[str, Path] = {}
        for symbol, metadata in series.items():
            if not isinstance(metadata, dict):
                continue
            raw_path = metadata.get("path")
            if not raw_path:
                continue
            candidate = Path(str(raw_path))
            if not candidate.is_absolute():
                candidate = manifest_path.parent / candidate
            paths[str(symbol).strip().upper()] = candidate
        if not paths:
            raise BrokerRiskError(
                "conversion manifest contains no usable local series paths"
            )
        return cls(paths)

    def get(self, symbol: str) -> pd.DataFrame:
        key = str(symbol or "").strip().upper()
        if not key:
            raise BrokerRiskError("conversion symbol is required")
        if key not in self.symbol_paths:
            raise BrokerRiskError(
                f"no historical conversion file configured for {key}"
            )
        if key not in self._cache:
            self._cache[key] = load_conversion_frame(
                self.symbol_paths[key],
                timestamp_column=self.timestamp_column,
                price_column=self.price_column,
                source_symbol=key,
            )
        return self._cache[key]

    def resolve(
        self,
        *,
        event_times: pd.Series | Sequence[Any],
        contract: ConversionContract,
        instrument_prices: pd.Series | Sequence[Any] | None = None,
    ) -> pd.DataFrame:
        frame = None
        if contract.mode in {"direct_symbol", "inverse_symbol"}:
            frame = self.get(contract.symbol)
        return resolve_conversion_rates(
            event_times=event_times,
            contract=contract,
            conversion_frame=frame,
            instrument_prices=instrument_prices,
        )
