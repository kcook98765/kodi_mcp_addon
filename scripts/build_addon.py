#!/usr/bin/env python3
"""Build a versioned install zip for one packaged Kodi addon."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES_DIR = ROOT / "packages"
DIST_DIR = ROOT / "dist"


def read_version(addon_dir: Path) -> str:
    addon_xml = addon_dir / "addon.xml"
    text = addon_xml.read_text(encoding="utf-8")
    match = re.search(r'<addon\b[^>]*\bversion="([^"]+)"', text)
    if not match:
        raise SystemExit("could not find addon version in %s" % addon_xml)
    return match.group(1)


def build_zip(addon_id: str, output_dir: Path = DIST_DIR) -> Path:
    addon_id = str(addon_id or "").strip()
    addon_dir = PACKAGES_DIR / addon_id
    if not addon_dir.is_dir():
        raise SystemExit("addon package not found: %s" % addon_dir)

    version = read_version(addon_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / ("%s-%s.zip" % (addon_id, version))
    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for path in sorted(addon_dir.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            archive_name = path.relative_to(addon_dir.parent).as_posix()
            zf.write(path, archive_name)

    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("addon_id")
    parser.add_argument("--output-dir", default=str(DIST_DIR))
    args = parser.parse_args()

    zip_path = build_zip(args.addon_id, Path(args.output_dir))
    print(zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
