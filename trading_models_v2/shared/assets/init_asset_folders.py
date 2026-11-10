"""
Initialize V2 asset-level folders.

Blueprint purpose
-----------------
This script creates the empty V2 asset foundation without touching V1 folders.
It enforces the scalable asset-level rule:

    assets/{ASSET}/raw/ticks/
    assets/{ASSET}/bars/standard_quote_tick/
    assets/{ASSET}/edges/

The folders are shared by all side-specialized edges under the same asset.
Bars are not edge-specific. Features/events/model rows are edge-specific.

Default starter assets come from the single V2 asset source of truth:

    config/asset_universe.json

Scalable usage examples
-----------------------
Run with assets from config/asset_universe.json:

    python ./shared/assets/init_asset_folders.py

Add/initialize any assets without editing this file:

    python ./shared/assets/init_asset_folders.py --assets XAUUSD BTCUSD USTECH EURUSD USDJPY US30 US500

or comma-separated:

    python ./shared/assets/init_asset_folders.py --assets XAUUSD,BTCUSD,USTECH,EURUSD
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.assets.asset_layout import AssetLayout, describe_layout, get_asset_layout  # noqa: E402
from shared.contracts import BAR_MODE_STANDARD_QUOTE_TICK, THRESHOLD_UNIT_TICKS  # noqa: E402


DEFAULT_ASSET_REGISTRY_PATH = _V2_ROOT / "config" / "asset_universe.json"
FALLBACK_ASSETS: tuple[str, ...] = ("XAUUSD", "BTCUSD", "USTECH")

README_TEMPLATE = """# {asset} V2 Asset Folder

This folder follows the V2 Blueprint asset-level contract.

## Ownership boundaries

- `raw/ticks/` contains this asset's raw quote tick files or local tick-file links.
- `bars/standard_quote_tick/` contains this asset's shared V2 standard quote-tick bars.
- `edges/` contains side-specialized edge packages such as `trend_long` and `trend_short`.

## Non-negotiable rule

Bars belong to the asset. Edge-specific features, candidate events, model rows,
models, calibrators, thresholds, and deployability manifests belong inside each
edge package.

The dashboard must not manually engineer edge-specific features.
"""


class AssetRegistryError(RuntimeError):
    """Raised when the scalable V2 asset registry is invalid."""


def load_assets_from_registry(registry_path: str | Path = DEFAULT_ASSET_REGISTRY_PATH) -> tuple[str, ...]:
    """Load enabled assets from config/asset_universe.json.

    Supported per-asset enable flags:
    - enabled: true/false
    - status: active, active_foundation, enabled, pilot, disabled

    If the registry does not exist, fallback starter assets are returned so the
    foundation remains runnable during early bootstrap.
    """

    path = Path(registry_path)
    if not path.exists():
        return FALLBACK_ASSETS

    data = json.loads(path.read_text(encoding="utf-8"))
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, list):
        raise AssetRegistryError("config/asset_universe.json must contain an 'assets' list.")

    assets: list[str] = []
    for idx, row in enumerate(raw_assets):
        if not isinstance(row, dict):
            raise AssetRegistryError(f"assets[{idx}] must be an object.")

        asset = str(row.get("asset", "")).strip().upper()
        if not asset:
            raise AssetRegistryError(f"assets[{idx}].asset must be non-empty.")

        bar_mode = str(row.get("bar_mode", BAR_MODE_STANDARD_QUOTE_TICK)).strip()
        if bar_mode != BAR_MODE_STANDARD_QUOTE_TICK:
            raise AssetRegistryError(
                f"assets[{idx}] {asset} has unsupported bar_mode={bar_mode!r}; "
                f"expected {BAR_MODE_STANDARD_QUOTE_TICK!r}."
            )

        threshold_unit = str(row.get("threshold_unit", THRESHOLD_UNIT_TICKS)).strip()
        if threshold_unit != THRESHOLD_UNIT_TICKS:
            raise AssetRegistryError(
                f"assets[{idx}] {asset} has unsupported threshold_unit={threshold_unit!r}; "
                f"expected {THRESHOLD_UNIT_TICKS!r}."
            )

        enabled = row.get("enabled")
        status = str(row.get("status", "active_foundation")).strip().lower()
        if enabled is False or status in {"disabled", "inactive", "off"}:
            continue

        assets.append(asset)

    if not assets:
        raise AssetRegistryError("config/asset_universe.json has no enabled assets.")

    return dedupe_assets(assets)


def dedupe_assets(assets: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for asset_raw in assets:
        asset = str(asset_raw).strip().upper()
        if not asset:
            continue
        if asset not in seen:
            deduped.append(asset)
            seen.add(asset)
    if not deduped:
        raise ValueError("No valid asset names supplied.")
    return tuple(deduped)


def parse_assets(raw_assets: list[str] | None) -> tuple[str, ...]:
    """Parse asset names from CLI args.

    Supports both:
    - --assets XAUUSD BTCUSD EURUSD
    - --assets XAUUSD,BTCUSD,EURUSD
    """

    if not raw_assets:
        return load_assets_from_registry()

    parsed: list[str] = []
    for item in raw_assets:
        parsed.extend(part.strip().upper() for part in item.split(",") if part.strip())

    return dedupe_assets(parsed)


def _touch_gitkeep(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    gitkeep = folder / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")


def _write_asset_readme(layout: AssetLayout) -> None:
    readme = layout.asset_root / "README.md"
    if not readme.exists():
        readme.write_text(README_TEMPLATE.format(asset=layout.asset), encoding="utf-8")


def initialize_asset(layout: AssetLayout) -> list[Path]:
    """Create the required V2 folders for one asset.

    Returns the directories that were ensured.
    """

    ensured: list[Path] = []
    for directory in layout.required_directories():
        directory.mkdir(parents=True, exist_ok=True)
        ensured.append(directory)

    _touch_gitkeep(layout.tick_root)
    _touch_gitkeep(layout.standard_quote_tick_root)
    _touch_gitkeep(layout.edges_root)
    _write_asset_readme(layout)
    return ensured


def initialize_assets(assets: tuple[str, ...] | None = None, v2_root: Path | None = None) -> list[AssetLayout]:
    selected_assets = assets if assets is not None else load_assets_from_registry()
    layouts: list[AssetLayout] = []
    for asset in selected_assets:
        layout = get_asset_layout(asset, v2_root=v2_root)
        initialize_asset(layout)
        layouts.append(layout)
    return layouts


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize scalable V2 asset folders.")
    parser.add_argument(
        "--assets",
        nargs="*",
        default=None,
        help=(
            "Asset names to initialize. Supports space-separated or comma-separated names. "
            "When omitted, enabled assets are read from config/asset_universe.json."
        ),
    )
    return parser


def _run(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    assets = parse_assets(args.assets)
    layouts = initialize_assets(assets=assets)

    print("✅ V2 asset folders initialized.")
    print("Assets:", ", ".join(layout.asset for layout in layouts))
    print("Rule: asset raw ticks -> asset shared standard_quote_tick bars -> side-specialized edges")
    print("Scalability: add assets in config/asset_universe.json or pass --assets; no dashboard/edit required.")

    for layout in layouts:
        print(f"\n[{layout.asset}]")
        for line in describe_layout(layout):
            print(f"  - {line}")


if __name__ == "__main__":
    _run()
