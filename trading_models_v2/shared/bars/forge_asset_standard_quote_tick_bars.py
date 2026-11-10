"""
Forge V2 asset-level standard quote-tick bars.

Blueprint purpose
-----------------
Bars belong to the asset, not to an edge.

This CLI forges shared asset bars from live-compatible quote tick CSVs:

    assets/{ASSET}/raw/ticks/
        -> assets/{ASSET}/bars/standard_quote_tick/

Edges under the asset then consume those shared bars through their own runtime.py.
The dashboard must not forge edge-specific bars or manually engineer edge features.

Inputs
------
Raw quote tick CSV files under:

    assets/{ASSET}/raw/ticks/

Accepted tick columns are normalized by the shared builder:

    timestamp/time/datetime/date_time
    bid/bidprice/bid_price
    ask/askprice/ask_price

Outputs
-------
By default, this writes CSV to avoid requiring pyarrow/fastparquet during the
foundation phase:

    assets/{ASSET}/bars/standard_quote_tick/{asset}_standard_quote_tick_bars.csv
    assets/{ASSET}/bars/standard_quote_tick/bar_manifest.json
    assets/{ASSET}/bars/standard_quote_tick/daily_tick_profile.csv
    assets/{ASSET}/bars/standard_quote_tick/daily_thresholds.csv

Performance engine
------------------
Default engine is ``streaming``:

    single chronological pass, causal thresholds from prior completed days only

Fallback engine is ``two_pass``:

    pass 1 daily profile, pass 2 forge bars

Run from ``trading_models_v2``:

    python ./shared/bars/forge_asset_standard_quote_tick_bars.py --asset XAUUSD --overwrite

For a dry contract/config check without forging:

    python ./shared/bars/forge_asset_standard_quote_tick_bars.py --asset XAUUSD --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import AssetLayout, get_asset_layout  # noqa: E402
from shared.bars.bar_contract_validator import validate_bar_table_file  # noqa: E402
from shared.bars.standard_quote_tick_builder import (  # noqa: E402
    DEFAULT_CHUNKSIZE,
    DEFAULT_DAILY_TICK_EWMA_SPAN_DAYS,
    DEFAULT_MIN_WARMUP_DAYS,
    DEFAULT_TARGET_BARS_PER_DAY,
    CausalThresholdConfig,
    build_causal_daily_tick_thresholds,
    forge_standard_quote_tick_bars_from_csv_folder,
    get_sorted_csv_files,
    profile_daily_tick_counts_from_csv_folder,
)
from shared.bars.streaming_standard_quote_tick_builder import (  # noqa: E402
    StreamingBarForgeError,
    forge_standard_quote_tick_bars_single_pass_from_csv_folder,
)
from shared.contracts import (  # noqa: E402
    BAR_MODE_STANDARD_QUOTE_TICK,
    THRESHOLD_UNIT_TICKS,
    V2_CONTRACT_VERSION,
)
from shared.io.artifact_hash import hash_artifact  # noqa: E402


class AssetBarForgeError(RuntimeError):
    """Raised when asset-level bar forging must fail closed."""


def _relative_to_v2(path: Path) -> str:
    try:
        return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _output_path(layout: AssetLayout, output_format: str) -> Path:
    if output_format == "csv":
        return layout.standard_quote_tick_bars_csv
    if output_format == "parquet":
        return layout.standard_quote_tick_bars_parquet
    raise AssetBarForgeError(f"Unsupported output_format={output_format!r}")


def _daily_thresholds_frame(thresholds_by_date: dict[pd.Timestamp, int]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": pd.Timestamp(date).date().isoformat(), "threshold_ticks": int(threshold)}
            for date, threshold in sorted(thresholds_by_date.items())
        ]
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _check_safe_to_write(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"- {path}" for path in existing)
        raise AssetBarForgeError(
            "Output file(s) already exist. Use --overwrite only if you intentionally want to replace them:\n"
            f"{formatted}"
        )


def _run_two_pass_engine(
    layout: AssetLayout,
    out_path: Path,
    config: CausalThresholdConfig,
    chunksize: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[pd.Timestamp, int], dict[str, Any], dict[str, Any], dict[str, Any]]:
    daily_profile, profile_stats = profile_daily_tick_counts_from_csv_folder(
        layout.tick_root,
        chunksize=chunksize,
    )
    thresholds_by_date, threshold_meta = build_causal_daily_tick_thresholds(daily_profile, config=config)

    bars_df, forge_stats = forge_standard_quote_tick_bars_from_csv_folder(
        layout.tick_root,
        output_path=out_path,
        thresholds_by_date=thresholds_by_date,
        chunksize=chunksize,
        source=f"{layout.asset}_raw_ticks",
    )
    threshold_meta["engine"] = "two_pass"
    profile_stats["engine"] = "two_pass"
    forge_stats["engine"] = "two_pass"
    return bars_df, daily_profile, thresholds_by_date, threshold_meta, profile_stats, forge_stats


def _run_streaming_engine(
    layout: AssetLayout,
    out_path: Path,
    config: CausalThresholdConfig,
    chunksize: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[pd.Timestamp, int], dict[str, Any], dict[str, Any], dict[str, Any]]:
    result = forge_standard_quote_tick_bars_single_pass_from_csv_folder(
        layout.tick_root,
        config=config,
        output_path=out_path,
        chunksize=chunksize,
        source=f"{layout.asset}_raw_ticks",
    )
    return (
        result.bars,
        result.daily_profile,
        result.thresholds_by_date,
        result.threshold_meta,
        result.profile_stats,
        result.forge_stats,
    )


def forge_asset_standard_quote_tick_bars(
    *,
    asset: str,
    output_format: str = "csv",
    target_bars_per_day: int = DEFAULT_TARGET_BARS_PER_DAY,
    min_warmup_days: int = DEFAULT_MIN_WARMUP_DAYS,
    daily_tick_ewma_span_days: int = DEFAULT_DAILY_TICK_EWMA_SPAN_DAYS,
    chunksize: int = DEFAULT_CHUNKSIZE,
    overwrite: bool = False,
    dry_run: bool = False,
    engine: str = "streaming",
) -> dict[str, Any]:
    """Forge shared standard_quote_tick bars for one asset.

    This function is intentionally asset-generic. It does not know about any
    specific edge family or side.
    """

    started = time.time()
    layout = get_asset_layout(asset)
    layout.create_directories()

    csv_files = get_sorted_csv_files(layout.tick_root)
    out_path = _output_path(layout, output_format)
    daily_profile_path = layout.standard_quote_tick_root / "daily_tick_profile.csv"
    thresholds_path = layout.standard_quote_tick_root / "daily_thresholds.csv"
    manifest_path = layout.bar_manifest_json

    if engine not in {"streaming", "two_pass"}:
        raise AssetBarForgeError(f"Unsupported engine={engine!r}. Use 'streaming' or 'two_pass'.")

    planned_outputs = [out_path, daily_profile_path, thresholds_path, manifest_path]
    if not dry_run:
        _check_safe_to_write(planned_outputs, overwrite=overwrite)

    config = CausalThresholdConfig(
        target_bars_per_day=target_bars_per_day,
        min_warmup_days=min_warmup_days,
        daily_tick_ewma_span_days=daily_tick_ewma_span_days,
    )
    config.validate()

    if dry_run:
        return {
            "status": "DRY_RUN_PASS",
            "asset": layout.asset,
            "tick_folder": _relative_to_v2(layout.tick_root),
            "tick_csv_files_found": len(csv_files),
            "bar_mode": BAR_MODE_STANDARD_QUOTE_TICK,
            "threshold_unit": THRESHOLD_UNIT_TICKS,
            "planned_bar_output": _relative_to_v2(out_path),
            "planned_manifest": _relative_to_v2(manifest_path),
            "target_bars_per_day": target_bars_per_day,
            "min_warmup_days": min_warmup_days,
            "daily_tick_ewma_span_days": daily_tick_ewma_span_days,
            "chunksize": chunksize,
            "engine": engine,
        }

    if engine == "streaming":
        bars_df, daily_profile, thresholds_by_date, threshold_meta, profile_stats, forge_stats = _run_streaming_engine(
            layout=layout,
            out_path=out_path,
            config=config,
            chunksize=chunksize,
        )
    else:
        bars_df, daily_profile, thresholds_by_date, threshold_meta, profile_stats, forge_stats = _run_two_pass_engine(
            layout=layout,
            out_path=out_path,
            config=config,
            chunksize=chunksize,
        )

    daily_profile.to_csv(daily_profile_path, index=False)
    thresholds_df = _daily_thresholds_frame(thresholds_by_date)
    thresholds_df.to_csv(thresholds_path, index=False)

    validation_report = validate_bar_table_file(out_path)
    if validation_report.failed:
        raise AssetBarForgeError("Forged bar output failed validation: " + "; ".join(validation_report.fail_messages()))

    bar_hash = hash_artifact(out_path, root=_V2_ROOT)
    profile_hash = hash_artifact(daily_profile_path, root=_V2_ROOT)
    thresholds_hash = hash_artifact(thresholds_path, root=_V2_ROOT)

    manifest: dict[str, Any] = {
        "schema_version": V2_CONTRACT_VERSION,
        "asset": layout.asset,
        "bar_mode": BAR_MODE_STANDARD_QUOTE_TICK,
        "threshold_unit": THRESHOLD_UNIT_TICKS,
        "proxy_value_definition": "Proxy_Value equals Ticks for backward compatibility only.",
        "bar_scope": "asset_level_shared_bars",
        "source_tick_folder": _relative_to_v2(layout.tick_root),
        "source_tick_csv_files": [_relative_to_v2(path) for path in csv_files],
        "outputs": {
            "bars": _relative_to_v2(out_path),
            "daily_tick_profile": _relative_to_v2(daily_profile_path),
            "daily_thresholds": _relative_to_v2(thresholds_path),
            "manifest": _relative_to_v2(manifest_path),
        },
        "config": {
            "target_bars_per_day": target_bars_per_day,
            "min_warmup_days": min_warmup_days,
            "daily_tick_ewma_span_days": daily_tick_ewma_span_days,
            "chunksize": chunksize,
            "output_format": output_format,
            "engine": engine,
        },
        "profile_stats": profile_stats,
        "threshold_meta": threshold_meta,
        "forge_stats": forge_stats,
        "validation": {
            "bar_contract_passed": validation_report.passed,
            "findings": [
                {
                    "severity": finding.severity.value,
                    "code": finding.code,
                    "message": finding.message,
                }
                for finding in validation_report.findings
            ],
        },
        "artifact_hashes": {
            "bars": bar_hash.to_dict(),
            "daily_tick_profile": profile_hash.to_dict(),
            "daily_thresholds": thresholds_hash.to_dict(),
        },
        "research_live_compatibility": {
            "uses_quote_ticks_only": True,
            "required_tick_fields": ["timestamp", "bid", "ask"],
            "price_source": "mid_price=(bid+ask)/2",
            "uses_volume": False,
            "uses_future_data_for_thresholds": False,
            "edge_specific_bars": False,
            "dashboard_may_engineer_edge_features": False,
        },
        "elapsed_seconds": round(time.time() - started, 2),
    }
    _write_json(manifest_path, manifest)

    manifest_hash = hash_artifact(manifest_path, root=_V2_ROOT)
    manifest["artifact_hashes"]["manifest_written_sha256"] = manifest_hash.to_dict()
    _write_json(manifest_path, manifest)

    return {
        "status": "PASS",
        "asset": layout.asset,
        "bars_forged": int(len(bars_df)),
        "bar_output": _relative_to_v2(out_path),
        "manifest": _relative_to_v2(manifest_path),
        "daily_profile": _relative_to_v2(daily_profile_path),
        "daily_thresholds": _relative_to_v2(thresholds_path),
        "start": str(bars_df["Datetime"].min()),
        "end": str(bars_df["Datetime"].max()),
        "median_ticks_per_bar": float(bars_df["Ticks"].median()),
        "mean_ticks_per_bar": float(bars_df["Ticks"].mean()),
        "elapsed_seconds": round(time.time() - started, 2),
        "engine": engine,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forge V2 asset-level standard_quote_tick bars.")
    parser.add_argument("--asset", required=True, help="Asset name, e.g. XAUUSD, BTCUSD, USTECH.")
    parser.add_argument(
        "--output-format",
        choices=["csv", "parquet"],
        default="csv",
        help="Bar output format. Defaults to csv to avoid requiring parquet dependencies during foundation.",
    )
    parser.add_argument(
        "--engine",
        choices=["streaming", "two_pass"],
        default="streaming",
        help="Forge engine. streaming is faster single-pass causal mode; two_pass is slower fallback/verification mode.",
    )
    parser.add_argument("--target-bars-per-day", type=int, default=DEFAULT_TARGET_BARS_PER_DAY)
    parser.add_argument("--min-warmup-days", type=int, default=DEFAULT_MIN_WARMUP_DAYS)
    parser.add_argument("--daily-tick-ewma-span-days", type=int, default=DEFAULT_DAILY_TICK_EWMA_SPAN_DAYS)
    parser.add_argument("--chunksize", type=int, default=DEFAULT_CHUNKSIZE)
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")
    parser.add_argument("--dry-run", action="store_true", help="Check paths/config without forging bars.")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = forge_asset_standard_quote_tick_bars(
        asset=args.asset,
        output_format=args.output_format,
        target_bars_per_day=args.target_bars_per_day,
        min_warmup_days=args.min_warmup_days,
        daily_tick_ewma_span_days=args.daily_tick_ewma_span_days,
        chunksize=args.chunksize,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        engine=args.engine,
    )

    if result["status"] == "DRY_RUN_PASS":
        print("✅ V2 asset bar forge dry-run passed.")
        print(f"Asset: {result['asset']}")
        print(f"Engine: {result['engine']}")
        print(f"Tick CSV files found: {result['tick_csv_files_found']}")
        print(f"Planned bar output: {result['planned_bar_output']}")
        print(f"Planned manifest: {result['planned_manifest']}")
        return

    print("✅ V2 asset standard_quote_tick bars forged.")
    print(f"Asset: {result['asset']}")
    print(f"Engine: {result['engine']}")
    print(f"Bars forged: {result['bars_forged']}")
    print(f"Start: {result['start']}")
    print(f"End: {result['end']}")
    print(f"Median ticks/bar: {result['median_ticks_per_bar']}")
    print(f"Mean ticks/bar: {result['mean_ticks_per_bar']}")
    print(f"Bar output: {result['bar_output']}")
    print(f"Manifest: {result['manifest']}")
    print(f"Daily profile: {result['daily_profile']}")
    print(f"Daily thresholds: {result['daily_thresholds']}")
    print(f"Elapsed seconds: {result['elapsed_seconds']}")


if __name__ == "__main__":
    try:
        _run()
    except (AssetBarForgeError, StreamingBarForgeError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
