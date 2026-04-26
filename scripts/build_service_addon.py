#!/usr/bin/env python3
"""Build a versioned install zip for service.kodi_mcp."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "packages" / "service.kodi_mcp"
ADDON_XML = SERVICE_DIR / "addon.xml"
DIST_DIR = ROOT / "dist"


def read_version() -> str:
    text = ADDON_XML.read_text(encoding="utf-8")
    match = re.search(r'<addon\b[^>]*\bversion="([^"]+)"', text)
    if not match:
        raise SystemExit("could not find service.kodi_mcp version in %s" % ADDON_XML)
    return match.group(1)


def build_zip(output_dir: Path = DIST_DIR) -> Path:
    version = read_version()
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / ("service.kodi_mcp-%s.zip" % version)
    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in sorted(SERVICE_DIR.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            archive_name = path.relative_to(SERVICE_DIR.parent).as_posix()
            zf.write(path, archive_name)

    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(DIST_DIR))
    args = parser.parse_args()

    zip_path = build_zip(Path(args.output_dir))
    print(zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
