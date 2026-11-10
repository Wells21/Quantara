from __future__ import annotations

"""Check or repair asset-level live FFD/HMM continuation for manifest assets."""

import argparse
import json
import sys
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
LIVE_DIR = THIS_FILE.parent
V2_ROOT = THIS_FILE.parents[1]
for path in (LIVE_DIR, V2_ROOT, THIS_FILE.parents[2]):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from live_feature_continuation_guard import ensure_assets_live_feature_continuation  # noqa: E402


def _load_manifest() -> dict:
    path = LIVE_DIR / "live_manifest.json"
    if not path.exists():
        path = LIVE_DIR / "live_deployment_manifest.json"
    if not path.exists():
        raise FileNotFoundError("Missing portfolio_live/live_manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Live manifest must be a JSON object")
    return payload


def _manifest_assets(manifest: dict) -> list[str]:
    return sorted({str(row.get("asset") or "").upper() for row in manifest.get("selected_edges", []) or [] if row.get("asset")})


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or repair live FFD/HMM continuation for active assets.")
    parser.add_argument("--repair", action="store_true", help="Archive stale continuation outputs and rebuild from immutable research checkpoints.")
    parser.add_argument("--assets", nargs="*", help="Optional asset list. Default: all assets in live manifest.")
    parser.add_argument("--no-cache", action="store_true", help="Force fresh checks even when latest live timestamp is unchanged.")
    args = parser.parse_args()

    manifest = _load_manifest()
    assets = [str(asset).upper() for asset in (args.assets or _manifest_assets(manifest))]
    if not assets:
        raise SystemExit("No assets resolved from arguments or live manifest.")
    report = ensure_assets_live_feature_continuation(
        assets=assets,
        manifest=manifest,
        auto_repair=bool(args.repair),
        fail_closed=False,
        use_cache=not args.no_cache,
    )
    print("=" * 88)
    print("LIVE FEATURE CONTINUATION CHECK")
    print("=" * 88)
    print(f"Status: {report['status']}")
    print(f"Healthy: {report['healthy_assets']}")
    print(f"Blocked: {report['blocked_assets']}")
    print(f"Repair enabled: {bool(args.repair)}")
    print("Report: portfolio_live/outputs/live_feature_continuation/live_feature_continuation_health_report.json")
    for asset, record in report["assets"].items():
        print(
            f"{asset:<8} {record['status']:<14} live={record.get('live_bar_latest')} "
            f"ffd={record.get('ffd', {}).get('runtime_latest')} "
            f"hmm={record.get('hmm', {}).get('runtime_latest')} "
            f"repair={record.get('repair_attempted')}"
        )
    if report["blocked_assets"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
