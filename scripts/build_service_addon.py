#!/usr/bin/env python3
"""Build a versioned install zip for service.kodi_mcp."""

from __future__ import annotations

import argparse
from pathlib import Path

from build_addon import DIST_DIR, build_zip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DIST_DIR))
    args = parser.parse_args()

    zip_path = build_zip("service.kodi_mcp", Path(args.output_dir))
    print(zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
