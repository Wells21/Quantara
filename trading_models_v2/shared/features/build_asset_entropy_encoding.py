"""Freeze the AFML Ch.18 quantile boundaries and sigma per asset (D069).

Writes assets/{A}/bars/entropy/entropy_encoding_manifest.json from the asset's
full research history (the FFD manifest's source bars). Re-run only when the
research history itself changes (as for the FFD d), then re-export the edges.

usage: python shared/features/build_asset_entropy_encoding.py [ASSET ...]   (default: every asset with an FFD manifest)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

V2_ROOT = Path(__file__).resolve().parents[2]
if str(V2_ROOT) not in sys.path:
    sys.path.insert(0, str(V2_ROOT))

from shared.features.afml_ch18_entropy import build_encoding_manifest, encoding_manifest_path, validate_encoding  # noqa: E402


def main(argv: list[str]) -> None:
    assets = [a.upper() for a in argv] or sorted(p.parents[2].name for p in (V2_ROOT / "assets").glob("*/bars/ffd/ffd_manifest.json"))
    for asset in assets:
        manifest = validate_encoding(build_encoding_manifest(asset), asset)
        path = encoding_manifest_path(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"{asset}: rows {manifest['source_bars_rows']} sigma {manifest['sigma']:.3e} "
              f"zero-return share {manifest['zero_return_share']:.4f} -> {path.relative_to(V2_ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
