from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

THIS_FILE = Path(__file__).resolve()
V2_ROOT = THIS_FILE.parents[1]
REPO_ROOT = THIS_FILE.parents[2]
for root in (V2_ROOT, REPO_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from shared.broker_risk import BrokerRiskError, load_broker_profile  # noqa: E402
from shared.io.timeparse import to_datetime_iso  # noqa: E402  (ISO-8601-safe pd.to_datetime, 2026-11-07)

SCHEMA_VERSION = "v2.3"
PROFILE_PATH = (
    V2_ROOT
    / "portfolio_allocator"
    / "config"
    / "broker_profiles"
    / "mt5_broker_profile.json"
)
LOCAL_SETTINGS_PATH = V2_ROOT / "portfolio_live" / "local_live_settings.json"
OUTPUT_DIR = V2_ROOT / "portfolio_allocator" / "config" / "broker_conversions"
MANIFEST_PATH = OUTPUT_DIR / "historical_conversion_manifest.json"

DEFAULT_TIMEFRAME = "H1"
TIMEFRAME_SPECS = {
    "M1": {
        "mt5_name": "TIMEFRAME_M1",
        "seconds": 60,
        "default_chunk_days": 30,
    },
    "M5": {
        "mt5_name": "TIMEFRAME_M5",
        "seconds": 300,
        "default_chunk_days": 90,
    },
    "M15": {
        "mt5_name": "TIMEFRAME_M15",
        "seconds": 900,
        "default_chunk_days": 180,
    },
    "M30": {
        "mt5_name": "TIMEFRAME_M30",
        "seconds": 1800,
        "default_chunk_days": 180,
    },
    "H1": {
        "mt5_name": "TIMEFRAME_H1",
        "seconds": 3600,
        "default_chunk_days": 180,
    },
}

START_KEYS = (
    "date_start",
    "start_time",
    "start_date",
    "data_start",
    "first_event",
)
END_KEYS = (
    "date_end",
    "end_time",
    "end_date",
    "data_end",
    "last_event",
)


def load_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def parse_utc(value: Any) -> pd.Timestamp | None:
    parsed = to_datetime_iso(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def collect_named_dates(
    value: Any,
    key: str = "",
) -> list[tuple[str, pd.Timestamp]]:
    found: list[tuple[str, pd.Timestamp]] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.extend(collect_named_dates(child, str(child_key)))
    elif isinstance(value, list):
        for child in value:
            found.extend(collect_named_dates(child, key))
    elif isinstance(value, str):
        lower_key = key.lower()
        if any(token in lower_key for token in (*START_KEYS, *END_KEYS)):
            parsed = parse_utc(value)
            if parsed is not None:
                found.append((lower_key, parsed))
    return found


def discover_edge_range() -> tuple[pd.Timestamp, pd.Timestamp, dict[str, Any]]:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    inspected = 0
    passed = 0

    pattern = "assets/*/edges/*/artifacts/edge_manifest.json"
    for path in sorted(V2_ROOT.glob(pattern)):
        inspected += 1
        try:
            payload = load_json(path, required=True)
        except Exception:
            continue

        if str(payload.get("deployment_status", "")).upper() != "PASS":
            continue
        if payload.get("deployable") is not True:
            continue

        passed += 1
        for key, timestamp in collect_named_dates(payload):
            if any(token in key for token in START_KEYS):
                starts.append(timestamp)
            if any(token in key for token in END_KEYS):
                ends.append(timestamp)

    if not starts or not ends:
        raise BrokerRiskError(
            "Could not discover a research range from deployable PASS edge "
            "manifests. Supply --start and --end explicitly."
        )

    start = min(starts)
    end = max(ends)
    if end <= start:
        raise BrokerRiskError(f"Invalid discovered range: {start} -> {end}")

    return start, end, {
        "source": "deployable_pass_edge_manifests",
        "edge_manifests_inspected": inspected,
        "pass_edge_manifests": passed,
        "start_candidates": len(starts),
        "end_candidates": len(ends),
        "discovered_start": start.isoformat(),
        "discovered_end": end.isoformat(),
    }


def first_present(*values: Any) -> Any:
    return next(
        (value for value in values if value not in (None, "")),
        None,
    )


def initialize_kwargs(
    args: argparse.Namespace,
    local: dict[str, Any],
) -> dict[str, Any]:
    login = first_present(
        args.login,
        os.getenv("MT5_LOGIN"),
        local.get("mt5_login"),
        local.get("MT5_LOGIN"),
    )
    password = first_present(
        args.password,
        os.getenv("MT5_PASSWORD"),
        local.get("mt5_password"),
        local.get("MT5_PASSWORD"),
    )
    server = first_present(
        args.server,
        os.getenv("MT5_SERVER"),
        local.get("mt5_server"),
        local.get("MT5_SERVER"),
    )
    terminal_path = first_present(
        args.terminal_path,
        os.getenv("MT5_PATH"),
        local.get("mt5_path"),
        local.get("MT5_PATH"),
    )
    timeout = first_present(
        args.timeout_ms,
        os.getenv("MT5_TIMEOUT_MS"),
        local.get("mt5_timeout_ms"),
        local.get("MT5_TIMEOUT_MS"),
        60000,
    )

    kwargs: dict[str, Any] = {"timeout": int(timeout)}
    if login is not None:
        kwargs["login"] = int(login)
    if password:
        kwargs["password"] = str(password)
    if server:
        kwargs["server"] = str(server)
    if terminal_path:
        kwargs["path"] = str(terminal_path)
    return kwargs


def local_conversion_map(local: dict[str, Any]) -> dict[str, str]:
    raw = (
        local.get("mt5_conversion_symbol_map")
        or local.get("MT5_CONVERSION_SYMBOL_MAP")
        or {}
    )
    if not isinstance(raw, dict):
        raise TypeError(
            "mt5_conversion_symbol_map must be a JSON object when provided"
        )
    return {
        str(key).strip().upper(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def resolve_symbol(
    mt5: Any,
    canonical: str,
    mapping: dict[str, str],
) -> tuple[str, str]:
    canonical = canonical.strip().upper()

    if canonical in mapping:
        mapped = mapping[canonical]
        if mt5.symbol_select(mapped, True) and mt5.symbol_info(mapped) is not None:
            return mapped, "local_conversion_symbol_map"
        raise BrokerRiskError(
            f"Configured MT5 symbol {mapped!r} for {canonical} is unavailable."
        )

    if mt5.symbol_select(canonical, True):
        if mt5.symbol_info(canonical) is not None:
            return canonical, "exact"

    candidates = mt5.symbols_get(f"*{canonical}*") or ()
    names = sorted(
        {
            str(getattr(item, "name", "") or "").strip()
            for item in candidates
            if str(getattr(item, "name", "") or "").strip()
        },
        key=lambda name: (
            0 if name.upper().startswith(canonical) else 1,
            len(name),
            name,
        ),
    )
    for name in names:
        if mt5.symbol_select(name, True) and mt5.symbol_info(name) is not None:
            return name, "auto_discovered"

    raise BrokerRiskError(
        f"Could not resolve {canonical}. Add mt5_conversion_symbol_map "
        "to portfolio_live/local_live_settings.json."
    )


def build_contract_plan(bundle: Any) -> tuple[dict[str, set[str]], dict[str, Any]]:
    symbol_modes: dict[str, set[str]] = {}
    contracts: dict[str, Any] = {}

    for asset in sorted(bundle.contracts):
        contract = bundle.contracts[asset]
        conversion = bundle.conversions[asset]
        contracts[asset] = {
            "broker_symbol": contract.broker_symbol,
            "account_currency": contract.account_currency,
            "profit_currency": contract.profit_currency,
            "contract_size": contract.contract_size,
            "min_lot": contract.min_lot,
            "lot_step": contract.lot_step,
            "max_lot": contract.max_lot,
            "trade_calc_mode": contract.trade_calc_mode,
            "conversion": conversion.to_dict(),
        }
        if conversion.mode in {"direct_symbol", "inverse_symbol"}:
            symbol_modes.setdefault(conversion.symbol, set()).add(conversion.mode)

    return symbol_modes, contracts


def estimate_calendar_row_upper_bound(
    start: pd.Timestamp,
    end: pd.Timestamp,
    timeframe_seconds: int,
) -> int:
    duration_seconds = max(0.0, (end - start).total_seconds())
    return int(duration_seconds // max(1, timeframe_seconds)) + 1


def fetch_series(
    mt5: Any,
    *,
    symbol: str,
    timeframe: Any,
    start: pd.Timestamp,
    end: pd.Timestamp,
    chunk_days: int,
    modes: set[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise BrokerRiskError(f"symbol_info unavailable: {symbol}")

    point = float(getattr(info, "point", 0.0) or 0.0)
    if point <= 0:
        raise BrokerRiskError(f"{symbol}: invalid MT5 point value {point}")

    chunks: list[pd.DataFrame] = []
    cursor = start.to_pydatetime()
    final = end.to_pydatetime()
    step = timedelta(days=max(1, int(chunk_days)))
    empty_chunks = 0
    request_count = 0

    while cursor <= final:
        chunk_end = min(cursor + step, final)
        request_count += 1
        rates = mt5.copy_rates_range(
            symbol,
            timeframe,
            cursor,
            chunk_end,
        )
        if rates is None or len(rates) == 0:
            empty_chunks += 1
        else:
            chunks.append(pd.DataFrame(rates))

        if chunk_end >= final:
            break
        cursor = chunk_end + timedelta(seconds=1)

    if not chunks:
        raise BrokerRiskError(
            f"No MT5 rates returned for {symbol}: {start} -> {end}"
        )

    raw = pd.concat(chunks, ignore_index=True)
    if not {"time", "open"}.issubset(raw.columns):
        raise BrokerRiskError(
            f"{symbol}: MT5 rates lack required time/open columns"
        )

    timestamp = pd.to_datetime(
        raw["time"],
        unit="s",
        utc=True,
        errors="coerce",
    )
    bid = pd.to_numeric(raw["open"], errors="coerce")
    spread_points = pd.to_numeric(
        raw["spread"] if "spread" in raw.columns else 0.0,
        errors="coerce",
    ).fillna(0.0)
    ask = bid + spread_points * point
    mid = (bid + ask) / 2.0

    if modes == {"direct_symbol"}:
        conversion_price = ask
        price_policy = "ask_open_adverse_for_direct_conversion_loss"
    elif modes == {"inverse_symbol"}:
        conversion_price = bid
        price_policy = "bid_open_adverse_for_inverse_conversion_loss"
    else:
        conversion_price = mid
        price_policy = "mid_open_shared_symbol_mixed_modes"

    frame = pd.DataFrame(
        {
            "timestamp": timestamp,
            "price": conversion_price,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_points": spread_points,
        }
    )
    frame = frame.dropna(subset=["timestamp", "price"])
    frame = frame[
        np.isfinite(frame["price"])
        & (frame["price"] > 0)
    ]
    frame = (
        frame.sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    if frame.empty:
        raise BrokerRiskError(
            f"{symbol}: no valid positive historical observations"
        )

    gaps = frame["timestamp"].diff().dt.total_seconds().dropna()
    return frame, {
        "broker_symbol": symbol,
        "point": point,
        "row_count": int(len(frame)),
        "first_timestamp": frame["timestamp"].iloc[0].isoformat(),
        "last_timestamp": frame["timestamp"].iloc[-1].isoformat(),
        "min_price": float(frame["price"].min()),
        "median_price": float(frame["price"].median()),
        "max_price": float(frame["price"].max()),
        "max_gap_seconds": float(gaps.max()) if not gaps.empty else 0.0,
        "empty_chunk_count": empty_chunks,
        "mt5_request_count": request_count,
        "price_policy": price_policy,
    }


def validate_coverage(
    metadata: dict[str, Any],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    tolerance_days: int,
    max_gap_seconds: int,
) -> list[str]:
    findings: list[str] = []
    first = parse_utc(metadata.get("first_timestamp"))
    last = parse_utc(metadata.get("last_timestamp"))
    tolerance = pd.Timedelta(days=max(0, int(tolerance_days)))

    if first is None or first > start + tolerance:
        findings.append(
            f"start_coverage_missing:first={first}:required={start}"
        )
    if last is None or last < end - tolerance:
        findings.append(
            f"end_coverage_missing:last={last}:required={end}"
        )

    observed_gap = float(metadata.get("max_gap_seconds", 0.0))
    if observed_gap > float(max_gap_seconds):
        findings.append(
            f"gap_too_large:observed={observed_gap}:allowed={max_gap_seconds}"
        )

    return findings


def resolve_requested_range(
    args: argparse.Namespace,
) -> tuple[pd.Timestamp, pd.Timestamp, dict[str, Any]]:
    if args.start and args.end:
        start = parse_utc(args.start)
        end = parse_utc(args.end)
        discovery = {
            "source": "explicit_cli",
            "discovered_start": str(start),
            "discovered_end": str(end),
        }
    else:
        found_start, found_end, discovery = discover_edge_range()
        start = parse_utc(args.start) if args.start else found_start
        end = parse_utc(args.end) if args.end else found_end

    if start is None or end is None or end <= start:
        raise BrokerRiskError(f"Invalid requested range: {start} -> {end}")

    start -= pd.Timedelta(days=max(0, int(args.start_buffer_days)))
    end += pd.Timedelta(days=max(0, int(args.end_buffer_days)))
    return start, end, discovery


def build(args: argparse.Namespace) -> dict[str, Any]:
    profile_path = Path(args.profile).expanduser().resolve()
    local_path = Path(args.local_settings).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()

    timeframe_spec = TIMEFRAME_SPECS[args.timeframe]
    effective_chunk_days = (
        int(args.chunk_days)
        if args.chunk_days is not None
        else int(timeframe_spec["default_chunk_days"])
    )
    if effective_chunk_days <= 0:
        raise BrokerRiskError("--chunk-days must be greater than zero")

    bundle = load_broker_profile(
        profile_path,
        max_staleness_seconds=args.max_staleness_seconds,
        require_status_pass=True,
    )
    local = load_json(local_path, required=False)
    start, end, discovery = resolve_requested_range(args)
    symbol_modes, contract_rows = build_contract_plan(bundle)

    estimated_rows_per_symbol = estimate_calendar_row_upper_bound(
        start,
        end,
        int(timeframe_spec["seconds"]),
    )
    plan = {
        "timeframe": args.timeframe,
        "timeframe_seconds": int(timeframe_spec["seconds"]),
        "chunk_days": effective_chunk_days,
        "symbols": sorted(symbol_modes),
        "required_series_count": len(symbol_modes),
        "calendar_row_upper_bound_per_symbol": estimated_rows_per_symbol,
        "calendar_row_upper_bound_all_symbols": (
            estimated_rows_per_symbol * len(symbol_modes)
        ),
        "note": (
            "This is a calendar upper bound. Actual Forex H1 rows are lower "
            "because weekends and market closures have no bars."
        ),
    }

    print("V2 Portfolio Allocator 00B — historical broker conversion artifacts")
    print(f"Timeframe: {args.timeframe}")
    print(f"Chunk size: {effective_chunk_days} days")
    print("Symbols: " + ", ".join(sorted(symbol_modes)))
    print(
        "Calendar row upper bound: "
        f"{estimated_rows_per_symbol:,} per symbol; "
        f"{plan['calendar_row_upper_bound_all_symbols']:,} total"
    )
    print(f"Required range: {start.isoformat()} -> {end.isoformat()}")

    if args.plan_only:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "PLAN_ONLY",
            "artifact_policy": "hashless_overwriteable_local_artifacts",
            "account_currency": bundle.account_currency,
            "required_range": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "discovery": discovery,
            },
            "plan": plan,
            "contracts": contract_rows,
        }

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise RuntimeError(
            "MetaTrader5 is not installed. Run: pip install MetaTrader5"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    initialized = False
    series: dict[str, Any] = {}
    failures: dict[str, str] = {}
    mapping = local_conversion_map(local)

    try:
        initialized = bool(
            mt5.initialize(**initialize_kwargs(args, local))
        )
        if not initialized:
            raise RuntimeError(
                f"MT5 initialize failed: {mt5.last_error()}"
            )

        timeframe = getattr(
            mt5,
            str(timeframe_spec["mt5_name"]),
        )

        for canonical in sorted(symbol_modes):
            try:
                broker_symbol, resolution = resolve_symbol(
                    mt5,
                    canonical,
                    mapping,
                )
                print(
                    f"Fetching {canonical} as {broker_symbol} "
                    f"({args.timeframe})..."
                )
                frame, metadata = fetch_series(
                    mt5,
                    symbol=broker_symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    chunk_days=effective_chunk_days,
                    modes=symbol_modes[canonical],
                )
                findings = validate_coverage(
                    metadata,
                    start=start,
                    end=end,
                    tolerance_days=args.coverage_tolerance_days,
                    max_gap_seconds=args.max_gap_seconds,
                )

                path = output_dir / f"{canonical}.csv"
                frame.to_csv(
                    path,
                    index=False,
                    date_format="%Y-%m-%dT%H:%M:%S.%fZ",
                )
                series[canonical] = {
                    "canonical_symbol": canonical,
                    "symbol_resolution": resolution,
                    "conversion_modes": sorted(symbol_modes[canonical]),
                    "path": path.name,
                    "timeframe": args.timeframe,
                    "timeframe_seconds": int(timeframe_spec["seconds"]),
                    "timestamp_semantics": "MT5_bar_open_time_UTC",
                    "causality_policy": "latest_at_or_before_entry",
                    "status": "PASS" if not findings else "FAIL",
                    "findings": findings,
                    **metadata,
                }
                print(
                    f"  {canonical}: {len(frame):,} rows, "
                    f"status={series[canonical]['status']}"
                )
            except Exception as exc:
                failures[canonical] = (
                    f"{type(exc).__name__}: {exc}"
                )
                print(f"  {canonical}: FAIL — {failures[canonical]}")
    finally:
        if initialized:
            mt5.shutdown()

    failed = sorted(
        set(failures)
        | {
            symbol
            for symbol, row in series.items()
            if row["status"] != "PASS"
        }
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "artifact_policy": "hashless_overwriteable_local_artifacts",
        "purpose": (
            "Causal historical profit-currency to USD-account conversion "
            "series for allocator risk."
        ),
        "account_currency": bundle.account_currency,
        "profile_path": str(profile_path),
        "timeframe": args.timeframe,
        "timeframe_seconds": int(timeframe_spec["seconds"]),
        "chunk_days": effective_chunk_days,
        "sampling_rationale": (
            "H1 is the default because currency conversion is used only for "
            "account-currency risk translation, not signal generation. "
            "Backward H1 lookup is sufficiently precise while remaining small."
        ),
        "price_policy": (
            "Adverse loss-side conversion: direct symbols use ask-open; "
            "inverse symbols use bid-open; lookups are backward-only."
        ),
        "required_range": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "start_buffer_days": args.start_buffer_days,
            "end_buffer_days": args.end_buffer_days,
            "discovery": discovery,
        },
        "download_plan": plan,
        "validation_policy": {
            "coverage_tolerance_days": args.coverage_tolerance_days,
            "max_gap_seconds": args.max_gap_seconds,
            "max_staleness_seconds_for_allocator_lookup": (
                args.max_staleness_seconds
            ),
        },
        "contracts": contract_rows,
        "series": series,
        "failures": failures,
        "summary": {
            "required_series_count": len(symbol_modes),
            "written_series_count": len(series),
            "failed_series_count": len(failed),
            "failed_series": failed,
            "total_rows_written": int(
                sum(
                    int(row.get("row_count", 0))
                    for row in series.values()
                )
            ),
            "status": "PASS" if not failed else "FAIL",
        },
        "next_step": (
            "Review this manifest, then integrate it into allocator Step 04B."
        ),
    }
    write_json(manifest_path, manifest)
    return manifest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Build read-only, causal, hashless MT5 historical conversion "
            "artifacts. H1 is the normal allocator timeframe; M1 remains "
            "available only as an explicit diagnostic override."
        )
    )
    p.add_argument("--profile", default=str(PROFILE_PATH))
    p.add_argument("--local-settings", default=str(LOCAL_SETTINGS_PATH))
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    p.add_argument("--manifest", default=str(MANIFEST_PATH))
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--start-buffer-days", type=int, default=3)
    p.add_argument("--end-buffer-days", type=int, default=3)
    p.add_argument(
        "--timeframe",
        choices=sorted(TIMEFRAME_SPECS),
        default=DEFAULT_TIMEFRAME,
        help=(
            "Historical conversion sampling timeframe. Default: H1. "
            "M1 is optional diagnostic mode and is not needed normally."
        ),
    )
    p.add_argument(
        "--chunk-days",
        type=int,
        default=None,
        help=(
            "Days requested per MT5 history call. Defaults by timeframe "
            "(H1 uses 180 days; M1 uses 30 days)."
        ),
    )
    p.add_argument(
        "--coverage-tolerance-days",
        type=int,
        default=5,
    )
    p.add_argument(
        "--max-gap-seconds",
        type=int,
        default=432000,
        help="Maximum allowed historical gap. Default: five days.",
    )
    p.add_argument(
        "--max-staleness-seconds",
        type=int,
        default=432000,
        help=(
            "Maximum age later accepted by allocator backward lookup. "
            "Default: five days for weekends and major market holidays."
        ),
    )
    p.add_argument("--login", type=int)
    p.add_argument("--password")
    p.add_argument("--server")
    p.add_argument("--terminal-path")
    p.add_argument("--timeout-ms", type=int)
    p.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "Print the discovered range, required symbols, timeframe, and "
            "row upper bound without connecting to MT5 or writing CSV files."
        ),
    )
    p.add_argument("--strict", action="store_true")
    p.add_argument("--print-json", action="store_true")
    return p


def main() -> None:
    args = parser().parse_args()
    manifest = build(args)

    if args.plan_only:
        print("Status: PLAN_ONLY")
        if args.print_json:
            print(
                json.dumps(
                    manifest,
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
        return

    summary = manifest["summary"]
    print(f"Status: {summary['status']}")
    print(
        f"Written: {summary['written_series_count']}/"
        f"{summary['required_series_count']}"
    )
    print(f"Total rows: {summary['total_rows_written']:,}")
    print(
        "Manifest: "
        f"{Path(args.manifest).expanduser().resolve()}"
    )

    for symbol in summary["failed_series"]:
        detail = (
            manifest["failures"].get(symbol)
            or "|".join(
                manifest["series"]
                .get(symbol, {})
                .get("findings", [])
            )
        )
        print(f"FAIL {symbol}: {detail}")

    if args.print_json:
        print(
            json.dumps(
                manifest,
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    if args.strict and summary["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
