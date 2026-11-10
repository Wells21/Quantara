"""
Build asset-level V2 HMM regime features from verified FFD features.

HMM/regime is shared asset-level infrastructure. Edges may consume regime
features only after this shared runtime has produced a verified artifact. The
dashboard and Colab must not create regime features.

Run from trading_models_v2:

    python ./shared/regime/build_asset_hmm_regime_features.py --asset XAUUSD --dry-run
    python ./shared/regime/build_asset_hmm_regime_features.py --asset XAUUSD --overwrite
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.ffd.verify_asset_ffd_manifest import verify_asset_ffd_manifest  # noqa: E402
from shared.io.artifact_hash import hash_artifact  # noqa: E402
from shared.regime.hmm_regime import (  # noqa: E402
    HMMRegimeConfig,
    HMMRegimeContractError,
    run_hmm_regime_pipeline,
)


class AssetHMMRegimeBuildError(RuntimeError):
    """Raised when an asset HMM artifact build must fail closed."""


def _relative_to_v2(path: Path) -> str:
    try:
        return path.resolve().relative_to(_V2_ROOT.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _regime_paths(asset: str) -> dict[str, Path]:
    layout = get_asset_layout(asset)
    root = layout.bars_root / "regime"
    asset_lower = asset.lower()
    return {
        "root": root,
        "features_csv": root / f"{asset_lower}_hmm_regime_features.csv",
        "live_bundle": root / f"{asset_lower}_hmm_regime_live_bundle.pkl",
        "manifest": root / "hmm_regime_manifest.json",
    }


def _check_safe_to_write(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"- {path}" for path in existing)
        raise AssetHMMRegimeBuildError(
            "HMM regime output file(s) already exist. Use --overwrite only if replacing intentionally:\n"
            f"{formatted}"
        )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def build_asset_hmm_regime_features(
    *,
    asset: str,
    n_states: int = 4,
    train_window: int = 5000,
    refit_frequency: int = 1000,
    inference_lookback: int = 1000,
    max_iter: int = 200,
    min_covar: float = 0.01,
    random_state: int = 42,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    started = time.time()
    asset_clean = asset.strip().upper()
    paths = _regime_paths(asset_clean)
    config = HMMRegimeConfig(
        n_states=n_states,
        train_window=train_window,
        refit_frequency=refit_frequency,
        inference_lookback=inference_lookback,
        max_iter=max_iter,
        min_covar=min_covar,
        random_state=random_state,
    )
    config.validate()

    ffd_verification = verify_asset_ffd_manifest(asset_clean)
    source_features_path = _V2_ROOT / ffd_verification["features"]
    planned_outputs = [paths["features_csv"], paths["live_bundle"], paths["manifest"]]
    if not dry_run:
        _check_safe_to_write(planned_outputs, overwrite=overwrite)

    if dry_run:
        return {
            "status": "DRY_RUN_PASS",
            "asset": asset_clean,
            "source_ffd_features": _relative_to_v2(source_features_path),
            "planned_regime_features": _relative_to_v2(paths["features_csv"]),
            "planned_live_bundle": _relative_to_v2(paths["live_bundle"]),
            "planned_manifest": _relative_to_v2(paths["manifest"]),
            "n_states": n_states,
            "train_window": train_window,
            "refit_frequency": refit_frequency,
            "inference_lookback": inference_lookback,
        }

    ffd_features = pd.read_csv(source_features_path)
    regime_features, live_bundle, manifest = run_hmm_regime_pipeline(
        ffd_features,
        asset=asset_clean,
        source_features_path=_relative_to_v2(source_features_path),
        config=config,
    )

    manifest.update(
        {
            "status": "asset_hmm_regime_features_built_not_edge_deployable",
            "outputs": {
                "regime_features": _relative_to_v2(paths["features_csv"]),
                "live_bundle": _relative_to_v2(paths["live_bundle"]),
                "manifest": _relative_to_v2(paths["manifest"]),
            },
            "ffd_verification": {
                "source_ffd_contract_passed": bool(ffd_verification["ffd_contract_passed"]),
                "source_ffd_hashes_passed": bool(ffd_verification["artifact_hashes_passed"]),
                "source_rows_output": ffd_verification["rows_output"],
                "source_frozen_d": ffd_verification["frozen_d"],
            },
        }
    )

    paths["root"].mkdir(parents=True, exist_ok=True)
    regime_features.to_csv(paths["features_csv"], index=False)
    joblib.dump(live_bundle, paths["live_bundle"])

    feature_hash = hash_artifact(paths["features_csv"], root=_V2_ROOT)
    bundle_hash = hash_artifact(paths["live_bundle"], root=_V2_ROOT)
    manifest["artifact_hashes"] = {
        "regime_features": feature_hash.to_dict(),
        "live_bundle": bundle_hash.to_dict(),
    }
    manifest["elapsed_seconds"] = round(time.time() - started, 2)
    _write_json(paths["manifest"], manifest)

    return {
        "status": "PASS",
        "asset": asset_clean,
        "rows_input_after_preprocessing": int(manifest["rows_input_after_preprocessing"]),
        "rows_output": int(manifest["rows_output"]),
        "n_states": int(manifest["n_states"]),
        "train_window": int(manifest["train_window"]),
        "refit_frequency": int(manifest["refit_frequency"]),
        "inference_lookback": int(manifest["inference_lookback"]),
        "refit_points_count": int(manifest["refit_points_count"]),
        "all_refits_converged": bool(manifest["all_refits_converged"]),
        "start_output": manifest["start_output"],
        "end_output": manifest["end_output"],
        "regime_features": _relative_to_v2(paths["features_csv"]),
        "live_bundle": _relative_to_v2(paths["live_bundle"]),
        "manifest": _relative_to_v2(paths["manifest"]),
        "elapsed_seconds": round(time.time() - started, 2),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build V2 asset-level HMM regime features.")
    parser.add_argument("--asset", required=True)
    parser.add_argument("--n-states", type=int, default=4)
    parser.add_argument("--train-window", type=int, default=5000)
    parser.add_argument("--refit-frequency", type=int, default=1000)
    parser.add_argument("--inference-lookback", type=int, default=1000)
    parser.add_argument("--max-iter", type=int, default=200)
    parser.add_argument("--min-covar", type=float, default=0.01)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = build_asset_hmm_regime_features(
        asset=args.asset,
        n_states=args.n_states,
        train_window=args.train_window,
        refit_frequency=args.refit_frequency,
        inference_lookback=args.inference_lookback,
        max_iter=args.max_iter,
        min_covar=args.min_covar,
        random_state=args.random_state,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    if result["status"] == "DRY_RUN_PASS":
        print("✅ V2 asset HMM regime dry-run passed.")
        print(f"Asset: {result['asset']}")
        print(f"Source FFD features: {result['source_ffd_features']}")
        print(f"Planned regime features: {result['planned_regime_features']}")
        print(f"Planned live bundle: {result['planned_live_bundle']}")
        print(f"Planned manifest: {result['planned_manifest']}")
        print(f"States: {result['n_states']}")
        print(f"Train window: {result['train_window']}")
        print(f"Refit frequency: {result['refit_frequency']}")
        print(f"Inference lookback: {result['inference_lookback']}")
        return

    print("✅ V2 asset HMM regime features built.")
    print(f"Asset: {result['asset']}")
    print(f"Rows after preprocessing: {result['rows_input_after_preprocessing']}")
    print(f"Rows output: {result['rows_output']}")
    print(f"States: {result['n_states']}")
    print(f"Train window: {result['train_window']}")
    print(f"Refit frequency: {result['refit_frequency']}")
    print(f"Inference lookback: {result['inference_lookback']}")
    print(f"Refit points: {result['refit_points_count']}")
    print(f"All refits converged: {result['all_refits_converged']}")
    print(f"Output range: {result['start_output']} -> {result['end_output']}")
    print(f"Regime features: {result['regime_features']}")
    print(f"Live bundle: {result['live_bundle']}")
    print(f"Manifest: {result['manifest']}")
    print(f"Elapsed seconds: {result['elapsed_seconds']}")


if __name__ == "__main__":
    try:
        _run()
    except (AssetHMMRegimeBuildError, HMMRegimeContractError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"❌ {exc}") from exc
