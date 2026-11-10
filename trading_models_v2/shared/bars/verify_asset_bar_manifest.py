"""
Verify a V2 asset-level standard_quote_tick bar manifest.

Blueprint purpose
-----------------
Before any edge runtime consumes asset-level bars, the asset bar artifact must be
verified:

1. Manifest exists.
2. Manifest declares asset-level shared standard_quote_tick bars.
3. Manifest declares no future threshold leakage.
4. Manifest declares no dashboard-side edge feature engineering.
5. Manifest artifact hashes match local files.
6. The actual bar file satisfies the V2 bar contract.

Run from ``trading_models_v2``:

    python ./shared/bars/verify_asset_bar_manifest.py --asset XAUUSD
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import get_asset_layout  # noqa: E402
from shared.bars.bar_contract_validator import validate_bar_table_file  # noqa: E402
from shared.contracts import BAR_MODE_STANDARD_QUOTE_TICK, THRESHOLD_UNIT_TICKS, V2_CONTRACT_VERSION  # noqa: E402
from shared.io.artifact_hash import verify_artifact_hashes  # noqa: E402


class AssetBarManifestVerificationError(RuntimeError):
    """Raised when an asset bar manifest violates the V2 contract."""


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AssetBarManifestVerificationError(f"Missing bar manifest: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssetBarManifestVerificationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AssetBarManifestVerificationError("Bar manifest must be a JSON object.")
    return data


def _require_equal(manifest: Mapping[str, Any], key: str, expected: Any) -> None:
    actual = manifest.get(key)
    if actual != expected:
        raise AssetBarManifestVerificationError(f"Manifest key {key!r} expected {expected!r}, got {actual!r}")


def _require_nested_bool(manifest: Mapping[str, Any], section: str, key: str, expected: bool) -> None:
    obj = manifest.get(section)
    if not isinstance(obj, dict):
        raise AssetBarManifestVerificationError(f"Manifest section {section!r} must be an object.")
    actual = obj.get(key)
    if actual is not expected:
        raise AssetBarManifestVerificationError(
            f"Manifest {section}.{key} expected {expected!r}, got {actual!r}"
        )


def _manifest_hash_table(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    hashes = manifest.get("artifact_hashes")
    if not isinstance(hashes, dict):
        raise AssetBarManifestVerificationError("Manifest artifact_hashes must be an object.")

    # The manifest hash records the manifest after its first write and cannot be
    # verified against the final manifest after adding its own hash. Verify data
    # artifacts only here.
    required = ("bars", "daily_tick_profile", "daily_thresholds")
    table: dict[str, Mapping[str, Any]] = {}
    for key in required:
        record = hashes.get(key)
        if not isinstance(record, dict):
            raise AssetBarManifestVerificationError(f"Manifest artifact_hashes.{key} must be an object.")
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise AssetBarManifestVerificationError(f"Manifest artifact_hashes.{key}.path must be non-empty.")
        table[path] = record
    return table


def verify_asset_bar_manifest(asset: str) -> dict[str, Any]:
    layout = get_asset_layout(asset)
    manifest = _load_manifest(layout.bar_manifest_json)

    _require_equal(manifest, "schema_version", V2_CONTRACT_VERSION)
    _require_equal(manifest, "asset", layout.asset)
    _require_equal(manifest, "bar_mode", BAR_MODE_STANDARD_QUOTE_TICK)
    _require_equal(manifest, "threshold_unit", THRESHOLD_UNIT_TICKS)
    _require_equal(manifest, "bar_scope", "asset_level_shared_bars")
    _require_equal(manifest, "proxy_value_definition", "Proxy_Value equals Ticks for backward compatibility only.")

    _require_nested_bool(manifest, "research_live_compatibility", "uses_quote_ticks_only", True)
    _require_nested_bool(manifest, "research_live_compatibility", "uses_volume", False)
    _require_nested_bool(manifest, "research_live_compatibility", "uses_future_data_for_thresholds", False)
    _require_nested_bool(manifest, "research_live_compatibility", "edge_specific_bars", False)
    _require_nested_bool(manifest, "research_live_compatibility", "dashboard_may_engineer_edge_features", False)

    threshold_meta = manifest.get("threshold_meta")
    if not isinstance(threshold_meta, dict):
        raise AssetBarManifestVerificationError("Manifest threshold_meta must be an object.")
    if threshold_meta.get("uses_future_data") is not False:
        raise AssetBarManifestVerificationError("threshold_meta.uses_future_data must be false.")
    if threshold_meta.get("warmup_days_are_emitted") is not False:
        raise AssetBarManifestVerificationError("threshold_meta.warmup_days_are_emitted must be false.")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise AssetBarManifestVerificationError("Manifest outputs must be an object.")
    bars_rel = outputs.get("bars")
    if not isinstance(bars_rel, str) or not bars_rel:
        raise AssetBarManifestVerificationError("Manifest outputs.bars must be non-empty.")

    mismatches = verify_artifact_hashes(_manifest_hash_table(manifest), root=_V2_ROOT)
    if mismatches:
        raise AssetBarManifestVerificationError("Artifact hash verification failed:\n" + "\n".join(mismatches))

    bar_path = _V2_ROOT / bars_rel
    bar_report = validate_bar_table_file(bar_path)
    if bar_report.failed:
        raise AssetBarManifestVerificationError("Bar file contract validation failed: " + "; ".join(bar_report.fail_messages()))

    forge_stats = manifest.get("forge_stats", {})
    profile_stats = manifest.get("profile_stats", {})
    return {
        "asset": layout.asset,
        "bars": bars_rel,
        "bars_forged": forge_stats.get("bars_forged"),
        "start": forge_stats.get("start"),
        "end": forge_stats.get("end"),
        "engine": forge_stats.get("engine"),
        "trading_days": profile_stats.get("trading_days"),
        "bar_contract_passed": True,
        "artifact_hashes_passed": True,
        "manifest": layout.bar_manifest_json.as_posix(),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify V2 asset-level bar manifest and bar artifact.")
    parser.add_argument("--asset", required=True, help="Asset name, e.g. XAUUSD.")
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    result = verify_asset_bar_manifest(args.asset)

    print("✅ V2 asset bar manifest verification passed.")
    print(f"Asset: {result['asset']}")
    print(f"Engine: {result['engine']}")
    print(f"Bars forged: {result['bars_forged']}")
    print(f"Trading days: {result['trading_days']}")
    print(f"Start: {result['start']}")
    print(f"End: {result['end']}")
    print(f"Bars: {result['bars']}")
    print("Artifact hashes: PASS")
    print("Bar contract: PASS")


if __name__ == "__main__":
    try:
        _run()
    except AssetBarManifestVerificationError as exc:
        raise SystemExit(f"❌ {exc}") from exc
