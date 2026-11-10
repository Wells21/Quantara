"""
Build asset-level V2 FFD features from verified standard_quote_tick bars.

FFD is shared asset-level infrastructure. Edges may consume FFD features only
after this shared runtime has produced a verified artifact. The dashboard must
not create FFD features.

Run from trading_models_v2:

    python ./shared/ffd/build_asset_ffd_features.py --asset XAUUSD --dry-run
    python ./shared/ffd/build_asset_ffd_features.py --asset XAUUSD --overwrite
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.bars.verify_asset_bar_manifest import verify_asset_bar_manifest  # noqa: E402
from shared.ffd.fractional_differentiation import (  # noqa: E402
    DEFAULT_D_ESTIMATION_BARS,
    DEFAULT_FFD_WINDOW,
    DEFAULT_LIVE_BUFFER_MARGIN,
    DEFAULT_MIN_CORR_THRESHOLD,
    FFDConfig,
    FFDContractError,
    run_ffd_pipeline,
    write_ffd_outputs,
)
from shared.io.artifact_hash import hash_artifact  # noqa: E402


class AssetFFDBuildError(RuntimeError):
    """Raised when an asset FFD artifact build must fail closed."""


def _relative_to_v2(path: Path) -> str:
    try:
        return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _ffd_paths(asset: str) -> dict[str, Path]:
    layout = get_asset_layout(asset)
    root = layout.bars_root / "ffd"
    asset_lower = asset.lower()
    return {
        "root": root,
        "features_csv": root / f"{asset_lower}_ffd_features.csv",
        "state_bundle": root / f"{asset_lower}_ffd_state_bundle.pkl",
        "manifest": root / "ffd_manifest.json",
    }


def _check_safe_to_write(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"- {path}" for path in existing)
        raise AssetFFDBuildError(
            "FFD output file(s) already exist. Use --overwrite only if replacing intentionally:\n"
            f"{formatted}"
        )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def build_asset_ffd_features(
    *,
    asset: str,
    d_estimation_bars: int = DEFAULT_D_ESTIMATION_BARS,
    ffd_window: int = DEFAULT_FFD_WINDOW,
    min_corr_threshold: float = DEFAULT_MIN_CORR_THRESHOLD,
    live_buffer_margin: int = DEFAULT_LIVE_BUFFER_MARGIN,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    started = time.time()
    asset_clean = asset.strip().upper()
    paths = _ffd_paths(asset_clean)
    config = FFDConfig(
        d_estimation_bars=d_estimation_bars,
        ffd_window=ffd_window,
        min_corr_threshold=min_corr_threshold,
        live_buffer_margin=live_buffer_margin,
    )
    config.validate()

    bar_verification = verify_asset_bar_manifest(asset_clean)
    bars_path = _V2_ROOT / bar_verification["bars"]

    planned_outputs = [paths["features_csv"], paths["state_bundle"], paths["manifest"]]
    if not dry_run:
        _check_safe_to_write(planned_outputs, overwrite=overwrite)

    if dry_run:
        return {
            "status": "DRY_RUN_PASS",
            "asset": asset_clean,
            "source_bars": _relative_to_v2(bars_path),
            "planned_ffd_features": _relative_to_v2(paths["features_csv"]),
            "planned_state_bundle": _relative_to_v2(paths["state_bundle"]),
            "planned_manifest": _relative_to_v2(paths["manifest"]),
            "d_estimation_bars": d_estimation_bars,
            "ffd_window": ffd_window,
            "live_buffer_bars": config.live_buffer_bars,
        }

    bars = pd.read_csv(bars_path)
    ffd_bars, state_bundle, manifest = run_ffd_pipeline(
        bars,
        asset=asset_clean,
        source_bars_path=_relative_to_v2(bars_path),
        config=config,
    )

    manifest.update(
        {
            "outputs": {
                "ffd_features": _relative_to_v2(paths["features_csv"]),
                "state_bundle": _relative_to_v2(paths["state_bundle"]),
                "manifest": _relative_to_v2(paths["manifest"]),
            },
            "bar_verification": {
                "source_bar_contract_passed": bool(bar_verification["bar_contract_passed"]),
                "source_bar_hashes_passed": bool(bar_verification["artifact_hashes_passed"]),
                "source_bars_forged": bar_verification["bars_forged"],
                "source_engine": bar_verification["engine"],
            },
        }
    )

    write_ffd_outputs(
        ffd_bars=ffd_bars,
        output_csv=paths["features_csv"],
        live_bundle=state_bundle,
        live_bundle_path=paths["state_bundle"],
        manifest=manifest,
        manifest_path=paths["manifest"],
    )

    feature_hash = hash_artifact(paths["features_csv"], root=_V2_ROOT)
    bundle_hash = hash_artifact(paths["state_bundle"], root=_V2_ROOT)
    manifest["artifact_hashes"] = {
        "ffd_features": feature_hash.to_dict(),
        "state_bundle": bundle_hash.to_dict(),
    }
    manifest["elapsed_seconds"] = round(time.time() - started, 2)
    _write_json(paths["manifest"], manifest)

    return {
        "status": "PASS",
        "asset": asset_clean,
        "rows_input": int(manifest["rows_input"]),
        "rows_output": int(manifest["rows_output"]),
        "frozen_d": float(manifest["frozen_d"]),
        "ffd_window": int(manifest["ffd_window"]),
        "live_buffer_bars": int(manifest["live_buffer_bars"]),
        "memory_retention_passed": bool(manifest["memory_retention_passed"]),
        "min_memory_retention_corr": float(manifest["min_memory_retention_corr"]),
        "ffd_features": _relative_to_v2(paths["features_csv"]),
        "state_bundle": _relative_to_v2(paths["state_bundle"]),
        "manifest": _relative_to_v2(paths["manifest"]),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build V2 asset-level frozen FFD features.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--d-estimation-bars", type=int, default=DEFAULT_D_ESTIMATION_BARS)
    parser.add_argument("--ffd-window", type=int, default=DEFAULT_FFD_WINDOW)
    parser.add_argument("--min-corr-threshold", type=float, default=DEFAULT_MIN_CORR_THRESHOLD)
    parser.add_argument("--live-buffer-margin", type=int, default=DEFAULT_LIVE_BUFFER_MARGIN)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = build_asset_ffd_features(
        asset=args.asset,
        d_estimation_bars=args.d_estimation_bars,
        ffd_window=args.ffd_window,
        min_corr_threshold=args.min_corr_threshold,
        live_buffer_margin=args.live_buffer_margin,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    if result["status"] == "DRY_RUN_PASS":
        print("✅ V2 asset FFD dry-run passed.")
        print(f"Asset: {result['asset']}")
        print(f"Source bars: {result['source_bars']}")
        print(f"Planned FFD features: {result['planned_ffd_features']}")
        print(f"Planned state bundle: {result['planned_state_bundle']}")
        print(f"Planned manifest: {result['planned_manifest']}")
        print(f"d estimation bars: {result['d_estimation_bars']}")
        print(f"FFD window: {result['ffd_window']}")
        print(f"Live buffer bars: {result['live_buffer_bars']}")
        return

    print("✅ V2 asset FFD features built.")
    print(f"Asset: {result['asset']}")
    print(f"Rows input: {result['rows_input']}")
    print(f"Rows output: {result['rows_output']}")
    print(f"Frozen d: {result['frozen_d']}")
    print(f"FFD window: {result['ffd_window']}")
    print(f"Live buffer bars: {result['live_buffer_bars']}")
    print(f"Memory retention passed: {result['memory_retention_passed']}")
    print(f"Min memory retention corr: {result['min_memory_retention_corr']}")
    print(f"FFD features: {result['ffd_features']}")
    print(f"State bundle: {result['state_bundle']}")
    print(f"Manifest: {result['manifest']}")
    print(f"Elapsed seconds: {result['elapsed_seconds']}")


if __name__ == "__main__":
    try:
        _run()
    except (AssetFFDBuildError, FFDContractError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
