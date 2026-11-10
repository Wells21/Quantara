"""
V2 asset-level layout contract.

Blueprint purpose
-----------------
Bars are shared at the asset level, not rebuilt inside individual edges:

    assets/{ASSET}/raw/ticks/
    assets/{ASSET}/bars/standard_quote_tick/
    assets/{ASSET}/edges/{family}_{side}/

Rules enforced here
-------------------
1. Each asset owns its own raw tick folder.
2. Each asset owns its own shared standard_quote_tick bar folder.
3. Edges under an asset consume the asset's shared bars through runtime.py.
4. Edge-specific feature/event/model-row logic lives inside the edge package.
5. The dashboard must never manually engineer edge-specific features.

Run from ``trading_models_v2``:

    python ./shared/assets/asset_layout.py

or from repository root:

    python trading_models_v2/shared/assets/asset_layout.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Allow direct execution from repo root or from inside trading_models_v2.
_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
_REPO_ROOT = _THIS_FILE.parents[3]
for _path in (_V2_ROOT, _REPO_ROOT):
    path_str = str(_path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from shared.contracts import (  # noqa: E402
    AuditFinding,
    AuditReport,
    AuditSeverity,
    BAR_MODE_STANDARD_QUOTE_TICK,
    EdgeIdentity,
)


class AssetLayoutError(RuntimeError):
    """Raised when an asset/edge path violates the V2 layout contract."""


@dataclass(frozen=True)
class AssetLayout:
    """Resolved V2 asset paths.

    Parameters
    ----------
    v2_root:
        Path to ``trading_models_v2``.
    asset:
        Asset name, e.g. XAUUSD, BTCUSD, USTECH.
    """

    v2_root: Path
    asset: str

    @property
    def asset_root(self) -> Path:
        return self.v2_root / "assets" / self.asset

    @property
    def raw_root(self) -> Path:
        return self.asset_root / "raw"

    @property
    def raw_tick_root(self) -> Path:
        """The vendor feed as downloaded (Dukascopy). Only curation and parity checks read it."""
        return self.raw_root / "ticks"

    @property
    def curated_root(self) -> Path:
        return self.asset_root / "curated"

    @property
    def tick_root(self) -> Path:
        """Ticks every research consumer reads (bars, labels, Task 2A, vol cache).

        config/asset_data_policy.json decides: tick_source=curated -> assets/{ASSET}/curated/ticks (quality-filtered,
        roll-adjusted where configured; shared/data/tick_curation.py), raw -> raw/ticks. Fails closed when the policy
        asks for curated ticks that have not been built, so research can never silently fall back to raw quotes.
        """
        import json

        policy_path = self.v2_root / "config" / "asset_data_policy.json"
        source = "raw"
        if policy_path.exists():
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            merged = dict(policy.get("default", {}))
            merged.update(policy.get("assets", {}).get(self.asset, {}))
            source = str(merged.get("tick_source", "raw"))
        if source == "raw":
            return self.raw_tick_root
        if source != "curated":
            raise AssetLayoutError(f"{self.asset}: unknown tick_source {source!r} in {policy_path}")
        if not (self.curated_root / "curation_manifest.json").exists():
            raise AssetLayoutError(
                f"{self.asset}: config/asset_data_policy.json selects curated ticks but {self.curated_root} has no "
                "curation_manifest.json. Run: python ./shared/data/tick_curation.py curate --assets " + self.asset
            )
        return self.curated_root / "ticks"

    @property
    def bars_root(self) -> Path:
        return self.asset_root / "bars"

    @property
    def standard_quote_tick_root(self) -> Path:
        return self.bars_root / BAR_MODE_STANDARD_QUOTE_TICK

    @property
    def edges_root(self) -> Path:
        return self.asset_root / "edges"

    @property
    def standard_quote_tick_bars_parquet(self) -> Path:
        return self.standard_quote_tick_root / f"{self.asset.lower()}_standard_quote_tick_bars.parquet"

    @property
    def standard_quote_tick_bars_csv(self) -> Path:
        return self.standard_quote_tick_root / f"{self.asset.lower()}_standard_quote_tick_bars.csv"

    @property
    def bar_manifest_json(self) -> Path:
        return self.standard_quote_tick_root / "bar_manifest.json"

    def edge_dir(self, family: str, side_name: str) -> Path:
        identity = EdgeIdentity(asset=self.asset, family=family, side_name=side_name, side_value=1 if side_name == "long" else -1)
        report = identity.validate()
        if report.failed:
            raise AssetLayoutError("Invalid edge identity: " + "; ".join(report.fail_messages()))
        return self.edges_root / f"{family}_{side_name}"

    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.asset_root,
            self.raw_root,
            self.tick_root,
            self.bars_root,
            self.standard_quote_tick_root,
            self.edges_root,
        )

    def create_directories(self) -> None:
        for path in self.required_directories():
            path.mkdir(parents=True, exist_ok=True)

    def validate_existing(self, require_tick_folder: bool = False, require_bar_folder: bool = False) -> AuditReport:
        findings: list[AuditFinding] = []

        if require_tick_folder and not self.tick_root.exists():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "ASSET_TICK_FOLDER_MISSING",
                    f"Missing asset tick folder: {self.tick_root}",
                )
            )

        if require_bar_folder and not self.standard_quote_tick_root.exists():
            findings.append(
                AuditFinding(
                    AuditSeverity.FAIL,
                    "ASSET_BAR_FOLDER_MISSING",
                    f"Missing asset standard_quote_tick bar folder: {self.standard_quote_tick_root}",
                )
            )

        if not findings:
            findings.append(
                AuditFinding(
                    AuditSeverity.PASS,
                    "ASSET_LAYOUT_OK",
                    f"Asset layout is valid for {self.asset}.",
                )
            )

        return AuditReport(name=f"asset_layout::{self.asset}", findings=tuple(findings))


def get_v2_root_from_current_file() -> Path:
    return _V2_ROOT


def get_asset_layout(asset: str, v2_root: str | Path | None = None) -> AssetLayout:
    root = Path(v2_root).resolve() if v2_root is not None else get_v2_root_from_current_file()
    asset_clean = asset.strip().upper()
    if not asset_clean:
        raise AssetLayoutError("asset must be non-empty")
    return AssetLayout(v2_root=root, asset=asset_clean)


def describe_layout(layout: AssetLayout) -> list[str]:
    return [
        f"asset_root={layout.asset_root.as_posix()}",
        f"tick_root={layout.tick_root.as_posix()}",
        f"standard_quote_tick_root={layout.standard_quote_tick_root.as_posix()}",
        f"edges_root={layout.edges_root.as_posix()}",
        f"bars_parquet={layout.standard_quote_tick_bars_parquet.as_posix()}",
        f"bar_manifest={layout.bar_manifest_json.as_posix()}",
    ]


def validate_assets_layout(assets: Iterable[str], v2_root: str | Path | None = None) -> tuple[AuditReport, ...]:
    return tuple(get_asset_layout(asset, v2_root=v2_root).validate_existing() for asset in assets)


def _run_self_test() -> None:
    layout = get_asset_layout("XAUUSD")

    if layout.asset != "XAUUSD":
        raise SystemExit(f"❌ Expected normalized asset XAUUSD, got {layout.asset}")

    expected_suffixes = {
        "tick_root": "assets/XAUUSD/raw/ticks",
        "bar_root": "assets/XAUUSD/bars/standard_quote_tick",
        "edges_root": "assets/XAUUSD/edges",
        "trend_long": "assets/XAUUSD/edges/trend_long",
    }

    actuals = {
        "tick_root": layout.tick_root.as_posix(),
        "bar_root": layout.standard_quote_tick_root.as_posix(),
        "edges_root": layout.edges_root.as_posix(),
        "trend_long": layout.edge_dir("trend", "long").as_posix(),
    }

    for key, suffix in expected_suffixes.items():
        if not actuals[key].endswith(suffix):
            raise SystemExit(f"❌ {key} path mismatch. Expected suffix {suffix!r}, got {actuals[key]!r}")

    report = layout.validate_existing()
    if report.failed:
        raise SystemExit("❌ Layout validation unexpectedly failed: " + "; ".join(report.fail_messages()))

    print("✅ shared.assets.asset_layout self-test passed.")
    print("Asset-level rule: raw ticks -> shared standard_quote_tick bars -> side-specialized edges")
    for line in describe_layout(layout):
        print(f"  - {line}")


if __name__ == "__main__":
    _run_self_test()
