from __future__ import annotations

import argparse
from pathlib import Path

TARGET_DIRS = ("feature_surgery", "model")


def locate_v2_root(explicit: Path | None = None) -> Path:
    """
    Locate the Quantara trading_models_v2 root.

    Accepts either:
      - the trading_models_v2 directory itself, or
      - the repository root containing trading_models_v2.

    Without --v2-root, searches upward from both the current working
    directory and this script's location.
    """
    if explicit is not None:
        root = explicit.expanduser().resolve()

        if root.name != "trading_models_v2":
            candidate = root / "trading_models_v2"
            if candidate.is_dir():
                root = candidate

        if not (root / "assets").is_dir():
            raise SystemExit(
                f"Could not find Quantara assets/ under supplied root: {root}"
            )
        return root

    starts = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    checked: set[Path] = set()

    for start in starts:
        for parent in (start, *start.parents):
            if parent in checked:
                continue
            checked.add(parent)

            if parent.name == "trading_models_v2" and (parent / "assets").is_dir():
                return parent

            candidate = parent / "trading_models_v2"
            if (candidate / "assets").is_dir():
                return candidate

    raise SystemExit(
        "Could not locate trading_models_v2 automatically.\n"
        "Run this script from inside the Quantara repository, or pass:\n"
        "  --v2-root <path-to-trading_models_v2-or-repo-root>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Create missing Quantara edge research artifact directories: "
            "artifacts/feature_surgery and artifacts/model."
        )
    )
    parser.add_argument(
        "--v2-root",
        type=Path,
        default=None,
        help=(
            "Path to trading_models_v2, or to the repository root that contains it. "
            "Normally unnecessary when run from inside the Quantara repository."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without modifying the filesystem.",
    )
    parser.add_argument(
        "--gitkeep",
        action="store_true",
        help=(
            "Also create .gitkeep inside target directories so Git can track "
            "otherwise-empty folders. Optional; not needed once lifecycle outputs exist."
        ),
    )
    args = parser.parse_args()

    v2_root = locate_v2_root(args.v2_root)
    assets_root = v2_root / "assets"

    # edge.yaml is the package identity marker. This avoids treating unrelated
    # folders under assets/*/edges/ as valid Quantara edge packages.
    edge_yamls = sorted(assets_root.glob("*/edges/*/edge.yaml"))

    if not edge_yamls:
        raise SystemExit(f"No Quantara edge packages found under: {assets_root}")

    existing_count = 0
    missing_count = 0
    created_count = 0
    gitkeep_created = 0

    print("=" * 96)
    print("QUANTARA V3 — ENSURE EDGE RESEARCH ARTIFACT DIRECTORIES")
    print("=" * 96)
    print(f"V2 root    : {v2_root}")
    print(f"Edges found: {len(edge_yamls)}")
    print(f"Mode       : {'DRY RUN' if args.dry_run else 'WRITE'}")
    print(f".gitkeep   : {'ENABLED' if args.gitkeep else 'DISABLED'}")
    print()

    for edge_yaml in edge_yamls:
        edge_root = edge_yaml.parent

        # Layout:
        # assets/<ASSET>/edges/<EDGE_NAME>/edge.yaml
        asset = edge_root.parent.parent.name
        edge_name = edge_root.name
        edge_id = f"{asset}/{edge_name}"

        print(f"[{edge_id}]")

        for dirname in TARGET_DIRS:
            target = edge_root / "artifacts" / dirname
            rel = target.relative_to(v2_root)

            if target.exists():
                if not target.is_dir():
                    raise RuntimeError(
                        "Safety stop: expected a directory but found a file at "
                        f"{target}"
                    )
                existing_count += 1
                print(f"  EXISTS        {rel}")
            else:
                missing_count += 1

                if args.dry_run:
                    print(f"  WOULD CREATE  {rel}")
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    created_count += 1
                    print(f"  CREATED       {rel}")

            if args.gitkeep:
                gitkeep = target / ".gitkeep"
                gitkeep_rel = gitkeep.relative_to(v2_root)

                if gitkeep.exists():
                    if not gitkeep.is_file():
                        raise RuntimeError(
                            "Safety stop: .gitkeep path exists but is not a file: "
                            f"{gitkeep}"
                        )
                    print(f"  GITKEEP EXISTS {gitkeep_rel}")
                elif args.dry_run:
                    print(f"  WOULD GITKEEP {gitkeep_rel}")
                else:
                    # target is guaranteed to exist here after mkdir above.
                    gitkeep.touch(exist_ok=False)
                    gitkeep_created += 1
                    print(f"  GITKEEP       {gitkeep_rel}")

        print()

    checked = len(edge_yamls) * len(TARGET_DIRS)

    print("-" * 96)
    print("SUMMARY")
    print("-" * 96)
    print(f"Edges scanned              : {len(edge_yamls)}")
    print(f"Target directories checked : {checked}")
    print(f"Already existed            : {existing_count}")
    print(f"Missing at scan time       : {missing_count}")

    if args.dry_run:
        print(f"Would create               : {missing_count}")
        if args.gitkeep:
            print("Git tracking                : .gitkeep dry-run enabled")
    else:
        print(f"Created                    : {created_count}")
        if args.gitkeep:
            print(f".gitkeep files created      : {gitkeep_created}")

    print()
    print("No existing files or directories were deleted, renamed, or overwritten.")
    print("Done.")


if __name__ == "__main__":
    main()
