#!/usr/bin/env python3
"""Print absolute paths of demo directories missing a top-level manifest.json.

Usage:
    # Scan the default runtime_sessions/demos directory.
    /usr/bin/python3 tools/find_missing_manifests.py

    # Scan a specific directory containing demo_* subdirectories.
    /usr/bin/python3 tools/find_missing_manifests.py \
      --demos-root /path/to/runtime_sessions/demos

The tool is read-only and prints one absolute demo directory path per line.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEMOS_ROOT = REPO_ROOT / "runtime_sessions" / "demos"


def find_missing_manifests(demos_root: Path) -> list[Path]:
    root = demos_root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(
            f"--demos-root does not exist or is not a directory: {root}"
        )

    return [
        demo_dir.resolve()
        for demo_dir in sorted(root.glob("demo_*"), key=lambda path: path.name)
        if demo_dir.is_dir() and not (demo_dir / "manifest.json").is_file()
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demos-root",
        type=Path,
        default=DEFAULT_DEMOS_ROOT,
        help=f"directory containing demo_* directories (default: {DEFAULT_DEMOS_ROOT})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        missing = find_missing_manifests(args.demos_root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for demo_dir in missing:
        print(demo_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
